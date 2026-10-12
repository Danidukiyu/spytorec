"""Drive the CLI through track changes without opening hardware or Spotify."""

import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC
from rich.console import Console
from rich.panel import Panel

from spytorec import cli, state
from tests.test_recording import TRACK, config


class CliLifecycleTests(unittest.TestCase):
    def test_mp3_request_saves_two_flacs_and_uses_flac_for_display_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root)
            cfg['Recording']['output_format'] = 'mp3'
            cfg['Recording']['force_unity_gain'] = 'false'
            cfg['Debug']['watchdog_enabled'] = 'false'
            output = io.StringIO()
            tracks = [TRACK, dict(TRACK, id='two', name='Second')]
            source = mock.Mock()
            source.name = 'Test source'
            saved_first = threading.Event()
            playback = iter([
                {'is_playing': True, 'item': track, 'progress_ms': 1000}
                for track in tracks + [TRACK]
            ] + [None])
            polls = 0
            def poll():
                nonlocal polls
                polls += 1
                if polls > 4:
                    state.stop_event.set()
                    return None
                if polls == 3:
                    self.assertTrue(saved_first.wait(2))
                return next(playback)
            source.get_playback.side_effect = poll
            keyboard = mock.Mock()
            keyboard.kbhit.side_effect = [False, False, True]
            keyboard.getch.return_value = 'q'
            for name, value in [('current_state', state.STATE_INIT), ('last_error_msg', ''),
                                ('stop_event', threading.Event()),
                                ('is_recording', False), ('watchdog_file_ref', None),
                                ('current_track_id_ref', None)]:
                self.enterContext(mock.patch.object(state, name, value))
            self.enterContext(mock.patch.object(cli, 'console', Console(file=output, force_terminal=False)))
            self.enterContext(mock.patch.object(cli, 'load_config', return_value=cfg))
            self.enterContext(mock.patch.object(cli, 'setup_logging'))
            self.enterContext(mock.patch.object(cli, 'load_blocklist', return_value=[]))
            self.enterContext(mock.patch.object(cli, 'discover_hardware', return_value=('Fake', 'fake', 48000, 2)))
            self.enterContext(mock.patch.object(cli, 'get_source', return_value=source))
            self.enterContext(mock.patch.object(cli, 'KBHit', return_value=keyboard))
            self.enterContext(mock.patch.object(cli, 'send_webhook'))
            self.enterContext(mock.patch.object(cli, 'watchdog_worker',
                                               side_effect=lambda cfg: state.stop_event.wait()))
            self.enterContext(mock.patch.object(cli, 'Live'))
            real_finalize = cli.finalize
            def save(*args):
                result = real_finalize(*args)
                if args[2]['id'] == 'one':
                    saved_first.set()
                return result
            self.enterContext(mock.patch.object(cli, 'finalize', side_effect=save))
            self.enterContext(mock.patch.object(cli.signal, 'signal'))
            self.enterContext(mock.patch.object(cli.atexit, 'register'))
            self.enterContext(mock.patch.object(cli.time, 'sleep'))
            self.enterContext(mock.patch('sys.argv', ['spytorec']))
            dashboard = self.enterContext(mock.patch.object(cli, 'build_dashboard', return_value=Panel('Test')))
            pipeline = self.enterContext(mock.patch('spytorec.session.audio.start_audio_process'))
            self.enterContext(mock.patch('spytorec.session.audio.shutdown_audio_process'))
            self.enterContext(mock.patch.object(cli, 'check_audio_health'))
            self.enterContext(mock.patch.object(cli, 'read_events', return_value=[]))
            self.enterContext(mock.patch.object(cli, 'read_meters', return_value=None))
            self.enterContext(mock.patch('spytorec.session.audio.stop_recording'))
            self.enterContext(mock.patch('spytorec.session.audio.start_recording', side_effect=lambda path, *_: sf.write(
                path, np.random.default_rng(7).uniform(-0.5, 0.5, (24000, 2)), 48000, subtype='PCM_24')))
            with self.assertLogs(level='WARNING'):
                cli.main()
            pipeline.assert_called_once()
            saved = list(root.rglob('*.flac'))
            self.assertEqual(len(saved), 2)
            self.assertEqual({FLAC(path)['title'][0] for path in saved}, {'Song', 'Second'})
            self.assertFalse(list(root.rglob('*.mp3')))
            self.assertFalse(list(root.glob('.tmp_*')))
            self.assertTrue(all(call.args[12] == 'flac' for call in dashboard.call_args_list))
            self.assertIn('Saving this session as FLAC', output.getvalue())
            self.assertIn('2 recorded', output.getvalue())
            self.assertIn('Skipping Duplicate', output.getvalue())
            source.close.assert_called_once()
