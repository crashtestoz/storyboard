#!/usr/bin/env bash
# ==========================================================================
# Storyboard -> Video : dev server
# --------------------------------------------------------------------------
# Serves this directory as static files, the same shape as launching
# vpipe-web-ui: run the script, it prints a URL, Ctrl-C stops it.
#
#   ./serve.sh                 # localhost only, port 9877
#   ./serve.sh --port 8080     # different port
#   ./serve.sh --lan           # bind all interfaces so a phone can reach it
#
# There is no backend: this only hands over index.html and its assets.
# ==========================================================================

set -euo pipefail

PORT=9877
BIND=127.0.0.1
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="${2:?--port needs a value}"; shift 2 ;;
    --lan)  BIND=0.0.0.0; shift ;;
    --bind) BIND="${2:?--bind needs a value}"; shift 2 ;;
    -h|--help)
      # Print the header comment block, stopping at the first non-comment
      # line so this stays correct if the header is edited.
      awk 'NR>1 { if (/^#/) { sub(/^# ?/, ""); print } else { exit } }' \
        "${BASH_SOURCE[0]}"
      exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

PY="$(command -v python3 || true)"
if [[ -z "$PY" ]]; then
  echo "error: python3 not found — needed to serve static files." >&2
  exit 1
fi

# Refuse rather than collide, so it never silently shadows vpipe-web-ui
# (which defaults to 9876) or a stale copy of this server.
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "error: port $PORT is already in use." >&2
  echo "       something else is listening there — try: ./serve.sh --port $((PORT + 1))" >&2
  exit 1
fi

if [[ "$BIND" == "0.0.0.0" ]]; then
  LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "")"
  SHOWN="http://${LAN_IP:-0.0.0.0}:${PORT}/"
else
  SHOWN="http://localhost:${PORT}/"
fi

cat <<'BANNER'
  ______ _____  ____   ______  __ ______  ____  ___    ____  ____
 / __/ //_  __// __ \ / __ \ \/ // __ / / __ \/ _ |  / __ \/ __ \
_\ \ / /  / /  / /_/ // /_/ /\  // /_/ / / /_/ / __ | / /_/ / /_/ /
/___//_/  /_/   \____/ \____/ /_/ \____/  \____/_/ |_|/_____/_____/
                                              storyboard -> video
BANNER

echo ""
echo "  serving  $ROOT"
echo "  at       $SHOWN"
if [[ "$BIND" == "0.0.0.0" ]]; then
  echo "           (bound to all interfaces — reachable on your LAN)"
else
  echo "           (this machine only — use --lan to expose it)"
fi
echo ""
echo "  POC: UI only. No backend, no vpipe calls, render is simulated."
echo ""
echo "  Ctrl-C to stop."
echo ""

cd "$ROOT"
exec "$PY" -m http.server "$PORT" --bind "$BIND"
