"""Stable Audio 3 through its pure-MLX runtime (``optimized/mlx`` upstream).

Run as one subprocess per generation rather than a resident server. A
storyboard needs one soundtrack now and then, not a stream of them, and the
CLI frees each model as soon as it is done (``--free-models``), so nothing
sits in unified memory beside the video models between runs. Loading costs a
few seconds, which is small next to a render.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from . import MODELS, SoundtrackEngine, SoundtrackResult

TIMEOUT = 1200


class Sa3MlxEngine(SoundtrackEngine):
    kind = "sa3-mlx"

    def __init__(self, sid: str, label: str, path: Path):
        self.id, self.label, self.path = sid, label, path

    @property
    def python(self) -> Path:
        return self.path / ".venv" / "bin" / "python"

    @property
    def script(self) -> Path:
        return self.path / "scripts" / "sa3_mlx.py"

    def health(self) -> tuple[bool, str]:
        if not str(self.path) or not self.path.is_dir():
            return False, f"Stable Audio 3 is not at {self.path} — run setup/install-stable-audio-3.sh."
        if not self.script.is_file():
            return False, f"{self.path} is not the optimized/mlx folder of a Stable Audio 3 checkout."
        if not self.python.is_file():
            return False, f"{self.path} has no .venv yet — run setup/install-stable-audio-3.sh."
        if not any(m["installed"] for m in self.models()):
            return False, "No music model weights are downloaded yet — run setup/install-stable-audio-3.sh."
        return True, "ready"

    def _weights(self, model: str) -> list[Path]:
        mlx = self.path / "models" / "mlx"
        codec = "same_s" if MODELS[model]["decoder"] == "same-s" else "same_l"
        return [mlx / f"dit_{model}_f16.npz", mlx / f"{codec}_decoder_f32.npz",
                mlx / "t5gemma_f16.npz"]

    def models(self) -> list[dict[str, Any]]:
        # exists() follows the symlinks into the HF cache, so a cache that was
        # deleted from under the links reads as not installed.
        return [{"id": mid, "label": spec["label"], "maxSeconds": spec["maxSeconds"],
                 "installed": all(p.exists() for p in self._weights(mid))}
                for mid, spec in MODELS.items()]

    def command(self, prompt: str, out: Path, *, seconds: float, model: str, seed: int,
                init_audio: Path | None = None, init_noise_level: float = 1.0) -> list[str]:
        argv = [str(self.python), str(self.script),
                "--prompt", prompt, "--dit", model, "--decoder", MODELS[model]["decoder"],
                "--seconds", f"{seconds:.2f}", "--seed", str(int(seed)),
                "--out", str(out)]
        if init_audio is not None:
            argv += ["--init-audio", str(init_audio),
                     "--init-noise-level", f"{init_noise_level:.3f}"]
        return argv

    def generate(self, prompt: str, out: Path, *, seconds: float, model: str,
                 seed: int, init_audio: Path | None = None,
                 init_noise_level: float = 1.0) -> SoundtrackResult:
        ok, msg = self.health()
        if not ok:
            return SoundtrackResult(error=msg)
        if model not in MODELS:
            return SoundtrackResult(error=f"unknown Stable Audio 3 model {model!r}")
        env = dict(os.environ)
        # The installer keeps the HF cache beside the checkout; point there so a
        # weight fetched on first use lands on the same drive as the rest.
        cache = self.path.parent.parent / "hf-cache"
        if cache.is_dir():
            env.setdefault("HF_HUB_CACHE", str(cache))
        out.parent.mkdir(parents=True, exist_ok=True)
        started = time.time()
        try:
            proc = subprocess.run(
                self.command(prompt, out, seconds=seconds, model=model, seed=seed,
                             init_audio=init_audio, init_noise_level=init_noise_level),
                cwd=self.path, env=env, capture_output=True, text=True,
                stdin=subprocess.DEVNULL, timeout=TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return SoundtrackResult(error=f"Stable Audio 3 took longer than {TIMEOUT // 60} minutes")
        tail = [line for line in ((proc.stderr or "") + (proc.stdout or "")).splitlines()
                if line.strip()][-8:]
        if proc.returncode != 0 or not out.is_file() or out.stat().st_size < 1024:
            return SoundtrackResult(error=f"Stable Audio 3 failed (exit {proc.returncode})", log=tail)
        return SoundtrackResult(
            ok=True, path=out, seconds=seconds,
            log=[f"[INFO] {model} generated {seconds:.1f}s of music in {time.time() - started:.1f}s"],
        )
