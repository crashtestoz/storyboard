#!/usr/bin/env bash
# Start Storyboard -> Video with a Python version new enough for server/.
#
# Usage:
#   ./start.sh
#   ./start.sh --port 9878
#   ./start.sh --lan

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

pick_python() {
  local candidates=(
    "/opt/homebrew/bin/python3.14"
    "/opt/homebrew/bin/python3.13"
    "/opt/homebrew/bin/python3.12"
    "/opt/homebrew/bin/python3.11"
    "/opt/homebrew/bin/python3.10"
    "python3.14"
    "python3.13"
    "python3.12"
    "python3.11"
    "python3.10"
    "python3"
  )

  local py
  for py in "${candidates[@]}"; do
    if ! command -v "$py" >/dev/null 2>&1; then
      continue
    fi
    if "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      command -v "$py"
      return 0
    fi
  done

  return 1
}

PY="$(pick_python || true)"
if [[ -z "$PY" ]]; then
  echo "error: python3 >= 3.10 required. Install a newer Python or edit start.sh." >&2
  exit 1
fi

echo "using $("$PY" -V 2>&1) at $PY"
cd "$ROOT"
exec "$PY" -u -m server "$@"
