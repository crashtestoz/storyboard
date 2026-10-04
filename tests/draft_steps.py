"""A draft only lowers the resolution; the steps stay the scene's own.

Drafts used to cap steps at 8 as well, so a scene set to 16 steps drafted at
8. Now a draft is the same render at 384px on the long edge. Fingerprints:
a scene at 8 steps or fewer drafts exactly as before and keeps its recorded
fingerprint; one set higher was drafted at fewer steps and shows as changed.

Run:  python3 tests/draft_steps.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.backends.base import ShotPaths  # noqa: E402
from server.backends.vpipe_backend import VpipeBackend  # noqa: E402
from server.store import Store, default_board, default_shot, render_fingerprint  # noqa: E402


def a_board(draft=True, steps=16) -> dict:
    board = default_board("Draft test")
    board["defaults"].update(resolution="832x480", draft=draft)
    shot = default_shot(board["defaults"])
    shot.update(id="s1", prompt="A man walks.", steps=steps, model="ref2va")
    board["shots"] = [shot]
    return board


class DraftSteps(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "models/local/MiniMax-H3-Ref2VA-8bit").mkdir(parents=True)
        self.backend = VpipeBackend(self.ws / "vpipe", self.ws)
        self.paths = ShotPaths(workspace=self.ws, abs_dir=self.ws / "shots/01",
                               rel_dir="shots/01", data_dir=self.ws)

    def geometry(self, board):
        job = self.backend.prepare(board["shots"][0], board, self.paths)
        stages = json.loads(Path(job.payload["spec_path"]).read_text())["stages"]
        gen = next(s for s in stages if s["type"] == "generate-video")
        sched = next((s for s in stages if s["type"] == "scheduler-select"), None)
        return gen["config"]["width"], gen["config"]["height"], (sched or gen)["config"].get("steps")

    def test_draft_lowers_only_the_resolution(self):
        self.assertEqual(self.geometry(a_board()), (384, 224, 16))

    def test_full_render_is_unchanged(self):
        self.assertEqual(self.geometry(a_board(draft=False)), (832, 480, 16))

    def test_low_step_scene_drafts_at_its_own_count(self):
        self.assertEqual(self.geometry(a_board(steps=4)), (384, 224, 4))

    def test_eight_step_drafts_keep_their_fingerprint(self):
        board = a_board(steps=8)
        self.assertEqual(render_fingerprint(board["shots"][0], board), self._legacy(board))

    def test_higher_step_drafts_show_as_changed(self):
        board = a_board(steps=16)
        self.assertNotEqual(render_fingerprint(board["shots"][0], board), self._legacy(board))

    @staticmethod
    def _legacy(board):
        """What the fingerprint was when every draft carried the 8-step profile."""
        import hashlib
        import server.store as store
        captured = {}
        real_dumps = store.json.dumps

        def spy(obj, **kw):
            if isinstance(obj, dict) and "draftProfile" in obj:
                obj = dict(obj, draftProfile="384-long-edge-8-step-with-audio")
                captured["blob"] = real_dumps(obj, **kw)
                return captured["blob"]
            return real_dumps(obj, **kw)

        store.json.dumps = spy
        try:
            store.render_fingerprint(board["shots"][0], board)
        finally:
            store.json.dumps = real_dumps
        return hashlib.sha256(captured["blob"].encode("utf-8")).hexdigest()[:16]

    def test_the_short_lived_toggle_is_dropped_on_load(self):
        board = a_board()
        board["defaults"]["draftFullSteps"] = True
        migrated = Store(workspace=self.ws, data_dir=self.ws).migrate(board)
        self.assertNotIn("draftFullSteps", migrated["defaults"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
