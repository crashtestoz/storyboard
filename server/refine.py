"""Auto-refine: draft a shot, review the clip against what was asked, adjust, repeat.

The Storyboard AD asks for this in plain words ("make a draft clip of scene 3,
review it, then adjust the prompt until it matches"). ``parse_refine_request``
spots that in code, rather than hoping a small local model emits the right
action, and the Orchestrator runs the loop (``Orchestrator.start_refine``).
Everything model-shaped lives here, behind ``Reviewer``:

1. **checklist** — once per run, the shot's prompt plus the user's request
   become a short list of things a still frame can show. Fixing the list up
   front is what makes attempt 3 comparable with attempt 1.
2. **judge** — stills from the drafted clip, plus the cast portraits, go to a
   vision model, which answers met / not met for each check. The score is
   computed here from those answers, not a number the model invents.
3. **revise** — the unmet checks, and what earlier attempts already tried,
   go back to the model to rewrite the shot's prompt.

All of it uses the AD's own configured model, so a local vision model works.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from .llm import H3_VISUAL_RULES, ClientGone, LLMService, streaming
from .storyboard_chat import (
    _Progress, _chat_reference_images, _first_json_object, clip_stills,
)

DEFAULT_ATTEMPTS = 3
MAX_ATTEMPTS = 8
DEFAULT_PASS_PERCENT = 100
MAX_CHECKS = 12
JUDGE_STILLS_MIN = 4        # per attempt: about one a second, first to last
JUDGE_STILLS_MAX = 8
JUDGE_STILL_EDGE = 512
JUDGE_MAX_PORTRAITS = 3
MODEL_TIMEOUT = 600.0

def still_count(seconds: float) -> int:
    """Stills to review a clip of *seconds* from: about one a second.

    Enough that something that happens in a beat (a pose change while the
    camera is blocked) lands between two of them, few enough that a local
    vision model can take them all in one request.
    """
    return max(JUDGE_STILLS_MIN, min(JUDGE_STILLS_MAX, math.ceil(seconds)))


# --- reading the request ------------------------------------------------- #

_WORDS = r"(?:tries|try|attempts?|times|rounds?|iterations?|passes|pass|renders?|drafts?|goes|loops?)"
_COUNT = re.compile(
    rf"\b(?:up to|max(?:imum)?(?: of)?|at most|no more than|stop after|limit(?:ed)? to|after)\s+"
    rf"(\d{{1,2}})\s*{_WORDS}\b|\b(\d{{1,2}})\s*{_WORDS}\s+(?:max|maximum|at most|tops)\b", re.I)
_SHOT = re.compile(r"\b(?:scene|shot)s?\s*#?\s*(\d{1,3})\b", re.I)
_RENDERY = re.compile(r"\b(draft|render|clip|preview|video|take)\b", re.I)
_REVIEWY = re.compile(r"\b(review|check|compare|look at|watch|inspect|judge|evaluate|critique)\b", re.I)
_ADJUSTY = re.compile(r"\b(adjust|fix|tweak|change|revise|rewrite|improve|retry|iterate|refine|"
                      r"correct|amend|re-?do)\b", re.I)
_LOOPY = re.compile(r"\b(keep trying|try again|loop|repeat|iterate|over and over|until|"
                    r"auto[- ]?refine|refine)\b", re.I)


def wants_refine(message: str) -> bool:
    """Does *message* ask for the draft → review → adjust loop?

    Either the explicit ``/refine`` (or "auto-refine"), or the three steps
    named together: a draft/render, a review, and an adjustment. A cheap
    false positive costs one click on Discard, so this leans towards matching.
    """
    text = message or ""
    if re.match(r"\s*/refine\b", text, re.I) or re.search(r"\bauto[- ]?refine\b", text, re.I):
        return True
    if not _RENDERY.search(text):
        return False
    if _REVIEWY.search(text) and _ADJUSTY.search(text):
        return True
    return bool(re.search(r"\b(keep trying|try again until|loop until|repeat until|iterate until|"
                          r"until (?:it|the clip|the draft|they|he|she)\b)", text, re.I))


def parse_refine_request(message: str, board: dict[str, Any],
                         selected_id: str | None) -> dict[str, Any] | None:
    """The refine request in *message*, or None when it isn't one.

    Returns ``{"shotId", "number", "requirement", "maxAttempts", "draft"}``,
    or ``{"error": "..."}`` when it is one but no scene can be told.
    """
    if not wants_refine(message):
        return None
    shots = board.get("shots") or []
    number = None
    m = _SHOT.search(message)
    if m and 1 <= int(m.group(1)) <= len(shots):
        number = int(m.group(1))
    elif selected_id:
        number = next((i for i, s in enumerate(shots, 1) if s.get("id") == selected_id), None)
    if number is None and len(shots) == 1:
        number = 1
    if number is None:
        return {"error": "Which scene should I refine? Select a shot, or say \"scene 3\"."}
    attempts = DEFAULT_ATTEMPTS
    c = _COUNT.search(message)
    if c:
        attempts = int(c.group(1) or c.group(2))
    attempts = max(1, min(attempts, MAX_ATTEMPTS))
    # What the clip should show, without the instructions for running the loop.
    requirement = re.sub(r"^\s*/refine\b:?", "", message, flags=re.I)
    requirement = _COUNT.sub("", requirement)
    requirement = re.sub(r"^[\s,:.;\-]*(?:for\s+)?(?:scene|shot)s?\s*#?\d{1,3}[\s,:.;\-]*", "",
                         requirement, flags=re.I)
    requirement = re.sub(r"^[\s,:.;\-]+", "", requirement).strip()
    return {
        "shotId": shots[number - 1]["id"],
        "number": number,
        "requirement": requirement[:1500],
        "maxAttempts": attempts,
        "draft": not re.search(r"\b(full|final)[- ]quality\b", message, re.I),
    }


def refine_reply(request: dict[str, Any], board: dict[str, Any],
                 checks: list[str] | None = None) -> dict[str, Any]:
    """The chat reply for a detected request: a Start card with the checklist.

    *checks* are what the clip will be judged against, shown for the user to
    edit before anything renders; without them the loop writes its own.
    """
    if "error" in request:
        return {"message": request["error"], "actions": []}
    shot = board["shots"][request["number"] - 1]
    if shot.get("locked"):
        return {"message": f"Scene {request['number']} is locked — unlock it first.",
                "actions": []}
    title = (shot.get("title") or "").strip()
    name = f"scene {request['number']}" + (f" (“{title}”)" if title else "")
    n = request["maxAttempts"]
    how = "draft" if request["draft"] else "full-quality"
    if checks:
        listed = ("Here is what each clip will be judged against — edit the checks, or the "
                  "number of attempts, before you start; the run is only as good as this list.")
    else:
        listed = "I'll write the checklist when the run starts."
    return {
        "message": (
            f"I can refine {name}: render a {how} clip, review stills from it against a "
            f"checklist, rewrite the prompt where it falls short, and render again — up to "
            f"{n} attempt{'s' if n != 1 else ''}, stopping early once every check passes. "
            f"{listed} Nothing on the board changes while it runs; at the end you can view "
            "each attempt's prompt and update the scene with the one you like."),
        "actions": [{"tool": "refine_shot", "shotId": request["shotId"],
                     "requirement": request["requirement"], "maxAttempts": n,
                     "draft": request["draft"], "checks": list(checks or [])}],
    }


def draft_checklist(service: LLMService, data_dir: Path, shot: dict[str, Any],
                    board: dict[str, Any], requirement: str, on_event=None) -> list[str]:
    """The checklist for the Start card; empty if the model could not write one.

    Failing here must not fail the chat turn — the loop writes its own when it
    starts — but the browser leaving (ClientGone) still has to stop the work.
    """
    progress = _Progress(on_event) if on_event else None
    with (streaming(progress.chunk, max_seconds=600) if progress else nullcontext()):
        if progress:
            progress.phase("Writing the checklist")
        try:
            return Reviewer(service, data_dir, requirement).checklist(shot, board)
        except ClientGone:
            raise
        except Exception:  # noqa: BLE001
            return []


# --- the model's three jobs ---------------------------------------------- #

def percent_met(results: list[dict[str, Any]]) -> int:
    """Share of checks met, 0–100. No checks answered is 0, never a pass."""
    if not results:
        return 0
    return round(100 * sum(1 for r in results if r.get("met")) / len(results))


def _brief(shot: dict[str, Any], board: dict[str, Any]) -> str:
    wanted = set(shot.get("characterIds") or [])
    cast = [f"- {c.get('name') or 'unnamed'}: {(c.get('description') or '').strip()}"
            for c in board.get("characters") or [] if c.get("id") in wanted]
    lines = [f"Scene description (applies to every shot): {board.get('sceneDescription') or '(none)'}",
             f"Render style (applies to every shot): {board.get('renderStyle') or '(none)'}"]
    if cast:
        lines.append("Cast in this shot:\n" + "\n".join(cast))
    if (shot.get("dialogue") or "").strip():
        lines.append(f"Dialogue (audio, not visible): {shot['dialogue'].strip()}")
    return "\n".join(lines)


def _json(reply: str) -> dict[str, Any]:
    doc = _first_json_object(reply or "")
    return doc if isinstance(doc, dict) else {}


CHECKLIST_SYSTEM = """\
You turn what a user wants a video clip to show into a checklist for judging \
the rendered clip from still frames taken about a second apart. Reply with \
ONLY JSON: {"checks": ["...", "..."]}.
- USER REQUEST is the source of truth. When it describes what the clip should \
show, write one check per concrete statement in it (split compound sentences), \
in the order things happen. Where the shot prompt disagrees with it, the \
request wins. Use the shot prompt only to fill in what the request leaves \
open, and only if there is room.
- If the request is empty or vague ("still not right", "fix it"), derive the \
checks from the shot prompt instead.
- The request may also say how to run the loop (scene numbers, attempts) — \
ignore that part.
- At most %d checks, each answerable yes/no from stills: who or what is in \
frame, where (frame left/right, foreground/background, in front of or behind \
what), facing, pose, framing, setting, lighting, props, camera position and \
which way it has moved between stills.
- Pin anything that happens over time to a moment: "at the start", "while the \
camera is behind him", "at the end". "Falls asleep while hidden" becomes: \
before the block he is working, during it he is out of view, after it he is \
already asleep.
- Never include dialogue, sound, music or fine motion between frames.
- Phrase each as a statement that is true when the clip is right.""" % MAX_CHECKS


def _clean_checks(raw: Any) -> list[str]:
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        text = (item.get("check") if isinstance(item, dict) else item)
        if isinstance(text, str) and text.strip() and text.strip() not in out:
            out.append(text.strip()[:240])
    return out[:MAX_CHECKS]


JUDGE_SYSTEM = """\
You review a rendered video clip, from stills taken at even intervals, against \
a checklist. Reply with ONLY JSON: \
{"results": [{"check": "<the check, verbatim>", "met": true|false, "evidence": "<what you \
see, with the frame number>"}], "overall": "<one sentence>"}.
- Judge only what the stills show. If you cannot tell, answer false and say so.
- Answer every check, in order. Be strict: partly right is false. A check about \
where something is must match the frame as the viewer sees it (left/right, \
foreground/background), not just that it is present.
- For something that happens over time, compare stills in order: name the \
still where it is before and the one where it is after. If the stills do not \
bracket the moment, answer false.
- Attached images are the stills first (labelled with their time), then cast \
portraits for who should look like whom."""


REVISE_SYSTEM = """\
You rewrite ONE shot's video prompt so the next render fixes what the last one \
got wrong. Reply with ONLY JSON: {"prompt": "<the complete revised prompt>"}.
- Keep the prompt's existing structure, voice and length; change only what the \
unmet checks need. Do not touch what already works.
- Do not repeat a change an earlier attempt already made without success — try \
a different, more explicit wording (frame-relative placement, an ordered \
action, what fills the frame).
- The cast, dialogue, scene description and render style are handled elsewhere; \
write only this shot's prompt.

""" + H3_VISUAL_RULES


class Reviewer:
    """The model-backed half of a refine run. Replaceable in tests."""

    def __init__(self, service: LLMService, data_dir: Path, requirement: str = ""):
        self.service = service
        self.data_dir = data_dir
        self.requirement = requirement

    # -- 1 -------------------------------------------------------------- #

    def checklist(self, shot: dict[str, Any], board: dict[str, Any]) -> list[str]:
        user = (f"USER REQUEST:\n{self.requirement or '(none — the clip should show the shot prompt)'}"
                f"\n\nSHOT PROMPT (secondary — fills gaps only):\n{shot.get('prompt') or ''}"
                f"\n\n{_brief(shot, board)}")
        for _ in range(2):
            checks = _clean_checks(_json(self.service.complete(
                CHECKLIST_SYSTEM, user, timeout=MODEL_TIMEOUT)).get("checks"))
            if checks:
                return checks
        raise RuntimeError("The model did not produce a usable checklist.")

    # -- 2 -------------------------------------------------------------- #

    def judge(self, shot: dict[str, Any], board: dict[str, Any], checks: list[str],
              clip: Path, seconds: float, workdir: Path) -> dict[str, Any]:
        stills = clip_stills(clip, seconds, workdir, "review",
                             count=still_count(seconds), edge=JUDGE_STILL_EDGE)
        if not stills:
            raise RuntimeError("Could not take stills from the drafted clip (is ffmpeg installed?).")
        portraits, portrait_labels = _chat_reference_images(
            {"shots": [shot], "characters": board.get("characters") or []},
            shot.get("id"), self.data_dir)
        portraits = [p for p, label in zip(portraits, portrait_labels)
                     if label.startswith("Character portrait")][:JUDGE_MAX_PORTRAITS]
        labels = [f"Image {i}: still {i} of {len(stills)} at {t:.1f}s of {seconds:.1f}s"
                  for i, (_, t) in enumerate(stills, 1)]
        labels += [f"Image {len(stills) + i}: cast portrait"
                   for i in range(1, len(portraits) + 1)]
        user = ("CHECKLIST:\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(checks, 1))
                + f"\n\nSHOT PROMPT (for context):\n{shot.get('prompt') or ''}"
                + f"\n\n{_brief(shot, board)}\n\nATTACHED IMAGES:\n" + "\n".join(labels))
        images = [p for p, _ in stills] + portraits
        doc: dict[str, Any] = {}
        for _ in range(2):
            doc = _json(self.service.complete_with_media(
                JUDGE_SYSTEM, user, images=images, timeout=MODEL_TIMEOUT))
            if isinstance(doc.get("results"), list) and doc["results"]:
                break
        else:
            raise RuntimeError("The model's review was unreadable — it may not be vision-capable.")
        # Align to the checklist by position, so a model that paraphrased a
        # check, or skipped one, cannot change what is being asked.
        answered = [r for r in doc["results"] if isinstance(r, dict)]
        results = []
        for i, check in enumerate(checks):
            r = answered[i] if i < len(answered) else {}
            results.append({"check": check, "met": r.get("met") is True,
                            "evidence": str(r.get("evidence") or "")[:300]})
        return {"results": results, "overall": str(doc.get("overall") or "")[:300],
                "score": percent_met(results)}

    # -- 3 -------------------------------------------------------------- #

    def revise(self, shot: dict[str, Any], board: dict[str, Any], prompt: str,
               results: list[dict[str, Any]], history: list[dict[str, Any]]) -> str:
        tried = "\n\n".join(
            f"Attempt {a['n']} ({a['score']}% met) prompt:\n{a['prompt']}\n"
            f"Unmet: " + "; ".join(r["check"] for r in a["results"] if not r["met"])
            for a in history)
        unmet = "\n".join(f"- {r['check']}  (seen: {r['evidence'] or 'no evidence given'})"
                          for r in results if not r["met"])
        user = (f"CURRENT PROMPT:\n{prompt}\n\nUNMET CHECKS:\n{unmet}\n\n"
                f"{_brief(shot, board)}\n\nUSER REQUEST:\n{self.requirement or '(none)'}"
                + (f"\n\nEARLIER ATTEMPTS:\n{tried}" if tried else ""))
        reply = self.service.complete(REVISE_SYSTEM, user, timeout=MODEL_TIMEOUT)
        revised = _json(reply).get("prompt")
        return revised.strip() if isinstance(revised, str) else ""


def clip_seconds(clip: Path, fallback: float) -> float:
    """The clip's real length: a draft may not run the shot's nominal frames.

    The *video* stream's duration, not the container's: a render's audio runs
    a hair past its last frame, and reviewing up to that point finds no frame.
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        for entry in ("stream=duration", "format=duration"):
            try:
                out = subprocess.run(
                    [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", entry,
                     "-of", "default=nw=1:nk=1", str(clip)],
                    capture_output=True, text=True, timeout=15, check=True).stdout.strip()
                if float(out) > 0:
                    return float(out)
            except Exception:  # noqa: BLE001 — try the next source, then the shot's own
                continue
    return fallback
