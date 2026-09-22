"""Entry point: ``python3 -m server`` (see serve.sh)."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path

from .app import SERVER_CONFIG_NAME, Context, build_server
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
# Storyboards and uploads are the user's documents, not vpipe's runtime state,
# so they live in one selected folder outside whichever workspace/sandbox
# vpipe happens to use. Project folders live directly beneath this directory.
DEFAULT_DATA_DIR = Path("/Volumes/KINGSTON/ai-diffusers/storyboard-projects")


def _configured_data_dir(ui_root: Path) -> Path | None:
    """What the Settings dialog last saved, if anything.

    Lowest priority above the hardcoded default: an explicit --data-dir or
    SBV_DATA_DIR always wins, so a launch script that already pins one keeps
    working exactly as before no matter what was saved here.
    """
    path = ui_root / SERVER_CONFIG_NAME
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    raw = doc.get("dataDir")
    return Path(raw).expanduser() if raw else None


BANNER = r"""
  ______ _____  ____   ______  __ ______  ____  ___    ____  ____
 / __/ //_  __// __ \ / __ \ \/ // __ / / __ \/ _ |  / __ \/ __ \
_\ \ / /  / /  / /_/ // /_/ /\  // /_/ / / /_/ / __ | / /_/ / /_/ /
/___//_/  /_/   \____/ \____/ /_/ \____/  \____/_/ |_|/_____/_____/
                                              storyboard -> video
"""


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="storyboard")
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
        "--data-dir",
        type=Path,
        default=None,
        help="where storyboards and uploads live — highest priority; falls "
             "back to SBV_DATA_DIR, then whatever Settings last saved, then "
             f"{DEFAULT_DATA_DIR}",
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
    # Two roots on purpose. The workspace is vpipe's — it resolves models/ and
    # its model registry relative to it, so it is not ours to choose. The data
    # directory is where the user's storyboards and uploads live. Priority,
    # highest first: an explicit --data-dir, SBV_DATA_DIR, whatever the
    # Settings dialog last saved to server-config.json, then DEFAULT_DATA_DIR.
    if args.data_dir:
        data_dir = args.data_dir.expanduser()
        data_dir_source = "cli"
    elif os.environ.get("SBV_DATA_DIR"):
        data_dir = Path(os.environ["SBV_DATA_DIR"]).expanduser()
        data_dir_source = "env"
    else:
        configured = _configured_data_dir(UI_ROOT)
        data_dir = configured or DEFAULT_DATA_DIR
        data_dir_source = "config" if configured else "default"
    data_dir.mkdir(parents=True, exist_ok=True)
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

    store = Store(workspace=workspace, data_dir=data_dir)
    # Anything left mid-run by a previous process is not running now.
    stranded = store.reconcile_startup()
    orch = Orchestrator(backend=backend, store=store, workspace=workspace,
                        data_dir=data_dir)
    ctx = Context(UI_ROOT, workspace, store, backend, orch,
                  data_dir=data_dir, data_dir_source=data_dir_source,
                  vpipe_binary=args.vpipe.expanduser(), default_tts=args.tts,
                  default_llm=args.llm)

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
    print(f"  workspace  {workspace}   (vpipe's — models and registry)")
    print(f"  projects   {data_dir}"
          + ("   (inside the workspace)" if data_dir == workspace else ""))
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
    if data_dir != workspace:
        old = workspace / "projects"
        if old.is_dir():
            left = [d.name for d in sorted(old.iterdir())
                    if (d / "storyboard.json").is_file()]
            if left:
                print(f"  NOTE       {len(left)} storyboard(s) are still in the old "
                      "location and will not be listed:")
                for name in left[:8]:
                    print(f"             {old / name}")
                if len(left) > 8:
                    print(f"             ... and {len(left) - 8} more")
                print(f"             Move them when ready:  mv {old}/* "
                      f"{data_dir}/")
                print("             (nothing is moved automatically — they are "
                      "your files)")
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

    # /api/server-settings/restart stops serve_forever() the same way Ctrl-C
    # does (from another thread, since shutdown() deadlocks called from the
    # loop's own thread) and sets this first, so it is the one thing that
    # tells the two apart once the loop has already exited either way.
    if ctx.restart_requested:
        print("\n  restarting onto the updated projects folder...")
        httpd.server_close()
        os.execv(sys.executable, [sys.executable, "-u", "-m", "server"] + sys.argv[1:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
