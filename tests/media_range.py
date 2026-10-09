"""Media files are served with HTTP Range support (video needs it to play/seek).

Run from storyboard:  python3 tests/media_range.py
"""
import http.client
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.app import Handler  # noqa: E402

DATA = bytes(range(256)) * 400          # 102,400 bytes: spans several 64 KiB reads


class Serve(Handler):
    target = None

    def do_GET(self):  # noqa: N802 — just the file sender under test
        self._send_file(self.target, cache=True)

    def log_message(self, *a):
        pass


class RangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = Path(cls.tmp.name) / "clip.mp4"
        path.write_bytes(DATA)
        handler = type("H", (Serve,), {"target": path})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def get(self, rng=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request("GET", "/", headers={"Range": rng} if rng else {})
        res = conn.getresponse()
        body = res.read()
        conn.close()
        return res, body

    def test_whole_file_advertises_ranges(self):
        res, body = self.get()
        self.assertEqual((res.status, body), (200, DATA))
        self.assertEqual(res.getheader("Accept-Ranges"), "bytes")

    def test_open_ended_range_from_zero_is_a_206_of_everything(self):
        res, body = self.get("bytes=0-")          # what <video> sends first
        self.assertEqual((res.status, body), (206, DATA))
        self.assertEqual(res.getheader("Content-Range"), f"bytes 0-{len(DATA) - 1}/{len(DATA)}")

    def test_middle_range_crosses_read_chunks(self):
        res, body = self.get("bytes=65000-66000")
        self.assertEqual((res.status, body), (206, DATA[65000:66001]))
        self.assertEqual(res.getheader("Content-Length"), "1001")

    def test_suffix_range_is_the_tail(self):
        res, body = self.get("bytes=-100")       # Safari reads the tail for the moov box
        self.assertEqual((res.status, body), (206, DATA[-100:]))

    def test_end_past_the_file_is_clamped(self):
        res, body = self.get(f"bytes=100000-{len(DATA) + 999}")
        self.assertEqual((res.status, body), (206, DATA[100000:]))

    def test_start_past_the_file_is_416(self):
        res, _ = self.get(f"bytes={len(DATA)}-")
        self.assertEqual(res.status, 416)
        self.assertEqual(res.getheader("Content-Range"), f"bytes */{len(DATA)}")

    def test_multi_range_and_junk_fall_back_to_the_whole_file(self):
        for rng in ("bytes=0-10,20-30", "items=0-5", "bytes=abc"):
            res, body = self.get(rng)
            self.assertEqual((res.status, body), (200, DATA), rng)


if __name__ == "__main__":
    unittest.main()
