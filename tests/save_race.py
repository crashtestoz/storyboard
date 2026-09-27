"""A page save must not undo a render the server has recorded.

The page autosaves its whole copy of the board. A tab that missed a render
finishing would save "running, no clip" over the finished result — losing the
clip and, with it, any CHANGED marker for later edits.

Run:  python3 tests/save_race.py
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import _keep_server_render_state  # noqa: E402
from server.store import (  # noqa: E402
    default_board, default_shot, render_fingerprint, stale_reason,
)


class SaveRaceTests(unittest.TestCase):
    def setUp(self):
        self.server = default_board("B")
        self.shot = default_shot(self.server["defaults"])
        self.shot.update(prompt="Ray gets in.", status="running", outputs=[])
        self.server["shots"] = [self.shot]
        # A tab opens the board while the render is still running...
        self.tab = copy.deepcopy(self.server)
        # ...and then the render finishes on the server.
        self.shot.update(status="done", outputs=["/media/b/shots/01/clip.mp4"],
                         thumb="/media/b/shots/01/frames/frame-0062.png",
                         renderedAs="final",
                         renderFingerprint=render_fingerprint(self.shot, self.server))

    def test_stale_tab_edit_keeps_the_render_and_shows_changed(self):
        # The stale tab edits the prompt and autosaves its whole copy.
        self.tab["shots"][0]["prompt"] = "Ray gets in and waves."
        _keep_server_render_state(self.tab, self.server)
        saved = self.tab["shots"][0]
        self.assertEqual(saved["status"], "done")
        self.assertEqual(saved["outputs"], ["/media/b/shots/01/clip.mp4"])
        self.assertEqual(saved["prompt"], "Ray gets in and waves.")      # the edit survives
        self.assertTrue(stale_reason(saved, self.tab))                   # and reads CHANGED

    def test_new_and_removed_shots_come_from_the_page(self):
        new = default_shot(self.tab["defaults"])
        self.tab["shots"] = [new]                 # old shot deleted, a new one added
        _keep_server_render_state(self.tab, self.server)
        self.assertEqual([s["id"] for s in self.tab["shots"]], [new["id"]])
        self.assertEqual(self.tab["shots"][0]["outputs"], [])

    def test_order_comes_from_the_page(self):
        other = default_shot(self.server["defaults"])
        self.server["shots"].append(other)
        self.tab["shots"] = [copy.deepcopy(other), copy.deepcopy(self.tab["shots"][0])]
        _keep_server_render_state(self.tab, self.server)
        self.assertEqual([s["id"] for s in self.tab["shots"]], [other["id"], self.shot["id"]])
        self.assertEqual(self.tab["shots"][1]["status"], "done")


if __name__ == "__main__":
    unittest.main()
