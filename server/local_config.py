"""User-editable config files: a gitignored per-machine copy, seeded from a
committed template.

llm-services.json, tts-services.json, mflux-engines.json and
soundtrack-services.json describe the
services and models on *this* machine, so each install edits its own copy
and a `git pull` never overwrites it. What is committed is
``<name>-sample.json``; each page load copies it into place if missing.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


CONFIG_NAMES = ("llm-services.json", "tts-services.json", "mflux-engines.json",
                "soundtrack-services.json")


# Everything per-machine and gitignored. Git leaves ignored files alone — except
# when a checkout lands on a commit that tracked one (before d1c1433 the
# service files were committed): that overwrites it, and the next checkout
# back deletes it. So each is backed up, and restored if it goes missing.
PROTECTED_NAMES = CONFIG_NAMES + ("server-config.json", "voice-presets.json")
BACKUP_DIR = ".local-config-backup"
KEEP_BACKUPS = 10


def sample_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}-sample{path.suffix}")


def _in_git_history(root: Path, data: bytes) -> bool:
    """Whether git already holds exactly these bytes as a blob. A config that
    a checkout just replaced with the old tracked copy is that, and must not
    displace the real backup; a user's own edits never are."""
    digest = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    try:
        return subprocess.run(
            ["git", "-C", str(root), "cat-file", "-e", digest],
            capture_output=True, timeout=5,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _versions(backup_dir: Path, name: str) -> list[Path]:
    """This file's backups, newest first (stamped with a nanosecond mtime)."""
    return sorted(backup_dir.glob(f"{name}.*"), reverse=True)


def protect_local_configs(root: Path) -> None:
    """Restore any per-machine config that has gone missing from its newest
    backup, then back up each one that has changed. Best effort: a read-only
    folder just means no protection, never a failed page load."""
    root = Path(root)
    backup_dir = root / BACKUP_DIR
    for name in PROTECTED_NAMES:
        path = root / name
        try:
            versions = _versions(backup_dir, name)
            if not path.exists():
                if versions:
                    shutil.copy2(versions[0], path)
                continue
            data = path.read_bytes()
            if (not data.strip() or
                    (versions and versions[0].read_bytes() == data) or
                    _in_git_history(root, data)):
                continue
            backup_dir.mkdir(exist_ok=True)
            target = backup_dir / f"{name}.{path.stat().st_mtime_ns:020d}"
            shutil.copy2(path, target)
            for old in _versions(backup_dir, name)[KEEP_BACKUPS:]:
                old.unlink()
        except OSError:
            continue


def ensure_local_configs(root: Path) -> None:
    """Copy each committed sample into place where this machine has no copy
    yet. Called on page load; a config without a sample is left to its
    loader, which writes built-in defaults. A missing config comes back from
    its backup first, so the sample only ever seeds a first install."""
    protect_local_configs(root)
    for name in CONFIG_NAMES:
        path = Path(root) / name
        sample = sample_path(path)
        if path.exists() or not sample.exists():
            continue
        try:
            shutil.copyfile(sample, path)
        except OSError:
            pass


def ensure_local_copy(root: Path, name: str, default_doc: dict[str, Any]) -> Path:
    """Path of this machine's *name*, created first if missing -- from the
    committed sample when there is one, else from *default_doc*."""
    path = Path(root) / name
    if path.exists():
        return path
    sample = sample_path(path)
    try:
        if sample.exists():
            shutil.copyfile(sample, path)
        else:
            path.write_text(json.dumps(default_doc, indent=2) + "\n")
    except OSError:
        pass
    return path
