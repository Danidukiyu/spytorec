"""Finalising recordings off the recording loop."""

import time
import threading
import unittest

from spytorec.recording import BackgroundFinalizer


def track(name):
    return {'name': name, 'artists': [{'name': 'Artist'}]}


class BackgroundFinalizerTests(unittest.TestCase):

    def setUp(self):
        self.finalizer = None

    def tearDown(self):
        if self.finalizer is not None:
            self.finalizer.close(timeout=5)

    def start(self, finalize_track):
        self.finalizer = BackgroundFinalizer(finalize_track)
        self.finalizer.start()
        return self.finalizer

    def collect(self, finalizer, expected, timeout=5):
        """Drains until `expected` results have arrived, or the timeout."""
        results = []
        deadline = time.monotonic() + timeout

        while len(results) < expected and time.monotonic() < deadline:
            results.extend(finalizer.drain())
            if len(results) < expected:
                time.sleep(0.01)

        return results

    def test_submitting_does_not_wait_for_the_finalise(self):
        release = threading.Event()
        finalizer = self.start(lambda t, f: release.wait(5) or {'ok': True, 'size_mb': 1})

        started = time.monotonic()
        finalizer.submit(track('Slow'), 'tmp')
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.5)
        release.set()

    def test_a_raising_finalise_does_not_take_the_worker_with_it(self):
        def finalize_track(t, f):
            if t['name'] == 'Explodes':
                raise RuntimeError('cover art fetch blew up')
            return {'ok': True, 'size_mb': 2}

        finalizer = self.start(finalize_track)

        finalizer.submit(track('Explodes'), 'tmp-1')
        finalizer.submit(track('Fine'), 'tmp-2')
        results = self.collect(finalizer, 2)

        self.assertEqual([t['name'] for t, _ in results], ['Explodes', 'Fine'])
        self.assertFalse(results[0][1]['ok'])
        self.assertTrue(results[1][1]['ok'])

    def test_close_waits_for_work_already_queued(self):
        done = []

        def finalize_track(t, f):
            time.sleep(0.05)
            done.append(t['name'])
            return {'ok': True, 'size_mb': 1}

        finalizer = self.start(finalize_track)

        for name in ('One', 'Two'):
            finalizer.submit(track(name), 'tmp')
        finalizer.close(timeout=5)

        self.assertEqual(done, ['One', 'Two'])
        self.assertEqual(len(list(finalizer.drain())), 2)


if __name__ == '__main__':
    unittest.main()
