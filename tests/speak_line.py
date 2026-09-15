#!/usr/bin/env python3
"""Speaking a shot's line: who says it, and what happens without a render.

"Speak this line" used to demand a rendered clip, because synthesis and muxing
were one function. That is the wrong order: hearing whether a cloned voice
reads a line correctly should not cost half an hour of video first. So the two
are separate, and the speech is returned whether or not there is a clip.

No network and no models here — the engine is a stub that writes a wav. The
branching is what matters, and it should be testable without a 9 GB download.

Run:  python3 tests/speak_line.py
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.dubbing import (                                   # noqa: E402
    _duration,
    dub_shot,
    polish_speech,
    speak_line,
    speaker_for,
)
from server.tts.base import SpeechResult, TTSEngine            # noqa: E402
from server.tts.http_services import _qwen_style_instruction   # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'pass' if ok else 'FAIL'}  {name}" + ("" if ok else f"  — {detail}"))
    if not ok:
        failures.append(name)


class StubEngine(TTSEngine):
    """Writes a real, if boring, wav. Records what it was asked for."""

    id = "stub"
    label = "Stub engine"
    supports_cloning = True

    def __init__(self, seconds: float = 1.0, fail: str = ""):
        self.seconds = seconds
        self.fail = fail
        self.calls: list[dict] = []

    def synth(self, text, out_path, *, voice=None, reference=None, **kw):
        self.calls.append({
            "text": text, "voice": voice, "reference": reference,
            "reference_text": kw.get("reference_text", ""),
            "style": kw.get("style", ""),
        })
        if self.fail:
            return SpeechResult(engine=self.id, error=self.fail)
        out_path = Path(out_path)
        write_tone(out_path, self.seconds)
        return SpeechResult(path=out_path, seconds=self.seconds,
                            engine=self.id, voice="clone" if reference else "default")


def write_tone(path: Path, seconds: float, *, amplitude: float = 0.05,
               trailing_silence: float = 0.0, rate: int = 24000) -> None:
    """A quiet tone, optionally followed by silence.

    Deliberately quiet and optionally padded, because that is what the models
    actually produce: a take here arrived at -23 dBFS with 74 seconds of
    silence after a 5-second line.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    peak = int(32767 * amplitude)
    frames = bytearray()
    for i in range(int(rate * seconds)):
        # a plain triangle wave — no math import needed, and it has real energy
        phase = (i % 200) / 200.0
        v = int(peak * (4 * abs(phase - 0.5) - 1))
        frames += struct.pack("<h", v)
    frames += struct.pack("<h", 0) * int(rate * trailing_silence)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def peak_dbfs(path: Path) -> float:
    """Max volume in dBFS via ffmpeg's volumedetect."""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect",
         "-f", "null", "/dev/null"],
        capture_output=True, text=True, timeout=60,
    )
    for line in (out.stderr or "").splitlines():
        if "max_volume:" in line:
            return float(line.split("max_volume:")[1].strip().split()[0])
    return -999.0


def test_polish(tmp: Path) -> None:
    print("\n-- trimming silence and raising the level --")
    if not shutil.which("ffmpeg"):
        print("skip  ffmpeg not on PATH")
        return

    # the shape of the real complaint: a short line, then a long silent tail
    f = tmp / "polish" / "tail.wav"
    write_tone(f, 2.0, amplitude=0.05, trailing_silence=8.0)
    from server.dubbing import _duration
    before_secs, before_peak = _duration(f), peak_dbfs(f)
    secs, log = polish_speech(f)
    after_peak = peak_dbfs(f)

    check("the silent tail is trimmed", 1.5 < secs < 3.0,
          f"{before_secs:.1f}s -> {secs:.1f}s")
    check("the reported duration is the trimmed one", abs(secs - _duration(f)) < 0.05)
    check("a quiet take is brought up", after_peak > before_peak + 6,
          f"{before_peak:.1f} dB -> {after_peak:.1f} dB")
    check("it does not clip", after_peak <= 0.0, f"{after_peak:.1f} dB")
    check("the trim is reported in the log",
          any("trimmed" in l for l in log), str(log))
    check("the level change is reported in the log",
          any("levelled" in l for l in log), str(log))

    # pauses *inside* a line are the delivery and must survive
    g = tmp / "polish" / "gap.wav"
    write_tone(g, 1.0, amplitude=0.05)
    with wave.open(str(g), "rb") as r:
        params, body = r.getparams(), r.readframes(r.getnframes())
    with wave.open(str(g), "wb") as w:
        w.setparams(params)
        w.writeframes(body + struct.pack("<h", 0) * 24000 + body)
    inner_before = _duration(g)
    inner_secs, _ = polish_speech(g)
    check("an internal pause is not removed", inner_secs > inner_before - 0.2,
          f"{inner_before:.1f}s -> {inner_secs:.1f}s")


def board_with_cast() -> dict:
    return {
        "characters": [
            {"id": "c1", "name": "Kira", "description": "a pilot",
             "voice": {"path": "x/refs/kira.wav"}, "voiceText": "hello there"},
            {"id": "c2", "name": "Vex", "description": "a courier",
             "voice": None, "voiceText": ""},
            {"id": "c3", "name": "Narrator", "description": "unseen",
             "voice": {"path": "x/refs/narr.wav"}, "voiceText": "once upon"},
        ]
    }


def test_speaker_for() -> None:
    print("\n-- who speaks --")
    board = board_with_cast()
    name = lambda ch: (ch or {}).get("name")

    check("one character in the shot needs no telling",
          name(speaker_for({"characterIds": ["c1"]}, board)) == "Kira")
    check("prefers whoever has a recorded voice",
          name(speaker_for({"characterIds": ["c2", "c1"]}, board)) == "Kira",
          str(speaker_for({"characterIds": ["c2", "c1"]}, board)))
    check("falls back to the first cast member when none has a clip",
          name(speaker_for({"characterIds": ["c2"]}, board)) == "Vex")
    check("an explicit choice wins over the preference",
          name(speaker_for({"characterIds": ["c1", "c2"], "speakerId": "c2"}, board)) == "Vex")
    check("an explicit choice is honoured even outside the shot's cast",
          name(speaker_for({"characterIds": ["c1"], "speakerId": "c3"}, board)) == "Narrator")
    check("no cast means nobody speaks",
          speaker_for({"characterIds": []}, board) is None)
    check("a dangling speakerId does not crash",
          name(speaker_for({"characterIds": ["c1"], "speakerId": "gone"}, board)) == "Kira")


def test_qwen_style_prompt() -> None:
    print("\n-- qwen style instruction --")
    empty = _qwen_style_instruction("")
    plain = _qwen_style_instruction("polite and anxious")
    template = _qwen_style_instruction(
        "[STYLE / VOICE DIRECTION]\n\nformal\n\n[TEXT TO SPEAK]\n\n{{TEXT}}\n\n[END]",
    )
    pasted = _qwen_style_instruction(
        "[STYLE / VOICE DIRECTION]\n\nformal\n\n[TEXT TO SPEAK]\n\nold placeholder\n\n[END]",
    )
    check("empty style stays empty", empty == "", empty)
    check("plain style stays plain", plain == "polite and anxious", plain)
    check("full pasted template keeps direction",
          template == "formal",
          template)
    check("full pasted template removes text section",
          pasted == "formal" and "old placeholder" not in pasted and "{{TEXT}}" not in pasted,
          pasted)


def test_preview_without_render(tmp: Path) -> None:
    print("\n-- preview, with no clip rendered --")
    eng = StubEngine(seconds=1.5)
    shot_dir = tmp / "shots" / "01"

    r = dub_shot(eng, clip=shot_dir / "clip.mp4", shot_dir=shot_dir,
                 text="And that is how you fly it.",
                 reference=Path("/some/kira.wav"), reference_text="hello there",
                 style="quiet, breathy, tired")
    check("succeeds with no clip to mux into", r.ok, r.error)
    check("no video is claimed", r.video is None)
    check("the spoken line is returned", bool(r.audio and r.audio.is_file()))
    check("the wav is real", r.audio.stat().st_size > 1000)
    check("the reference clip was passed to the engine",
          eng.calls and eng.calls[0]["reference"] == Path("/some/kira.wav"))
    check("the transcript was passed too",
          eng.calls and eng.calls[0]["reference_text"] == "hello there")
    check("voice direction was passed separately",
          eng.calls and eng.calls[0]["style"] == "quiet, breathy, tired")

    empty = dub_shot(eng, clip=shot_dir / "clip.mp4", shot_dir=shot_dir, text="   ")
    check("an empty line is refused", not empty.ok and "no dialogue" in empty.error,
          empty.error)

    broken = dub_shot(StubEngine(fail="the service is down"),
                      clip=shot_dir / "clip.mp4", shot_dir=shot_dir, text="hi")
    check("an engine failure is reported, not swallowed",
          not broken.ok and "service is down" in broken.error, broken.error)


def test_mux_when_clip_exists(tmp: Path) -> None:
    print("\n-- mixing over a rendered clip --")
    if not shutil.which("ffmpeg"):
        print("skip  ffmpeg not on PATH")
        return

    shot_dir = tmp / "shots" / "02"
    shot_dir.mkdir(parents=True)
    clip = shot_dir / "clip.mp4"
    # two seconds of black with silence, so there is a real video to mux onto
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:s=128x72:d=2",
         "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
         "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(clip)],
        check=True, capture_output=True, timeout=120,
    )

    r = dub_shot(StubEngine(seconds=1.0), clip=clip, shot_dir=shot_dir, text="short line")
    check("mux succeeds", r.ok, r.error)
    check("a dubbed video is produced", bool(r.video and r.video.is_file()))
    check("the original clip is left alone", clip.is_file())
    check("no truncation warning for a line that fits", r.warning == "", r.warning)

    # a line longer than its clip must say so rather than be cut silently
    long = dub_shot(StubEngine(seconds=5.0), clip=clip, shot_dir=shot_dir, text="long line")
    check("mux still succeeds for an overlong line", long.ok, long.error)
    check("an overlong line is warned about", "cut off" in long.warning, long.warning)


def test_replace_mode_drops_original_audio(tmp: Path) -> None:
    print("\n-- dubMode: replace --")
    if not shutil.which("ffmpeg"):
        print("skip  ffmpeg not on PATH")
        return

    shot_dir = tmp / "shots" / "04"
    shot_dir.mkdir(parents=True)
    clip = shot_dir / "clip.mp4"
    # 2s of video with a real (silent) audio track, same as the mux test above.
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:s=128x72:d=2",
         "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
         "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(clip)],
        check=True, capture_output=True, timeout=120,
    )

    # Mixed: the bed (2s) outlasts the 1s line, so the dub stays clip-length —
    # this is what lets engine hum/ambience survive under a short line.
    mixed = dub_shot(StubEngine(seconds=1.0), clip=clip, shot_dir=shot_dir,
                      text="short line", keep_original_audio=True)
    check("mixed dub succeeds", mixed.ok, mixed.error)
    check("mixed dub keeps the clip's own length",
          abs(_duration(mixed.video) - 2.0) < 0.2,
          _duration(mixed.video))

    # Replacement pads the short line so the final frame remains available.
    replaced = dub_shot(StubEngine(seconds=1.0), clip=clip, shot_dir=shot_dir,
                        text="short line", keep_original_audio=False)
    check("replaced dub succeeds", replaced.ok, replaced.error)
    check("replaced dub preserves the full clip for continuity",
          abs(_duration(replaced.video) - 2.0) < 0.2,
          _duration(replaced.video))


def test_speak_line_alone(tmp: Path) -> None:
    print("\n-- speak_line on its own --")
    eng = StubEngine(seconds=0.5)
    r = speak_line(eng, shot_dir=tmp / "shots" / "03", text="just the words")
    check("writes dialogue.wav", r.ok and r.path.name == "dialogue.wav", r.error)
    check("no video involved at all", not hasattr(r, "video"))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="sbv-speak-"))
    try:
        test_speaker_for()
        test_qwen_style_prompt()
        test_polish(tmp)
        test_preview_without_render(tmp)
        test_mux_when_clip_exists(tmp)
        test_replace_mode_drops_original_audio(tmp)
        test_speak_line_alone(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
