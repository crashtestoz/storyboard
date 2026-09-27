"""How long a shot needs to be, from what its prompt asks for.

A video model has no sense of time: give it six actions and 5 seconds and it
does all six, fast. The render then shows motion blur where a turn was rushed
and ends before the last action finishes (a gull-wing door that never closes).
Checking the length before rendering costs a minute; finding out after costs
the whole render.

Two parts, and the shot needs the longer of them, since a character talks
while acting:

*   **Dialogue** is measured, not guessed: the recorded take's own length when
    one exists and still matches the line, otherwise the word count at a
    natural speaking rate plus pauses.
*   **Action** is judged by the board's LLM, which splits the prompt into the
    ordered beats the model must show and gives each a minimum and a
    comfortable duration, including holds a viewer needs (reading on-screen
    text, a reaction registering).

Both totals are snapped up to a length the shot's video model can render, so
the suggestion is always one the Duration picker offers.
"""

from __future__ import annotations

import json
import math
import re
import wave
from pathlib import Path
from typing import Any

from .llm import LLMService

FPS = 24
# Conversational English; a delivery note like "slowly" or "rapid" moves it.
WORDS_PER_SECOND = 2.5
PAUSE_SECONDS = 0.4          # per "...", "—" or line break between lines
DIALOGUE_LEAD_SECONDS = 0.5  # a beat before the first word and after the last
H3_MAX_FRAMES = 362          # H3's released clip range tops out at ~15 s

SYSTEM_PROMPT = """\
You time shots for an AI video model. Given one shot's prompt, list the \
ordered beats of action the clip must show, and how long each needs at \
natural, real-world speed: not rushed, not slow motion.

Rules:
- One beat per distinct visible action or change, in order. Merge actions \
that happen at the same moment into one beat.
- A camera move that runs during other beats is not a separate beat.
- Include holds a viewer needs: on-screen text must stay readable (about 3 \
words per second plus 1 second), and a reaction or expression needs a moment \
to register.
- Ignore dialogue and sound; they are timed separately.
- "min" is the shortest the beat can take and still read clearly; \
"comfortable" is an unhurried, natural pace. Seconds, one decimal place.

Typical durations to anchor on (min–comfortable, seconds):
- turn the head or glance at someone: 0.6–1.0; hold a look or expression \
so it registers: +0.5
- turn the whole body: 1.0–1.5
- reach for and grab something close: 0.5–0.8
- sit down, or stand up: 1.2–1.8; climb into or out of a car seat: 1.8–2.5
- open or close a door, lid or hatch: 1.0–1.5; a heavy or overhead one: 1.5–2.0
- take a few steps: 1.0–1.5 per step pair; cross a room: 3.0–4.0
- a gesture (wave, point, nod, shrug): 0.6–1.0
- an object or title sliding into place: 0.8–1.2; a fade in or out: 0.6–1.0
- a hand writing or scribbling a short word: 1.0–1.5
- the final beat of a shot, so the cut is not abrupt: 0.3–0.5 (include it)

Reply with JSON only, no prose and no markdown fences:
{"beats":[{"action":"short description","min":1.5,"comfortable":2.0}],\
"note":"one sentence on anything that makes this shot hard to time, or empty"}
"""


# --------------------------------------------------------------------------
# dialogue
# --------------------------------------------------------------------------

def dialogue_seconds(shot: dict[str, Any], shot_dir: Path | None) -> tuple[float, str]:
    """(seconds, how it was worked out) for the shot's spoken line; (0, "")
    when it has none."""
    line = (shot.get("dialogue") or "").strip()
    if not line:
        return 0.0, ""
    take = shot_dir / "dialogue.wav" if shot_dir else None
    if (take and take.is_file()
            and (shot.get("dialogueSpokenText") or "").strip() == line):
        try:
            with wave.open(str(take)) as w:
                return w.getnframes() / float(w.getframerate()), "recorded take"
        except (wave.Error, OSError, ZeroDivisionError):
            pass
    words = len(re.findall(r"[\w'’-]+", line))
    pauses = len(re.findall(r"\.\.\.|…|—|\n\s*\n", line))
    rate = WORDS_PER_SECOND
    style = (shot.get("dialogueStyle") or "").lower()
    if re.search(r"slow|hesita|drawl|whisper|breathy|tired", style):
        rate *= 0.8
    elif re.search(r"fast|rapid|excited|panic|rushed|manic", style):
        rate *= 1.2
    return words / rate + pauses * PAUSE_SECONDS, f"{words} words at ~{rate:.1f} words/s"


# --------------------------------------------------------------------------
# action beats (LLM)
# --------------------------------------------------------------------------

def _first_json_object(text: str) -> dict[str, Any] | None:
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S | re.I)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        doc = json.loads(text[start:i + 1])
                        return doc if isinstance(doc, dict) else None
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def action_beats(service: LLMService, prompt: str) -> tuple[list[dict[str, Any]], str]:
    """The prompt's ordered beats with min/comfortable seconds, from the LLM."""
    raw = service.complete(SYSTEM_PROMPT, prompt, timeout=180.0)
    doc = _first_json_object(raw or "")
    if not doc or not isinstance(doc.get("beats"), list):
        raise ValueError("the model's reply had no beat list — try again")
    beats = []
    for b in doc["beats"]:
        if not isinstance(b, dict):
            continue
        try:
            lo = max(0.1, float(b.get("min")))
            hi = max(lo, float(b.get("comfortable") or lo))
        except (TypeError, ValueError):
            continue
        action = str(b.get("action") or "").strip()
        if action:
            beats.append({"action": action, "min": round(lo, 1), "comfortable": round(hi, 1)})
    if not beats:
        raise ValueError("the model found no action to time in this prompt")
    return beats, str(doc.get("note") or "").strip()


# --------------------------------------------------------------------------
# putting it together
# --------------------------------------------------------------------------

def _frames_for(seconds: float, frame_rule) -> int:
    frames = max(1, math.ceil(seconds * FPS))
    return frame_rule.snap(frames) if frame_rule is not None else frames


def estimate(service: LLMService, shot: dict[str, Any], *, frame_rule=None,
             shot_dir: Path | None = None, h3: bool = True) -> dict[str, Any]:
    """The full length check for *shot*, as stored on it and shown in the
    Parameters card. The verdict against the shot's current length is left to
    the caller to recompute, since the Duration can change afterwards."""
    prompt = (shot.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("this shot has no prompt to time")
    beats, note = action_beats(service, prompt)
    act_min = sum(b["min"] for b in beats)
    act_ok = sum(b["comfortable"] for b in beats)
    talk, talk_basis = dialogue_seconds(shot, shot_dir)
    talk_total = talk + 2 * DIALOGUE_LEAD_SECONDS if talk else 0.0

    min_s = max(act_min, talk_total)
    ok_s = max(act_ok, talk_total)
    min_f = _frames_for(min_s, frame_rule)
    ok_f = _frames_for(ok_s, frame_rule)
    split = h3 and min_f > H3_MAX_FRAMES
    return {
        "beats": beats,
        "note": note,
        "actionSeconds": {"min": round(act_min, 1), "comfortable": round(act_ok, 1)},
        "dialogueSeconds": round(talk, 1),
        "dialogueBasis": talk_basis,
        "minSeconds": round(min_s, 1),
        "comfortableSeconds": round(ok_s, 1),
        "minFrames": min(min_f, H3_MAX_FRAMES) if h3 else min_f,
        "comfortableFrames": min(ok_f, H3_MAX_FRAMES) if h3 else ok_f,
        "tooLongForOneShot": split,
        "limitedBy": "dialogue" if talk_total > act_ok else "action",
        # what was timed, so the card can tell when the prompt has moved on
        "basis": {"prompt": prompt, "dialogue": (shot.get("dialogue") or "").strip()},
        "service": getattr(service, "label", ""),
    }


def verdict(est: dict[str, Any], frames: int) -> str:
    """'short' | 'tight' | 'ok' for a shot of *frames* against *est*."""
    if frames < est.get("minFrames", 0):
        return "short"
    if frames < est.get("comfortableFrames", 0):
        return "tight"
    return "ok"
