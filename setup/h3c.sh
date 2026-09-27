#!/usr/bin/env bash
# Set up antirez's h3.c as Storyboard's MiniMax H3 engine, instead of vpipe.
#
#   setup/h3c.sh                  # build h3.c + download the weights
#   setup/h3c.sh --switch         # ...and switch server-config.json to it
#   setup/h3c.sh --switch-only    # just flip the config (already set up)
#   setup/h3c.sh --back           # switch back to vpipe (same as setup/vpipe.sh --switch-only)
#
#   H3C_DIR=/path/to/h3.c setup/h3c.sh   # somewhere other than ../h3.c
#
# Download size: ~196 GiB, not the ~268 GiB of the two folders h3.c reads.
# FL2VA/ and Ref2VA/ ship byte-identical text encoder and VAEs (checked by
# LFS hash). Only the transformer differs, so Ref2VA's shared parts are
# symlinked to FL2VA's. (The full repo is ~600 GB with its diffusers copy,
# none of which h3.c uses.)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$ROOT/server-config.json"
H3C_DIR="${H3C_DIR:-$(cd "$ROOT/.." && pwd)/h3.c}"
MODEL_DIR="$H3C_DIR/MiniMax-H3"
REPO="MiniMaxAI/MiniMax-H3"
NEED_GIB=200

if [[ -t 1 ]]; then B=$'\033[1m'; G=$'\033[32m'; R=$'\033[31m'; N=$'\033[0m'; else B=""; G=""; R=""; N=""; fi
ok()  { printf '%s✓%s %s\n' "$G" "$N" "$1"; }
die() { printf '%sStopped:%s %s\n' "$R" "$N" "$1" >&2; exit 1; }

PY="$(command -v /opt/homebrew/bin/python3 || command -v python3)"

set_backend() {  # set_backend h3c|vpipe
  [[ -f "$CONFIG" ]] || echo '{}' > "$CONFIG"
  "$PY" - "$CONFIG" "$1" "$H3C_DIR/h3" "$MODEL_DIR" <<'EOF'
import json, sys
path, backend, binary, models = sys.argv[1:]
doc = json.load(open(path))
doc["backend"] = backend
if backend == "h3c":
    doc["h3cBinary"] = binary
    doc["h3cModelDir"] = models
    doc.setdefault("h3cOptions", {"layers": 50, "reuse": 1, "defaultSteps": 20})
open(path + ".tmp", "w").write(json.dumps(doc, indent=2) + "\n")
import os; os.replace(path + ".tmp", path)
EOF
  ok "server-config.json: \"backend\": \"$1\" — restart with ./start.sh --restart"
}

case "${1:-}" in
  --back)        set_backend vpipe; exit 0 ;;
  --switch-only) set_backend h3c; exit 0 ;;
  --switch|"")   ;;
  *)             sed -n '2,15p' "$0"; exit 2 ;;
esac

# 1. build
command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null ||
  die "ffmpeg/ffprobe not on PATH — brew install ffmpeg"
if [[ ! -d "$H3C_DIR/.git" ]]; then
  git clone https://github.com/antirez/h3.c "$H3C_DIR"
else
  git -C "$H3C_DIR" pull --ff-only || true
fi
make -C "$H3C_DIR" -j8 h3 >/dev/null
ok "built $H3C_DIR/h3"

# 2. weights
HF="$(command -v hf || true)"
[[ -n "$HF" ]] || die "the Hugging Face CLI is needed: brew install huggingface-cli (or pip install -U huggingface_hub)"
mkdir -p "$MODEL_DIR"
free_gib=$(df -g "$MODEL_DIR" | awk 'NR==2 {print $4}')
if [[ ! -f "$MODEL_DIR/Ref2VA/transformer/model.safetensors.index.json" && "$free_gib" -lt "$NEED_GIB" ]]; then
  die "only ${free_gib} GiB free at $MODEL_DIR; ~${NEED_GIB} GiB needed. Set H3C_DIR to a bigger drive."
fi
echo "${B}Downloading $REPO (FL2VA + Ref2VA transformer, ~196 GiB). Re-run to resume.${N}"
# One --include per pattern: hf 2.x reads any further words as explicit file
# names and then ignores --include altogether.
INCLUDE=()
for pat in "FL2VA/transformer/*" "FL2VA/text_encoder/*" "FL2VA/tokenizer/*" \
           "FL2VA/video_vae/*" "FL2VA/audio_vae/*" "FL2VA/model_index.json" \
           "Ref2VA/transformer/*" "Ref2VA/tokenizer/*" "Ref2VA/model_index.json"; do
  INCLUDE+=(--include "$pat")
done
"$HF" download "$REPO" --local-dir "$MODEL_DIR" "${INCLUDE[@]}"
# Include globs match across "/", so nothing above may name a Ref2VA folder
# that is about to become a symlink: a real folder there holding only JSON
# would be kept, and h3.c would find no weights in it.
for part in text_encoder video_vae audio_vae; do
  link="$MODEL_DIR/Ref2VA/$part"
  if [[ -d "$link" && ! -L "$link" ]] && ! find "$link" -name '*.safetensors' | grep -q .; then
    rm -rf "$link"   # weightless stub from an earlier, broader download
  fi
  [[ -e "$link" ]] || ln -s "../FL2VA/$part" "$link"
done
ok "weights in $MODEL_DIR"

(cd "$H3C_DIR" && ./h3 --info -d "$MODEL_DIR")

# 3. switch (optional)
if [[ "${1:-}" == "--switch" ]]; then
  set_backend h3c
else
  echo
  echo "To make Storyboard use it:  setup/h3c.sh --switch-only"
fi
