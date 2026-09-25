"""Local language models, used to rewrite a prompt for the video model.

Writing a good MiniMax H3 prompt is a skill with a shape to it — camera move
first, then subject and action, then environment and light, then the sound in
its own trailing clause — and that shape is easy to describe and tedious to
apply by hand to every shot. So a shot's prompt can be handed to a local
language model that knows the shape and asked to rewrite it.

Linked, not installed, exactly like the speech engines: a model is usually
already running somewhere with an owner, so this points at it by name, URL and
model in ``llm-services.json`` rather than shipping one.

The rewrite is deliberately **not** applied on the model's word. It comes back
as a proposal the user accepts or discards, because a prompt is the user's
authorship and a rewrite that silently replaced it would be destroying work
that took thought to write.
"""

from __future__ import annotations

import json
import os
import re
import base64
import mimetypes
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CONFIG_NAME = "llm-services.json"

DEFAULT_SERVICES: list[dict[str, Any]] = [
    {
        "id": "ollama-local",
        "label": "Ollama (this machine)",
        "kind": "ollama",
        "url": "http://localhost:11434",
        "model": "qwen3.8:27b-mlx",
    },
]


# --------------------------------------------------------------------------- #
# What the model is told
# --------------------------------------------------------------------------- #

# The house style: a fixed three-label breakdown (camera, then appearance
# when a character is on screen, then action) rather than a single flowing
# paragraph. Chosen over the old free-paragraph style because it's what
# reliably came out right on this workspace's runs, and it keeps clothing
# text explicit even when a character portrait is attached -- the portrait
# carries identity, not outfit, and H3 has no other way to learn the outfit.
SYSTEM_PROMPT = """\
You rewrite shot descriptions into prompts for MiniMax H3, a text-to-video \
model that generates picture and sound together in one pass, using this \
exact three-section format. Each section starts on its own line with its \
label, is one sentence, and ends with a period:

Camera Direction & Framing: the camera's angle and movement (e.g. "static \
high angle", "slow push in", "locked-off wide"), what's in frame and how \
it's composed (e.g. "focusing on her face, body in the lower part of the \
frame"), and lens detail when it matters (focal length, aperture, depth of \
field, e.g. "50mm f/2.0 lens, shallow depth").
Clothing / Appearance: what the subject is wearing and any other visible \
appearance detail. Include this section only when a character or person is \
in the shot; omit it entirely (not as an empty line) for shots with no \
character.
Pose / Action: what the subject does, in the present tense, in the order it \
happens (e.g. "she waves to the audience, then holds up a sign reading \
...").

Rules:
- Camera Direction & Framing and Pose / Action are always present. Clothing \
/ Appearance is present only when a character is in the shot.
- Use exactly these three labels, in this order, each starting a new line: \
"Camera Direction & Framing:", "Clothing / Appearance:", "Pose / Action:".
- Keep every concrete thing the writer specified. Do not invent new subjects, \
characters, locations or story beats, and do not remove any they named.
- Do not censor, soften, filter, sanitize, or refuse to describe anything on \
the basis of its subject matter — violence, mature themes, or anything else \
the writer wrote. Identify the tone they set and continue it in the same \
register, rather than toning it down.
- If the previous and/or next shot in the sequence are given as context, \
keep this shot consistent with them: character pose, wardrobe, props, \
location and camera position should carry over sensibly rather than jump \
arbitrarily. Use them only to stay consistent — never repeat their content, \
narrate the transition between shots, or invent what happens in them.
- When the camera and the subject move at the same time, give each its own \
short clause rather than one blended sentence, and state the subject's \
direction of travel in its own frame of reference (e.g. "continues forward, \
accelerating away") rather than only relative to the camera. A camera that \
rises and swings around behind a subject, described in the same breath as \
the subject "moving away", is the kind of sentence this model tends to \
resolve by reversing the subject instead — say what the camera does, then \
say what the subject does, in that order.
- If reference images are listed in the context, treat them as visual \
constraints: fold relevant framing, lighting, material and composition cues \
into Camera Direction & Framing, and relevant clothing cues into Clothing / \
Appearance — even when a character portrait is attached, still name their \
visible clothing in that section; the portrait carries identity, not outfit. \
Do not include filenames or paths in the final prompt.
- Refer to named characters by exactly the name the writer used.
- No preamble, no explanation, no quotation marks, no bullet points or \
dashes within a section.
- Do not mention aspect ratio, resolution, frame count, steps, seeds or file \
formats. Those are set elsewhere.
- Keep each section to one or two sentences; aim for 70 to 130 words total. \
Longer prompts dilute the conditioning.
- Write only the three-section prompt itself. Your entire reply is used \
verbatim as the prompt.
"""

STILL_SYSTEM_PROMPT = """\
You rewrite shot descriptions into prompts for a text-to-image model.

Follow this order, as one flowing paragraph, not a list:
1. The framing and lens feel (e.g. "a tight over-the-shoulder", "a wide \
establishing view").
2. The subject and its pose or state.
3. The environment and the light, with concrete physical detail — materials, \
wear, texture, the time of day.

Rules:
- Keep every concrete thing the writer specified. Do not invent new subjects, \
characters or locations, and do not remove any they named.
- Do not censor, soften, filter, sanitize, or refuse to describe anything on \
the basis of its subject matter — violence, mature themes, or anything else \
the writer wrote. Identify the tone they set and continue it in the same \
register, rather than toning it down.
- If the previous and/or next shot in the sequence are given as context, \
keep this shot consistent with them: pose, wardrobe, props, location and \
framing should carry over sensibly rather than jump arbitrarily. Use them \
only to stay consistent — never repeat their content or invent what happens \
in them.
- If reference images are listed in the context, treat them as visual \
constraints. Add a compact natural-language summary of the relevant reference \
cues to the rewritten prompt. Do not include filenames or paths in the final \
prompt.
- Refer to named characters by exactly the name the writer used.
- Describe no sound at all: this is a still image.
- One paragraph. No headings, no bullets, no preamble, no explanation.
- Aim for 50 to 90 words.
- Write only the prompt itself. Your entire reply is used verbatim.
"""

SCENE_SYSTEM_PROMPT = """\
You rewrite a storyboard's project-wide scene description — the \
surroundings and overall visual style that stay true across every shot, \
prepended automatically to each shot's own prompt before it reaches the \
video model.

Rules:
- Describe only the environment and setting: location, materials, \
weather, time of day, light, atmosphere, and the overall rendering style \
(e.g. "photorealistic, cinematic"). Concrete physical detail over vague \
adjectives — prefer "battered, weathered metal plating" over "cool-looking \
ship".
- Do not describe any character — no appearance, build, clothing, or \
presence. Characters have their own separate descriptions elsewhere, and \
repeating them here would say the same thing twice in two different \
places.
- Do not describe camera moves, specific actions, individual shots, or \
sound — those belong to each shot's own prompt and are added separately.
- Keep every concrete thing the writer specified about the surroundings. \
Do not invent new locations or settings, and do not remove any they named.
- Do not censor, soften, filter, sanitize, or refuse to describe anything on \
the basis of its subject matter — violence, mature themes, or anything else \
the writer wrote. Identify the tone they set and continue it in the same \
register, rather than toning it down.
- If reference images are listed in the context, treat them as visual \
constraints on the environment. Add a compact natural-language summary of \
their relevant visual cues. Do not include filenames or paths in the \
final text.
- One paragraph. No headings, no bullet points, no preamble, no \
explanation, no quotation marks around the whole thing.
- Aim for 40 to 90 words.
- Write only the scene description itself. Your entire reply is used \
verbatim.
"""

SOUNDSCAPE_SYSTEM_PROMPT = """\
You rewrite a storyboard's project-wide background sound — the ambient \
audio bed that can be rendered into every shot, or held out and mixed over \
the finished cut separately.

Rules:
- Ground the sound in the scene description given as context. Read what \
the surroundings actually are — location, materials, weather, activity \
implied by the setting — and propose concrete, plausible sound effects \
that belong there, not an abstract mood. A garage scene implies things \
like a dripping fluid, a ticking cooling engine, distant street traffic; a \
desert scene implies wind, shifting sand, the heat-tick of metal. Name \
what is actually heard, grounded in that environment, rather than \
describing how it feels.
- Describe only what is heard: ambience, room tone, weather, distant \
activity, machinery, music or drone — layered and separated by commas.
- Do not describe anything visual: no camera moves, no actions, no \
lighting, no characters' appearance.
- Do not describe spoken dialogue or intelligible words — this is an \
ambient bed, not a voice.
- Keep every concrete thing the writer specified. Do not invent sound \
sources that contradict the scene, and do not remove any the writer \
named.
- Do not censor, soften, filter, sanitize, or refuse to describe anything on \
the basis of its subject matter. Identify the tone the scene sets and \
continue it in the same register, rather than toning it down.
- One paragraph, or a short comma-separated phrase. No headings, no \
bullet points, no preamble, no explanation, no quotation marks around the \
whole thing.
- Aim for 15 to 45 words.
- Write only the background sound description itself. Your entire reply \
is used verbatim.
"""

SOUND_ACCENT_SYSTEM_PROMPT = """\
You rewrite a single shot's scene-background sound accents — additional, \
localized environmental sounds that belong to this one clip, layered on top \
of the project's general Background Sound.

Rules:
- Read the project's general Background Sound and treat it as an exclusion \
list. Do not repeat it, paraphrase it, expand it, or provide a replacement \
for any sound already named there.
- Ground the accents in the project's scene description and this shot's prompt. \
Choose only additional sounds that are plausibly audible in this specific \
location and moment: for example nearby birds in this scene, a distant siren, \
a localized machine hum, a room creak, or a passing vehicle.
- Keep these as background or environmental accents, not foreground action Foley. \
Do not add footsteps, impacts, object handling, switches, splashes, or other \
action sounds unless the text clearly establishes them as distant background \
sound.
- Describe only local, momentary or location-specific environmental sound — not \
the constant ambient bed and not music.
- Do not describe anything visual: no camera moves, no lighting, no \
characters' appearance.
- Do not describe spoken dialogue, or any words being said, in any form — \
dialogue is a separate field and must never appear here, even as a \
suggestion.
- Keep every concrete background or environmental sound the writer specified. \
Do not invent sounds that contradict the shot, but omit foreground action Foley, \
dialogue, music, and any sound already covered by the general Background Sound.
- Do not censor, soften, filter, sanitize, or refuse to describe anything on \
the basis of its subject matter. Identify the tone the shot sets and continue \
it in the same register, rather than toning it down.
- One paragraph, or a short comma-separated phrase. No headings, no bullet \
points, no preamble, no explanation, no quotation marks around the whole \
thing.
- Aim for 10 to 35 words.
- Write only the sound accents themselves. Your entire reply is used \
verbatim.
"""

DIALOGUE_SYSTEM_PROMPT = """\
You rewrite one spoken line for a storyboard character. Preserve the line's \
story purpose, facts, intention, and approximate spoken length, but make the \
word choice, rhythm, syntax, humour, formality, and verbal mannerisms sound \
authentic to the selected speaker.

Rules:
- Research notes may be supplied from a web search. Use them only when they \
clearly describe the same named character. Treat unrelated or ambiguous \
results as irrelevant.
- Extract general speech traits from research; never copy, closely paraphrase, \
or quote an existing line. The result must be original dialogue for this scene.
- Use the character card and shot context as constraints. Do not invent new \
plot facts, actions, relationships, names, or backstory.
- Preserve the meaning of the writer's line unless changing its wording is \
necessary to express the same intent in character.
- Keep it short enough for the supplied shot duration. Natural dialogue is \
roughly two to three words per second.
- Return only the words to be spoken: no speaker name, quotation marks, stage \
directions, delivery notes, explanation, headings, or alternatives.
"""

STILL_PHASES_SYSTEM_PROMPT = """\
You read a single shot's camera/action description and identify what is \
actually visible at the very START of the described action -- the opening \
pose and composition -- for someone who will render that instant as one \
still image.

Rules:
- Use only what the shot description itself establishes — distance, \
position, pose, camera framing, lighting, motion direction. Never invent \
detail it does not support.
- Do not censor, soften, filter, sanitize, or refuse to describe anything on \
the basis of its subject matter. Identify the tone the shot description sets \
and continue it in the same register, rather than toning it down.
- Describe a single frozen instant, fully specifying position, framing and \
pose, not the movement that follows it.
- Never describe dialogue, sound, or anything not visible in a single frame.
- Reply with exactly one short paragraph (one or two sentences), no \
preamble, no extra commentary, in exactly this format:
START: <description>
"""

CHARACTER_IMAGE_SYSTEM_PROMPT = """\
You write compact, production-ready character descriptions for a storyboard to \
video generator, using the supplied portrait and voice reference as evidence.

Rules:
- Describe only stable visible traits useful for recreating the character: age \
range, build, face shape, hair, skin tone, clothing, accessories, posture or \
distinctive marks.
- The voice clip is a reference for this character only. Do not describe the \
scene, setting, action, or camera, and do not transcribe or quote the clip.
- If a name is provided, start with that name followed by a colon.
- Preserve any concrete user-provided details that do not contradict the image.
- Do not censor, soften, filter, sanitize, or refuse to describe anything \
visible on the basis of its subject matter. Describe what is actually there.
- Do not identify real people or copyrighted characters from the image. If the \
user supplied a name, use that name as a label without claiming identity.
- One paragraph. No headings, no bullets, no preamble, no explanation.
- Aim for 35 to 75 words.
- Write only the character description.
"""

# Vision models are more reliable with two short labelled prose sections than
# with a required JSON schema. The parser below also accepts JSON as a
# convenience, but the headings make the contract easy for Qwen to follow.
CHARACTER_EXTRACTION_SYSTEM_PROMPT = """
You are a visual character and environment extraction system for a storyboard video generator. Analyse the supplied image and separate the visible character from everything around them.

Return exactly two sections in this order:
CHARACTER:
<one compact paragraph>
ENVIRONMENT:
<one compact paragraph>

CHARACTER rules:
- Describe only the visible character: apparent age range, face, skin, eyes, hair, facial hair, build, clothing, footwear, accessories, jewellery, glasses, headwear, tattoos, distinguishing marks, current pose, expression, and objects physically worn, held, or directly carried.
- Treat the character as if isolated on a neutral background. The description must remain useful when the character is placed in a different scene.
- Never include the room, background, buildings, furniture, landscape, weather, location, camera, unrelated people, or environmental lighting.
- Do not infer personality, profession, nationality, ethnicity, identity, name, or backstory. Do not speculate about anything not clearly visible.

ENVIRONMENT rules:
- Describe only what is not part of the character: setting, architecture, furniture, background and foreground objects, landscape, weather, lighting, colours, textures, and visible background people or vehicles.
- Do not repeat physical character details. If no meaningful environment is visible, write "No meaningful environment visible.".

Additional rules:
- The voice clip is evidence for this character only. Do not transcribe or describe it.
- Preserve concrete user-provided character details when they do not contradict the image.
- Do not censor, soften, filter, sanitize, or refuse to describe anything visible on the basis of its subject matter. Describe what is actually there.
- If a name is provided, use it only as a label at the start of CHARACTER.
- Do not identify real people or copyrighted characters from the image. A name supplied by the user is only a label.
- Keep each section factual, visually grounded, and suitable for prompting.
- No preamble, explanation, bullets, or markdown fences.
- Aim for 35 to 75 words for CHARACTER and 15 to 60 words for ENVIRONMENT.
"""


def build_user_message(
    text: str,
    *,
    scene: str = "",
    soundscape: str = "",
    context: str = "",
    context_label: str = "",
    previous_shot: dict[str, str] | None = None,
    next_shot: dict[str, str] | None = None,
    characters: list[dict[str, Any]] | None = None,
    reference_images: list[dict[str, str]] | None = None,
    instruction: str = "Rewrite this shot description:",
) -> str:
    """The text to rewrite, plus the context it has to stay consistent with.

    The scene description and cast are given as *context, not as material to
    fold in* — they are already prepended to every shot when the full prompt
    is assembled, so repeating them here would say everything twice.

    *soundscape* is the project's general Background Sound. It is included as
    an explicit exclusion context for shot sound accents, so the rewrite can
    add only sounds that are not already in the general bed.

    *context* is a second, generic slot for whatever else the text being
    rewritten has to stay grounded in but must not repeat — a shot's own
    prompt, say, when rewriting that shot's sound accents. *context_label*
    names what it is; without one a generic label is used.

    *previous_shot*/*next_shot* are the immediately adjacent shots in the
    board, each ``{"title": ..., "prompt": ...}`` when one exists on that
    side. They are continuity references only, same spirit as *scene*: shown
    so the rewrite doesn't contradict where the sequence just was or is about
    to go, never material to fold in or restate.
    """
    blocks = []
    if scene.strip():
        blocks.append(
            "The project's scene description, already applied to every shot. "
            "Do not repeat it; stay consistent with it:\n" + scene.strip()
        )
    if soundscape.strip():
        blocks.append(
            "The project's general Background Sound, already applied as the "
            "shared sound bed. Do not repeat, paraphrase, or replace any of "
            "these sounds when writing shot-specific sound accents:\n"
            + soundscape.strip()
        )
    if context.strip():
        label = context_label or (
            "Context this must stay consistent with, already written "
            "elsewhere and not to be repeated:"
        )
        blocks.append(label + "\n" + context.strip())
    for side, shot in (("previous", previous_shot), ("next", next_shot)):
        prompt = ((shot or {}).get("prompt") or "").strip()
        if not prompt:
            continue
        title = ((shot or {}).get("title") or "").strip()
        heading = title or f"the {side} shot"
        blocks.append(
            f"The {side} shot in this sequence, \"{heading}\" — for continuity "
            "only. Stay consistent with where it leaves off (or is about to "
            "pick up): camera position, character pose/wardrobe, props, and "
            "location. Do not repeat, summarize, or describe its content:\n"
            + prompt
        )
    for ch in characters or []:
        name = (ch.get("name") or "").strip()
        desc = (ch.get("description") or "").strip()
        if name:
            blocks.append(
                f"A character in this shot, already described elsewhere. Refer "
                f"to them as \"{name}\"; do not re-describe them:\n"
                f"{name}: {desc}" if desc else
                f"A character in this shot. Refer to them as \"{name}\"."
            )
    refs = [r for r in (reference_images or []) if (r.get("label") or "").strip()]
    if refs:
        lines = []
        for r in refs:
            role = (r.get("role") or "Reference image").strip()
            label = (r.get("label") or "").strip()
            summary = (r.get("summary") or "").strip()
            tag = (r.get("tag") or "").strip().lstrip("@")
            tag_text = f" @{tag}" if tag else ""
            line = f"- {role}{tag_text}: {label}"
            if summary and summary != label:
                line += f" ({summary})"
            lines.append(line)
        blocks.append(
            "Reference images attached to this shot. Use these as visual "
            "constraints when rewriting, and fold a concise summary of their "
            "visual cues into the prompt. An @tag names the corresponding "
            "attached image; preserve that @tag when it is part of the user's "
            "shot prompt. Do not mention filenames or paths in the rewritten "
            "prompt:\n" + "\n".join(lines)
        )
    blocks.append(instruction + "\n" + text.strip())
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #


class LLMService:
    """Base class for a language model this tool can ask for a rewrite."""

    id: str = "base"
    label: str = "Base"
    model: str = ""
    # Optional bearer token (see _apply_key). Sent only when set, so a service
    # that needs none is called exactly as before.
    api_key: str = ""
    key_source: str = ""        # "settings" | "env" | "file" | ""
    key_env: str = ""           # the apiKeyEnv variable name, if any
    needs_key: bool = False     # requiresKey / apiKeyEnv declared
    supports_key: bool = False  # an HTTP service a token could be sent to

    def health(self) -> tuple[bool, str]:
        return True, ""

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _key_problem(self) -> str:
        if self.needs_key and not self.api_key:
            where = f"set ${self.key_env}, or " if self.key_env else ""
            return (f"{self.label} needs an API key — {where}enter one in "
                    "Settings → Prompt rewriting.")
        return ""

    def _rejected(self, code: int) -> str:
        return (f"{self.label} rejected the API key (HTTP {code}) — check it in "
                "Settings → Prompt rewriting." if self.api_key else
                f"{self.label} requires an API key (HTTP {code}) — enter one in "
                "Settings → Prompt rewriting.")

    def _open(self, req: urllib.request.Request, timeout: float):
        """urlopen, with an auth failure reported as the key problem it is."""
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RuntimeError(self._rejected(exc.code)) from exc
            raise

    def complete(self, system: str, user: str, *, timeout: float = 300.0,
                 max_tokens: int | None = None) -> str:
        raise NotImplementedError

    def complete_with_image(
        self,
        system: str,
        user: str,
        image: Path,
        *,
        timeout: float = 300.0,
    ) -> str:
        raise RuntimeError(
            f"{self.label} is configured for text completion only. Use a "
            "vision-capable Ollama or OpenAI-compatible model for image-based "
            "character descriptions."
        )

    def complete_with_media(
        self, system: str, user: str, *, images: list[Path] | None = None,
        audio: Path | None = None, timeout: float = 300.0,
    ) -> str:
        """Complete with reference media attached to the user message."""
        if audio is not None:
            raise RuntimeError(
                f"{self.label} does not support audio input for character "
                "descriptions. Configure an OpenAI-compatible multimodal service."
            )
        if images:
            return self.complete_with_image(system, user, images[0], timeout=timeout)
        return self.complete(system, user, timeout=timeout)

    def to_json(self) -> dict[str, Any]:
        ok, msg = self.health()
        return {
            "id": self.id,
            "label": self.label,
            "model": self.model,
            "healthy": ok,
            "message": msg,
            # Where the key comes from, never the key itself.
            "supportsKey": self.supports_key,
            "needsKey": self.needs_key,
            "keySource": self.key_source,
            "keyEnv": self.key_env,
        }


class NullLLM(LLMService):
    id = "none"
    label = "None — no prompt rewriting"

    def health(self) -> tuple[bool, str]:
        return False, "No language model is configured for prompt rewriting."

    def complete(self, system: str, user: str, *, timeout: float = 300.0,
                 max_tokens: int | None = None) -> str:
        raise RuntimeError(self.health()[1])


class BrokenLLM(LLMService):
    def __init__(self, sid: str, label: str, why: str):
        self.id, self.label, self._why = sid, label, why

    def health(self) -> tuple[bool, str]:
        return False, self._why

    def complete(self, system: str, user: str, *, timeout: float = 300.0,
                 max_tokens: int | None = None) -> str:
        raise RuntimeError(self._why)


class OllamaLLM(LLMService):
    """Ollama's native chat API.

    Health checks that the *configured model* is actually pulled, not merely
    that the port answers — the same lesson the speech engines taught, where a
    server returning 200 on ``/`` while the capability behind it was
    unconfigured showed a false green light.
    """

    def __init__(self, sid: str, label: str, url: str, model: str,
                 num_ctx: int = 8192):
        self.id = sid
        self.label = label
        self.url = (url or "http://localhost:11434").rstrip("/")
        self.model = model
        self.num_ctx = num_ctx

    def health(self) -> tuple[bool, str]:
        if not self.model:
            return False, f"{self.id}: no model set in {CONFIG_NAME}"
        if self._key_problem():
            return False, self._key_problem()
        try:
            req = urllib.request.Request(f"{self.url}/api/tags", headers=self._headers())
            with urllib.request.urlopen(req, timeout=4) as r:
                doc = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return False, self._rejected(e.code)
            return False, f"Ollama at {self.url} answered HTTP {e.code}"
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            return False, f"cannot reach Ollama at {self.url} ({e})"

        names = {m.get("name", "") for m in doc.get("models") or []}
        if self.model in names:
            return True, ""
        # Ollama reports "name:tag"; accept a bare name that matches one tag.
        if any(n.split(":")[0] == self.model for n in names):
            return True, ""
        return False, (
            f"Ollama at {self.url} does not have {self.model!r}. "
            f"Pull it with: ollama pull {self.model}"
        )

    def complete(self, system: str, user: str, *, timeout: float = 300.0,
                 max_tokens: int | None = None) -> str:
        options = {"temperature": 0.7, "top_p": 0.9, "num_ctx": self.num_ctx}
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                # A rewrite should be a rewrite, not a reinvention.
                "options": options,
                # Qwen3 thinking models otherwise return their reasoning, and
                # the reply here is used verbatim as the prompt.
                "think": False,
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.url}/api/chat", data=body,
            headers=self._headers(),
        )
        with self._open(req, timeout) as r:
            doc = json.loads(r.read().decode())
        return ((doc.get("message") or {}).get("content") or "").strip()

    def complete_with_image(
        self,
        system: str,
        user: str,
        image: Path,
        *,
        timeout: float = 300.0,
    ) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": user,
                        "images": [
                            base64.b64encode(image.read_bytes()).decode("ascii")
                        ],
                    },
                ],
                "stream": False,
                "options": {"temperature": 0.4, "top_p": 0.9,
                            "num_ctx": self.num_ctx},
                "think": False,
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.url}/api/chat", data=body,
            headers=self._headers(),
        )
        with self._open(req, timeout) as r:
            doc = json.loads(r.read().decode())
        return ((doc.get("message") or {}).get("content") or "").strip()

    def complete_with_media(
        self, system: str, user: str, *, images: list[Path] | None = None,
        audio: Path | None = None, timeout: float = 300.0,
    ) -> str:
        if audio is not None:
            raise RuntimeError(
                f"{self.label} cannot receive voice clips. Configure an "
                "OpenAI-compatible multimodal service for character rewrite."
            )
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user, "images": [
                    base64.b64encode(p.read_bytes()).decode("ascii")
                    for p in (images or [])
                ]},
            ],
            "stream": False,
            "options": {"temperature": 0.4, "top_p": 0.9,
                        "num_ctx": self.num_ctx},
            "think": False,
        }).encode()
        req = urllib.request.Request(
            f"{self.url}/api/chat", data=body,
            headers=self._headers(),
        )
        with self._open(req, timeout) as r:
            doc = json.loads(r.read().decode())
        return ((doc.get("message") or {}).get("content") or "").strip()


class OpenAICompatLLM(LLMService):
    """Anything exposing ``/v1/chat/completions`` — llama.cpp, vLLM, LM Studio."""

    def __init__(self, sid: str, label: str, url: str, model: str):
        self.id = sid
        self.label = label
        self.url = (url or "").rstrip("/")
        self.model = model

    def health(self) -> tuple[bool, str]:
        if not self.url:
            return False, f"{self.id}: no url set in {CONFIG_NAME}"
        if not self.model:
            return False, f"{self.id}: no model set in {CONFIG_NAME}"
        if self._key_problem():
            return False, self._key_problem()
        try:
            req = urllib.request.Request(f"{self.url}/v1/models",
                                         headers=self._headers())
            with urllib.request.urlopen(req, timeout=4) as r:
                doc = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return False, self._rejected(e.code)
            return False, f"{self.url} answered HTTP {e.code}"
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            return False, f"cannot reach {self.url} ({e})"
        ids = {m.get("id") for m in doc.get("data") or []}
        if ids and self.model not in ids:
            return False, f"{self.url} does not serve {self.model!r}"
        return True, ""

    def complete(self, system: str, user: str, *, timeout: float = 300.0,
                 max_tokens: int | None = None) -> str:
        body_dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.7,
            "stream": False,
            # Qwen3's hybrid thinking mode otherwise runs in full before the
            # actual reply -- burning thousands of unseen tokens and, on a
            # local model, minutes -- exactly what OllamaLLM's "think": False
            # heads off for that backend. This is the vLLM/SGLang/llama.cpp
            # server convention for the same switch; a server that doesn't
            # recognize it ignores the unknown field.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if max_tokens is not None:
            body_dict["max_tokens"] = max_tokens
        body = json.dumps(body_dict).encode()
        req = urllib.request.Request(f"{self.url}/v1/chat/completions",
                                     data=body, headers=self._headers())
        with self._open(req, timeout) as r:
            doc = json.loads(r.read().decode())
        choices = doc.get("choices") or [{}]
        return ((choices[0].get("message") or {}).get("content") or "").strip()

    def complete_with_image(
        self,
        system: str,
        user: str,
        image: Path,
        *,
        timeout: float = 300.0,
    ) -> str:
        mime = mimetypes.guess_type(str(image))[0] or "application/octet-stream"
        encoded = base64.b64encode(image.read_bytes()).decode("ascii")
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime};base64,{encoded}"
                                },
                            },
                        ],
                    },
                ],
                "temperature": 0.4,
                "stream": False,
                # Same thinking-off switch as complete(): without it a Qwen3
                # hybrid model reasons at length before every description.
                "chat_template_kwargs": {"enable_thinking": False},
            }
        ).encode()
        req = urllib.request.Request(f"{self.url}/v1/chat/completions",
                                     data=body, headers=self._headers())
        with self._open(req, timeout) as r:
            doc = json.loads(r.read().decode())
        choices = doc.get("choices") or [{}]
        return ((choices[0].get("message") or {}).get("content") or "").strip()

    def complete_with_media(
        self, system: str, user: str, *, images: list[Path] | None = None,
        audio: Path | None = None, timeout: float = 300.0,
    ) -> str:
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        for image in images or []:
            mime = mimetypes.guess_type(str(image))[0] or "application/octet-stream"
            encoded = base64.b64encode(image.read_bytes()).decode("ascii")
            content.append({"type": "image_url", "image_url": {
                "url": f"data:{mime};base64,{encoded}"
            }})
        if audio is not None:
            mime = mimetypes.guess_type(str(audio))[0] or "audio/wav"
            fmt = mime.split("/", 1)[-1].split(";", 1)[0]
            if fmt == "mpeg":
                fmt = "mp3"
            content.append({"type": "input_audio", "input_audio": {
                "data": base64.b64encode(audio.read_bytes()).decode("ascii"),
                "format": fmt,
            }})
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": content}],
            "temperature": 0.4, "stream": False,
            # Every rewrite goes through here (with or without media), so
            # this is the switch that keeps a Qwen3 model from thinking first.
            "chat_template_kwargs": {"enable_thinking": False},
        }).encode()
        req = urllib.request.Request(f"{self.url}/v1/chat/completions",
                                     data=body, headers=self._headers())
        try:
            with self._open(req, timeout) as r:
                doc = json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            # LM Studio and several otherwise OpenAI-compatible servers accept
            # image_url blocks but not OpenAI's input_audio block. Character
            # cards commonly have both a portrait and a voice attached, so an
            # unsupported voice must not prevent the portrait description.
            # Retry only for the server's explicit media-type rejection; other
            # 400s still surface instead of being disguised by a second call.
            detail = exc.read().decode("utf-8", "replace")
            unsupported_audio = (
                audio is not None
                and exc.code == 400
                and (
                    "input_audio" in detail
                    or (
                        "content" in detail
                        and "text" in detail
                        and "image_url" in detail
                        and "type" in detail
                    )
                )
            )
            if unsupported_audio:
                return self.complete_with_media(
                    system, user, images=images, audio=None, timeout=timeout
                )
            raise RuntimeError(
                f"{self.label} rejected the multimodal request "
                f"(HTTP {exc.code}): {detail or exc.reason}"
            ) from exc
        choices = doc.get("choices") or [{}]
        return ((choices[0].get("message") or {}).get("content") or "").strip()


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def load_config(project_root: Path) -> list[dict[str, Any]]:
    path = Path(project_root) / CONFIG_NAME
    if not path.exists():
        path.write_text(
            json.dumps({"services": DEFAULT_SERVICES}, indent=2) + "\n"
        )
        return list(DEFAULT_SERVICES)
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return [{"id": "config-error", "kind": "broken",
                 "label": f"unreadable {CONFIG_NAME}", "why": str(e)}]
    services = doc.get("services")
    return services if isinstance(services, list) else list(DEFAULT_SERVICES)


# Machine-local settings (gitignored); "llmKeys" maps a service id to a key
# entered in Settings, so a key never has to live in the tracked config.
LOCAL_SETTINGS_NAME = "server-config.json"


def local_keys(project_root: Path) -> dict[str, str]:
    path = Path(project_root) / LOCAL_SETTINGS_NAME
    try:
        keys = json.loads(path.read_text()).get("llmKeys")
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}
    return {str(k): str(v) for k, v in keys.items() if v} if isinstance(keys, dict) else {}


def _apply_key(svc: LLMService, entry: dict[str, Any], saved: str) -> LLMService:
    """Settings key, else $apiKeyEnv, else an inline apiKey, else none.

    A service only *needs* one when its entry says so (requiresKey, or an
    apiKeyEnv to read it from); otherwise no key means none is sent.
    """
    env_name = str(entry.get("apiKeyEnv") or "")
    svc.supports_key = True
    svc.key_env = env_name
    svc.needs_key = bool(entry.get("requiresKey")) or bool(env_name)
    if saved:
        svc.api_key, svc.key_source = saved, "settings"
    elif env_name and os.environ.get(env_name):
        svc.api_key, svc.key_source = os.environ[env_name], "env"
    elif entry.get("apiKey"):
        svc.api_key, svc.key_source = str(entry["apiKey"]), "file"
    return svc


def build_one(entry: dict[str, Any], saved_key: str = "") -> LLMService:
    kind = entry.get("kind") or entry.get("id")
    sid = str(entry.get("id") or kind or "unnamed")
    label = str(entry.get("label") or sid)
    url = str(entry.get("url") or "")
    model = str(entry.get("model") or "")

    if kind == "ollama":
        return _apply_key(OllamaLLM(sid, label, url, model,
                                    num_ctx=int(entry.get("numCtx") or 8192)),
                          entry, saved_key)
    if kind in ("openai", "openai-compatible"):
        return _apply_key(OpenAICompatLLM(sid, label, url, model), entry, saved_key)
    if kind == "broken":
        return BrokenLLM(sid, label, str(entry.get("why") or "misconfigured"))
    return BrokenLLM(
        sid, label,
        f"unknown service kind {kind!r} in {CONFIG_NAME} "
        "(expected ollama or openai)",
    )


def load_services(project_root: Path) -> dict[str, LLMService]:
    services: dict[str, LLMService] = {}
    saved = local_keys(project_root)
    for entry in load_config(project_root):
        s = build_one(entry, saved.get(str(entry.get("id") or "")) or "")
        services[s.id] = s
    services["none"] = NullLLM()
    return services


# --------------------------------------------------------------------------- #
# The rewrite itself
# --------------------------------------------------------------------------- #


#: What is being rewritten, and how to talk about it — the system prompt to
#: use, the label for error/instruction text, and the instruction line handed
#: to the model as the last block of `build_user_message`.
_REWRITE_KINDS: dict[str, dict[str, str]] = {
    "shot": {
        "system": SYSTEM_PROMPT,
        "label": "shot prompt",
        "instruction": "Rewrite this shot description:",
    },
    "still": {
        "system": STILL_SYSTEM_PROMPT,
        "label": "shot prompt",
        "instruction": "Rewrite this shot description:",
    },
    "scene": {
        "system": SCENE_SYSTEM_PROMPT,
        "label": "scene description",
        "instruction": "Rewrite this project's scene description:",
    },
    "soundscape": {
        "system": SOUNDSCAPE_SYSTEM_PROMPT,
        "label": "background sound",
        "instruction": "Rewrite this project's background sound:",
    },
    "soundNote": {
        "system": SOUND_ACCENT_SYSTEM_PROMPT,
        "label": "sound accents",
        "instruction": "Rewrite this shot's sound accents:",
    },
}


def rewrite_prompt(
    service: LLMService,
    text: str,
    *,
    scene: str = "",
    soundscape: str = "",
    context: str = "",
    context_label: str = "",
    previous_shot: dict[str, str] | None = None,
    next_shot: dict[str, str] | None = None,
    characters: list[dict[str, Any]] | None = None,
    reference_images: list[dict[str, str]] | None = None,
    reference_files: list[Path] | None = None,
    kind: str = "shot",
) -> str:
    """Ask *service* to restyle *text*. Returns the proposal, never applies it.

    *kind* picks both the system prompt and how the text is described to the
    model and in error messages — "shot" or "still" for a per-shot prompt,
    "scene" for the project's scene description, "soundscape" for its
    background sound bed, "soundNote" for one shot's own sound accents.

    *previous_shot*/*next_shot* are only meaningful for "shot"/"still" — see
    build_user_message.
    """
    spec = _REWRITE_KINDS.get(kind, _REWRITE_KINDS["shot"])
    text = (text or "").strip()
    if not text:
        raise ValueError(f"There is nothing in the {spec['label']} to rewrite yet.")

    ok, msg = service.health()
    if not ok:
        raise RuntimeError(msg)

    out = service.complete_with_media(
        spec["system"],
        build_user_message(
            text,
            scene=scene,
            soundscape=soundscape,
            context=context,
            context_label=context_label,
            previous_shot=previous_shot,
            next_shot=next_shot,
            characters=characters,
            reference_images=reference_images,
            instruction=spec["instruction"],
        ),
        images=reference_files,
    )
    out = _strip_wrapping(out)
    if not out:
        raise RuntimeError(
            f"{service.label} returned an empty rewrite. It may have run out "
            "of context, or be a model that only emits reasoning."
        )
    return out


def rewrite_dialogue(
    service: LLMService,
    text: str,
    *,
    speaker: dict[str, Any],
    shot_prompt: str = "",
    scene: str = "",
    dialogue_style: str = "",
    duration_seconds: float | None = None,
    research: str = "",
) -> str:
    """Rewrite one line in its selected speaker's voice, as a proposal."""
    text = (text or "").strip()
    if not text:
        raise ValueError("There is nothing in the dialogue to rewrite yet.")
    name = str(speaker.get("name") or "").strip()
    if not name:
        raise ValueError("Choose a speaking character before rewriting dialogue.")
    ok, msg = service.health()
    if not ok:
        raise RuntimeError(msg)

    blocks = [
        f"SELECTED SPEAKER: {name}",
        "CHARACTER CARD:\n" + (
            str(speaker.get("description") or "").strip()
            or "No character description is available."
        ),
    ]
    if scene.strip():
        blocks.append("PROJECT SCENE:\n" + scene.strip())
    if shot_prompt.strip():
        blocks.append("THIS SHOT:\n" + shot_prompt.strip())
    if dialogue_style.strip():
        blocks.append("DELIVERY DIRECTION:\n" + dialogue_style.strip())
    if duration_seconds:
        blocks.append(f"SHOT DURATION: {duration_seconds:.2f} seconds")
    blocks.append(
        "WEB RESEARCH RESULTS:\n" + (
            research.strip()
            or "No reliable external result was found; rely on the character card."
        )
    )
    blocks.append("DIALOGUE TO REWRITE:\n" + text)

    out = _strip_wrapping(service.complete(
        DIALOGUE_SYSTEM_PROMPT, "\n\n".join(blocks), timeout=300.0
    ))
    # Models occasionally retain the requested quotation marks despite the
    # output rule. Only strip a single pair enclosing the whole response.
    if len(out) >= 2 and out[0] == out[-1] and out[0] in {'"', "'"}:
        out = out[1:-1].strip()
    if not out:
        raise RuntimeError(f"{service.label} returned an empty dialogue rewrite.")
    return out


def describe_character(
    service: LLMService,
    image: Path,
    *,
    voice: Path | None = None,
    name: str = "",
    current: str = "",
) -> str:
    """Ask *service* to describe a character from a reference image."""
    ok, msg = service.health()
    if not ok:
        raise RuntimeError(msg)
    if not image.is_file():
        raise FileNotFoundError(f"no such reference image: {image}")
    if voice is not None and not voice.is_file():
        raise FileNotFoundError(f"no such reference voice: {voice}")

    blocks = [
        "Write or improve the character description from the attached portrait "
        "and voice clip. Describe the character, never the scene."
    ]
    if name.strip():
        blocks.append(f"Character label: {name.strip()}")
    if current.strip():
        blocks.append(
            "Current description to preserve where accurate:\n" + current.strip()
        )

    out = service.complete_with_media(
        CHARACTER_EXTRACTION_SYSTEM_PROMPT,
        "\n\n".join(blocks),
        images=[image],
        audio=voice,
    )
    result = _parse_character_result(out)
    if not result["character"]:
        raise RuntimeError(
            f"{service.label} returned an empty character description."
        )
    return result


def _parse_character_result(text: str) -> dict[str, str]:
    """Split a character/environment extraction, with legacy fallback."""
    raw = _strip_wrapping(text)
    if not raw:
        return {"character": "", "environment": ""}

    candidates = [raw]
    if "{" in raw and "}" in raw:
        start, end = raw.find("{"), raw.rfind("}")
        if start < end:
            candidates.append(raw[start:end + 1])
    for candidate in candidates:
        try:
            doc = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        character = doc.get("character") or doc.get("CHARACTER") or doc.get("Character") or ""
        environment = doc.get("environment") or doc.get("ENVIRONMENT") or doc.get("Environment") or ""
        if isinstance(character, dict):
            character = character.get("summary") or ""
        if isinstance(environment, dict):
            environment = environment.get("summary") or ""
        return {
            "character": _strip_wrapping(str(character)),
            "environment": _strip_wrapping(str(environment)),
        }

    matches = list(re.finditer(
        r"(?im)^\s*(?:[*#]+\s*)?(CHARACTER|ENVIRONMENT)\s*:\s*", raw
    ))
    char_match = next((m for m in matches if m.group(1).upper() == "CHARACTER"), None)
    env_match = next((m for m in matches if m.group(1).upper() == "ENVIRONMENT"), None)
    if char_match and env_match and char_match.start() < env_match.start():
        return {
            "character": _strip_wrapping(raw[char_match.end():env_match.start()]),
            "environment": _strip_wrapping(raw[env_match.end():]),
        }

    # Older or non-compliant models may still return one plain paragraph.
    return {"character": raw, "environment": ""}


# A model that still answers with an END: line has it cut off here.
_STILL_PHASE_RE = re.compile(r"START:\s*(?P<start>.+?)\s*(?:\bEND:|$)", re.S | re.I)


def describe_still_phases(service: LLMService, shot_prompt: str) -> dict[str, str]:
    """A short visual description of the opening instant *shot_prompt*
    establishes, for "Create Stills" to render as its one still -- a model
    has no sense of time, so "the start of this shot" alone means nothing.

    Best-effort: an unconfigured/unhealthy service, a network error, or a
    reply that does not parse all come back as an empty dict rather than a
    raised error, since the caller's fallback (a generic phase label glued
    onto the shot's own prompt) is a fine second choice, not a failure.
    """
    text = (shot_prompt or "").strip()
    if not text:
        return {}
    try:
        ok, _msg = service.health()
        if not ok:
            return {}
        raw = service.complete(STILL_PHASES_SYSTEM_PROMPT, text, timeout=45.0)
    except Exception:  # noqa: BLE001 - a missing phase description is fine
        return {}
    m = _STILL_PHASE_RE.search(raw or "")
    if not m:
        return {}
    start = m.group("start").strip()
    return {"start": start} if start else {}


def _strip_wrapping(text: str) -> str:
    """Undo the packaging a chat model adds even when told not to.

    Small local models reliably do two of these: wrap the answer in quotes, or
    lead with "Here is the rewritten prompt:". Both would end up inside the
    prompt that reaches the video model.
    """
    t = (text or "").strip()

    # Thinking models that ignore think:false leave the reasoning inline.
    if "</think>" in t:
        t = t.rsplit("</think>", 1)[1].strip()

    # A fenced block, sometimes with a language tag.
    if t.startswith("```"):
        parts = t.split("```")
        if len(parts) >= 3:
            t = parts[1]
            if "\n" in t:
                first, rest = t.split("\n", 1)
                if len(first.split()) <= 1:
                    t = rest
            t = t.strip()

    for lead in (
        "here is the rewritten prompt:", "here's the rewritten prompt:",
        "rewritten prompt:", "here is the prompt:", "here's the prompt:",
        "prompt:",
    ):
        if t.lower().startswith(lead):
            t = t[len(lead):].strip()
            break

    if len(t) >= 2 and t[0] in "\"“'" and t[-1] in "\"”'":
        t = t[1:-1].strip()

    return t
