"""User-editable config files: a gitignored per-machine copy, seeded from a
committed template.

llm-services.json, tts-services.json and mflux-engines.json describe the
services and models on *this* machine, so each install edits its own copy
and a `git pull` never overwrites it. What is committed is
``<name>.example.json``; the first start copies it into place.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


def example_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.example{path.suffix}")


def ensure_local_copy(root: Path, name: str, default_doc: dict[str, Any]) -> Path:
    """Path of this machine's *name*, created first if missing -- from the
    committed example when there is one, else from *default_doc*."""
    path = Path(root) / name
    if path.exists():
        return path
    example = example_path(path)
    try:
        if example.exists():
            shutil.copyfile(example, path)
        else:
            path.write_text(json.dumps(default_doc, indent=2) + "\n")
    except OSError:
        pass
    return path
