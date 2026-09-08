#!/usr/bin/env bash
# ==========================================================================
# Storyboard -> Video : server
# --------------------------------------------------------------------------
# Same shape as launching vpipe-web-ui: run the script, it prints a URL,
# Ctrl-C stops it.
#
#   ./serve.sh                      # localhost only, port 9877
#   ./serve.sh --port 8080          # different port
#   ./serve.sh --lan                # bind all interfaces (e.g. view on a phone)
#   ./serve.sh --backend comfyui    # scaffold only, not implemented yet
#   ./serve.sh --workspace DIR      # directory vpipe is launched from
#   ./serve.sh --vpipe PATH         # path to the vpipe CLI binary
#
# The workspace matters: vpipe resolves models/ and its LMDB model registry
# relative to the directory it is launched from, so this must be the
# directory the models were prepared in.
#
# Storyboards are saved under <workspace>/projects/<slug>/storyboard.json,
# alongside the shots they render, so a project folder is self-contained.
# ==========================================================================

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  awk 'NR>1 { if (/^#/) { sub(/^# ?/, ""); print } else { exit } }' "${BASH_SOURCE[0]}"
  exit 0
fi

PY="$(command -v python3 || true)"
if [[ -z "$PY" ]]; then
  echo "error: python3 not found." >&2
  exit 1
fi

# 3.10+ for the match/union syntax used in server/
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "error: python3 >= 3.10 required (found $("$PY" -V 2>&1))." >&2
  exit 1
fi

cd "$ROOT"
exec "$PY" -u -m server "$@"
