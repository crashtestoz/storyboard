"""User-editable config files: a gitignored per-machine copy, seeded from a
committed template.

llm-services.json, tts-services.json, mflux-engines.json and
soundtrack-services.json describe the
services and models on *this* machine, so each install edits its own copy
and a `git pull` never overwrites it. What is committed is
``<name>-sample.json``; each page load copies it into place if missing.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


CONFIG_NAMES = ("llm-services.json", "tts-services.json", "mflux-engines.json",
                "soundtrack-services.json")


def sample_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}-sample{path.suffix}")


def ensure_local_configs(root: Path) -> None:
    """Copy each committed sample into place where this machine has no copy
    yet. Called on page load; a config without a sample is left to its
    loader, which writes built-in defaults."""
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
