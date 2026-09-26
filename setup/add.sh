#!/usr/bin/env bash
# Add optional extras to Storyboard, one at a time.
#
#   setup/add.sh            # list what can be added
#   setup/add.sh <name>     # add one, e.g. setup/add.sh prompt-help
#
# Run ./setup.sh first: this reads the vpipe locations it saved.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$ROOT/server-config.json"

if [[ -t 1 ]]; then B=$'\033[1m'; G=$'\033[32m'; R=$'\033[31m'; N=$'\033[0m'; else B=""; G=""; R=""; N=""; fi
ok()  { printf '%s✓%s %s\n' "$G" "$N" "$1"; }
die() { printf '%sStopped:%s %s\n' "$R" "$N" "$1" >&2; exit 1; }

list() {
  cat <<EOF
${B}Extras you can add${N}   (run: setup/add.sh <name>)

  ${B}prompt-help${N}   The Rewrite buttons and the Storyboard AD chat.
                Installs Ollama and a Llama 3.1 8B model.            ~5 GB
  ${B}voices${N}        Spoken dialogue in each character's own voice
                (Qwen3-TTS voice cloning, runs in its own window).   ~3 GB
  ${B}voices-moss${N}   Higher-quality voice cloning through vpipe
                (MOSS-TTS 8B, no extra window needed).               ~22 GB
  ${B}images${N}        Create Image previews with Krea-2 through vpipe.     ~33 GB
  ${B}images-mflux${N}  Create Image with mflux instead (Z-Image Turbo);
                its model downloads the first time you use it.       ~11 GB
  ${B}soundtrack${N}    Music under the final video (Stable Audio 3).        ~7 GB

Each one is optional and can be added any time. After adding one, restart
Storyboard (./start.sh --restart) and pick it in Settings.
EOF
}

config_value() {  # config_value workspace
  [[ -f "$CONFIG" ]] || die "Run ./setup.sh first."
  /opt/homebrew/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],""))' "$CONFIG" "$1" 2>/dev/null ||
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],""))' "$CONFIG" "$1"
}

vpipe_launch() {  # vpipe_launch <pipeline file in the workspace's setup/>
  local workspace vpipe
  workspace="$(config_value workspace)"; vpipe="$(config_value vpipe)"
  [[ -d "$workspace" && -x "$vpipe" ]] || die "vpipe is not set up yet — run ./setup.sh first."
  [[ -f "$workspace/setup/$1" ]] || cp "$ROOT/setup/$1" "$workspace/setup/$1"
  (cd "$workspace" && caffeinate -dims "$vpipe" --launch "setup/$1")
}

need_brew() {
  command -v brew >/dev/null 2>&1 || eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null)" ||
    die "Homebrew is missing — run ./setup.sh first."
}

case "${1:-}" in
  ""|-h|--help|list)
    list ;;

  prompt-help)
    need_brew
    command -v ollama >/dev/null 2>&1 || brew install ollama
    brew services start ollama >/dev/null
    for _ in {1..30}; do curl -sf http://localhost:11434/api/tags >/dev/null && break; sleep 1; done
    ollama pull llama3.1:8b
    ok "Prompt help is ready. It starts with your Mac; pick “Llama 3.1 8B (Ollama)” in Settings → General → Prompt rewriting." ;;

  voices)
    echo "Setting up Qwen3-TTS. The first run installs it and downloads its model (a few minutes)."
    echo "It then keeps running in this window — leave the window open while you use voices,"
    echo "and pick “Qwen3-TTS voice clone” in Settings → Audio. Next time, start it with:"
    echo "    vendor/start-qwen3-tts.sh"
    echo
    exec "$ROOT/vendor/start-qwen3-tts.sh" ;;

  voices-moss)
    echo "Downloading MOSS-TTS 8B into vpipe's models folder (~22 GB)…"
    vpipe_launch prepare-moss-tts.vpipeline
    ok "MOSS-TTS is ready. Pick “MOSS-TTS 8B via vpipe” in Settings → Audio." ;;

  images)
    echo "Downloading Krea-2 Turbo (~33 GB)…"
    if ! vpipe_launch prepare-krea-2.vpipeline; then
      cat >&2 <<'EOF'

The download failed. If the error mentions a timeout or ModelScope, fetch it
from Hugging Face instead:
  1. Sign in (or sign up, free) at https://huggingface.co
  2. Open https://huggingface.co/krea/Krea-2-Turbo and accept the licence
  3. Create a read token at https://huggingface.co/settings/tokens
  4. Run:  HF_TOKEN=<your token> setup/add.sh images-hf
EOF
      exit 1
    fi
    ok "Krea-2 is ready. Create Image uses it automatically." ;;

  images-hf)
    [[ -n "${HF_TOKEN:-}" ]] || die "Set HF_TOKEN first — see: setup/add.sh images"
    workspace="$(config_value workspace)"
    # The same pipeline with every fetch pointed at Hugging Face.
    sed 's/"source": ""/"source": "huggingface"/g' "$workspace/setup/prepare-krea-2.vpipeline" \
      > "$workspace/setup/prepare-krea-2-hf.vpipeline"
    vpipe_launch prepare-krea-2-hf.vpipeline
    ok "Krea-2 is ready. Create Image uses it automatically." ;;

  images-mflux)
    need_brew
    command -v uv >/dev/null 2>&1 || brew install uv
    uv tool install --upgrade mflux
    ok "mflux is installed. Pick an mflux engine in Settings → Image; its model downloads on first use." ;;

  soundtrack)
    exec "$ROOT/setup/install-stable-audio-3.sh" ;;

  *)
    printf 'Unknown extra: %s\n\n' "$1" >&2
    list >&2
    exit 1 ;;
esac
