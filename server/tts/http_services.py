"""Speech engines that call a service someone else already runs.

Why link rather than vendor: the voice models in this network are already
running as services with an owner. Qwen3-TTS is a FastAPI process alongside
MCC; MCC's plain endpoint drives a sherpa-onnx binary configured by that app's
environment. Copying either into this project would mean a second set of
weights, a second lifecycle to maintain, and two processes competing for the
same box — for no gain, since a storyboard needs a few seconds of speech, not
a dedicated instance.

So a service is described by a name and a URL in ``tts-services.json``, and
this project keeps its "nothing to install" property.

Two engines, because the two endpoints are genuinely different:

*   :class:`Qwen3CloneTTS` — the real Qwen3-TTS voice-clone server. Needs a
    reference clip **and a transcript of what that clip says**: its
    ``generate_voice_clone()`` conditions on both, so a clip alone is not
    enough. It can produce the transcript itself via ``/transcribe``.
*   :class:`MccSherpaTTS` — MCC's ``/api/tts``, a sherpa-onnx VITS voice. Text
    in, wav out, no cloning and no voice choice.

Both contracts were read from MCC's source rather than guessed.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import time
import urllib.request
import wave
from pathlib import Path

from .base import SpeechResult, TTSEngine, Voice

SYNTH_TIMEOUT = 600      # cloning a voice is CPU-bound and slow
TRANSCRIBE_TIMEOUT = 120
PROBE_TIMEOUT = 4


def _clip(text: str, limit: int = 90) -> str:
    """A quotable one-liner for a log: collapsed, shortened, quoted."""
    t = " ".join((text or "").split())
    return f'"{t[:limit]}…"' if len(t) > limit else f'"{t}"'


def _qwen_style_instruction(style: str) -> str:
    style = (style or "").strip()
    if not style:
        return ""
    if "[STYLE / VOICE DIRECTION]" in style or "[TEXT TO SPEAK]" in style:
        style = re.sub(r"(?is)\[TEXT TO SPEAK\].*$", "", style).strip()
        style = re.sub(r"(?is)^\[STYLE / VOICE DIRECTION\]\s*", "", style).strip()
        style = re.sub(r"(?is)\[END\]\s*$", "", style).strip()
    return style


def _post(url: str, payload: dict, timeout: int) -> tuple[bytes, str, str]:
    """(body, content-type, error)."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), (r.headers.get("Content-Type") or "").lower(), ""
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = (json.loads(exc.read() or b"{}") or {}).get("error", "")
        except Exception:  # noqa: BLE001
            pass
        return b"", "", detail or f"{url} returned {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return b"", "", f"cannot reach {url}: {exc}"


def _reachable(url: str) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT) as r:
            r.read(1)
        return True, ""
    except urllib.error.HTTPError:
        # answered, just not with a 200 on the root — the host is up
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


class Qwen3CloneTTS(TTSEngine):
    """Qwen3-TTS voice cloning, as MCC's /api/tts/clone calls it."""

    supports_cloning = True
    # the same service exposes /transcribe (faster-whisper)
    supports_transcription = True

    def __init__(self, service_id: str, label: str, base_url: str):
        self.id = service_id
        self.label = label
        self.base_url = (base_url or "").rstrip("/")

    def health(self) -> tuple[bool, str]:
        """Ask the server's own /health, which reports whether both models are
        resident — it loads Qwen3-TTS and faster-whisper at startup, and until
        they are in memory a synth request would simply block."""
        if not self.base_url:
            return False, "no URL configured for this service"
        try:
            with urllib.request.urlopen(self.base_url + "/health",
                                        timeout=PROBE_TIMEOUT) as r:
                doc = json.loads(r.read() or b"{}")
        except Exception as exc:  # noqa: BLE001
            return False, (
                f"cannot reach {self.base_url} ({exc}). The Qwen3-TTS server binds "
                "127.0.0.1, so it only answers on the machine it runs on. To use it "
                "from here, on that host (OptiPlex) start it with "
                "--host 0.0.0.0 (uvicorn's default is 127.0.0.1), open port 8790, "
                "then set this service's url to http://<that-host>:8790 in "
                "tts-services.json. Alternatively tunnel it "
                "(ssh -L 8790:127.0.0.1:8790 <host>) and keep the URL as localhost."
            )
        if not doc.get("loaded"):
            return False, f"{self.base_url} is up but the speech model is still loading"
        if not doc.get("whisperLoaded", True):
            return False, (
                f"{self.base_url} is up but the transcription model is still "
                "loading — auto-fill of the reference transcript will not work yet"
            )
        return True, ""

    def voices(self) -> list[Voice]:
        # The voice IS the reference clip; there are no presets to list.
        return [Voice(id="clone", label="Cloned from the character's reference clip",
                      kind="clone")]

    def transcribe(self, reference: Path) -> tuple[str, str]:
        """(transcript, error) for a reference clip, via the service."""
        body, ctype, err = _post(
            self.base_url + "/transcribe",
            {"reference_audio_b64": base64.b64encode(reference.read_bytes()).decode(),
             "reference_ext": reference.suffix.lstrip(".").lower() or "wav"},
            TRANSCRIBE_TIMEOUT,
        )
        if err:
            return "", err
        try:
            doc = json.loads(body or b"{}")
        except ValueError:
            return "", "transcription reply was not JSON"
        return str(doc.get("text") or doc.get("transcript") or "").strip(), ""

    def synth(
        self,
        text: str,
        out_path: Path,
        *,
        voice: str | None = None,
        reference: Path | None = None,
        reference_text: str | None = None,
        language: str = "en",
        style: str = "",
        speed: float = 1.0,
    ) -> SpeechResult:
        ok, msg = self.health()
        if not ok:
            return SpeechResult(engine=self.id, error=msg)
        if not (text or "").strip():
            return SpeechResult(engine=self.id, error="nothing to say")
        if not reference or not Path(reference).exists():
            return SpeechResult(
                engine=self.id,
                error="this engine clones a voice, so the character needs a "
                      "reference voice clip",
            )

        reference = Path(reference)
        started = time.time()
        # Narrated because this ends up in the Backend output pane: a clone
        # that comes back wrong is nearly always the reference or its
        # transcript, and neither is visible from the waveform.
        log: list[str] = [
            f"[INFO] {self.label}: POST {self.base_url}/synthesize",
            f"[INFO] reference: {reference.name} "
            f"({reference.stat().st_size} bytes, "
            f"{reference.suffix.lstrip('.').lower() or 'wav'})",
        ]

        ref_text = (reference_text or "").strip()
        if not ref_text:
            # The server requires a transcript; ask it to make one rather than
            # failing on a requirement it can satisfy itself.
            log.append("[INFO] no transcript given — asking the service to make one")
            ref_text, terr = self.transcribe(reference)
            if not ref_text:
                log.append(f"[ERROR] auto-transcribe failed: {terr or 'no text'}")
                return SpeechResult(
                    engine=self.id,
                    log=log,
                    error=(
                        "the reference clip needs a transcript of what it says "
                        + (f"(auto-transcribe failed: {terr})" if terr else "")
                    ),
                )
            log.append(f"[INFO] auto-transcribed: {_clip(ref_text)}")
        else:
            log.append(f"[INFO] reference text: {_clip(ref_text)}")

        log.append(f"[INFO] speaking {len(text.strip())} chars: {_clip(text)}")
        style = (style or "").strip()
        log.append(f"[INFO] language={language} speed={speed}")
        if style:
            log.append(f"[INFO] style: {_clip(style)}")
            log.append("[INFO] style sent through Qwen instruct field")
        request_style = _qwen_style_instruction(style)

        body, ctype, err = _post(
            self.base_url + "/synthesize",
            {
                "reference_audio_b64": base64.b64encode(reference.read_bytes()).decode(),
                "reference_ext": reference.suffix.lstrip(".").lower() or "wav",
                "reference_text": ref_text,
                "text": text.strip(),
                "language": language,
                "style": request_style,
                "speed": speed,
            },
            SYNTH_TIMEOUT,
        )
        if err:
            log.append(f"[ERROR] {err}")
            return SpeechResult(engine=self.id, log=log, error=err)
        audio = _audio_from(body, ctype)
        if not audio:
            log.append(f"[ERROR] response was {ctype or 'untyped'}, "
                       f"{len(body)} bytes, and held no audio")
            return SpeechResult(engine=self.id, log=log,
                                error="the server returned no audio")

        elapsed = time.time() - started
        log.append(
            f"[INFO] response: {ctype or 'untyped'}, {len(audio)} bytes "
            f"in {elapsed:.1f}s"
        )

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)
        seconds = _wav_seconds(out_path)
        log.append(
            f"[INFO] wrote {out_path.name} — {seconds:.2f}s of speech "
            f"({elapsed / seconds:.1f}x realtime)" if seconds
            else f"[INFO] wrote {out_path.name}"
        )
        return SpeechResult(path=out_path, seconds=seconds,
                            engine=self.id, voice="clone", log=log)


class MccSherpaTTS(TTSEngine):
    """MCC's /api/tts — a sherpa-onnx VITS voice. Text in, wav out."""

    supports_cloning = False

    def __init__(self, service_id: str, label: str, base_url: str):
        self.id = service_id
        self.label = label
        self.base_url = (base_url or "").rstrip("/")
        self._cached: tuple[float, bool, str] | None = None

    def health(self) -> tuple[bool, str]:
        """Actually try to synthesise, briefly cached.

        Probing only that the host answers is misleading here: MCC's web app
        replies 200 on / while its speech endpoint returns 503 because
        sherpa-onnx is not configured on that machine. A green light that
        fails at the first dub is worse than a red one, so this asks the
        endpoint itself and repeats the server's own explanation.
        """
        if not self.base_url:
            return False, "no URL configured for this service"

        now = time.time()
        if self._cached and now - self._cached[0] < 60:
            return self._cached[1], self._cached[2]

        body, ctype, err = _post(self.base_url + "/api/tts", {"text": "test"}, 30)
        if err:
            ok, msg = False, err
        elif _audio_from(body, ctype):
            ok, msg = True, ""
        else:
            ok, msg = False, f"{self.base_url}/api/tts returned no audio"
        self._cached = (now, ok, msg)
        return ok, msg

    def voices(self) -> list[Voice]:
        return [Voice(id="default", label="MCC sherpa-onnx voice", kind="preset")]

    def synth(self, text, out_path, *, voice=None, reference=None, **_) -> SpeechResult:
        ok, msg = self.health()
        if not ok:
            return SpeechResult(engine=self.id, error=msg)
        if not (text or "").strip():
            return SpeechResult(engine=self.id, error="nothing to say")

        body, ctype, err = _post(self.base_url + "/api/tts",
                                 {"text": text.strip()}, SYNTH_TIMEOUT)
        if err:
            return SpeechResult(engine=self.id, error=err)
        audio = _audio_from(body, ctype)
        if not audio:
            return SpeechResult(engine=self.id, error="the server returned no audio")

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)
        return SpeechResult(path=out_path, seconds=_wav_seconds(out_path),
                            engine=self.id, voice="default")


def _audio_from(payload: bytes, ctype: str) -> bytes | None:
    if not payload:
        return None
    if payload[:4] == b"RIFF" or payload[:3] == b"ID3" or "audio/" in ctype:
        return payload
    if "json" in ctype or payload[:1] in (b"{", b"["):
        try:
            doc = json.loads(payload)
        except ValueError:
            return None
        if isinstance(doc, dict):
            for key in ("audio", "audio_base64", "data", "wav"):
                val = doc.get(key)
                if isinstance(val, str) and val:
                    try:
                        return base64.b64decode(val, validate=False)
                    except Exception:  # noqa: BLE001
                        continue
    return None


def _wav_seconds(p: Path) -> float:
    try:
        with wave.open(str(p), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:  # noqa: BLE001
        return 0.0
