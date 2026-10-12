"""Saving must retain recoverable audio when metadata or destination fails."""

import configparser
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

from spytorec.config import DEFAULT_CONFIG
from spytorec.recording import finalize


TRACK = {'id': 'one', 'name': 'Song', 'artists': [{'name': 'Artist'}],
         'album': {'name': 'Album', 'release_date': '2024', 'images': []},
         'track_number': 1}


def config(out_dir):
    cfg = configparser.ConfigParser()
    cfg.read_dict(DEFAULT_CONFIG)
    cfg['Recording']['output_directory'] = str(out_dir)
    return cfg


class FinalizeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / '.tmp_audio.flac'
        self.cfg = config(self.root)
        sf.write(self.path, np.random.default_rng(0).uniform(-0.5, 0.5, (24000, 2)),
                 48000, subtype='PCM_24')

    def save(self):
        return finalize(self.path, self.root, TRACK, '{title}', 'flac', self.cfg)

    def test_tagging_failure_preserves_original_audio_and_reports_path(self):
        before = self.path.read_bytes()
        with mock.patch('spytorec.recording.FLAC.save', side_effect=OSError('disk full')), \
             self.assertLogs(level='ERROR'):
            result = self.save()
        self.assertFalse(result['ok'])
        self.assertEqual(result['recovery_path'], str(self.path.resolve()))
        self.assertIn('disk full', result['error'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_move_failure_preserves_the_original_readable_audio(self):
        before = self.path.read_bytes()
        with mock.patch.object(Path, 'replace', side_effect=PermissionError('destination locked')), \
             self.assertLogs(level='ERROR'):
            result = self.save()
        self.assertFalse(result['ok'])
        self.assertEqual(result['recovery_path'], str(self.path.resolve()))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sf.info(self.path).frames, 24000)

    def test_partial_metadata_write_does_not_damage_the_original(self):
        before = self.path.read_bytes()
        def damage_copy(audio):
            Path(audio.filename).write_bytes(b'damaged')
            raise OSError('metadata write failed')
        with mock.patch('spytorec.recording.FLAC.save', autospec=True, side_effect=damage_copy), \
             self.assertLogs(level='ERROR'):
            result = self.save()
        self.assertFalse(result['ok'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.root.glob('.tmp_tag_*')))

    def test_small_recording_is_retained_for_inspection(self):
        sf.write(self.path, np.zeros((1024, 2)), 48000, subtype='PCM_24')
        with self.assertLogs(level='WARNING'):
            result = self.save()
        self.assertFalse(result['ok'])
        self.assertTrue(self.path.exists())
        self.assertEqual(result['recovery_path'], str(self.path.resolve()))

    def test_successful_save_is_tagged_and_moved(self):
        result = self.save()
        saved = self.root / 'Artist' / 'Album' / 'Song.flac'
        self.assertTrue(result['ok'])
        self.assertFalse(self.path.exists())
        self.assertEqual(FLAC(saved)['title'], ['Song'])
        self.assertEqual(sf.info(saved).frames, 24000)
