"""Compact context and reviewable edit proposals for the assistant blade."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

from .llm import LLMService


def _load_mcp_tools() -> list[dict[str, str]]:
    """The MCP tool catalogue, read straight from mcp/server.py's own TOOLS.

    Not hand-copied here, because a second copy is a copy that goes stale the
    moment one of them changes and the other does not — the exact failure
    mode a native-dialogue shot hit before sbv_dub_shot itself learned to
    refuse: the chat blade would otherwise keep telling users it could do
    something the tool had since stopped doing, or vice versa. mcp/server.py
    has no side effects at import time (``main()`` is guarded by
    ``__name__ == "__main__"``), so loading it here just reads data.
    """
    path = Path(__file__).resolve().parent.parent / "mcp" / "server.py"
    try:
        spec = importlib.util.spec_from_file_location("_storyboard_mcp_catalogue", path)
        if spec is None or spec.loader is None:
            return []
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return [{"name": t["name"], "description": t["description"]} for t in module.TOOLS]
    except Exception:  # noqa: BLE001 — the chat still works without this section
        return []


def _mcp_reference() -> str:
    tools = _load_mcp_tools()
    if not tools:
        return ""
    lines = [f"- {t['name']}: {t['description']}" for t in tools]
    return (
        "MCP TOOLS (mcp/server.py — an external MCP client such as Claude "
        "Desktop or Claude Code can call these directly; you cannot):\n"
        + "\n".join(lines)
    )


# Hand-written and kept deliberately compact: this is what an assistant needs
# to advise on board content and point at the right tool, not the full
# README (installation, engine config, dev rationale) — none of that helps
# answer "will this line fit" or "why is Generate disabled here". It lives in
# the *system* prompt rather than being re-sent inside the per-turn user
# message precisely because the system prompt is the one part of this
# request that is identical on every turn and every board, so a backend
# that reuses a cached prompt prefix (llama.cpp, vLLM, and similar) pays for
# it once per conversation rather than once per message.
STORYBOARD_KNOWLEDGE = """\
HOW STORYBOARD WORKS (reference — you cannot trigger any of this yourself; see Rules):

Board = sceneDescription (project-wide look, auto-prepended to every shot — \
never repeat it in a shot prompt) + soundscape (project background bed; can \
render into every shot, or be held out for a later mix) + characters (name, \
description, optional portrait + voice clip) + shots.

Shot prompt shape: camera move named first, then subject/action, then \
environment/light with concrete material detail, then sound as a trailing \
clause — MiniMax H3 denoises picture and sound together.

Model auto-selection: a shot with a Start or End frame reference uses FL2VA \
(hard first/last-frame anchors; cast/style/shot references are NOT sent on \
that path). No anchors -> Ref2VA, which carries cast portraits, style refs \
and shot refs (limits: 9 images, 3 audio, 12 total).

Dialogue has two independent mechanisms, chosen per shot via dialogueSource:
- "recording" (default): a separate TTS take, cloned from the speaking \
character's reference voice clip. Generate / sbv_dub_shot can synthesise it \
any time, before or after rendering; dubMode "mix" layers it over the clip's \
own audio, "replace" replaces it. This call is also the fit check: its \
response warns if the spoken line runs longer than the shot's frames (at the \
fixed 24fps) or, once rendered, the actual clip.
- "native": only valid on a Ref2VA shot whose speaker has a reference voice \
clip. H3 generates that shot's speech itself, lip-synced, during rendering, \
from the voice already sent in as an audio reference. There is no separate \
take: Generate is disabled in the UI and sbv_dub_shot refuses over MCP. The \
only way to get this shot's audio is to render it (sbv_start_render / the \
Render button).
dialogueStyle is delivery direction ("tired", "quiet", "breathy") sent to \
the engine — never spoken aloud itself.

Render rate is fixed at 24fps everywhere: a shot's length in seconds is \
frames / 24.

Render/assemble: sbv_start_render queues shots and renders one at a time; \
sbv_status polls progress; sbv_stop_render cancels. A shot goes "stale" once \
its saved fields diverge from what its last render used. sbv_assemble joins \
rendered clips (the dubbed version wins while current) into final.mp4, in \
shot order.

Draft mode renders small and fast (capped resolution, 4 steps, no generated \
audio) for blocking iteration before a full-quality pass.
"""

CHAT_SYSTEM_PROMPT = """\
You are the Storyboard AD, the user's assistant director. Help the user review, plan, and edit
the storyboard currently open in the app. Be concise, concrete, and candid
about continuity, camera direction, pacing, visual consistency, and sound.

You have the authoring portion of the Storyboard MCP surface available as
reviewable proposals: set project scene/sound fields, add or update cast, and
add or update shots. The app, not you, assigns IDs and saves changes. Never
claim a change is already applied. If the user asks for an edit, include it as
an action and say that it is ready to apply.

Return ONLY one JSON object with this shape:
{"message":"your response","actions":[]}

Allowed actions:
{"tool":"set_board_fields","fields":{"sceneDescription":"...","soundscape":"..."}}
{"tool":"add_character","character":{"name":"...","description":"..."}}
{"tool":"update_character","characterId":"existing id","fields":{"name":"...","description":"..."}}
{"tool":"add_shot","shot":{"title":"...","prompt":"...","soundNote":"...","dialogue":"...","dialogueStyle":"...","characterIds":["existing id"],"frames":124,"steps":8,"seed":0}}
{"tool":"update_shot","shotId":"existing id","fields":{"title":"...","prompt":"...","soundNote":"...","dialogue":"...","dialogueStyle":"...","characterIds":["existing id"],"frames":124,"steps":8,"seed":0}}

Rules:
- Use only the allowed tools and fields. Never invent IDs.
- Prefer updating an existing shot when it represents the same story beat.
- Keep video prompts production-ready: camera first, then subject/action,
  environment/light, and finally sound. Do not duplicate the shared scene.
- Preserve intentional details unless the user asks to replace them.
- For a request to build a board, propose a coherent sequence of add_shot
  actions. Keep a single response to 24 actions or fewer.
- For discussion, review, or questions, return an empty actions array.
- You cannot render video, synthesise or dub audio, start/stop a render, or
  trigger any other side-effecting action — only the field edits above. If
  asked to do one, say so and name the actual way: the matching button here
  in the app (Render, or Generate on that shot's Dialogue tab), or the
  matching tool in the MCP TOOLS list below for an MCP client (Claude
  Desktop, Claude Code, etc.) to call. Never say only that you can't — use
  the HOW STORYBOARD WORKS and MCP TOOLS reference below to name the real
  path, including any precondition it has (e.g. native dialogue has none).
- Do not wrap the JSON in markdown fences or add text outside it.
"""

CHAT_SYSTEM_PROMPT = CHAT_SYSTEM_PROMPT + "\n" + STORYBOARD_KNOWLEDGE + "\n" + _mcp_reference()

BOARD_FIELDS = {"sceneDescription", "soundscape"}
CHARACTER_FIELDS = {"name", "description"}
SHOT_FIELDS = {
    "title", "prompt", "soundNote", "dialogue", "dialogueStyle",
    "characterIds", "frames", "steps", "seed",
}


def compact_board_context(
    board: dict[str, Any], selected_id: str | None = None
) -> dict[str, Any]:
    """Keep authoring data; omit render outputs, logs, URLs, and status."""
    defaults = board.get("defaults") or {}
    characters = [
        {
            "id": c.get("id"), "name": c.get("name") or "",
            "description": c.get("description") or "",
            "hasImage": bool(c.get("image")), "hasVoice": bool(c.get("voice")),
        }
        for c in (board.get("characters") or [])
    ]
    shots = []
    for index, shot in enumerate(board.get("shots") or [], 1):
        shots.append({
            "number": index, "id": shot.get("id"),
            "title": shot.get("title") or "", "prompt": shot.get("prompt") or "",
            "soundNote": shot.get("soundNote") or "",
            "dialogue": shot.get("dialogue") or "",
            "dialogueStyle": shot.get("dialogueStyle") or "",
            "characterIds": shot.get("characterIds") or [],
            "frames": shot.get("frames", defaults.get("frames", 124)),
            "steps": shot.get("steps", defaults.get("steps", 8)),
            "seed": shot.get("seed", 0),
            "hasStartFrame": bool(shot.get("startRef")),
            "hasEndFrame": bool(shot.get("endRef")),
            "referenceImageCount": len(shot.get("referenceImages") or []),
        })
    return {
        "name": board.get("name") or "Untitled storyboard",
        "sceneDescription": board.get("sceneDescription") or "",
        "soundscape": board.get("soundscape") or "",
        "format": {"resolution": defaults.get("resolution"),
                   "defaultFrames": defaults.get("frames"),
                   "defaultSteps": defaults.get("steps")},
        "characters": characters,
        "styleReferenceCount": len(board.get("styleRefs") or []),
        "selectedShotId": selected_id,
        "shots": shots,
    }


def _clean_fields(value: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out = {key: value[key] for key in allowed if key in value}
    for key in tuple(out):
        if key not in {"characterIds", "frames", "steps", "seed"} and not isinstance(out[key], str):
            out.pop(key)
    if "characterIds" in out:
        if isinstance(out["characterIds"], list):
            out["characterIds"] = [v for v in out["characterIds"] if isinstance(v, str)]
        else:
            out.pop("characterIds")
    for key in ("frames", "steps", "seed"):
        if key in out and (not isinstance(out[key], int) or isinstance(out[key], bool)):
            out.pop(key)
    return out


def validate_actions(actions: Any, board: dict[str, Any]) -> list[dict[str, Any]]:
    """Allow-list model proposals before they reach browser state."""
    if not isinstance(actions, list):
        return []
    shot_ids = {s.get("id") for s in board.get("shots") or []}
    character_ids = {c.get("id") for c in board.get("characters") or []}
    clean: list[dict[str, Any]] = []
    for raw in actions[:24]:
        if not isinstance(raw, dict):
            continue
        tool = raw.get("tool")
        if tool == "set_board_fields":
            fields = _clean_fields(raw.get("fields"), BOARD_FIELDS)
            if fields:
                clean.append({"tool": tool, "fields": fields})
        elif tool == "add_character":
            fields = _clean_fields(raw.get("character"), CHARACTER_FIELDS)
            if fields.get("name"):
                clean.append({"tool": tool, "character": fields})
        elif tool == "update_character" and raw.get("characterId") in character_ids:
            fields = _clean_fields(raw.get("fields"), CHARACTER_FIELDS)
            if fields:
                clean.append({"tool": tool, "characterId": raw["characterId"], "fields": fields})
        elif tool == "add_shot":
            fields = _clean_fields(raw.get("shot"), SHOT_FIELDS)
            if "characterIds" in fields:
                fields["characterIds"] = [v for v in fields["characterIds"] if v in character_ids]
            if fields:
                clean.append({"tool": tool, "shot": fields})
        elif tool == "update_shot" and raw.get("shotId") in shot_ids:
            fields = _clean_fields(raw.get("fields"), SHOT_FIELDS)
            if "characterIds" in fields:
                fields["characterIds"] = [v for v in fields["characterIds"] if v in character_ids]
            if fields:
                clean.append({"tool": tool, "shotId": raw["shotId"], "fields": fields})
    return clean


def _parse_reply(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return {"message": text or "The model returned an empty response.", "actions": []}
    if not isinstance(doc, dict):
        return {"message": text, "actions": []}
    return {"message": str(doc.get("message") or "").strip(), "actions": doc.get("actions")}


def chat(
    service: LLMService, board: dict[str, Any], message: str,
    history: list[dict[str, Any]] | None = None,
    selected_id: str | None = None,
) -> dict[str, Any]:
    message = (message or "").strip()
    if not message:
        raise ValueError("message is required")
    ok, why = service.health()
    if not ok:
        raise RuntimeError(why)
    recent = []
    for turn in (history or [])[-12:]:
        role = turn.get("role") if isinstance(turn, dict) else None
        content = turn.get("content") if isinstance(turn, dict) else None
        if role in ("user", "assistant") and isinstance(content, str):
            recent.append({"role": role, "content": content[:4000]})
    user = (
        "CURRENT STORYBOARD (compact JSON):\n"
        + json.dumps(compact_board_context(board, selected_id), ensure_ascii=False, separators=(",", ":"))
        + "\n\nRECENT CONVERSATION:\n"
        + json.dumps(recent, ensure_ascii=False, separators=(",", ":"))
        + "\n\nUSER:\n" + message
    )
    parsed = _parse_reply(service.complete(CHAT_SYSTEM_PROMPT, user, timeout=180.0))
    return {
        "message": parsed["message"] or "I prepared the requested storyboard changes.",
        "actions": validate_actions(parsed.get("actions"), board),
        "service": service.label, "model": service.model,
    }
