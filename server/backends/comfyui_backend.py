"""ComfyUI backend — scaffold, not yet implemented.

This file exists to keep the :class:`~server.backends.base.Backend` interface
honest. It is easy to write an "abstraction" that secretly assumes one
implementation; writing the second one's signature down is how you find out.
Two things were changed in the base interface because of this file:

*   ``run()`` owns execution and merely emits events, instead of the
    orchestrator managing a subprocess. ComfyUI is a *server* — you POST a
    prompt and then watch it over a websocket — so there is no child process
    to wait on.
*   ``capabilities()`` returns data rather than the UI hardcoding model names.
    A ComfyUI install's models are whatever the user has in
    ``models/checkpoints``, discovered at runtime via ``/object_info``.

To finish it, in rough order:

1.  **Discovery.** ``GET {base}/object_info`` lists every node type and, in
    ``CheckpointLoaderSimple.input.required.ckpt_name``, the checkpoints
    actually installed. Map those to :class:`ModelCapability` entries. Frame
    rules come from the workflow family (WAN wants ``4k+1``, LTX has its own
    rule), not from ComfyUI itself.
2.  **Workflow templates.** ComfyUI's API format is a flat dict of
    ``{node_id: {"class_type": ..., "inputs": {...}}}``. Unlike vpipe's ~8
    stage pipelines these graphs are large, so keep one template JSON per
    shot type in ``templates/comfyui/*.json`` exported from the ComfyUI UI
    ("Save (API format)"), and have ``prepare()`` only patch the few input
    values that vary: prompt text, dimensions, frame count, seed, and the
    filenames of any uploaded reference images.
3.  **Reference images.** ``POST {base}/upload/image`` (multipart) puts a file
    where a ``LoadImage`` node can see it and returns the name to patch in.
4.  **Submission.** ``POST {base}/prompt`` with
    ``{"prompt": graph, "client_id": ...}`` returns a ``prompt_id``.
5.  **Progress.** Connect ``ws://{base}/ws?clientId=...`` before submitting.
    ``executing`` messages carry the current node, ``progress`` carries
    ``value``/``max`` for the sampler — translate those into
    :class:`ProgressEvent` the same way the vpipe backend translates its
    ``[PROGRESS]`` stdout lines. Polling ``GET /history/{prompt_id}`` is the
    fallback if the socket drops.
6.  **Outputs.** ComfyUI writes into its own ``output/`` directory and reports
    filenames in the history entry. Either point ``SaveImage``'s
    ``filename_prefix`` at the shot folder, or copy the reported files across
    afterwards — the validation in the base class checks
    ``spec.expected_outputs``, so those paths have to end up real either way.
7.  **Cancellation.** ``POST {base}/interrupt`` cancels the running prompt;
    ``POST {base}/queue`` with ``{"delete": [prompt_id]}`` drops a queued one.
    Note the asymmetry with vpipe: there is no signal escalation to do, but
    also no guarantee the interrupt is honoured promptly, so the same
    "did it actually stop" polling is needed.

Nothing here needs the orchestrator, the HTTP API or the front end to change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .base import (
    Backend,
    JobSpec,
    ModelCapability,
    ProgressEvent,
    RunResult,
    ShotPaths,
)


class ComfyUIBackend(Backend):
    id = "comfyui"
    label = "ComfyUI (HTTP API)"

    def __init__(self, base_url: str = "http://127.0.0.1:8188", output_dir: Path | None = None):
        self.base_url = base_url.rstrip("/")
        self.output_dir = output_dir

    def health(self) -> tuple[bool, str]:
        return False, (
            "The ComfyUI backend is a scaffold — see "
            "server/backends/comfyui_backend.py for the integration plan. "
            "Select the vpipe backend."
        )

    def capabilities(self) -> list[ModelCapability]:
        # Real implementation: GET /object_info and read the checkpoint list
        # out of CheckpointLoaderSimple's enum.
        return []

    def prepare(self, shot: dict, project: dict, paths: ShotPaths) -> JobSpec:
        raise NotImplementedError(
            "ComfyUI backend not implemented — load a workflow template, patch "
            "prompt/size/frames/seed into it, and upload any reference images."
        )

    def run(
        self,
        spec: JobSpec,
        on_event: Callable[[ProgressEvent], None],
        should_cancel: Callable[[], bool],
    ) -> RunResult:
        raise NotImplementedError(
            "ComfyUI backend not implemented — POST /prompt, then follow "
            "progress over /ws and fall back to polling /history."
        )

    def cancel(self) -> None:
        # POST /interrupt
        raise NotImplementedError
