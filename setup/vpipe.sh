#!/usr/bin/env bash
# Switch Storyboard's video engine back to vpipe.
#
#   setup/vpipe.sh --switch-only    # set "backend": "vpipe" in server-config.json
#
# The counterpart of setup/h3c.sh --switch-only. Installing or updating vpipe
# itself is ./setup.sh's job; this only changes which engine renders.
# Your h3c settings are left in place, so switching back to h3c later is
# again one command.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$ROOT/server-config.json"

if [[ -t 1 ]]; then G=$'\033[32m'; Y=$'\033[33m'; N=$'\033[0m'; else G=""; Y=""; N=""; fi
ok()   { printf '%s✓%s %s\n' "$G" "$N" "$1"; }
warn() { printf '%s!%s %s\n' "$Y" "$N" "$1"; }

PY="$(command -v /opt/homebrew/bin/python3 || command -v python3)"

case "${1:-}" in
  --switch-only) ;;
  *) sed -n '2,9p' "$0"; exit 2 ;;
esac

[[ -f "$CONFIG" ]] || echo '{}' > "$CONFIG"
"$PY" - "$CONFIG" <<'EOF'
import json, os, sys
path = sys.argv[1]
doc = json.load(open(path))
doc["backend"] = "vpipe"
open(path + ".tmp", "w").write(json.dumps(doc, indent=2) + "\n")
os.replace(path + ".tmp", path)
EOF

# Warn, don't refuse: the server's own health check says the same at start.
vpipe="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("vpipe",""))' "$CONFIG")"
workspace="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("workspace",""))' "$CONFIG")"
[[ -z "$vpipe" || -x "$vpipe" ]] || warn "vpipe binary not found at $vpipe — run ./setup.sh"
[[ -z "$workspace" || -d "$workspace/models" ]] || warn "no models/ under $workspace — run ./setup.sh"

ok "server-config.json: \"backend\": \"vpipe\" — restart with ./start.sh --restart"
