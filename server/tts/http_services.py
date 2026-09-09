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
import urllib.error
import urllib.request
import wave
from pathlib import Path

from .base import SpeechResult, TTSEngine, Voice

SYNTH_TIMEOUT = 600      # cloning a voice is CPU-bound and slow
TRANSCRIBE_TIMEOUT = 120
PROBE_TIMEOUT = 4


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

    def __init__(self, service_id: str, label: str, base_url: str):
        self.id = service_id
        self.label = label
        self.base_url = (base_url or "").rstrip("/")

    def health(self) -> tuple[bool, str]:
        if not self.base_url:
            return False, "no URL configured for this service"
        ok, err = _reachable(self.base_url + "/")
        if ok:
            return True, ""
        return False, (
            f"cannot reach {self.base_url} ({err}). This server binds "
            "127.0.0.1 by default, so it is only reachable from the machine it "
            "runs on — rebind it, tunnel it, or run an instance locally."
        )

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
        ref_text = (reference_text or "").strip()
        if not ref_text:
            # The server requires a transcript; ask it to make one rather than
            # failing on a requirement it can satisfy itself.
            ref_text, terr = self.transcribe(reference)
            if not ref_text:
                return SpeechResult(
                    engine=self.id,
                    error=(
                        "the reference clip needs a transcript of what it says "
                        + (f"(auto-transcribe failed: {terr})" if terr else "")
                    ),
                )

        body, ctype, err = _post(
            self.base_url + "/synthesize",
            {
                "reference_audio_b64": base64.b64encode(reference.read_bytes()).decode(),
                "reference_ext": reference.suffix.lstrip(".").lower() or "wav",
                "reference_text": ref_text,
                "text": text.strip(),
                "language": language,
                "style": "",
                "speed": speed,
            },
            SYNTH_TIMEOUT,
        )
        if err:
            return SpeechResult(engine=self.id, error=err)
        audio = _audio_from(body, ctype)
        if not audio:
            return SpeechResult(engine=self.id,
                                error="the server returned no audio")

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)
        return SpeechResult(path=out_path, seconds=_wav_seconds(out_path),
                            engine=self.id, voice="clone")


class MccSherpaTTS(TTSEngine):
    """MCC's /api/tts — a sherpa-onnx VITS voice. Text in, wav out."""

    supports_cloning = False

    def __init__(self, service_id: str, label: str, base_url: str):
        self.id = service_id
        self.label = label
        self.base_url = (base_url or "").rstrip("/")

    def health(self) -> tuple[bool, str]:
        if not self.base_url:
            return False, "no URL configured for this service"
        ok, err = _reachable(self.base_url + "/")
        return (True, "") if ok else (False, f"cannot reach {self.base_url} ({err})")

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
