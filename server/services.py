"""Start the local servers some engines talk to, from Settings.

Storyboard runs h3, mflux and Stable Audio itself, once per render, but a few
engines are servers of their own: the Qwen3-TTS voice clone and Ollama. After
a reboot they are simply not running, so Settings offers a Start button for
the ones this module knows how to launch.

Only the commands defined here ever run. A request names a configured
service; its command is looked up from that service's kind and URL, and only
for a server on this Mac (loopback, or this Mac's own .local name).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import urllib.parse
from pathlib import Path
from typing import Any

from .llm import load_config as load_llm_config
from .tts import load_config as load_tts_config

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
LOG_DIR = Path.home() / "Library" / "Logs" / "storyboard"
OLLAMA_APP = Path("/Applications/Ollama.app")

_lock = threading.Lock()
_started: dict[str, subprocess.Popen] = {}


def _this_mac() -> set[str]:
    """Names that reach this Mac: loopback plus its own Bonjour name, which
    is what a URL like http://mac-studio.local:11434 uses."""
    names = set(LOCAL_HOSTS)
    host = socket.gethostname().lower()
    names.add(host)
    names.add(host.removesuffix(".local") + ".local")
    return names


def _local_port(url: str) -> int | None:
    """The port of a URL on this Mac, else None (remote, or unparseable)."""
    try:
        parts = urllib.parse.urlparse(url or "")
    except ValueError:
        return None
    if (parts.hostname or "").lower() not in _this_mac():
        return None
    return parts.port or (443 if parts.scheme == "https" else 80)


def _command(ui_root: Path, group: str, entry: dict[str, Any]) -> list[str] | None:
    port = _local_port(str(entry.get("url") or ""))
    if port is None:
        return None
    kind = entry.get("kind")
    if group == "tts" and kind == "qwen3-clone":
        script = Path(ui_root) / "vendor" / "start-qwen3-tts.sh"
        return [str(script), "--port", str(port)] if script.is_file() else None
    if group == "llm" and kind == "ollama" and port == 11434:
        # The app when it is installed: it also offers its own menu-bar
        # controls and updates. Otherwise the bare CLI on its default port.
        if OLLAMA_APP.is_dir():
            return ["open", "-a", str(OLLAMA_APP)]
        ollama = shutil.which("ollama")
        return [ollama, "serve"] if ollama else None
    return None


def _entries(ui_root: Path, group: str) -> list[dict[str, Any]]:
    load = load_tts_config if group == "tts" else load_llm_config
    return [e for e in load(ui_root) if isinstance(e, dict) and e.get("id")]


def startable(ui_root: Path) -> dict[str, list[str]]:
    """Ids of the configured services Settings can offer to start."""
    return {group: [str(e["id"]) for e in _entries(ui_root, group)
                    if _command(ui_root, group, e)]
            for group in ("tts", "llm")}


def start(ui_root: Path, group: str, service_id: str) -> dict[str, Any]:
    """Launch one service, detached from this server so it outlives a
    Storyboard restart. Returns at once; the page polls health for "ready"."""
    if group not in ("tts", "llm"):
        raise ValueError("group must be 'tts' or 'llm'")
    entry = next((e for e in _entries(ui_root, group)
                  if str(e.get("id")) == service_id), None)
    if entry is None:
        raise FileNotFoundError(f"no {group} service {service_id!r}")
    argv = _command(ui_root, group, entry)
    if argv is None:
        raise ValueError(f"{entry.get('label') or service_id} cannot be started "
                         "from Storyboard — start it yourself")
    key = f"{group}:{service_id}"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{service_id}.log"
    with _lock:
        running = _started.get(key)
        if running is not None and running.poll() is None:
            return {"ok": True, "alreadyStarting": True, "log": str(log_path)}
        with open(log_path, "ab") as log:
            _started[key] = subprocess.Popen(
                argv, cwd=str(ui_root), stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, env=os.environ.copy(),
            )
    return {"ok": True, "alreadyStarting": False, "log": str(log_path)}
