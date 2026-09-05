"""Tests for the shared image-fetching helpers (spytorec.utils).

These back both the http(s) CDN urls (Web API, macOS AppleScript) and the
file:// urls the Windows SMTC source produces for its thumbnail bytes.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spytorec.utils import fetch_image_bytes, sniff_image_mime


class SniffImageMimeTests(unittest.TestCase):
    def test_jpeg(self):
        self.assertEqual(sniff_image_mime(b"\xff\xd8\xff\xe0rest"), "image/jpeg")

    def test_png(self):
        self.assertEqual(sniff_image_mime(b"\x89PNG\r\n\x1a\nrest"), "image/png")

    def test_bmp(self):
        self.assertEqual(sniff_image_mime(b"BMrest"), "image/bmp")

    def test_gif(self):
        self.assertEqual(sniff_image_mime(b"GIF89arest"), "image/gif")

    def test_unrecognised_returns_none(self):
        self.assertIsNone(sniff_image_mime(b"not an image at all"))


class FetchImageBytesTests(unittest.TestCase):
    def test_empty_url_returns_none(self):
        self.assertIsNone(fetch_image_bytes(""))
        self.assertIsNone(fetch_image_bytes(None))

    def test_file_url_reads_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cover.jpg"
            p.write_bytes(b"\xff\xd8\xff" + b"x" * 100)
            self.assertEqual(fetch_image_bytes(p.as_uri()), p.read_bytes())

    def test_file_url_missing_file_returns_none(self):
        self.assertIsNone(fetch_image_bytes("file:///no/such/path/cover.jpg"))

    def test_file_url_oversize_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cover.jpg"
            p.write_bytes(b"x" * 100)
            self.assertIsNone(fetch_image_bytes(p.as_uri(), max_bytes=10))

    def test_http_url_success(self):
        resp = mock.Mock(status_code=200)
        resp.iter_content.return_value = [b"abc", b"def"]
        with mock.patch("requests.get", return_value=resp) as m:
            data = fetch_image_bytes("https://i.scdn.co/image/abc")
        self.assertEqual(data, b"abcdef")
        m.assert_called_once()

    def test_http_url_non_200_returns_none(self):
        resp = mock.Mock(status_code=404)
        with mock.patch("requests.get", return_value=resp):
            self.assertIsNone(fetch_image_bytes("https://i.scdn.co/image/missing"))

    def test_http_url_oversize_aborts(self):
        resp = mock.Mock(status_code=200)
        resp.iter_content.return_value = [b"x" * 10]
        with mock.patch("requests.get", return_value=resp):
            self.assertIsNone(fetch_image_bytes("https://i.scdn.co/image/big", max_bytes=5))

    def test_http_url_exception_returns_none(self):
        with mock.patch("requests.get", side_effect=OSError("network down")):
            self.assertIsNone(fetch_image_bytes("https://i.scdn.co/image/x"))


if __name__ == "__main__":
    unittest.main()
