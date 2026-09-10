"""Configuration loading, validation, and logging setup."""

import sys
import logging
import platform
import configparser
from logging.handlers import RotatingFileHandler

from rich.console import Console

from spytorec.utils import resolve_path, file_lock, CONFIG_FILE_PATH, LOCK_FILE_PATH
from spytorec.state import SCRIPT_VERSION


def _widen_console_encoding() -> None:
    """
    Encodes console output as UTF-8, replacing what a stream cannot carry.

    A redirected stream on Windows takes the ANSI code page, which carries no
    tick, cross or meter block.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, 'reconfigure'):
            continue
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (OSError, ValueError):
            pass


_widen_console_encoding()
console = Console()

DEFAULT_CONFIG = {
    'Recording': {
        'device_id': '', 'ffmpeg_name': '', 'sample_rate': '48000', 'bit_depth': '24',
        'channels': '2', 'output_format': 'flac', 'output_directory': 'Recordings',
        'auto_start': 'false', 'overwrite_existing': 'false', 'force_safe_mode': 'false',
        'max_retries': '3', 'retry_delay': '1', 'force_unity_gain': 'true'
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
        'naming_format': '{track_no}. {artist} - {title}',
        'organize_by': 'artist/album'
    },
    'Webhooks': {
        'discord_url': '',
        'notify_on_track_saved': 'true',
        'notify_on_session_end': 'true'
    },
    'SpotifyAPI': {
        'SPOTIPY_CLIENT_ID': '', 'SPOTIPY_CLIENT_SECRET': ''
    }
}


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
            with file_lock(LOCK_FILE_PATH):
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
