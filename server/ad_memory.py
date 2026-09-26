"""Rolling conversation memory for the Storyboard AD, one per board.

The browser keeps the chat transcript (localStorage, up to 60 turns) and sends
it with every message. Sending all of it verbatim grows the prompt with every
turn; sending only the last few (as before) forgets decisions made earlier in
the same session. This keeps the recent turns verbatim and folds everything
older into a short running summary, written by the same model, that lives
beside the board as ``ad-memory.json``.

The summary is incremental and cached: it remembers a fingerprint of the last
turn it absorbed, finds that turn again in the next request's history, and
only summarises what has rolled out of the verbatim window since. Turns are
folded in batches, so the extra model call happens once every BATCH turns
rather than on every message. A history that no longer contains the
remembered turn (the chat was cleared) starts the memory over.

It is a sidecar file rather than a board field for the same reason the
transcript lives in the browser: it is a scratchpad for talking to the AD,
not authored storyboard content.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

MEMORY_FILE = "ad-memory.json"
KEEP = 8          # most recent turns always sent verbatim
BATCH = 8         # older turns are folded into the summary this many at a time
TURN_CHARS = 2000  # a single turn's cap, verbatim or when summarised
MEMORY_MAX_TOKENS = 400

MEMORY_SYSTEM_PROMPT = """\
You keep the running memory of a conversation between a storyboard author and \
their assistant director (the AD). Merge the existing memory with the new \
turns into at most 150 words of terse bullet points, most important first:
- decisions made and the author's stated goals, preferences and corrections
- changes the AD proposed and whether the author accepted or rejected them
- problems found in specific shots (by number or title) and what fixed them
- open questions or next steps
Drop greetings, restated shot text, and anything superseded by a later turn. \
Write only the bullets.
"""


def _fingerprint(turn: dict[str, str]) -> str:
    raw = f"{turn.get('role')}\n{turn.get('content', '')[:TURN_CHARS]}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, doc: dict[str, Any]) -> None:
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # memory is an optimisation; the chat still works without it


def _summarise(service, summary: str, turns: list[dict[str, str]]) -> str:
    transcript = "\n".join(
        f"{'AUTHOR' if t['role'] == 'user' else 'AD'}: {t['content'][:TURN_CHARS]}"
        for t in turns
    )
    user = (
        ("EXISTING MEMORY:\n" + summary + "\n\n" if summary else "")
        + "NEW TURNS:\n" + transcript
    )
    out = service.complete(MEMORY_SYSTEM_PROMPT, user, timeout=180.0,
                           max_tokens=MEMORY_MAX_TOKENS)
    out = (out or "").strip()
    if "</think>" in out:
        out = out.rsplit("</think>", 1)[1].strip()
    return out


def condense(
    service, history: list[dict[str, str]], memory_path: Path | None,
) -> tuple[str, list[dict[str, str]]]:
    """Return ``(summary, verbatim_turns)`` for *history*.

    *history* is the cleaned transcript, oldest first, not including the
    message being sent now. Without a *memory_path* nothing is summarised
    and the last KEEP + BATCH turns are returned verbatim.
    """
    turns = [{"role": t["role"], "content": t["content"][:TURN_CHARS]} for t in history]
    if memory_path is None:
        return "", turns[-(KEEP + BATCH):]
    older, recent = turns[:-KEEP] if len(turns) > KEEP else [], turns[-KEEP:]

    memory = _load(memory_path)
    summary = str(memory.get("summary") or "")
    last = memory.get("last")
    start = 0
    if last:
        prints = [_fingerprint(t) for t in older]
        # Search from the end: a short turn ("yes") can repeat, and the most
        # recent occurrence is the one the memory absorbed.
        found = next((i for i in range(len(prints) - 1, -1, -1) if prints[i] == last), None)
        if found is not None:
            start = found + 1
        elif not any(_fingerprint(t) == last for t in recent):
            summary, start = "", 0  # the chat was cleared or replaced
        else:
            start = len(older)  # absorbed turn is still in the recent window
    pending = older[start:]

    if len(pending) >= BATCH:
        try:
            new_summary = _summarise(service, summary, pending)
        except Exception:  # noqa: BLE001 — fall back to sending them verbatim
            new_summary = ""
        if new_summary:
            summary, pending = new_summary, []
            _save(memory_path, {"summary": summary, "last": _fingerprint(older[-1])})
    if not turns and memory:
        _save(memory_path, {})  # a fresh chat forgets the old memory
        summary = ""
    return summary, pending + recent
