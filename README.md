# Storyboard → Video

A storyboard-shaped front end for local video generation. Build a piece as an
ordered sequence of shots — each with a prompt, optional reference frames, a
cast and a length — and the tool compiles each shot into a real pipeline, runs
them one at a time, and tells you honestly whether each one worked.

Currently drives [vpipe](https://github.com/tgo-app-dev/vpipe) (MiniMax H3 for
video, Krea-2 Turbo for stills) on Apple Silicon. ComfyUI is a documented
scaffold behind the same interface.

Design rationale and the orchestrator contract:
[`docs/STORYBOARD-UI-DESIGN.md`](docs/STORYBOARD-UI-DESIGN.md).

## Running it

```sh
./serve.sh                              # http://localhost:9877
./serve.sh --lan                        # reachable on your LAN
./serve.sh --port 8080
./serve.sh --workspace DIR              # where vpipe is launched from
./serve.sh --data-dir DIR               # where your storyboards live
./serve.sh --tts qwen3-clone            # default engine id (see tts-services.json)
./serve.sh --llm ollama-local           # default rewrite model (see llm-services.json)
```

Standard library Python only — nothing to install. Run the script, it prints a
URL, Ctrl-C stops it. Port 9877 by default so it never collides with
`vpipe-web-ui` on 9876.

### Fetching the speech models

`vpipe-moss` needs two models in the workspace. Once:

```sh
cd <workspace>
<vpipe>/build/apps/vpipe/vpipe --launch setup/prepare-moss-tts.vpipeline
```

That pulls `mlx-community/MOSS-TTS-8B-8bit` (already 8-bit, no quantize pass)
and `OpenMOSS-Team/MOSS-Audio-Tokenizer`. The engine reports itself
unavailable until both are present, and treats a directory containing
`*.part` as still downloading.

**Two directories, on purpose.**

`--workspace` is vpipe's. It resolves `models/` and its LMDB model registry
relative to the directory it is launched from, so this must be the directory
the models were prepared in — it is not ours to choose.

`--data-dir` is yours: storyboards and uploaded references. It **defaults to
the workspace**, so an existing install is unaffected, but it can be anywhere,
which is the point — a storyboard and its reference images are documents, and
should not have to live inside another tool's runtime directory to be usable.
Nothing is moved automatically when you point it somewhere new; the startup
banner names any boards left behind in the old location and prints the `mv` to
move them, because they are your files.

Paths stored in a board stay relative to the data directory, which is what
keeps a project folder portable. Paths written *into* a generated pipeline are
absolute, since the data directory need not sit under the workspace vpipe runs
in. (Verified: vpipe reads and writes outside its workspace, because the file
sandbox in `common/session.cc` is opt-in via a `file_sandbox` config key that
these pipelines do not set.)

The startup banner lists both directories and which models, speech engines and
rewrite models it can actually see.

## Where things live

```
<workspace>/projects/<board-slug>/
├── storyboard.json      the whole board: scene, sound, cast, shots
├── final.mp4            every shot, joined in order
├── refs/                reference images and voice clips (uploads and picks)
└── shots/01/
    ├── shot.vpipeline   generated per render
    ├── clip.mp4         the render
    ├── clip-dubbed.mp4  with synthesised dialogue mixed in
    ├── dialogue.wav
    ├── run.log          stdout, exit code and timing
    └── frames/*.png
```

A project folder is self-contained: copy or zip it and you have the board, its
references and everything it rendered. Picked images are copied in rather than
referenced in place, so a board never depends on another project's folder.
Save, load and export are all the same file.

## What it does

**Two-tier prompting.** A project-level scene description (subject and style,
true of every shot) plus a per-shot prompt (action, camera, mood). The editor
shows the assembled result, so nothing about the composition is hidden.

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

**Frame anchors and chaining.** A shot can open — and close — on an exact
frame, including "the last frame of the previous shot", which is how a
sequence reads as continuous. Verified: a chained shot's first frame matches
its predecessor's last.

**Transcription is its own capability.** Cloning a voice and recognising
speech are separate, and one does not imply the other — MOSS clones a voice and
has no speech recognition at all. So engines declare `supports_transcription`,
the Transcribe button reports on whichever configured service actually has it
(naming it in the tooltip), and `/api/transcribe` uses the engine you asked for
when it can and otherwise finds one that can, saying which it used. Refusing on
the grounds that *the selected* engine cannot transcribe was a dead end when
another configured service was sitting right there.

**Dialogue, in the character's own voice.** *Speak this line* synthesises the
shot's line using the speaking character's reference clip and transcript, so
what you hear is their cloned voice, not a generic one. Who speaks is inferred
when a shot has one character in it and chosen from a list when it has several,
preferring whoever actually has a clip.

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
prompt is assembled.

**Tabbed prompt editing.** Shot prompt, dialogue, sound accents and the
resolved prompt share one tall pane rather than stacking four short boxes.
Each tab carries a dot when its field has content, so tabbing away never hides
the fact that a shot has dialogue. The resolved tab shows the assembled prompt
**segmented and labelled by source** — scene, each cast member, shot, sound
bed, accents — so it is never ambiguous which clause came from where.

**Draft mode.** Renders at half size and 6 steps — 3.7x to 8.3x faster
depending on settings — to check framing and camera motion before committing.
Clip length and seed are left alone, so the move you see is the move you will
get. A draft output is badged as such so it is never mistaken for a finished
shot.

**Aspect ratios.** 16:9, 4:3, 1:1, 9:16, every dimension a multiple of 16 as
these models require. Sizes a model's own documentation cites are marked;
anything else is offered but labelled untested.

## Selectable engines

Render backends (`server/backends/`) and speech engines (`server/tts/`) are
both pluggable, and each reports its own health so a missing model or an
unreachable service says so at startup rather than mid-render.

Speech services are **linked, not installed**. A voice model here is usually
already running somewhere with an owner, so this project points at it by name
and URL in `tts-services.json` (created on first run, meant to be edited):

```json
{"services": [
  {"id": "qwen3-clone", "label": "Qwen3-TTS voice clone",
   "kind": "qwen3-clone", "url": "http://optiplex:8790"}
]}
```

Adding another instance is an entry, not a code change. `kind` picks the
client:

| kind | Notes |
| --- | --- |
| `vpipe-moss` | MOSS-TTS 8B through vpipe's own text-to-speech stage. Local, no URL needed, and clones a voice from a reference clip. Needs the MOSS models fetched — run `setup/prepare-moss-tts.vpipeline` from the workspace (~9 GB, one time). Roughly 27s for a short line on an M4 Pro. |
| `qwen3-clone` | The Qwen3-TTS voice-clone server MCC uses. Around 3x faster than MOSS for a short line, and the transcript conditions the clone. Needs a reference clip **and a transcript of what it says** — its `generate_voice_clone()` conditions on both — and it will transcribe the clip itself if the transcript is blank. **Reachability:** it binds `127.0.0.1`, so from another machine either start it with `--host 0.0.0.0` and open port 8790, or tunnel it with `ssh -L 8790:127.0.0.1:8790 <host>` and leave the URL as localhost. |
| `mcc-sherpa` | MCC's `/api/tts`, a sherpa-onnx VITS voice. Text in, wav out, no cloning. |
| `none` | Dialogue is stored but not spoken. |

Language models for prompt rewriting work the same way, in
`llm-services.json`:

```json
{"services": [
  {"id": "ollama-local", "label": "Ollama (this machine)",
   "kind": "ollama", "url": "http://localhost:11434",
   "model": "qwen3.8:27b-mlx"}
]}
```

`kind` is `ollama` (native `/api/chat`) or `openai` (anything serving
`/v1/chat/completions` — llama.cpp, vLLM, LM Studio). Health checks that the
**configured model is actually pulled**, not merely that the port answers. A
27B local model takes roughly a minute per rewrite, which is why the button
shows progress and the result waits for approval. For a service needing a key,
use `apiKeyEnv` to name an environment variable rather than putting the key in
this tracked file.

Both HTTP contracts were read from MCC's source, not guessed.

## Tests

```sh
python3 tests/store_paths.py             # renaming, and an independent data dir
python3 tests/speak_line.py              # who speaks, and previewing without a render
python3 tests/render_all.py              # what "Render all" picks up, and the final cut
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

- The theme is a placeholder, not MCC's palette. All colours live in
  `css/tokens.css` and `css/app.css` has none, so matching MCC is a
  value-for-value swap in one file — but this repo had no read access to
  `admin/mcc`.
- The ComfyUI backend is a scaffold with an integration plan, not an
  implementation.
- Board editing is a whole-board save, so two browser tabs on one board are
  last-write-wins. The render queue writes the board too, so an edit made
  during a long shot can be overwritten when that shot finishes.
- The cut is a straight concatenation: no transitions, no per-shot trimming,
  and no separate audio bed across the whole piece.
- `library()` (the pick-an-existing-image grid) scans the data directory only.
  Images sitting elsewhere in the vpipe workspace are not offered; upload them
  or copy them into a project folder.
