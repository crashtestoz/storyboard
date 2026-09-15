# Storyboard: scene continuity, character references, and cloned voice

Status: implemented locally on 16 September 2026. Automated pipeline and
FFmpeg checks pass; generated-model continuity quality still needs a short
visual/audio trial. The design below records the intended behavior and remaining
experimental work.

## Using the implementation

1. Restart Storyboard and refresh the browser after any running render stops.
2. Open the final-video panel's **Continuity, dialogue & cut settings**.
3. Choose **Continue all scenes with Ref2VA**, or enable **Continue from shot N
   using Ref2VA references** in an individual scene. Remove Start/End anchors
   first; existing hard chains are preserved until explicitly changed.
4. Use **H3 native speech** to send the character voice into Ref2VA, or keep
   **Dialogue-window recording** for a separate cloned take. Keep the original
   character assigned to every relevant scene.
5. **Prepare all dialogue** generates only missing/stale recordings and reuses
   current takes. Render batches perform the same server-side preparation before
   generating video. Native speech is generated during the video render.
6. Review adjacent scene endings/openings in the scene editor. Set start/end
   trims there; set crossfade duration, edge audio fades, level matching, and an
   optional looping background audio file in the final-video panel.
7. Render all, or re-assemble existing clips when only cut settings changed.

Selected-scene renders automatically include missing or stale prerequisites,
with the reason shown in the queue. Sources must be earlier scenes: missing,
forward, and cyclic links are rejected before rendering. A failed source blocks
its dependents. A continuity frame's content hash detects changes even when the
filename stays the same.

**Audio correction:** replacing the generated soundtrack now preserves the full
video duration, padding a short recording with silence. Previously a short dub
could remove the ending used by the next scene's chain. Longer speech still
requires a longer scene or shorter line; dubbing reports truncation.

Trimming affects assembly only; chaining uses the original final rendered frame.
Review that relationship when trimming an ending. Crossfades overlap both picture
and sound and shorten the final cut. Neither reference guidance nor crossfading
ensures exact motion continuity or lip-sync.

Implementation lives in `storyboard/server/{orchestrator,store,speech,assemble}.py`,
`storyboard/server/backends/vpipe_backend.py`, and `storyboard/js/app.js`.
Regression coverage includes `storyboard/tests/continuity.py` and the existing
scene-layer, render-all, speech, persistence, and reference-control suites.
No Kermit settings were changed and no new model render was started as part of
implementation.

## Goal

Render a sequence in which each scene continues naturally from the previous
scene, preserves the original character's appearance, and uses the same cloned
voice. A whole-board render should handle dependencies and audio preparation
without requiring Generate to be clicked separately on every scene.

The recommended first implementation is **Ref2VA continuity references**:
automatically pass the previous scene's final frame alongside the original
character portrait and, for native speech, the speaker's voice reference.
This guides continuity; it does not fix the opening frame or guarantee seamless
motion across the cut.

## What works today

The current implementation is in `storyboard/`:

| Configuration | Actual behavior |
| --- | --- |
| No Start/End frame | Uses Ref2VA with scene references, cast portraits, and project style references. |
| Start/End frame, including a chain | Automatically selects FL2VA. The supplied frame becomes an anchor; separate Ref2VA portraits and voice references are not supplied on this path. |
| Ref2VA with native speech | Sends the speaking character's voice reference and asks H3 to generate the dialogue. |
| Dialogue-window recording | Uses a separately prepared dialogue take, applied after the video render. |
| Replace clip audio with the dub | Replaces the generated soundtrack with the existing recording, including removing generated ambience and effects. It does not generate a recording. |
| Render all | Renders stale scenes and dependent frame chains, then assembles the clips. It prepares missing/stale recorded dialogue before video generation. |

Existing recordings can be reused after a render. The post-render relay checks
whether the spoken text and direction still match. Native speech bypasses that
relay so an older recording does not replace the newly generated speech.

### Kermit board observed on 16 September 2026

`projects/camera-motion-test-with-kermit/storyboard.json` had:

- The same Kermit character portrait and reference voice attached to the cast.
- No Start/End frame chains on any of its ten scenes.
- Scene 1 using native speech.
- Scenes 2–10 using recordings, with existing dialogue WAV files and matching
  spoken text.
- Replace mode enabled on all scenes, although native speech bypasses dubbing.

This is a snapshot, not a permanent statement about the project's settings.
Its camera-test prompts also specify different opening compositions. A
continuous version needs those instructions revised so a new scene does not
demand a framing reset that conflicts with the preceding scene's ending.

## Why the model documentation and UI seem inconsistent

MiniMax documents two distinct base variants: FL2VA accepts first/last-frame
images; Ref2VA accepts multimodal references, including images and audio.
Ref2VA supports up to nine images, three audio clips, and twelve total reference
files, with additional duration limits. These capabilities do not mean that
Storyboard currently exposes every combination or that a reference image is a
fixed first-frame constraint. See the
[official model specifications](https://huggingface.co/MiniMaxAI/MiniMax-H3#model-variants-and-input-specifications).

The full H3 system also includes hosted context processing that is not part of
the open-source release. Local results therefore depend on how the application
constructs the reference set and prompt. See
[H3-Context-IR](https://huggingface.co/MiniMaxAI/MiniMax-H3#h3-context-ir).

## 1. Separate continuity guidance from frame anchors

Add an explicit continuity control with three choices:

| Choice | Backend | Meaning |
| --- | --- | --- |
| Independent scene | Ref2VA | Use original cast/style/scene references. |
| Continue using previous scene as reference | Ref2VA | Add the previous final frame as composition and continuity guidance while retaining original references. |
| Anchor opening to previous final frame | FL2VA | Use the existing Start-frame chain behavior; use recorded dialogue for a cloned voice. |

Proposed new field, independent of `startRef` and `endRef`:

```json
{
  "continuityRef": {
    "kind": "chain",
    "from": "previous-shot-id",
    "mode": "reference"
  }
}
```

Resolve this field immediately before preparing the scene, after its source
has completed successfully. Keep the resolved frame and source take identity
as derived metadata. Do not convert existing Start-frame chains silently.
Reject ambiguous combinations of reference continuity and hard anchors, with
an explanation of the available choices.

## 2. Build one consistent Ref2VA reference manifest

Extend `_reference_bindings` and `_ref2va_references` in
`storyboard/server/backends/vpipe_backend.py` to include the continuity frame.
Use the same manifest for encoder inputs, numbered prompt references, and the
Resolved view so labels cannot drift from actual input order.

Keep these roles explicit in the prompt:

- Previous final frame: opening composition, character position, environment,
  lighting, and direction of travel.
- Original character portrait: character identity and persistent appearance.
- Environment/style references: the intended setting and visual treatment.
- Speaker voice reference: voice identity when native speech is selected.

Retain the original portrait on every scene to help limit accumulated identity
drift. Validate the complete reference set against the limits; never silently
drop a portrait or continuity reference to make room. Deduplicate identical
files without losing their roles.

Do not promise exact matching from a prompt such as “continue from Picture 1.”
The model's response must be evaluated with actual generated clips.

## 3. Extend dependency handling and stale detection

Reference continuity needs the same dependency guarantees as frame chaining:

- Resolve the newest valid final frame, excluding stale frames from older takes.
- Render source scenes before dependents; reject cycles and missing sources.
- Block dependent scenes if the source fails or is interrupted.
- Save final frames even in draft mode when downstream scenes need them.
- Mark downstream scenes stale when a source is rerendered.
- Fingerprint the continuity mode, source take identity, and reference content.
  A stable filename alone cannot detect a changed image at that path.
- For a selected-scene render, report missing/stale prerequisites before
  starting and provide an explicit way to include them.

Relevant integration points:

- `storyboard/server/orchestrator.py`: `_pending`, `_resolve_chain`, `_run_one`.
- `storyboard/server/store.py`: reference identities, render fingerprints,
  persisted defaults, and chain previews.
- `storyboard/server/backends/vpipe_backend.py`: `_effective_video_model`,
  `_has_downstream_chain`, reference assembly, and resolved prompts.
- `storyboard/js/app.js`: continuity controls, effective model, dependency
  preview, and dialogue compatibility messaging.

## 4. Make voice handling consistent and automatic

Choose one dialogue approach across the sequence unless a deliberate exception
is needed:

**Native Ref2VA speech:** supply the same speaker voice reference in every
scene. H3 generates picture and speech together. This is the first option to
test for integrated speaking performance, but exact wording, timing, and voice
consistency still need review. Do not apply a separate dub afterward.

**Cloned recordings:** reuse approved takes, and add a batch action to generate
missing or stale dialogue before rendering. The batch should fingerprint text,
direction, speaker, voice reference, engine, and relevant engine settings;
surface speech failures before expensive video generation; and let users
preview the results. Avoid regenerating an approved current take unnecessarily.

A recorded dub is applied after video generation. It does not currently drive
exact lip timing. Accurate synchronization would need a separately validated
audio-driven or lip-sync stage. Sending audio as a voice reference is not proof
that H3 will reproduce a prerecorded performance exactly.

Make Replace/Mix controls applicable only to recordings. Explain in the UI
that Replace removes generated ambience too. Apply audio-mode-only changes by
remuxing existing media where possible rather than rerendering video.

## 5. Finish continuity at assembly time

Matching a final frame to an opening frame does not preserve camera velocity,
gesture timing, or sound across the boundary. The current assembly joins clips
directly. A later assembly improvement should provide:

- Boundary previews showing the end and beginning of adjacent scenes.
- Optional trimming and short transitions, selected after reviewing the cut.
- A continuous ambience/music track independent of per-scene dialogue.
- Audio fades and level matching to reduce audible joins.

Crossfades can hide some cuts but may create ghosting; they are not a substitute
for coherent camera and action direction. Native generated dialogue and
ambience may share one track, so mixing a continuous bed needs listening checks.

## Delivery and validation

1. Implement reference continuity, reference-role prompts, dependency handling,
   fingerprints, and clear model/source feedback.
2. Add batch preparation of missing/stale cloned recordings and reliable reuse
   of approved takes.
3. Add boundary review and assembly audio/transition controls.
4. Investigate previous-video reference conditioning only after the image-based
   path is evaluated. Official Ref2VA video support does not establish that the
   local VPIPE adapter already supports it correctly.

Start with a separate three-scene copy of the Kermit board, leaving the active
render untouched. Compare independent Ref2VA, Ref2VA reference continuity, and
FL2VA frame chaining with cloned recordings. Keep duration, resolution, and
shared scene direction comparable; test native speech and recorded speech as
separate runs. Check identity drift, opening composition, camera motion at cuts,
voice similarity, wording, lip timing, and audio discontinuities.

Automated checks should cover reference numbering and limits, retained cast
portraits, correct model selection, cycle/failure handling, downstream stale
propagation, changed source content at the same filename, current audio reuse,
and prevention of double dialogue. Extend the existing tests in
`storyboard/tests/scene_layers.py` and `storyboard/tests/render_all.py`.

The first milestone is complete when one batch automatically passes each
finished scene's final frame into the next Ref2VA request while retaining the
character portrait and selected voice path, with visible and accurate status.
Whether the resulting joins are smooth enough is a separate visual/audio
acceptance check, not something a successful pipeline run can guarantee.

## Beyond the first milestone

Exact opening-frame constraints plus separate identity references plus native
voice conditioning in one generation are not implemented by the current
Storyboard paths. Achieving that combination requires a backend/model workflow
that demonstrably supports all three together. Treat hybrid checkpoints or
mixed conditioning as a separate experiment, with compatibility and quality
validation before exposing them as a supported mode.
