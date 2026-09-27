"""Adding an image to one shot must not mark every other rendered shot CHANGED.

Adding a shot reference also files it in the project's style-reference
library, and the library used to be part of every shot's render
fingerprint — so one upload made the whole board look stale. The library
does not reach a render (only a shot's own references do), so it no longer
counts, and fingerprints recorded under the old formula are recognised.

Run:  python3 tests/fingerprint_library.py
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.store import (  # noqa: E402
    default_board, default_shot, render_fingerprint, stale_reason,
)


def ref(name, tag=""):
    return {"path": f"b/refs/{name}", "tag": tag}


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.board = default_board("B")
        self.board["styleRefs"] = [ref("logo.png"), ref("car.png")]
        a, c = default_shot(self.board["defaults"]), default_shot(self.board["defaults"])
        a.update(prompt="A garage.", outputs=["/media/b/shots/01/clip.mp4"])
        c.update(prompt="The car.", outputs=["/media/b/shots/02/clip.mp4"],
                 referenceImages=[ref("car.png", "DeLorean")])
        self.board["shots"] = [a, c]
        for s in self.board["shots"]:
            s["renderFingerprint"] = render_fingerprint(s, self.board)

    def test_growing_the_library_leaves_other_shots_current(self):
        self.board["styleRefs"].append(ref("car-side.png"))
        self.assertEqual([stale_reason(s, self.board) for s in self.board["shots"]], ["", ""])

    def test_a_shots_own_new_reference_still_marks_it_changed(self):
        shot = self.board["shots"][1]
        shot["referenceImages"] = [ref("car-side.png", "DeLorean")]   # same tag, new file
        self.board["styleRefs"].append(ref("car-side.png"))
        self.assertTrue(stale_reason(shot, self.board))
        self.assertEqual(stale_reason(self.board["shots"][0], self.board), "")

    def test_fingerprints_from_the_old_formula_are_recognised(self):
        # As recorded before the fix, when the library held two images...
        lib = copy.deepcopy(self.board["styleRefs"])
        for s in self.board["shots"]:
            s["renderFingerprint"] = render_fingerprint(s, self.board, _library_refs=lib)
        # ...then two more images were added, as on the Back to the Future board.
        self.board["styleRefs"] += [ref("side.png"), ref("rear.png")]
        self.assertEqual([stale_reason(s, self.board) for s in self.board["shots"]], ["", ""])
        # A real change is still a change under the old formula too.
        self.board["shots"][0]["prompt"] = "A different garage."
        self.assertTrue(stale_reason(self.board["shots"][0], self.board))


if __name__ == "__main__":
    unittest.main()
