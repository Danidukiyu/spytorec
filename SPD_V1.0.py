#!/usr/bin/env python3
"""
SpytoRec V8.0.0
================================================================================
WHAT IT DOES:
    Records Spotify audio streams in real-time to high-quality FLAC files with
    automatic track detection, file tagging, and album art embedding.

KEY FUNCTIONS:
    • Automatically detects when Spotify starts/stops playing
    • Records each track as a separate FLAC file
    • Tags files with artist, album, title, track number, and year
    • Downloads and embeds album artwork
    • Real-time audio level meters with clipping detection
    • Auto-switches recording between tracks seamlessly
    • Monitors audio health (stereo/mono, signal strength)
    • Recovers from errors automatically
    • Cross-platform (Windows/Linux/macOS)

TECHNICAL HIGHLIGHTS:
    • Uses FFmpeg for high-quality FLAC encoding (16/24/32-bit, up to 96kHz)
    • Spotify API for track metadata and playback state
    • Real-time audio analysis with sounddevice
    • Rich terminal UI with live meters and progress bars
    • Thread-safe state machine with error recovery
    • Watchdog monitoring for process health

REMOVED (for stability):
    • BPM/Key analysis (was unstable)
    • MusicBrainz metadata (unreliable)
    • DSP analysis threads (performance overhead)

Author: @Darkphoenix
GitHub: https://github.com/Danidukiyu
Version: 8.0.0

Contributors:
    • @electrodics-ship-it — contributed the V8.0.0 optimized build (issue #6):
      real-time audio metering, state machine, watchdog, rotating logs,
      24-bit FLAC support, and cross-platform improvements.

License:
    This project is licensed under the MIT License.
    Refer to the LICENSE file in the repository for full details.

Disclaimer:
    This script is intended for personal, private use only. Users are solely
    responsible for ensuring their use complies with all applicable laws and
    Spotify's Terms of Service regarding content recording.
"""

import os
import sys
import time
import argparse
import subprocess
import re
import queue
import threading
import configparser
import requests
import logging
import platform
import signal
import atexit
from contextlib import contextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List, Union

# --- Dependency Check ---
try:
    import sounddevice as sd
    import numpy as np
    from mutagen.flac import FLAC, Picture
    from spotipy import Spotify
    from spotipy.oauth2 import SpotifyOAuth
    from spotipy.exceptions import SpotifyException
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    from rich.live import Live
    from rich.table import Table
    from rich.progress_bar import ProgressBar
    from rich.spinner import Spinner
except ImportError as e:
    print(f"CRITICAL: Missing library: {e}\nPlease install required packages: pip install sounddevice numpy mutagen spotipy rich requests")
    sys.exit(1)

# Platform-specific imports
if os.name == 'nt':
    import msvcrt
    try:
        import win32file
        import win32con
        import pywintypes
        HAS_WIN32 = True
    except ImportError:
        HAS_WIN32 = False
else:
    import fcntl
    import termios
    import tty
    import select
    HAS_WIN32 = False

# --- Global Constants ---
SCRIPT_VERSION = "8.0.0"
SPOTIPY_REDIRECT_URI = 'http://127.0.0.1:8888/callback'
SPOTIPY_SCOPE = "user-read-playback-state user-read-currently-playing"
AUDIO_THRESHOLD = 0.0001
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE_PATH = BASE_DIR / "config.ini"
LOCK_FILE_PATH = BASE_DIR / "spyto.lock"
MAX_RETRIES = 3
RETRY_DELAY = 1.0
HEARTBEAT_TIMEOUT = 5.0
MAX_COVER_ART_BYTES = 2 * 1024 * 1024  # 2 MB safety cap for album art

console = Console()

# --- Internal State Machine Definitions ---
STATE_INIT = "INITIALISING"
STATE_IDLE = "IDLE"
STATE_MONITORING = "MONITORING"
STATE_RECORDING = "RECORDING"
STATE_SWITCHING = "SWITCHING_TRACK"
STATE_STOPPING = "STOPPING"
STATE_ERROR = "ERROR"
STATE_RECOVERING = "RECOVERING"

VALID_STATE_TRANSITIONS = {
    STATE_INIT: {STATE_IDLE, STATE_ERROR},
    STATE_IDLE: {STATE_MONITORING, STATE_ERROR},
    STATE_MONITORING: {STATE_RECORDING, STATE_IDLE, STATE_ERROR},
    STATE_RECORDING: {STATE_SWITCHING, STATE_STOPPING, STATE_ERROR, STATE_RECOVERING},
    STATE_SWITCHING: {STATE_RECORDING, STATE_IDLE, STATE_ERROR},
    STATE_STOPPING: {STATE_IDLE, STATE_ERROR},
    STATE_ERROR: {STATE_RECOVERING, STATE_IDLE},
    STATE_RECOVERING: {STATE_IDLE, STATE_MONITORING, STATE_ERROR}
}

# --- Thread-Safe Shared State ---
state_lock = threading.Lock()
meter_lock = threading.Lock()
current_state = STATE_INIT
last_error_msg = ""
failed_recordings = []
error_count = 0
last_heartbeat = time.time()

# Audio Telemetry Globals
meter_data = {}
meter_peaks = {}
live_rms_l, live_rms_r = 0.0, 0.0
peak_l, peak_r = 0.0, 0.0
raw_l, raw_r = 0.0, 0.0
smoothed_rms_l, smoothed_rms_r = 0.0, 0.0
mono_warning_frames = 0

# Queues
metadata_queue = queue.Queue()
stop_event = threading.Event()

# Process & Stream References
watchdog_proc_ref = None
watchdog_file_ref = None
active_monitor_stream = None
current_track_id_ref = None
ffmpeg_process = None
ffmpeg_lock = threading.Lock()

# --- Platform detection for FFmpeg input format ---
def get_ffmpeg_input_format() -> str:
    """Returns the correct FFmpeg audio input format for the current platform."""
    if os.name == 'nt':
        return 'dshow'
    elif sys.platform == 'darwin':
        return 'avfoundation'
    else:
        return 'pulse'

def get_ffmpeg_device_arg(device_name: str) -> str:
    """Returns the correctly formatted device argument for the current platform."""
    if os.name == 'nt':
        return f"audio={device_name}"
    elif sys.platform == 'darwin':
        return device_name
    else:
        return device_name



# --- Context Managers for Resource Management ---

@contextmanager
def file_lock(lock_path: Path, timeout: float = 5.0):
    """Cross-platform file locking context manager. Degrades gracefully if locking is unavailable."""
    lock_file = None
    locked = False
    try:
        if os.name == 'nt' and HAS_WIN32:
            lock_file = open(lock_path, 'w')
            try:
                win32file.LockFileEx(
                    win32file._get_osfhandle(lock_file.fileno()),
                    win32con.LOCKFILE_EXCLUSIVE_LOCK,
                    0,
                    -0x10000,
                    pywintypes.OVERLAPPED()
                )
                locked = True
            except Exception as lock_err:
                logging.warning(f"File locking (Win32) failed: {lock_err}")
        elif os.name != 'nt':
            lock_file = open(lock_path, 'w')
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except Exception as lock_err:
                logging.warning(f"File locking (fcntl) failed: {lock_err}")

        yield

    finally:
        if lock_file:
            try:
                if locked:
                    if os.name == 'nt' and HAS_WIN32:
                        try:
                            win32file.UnlockFileEx(
                                win32file._get_osfhandle(lock_file.fileno()),
                                0,
                                -0x10000,
                                pywintypes.OVERLAPPED()
                            )
                        except Exception:
                            pass
                    elif os.name != 'nt':
                        try:
                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                        except Exception:
                            pass
            except Exception:
                pass
            try:
                lock_file.close()
            except Exception:
                pass


# --- 1. CONFIGURATION & LOGGING ---

DEFAULT_CONFIG = {
    'Recording': {
        'device_id': '', 'ffmpeg_name': '', 'sample_rate': '48000', 'bit_depth': '24',
        'channels': '2', 'output_format': 'flac', 'output_directory': 'Recordings',
        'auto_start': 'false', 'overwrite_existing': 'false', 'force_safe_mode': 'false',
        'max_retries': '3', 'retry_delay': '1'
    },
    'QualityDisplay': {
        'show_sample_rate': 'true', 'show_bit_depth': 'true', 'show_channels': 'true',
        'show_lr_meters': 'true', 'show_peak_hold': 'true'
    },
    'UIOptions': {
        'show_analysis_status': 'false'
    },
    'Diagnostics': {
        'enable_logging': 'true', 'log_level': 'info', 'log_file': 'spyto_system.log',
        'ffmpeg_log_file': 'spyto_ffmpeg.log', 'max_log_size_mb': '5', 'log_analysis': 'false',
        'clear_log_on_startup': 'false'
    },
    'UI': {
        'theme': 'dark', 'show_status_strip': 'true', 'show_file_path': 'true',
        'smooth_meter_animation': 'true'
    },
    'SafetyChecks': {
        'validate_device': 'true', 'validate_output_path': 'true', 'validate_stereo': 'true',
        'validate_encoder': 'true', 'validate_file_integrity': 'true'
    },
    'Debug': {
        'show_debug_overlay': 'false', 'watchdog_enabled': 'true'
    },
    'Naming': {
        'naming_format': '{track_no}. {artist} - {title}'
    },
    'SpotifyAPI': {
        'SPOTIPY_CLIENT_ID': '', 'SPOTIPY_CLIENT_SECRET': ''
    }
}


def resolve_path(path_str: Union[str, Path]) -> Path:
    """Ensures all paths are absolute and relative to the script directory.
    Returns a resolved absolute path; relative inputs are anchored to BASE_DIR.
    """
    p = Path(path_str) if not isinstance(path_str, Path) else path_str
    resolved = p if p.is_absolute() else BASE_DIR / p
    return resolved.resolve()


def load_config() -> configparser.ConfigParser:
    """Loads config.ini with validation and auto-populates missing defaults."""
    cfg = configparser.ConfigParser()

    if CONFIG_FILE_PATH.exists():
        try:
            cfg.read(CONFIG_FILE_PATH, encoding='utf-8')
        except Exception as e:
            console.print(f"[yellow]Warning: Could not read config file: {e}[/yellow]")

    modified = False
    for section, keys in DEFAULT_CONFIG.items():
        if not cfg.has_section(section):
            cfg.add_section(section)
            modified = True
        for key, val in keys.items():
            if not cfg.has_option(section, key):
                cfg.set(section, key, val)
                modified = True

    if cfg.get('Recording', 'sample_rate') not in ['44100', '48000', '96000']:
        cfg.set('Recording', 'sample_rate', '48000')
        modified = True

    if cfg.get('Recording', 'bit_depth') not in ['16', '24', '32']:
        cfg.set('Recording', 'bit_depth', '24')
        modified = True

    if modified:
        try:
            with file_lock(CONFIG_FILE_PATH):
                with open(CONFIG_FILE_PATH, 'w', encoding='utf-8') as f:
                    cfg.write(f)
        except Exception as e:
            console.print(f"[red]Error saving config: {e}[/red]")

    return cfg


def setup_logging(config: configparser.ConfigParser) -> None:
    """Initialises rotating logs with error handling."""
    if not config['Diagnostics'].getboolean('enable_logging'):
        logging.getLogger().addHandler(logging.NullHandler())
        return

    try:
        sys_log = resolve_path(config['Diagnostics'].get('log_file'))

        if config['Diagnostics'].getboolean('clear_log_on_startup'):
            try:
                if sys_log.exists():
                    sys_log.unlink()
            except Exception:
                pass

        sys_log.parent.mkdir(parents=True, exist_ok=True)

        handler = RotatingFileHandler(
            sys_log,
            maxBytes=int(config['Diagnostics'].get('max_log_size_mb', 5)) * 1024 * 1024,
            backupCount=2,
            encoding='utf-8'
        )
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

        logger = logging.getLogger()
        logger.setLevel(getattr(logging, config['Diagnostics'].get('log_level', 'info').upper(), logging.INFO))

        for h in logger.handlers[:]:
            logger.removeHandler(h)

        logger.addHandler(handler)

        if config['Diagnostics'].get('log_level', 'info').upper() == 'DEBUG':
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
            logger.addHandler(console_handler)

        logging.info(f"SpytoRec {SCRIPT_VERSION} started on {platform.system()} {platform.release()}")

    except Exception as e:
        console.print(f"[red]Failed to setup logging: {e}[/red]")
        logging.basicConfig(level=logging.INFO)


cfg = load_config()
setup_logging(cfg)


# --- 2. HELPER FUNCTIONS & LOGIC ---

def set_state(new_state: str, msg: str = "") -> bool:
    """Thread-safe application state transitions with validation."""
    global current_state, last_error_msg, error_count

    with state_lock:
        old_state = current_state
        allowed_states = VALID_STATE_TRANSITIONS.get(old_state, set())

        if new_state not in allowed_states:
            logging.warning(f"Invalid state transition: {old_state} -> {new_state}")
            return False

        if new_state != old_state:
            logging.info(f"State: {old_state} -> {new_state} {f'[{msg}]' if msg else ''}")
            current_state = new_state

            if new_state == STATE_ERROR:
                last_error_msg = msg
                error_count += 1
            elif new_state == STATE_RECOVERING:
                error_count = max(0, error_count - 1)
            elif new_state == STATE_IDLE:
                error_count = 0

            return True
        return False


def get_state() -> str:
    """Get current application state."""
    with state_lock:
        return current_state


def clean_filename(name: str) -> str:
    """Strips illegal filesystem characters from filenames (cross-platform safe)."""
    if not name:
        return "unknown"
    cleaned = re.sub(r'[\\/*?:"<>|\r\n\t]', '', str(name))
    cleaned = cleaned.strip('. ')
    if len(cleaned) > 200:
        cleaned = cleaned[:200]
    return cleaned or "unknown"


def get_tail_logs(n: int = 4) -> str:
    """Safely retrieves end of log file for UI display."""
    if not cfg['Diagnostics'].getboolean('enable_logging'):
        return "Logging Disabled"

    path = resolve_path(cfg['Diagnostics'].get('log_file'))
    try:
        if not path.exists():
            return "Waiting for logs..."

        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
            return "".join(lines[-n:]).strip()
    except Exception as e:
        return f"Log Error: {e}"


def cleanup_resources():
    """Clean up all resources before exit."""
    global active_monitor_stream, ffmpeg_process

    logging.info("Cleaning up resources...")

    stop_monitor_stream()

    with ffmpeg_lock:
        if ffmpeg_process and ffmpeg_process.poll() is None:
            try:
                safely_stop_ffmpeg(ffmpeg_process)
            except Exception:
                pass
        ffmpeg_process = None

    stop_event.set()

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
    stop_event.set()
    sys.exit(0)


# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)
atexit.register(cleanup_resources)


# --- 3. HARDWARE & MONITORING ---

def stop_monitor_stream() -> None:
    """Stops the active audio monitor stream safely."""
    global active_monitor_stream

    try:
        if active_monitor_stream:
            active_monitor_stream.stop()
            active_monitor_stream.close()
            active_monitor_stream = None
    except Exception as e:
        logging.error(f"Monitor stop failed: {e}")


def audio_callback_factory(idx: int):
    """Factory for simple meter callbacks."""
    def cb(indata, frames, time_info, status):
        try:
            rms = np.sqrt(np.mean(indata**2))
            with meter_lock:
                meter_data[idx] = rms
                meter_peaks[idx] = max(meter_peaks.get(idx, 0.0), rms)
        except Exception:
            pass
    return cb


def live_monitor_callback(indata, frames, time_info, status) -> None:
    """Dual-purpose callback: Powers UI meters and monitors audio levels."""
    global smoothed_rms_l, smoothed_rms_r, peak_l, peak_r, raw_l, raw_r, mono_warning_frames
    global last_heartbeat

    try:
        last_heartbeat = time.time()

        if indata.shape[1] >= 2:
            raw_l = float(np.sqrt(np.mean(indata[:, 0]**2)))
            raw_r = float(np.sqrt(np.mean(indata[:, 1]**2)))
        else:
            raw_l = raw_r = float(np.sqrt(np.mean(indata[:, 0]**2)))

        alpha = 0.4 if cfg['UI'].getboolean('smooth_meter_animation') else 1.0
        smoothed_rms_l = (alpha * raw_l) + ((1 - alpha) * smoothed_rms_l)
        smoothed_rms_r = (alpha * raw_r) + ((1 - alpha) * smoothed_rms_r)

        peak_l = max(raw_l, peak_l * 0.99)
        peak_r = max(raw_r, peak_r * 0.99)

        if raw_l > 0.01 and abs(raw_l - raw_r) < 0.0001:
            mono_warning_frames += 1
        else:
            mono_warning_frames = max(0, mono_warning_frames - 2)

    except Exception as e:
        logging.debug(f"Monitor callback error: {e}")


def start_monitor(idx: int, sr: int, ch: int) -> bool:
    """Binds to audio device for metering."""
    global active_monitor_stream

    if ch < 2 and cfg['SafetyChecks'].getboolean('validate_stereo'):
        logging.error("Stereo validation failed: need at least 2 channels")
        return False

    try:
        stop_monitor_stream()

        active_monitor_stream = sd.InputStream(
            device=idx,
            channels=min(2, ch),
            samplerate=sr,
            callback=live_monitor_callback,
            blocksize=1024,
            latency='low'
        )
        active_monitor_stream.start()
        logging.info(f"Monitor started on device {idx} at {sr}Hz")
        return True
    except Exception as e:
        logging.error(f"Monitor bind failed: {e}")
        return False


def get_keypress_unix():
    """Get a single keypress on Unix systems without blocking."""
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(sys.stdin.fileno())
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch


def discover_hardware(ffmpeg_path: str) -> Tuple[str, int, int, int]:
    """Interactive hardware wizard with better error handling."""
    set_state(STATE_IDLE, "Hardware Discovery")
    console.clear()

    try:
        devices = sd.query_devices()
    except Exception as e:
        console.print(f"[red]Failed to query audio devices: {e}[/red]")
        sys.exit(1)

    streams = []
    active_idx = []
    dshow_names = []

    # Poll FFmpeg for Friendly DShow Names (Windows only)
    if os.name == 'nt':
        try:
            p = subprocess.run(
                [ffmpeg_path, '-list_devices', 'true', '-f', 'dshow', '-i', 'dummy'],
                capture_output=True,
                text=True,
                errors='ignore',
                timeout=10
            )
            dshow_names = re.findall(r'"(.+?)"', p.stderr)
        except Exception as e:
            console.print(f"[yellow]Warning: Could not list FFmpeg devices: {e}[/yellow]")

    # Find active devices
    for i, d in enumerate(devices):
        if d['max_input_channels'] > 0:
            try:
                hostapi = sd.query_hostapis(d['hostapi'])
                if 'WDM-KS' not in hostapi['name']:
                    s = sd.InputStream(
                        device=i,
                        channels=min(d['max_input_channels'], 2),
                        samplerate=d['default_samplerate'],
                        callback=audio_callback_factory(i)
                    )
                    s.start()
                    streams.append(s)
                    active_idx.append(i)
                    with meter_lock:
                        meter_data[i] = 0.0
            except Exception:
                pass

    if not active_idx:
        console.print("[red]No suitable input devices found![/red]")
        sys.exit(1)

    selected = None
    buf = ""

    def build_hw_table():
        t = Table(title="[bold cyan]Hardware Selection[/bold cyan]")
        t.add_column("ID", justify="center")
        t.add_column("Device Name")
        t.add_column("Details", style="magenta")
        t.add_column("Level")

        rows = {}
        count = 1

        with meter_lock:
            sorted_idx = sorted(active_idx, key=lambda x: meter_peaks.get(x, 0), reverse=True)
            for i in sorted_idx:
                if meter_peaks.get(i, 0) > AUDIO_THRESHOLD:
                    sr = int(devices[i]['default_samplerate'])
                    ch = devices[i]['max_input_channels']
                    level = min(40, int(meter_data.get(i, 0) * 40))

                    t.add_row(
                        f"[{count}]",
                        devices[i]['name'],
                        f"{sr}Hz | {ch}ch",
                        "\u2588" * level
                    )
                    rows[count] = dict(devices[i])
                    rows[count]['idx'] = i
                    count += 1

        return Panel(t, subtitle=f"Selection: {buf}"), rows

    console.print("[yellow]Press number keys to select device, Enter to confirm[/yellow]")

    with Live(build_hw_table()[0], refresh_per_second=10) as live:
        while not selected:
            try:
                panel, rows = build_hw_table()
                live.update(panel)

                if os.name == 'nt':
                    if msvcrt.kbhit():
                        c = msvcrt.getch()
                        if c in [b'\r', b'\n']:
                            if buf.isdigit() and int(buf) in rows:
                                selected = rows[int(buf)]
                            buf = ""
                        elif c == b'\x08':
                            buf = buf[:-1]
                        elif c.isdigit():
                            buf += c.decode()
                else:
                    if select.select([sys.stdin], [], [], 0.05)[0]:
                        c = get_keypress_unix()
                        if c in ['\r', '\n']:
                            if buf.isdigit() and int(buf) in rows:
                                selected = rows[int(buf)]
                            buf = ""
                        elif c == '\x7f':
                            buf = buf[:-1]
                        elif c.isdigit():
                            buf += c

                time.sleep(0.05)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                console.print(f"[red]Selection error: {e}[/red]")
                time.sleep(1)

    # Cleanup streams
    for s in streams:
        try:
            s.stop()
            s.close()
        except Exception:
            pass

    # Find best FFmpeg name match (Windows DShow only)
    best_name = selected['name']
    if os.name == 'nt':
        for dshow_name in dshow_names:
            if selected['name'][:15] in dshow_name:
                best_name = dshow_name
                break

    # Save to config
    cfg.set('Recording', 'ffmpeg_name', best_name)
    cfg.set('Recording', 'device_id', str(selected['idx']))
    cfg.set('Recording', 'sample_rate', str(int(selected['default_samplerate'])))
    cfg.set('Recording', 'channels', str(selected['max_input_channels']))

    try:
        with file_lock(CONFIG_FILE_PATH):
            with open(CONFIG_FILE_PATH, 'w', encoding='utf-8') as f:
                cfg.write(f)
    except Exception as e:
        console.print(f"[yellow]Could not save config: {e}[/yellow]")

    return best_name, selected['idx'], int(selected['default_samplerate']), selected['max_input_channels']


# --- 4. RECORDING & SUBPROCESS ---

def watchdog_worker() -> None:
    """Monitors health of recording threads and FFmpeg."""
    last_sz = 0
    stalled_count = 0
    no_heartbeat_count = 0

    while not stop_event.is_set():
        try:
            time.sleep(2.0)

            if not cfg['Debug'].getboolean('watchdog_enabled'):
                continue

            current_state_val = get_state()

            # Check heartbeat
            if current_state_val == STATE_RECORDING:
                if time.time() - last_heartbeat > HEARTBEAT_TIMEOUT:
                    no_heartbeat_count += 1
                    if no_heartbeat_count >= 3:
                        logging.error("Audio stream heartbeat lost")
                        set_state(STATE_ERROR, "Audio stream lost")
                else:
                    no_heartbeat_count = 0

            # Check FFmpeg process
            with ffmpeg_lock:
                if ffmpeg_process and ffmpeg_process.poll() is not None:
                    returncode = ffmpeg_process.poll()
                    logging.error(f"FFmpeg terminated with code {returncode}")
                    set_state(STATE_ERROR, f"FFmpeg terminated (code {returncode})")

            # Check file growth
            if current_state_val == STATE_RECORDING and watchdog_file_ref and watchdog_file_ref.exists():
                try:
                    sz = watchdog_file_ref.stat().st_size
                    if sz == last_sz and sz > 0:
                        stalled_count += 1
                        if stalled_count >= 3:
                            logging.warning("Recording file stalled")
                            stalled_count = 0
                    else:
                        stalled_count = 0
                        last_sz = sz
                except Exception as e:
                    logging.debug(f"File size check failed: {e}")

        except Exception as e:
            logging.error(f"Watchdog error: {e}")


def finalize(temp_file: Path, out_dir: Path, track_info: Dict, naming_format: str) -> bool:
    """Tags and moves the recorded file with integrity checking."""
    if not temp_file or not temp_file.exists():
        return False

    try:
        # Verify file integrity
        if cfg['SafetyChecks'].getboolean('validate_file_integrity'):
            if temp_file.stat().st_size < 8192:
                logging.warning(f"File too small, possible corruption: {temp_file}")
                temp_file.unlink()
                return False

        # Wait a moment for file to be fully written
        time.sleep(1.5)

        # Clean filenames
        artist = clean_filename(track_info['artists'][0]['name'])
        album = clean_filename(track_info['album']['name'])
        title = clean_filename(track_info['name'])
        track_no = str(track_info.get('track_number', 0)).zfill(2)
        year = track_info['album'].get('release_date', '0000')[:4]

        tags = {
            'artist': artist,
            'album': album,
            'title': title,
            'track_no': track_no,
            'year': year
        }

        # Create final path
        try:
            final_name = naming_format.format(**tags)
        except KeyError as e:
            logging.warning(f"Missing tag in naming format: {e}, using fallback")
            final_name = f"{track_no}. {artist} - {title}"

        final_name = clean_filename(final_name)
        final_path = out_dir / f"{final_name}.flac"

        # Handle existing files
        if final_path.exists():
            if not cfg['Recording'].getboolean('overwrite_existing'):
                timestamp = int(time.time())
                final_path = out_dir / f"{final_name}_{timestamp}.flac"
                logging.info(f"File exists, created: {final_path.name}")
            else:
                logging.info(f"Overwriting existing file: {final_path.name}")

        # Load and tag the FLAC file
        audio = FLAC(temp_file)

        audio['title'] = track_info['name']
        audio['artist'] = track_info['artists'][0]['name']
        audio['album'] = track_info['album']['name']
        audio['date'] = year
        audio['tracknumber'] = track_no

        # Add album art with content-type and size validation
        if not cfg['Recording'].getboolean('force_safe_mode'):
            try:
                images = track_info['album'].get('images', [])
                if images:
                    img_url = images[0]['url']
                    response = requests.get(img_url, timeout=5, stream=True)
                    if response.status_code == 200:
                        content_type = response.headers.get('Content-Type', '')
                        # Reject immediately if Content-Length header exceeds our cap
                        content_length = response.headers.get('Content-Length')
                        if content_length and int(content_length) > MAX_COVER_ART_BYTES:
                            logging.warning(f"Album art Content-Length exceeds {MAX_COVER_ART_BYTES} byte limit, skipping")
                        elif content_type.startswith('image/'):
                            # Read with size cap to prevent memory exhaustion
                            img_data = b''
                            for chunk in response.iter_content(chunk_size=65536):
                                img_data += chunk
                                if len(img_data) > MAX_COVER_ART_BYTES:
                                    logging.warning(f"Album art exceeds {MAX_COVER_ART_BYTES} byte limit, skipping")
                                    img_data = b''
                                    break
                            if img_data:
                                mime = content_type.split(';')[0].strip()
                                picture = Picture()
                                picture.data = img_data
                                picture.type = 3
                                picture.mime = mime
                                picture.desc = "Cover Art"
                                audio.add_picture(picture)
                                logging.debug("Added album art")
                        else:
                            logging.warning(f"Unexpected content type for album art: {content_type}")
            except Exception as e:
                logging.debug(f"Could not add album art: {e}")

        # Save the tagged file
        audio.save()

        # Move to final location
        temp_file.replace(final_path)
        logging.info(f"Finalised: {final_path.name}")
        return True

    except Exception as e:
        logging.exception(f"Finalise Error: {e}")
        try:
            if temp_file and temp_file.exists():
                temp_file.unlink()
        except Exception:
            pass
        return False


def safely_stop_ffmpeg(proc) -> None:
    """Shutdown protocol for FFmpeg to prevent pipe corruption."""
    if not proc or proc.poll() is not None:
        return

    set_state(STATE_STOPPING)

    try:
        if proc.stdin:
            proc.stdin.write(b'q')
            proc.stdin.flush()
            proc.stdin.close()

        try:
            proc.wait(timeout=10)
            logging.info("FFmpeg stopped gracefully")
        except subprocess.TimeoutExpired:
            logging.warning("FFmpeg didn't respond, terminating...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                logging.warning("FFmpeg killed")

    except Exception as e:
        logging.error(f"Error stopping FFmpeg: {e}")
        try:
            proc.kill()
        except Exception:
            pass


def spotify_with_retry(sp, max_retries=3):
    """Get Spotify playback with retry logic."""
    for attempt in range(max_retries):
        try:
            return sp.current_playback()
        except SpotifyException as e:
            if e.http_status == 429:
                retry_after = int(e.headers.get('Retry-After', 5))
                logging.warning(f"Rate limited, waiting {retry_after}s")
                time.sleep(retry_after)
            elif attempt < max_retries - 1:
                logging.warning(f"Spotify API error: {e}, retrying...")
                time.sleep(1 * (attempt + 1))
            else:
                raise
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(1)
            else:
                raise
    return None


# --- 5. UI HELPER FUNCTIONS ---

def get_health_indicator(rms: float) -> str:
    """Returns health status based on RMS level."""
    if rms > 0.85:
        return "[bold red]\U0001f534 Clipping[/bold red]"
    if rms < 0.001:
        return "[yellow]\U0001f7e1 Low[/yellow]"
    return "[green]\U0001f7e2 Good[/green]"


def build_gradient_bar(rms: float, peak: float, width: int = 40, show_peak: bool = True) -> str:
    """Builds a gradient level meter bar."""
    if np.isnan(rms) or rms <= 1e-6:
        return "[dim]\u2501[/dim]" * width

    db = 20 * np.log10(rms + 1e-9)
    db_peak = 20 * np.log10(peak + 1e-9)

    fill = int(np.clip((db + 60) / 60 * width, 0, width))
    peak_pos = int(np.clip((db_peak + 60) / 60 * width, 0, width - 1))

    bar_chars = []
    for i in range(width):
        if show_peak and i == peak_pos:
            bar_chars.append("[white]\u275a[/white]")
        elif i < fill:
            if i < width * 0.6:
                bar_chars.append("[green]\u2588[/green]")
            elif i < width * 0.85:
                bar_chars.append("[yellow]\u2588[/yellow]")
            else:
                bar_chars.append("[red]\u2588[/red]")
        else:
            bar_chars.append("[dim]\u2501[/dim]")

    return "".join(bar_chars)


# --- 6. MAIN ENGINE ---

def main():
    global ffmpeg_process, watchdog_proc_ref, watchdog_file_ref, current_track_id_ref

    # Parse arguments
    parser = argparse.ArgumentParser(description=f'SpytoRec v{SCRIPT_VERSION} - Spotify Recording Tool')
    parser.add_argument('--ffmpeg', default='ffmpeg', help='Path to ffmpeg executable')
    args = parser.parse_args()

    # Setup directories
    out_dir = resolve_path(cfg['Recording'].get('output_directory', 'Recordings'))
    naming_format = cfg['Naming'].get('naming_format', '{track_no}. {artist} - {title}')

    # Validate output directory
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        test_file = out_dir / ".perms_check"
        test_file.touch()
        test_file.unlink()
        logging.info(f"Output directory ready: {out_dir}")
    except Exception as e:
        console.print(f"[bold red]Output Path Unwritable: {e}[/bold red]")
        sys.exit(1)

    # Hardware initialisation
    hw_ready = cfg.get('Recording', 'ffmpeg_name', fallback='') != ''

    if hw_ready:
        console.print("[yellow]Found existing config. Press SPACE in 5s to re-scan hardware...[/yellow]")
        start_time = time.time()
        rescan = False

        while time.time() - start_time < 5:
            if os.name == 'nt' and msvcrt.kbhit():
                if msvcrt.getch() == b' ':
                    rescan = True
                    break
            elif os.name != 'nt':
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    c = sys.stdin.read(1)
                    if c == ' ':
                        rescan = True
                        break
            time.sleep(0.1)

        if rescan:
            hw_name, hw_idx, hw_sr, hw_ch = discover_hardware(args.ffmpeg)
        else:
            hw_name = cfg['Recording'].get('ffmpeg_name')
            hw_idx = int(cfg['Recording'].get('device_id'))
            hw_sr = int(cfg['Recording'].get('sample_rate'))
            hw_ch = int(cfg['Recording'].get('channels'))
    else:
        hw_name, hw_idx, hw_sr, hw_ch = discover_hardware(args.ffmpeg)

    # Spotify initialisation
    client_id = cfg['SpotifyAPI'].get('SPOTIPY_CLIENT_ID')
    client_secret = cfg['SpotifyAPI'].get('SPOTIPY_CLIENT_SECRET')

    if not client_id or not client_secret:
        console.print("[red]Spotify Credentials Missing from config.ini![/red]")
        console.print("Please add your Spotify API credentials to config.ini under [SpotifyAPI].")
        sys.exit(1)

    try:
        sp = Spotify(auth_manager=SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=SPOTIPY_REDIRECT_URI,
            scope=SPOTIPY_SCOPE,
            open_browser=True
        ))
        console.print("[green]Spotify authentication successful![/green]")
    except Exception as e:
        console.print(f"[red]Spotify authentication failed: {e}[/red]")
        sys.exit(1)

    # Start background workers
    workers = []
    for target in [watchdog_worker]:
        t = threading.Thread(target=target, daemon=True)
        t.start()
        workers.append(t)

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

    # Determine platform-specific FFmpeg input format
    ffmpeg_input_fmt = get_ffmpeg_input_format()

    set_state(STATE_MONITORING)

    try:
        with Live(Panel(Text("Waiting for Spotify...", style="yellow")), refresh_per_second=10) as live:
            while not stop_event.is_set():
                try:
                    # Get playback info with retry
                    playback = spotify_with_retry(sp)

                    if playback and playback.get('is_playing'):
                        track = playback['item']
                        track_id = track['id']

                        # Handle track change
                        if track_id != current_id:
                            # Stop current recording if any
                            if ffmpeg_process:
                                set_state(STATE_SWITCHING)
                                stop_monitor_stream()

                                with ffmpeg_lock:
                                    safely_stop_ffmpeg(ffmpeg_process)
                                    ffmpeg_process = None

                                if temp_file and current_track:
                                    finalize(temp_file, out_dir, current_track, naming_format)
                                    temp_file = None

                            # Start new recording
                            safe_mode = cfg['Recording'].getboolean('force_safe_mode')
                            sr = 44100 if safe_mode else hw_sr
                            ch = 2 if safe_mode else hw_ch
                            bit_depth = cfg['Recording'].get('bit_depth', '24')

                            # Map bit depth to sample format
                            fmt_map = {'16': 's16', '24': 's32', '32': 's32'}
                            sample_fmt = fmt_map.get(bit_depth, 's16')

                            # Start audio monitor
                            if not start_monitor(hw_idx, sr, ch):
                                set_state(STATE_ERROR, "Audio Monitor Failed")
                                time.sleep(3)
                                continue

                            # Create temp file
                            temp_file = out_dir / f".tmp_{int(time.time())}.flac"

                            # Build cross-platform FFmpeg command
                            device_arg = get_ffmpeg_device_arg(hw_name)
                            cmd = [
                                args.ffmpeg, '-y',
                                '-f', ffmpeg_input_fmt,
                                '-i', device_arg,
                                '-ac', str(ch),
                                '-ar', str(sr),
                                '-sample_fmt', sample_fmt,
                                '-c:a', 'flac',
                                '-compression_level', '8',
                                str(temp_file)
                            ]

                            # Start FFmpeg
                            try:
                                with ffmpeg_lock:
                                    ffmpeg_process = subprocess.Popen(
                                        cmd,
                                        stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL,
                                        stderr=ff_log_ptr
                                    )

                                watchdog_proc_ref = ffmpeg_process
                                watchdog_file_ref = temp_file
                                current_track = track
                                current_id = track_id
                                current_track_id_ref = track_id

                                set_state(STATE_RECORDING)
                                logging.info(f"Started recording: {track['name']} - {track['artists'][0]['name']}")

                            except Exception as e:
                                logging.error(f"Failed to start FFmpeg: {e}")
                                if temp_file and temp_file.exists():
                                    try:
                                        temp_file.unlink()
                                    except Exception:
                                        pass
                                temp_file = None
                                set_state(STATE_ERROR, f"FFmpeg error: {e}")
                                continue

                        # Build UI display
                        if temp_file and temp_file.exists():
                            file_size = round(temp_file.stat().st_size / (1024 ** 2), 2)
                        else:
                            file_size = 0.0

                        progress = playback.get('progress_ms', 0)
                        duration = track.get('duration_ms', 1)

                        dashboard = Table.grid(expand=True)

                        rec_indicator = "[bold red]REC \u25cf[/bold red]" if get_state() == STATE_RECORDING else "[yellow]WAIT[/yellow]"
                        dashboard.add_row(Text.from_markup(f"{rec_indicator}  [bold cyan]{track['name']}[/bold cyan]"))
                        dashboard.add_row(Text(f"Artist: {track['artists'][0]['name']}", style="grey70"))
                        dashboard.add_row(Text(f"Album: {track['album']['name']}", style="grey70"))

                        dashboard.add_row(ProgressBar(total=duration, completed=progress, width=None))

                        if failed_recordings:
                            dashboard.add_row(Text(f"\u26a0\ufe0f Failed recordings: {len(failed_recordings)}", style="bold red"))

                        dashboard.add_section()

                        if cfg['UI'].getboolean('show_status_strip'):
                            safe_mode_active = cfg['Recording'].getboolean('force_safe_mode')
                            safe_tag = "[bold yellow](SAFE MODE) [/bold yellow]" if safe_mode_active else ""
                            minutes = int(progress / 60000)
                            seconds = int((progress / 1000) % 60)
                            time_str = f"{minutes:02d}:{seconds:02d}"

                            status_items = []
                            if cfg['QualityDisplay'].getboolean('show_sample_rate'):
                                status_items.append(f"{sr}Hz")
                            if cfg['QualityDisplay'].getboolean('show_bit_depth'):
                                status_items.append(f"{bit_depth}-bit")
                            if cfg['QualityDisplay'].getboolean('show_channels'):
                                status_items.append(f"{ch}ch")

                            tech_info = " | ".join(status_items) if status_items else ""
                            status_line = f"{safe_tag}{tech_info} | {time_str} | {file_size}MB"
                            dashboard.add_row(Text.from_markup(f"\u2699\ufe0f {status_line}"))

                        if cfg['QualityDisplay'].getboolean('show_lr_meters'):
                            show_peak = cfg['QualityDisplay'].getboolean('show_peak_hold')
                            dashboard.add_row(Text.from_markup(f"L {build_gradient_bar(smoothed_rms_l, peak_l, show_peak=show_peak)}"))
                            dashboard.add_row(Text.from_markup(f"R {build_gradient_bar(smoothed_rms_r, peak_r, show_peak=show_peak)}"))

                            if mono_warning_frames > 30:
                                dashboard.add_row(Text("[yellow]\u26a0\ufe0f Warning: Possible mono input detected[/yellow]"))

                        health = get_health_indicator(peak_l)
                        dashboard.add_row(Text.from_markup(f"Signal Health: {health}"))

                        if cfg['Debug'].getboolean('show_debug_overlay'):
                            dashboard.add_section()
                            pid = ffmpeg_process.pid if ffmpeg_process else 'None'
                            state = get_state()
                            dashboard.add_row(Text.from_markup(
                                f"[dim]PID: {pid} | State: {state} | L={raw_l:.3f} R={raw_r:.3f} | Mono frames: {mono_warning_frames}[/dim]"
                            ))

                        dashboard.add_section()

                        if cfg['Diagnostics'].getboolean('enable_logging'):
                            logs = get_tail_logs(3)
                            if logs:
                                dashboard.add_row(Panel(Text.from_markup(f"[dim]{logs}[/dim]"), title="System Log", border_style="dim"))

                        live.update(Panel(dashboard, title=f"SpytoRec v{SCRIPT_VERSION} - Recording", border_style="green"))

                    else:
                        # Not playing - cleanup if needed
                        if ffmpeg_process:
                            set_state(STATE_STOPPING)
                            stop_monitor_stream()

                            with ffmpeg_lock:
                                safely_stop_ffmpeg(ffmpeg_process)
                                ffmpeg_process = None

                            if temp_file and current_track:
                                if finalize(temp_file, out_dir, current_track, naming_format):
                                    logging.info(f"Saved: {current_track['name']}")
                                    console.print(f"[green]\u2713 Saved: {current_track['name']}[/green]")
                                else:
                                    failed_recordings.append(current_track['name'])
                                    if len(failed_recordings) > 5:
                                        failed_recordings.pop(0)
                                    console.print(f"[red]\u2717 Failed to save: {current_track['name']}[/red]")

                            temp_file = None
                            current_track = None
                            current_id = None
                            current_track_id_ref = None
                            set_state(STATE_IDLE)

                        idle_dashboard = Table.grid(expand=True)
                        idle_dashboard.add_row(Text("\U0001f3b5 Spotify Paused", style="bold yellow"))
                        idle_dashboard.add_row(Text("Waiting for playback to start...", style="grey70"))

                        if failed_recordings:
                            idle_dashboard.add_section()
                            idle_dashboard.add_row(Text(f"\u26a0\ufe0f Failed recordings: {len(failed_recordings)}", style="bold red"))

                        if cfg['Diagnostics'].getboolean('enable_logging'):
                            idle_dashboard.add_section()
                            logs = get_tail_logs(3)
                            if logs:
                                idle_dashboard.add_row(Panel(Text.from_markup(f"[dim]{logs}[/dim]"), title="System Log", border_style="dim"))

                        live.update(Panel(idle_dashboard, title=f"SpytoRec v{SCRIPT_VERSION}", border_style="blue"))

                    time.sleep(0.1)

                except SpotifyException as e:
                    logging.error(f"Spotify API error: {e}")
                    if time.time() - last_error_time > error_recovery_delay:
                        set_state(STATE_ERROR, f"Spotify error: {e}")
                        last_error_time = time.time()
                        time.sleep(5)
                        set_state(STATE_RECOVERING)
                        time.sleep(2)
                        set_state(STATE_MONITORING)
                    else:
                        time.sleep(1)

                except Exception as e:
                    logging.exception(f"Main loop error: {e}")
                    if time.time() - last_error_time > error_recovery_delay:
                        set_state(STATE_ERROR, str(e))
                        last_error_time = time.time()
                        time.sleep(5)
                        set_state(STATE_RECOVERING)
                        time.sleep(2)
                        set_state(STATE_MONITORING)
                    else:
                        time.sleep(1)

    finally:
        if ff_log_ptr != subprocess.DEVNULL:
            try:
                ff_log_ptr.close()
            except Exception:
                pass

        for worker in workers:
            worker.join(timeout=2)

        if failed_recordings:
            console.print(f"\n[yellow]Recording session completed with {len(failed_recordings)} failed tracks[/yellow]")
        else:
            console.print("\n[green]Recording session completed successfully![/green]")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        stop_event.set()
        console.print("\n[yellow]Shutdown complete.[/yellow]")
        sys.exit(0)
    except Exception as e:
        console.print(f"\n[red]Fatal error: {e}[/red]")
        logging.exception("Fatal error")
        sys.exit(1)
