"""Render backend registry.

A backend is selected once at startup (``--backend``) and everything above
this layer — orchestrator, HTTP API, front end — is written against
:class:`~server.backends.base.Backend` only.
"""

from __future__ import annotations

from pathlib import Path

from .base import Backend
from .comfyui_backend import ComfyUIBackend
from .vpipe_backend import VpipeBackend

__all__ = ["Backend", "VpipeBackend", "ComfyUIBackend", "build_backend", "BACKEND_IDS"]

BACKEND_IDS = ("vpipe", "comfyui")


def build_backend(kind: str, *, vpipe_binary: Path, workspace: Path,
                  comfyui_url: str = "http://127.0.0.1:8188") -> Backend:
    if kind == "vpipe":
        return VpipeBackend(binary=vpipe_binary, workspace=workspace)
    if kind == "comfyui":
        return ComfyUIBackend(base_url=comfyui_url, output_dir=workspace)
    raise ValueError(f"unknown backend: {kind!r} (expected one of {BACKEND_IDS})")
