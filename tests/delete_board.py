"""Deletion safeguards through the HTTP API, using temporary projects only."""
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.app import build_server
from server.store import Store


class DeleteBoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.slug, self.board = self.store.create("Delete test 🎬")
        self.orch = SimpleNamespace(busy=False, stills_busy=False)
        self.server = build_server("127.0.0.1", 0, SimpleNamespace(store=self.store, orch=self.orch))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, payload=None, method="DELETE"):
        req = Request(
            f"http://127.0.0.1:{self.server.server_port}/api/boards/{self.slug}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"}, method=method,
        )
        try:
            with urlopen(req) as response:
                return response.status
        except HTTPError as error:
            error.close()
            return error.code

    def test_confirmation_required(self):
        for payload in (None, {}, {"confirmName": "wrong"}, {"confirmName": ""}):
            self.assertEqual(self.request(payload), 400)
            self.assertTrue(self.store.board_path(self.slug).exists())

    def test_active_work_blocks_deletion(self):
        for field in ("busy", "stills_busy"):
            setattr(self.orch, field, True)
            self.assertEqual(self.request({"confirmName": self.board["name"]}), 409)
            self.assertTrue(self.store.board_path(self.slug).exists())
            setattr(self.orch, field, False)

    def test_delete_preserves_media_and_rejects_stale_save(self):
        media = self.store.project_dir(self.slug) / "clip.mp4"
        media.write_bytes(b"expensive render")
        other, _ = self.store.create("Other board")
        self.assertEqual(self.request({"confirmName": self.board["name"]}), 200)
        self.assertFalse(self.store.board_path(self.slug).exists())
        self.assertEqual(media.read_bytes(), b"expensive render")
        self.assertTrue(self.store.board_path(other).exists())
        self.assertEqual(self.request(self.board, method="PUT"), 400)
        self.assertFalse(self.store.board_path(self.slug).exists())
        self.assertEqual(self.request({"confirmName": self.board["name"]}), 404)


if __name__ == "__main__":
    unittest.main()
