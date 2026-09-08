# Storyboard → Video

A storyboard-shaped front end for [vpipe](https://github.com/tgo-app-dev/vpipe):
build a video idea as an ordered sequence of shots — prompt, reference frames,
a few high-level parameters each — and let the tool generate and queue the
underlying vpipe pipelines.

**This repo is currently a UI proof of concept.** There is no backend: no vpipe
is invoked, nothing is generated, and the render queue is simulated. It exists
to settle layout, interaction and — importantly — what the failure states
should look like, before any orchestration code is written.

Design rationale, data model and the orchestrator contract:
[`docs/STORYBOARD-UI-DESIGN.md`](docs/STORYBOARD-UI-DESIGN.md).

## Running it

```sh
./serve.sh                 # http://localhost:9877
./serve.sh --port 8080     # different port
./serve.sh --lan           # bind all interfaces, e.g. to view on a phone
```

Same shape as launching `vpipe-web-ui`: run the script, it prints a URL,
Ctrl-C stops it. Static files only — it needs nothing but `python3`.

Port defaults to **9877** deliberately, so it never collides with
`vpipe-web-ui` on 9876.

### Demo flags

| URL | Effect |
| --- | --- |
| `?demo=run` | Starts the simulated render queue on load |
| `?shot=3` | Deep-links to a shot (1-based) |

`?demo=run&shot=3` is the quickest way to see the failure-handling UI.

## What the POC demonstrates

- **Two-tier prompting.** A project-level *scene description* (subject and
  style, reused everywhere) plus a per-shot prompt (action, camera, mood).
  The editor shows the concatenated result that would actually be sent to
  vpipe's `text-prompt` stage, so the split is visible rather than implied.
- **Frame anchors.** Per-shot start/end reference slots, mapping to
  `generate-video` ports 5 and 6, plus a one-click *chain start frame from
  the previous shot* toggle — which needs no new vpipe capability, only
  pointing at the previous shot's last written frame.
- **Serial queue.** One generation at a time by construction, with per-shot
  progress. Concurrent GPU-bound generations were a real source of trouble
  when driving vpipe by hand; the queue exists to make that impossible.
- **Failure states, in full.** Press *Render all* and shot 3 fails, taking
  shot 4 (chained off it) to *Blocked* rather than letting it render against
  a frame that was never written. The diagnostic panel shows which of the
  five validation checks passed and which failed.

That last point is the reason this POC has teeth. **Every real failure hit
while driving vpipe by hand exited with code 0**, because vpipe fails quietly
by design — a stage that can't proceed logs a warning and moves on. So the
UI is built around the idea that "success" means exit code *and* a complete
output manifest *and* a plausible runtime *and* evidence that denoise
actually ran — not just a zero exit. The two failure outcomes scripted into
the mock data are both bugs that genuinely happened:

- a request too short for the VAE to decode, which wrote nothing at all
- reference rows emitted in the wrong shape, so generation silently skipped
  them and finished in 1m 25s against a ~28 minute expectation

## Layout

```
├── index.html          # single page, no build step
├── serve.sh            # dev server
├── css/
│   ├── tokens.css      # ← the ONLY file to change for a reskin
│   └── app.css         # layout + components, no literal colours
├── js/
│   ├── mock-data.js    # stands in for the backend
│   └── app.js          # state, render, simulated queue
├── assets/thumbs/      # real MiniMax H3 output frames, used as thumbnails
└── docs/
    └── STORYBOARD-UI-DESIGN.md
```

## Theming

Every colour, radius, spacing step and font resolves through a custom
property declared in `css/tokens.css`; `app.css` contains no literal colours.

The current values are a neutral dark-dashboard placeholder chosen to be
close in spirit to MCC (dark ground, panel tiles, one accent, semantic status
colours) so that matching MCC properly is a value-for-value swap in that one
file rather than a re-layout. **It is not yet MCC's real palette** — this repo
didn't have read access to the MCC sources when the POC was built.

## Not built yet

Everything behind the UI, i.e. the parts the design doc calls the Generator
and the Orchestrator:

- turning a shot into a real `.vpipeline` file
- launching `vpipe --launch`, parsing its `[PROGRESS]` lines
- the actual output-manifest / runtime / log-scan validation
- persistence, so a long batch survives the tab closing
- image upload (the reference slots are click-to-fill placeholders)
