"""Terminal UI: dashboard builder, audio meters, history tables, and album art."""

import io
import time
import shutil
import logging
from typing import Dict, List, Optional

import numpy as np
from PIL import Image
from rich.text import Text
from rich.table import Table
from rich.panel import Panel
from rich.progress_bar import ProgressBar

from spytorec import state
from spytorec.utils import resolve_path, fetch_image_bytes

# --- Spotify-Branded Color Theme ---
SP_GREEN = "bold #1DB954"
SP_GREEN_DIM = "#1DB954"
SP_WHITE = "bold white"
SP_GREY = "grey70"
SP_DARK = "grey30"
SP_BAR_BORDER = "#1DB954"
SP_IDLE_BORDER = "#535353"
SP_REC_RED = "bold red"
SP_SKIP_YELLOW = "bold yellow"

# Album art cache (capped at 50 entries)
_album_art_cache: Dict[str, List[str]] = {}
_ALBUM_ART_CACHE_MAX = 50

# Cached log text (refreshed every 2 seconds to avoid disk I/O on every frame)
_cached_logs = ""
_cached_logs_time = 0.0


def get_health_indicator(rms: float) -> str:
    """Returns health status based on RMS level."""
    if rms > 0.85:
        return "[bold red]\U0001f534 Clipping[/bold red]"
    if rms < 0.001:
        return "[yellow]\U0001f7e1 Low[/yellow]"
    return f"[{SP_GREEN_DIM}]\U0001f7e2 Good[/{SP_GREEN_DIM}]"


def build_gradient_bar(rms: float, peak: float, width: int = 40, show_peak: bool = True) -> str:
    """Builds a gradient level meter bar with dB readout."""
    if np.isnan(rms) or rms <= 1e-6:
        return "[dim]\u2501[/dim]" * width + "  [dim]  -∞ dB[/dim]"

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
                bar_chars.append(f"[{SP_GREEN_DIM}]\u2588[/{SP_GREEN_DIM}]")
            elif i < width * 0.85:
                bar_chars.append("[yellow]\u2588[/yellow]")
            else:
                bar_chars.append("[red]\u2588[/red]")
        else:
            bar_chars.append("[dim]\u2501[/dim]")

    db_str = f"{db:+.1f} dB"
    if db > -3:
        db_color = "bold red"
    elif db > -12:
        db_color = "yellow"
    else:
        db_color = SP_GREEN_DIM

    return "".join(bar_chars) + f"  [{db_color}]{db_str}[/{db_color}]"


def format_time_ms(ms: int) -> str:
    """Formats milliseconds as MM:SS."""
    minutes = ms // 60000
    seconds = (ms // 1000) % 60
    return f"{minutes:02d}:{seconds:02d}"


def fetch_album_art_ascii(track: Dict, width: int = 12, height: int = 6) -> List[str]:
    """Downloads album art and converts to half-block ASCII art. Cached per track ID."""
    track_id = track.get('id', '')
    if track_id in _album_art_cache:
        return _album_art_cache[track_id]

    # Evict oldest entries if cache is full
    if len(_album_art_cache) >= _ALBUM_ART_CACHE_MAX:
        try:
            oldest_key = next(iter(_album_art_cache))
            del _album_art_cache[oldest_key]
        except StopIteration:
            pass

    blank = [f"[dim]{'░' * width}[/dim]"] * height
    try:
        images = track.get('album', {}).get('images', [])
        if not images:
            _album_art_cache[track_id] = blank
            return blank

        img_url = images[-1]['url']
        img_data = fetch_image_bytes(img_url, timeout=2)
        if not img_data:
            _album_art_cache[track_id] = blank
            return blank

        img = Image.open(io.BytesIO(img_data)).convert('RGB')
        img = img.resize((width, height * 2), Image.LANCZOS)

        lines = []
        for y in range(0, height * 2, 2):
            line_parts = []
            for x in range(width):
                r1, g1, b1 = img.getpixel((x, y))
                r2, g2, b2 = img.getpixel((x, min(y + 1, height * 2 - 1)))
                line_parts.append(
                    f"[rgb({r1},{g1},{b1}) on rgb({r2},{g2},{b2})]▀[/rgb({r1},{g1},{b1}) on rgb({r2},{g2},{b2})]"
                )
            lines.append("".join(line_parts))

        _album_art_cache[track_id] = lines
        return lines

    except Exception as e:
        logging.debug(f"Album art ASCII conversion failed: {e}")
        _album_art_cache[track_id] = blank
        return blank


def build_shortcut_bar() -> Text:
    """Builds a color-coded keyboard shortcut bar."""
    return Text.from_markup(
        f"  [{SP_REC_RED}]Q[/{SP_REC_RED}][dim] Quit[/dim]"
        f"   [cyan]D[/cyan][dim] Debug[/dim]"
        f"   [yellow]F[/yellow][dim] Safe Mode[/dim]"
        f"   [{SP_GREEN_DIM}]S[/{SP_GREEN_DIM}][dim] Skip Track[/dim]"
    )


def build_history_table(track_history) -> Optional[Panel]:
    """Builds a structured track history table. Returns None if history is empty."""
    if not track_history:
        return None

    history = Table(show_header=True, header_style=SP_DARK, expand=True, box=None, padding=(0, 1))
    history.add_column("", width=2)
    history.add_column("Artist", style=SP_GREY, max_width=20, no_wrap=True)
    history.add_column("Track", max_width=30, no_wrap=True)
    history.add_column("Size", justify="right", style=SP_GREY, width=8)
    history.add_column("Status", justify="center", width=6)

    for entry in track_history:
        if entry['status'] == 'ok':
            icon = f"[{SP_GREEN_DIM}]✅[/{SP_GREEN_DIM}]"
            status_text = f"[{SP_GREEN_DIM}]OK[/{SP_GREEN_DIM}]"
            size_str = f"{entry['size']}MB"
        elif entry['status'] == 'skip':
            icon = "[yellow]⏭[/yellow]"
            status_text = "[yellow]SKIP[/yellow]"
            size_str = "—"
        else:
            icon = "[red]❌[/red]"
            status_text = "[red]FAIL[/red]"
            size_str = "—"

        history.add_row(icon, entry['artist'], entry['name'], size_str, status_text)

    return Panel(history, title=f"[{SP_DARK}]Recent Recordings[/{SP_DARK}]", border_style=SP_DARK)


def get_tail_logs(cfg, n: int = 3) -> str:
    """Safely retrieves end of log file for UI display. Cached for 2 seconds."""
    global _cached_logs, _cached_logs_time

    if not cfg['Diagnostics'].getboolean('enable_logging'):
        return "Logging Disabled"

    now = time.time()
    if now - _cached_logs_time < 2.0:
        return _cached_logs

    path = resolve_path(cfg['Diagnostics'].get('log_file'))
    try:
        if not path.exists():
            result = "Waiting for logs..."
        else:
            with open(path, 'rb') as f:
                f.seek(0, 2)
                file_size = f.tell()
                if file_size == 0:
                    result = "Waiting for logs..."
                else:
                    read_size = min(file_size, 4096)
                    f.seek(-read_size, 2)
                    data = f.read().decode('utf-8', errors='replace')
                    lines = [l.strip() for l in data.split('\n') if l.strip()]
                    result = '\n'.join(lines[-n:])
    except Exception:
        result = "Waiting for logs..."

    _cached_logs = result
    _cached_logs_time = now
    return result


def build_dashboard(
    playback, track, file_size, session_tracks_ok, session_total_bytes,
    session_start_time, track_history, failed_recordings,
    sr, ch, bit_depth, hw_name, output_format, out_dir, cfg
) -> Panel:
    """Unified dashboard builder for both recording and idle states."""
    is_playing = playback and playback.get('is_playing') and track
    cur_state = state.get_state()

    dashboard = Table.grid(expand=True)

    if is_playing:
        progress = playback.get('progress_ms', 0)
        duration = track.get('duration_ms', 1)

        if cur_state == state.STATE_RECORDING:
            rec_indicator = f"[{SP_REC_RED}]REC ●[/{SP_REC_RED}]"
        elif cur_state == state.STATE_SKIPPED:
            rec_indicator = f"[{SP_SKIP_YELLOW}]SKIP ⏭[/{SP_SKIP_YELLOW}]"
        elif cur_state == state.STATE_SWITCHING:
            rec_indicator = "[yellow]⟳ Switching...[/yellow]"
        else:
            rec_indicator = "[yellow]WAIT[/yellow]"

        track_info = Table.grid(expand=True)
        track_info.add_row(Text.from_markup(f"{rec_indicator}  [{SP_WHITE}]{track['name']}[/{SP_WHITE}]"))
        track_info.add_row(Text(f"Artist: {track['artists'][0]['name']}", style=SP_GREY))
        track_info.add_row(Text(f"Album:  {track['album']['name']}", style=SP_GREY))

        elapsed_str = format_time_ms(progress)
        total_str = format_time_ms(duration)

        progress_row = Table.grid(expand=True)
        progress_row.add_column(width=6)
        progress_row.add_column(ratio=1)
        progress_row.add_column(width=6, justify="right")
        progress_row.add_row(
            Text(elapsed_str, style=SP_GREEN_DIM),
            ProgressBar(total=duration, completed=progress, width=None,
                        complete_style=SP_GREEN_DIM, finished_style=SP_GREEN_DIM),
            Text(total_str, style=SP_DARK)
        )
        track_info.add_row(progress_row)

        art_lines = fetch_album_art_ascii(track)
        art_text = "\n".join(art_lines)

        layout_table = Table.grid(expand=True)
        layout_table.add_column(width=14)
        layout_table.add_column(ratio=1)
        layout_table.add_row(Text.from_markup(art_text), track_info)

        dashboard.add_row(layout_table)

        if failed_recordings:
            dashboard.add_row(Text(f"⚠️ Failed recordings: {len(failed_recordings)}", style="bold red"))

        dashboard.add_section()

        if cfg['UI'].getboolean('show_status_strip'):
            safe_mode_active = cfg['Recording'].getboolean('force_safe_mode')
            safe_tag = f"[{SP_SKIP_YELLOW}](SAFE) [/{SP_SKIP_YELLOW}]" if safe_mode_active else ""

            status_items = []
            if cfg['QualityDisplay'].getboolean('show_sample_rate'):
                status_items.append(f"{sr}Hz")
            if cfg['QualityDisplay'].getboolean('show_bit_depth'):
                status_items.append(f"{bit_depth}-bit")
            if cfg['QualityDisplay'].getboolean('show_channels'):
                status_items.append(f"{ch}ch")

            tech_info = " | ".join(status_items) if status_items else ""
            status_line = f"{safe_tag}{tech_info} | {output_format.upper()} | {file_size}MB"
            dashboard.add_row(Text.from_markup(f"[{SP_DARK}]⚙️ {status_line}[/{SP_DARK}]"))

        if cfg['QualityDisplay'].getboolean('show_lr_meters'):
            show_peak = cfg['QualityDisplay'].getboolean('show_peak_hold')
            dashboard.add_row(Text.from_markup(f"[{SP_DARK}]L[/{SP_DARK}] {build_gradient_bar(state.smoothed_rms_l, state.peak_l, show_peak=show_peak)}"))
            dashboard.add_row(Text.from_markup(f"[{SP_DARK}]R[/{SP_DARK}] {build_gradient_bar(state.smoothed_rms_r, state.peak_r, show_peak=show_peak)}"))

            if state.mono_warning_frames > 30:
                dashboard.add_row(Text("[yellow]⚠️ Warning: Possible mono input detected[/yellow]"))

        health = get_health_indicator(state.peak_l)
        dashboard.add_row(Text.from_markup(f"[{SP_DARK}]Signal:[/{SP_DARK}] {health}"))

    else:
        idle_grid = Table.grid(expand=True)
        idle_grid.add_row(Text.from_markup(f"[{SP_GREEN}]🎵 Waiting for Spotify...[/{SP_GREEN}]    [dim italic]Listening for audio ♪[/dim italic]"))
        idle_grid.add_row(Text(f"Device: {hw_name} ({sr}Hz / {bit_depth}-bit / {ch}ch)", style=SP_DARK))

        if state.smoothed_rms_l > 0.0001:
            idle_grid.add_row(Text.from_markup(
                f"[{SP_DARK}]Signal:[/{SP_DARK}] {build_gradient_bar(state.smoothed_rms_l, state.peak_l, width=30, show_peak=False)}"
            ))

        if failed_recordings:
            idle_grid.add_row(Text(f"⚠️ Failed recordings: {len(failed_recordings)}", style="bold red"))

        dashboard.add_row(idle_grid)

    # Debug overlay
    if cfg['Debug'].getboolean('show_debug_overlay'):
        dashboard.add_section()
        pid = state.ffmpeg_process.pid if state.ffmpeg_process else 'None'
        dashboard.add_row(Text.from_markup(
            f"[dim]PID: {pid} | State: {cur_state} | L={state.raw_l:.3f} R={state.raw_r:.3f} | Mono frames: {state.mono_warning_frames}[/dim]"
        ))

    dashboard.add_section()

    # Session statistics
    elapsed = time.time() - session_start_time
    elapsed_str = f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m {int(elapsed % 60)}s"
    total_mb = round(session_total_bytes / (1024 ** 2), 1)
    try:
        disk_free = round(shutil.disk_usage(out_dir).free / (1024 ** 3), 1)
        disk_str = f" | Disk: {disk_free} GB free"
    except Exception:
        disk_str = ""
    stats_line = f"📊 {session_tracks_ok} tracks | {total_mb} MB | {elapsed_str}{disk_str}"
    dashboard.add_row(Text.from_markup(f"[{SP_DARK}]{stats_line}[/{SP_DARK}]"))

    # Track history
    history_panel = build_history_table(track_history)
    if history_panel:
        dashboard.add_row(history_panel)

    # Shortcut bar
    dashboard.add_row(build_shortcut_bar())

    # System log
    if cfg['Diagnostics'].getboolean('enable_logging'):
        logs = get_tail_logs(cfg, 3)
        if logs:
            dashboard.add_row(Panel(
                Text.from_markup(f"[dim]{logs}[/dim]"),
                title=f"[{SP_DARK}]System Log[/{SP_DARK}]",
                border_style=SP_DARK
            ))

    # Panel wrapper
    if is_playing and cur_state == state.STATE_RECORDING:
        border_style = SP_BAR_BORDER
        title_str = f"[{SP_GREEN}]SpytoRec v{state.SCRIPT_VERSION}[/{SP_GREEN}] [dim]— Recording[/dim]"
    elif is_playing:
        border_style = "yellow"
        title_str = f"[{SP_GREEN}]SpytoRec v{state.SCRIPT_VERSION}[/{SP_GREEN}]"
    else:
        border_style = SP_IDLE_BORDER
        title_str = f"[{SP_GREEN}]SpytoRec v{state.SCRIPT_VERSION}[/{SP_GREEN}]"

    subtitle = f"[{SP_DARK}]{hw_name} • {output_format.upper()}[/{SP_DARK}]"

    return Panel(dashboard, title=title_str, subtitle=subtitle, border_style=border_style)
