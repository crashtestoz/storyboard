/* ==========================================================================
   Storyboard → Video — front end.
   --------------------------------------------------------------------------
   Talks to the server in server/. Board edits are saved back with a short
   debounce; while a render is running the queue is polled once a second and
   the server's view of a shot wins over the local one, since it is the thing
   actually watching the process.

   Model choices, frame-count rules and which shots can take frame anchors all
   come from /api/info rather than being hardcoded — that is what lets a
   second backend (ComfyUI) drop in without touching this file.
   ========================================================================== */

"use strict";

const state = {
  slug: null,
  board: null,
  boards: [],
  info: null,
  models: [],
  selectedId: null,
  status: null,       // last /api/status payload
  poll: null,
  saveTimer: null,
  dirty: false,
  toast: null,
};

const STATUS_LABELS = {
  draft: "Draft",
  queued: "Queued",
  running: "Running",
  done: "Done",
  failed: "Failed",
  blocked: "Blocked",
  review: "Needs review",
  interrupted: "Interrupted",
};

/* --- helpers ------------------------------------------------------------- */

const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const shots = () => (state.board && state.board.shots) || [];
const shotById = (id) => shots().find((s) => s.id === id);
const shotIndex = (id) => shots().findIndex((s) => s.id === id);
const selectedShot = () => shotById(state.selectedId);
const modelCap = (id) => state.models.find((m) => m.id === id) || null;

function chip(status) {
  const c = el("span", "chip", STATUS_LABELS[status] || status);
  c.dataset.status = status;
  return c;
}

function dur(seconds) {
  if (seconds == null) return "—";
  seconds = Math.round(seconds);
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m < 60) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

function toast(msg, kind = "info") {
  state.toast = { msg, kind, at: Date.now() };
  const box = $("#toast");
  box.textContent = msg;
  box.dataset.kind = kind;
  box.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => {
    box.hidden = true;
  }, kind === "error" ? 8000 : 3500);
}

/* --- persistence --------------------------------------------------------- */

function markDirty() {
  state.dirty = true;
  $("#saveState").textContent = "unsaved";
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(saveNow, 700);
}

async function saveNow() {
  if (!state.slug || !state.board) return;
  clearTimeout(state.saveTimer);
  try {
    $("#saveState").textContent = "saving…";
    const { board } = await API.saveBoard(state.slug, state.board);
    state.board = board;
    state.dirty = false;
    $("#saveState").textContent = "saved";
  } catch (err) {
    $("#saveState").textContent = "save failed";
    toast(`Could not save: ${err.message}`, "error");
  }
}

/* --- run state merge ----------------------------------------------------- */

function runFor(shotId) {
  const runs = state.status && state.status.runs;
  return (runs && runs[shotId]) || null;
}

/** The live view of a shot: server run state wins while a batch is active. */
function view(shot) {
  const run = runFor(shot.id);
  if (!run) return shot;
  return {
    ...shot,
    status: run.status || shot.status,
    progress: run.progress != null ? run.progress : shot.progress,
    runtimeSeconds:
      run.runtimeSeconds != null ? run.runtimeSeconds : shot.runtimeSeconds,
    validation: run.validation || shot.validation,
    reason: run.reason || "",
    phase: run.phase || "",
    outputs: run.outputs && run.outputs.length ? run.outputs : shot.outputs,
    log: run.log || null,
    summary: run.summary || "",
  };
}

/* ==========================================================================
   Boot
   ========================================================================== */

async function boot() {
  wireChrome();

  try {
    state.info = await API.info();
    state.models = state.info.models || [];
  } catch (err) {
    toast(`Cannot reach the server: ${err.message}`, "error");
    return;
  }

  renderBackendBadge();

  try {
    const { boards } = await API.listBoards();
    state.boards = boards;
    if (boards.length) {
      await openBoard(boards[0].slug);
    } else {
      const { slug, board } = await API.createBoard("My first storyboard");
      state.boards = [{ slug, name: board.name, shots: 0 }];
      setBoard(slug, board);
    }
  } catch (err) {
    toast(`Could not load storyboards: ${err.message}`, "error");
  }
}

function renderBackendBadge() {
  const b = state.info.backend;
  const box = $("#backendBadge");
  box.textContent = `${b.label}${b.healthy ? "" : " — unavailable"}`;
  box.dataset.healthy = String(b.healthy);
  box.title = b.healthy ? state.info.workspace : b.message;
  if (!b.healthy) toast(b.message, "error");

  const unavailable = state.models.filter((m) => !m.available);
  if (unavailable.length) {
    toast(
      `${unavailable.length} model(s) not ready: ` +
        unavailable.map((m) => m.id).join(", "),
      "warn"
    );
  }
}

async function openBoard(slug) {
  const { board } = await API.getBoard(slug);
  setBoard(slug, board);
}

function setBoard(slug, board) {
  state.slug = slug;
  state.board = board;
  state.selectedId = board.shots.length ? board.shots[0].id : null;
  state.dirty = false;
  $("#saveState").textContent = "saved";
  render();
  refreshStatus();
}

/* ==========================================================================
   Chrome (header + rail controls)
   ========================================================================== */

function wireChrome() {
  $("#btnRender").addEventListener("click", async () => {
    await saveNow();
    try {
      state.status = await API.render(state.slug);
      startPolling();
      render();
    } catch (err) {
      toast(err.message, "error");
    }
  });

  $("#btnStop").addEventListener("click", async () => {
    try {
      state.status = await API.stop();
      render();
      toast("Stopping — waiting for the current shot to wind down.");
    } catch (err) {
      toast(err.message, "error");
    }
  });

  $("#btnNewBoard").addEventListener("click", async () => {
    const name = prompt("Name for the new storyboard:", "Untitled storyboard");
    if (!name) return;
    const { slug, board } = await API.createBoard(name);
    state.boards.unshift({ slug, name: board.name, shots: 0 });
    setBoard(slug, board);
    renderBoardPicker();
  });

  $("#btnExport").addEventListener("click", () => {
    if (state.slug) window.location.href = API.exportUrl(state.slug);
  });

  $("#btnImport").addEventListener("click", () => $("#importFile").click());
  $("#importFile").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    try {
      const board = JSON.parse(await file.text());
      const { slug, board: saved } = await API.importBoard(board, board.name);
      state.boards.unshift({ slug, name: saved.name, shots: saved.shots.length });
      setBoard(slug, saved);
      renderBoardPicker();
      toast(`Imported “${saved.name}” — run state was reset.`);
    } catch (err) {
      toast(`Import failed: ${err.message}`, "error");
    }
  });

  $("#boardPicker").addEventListener("change", async (e) => {
    await saveNow();
    await openBoard(e.target.value);
    renderBoardPicker();
  });

  $("#sceneDescription").addEventListener("input", (e) => {
    state.board.sceneDescription = e.target.value;
    markDirty();
    renderEditor();
  });

  $("#addStyleRef").addEventListener("click", () => $("#styleRefFile").click());
  $("#styleRefFile").addEventListener("change", async (e) => {
    const files = [...e.target.files];
    e.target.value = "";
    for (const f of files) {
      try {
        const ref = await API.uploadRef(state.slug, f);
        state.board.styleRefs.push(ref);
      } catch (err) {
        toast(`Upload failed: ${err.message}`, "error");
      }
    }
    markDirty();
    await saveNow();
    render();
  });

  window.addEventListener("beforeunload", (e) => {
    if (state.dirty) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
}

function renderBoardPicker() {
  const sel = $("#boardPicker");
  sel.innerHTML = "";
  state.boards.forEach((b) => {
    const o = el("option", null, `${b.name} (${b.shots})`);
    o.value = b.slug;
    if (b.slug === state.slug) o.selected = true;
    sel.appendChild(o);
  });
}

/* ==========================================================================
   Polling
   ========================================================================== */

function startPolling() {
  stopPolling();
  state.poll = setInterval(refreshStatus, 1000);
}

function stopPolling() {
  if (state.poll) clearInterval(state.poll);
  state.poll = null;
}

async function refreshStatus() {
  try {
    const s = await API.status();
    const wasBusy = state.status && state.status.busy;
    state.status = s;

    if (s.busy && !state.poll) startPolling();

    if (!s.busy && wasBusy) {
      stopPolling();
      // the server has been mutating the board as shots finish; re-read it
      const { board } = await API.getBoard(state.slug);
      state.board = board;
      toast("Render batch finished.");
    }
    if (s.error) toast(s.error, "error");
    render();
  } catch {
    /* transient — next tick will retry */
  }
}

/* ==========================================================================
   Render
   ========================================================================== */

function render() {
  if (!state.board) return;
  renderBoardPicker();
  renderRail();
  renderStrip();
  renderEditor();
  renderPreview();

  const busy = !!(state.status && state.status.busy);
  $("#btnRender").disabled = busy || !state.info.backend.healthy;
  $("#btnStop").disabled = !busy;

  const counts = shots().reduce((a, s) => {
    const st = view(s).status;
    a[st] = (a[st] || 0) + 1;
    return a;
  }, {});
  $("#projectTitle").textContent = state.board.name;
  $("#projectMeta").textContent =
    `${shots().length} shots` +
    (Object.keys(counts).length
      ? " · " +
        Object.entries(counts)
          .map(([k, v]) => `${v} ${(STATUS_LABELS[k] || k).toLowerCase()}`)
          .join(", ")
      : "");
}

/* --- rail ---------------------------------------------------------------- */

function renderRail() {
  const sd = $("#sceneDescription");
  if (document.activeElement !== sd) sd.value = state.board.sceneDescription || "";

  const wrap = $("#styleRefs");
  wrap.innerHTML = "";
  (state.board.styleRefs || []).forEach((r, i) => {
    const d = el("div", "style-ref");
    d.title = r.label || r.path;
    const img = el("img");
    img.src = r.url || r.src;
    img.alt = "";
    d.appendChild(img);
    const x = el("button", "clear-ref", "✕");
    x.addEventListener("click", () => {
      state.board.styleRefs.splice(i, 1);
      markDirty();
      render();
    });
    d.appendChild(x);
    wrap.appendChild(d);
  });
  wrap.appendChild($("#addStyleRef"));

  const q = $("#queueList");
  q.innerHTML = "";
  shots().forEach((raw, i) => {
    const shot = view(raw);
    const row = el("div", "queue-row");
    if (raw.id === state.selectedId) row.classList.add("selected");
    row.appendChild(el("span", "queue-num", String(i + 1)));
    const dot = el("span", "queue-dot");
    dot.dataset.status = shot.status;
    row.appendChild(dot);
    row.appendChild(el("span", "queue-name", raw.title));
    if (shot.status === "running") {
      row.appendChild(el("span", "queue-pct", `${Math.round(shot.progress)}%`));
    }
    row.addEventListener("click", () => {
      state.selectedId = raw.id;
      render();
    });
    q.appendChild(row);
  });
}

/* --- strip --------------------------------------------------------------- */

function renderStrip() {
  const strip = $("#strip");
  strip.innerHTML = "";

  shots().forEach((raw, i) => {
    const shot = view(raw);
    const card = el("div", "shot-card");
    card.dataset.id = raw.id;
    card.draggable = true;
    if (raw.id === state.selectedId) card.classList.add("selected");

    const thumb = el("div", "shot-thumb");
    if (raw.thumb) {
      const img = el("img");
      img.src = raw.thumb;
      img.alt = "";
      if (shot.status !== "done") {
        img.style.opacity = "0.3";
        img.style.filter = "grayscale(1)";
      }
      thumb.appendChild(img);
    } else {
      thumb.appendChild(el("div", "shot-thumb-empty", "not rendered"));
    }
    thumb.appendChild(el("span", "shot-index", String(i + 1).padStart(2, "0")));
    if (raw.startRef && raw.startRef.kind === "chain") {
      const b = el("span", "chain-badge");
      b.append(el("span", null, "⛓"), el("span", null, "chained"));
      thumb.appendChild(b);
    }
    card.appendChild(thumb);

    const body = el("div", "shot-body");
    body.appendChild(el("div", "shot-title", raw.title || "Untitled"));
    const cap = modelCap(raw.model);
    body.appendChild(
      el(
        "div",
        "shot-sub",
        `${raw.resolution}` +
          (cap && cap.kind === "image" ? " · still" : ` · ${raw.frames}f`) +
          ` · ${raw.steps} steps`
      )
    );

    const foot = el("div", "shot-foot");
    foot.appendChild(chip(shot.status));
    if (shot.status === "running") {
      foot.appendChild(el("span", "queue-pct", `${Math.round(shot.progress)}%`));
    } else if (shot.runtimeSeconds != null) {
      foot.appendChild(el("span", "shot-sub", dur(shot.runtimeSeconds)));
    }
    body.appendChild(foot);

    if (shot.status === "running") {
      const track = el("div", "progress-track");
      const fill = el("div", "progress-fill");
      fill.style.width = `${shot.progress}%`;
      track.appendChild(fill);
      body.appendChild(track);
      if (shot.phase) body.appendChild(el("div", "shot-sub", shot.phase));
    }
    card.appendChild(body);

    card.addEventListener("click", () => {
      state.selectedId = raw.id;
      render();
    });
    wireDrag(card, raw);
    strip.appendChild(card);
  });

  const add = el("button", "add-shot");
  add.append(el("span", "plus", "+"), el("span", null, "Add shot"));
  add.addEventListener("click", addShot);
  strip.appendChild(add);
}

function wireDrag(card, shot) {
  card.addEventListener("dragstart", (e) => {
    card.classList.add("dragging");
    e.dataTransfer.setData("text/plain", shot.id);
  });
  card.addEventListener("dragend", () => card.classList.remove("dragging"));
  card.addEventListener("dragover", (e) => {
    e.preventDefault();
    card.classList.add("drop-target");
  });
  card.addEventListener("dragleave", () => card.classList.remove("drop-target"));
  card.addEventListener("drop", (e) => {
    e.preventDefault();
    card.classList.remove("drop-target");
    const id = e.dataTransfer.getData("text/plain");
    if (!id || id === shot.id) return;
    const from = shotIndex(id);
    const to = shotIndex(shot.id);
    const [moved] = state.board.shots.splice(from, 1);
    state.board.shots.splice(to, 0, moved);
    markDirty();
    render();
  });
}

async function addShot() {
  try {
    const { board, shot } = await API.addShot(state.slug, {});
    state.board = board;
    state.selectedId = shot.id;
    render();
  } catch (err) {
    toast(err.message, "error");
  }
}

/* --- editor -------------------------------------------------------------- */

function renderEditor() {
  const host = $("#editor");
  host.innerHTML = "";
  const raw = selectedShot();
  if (!raw) {
    host.appendChild(el("div", "empty-state", "No shots yet — add one above."));
    return;
  }
  const shot = view(raw);
  const cap = modelCap(raw.model);
  const idx = shotIndex(raw.id);

  const head = el("div", "editor-head");
  const h = el("div", "section-label", `Shot ${idx + 1}`);
  h.style.margin = "0";
  head.append(h, chip(shot.status));
  const sp = el("div");
  sp.style.flex = "1";
  head.appendChild(sp);

  const one = el("button", "btn btn-sm", "Render this shot");
  one.disabled = !!(state.status && state.status.busy);
  one.addEventListener("click", async () => {
    await saveNow();
    try {
      state.status = await API.render(state.slug, [raw.id]);
      startPolling();
      render();
    } catch (err) {
      toast(err.message, "error");
    }
  });
  head.appendChild(one);

  const del = el("button", "btn btn-ghost btn-sm", "Delete");
  del.addEventListener("click", () => {
    state.board.shots = state.board.shots.filter((s) => s.id !== raw.id);
    state.selectedId = shots().length ? shots()[0].id : null;
    markDirty();
    render();
  });
  head.appendChild(del);
  host.appendChild(head);

  // prompt panel
  const panel = el("div", "panel");
  panel.appendChild(
    field("Shot name", input("text", raw.title, (v) => {
      raw.title = v;
      markDirty();
      renderStrip();
      renderRail();
    }))
  );

  const ta = el("textarea");
  ta.rows = 5;
  ta.value = raw.prompt || "";
  ta.placeholder =
    "What happens in THIS shot — action, camera move, mood.\n" +
    "The scene description is prepended automatically; don't restate the subject.";
  ta.addEventListener("input", () => {
    raw.prompt = ta.value;
    markDirty();
    updateResolved();
  });
  panel.appendChild(
    field("Shot prompt", ta, "action / camera / mood only")
  );

  const resolved = el("div", "resolved mono");
  panel.appendChild(field("Resolved prompt sent to the backend", resolved));
  function updateResolved() {
    const scene = (state.board.sceneDescription || "").trim();
    const own = (raw.prompt || "").trim();
    resolved.textContent = scene ? `${scene} ${own}`.trim() : own || "(empty)";
  }
  updateResolved();
  host.appendChild(panel);

  // anchors — only if the model supports them
  if (cap && (cap.supportsStartAnchor || cap.supportsEndAnchor)) {
    const refPanel = el("div", "panel");
    refPanel.style.marginTop = "var(--sp-3)";
    const lbl = el("div", "section-label", "Frame anchors");
    lbl.appendChild(el("span", "hint", "— opens/closes on this exact frame"));
    refPanel.appendChild(lbl);

    const slots = el("div", "ref-slots");
    if (cap.supportsStartAnchor)
      slots.appendChild(refSlot("Start frame", raw, "startRef"));
    if (cap.supportsEndAnchor)
      slots.appendChild(refSlot("End frame", raw, "endRef"));
    refPanel.appendChild(slots);

    if (idx > 0) {
      const prev = shots()[idx - 1];
      const wrap = el("div");
      wrap.style.marginTop = "var(--sp-3)";
      const t = el("label", "toggle");
      const cb = el("input");
      cb.type = "checkbox";
      cb.checked = !!(raw.startRef && raw.startRef.kind === "chain");
      cb.addEventListener("change", () => {
        raw.startRef = cb.checked
          ? { kind: "chain", from: prev.id, label: `last frame of shot ${idx}` }
          : null;
        markDirty();
        render();
      });
      t.append(cb, el("span", "toggle-track"),
               el("span", null, `Chain start frame from shot ${idx}`));
      wrap.appendChild(t);
      refPanel.appendChild(wrap);
    }
    host.appendChild(refPanel);
  } else if (cap && cap.supportsStyleRefs) {
    const note = el("div", "panel");
    note.style.marginTop = "var(--sp-3)";
    note.appendChild(el("div", "section-label", "References"));
    note.appendChild(
      el(
        "div",
        "hint-body",
        `${cap.label.split("—")[0].trim()} conditions on the project's style ` +
          `references (up to ${cap.maxStyleRefs}) rather than frame anchors — ` +
          `they carry subject and style across the whole clip, so this model ` +
          `offers no start/end frame.`
      )
    );
    host.appendChild(note);
  }

  // params
  const params = el("div", "panel");
  params.style.marginTop = "var(--sp-3)";
  params.appendChild(el("div", "section-label", "Parameters"));

  params.appendChild(
    field(
      "Model",
      select(
        state.models.map((m) => [
          m.id,
          m.available ? m.label : `${m.label}  — unavailable`,
        ]),
        raw.model,
        (v) => {
          raw.model = v;
          const c = modelCap(v);
          if (c) {
            if (!c.resolutions.includes(raw.resolution))
              raw.resolution = c.resolutions[0];
            raw.frames = c.frameRule.minimum > raw.frames
              ? c.frameRule.minimum
              : raw.frames;
          }
          markDirty();
          render();
        }
      )
    )
  );

  const isImage = cap && cap.kind === "image";
  const row = el("div", isImage ? "field-row" : "field-row-3");
  row.appendChild(
    field(
      "Resolution",
      select(
        (cap ? cap.resolutions : [raw.resolution]).map((r) => [r, r.replace("x", " × ")]),
        raw.resolution,
        (v) => {
          raw.resolution = v;
          markDirty();
          renderStrip();
        }
      )
    )
  );
  if (!isImage) {
    row.appendChild(
      field(
        "Frames",
        input("number", raw.frames, (v) => {
          raw.frames = Number(v);
          markDirty();
          renderEditor();
          renderStrip();
        }),
        null,
        frameHint(raw.frames, cap)
      )
    );
  }
  row.appendChild(
    field(
      "Steps",
      input("number", raw.steps, (v) => {
        raw.steps = Number(v);
        markDirty();
        renderStrip();
      })
    )
  );
  params.appendChild(row);

  const row2 = el("div", "field-row");
  row2.appendChild(
    field("Seed", input("number", raw.seed, (v) => {
      raw.seed = Number(v);
      markDirty();
    }))
  );
  row2.appendChild(
    field(
      "Duration",
      readOnly(isImage ? "still image" : `${(raw.frames / 24).toFixed(2)}s @ 24fps`)
    )
  );
  params.appendChild(row2);

  if (cap && !cap.available) {
    const warn = el("div", "inline-warn", `⚠ ${cap.unavailableReason}`);
    params.appendChild(warn);
  }
  host.appendChild(params);
}

function frameHint(frames, cap) {
  if (!cap) return null;
  const r = cap.frameRule;
  if (r.kind === "any") return null;
  if (frames < r.minimum) return `⚠ needs ≥ ${r.minimum} — shorter will not decode`;
  if ((frames - r.offset) % r.step !== 0) {
    let snapped = frames;
    while ((snapped - r.offset) % r.step !== 0) snapped += 1;
    return `⚠ will be snapped up to ${snapped}`;
  }
  return `${r.step}n + ${r.offset} — ok`;
}

/* --- preview ------------------------------------------------------------- */

function renderPreview() {
  const host = $("#preview");
  host.innerHTML = "";
  const raw = selectedShot();
  if (!raw) return;
  const shot = view(raw);

  host.appendChild(el("div", "section-label", "Output"));

  const stage = el("div", "preview-stage");
  const video = (shot.outputs || []).find((u) => u.endsWith(".mp4"));
  const image = (shot.outputs || []).find((u) => /\.(jpe?g|png|webp)$/i.test(u));
  if (video) {
    const v = el("video");
    v.src = video;
    v.controls = true;
    v.loop = true;
    v.muted = false;
    stage.appendChild(v);
  } else if (image) {
    const img = el("img");
    img.src = image;
    stage.appendChild(img);
  } else if (raw.thumb) {
    const img = el("img");
    img.src = raw.thumb;
    stage.appendChild(img);
  } else {
    const e = el("div", "preview-empty");
    e.appendChild(el("span", "big", shot.status === "running" ? "◐" : "▦"));
    e.appendChild(
      el(
        "span",
        null,
        shot.status === "running"
          ? `Rendering — ${Math.round(shot.progress)}%${shot.phase ? ` (${shot.phase})` : ""}`
          : "Not rendered yet"
      )
    );
    stage.appendChild(e);
  }
  host.appendChild(stage);

  const diag = diagnostic(raw, shot);
  if (diag) host.appendChild(diag);

  const stats = el("dl", "stat-grid");
  [
    ["Status", STATUS_LABELS[shot.status] || shot.status],
    ["Model", (modelCap(raw.model) || {}).label || raw.model],
    ["Runtime", dur(shot.runtimeSeconds)],
    ["Outputs", (shot.outputs || []).length
      ? (shot.outputs || []).map((u) => u.split("/").pop()).join(", ")
      : "—"],
  ].forEach(([k, v]) => {
    stats.appendChild(el("dt", null, k));
    stats.appendChild(el("dd", null, v));
  });
  const sp = el("div", "panel");
  sp.style.marginBottom = "var(--sp-3)";
  sp.appendChild(stats);
  host.appendChild(sp);

  const lbl = el("div", "section-label", "Backend output");
  lbl.appendChild(el("span", "hint", "— parsed by the orchestrator"));
  host.appendChild(lbl);

  const box = el("div", "log");
  const lines = shot.log;
  if (!lines || !lines.length) {
    box.appendChild(el("span", "log-empty", "No run yet."));
  } else {
    lines.slice(-200).forEach(({ level, text }) => {
      const line = el("div", "log-line");
      line.dataset.lvl = level;
      line.appendChild(el("span", "lvl", `[${level}] `));
      line.appendChild(el("span", null, text));
      box.appendChild(line);
    });
  }
  host.appendChild(box);
  box.scrollTop = box.scrollHeight;
}

function diagnostic(raw, shot) {
  if (!["failed", "blocked", "review", "interrupted"].includes(shot.status)) {
    return null;
  }
  const d = el("div", "diag");
  d.dataset.kind = shot.status;
  const titles = {
    failed: "Run failed",
    blocked: "Blocked by dependency",
    review: "Flagged for review",
    interrupted: "Interrupted",
  };
  const head = el("div", "diag-head");
  head.append(chip(shot.status), el("span", null, titles[shot.status]));
  d.appendChild(head);

  const reason =
    shot.reason || (shot.validation && shot.validation.reason) || "";
  if (reason) d.appendChild(el("div", "diag-body", reason));

  const checks = shot.validation && shot.validation.checks;
  if (checks && checks.length) {
    const ul = el("ul", "diag-checks");
    checks.forEach((c) => {
      const li = el("li");
      const m = el("span", "mark", c.passed === true ? "✓" : c.passed === false ? "✕" : "–");
      m.dataset.pass = String(c.passed);
      li.append(m, el("span", null, `${c.name}${c.detail ? " — " + c.detail : ""}`));
      ul.appendChild(li);
    });
    d.appendChild(ul);
  }

  const actions = el("div", "diag-actions");
  const busy = !!(state.status && state.status.busy);
  const again = el("button", "btn btn-sm", "Re-run this shot");
  again.disabled = busy;
  again.addEventListener("click", async () => {
    await saveNow();
    try {
      state.status = await API.render(state.slug, [raw.id]);
      startPolling();
      render();
    } catch (err) {
      toast(err.message, "error");
    }
  });
  actions.appendChild(again);

  if (shot.status === "review") {
    const accept = el("button", "btn btn-sm btn-ghost", "Accept anyway");
    accept.addEventListener("click", () => {
      raw.status = "done";
      markDirty();
      render();
    });
    actions.appendChild(accept);
  }
  d.appendChild(actions);
  return d;
}

/* --- small controls ------------------------------------------------------ */

function field(label, control, hint, warn) {
  const f = el("div", "field");
  const l = el("label", null, label);
  if (hint) {
    const s = el("span", "hint-inline", "  " + hint);
    l.appendChild(s);
  }
  f.append(l, control);
  if (warn) {
    const w = el("div", warn.startsWith("⚠") ? "field-warn" : "field-note", warn);
    f.appendChild(w);
  }
  return f;
}

function input(type, value, onChange) {
  const i = el("input");
  i.type = type;
  i.value = value;
  i.addEventListener("input", () => onChange(i.value));
  return i;
}

function readOnly(text) {
  return el("div", "readonly", text);
}

function select(options, value, onChange) {
  const s = el("select");
  options.forEach(([v, label]) => {
    const o = el("option", null, label);
    o.value = v;
    if (v === value) o.selected = true;
    s.appendChild(o);
  });
  s.addEventListener("change", () => onChange(s.value));
  return s;
}

function refSlot(label, shot, key) {
  const ref = shot[key];
  const slot = el("div", "ref-slot");
  if (ref) {
    slot.classList.add("filled");
    if (ref.kind === "chain") {
      const l = el("div", "ref-slot-label");
      l.append(el("strong", null, "⛓ chained"), el("span", null, ref.label || ""));
      slot.appendChild(l);
    } else {
      const img = el("img");
      img.src = ref.url || ref.path;
      slot.appendChild(img);
    }
    const x = el("button", "clear-ref", "✕");
    x.addEventListener("click", (e) => {
      e.stopPropagation();
      shot[key] = null;
      markDirty();
      render();
    });
    slot.appendChild(x);
  } else {
    const l = el("div", "ref-slot-label");
    l.append(el("strong", null, label), el("span", null, "click to upload"));
    slot.appendChild(l);
    slot.addEventListener("click", () => pickRef(shot, key));
  }
  return slot;
}

function pickRef(shot, key) {
  const inp = el("input");
  inp.type = "file";
  inp.accept = "image/png,image/jpeg,image/webp";
  inp.addEventListener("change", async () => {
    const f = inp.files[0];
    if (!f) return;
    try {
      shot[key] = await API.uploadRef(state.slug, f);
      markDirty();
      await saveNow();
      render();
    } catch (err) {
      toast(`Upload failed: ${err.message}`, "error");
    }
  });
  inp.click();
}

document.addEventListener("DOMContentLoaded", boot);
