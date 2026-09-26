#!/usr/bin/env bash
# Install Stable Audio 3 (MLX, Apple Silicon) for Storyboard soundtracks.
#
# Storyboard links to a soundtrack engine rather than vendoring it, the same
# way it treats speech engines: this script puts the upstream MLX runtime in
# its own folder with its own .venv, downloads the weights into a Hugging Face
# cache beside it (so ~7 GB does not land on the boot disk), and points
# soundtrack-services.json at it.
#
# Usage:
#   setup/install-stable-audio-3.sh
#   setup/install-stable-audio-3.sh --dir /Volumes/KINGSTON/ai-diffusers/stable-audio-3
#   setup/install-stable-audio-3.sh --models sm-music        # small only (~1.9 GB)
#   setup/install-stable-audio-3.sh --models medium,sm-music # default (~7.3 GB)
#
# Re-running is safe: the clone is fast-forwarded and present weights are kept.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="$(cd "$ROOT/.." && pwd)/stable-audio-3"
MODELS="medium,sm-music"
REPO="https://github.com/Stability-AI/stable-audio-3"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --dir=*) DIR="${1#--dir=}"; shift ;;
    --models) MODELS="$2"; shift 2 ;;
    --models=*) MODELS="${1#--models=}"; shift ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "error: unknown option $1" >&2; exit 1 ;;
  esac
done

if [[ "$(uname -s)/$(uname -m)" != "Darwin/arm64" ]]; then
  echo "error: the MLX runtime needs an Apple Silicon Mac" >&2
  exit 1
fi
for tool in git uv ffmpeg; do
  command -v "$tool" >/dev/null || { echo "error: $tool is not on PATH (brew install $tool)" >&2; exit 1; }
done

if [[ -d "$DIR/.git" ]]; then
  echo "→ Updating $DIR"
  git -C "$DIR" pull --ff-only
elif [[ -e "$DIR" ]]; then
  echo "error: $DIR exists but is not a git checkout" >&2
  exit 1
else
  echo "→ Cloning $REPO → $DIR"
  git clone --depth 1 "$REPO" "$DIR"
fi

MLX="$DIR/optimized/mlx"
# Weights are symlinked into models/mlx/ from the HF cache, so the cache has to
# live on this drive too or the symlinks would point at the boot disk.
export HF_HUB_CACHE="$DIR/hf-cache"
mkdir -p "$HF_HUB_CACHE"

echo "→ Installing the MLX runtime and weights ($MODELS)"
(cd "$MLX" && ./install.sh -y --download "$MODELS")

echo "→ Smoke test: 5 s of sm-music"
SMOKE="$MLX/output/storyboard-smoke.wav"
"$MLX/.venv/bin/python" "$MLX/scripts/sa3_mlx.py" \
  --prompt "warm cinematic strings, slow" --dit sm-music --decoder same-s \
  --seconds 5 --seed 1 --out "$SMOKE" >/dev/null
[[ -s "$SMOKE" ]] || { echo "error: smoke test produced no audio" >&2; exit 1; }
echo "  ✓ $SMOKE"

CONFIG="$ROOT/soundtrack-services.json"
if [[ -f "$CONFIG" ]]; then
  echo "→ $CONFIG already exists — left alone. It should point \"path\" at:"
  echo "    $MLX"
else
  cat > "$CONFIG" <<JSON
{
  "services": [
    {
      "id": "sa3-mlx",
      "label": "Stable Audio 3 (MLX, local)",
      "kind": "sa3-mlx",
      "path": "$MLX"
    }
  ]
}
JSON
  echo "→ Wrote $CONFIG"
fi
echo "✓ Stable Audio 3 is ready. Restart Storyboard (./start.sh --restart) to pick it up."
