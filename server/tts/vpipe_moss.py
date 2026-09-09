"""MOSS-TTS through vpipe's own ``text-to-speech`` stage.

The natural default here: it runs on the runtime this project already drives,
needs no extra process or dependency, and — the part that matters for a
storyboard — it can **clone a voice from a reference clip**, which is exactly
what a character's recorded voice is for.

Two variants exist, picked by vpipe from the model directory's
``config.json``: the 8B delay-pattern model (24 kHz mono, voice cloning) and
the v1.5 local transformer (48 kHz stereo). The 8B one is used here because
cloning is the feature worth having.

The models are not part of vpipe's build; they are fetched into the workspace
like any other, so this engine reports itself unavailable until they are
present rather than failing mid-render.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import wave
from pathlib import Path

from .base import SpeechResult, TTSEngine, Voice

# vpipe's catalogue entries for the pieces this needs
LM_MODEL = "mlx-community/MOSS-TTS-8B-8bit"
CODEC_MODEL = "OpenMOSS-Team/MOSS-Audio-Tokenizer"


class VpipeMossTTS(TTSEngine):
    id = "vpipe-moss"
    label = "MOSS-TTS 8B via vpipe (local, voice cloning)"
    supports_cloning = True

    def __init__(self, binary: Path, workspace: Path):
        self.binary = Path(binary)
        self.workspace = Path(workspace)

    # ------------------------------------------------------------------ #

    def _present(self, rel: str) -> bool:
        root = self.workspace / "models" / rel
        if not root.exists():
            return False
        try:
            return not any(root.rglob("*.part"))
        except OSError:
            return False

    def health(self) -> tuple[bool, str]:
        if not self.binary.exists():
            return False, f"vpipe binary not found at {self.binary}"
        missing = [m for m in (LM_MODEL, CODEC_MODEL) if not self._present(m)]
        if missing:
            return False, (
                "MOSS-TTS models are not in this workspace yet: "
                + ", ".join(missing)
                + ". Fetch them with a model-fetch pipeline (roughly 9 GB) "
                "before using this engine."
            )
        return True, ""

    def voices(self) -> list[Voice]:
        # The 8B model has no named preset voices: its output voice comes from
        # the reference clip, or is the model's own default when none is given.
        return [
            Voice(id="default", label="MOSS default voice", kind="preset"),
            Voice(id="clone", label="Cloned from a character's reference clip",
                  kind="clone"),
        ]

    # ------------------------------------------------------------------ #

    def synth(
        self,
        text: str,
        out_path: Path,
        *,
        voice: str | None = None,
        reference: Path | None = None,
        **_: object,
    ) -> SpeechResult:
        ok, msg = self.health()
        if not ok:
            return SpeechResult(engine=self.id, error=msg)

        text = (text or "").strip()
        if not text:
            return SpeechResult(engine=self.id, error="nothing to say")

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rel_out = self._rel(out_path)

        stages: list[dict] = [
            {
                "id": "model-select",
                "type": "model-select",
                "iports": [],
                "config": {"hf_dir": LM_MODEL},
            },
            {
                "id": "text-prompt",
                "type": "text-prompt",
                "iports": [],
                "config": {"text": text},
            },
        ]

        tts_iports = [{"src": "text-prompt", "oport": 0}]

        # A reference clip goes in on the stage's second port as mono PCM, and
        # the stage splices its RVQ codes into the prompt so the synthesised
        # speech adopts that timbre.
        if reference and Path(reference).exists() and self.supports_cloning:
            stages += [
                {
                    "id": "load-voice",
                    "type": "load-video",
                    "iports": [],
                    "config": {"input_url": self._rel(Path(reference))},
                },
                {
                    "id": "voice-pcm",
                    "type": "audio-to-pcm",
                    "iports": [{"src": "load-voice", "oport": 1}],
                    "config": {"output_sample_rate": 24000, "channels": 1},
                },
            ]
            tts_iports.append({"src": "voice-pcm", "oport": 0})

        stages.append(
            {
                "id": "text-to-speech",
                "type": "text-to-speech",
                "iports": tts_iports,
                # hf_dir belongs on THIS stage, not only on model-select: the
                # stage declares it required and fails its own config check
                # without it, which makes it inert — and an inert stage means
                # the pipeline completes in milliseconds, writes nothing, and
                # exits 0. That looked exactly like a working engine that
                # produced no audio.
                "config": {"hf_dir": LM_MODEL, "codec_dir": CODEC_MODEL},
            }
        )
        stages.append(
            {
                "id": "save-audio",
                "type": "save-audio",
                "iports": [{"src": "text-to-speech", "oport": 0}],
                # save-audio wants output_path, not output_url (save-video is
                # the one that takes a URL). With the wrong key the stage
                # declared its config invalid and skipped itself, so MOSS
                # generated the speech and then quietly dropped it on the
                # floor — and vpipe still exited 0.
                "config": {"output_path": rel_out},
            }
        )

        spec_path = out_path.with_suffix(".tts.vpipeline")
        spec_path.write_text(
            json.dumps({"id": "tts", "stages": stages, "subpipelines": []}, indent=2)
        )

        started = time.time()
        proc = subprocess.run(
            [str(self.binary), "--launch", self._rel(spec_path)],
            cwd=str(self.workspace),
            capture_output=True,
            text=True,
            timeout=900,
        )
        log = [
            ln
            for ln in ((proc.stdout or "") + "\n" + (proc.stderr or "")).splitlines()
            if ln.strip()
        ]

        # Freshness, not mere existence. dialogue.wav is written to the same
        # path every time, so a file left by an earlier take — even one made by
        # a different engine — satisfies "it exists" and made a run that
        # produced nothing report the previous take's duration as its own.
        # vpipe exits 0 when a stage goes inert, so the file is the only
        # evidence there is, and it has to be evidence of *this* run.
        fresh = (
            out_path.exists()
            and out_path.stat().st_size >= 1024
            and out_path.stat().st_mtime >= started - 1
        )
        if not fresh:
            stale = out_path.exists() and out_path.stat().st_mtime < started - 1
            return SpeechResult(
                engine=self.id,
                voice=voice or "default",
                log=log[-40:],
                error=(
                    f"no audio was written by this run (exit {proc.returncode})"
                    + (" — the file present is from an earlier take" if stale else "")
                    + ". "
                    + (_first_problem(log) or "see the log for why")
                ),
            )

        return SpeechResult(
            path=out_path,
            seconds=_wav_seconds(out_path) or (time.time() - started),
            engine=self.id,
            voice="clone" if reference else (voice or "default"),
            log=log[-40:],
        )

    def _rel(self, p: Path) -> str:
        try:
            return str(Path(p).resolve().relative_to(self.workspace.resolve()))
        except ValueError:
            return str(p)


def _first_problem(log: list[str]) -> str:
    for line in log:
        if "[ERROR]" in line or "[WARN]" in line:
            return line.strip()
    return ""


def _wav_seconds(p: Path) -> float:
    try:
        with wave.open(str(p), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:  # noqa: BLE001
        return 0.0
