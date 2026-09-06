"""Thread-safe application state machine and shared mutable globals."""

import time
import queue
import threading
import logging
from collections import deque

# --- State Constants ---
STATE_INIT = 'Init'
STATE_MONITORING = 'Monitoring'
STATE_SWITCHING = 'Switching Tracks'
STATE_RECORDING = 'Recording'
STATE_STOPPING = 'Stopping'
STATE_RECOVERING = 'Recovering'
STATE_ERROR = 'Error'
STATE_IDLE = 'Idle'
STATE_SKIPPED = 'Skipped'

# Each state lists every state the main loop can move it to.
VALID_STATE_TRANSITIONS = {
    STATE_INIT: {STATE_IDLE, STATE_ERROR, STATE_MONITORING},
    STATE_IDLE: {STATE_MONITORING, STATE_RECORDING, STATE_ERROR, STATE_SKIPPED},
    STATE_MONITORING: {STATE_RECORDING, STATE_IDLE, STATE_ERROR, STATE_SKIPPED, STATE_SWITCHING},
    STATE_RECORDING: {STATE_SWITCHING, STATE_STOPPING, STATE_SKIPPED, STATE_ERROR, STATE_RECOVERING},
    STATE_SWITCHING: {STATE_RECORDING, STATE_SKIPPED, STATE_IDLE, STATE_ERROR},
    STATE_STOPPING: {STATE_IDLE, STATE_ERROR},
    STATE_ERROR: {STATE_RECOVERING, STATE_IDLE, STATE_ERROR},
    STATE_RECOVERING: {STATE_IDLE, STATE_MONITORING, STATE_ERROR},
    STATE_SKIPPED: {STATE_MONITORING, STATE_RECORDING, STATE_IDLE, STATE_ERROR, STATE_SWITCHING, STATE_SKIPPED}
}

# --- Global Constants ---
SCRIPT_VERSION = "8.1.0"
SPOTIPY_REDIRECT_URI = 'http://127.0.0.1:8888/callback'
SPOTIPY_SCOPE = "user-read-playback-state user-read-currently-playing"
AUDIO_THRESHOLD = 0.0001
MAX_COVER_ART_BYTES = 2 * 1024 * 1024  # 2 MB safety cap for album art
HEARTBEAT_TIMEOUT = 5.0

# --- Thread-Safe Shared State ---
state_lock = threading.Lock()
meter_lock = threading.Lock()

current_state = STATE_INIT
last_error_msg = ""
failed_recordings = deque(maxlen=5)
error_count = 0
last_heartbeat = time.time()

# Audio Telemetry Globals (updated from meter_queue by main loop)
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
current_track_id_ref = None
is_recording = False  # Simple flag, set by main loop when audio process is recording


def set_state(new_state: str, msg: str = "") -> bool:
    """Thread-safe application state transitions with validation."""
    global current_state, last_error_msg, error_count

    with state_lock:
        old_state = current_state

        # Re-entering the current state is not a transition.
        if new_state == old_state:
            return False

        allowed_states = VALID_STATE_TRANSITIONS.get(old_state, set())

        if new_state not in allowed_states:
            logging.warning(f"Invalid state transition: {old_state} -> {new_state}")
            return False

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


def get_state() -> str:
    """Get current application state."""
    with state_lock:
        return current_state


def handle_error_recovery(error, last_error_time, error_recovery_delay):
    """Common error recovery logic for main loop exception handlers."""
    if time.time() - last_error_time > error_recovery_delay:
        set_state(STATE_ERROR, str(error))
        time.sleep(5)
        set_state(STATE_RECOVERING)
        time.sleep(2)
        set_state(STATE_MONITORING)
        return time.time()
    else:
        time.sleep(1)
        return last_error_time
