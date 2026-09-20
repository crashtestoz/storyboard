# Scene rendering inputs

## Aspect ratio and frame size

The local MiniMax H3-Base backend offers the documented 21:9, 16:9, 4:3,
1:1, 3:4, and 9:16 aspect ratios. Its base canvases use a 768-pixel short
edge, rounded to H3's required multiple of 32:

| Aspect | H3-Base canvas | Reduced local canvases |
| --- | --- | --- |
| 21:9 | 1792 × 768 | 1344 × 576, 1120 × 480 |
| 16:9 | 1344 × 768 | 960 × 544, 832 × 480 |
| 4:3 | 1024 × 768 | 768 × 576, 640 × 480 |
| 1:1 | 768 × 768 | 576 × 576, 480 × 480 |
| 3:4 | 768 × 1024 | 576 × 768, 480 × 640 |
| 9:16 | 768 × 1344 | 544 × 960, 480 × 832 |

The smaller canvases are local performance options. MiniMax's 2K output uses
the separate H3-Regenerate-2K stage, which is not included in this backend.
All output is 24 fps. Existing projects with an older custom size keep that
value until a supported size is selected.

Define shared environment in Scene Description and shared ambience in Background
Sound. Both are optional. Enabled background sound is included in each generated
clip; disabling it does not automatically create a separate soundtrack later.

Select the Cast members in each scene. Their descriptions are included once.
Ref2VA also receives their portraits; the prompt assigns portraits to identity
and Cast text to persistent appearance details. Shot instructions take priority
for actions, posture, environment, and explicit appearance changes. A reference
voice is supplied only for the speaker, when native dialogue is selected.

## Image references

Scene reference images have an optional tag and role. For example, give a
corridor image the tag `falcon-corridor` and role `environment`, then write:

> C-3PO walks through @falcon-corridor.

The backend replaces the tag with the numbered picture identifier matching the
actual encoder input order. Unknown/duplicate tags and more than nine unique
Ref2VA images produce an error rather than silently discarding references.
Tags use letters, numbers, hyphens, and underscores. Shared descriptions may use
tags when those images are supplied in every affected shot.

Selected Cast portraits are attached and named automatically; mentioning the
character by name is sufficient. Project style images are a library, not an
automatic input: a style image is only sent for a shot once it's also added to
that shot's own reference images. A Start frame or End frame automatically selects FL2VA and wires those images
as hard first/last-frame anchors; separate Ref2VA images are not sent on that
path. With no anchors, Ref2VA receives the combined ordered reference set and
its nine-image limit applies. A chained Start frame resolves to the previous
shot's last saved frame before the FL2VA request is generated.

### Reference continuity

Use **Continue from shot N using Ref2VA references** to supply the previous
scene's final frame alongside the original cast portraits and native speaker
voice (plus any style reference explicitly added to this shot). This is
guidance, not a pinned first frame. It cannot
be combined with Start/End anchors. **Continue all scenes with Ref2VA** applies
this relationship across the board. Dependencies resolve before each render,
retain frames in draft mode, detect changed frame content, and block on failed
sources. Selected renders include missing/stale prerequisites automatically;
links must point to an earlier scene.

## Dialogue source

- **Dialogue-window recording** (new scene default): generate and preview the
  take before rendering. The video is prompted without speech and the recording
  is mixed with the generated ambience and effects. Missing or outdated takes
  are prepared automatically before video generation; speech failures block the
  batch. **Prepare all dialogue** prepares/reuses takes without rendering video. Compatible TTS engines receive voice direction separately.
- **H3 native speech**: requires Ref2VA and a speaker with a reference voice. H3
  receives dialogue and direction and creates a new performance. It does not use
  the Dialogue-window take. Exact speech timing is not guaranteed.
- **Legacy automatic selection**: retained for existing scenes. Uses native
  speech with a Ref2VA speaker voice; otherwise uses the existing recording path.
  Select an explicit source when editing existing scenes.

Keep dialogue out of visual prompts and sound accents. Keep shared ambience out
of shot descriptions. Use sound accents for events specific to that shot.

## Preview and rendering

The Resolved tab requests the prompt from the backend assembly function and shows
reference numbering, effective model, dialogue source, and model limitations.
Changes to scene text, selected Cast, reference tags/roles, speaker, voice,
direction, or dialogue source invalidate render fingerprints. Changing prompt
assembly versions also invalidates older renders once. Render-all selects stale
scenes and downstream chains; explicitly selected renders process those scenes.

Neither reference conditioning nor prompt directions guarantee exact generated
performance. Check the output and allow enough clip time for spoken dialogue.

## Final-cut controls

The final-video panel exposes crossfade duration, per-scene edge audio fades,
audio level matching, and a continuous looping background audio file with volume.
The scene editor exposes trims and a side-by-side boundary review. These settings
affect assembly only. Continuity still uses the original final rendered frame,
so review cuts when trimming scene endings. Crossfades overlap both picture and
sound and shorten the final duration.

Replace mode pads short dialogue with silence to preserve the entire video.
Re-assembling reapplies current recordings when mix/replace changed; it does not
synthesize missing takes. Prepare dialogue first if a recording is stale.
