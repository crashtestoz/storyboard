"""Qwen3-TTS via MCC's HTTP service.

This is the engine to prefer when it is reachable: it is the same voice stack
MCC already uses, so dialogue in a storyboard sounds like everything else in
that system rather than introducing a second voice.

**It needs one piece of configuration this repo could not discover.** Qwen3-TTS
is not a model vpipe implements and it is not installed on this machine, so the
only way to use it is to call MCC, and the MCC sources were not readable from
here (the ``claude-code`` account has no access to ``admin/mcc``). The request
shape below is therefore an assumption, not a transcription.

Point it at the service with ``--tts-url`` (or ``SBV_TTS_URL``) and, if the
assumed contract does not match, adjust ``REQUEST_FIELD_*`` / :meth:`_parse`
here — everything else in the project is already engine-agnostic.

Assumed contract::

    POST {base}/api/tts
      {"text": "...", "voice": "<voice id>"}
    -> audio bytes (audio/wav or audio/mpeg)

    GET {base}/api/tts/voices
    -> {"voices": [{"id": "...", "label": "..."}, ...]}

A JSON reply carrying base64 audio under ``audio``/``data``/``wav`` is also
handled, since that is the other common shape.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
import wave
from pathlib import Path

from .base import SpeechResult, TTSEngine, Voice

REQUEST_FIELD_TEXT = "text"
REQUEST_FIELD_VOICE = "voice"
SYNTH_PATH = "/api/tts"
VOICES_PATH = "/api/tts/voices"
TIMEOUT = 120


class MccQwen3TTS(TTSEngine):
    id = "mcc-qwen3"
    label = "Qwen3-TTS via MCC (shared with the visual office)"
    # MCC's own API may support reference audio; not assumed here.
    supports_cloning = False

    def __init__(self, base_url: str = ""):
        self.base_url = (base_url or "").rstrip("/")

    # ------------------------------------------------------------------ #

    def health(self) -> tuple[bool, str]:
        if not self.base_url:
            return False, (
                "No MCC TTS URL configured. Start with "
                "--tts-url http://<mcc-host>:<port> (or set SBV_TTS_URL). "
                "The endpoint shape is assumed — see "
                "server/tts/mcc_qwen3.py if it needs adjusting."
            )
        try:
            with urllib.request.urlopen(self.base_url + VOICES_PATH, timeout=5) as r:
                r.read(1)
            return True, ""
        except urllib.error.HTTPError as exc:
            # the host is up; the path may simply differ
            return exc.code < 500, (
                f"{self.base_url}{VOICES_PATH} returned {exc.code} — the service "
                "is reachable but this path may not be its voices endpoint"
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"cannot reach {self.base_url}: {exc}"

    def voices(self) -> list[Voice]:
        if not self.base_url:
            return []
        try:
            with urllib.request.urlopen(self.base_url + VOICES_PATH, timeout=5) as r:
                data = json.loads(r.read() or b"{}")
        except Exception:  # noqa: BLE001
            return []
        raw = data.get("voices") if isinstance(data, dict) else data
        out: list[Voice] = []
        for v in raw or []:
            if isinstance(v, str):
                out.append(Voice(id=v, label=v))
            elif isinstance(v, dict):
                vid = v.get("id") or v.get("name") or v.get("voice")
                if vid:
                    out.append(
                        Voice(id=str(vid), label=str(v.get("label") or vid),
                              language=str(v.get("language") or ""))
                    )
        return out

    # ------------------------------------------------------------------ #

    def synth(
        self,
        text: str,
        out_path: Path,
        *,
        voice: str | None = None,
        reference: Path | None = None,
    ) -> SpeechResult:
        ok, msg = self.health()
        if not ok:
            return SpeechResult(engine=self.id, error=msg)

        text = (text or "").strip()
        if not text:
            return SpeechResult(engine=self.id, error="nothing to say")

        body = {REQUEST_FIELD_TEXT: text}
        if voice:
            body[REQUEST_FIELD_VOICE] = voice

        req = urllib.request.Request(
            self.base_url + SYNTH_PATH,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                payload = r.read()
                ctype = (r.headers.get("Content-Type") or "").lower()
        except Exception as exc:  # noqa: BLE001
            return SpeechResult(engine=self.id, error=f"MCC TTS request failed: {exc}")

        audio = self._parse(payload, ctype)
        if audio is None:
            return SpeechResult(
                engine=self.id,
                error=(
                    f"MCC replied with {ctype or 'no content type'} and no audio "
                    "this engine recognised — the assumed response shape probably "
                    "needs adjusting in server/tts/mcc_qwen3.py"
                ),
            )

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)
        return SpeechResult(
            path=out_path,
            seconds=_wav_seconds(out_path),
            engine=self.id,
            voice=voice or "default",
        )

    @staticmethod
    def _parse(payload: bytes, ctype: str) -> bytes | None:
        """Raw audio, or base64 audio inside a JSON envelope."""
        if payload[:4] == b"RIFF" or payload[:3] == b"ID3" or "audio/" in ctype:
            return payload
        if "json" in ctype or payload[:1] in (b"{", b"["):
            try:
                doc = json.loads(payload)
            except ValueError:
                return None
            if isinstance(doc, dict):
                for key in ("audio", "data", "wav", "audio_base64", "content"):
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
