"""Names and paths a recording is saved under."""

import configparser
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import soundfile

from spytorec import recording
from spytorec.recording import fit_path, get_final_path, finalize
from spytorec.utils import clean_filename, unreserved


def track(artist='Artist', album='Album', title='Title'):
    return {'artists': [{'name': artist}], 'album': {'name': album, 'release_date': '2020'},
            'name': title, 'track_number': 4}


def config(organize_by='artist/album'):
    cfg = configparser.ConfigParser()
    cfg['Naming'] = {'organize_by': organize_by}
    cfg['Recording'] = {'overwrite_existing': 'false'}
    cfg['SafetyChecks'] = {'validate_file_integrity': 'false'}
    return cfg


class UnreservedTests(unittest.TestCase):

    def test_reserved_device_names_are_renamed(self):
        for name, cleaned in [('CON', 'CON_'), ('nul', 'nul_'), ('Com1.flac', 'Com1_.flac'),
                              ('LPT9', 'LPT9_'), ('Aux .mp3', 'Aux _.mp3'), ('Aux. Mix', 'Aux_. Mix')]:
            self.assertEqual(unreserved(name), cleaned)

    def test_names_that_only_start_like_a_device_are_kept(self):
        for name in ['Console', 'Nullify', 'COM10', 'Con Te Partirò', '02. CON - Aux']:
            self.assertEqual(unreserved(name), name)

    def test_only_whole_path_parts_are_renamed(self):
        _, path = get_final_path(Path('/Music'), track('CON', 'Nul', 'Aux'),
                                 '{artist} - {title}', 'flac', config())
        self.assertEqual(path, Path('/Music/CON_/Nul_/CON - Aux.flac'))


class CleanFilenameTests(unittest.TestCase):

    def test_control_characters_are_stripped(self):
        self.assertEqual(clean_filename('Ti\x00t\x01le\x1f\t\n'), 'Title')

    def test_a_cut_leaves_no_trailing_dot(self):
        self.assertEqual(clean_filename('A' * 199 + '. B'), 'A' * 199)


class FitPathTests(unittest.TestCase):

    base = 'C:\\Users\\someone\\Music\\Recordings'

    def length(self, dirs, name, suffix='.flac'):
        return len(self.base) + sum(len(d) + 1 for d in dirs) + 1 + len(name) + len(suffix)

    def test_a_short_path_is_kept(self):
        self.assertEqual(fit_path(self.base, ['Artist', 'Album'], '01. Title', '.flac'),
                         (['Artist', 'Album'], '01. Title'))

    def test_a_long_path_fits(self):
        dirs, name = fit_path(self.base, ['A' * 150, 'B' * 150], 'C' * 150, '.flac')

        self.assertEqual(self.length(dirs, name), 259)
        self.assertLessEqual(self.length(dirs, '') - 2 - len('.flac'), 247)
        self.assertTrue(dirs[0].startswith('A') and dirs[1].startswith('B') and name.startswith('C'))

    def test_an_album_folder_is_the_same_for_every_track(self):
        dirs = ['Ludwig van Beethoven', 'Symphony No. 9 in D Minor, Op. 125 "Choral" ' * 4]
        short = fit_path(self.base, dirs, '01. Short', '.flac')[0]
        long = fit_path(self.base, dirs, '04. ' + 'Presto - Allegro assai ' * 8, '.flac')[0]

        self.assertEqual(short, long)

    def test_a_cut_leaves_no_trailing_dot_or_space(self):
        dirs, name = fit_path(self.base, [], 'A' * 200 + '. ' * 30, '.flac')
        self.assertFalse(name.endswith(('.', ' ')))


class FinalPathTests(unittest.TestCase):

    def test_windows_paths_are_fitted(self):
        out_dir = Path('C:/Users/someone/Music/Recordings')
        with mock.patch.object(recording, '_ON_WINDOWS', True):
            _, path = get_final_path(out_dir, track('A' * 150, 'B' * 150, 'C' * 150),
                                     '{track_no}. {artist} - {title}', 'flac', config())

        self.assertLessEqual(len(str(out_dir)) + len(str(path.relative_to(out_dir))) + 1, 259)
        self.assertEqual(path.suffix, '.flac')

    def test_other_platforms_are_left_alone(self):
        out_dir = Path('/Users/someone/Music/Recordings')
        with mock.patch.object(recording, '_ON_WINDOWS', False):
            target_dir, _ = get_final_path(out_dir, track('A' * 150, 'B' * 150),
                                           '{title}', 'flac', config())

        self.assertEqual(target_dir, out_dir / ('A' * 150) / ('B' * 150))


class FinaliseClashTests(unittest.TestCase):

    def test_a_clashing_long_name_fits_in_the_same_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            info = track('A' * 100, 'B' * 100, 'C' * 300)
            temp_file = out_dir / '.tmp_1.flac'
            soundfile.write(temp_file, [0.0] * 4800, 48000)

            with mock.patch.object(recording, '_ON_WINDOWS', True):
                target_dir, first = get_final_path(out_dir, info, '{title}', 'flac', config())
                target_dir.mkdir(parents=True)
                first.write_bytes(b'earlier take')
                result = finalize(temp_file, out_dir, info, '{title}', 'flac', config())

            self.assertTrue(result['ok'])
            saved = [p for p in target_dir.iterdir() if p != first]
            self.assertEqual(len(saved), 1)
            self.assertLessEqual(len(str(saved[0])), 259)
            self.assertEqual(len(str(first)), 259)


class FinaliseFailureTests(unittest.TestCase):

    def test_a_recording_that_cannot_be_saved_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            temp_file = out_dir / '.tmp_1760000000000000000.flac'
            temp_file.write_bytes(b'audio')

            with mock.patch.object(recording, 'get_final_path', side_effect=OSError('invalid name')), \
                    self.assertLogs(level='WARNING'):
                result = finalize(temp_file, out_dir, track(), '{title}', 'flac', config())

            self.assertFalse(result['ok'])
            self.assertEqual([p.name for p in out_dir.iterdir()], ['Unsaved_1760000000000000000.flac'])


if __name__ == '__main__':
    unittest.main()
