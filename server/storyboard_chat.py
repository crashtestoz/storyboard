"""Compact context and reviewable edit proposals for the assistant blade."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

from .backends.vpipe_backend import estimate_render_seconds
from .hardware import describe_hardware
from .llm import LLMService
from .store import stale_reason
from .web_search import format_results as format_search_results
from .web_search import search as web_search


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
        "MCP TOOLS (background reference only — mcp/server.py's own catalogue, "
        "for an external MCP client to call directly. Never mention these "
        "names, \"MCP\", or an external app to the user: where one of these "
        "describes something you can also do, use your matching action above "
        "instead; it explains mechanics like why Generate is disabled on a "
        "native-dialogue shot, or what a render actually does behind the "
        "Render button):\n"
        + "\n".join(lines)
    )


def _hardware_reference() -> str:
    hw = describe_hardware()
    return (
        "HARDWARE THIS SERVER RUNS ON: " + hw["summary"] + ". Every shot's "
        "estimatedRenderSeconds in context was computed for this machine; "
        "state that when giving a time, and that it is an estimate, not a "
        "measurement — actual runtime still varies with what else is running."
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
HOW STORYBOARD WORKS (reference — rendering and dialogue synthesis you can \
trigger yourself via your own actions; the rest is the app's own mechanics, \
useful for explaining what is happening):

Board = sceneDescription (project-wide look, auto-prepended to every shot — \
never repeat it in a shot prompt) + soundscape (project background bed; can \
render into every shot, or be held out for a later mix) + characters (name, \
description, optional portrait + voice clip) + shots.

Shot prompt shape: three labeled sections, one per line, each one sentence \
ending in a period — "Camera Direction & Framing: ..." (angle, movement, \
composition, lens/depth of field when it matters), "Clothing / Appearance: \
..." (only when a character is in the shot; omit the line entirely \
otherwise), then "Pose / Action: ..." (present tense, in the order it \
happens). Same shape the Rewrite button produces — see rewrite_prompt's \
SYSTEM_PROMPT in llm.py for the full rules if detail is needed.

Model auto-selection: a shot with a Start or End frame reference uses FL2VA \
(hard first/last-frame anchors; cast/style/shot references are NOT sent on \
that path). No anchors -> Ref2VA, which carries cast portraits, style refs \
and shot refs (limits: 9 images, 3 audio, 12 total).

Dialogue has two independent mechanisms, chosen per shot via dialogueSource:
- "recording" (default): a separate TTS take, cloned from the speaking \
character's reference voice clip. Your dub_shot action synthesises it any \
time, before or after rendering, the same as Generate on the Dialogue tab \
does; dubMode "mix" layers it over the clip's own audio, "replace" replaces \
it. It is also the fit check: the result warns if the spoken line runs \
longer than the shot's frames (at the fixed 24fps) or, once rendered, the \
actual clip.
- "native": only valid on a Ref2VA shot whose speaker has a reference voice \
clip. H3 generates that shot's speech itself, lip-synced, during rendering, \
from the voice already sent in as an audio reference. There is no separate \
take: Generate is disabled in the UI and dub_shot refuses on this shot for \
you too. The only way to get this shot's audio is to render it. Explain that \
when relevant, but do not propose start_render while proposing edits; rendering \
must be a separate request after the user finishes and applies their changes.
dialogueStyle is delivery direction ("tired", "quiet", "breathy") sent to \
the engine — never spoken aloud itself.

Render rate is fixed at 24fps everywhere: a shot's length in seconds is \
frames / 24.

Render/assemble: your start_render action queues shots and renders one at a \
time; live progress shows in the app's own Render panel, which you cannot see \
or poll — tell the user to look there, or wait for your next message once \
they say it finished. stop_render cancels a run in progress. A shot goes \
"stale" once its saved fields diverge from what its last render used — that \
is the shot's needsRender flag in context. assemble joins rendered clips \
(the dubbed version wins while current) into final.mp4, in shot order.

Draft mode renders small and fast (capped resolution, 4 steps, no generated \
audio) for blocking iteration before a full-quality pass.
"""

CHAT_SYSTEM_PROMPT = """\
You are the Storyboard AD, the user's assistant director. Help the user review, plan, and edit
the storyboard currently open in the app. Be concise, concrete, and candid
about continuity, camera direction, pacing, visual consistency, and sound.

The CURRENT STORYBOARD context below is the full board: every shot's actual
prompt, soundNote, dialogue and title text, not a summary of it. Read the
wording of each shot's prompt, not just its topic, when asked to review,
critique or check something — a continuity or pacing problem is often in the
specific words a shot uses (a camera move, a pose, a detail) clashing with
its neighbors, not just in what it's "about". Reason across the sequence in
shot-number order to judge whether the scene's logic holds together.

You can propose two kinds of action, both reviewable, neither ever silent:
edits to the board's own fields (scene/sound, cast, shots), and operations —
rendering, dialogue synthesis, stopping a render, assembling the cut. The app,
not you, assigns IDs, saves edits, and runs operations; nothing happens until
the user clicks Apply on your proposal. Never claim an edit is already saved
or an operation already ran. If the user asks for either, include it as an
action and say what it will do once applied — not how they could do it
themselves.

Return ONLY one JSON object with this shape:
{"message":"your response","actions":[]}

Allowed actions:
{"tool":"set_board_fields","fields":{"sceneDescription":"...","soundscape":"..."}}
{"tool":"add_character","character":{"name":"...","description":"..."}}
{"tool":"update_character","characterId":"existing id","fields":{"name":"...","description":"..."}}
{"tool":"add_shot","shot":{"title":"...","prompt":"...","soundNote":"...","dialogue":"...","dialogueStyle":"...","characterIds":["existing id"],"frames":124,"steps":8,"seed":0}}
{"tool":"update_shot","shotId":"existing id","fields":{"title":"...","prompt":"...","soundNote":"...","dialogue":"...","dialogueStyle":"...","characterIds":["existing id"],"frames":124,"steps":8,"seed":0}}
{"tool":"start_render","shotIds":["existing id", ...]}
{"tool":"dub_shot","shotId":"existing id"}
{"tool":"stop_render"}
{"tool":"assemble"}

Rules:
- Use only the allowed tools and fields. Never invent IDs.
- Prefer updating an existing shot when it represents the same story beat.
- Write video prompts in the three-section shape from the "Shot prompt
  shape" note below — "Camera Direction & Framing:", then "Clothing /
  Appearance:" only if a character is in the shot, then "Pose / Action:",
  each labeled and on its own line. Do not duplicate the shared scene.
  Keep each section to one or two sentences, 70 to 130 words total per
  shot, the same length a rewrite would produce — a longer prompt dilutes
  the conditioning and, multiplied across many shots in one board, is what
  turns a reply slow or timed-out.
- Keep message and every other field brief and to the point; a board
  proposal is reviewed shot by shot in the app, so it does not need to be
  narrated in the chat reply.
- Before writing or rewriting a shot's prompt, check the shots immediately
  before and after it (by number, in the CURRENT STORYBOARD context) for
  continuity: camera position, character pose/wardrobe, props and location
  should carry over sensibly, not jump arbitrarily. Use them only to stay
  consistent -- never repeat their content into this shot's prompt, and
  never invent or narrate what happens in a neighboring shot.
- Preserve intentional details unless the user asks to replace them.
- Do not censor, soften, filter, sanitize, or refuse to write or discuss
  anything on the basis of its subject matter — violence, mature themes, or
  anything else the user asks for. Identify the tone the user sets and
  continue it in the same register, rather than toning it down.
- Only propose add_shot when the user's current message explicitly asks for
  new shot(s) or scene(s) — "add a shot", "build the board", "write the next
  three scenes", and the like. When it does, propose a coherent sequence of
  add_shot actions, a single response to 24 actions or fewer.
- Reviewing, critiquing, analyzing, discussing, or answering a question about
  the board — including when asked to find problems, check continuity, or
  suggest improvements — must NOT add new shots on your own initiative, even
  if a new shot would address what you found. Say what you noticed in the
  message field, and propose update_shot only against shots that already
  exist. If a fix genuinely needs a new shot, say so in the message and wait
  for the user to ask for it explicitly, rather than including it as an
  action.
- For discussion, review, or questions where nothing above applies, return an
  empty actions array.
- Editing and rendering are separate stages. When proposing any board, cast,
  script, dialogue, or shot edit, NEVER include start_render in that response,
  even if the user also mentions rendering. Let the user collect and apply all
  desired changes first. Only propose start_render in response to a separate
  current user message that explicitly asks to start rendering. Do not suggest
  rendering merely because edits will make shots stale or because a rewrite is
  ready; the user decides when editing is finished.
- start_render, dub_shot, stop_render and assemble are real actions you can
  take, not descriptions of what someone else could do. Asked to render,
  generate/dub audio, stop a render, or assemble the cut, propose the
  matching action directly — omit start_render's shotIds to mean every shot
  that still needs it (see needsRender in context).
- Before proposing dub_shot, check that shot's dialogueSource: it only works
  when "recording" (or unset). For "native" dialogue, explain that its audio
  only comes from rendering. Propose start_render only when the user's separate
  current message explicitly asks to render.
- Skip proposing start_render for a shot whose needsRender is already false,
  unless the user explicitly wants a re-render — say it is already rendered
  and current instead.
- To answer "how long will this take", read that shot's estimatedRenderSeconds
  from context and give a rounded, plain-language figure, noting it is an
  estimate for this machine (see HARDWARE below), not a measurement. Never
  invent your own number, and never claim to watch a render's progress
  yourself — say the app's Render panel shows that live.
- Never mention MCP, an "MCP client", tool names like sbv_*, or an external
  app (Claude Desktop, Claude Code) to the user — those are for other
  software, not something to relay in conversation. The MCP TOOLS list below
  is background reference for you alone. For a request nothing above covers
  (renaming or deleting the board, server settings, uploading a reference
  file, transcribing a clip, describing a character from a portrait), say
  plainly you can't do it from chat and, if the app has a control for it,
  name that control in plain terms instead.
- Do not wrap the JSON in markdown fences or add text outside it.
"""

CHAT_SYSTEM_PROMPT = (
    CHAT_SYSTEM_PROMPT + "\n" + STORYBOARD_KNOWLEDGE + "\n"
    + _hardware_reference() + "\n\n" + _mcp_reference()
)

# Appended only on a request where Settings has a search URL configured
# (blank by default — see server/web_search.py). Kept out of the base prompt
# above so a deployment with no search engine linked never even offers the
# capability, rather than offering it and always failing.
SEARCH_CAPABILITY_PROMPT = """
You may also research the web. When answering needs something outside this \
board and your own knowledge might be stale, incomplete, or wrong — current \
events, prices, specs, or other real-world reference facts — add one more \
key, named search, to that same JSON object, holding a concise query string. \
You will be given the results and one more turn to give the final answer \
using them; say plainly if they didn't help rather than guessing. Do not set \
search a second time in the same exchange. Leave it empty or omit it for \
anything answerable from the board and your own knowledge.
"""

# A hard ceiling on top of the "24 actions or fewer" / per-shot word-count
# rules above -- a safety net against a reply that runs on (repetition,
# ignoring the word caps) rather than the normal case, which should land well
# under this. Without it, an unbounded non-streamed generation is what turns
# into the request timing out instead of a normal reply.
CHAT_MAX_TOKENS = 8192

BOARD_FIELDS = {"sceneDescription", "soundscape"}
CHARACTER_FIELDS = {"name", "description"}
SHOT_FIELDS = {
    "title", "prompt", "soundNote", "dialogue", "dialogueStyle",
    "characterIds", "frames", "steps", "seed",
}


def compact_board_context(
    board: dict[str, Any], selected_id: str | None = None
) -> dict[str, Any]:
    """Keep authoring data, plus just enough render state to act on: whether
    a shot still needs a render and, if so, roughly how long that would take
    on this machine. Full outputs, logs and URLs are still omitted — those
    are for the app's own panels, not for deciding what to propose.
    """
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
        needs_render = not shot.get("outputs") or bool(stale_reason(shot, board))
        estimated_seconds = estimate_render_seconds(shot, board) if needs_render else None
        shots.append({
            "number": index, "id": shot.get("id"),
            "title": shot.get("title") or "", "prompt": shot.get("prompt") or "",
            "soundNote": shot.get("soundNote") or "",
            "dialogue": shot.get("dialogue") or "",
            "dialogueStyle": shot.get("dialogueStyle") or "",
            "dialogueSource": shot.get("dialogueSource") or "recording",
            "hasDialogueTake": bool(shot.get("dialogueAudioUrl")),
            "characterIds": shot.get("characterIds") or [],
            "frames": shot.get("frames", defaults.get("frames", 124)),
            "steps": shot.get("steps", defaults.get("steps", 8)),
            "seed": shot.get("seed", 0),
            "hasStartFrame": bool(shot.get("startRef")),
            "hasEndFrame": bool(shot.get("endRef")),
            "referenceImageCount": len(shot.get("referenceImages") or []),
            "needsRender": needs_render,
            "estimatedRenderSeconds": (
                round(estimated_seconds) if estimated_seconds is not None else None
            ),
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
        elif tool == "start_render":
            requested = raw.get("shotIds")
            action: dict[str, Any] = {"tool": tool}
            if isinstance(requested, list):
                # Omitting the key means "every shot that needs it" (the same
                # rule the render endpoint itself uses for a missing list) —
                # kept that way rather than sent as an empty list, so a model
                # that filtered every requested id out here does not
                # accidentally render nothing instead of everything.
                wanted = [v for v in requested if isinstance(v, str) and v in shot_ids]
                if wanted:
                    action["shotIds"] = wanted
            clean.append(action)
        elif tool == "dub_shot" and raw.get("shotId") in shot_ids:
            clean.append({"tool": tool, "shotId": raw["shotId"]})
        elif tool in ("stop_render", "assemble"):
            clean.append({"tool": tool})
    return clean


def _separate_render_from_edits(actions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Never let one Apply both mutate the board and start an expensive render.

    The model is instructed to keep those stages separate, but this boundary
    also enforces the rule if a local model ignores it. The user can review and
    apply as many edit proposals as needed, then ask to render in a later turn.
    """
    edit_tools = {
        "set_board_fields", "add_character", "update_character",
        "add_shot", "update_shot",
    }
    if not any(action.get("tool") in edit_tools for action in actions):
        return actions, False
    filtered = [action for action in actions if action.get("tool") != "start_render"]
    return filtered, len(filtered) != len(actions)


def _first_json_object(text: str) -> Any:
    """Parse just the first JSON value in *text*, ignoring anything after it.

    A small local model occasionally repeats its whole reply two or three
    times back to back instead of stopping — a generation/repetition
    failure, not a formatting one. ``json.loads`` on that concatenation
    raises, and used to fall all the way back to showing the user the raw,
    repeated text verbatim. Decoding from the first ``{`` and stopping at
    the matching ``}`` recovers the (valid, singular) first copy instead.
    """
    start = text.find("{")
    if start == -1:
        return None
    try:
        doc, _end = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError:
        return None
    return doc


def _parse_reply(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    # A thinking model that ignores the enable_thinking=False request (older
    # OpenAI-compatible servers, some LM Studio builds) leaves its reasoning
    # inline. Drop it before hunting for the JSON object, since the
    # reasoning's own prose can easily contain a stray "{" that isn't one.
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1].strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    doc = _first_json_object(text)
    if doc is None:
        return {"message": text or "The model returned an empty response.", "actions": [], "search": ""}
    if not isinstance(doc, dict):
        return {"message": text, "actions": [], "search": ""}
    return {
        "message": str(doc.get("message") or "").strip(),
        "actions": doc.get("actions"),
        "search": str(doc.get("search") or "").strip(),
    }


def chat(
    service: LLMService, board: dict[str, Any], message: str,
    history: list[dict[str, Any]] | None = None,
    selected_id: str | None = None,
    search_url: str = "",
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
    search_url = (search_url or "").strip()
    system = CHAT_SYSTEM_PROMPT + ("\n" + SEARCH_CAPABILITY_PROMPT if search_url else "")
    parsed = _parse_reply(
        service.complete(system, user, timeout=300.0, max_tokens=CHAT_MAX_TOKENS)
    )
    query = parsed.get("search") if search_url else ""
    if query:
        results = web_search(search_url, query)
        followup = (
            user
            + f"\n\nSEARCH RESULTS for \"{query}\":\n" + format_search_results(results)
            + "\n\nAnswer the user now using these results if they help; say "
              "so plainly if they don't. Do not request another search."
        )
        parsed = _parse_reply(
            service.complete(system, followup, timeout=300.0,
                              max_tokens=CHAT_MAX_TOKENS)
        )
    actions = validate_actions(parsed.get("actions"), board)
    actions, render_removed = _separate_render_from_edits(actions)
    response_message = parsed["message"] or "I prepared the requested storyboard changes."
    if render_removed:
        response_message += (
            " Rendering is not included with edit proposals; ask to render "
            "separately after all changes are applied."
        )
    return {
        "message": response_message,
        "actions": actions,
        "service": service.label, "model": service.model,
    }
