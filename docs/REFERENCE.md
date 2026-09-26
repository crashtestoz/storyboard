# How Storyboard works

What each part of the app does and why it was built that way — for contributors and the curious. Setup is in the [README](../README.md) and [Advanced setup](ADVANCED-SETUP.md).

## What it does

**Two-tier prompting.** A project-level scene description (subject and style,
true of every shot) plus a per-shot prompt (action, camera, mood). The editor
shows the assembled result, so nothing about the composition is hidden.

**Style references and the media library.** Style reference images are
project-wide and are sent to vpipe's `video-ref-encoder` with each shot's own
references and selected Cast media when the shot uses Ref2VA. A shot with a
Start frame or End frame automatically uses FL2VA, whose images are hard
first/last-frame anchors; separate Cast, style and shot-reference images are
not sent on that path. With no anchors, Ref2VA carries that reference set and
the model's nine-image/three-audio limits are enforced before rendering.

The image picker scans project `refs/` folders and rendered stills, groups
exact duplicate images by content hash, and shows where used files are
referenced: scene start/end frames, scene character refs, cast refs, or global
style refs. Used images are faded and protected from deletion. Unused images,
including unused duplicate copies, show a trash button.

**Sound in two layers.** A project-wide background bed and per-shot accents.
The background bed can be rendered into every shot for quick all-in-one clips,
or held out of shot renders so music/ambience can be added later as one
continuous final mix. Per-shot accents still render with each clip: local
effects like switches, impacts, footsteps or spray. Sound cues are appended
after the visual description because MiniMax H3 generates its soundtrack in the
same denoise loop as the picture and its own examples put sound in a trailing
clause. Dropped automatically for a still.

**A cast.** Characters have a required name and description, and optional
reference image and voice clip. An attached clip shows its name behind a ♪ and
a player, so it can be heard without leaving the dialog — and both slots pick
from what is already in your projects as well as offering an upload. Audio used
to jump straight to a file dialog, which meant a clip already uploaded into a
project's `refs/` could never be attached to anyone: you could only upload it a
second time. A shot casts whoever appears in it and refers
to them by name in its prompt. On Ref2VA the portrait becomes an image
reference and the voice clip a soundtrack reference, respecting that model's
real limits (9 images, 3 soundtracks, 12 total).

**Start/end references and chaining.** A Start frame or End frame automatically
switches the shot to FL2VA and wires the supplied image directly to the
corresponding first/last-frame input. “Chain start frame from the previous
shot” resolves the previous shot's last saved frame before that FL2VA request
is generated. Remove both anchors when the shot should use Ref2VA's ordered
character, style, object and environment reference set instead.

**Transcription is its own capability.** Cloning a voice and recognising
speech are separate, and one does not imply the other — MOSS clones a voice and
has no speech recognition at all. So engines declare `supports_transcription`,
the Transcribe button reports on whichever configured service actually has it
(naming it in the tooltip), and `/api/transcribe` uses the engine you asked for
when it can and otherwise finds one that can, saying which it used.

**Dialogue, in the character's own voice.** **Generate** synthesises the shot's
line using the speaking character's reference clip and transcript, so what you
hear is their cloned voice, not a generic one. Who speaks is inferred when a
shot has one character in it and chosen from a list when it has several,
preferring whoever actually has a clip.

The Dialogue tab also has **Voice direction** for delivery notes such as
`tired`, `quiet`, `breathy`, `urgent whisper`, or `slight smile`. Those notes
are sent separately to compatible speech engines and are not spoken aloud. If
the direction changes after a take was generated, the app treats the existing
spoken audio as stale and asks you to speak the line again.

For `qwen3-clone`, the adapter sends the shot dialogue as the text to speak
and sends voice direction through Qwen's instruction field. You can paste a
full Qwen-style block in **Voice direction**; the app keeps the delivery
section and ignores the `[TEXT TO SPEAK]` section because the shot dialogue is
already the source of truth:

```text
[STYLE / VOICE DIRECTION]

...

[TEXT TO SPEAK]

...

[END]
```

It works **before the shot is rendered**. Synthesis and muxing are separate
functions for that reason: finding out after half an hour of video that a
cloned voice reads the line wrong is exactly the wrong order. You get a player
next to the line immediately. The line also reaches the video render as a
visual cue — "speaks the line with natural jaw and lip movement" — so the shot
prompt can stay about action and framing instead of carrying spoken words.
When the clip exists, the TTS speech is mixed over it while the generated audio
is explicitly asked to stay ambient only: no spoken words, no voice and no
intelligible dialogue.

Dubbing separately from rendering is deliberate in the other direction too: a
line can be rewritten and re-spoken in seconds without touching a clip that
took half an hour, and nothing in H3's documentation claims intelligible
phoneme-accurate lip-synced speech from written text.

**Every take is trimmed and levelled.** Both of these are model behaviour, not
integration bugs, and both were measured here rather than assumed. MOSS 8B does
not stop when it has finished a line — it emits silent frames until it runs out
of token budget, and one take arrived 79.4 seconds long with the speech ending
at 5.2s. The same take peaked at −23 dBFS, far too quiet to sit under a
generated soundtrack. So silence is trimmed off *both ends* (never from the
middle — those are the pauses between words, and removing them makes the
delivery unnatural) and the result is normalised to −16 LUFS with a −1.5 dB
true-peak ceiling. That happens in `speak_line`, so no engine added later can
quietly skip it. MOSS also gets a text-proportional token budget, which stops
it spending most of its runtime generating nothing.

**The engine says what it did.** A cloned voice that comes back wrong is nearly
always the reference clip or its transcript, and neither is visible from a
waveform. So each take writes a `speech.log` beside the audio — the reference
used, the transcript (and whether it had to be derived), the text, the
response, what was trimmed, what the level was set to — and it appears in the
same **Backend output** window as the render, after it, under its own header.
One window because they are both "what the machine did on this shot"; separate
containers inside it so each can be filled independently and the render poll's
append only ever touches its own lines.

**Open, not import.** The header's **Open** lists every storyboard on disk
with what each folder actually holds — shot count, how many are rendered, and
the full path to its `storyboard.json` — and opening one edits that file in
place. This replaces an Import button that was the wrong verb: picking your own
board's `storyboard.json` made a *second* project from it, carrying the shot
list but none of the renders, with its references still pointing back into the
original folder. Copying a board in from elsewhere is still available in that
dialog, and now says what it does — a browser hands over a file's contents, not
its location, so it cannot open one in place.

The rendered count is on every row because two projects can carry the same
name, and telling them apart is exactly what you need at that moment.

**Renaming a project.** A project's name and its folder move together. The
folder name is the slug, and the slug is baked into every path the board has
recorded — thumbnails, run logs, outputs, cast portraits — so renaming the
display name alone would leave the folder saying something else, and moving the
folder alone would break all of those. Rename does both and rewrites the paths,
walking the whole board rather than the fields it happens to know about. It
refuses while a render is running, since the orchestrator is holding those
paths.

**Prompt rewriting.** A good MiniMax H3 prompt has a shape — camera move named
first, subject and action, environment and light with concrete material detail,
then the sound in one trailing clause. The 🪄 button on a shot prompt hands
your words to a local language model that knows that shape and asks for a
restyle. The result is shown as a **proposal with Use this / Discard** — never
applied on the model's word, because the prompt is your authorship and took
thought to write. The scene description and the shot's cast go along as
context marked *do not repeat*, since both are already prepended when the full
prompt is assembled. The shot's attached reference photos are also sent to
vision-capable rewrite services, so the rewrite can use their actual visual
constraints without describing attached character identities in prose.

The same 🪄 button rewrites three other fields, each with its own house
style so the model does not blur what belongs where: the project's **scene
description** (surroundings and visual style only — characters have their
own separate description, so it is never asked to describe one), the
project's **background sound** (grounded in the scene description, proposing
concrete effects suited to that setting rather than a vague mood), and a
shot's own **sound accents** (grounded in that shot's own prompt — the
action, environment and materials in it — and explicitly never dialogue).
The character **AI** button sends the portrait, attached voice clip (when
present), and starting description to a multimodal service and asks only for a
character description, never a scene description.

**Dialogue, cloned twice over — once for real.** On a Ref2VA shot, the
speaking character's own voice clip is already sent in as a soundtrack
reference (see "A cast" below), and verified to be enough on its own for H3
to speak a line correctly. So there, the shot is asked to speak the line
aloud in that voice directly, and the Dialogue tab shows a note instead of
the dub controls — a separate TTS take would be a second, independently
synthesised copy of the same line, which is exactly the doubled-voice bug
this replaced. FL2VA has no mechanism to tell H3 what anyone sounds like, so
dialogue there stays a silent lip-movement cue for TTS to dub in afterwards,
same as before.

**Tabbed prompt editing.** Shot prompt, dialogue, sound accents and the
resolved prompt share one tall pane rather than stacking four short boxes.
Each tab carries a dot when its field has content, so tabbing away never hides
the fact that a shot has dialogue. The resolved tab shows the assembled prompt
**segmented and labelled by source** — scene, each cast member, shot, sound
bed, accents — so it is never ambiguous which clause came from where.

**Draft mode.** Renders with the long edge capped around 384 px and 4 steps,
without generated audio or full PNG frame dumps unless another shot chains
from it. Clip length and seed are left alone, so the move you see is the move
you will get. A draft output is badged as such so it is never mistaken for a
finished shot.

**Aspect ratios.** 21:9, 16:9, 4:3, 1:1, 3:4 and 9:16, every dimension a
multiple of 32 so H3 does not silently round the request. The picker includes
large experimental outputs such as 1536x864 and 1920x1088; sizes a model's own
documentation cites are marked, anything else is offered but labelled untested.

## Selectable engines

Render backends (`server/backends/`) and speech engines (`server/tts/`) are
both pluggable, and each reports its own health so a missing model or an
unreachable service says so at startup rather than mid-render.

Speech services are **linked, not installed**. A voice model may be running as
a separate service, so this app points at it by name and URL in
`tts-services.json` (created on first run, meant to be edited):

```json
{"services": [
  {"id": "qwen3-clone", "label": "Qwen3-TTS voice clone",
   "kind": "qwen3-clone", "url": "http://127.0.0.1:8790"}
]}
```

Adding another instance is an entry, not a code change. `kind` picks the
client:

| kind | Notes |
| --- | --- |
| `vpipe-moss` | MOSS-TTS 8B through vpipe's own text-to-speech stage. Local, no URL needed, and clones a voice from a reference clip. Needs the MOSS models fetched — run `setup/prepare-moss-tts.vpipeline` from the workspace (~9 GB, one time). Roughly 27s for a short line on an M4 Pro. |
| `qwen3-clone` | A Qwen3-TTS-compatible voice-clone HTTP service. The transcript conditions the clone, so it needs a reference clip **and a transcript of what it says**; if the service exposes transcription, the app can fill that transcript from the clip. |
| `none` | Dialogue is stored but not spoken. |

Language models for prompt rewriting work the same way, in
`llm-services.json`:

```json
{"services": [
  {"id": "ollama-local", "label": "Ollama",
   "kind": "ollama", "url": "http://localhost:11434",
   "model": "qwen3.8:27b-mlx"}
]}
```

`kind` is `ollama` (native `/api/chat`) or `openai` (anything serving
`/v1/chat/completions` — llama.cpp, vLLM, LM Studio). Health checks that the
**configured model is actually pulled**, not merely that the port answers. A
27B local model takes roughly a minute per rewrite, which is why the button
shows progress and the result waits for approval. For a service needing a key,
save it in Settings or use `apiKeyEnv` (see above) rather than putting it in
this file.

The **Storyboard AD** handle on the right edge opens a chat with that same selected
prompt-rewriting model. It receives a compact authoring snapshot of the open
board—scene, cast, shot prompts, dialogue, timing, reference presence, and the
focused shot—while render outputs and logs are left out to save context. When a
shot is focused, its start/end frames, shot references, relevant cast portraits,
and project style references are also attached to the request (up to nine
images), so a configured vision model can inspect what they depict. It can
review the board or propose scene, cast, and shot changes using the authoring
part of Storyboard's MCP vocabulary. Proposed edits are never silent: they are
shown as an Apply/Discard card, and only **Apply changes** saves them to the
board. Chat history is kept per board, in the browser's own storage, and
survives a page refresh.

Storyboard AD deliberately **cannot** render video, synthesise or dub audio,
or start/stop anything — it only proposes the field edits above, because those
are reviewable before they touch the board and a render or a spoken take is
not. Asked to do one of those, it says so and points at the actual way to:
the **Render** button, or **Generate** on a shot's Dialogue tab, here in the
app; or, for scripting it, the full MCP server below, which has tools like
`sbv_dub_shot` and `sbv_start_render` that an external MCP client (Claude
Desktop, Claude Code) can call directly.

It knows enough to say which, too: its system prompt (`server/storyboard_chat.py`)
carries a compact, hand-written reference on how a board actually works —
dialogueSource, model auto-selection, frame rate, staleness — plus the full
MCP tool catalogue, read live from `mcp/server.py`'s own tool list rather
than copied in, so the two surfaces cannot silently drift apart. That
reference sits in the *system* prompt rather than the per-board context that
changes every turn, since the system prompt is the one part of the request
identical across a whole conversation — the natural place to put something
large but static, and the part a backend with its own prompt caching
(llama.cpp, vLLM, and similar) reuses instead of re-billing every message.

## MCP: driving Storyboard from an AI assistant

`mcp/server.py` is an [MCP](https://modelcontextprotocol.io) server that
exposes every storyboard feature the web UI has — boards, shots, references,
dubbing, rendering, settings — as tools an AI assistant (Claude Desktop,
Claude Code, or any other MCP client) can call directly. It is a thin bridge
to the same JSON API the browser uses (`server/app.py`); it has no logic of
its own, so a board edited by an assistant is edited exactly the way the UI
would edit it. Standard library only, same as the rest of this app — nothing
to `pip install`.

**Why the built-in MCP server is useful.**

- **Talk to the project in plain language** — ask an AI assistant to create a
  board, add shots, rewrite prompts, configure references, or explain a render
  failure.
- **Automate repetitive work** — generate shot lists, apply consistent scene
  details, prepare dialogue, start renders, monitor progress, and assemble
  finished clips without repeating UI actions.
- **One source of truth** — the assistant uses the same API as the browser, so
  changes made through MCP appear in the UI immediately and follow the same
  validation and persistence rules.
- **Keep creative intent separate from backend complexity** — describe the
  result you want while Storyboard translates it into the configured VPIPE or
  ComfyUI workflow.
- **Local-first by design** — the MCP bridge runs locally over standard input
  and talks to the local Storyboard server; it does not require a separate
  cloud orchestration service.
- **Easy to connect** — any MCP-compatible client can use the server, and it
  can start the Storyboard app automatically when the first tool is called.

**Setup.** Point your MCP client at the script:

```json
{
  "mcpServers": {
    "storyboard": {
      "command": "python3",
      "args": ["/absolute/path/to/storyboard/mcp/server.py"]
    }
  }
}
```

For Claude Code: `claude mcp add storyboard python3 /absolute/path/to/storyboard/mcp/server.py`.

**It starts the app for you.** The MCP server doesn't run the storyboard app
itself — it talks to it over HTTP at `http://127.0.0.1:9877`. But the first
time a tool is called, it checks whether that server is already up and, if
not, launches `start.sh` and waits for it, so simply having the MCP server
configured is enough: you don't need to `./start.sh` first. Set `SBV_PORT` if
you run the app on a non-default port, or `SBV_MCP_URL` to point at a host
other than localhost.

**What it can do.** One tool per API endpoint — the same list in
`server/app.py`'s own routing table:

| Tool | Same as the UI's... |
| --- | --- |
| `sbv_info` | startup banner — backend health, available models, speech/rewrite services |
| `sbv_list_boards` | the **Open** dialog |
| `sbv_get_board` / `sbv_save_board` | loading a board / every autosave while editing |
| `sbv_create_board` / `sbv_import_board` | **New** / **Copy a board in** |
| `sbv_delete_board` | deleting a project |
| `sbv_rename_board` | renaming a project (moves its folder, rewrites its paths) |
| `sbv_export_board` | downloading a board as JSON |
| `sbv_add_shot` | **+ Shot** |
| `sbv_upload_ref` / `sbv_adopt_ref` | uploading or picking a reference image or voice clip |
| `sbv_list_library` / `sbv_delete_library_item` | the image/voice picker grid and its trash button |
| `sbv_transcribe` | **Transcribe** on a reference clip |
| `sbv_dub_shot` | **Generate** on the Dialogue tab |
| `sbv_accept_take` | **Keep this take** |
| `sbv_rewrite_prompt` | the 🪄 prompt rewrite button |
| `sbv_describe_character` | drafting a character description from their portrait |
| `sbv_start_render` / `sbv_stop_render` / `sbv_status` | **Render** / **Stop** / the live progress rail |
| `sbv_assemble` | joining rendered clips into `final.mp4` on demand |
| `sbv_set_data_dir` / `sbv_restart_server` | **⚙ Settings → Storyboard data folder** |

Since a board is a single JSON document, most edits — the scene description,
sound, cast, per-shot prompt/dialogue/model/reference fields — go through
`sbv_get_board` → edit the object → `sbv_save_board`, exactly as the browser's
own autosave does; the other tools are the actions the UI has as their own
buttons (render, dub, rename, transcribe, and so on).

**Checking a line fits before you render.** `sbv_dub_shot` always synthesises
the line, whether or not the shot has been rendered yet, so it doubles as a
fit check: call it right after writing a shot's dialogue, and its response's
`warning` field says so if the spoken line runs longer than the shot's
`frames` (at the fixed 24fps render rate) — before a render has been paid for,
that comparison is against the planned length; after, it is against the
actual clip. No warning means the line fits either way.

This only applies to a shot using a separate TTS take (`dialogueSource:
"recording"`, the default). A shot set to H3 native speech has no separate
take to check or preview — the video model generates that shot's speech
itself, lip-synced, while rendering — so `sbv_dub_shot` refuses on one with a
message pointing at `sbv_start_render` instead. The UI's own Generate button
is disabled on such a shot for the same reason.

**Example prompts**, once the tool is connected:

- "List my storyboards and tell me which ones have unrendered shots."
- "Open the 'Mustang' board, rewrite shot 2's prompt, and start rendering it."
- "Add a new shot to `lara-croft-the-hunt` with this prompt: ..."
- "Transcribe `refs/villain.wav` in the Mustang board, then generate the
  dialogue for shot 3 using it."

## Tests

```sh
python3 tests/store_paths.py             # renaming, and an independent data dir
python3 tests/speak_line.py              # who speaks, and previewing without a render
python3 tests/render_all.py              # what "Render all" picks up, and the final cut
python3 tests/continuity.py              # continuity references, dialogue reuse, trims and transitions
python3 tests/soundtrack.py              # when the soundtrack is generated or reused, and the ducking
node tests/edits-persist.mjs             # every edit reaches the server, not just the first
node tests/poll-does-not-rebuild.mjs     # the poll must not rebuild a focused subtree
```

The two `.mjs` ones drive the real page over the DevTools protocol; each file's
header has the two commands to start the server and a debug Chrome, and both
take `CDP_PORT` (9222 by default).

All of them cover one class of bug: something is replaced underneath state that
had already recorded where it was. Renaming rewrites the paths a board
recorded. The poll must not rebuild a subtree holding a focused input. And
saving must not replace the object graph the editor's handlers point into —
each was a silent failure, which is why they are pinned rather than
remembered.

## Why a run is judged, not just started

Every failure encountered while driving vpipe by hand **exited with code 0**.
It warns and continues rather than crashing, which is right for an interactive
tool and dangerous for an unattended queue. So a shot counts as done only if
it exited clean **and** produced every file it declared **and** took a
plausible amount of time **and** showed evidence the denoise loop ran. A run
that produced its outputs but tripped a softer signal is flagged for review
rather than discarded, and a shot chained to a failed shot is blocked rather
than rendered against a missing frame.

The runtime baseline is fitted to measured runs on an M4 Pro: 124 frames in
27m44s, 39 frames in 7m32s, denoise growing as roughly `frames^1.37`. It has
predicted subsequent runs within 5%.

## Why "Render all" re-renders a shot that says done

A shot already marked done is skipped, because a re-render costs between six
and forty minutes and doing it casually is worse than not doing it. But "done"
has to mean *done from what the board says now*, and for a while it only meant
"done, once". A full run over a three-shot board rendered one shot, kept two
clips of prompts that had since been rewritten, reported success, and gave no
hint that two thirds of the batch had been skipped.

So every render records a fingerprint of its inputs — the shot prompt and
dialogue line, sound note, the scene description and background bed, the cast
it uses and their descriptions and portraits, the reference images, the model,
frame size, frame count, steps, seed and draft mode. A whole-board run picks up
any shot whose fingerprint has moved, plus anything chained to one of those,
since a start anchor taken from a re-rendered shot is a different picture.
Dialogue is included because it is passed to the video model as a visual cue
for natural jaw and lip movement; the spoken audio is still synthesised outside
the render and mixed over the finished clip.

A shot rendered before any of this existed has no fingerprint, and is treated
as stale — it may well be current, but nothing can show that it is, and
assuming otherwise is how the wrong clip reaches the cut. **Keep this take**
on the shot records it as current without re-rendering, for when you know it
is. The board header says how many shots a run will take before you press it.

## Why there is a concat pass at all

A board of finished clips is not the deliverable. The last step used to be
manual and undocumented, so a run could render every shot and still leave you
with a folder. `server/assemble.py` joins them in board order into
`final.mp4` at the end of a whole-board run, or on demand from the rail.

It re-encodes rather than stream-copying, because the inputs genuinely are not
uniform: a dubbed clip carries AAC, an undubbed one whatever the model wrote,
and a still has no audio track at all — and `concat` with `-c copy` over
mismatched streams yields a file that plays for the length of the first clip.
A clip with no audio gets a matched stretch of silence so nothing after it
drifts. A shot with no render is left out and *named*, and the cut is marked
partial rather than quietly being short.

`clip-dubbed.mp4` is preferred over `clip.mp4` only while it is newer, since
it is built from the clip and a re-render leaves it describing a video that no
longer exists. A line that was spoken before its shot was rendered is now laid
onto the clip when that render finishes; one that was spoken before this
existed is reported in the rail rather than silently missing from the cut.

## Why the poll paints instead of re-rendering

While a render is in flight the queue is polled once a second. That tick
changes percentages and a phase label, almost never the shape of the page — so
it updates those in place and rebuilds nothing. Rebuilding was the original
approach and it cost two visible bugs at once: every image re-decoded and any
playing `<video>` reloaded (a flicker on the second), and the field you were
typing in was destroyed and recreated, so editing one shot while another
rendered dropped the caret every second. A full rebuild now happens only when
something structural moves — a status transition, a new thumbnail, a new
output — and carries the caret across when it does.
`tests/poll-does-not-rebuild.mjs` asserts all of it against the running page.

## Known gaps

- The ComfyUI backend is a scaffold with an integration plan, not an
  implementation.
- Board editing is a whole-board save, so two browser tabs on one board are
  last-write-wins. The render queue writes the board too, so an edit made
  during a long shot can be overwritten when that shot finishes.
- Reference continuity guides the next scene but does not guarantee exact
  opening-frame matches, motion continuity, or lip-sync with recorded dialogue.
- `library()` (the pick-an-existing-image grid) scans the data directory only.
  Images sitting elsewhere in the vpipe workspace are not offered; upload them
  or copy them into a project folder.
