"""SpytoRec CLI entry point — main recording engine."""

import sys
import time
import signal
import atexit
import argparse
import threading
import logging
import warnings
from collections import deque

warnings.filterwarnings("ignore", module="soundcard")

import soundcard as sc
from spotipy.exceptions import SpotifyException
from rich.text import Text
from rich.panel import Panel
from rich.table import Table
from rich.live import Live

from spytorec import state, coreaudio
from spytorec.config import load_config, setup_logging, console
from spytorec.utils import resolve_path, KBHit, LOCK_FILE_PATH
from spytorec.audio_process import (
    shutdown_audio_process, read_meters, read_events, check_audio_health, find_device
)
from spytorec.session import RecordingSession, effective_output_format, playback_lead_ms
from spytorec.boundary import settings_from_config
from spytorec.recording import (
    get_final_path, finalize, watchdog_worker, BackgroundFinalizer
)
from spytorec.spotify.selector import get_source
from spytorec.blocklist import load_blocklist, is_track_blocked
from spytorec.webhooks import send_webhook
from spytorec.hardware import discover_hardware, capture_rate
from spytorec.ui import (
    build_dashboard, build_history_table,
    SP_GREEN, SP_GREEN_DIM, SP_GREY
)


def cleanup_resources():
    """Clean up all resources before exit."""
    logging.info("Cleaning up resources...")
    state.stop_event.set()
    shutdown_audio_process()

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
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    atexit.register(cleanup_resources)

    # Load configuration
    cfg = load_config()
    setup_logging(cfg)

    import multiprocessing
    multiprocessing.freeze_support()

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
    requested_format = cfg['Recording'].get('output_format', 'flac')
    output_format = effective_output_format(requested_format)
    if requested_format.lower() == 'mp3':
        console.print('[yellow]MP3 capture is unavailable. Saving this session as FLAC.[/yellow]')

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
            saved_id = cfg['Recording'].get('device_id')
            saved_name = cfg['Recording'].get('ffmpeg_name')
            mics = sc.all_microphones(include_loopback=True)
            if find_device(mics, saved_id, saved_name) is None:
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
            hw_idx = cfg['Recording'].get('device_id')
            hw_sr = capture_rate(cfg['Recording'].get('sample_rate'), hw_name)
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

    # Main loop variables
    current_track = None
    current_id = None
    last_error_time = 0
    error_recovery_delay = 2
    last_file_size_check = 0
    cached_file_size = 0.0

    # Session tracking
    session_start_time = time.time()
    session_tracks_ok = 0
    session_total_bytes = 0
    track_history = deque(maxlen=8)

    def finalize_track(track, tmp):
        """Tags and files one recording, on the finaliser thread."""
        result = finalize(tmp, out_dir, track, naming_format, output_format, cfg)

        if result['ok'] and cfg['Webhooks'].getboolean('notify_on_track_saved'):
            send_webhook(
                f"✅ **Saved:** `{track['artists'][0]['name']} - {track['name']}` "
                f"({result['size_mb']} MB)", cfg
            )

        return result

    finalizer = BackgroundFinalizer(finalize_track)
    finalizer.start()

    def report_failure(track, recovery_path, error):
        track_history.append({'name': track['name'], 'artist': track['artists'][0]['name'],
                              'size': 0, 'status': 'fail'})
        state.failed_recordings.append(track['name'])
        console.print(f"\u2717 Failed: {track['name']} ({error})", style='red', markup=False)
        if recovery_path:
            console.print(f'Recording retained: {recovery_path}', markup=False)

    def drain_results():
        """Applies finished saves to the session tally and history."""
        nonlocal session_tracks_ok, session_total_bytes

        for track, result in finalizer.drain():
            artist = track['artists'][0]['name']

            if result['ok']:
                session_tracks_ok += 1
                session_total_bytes += int(result['size_mb'] * 1024 * 1024)
                track_history.append({'name': track['name'], 'artist': artist,
                                      'size': result['size_mb'], 'status': 'ok'})
                logging.info(f"Saved: {track['name']}")
                console.print(f"[green]\u2713 Saved: {track['name']}[/green]")
            else:
                report_failure(track, result.get('recovery_path'),
                               result.get('error', 'Could not save recording'))

    # Initialize recording params
    bit_depth = cfg['Recording'].get('bit_depth', '24')

    # Initialize keyboard listener
    kb = KBHit()
    kb.set_cbreak()

    # Start process-isolated audio pipeline
    sr, ch = hw_sr, hw_ch
    smooth_meter = cfg['UI'].getboolean('smooth_meter_animation', fallback=True)

    # The device's own volume attenuates what the loopback captures
    if cfg['Recording'].getboolean('force_unity_gain', fallback=True):
        for change in coreaudio.force_unity_gain(hw_name):
            logging.info(f"Capture gain: '{hw_name}' {change}")
            console.print(f"[yellow]Capture gain corrected: '{hw_name}' {change}[/yellow]")

    session = RecordingSession(out_dir, {
        'mic_id': hw_idx, 'sr': sr, 'ch': ch, 'smooth_meter': smooth_meter,
        'mic_name': hw_name, 'boundary': settings_from_config(cfg),
    }, finalizer.submit, report_failure)

    try:
        session.start_pipeline()
        state.set_state(state.STATE_MONITORING)
        last_poll_done = time.monotonic()
        with Live(Panel(Text("Waiting for Spotify...", style="yellow")), refresh_per_second=10) as live:
            while not state.stop_event.is_set():
                try:
                    # Update metering from audio process
                    meters = read_meters()
                    if meters:
                        state.smoothed_rms_l, state.smoothed_rms_r = meters[0], meters[1]
                        state.peak_l, state.peak_r = meters[2], meters[3]
                        state.raw_l, state.raw_r = meters[4], meters[5]
                        state.last_heartbeat = time.time()

                    # Where the audio process opened each recording
                    for _, replayed_ms, waited, delay_ms in read_events():
                        how = "held for the boundary" if waited else "replayed from the buffer"
                        logging.info(f"Opened recording: {replayed_ms:.0f}ms {how}, "
                                     f"capture delay now {delay_ms:.0f}ms")

                    # Check for error state set by watchdog and recover
                    if state.get_state() == state.STATE_ERROR:
                        logging.info("Main loop detected error state, attempting recovery...")
                        current_track = None
                        current_id = None
                        state.current_track_id_ref = None
                        state.set_state(state.STATE_RECOVERING)
                        session.recover()
                        state.set_state(state.STATE_MONITORING)
                        last_poll_done = time.monotonic()
                        continue

                    check_audio_health()

                    # File anything the finaliser thread has completed
                    drain_results()

                    # Get playback info from the active track source, dated
                    poll_started = time.monotonic()
                    playback = source.get_playback()
                    poll_done = time.monotonic()
                    poll_gap_ms = (poll_done - last_poll_done) * 1000
                    last_poll_done = poll_done

                    if playback and playback.get('is_playing') and playback.get('item'):
                        track = playback['item']
                        track_id = track['id']

                        # Handle track change
                        if track_id != current_id:
                            lead_ms = playback_lead_ms(playback, poll_started, poll_gap_ms)
                            # Stop current recording if any
                            if state.is_recording:
                                state.set_state(state.STATE_SWITCHING)
                                session.finish_track(lead_ms)

                            # Check for duplicates before starting recording
                            _, predicted_path = get_final_path(out_dir, track, naming_format, output_format, cfg)

                            # 1. Check blocklist
                            is_blocked, block_reason = is_track_blocked(track, blocklist)
                            if is_blocked:
                                console.print(f"[yellow]Skipping Blocked Track: {track['name']} ({block_reason})[/yellow]")
                                logging.info(f"Skipped track {track['name']}: {block_reason}")
                                if not (track_history and track_history[-1]['name'] == track['name'] and track_history[-1]['status'] == 'skip'):
                                    track_history.append({'name': track['name'], 'artist': track['artists'][0]['name'], 'size': 0, 'status': 'skip'})
                                state.set_state(state.STATE_SKIPPED)
                                current_track = track
                                current_id = track_id
                                state.current_track_id_ref = track_id
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
                                continue

                            # Start new recording
                            bit_depth = cfg['Recording'].get('bit_depth', '24')

                            lead_ms = playback_lead_ms(playback, poll_started, poll_gap_ms)

                            try:
                                session.start_track(track, bit_depth, lead_ms)
                                current_track = track
                                current_id = track_id
                                state.current_track_id_ref = track_id

                                state.set_state(state.STATE_RECORDING)
                                logging.info(
                                    f"Started recording: {track['name']} - {track['artists'][0]['name']} "
                                    f"(reported {lead_ms:.0f}ms in)"
                                )

                            except Exception as e:
                                logging.error(f"Failed to start recording: {e}")
                                state.set_state(state.STATE_ERROR, f"Recording error: {e}")
                                continue

                        # Build UI display
                        now = time.time()
                        if now - last_file_size_check > 1.0:
                            if session.path and session.path.exists():
                                cached_file_size = round(session.path.stat().st_size / (1024 ** 2), 2)
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
                        if state.is_recording:
                            state.set_state(state.STATE_STOPPING)
                            session.finish_track(lead_ms=0)
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
                        time.sleep(1.0)

                    # Keyboard Polling
                    if kb.kbhit():
                        key = kb.getch()
                        if key == 'q':
                            logging.info("User initiated graceful quit")
                            state.stop_event.set()
                        elif key == 'd':
                            cfg['Debug']['show_debug_overlay'] = str(not cfg['Debug'].getboolean('show_debug_overlay'))
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
                            session.finish_track(discard=True)
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
        state.stop_event.set()
        if state.is_recording:
            state.set_state(state.STATE_STOPPING)
        session.shutdown()

        # Queued saves finish before the summary counts them
        if finalizer.busy:
            console.print("[yellow]Finishing pending saves...[/yellow]")
        finalizer.close()
        drain_results()

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
