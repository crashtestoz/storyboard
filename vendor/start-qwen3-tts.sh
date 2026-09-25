#!/usr/bin/env bash
# Start the Qwen3-TTS voice-clone server that Storyboard's "qwen3-clone"
# speech engine talks to (tts-services.json -> http://127.0.0.1:8790).
#
#   vendor/start-qwen3-tts.sh              # 127.0.0.1:8790
#   vendor/start-qwen3-tts.sh --port 8791  # any serve_qwen3_tts.py option
#
# The first run creates vendor/.venv and installs requirements.txt (a few
# minutes); the first launch then downloads the ~2.5 GB model and a small
# Whisper model into the Hugging Face cache. Later runs just start.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"
STAMP="$VENV/.requirements-installed"

pick_python() {
  local py
  for py in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    for cand in "/opt/homebrew/bin/$py" "$py"; do
      if command -v "$cand" >/dev/null 2>&1 &&
         "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        command -v "$cand"
        return 0
      fi
    done
  done
  return 1
}

if [[ ! -x "$VENV/bin/python" ]]; then
  PY="$(pick_python)" || { echo "error: Python 3.10+ is required (brew install python@3.14)" >&2; exit 1; }
  echo "Creating $VENV with $PY ..."
  "$PY" -m venv "$VENV"
fi

# (Re)install when requirements.txt is newer than the last install.
if [[ ! -f "$STAMP" || "$HERE/requirements.txt" -nt "$STAMP" ]]; then
  echo "Installing Qwen3-TTS requirements (first run takes a few minutes) ..."
  "$VENV/bin/python" -m pip install --upgrade pip >/dev/null
  "$VENV/bin/python" -m pip install -r "$HERE/requirements.txt"
  touch "$STAMP"
fi

exec "$VENV/bin/python" "$HERE/serve_qwen3_tts.py" "$@"
