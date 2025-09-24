#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# SpytoRec - Spotify Track Recorder
#
# Author: @Darkphoenix
# GitHub: https://github.com/Danidukiyu
# Version: 1.2 
#
# Description:
#   A Python script to record currently playing Spotify tracks with high
#   accuracy. It automatically splits tracks, embeds metadata (title, artist,
#   album, cover art), and can organize recordings into an Artist/Album
#   directory structure. Features a configuration file for defaults,
#   interactive API key setup, asynchronous finalization for responsiveness,
#   audio header rewriting for player compatibility, and various command-line
#   options and subcommands for customization and utility.
#
# License:
#   This project is licensed under the MIT License.
#   Refer to the LICENSE file in the repository for full details.
#
# Disclaimer:
#   This script is intended for personal, private use only. Users are solely
#   responsible for ensuring their use complies with all applicable laws and
#   Spotify's Terms of Service regarding content recording.
# -----------------------------------------------------------------------------

import os
import time
import argparse
import subprocess
import requests
import json
import sys
import re 
from datetime import datetime, timezone
from pathlib import Path
from mutagen.oggvorbis import OggVorbis
from mutagen.flac import FLAC, Picture
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.markup import escape
from rich.prompt import Prompt
from rich.table import Table 
import traceback
import queue 
import threading
import configparser 

# --- Constants ---
SCRIPT_VERSION = "1.2" # Script version, keep in sync with argparse
SPOTIPY_REDIRECT_URI = 'http://127.0.0.1:8888/callback'
console = Console()
current_ffmpeg_process = None
current_recording_info = {}
finalization_task_queue = queue.Queue()
stop_worker_event = threading.Event()
CONFIG_FILE_NAME = "config.ini"
CONFIG_FILE_PATH = Path(__file__).resolve().parent / CONFIG_FILE_NAME

# --- Intro Banner ---
def display_intro_banner():
    console.print(Panel(
        Text.from_markup(
            f"[bold sky_blue1]SpytoRec - Spotify Track Recorder[/bold sky_blue1]\n"
            f"Developed by: [bold magenta]@Darkphoenix[/bold magenta]\n"
            f"GitHub: [link=https://github.com/Danidukiyu]https://github.com/Danidukiyu[/link]\n"
            f"Version: {SCRIPT_VERSION}"
        ),
        title="[white]Welcome[/white]",
        border_style="magenta",
        expand=False,
        padding=(1, 2)
    ))
    console.print() # Add a blank line after the banner

# --- Legal Disclaimer (Printed to console) ---
DISCLAIMER_TEXT = """
[bold red]Disclaimer:[/bold red]
This script is intended for personal, private use only.
Recording copyrighted material from streaming services may violate their Terms of Service
and/or copyright laws in your country. Users are solely responsible for ensuring their use
of this script complies with all applicable laws and terms of service.
The developers of this script assume no liability for any misuse.

[bold yellow]Recommendation:[/bold yellow] For best results, please ensure 'Crossfade songs'
and 'Automix' are DISABLED in your Spotify client's playback settings.
"""

def display_disclaimer():
    console.print(Panel(Text.from_markup(DISCLAIMER_TEXT), title="[bold yellow]Important Notice[/bold yellow]", border_style="yellow", expand=False))
    console.print("")

# --- Credential and Config Handling ---
def _create_template_config(config_path: Path):
    console.print(f"[yellow]Creating template configuration file at: {config_path}[/yellow]")
    config = configparser.ConfigParser(allow_no_value=True) 
    config['SpotifyAPI'] = {
        'SPOTIPY_CLIENT_ID': 'YOUR_CLIENT_ID_HERE',
        'SPOTIPY_CLIENT_SECRET': 'YOUR_CLIENT_SECRET_HERE',
        '# Instructions:': None,
        '# 1. Replace YOUR_CLIENT_ID_HERE and YOUR_CLIENT_SECRET_HERE with your actual credentials.': None,
    }
    config['GeneralSettings'] = {
        '# Instructions: Uncomment (remove "#") and edit values below to set your preferred defaults.': None,
        '# output_directory': 'Recordings',
        '# default_format': 'flac',
        '# default_quality_ogg': '7',
        '# polling_interval_seconds': '0.5',
        '# audio_device': 'audio=CABLE Output (VB-Audio Virtual Cable) ; For Windows. Mac/Linux users: see --help for device info',
        '# ffmpeg_path': 'ffmpeg',
        '# min_duration_seconds': '25',
        '# recording_buffer_seconds': '-0.2',
        '# skip_existing_file': 'false',
        '# organize_by_artist_album': 'false  (true or false, creates Artist/Album/track.flac structure)'
    }
    try:
        with open(config_path, 'w', encoding='utf-8') as configfile: config.write(configfile)
        console.print(f"[green]Template config file '{config_path.name}' created.[/green]")
        console.print(f"[bold yellow]PLEASE EDIT this file with your Spotify API credentials (under [SpotifyAPI]) and then re-run the script.[/bold yellow]")
    except OSError as e:
        console.print(f"[red]Error creating template config file '{config_path.name}': {e}[/red]")

def get_spotify_credentials():
    client_id_env = os.environ.get('SPOTIPY_CLIENT_ID')
    client_secret_env = os.environ.get('SPOTIPY_CLIENT_SECRET')
    if client_id_env and client_secret_env:
        console.print("[grey50]Using Spotify credentials from environment variables.[/grey50]")
        return client_id_env, client_secret_env
    config = configparser.ConfigParser(allow_no_value=True)
    ask_user_for_creds = False
    if CONFIG_FILE_PATH.exists():
        try:
            config.read(CONFIG_FILE_PATH, encoding='utf-8') 
            if config.has_section('SpotifyAPI'):
                cid = config.get('SpotifyAPI', 'SPOTIPY_CLIENT_ID', fallback=None)
                cs = config.get('SpotifyAPI', 'SPOTIPY_CLIENT_SECRET', fallback=None)
                if cid and cid not in ['YOUR_CLIENT_ID_HERE', ''] and cs and cs not in ['YOUR_CLIENT_SECRET_HERE', '']:
                    console.print(f"[grey50]Using Spotify credentials from '{CONFIG_FILE_PATH.name}'.[/grey50]")
                    return cid, cs
                else: ask_user_for_creds = True; console.print(f"[yellow]Config '{CONFIG_FILE_PATH.name}' has placeholder/missing API keys.[/yellow]")
            else: ask_user_for_creds = True; console.print(f"[yellow]Section [SpotifyAPI] missing in '{CONFIG_FILE_PATH.name}'.[/yellow]")
        except configparser.Error as e:
            ask_user_for_creds = True; console.print(f"[red]Error reading '{CONFIG_FILE_PATH.name}': {e}. Reconfigure.[/red]")
            config = configparser.ConfigParser(allow_no_value=True)
    else: ask_user_for_creds = True; console.print(f"[yellow]Config file '{CONFIG_FILE_PATH.name}' not found.[/yellow]")

    if ask_user_for_creds:
        if not CONFIG_FILE_PATH.exists(): 
            _create_template_config(CONFIG_FILE_PATH)
            console.print(f"[bold yellow]Template '{CONFIG_FILE_NAME}' created. Please edit it with your credentials and re-run, or provide them now.[/bold yellow]")
            
        console.print(Panel(
            "To use this script, you need Spotify API credentials (Client ID & Secret).\n"
            "These will be saved to a configuration file named '[bold cyan]config.ini[/bold cyan]' in the same directory as the script.\n\n"
            "[green]Steps to get credentials:[/green]\n"
            "1. Go to the Spotify Developer Dashboard: [link=dashboard.spotify.com]dashboard.spotify.com[/link]\n"
            "2. Log in and 'Create an App'.\n"
            "3. Note down the 'Client ID' and 'Client Secret'.\n"
            "4. In your App settings on the dashboard, add this Redirect URI: [bold]http://127.0.0.1:8888/callback[/bold]\n"
            "5. Enter the credentials below when prompted.",
            title="[yellow]Spotify API Credentials Setup[/yellow]", border_style="yellow", expand=False
        ))
        
        new_cid = ""
        while not new_cid:
            new_cid = Prompt.ask("Enter your Spotify Client ID").strip()
            if not new_cid: console.print("[red]Client ID cannot be empty. Please try again.[/red]")
            
        new_cs = ""
        while not new_cs:
            new_cs = Prompt.ask("Enter your Spotify Client Secret", password=True).strip()
            if not new_cs: console.print("[red]Client Secret cannot be empty. Please try again.[/red]")

        try:
            if not config.has_section('SpotifyAPI'): config.add_section('SpotifyAPI')
            config.set('SpotifyAPI', 'SPOTIPY_CLIENT_ID', new_cid)
            config.set('SpotifyAPI', 'SPOTIPY_CLIENT_SECRET', new_cs)
            config.set('SpotifyAPI', '#comment1', 'Spotify API credentials.')
            config.set('SpotifyAPI', '#comment2', 'If you need to change these, edit this file or delete it to re-trigger setup.')

            if not config.has_section('GeneralSettings'): 
                config.add_section('GeneralSettings')
                config.set('GeneralSettings', '# Instructions: Uncomment (remove "#") and edit values below to set your preferred defaults.', None)
                config.set('GeneralSettings', '# output_directory', 'Recordings')
                config.set('GeneralSettings', '# default_format', 'flac')
                config.set('GeneralSettings', '# default_quality_ogg', '7')
                config.set('GeneralSettings', '# polling_interval_seconds', '0.5')
                config.set('GeneralSettings', '# audio_device', 'audio=CABLE Output (VB-Audio Virtual Cable) ; For Windows. Check --help for your OS.')
                config.set('GeneralSettings', '# ffmpeg_path', 'ffmpeg')
                config.set('GeneralSettings', '# min_duration_seconds', '25')
                config.set('GeneralSettings', '# recording_buffer_seconds', '-0.2')
                config.set('GeneralSettings', '# skip_existing_file', 'false')
                config.set('GeneralSettings', '# organize_by_artist_album', 'false')

            with open(CONFIG_FILE_PATH, 'w', encoding='utf-8') as cf: config.write(cf)
            console.print(f"[green]Credentials and settings template saved to '{CONFIG_FILE_PATH.name}'.[/green]")
            console.print("[bold yellow]Please re-run the script now.[/bold yellow]")
            exit(0) 
        
        except OSError as e:
            console.print(f"[red]Error saving credentials to '{CONFIG_FILE_PATH.name}': {e}[/red]")
        except configparser.Error as e:
            console.print(f"[red]Error preparing config data to save: {e}[/red]")
        
        console.print("[yellow]Could not save credentials to config file. Please set SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET environment variables as a fallback.[/yellow]")
        exit(1)
    
    console.print("[bold red]Fatal: Could not obtain Spotify credentials after all checks.[/bold red]"); exit(1)

# --- Helper function to get typed config values ---
def get_typed_config(cfg_parser: configparser.ConfigParser, section: str, option: str, o_type, default):
    if cfg_parser.has_section(section) and cfg_parser.has_option(section, option): 
        val = cfg_parser.get(section, option)
        if not val or val.strip() == "" or val.strip().startswith(('#',';')): 
            return default
        try:
            if o_type == bool: return cfg_parser.getboolean(section, option)
            if o_type == int: return cfg_parser.getint(section, option)
            if o_type == float: return cfg_parser.getfloat(section, option)
            if o_type == Path: return Path(val)
            return val
        except ValueError: console.print(f"[yellow]Warning: Invalid config value for '{option}' in section '[{section}]'. Using script default: '{default}'[/yellow]"); return default
    return default

# --- Helper functions (load_recorded_ids, format_time, get_current_track, etc.) ---
def load_recorded_ids(log_file_path: Path):
    ids = set()
    if log_file_path.exists():
        with open(log_file_path, 'r', encoding='utf-8') as f:
            for line in f:
                try: entry = json.loads(line); ids.add(entry['track_id'])
                except (json.JSONDecodeError, KeyError): pass
    return ids

def format_time(seconds):
    if seconds is None or not isinstance(seconds, (int, float)) or seconds < 0: return "--:--"
    minutes = int(seconds // 60); secs = int(seconds % 60)
    return f"{minutes:02d}:{secs:02d}"

def get_current_track(sp: Spotify):
    try:
        current = sp.current_playback()
        if not current: return None
        is_playing = current.get('is_playing', False); item = current.get('item')
        if not item or item.get('type') != 'track': return None
        artists = [a['name'] for a in item.get('artists', [])]
        return {'id': item['id'], 'name': item['name'], 'artists': artists, 'artist_str': ' & '.join(artists),
                'album': item.get('album', {}).get('name', 'Unknown Album'),
                'cover_url': item.get('album', {}).get('images', [{}])[0].get('url') if item.get('album', {}).get('images') else None,
                'duration_ms': item['duration_ms'], 'is_playing': is_playing}
    except requests.exceptions.ReadTimeout: console.print("[yellow]Spotify API Read Timeout.[/yellow]")
    except Exception as e: console.print(f"[red]Error fetching playback: {e}\n{escape(traceback.format_exc())}[/red]")
    return None

def download_cover(url: str, cover_path: Path):
    if not url: return False
    try:
        r = requests.get(url, timeout=10); r.raise_for_status()
        with open(cover_path, 'wb') as f: f.write(r.content)
        return True
    except requests.exceptions.RequestException as e: console.print(f"[red][Worker] Cover DL error: {e}[/red]"); return False

def embed_metadata(audio_path: Path, metadata: dict, cover_path: Path):
    try:
        if not audio_path.exists() or audio_path.stat().st_size == 0: return
        if audio_path.suffix == '.ogg': audio = OggVorbis(audio_path)
        elif audio_path.suffix == '.flac': audio = FLAC(audio_path)
        else: return
        audio['TITLE'] = metadata['name']; audio['ARTIST'] = metadata['artist_str']; audio['ALBUM'] = metadata['album']
        if audio_path.suffix == '.flac' and cover_path.exists():
            img = Picture(); img.data = cover_path.read_bytes()
            img.type = 3; img.mime = 'image/jpeg' 
            audio.add_picture(img)
        audio.save()
    except Exception as e: console.print(f"[red][Worker] Metadata embed error for {audio_path.name}: {e}[/red]")

def sanitize_for_filesystem(text: str, max_len: int = 70):
    text = "".join(c if c.isalnum() or c in " ._-" else "_" for c in text).strip()
    text = re.sub(r'[_ ]{2,}', '_', text) 
    return text[:max_len].strip('_')

def generate_safe_filename(artist_str: str, name: str, audio_format: str): 
    s_track_artist = sanitize_for_filesystem(artist_str)
    s_track_title = sanitize_for_filesystem(name)
    return f"{s_track_artist} - {s_track_title}.{audio_format}"

def rewrite_audio_file(audio_path: Path, ffmpeg_exe: str, console_instance: Console):
    if not audio_path.exists() or audio_path.stat().st_size < 1024: 
        console_instance.print(f"[yellow][Worker] Rewrite skipped for '{audio_path.name}' (missing or too small).[/yellow]")
        return False 
    
    original_stem = audio_path.stem
    original_suffix = audio_path.suffix
    temp_audio_path = audio_path.parent / (original_stem + "_rewrite_temp" + original_suffix)

    console_instance.print(f"[grey50][Worker] Rewriting '{audio_path.name}' for headers...[/grey50]")
    cmd = [ffmpeg_exe, '-y', '-i', str(audio_path), '-acodec', 'copy', '-vn', '-map_metadata', '-1', str(temp_audio_path)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=120)
        if res.returncode == 0 and temp_audio_path.exists() and temp_audio_path.stat().st_size >= 1024:
            audio_path.unlink(missing_ok=True); temp_audio_path.rename(audio_path)
            console_instance.print(f"[green][Worker] Rewrote headers for '{audio_path.name}'.[/green]"); return True
        else:
            console_instance.print(f"[red][Worker] Rewrite failed for '{audio_path.name}' (Code: {res.returncode}). Original kept.[/red]")
            if res.stderr: console_instance.print(f"[grey70]{escape(res.stderr.strip())}[/grey70]")
            temp_audio_path.unlink(missing_ok=True); return False
    except Exception as e:
        console_instance.print(f"[red][Worker] Rewrite error for '{audio_path.name}': {e}. Original kept.[/red]")
        temp_audio_path.unlink(missing_ok=True); return False

def finalization_worker_function(ffmpeg_exe_cfg: str, log_file_path_global: Path):
    console.print("[cyan]Finalization worker started.[/cyan]")
    while not stop_worker_event.is_set() or not finalization_task_queue.empty():
        try:
            task = finalization_task_queue.get(timeout=1)
            proc, audio_p, meta, stop_r, start_iso, exp_dur, ffmpeg_p_arg = \
                task['process_obj'], task['audio_path'], task['metadata'], task['stop_reason'], \
                task['start_iso'], task['expected_duration_sec'], task.get('ffmpeg_path_arg', ffmpeg_exe_cfg)
            
            console.print(f"[grey50][Worker] Processing: '{meta['name']}' (Reason: {stop_r})[/grey50]")
            stderr_b = b""; exit_c = -99; graceful_stop_by_q = False
            
            if proc.poll() is None: 
                try:
                    _, stderr_b = proc.communicate(timeout=20) 
                    exit_c = proc.returncode
                    graceful_stop_by_q = task.get('q_sent_by_main', False) and (exit_c == 0 or exit_c == 255)

                except subprocess.TimeoutExpired:
                    console.print(f"[yellow][Worker] FFmpeg for '{meta['name']}' timed out post 'q'/communicate. Killing.[/yellow]")
                    proc.kill(); _, stderr_b = proc.communicate(timeout=5)
                    exit_c = proc.poll() if proc.poll() is not None else -1
            else: 
                exit_c = proc.poll()
                graceful_stop_by_q = task.get('q_sent_by_main', False) and (exit_c == 0 or exit_c == 255)
                try: 
                    if proc.stderr: stderr_b = proc.stderr.read()
                except Exception: pass 
            
            console.print(f"[grey50][Worker] FFmpeg exit for '{meta['name']}': {exit_c} (Graceful Q success: {graceful_stop_by_q})[/grey50]")
            
            file_ok_initially = audio_p.exists() and audio_p.stat().st_size > 1024
            is_early_stop_by_logic = stop_r in ["Track changed", "Playback stopped or track unavailable", "User interrupted", "Shutdown", "Main loop error"]
            is_valid_initial_exit = (exit_c == 0) or graceful_stop_by_q 
            
            if not (is_valid_initial_exit or (is_early_stop_by_logic and file_ok_initially)):
                console.print(f"[red][Worker] FFmpeg for '{meta['name']}' had an issue (Code: {exit_c}).[/red]")
                if stderr_b: console.print(f"[grey70]{escape(stderr_b.decode('utf-8', errors='ignore').strip())}[/grey70]")
                if audio_p.exists() and not file_ok_initially: 
                    audio_p.unlink(missing_ok=True)
                    console.print(f"[yellow][Worker] Deleted unusable file: {audio_p.name}[/yellow]")
                finalization_task_queue.task_done(); continue

            rewrite_ok = False
            if file_ok_initially: 
                rewrite_ok = rewrite_audio_file(audio_p, ffmpeg_p_arg, console)
            
            if audio_p.exists() and audio_p.stat().st_size > 1024:
                cover_p = audio_p.with_name(f"{audio_p.stem}_cover.jpg")
                cover_ok = download_cover(meta['cover_url'], cover_p)
                embed_metadata(audio_p, meta, cover_p)
                if cover_ok and cover_p.exists(): cover_p.unlink(missing_ok=True)
                
                end_iso = datetime.now(timezone.utc).isoformat()
                actual_dur = -1
                try: actual_dur = (datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)).total_seconds()
                except ValueError: pass
                
                log_entry = {'track_id': meta['id'], 'title': meta['name'], 
                             'artist_str': meta['artist_str'], 'album': meta['album'], 
                             'start_time': start_iso, 'end_time': end_iso,
                             'original_duration_sec': meta['duration_ms'] / 1000,
                             'ffmpeg_target_duration_sec': exp_dur,
                             'recorded_duration_seconds': round(actual_dur,2) if actual_dur !=-1 else "N/A", 
                             'header_rewrite_successful': rewrite_ok,
                             'ffmpeg_initial_exit_code': exit_c, 'stop_reason': stop_r, 
                             'filename': str(audio_p.relative_to(log_file_path_global.parent)), 
                             'format': audio_p.suffix.lstrip('.')}
                
                with open(log_file_path_global, 'a', encoding='utf-8') as f: f.write(json.dumps(log_entry) + '\n')
                console.print(f"[bold green][Worker] ✔ Finalized:[/bold green] {audio_p.name}\n")
            elif file_ok_initially and not rewrite_ok: 
                console.print(f"[red][Worker] File '{audio_p.name}' became unusable after failed rewrite. No metadata embedded.[/red]")
            elif not file_ok_initially:
                 console.print(f"[yellow][Worker] Initial file '{audio_p.name}' was too small or missing. Not finalizing.[/yellow]")
            else: 
                 console.print(f"[yellow][Worker] File '{audio_p.name}' not found or unusable for unknown reason. Not finalizing.[/yellow]")

            finalization_task_queue.task_done()
        except queue.Empty:
            if stop_worker_event.is_set() and finalization_task_queue.empty(): break
        except Exception as e:
            console.print(f"[bold red][Worker] Critical Error in finalization loop: {e}\n{escape(traceback.format_exc())}[/bold red]")
            try: finalization_task_queue.task_done() 
            except ValueError: pass 

    console.print("[cyan]Finalization worker stopped.[/cyan]")


def start_recording(track_m: dict, audio_fmt: str, out_dir_base: Path, ogg_q: int, dev_name: str, 
                    ffmpeg_exe: str, rec_buf: float, min_dur: int, organize: bool):
    global current_ffmpeg_process, current_recording_info
    dur_sec = track_m['duration_ms'] / 1000
    if dur_sec < min_dur: 
        console.print(f"[yellow]Track '{track_m['name']}' is too short ({dur_sec:.1f}s < {min_dur}s). Skipping.[/yellow]")
        return False

    file_name_part = generate_safe_filename(track_m['artist_str'], track_m['name'], audio_fmt)
    
    if organize:
        s_artist_folder = sanitize_for_filesystem(track_m.get('artists', [{}])[0].get('name', 'Unknown Artist'))
        s_album_folder = sanitize_for_filesystem(track_m['album'])
        target_dir_for_file = out_dir_base / s_artist_folder / s_album_folder
        audio_p_final = target_dir_for_file / file_name_part
    else:
        target_dir_for_file = out_dir_base
        audio_p_final = target_dir_for_file / file_name_part

    try: target_dir_for_file.mkdir(parents=True, exist_ok=True)
    except OSError as e: console.print(f"[red]Error creating dir '{target_dir_for_file}': {e}[/red]"); return False
    
    rec_for_duration = max(0.1, dur_sec + rec_buf)
    console.print(Panel.fit(
        f"🎧 Start: {track_m['artist_str']} - {track_m['name']} ({audio_fmt.upper()})\n"
        f"   To: [grey50]{audio_p_final}[/grey50]\n"
        f"   Target duration: ~{rec_for_duration:.1f}s", 
        title="[green]Recording Initiated[/green]", border_style="green"
    ))
    
    input_format = 'dshow' if os.name == 'nt' else ('avfoundation' if sys.platform == 'darwin' else 'alsa')
    cmd = [ffmpeg_exe, '-y', '-f', input_format, '-i', dev_name, '-t', str(rec_for_duration)]
    if audio_fmt == 'ogg': cmd += ['-acodec', 'libvorbis', '-qscale:a', str(ogg_q), '-vn']
    elif audio_fmt == 'flac': cmd += ['-acodec', 'flac', '-vn']
    else: console.print(f"[red]Unsupported format: {audio_fmt}[/red]"); return False
    cmd.append(str(audio_p_final))
    
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        current_ffmpeg_process = proc
        current_recording_info = {'process_obj': proc, 'track_id': track_m['id'], 
                                  'start_iso': datetime.now(timezone.utc).isoformat(),
                                  'audio_path': audio_p_final, 'metadata': track_m, 
                                  'expected_duration_sec': rec_for_duration, 
                                  'ffmpeg_path_arg': ffmpeg_exe}
        return True
    except FileNotFoundError:
        console.print(f"[bold red]Error: FFmpeg executable not found at '{ffmpeg_exe}'. Please check path or install FFmpeg.[/bold red]")
        return False
    except Exception as e: 
        console.print(f"[red]FFmpeg start error for {track_m['name']}: {e}[/red]")
        console.print(f"[grey50]Command attempted: {' '.join(cmd)}[/grey50]")
        return False

def submit_to_finalization_queue(reason: str):
    global current_ffmpeg_process, current_recording_info
    if not current_ffmpeg_process or not current_recording_info:
        if current_ffmpeg_process and current_ffmpeg_process.poll() is None: 
             try: current_ffmpeg_process.kill() 
             except Exception: pass
        current_ffmpeg_process = None; current_recording_info = {}; return

    rec_info_snap = current_recording_info.copy(); ffmpeg_proc_snap = current_ffmpeg_process
    current_ffmpeg_process = None; current_recording_info = {}   
    
    console.print(f"[yellow]Stop requested for '{rec_info_snap['metadata']['name']}' (Reason: {reason}). Queuing for finalization.[/yellow]")
    
    q_was_sent_by_main = False
    if ffmpeg_proc_snap.poll() is None: 
        try:
            if ffmpeg_proc_snap.stdin and not ffmpeg_proc_snap.stdin.closed:
                console.print(f"[grey50]Sending 'q' to FFmpeg for '{rec_info_snap['metadata']['name']}'...[/grey50]")
                ffmpeg_proc_snap.stdin.write(b'q') 
                ffmpeg_proc_snap.stdin.flush()
                q_was_sent_by_main = True
            else:
                console.print(f"[yellow]Stdin not available for 'q' on '{rec_info_snap['metadata']['name']}'. Worker will use terminate/kill.[/yellow]")
        except (OSError, BrokenPipeError, ValueError) as e: 
            console.print(f"[yellow]Error sending 'q' for '{rec_info_snap['metadata']['name']}': {e}. Worker handles termination.[/yellow]")

    task = rec_info_snap; task['process_obj'] = ffmpeg_proc_snap; task['stop_reason'] = reason
    task['q_sent_by_main'] = q_was_sent_by_main # Let worker know
    finalization_task_queue.put(task)

# --- Subcommand Functions ---
def execute_list_devices_command(args_list_devices: argparse.Namespace, config_obj: configparser.ConfigParser):
    ffmpeg_exe = args_list_devices.ffmpeg_path
    console.print(f"\n[bold cyan]Listing audio devices using FFmpeg path: '{ffmpeg_exe}'[/bold cyan]")
    cmd_to_run = None; guidance = ""
    if os.name == 'nt': 
        cmd_to_run = [ffmpeg_exe, '-list_devices', 'true', '-f', 'dshow', '-i', 'dummy']
        guidance = ("For Windows, find names under 'DirectShow audio devices' (e.g., \"Microphone (...)\", \"CABLE Output (...)\").\n"
                    "Use the name in quotes for --device, prefixed with 'audio='. Ex: --device \"audio=CABLE Output (VB-Audio Virtual Cable)\"")
    elif sys.platform == 'darwin':
        cmd_to_run = [ffmpeg_exe, '-f', 'avfoundation', '-list_devices', 'true', '-i', '""']
        guidance = ("For macOS, find AVFoundation devices and their indexes (e.g., \"[0] BlackHole 2ch\", \"[1] MacBook Pro Microphone\").\n"
                    "Use index OR name for --device. Ex: --device \"0\" or --device \"BlackHole 2ch\"")
    else: # Linux
        guidance = ("For Linux, use system tools to find device names/indexes:\n"
                    "  ALSA: `arecord -L` (look for card,device like 'hw:0,0')\n"
                    "  PulseAudio: `pactl list sources short` (look for source name or index; often use '.monitor' of an output sink for loopback).\n"
                    "Common FFmpeg --device values: 'default', 'pulse', 'hw:0,0'.")
    
    if guidance: console.print(Panel(Text.from_markup(guidance), title="[yellow]Device Identification Guidance[/yellow]", border_style="yellow", expand=False))
    
    if cmd_to_run:
        console.print(f"\nRunning FFmpeg command: [code]{' '.join(cmd_to_run)}[/code]")
        try:
            process = subprocess.Popen(cmd_to_run, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='ignore')
            stdout_output, _ = process.communicate(timeout=20)
            console.print("\n--- FFmpeg Output ---")
            if stdout_output and stdout_output.strip(): console.print(escape(stdout_output))
            else: console.print("[italic grey50]No specific device list output from FFmpeg. It might have listed general options or encountered an internal error. Check FFmpeg documentation for your OS if device names are unclear.[/italic grey50]")
        except FileNotFoundError: console.print(f"[bold red]Error: FFmpeg not found at '{ffmpeg_exe}'.[/bold red]")
        except subprocess.TimeoutExpired: console.print(f"[red]Error: FFmpeg command timed out.[/red]")
        except Exception as e: console.print(f"[red]Error listing devices: {e}[/red]")
    elif os.name != 'nt' and sys.platform != 'darwin':
        console.print("\n[italic]For detailed FFmpeg device enumeration on Linux, please refer to FFmpeg documentation for ALSA or PulseAudio input devices, or use the system commands mentioned above.[/italic]")

def execute_test_auth_command(args_test_auth: argparse.Namespace, config_obj: configparser.ConfigParser):
    console.print("\n[bold cyan]Testing Spotify API Authentication...[/bold cyan]")
    try:
        client_id, client_secret = get_spotify_credentials() 
        sp = Spotify(auth_manager=SpotifyOAuth(
            client_id=client_id, client_secret=client_secret,
            redirect_uri=SPOTIPY_REDIRECT_URI, scope="user-read-playback-state user-read-currently-playing",
            open_browser=False, requests_timeout=10 
        ))
        user = sp.current_user()
        if user and user.get('display_name'):
            console.print(f"[green]✔ Authentication successful for user: [bold]{user['display_name']}[/bold] ({user['id']})[/green]")
        else:
            console.print("[red]❌ Auth may have succeeded (token obtained) but failed to fetch user details clearly.[/red]")
            try:
                token_info = sp.auth_manager.get_access_token(check_cache=False)
                if token_info and token_info.get('access_token'): console.print("[green]✔ Token obtained directly. API access likely working.[/green]")
                else: console.print("[red]❌ Failed to obtain access token directly.[/red]"); return
            except Exception as token_err: console.print(f"[red]❌ Error during direct token fetch: {token_err}[/red]"); return

        console.print("\n[bold cyan]Fetching current playback state...[/bold cyan]")
        playback = sp.current_playback()
        if playback and playback.get('item'):
            console.print("[green]✔ Playback state context fetched.[/green]")
            if playback['item']['type'] == 'track':
                console.print(f"  Status:     {'Playing' if playback['is_playing'] else 'Paused'}")
                console.print(f"  Track:      {playback['item']['artists'][0]['name']} - {playback['item']['name']}")
                console.print(f"  Album:      {playback['item']['album']['name']}")
                prog_ms = playback.get('progress_ms',0); dur_ms = playback['item']['duration_ms']
                console.print(f"  Progress:   {format_time(prog_ms/1000)} / {format_time(dur_ms/1000)}")
            else: console.print(f"  Playing:    A '{playback['item']['type']}'.")
            if playback.get('device'): console.print(f"  Device:     {playback['device']['name']} ({playback['device']['type']})")
        else: console.print("  No active playback or nothing specific playing.")
    except Exception as e:
        console.print(f"[bold red]❌ Test authentication or playback fetch failed: {e}[/bold red]\n{escape(traceback.format_exc())}")

def execute_record_command(args: argparse.Namespace, config_obj: configparser.ConfigParser):
    global current_ffmpeg_process, current_recording_info 
    console.print(f"\n[bold green]Starting Record Mode[/bold green]")
    console.print(f"  Output Directory:      '{args.dir}'")
    console.print(f"  Audio Format:          {args.format.upper()} " + (f"(OGG Quality: {args.quality})" if args.format == 'ogg' else ""))
    console.print(f"  Organize by Artist/Album: {args.organize}")
    console.print(f"  Skip Existing Files:   {args.skip_existing_file}") # This now reflects the processed default
    # ... (print other relevant args as desired) ...

    try: args.dir.mkdir(parents=True, exist_ok=True)
    except OSError as e: console.print(f"[red]Fatal: Cannot create output dir '{args.dir}': {e}.[/red]"); return
    resolved_log_file_path = args.dir / 'spytorec_metadata.jsonl' # Worker uses this global via argument
    
    try:
        client_id, client_secret = get_spotify_credentials()
        sp = Spotify(auth_manager=SpotifyOAuth(
            client_id=client_id, client_secret=client_secret, redirect_uri=SPOTIPY_REDIRECT_URI,
            scope="user-read-playback-state user-read-currently-playing", open_browser=True, requests_timeout=15
        ))
        user = sp.current_user() 
        if not user or not user.get('display_name'):
            console.print("[red]Fatal: Spotify auth succeeded but no user details. Exiting.[/red]"); stop_worker_event.set(); return
        console.print(f"[green]Authenticated as [bold]{user['display_name']}[/bold]. Polling: {args.interval}s.[/green]")
    except Exception as e:
        console.print(f"[bold red]Spotify auth failed for recording: {e}[/bold red]"); stop_worker_event.set(); return

    persisted_recorded_ids = load_recorded_ids(resolved_log_file_path)
    session_attempted_ids = set() 
    console.print(f"Loaded [bold cyan]{len(persisted_recorded_ids)}[/bold cyan] track IDs from log: '{resolved_log_file_path.name}'.")
    console.print(Panel("[bold]Spotify Recorder Active[/bold]", subtitle="Monitoring Spotify... Press Ctrl+C to stop.", border_style="sky_blue1"))
    try:
        current_status_message = "[italic grey50]Initializing...[/italic grey50]"
        with console.status(current_status_message, spinner="line", speed=1.5) as status:
            while True:
                if stop_worker_event.is_set(): break 
                spotify_info = get_current_track(sp)
                if current_ffmpeg_process: 
                    active_rec_name = current_recording_info.get('metadata',{}).get('name','Unknown Track')
                    try:
                        start_dt_str = current_recording_info.get('start_iso', '')
                        start_dt = datetime.fromisoformat(start_dt_str) 
                        if start_dt.tzinfo is None: start_dt = start_dt.replace(tzinfo=timezone.utc)
                        elapsed_seconds = (datetime.now(timezone.utc) - start_dt).total_seconds()
                        total_seconds_api = current_recording_info.get('metadata', {}).get('duration_ms', 0) / 1000
                        current_status_message = f"Recording: [cyan]{active_rec_name}[/cyan] [{format_time(elapsed_seconds)} / {format_time(total_seconds_api)}]"
                    except Exception: current_status_message = f"Recording: [yellow]{active_rec_name} (Calculating time...)[/yellow]"
                    stop_reason = None
                    if current_ffmpeg_process.poll() is not None: stop_reason = "FFmpeg process ended"
                    elif not spotify_info or not spotify_info.get('is_playing'): stop_reason = "Playback stopped or track unavailable"
                    elif spotify_info and spotify_info.get('id') != current_recording_info.get('track_id'): stop_reason = "Track changed"
                    if stop_reason: submit_to_finalization_queue(stop_reason)
                elif spotify_info and spotify_info.get('is_playing') and spotify_info.get('id'): 
                    current_track_id = spotify_info['id']; current_track_name = spotify_info['name']
                    current_status_message = f"Detected: [cyan]{spotify_info['artist_str']} - {current_track_name}[/cyan]"
                    if current_track_id in persisted_recorded_ids or current_track_id in session_attempted_ids:
                        current_status_message = f"[grey50]Skipping '{current_track_name}' (already handled/logged).[/grey50]"
                    else:
                        # Determine potential full path for skip check based on organization flag
                        file_name_part_for_check = generate_safe_filename(spotify_info['artist_str'], spotify_info['name'], args.format)
                        if args.organize:
                            s_artist_folder = sanitize_for_filesystem(spotify_info.get('artists', [{}])[0].get('name', 'Unknown Artist'))
                            s_album_folder = sanitize_for_filesystem(spotify_info['album'])
                            path_for_skip_check = args.dir / s_artist_folder / s_album_folder / file_name_part_for_check
                        else:
                            path_for_skip_check = args.dir / file_name_part_for_check
                        
                        if args.skip_existing_file and path_for_skip_check.exists():
                            skip_msg = f"[yellow]File exists ('{path_for_skip_check.name}'), skipping '{current_track_name}'.[/yellow]"
                            if current_track_id not in session_attempted_ids: console.print(skip_msg); session_attempted_ids.add(current_track_id)
                            current_status_message = skip_msg
                        else:
                             if start_recording(spotify_info, args.format, args.dir, args.quality, args.device, 
                                                args.ffmpeg_path, args.recording_buffer, args.min_duration, args.organize):
                                 session_attempted_ids.add(current_track_id)
                             else: 
                                  current_status_message = f"[yellow]Start fail for '{current_track_name}'. Waiting...[/yellow]"
                                  if spotify_info['duration_ms'] / 1000 < args.min_duration:
                                       session_attempted_ids.add(current_track_id)
                else: current_status_message = "[italic grey50]Waiting for a track to play...[/italic grey50]"
                status.update(current_status_message)
                time.sleep(args.interval)
    except KeyboardInterrupt: console.print("\n[yellow][Record Mode] Keyboard Interrupt. Preparing to shut down record loop...[/yellow]")
    except Exception as e: console.print(f"\n[red][Record Mode] Critical error in recording loop: {e}\n{escape(traceback.format_exc())}[/red]")
    finally:
        if current_ffmpeg_process: submit_to_finalization_queue("Record loop ended or error")


def main():
    global current_ffmpeg_process, current_recording_info 
    display_intro_banner() # Display banner first
    display_disclaimer()
    
    config = configparser.ConfigParser(allow_no_value=True, inline_comment_prefixes=('#',';'))
    hardcoded_defaults = {
        'format': 'flac', 'dir': Path('Recordings'), 'quality': 7, 'interval': 0.5,
        'device': 'audio=CABLE Output (VB-Audio Virtual Cable)' if os.name == 'nt' else ('default' if sys.platform != 'darwin' else '0'),
        'ffmpeg_path': 'ffmpeg', 'skip_existing_file': False, 'min_duration': 25, 
        'recording_buffer': -0.2, 'organize': False
    }
    if CONFIG_FILE_PATH.exists():
        try: config.read(CONFIG_FILE_PATH, encoding='utf-8')
        except configparser.Error as e: console.print(f"[red]Error reading '{CONFIG_FILE_PATH.name}': {e}. Using script defaults.[/red]")

    parser = argparse.ArgumentParser(
        description="🎵 SpytoRec: Spotify Track Recorder & Utilities.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="Example: python spytorec.py record --format ogg --organize"
    )
    parser.add_argument('-v', '--version', action='version', version=f'SpytoRec v{SCRIPT_VERSION}') 
    subparsers = parser.add_subparsers(title="Available Commands", dest="command_name", metavar="COMMAND",
                                       help="Use COMMAND -h for command-specific help.")
    
    # --- Define defaults using config or hardcoded ---
    cfg_dir = get_typed_config(config, 'GeneralSettings', 'output_directory', Path, hardcoded_defaults['dir'])
    cfg_fmt = get_typed_config(config, 'GeneralSettings', 'default_format', str, hardcoded_defaults['format'])
    cfg_q = get_typed_config(config, 'GeneralSettings', 'default_quality_ogg', int, hardcoded_defaults['quality'])
    cfg_int = get_typed_config(config, 'GeneralSettings', 'polling_interval_seconds', float, hardcoded_defaults['interval'])
    cfg_dev = get_typed_config(config, 'GeneralSettings', 'audio_device', str, hardcoded_defaults['device'])
    cfg_ffp = get_typed_config(config, 'GeneralSettings', 'ffmpeg_path', str, hardcoded_defaults['ffmpeg_path'])
    cfg_skip = get_typed_config(config, 'GeneralSettings', 'skip_existing_file', bool, hardcoded_defaults['skip_existing_file'])
    cfg_min_d = get_typed_config(config, 'GeneralSettings', 'min_duration_seconds', int, hardcoded_defaults['min_duration'])
    cfg_rec_b = get_typed_config(config, 'GeneralSettings', 'recording_buffer_seconds', float, hardcoded_defaults['recording_buffer'])
    cfg_org = get_typed_config(config, 'GeneralSettings', 'organize_by_artist_album', bool, hardcoded_defaults['organize'])

    # --- Record Subcommand Parser ---
    parser_record = subparsers.add_parser("record", help="Record Spotify tracks (default action).", aliases=['rec'], formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser_record.add_argument('--format', choices=['ogg', 'flac'], default=cfg_fmt, help="Audio format.")
    parser_record.add_argument('--dir', type=Path, default=cfg_dir, help="Output directory.")
    parser_record.add_argument('--quality', type=int, default=cfg_q, help="OGG quality (0-10).")
    parser_record.add_argument('--interval', type=float, default=cfg_int, help="Polling interval (secs).")
    parser_record.add_argument('--device', type=str, default=cfg_dev, help="Audio input device for FFmpeg.")
    parser_record.add_argument('--ffmpeg-path', type=str, default=cfg_ffp, help="Path to FFmpeg.")
    parser_record.add_argument('--skip-existing-file', action=argparse.BooleanOptionalAction, default=cfg_skip, help="Skip if output filename already exists.")
    parser_record.add_argument('--min-duration', type=int, default=cfg_min_d, help="Min track duration to record (secs).")
    parser_record.add_argument('--recording-buffer', type=float, default=cfg_rec_b, help="Time buffer for FFmpeg -t (secs).")
    parser_record.add_argument('--organize', action=argparse.BooleanOptionalAction, default=cfg_org, help="Organize into Artist/Album folders.")
    parser_record.set_defaults(func=execute_record_command)

    # --- List Devices Subcommand Parser ---
    parser_list_devices = subparsers.add_parser("list-devices", help="List audio input devices.", aliases=['lsdev'], formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser_list_devices.add_argument('--ffmpeg-path', type=str, default=cfg_ffp, help="Path to FFmpeg.")
    parser_list_devices.set_defaults(func=execute_list_devices_command)

    # --- Test Auth Subcommand Parser ---
    parser_test_auth = subparsers.add_parser("test-auth", help="Test Spotify API authentication.", aliases=['auth'], formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser_test_auth.set_defaults(func=execute_test_auth_command)
    
    # --- Handle default command behavior ---
    args_list_to_parse = sys.argv[1:]
    if not args_list_to_parse or (args_list_to_parse[0].startswith('-') and not any(cmd in args_list_to_parse for cmd in subparsers.choices.keys())):
        console.print("[italic yellow]No command specified, defaulting to 'record' command.[/italic yellow]\n")
        args_list_to_parse.insert(0, 'record') 
    
    try:
        args = parser.parse_args(args_list_to_parse)
        if not hasattr(args, 'func'): # If parsing succeeded but no function was set (e.g. only script name given, no default logic triggered)
            if not args.command_name: # Truly no command
                 args = parser.parse_args(['record'] + args_list_to_parse) # Try again with record prepended
            else: # A command was given but no func set - should not happen with set_defaults
                 parser.print_help(); return
    except SystemExit as e: return

    # --- Finalization Worker Thread ---
    worker = None
    # Resolve paths based on the parsed args for the specific command (or defaults if 'record' was implicit)
    final_ffmpeg_path = args.ffmpeg_path if hasattr(args, 'ffmpeg_path') else cfg_ffp
    final_log_dir = args.dir if hasattr(args, 'dir') else cfg_dir
    resolved_log_file_path_for_worker = final_log_dir / 'spytorec_metadata.jsonl'

    if args.command_name == "record":
        try: resolved_log_file_path_for_worker.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e: console.print(f"[red]Error ensuring log dir '{resolved_log_file_path_for_worker.parent}': {e}.[/red]")
        worker = threading.Thread(target=finalization_worker_function, args=(final_ffmpeg_path, resolved_log_file_path_for_worker), daemon=True)
        worker.start()

    # --- Execute Command ---
    try:
        if hasattr(args, 'func'):
            args.func(args, config) # Pass full config if subcommand function needs other sections
        else: # Fallback if command resolution was incomplete
            parser.print_help()
            console.print("\n[red]Please specify a valid command.[/red]")
    except KeyboardInterrupt: 
        console.print("\n[bold red]Global Keyboard Interrupt. Shutting down...[/bold red]")
    except Exception as e: 
        console.print(f"\n[bold red]Critical unhandled error in main execution: {e}[/bold red]")
        console.print(f"[grey70]{escape(traceback.format_exc())}[/grey70]")
    finally:
        console.print("\n[yellow]Initiating shutdown sequence...[/yellow]")
        stop_worker_event.set() 

        if current_ffmpeg_process and current_ffmpeg_process.poll() is None: 
            console.print("[yellow]Main thread: Active recording at shutdown. Submitting for finalization...[/yellow]")
            submit_to_finalization_queue("Shutdown signal to main thread")
        
        if worker and worker.is_alive():
            q_sz = finalization_task_queue.qsize()
            if q_sz > 0: console.print(f"[grey50]Waiting for finalization queue ({q_sz} tasks)...[/grey50]")
            else: console.print("[grey50]Finalization queue empty or worker not for this command.[/grey50]")
            finalization_task_queue.join() 
            if q_sz > 0: console.print("[green]Finalization queue processed.[/green]")
            worker.join(timeout=30) 
            if worker.is_alive(): console.print("[red]Finalization worker did not stop cleanly.[/red]")
            else: console.print("[green]Finalization worker stopped.[/green]")
        elif args.command_name == "record" and not worker : # Should not happen if record command was selected
             console.print("[yellow]Worker thread was expected for 'record' but not started.[/yellow]")
        console.print("\nSpytoRec shut down gracefully.")

if __name__ == "__main__":
    main()