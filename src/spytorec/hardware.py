"""Interactive audio hardware discovery wizard."""

import os
import sys
import time
import logging
import threading
import warnings
from typing import Tuple

warnings.filterwarnings("ignore", module="soundcard")

import soundcard as sc
import numpy as np
from rich.table import Table
from rich.panel import Panel
from rich.live import Live

from spytorec import state
from spytorec.config import console
from spytorec.utils import file_lock, CONFIG_FILE_PATH, LOCK_FILE_PATH, KBHit
from spytorec.ui import SP_GREEN, SP_GREEN_DIM, SP_GREY


def _meter_worker(mic_id: str, mic, stop_event: threading.Event):
    """Background thread to poll RMS audio levels for the UI wizard."""
    try:
        with mic.recorder(samplerate=48000, channels=2) as r:
            while not stop_event.is_set():
                data = r.record(numframes=1024)
                if len(data) > 0:
                    # data is (frames, channels) of float32
                    rms = float(np.sqrt(np.mean(data**2)))
                    with state.meter_lock:
                        state.meter_data[mic_id] = rms
                        state.meter_peaks[mic_id] = max(state.meter_peaks.get(mic_id, 0.0), rms)
    except Exception as e:
        logging.debug(f"Meter worker error for {mic_id}: {e}")


def discover_hardware(ffmpeg_path: str, cfg) -> Tuple[str, str, int, int]:
    """Interactive hardware wizard with better error handling."""
    state.set_state(state.STATE_IDLE, "Hardware Discovery")
    console.clear()

    try:
        mics = sc.all_microphones(include_loopback=True)
    except Exception as e:
        console.print(f"[red]Failed to query audio devices: {e}[/red]")
        sys.exit(1)

    if not mics:
        console.print("[red]No suitable input devices found![/red]")
        sys.exit(1)

    stop_event = threading.Event()
    threads = []
    
    # Start metering threads
    for mic in mics:
        with state.meter_lock:
            state.meter_data[mic.id] = 0.0
            
        t = threading.Thread(target=_meter_worker, args=(mic.id, mic, stop_event), daemon=True)
        t.start()
        threads.append(t)

    selected = None
    buf = ""

    def build_hw_table():
        t = Table(title=f"[{SP_GREEN}]Hardware Selection[/{SP_GREEN}]")
        t.add_column("ID", justify="center", style=SP_GREEN_DIM)
        t.add_column("Device Name")
        t.add_column("Details", style=SP_GREY)
        t.add_column("Level")

        rows = {}
        count = 1

        with state.meter_lock:
            for mic in mics:
                level = min(40, int(state.meter_data.get(mic.id, 0) * 40))
                has_audio = state.meter_peaks.get(mic.id, 0) > state.AUDIO_THRESHOLD
                style = SP_GREEN_DIM if has_audio else "dim"
                
                # Highlight if it's a loopback (output device)
                name_display = f"{mic.name} (Loopback)" if mic.isloopback else mic.name

                t.add_row(
                    f"[{count}]",
                    f"[{style}]{name_display}[/{style}]",
                    f"[{style}]48000Hz | 2ch[/{style}]",
                    f"[{SP_GREEN_DIM}]" + "\u2588" * level + f"[/{SP_GREEN_DIM}]" if has_audio else "[dim]" + "\u2501" * 5 + "[/dim]"
                )
                rows[count] = mic
                count += 1

        return Panel(t, subtitle=f"[{SP_GREY}]Selection: {buf}[/{SP_GREY}]", border_style=SP_GREEN_DIM), rows

    console.print("[yellow]Press a number key to pick a device (Enter to confirm a multi-digit number)[/yellow]")

    def apply_key(c):
        nonlocal buf
        if c in ('\r', '\n'):
            if buf.isdigit() and int(buf) in rows:
                return rows[int(buf)]
            buf = ""
        elif c in ('\x7f', '\x08'):
            buf = buf[:-1]
        elif c.isdigit():
            buf += c
            if int(buf) in rows and not any(
                str(k).startswith(buf) and len(str(k)) > len(buf) for k in rows
            ):
                return rows[int(buf)]
        return None

    kb = KBHit()
    kb.set_cbreak()

    try:
        with Live(build_hw_table()[0], refresh_per_second=10, screen=True) as live:
            while not selected:
                try:
                    panel, rows = build_hw_table()
                    live.update(panel)

                    while kb.kbhit() and not selected:
                        selected = apply_key(kb.getch())

                    time.sleep(0.05)
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    logging.debug(f"Hardware selection error: {e}")
                    time.sleep(0.2)
    finally:
        kb.set_normal_term()

    stop_event.set()

    # Save to config
    cfg.set('Recording', 'ffmpeg_name', selected.name)
    cfg.set('Recording', 'device_id', str(selected.id))
    # Soundcard generally works best internally if we just request standard rates,
    # it auto-resamples if needed.
    cfg.set('Recording', 'sample_rate', '48000')
    cfg.set('Recording', 'channels', '2')

    try:
        with file_lock(LOCK_FILE_PATH):
            with open(CONFIG_FILE_PATH, 'w', encoding='utf-8') as f:
                cfg.write(f)
    except Exception as e:
        console.print(f"[yellow]Could not save config: {e}[/yellow]")

    return selected.name, selected.id, 48000, 2
