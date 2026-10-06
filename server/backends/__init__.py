"""Render backend registry.

A backend is selected once at startup — ``--backend``, else ``SBV_BACKEND``,
else ``"backend"`` in server-config.json, else vpipe — and everything above
this layer — orchestrator, HTTP API, front end — is written against
:class:`~server.backends.base.Backend` only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Backend
from .comfyui_backend import ComfyUIBackend
from .h3c_backend import H3cBackend
from .mflux_backend import MfluxStills, WithMfluxStills, load_engines as load_mflux_engines
from .vpipe_backend import VpipeBackend

__all__ = ["Backend", "VpipeBackend", "ComfyUIBackend", "H3cBackend",
           "build_backend", "BACKEND_IDS"]

BACKEND_IDS = ("vpipe", "comfyui", "h3c")


def build_backend(kind: str, *, vpipe_binary: Path, workspace: Path,
                  comfyui_url: str = "http://127.0.0.1:8188",
                  project_root: Path | None = None,
                  h3c_binary: Path | None = None,
                  h3c_model_dir: Path | None = None,
                  h3c_options: dict[str, Any] | None = None,
                  vpipe_h3_turbo: dict[str, Any] | None = None) -> Backend:
    if kind == "vpipe":
        inner: Backend = VpipeBackend(binary=vpipe_binary, workspace=workspace,
                                      h3_turbo=vpipe_h3_turbo)
    elif kind == "comfyui":
        inner = ComfyUIBackend(base_url=comfyui_url, output_dir=workspace)
    elif kind == "h3c":
        if h3c_binary is None or h3c_model_dir is None:
            raise ValueError("the h3c backend needs h3cBinary and h3cModelDir")
        inner = H3cBackend(binary=h3c_binary, model_dir=h3c_model_dir,
                           workspace=workspace, options=h3c_options)
    else:
        raise ValueError(f"unknown backend: {kind!r} (expected one of {BACKEND_IDS})")
    if project_root is None:
        return inner
    mflux = MfluxStills(load_mflux_engines(project_root),
                        settings_path=Path(project_root) / "server-config.json")
    return WithMfluxStills(inner, mflux)
