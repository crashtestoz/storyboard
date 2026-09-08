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
./serve.sh --tts mcc-qwen3 --tts-url http://mcc-host:PORT
```

Standard library Python only — nothing to install. Run the script, it prints a
URL, Ctrl-C stops it. Port 9877 by default so it never collides with
`vpipe-web-ui` on 9876.

**The workspace matters.** vpipe resolves `models/` and its LMDB model
registry relative to the directory it is launched from, so `--workspace` must
be the directory the models were prepared in. The startup banner lists which
models and speech engines it can actually see.

## Where things live

```
<workspace>/projects/<board-slug>/
├── storyboard.json      the whole board: scene, sound, cast, shots
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
Both are appended after the visual description, because MiniMax H3 generates
its soundtrack in the same denoise loop as the picture and its own examples put
sound in a trailing clause. Dropped automatically for a still.

**A cast.** Characters have a required name and description, and optional
reference image and voice clip. A shot casts whoever appears in it and refers
to them by name in its prompt. On Ref2VA the portrait becomes an image
reference and the voice clip a soundtrack reference, respecting that model's
real limits (9 images, 3 soundtracks, 12 total).

**Frame anchors and chaining.** A shot can open — and close — on an exact
frame, including "the last frame of the previous shot", which is how a
sequence reads as continuous. Verified: a chained shot's first frame matches
its predecessor's last.

**Dialogue.** Spoken lines are synthesised by a selectable speech engine and
mixed over the finished clip, ducking the generated soundtrack rather than
replacing it. This is deliberate: H3 produces a soundtrack, but nothing in its
documentation claims intelligible lip-synced speech, and dubbing separately
means a line can be rewritten in seconds without re-rendering half an hour of
video.

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

| Speech engine | Notes |
| --- | --- |
| `vpipe-moss` | MOSS-TTS 8B through vpipe's own text-to-speech stage. Fully local, and can clone a voice from a character's reference clip. Needs the MOSS models fetched (~9 GB). |
| `mcc-qwen3` | Qwen3-TTS through MCC's HTTP service — the same voices as the rest of that system. Needs `--tts-url`. **The request shape is an assumption**, since the MCC sources were not readable from this repo; adjust `server/tts/mcc_qwen3.py` if it does not match. |
| `none` | Dialogue is stored but not spoken. |

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

## Known gaps

- The theme is a placeholder, not MCC's palette. All colours live in
  `css/tokens.css` and `css/app.css` has none, so matching MCC is a
  value-for-value swap in one file — but this repo had no read access to
  `admin/mcc`.
- The ComfyUI backend is a scaffold with an integration plan, not an
  implementation.
- The `mcc-qwen3` endpoint contract is assumed, as above.
- Board editing is a whole-board save, so two browser tabs on one board are
  last-write-wins.
