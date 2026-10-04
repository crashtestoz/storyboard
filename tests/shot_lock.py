"""A locked shot stays exactly as it is, whatever else on the board changes.

Locking is the user saying "this clip is final". So a project setting changed
afterwards must not mark it CHANGED or queue it in Render all; a page save, an
AD proposal or a stale tab must not edit, drop or unlock it; and no render may
land in the folder holding its clip.

No backend and no models: the guards are all pure or run on a temp store.

Run:  python3 tests/shot_lock.py
"""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import _keep_locked_shots  # noqa: E402
from server.orchestrator import Orchestrator  # noqa: E402
from server.store import (  # noqa: E402
    Store, default_board, default_shot, render_fingerprint, stale_reason,
)
from server.storyboard_chat import compact_board_context, validate_actions  # noqa: E402


class StubBackend:
    id = "stub"
    label = "Stub"

    def health(self):
        return True, ""


def a_board(n: int = 3, locked: tuple[int, ...] = (2,)) -> dict:
    """*n* rendered, current shots; the 1-based positions in *locked* locked."""
    board = default_board("Lock test")
    for i in range(n):
        shot = default_shot(board["defaults"])
        shot.update(id=f"s{i + 1}", title=f"Shot {i + 1}", prompt=f"Take {i + 1}.",
                    status="done", outputs=[f"/media/lock-test/shots/{i + 1:02d}/clip.mp4"],
                    locked=(i + 1) in locked)
        board["shots"].append(shot)
    for shot in board["shots"]:
        shot["renderFingerprint"] = render_fingerprint(shot, board)
    return board


class StalenessAndQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(workspace=self.tmp, data_dir=self.tmp)
        self.orch = Orchestrator(StubBackend(), self.store, workspace=self.tmp, data_dir=self.tmp)
        self.board = a_board()
        # A project-wide change every shot's fingerprint depends on.
        self.board["defaults"]["resolution"] = "1280x720"
        self.board["sceneDescription"] = "Now it is raining."

    def test_global_change_leaves_locked_shot_current(self):
        self.assertTrue(stale_reason(self.board["shots"][0], self.board))
        self.assertEqual(stale_reason(self.board["shots"][1], self.board), "")

    def test_render_all_skips_locked_shot(self):
        targets, _ = self.orch._pending(self.board)
        self.assertEqual([s["id"] for s in targets], ["s1", "s3"])

    def test_locked_draft_is_not_queued_either(self):
        self.board["shots"][1].update(status="failed")
        targets, _ = self.orch._pending(self.board)
        self.assertNotIn("s2", [s["id"] for s in targets])

    def test_chain_into_locked_shot_does_not_pull_it_in(self):
        self.board["shots"][1]["startRef"] = {"kind": "chain", "from": "s1"}
        targets, _ = self.orch._pending(self.board)
        self.assertNotIn("s2", [s["id"] for s in targets])

    def test_rendering_a_locked_shot_by_name_is_refused(self):
        self.store.save("lock-test", self.board)
        with self.assertRaisesRegex(RuntimeError, "locked"):
            self.orch._prime_batch("lock-test", ["s2"])
        saved = self.store.load("lock-test")["shots"][1]
        self.assertEqual(saved["status"], "done")
        self.assertTrue(saved["outputs"])

    def test_locked_shot_is_dropped_from_a_mixed_request(self):
        self.store.save("lock-test", self.board)
        self.orch._prime_batch("lock-test", ["s1", "s2"])
        self.assertEqual(self.orch._order, ["s1"])

    def test_no_render_into_a_locked_shot_folder(self):
        # s1 is locked with its clip in shots/01, and the board has been
        # reversed under it (hand edit, old tab): s3 now sits at position 1.
        board = a_board(locked=(1,))
        board["shots"].reverse()
        self.store.save("lock-test", board)
        with self.assertRaisesRegex(RuntimeError, "locked"):
            self.orch._prime_batch("lock-test", ["s3"])
        self.orch._prime_batch("lock-test", ["s3", "s2"])
        self.assertEqual(self.orch._order, ["s2"])
        self.assertEqual(self.store.load("lock-test")["shots"][0]["status"], "done")


class PageSaves(unittest.TestCase):
    def setUp(self):
        self.current = a_board()
        self.page = copy.deepcopy(self.current)

    def test_edit_to_locked_shot_is_undone(self):
        self.page["shots"][1]["prompt"] = "Rewritten."
        self.page["shots"][0]["prompt"] = "Also rewritten."
        _keep_locked_shots(self.page, self.current)
        self.assertEqual(self.page["shots"][1]["prompt"], "Take 2.")
        self.assertEqual(self.page["shots"][0]["prompt"], "Also rewritten.")

    def test_a_save_cannot_unlock_or_lock(self):
        self.page["shots"][1]["locked"] = False
        self.page["shots"][0]["locked"] = True
        _keep_locked_shots(self.page, self.current)
        self.assertTrue(self.page["shots"][1]["locked"])
        self.assertFalse(self.page["shots"][0]["locked"])

    def test_dropped_locked_shot_comes_back_in_place(self):
        del self.page["shots"][1]
        _keep_locked_shots(self.page, self.current)
        self.assertEqual([s["id"] for s in self.page["shots"]], ["s1", "s2", "s3"])

    def test_moving_a_locked_shot_is_refused(self):
        del self.page["shots"][0]
        with self.assertRaisesRegex(ValueError, "locked"):
            _keep_locked_shots(self.page, self.current)

    def test_shots_after_it_can_still_change(self):
        del self.page["shots"][2]
        self.page["shots"].append({**copy.deepcopy(self.current["shots"][0]), "id": "s9"})
        _keep_locked_shots(self.page, self.current)
        self.assertEqual([s["id"] for s in self.page["shots"]], ["s1", "s2", "s9"])


class StoryboardAD(unittest.TestCase):
    def setUp(self):
        self.board = a_board()

    def test_edits_and_dubs_of_locked_shot_are_dropped(self):
        notes: list[str] = []
        clean = validate_actions([
            {"tool": "update_shot", "shotId": "s2", "fields": {"prompt": "x"}},
            {"tool": "dub_shot", "shotId": "s2"},
            {"tool": "update_shot", "shotId": "s1", "fields": {"prompt": "y"}},
        ], self.board, notes=notes)
        self.assertEqual([(a["tool"], a.get("shotId")) for a in clean], [("update_shot", "s1")])
        self.assertTrue(notes)

    def test_render_of_only_locked_shots_is_not_a_whole_board_render(self):
        clean = validate_actions([{"tool": "start_render", "shotIds": ["s2"]}], self.board, notes=[])
        self.assertEqual(clean, [])

    def test_replace_text_skips_locked_shot(self):
        clean = validate_actions([{"tool": "replace_text", "find": "Take", "replace": "Shot",
                                   "scope": ["shots"]}], self.board)
        self.assertEqual(sorted(a["shotId"] for a in clean), ["s1", "s3"])

    def test_context_says_which_shots_are_locked(self):
        shots = compact_board_context(self.board)["shots"]
        self.assertEqual([s.get("locked", False) for s in shots], [False, True, False])


if __name__ == "__main__":
    unittest.main(verbosity=2)
