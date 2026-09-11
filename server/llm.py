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

# The house style, taken from MiniMax H3's own shipped examples and from what
# actually changed the output on this workspace's runs: the camera move named
# up front rather than buried, concrete material detail over adjectives, and
# the sound as one trailing clause because H3 denoises audio and picture
# together and its examples put it there.
SYSTEM_PROMPT = """\
You rewrite shot descriptions into prompts for MiniMax H3, a text-to-video \
model that generates picture and sound together in one pass.

Follow this order, as one flowing paragraph, not a list:
1. The camera move, named explicitly and first (e.g. "a low, fast tracking \
shot", "a slow push in", "a locked-off wide").
2. The subject and what it does, in the present tense.
3. The environment and the light, with concrete physical detail — materials, \
wear, texture, the time of day. Prefer "battered, weathered metal plating" \
over "cool-looking ship".
4. A final clause for the sound: what is heard, layered, separated by commas.

Rules:
- Keep every concrete thing the writer specified. Do not invent new subjects, \
characters, locations or story beats, and do not remove any they named.
- When the camera and the subject move at the same time, give each its own \
short clause rather than one blended sentence, and state the subject's \
direction of travel in its own frame of reference (e.g. "continues forward, \
accelerating away") rather than only relative to the camera. A camera that \
rises and swings around behind a subject, described in the same breath as \
the subject "moving away", is the kind of sentence this model tends to \
resolve by reversing the subject instead — say what the camera does, then \
say what the subject does, in that order.
- If reference images are listed in the context, treat them as visual \
constraints. Add a compact natural-language summary of the relevant reference \
cues to the rewritten prompt, especially location, framing, lighting, material \
and character identity. Do not include filenames or paths in the final prompt.
- Refer to named characters by exactly the name the writer used.
- One paragraph. No headings, no bullet points, no preamble, no explanation, \
no quotation marks around the whole thing.
- Aim for 60 to 110 words. Longer prompts dilute the conditioning.
- Do not mention aspect ratio, resolution, frame count, steps, seeds or file \
formats. Those are set elsewhere.
- Write only the prompt itself. Your entire reply is used verbatim as the \
prompt.
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

CHARACTER_IMAGE_SYSTEM_PROMPT = """\
You write compact, production-ready character descriptions for a storyboard to \
video generator, using the supplied reference image as visual evidence.

Rules:
- Describe only stable visible traits useful for recreating the character: age \
range, build, face shape, hair, skin tone, clothing, accessories, posture or \
distinctive marks.
- If a name is provided, start with that name followed by a colon.
- Preserve any concrete user-provided details that do not contradict the image.
- Do not identify real people or copyrighted characters from the image. If the \
user supplied a name, use that name as a label without claiming identity.
- One paragraph. No headings, no bullets, no preamble, no explanation.
- Aim for 35 to 75 words.
- Write only the character description.
"""


def build_user_message(
    text: str,
    *,
    scene: str = "",
    characters: list[dict[str, Any]] | None = None,
    reference_images: list[dict[str, str]] | None = None,
) -> str:
    """The shot to rewrite, plus the context it has to stay consistent with.

    The scene description and cast are given as *context, not as material to
    fold in* — they are already prepended to every shot when the full prompt
    is assembled, so repeating them here would say everything twice.
    """
    blocks = []
    if scene.strip():
        blocks.append(
            "The project's scene description, already applied to every shot. "
            "Do not repeat it; stay consistent with it:\n" + scene.strip()
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
            line = f"- {role}: {label}"
            if summary and summary != label:
                line += f" ({summary})"
            lines.append(line)
        blocks.append(
            "Reference images attached to this shot. Use these as visual "
            "constraints when rewriting, and fold a concise summary of their "
            "visual cues into the prompt. Do not mention filenames or paths in "
            "the rewritten prompt:\n" + "\n".join(lines)
        )
    blocks.append("Rewrite this shot description:\n" + text.strip())
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #


class LLMService:
    """Base class for a language model this tool can ask for a rewrite."""

    id: str = "base"
    label: str = "Base"
    model: str = ""

    def health(self) -> tuple[bool, str]:
        return True, ""

    def complete(self, system: str, user: str, *, timeout: float = 120.0) -> str:
        raise NotImplementedError

    def complete_with_image(
        self,
        system: str,
        user: str,
        image: Path,
        *,
        timeout: float = 120.0,
    ) -> str:
        raise RuntimeError(
            f"{self.label} is configured for text completion only. Use a "
            "vision-capable Ollama or OpenAI-compatible model for image-based "
            "character descriptions."
        )

    def to_json(self) -> dict[str, Any]:
        ok, msg = self.health()
        return {
            "id": self.id,
            "label": self.label,
            "model": self.model,
            "healthy": ok,
            "message": msg,
        }


class NullLLM(LLMService):
    id = "none"
    label = "None — no prompt rewriting"

    def health(self) -> tuple[bool, str]:
        return False, "No language model is configured for prompt rewriting."

    def complete(self, system: str, user: str, *, timeout: float = 120.0) -> str:
        raise RuntimeError(self.health()[1])


class BrokenLLM(LLMService):
    def __init__(self, sid: str, label: str, why: str):
        self.id, self.label, self._why = sid, label, why

    def health(self) -> tuple[bool, str]:
        return False, self._why

    def complete(self, system: str, user: str, *, timeout: float = 120.0) -> str:
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
        try:
            with urllib.request.urlopen(f"{self.url}/api/tags", timeout=4) as r:
                doc = json.loads(r.read().decode())
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

    def complete(self, system: str, user: str, *, timeout: float = 120.0) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                # A rewrite should be a rewrite, not a reinvention.
                "options": {"temperature": 0.7, "top_p": 0.9,
                            "num_ctx": self.num_ctx},
                # Qwen3 thinking models otherwise return their reasoning, and
                # the reply here is used verbatim as the prompt.
                "think": False,
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.url}/api/chat", data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.loads(r.read().decode())
        return ((doc.get("message") or {}).get("content") or "").strip()

    def complete_with_image(
        self,
        system: str,
        user: str,
        image: Path,
        *,
        timeout: float = 120.0,
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
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.loads(r.read().decode())
        return ((doc.get("message") or {}).get("content") or "").strip()


class OpenAICompatLLM(LLMService):
    """Anything exposing ``/v1/chat/completions`` — llama.cpp, vLLM, LM Studio."""

    def __init__(self, sid: str, label: str, url: str, model: str,
                 api_key: str = ""):
        self.id = sid
        self.label = label
        self.url = (url or "").rstrip("/")
        self.model = model
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def health(self) -> tuple[bool, str]:
        if not self.url:
            return False, f"{self.id}: no url set in {CONFIG_NAME}"
        if not self.model:
            return False, f"{self.id}: no model set in {CONFIG_NAME}"
        try:
            req = urllib.request.Request(f"{self.url}/v1/models",
                                         headers=self._headers())
            with urllib.request.urlopen(req, timeout=4) as r:
                doc = json.loads(r.read().decode())
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            return False, f"cannot reach {self.url} ({e})"
        ids = {m.get("id") for m in doc.get("data") or []}
        if ids and self.model not in ids:
            return False, f"{self.url} does not serve {self.model!r}"
        return True, ""

    def complete(self, system: str, user: str, *, timeout: float = 120.0) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.7,
                "stream": False,
            }
        ).encode()
        req = urllib.request.Request(f"{self.url}/v1/chat/completions",
                                     data=body, headers=self._headers())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.loads(r.read().decode())
        choices = doc.get("choices") or [{}]
        return ((choices[0].get("message") or {}).get("content") or "").strip()

    def complete_with_image(
        self,
        system: str,
        user: str,
        image: Path,
        *,
        timeout: float = 120.0,
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
            }
        ).encode()
        req = urllib.request.Request(f"{self.url}/v1/chat/completions",
                                     data=body, headers=self._headers())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.loads(r.read().decode())
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


def build_one(entry: dict[str, Any]) -> LLMService:
    kind = entry.get("kind") or entry.get("id")
    sid = str(entry.get("id") or kind or "unnamed")
    label = str(entry.get("label") or sid)
    url = str(entry.get("url") or "")
    model = str(entry.get("model") or "")

    if kind == "ollama":
        return OllamaLLM(sid, label, url, model,
                         num_ctx=int(entry.get("numCtx") or 8192))
    if kind in ("openai", "openai-compatible"):
        # apiKeyEnv names an environment variable to read the key from, so a
        # remote service can be used without a secret sitting in a tracked
        # config file. An inline apiKey still works for throwaway local setups.
        key = str(entry.get("apiKey") or "")
        env_name = str(entry.get("apiKeyEnv") or "")
        if env_name:
            key = os.environ.get(env_name, "")
        return OpenAICompatLLM(sid, label, url, model, api_key=key)
    if kind == "broken":
        return BrokenLLM(sid, label, str(entry.get("why") or "misconfigured"))
    return BrokenLLM(
        sid, label,
        f"unknown service kind {kind!r} in {CONFIG_NAME} "
        "(expected ollama or openai)",
    )


def load_services(project_root: Path) -> dict[str, LLMService]:
    services: dict[str, LLMService] = {}
    for entry in load_config(project_root):
        s = build_one(entry)
        services[s.id] = s
    services["none"] = NullLLM()
    return services


# --------------------------------------------------------------------------- #
# The rewrite itself
# --------------------------------------------------------------------------- #


def rewrite_prompt(
    service: LLMService,
    text: str,
    *,
    scene: str = "",
    characters: list[dict[str, Any]] | None = None,
    reference_images: list[dict[str, str]] | None = None,
    still: bool = False,
) -> str:
    """Ask *service* to restyle *text*. Returns the proposal, never applies it."""
    text = (text or "").strip()
    if not text:
        raise ValueError("There is nothing in the shot prompt to rewrite yet.")

    ok, msg = service.health()
    if not ok:
        raise RuntimeError(msg)

    out = service.complete(
        STILL_SYSTEM_PROMPT if still else SYSTEM_PROMPT,
        build_user_message(
            text,
            scene=scene,
            characters=characters,
            reference_images=reference_images,
        ),
    )
    out = _strip_wrapping(out)
    if not out:
        raise RuntimeError(
            f"{service.label} returned an empty rewrite. It may have run out "
            "of context, or be a model that only emits reasoning."
        )
    return out


def describe_character(
    service: LLMService,
    image: Path,
    *,
    name: str = "",
    current: str = "",
) -> str:
    """Ask *service* to describe a character from a reference image."""
    ok, msg = service.health()
    if not ok:
        raise RuntimeError(msg)
    if not image.is_file():
        raise FileNotFoundError(f"no such reference image: {image}")

    blocks = [
        "Write or improve the character description from the attached image."
    ]
    if name.strip():
        blocks.append(f"Character label: {name.strip()}")
    if current.strip():
        blocks.append(
            "Current description to preserve where accurate:\n" + current.strip()
        )

    out = service.complete_with_image(
        CHARACTER_IMAGE_SYSTEM_PROMPT,
        "\n\n".join(blocks),
        image,
    )
    out = _strip_wrapping(out)
    if not out:
        raise RuntimeError(
            f"{service.label} returned an empty character description."
        )
    return out


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
