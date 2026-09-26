#!/usr/bin/env bash
# Storyboard setup — run once after downloading Storyboard.
#
#   ./setup.sh               # install everything, asking before big downloads
#   ./setup.sh --yes         # don't ask, just do it
#   ./setup.sh --no-model    # everything except the MiniMax H3 download
#   ./setup.sh --vpipe PATH  # use a vpipe CLI you built yourself
#
# What it does, in order:
#   1. checks this is an Apple Silicon Mac on macOS 26 or newer
#   2. installs Homebrew (if missing), then Python and FFmpeg
#   3. installs the Vpipe Manager app, which contains vpipe — the engine
#      Storyboard renders with
#   4. creates two folders next to this one:
#        vpipe-workspace/      vpipe's models (MiniMax H3 lives here)
#        storyboard-projects/  your storyboards
#   5. downloads and prepares MiniMax H3, the video model (~115 GB download,
#      converted to ~65 GB; this is the step that takes hours)
#
# Safe to run again: anything already done is skipped, and an interrupted
# model download carries on from where it stopped.

set -euo pipefail

VPIPE_VERSION="0.1.74"
VPIPE_DMG="VpipeManager-${VPIPE_VERSION}-with-ffmpeg.dmg"
VPIPE_URL="https://github.com/tgo-app-dev/vpipe/releases/download/v${VPIPE_VERSION}/${VPIPE_DMG}"
PIPELINES_URL="https://raw.githubusercontent.com/tgo-app-dev/vpipe/v${VPIPE_VERSION}/docs/pipelines"
H3_MODEL="local/MiniMax-H3-Ref2VA-8bit"
H3_PIPELINE="prepare-minimax-h3-ref2va-8bit.vpipeline"
NEEDED_GB=200
APP_CLI="Vpipe Manager.app/Contents/Helpers/vpipe"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT="$(dirname "$ROOT")"
CONFIG="$ROOT/server-config.json"

YES=0
NO_MODEL=0
VPIPE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes) YES=1; shift ;;
    --no-model) NO_MODEL=1; shift ;;
    --vpipe) VPIPE="$2"; shift 2 ;;
    --vpipe=*) VPIPE="${1#--vpipe=}"; shift ;;
    -h|--help) sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (try ./setup.sh --help)" >&2; exit 1 ;;
  esac
done

if [[ -t 1 ]]; then
  B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'
else
  B=""; G=""; Y=""; R=""; D=""; N=""
fi
step() { printf '\n%s==> %s%s\n' "$B" "$1" "$N"; }
ok()   { printf '    %s✓%s %s\n' "$G" "$N" "$1"; }
note() { printf '    %s\n' "$1"; }
warn() { printf '    %s!%s %s\n' "$Y" "$N" "$1"; }
die()  { printf '\n%sSetup stopped:%s %s\n' "$R" "$N" "$1" >&2; exit 1; }
ask()  {  # ask "question" -> 0 for yes
  [[ "$YES" == 1 ]] && return 0
  local reply
  read -r -p "    $1 [Y/n] " reply < /dev/tty || return 1
  [[ -z "$reply" || "$reply" =~ ^[Yy] ]]
}

printf '%sStoryboard setup%s\n' "$B" "$N"
note "Storyboard is in $ROOT"

# --- 1. this Mac --------------------------------------------------------------
step "Checking this Mac"
[[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]] ||
  die "Storyboard needs a Mac with Apple Silicon (M1 or newer)."
MACOS="$(sw_vers -productVersion)"
[[ "${MACOS%%.*}" -ge 26 ]] ||
  die "vpipe needs macOS 26 or newer; this Mac has macOS $MACOS. Update macOS in System Settings → General → Software Update."
RAM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
ok "Apple Silicon, macOS $MACOS, ${RAM_GB} GB memory"
[[ "$RAM_GB" -ge 48 ]] || warn "Storyboard is tested on 48 GB Macs; with ${RAM_GB} GB, MiniMax H3 may be very slow or fail."

# --- 2. Homebrew, Python, FFmpeg ------------------------------------------------
step "Installing the basic tools (Homebrew, Python, FFmpeg)"
if ! command -v brew >/dev/null 2>&1; then
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [[ -x "$b" ]] && eval "$("$b" shellenv)" && break
  done
fi
if ! command -v brew >/dev/null 2>&1; then
  note "Homebrew installs command-line tools on a Mac (https://brew.sh)."
  ask "Install Homebrew now? It will ask for your Mac password." ||
    die "Homebrew is needed. Install it from https://brew.sh, then run ./setup.sh again."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  eval "$(/opt/homebrew/bin/brew shellenv)"
fi
ok "Homebrew $(brew --version | head -1 | awk '{print $2}')"

find_python() {  # sets PY to a Python 3.10+
  local py
  for py in /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
            /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10; do
    if [[ -x "$py" ]] && "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      PY="$py"; return 0
    fi
  done
  return 1
}
find_python || { brew install python@3.13 && find_python; } || die "Python did not install."
ok "$("$PY" -V)"
command -v ffmpeg >/dev/null 2>&1 || brew install ffmpeg
ok "FFmpeg"

saved() {  # a location an earlier run (or Settings) saved in server-config.json
  [[ -f "$CONFIG" ]] || return 0
  "$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],""))' "$CONFIG" "$1" 2>/dev/null || true
}
WORKSPACE="$(saved workspace)"; WORKSPACE="${WORKSPACE:-$PARENT/vpipe-workspace}"
PROJECTS="$(saved dataDir)"; PROJECTS="${PROJECTS:-$PARENT/storyboard-projects}"

# --- 3. vpipe -----------------------------------------------------------------
step "Installing vpipe (the engine Storyboard renders with)"
app_version() {  # version of a Vpipe Manager.app, e.g. 0.1.74 ("0.1" + build "74")
  local plist="$1/Contents/Info.plist" short build
  short="$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$plist" 2>/dev/null || true)"
  build="$(/usr/libexec/PlistBuddy -c "Print :CFBundleVersion" "$plist" 2>/dev/null || true)"
  [[ -n "$short" ]] && echo "$short.${build:-0}"
}
at_least() {  # at_least 0.1.80 0.1.74
  [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" == "$2" ]]
}
[[ -n "$VPIPE" ]] || VPIPE="$(saved vpipe)"
# A saved path into the app is still checked below, so an older app is updated.
APP_HINT=""
case "$VPIPE" in */"$APP_CLI") APP_HINT="$(dirname "${VPIPE%/"$APP_CLI"}")"; VPIPE="" ;; esac
if [[ -n "$VPIPE" ]]; then
  [[ -x "$VPIPE" ]] || die "No vpipe program at $VPIPE."
  ok "using $VPIPE"
else
  APP=""
  # VPIPE_APPS_DIR installs somewhere other than /Applications (used by tests).
  for dir in ${APP_HINT:+"$APP_HINT"} ${VPIPE_APPS_DIR:+"$VPIPE_APPS_DIR"} /Applications "$HOME/Applications"; do
    if [[ -d "$dir/Vpipe Manager.app" ]]; then APP="$dir/Vpipe Manager.app"; break; fi
  done
  if [[ -n "$APP" ]] && at_least "$(app_version "$APP")" "$VPIPE_VERSION"; then
    ok "Vpipe Manager $(app_version "$APP") is already installed"
  else
    [[ -n "$APP" ]] && note "Vpipe Manager $(app_version "$APP") is older than $VPIPE_VERSION — updating it."
    TMP="$(mktemp -d)"
    trap 'hdiutil detach -quiet "$TMP/mnt" 2>/dev/null || true; rm -rf "$TMP"' EXIT
    note "Downloading Vpipe Manager $VPIPE_VERSION (30 MB)…"
    curl -fL --progress-bar "$VPIPE_URL" -o "$TMP/$VPIPE_DMG" || die "Could not download $VPIPE_URL"
    hdiutil attach -quiet -nobrowse -readonly -mountpoint "$TMP/mnt" "$TMP/$VPIPE_DMG"
    DEST="${VPIPE_APPS_DIR:-/Applications}"
    [[ -w "$DEST" || -n "${VPIPE_APPS_DIR:-}" ]] || DEST="$HOME/Applications"
    mkdir -p "$DEST"
    rm -rf "$DEST/Vpipe Manager.app"
    ditto "$TMP/mnt/Vpipe Manager.app" "$DEST/Vpipe Manager.app"
    hdiutil detach -quiet "$TMP/mnt"
    APP="$DEST/Vpipe Manager.app"
    ok "installed $APP"
  fi
  VPIPE="$APP/Contents/Helpers/vpipe"
  [[ -x "$VPIPE" ]] || die "The Vpipe Manager app has no command-line tool at $VPIPE."
fi

# --- 4. folders and settings ----------------------------------------------------
step "Creating folders"
note "Models:    $WORKSPACE"
note "Projects:  $PROJECTS"
mkdir -p "$WORKSPACE/setup" "$PROJECTS"
ok "$WORKSPACE"
ok "$PROJECTS"
# Remember where everything is, so ./start.sh needs no options. Values already
# saved (for example a projects folder chosen in Settings) are kept.
"$PY" - "$CONFIG" "$WORKSPACE" "$VPIPE" "$PROJECTS" <<'PY'
import json, sys
from pathlib import Path
path, workspace, vpipe, projects = sys.argv[1:]
p = Path(path)
doc = json.loads(p.read_text()) if p.exists() else {}
doc.setdefault("workspace", workspace)
doc["vpipe"] = doc.get("vpipe") or vpipe
doc.setdefault("dataDir", projects)
p.write_text(json.dumps(doc, indent=2) + "\n")
PY
ok "saved these locations in server-config.json"

for pipeline in "$H3_PIPELINE" prepare-krea-2.vpipeline; do
  [[ -s "$WORKSPACE/setup/$pipeline" ]] ||
    curl -fsSL "$PIPELINES_URL/$pipeline" -o "$WORKSPACE/setup/$pipeline" ||
    die "Could not download $pipeline from the vpipe release."
done
cp "$ROOT/setup/prepare-moss-tts.vpipeline" "$WORKSPACE/setup/" 2>/dev/null || true

# --- 5. MiniMax H3 ------------------------------------------------------------
step "MiniMax H3 (the video model)"
# Same test Storyboard uses: the folder exists and nothing in it is still a
# half-downloaded .part file.
if [[ -d "$WORKSPACE/models/$H3_MODEL" && -z "$(find "$WORKSPACE/models/$H3_MODEL" -name '*.part' -print -quit)" ]]; then
  ok "already prepared"
elif [[ "$NO_MODEL" == 1 ]]; then
  warn "skipped (--no-model). Run ./setup.sh again when you are ready to download it."
else
  FREE_GB=$(( $(df -k "$WORKSPACE" | awk 'NR==2 {print $4}') / 1048576 ))
  note "This downloads about 115 GB and converts it to an 8-bit model of about 65 GB."
  note "It needs ${NEEDED_GB} GB free while it works; this drive has ${FREE_GB} GB free."
  note "Expect several hours. Leave the Mac plugged in and awake."
  if [[ "$FREE_GB" -lt "$NEEDED_GB" ]]; then
    die "Not enough free space on this drive. Move the storyboard folder to a drive with ${NEEDED_GB} GB free (the models are stored next to it), or run ./setup.sh --no-model to set up everything else first."
  fi
  if ask "Download and prepare MiniMax H3 now?"; then
    LOG="$WORKSPACE/setup/prepare-minimax-h3.log"
    note "Progress is also written to $LOG"
    # caffeinate keeps the Mac awake for as long as the download runs.
    (cd "$WORKSPACE" && caffeinate -dims "$VPIPE" --launch "setup/$H3_PIPELINE" 2>&1 | tee "$LOG") ||
      die "Preparing MiniMax H3 failed — see $LOG. Running ./setup.sh again resumes the download."
    ok "MiniMax H3 is ready"
  else
    warn "skipped. Run ./setup.sh again when you are ready to download it."
  fi
fi

# --- done ---------------------------------------------------------------------
step "Done"
note "Start Storyboard:   ${B}./start.sh${N}"
note "Then open:          ${B}http://localhost:9877${N}"
note ""
note "Want voices, stills, music or prompt help? See the list of extras:"
note "                    ${B}setup/add.sh${N}"
