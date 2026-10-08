"""Per-machine config survives a git checkout; no model or network required."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.local_config import (  # noqa: E402
    BACKUP_DIR, KEEP_BACKUPS, ensure_local_configs, protect_local_configs,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t",
                    "-c", "user.email=t@t", *args], check=True,
                   capture_output=True)


class ProtectLocalConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.cfg = self.root / "llm-services.json"

    def test_deleted_config_is_restored_not_reseeded_from_the_sample(self):
        (self.root / "llm-services-sample.json").write_text('{"sample": true}')
        self.cfg.write_text('{"mine": true}')
        protect_local_configs(self.root)
        self.cfg.unlink()  # what checking out a branch that dropped it does
        ensure_local_configs(self.root)
        self.assertEqual(self.cfg.read_text(), '{"mine": true}')

    def test_first_install_is_seeded_from_the_sample(self):
        (self.root / "llm-services-sample.json").write_text('{"sample": true}')
        ensure_local_configs(self.root)
        self.assertEqual(self.cfg.read_text(), '{"sample": true}')

    def test_copy_git_checkout_put_back_does_not_displace_the_real_backup(self):
        _git(self.root, "init", "-q")
        self.cfg.write_text('{"old": "tracked in git"}')
        _git(self.root, "add", "-f", "llm-services.json")
        _git(self.root, "commit", "-q", "-m", "old")
        self.cfg.write_text('{"mine": true}')
        protect_local_configs(self.root)
        self.cfg.write_text('{"old": "tracked in git"}')  # checkout overwrote it
        protect_local_configs(self.root)
        self.cfg.unlink()                                  # checkout back removed it
        protect_local_configs(self.root)
        self.assertEqual(self.cfg.read_text(), '{"mine": true}')

    def test_keeps_only_the_newest_backups(self):
        for n in range(KEEP_BACKUPS + 4):
            self.cfg.write_text(f'{{"n": {n}}}')
            protect_local_configs(self.root)
        kept = list((self.root / BACKUP_DIR).glob("llm-services.json.*"))
        self.assertEqual(len(kept), KEEP_BACKUPS)
        self.cfg.unlink()
        protect_local_configs(self.root)
        self.assertEqual(self.cfg.read_text(), f'{{"n": {KEEP_BACKUPS + 3}}}')

    def test_server_and_voice_settings_are_protected_too(self):
        for name in ("server-config.json", "voice-presets.json"):
            (self.root / name).write_text('{"keep": "%s"}' % name)
        protect_local_configs(self.root)
        for name in ("server-config.json", "voice-presets.json"):
            (self.root / name).unlink()
        protect_local_configs(self.root)
        self.assertEqual((self.root / "server-config.json").read_text(),
                         '{"keep": "server-config.json"}')


if __name__ == "__main__":
    unittest.main()
