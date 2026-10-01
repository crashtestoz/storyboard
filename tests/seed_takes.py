"""Seed comparison: one shot rendered at several seeds, into takes beside it.

No model inference — a fake backend writes a clip and frames whose bytes say
which seed made them. Run from storyboard:  python3 tests/seed_takes.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server.backends.base import JobSpec, RunResult, Validation  # noqa: E402
from render_all import a_board, an_orchestrator  # noqa: E402


class SeedBackend:
    """Writes clip.mp4 and two frames into the folder it is pointed at."""

    id = "seedstub"
    label = "Seed stub"

    def __init__(self):
        self.prepared = []

    def health(self):
        return True, ""

    def prepare(self, shot, project, paths):
        self.prepared.append((shot["seed"], paths.abs_dir))
        return JobSpec(shot_id=shot["id"], payload={"nativeDialogueSpoken": True, "seed": shot["seed"]},
                       expected_outputs=[paths.abs_dir / "clip.mp4"],
                       frames_dir=paths.abs_frames)

    def run(self, spec, on_event, should_cancel):
        out = spec.expected_outputs[0]
        out.write_bytes(f"clip seed {spec.payload['seed']}".encode())
        spec.frames_dir.mkdir(parents=True, exist_ok=True)
        for i in range(2):
            (spec.frames_dir / f"frame-{i:04d}.png").write_bytes(f"seed {spec.payload['seed']} f{i}".encode())
        return RunResult(exit_code=0, started_at=0.0, ended_at=1.0)

    def validate(self, spec, result):
        return Validation(verdict="done")


class SeedTakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.orch = an_orchestrator(self.root)
        self.orch.backend = SeedBackend()
        self.board = a_board(2)
        self.board["shots"][0]["seed"] = 1
        self.slug = "test"
        self.orch.store.save(self.slug, self.board)

    def sweep(self, shot_id="s1", **kw):
        self.orch.start_seed_sweep(self.slug, shot_id, **kw)
        self.orch._thread.join(10)
        return self.orch.status()

    def test_sweep_renders_each_seed_beside_the_shot_not_over_it(self):
        status = self.sweep(count=3)
        self.assertEqual(status["seedSweep"]["seeds"], [0, 2, 3])      # skips the shot's own seed 1
        self.assertEqual([r["status"] for r in status["runs"].values()], ["done"] * 3)
        takes = self.orch.seed_takes(self.slug, "s1")
        self.assertEqual([t["seed"] for t in takes], [0, 2, 3])
        self.assertTrue(all(t["current"] and t["clipUrl"] for t in takes))
        shot = self.orch.store.load(self.slug)["shots"][0]
        self.assertEqual(shot["seed"], 1)
        self.assertEqual(shot["outputs"], self.board["shots"][0]["outputs"])
        self.assertFalse((self.root / "test/shots/01/clip.mp4").exists())

    def test_a_second_sweep_picks_new_seeds(self):
        self.sweep(count=2)
        self.assertEqual(self.sweep(count=2)["seedSweep"]["seeds"], [3, 4])

    def test_take_goes_out_of_date_when_the_prompt_changes(self):
        self.sweep(count=1)
        board = self.orch.store.load(self.slug)
        board["shots"][0]["prompt"] = "Something else entirely."
        self.orch.store.save(self.slug, board)
        self.assertFalse(self.orch.seed_takes(self.slug, "s1")[0]["current"])

    def test_adopting_a_take_makes_it_the_shots_clip_and_seed(self):
        self.sweep(count=2)
        board = self.orch.adopt_seed_take(self.slug, "s1", 2)
        shot = board["shots"][0]
        self.assertEqual(shot["seed"], 2)
        self.assertEqual(shot["status"], "done")
        self.assertEqual((self.root / "test/shots/01/clip.mp4").read_bytes(), b"clip seed 2")
        self.assertEqual((self.root / "test/shots/01/frames/frame-0001.png").read_bytes(), b"seed 2 f1")
        self.assertTrue(shot["outputs"][0].startswith("/media/test/shots/01/"))
        # recorded as a render of what the board now says, seed included
        from server.store import render_fingerprint
        self.assertEqual(shot["renderFingerprint"], render_fingerprint(shot, board))

    def test_chained_shot_renders_from_the_upstream_last_frame(self):
        frame = self.root / "test/shots/01/frames/frame-0000.png"
        frame.parent.mkdir(parents=True)
        frame.write_bytes(b"upstream")
        board = self.orch.store.load(self.slug)
        board["shots"][1]["startRef"] = {"kind": "chain", "from": "s1"}
        self.orch.store.save(self.slug, board)
        status = self.sweep("s2", count=1)
        self.assertEqual(list(status["runs"].values())[0]["status"], "done")
        self.assertEqual(self.orch.backend.prepared[-1][0], 1)
        shot = self.orch.store.load(self.slug)["shots"][1]
        self.assertEqual((shot["seed"], shot["status"]), (0, "done"))   # untouched

    def test_delete_takes(self):
        self.sweep(count=2)
        self.assertEqual(self.orch.delete_seed_takes(self.slug, "s1"), 2)
        self.assertEqual(self.orch.seed_takes(self.slug, "s1"), [])


if __name__ == "__main__":
    unittest.main()
