"""A locked shot's render record comes from its clip, not the project."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.render_record import ensure_render_record  # noqa: E402


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class RenderRecord(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        clip = self.root / "b/shots/01/clip.mp4"
        clip.parent.mkdir(parents=True)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                        "color=c=black:s=320x192:d=0.2", str(clip)], check=True)
        self.timings = self.root / "render-timings.json"
        self.timings.write_text(json.dumps({"renders": [
            {"model": "h3c:ref2va", "width": 320, "height": 192, "frames": 22,
             "steps": 12, "seconds": 41.3, "at": 0}]}))

    def shot(self, **kw):
        return {"outputs": ["/media/b/shots/01/clip.mp4?t=1"], "frames": 22,
                "steps": 20, "runtimeSeconds": 41.3, **kw}

    def test_backfill_reads_clip_size_and_timing_steps(self):
        s = self.shot()
        self.assertTrue(ensure_render_record(s, self.root, self.timings))
        self.assertEqual(s["renderedWith"], {"engine": "h3c", "turbo": False, "steps": 12,
                                             "width": 320, "height": 192, "frames": 22})

    def test_complete_record_is_kept(self):
        rec = {"engine": "vpipe", "turbo": True, "steps": 8,
               "width": 832, "height": 480, "frames": 124}
        s = self.shot(renderedWith=dict(rec))
        self.assertFalse(ensure_render_record(s, self.root, self.timings))
        self.assertEqual(s["renderedWith"], rec)

    def test_no_clip_no_record(self):
        s = {"outputs": [], "frames": 22, "steps": 20}
        self.assertFalse(ensure_render_record(s, self.root, self.timings))
        self.assertNotIn("renderedWith", s)


if __name__ == "__main__":
    unittest.main()
