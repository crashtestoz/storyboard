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
# "with 5 attempts", "in 3 tries": the count without "up to" in front. Any
# word starting "att" counts, since "attepmts" is a typo people really make.
_COUNT_WITH = re.compile(
    r"\b(?:with|in|using|for|over)\s+(\d{1,2})\s*(?:att\w*|tries|try|rounds?|iterations?|goes|passes)\b", re.I)
# /refine anywhere in the message, not only first: people describe the
# problem and then say "/refine". Not part of a path like a/refine.
_REFINE_CMD = re.compile(r"(?<![\w/])/refine\b:?", re.I)
_SHOT = re.compile(r"\b(?:scene|shot)s?\s*#?\s*(\d{1,3})\b", re.I)
_RENDERY = re.compile(r"\b(draft|render|clip|preview|video|take)\b", re.I)
_REVIEWY = re.compile(r"\b(review|check|compare|look at|watch|inspect|judge|evaluate|critique)\b", re.I)
_ADJUSTY = re.compile(r"\b(adjust|fix|tweak|change|revise|rewrite|improve|retry|iterate|refine|"
                      r"correct|amend|re-?do)\b", re.I)
_LOOPY = re.compile(r"\b(keep trying|try again|loop|repeat|iterate|over and over|until|"
                    r"auto[- ]?refine|refine)\b", re.I)


def wants_refine(message: str) -> bool:
    """Does *message* ask for the draft → review → adjust loop?

    Either the explicit ``/refine`` (or "auto-refine"), anywhere in the
    message, or the three steps named together: a draft/render, a review, and
    an adjustment. A cheap false positive costs one click on Discard, so this
    leans towards matching.
    """
    text = message or ""
    if _REFINE_CMD.search(text) or re.search(r"\bauto[- ]?refine\b", text, re.I):
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
    elif (w := _COUNT_WITH.search(message)):
        attempts = int(w.group(1))
    attempts = max(1, min(attempts, MAX_ATTEMPTS))
    # What the clip should show, without the instructions for running the loop.
    requirement = _REFINE_CMD.sub("", message)
    requirement = _COUNT.sub("", requirement)
    requirement = _COUNT_WITH.sub("", requirement)
    requirement = re.sub(r"^[\s,:.;\-]*(?:(?:with|for|in|on|about|regarding)\s+)?"
                         r"(?:scene|shot)s?\s*#?\d{1,3}[\s,:.;\-]*", "", requirement, flags=re.I)
    requirement = re.sub(r"^[\s,:.;\-]+", "", requirement)
    requirement = re.sub(r"\s{2,}", " ", requirement).strip()
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
                  "number of attempts, before you start; the run is only as good as this list. "
                  "Checks marked ! are critical: I keep going until every one is met, "
                  "however well the rest do.")
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


# --- critical checks ------------------------------------------------------ #
# A checklist line that starts with "!" is critical: the shot is wrong without
# it. That is how it is typed on the Start card and how the model's checklist
# is handed over; it is stripped before the checks reach the judge.

_CRITICAL_LEAD = re.compile(r"^\s*(?:[!★]\s*)+")


def split_checks(raw: Any) -> tuple[list[str], list[bool]]:
    """Checklist lines as typed → (check texts, critical flags), same order."""
    texts: list[str] = []
    flags: list[bool] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, str):
            continue
        lead = _CRITICAL_LEAD.match(item)
        text = item[lead.end():].strip() if lead else item.strip()
        if text:
            texts.append(text[:240])
            flags.append(bool(lead))
    return texts[:MAX_CHECKS], flags[:MAX_CHECKS]


def critical_counts(results: list[dict[str, Any]]) -> tuple[int, int]:
    """(critical checks met, critical checks in total)."""
    crit = [r for r in results if r.get("critical")]
    return sum(1 for r in crit if r.get("met")), len(crit)


def is_pass(results: list[dict[str, Any]], score: int, pass_percent: int) -> bool:
    """Is this attempt good enough to stop on?

    When the checklist has critical checks, they alone decide: every one met
    is a pass, however many of the rest are not, and one missed is never a
    pass, however high the score. Without any, the share of checks met has to
    reach *pass_percent*.
    """
    met, total = critical_counts(results)
    if total:
        return met == total
    return score >= pass_percent


def rank(attempt: dict[str, Any]) -> tuple[int, int, int]:
    """Sort key for the best attempt: criticals first, then the score."""
    met, total = critical_counts(attempt.get("results") or [])
    return (int(met == total), met, attempt.get("score") or 0)


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
ONLY JSON: {"checks": [{"check": "...", "critical": true|false}]}.
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
- Phrase each as a statement that is true when the clip is right.
- Set "critical": true on the checks the shot cannot be accepted without: what \
the user stressed or complained about, and the continuity or story point the \
shot exists for. Usually one to three. Everything else is false. Never mark \
every check critical.""" % MAX_CHECKS


def _clean_checks(raw: Any) -> list[str]:
    """The model's checks as lines, critical ones starting "! " (see split_checks)."""
    out: list[str] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        crit = isinstance(item, dict) and item.get("critical") is True
        text = (item.get("check") if isinstance(item, dict) else item)
        if not isinstance(text, str):
            continue
        texts, flags = split_checks([text])
        if texts and texts[0] not in seen:
            seen.add(texts[0])
            out.append(("! " if crit or flags[0] else "") + texts[0])
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
- CRITICAL unmet checks come first: the shot is unusable until they are met. \
Fix them before anything else, and never trade a met critical check away for \
another.
- ALREADY MET lists what the last clip got right. Keep every sentence and \
phrase that delivers one of those exactly as written, word for word. Edit only \
the sentences that carry an unmet check, and leave every other sentence \
untouched. If a met check and an unmet one share a sentence, change only the \
part that serves the unmet one.
- Do not repeat a change an earlier attempt already made without success — try \
a different, more explicit wording (frame-relative placement, an ordered \
action, what fills the frame).
- The cast, dialogue, scene description and render style are handled elsewhere; \
write only this shot's prompt.

""" + H3_VISUAL_RULES


ESCALATE_NOTE = """

The last rewrite came back unchanged, but the unmet checks above are still \
unmet, so you MUST change the prompt now. Rewrite the sentence that carries the \
critical check: say it plainly and literally, and describe what is on screen \
instead (what fills that part of the frame), not what is absent. Leave every \
other sentence exactly as it is."""


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
        try:
            return self._judge_stills(shot, board, checks, stills, seconds)
        finally:
            # The stills are evidence for this one review, not material to keep:
            # left in the project they pile up eight an attempt and turn up
            # wherever the project's images are listed. (The cast portraits sent
            # alongside are the project's own files and are never touched.)
            for path, _ in stills:
                path.unlink(missing_ok=True)

    def _judge_stills(self, shot: dict[str, Any], board: dict[str, Any], checks: list[str],
                      stills: list[tuple[Path, float]], seconds: float) -> dict[str, Any]:
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
               results: list[dict[str, Any]], history: list[dict[str, Any]],
               escalate: bool = False) -> str:
        tried = "\n\n".join(
            f"Attempt {a['n']} ({a['score']}% met) prompt:\n{a['prompt']}\n"
            f"Unmet: " + "; ".join(r["check"] for r in a["results"] if not r["met"])
            for a in history)

        def lines(rows):
            return "\n".join(f"- {r['check']}  (seen: {r['evidence'] or 'no evidence given'})"
                             for r in rows)
        critical = [r for r in results if not r["met"] and r.get("critical")]
        other = [r for r in results if not r["met"] and not r.get("critical")]
        unmet = ((f"CRITICAL (must be fixed):\n{lines(critical)}\n\n" if critical else "")
                 + (f"OTHER:\n{lines(other)}" if other else "")).strip()
        met = "\n".join(f"- {r['check']}" for r in results if r["met"])
        user = (f"CURRENT PROMPT:\n{prompt}\n\nUNMET CHECKS:\n{unmet}\n\n"
                + (f"ALREADY MET (keep these true):\n{met}\n\n" if met else "")
                + f"{_brief(shot, board)}\n\nUSER REQUEST:\n{self.requirement or '(none)'}"
                + (f"\n\nEARLIER ATTEMPTS:\n{tried}" if tried else "")
                + (ESCALATE_NOTE if escalate else ""))
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
