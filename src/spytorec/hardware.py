"""Interactive audio hardware discovery wizard."""

import os
import sys
import re
import subprocess
import time
import logging
from typing import Tuple

import sounddevice as sd
from rich.table import Table
from rich.panel import Panel
from rich.live import Live

from spytorec import state
from spytorec.audio import audio_callback_factory
from spytorec.config import console
from spytorec.utils import file_lock, CONFIG_FILE_PATH, LOCK_FILE_PATH, KBHit
from spytorec.ui import SP_GREEN, SP_GREEN_DIM, SP_GREY

# Platform-specific imports for hardware wizard input
if os.name == 'nt':
    import msvcrt
else:
    import select


def discover_hardware(ffmpeg_path: str, cfg) -> Tuple[str, int, int, int]:
    """Interactive hardware wizard with better error handling."""
    state.set_state(state.STATE_IDLE, "Hardware Discovery")
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
                capture_output=True, text=True, errors='ignore', timeout=10
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
                    with state.meter_lock:
                        state.meter_data[i] = 0.0
            except Exception:
                pass

    if not active_idx:
        console.print("[red]No suitable input devices found![/red]")
        sys.exit(1)

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
            for i in active_idx:
                sr = int(devices[i]['default_samplerate'])
                ch = devices[i]['max_input_channels']
                level = min(40, int(state.meter_data.get(i, 0) * 40))

                has_audio = state.meter_peaks.get(i, 0) > state.AUDIO_THRESHOLD
                style = SP_GREEN_DIM if has_audio else "dim"

                t.add_row(
                    f"[{count}]",
                    f"[{style}]{devices[i]['name']}[/{style}]",
                    f"[{style}]{sr}Hz | {ch}ch[/{style}]",
                    f"[{SP_GREEN_DIM}]" + "\u2588" * level + f"[/{SP_GREEN_DIM}]" if has_audio else "[dim]" + "\u2501" * 5 + "[/dim]"
                )
                rows[count] = dict(devices[i])
                rows[count]['idx'] = i
                count += 1

        return Panel(t, subtitle=f"[{SP_GREY}]Selection: {buf}[/{SP_GREY}]", border_style=SP_GREEN_DIM), rows

    console.print("[yellow]Press a number key to pick a device (Enter to confirm a multi-digit number)[/yellow]")

    def apply_key(c):
        """Feed one character into the selection buffer. Returns a chosen row or None."""
        nonlocal buf
        if c in ('\r', '\n'):
            if buf.isdigit() and int(buf) in rows:
                return rows[int(buf)]
            buf = ""
        elif c in ('\x7f', '\x08'):
            buf = buf[:-1]
        elif c.isdigit():
            buf += c
            # Auto-confirm as soon as the buffer is an unambiguous complete match
            # (i.e. no longer device number starts with these digits).
            if int(buf) in rows and not any(
                str(k).startswith(buf) and len(str(k)) > len(buf) for k in rows
            ):
                return rows[int(buf)]
        return None

    # cbreak + raw os.read() here: select() plus buffered sys.stdin.read()
    # strands bytes in Python's buffer and the wizard hangs.
    kb = None
    if os.name != 'nt':
        try:
            kb = KBHit()
            kb.set_cbreak()
        except Exception as e:
            # stdin isn't a real terminal (piped/redirected) — os.read still works.
            logging.debug(f"Could not set cbreak for hardware wizard: {e}")
            kb = None

    try:
        with Live(build_hw_table()[0], refresh_per_second=10, screen=True) as live:
            while not selected:
                try:
                    panel, rows = build_hw_table()
                    live.update(panel)

                    if os.name == 'nt':
                        while msvcrt.kbhit() and not selected:
                            c = msvcrt.getch()
                            if c in (b'\r', b'\n', b'\x08') or c.isdigit():
                                selected = apply_key(c.decode('ascii', 'ignore')) or selected
                    else:
                        if select.select([sys.stdin], [], [], 0.05)[0]:
                            try:
                                chunk = os.read(sys.stdin.fileno(), 64).decode('utf-8', 'ignore')
                            except (OSError, BlockingIOError):
                                chunk = ""
                            for c in chunk:
                                selected = apply_key(c) or selected
                                if selected:
                                    break

                    time.sleep(0.05)
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    logging.debug(f"Hardware selection error: {e}")
                    time.sleep(0.2)
    finally:
        if kb is not None:
            kb.set_normal_term()

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
    cfg.set('Recording', 'channels', str(min(2, selected['max_input_channels'])))

    try:
        with file_lock(LOCK_FILE_PATH):
            with open(CONFIG_FILE_PATH, 'w', encoding='utf-8') as f:
                cfg.write(f)
    except Exception as e:
        console.print(f"[yellow]Could not save config: {e}[/yellow]")

    return best_name, selected['idx'], int(selected['default_samplerate']), min(2, selected['max_input_channels'])
