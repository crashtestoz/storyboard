#!/usr/bin/env bash
# Start Storyboard -> Video with a Python version new enough for server/.
#
# Usage:
#   ./start.sh
#   ./start.sh --restart
#   ./start.sh --restart --port 9878
#   ./start.sh --port 9878
#   ./start.sh --lan

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESTART=0
PORT=9877
ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --restart)
      RESTART=1
      shift
      ;;
    --port)
      if [[ $# -lt 2 ]]; then
        echo "error: --port needs a value" >&2
        exit 1
      fi
      PORT="$2"
      ARGS+=("$1" "$2")
      shift 2
      ;;
    --port=*)
      PORT="${1#--port=}"
      ARGS+=("$1")
      shift
      ;;
    *)
      ARGS+=("$1")
      shift
      ;;
  esac
done

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

server_cmd() {
  if [[ "${#ARGS[@]}" -gt 0 ]]; then
    "$PY" -u -m server "${ARGS[@]}"
  else
    "$PY" -u -m server
  fi
}

echo "using $("$PY" -V 2>&1) at $PY"
cd "$ROOT"

if [[ "$RESTART" == 1 ]]; then
  pids=()
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && pids+=("$pid")
  done < <(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
  if [[ "${#pids[@]}" -gt 0 ]]; then
    echo "stopping listener(s) on port $PORT: ${pids[*]}"
    kill "${pids[@]}"
    for _ in {1..50}; do
      if ! lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        break
      fi
      sleep 0.1
    done
  fi

  if lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "error: port $PORT is still in use after stopping old listener(s)" >&2
    exit 1
  fi

  LOG="$ROOT/server.log"
  if [[ "${#ARGS[@]}" -gt 0 ]]; then
    nohup "$PY" -u -m server "${ARGS[@]}" >"$LOG" 2>&1 &
  else
    nohup "$PY" -u -m server >"$LOG" 2>&1 &
  fi
  PID="$!"
  echo "$PID" > "$ROOT/server.pid"
  sleep 0.5
  if ! kill -0 "$PID" >/dev/null 2>&1; then
    echo "error: server exited immediately; see $LOG" >&2
    exit 1
  fi
  echo "started detached on port $PORT: pid $PID"
  echo "log: $LOG"
  exit 0
fi

server_cmd
