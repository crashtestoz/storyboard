"""Generate image, in the Cast editor: a portrait made from the description.

The still engine renders the name and description on their own -- no scene
description, no other cast -- in the board's render style, and the image is
filed in the project's refs/ for the editor to set as the character's image.

The still engine is a stub that writes a file, so this needs no models.

Run:  python3 tests/character_image.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.backends.base import JobSpec, RunResult  # noqa: E402
from server.orchestrator import Orchestrator  # noqa: E402
from server.store import Store, default_board  # noqa: E402


class StubStillBackend:
    id = "stub"
    label = "Stub"

    def __init__(self):
        self.prepared: list[tuple[dict, dict]] = []

    def health(self):
        return True, ""

    def capability(self, model):
        return SimpleNamespace(available=True, unavailable_reason="")

    def prepare(self, shot, project, paths):
        self.prepared.append((shot, project))
        paths.abs_dir.mkdir(parents=True, exist_ok=True)
        return JobSpec(shot_id=shot["id"], expected_outputs=[paths.abs_dir / "still.jpeg"])

    def run(self, spec, on_event, cancelled):
        spec.expected_outputs[0].write_bytes(b"\xff\xd8 not really a jpeg")
        return RunResult(exit_code=0)


class CharacterImage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(workspace=self.tmp, data_dir=self.tmp)
        self.backend = StubStillBackend()
        self.orch = Orchestrator(self.backend, self.store, workspace=self.tmp, data_dir=self.tmp)
        self.orch._still_model = lambda board: "krea2-still"
        board = default_board("Cast test")
        board["sceneDescription"] = "A ruined chapel at night."
        board["renderStyle"] = "Moody cinematic live action."
        board["characters"] = [{"id": "c1", "name": "Alex", "description": "An old priest.",
                                "image": None, "voice": None, "voiceText": ""}]
        self.store.save("cast-test", board)

    def run_job(self, name, description, kind="character"):
        self.orch.create_character_image("cast-test", name, description, kind)
        self.orch._stills_thread.join(30)
        return self.orch.status()["stills"]

    def test_image_is_filed_in_refs_and_returned(self):
        st = self.run_job("Maya", "A young widow in a grey shawl.")
        self.assertEqual(st["error"], "")
        self.assertEqual(st["kind"], "reference")
        image = st["results"]["image"]
        self.assertEqual(image["path"], "cast-test/refs/maya-portrait.jpeg")
        self.assertEqual(image["url"], "/media/cast-test/refs/maya-portrait.jpeg")
        self.assertTrue((self.tmp / image["path"]).is_file())

    def test_a_second_one_does_not_overwrite_the_first(self):
        self.run_job("Maya", "A young widow.")
        st = self.run_job("Maya", "A young widow, older.")
        self.assertEqual(st["results"]["image"]["path"], "cast-test/refs/maya-portrait-2.jpeg")

    def test_engine_gets_only_the_character_in_the_render_style(self):
        self.run_job("Maya", "A young widow in a grey shawl.")
        shot, project = self.backend.prepared[0]
        self.assertIn("Maya: A young widow in a grey shawl.", shot["prompt"])
        self.assertIn("plain neutral studio background", shot["prompt"])
        self.assertEqual(shot["characterIds"], [])
        self.assertIsNone(shot["startRef"])
        self.assertEqual(project["sceneDescription"], "")
        self.assertEqual(project["renderStyle"], "Moody cinematic live action.")
        self.assertFalse(project["defaults"]["draft"])
        self.assertEqual(project["defaults"]["resolution"], "544x960")
        self.assertNotIn("shawl..", shot["prompt"])

    def test_board_is_not_changed(self):
        before = self.store.load("cast-test")
        self.run_job("Maya", "A young widow.")
        after = self.store.load("cast-test")
        self.assertEqual(after["characters"], before["characters"])
        self.assertEqual(after.get("styleRefs"), before.get("styleRefs"))

    def test_prop_is_the_object_alone(self):
        st = self.run_job("Headset", "A thin, flat band of matte black material.", "prop")
        self.assertEqual(st["results"]["image"]["path"], "cast-test/refs/headset-prop.jpeg")
        shot, project = self.backend.prepared[0]
        self.assertIn("Headset: A thin, flat band of matte black material.", shot["prompt"])
        self.assertIn("No people", shot["prompt"])
        self.assertIn("no face", shot["prompt"])
        self.assertNotIn("portrait", shot["prompt"].lower())
        self.assertEqual(shot["characterIds"], [])
        self.assertNotEqual(project["defaults"]["resolution"], "544x960")

    def test_location_has_nobody_in_it(self):
        st = self.run_job("Lab", "A cluttered basement workshop.", "location")
        self.assertEqual(st["results"]["image"]["path"], "cast-test/refs/lab-location.jpeg")
        shot, _ = self.backend.prepared[0]
        self.assertIn("No people", shot["prompt"])

    def test_unknown_kind_is_refused(self):
        with self.assertRaisesRegex(ValueError, "kind"):
            self.orch.create_character_image("cast-test", "X", "Y", "vehicle")

    def test_no_description_is_refused(self):
        with self.assertRaisesRegex(ValueError, "description"):
            self.orch.create_character_image("cast-test", "Maya", "  ")

    def test_unsaved_character_without_a_name_still_works(self):
        st = self.run_job("", "A tall lighthouse keeper.")
        self.assertEqual(st["error"], "")
        self.assertTrue(st["results"]["image"]["path"].startswith("cast-test/refs/a-tall-lighthouse-keeper"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
