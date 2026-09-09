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
from .llm import CONFIG_NAME as LLM_CONFIG_NAME
from .llm import load_services as load_llm_services
from .tts import CONFIG_NAME, load_engines

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
        default=os.environ.get("SBV_TTS", "none"),
        help=f"default speech engine id for new boards (see {CONFIG_NAME})",
    )
    p.add_argument(
        "--llm",
        default=os.environ.get("SBV_LLM", "ollama-local"),
        help=f"default prompt-rewriting model id (see {LLM_CONFIG_NAME})",
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
    # Engines come from tts-services.json: a service is a name and a URL, so
    # adding another instance is an edit rather than a code change. None loads
    # anything until asked, so a board can switch between them at runtime.
    tts_engines = load_engines(
        UI_ROOT, vpipe_binary=args.vpipe.expanduser(), workspace=workspace
    )

    # Prompt rewriting, from llm-services.json, on the same linked-not-installed
    # footing as the speech engines.
    llm_services = load_llm_services(UI_ROOT)

    store = Store(workspace=workspace)
    # Anything left mid-run by a previous process is not running now.
    stranded = store.reconcile_startup()
    orch = Orchestrator(backend=backend, store=store, workspace=workspace)
    ctx = Context(UI_ROOT, workspace, store, backend, orch,
                  tts_engines=tts_engines, default_tts=args.tts,
                  llm_services=llm_services, default_llm=args.llm)

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
    print(f"  speech     from {CONFIG_NAME}, default '{args.tts}'")
    for kind, eng in tts_engines.items():
        if kind == "none":
            continue
        tok, tmsg = eng.health()
        print(f"             [{'ok  ' if tok else '--  '}] {kind}"
              + ("" if tok else f"  ({tmsg.splitlines()[0][:86]})"))
    print("")
    print(f"  rewriting  from {LLM_CONFIG_NAME}, default '{args.llm}'")
    for sid, svc in llm_services.items():
        if sid == "none":
            continue
        lok, lmsg = svc.health()
        print(f"             [{'ok  ' if lok else '--  '}] {sid}"
              + (f"  ({svc.model})" if lok else f"  ({lmsg.splitlines()[0][:86]})"))
    print("")
    if stranded:
        print(f"  recovered   {len(stranded)} shot(s) left mid-run by a previous "
              "process, now marked interrupted:")
        for name in stranded[:5]:
            print(f"              {name}")
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
