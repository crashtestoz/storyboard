
# Advanced setup

Most people only need [`./setup.sh`](../README.md#install) — it does
everything on this page for you. Read on to install by hand, build vpipe from
source, keep models somewhere else, or change how the server runs.

The web app itself has no Python package install step and no Node build step:
it runs on Python's standard library. The full workflow does need system tools
and model services.

### 1. Install system dependencies

macOS/Homebrew:

```sh
brew install python@3.14 ffmpeg lsof
```

Minimum versions:

| Dependency | Required for | Notes |
| --- | --- | --- |
| Python 3.10+ | The storyboard web server | `start.sh` prefers Homebrew Python 3.14, 3.13, 3.12, 3.11, then 3.10. |
| vpipe **v0.1.74+** CLI | Rendering MiniMax H3 / Ref2VA / Krea-2 pipelines | Pass with `--vpipe PATH` or set `SBV_VPIPE`. v0.1.74 is the first release with prompt-only Ref2VA and macOS 27 support — see [§2](#2-install-or-point-to-vpipe). |
| vpipe workspace with `models/` | Model discovery and render runtime | Pass with `--workspace DIR` or set `SBV_WORKSPACE`. |
| ffmpeg | Final video assembly and dialogue muxing | Without it, individual renders can still run, but final joining/dubbing is limited. |
| lsof | `./start.sh --restart` | Used to find the process listening on the selected port. |
| Ollama or OpenAI-compatible LLM service | Prompt rewrite and character-description AI buttons | Optional; configured in `llm-services.json`. |
| TTS service or MOSS models | Spoken dialogue | Optional; configured in `tts-services.json`. |
| Stable Audio 3 (MLX) | Generated soundtrack under the final cut | Optional; `setup/install-stable-audio-3.sh` installs it and writes `soundtrack-services.json`. Apple Silicon only. |
| mflux | Create Stills without vpipe's Krea-2 (Z-Image Turbo or Krea-2 via MLX) | Optional; `uv tool install mflux`. Engines are configured in `mflux-engines.json` and picked under **Settings → Create Stills → Engine**. Its **Model** list shows the matching models already in your Hugging Face cache (the choice is saved per machine in `server-config.json`); only when none is downloaded does mflux fetch the engine's default itself. |
| Node.js | Browser/JS regression tests only | Not needed to run the app. |

There is deliberately no `pip install -r requirements.txt` and no
`npm install`.

### 2. Install or point to vpipe

Storyboard needs **vpipe v0.1.74 or newer**: the first release that renders a
Ref2VA request with an explicitly empty reference list (a shot with no
Start/End frame, cast portraits or voice clip —
[tgo-app-dev/vpipe#37](https://github.com/tgo-app-dev/vpipe/issues/37)), and
whose GPU kernels build and run on macOS 27. Either install works:

**The app (easiest).** Install **Vpipe Manager** from vpipe's
[latest release](https://github.com/tgo-app-dev/vpipe/releases/latest) — the
`-with-ffmpeg` .dmg if you don't already have FFmpeg. Its command-line tool,
which is what Storyboard runs, is inside the app:

```sh
./start.sh --workspace /path/to/vpipe-workspace \
  --vpipe "/Applications/Vpipe Manager.app/Contents/Helpers/vpipe"
```

**From source.**

```sh
git clone --recursive https://github.com/tgo-app-dev/vpipe.git
cd vpipe && cmake -S . -B build && cmake --build build -j
./start.sh --workspace /path/to/vpipe-workspace \
  --vpipe /path/to/vpipe/build/apps/vpipe/vpipe
```

The configure step prints `vpipe Metal kernels: build-time metallib mode`
when the Xcode Metal toolchain is installed, which compiles the GPU kernels
into the binary. In runtime-compile mode they are compiled on each machine
at first use instead, so a build copied to another Mac depends on that Mac's
own Metal compiler; configure with `-DVPIPE_METAL_RUNTIME_COMPILE=OFF` when
you intend to hand a build to someone else.

Use environment variables instead of flags if you prefer:

```sh
export SBV_WORKSPACE=/path/to/vpipe-workspace
export SBV_VPIPE="/Applications/Vpipe Manager.app/Contents/Helpers/vpipe"
```

The workspace is vpipe's runtime root. It is where vpipe resolves `models/`
and its model registry from, so it must be the same workspace used when the
models were prepared.

Why v0.1.74: every video shot routes through Ref2VA (see
`server/backends/vpipe_backend.py::_effective_video_model`), so a shot with
nothing attached sends Ref2VA `"references": []`. Older vpipe refuses that
with `a ref2va request needs at least one reference`; if you see it, or shots
with references render but bare-prompt shots don't, upgrade vpipe.

### 3. Prepare vpipe models

Run the setup pipelines from the vpipe workspace for the models you want to
use:

```sh
cd "$SBV_WORKSPACE"
"$SBV_VPIPE" --launch setup/prepare-minimax-h3-ref2va-8bit.vpipeline
"$SBV_VPIPE" --launch setup/prepare-krea-2.vpipeline
```

| Model | Used for | On disk |
| --- | --- | --- |
| MiniMax H3 Ref2VA 8-bit | Every video shot, with or without references | ~65 GB, plus the ~115 GB full-precision download it is converted from |
| Krea-2 Turbo | Create Stills previews (optional — mflux can do stills instead) | ~33 GB |

The Ref2VA pipeline downloads the full-precision checkpoint from Hugging Face
and converts it to 8-bit on the machine, which takes a while; leave room for
both. You don't need `prepare-minimax-h3-8bit.vpipeline` (FL2VA): Storyboard
no longer routes shots to it. `prepare-krea-2.vpipeline` fetches from
ModelScope; if that times out from your network, change its `"source"` to
`"huggingface"`, accept the model's terms at huggingface.co/krea/Krea-2-Turbo,
and export `HF_TOKEN` before launching it.

If your vpipe workspace keeps setup files elsewhere, run the equivalent
prepare pipelines from that workspace. The server startup banner reports which
models are available.

### 4. Configure storyboard storage

Storyboards and uploads are saved under:

```text
<projects-folder>/<board-slug>/
```

By default this is `storyboard-projects/`, next to the storyboard folder —
where `./setup.sh` creates it. It is deliberately outside the vpipe
workspace, so storyboards are never left inside a runtime folder. Point it
somewhere else with:

```sh
export SBV_DATA_DIR=/path/to/storyboard-data
```

or from the app itself: **⚙ Settings → Storyboard data folder**, which saves
the path to `server-config.json` (next to `llm-services.json`) and restarts
the server onto it — no flag or environment variable needed. Priority, highest
first: `--data-dir` flag, `SBV_DATA_DIR`, `server-config.json`, then the
default next to the storyboard folder — so a launch script that already pins one of the first two
keeps working untouched, and Settings tells you so if it would otherwise be
overridden. Select the folder that directly contains the project folders. For
example, selecting `/Volumes/Media/storyboard-projects` stores a board at
`/Volumes/Media/storyboard-projects/<board-slug>/`.

### 5. Speech engines (optional)

**Qwen3-TTS voice clone** (the lighter option, ~2.5 GB) runs as its own small
server. Start it in a separate Terminal tab and leave it running:

```sh
vendor/start-qwen3-tts.sh
```

The first run sets up `vendor/.venv` and installs its Python packages; the
first launch downloads the model. Then pick **Qwen3-TTS voice clone** under
Settings → Speech engine. Details in [`vendor/README.md`](../vendor/README.md).

**MOSS-TTS through vpipe** (~22 GB) needs no separate server, only its models:


`vpipe-moss` needs two models in the workspace. Once:

```sh
cd "$SBV_WORKSPACE"
"$SBV_VPIPE" --launch setup/prepare-moss-tts.vpipeline
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
whatever `DEFAULT_DATA_DIR` in `server/__main__.py` says** (so projects land
directly inside that path), but it can be anywhere, which is the point — a
storyboard and its reference images are documents, and should not have to
live inside another tool's runtime directory to be usable. Set `--data-dir`,
`SBV_DATA_DIR`, or Settings → Storyboard data folder to your own path; don't
rely on the shipped default.
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

### 5b. Soundtrack (optional)

`setup/add.sh soundtrack` runs the installer below.

Settings → **Soundtrack** adds one continuous piece of music under the whole
cut, generated by [Stable Audio 3](https://github.com/Stability-AI/stable-audio-3)
or taken from your own audio file. It is separate from the **Background sound
effects**, which H3 renders into each shot.

```sh
setup/install-stable-audio-3.sh                  # clone + venv + weights (~7 GB), next to this folder
setup/install-stable-audio-3.sh --models sm-music  # small model only (~1.9 GB)
```

The script clones the upstream repo beside this one (`../stable-audio-3`),
installs its pure-MLX runtime into its own `.venv`, keeps the Hugging Face
cache inside that folder so the weights stay on the same drive, runs a 5 s
smoke test and writes `soundtrack-services.json`. Small music handles up to
2 minutes, Medium up to 6 min 20 s; longer cuts loop the music.

How it behaves:

* **Generated once every shot has a clip**, because only then is the cut's
  length known. Assembly (and "Render all") generates it if needed, then mixes
  it; **Generate now** in the tab does the same step early.
* **Cached** as `soundtrack.wav` in the project, keyed on engine, model,
  prompt, seed, reference clip and cut length. Re-assembling with nothing
  changed reuses it; a trim or a re-rendered shot that changes the length
  makes it generate again.
* **Ducked under dialogue**, timed from each recorded `dialogue.wav`, so the
  music dips just before the first word rather than reacting to it. Shots
  where H3 speaks the line itself have no separate take and are ducked for
  their whole length.
* **Reference audio** steers the tone (audio-to-audio). Use music you own or
  are licensed to use — at high strength the result can keep its melody.
* The model does not know film or composer names; the Rewrite button turns
  "like the Star Wars theme" into a description of that style.

### 6. Configure optional services

The first run creates service config files if they do not exist:

```text
tts-services.json
llm-services.json
```

Edit `tts-services.json` for speech engines such as local MOSS through vpipe,
a Qwen-style voice-clone service, or a plain HTTP TTS service. Edit
`llm-services.json` for prompt rewriting and image-based character
description services such as Ollama or an OpenAI-compatible
`/v1/chat/completions` server.

API keys are optional: a prompt-rewriting service (Ollama or
OpenAI-compatible) is called with no key unless one is set, so local
services need nothing. For one that does need a key, either paste it in
**Settings → Prompt rewriting → API key** — saved on that machine only, in the
gitignored `server-config.json` (readable by your user only) and never sent
back to the browser — or name an environment variable with `apiKeyEnv` in the
service's entry. Add `"requiresKey": true` (implied by `apiKeyEnv`) so the
service reports "needs an API key" up front instead of failing on first use.
When several are set, the Settings key wins, then the environment variable,
then an inline `apiKey` (plain text in `llm-services.json`, so the least safe).

```json
{ "id": "my-cloud-llm", "label": "Cloud LLM", "kind": "openai",
  "url": "https://api.example.com", "model": "some-model", "requiresKey": true }
```

## Running it

```sh
./start.sh                              # http://localhost:9877
./start.sh --lan                        # reachable on your LAN
./start.sh --port 8080
./start.sh --workspace DIR              # where vpipe is launched from
./start.sh --data-dir DIR               # where your storyboards live
./start.sh --vpipe PATH                 # path to the vpipe CLI binary
./start.sh --tts qwen3-clone            # default engine id (see tts-services.json)
./start.sh --llm ollama-local           # default rewrite model (see llm-services.json)
```

Run in the foreground while developing; Ctrl-C stops it.

```sh
./start.sh
```

Run or restart detached on the same port:

```sh
./start.sh --restart
./start.sh --restart --port 9877
```

Detached mode writes:

```text
server.log
server.pid
```

Port 9877 is the default so it does not collide with `vpipe-web-ui` on 9876.
`serve.sh` is still present as the older foreground-only launcher, but
`start.sh` is the recommended entry point.

## Settings reference

Every machine-specific location or service this app talks to is configurable
from outside the code — nothing here should ever need editing in a `.py` or
`.js` file. Three places to set it, later ones winning over earlier ones:

1. A built-in default — always something harmless (`127.0.0.1`, or a folder
   next to this checkout), never a real personal host.
2. A config file — `server-config.json`, `tts-services.json`,
   `llm-services.json`. `./setup.sh` writes the vpipe locations into
   `server-config.json`; the data directory can also be changed from
   **⚙ Settings** in the app itself.
3. An environment variable, or the matching CLI flag (flags win outright).

| What | Flag | Env var | Config file |
| --- | --- | --- | --- |
| Port | `--port` | `SBV_PORT` | — |
| Bind address | `--bind` / `--lan` | `SBV_BIND` | — |
| Render backend | `--backend` | `SBV_BACKEND` | — |
| vpipe workspace | `--workspace` | `SBV_WORKSPACE` | `server-config.json` (`workspace`); default `../vpipe-workspace` |
| vpipe CLI path | `--vpipe` | `SBV_VPIPE` | `server-config.json` (`vpipe`); default the CLI inside `Vpipe Manager.app` |
| Storyboard data dir | `--data-dir` | `SBV_DATA_DIR` | `server-config.json` (`dataDir`); default `../storyboard-projects` |
| ComfyUI URL | `--comfyui-url` | `SBV_COMFYUI` | — |
| Default TTS engine id | `--tts` | `SBV_TTS` | `tts-services.json` |
| Default LLM/rewrite id | `--llm` | `SBV_LLM` | `llm-services.json` |
| TTS/LLM service URLs, API keys | — | — | `tts-services.json`, `llm-services.json` — use `apiKeyEnv` in either to name an environment variable rather than checking a key in |

`llm-services.json`, `tts-services.json`, `mflux-engines.json` and
`soundtrack-services.json` are yours
to edit and are **not** tracked: git ignores them, so a `git pull` never
overwrites your services. What the repo ships is `llm-services-sample.json`,
`tts-services-sample.json` and `mflux-engines-sample.json`; loading the page
copies each one into place if your own copy is missing. Change the
`-sample.json` files only to change what a new install starts with.
`server-config.json` is created by the app and gitignored, so your data-dir
choice, per-machine model choices and any API keys saved in Settings never
get committed at all. `render-timings.json`, beside it and also gitignored,
records how long each render actually took on this machine: it is the only
source of render-time estimates (in the run log, the runtime check and the
Storyboard AD's "how long will this take"). Until a model has been timed on a
machine there is no estimate, and the runtime check is skipped rather than
held to a guess. The server only serves `index.html` and the `css/`,
`js/` and `assets/` folders, so none of these config files can be downloaded
from it either.

## Where things live

```
<projects-folder>/<board-slug>/
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
referenced in place, so a board never depends on an outside folder.
Save, load and export are all the same file.
