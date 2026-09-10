"""FFmpeg process control, file finalization, tagging, and watchdog."""

import time
import queue
import logging
import threading
import subprocess
from pathlib import Path
from typing import Dict

from mutagen.flac import FLAC, Picture
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TYER, TRCK, APIC

from spytorec import state
from spytorec.utils import clean_filename, fetch_image_bytes, sniff_image_mime


def get_final_path(out_dir: Path, track_info: Dict, naming_format: str,
                   output_format: str, cfg) -> tuple:
    """Generates the target directory and final path for a track based on config rules.
    Returns (target_dir, final_path).
    """
    artist = clean_filename(track_info['artists'][0]['name'])
    album = clean_filename(track_info['album']['name'])
    title = clean_filename(track_info['name'])
    track_no = str(track_info.get('track_number', 0)).zfill(2)
    year = track_info['album'].get('release_date', '0000')[:4]

    tags = {
        'artist': artist, 'album': album, 'title': title,
        'track_no': track_no, 'year': year
    }

    organize_by = cfg['Naming'].get('organize_by', 'none').lower()
    if organize_by == 'artist/album':
        target_dir = out_dir / artist / album
    elif organize_by == 'artist':
        target_dir = out_dir / artist
    else:
        target_dir = out_dir

    try:
        final_name = naming_format.format(**tags)
    except KeyError as e:
        logging.warning(f"Missing tag in naming format: {e}, using fallback")
        final_name = f"{track_no}. {artist} - {title}"

    final_name = clean_filename(final_name)
    final_path = target_dir / f"{final_name}.{output_format}"

    return target_dir, final_path


def finalize(temp_file: Path, out_dir: Path, track_info: Dict,
             naming_format: str, output_format: str, cfg) -> Dict:
    """Tags and moves the recorded file with integrity checking.
    Returns a dict with 'ok': bool, 'size_mb': float, 'path': str on success.
    """
    if not temp_file or not temp_file.exists():
        return {'ok': False}

    try:
        # Verify file integrity
        if cfg['SafetyChecks'].getboolean('validate_file_integrity'):
            if temp_file.stat().st_size < 8192:
                logging.warning(f"File too small, possible corruption: {temp_file}")
                temp_file.unlink()
                return {'ok': False}

        # Wait a moment for file to be fully written
        time.sleep(1.5)

        target_dir, final_path = get_final_path(out_dir, track_info, naming_format, output_format, cfg)
        target_dir.mkdir(parents=True, exist_ok=True)

        # Handle existing files
        if final_path.exists():
            if not cfg['Recording'].getboolean('overwrite_existing'):
                timestamp = int(time.time())
                final_path = target_dir / f"{final_path.stem}_{timestamp}.{output_format}"
                logging.info(f"File exists, created: {final_path.name}")
            else:
                logging.info(f"Overwriting existing file: {final_path.name}")

        year = track_info['album'].get('release_date', '0000')[:4]
        track_no = str(track_info.get('track_number', 0)).zfill(2)

        if output_format == 'mp3':
            audio = MP3(temp_file, ID3=ID3)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.add(TIT2(encoding=3, text=track_info['name']))
            audio.tags.add(TPE1(encoding=3, text=track_info['artists'][0]['name']))
            audio.tags.add(TALB(encoding=3, text=track_info['album']['name']))
            audio.tags.add(TYER(encoding=3, text=year))
            audio.tags.add(TRCK(encoding=3, text=track_no))
        else:
            audio = FLAC(temp_file)
            audio['title'] = track_info['name']
            audio['artist'] = track_info['artists'][0]['name']
            audio['album'] = track_info['album']['name']
            audio['date'] = year
            audio['tracknumber'] = track_no

        # Add album art. fetch_image_bytes takes http(s):// and file:// urls
        # alike; MIME is sniffed from the bytes since a local file has no header.
        if not cfg['Recording'].getboolean('force_safe_mode'):
            try:
                images = track_info['album'].get('images', [])
                if images:
                    img_url = images[0]['url']
                    img_data = fetch_image_bytes(img_url, timeout=3, max_bytes=state.MAX_COVER_ART_BYTES)
                    if img_data:
                        mime = sniff_image_mime(img_data)
                        if mime:
                            if output_format == 'mp3':
                                audio.tags.add(
                                    APIC(encoding=3, mime=mime, type=3, desc='Cover', data=img_data)
                                )
                            else:
                                picture = Picture()
                                picture.data = img_data
                                picture.type = 3
                                picture.mime = mime
                                picture.desc = "Cover Art"
                                audio.add_picture(picture)
                            logging.debug("Added album art")
                        else:
                            logging.warning("Unrecognised album art format, skipping")
            except Exception as e:
                logging.debug(f"Could not add album art: {e}")

        audio.save()

        # Move to final location
        temp_file.replace(final_path)
        size_mb = round(final_path.stat().st_size / (1024 ** 2), 2)
        logging.info(f"Finalised: {final_path.name}")
        return {'ok': True, 'size_mb': size_mb, 'path': str(final_path.name)}

    except Exception as e:
        logging.exception(f"Finalise Error: {e}")
        try:
            if temp_file and temp_file.exists():
                temp_file.unlink()
        except Exception:
            pass
        return {'ok': False}


# safely_stop_ffmpeg removed, handled by writers.py


def watchdog_worker(cfg) -> None:
    """Monitors health of recording threads and FFmpeg."""
    last_sz = 0
    stalled_count = 0
    no_heartbeat_count = 0

    while not state.stop_event.is_set():
        try:
            time.sleep(2.0)

            if not cfg['Debug'].getboolean('watchdog_enabled'):
                continue

            current_state_val = state.get_state()

            # Check heartbeat
            if current_state_val == state.STATE_RECORDING:
                if time.time() - state.last_heartbeat > state.HEARTBEAT_TIMEOUT:
                    no_heartbeat_count += 1
                    if no_heartbeat_count >= 3:
                        logging.error("Audio stream heartbeat lost")
                        state.set_state(state.STATE_ERROR, "Audio stream lost")
                else:
                    no_heartbeat_count = 0

            # Check audio process is alive
            if current_state_val == state.STATE_RECORDING:
                from spytorec.audio_process import is_audio_alive
                if not is_audio_alive():
                    logging.error("Audio process terminated unexpectedly")
                    state.set_state(state.STATE_ERROR, "Audio process terminated unexpectedly")

            # Check file growth
            if current_state_val == state.STATE_RECORDING and state.watchdog_file_ref and state.watchdog_file_ref.exists():
                try:
                    sz = state.watchdog_file_ref.stat().st_size
                    if sz == last_sz and sz > 0:
                        stalled_count += 1
                        if stalled_count >= 3:
                            logging.error("Recording file stalled for 6+ seconds, triggering recovery")
                            state.set_state(state.STATE_ERROR, "Recording stalled")
                            stalled_count = 0
                    else:
                        stalled_count = 0
                        last_sz = sz
                except Exception as e:
                    logging.debug(f"File size check failed: {e}")

        except Exception as e:
            logging.error(f"Watchdog error: {e}")


class BackgroundFinalizer:
    """Finalises recordings on a worker thread; the loop collects the results."""

    def __init__(self, finalize_track):
        self._finalize_track = finalize_track
        self._pending = queue.Queue()
        self._results = queue.Queue()
        self._thread = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def submit(self, track: Dict, temp_file: Path) -> None:
        """Queues a finished recording. Returns immediately."""
        self._pending.put((track, temp_file))

    def drain(self):
        """Yields every (track, result) finished since the last call."""
        while True:
            try:
                yield self._results.get_nowait()
            except queue.Empty:
                return

    @property
    def busy(self) -> bool:
        return not self._pending.empty()

    def close(self, timeout: float = 60.0) -> None:
        """Stops the worker once it has finished what it was given."""
        if self._thread is None:
            return

        self._pending.put(None)
        self._thread.join(timeout=timeout)
        self._thread = None

    def _work(self) -> None:
        while True:
            item = self._pending.get()
            if item is None:
                return

            track, temp_file = item
            try:
                result = self._finalize_track(track, temp_file)
            except Exception as e:
                logging.exception(f"Finalise failed for {track.get('name')}: {e}")
                result = {'ok': False, 'size_mb': 0}

            self._results.put((track, result))
