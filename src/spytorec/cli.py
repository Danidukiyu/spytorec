"""SpytoRec CLI entry point — main recording engine."""

import os
import sys
import time
import signal
import atexit
import argparse
import subprocess
import threading
import logging
from collections import deque

import sounddevice as sd
from spotipy.exceptions import SpotifyException
from rich.text import Text
from rich.panel import Panel
from rich.table import Table
from rich.live import Live

from spytorec import state
from spytorec.config import load_config, setup_logging, console
from spytorec.utils import resolve_path, KBHit, LOCK_FILE_PATH
from spytorec.audio import (
    init_audio_config, start_monitor, stop_monitor_stream,
    start_writer_thread
)
from spytorec.recording import (
    get_final_path, finalize, safely_stop_ffmpeg, watchdog_worker
)
from spytorec.spotify.selector import get_source
from spytorec.blocklist import load_blocklist, is_track_blocked
from spytorec.webhooks import send_webhook
from spytorec.hardware import discover_hardware
from spytorec.ui import (
    build_dashboard, build_history_table,
    SP_GREEN, SP_GREEN_DIM, SP_GREY, SP_DARK
)


def cleanup_resources():
    """Clean up all resources before exit."""
    logging.info("Cleaning up resources...")
    state.stop_event.set()
    stop_monitor_stream()

    with state.ffmpeg_lock:
        if state.ffmpeg_process and state.ffmpeg_process.poll() is None:
            try:
                safely_stop_ffmpeg(state.ffmpeg_process)
            except Exception:
                pass
        state.ffmpeg_process = None

    for handler in logging.getLogger().handlers:
        try:
            handler.close()
        except Exception:
            pass

    try:
        if LOCK_FILE_PATH.exists():
            LOCK_FILE_PATH.unlink()
    except Exception:
        pass


def signal_handler(signum, frame):
    """Handle system signals gracefully."""
    print(f"\nReceived signal {signum}, shutting down...")
    state.stop_event.set()
    sys.exit(0)


def main():
    global cfg

    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    atexit.register(cleanup_resources)

    # Load configuration
    cfg = load_config()
    setup_logging(cfg)
    init_audio_config(cfg)

    # Parse arguments
    parser = argparse.ArgumentParser(description=f'SpytoRec v{state.SCRIPT_VERSION} - Spotify Recording Tool')
    parser.add_argument('--ffmpeg', default='ffmpeg', help='Path to ffmpeg executable')
    args = parser.parse_args()

    # Load blocklist
    blocklist = load_blocklist()
    if blocklist:
        console.print(f"[green]Loaded {len(blocklist)} blocklist rules.[/green]")

    # Setup directories
    out_dir = resolve_path(cfg['Recording'].get('output_directory', 'Recordings'))
    naming_format = cfg['Naming'].get('naming_format', '{track_no}. {artist} - {title}')
    output_format = cfg['Recording'].get('output_format', 'flac').lower()
    if output_format not in ['flac', 'mp3']:
        output_format = 'flac'

    # Validate output directory
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        test_file = out_dir / ".perms_check"
        test_file.touch()
        test_file.unlink()
        logging.info(f"Output directory ready: {out_dir}")
    except Exception as e:
        console.print(f"[bold red]Output Path Unwritable: {e}[/bold red]")
        time.sleep(5)
        sys.exit(1)

    # Hardware initialisation
    hw_ready = cfg.get('Recording', 'ffmpeg_name', fallback='') != ''

    if hw_ready:
        try:
            saved_idx = int(cfg['Recording'].get('device_id'))
            devices = sd.query_devices()
            if saved_idx >= len(devices) or devices[saved_idx]['max_input_channels'] == 0:
                console.print("[yellow]Previously saved audio device not found. Re-scanning...[/yellow]")
                hw_ready = False
        except Exception:
            console.print("[yellow]Could not validate saved device. Re-scanning...[/yellow]")
            hw_ready = False

    if hw_ready:
        console.print("[yellow]Found existing config. Press SPACE in 5s to re-scan hardware...[/yellow]")
        start_time = time.time()
        rescan = False

        kb = KBHit()
        kb.set_cbreak()
        try:
            while time.time() - start_time < 5:
                if kb.kbhit():
                    if kb.getch() == ' ':
                        rescan = True
                        break
                time.sleep(0.1)
        finally:
            kb.set_normal_term()

        if rescan:
            hw_name, hw_idx, hw_sr, hw_ch = discover_hardware(args.ffmpeg, cfg)
        else:
            hw_name = cfg['Recording'].get('ffmpeg_name')
            hw_idx = int(cfg['Recording'].get('device_id'))
            hw_sr = int(cfg['Recording'].get('sample_rate'))
            hw_ch = int(cfg['Recording'].get('channels'))
    else:
        hw_name, hw_idx, hw_sr, hw_ch = discover_hardware(args.ffmpeg, cfg)

    # Track source: the platform's OS media app on macOS and Windows, the Web API elsewhere
    try:
        source = get_source(cfg)
    except Exception as e:
        console.print(f"[red]Could not initialise a track source: {e}[/red]")
        console.print("Make sure the Spotify desktop app is running and playing. On Linux, add credentials under [SpotifyAPI].")
        time.sleep(5)
        sys.exit(1)

    console.print(f"[green]Track source: {source.name}[/green]")

    # Start background watchdog worker
    watchdog_thread = threading.Thread(target=watchdog_worker, args=(cfg,), daemon=True)
    watchdog_thread.start()

    # Setup FFmpeg logging
    ff_log_ptr = subprocess.DEVNULL
    ff_log_file = None

    if cfg['Diagnostics'].getboolean('enable_logging'):
        ff_log_file = resolve_path(cfg['Diagnostics'].get('ffmpeg_log_file', 'spyto_ffmpeg.log'))
        try:
            ff_log_ptr = open(ff_log_file, "a", encoding='utf-8')
        except Exception as e:
            logging.warning(f"Could not open FFmpeg log file: {e}")
            ff_log_ptr = subprocess.DEVNULL

    # Main loop variables
    current_track = None
    current_id = None
    temp_file = None
    last_error_time = 0
    error_recovery_delay = 2
    last_file_size_check = 0
    cached_file_size = 0.0

    # Session tracking
    session_start_time = time.time()
    session_tracks_ok = 0
    session_total_bytes = 0
    track_history = deque(maxlen=8)

    def save_current(track, tmp):
        """Tags and files a finished recording, and records it in the session.

        The one path a completed recording takes, whether the track changed,
        playback stopped, or SpytoRec is shutting down.
        """
        nonlocal session_tracks_ok, session_total_bytes

        result = finalize(tmp, out_dir, track, naming_format, output_format, cfg)
        artist = track['artists'][0]['name']

        if result['ok']:
            session_tracks_ok += 1
            session_total_bytes += int(result['size_mb'] * 1024 * 1024)
            track_history.append({'name': track['name'], 'artist': artist, 'size': result['size_mb'], 'status': 'ok'})
            logging.info(f"Saved: {track['name']}")
            console.print(f"[green]\u2713 Saved: {track['name']}[/green]")
            if cfg['Webhooks'].getboolean('notify_on_track_saved'):
                send_webhook(f"✅ **Saved:** `{artist} - {track['name']}` ({result['size_mb']} MB)", cfg)
        else:
            track_history.append({'name': track['name'], 'artist': artist, 'size': 0, 'status': 'fail'})
            state.failed_recordings.append(track['name'])
            console.print(f"[red]\u2717 Failed to save: {track['name']}[/red]")

    # Initialize recording params
    sr = int(cfg['Recording'].get('sample_rate', '48000'))
    ch = int(cfg['Recording'].get('channels', '2'))
    bit_depth = cfg['Recording'].get('bit_depth', '24')

    # Initialize keyboard listener
    kb = KBHit()
    kb.set_cbreak()

    # Start audio writer background thread
    start_writer_thread()

    # Start continuous audio monitor once at startup
    safe_mode = cfg['Recording'].getboolean('force_safe_mode')
    sr = 44100 if safe_mode else hw_sr
    ch = 2 if safe_mode else hw_ch
    start_monitor(hw_idx, sr, ch, cfg)

    state.set_state(state.STATE_MONITORING)

    try:
        with Live(Panel(Text("Waiting for Spotify...", style="yellow")), refresh_per_second=10) as live:
            while not state.stop_event.is_set():
                try:
                    # Check for error state set by watchdog and recover
                    if state.get_state() == state.STATE_ERROR:
                        logging.info("Main loop detected error state, attempting recovery...")
                        with state.ffmpeg_lock:
                            if state.ffmpeg_process and state.ffmpeg_process.poll() is not None:
                                state.ffmpeg_process = None
                            elif state.ffmpeg_process:
                                safely_stop_ffmpeg(state.ffmpeg_process)
                                state.ffmpeg_process = None
                        if temp_file and temp_file.exists():
                            try:
                                temp_file.unlink()
                            except Exception:
                                pass
                        temp_file = None
                        current_track = None
                        current_id = None
                        state.current_track_id_ref = None
                        state.set_state(state.STATE_RECOVERING)
                        time.sleep(2)
                        state.set_state(state.STATE_MONITORING)
                        continue

                    # Get playback info from the active track source
                    playback = source.get_playback()

                    if playback and playback.get('is_playing') and playback.get('item'):
                        track = playback['item']
                        track_id = track['id']

                        # Handle track change
                        if track_id != current_id:
                            # Stop current recording if any
                            if state.ffmpeg_process:
                                state.set_state(state.STATE_SWITCHING)

                                with state.ffmpeg_lock:
                                    safely_stop_ffmpeg(state.ffmpeg_process)
                                    state.ffmpeg_process = None

                                if temp_file and current_track:
                                    save_current(current_track, temp_file)
                                    temp_file = None

                            # Check for duplicates before starting recording
                            _, predicted_path = get_final_path(out_dir, track, naming_format, output_format, cfg)

                            # 1. Check blocklist
                            is_blocked, block_reason = is_track_blocked(track, blocklist)
                            if is_blocked:
                                console.print(f"[yellow]Skipping Blocked Track: {track['name']} ({block_reason})[/yellow]")
                                logging.info(f"Skipped track {track['name']}: {block_reason}")
                                track_history.append({'name': track['name'], 'artist': track['artists'][0]['name'], 'size': 0, 'status': 'skip'})
                                state.set_state(state.STATE_SKIPPED)
                                current_track = track
                                current_id = track_id
                                state.current_track_id_ref = track_id
                                temp_file = None
                                continue

                            # 2. Check duplicate
                            if predicted_path.exists() and not cfg['Recording'].getboolean('overwrite_existing'):
                                console.print(f"[yellow]Skipping Duplicate: {track['name']} - {track['artists'][0]['name']}[/yellow]")
                                logging.info(f"Skipping duplicate: {predicted_path.name}")
                                track_history.append({'name': track['name'], 'artist': track['artists'][0]['name'], 'size': 0, 'status': 'skip'})
                                state.set_state(state.STATE_SKIPPED)
                                current_track = track
                                current_id = track_id
                                state.current_track_id_ref = track_id
                                temp_file = None
                                continue

                            # Start new recording
                            safe_mode = cfg['Recording'].getboolean('force_safe_mode')
                            sr = 44100 if safe_mode else hw_sr
                            ch = 2 if safe_mode else hw_ch
                            bit_depth = cfg['Recording'].get('bit_depth', '24')

                            fmt_map = {'16': 's16', '24': 's32', '32': 's32'}
                            sample_fmt = fmt_map.get(bit_depth, 's16')

                            # Clear old audio chunks
                            with state.audio_queue.mutex:
                                state.audio_queue.queue.clear()

                            # Create temp file
                            temp_file = out_dir / f".tmp_{int(time.time())}.{output_format}"

                            # Build FFmpeg command for piped raw PCM
                            cmd = [
                                args.ffmpeg, '-y',
                                '-f', 'f32le',
                                '-ar', str(sr),
                                '-ac', str(min(2, ch)),
                                '-i', 'pipe:0'
                            ]

                            if output_format == 'mp3':
                                cmd.extend(['-c:a', 'libmp3lame', '-b:a', '320k', str(temp_file)])
                            else:
                                cmd.extend([
                                    '-sample_fmt', sample_fmt,
                                    '-c:a', 'flac', '-compression_level', '8',
                                    str(temp_file)
                                ])

                            # Start FFmpeg
                            try:
                                with state.ffmpeg_lock:
                                    state.ffmpeg_process = subprocess.Popen(
                                        cmd,
                                        stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL,
                                        stderr=ff_log_ptr
                                    )

                                state.watchdog_proc_ref = state.ffmpeg_process
                                state.watchdog_file_ref = temp_file
                                current_track = track
                                current_id = track_id
                                state.current_track_id_ref = track_id

                                state.set_state(state.STATE_RECORDING)
                                logging.info(f"Started recording: {track['name']} - {track['artists'][0]['name']}")

                            except Exception as e:
                                logging.error(f"Failed to start FFmpeg: {e}")
                                if temp_file and temp_file.exists():
                                    try:
                                        temp_file.unlink()
                                    except Exception:
                                        pass
                                temp_file = None
                                state.set_state(state.STATE_ERROR, f"FFmpeg error: {e}")
                                continue

                        # Build UI display
                        now = time.time()
                        if now - last_file_size_check > 1.0:
                            if temp_file and temp_file.exists():
                                cached_file_size = round(temp_file.stat().st_size / (1024 ** 2), 2)
                            else:
                                cached_file_size = 0.0
                            last_file_size_check = now
                        file_size = cached_file_size

                        live.update(build_dashboard(
                            playback, track, file_size,
                            session_tracks_ok, session_total_bytes,
                            session_start_time, track_history, state.failed_recordings,
                            sr, ch, bit_depth, hw_name, output_format, out_dir, cfg
                        ))

                    else:
                        # Not playing - cleanup if needed
                        if state.ffmpeg_process:
                            state.set_state(state.STATE_STOPPING)

                            with state.ffmpeg_lock:
                                safely_stop_ffmpeg(state.ffmpeg_process)
                                state.ffmpeg_process = None

                            if temp_file and current_track:
                                save_current(current_track, temp_file)

                            temp_file = None
                            current_track = None
                            current_id = None
                            state.current_track_id_ref = None
                            state.set_state(state.STATE_IDLE)

                        live.update(build_dashboard(
                            playback, None, 0.0,
                            session_tracks_ok, session_total_bytes,
                            session_start_time, track_history, state.failed_recordings,
                            sr, ch, bit_depth, hw_name, output_format, out_dir, cfg
                        ))

                    # Adaptive polling
                    if playback and playback.get('is_playing'):
                        time.sleep(0.5)
                    else:
                        time.sleep(2.0)

                    # Keyboard Polling
                    if kb.kbhit():
                        key = kb.getch()
                        if key == 'q':
                            logging.info("User initiated graceful quit")
                            state.stop_event.set()
                        elif key == 'd':
                            cfg['Debug']['show_debug_overlay'] = str(not cfg['Debug'].getboolean('show_debug_overlay'))
                        elif key == 'f':
                            cfg['Recording']['force_safe_mode'] = str(not cfg['Recording'].getboolean('force_safe_mode'))
                            console.print("[yellow]Safe Mode toggled (will apply next track)[/yellow]")
                        elif key == 's' and current_track:
                            track_name = current_track['name']
                            track_skip_id = current_track.get('id', '')
                            logging.info(f"User skipped track: {track_name}")
                            try:
                                blocklist_path = resolve_path('blocklist.txt')
                                with open(blocklist_path, 'a', encoding='utf-8') as bl:
                                    bl.write(f"id:{track_skip_id}\n")
                                blocklist.append(f"id:{track_skip_id}")
                                console.print(f"[yellow]Skipped & blocklisted: {track_name}[/yellow]")
                            except Exception as e:
                                logging.error(f"Failed to update blocklist: {e}")
                            with state.ffmpeg_lock:
                                if state.ffmpeg_process:
                                    safely_stop_ffmpeg(state.ffmpeg_process)
                                    state.ffmpeg_process = None
                            if temp_file and temp_file.exists():
                                try:
                                    temp_file.unlink()
                                except Exception:
                                    pass
                            temp_file = None
                            track_history.append({'name': track_name, 'artist': current_track['artists'][0]['name'], 'size': 0, 'status': 'skip'})
                            state.set_state(state.STATE_SKIPPED)
                            current_track = None
                            current_id = None
                            state.current_track_id_ref = None

                except SpotifyException as e:
                    logging.error(f"Spotify API error: {e}")
                    if hasattr(e, 'http_status') and e.http_status == 401:
                        console.print("[red]Spotify authentication expired. Please restart to re-authenticate.[/red]")
                        logging.error("Spotify auth token expired (401). Manual restart required.")
                        time.sleep(5)
                        state.stop_event.set()
                    else:
                        last_error_time = state.handle_error_recovery(e, last_error_time, error_recovery_delay)

                except Exception as e:
                    logging.exception(f"Main loop error: {e}")
                    last_error_time = state.handle_error_recovery(e, last_error_time, error_recovery_delay)

    finally:
        # Finalise a recording still in progress at shutdown (e.g. 'q' mid-track),
        # otherwise its .tmp file is left untagged and unnamed.
        with state.ffmpeg_lock:
            if state.ffmpeg_process:
                safely_stop_ffmpeg(state.ffmpeg_process)
                state.ffmpeg_process = None
        if temp_file and current_track and temp_file.exists():
            try:
                save_current(current_track, temp_file)
            except Exception as e:
                logging.error(f"Finalise-on-exit failed: {e}")
            temp_file = None

        if ff_log_ptr != subprocess.DEVNULL:
            try:
                ff_log_ptr.close()
            except Exception:
                pass

        try:
            source.close()
        except Exception:
            pass

        kb.set_normal_term()
        watchdog_thread.join(timeout=2)

        # Post-Session Summary
        elapsed = time.time() - session_start_time
        elapsed_str = f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m {int(elapsed % 60)}s"
        total_mb = round(session_total_bytes / (1024 ** 2), 1)

        summary = Table(title=f"[{SP_GREEN}]SpytoRec Session Summary[/{SP_GREEN}]", show_header=False, expand=True)
        summary.add_column("Stat", style=SP_GREY)
        summary.add_column("Value", style=SP_GREEN_DIM)

        summary.add_row("Duration:", elapsed_str)
        summary.add_row("Tracks:", f"{session_tracks_ok} recorded, {len(state.failed_recordings)} failed")
        summary.add_row("Total Size:", f"{total_mb} MB")
        summary.add_row("Format:", f"{output_format.upper()} {bit_depth}-bit / {sr}Hz")
        summary.add_row("Output:", str(out_dir))

        console.print("\n")
        console.print(Panel(summary, border_style=SP_GREEN_DIM))

        if track_history:
            breakdown = build_history_table(track_history)
            if breakdown:
                console.print(breakdown)

        if state.failed_recordings:
            console.print(f"\n[yellow]Recording session completed with {len(state.failed_recordings)} failed tracks[/yellow]")
        else:
            console.print(f"\n[{SP_GREEN}]Recording session completed successfully![/{SP_GREEN}]")

        if cfg['Webhooks'].getboolean('notify_on_session_end'):
            fail_str = f" ({len(state.failed_recordings)} failed)" if state.failed_recordings else ""
            msg = (
                f"🏁 **Session Completed**\n"
                f"- **Duration:** {elapsed_str}\n"
                f"- **Tracks:** {session_tracks_ok} recorded{fail_str}\n"
                f"- **Total Size:** {total_mb} MB"
            )
            send_webhook(msg, cfg)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        state.stop_event.set()
        console.print("\n[yellow]Shutdown complete.[/yellow]")
        sys.exit(0)
    except Exception as e:
        console.print(f"\n[red]Fatal error: {e}[/red]")
        logging.exception("Fatal error")
        sys.exit(1)
