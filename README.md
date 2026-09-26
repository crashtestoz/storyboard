# Storyboard

**Storyboard runs on [vpipe](https://github.com/tgo-app-dev/vpipe)** by T-Go LLC,
the engine that runs AI video models on Apple Silicon Macs. Storyboard is the
creative layer on top: you plan a film shot by shot, and vpipe renders each
shot with **MiniMax H3**, a video model that makes picture and sound together.

![Storyboard — creators, not workflow engineers](assets/storyboard-infographic.png)

Storyboard is for creators who want to direct a sequence of shots without
building or debugging a node workflow.

- **Shot-by-shot storytelling** — plan, reorder and refine a complete piece.
- **Characters that stay the same** — give a character a portrait and a
  description, and they look the same in every shot.
- **Dialogue, sound and music** — write spoken lines, clone a character's
  voice, and add a soundtrack under the finished video.
- **AI help** — rewrite rough ideas into clear shot prompts, or ask the
  Storyboard AD (a chat assistant) to review and edit your board.
- **Render, review, assemble** — see what worked and why something failed,
  then join the shots into one video.
- **Runs on your own Mac** — nothing is uploaded; your models, your files.

## What you need

- A **Mac with Apple Silicon** (M1 or newer) running **macOS 26** or newer
- **Memory:** Storyboard is developed and tested on a 48 GB Mac. With less,
  MiniMax H3 may be very slow or fail.
- **200 GB of free disk space** on the drive you install to — MiniMax H3 is a
  115 GB download, converted to a 65 GB model. An external SSD is fine.
- An internet connection, and a few hours for the first model download

## Install

You type a few commands into **Terminal** (in Applications → Utilities).
Copy each grey block, paste it into Terminal and press Return.

**1. Pick where Storyboard will live.** Everything — Storyboard, the video
model and your projects — is kept in one folder, so choose a drive with
200 GB free. For your home folder:

```sh
mkdir -p ~/Storyboard && cd ~/Storyboard
```

For an external drive named *MyDrive*, use `/Volumes/MyDrive/Storyboard`
instead of `~/Storyboard`.

**2. Download Storyboard.**

```sh
git clone https://github.com/crashtestoz/storyboard.git
cd storyboard
```

If macOS asks to install the "command line developer tools", click
**Install**, wait for it to finish, then paste the `git clone` line again.

**3. Run the setup script.**

```sh
./setup.sh
```

It checks your Mac, installs the tools Storyboard needs (Homebrew, Python,
FFmpeg), installs the **Vpipe Manager** app (which contains vpipe), and then
downloads and prepares **MiniMax H3**. It asks before each big step. The model
download takes hours: leave the Mac plugged in; it is kept awake for you.

If anything is interrupted, run `./setup.sh` again — finished steps are
skipped and the download carries on where it stopped.

When it is done your folder looks like this:

```text
Storyboard/
├── storyboard/            the app (this download)
├── vpipe-workspace/       vpipe's models, including MiniMax H3
└── storyboard-projects/   your storyboards and videos
```

## Start Storyboard

```sh
cd ~/Storyboard/storyboard
./start.sh
```

Then open **http://localhost:9877** in your browser. Leave Terminal open while
you work; press **Control-C** in it to stop Storyboard.

To keep it running in the background instead, use `./start.sh --restart`
(the same command restarts it after an update).

## Your first storyboard

1. Click **New** and give the project a name.
2. In **Scene description** (left column), describe what stays the same in
   every shot — the place, the people, the look.
3. Click **Add shot** and describe what happens: the camera, the action.
   Click the **Storyboard** logo (top left) for a prompt guide with templates
   you can copy.
4. Click **Render this shot**. The first render loads the model, so it is the
   slowest; a shot takes tens of minutes, depending on its length and your Mac.
5. Add more shots, then **Render all**. When every shot is done, the
   **Final Video** tab joins them — press **Download** to save the video.

**Tip:** turn on **Settings → Video → Draft mode** for fast, rough renders
while you work out the shots, then turn it off for the final pass.

## Add more (optional)

Storyboard works with just MiniMax H3. These extras add more features; each
is one command, and you can add them any time:

```sh
setup/add.sh             # shows this list
setup/add.sh voices      # for example
```

| Command | What it adds | Download |
| --- | --- | --- |
| `setup/add.sh prompt-help` | The **Rewrite** buttons and the **Storyboard AD** chat (Ollama with Llama 3.1 8B) | ~5 GB |
| `setup/add.sh voices` | Spoken dialogue in each character's own voice (Qwen3-TTS; runs in its own Terminal window) | ~3 GB |
| `setup/add.sh voices-moss` | Higher-quality voice cloning through vpipe (MOSS-TTS 8B) | ~22 GB |
| `setup/add.sh images` | **Create Image** previews with Krea-2 through vpipe | ~33 GB |
| `setup/add.sh images-mflux` | **Create Image** with mflux (Z-Image Turbo) instead | ~11 GB |
| `setup/add.sh soundtrack` | Music under the final video (Stable Audio 3) | ~7 GB |

After adding one, restart Storyboard (`./start.sh --restart`) and choose it in
**⚙ Settings**. Cloud services (OpenAI, Anthropic, Gemini and others) can also
power the AI buttons: add one in **Settings → General → Prompt rewriting**.

## Updating

```sh
cd ~/Storyboard/storyboard
git pull
./setup.sh
./start.sh --restart
```

`./setup.sh` updates vpipe if the new version of Storyboard needs a newer one.
Your projects and settings are never touched by an update.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| `permission denied` when running a script | Run it with bash: `bash setup.sh` |
| "Not enough free space on this drive" | Move the whole `Storyboard` folder to a drive with 200 GB free and run `./setup.sh` there, or run `./setup.sh --no-model` to set up the rest first. |
| The model download stopped | Run `./setup.sh` again; it resumes. The log is in `vpipe-workspace/setup/prepare-minimax-h3.log`. |
| The page does not open | Check that Terminal shows the Storyboard banner. If it says `cannot bind`, Storyboard is already running — run `./start.sh --restart`. |
| The startup banner says `ref2va … not prepared` | MiniMax H3 is not finished yet — run `./setup.sh` again. |
| Renders are very slow | Close other large apps, use Draft mode, or shorten the shot. The Details panel shows memory use while a render runs. |

Still stuck? Open an issue on
[GitHub](https://github.com/crashtestoz/storyboard/issues) with what you ran
and what Terminal printed.

## More documentation

- [Advanced setup](docs/ADVANCED-SETUP.md) — installing by hand, building vpipe
  from source, keeping models elsewhere, server options and every setting.
- [How Storyboard works](docs/REFERENCE.md) — what each part does, the engines,
  driving it from an AI assistant (MCP), tests and design decisions.
- [Design notes](docs/STORYBOARD-UI-DESIGN.md) — the original design and the
  orchestrator contract.

## Credits

Storyboard stands on the work of these projects and their makers. Each keeps
its own licence; models are downloaded from their makers when you install
them, and their terms apply to what you make with them.

| Project | Made by | Used for | Licence |
| --- | --- | --- | --- |
| [vpipe](https://github.com/tgo-app-dev/vpipe) | T-Go LLC | The engine every render runs on | Apache 2.0 |
| [MiniMax H3](https://huggingface.co/MiniMaxAI/MiniMax-H3) | MiniMax (weights packaged by [Comfy-Org](https://huggingface.co/Comfy-Org/MiniMax-H3)) | Video and sound for every shot | MiniMax H3 Community License |
| [Krea-2 Turbo](https://huggingface.co/krea/Krea-2-Turbo) | Krea (M87 LoRA by mgwr) | Create Image previews | Krea-2 Community License |
| [Wan 2.2](https://huggingface.co/Wan-AI/Wan2.2-I2V-A14B) | Wan-AI (Alibaba) | Optional video engine | Apache 2.0 |
| [LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5) | Lightricks | Optional experimental video engine | LTX-2 Community License |
| [Stable Audio 3](https://github.com/Stability-AI/stable-audio-3) | Stability AI (text encoder T5Gemma by Google) | Soundtrack | Stability AI Community License (code MIT) |
| [Qwen3-TTS](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-Base) | Qwen team, Alibaba | Voice cloning | Apache 2.0 |
| [MOSS-TTS](https://huggingface.co/OpenMOSS-Team/MOSS-TTS) | OpenMOSS team (MLX version by mlx-community) | Voice cloning through vpipe | Apache 2.0 |
| [mflux](https://github.com/filipstrand/mflux) | Filip Strand | Create Image engine | MIT |
| [Z-Image Turbo](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo) | Tongyi-MAI (Alibaba) | Create Image model for mflux | Apache 2.0 |
| [Ollama](https://github.com/ollama/ollama) | Ollama | Runs the local AI for Rewrite and the AD | MIT |
| [Llama 3.1](https://www.llama.com) | Meta | Default local model for Rewrite and the AD | Llama 3.1 Community License |
| [FFmpeg](https://ffmpeg.org) | The FFmpeg developers | Joining shots, mixing dialogue and music | LGPL / GPL |
| [Homebrew](https://brew.sh) | The Homebrew maintainers | Installing tools on macOS | BSD 2-Clause |

## Free to use, built to collaborate

Storyboard is free for personal, educational, research, hobby and other
non-commercial work under its licence. Commercial use needs separate
permission, and the models and services it connects to have their own terms.

Contributions are welcome — ideas, bug reports, documentation, tests and pull
requests. Please read [`CONTRIBUTING.md`](CONTRIBUTING.md) first. Development
happens on Gitea, and GitHub is the public mirror.

## License

[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) — see
[`LICENSE`](LICENSE). Use it, modify it, contribute back — but not for
commercial purposes, and not relabelled as someone else's own work.

## Author

Peter Chodyra — [candco.com.au](https://candco.com.au)
