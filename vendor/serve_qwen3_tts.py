#!/usr/bin/env python3
"""
Qwen3-TTS voice-cloning inference server.

Runs on the OptiPlex gateway itself (CPU-only, i7, 16GB RAM), alongside the MCC app
that calls it over HTTP via /api/tts/clone (see src/lib/server/tts-clone.ts in the
mcc project). Deliberately NOT offloaded to the Mac Mini the way image generation
is - Qwen3-TTS-0.6B is a ~2.5GB model and this is a non-realtime "click generate,
wait a bit" workload, so it doesn't need a GPU, and colocating with MCC avoids a
cross-machine dependency on the Mac Mini being on and reachable. Ollama does not
support TTS models at all regardless of host, so unlike image generation this is a
small dedicated FastAPI server rather than an Ollama call.

Model: Qwen/Qwen3-TTS-12Hz-0.6B-Base (Apache 2.0, ~2.5GB). This is the "Base"
variant specifically - the sibling "CustomVoice" variant only picks from a fixed
set of built-in speakers and does NOT clone an arbitrary uploaded reference clip,
which is what this feature needs. Uses the dedicated `qwen-tts` PyPI package
(`Qwen3TTSModel.generate_voice_clone()`), not generic transformers
AutoModel/AutoProcessor - Qwen3-TTS ships its own model class, confirmed against
the model cards at https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-Base and
https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice.

Chosen over dots.tts (rednote-hilab, 2B params) because dots.tts's only serving
path is vLLM-Omni, which targets CUDA and has no mature CPU story - a bad fit
either way. Qwen3-TTS is smaller and lighter to run CPU-only on the OptiPlex.

generate_voice_clone() requires ref_text - a transcript of what the reference
clip actually says, not just the clip itself. Typing that by hand is a chore, so
this also runs faster-whisper (CPU-efficient CTranslate2 build of Whisper, not
the heavier openai-whisper PyTorch package) to auto-transcribe the reference
clip via POST /transcribe - MCC's /tts page calls this right after upload and
pre-fills the transcript field, which stays editable since ASR isn't perfect.
Whisper is a well-established, thoroughly documented model/package - used here
deliberately instead of anything newer/less certain, after the Qwen3-TTS model
ID/API mismatches that came up getting the cloning path itself working.

The `qwen-tts` package is very new (shipped alongside the model in Jan 2026); if
`pip install -U qwen-tts` or the calls below start erroring after a `qwen-tts`
version bump, check https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-Base for the
current reference snippet.

Usage:
    python3 serve_qwen3_tts.py [--host 127.0.0.1] [--port 8790]
                                [--model Qwen/Qwen3-TTS-12Hz-0.6B-Base]
                                [--whisper-model small]

Endpoints:
    GET  /health
    POST /synthesize   {reference_audio_b64, reference_ext, reference_text,
                         text, language, style, speed}
                        -> 200 with audio/wav body, or 4xx/5xx with {"error": "..."}
    POST /transcribe   {reference_audio_b64, reference_ext}
                        -> 200 {"text": "...", "language": "en"}, or 4xx/5xx with {"error": "..."}
"""

import argparse
import base64
import io
import json
import logging
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("qwen3-tts-server")

DEFAULT_MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
DEFAULT_WHISPER_MODEL = "small"

app = FastAPI(title="Qwen3-TTS Voice Clone Server")

_model = None
_model_id = DEFAULT_MODEL
_supports_instruct = True  # narrowed to False if the installed qwen-tts rejects `instruct`
_whisper_model = None
_whisper_model_id = DEFAULT_WHISPER_MODEL
_forced_device = ""
_forced_dtype = "float32"


class SynthesizeRequest(BaseModel):
    reference_audio_b64: str
    reference_ext: str = "wav"
    reference_text: str
    text: str
    language: str = "en"
    style: Optional[str] = ""
    speed: float = 1.0


class TranscribeRequest(BaseModel):
    reference_audio_b64: str
    reference_ext: str = "wav"


def load_model(model_id: str):
    """Load the Qwen3-TTS model once at startup and keep it resident (avoids the
    multi-second model-load cost on every request, same reasoning as Ollama's
    keep_alive for the image generator)."""
    global _model, _model_id
    logger.info("Loading %s ...", model_id)

    import torch
    from qwen_tts import Qwen3TTSModel

    # OptiPlex has no GPU, so this normally resolves to the cpu branch - the
    # cuda/mps branches are just so this still does the right thing if ever run
    # somewhere with a GPU.
    #
    # History on the cpu branch's dtype, because it's bounced around twice:
    # 1. Originally float32 (this box's only proven-good config for a long
    #    stretch - no OOM ever recorded in journalctl's full history).
    # 2. A live-only edit to float16 (to roughly halve the ~8GiB RSS) made
    #    every generation request fail with "probability tensor contains inf,
    #    nan or element < 0" - float16's exponent range is too narrow for
    #    PyTorch's CPU kernels, which aren't the mixed-precision-aware GPU
    #    ones that normally keep float16 accumulations in range.
    # 3. Switched to bfloat16 instead (same 2 bytes/value, full 8-bit exponent
    #    so it doesn't overflow the same way) - this did fix the NaN crash.
    #    But this host's CPU (i7-8700T, Coffee Lake) has no AVX-512/native
    #    bf16 support, so PyTorch falls back to a much slower bf16 CPU path:
    #    confirmed live, a 6-word test sentence that should take ~20-70s (the
    #    documented float32 baseline) took 384s under bfloat16. Combined with
    #    the deploy pipeline's unconditional `systemctl restart` of this
    #    service whenever its own files change (see optiplex-autopull.sh),
    #    a generation that now runs 5-10x longer is far more likely to still
    #    be in flight when the next deploy restarts the process out from
    #    under it, dropping the connection - this is what actually produced
    #    the "Could not reach voice cloning server ... fetch failed" reports.
    # Reverted to float32: it's the only variant with a long track record of
    # both correctness and acceptable latency on this specific CPU. If memory
    # pressure becomes a real (observed, not theoretical) problem again, a CPU
    # int8 dynamic quantization path would be worth trying instead of float16
    # or bfloat16, neither of which this hardware handles well.
    # Apple Silicon note (this host, a Mac mini M4 Pro), added after the mps
    # branch failed the first time it was ever actually exercised:
    #
    # mps + float16 produced exactly the failure the cpu history above
    # describes - "probability tensor contains inf, nan or element < 0" - and
    # for the same reason: float16's exponent range is too narrow for the
    # sampling step. So bfloat16 here too, for the reason given above ("same
    # 2 bytes/value, full 8-bit exponent so it doesn't overflow the same
    # way"), which is also why the cuda branch already picks it.
    #
    # The objection that sank bfloat16 on OptiPlex does not apply: that was an
    # i7-8700T with no native bf16, falling back to a slow emulated path.
    # Apple Silicon GPUs support bf16 natively, so this keeps both the
    # correctness and the speed. --device / --dtype override if needed.
    if _forced_device:
        device_map = _forced_device
        dtype = {"float32": torch.float32, "float16": torch.float16,
                 "bfloat16": torch.bfloat16}[_forced_dtype]
    elif torch.cuda.is_available():
        device_map, dtype = "cuda:0", torch.bfloat16
    elif torch.backends.mps.is_available():
        device_map, dtype = "mps", torch.bfloat16
    else:
        device_map, dtype = "cpu", torch.float32

    _model = Qwen3TTSModel.from_pretrained(model_id, device_map=device_map, dtype=dtype)
    _model_id = model_id
    logger.info("Model loaded (device_map=%s, dtype=%s)", device_map, dtype)


def load_whisper_model(model_size: str):
    """Load faster-whisper once at startup, same keep-resident reasoning as the
    TTS model. int8 compute_type keeps this cheap on CPU - a `small` model is a
    few hundred MB, small next to the ~2.5GB TTS model already resident."""
    global _whisper_model, _whisper_model_id
    logger.info("Loading faster-whisper model %s ...", model_size)

    from faster_whisper import WhisperModel

    _whisper_model = WhisperModel(model_size, device="cpu", compute_type="int8")
    _whisper_model_id = model_size
    logger.info("Whisper model loaded")


def _first_waveform(wavs):
    """generate_voice_clone() returns a waveform (or a list of them, for batch
    calls) - this server always makes single-item requests, so unwrap either
    shape down to one array."""
    if isinstance(wavs, (list, tuple)):
        return wavs[0]
    return wavs


def _apply_speed(audio_array, speed: float):
    """Speed adjustment via phase-vocoder time-stretching (librosa, pulled in
    transitively by qwen-tts - declared directly in requirements.txt since we
    now depend on it directly too). generate_voice_clone() doesn't expose a
    speed/duration control of its own, so this is applied as a post-processing
    step instead of guessing at an unconfirmed model kwarg.

    Was previously a naive resample (np.interp to a shorter/longer array),
    which changes pitch along with tempo - fine at ~1.0x but audibly
    chipmunk/growly and prone to aliasing artifacts away from it. Found while
    investigating a "output isn't clear" report - time_stretch keeps pitch
    fixed and doesn't introduce that class of artifact."""
    import librosa

    if speed <= 0:
        return audio_array
    return librosa.effects.time_stretch(audio_array.astype("float32"), rate=speed)


# MCC's UI/API use short ISO codes (matching TTS_CLONE_LANGUAGES in tts-clone.ts),
# but generate_voice_clone() rejects those - confirmed live: passing language="en"
# raises "Unsupported languages: ['en']. Supported: ['auto', 'chinese', 'english',
# ...]". Translated here, at the model boundary, so the rest of the stack (UI, API,
# CLI script) doesn't need to know about this mismatch.
LANGUAGE_CODE_MAP = {
    "en": "english",
    "zh": "chinese",
    "ja": "japanese",
    "ko": "korean",
    "de": "german",
    "fr": "french",
    "ru": "russian",
    "pt": "portuguese",
    "es": "spanish",
    "it": "italian",
}


def run_inference(
    reference_wav_path: str, reference_text: str, text: str, language: str, style: str, speed: float
) -> bytes:
    """Synthesize speech cloning the reference voice. Returns WAV bytes."""
    global _supports_instruct

    if _model is None:
        raise RuntimeError("Model not loaded")

    import soundfile as sf

    model_language = LANGUAGE_CODE_MAP.get(language.lower(), "auto")
    kwargs = dict(text=text, language=model_language, ref_audio=reference_wav_path, ref_text=reference_text)
    if style and style.strip() and _supports_instruct:
        kwargs["instruct"] = style.strip()
        try:
            wavs, sr = _model.generate_voice_clone(**kwargs)
        except TypeError:
            # Installed qwen-tts version's generate_voice_clone() doesn't accept
            # `instruct` for the Base/cloning variant - remember that and retry
            # without it rather than failing the whole request over a style hint.
            logger.warning("generate_voice_clone() rejected instruct=, retrying without it")
            _supports_instruct = False
            kwargs.pop("instruct")
            wavs, sr = _model.generate_voice_clone(**kwargs)
    else:
        wavs, sr = _model.generate_voice_clone(**kwargs)

    audio_array = _first_waveform(wavs)

    if speed and abs(speed - 1.0) > 1e-3:
        audio_array = _apply_speed(audio_array, speed)

    buffer = io.BytesIO()
    sf.write(buffer, audio_array, sr, format="WAV")
    return buffer.getvalue()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": _model_id,
        "loaded": _model is not None,
        "whisperModel": _whisper_model_id,
        "whisperLoaded": _whisper_model is not None,
    }


@app.post("/transcribe")
def transcribe(req: TranscribeRequest):
    if not req.reference_audio_b64:
        return JSONResponse(status_code=400, content={"error": "Missing reference_audio_b64."})
    if _whisper_model is None:
        return JSONResponse(status_code=503, content={"error": "Whisper model is still loading, try again shortly."})

    try:
        reference_bytes = base64.b64decode(req.reference_audio_b64)
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": f"Invalid reference_audio_b64: {exc}"})

    suffix = f".{req.reference_ext.lstrip('.')}" if req.reference_ext else ".wav"
    with tempfile.TemporaryDirectory() as tmpdir:
        ref_path = Path(tmpdir) / f"reference{suffix}"
        ref_path.write_bytes(reference_bytes)

        try:
            segments, info = _whisper_model.transcribe(str(ref_path), beam_size=5)
            text = " ".join(segment.text.strip() for segment in segments).strip()
        except Exception as exc:
            logger.exception("Transcription failed")
            return JSONResponse(status_code=500, content={"error": f"Transcription failed: {exc}"})

    if not text:
        return JSONResponse(
            status_code=422,
            content={"error": "Could not make out any speech in the reference clip - try a clearer recording."},
        )

    return {"text": text, "language": info.language}


@app.post("/synthesize")
def synthesize(req: SynthesizeRequest):
    if not req.text.strip():
        return JSONResponse(status_code=400, content={"error": "Empty text."})
    if not req.reference_audio_b64:
        return JSONResponse(status_code=400, content={"error": "Missing reference_audio_b64."})
    if not req.reference_text.strip():
        return JSONResponse(
            status_code=400,
            content={"error": "Missing reference_text - a transcript of what the reference clip says is required."},
        )
    if _model is None:
        return JSONResponse(status_code=503, content={"error": "Model is still loading, try again shortly."})

    try:
        reference_bytes = base64.b64decode(req.reference_audio_b64)
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": f"Invalid reference_audio_b64: {exc}"})

    suffix = f".{req.reference_ext.lstrip('.')}" if req.reference_ext else ".wav"
    with tempfile.TemporaryDirectory() as tmpdir:
        ref_path = Path(tmpdir) / f"reference{suffix}"
        ref_path.write_bytes(reference_bytes)

        try:
            wav_bytes = run_inference(
                str(ref_path),
                req.reference_text,
                req.text,
                req.language or "en",
                req.style or "",
                req.speed or 1.0,
            )
        except Exception as exc:
            logger.exception("Synthesis failed")
            return JSONResponse(status_code=500, content={"error": f"Synthesis failed: {exc}"})

    return Response(content=wav_bytes, media_type="audio/wav")


def main():
    parser = argparse.ArgumentParser(description="Qwen3-TTS voice-cloning inference server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--whisper-model", default=DEFAULT_WHISPER_MODEL, help="faster-whisper model size (tiny/base/small/medium/large-v3)")
    parser.add_argument("--device", default="", choices=["", "cpu", "mps", "cuda:0"],
                        help="override device selection (default: auto)")
    parser.add_argument("--dtype", default="float32",
                        choices=["float32", "float16", "bfloat16"],
                        help="dtype to use with --device")
    args = parser.parse_args()
    global _forced_device, _forced_dtype
    _forced_device, _forced_dtype = args.device, args.dtype

    load_model(args.model)
    load_whisper_model(args.whisper_model)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
