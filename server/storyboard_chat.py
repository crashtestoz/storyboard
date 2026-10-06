"""Compact context and reviewable edit proposals for the assistant blade."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .backends.vpipe_backend import estimate_render_seconds
from .hardware import describe_hardware
from . import ad_memory
from .llm import H3_SOUND_RULES, H3_VISUAL_RULES, LLMService
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


class _GuideText(HTMLParser):
    """Plain text of index.html's #helpDialog: headings, term/definition
    lists, paragraphs and the copyable examples. Diagrams and buttons are
    skipped — they carry nothing a text model can use."""

    SKIP = {"svg", "button", "script", "style"}
    BREAK = {"p", "dt", "section", "li"}

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.buf: list[str] = []
        self.inside = False
        self.depth = 0
        self.skip = 0
        self.heading = ""

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
        self.buf = []
        if text:
            self.lines.append(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id") == "helpDialog":
            self.inside = True
        if not self.inside:
            return
        if tag == "div":
            self.depth += 1
        if tag in self.SKIP:
            self.skip += 1
        if self.skip:
            return
        cls = a.get("class") or ""
        if tag in self.BREAK or "card-heading-title" in cls or "help-panel" in cls:
            self._flush()
        if "card-heading-title" in cls:
            self.buf.append("## ")
        elif "card-heading-desc" in cls:
            self.buf.append(" — ")
        elif tag == "dt":
            self.buf.append("- ")
        elif tag == "code":
            self.buf.append(" `")

    def handle_endtag(self, tag):
        if not self.inside:
            return
        if tag in self.SKIP:
            self.skip -= 1
            return
        if self.skip:
            return
        if tag in ("dt", "strong"):
            self.buf.append(": ")
        elif tag == "code":
            self.buf.append("`")
            self._flush()
        elif tag == "dd":
            self._flush()
        elif tag == "div":
            self.depth -= 1
            if self.depth == 0:
                self._flush()
                self.inside = False

    def handle_data(self, data):
        if self.inside and not self.skip:
            self.buf.append(data)


def _prompt_guide() -> str:
    """The in-app "Storyboard prompt guide", read from index.html itself.

    Same reasoning as the MCP catalogue above: the dialog is the one copy the
    user reads, so the AD reads the same one rather than a hand-kept summary
    that drifts from it. ~1.5k tokens, and it sits in the system prompt, so a
    backend with prefix caching pays for it once per conversation.
    """
    path = Path(__file__).resolve().parent.parent / "index.html"
    try:
        parser = _GuideText()
        parser.feed(path.read_text(encoding="utf-8"))
        parser._flush()
    except (OSError, ValueError):
        return ""
    lines = [re.sub(r":\s*:", ":", line).replace(" : ", ": ") for line in parser.lines
             if line not in ("Storyboard prompt guide", "Camera prompt guide")]
    if not lines:
        return ""
    return (
        "STORYBOARD PROMPT GUIDE (the app's own ? guide, which the user also "
        "reads — use its vocabulary and templates, and point the user to it "
        "when helpful). In this app the spoken words go in a shot's dialogue "
        "field and the voice direction in dialogueStyle; both are appended to "
        "the prompt automatically, so use the dialogue templates for those "
        "fields rather than pasting the line into the prompt. Timed sound "
        "accents go in soundNote.\n" + "\n".join(lines)
    )


def _hardware_reference() -> str:
    hw = describe_hardware()
    return (
        "HARDWARE THIS SERVER RUNS ON: " + hw["summary"] + ". Every shot's "
        "estimatedRenderSeconds in context is derived from this machine's own "
        "previous renders of the same model; null means none has been timed "
        "here yet. State that when giving a time, and that it is an estimate "
        "— actual runtime still varies with what else is running."
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

Board = sceneDescription (project-wide scene content, auto-prepended to every \
shot) + renderStyle (project-wide visual rendering style, auto-prepended to \
every shot — never repeat either in a shot prompt) + soundscape (project background bed; can \
render into every shot, or be held out for a later mix) + characters (name, \
description, optional portrait + voice clip) + shots.

Shot prompt shape: labeled sections, one per line, each one sentence \
ending in a period — "Camera Direction & Framing: ..." (angle, movement, \
composition, lens/depth of field when it matters), "Clothing / Appearance: \
..." (only for what differs in this shot from the character's Cast \
description and portrait, or for a character with no Cast description; \
omit the line entirely otherwise), "Setting: ..." (the room or location and \
its fixed details; reuse the SAME Setting text word for word in every shot \
set in that place, and omit the line when no location is given), then \
"Pose / Action: ..." (present tense, in the order it happens). Every render already sends each cast \
member's Cast description, and on Ref2VA their portrait, alongside the \
shot prompt, so restating clothing they already give is redundant. Same \
shape the Rewrite button produces — see rewrite_prompt's \
SYSTEM_PROMPT in llm.py for the full rules if detail is needed.

Model routing: every H3 shot renders through Ref2VA, chained or not. A \
Start or End frame -- picked by hand, or chained from an earlier shot's \
last rendered frame (usually the one before; a chain may skip back past \
a cutaway) -- is sent as the first ordered reference: soft \
guidance for continuity, not a hard-pinned keyframe, so the opening frame \
will resemble it rather than reproduce it exactly. It travels alongside the \
cast portraits, the speaker's voice clip and the shot's own references \
(style images only when added to that shot), and counts toward Ref2VA's \
limits (9 images, 3 audio, 12 total). FL2VA is no longer auto-selected. \
Only an explicitly chosen project engine changes this: Wan 2.2 (needs a \
Start frame) and LTX-2.5 wire Start/End frames as hard anchors and send no \
separate Cast, style or shot-reference images.

To change what a character says in a shot, propose update_shot with a new \
fields.dialogue string — that is an ordinary board edit, reviewed and \
applied like any other field, not a synthesis step. What follows is about \
turning that saved line into audio, a separate concern from the line's own \
wording:

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

Draft mode renders small and fast (capped resolution, up to 8 steps, no generated \
audio) for blocking iteration before a full-quality pass. Steps: 8 is draft \
quality, 16 is the final-quality setting.

Music: Settings → Soundtrack lays one continuous piece under the whole \
assembled cut — generated by Stable Audio 3 from its own music prompt, or an \
uploaded file — and ducks it under dialogue. That is where a theme or score \
belongs, not in a shot's soundNote or the soundscape (see the H3 sound facts below).

Writing sceneDescription, renderStyle or soundscape: all are applied to every shot, \
interiors and exteriors alike, so they must hold for all of them; anything \
true of only some shots belongs in those shots.
""" + "\n" + H3_VISUAL_RULES + "\n" + H3_SOUND_RULES

CHAT_SYSTEM_PROMPT = """\
You are the Storyboard AD, the user's assistant director. Help the user review, plan, and edit
the storyboard currently open in the app. Be concise, concrete, and candid
about continuity, camera direction, pacing, visual consistency, and sound.

The CURRENT STORYBOARD context below is the full board: every shot's actual
prompt, soundNote, dialogue and title text, not a summary of it. A field
missing from a shot is empty (no dialogue, no start frame, and so on); refs
lists the shot's reference images by @tag. CONVERSATION MEMORY, when present,
summarises the earlier part of this conversation: honour the decisions and
preferences it records. OTHER STORYBOARDS lists the project's other boards
by slug, name and shot count — the open board is not among them. Read the
wording of each shot's prompt, not just its topic, when asked to review,
critique or check something — a continuity or pacing problem is often in the
specific words a shot uses (a camera move, a pose, a detail) clashing with
its neighbors, not just in what it's "about". Reason across the sequence in
shot-number order to judge whether the scene's logic holds together.

YOUR EXPERTISE — bring all of it to every review, rewrite and new shot:

Storyteller. Every shot is a story beat with one job: establish, reveal,
escalate, react, or release. Know what each shot's job is and cut anything
that does not serve it. Shape the board as a sequence with setup, rising
tension, a turn and a payoff; vary rhythm (wide to close, still to moving,
long to short) so the cut breathes; open on a strong image and end on one.
Show, do not tell: prefer an action or expression over a line of dialogue
that explains it. When a board lacks a clear want, obstacle or turn, say so.

Story developer and showrunner. You can grow new storylines out of a board —
the next episode, a sequel, a prequel, a spin-off, a missing scene, an
alternate ending, a standalone story in the same world. Work from what the
board actually establishes, never from its title alone:
- Mine it first. Read every shot in order, its dialogue and the cast, and
  build a story bible: the plot so far; each character's want, fear, secret
  and what they have done on screen; who holds power over whom; the world's
  rules (its technology, institutions, what is possible); recurring
  locations, props and visual motifs; tone, genre and theme; and above all
  the open threads — setups not yet paid off, unanswered questions, promises
  made, and the final hook. When the story spans boards (earlier episodes, a
  parent story), read them too before developing anything.
- Grow from the open threads. Every new storyline pays off or complicates at
  least one of them, and its premise follows from where the board left its
  characters. Honour every fact already on screen: never retcon unless the
  user asks, and say so plainly if an idea would contradict something shown.
  Characters stay true to what they have done; any change in them is earned
  on screen. Keep the tone and genre unless the user asks to shift them.
- Escalate. A continuation raises the stakes, narrows the options or turns
  an ally — it does not repeat the last story's shape. Give each episode its
  own question that it answers by the end, plus a hook that opens the next.
- Use the established cast first; add a character only with a clear story
  function (an obstacle, a mirror, a source of information), and say why.
- Plan for what this pipeline renders: short clips (a 124-frame shot is about
  five seconds), so an episode of 24 to 36 shots runs two to three minutes.
  Favour few locations, a handful of characters per shot, and conflict told
  through visible action, faces and short lines rather than exposition.
- When asked for new storylines or ideas, pitch before building and propose
  no actions. Offer two to four options that differ in direction, not just
  detail. For each one give: a title; a one-sentence logline (who, what they
  want, what stands in the way, what is at stake); the open thread it grows
  from; a beat outline of five to eight beats (setup, escalation, turn,
  climax, hook); any new cast or locations; and a rough shot count. Then
  recommend one and say why. Use plain text with line breaks and keep each
  option tight; this is the one kind of reply that may run longer than a
  few sentences.
- When the user picks one, develop or build it as they ask: a fuller beat
  sheet in chat; add_shot on this board if the story continues here; or, for
  a new episode or spin-off, create_board carrying over all continuity
  ("all"), named in the series' existing pattern (the next "... - Ep N"), with
  its opening shots.

MiniMax H3 prompt writer. Write for what the model can actually render in
one clip at 24fps (a 124-frame shot is about five seconds): one subject
focus, one camera move, one ordered action with a clear start and end state.
Use concrete, visible nouns and verbs, not moods or abstractions ("rain
beads on the cockpit glass", not "a tense atmosphere"). Lead each section
with its most important element. Name every character exactly as the cast
list does, in every shot. Do not restate clothing or appearance the
character's Cast description or portrait already covers: the render sends
both with every shot, and that is what keeps them recognisable. Use
Clothing / Appearance only for what changes in this shot (soaked, torn
sleeve, helmet on, bloodied) or for a character with no Cast description,
and omit the section when there is nothing to add. Match
motion to length: too many actions for the frame count blur or get dropped;
too few leave dead air. Follow the MiniMax H3 facts below without exception.

Continuity supervisor. Before judging or writing any shot, build a mental
continuity ledger from the preceding shots and check the new one against it:
- Screen direction and the 180-degree line: a character or craft moving
  frame left to right keeps doing so across cuts unless a shot on the line
  or a visible turn resets it; two characters facing each other keep their
  frame sides across a conversation.
- Character placement: who is frame left/right, foreground/background, near
  or far, standing or seated, and where they are relative to each other and
  to fixed landmarks. A character cannot jump sides, rooms or distances
  between consecutive shots without on-screen motivation.
- Eye lines: a character looking frame right at someone is answered by that
  person looking frame left.
- Pose and action handoff: a shot opens in the state the previous one ended
  in (standing stays standing, a raised hand stays raised, a door opened
  stays open), unless time has visibly passed.
- Wardrobe, hair, injuries, dirt, wet or dry, held props and which hand holds
  them.
- Environment: time of day, light direction and colour, weather, set dressing,
  vehicle damage.

Physical staging rules — every character and object behaves like a real body
in a real space, unless the user explicitly asks otherwise:
- Characters move forward, facing their direction of travel. Walking,
  running or backing up in reverse happens only when the user explicitly
  asks for it; otherwise write "walks forward toward frame left", not just
  "moves left". The same applies to vehicles and craft: nose first.
- Solid things block movement. A character never walks through a wall,
  table, door, vehicle or another person: route them around it, over it, or
  through a visible opening ("steps around the crate", "pushes the door open
  and walks through the doorway"). Enter and exit only through doors,
  openings or the frame edge.
- Feet stay on the ground and weight is real: no floating, sliding or gliding
  unless the story calls for it. Seated characters sit on something named;
  a character who leans, grips or carries something makes contact with it.
- One body, one place. A character appears once per frame, never duplicated,
  never teleporting within a shot, never merging or overlapping with another
  character or object. Keep scale consistent with the surroundings and
  between shots.
- Movement fits the space and the time: a character crosses only the
  distance they could cover in the shot's length, and ends where the next
  shot finds them.
- H3 has no negative prompt, so write these rules as what happens, never as
  "not backwards" or "without passing through": state the forward direction,
  the path, and the obstacle they go around.
When asked to check continuity, also flag any shot whose wording invites a
break in these rules (an ambiguous direction of travel, a path through a
named obstacle, a missing seat or doorway).
When asked to check continuity, report each break concretely: shot numbers,
the specific words that clash, and the fix. Then propose update_shot for the
shots that need it.

Camera operator and director of photography. Pick the shot that tells the
beat best, not the one that is merely pretty: establish geography with a
wide before cutting in; use a close-up for emotion and decisive detail;
use an over-the-shoulder or two-shot to hold spatial relationships in
dialogue; use low angles for power and high angles for vulnerability. Every
camera move must be motivated by the action or the reveal: push in for
realisation, pull back for isolation or scale, track with moving subjects,
hold still when the performance carries the shot. Specify lens feel (wide
for scale and speed, long for compression and isolation) and depth of field
when it matters. Vary shot size between consecutive shots (cutting wide to
medium to close) and avoid jump cuts between two near-identical framings of
the same subject. Keep camera height and side consistent within a scene
unless changing them is the point.

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
{"tool":"set_board_fields","fields":{"sceneDescription":"...","renderStyle":"...","soundscape":"..."}}
{"tool":"add_character","character":{"name":"...","description":"..."}}
{"tool":"update_character","characterId":"existing id","fields":{"name":"...","description":"..."}}
{"tool":"add_shot","shot":{"title":"...","prompt":"...","soundNote":"...","dialogue":"...","dialogueStyle":"...","characterIds":["existing id"],"frames":124,"steps":8,"seed":0}}
{"tool":"update_shot","shotId":"existing id","fields":{"title":"...","prompt":"...","soundNote":"...","dialogue":"...","dialogueStyle":"...","characterIds":["existing id"],"frames":124,"steps":8,"seed":0}}
{"tool":"replace_text","find":"exact existing words","replace":"new words","scope":["shots","board","cast"],"shotIds":["existing id"]}
{"tool":"start_render","shotIds":["existing id", ...]}
{"tool":"dub_shot","shotId":"existing id"}
{"tool":"stop_render"}
{"tool":"assemble"}
{"tool":"create_board","name":"...","from":"source board slug (omit for the open board)","carryOver":["cast","style","settings"],"fields":{"sceneDescription":"...","renderStyle":"...","soundscape":"..."},"characters":[{"name":"...","description":"..."}],"shots":[{"title":"...","prompt":"...","characterIds":["existing id"], ...same fields as add_shot}]}
{"tool":"open_board","slug":"other board slug"}
{"tool":"rename_board","name":"..."}
{"tool":"delete_board","slug":"board slug"}
{"tool":"copy_from_board","slug":"other board slug","carryOver":["cast","style","settings"]}

A shot with "locked": true is final and frozen. Never propose update_shot,
replace_text, start_render or dub_shot for it — the app drops them. If the
user asks to change one, say it is locked and that they can unlock it in the
shot editor.

Board management — create_board, open_board, rename_board, delete_board and
copy_from_board act on whole storyboards rather than on the open board's
contents. Show continuity comes from carryOver, which copies from a source
board in three groups:
  "cast"     every cast member with their portrait, voice clip and existing
             id, so shots can keep using those ids in characterIds;
  "style"    sceneDescription, renderStyle, soundscape and style references;
  "settings" the render format and every board default: resolution, frames,
             steps, model, sketch/draft, stills size/style/engine, dialogue
             voice engine, the AD's speaking voice, soundscape-in-shots.
  "all" means all three. A sequel, next episode, spin-off or anything else
  that continues a show carries over all three unless the user says
  otherwise.
- create_board makes a new board and opens it. from names the source board
  (a slug from OTHER STORYBOARDS); omit it to copy from the open board.
  fields set or override the new board's sceneDescription, renderStyle and
  soundscape after the copy (a sequel moving to new locations may change
  sceneDescription); characters adds new cast members; shots seeds it with
  opening shots (characterIds may only use carried-over cast ids — read the
  source board first if it is not the open one). The conversation moves with
  the user to the new board, so keep building there.
- copy_from_board copies the chosen groups from another board into the open
  board, for a board that already exists. Copied cast replace an existing
  cast member with the same id, and are added otherwise; style and settings
  overwrite the open board's own.
- open_board switches the app to another board, by slug from OTHER
  STORYBOARDS. rename_board renames the open board. delete_board deletes a
  board by slug (the open board's slug, or one from OTHER STORYBOARDS); it
  keeps rendered video on disk, and the app asks the user to confirm. Only
  propose delete_board when the user explicitly asks to delete that board.
- A board management action is always proposed alone: never in the same
  response as another management action, an edit, or an operation. Put any
  shots for a new board inside create_board itself, not as add_shot.

Reading other storyboards: to look at another board's full contents (its
cast, shots, prompts and dialogue) — to continue its story, match its style,
reuse a character, or compare — add a key named read to the JSON object,
holding a list of up to three slugs from OTHER STORYBOARDS. You will be given
those boards and one more turn to give the final answer; do not set read a
second time in the same exchange. Read a board whenever the user's request
depends on what it contains, rather than guessing from its name.

Rules:
- Use only the allowed tools and fields. Never invent IDs.
- Prefer updating an existing shot when it represents the same story beat.
- To change a word or phrase while leaving the rest of the text alone —
  renaming a prop, swapping a detail, trimming a repeated description — use
  replace_text instead of rewriting whole prompts with update_shot. The app
  replaces every exact occurrence for you and shows each changed shot for
  review. find is case-sensitive and must be copied character for character
  from the CURRENT STORYBOARD context; when the same thing is worded
  differently in different shots, propose one replace_text per wording.
  replace may be empty to delete the words. scope limits where it looks
  (shot text fields, the board's sceneDescription/renderStyle/soundscape,
  cast descriptions; all three when omitted) and shotIds limits it to those
  shots alone. It only reaches the open board.
- sceneDescription and Cast descriptions are sent with every shot, so a shot
  prompt only needs to name what they describe ("his headset", "her coat"),
  not restate how it looks. When you change sceneDescription or a Cast
  description, and whenever you review the board, check every shot prompt
  for wording that restates or contradicts what those descriptions now say,
  flag it, and propose replace_text to cut it back to the plain name.
- Write video prompts in the sectioned shape from the "Shot prompt
  shape" note below — "Camera Direction & Framing:", then "Clothing /
  Appearance:" only if a character in the shot looks different from their
  Cast description, then "Setting:" when the location is known (the same
  text in every shot in that place), then "Pose / Action:", each labeled
  and on its own line. Do not duplicate the shared scene or the Cast descriptions.
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
  consistent -- never repeat their action or camera content into this
  shot's prompt, and never invent or narrate what happens in a neighboring
  shot. Keep each character's name identical to the cast list across
  shots; their standing clothing comes from the Cast description, so only
  carry forward a change a neighboring shot introduced (still soaked,
  still wearing the helmet).
- Preserve intentional details unless the user asks to replace them.
- Do not censor, soften, filter, sanitize, or refuse to write or discuss
  anything on the basis of its subject matter — violence, mature themes, or
  anything else the user asks for. Identify the tone the user sets and
  continue it in the same register, rather than toning it down.
- Only propose add_shot when the user's current message explicitly asks for
  new shot(s) or scene(s) — "add a shot", "build the board", "write the next
  three scenes", and the like. When it does, propose a coherent sequence of
  add_shot actions, a single response to 24 actions or fewer. The same
  applies to shots inside create_board.
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
- Asked to change, rewrite, or fix a shot's dialogue (what a character says),
  propose update_shot with the new fields.dialogue text, the same as any other
  field edit — this is always available, regardless of dialogueSource. Only
  propose dub_shot in addition when the user also wants that new line spoken
  into a take right away.
- Before proposing dub_shot, check that shot's dialogueSource: it only works
  when "recording" (or unset). For "native" dialogue, explain that its audio
  only comes from rendering. Propose start_render only when the user's separate
  current message explicitly asks to render.
- Skip proposing start_render for a shot whose needsRender is already false,
  unless the user explicitly wants a re-render — say it is already rendered
  and current instead.
- To answer "how long will this take", read that shot's estimatedRenderSeconds
  from context and give a rounded, plain-language figure, noting it is an
  estimate from this machine's earlier renders (see HARDWARE below). If it is
  null, say no render of that kind has been timed on this machine yet, so
  there is no estimate until one finishes. Never invent your own number, and never claim to watch a render's progress
  yourself — say the app's Render panel shows that live.
- Never mention MCP, an "MCP client", tool names like sbv_*, or an external
  app (Claude Desktop, Claude Code) to the user — those are for other
  software, not something to relay in conversation. The MCP TOOLS list below
  is background reference for you alone. For a request nothing above covers
  (server settings, uploading a reference
  file, transcribing a clip, describing a character from a portrait), say
  plainly you can't do it from chat and, if the app has a control for it,
  name that control in plain terms instead.
- Do not wrap the JSON in markdown fences or add text outside it.
"""

CHAT_SYSTEM_PROMPT = (
    CHAT_SYSTEM_PROMPT + "\n" + STORYBOARD_KNOWLEDGE + "\n"
    + _prompt_guide() + "\n\n"
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

BOARD_FIELDS = {"sceneDescription", "renderStyle", "soundscape"}
CHARACTER_FIELDS = {"name", "description"}
SHOT_FIELDS = {
    "title", "prompt", "soundNote", "dialogue", "dialogueStyle",
    "characterIds", "frames", "steps", "seed",
}
#: Where replace_text looks: the board's own fields, cast descriptions, and
#: the text fields of shots.
REPLACE_SCOPES = ("board", "cast", "shots")
REPLACE_SHOT_FIELDS = {"title", "prompt", "soundNote", "dialogue", "dialogueStyle"}
#: What create_board / copy_from_board can carry from one board to another
#: for show continuity. The browser does the copying (js/app.js
#: carryOverBoard), so the two lists must name the same groups.
CARRY_OVER = ("cast", "style", "settings")
BOARD_TOOLS = {
    "create_board", "open_board", "rename_board", "delete_board", "copy_from_board",
}
#: Boards the AD may read in full in one exchange. Each is a whole board's
#: prompts and dialogue, so this is a token budget, not just a sanity cap.
MAX_READ_BOARDS = 3


def compact_board_context(
    board: dict[str, Any], selected_id: str | None = None, timings=None,
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
        estimated_seconds = (estimate_render_seconds(shot, board, timings)
                             if needs_render else None)
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
            "refs": [
                "@" + r["tag"] if r.get("tag") else (r.get("label") or "image")
                for r in shot.get("referenceImages") or [] if isinstance(r, dict)
            ],
            "needsRender": needs_render and not shot.get("locked"),
            "locked": bool(shot.get("locked")),
            "estimatedRenderSeconds": (
                round(estimated_seconds) if estimated_seconds is not None else None
            ),
        })
    # Empty text and false has-flags carry no information but cost tokens on
    # every shot of every turn; the prompt says an absent field means empty.
    shots = [
        {k: v for k, v in shot.items()
         if not (v in ("", []) or (v is False and (k.startswith("has") or k == "locked")))}
        for shot in shots
    ]
    return {
        "name": board.get("name") or "Untitled storyboard",
        "sceneDescription": board.get("sceneDescription") or "",
        "renderStyle": board.get("renderStyle") or "",
        "soundscape": board.get("soundscape") or "",
        "format": {"resolution": defaults.get("resolution"),
                   "defaultFrames": defaults.get("frames"),
                   "defaultSteps": defaults.get("steps")},
        "characters": characters,
        "styleReferenceCount": len(board.get("styleRefs") or []),
        "selectedShotId": selected_id,
        "shots": shots,
    }


def _chat_reference_images(
    board: dict[str, Any], selected_id: str | None, data_dir: Path | None,
) -> tuple[list[Path], list[str]]:
    """Resolve the focused shot's visual references for multimodal chat."""
    if data_dir is None:
        return [], []
    shots = board.get("shots") or []
    shot = next((s for s in shots if s.get("id") == selected_id), None)
    if shot is None and len(shots) == 1:
        shot = shots[0]
    refs: list[tuple[str, Any]] = []
    if shot:
        refs.extend((label, ref) for label, ref in (
            ("Shot start frame", shot.get("startRef")),
            ("Shot end frame", shot.get("endRef")),
        ) if ref)
        refs.extend((f"Shot reference {i}", ref) for i, ref in enumerate(
            shot.get("referenceImages") or [], 1))
        wanted = set(shot.get("characterIds") or [])
        refs.extend((f"Character portrait: {c.get('name') or 'unnamed'}", c.get("image"))
                    for c in board.get("characters") or []
                    if c.get("id") in wanted and c.get("image"))
    refs.extend((f"Project style reference {i}", ref) for i, ref in enumerate(
        board.get("styleRefs") or [], 1))
    files: list[Path] = []
    labels: list[str] = []
    root = data_dir.resolve()
    for label, ref in refs:
        value = ref.get("path") if isinstance(ref, dict) else ref
        if not isinstance(value, str) or not value.strip():
            continue
        target = (root / value.lstrip("/\\")).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        if target.is_file() and target.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
            files.append(target)
            tag = ref.get("tag") if isinstance(ref, dict) else None
            labels.append(f"{label}{f' ({tag})' if tag else ''}")
            if len(files) >= 9:
                break
    return files, labels


CLIP_REVIEW_FRAMES = 6      # per shot: first, last and evenly between
CLIP_REVIEW_MAX_SHOTS = 2
CLIP_REVIEW_EDGE = 768      # long edge, px — enough to read composition


def _shots_to_review(board: dict[str, Any], selected_id: str | None,
                     message: str) -> list[tuple[int, dict[str, Any]]]:
    """The focused shot, plus any shot the message names by number
    ("scene 8", "shot 3"), as (1-based number, shot)."""
    shots = board.get("shots") or []
    picked: list[int] = [i for i, s in enumerate(shots, 1) if s.get("id") == selected_id]
    for m in re.finditer(r"\b(?:scene|shot)s?\s*#?\s*(\d{1,3})\b", message, re.I):
        n = int(m.group(1))
        if 1 <= n <= len(shots) and n not in picked:
            picked.append(n)
    return [(n, shots[n - 1]) for n in picked[:CLIP_REVIEW_MAX_SHOTS]]


def _rendered_clip(shot: dict[str, Any], data_dir: Path) -> Path | None:
    """The shot's rendered clip on disk, from the /media/ URL in outputs."""
    root = data_dir.resolve()
    for url in shot.get("outputs") or []:
        if not isinstance(url, str) or not url.startswith("/media/"):
            continue
        rel = url[len("/media/"):].split("?", 1)[0]
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        # clip.mp4 beside the scene-named copy is the file renders write.
        for candidate in (target.parent / "clip.mp4", target):
            if candidate.is_file() and candidate.suffix.lower() == ".mp4":
                return candidate
    return None


def _chat_clip_frames(
    board: dict[str, Any], selected_id: str | None, message: str,
    data_dir: Path | None, workdir: Path,
) -> tuple[list[Path], list[str]]:
    """Stills from the rendered clip(s) under discussion, for the AD to look at.

    A vision model cannot take video, so the clip goes in as a handful of
    evenly spaced frames, each labelled with its time. That is enough to
    judge composition, layout, and where things are at the start, middle and
    end — which is most of what "review the clip" means. It is not enough to
    judge fine motion or anything about the sound, and the labels say so.
    """
    ffmpeg = shutil.which("ffmpeg")
    if data_dir is None or not ffmpeg:
        return [], []
    files: list[Path] = []
    labels: list[str] = []
    for number, shot in _shots_to_review(board, selected_id, message):
        clip = _rendered_clip(shot, data_dir)
        if clip is None:
            continue
        seconds = max(int(shot.get("frames") or 0), 1) / 24.0
        stale = stale_reason(shot, board)
        title = (shot.get("title") or "").strip()
        name = f"Scene {number}" + (f" (\"{title}\")" if title else "")
        for i in range(CLIP_REVIEW_FRAMES):
            # Stop one frame short of the end: seeking to the exact duration
            # can land past the last frame and return nothing.
            t = (seconds - 1 / 24.0) * i / (CLIP_REVIEW_FRAMES - 1)
            out = workdir / f"scene{number:02d}-{i + 1}.jpg"
            try:
                subprocess.run(
                    [ffmpeg, "-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(clip),
                     "-frames:v", "1", "-vf",
                     f"scale='min({CLIP_REVIEW_EDGE},iw)':-2", "-q:v", "3", str(out)],
                    check=True, capture_output=True, timeout=30,
                )
            except Exception:  # noqa: BLE001 — a frame short is still a review
                continue
            if out.is_file() and out.stat().st_size:
                files.append(out)
                labels.append(
                    f"{name} rendered clip, frame {i + 1} of {CLIP_REVIEW_FRAMES} at "
                    f"{t:.1f}s of {seconds:.1f}s"
                    + (" — rendered before the current prompt; it may not reflect "
                       f"recent edits ({stale})" if stale else "")
                )
    return files, labels


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


def _expand_replace_text(raw: dict[str, Any], board: dict[str, Any],
                         clean: list[dict[str, Any]]) -> int:
    """Turn a replace_text proposal into ordinary edit actions, in place.

    The model names the words; the exact replacement happens here, so a
    small model never has to copy whole prompts back out to change a phrase
    (and reword them on the way). Each touched shot, cast member or the
    board becomes one update_shot / update_character / set_board_fields
    action, merged into one already proposed for the same target so a
    phrase replaced in a prompt the model also rewrote lands in that
    rewrite. Returns how many places matched.
    """
    find, replace = raw.get("find"), raw.get("replace", "")
    if not isinstance(find, str) or not find or not isinstance(replace, str):
        return 0
    only = raw.get("shotIds")
    only = {v for v in only if isinstance(v, str)} if isinstance(only, list) else None
    scope = raw.get("scope")
    if isinstance(scope, list):
        scope = set(scope) & set(REPLACE_SCOPES)
    else:
        # Naming shots means just those shots, not the board and cast too.
        scope = {"shots"} if only is not None else set(REPLACE_SCOPES)

    def pending(tool: str, key: str | None, target: str | None) -> dict[str, Any]:
        for action in clean:
            if action["tool"] == tool and (key is None or action.get(key) == target):
                return action["fields"]
        action = {"tool": tool, "fields": {}}
        if key:
            action[key] = target
        clean.append(action)
        return action["fields"]

    targets: list[tuple[str, str | None, str | None, dict[str, Any], set[str]]] = []
    if "board" in scope:
        targets.append(("set_board_fields", None, None, board, BOARD_FIELDS))
    if "cast" in scope:
        targets.extend(("update_character", "characterId", c.get("id"), c, {"description"})
                       for c in board.get("characters") or [])
    if "shots" in scope:
        targets.extend(("update_shot", "shotId", s.get("id"), s, REPLACE_SHOT_FIELDS)
                       for s in board.get("shots") or []
                       if (only is None or s.get("id") in only) and not s.get("locked"))
    matched = 0
    for tool, key, target, source, fields in targets:
        for field in sorted(fields):
            proposed = next((a["fields"].get(field) for a in clean
                             if a["tool"] == tool and (key is None or a.get(key) == target)
                             and isinstance(a["fields"].get(field), str)), None)
            text = proposed if proposed is not None else source.get(field)
            if isinstance(text, str) and find in text:
                matched += text.count(find)
                pending(tool, key, target)[field] = text.replace(find, replace)
    return matched


def _clean_name(value: Any) -> str:
    return value.strip()[:120] if isinstance(value, str) else ""


def _clean_carry(value: Any) -> list[str]:
    if value == "all" or (isinstance(value, list) and "all" in value):
        return list(CARRY_OVER)
    return [v for v in CARRY_OVER if isinstance(value, list) and v in value]


def _validate_create_board(raw: dict[str, Any], character_ids: set,
                           source: str | None) -> dict[str, Any] | None:
    name = _clean_name(raw.get("name"))
    if not name:
        return None
    carry = _clean_carry(raw.get("carryOver"))
    action: dict[str, Any] = {"tool": "create_board", "name": name, "carryOver": carry}
    if source:
        action["from"] = source
    fields = _clean_fields(raw.get("fields"), BOARD_FIELDS)
    if fields:
        action["fields"] = fields
    raw_characters = raw.get("characters")
    characters = [
        fields for fields in (
            _clean_fields(c, CHARACTER_FIELDS)
            for c in (raw_characters if isinstance(raw_characters, list) else [])
        ) if fields.get("name")
    ]
    if characters:
        action["characters"] = characters[:24]
    # Only carried-over cast keep their ids on the new board; a new
    # character's id does not exist until the browser makes it.
    usable = character_ids if "cast" in carry else set()
    raw_shots = raw.get("shots")
    shots = []
    for shot in raw_shots if isinstance(raw_shots, list) else []:
        fields = _clean_fields(shot, SHOT_FIELDS)
        if "characterIds" in fields:
            fields["characterIds"] = [v for v in fields["characterIds"] if v in usable]
        if fields:
            shots.append(fields)
    if shots:
        action["shots"] = shots[:24]
    return action


def validate_actions(
    actions: Any, board: dict[str, Any], board_slugs: set[str] | None = None,
    current_slug: str | None = None, load_board=None,
    notes: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Allow-list model proposals before they reach browser state.

    *board_slugs* are the project's other boards; *current_slug* is the open
    one. Without them, open_board, delete_board and copying from another
    board have nothing valid to point at and are dropped. *load_board*
    (slug -> board) resolves another board's cast, so a new board's shots
    copied from it can be checked against real character ids. A replace_text
    that matched nothing adds a line to *notes*, for the user to see.
    """
    if not isinstance(actions, list):
        return []
    board_slugs = board_slugs or set()

    def source_cast(slug: Any) -> set | None:
        """Character ids of the board a copy reads from, or None if invalid."""
        if not slug or slug == current_slug:
            return character_ids
        if slug not in board_slugs or load_board is None:
            return None
        try:
            return {c.get("id") for c in load_board(slug).get("characters") or []}
        except Exception:  # noqa: BLE001 — an unreadable board is not a source
            return None

    shot_ids = {s.get("id") for s in board.get("shots") or [] if not s.get("locked")}
    locked_ids = {s.get("id") for s in board.get("shots") or [] if s.get("locked")}
    character_ids = {c.get("id") for c in board.get("characters") or []}
    clean: list[dict[str, Any]] = []
    replacements: list[dict[str, Any]] = []
    for raw in actions[:24]:
        if not isinstance(raw, dict):
            continue
        tool = raw.get("tool")
        if tool == "replace_text":
            # After everything else, so it lands in any rewrite of the same
            # shot proposed alongside it, whichever order the model wrote.
            replacements.append(raw)
        elif tool == "create_board":
            source = raw.get("from") if raw.get("from") != current_slug else None
            cast = source_cast(source)
            action = (_validate_create_board(raw, cast, source)
                      if cast is not None else None)
            if action:
                clean.append(action)
        elif tool == "copy_from_board" and raw.get("slug") in board_slugs:
            carry = _clean_carry(raw.get("carryOver"))
            if carry:
                clean.append({"tool": tool, "slug": raw["slug"], "carryOver": carry})
        elif tool == "open_board" and raw.get("slug") in board_slugs:
            clean.append({"tool": tool, "slug": raw["slug"]})
        elif tool == "rename_board" and _clean_name(raw.get("name")):
            clean.append({"tool": tool, "name": _clean_name(raw["name"])})
        elif tool == "delete_board" and (
                raw.get("slug") in board_slugs
                or (current_slug and raw.get("slug") == current_slug)):
            clean.append({"tool": tool, "slug": raw["slug"]})
        elif tool == "set_board_fields":
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
        elif tool in ("update_shot", "dub_shot") and raw.get("shotId") in locked_ids:
            if notes is not None:
                notes.append("A proposed change to a locked shot was dropped — unlock it first.")
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
                elif any(v in locked_ids for v in requested):
                    # Only locked shots were named: render nothing, not
                    # (by omission) the whole board.
                    if notes is not None:
                        notes.append("A render of a locked shot was dropped — unlock it first.")
                    continue
            clean.append(action)
        elif tool == "dub_shot" and raw.get("shotId") in shot_ids:
            clean.append({"tool": tool, "shotId": raw["shotId"]})
        elif tool in ("stop_render", "assemble"):
            clean.append({"tool": tool})
    for raw in replacements:
        if not _expand_replace_text(raw, board, clean) and notes is not None \
                and isinstance(raw.get("find"), str) and raw["find"]:
            notes.append(f'No text matched "{raw["find"][:80]}", so nothing was replaced.')
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
    # A board management action switches, renames or removes the board the
    # rest of the proposal would land on, so it only ever applies alone.
    board_action = next((a for a in actions if a.get("tool") in BOARD_TOOLS), None)
    if board_action is not None:
        return [board_action], False
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


EMPTY_REPLY = "The model returned an empty response."


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
        return {"message": text or EMPTY_REPLY, "actions": [],
                "search": "", "read": []}
    if not isinstance(doc, dict):
        return {"message": text, "actions": [], "search": "", "read": []}
    read = doc.get("read")
    if isinstance(read, str):
        read = [read]
    return {
        "message": str(doc.get("message") or "").strip(),
        "actions": doc.get("actions"),
        "search": str(doc.get("search") or "").strip(),
        "read": [v for v in read if isinstance(v, str)] if isinstance(read, list) else [],
    }


def chat(
    service: LLMService, board: dict[str, Any], message: str,
    history: list[dict[str, Any]] | None = None,
    selected_id: str | None = None,
    search_url: str = "",
    data_dir: Path | None = None,
    timings=None,
    memory_path: Path | None = None,
    slug: str | None = None,
    other_boards: list[dict[str, Any]] | None = None,
    load_board=None,
) -> dict[str, Any]:
    """One Storyboard AD turn.

    *other_boards* is the project's board listing (``Store.list_boards``)
    without the open board; *load_board* (slug -> board) lets the AD read
    one of them in full and lets validation check a copy's source cast.
    """
    message = (message or "").strip()
    if not message:
        raise ValueError("message is required")
    ok, why = service.health()
    if not ok:
        raise RuntimeError(why)
    cleaned = []
    for turn in history or []:
        role = turn.get("role") if isinstance(turn, dict) else None
        content = turn.get("content") if isinstance(turn, dict) else None
        if role in ("user", "assistant") and isinstance(content, str):
            cleaned.append({"role": role, "content": content})
    summary, recent = ad_memory.condense(service, cleaned, memory_path)
    with tempfile.TemporaryDirectory(prefix="sbv-ad-frames-") as tmp:
        return _chat_turn(service, board, message, selected_id, search_url,
                          data_dir, timings, summary, recent, Path(tmp),
                          slug, other_boards or [], load_board)


def _other_boards_context(other_boards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"slug": b.get("slug"), "name": b.get("name"), "shots": b.get("shots", 0)}
            for b in other_boards]


def _read_boards(slugs: list[str], other_boards: list[dict[str, Any]], load_board) -> str:
    """The requested boards as compact JSON blocks, for the AD's second turn.

    Only slugs from the listing are honoured, so the model cannot read a path
    of its choosing. Render estimates are left out: they are about this
    machine's queue, not about what a board says.
    """
    known = {b.get("slug") for b in other_boards}
    blocks = []
    for slug in list(dict.fromkeys(slugs))[:MAX_READ_BOARDS]:
        if slug not in known or load_board is None:
            blocks.append(f"BOARD {slug}: not one of OTHER STORYBOARDS.")
            continue
        try:
            context = compact_board_context(load_board(slug))
        except Exception as exc:  # noqa: BLE001 — say so rather than fail the turn
            blocks.append(f"BOARD {slug}: could not be read ({exc}).")
            continue
        for shot in context["shots"]:
            shot.pop("needsRender", None)
            shot.pop("estimatedRenderSeconds", None)
        context.pop("selectedShotId", None)
        blocks.append(f"BOARD {slug} (compact JSON):\n"
                      + json.dumps(context, ensure_ascii=False, separators=(",", ":")))
    return "\n\n".join(blocks)


def _chat_turn(service, board, message, selected_id, search_url, data_dir,
               timings, summary, recent, frames_dir: Path,
               slug=None, other_boards=(), load_board=None) -> dict[str, Any]:
    images, image_labels = _chat_reference_images(board, selected_id, data_dir)
    clip_images, clip_labels = _chat_clip_frames(
        board, selected_id, message, data_dir, frames_dir)
    images, image_labels = images + clip_images, image_labels + clip_labels
    visual_context = ""
    if image_labels:
        visual_context = (
            "\n\nATTACHED IMAGES (inspect their visual contents and use them as "
            "context for the user's request):\n" + "\n".join(
                f"- Image {i}: {label}" for i, label in enumerate(image_labels, 1)
            )
        )
    if clip_labels:
        visual_context += (
            "\nThe 'rendered clip' images are stills taken at even intervals from the "
            "shot's actual render, so you CAN review what the clip shows: layout, "
            "positions, framing, and how they change from start to end. Describe what "
            "you see in them before suggesting changes. They are single frames, so say "
            "so if a judgment needs the motion between them, and you still cannot hear "
            "the clip's sound."
        )
    # Conversation first, board second: the conversation only ever grows at
    # its end, so a backend that reuses a cached prompt prefix (Ollama,
    # llama.cpp, vLLM) keeps it warm from turn to turn, while the board —
    # which changes whenever an edit is applied — sits after it and costs
    # only its own re-read.
    user = (
        ("CONVERSATION MEMORY (earlier turns, summarised):\n" + summary + "\n\n"
         if summary else "")
        + "RECENT CONVERSATION:\n"
        + json.dumps(recent, ensure_ascii=False, separators=(",", ":"))
        + "\n\nCURRENT STORYBOARD (compact JSON):\n"
        + json.dumps(compact_board_context(board, selected_id, timings), ensure_ascii=False, separators=(",", ":"))
        + "\n\nOTHER STORYBOARDS:\n"
        + json.dumps(_other_boards_context(other_boards), ensure_ascii=False, separators=(",", ":"))
        + visual_context
        + "\n\nUSER:\n" + message
    )
    search_url = (search_url or "").strip()
    system = CHAT_SYSTEM_PROMPT + ("\n" + SEARCH_CAPABILITY_PROMPT if search_url else "")
    complete = (lambda prompt: service.complete_with_media(
        system, prompt, images=images, timeout=300.0
    )) if images else (lambda prompt: service.complete(
        system, prompt, timeout=300.0, max_tokens=CHAT_MAX_TOKENS
    ))
    def ask(prompt: str) -> dict[str, Any]:
        # A local model occasionally returns nothing at all (seen on a long
        # storyline pitch after a board read); one retry usually answers,
        # where showing the user "empty response" never does.
        reply = _parse_reply(complete(prompt))
        if reply["message"] == EMPTY_REPLY:
            reply = _parse_reply(complete(prompt))
        return reply

    parsed = ask(user)
    query = parsed.get("search") if search_url else ""
    reads = parsed.get("read") or []
    if query or reads:
        # One follow-up turn answers both, so asking for a search and a board
        # read together still costs a single extra round trip.
        followup = user
        if reads:
            followup += ("\n\nREQUESTED STORYBOARDS:\n"
                         + _read_boards(reads, list(other_boards), load_board))
        if query:
            results = web_search(search_url, query)
            followup += (f"\n\nSEARCH RESULTS for \"{query}\":\n"
                         + format_search_results(results))
        followup += ("\n\nAnswer the user now using what was requested above; say "
                     "so plainly if it didn't help. Do not request another search "
                     "or board read.")
        parsed = ask(followup)
    notes: list[str] = []
    actions = validate_actions(
        parsed.get("actions"), board,
        board_slugs={b.get("slug") for b in other_boards},
        current_slug=slug, load_board=load_board, notes=notes,
    )
    actions, render_removed = _separate_render_from_edits(actions)
    response_message = parsed["message"] or "I prepared the requested storyboard changes."
    for note in notes:
        response_message += " " + note
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
