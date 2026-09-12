import logging
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional
import multiprocessing as mp
import queue
import traceback

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

def _flac_writer_process(file_path: str, sr: int, ch: int, subtype: str, q: mp.Queue):
    """Child process that continuously reads from the IPC queue and encodes to FLAC natively."""
    try:
        with sf.SoundFile(
            file=file_path,
            mode='w',
            samplerate=sr,
            channels=ch,
            subtype=subtype,
            format='FLAC'
        ) as f:
            while True:
                chunk = q.get()
                if chunk is None:
                    break
                # Clip to [-1.0, 1.0] as a safety net for FLAC encoding.
                # Diagnostic confirmed raw WASAPI data is clean (no NaN/Inf, max ~0.93),
                # so this is purely a safeguard, not the fix for popping.
                f.write(np.clip(chunk, -1.0, 1.0))
    except Exception as e:
        print(f"NativeFlacWriter process error: {e}")
        traceback.print_exc()

class NativeFlacWriter(AudioWriter):
    """Native Python FLAC writer using soundfile in a dedicated child process."""
    
    def __init__(self, file_path: Path, sr: int, ch: int, bit_depth: str):
        subtype_map = {
            '16': 'PCM_16',
            '24': 'PCM_24',
            '32': 'PCM_24'
        }
        subtype = subtype_map.get(bit_depth, 'PCM_16')
        
        self.file_path = file_path
        # Increase queue size to 500 (approx 42 seconds of buffer) to prevent any dropped frames
        self._queue = mp.Queue(maxsize=500)
        self._proc = mp.Process(
            target=_flac_writer_process,
            args=(str(file_path), sr, ch, subtype, self._queue),
            daemon=True
        )
        self._proc.start()

    def write(self, chunk: np.ndarray) -> None:
        if self._proc and self._proc.is_alive():
            try:
                # Use blocking put to prevent frame drops. The child process should
                # always keep up with real-time encoding, so this rarely blocks.
                self._queue.put(chunk, timeout=1.0)
            except queue.Full:
                logging.warning("NativeFlacWriter IPC queue is full for 1s — dropping chunk!")

    def close(self) -> None:
        if self._proc:
            try:
                self._queue.put(None, timeout=2.0)
                self._proc.join(timeout=3.0)
            except Exception as e:
                logging.error(f"Error closing NativeFlacWriter: {e}")
            
            if self._proc.is_alive():
                logging.warning("NativeFlacWriter timed out, terminating.")
                self._proc.terminate()
                self._proc.join()
            self._proc = None

    def is_alive(self) -> bool:
        if self._proc:
            return self._proc.is_alive()
        return False


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
