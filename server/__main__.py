"""Entry point: ``python3 -m server`` (see serve.sh)."""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

from .app import Context, build_server
from .backends import BACKEND_IDS, build_backend
from .orchestrator import Orchestrator
from .store import Store
from .tts import TTS_IDS, build_tts

UI_ROOT = Path(__file__).resolve().parent.parent

# Defaults for this machine. vpipe resolves models/ and its LMDB registry
# relative to the directory it is launched from, so the workspace must be the
# directory the models were prepared in — not merely the vpipe checkout.
DEFAULT_WORKSPACE = Path("/Volumes/KINGSTON/ai-diffusers/vpipe-work/sandbox")
DEFAULT_VPIPE = Path("/Volumes/KINGSTON/ai-diffusers/vpipe/build/apps/vpipe/vpipe")

BANNER = r"""
  ______ _____  ____   ______  __ ______  ____  ___    ____  ____
 / __/ //_  __// __ \ / __ \ \/ // __ / / __ \/ _ |  / __ \/ __ \
_\ \ / /  / /  / /_/ // /_/ /\  // /_/ / / /_/ / __ | / /_/ / /_/ /
/___//_/  /_/   \____/ \____/ /_/ \____/  \____/_/ |_|/_____/_____/
                                              storyboard -> video
"""


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="storyboard-to-video")
    p.add_argument("--port", type=int, default=int(os.environ.get("SBV_PORT", 9877)))
    p.add_argument("--bind", default=os.environ.get("SBV_BIND", "127.0.0.1"))
    p.add_argument("--lan", action="store_true", help="bind all interfaces")
    p.add_argument(
        "--backend",
        choices=BACKEND_IDS,
        default=os.environ.get("SBV_BACKEND", "vpipe"),
    )
    p.add_argument(
        "--workspace",
        type=Path,
        default=Path(os.environ.get("SBV_WORKSPACE", DEFAULT_WORKSPACE)),
        help="directory vpipe is launched from (must contain models/)",
    )
    p.add_argument(
        "--vpipe",
        type=Path,
        default=Path(os.environ.get("SBV_VPIPE", DEFAULT_VPIPE)),
        help="path to the vpipe CLI binary",
    )
    p.add_argument(
        "--comfyui-url", default=os.environ.get("SBV_COMFYUI", "http://127.0.0.1:8188")
    )
    p.add_argument(
        "--tts",
        choices=TTS_IDS,
        default=os.environ.get("SBV_TTS", "vpipe-moss"),
        help="default speech engine for new boards (each board may override it)",
    )
    p.add_argument(
        "--tts-url",
        default=os.environ.get("SBV_TTS_URL", ""),
        help="MCC base URL for the mcc-qwen3 engine, e.g. http://mcc-host:8000",
    )
    return p.parse_args(argv)


def lan_ip() -> str:
    """Best-effort local address for the printed URL."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    bind = "0.0.0.0" if args.lan else args.bind

    workspace = args.workspace.expanduser()
    backend = build_backend(
        args.backend,
        vpipe_binary=args.vpipe.expanduser(),
        workspace=workspace,
        comfyui_url=args.comfyui_url,
    )
    # Every engine is constructed, none loads anything until asked, so a board
    # can switch between them at runtime instead of needing a restart.
    tts_engines = {
        kind: build_tts(kind, vpipe_binary=args.vpipe.expanduser(),
                        workspace=workspace, mcc_url=args.tts_url)
        for kind in TTS_IDS
    }

    store = Store(workspace=workspace)
    orch = Orchestrator(backend=backend, store=store, workspace=workspace)
    ctx = Context(UI_ROOT, workspace, store, backend, orch,
                  tts_engines=tts_engines, default_tts=args.tts)

    try:
        httpd = build_server(bind, args.port, ctx)
    except OSError as exc:
        print(f"error: cannot bind {bind}:{args.port} — {exc}", file=sys.stderr)
        print(f"       try: --port {args.port + 1}", file=sys.stderr)
        return 1

    shown = (
        f"http://{lan_ip()}:{args.port}/"
        if bind == "0.0.0.0"
        else f"http://localhost:{args.port}/"
    )
    healthy, message = backend.health()
    available = [c for c in backend.capabilities() if c.available]

    print(BANNER)
    print(f"  backend    {backend.id} — {backend.label}")
    print(f"  workspace  {workspace}")
    print(f"  serving    {shown}")
    if bind == "0.0.0.0":
        print("             (all interfaces — reachable on your LAN)")
    else:
        print("             (this machine only — use --lan to expose it)")
    print("")
    if healthy:
        print(f"  models     {len(available)} available:")
        for c in backend.capabilities():
            mark = "ok  " if c.available else "--  "
            note = "" if c.available else f"  ({c.unavailable_reason})"
            print(f"             [{mark}] {c.id}{note}")
    else:
        print(f"  WARNING    backend not usable: {message}")
    print("")
    print(f"  speech     default {args.tts}")
    for kind, eng in tts_engines.items():
        if kind == "none":
            continue
        tok, tmsg = eng.health()
        print(f"             [{'ok  ' if tok else '--  '}] {kind}"
              + ("" if tok else f"  ({tmsg.splitlines()[0][:88]})"))
    print("")
    print("  Ctrl-C to stop.")
    print("")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopping...")
        if orch.busy:
            print("  a render is in flight — asking it to stop")
            orch.stop()
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
