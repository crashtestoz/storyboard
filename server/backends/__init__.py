"""Render backend registry.

A backend is selected once at startup (``--backend``) and everything above
this layer — orchestrator, HTTP API, front end — is written against
:class:`~server.backends.base.Backend` only.
"""

from __future__ import annotations

from pathlib import Path

from .base import Backend
from .comfyui_backend import ComfyUIBackend
from .mflux_backend import MfluxStills, WithMfluxStills, load_engines as load_mflux_engines
from .vpipe_backend import VpipeBackend

__all__ = ["Backend", "VpipeBackend", "ComfyUIBackend", "build_backend", "BACKEND_IDS"]

BACKEND_IDS = ("vpipe", "comfyui")


def build_backend(kind: str, *, vpipe_binary: Path, workspace: Path,
                  comfyui_url: str = "http://127.0.0.1:8188",
                  project_root: Path | None = None) -> Backend:
    if kind == "vpipe":
        inner: Backend = VpipeBackend(binary=vpipe_binary, workspace=workspace)
    elif kind == "comfyui":
        inner = ComfyUIBackend(base_url=comfyui_url, output_dir=workspace)
    else:
        raise ValueError(f"unknown backend: {kind!r} (expected one of {BACKEND_IDS})")
    if project_root is None:
        return inner
    mflux = MfluxStills(load_mflux_engines(project_root),
                        settings_path=Path(project_root) / "server-config.json")
    return WithMfluxStills(inner, mflux)
