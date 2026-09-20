# Storyboard UI for vpipe — design summary

## Goal

A higher-level web UI, in front of vpipe, where a video idea is built as an
ordered sequence of **shots** rather than a raw pipeline graph — prompt +
reference image(s) + a few high-level params per shot — with shots optionally
chaining off each other (end of shot N feeds the start of shot N+1). The
underlying vpipe pipeline JSON is generated and run for you; the wrapper's
job is to make the common case fast and to manage the queue of long-running
generations, not to replace vpipe's own Composer/Pipeline Manager for
power users.

## Why vpipe, not ComfyUI, as the backend

- **vpipe's `.vpipeline` files are flat, small, composable JSON** — a
  text-to-video shot is ~8 stages, a reference-conditioned one ~10. That's
  templatable per shot type with straightforward find/replace-style
  generation. ComfyUI's node graphs are larger and more workflow-specific;
  templating one programmatically per shot type is more work for the same
  result.
- **The primitives already map onto "shot" concepts almost 1:1**:
  - `generate-video` ports 5/6 (`vae-encode` of an image) = start/end anchor,
    already exactly the "starting and ending reference image" feature asked
    for here.
  - `video-ref-encoder`'s `references` list = subject/style consistency
    images, for when a shot needs "this exact ship" rather than "this exact
    opening frame."
  - `model-select` + one config block per stage = the "which model, what
    resolution, how long" knobs, already isolated from the graph shape.
- **A CLI built for exactly this** — `vpipe --launch <file>` as a
  subprocess, with structured `[PROGRESS] N% of 'denoise' completed at
  HH:MM:SS (x/y)` lines already on stdout. A queue/orchestrator can parse
  that directly instead of scraping a GUI.
- Native Metal backend, no Python/PyTorch dependency — matches the hardware
  this would actually run on.

## Scene description: global vs. per-shot

Three shipping tools answer this differently, and the differences are
instructive:

- **OpenArt Smart Shot** — one global scene prompt; the AI auto-decomposes
  it into a shot plan. Characters/environments are attached globally from a
  saved-asset library, not per shot. Strong consistency, weak per-shot
  control.
- **Higgsfield Popcorn** — the opposite: per-shot prompt is the primary
  authoring unit ("write a prompt for each scene — framing, motion, mood").
  Reference images are either set once globally or **chained shot-to-shot**
  — literally "take the final image and use it as the new reference input
  for the next shot," the same pattern as this design's "last frame of shot
  N." Strong per-shot control, consistency is only as good as remembering
  to restate it each time.
- **Runway** — a hybrid: per-shot prompts for direction, plus a *separate*
  "Characters" identity-locking engine that holds appearance constant
  independent of prompt text, so consistency doesn't depend on the user
  re-typing a description correctly every shot.

**Recommendation: two-tier prompt, matching Runway's split most closely,**
because it maps directly onto a distinction vpipe already makes at the
architecture level — `video-ref-encoder`'s reference list carries "subject
and style... everywhere, and nowhere in particular" (the model's own
docs), while the per-shot prompt text drives what happens in *this* clip
specifically. Those are already two different vpipe mechanisms, not two
UI fields imposed on one mechanism:

- **Project-level scene description** (optional, recommended by default):
  what should stay true across every shot — subject appearance, overall
  visual style/palette. Maps onto `style_refs` (Ref2VA reference images)
  plus a reusable text fragment.
- **Per-shot prompt** (required): what's different about *this* shot —
  action, camera move, mood. Maps onto Higgsfield's per-panel model, which
  is the better fit for the actual editing loop (you're constantly
  rewriting camera direction, rarely rewriting who the subject is).

At generation time the Generator concatenates
`[project scene description] + [shot prompt]` into the text fed to
`text-prompt`. Project-level `style_refs` are a library rather than an
automatic input — a shot only gets one once it's added to that shot's own
reference images — while shot-level start/end anchors are always wired for
that shot, without losing Higgsfield's per-shot control where it matters.

## Data model

```
Project
  ├─ scene_description (text)         — subject/style, reused every shot
  ├─ style_refs: upload[]             — Ref2VA subject images, project-wide
  ├─ default resolution / steps (inherited per shot unless overridden)
  └─ Shot[]  (ordered)
       ├─ prompt (text)                — this shot's action/camera/mood only
       ├─ start_ref: upload | "last frame of shot N" | none
       │             FL2VA hard first-frame anchor when present
       ├─ end_ref:   upload | none               (FL2VA last-frame anchor)
       ├─ model: ref2va                         (compatibility default)
       ├─ resolution, frames/duration, steps, seed
       └─ status: draft | queued | running | done | failed
```

A shot with `start_ref` set to "last frame of shot N" selects FL2VA and needs
no new vpipe capability — every shot already writes its per-frame PNGs
(`save-image` alongside `save-video`), so chaining is just pointing the next
shot's first-frame anchor loader at `shots/N/frames/frame-<max>.png`.

## Architecture

**1. Generator** — turns one Shot into a `.vpipeline` file. Video shots with
Start/End anchors use the `fl2va` template and its direct first/last-frame
ports. Shots without anchors use the `ref2va` template, with an ordered
`references` list built from other shot references, Cast portraits, and
voice clips within Ref2VA's image/audio limits — project `style_refs` join
that list only for shots that also add the image as one of their own shot
references. The separate
`krea2-still` template remains available only to Create Stills previews.

Every shot gets its own project folder (`shots/<n>/`), same convention
used by hand all through this session — spec file, output video, frames,
nothing loose at the top level.

**2. Queue/orchestrator** — the actual value-add over raw vpipe:
- One generation at a time, strictly serial (this session hit real problems
  running two GPU-bound generations concurrently; the queue exists
  specifically to make that mistake structurally impossible).
- Launches each shot's `vpipe --launch`, tails stdout, republishes the
  `[PROGRESS]` lines as job-percent/ETA to the UI.
- Auto-advances to the next queued shot on clean exit; on a failure (see
  **Stability & error correction** below), stop and flag rather than
  silently continuing to a shot that depends on the broken one's output.
- Persists state so a long batch survives the browser tab closing.

**3. Frontend** — storyboard strip of shot thumbnails (drag to reorder),
a per-shot editor panel (prompt, two image-drop slots for start/end with a
one-click "chain from previous shot" toggle, model/resolution/length
dropdowns), a global defaults panel, and a progress view per shot that
reuses vpipe's own percent-complete semantics. Preview is just serving the
resulting `.mp4`/`.jpeg` directly — no need to reimplement vpipe-web-ui's
live pipeline graph or profiler.

**Visual style should match your own dashboard**, if you have one, rather
than invent a new look — same panel/dashboard language, so this reads as
one more tile in that system instead of a bolted-on third-party tool.
Concretely means pulling that dashboard's actual component library and
design tokens (colors, spacing, panel chrome) from its repo rather than
guessing at them here; see [`css/tokens.css`](../css/tokens.css) for the
placeholder values shipped in the meantime.

## Relationship to vpipe's own web UI

Not a replacement. vpipe-web-ui's Composer stays the tool for anything the
storyboard UI doesn't expose — hand-editing a generated shot's pipeline
file, wiring an unusual reference combination, debugging. The storyboard
tool only ever writes plain `.vpipeline` JSON, so dropping any shot into
vpipe-web-ui's **Load** is always available as an escape hatch.

## Stability & error correction

The reason this needs real design rather than "check the exit code" is that
**every actual failure hit tonight exited 0.** vpipe fails quietly by
design — a stage that can't proceed logs a `WARN` and moves on rather than
crashing the process — which is good behavior for an interactive tool and
actively dangerous for an unattended queue, because "exit 0" and "did the
right thing" are not the same claim. Four real incidents from tonight, and
what each implies for the orchestrator:

**1. Silent empty output** (`frames: 5` on MiniMax H3). Denoise ran, VAE
decode logged one `WARN` ("fewer than the 8 one chunk needs") and skipped,
process exited 0, zero files written. *Implies:* success can never be
"exit code 0" alone — it has to be **every file the pipeline declared it
would produce actually exists**, checked against the shot's own output
manifest (the Generator knows what `save-video`/`save-image` paths it
wrote into the `.vpipeline`, so it knows what to check for).

**2. Silent wrong output** (Ref2VA reference rows in the wrong shape). Same
shape as #1 — one `WARN`, exit 0, nothing written — but this time it wasn't
obvious until a human read the log line-by-line: the run also finished in
1m25s against a ~28-minute expectation. *Implies:* a **runtime sanity
check** matters independently of the file-existence check, because a bug
could plausibly produce a file that exists but is truncated/wrong rather
than absent. Track expected duration per (model, resolution, frame count)
from prior successful runs and flag anything that finishes in a small
fraction of it, even if the file-existence check passes. Also flag if no
`[PROGRESS] ... 'denoise'` line ever appeared — generation that never
actually started denoising is never a success regardless of exit code.

**3. A warning that must NOT fail the job** (`wired pool: the box refused
to wire 0 MB... the rest of this run's weights stay reclaimable`). This
looked alarming and was completely benign — vpipe degraded gracefully to
streamable memory and the run finished correctly. *Implies:* log scanning
can't be "any WARN = fail." It needs a small **classified pattern list** —
a short denylist of WARN/ERROR substrings known to correlate with silent
failure (skipping a stage, VAE decode refusing, "not found" on a model
path) checked against, rather than a blanket rule — and the classification
list is going to need real entries added over time as new failure modes
turn up, the same way tonight's two bugs were only discovered by actually
reading logs by hand.

**4. Interrupted background jobs** (a session/harness restart silently
killed two in-flight model downloads, leaving `.part` files at 73% and
~90%). *Implies:* on startup, the orchestrator should reconcile its job
state against reality — a job marked "running" whose process no longer
exists is not "failed," it's **interrupted**, and prep/download jobs are
safely resumable (`skip_existing_files: true` picks up an interrupted
`.part` file from the byte it stopped at — vpipe's own doc-confirmed
behavior, not something the wrapper has to build). Generation jobs are a
different case: rerunning one from scratch is the correct recovery, not a
resume, since there's no partial-generation state to pick back up from.

**Retry policy follows from that split.** A resumable prep/download job is
safe to auto-retry (it's making real forward progress each time, same as
the manual resume tonight). A generation job that failed by hitting a real
bug — like #1 and #2 — will fail exactly the same way again with identical
inputs; auto-retrying it just burns another 28 minutes to reproduce the
same wrong answer. Default to **no automatic retry on a generation
failure** — stop the queue, surface the classified reason, let a human (or
the next day's session) look at it. This is a case where doing less
automatically is the safer default, not a missing feature.

**Cascading failures.** If a shot fails validation, any shot chained off
its output (`start_ref: "last frame of shot N"`) must not run — the
orchestrator needs the dependency edge, not just a flat queue, so a bad
shot blocks its dependents specifically rather than either halting
everything downstream or silently generating from a missing/stale frame.

**Cancellation.** Worth designing for from the incidents tonight where a
running job was manually stopped: SIGTERM and SIGINT (`vpipe`'s documented
Ctrl-C path) both went unanswered for several seconds before an escalation
to SIGKILL was needed. A "stop this job" button needs the same escalating
sequence with a timeout, not a single signal-and-hope. It's also safe to
skip straight to a hard kill when the job hasn't reached its write stage
yet (no output files exist) — nothing partial to corrupt — which the
orchestrator can already tell from the same output-manifest check used for
success detection.

## MVP scope — built

1. Generator for the shot templates. **Done** — FL2VA anchored video shots,
   Ref2VA reference-conditioned shots, plus a Krea-2 still preview.
2. Serial queue + progress parsing. **Done**, including the validation
   contract below and per-shot run logs persisted next to the outputs.
3. Storyboard strip + per-shot editor, chaining via "last frame of shot N".
   **Done and verified**: a chained shot's opening frame matches its
   predecessor's closing frame.
4. Direct file-serving preview. **Done.**

Added since, from use rather than from the plan:

5. **A cast.** Characters with a required name and description and optional
   reference image and voice clip; a shot casts who appears in it. Portraits
   and voices become Ref2VA references, within that model's real limits.
6. **Sound in two layers** — a project bed and per-shot accents. The bed can
   render into every shot, or be held out for a continuous final mix/add-later
   workflow; per-shot accents still render with their clip.
7. **Dialogue via a pluggable speech engine** (`server/tts/`), with the line
   passed to the video model only as a visual mouth-movement cue. The final
   voice is mixed over the finished clip rather than trusted to the video
   model, which does not produce intelligible speech.
8. **Draft mode** — long edge capped around 384 px, 4 steps, same length and seed,
   no generated audio or full frame dump unless needed for chained shots.
9. **Aspect ratios** with the sizes each model's docs cite marked as such.

## What use taught us that the plan did not

Three things only showed up once the thing existed:

*   **Every real failure exits 0.** This was the design's central bet and it
    held: the validation contract has caught a run that wrote nothing, a run
    that finished in 5% of its expected time, and a malformed-reference run
    that never reached denoise — all of which reported success.
*   **A UI that renders is not a UI that works.** Several faults were
    invisible to the server and to a syntax check: handlers silently removed
    by an over-broad edit, a modal that threw before unhiding itself, a field
    that rebuilt itself on every keystroke and so accepted only the first
    character. Screenshotting the actual page found each one. An audit
    comparing every id in the markup against the ids the script references now
    guards the first class.
*   **Constraints are worth encoding, not documenting.** H3's `17n+5` frame
    rule and its 39-frame decode floor became a duration picker rather than a
    warning, and the models' 16-pixel multiple became the only sizes on offer.
    A rule the UI cannot violate beats a rule the user has to remember.

## Stretch / open questions

Multi-clip stitching is no longer one of these: it is `server/assemble.py`,
run at the end of a whole-board batch. It was left as a stretch item for
longer than it should have been, and the cost was a run that rendered
everything and produced no video.

- Batch variations (same shot, several seeds) — the queue already supports
  this trivially, just a "generate N seeds" button per shot.
- Model prep (first-time downloads) is a separate, much longer-running
  concern from generation and probably belongs in a one-time setup screen
  rather than the shot queue.
