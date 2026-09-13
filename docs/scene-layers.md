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
character by name is sufficient. Project style images are used when the shot has
no local reference images. FL2VA uses actual start/end anchors and chaining,
but does not accept the scene reference list or Cast portraits. Ref2VA accepts
separately tagged references, but cannot pin an exact opening or closing frame.
Adding an anchor selects FL2VA. Adding a reference image selects Ref2VA when no
anchors are present. If both remain stored on a scene, exact FL2VA anchors take
priority and the reference-image list is not sent because H3 cannot combine the
two layouts.

## Dialogue source

- **Dialogue-window recording** (new scene default): generate and preview the
  take before rendering. The video is prompted without speech and the recording
  is mixed with the generated ambience and effects. Missing or outdated takes
  block preparation. Compatible TTS engines receive voice direction separately.
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
