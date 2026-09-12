import logging
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf


class AudioWriter(ABC):
    """Abstract base class for audio writers."""
    
    @abstractmethod
    def write(self, chunk: np.ndarray) -> None:
        """Write a chunk of audio data (shape: frames, channels)."""
        pass

    @abstractmethod
    def close(self) -> None:
        """Finalize and close the writer safely."""
        pass

    def is_alive(self) -> bool:
        """Check if the underlying writer is still alive/valid."""
        return True

class NativeFlacWriter(AudioWriter):
    """Native Python FLAC writer using soundfile (libsndfile)."""
    
    def __init__(self, file_path: Path, sr: int, ch: int, bit_depth: str):
        # Map generic bit depths to soundfile subtypes
        subtype_map = {
            '16': 'PCM_16',
            '24': 'PCM_24',
            '32': 'PCM_24'  # libsndfile FLAC encoder only supports up to 24-bit
        }
        subtype = subtype_map.get(bit_depth, 'PCM_16')
        
        self.file_path = file_path
        self._sf = sf.SoundFile(
            file=str(file_path),
            mode='w',
            samplerate=sr,
            channels=ch,
            subtype=subtype,
            format='FLAC'
        )

    def write(self, chunk: np.ndarray) -> None:
        if self._sf and not self._sf.closed:
            # Clip WASAPI floating point streams to strictly [-1.0, 1.0]
            # to prevent libsndfile from hard clipping or wrapping around, which causes static.
            self._sf.write(np.clip(chunk, -1.0, 1.0))

    def close(self) -> None:
        if self._sf and not self._sf.closed:
            self._sf.close()


class FFmpegMp3Writer(AudioWriter):
    """Fallback FFmpeg writer for MP3 encoding."""
    
    def __init__(self, file_path: Path, sr: int, ch: int, ffmpeg_path: str, log_file: Optional[Path] = None):
        cmd = [
            ffmpeg_path, '-y',
            '-f', 'f32le',
            '-ar', str(sr),
            '-ac', str(min(2, ch)),
            '-i', 'pipe:0',
            '-c:a', 'libmp3lame', 
            '-b:a', '320k',
            str(file_path)
        ]
        
        self.file_path = file_path
        
        stderr_dest = subprocess.DEVNULL
        self._log_file_obj = None
        if log_file:
            self._log_file_obj = open(log_file, "a")
            stderr_dest = self._log_file_obj

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=stderr_dest
        )

    def write(self, chunk: np.ndarray) -> None:
        if self._proc and self._proc.stdin:
            try:
                # Convert float32 numpy array to raw bytes for FFmpeg stdin
                self._proc.stdin.write(chunk.astype(np.float32).tobytes())
            except (BrokenPipeError, OSError) as e:
                logging.debug(f"FFmpeg MP3 Writer stdin closed: {e}")

    def close(self) -> None:
        if self._proc:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
                self._proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                logging.warning("FFmpeg MP3 Writer timed out, killing.")
                self._proc.kill()
                self._proc.wait()
            except Exception as e:
                logging.error(f"Error closing FFmpeg MP3 Writer: {e}")
            self._proc = None

        if self._log_file_obj:
            try:
                self._log_file_obj.close()
            except Exception:
                pass

    def is_alive(self) -> bool:
        if self._proc:
            return self._proc.poll() is None
        return False
