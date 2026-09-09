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
  tts: [],
  selectedId: null,
  status: null,       // last /api/status payload
  poll: null,
  tab: "prompt",          // which editor tab is open; survives a re-render
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

/* Rebuilding a subtree blows away the caret of anything focused inside it.
   The poll used to do exactly that once a second, so typing in one shot's
   fields while another rendered lost a keystroke per tick. Full rebuilds are
   now rare (see refreshStatus), but they still happen mid-typing — a shot
   finishing while you write — so they carry the caret across. */
const LOG_WINDOW = 200;

function focusSnapshot() {
  const a = document.activeElement;
  if (!a || a === document.body) return null;
  const sel =
    a.id ? "#" + a.id : a.dataset.fkey ? `[data-fkey="${a.dataset.fkey}"]` : null;
  if (!sel) return null;
  const snap = { sel };
  try {
    snap.start = a.selectionStart;
    snap.end = a.selectionEnd;
  } catch {
    /* selectionStart throws on inputs that have no text selection */
  }
  return snap;
}

function focusRestore(snap) {
  if (!snap) return;
  const n = document.querySelector(snap.sel);
  if (!n || n === document.activeElement) return;
  n.focus({ preventScroll: true });
  if (snap.start != null) {
    try {
      n.setSelectionRange(snap.start, snap.end);
    } catch {
      /* not a text control any more */
    }
  }
}

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
    state.tts = (state.info.tts && state.info.tts.engines) || [];
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

  $("#btnSettings").addEventListener("click", openSettings);
  // The header title is the obvious thing to click when you want to rename.
  $("#projectTitle").title = "Click to rename this project";
  $("#projectTitle").addEventListener("click", openSettings);
  $("#projName").addEventListener("input", () => {
    const v = $("#projName").value.trim();
    $("#btnRename").disabled = !v || !state.board || v === state.board.name;
    paintProjPath();
  });
  $("#projName").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      renameProject();
    }
  });
  $("#btnRename").addEventListener("click", renameProject);

  $("#settingsClose").addEventListener("click", () => {
    $("#settings").hidden = true;
  });
  // Click the backdrop, or press Escape, to close — same as the other dialogs.
  $("#settings").addEventListener("click", (e) => {
    if (e.target === $("#settings")) $("#settings").hidden = true;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#settings").hidden) $("#settings").hidden = true;
  });

  $("#btnExport").addEventListener("click", () => {
    if (state.slug) window.location.href = API.exportUrl(state.slug);
  });

  $("#btnOpen").addEventListener("click", openDialog);
  $("#openClose").addEventListener("click", () => ($("#openDialog").hidden = true));
  $("#openDialog").addEventListener("click", (e) => {
    if (e.target === $("#openDialog")) $("#openDialog").hidden = true;
  });
  $("#openElsewhere").addEventListener("click", () => $("#importFile").click());
  $("#importFile").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    try {
      const board = JSON.parse(await file.text());
      const { slug, board: saved } = await API.importBoard(board, board.name);
      state.boards = (await API.listBoards()).boards;
      setBoard(slug, saved);
      renderBoardPicker();
      $("#openDialog").hidden = true;
      toast(
        `Copied in “${saved.name}” as a new project. Renders stay with the ` +
          `original — this copy has none.`,
        "warn"
      );
    } catch (err) {
      toast(`Could not read that board file: ${err.message}`, "error");
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
    updateResolvedPreview();
  });

  $("#soundscape").addEventListener("input", (e) => {
    state.board.soundscape = e.target.value;
    markDirty();
    updateResolvedPreview();
  });

  $("#addCharacter").addEventListener("click", () => editCharacter(null));

  $("#projAspect").addEventListener("change", (e) => {
    // keep the closest size when the ratio changes, rather than resetting
    const opts = resolutionsByAspect()[e.target.value] || [];
    if (!opts.length) return;
    const [cw] = projectResolution().split("x").map(Number);
    const closest = opts.reduce((best, r) =>
      Math.abs(Number(r.split("x")[0]) - cw) <
      Math.abs(Number(best.split("x")[0]) - cw)
        ? r
        : best
    );
    state.board.defaults.resolution = closest;
    markDirty();
    render();
  });

  $("#projResolution").addEventListener("change", (e) => {
    state.board.defaults.resolution = e.target.value;
    markDirty();
    render();
  });

  $("#draftToggle").addEventListener("change", (e) => {
    state.board.defaults.draft = e.target.checked;
    markDirty();
    render();
  });

  $("#defModel").addEventListener("change", (e) => {
    state.board.defaults.model = e.target.value;
    markDirty();
  });

  $("#llmService").addEventListener("change", (e) => {
    state.board.defaults.llm = e.target.value;
    markDirty();
    render();
  });
  $("#ttsEngine").addEventListener("change", (e) => {
    state.board.defaults.tts = e.target.value;
    markDirty();
    render();
  });

  $("#defSteps").addEventListener("input", (e) => {
    state.board.defaults.steps = Number(e.target.value);
    markDirty();
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
   Open
   ========================================================================== */

/* This was "Import", which was the wrong verb and cost a real project: picking
   your own board's storyboard.json made a *second* project from it, carrying
   the shot list but none of the renders, while its references still pointed
   back into the original folder. Opening a board should open it, in place.
   Copying one in from elsewhere is still available, and now says what it does. */
async function openDialog() {
  const host = $("#openList");
  $("#openDialog").hidden = false;
  host.innerHTML = "";
  host.appendChild(el("div", "empty-state", "loading…"));
  try {
    state.boards = (await API.listBoards()).boards;
  } catch (err) {
    host.innerHTML = "";
    host.appendChild(el("div", "empty-state", `Could not list storyboards: ${err.message}`));
    return;
  }
  paintOpenList();
}

function paintOpenList() {
  const host = $("#openList");
  host.innerHTML = "";
  if (!state.boards.length) {
    host.appendChild(el("div", "empty-state", "No storyboards yet — use New."));
    return;
  }
  // Two projects can carry the same name, so the row has to say what each one
  // actually holds and where it is.
  const names = state.boards.reduce((a, b) => ((a[b.name] = (a[b.name] || 0) + 1), a), {});

  state.boards.forEach((b) => {
    const row = el("div", "open-row");
    if (b.slug === state.slug) row.classList.add("current");

    const main = el("div", "open-main");
    const title = el("div", "open-name");
    title.appendChild(el("span", null, b.name));
    if (b.slug === state.slug) title.appendChild(el("span", "open-tag", "open now"));
    if (names[b.name] > 1) {
      title.appendChild(
        el("span", b.rendered ? "open-tag" : "open-tag warn",
           b.rendered ? `${b.rendered} rendered` : "no renders")
      );
    }
    main.appendChild(title);
    main.appendChild(
      el("div", "open-meta",
         `${b.shots} shot${b.shots === 1 ? "" : "s"} · ` +
         `${b.rendered} rendered · ${relTime(b.updatedAt)}`)
    );
    main.appendChild(el("div", "open-path", b.configPath || `projects/${b.slug}/storyboard.json`));
    row.appendChild(main);

    const act = el("button", "btn btn-sm", b.slug === state.slug ? "Reload" : "Open");
    act.addEventListener("click", async (e) => {
      e.stopPropagation();
      act.disabled = true;
      act.textContent = "opening…";
      try {
        await saveNow();
        await openBoard(b.slug);
        renderBoardPicker();
        $("#openDialog").hidden = true;
      } catch (err) {
        toast(`Could not open: ${err.message}`, "error");
        act.disabled = false;
        act.textContent = "Open";
      }
    });
    row.appendChild(act);
    row.addEventListener("click", () => act.click());
    host.appendChild(row);
  });
}

function relTime(ts) {
  if (!ts) return "unknown";
  const secs = Math.max(0, Date.now() / 1000 - ts);
  if (secs < 90) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} hour${hrs === 1 ? "" : "s"} ago`;
  const days = Math.round(hrs / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

/* ==========================================================================
   Project name
   ========================================================================== */

/* The name field is filled when the dialog opens rather than on every render,
   so a repaint mid-render can never overwrite what you are typing. */
function openSettings() {
  $("#projName").value = state.board ? state.board.name : "";
  paintProjPath();
  $("#btnRename").disabled = true;
  $("#settings").hidden = false;
}

/** Mirror of the server's slugify, for previewing the folder a rename lands in. */
function slugify(name) {
  const s = (name || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return s.slice(0, 60) || "untitled";
}

function paintProjPath() {
  const typed = $("#projName").value.trim();
  const current = state.board ? state.board.name : "";
  const slug = typed && typed !== current ? slugify(typed) : state.slug;
  const ws = (state.info && state.info.workspace) || "<workspace>";
  const note = $("#projPath");
  note.textContent = `${ws}/projects/${slug}/`;
  note.classList.toggle("field-warn", slug !== state.slug);
  if (slug !== state.slug) {
    note.textContent += "   ← the folder moves here";
  }
}

async function renameProject() {
  const name = $("#projName").value.trim();
  if (!name || !state.board || name === state.board.name) return;

  const btn = $("#btnRename");
  btn.disabled = true;
  const was = btn.textContent;
  btn.textContent = "renaming…";
  try {
    // Any unsaved edits must land first: the rename reloads the board from
    // disk, and would otherwise discard them.
    await saveNow();
    const r = await API.renameBoard(state.slug, name);
    state.slug = r.slug;
    state.board = r.board;
    state.boards = (await API.boards()).boards;
    state.sig = null;
    render();
    paintProjPath();
    toast(`Renamed to “${r.board.name}”.`);
  } catch (err) {
    toast(`Rename failed: ${err.message}`, "error");
    btn.disabled = false;
  } finally {
    btn.textContent = was;
  }
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

    // A poll tick almost never changes the *shape* of the page — only
    // percentages, phase text and a progress bar. Rebuilding everything for
    // that flickered every image and every second (a <video> reloaded and
    // restarted), so the expensive path runs only when something structural
    // actually moved: a status transition, a new thumbnail, a new output.
    const sig = structuralSig();
    if (sig !== state.sig) {
      state.sig = sig;
      render();
    } else {
      paintLive();
    }
  } catch {
    /* transient — next tick will retry */
  }
}

/* ==========================================================================
   Render
   ========================================================================== */

function structuralSig() {
  const busy = !!(state.status && state.status.busy);
  return JSON.stringify([
    busy,
    state.selectedId,
    shots().map((raw) => {
      const v = view(raw);
      return [
        raw.id,
        v.status,
        raw.thumb || "",
        raw.dubUrl || "",
        raw.renderedAs || "",
        (v.outputs || []).join("|"),
      ];
    }),
  ]);
}

/* The poll's normal path: update in place, touch no structure, create no
   elements that already exist. Nothing here clears a container, so nothing
   flickers and nothing focused is destroyed. */
function paintLive() {
  if (!state.board) return;
  const busy = !!(state.status && state.status.busy);
  $("#btnRender").disabled = busy || !state.info.backend.healthy;
  $("#btnStop").disabled = !busy;
  paintMeta();

  shots().forEach((raw) => {
    const shot = view(raw);
    const pct = `${Math.round(shot.progress || 0)}%`;

    const row = document.querySelector(`.queue-row[data-id="${raw.id}"]`);
    if (row) {
      const q = row.querySelector(".queue-pct");
      if (q) q.textContent = pct;
    }

    const card = document.querySelector(`.shot-card[data-id="${raw.id}"]`);
    if (card) {
      const c = card.querySelector(".shot-foot .queue-pct");
      if (c) c.textContent = pct;
      const fill = card.querySelector(".progress-fill");
      if (fill) fill.style.width = `${shot.progress || 0}%`;
      const ph = card.querySelector(".progress-track + .shot-sub");
      if (ph && shot.phase) ph.textContent = shot.phase;
    }

    if (raw.id === state.selectedId) {
      const pe = document.querySelector(".preview-empty span:not(.big)");
      if (pe && shot.status === "running") {
        pe.textContent =
          `Rendering — ${pct}${shot.phase ? ` (${shot.phase})` : ""}`;
      }
      const rt = document.querySelector('#preview .stat-grid dt + dd');
      if (rt) rt.textContent = STATUS_LABELS[shot.status] || shot.status;
      paintLog(shot);
    }
  });
}

function paintLog(shot) {
  const box = $("#preview .log");
  const lines = shot.log;
  if (!box || !lines) return;
  // Only ever append: rewriting the box would fight the user's scroll and
  // flash a few hundred lines of text once a second.
  const have = Number(box.dataset.count || 0);
  if (lines.length <= have) return;

  // The box was showing "No run yet." or a saved log; this run supersedes it.
  if (!have) box.innerHTML = "";

  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
  lines.slice(have).forEach((e) => box.appendChild(logLine(e)));
  box.dataset.count = String(lines.length);

  // Hold the same window renderPreview draws, so the two agree.
  let extra = box.querySelectorAll(".log-line").length - LOG_WINDOW;
  while (extra-- > 0 && box.firstChild) box.removeChild(box.firstChild);

  if (atBottom) box.scrollTop = box.scrollHeight;
}

function render() {
  if (!state.board) return;
  const snap = focusSnapshot();
  renderBoardPicker();
  renderRail();
  renderStrip();
  renderEditor();
  renderPreview();

  const busy = !!(state.status && state.status.busy);
  $("#btnRender").disabled = busy || !state.info.backend.healthy;
  $("#btnStop").disabled = !busy;

  paintMeta();
  focusRestore(snap);
}

function paintMeta() {
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
  const snd = $("#soundscape");
  if (document.activeElement !== snd) snd.value = state.board.soundscape || "";

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

  // Built fresh each render rather than moved: this container is cleared with
  // innerHTML, so a button living inside it would be destroyed on the first
  // render and appendChild(null) would throw on the second.
  const add = el("button", "style-ref-add", "+");
  add.title = "Add a reference image (pick an existing one or upload)";
  add.addEventListener("click", async () => {
    const chosen = await chooseImage("Choose a style reference");
    if (!chosen) return;
    state.board.styleRefs.push(chosen);
    markDirty();
    await saveNow();
    render();
  });
  wrap.appendChild(add);

  // Drag images straight in from Finder.
  wrap.ondragover = (e) => {
    e.preventDefault();
    wrap.classList.add("dropping");
  };
  wrap.ondragleave = () => wrap.classList.remove("dropping");
  wrap.ondrop = async (e) => {
    e.preventDefault();
    wrap.classList.remove("dropping");
    for (const f of [...(e.dataTransfer.files || [])]) {
      if (!f.type.startsWith("image/")) continue;
      try {
        state.board.styleRefs.push(await API.uploadRef(state.slug, f));
      } catch (err) {
        toast(`Upload failed: ${err.message}`, "error");
      }
    }
    markDirty();
    await saveNow();
    render();
  };

  // clip settings (project-level): aspect first, then a size within it
  const groups = resolutionsByAspect();
  const current = projectResolution();
  const currentAspect = aspectOf(current);

  const aspSel = $("#projAspect");
  aspSel.innerHTML = "";
  Object.keys(groups).forEach((a) => {
    const o = el("option", null, a);
    o.value = a;
    if (a === currentAspect) o.selected = true;
    aspSel.appendChild(o);
  });

  const resSel = $("#projResolution");
  resSel.innerHTML = "";
  (groups[currentAspect] || []).forEach((r) => {
    const tested = isTestedResolution(r);
    const o = el("option", null, r.replace("x", " × ") + (tested ? "" : "  (untested)"));
    o.value = r;
    if (r === current) o.selected = true;
    resSel.appendChild(o);
  });

  // Say plainly when a size is outside what the model's docs cite. It will
  // usually work; it is just not a promise, and the cost scales with pixels.
  const noteHost = $("#resNoteField");
  noteHost.innerHTML = "";
  if (!isTestedResolution(current)) {
    noteHost.appendChild(
      el(
        "div",
        "field-warn",
        `⚠ ${current} is not a size this model's docs cite — usually fine, but ` +
          `untested, and cost scales with pixel count`
      )
    );
  }

  // draft mode
  const dt = $("#draftToggle");
  const draftOn = !!state.board.defaults.draft;
  dt.checked = draftOn;
  const [cw, ch] = current.split("x").map(Number);
  const dw = Math.max(320, Math.floor(cw / 2 / 16) * 16);
  const dh = Math.max(192, Math.floor(ch / 2 / 16) * 16);
  $("#draftNote").textContent = draftOn
    ? `Rendering at ${dw}×${dh} and 6 steps — roughly 4x faster. Clip length ` +
      `and seed are unchanged, so the camera move is the one you will get.`
    : `Drafts render at half size (${dw}×${dh}) and 6 steps to check framing ` +
      `and motion quickly. Length and seed stay the same.`;

  const modelSel = $("#defModel");
  if (modelSel.dataset.built !== "1") {
    state.models.forEach((m) => {
      const o = el("option", null, m.available ? m.label : `${m.label} — unavailable`);
      o.value = m.id;
      modelSel.appendChild(o);
    });
    modelSel.dataset.built = "1";
  }
  modelSel.value = state.board.defaults.model || "fl2va";

  const ttsSel = $("#ttsEngine");
  if (ttsSel.dataset.built !== "1") {
    state.tts.forEach((e) => {
      const o = el("option", null, e.healthy ? e.label : `${e.label} — unavailable`);
      o.value = e.id;
      ttsSel.appendChild(o);
    });
    ttsSel.dataset.built = "1";
  }
  const chosenTts = state.board.defaults.tts || "none";
  ttsSel.value = chosenTts;
  const eng = state.tts.find((e) => e.id === chosenTts);
  const noteBox = $("#ttsNote");
  noteBox.textContent = eng && !eng.healthy ? eng.message : "";
  noteBox.className = eng && !eng.healthy ? "field-warn" : "field-note";

  const llmSel = $("#llmService");
  const llmAll = (state.info.llm && state.info.llm.services) || [];
  if (llmSel.dataset.built !== "1" && llmAll.length) {
    llmAll.forEach((sv) => {
      const o = el("option", null,
        sv.id === "none" ? sv.label
          : sv.healthy ? `${sv.label} · ${sv.model}`
          : `${sv.label} — unavailable`);
      o.value = sv.id;
      llmSel.appendChild(o);
    });
    llmSel.dataset.built = "1";
  }
  const chosenLlm =
    state.board.defaults.llm || (state.info.llm && state.info.llm.default) || "none";
  llmSel.value = chosenLlm;
  const sv = llmAll.find((x) => x.id === chosenLlm);
  const llmNote = $("#llmNote");
  llmNote.textContent = sv && !sv.healthy
    ? sv.message
    : sv && sv.id !== "none"
    ? "Rewrites are shown for approval before they replace anything."
    : "";
  llmNote.className = sv && !sv.healthy ? "field-warn" : "field-note";

  const stepsInput = $("#defSteps");
  if (document.activeElement !== stepsInput) {
    stepsInput.value = state.board.defaults.steps || 8;
  }

  renderCast();

  const q = $("#queueList");
  q.innerHTML = "";
  shots().forEach((raw, i) => {
    const shot = view(raw);
    const row = el("div", "queue-row");
    row.dataset.id = raw.id;
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

/* --- cast ---------------------------------------------------------------- */

function renderCast() {
  const host = $("#castList");
  host.innerHTML = "";
  const cast = state.board.characters || [];
  if (!cast.length) {
    host.appendChild(
      el("div", "cast-note", "No characters yet. Add one to describe someone who should look and sound the same every time they appear.")
    );
    return;
  }
  cast.forEach((ch) => {
    const row = el("div", "cast-row");
    const av = el("div", "cast-avatar");
    if (ch.image && (ch.image.url || ch.image.path)) {
      const img = el("img");
      img.src = ch.image.url || ch.image.path;
      av.appendChild(img);
    } else {
      av.textContent = (ch.name || "?").slice(0, 1).toUpperCase();
    }
    row.appendChild(av);

    const body = el("div", "cast-body");
    body.appendChild(el("div", "cast-name", ch.name || "(unnamed)"));
    body.appendChild(el("div", "cast-desc", ch.description || "no description"));
    row.appendChild(body);

    const badges = el("div", "cast-badges");
    if (ch.image) badges.appendChild(el("span", "cast-badge", "img"));
    if (ch.voice) badges.appendChild(el("span", "cast-badge", "voice"));
    row.appendChild(badges);

    row.onclick = () => editCharacter(ch);
    host.appendChild(row);
  });
}

/** Modal editor. Name and description are required; media is optional. */
async function editCharacter(existing) {
  const modal = $("#castEditor");
  const isNew = !existing;
  const draft = existing
    ? JSON.parse(JSON.stringify(existing))
    : { id: null, name: "", description: "", image: null, voice: null };

  $("#castEditorTitle").textContent = isNew
    ? "New character"
    : `Edit ${draft.name || "character"}`;
  $("#castName").value = draft.name || "";
  $("#castDesc").value = draft.description || "";
  $("#castVoiceText").value = draft.voiceText || "";
  const trBtn = $("#castTranscribe");
  trBtn.onclick = () => autoTranscribe(draft);
  trBtn.disabled = !(draft.voice && draft.voice.path);
  $("#castNote").textContent =
    "The image and voice become reference inputs on models that accept them " +
    "(Ref2VA: up to 9 images and 3 voices per shot). On a model without " +
    "reference lists only the description is used. A voice-cloning speech " +
    "engine also needs the transcript above — leave it blank and the server " +
    "will transcribe the clip itself.";

  // NOTE: these are declared in this scope, not inside the Promise below.
  // A function declaration is only hoisted within its own function scope, so
  // defining them in the executor made paint() throw a ReferenceError before
  // the modal was ever unhidden — the dialog simply never appeared.
  const paint = () => {
    mediaSlot($("#castImage"), draft, "image", "Image", "image");
    mediaSlot($("#castVoice"), draft, "voice", "Voice clip", "audio");
  };

  function mediaSlot(host, obj, key, label, kind) {
    host.innerHTML = "";
    host.className = "ref-slot";
    host.onclick = () => pickMedia(obj, key, kind);
    const cur = obj[key];
    if (cur) {
      host.classList.add("filled");
      host.title = "Click to replace";
      if (kind === "image") {
        const img = el("img");
        img.src = cur.url || cur.path;
        host.appendChild(img);
      } else {
        // An attached clip should be identifiable and audible, not just a
        // word saying it is there: filenames all look alike, and the whole
        // point of a reference voice is how it sounds.
        host.classList.add("ref-slot-audio");
        const l = el("div", "ref-slot-label");
        l.append(
          el("strong", null, "♪ " + (cur.label || "voice clip")),
          el("span", null, "attached — click to change")
        );
        host.appendChild(l);
        if (cur.url) {
          const au = el("audio");
          au.src = cur.url;
          au.controls = true;
          au.preload = "none";
          au.className = "ref-slot-player";
          // The slot itself opens the picker; the player must not.
          au.onclick = (e) => e.stopPropagation();
          host.appendChild(au);
        }
      }
      const x = el("button", "clear-ref", "✕");
      x.title = "Remove";
      x.onclick = (e) => {
        e.stopPropagation();
        obj[key] = null;
        if (key === "voice") $("#castTranscribe").disabled = true;
        paint();
      };
      host.appendChild(x);
    } else {
      const l = el("div", "ref-slot-label");
      l.append(el("strong", null, label), el("span", null, "optional — click to add"));
      host.appendChild(l);
    }
  }

  /* Both kinds go through the picker, which offers what is already in the
     project as well as an upload. Audio used to jump straight to a file
     dialog, which meant a clip already uploaded into refs/ could never be
     attached — you could only upload it a second time. */
  async function pickMedia(obj, key, kind) {
    const who = $("#castName").value.trim() || "this character";
    const chosen = await chooseMedia(
      kind === "image"
        ? `Choose an image for ${who}`
        : `Choose a reference voice for ${who}`,
      kind
    );
    if (!chosen) return;
    obj[key] = chosen;
    // A new clip invalidates a transcript of the old one, and the Transcribe
    // button is only usable once something is attached.
    if (kind === "audio") {
      $("#castVoiceText").value = "";
      draft.voiceText = "";
      $("#castTranscribe").disabled = false;
    }
    paint();
  }

  function castError(msg) {
    // Reported in the dialog as well as the toast: an error about something
    // you are doing inside a modal should appear inside that modal.
    const box = $("#castNote");
    box.textContent = msg;
    box.className = msg ? "field-warn" : "cast-note";
    if (msg) toast(msg, "error");
  }

  async function autoTranscribe(obj) {
    const clip = obj.voice && obj.voice.path;
    if (!clip) return;
    const field = $("#castVoiceText");
    const prev = field.value;
    field.value = "transcribing…";
    field.disabled = true;
    try {
      const r = await API.transcribe(clip, state.board.defaults.tts);
      field.value = r.text;
      draft.voiceText = r.text;
      toast("Transcribed the reference clip — check it reads correctly.");
    } catch (err) {
      field.value = prev;
      castError(
        `Could not transcribe: ${err.message} — type what the clip says instead.`
      );
    } finally {
      field.disabled = false;
    }
  }

  const foot = $("#castFoot");
  foot.innerHTML = "";
  if (!isNew) {
    const del = el("button", "btn btn-sm btn-danger", "Delete character");
    del.onclick = async () => {
      state.board.characters = (state.board.characters || []).filter(
        (c) => c.id !== draft.id
      );
      shots().forEach((sh) => {
        sh.characterIds = (sh.characterIds || []).filter((id) => id !== draft.id);
      });
      modal.hidden = true;
      markDirty();
      await saveNow();
      render();
    };
    foot.appendChild(del);
  } else {
    foot.appendChild(
      el("span", null, "Name and description are required; image and voice are optional.")
    );
  }

  paint();
  modal.hidden = false;
  $("#castName").focus();

  return new Promise((resolve) => {
    const close = () => {
      modal.hidden = true;
      resolve();
    };
    $("#castCancel").onclick = close;
    modal.onclick = (e) => {
      if (e.target === modal) close();
    };
    $("#castSave").onclick = async () => {
      draft.name = $("#castName").value.trim();
      draft.description = $("#castDesc").value.trim();
      draft.voiceText = $("#castVoiceText").value.trim();
      if (!draft.name || !draft.description) {
        castError("A character needs both a name and a description.");
        return;
      }
      state.board.characters = state.board.characters || [];
      if (draft.id) {
        const i = state.board.characters.findIndex((c) => c.id === draft.id);
        state.board.characters[i] = draft;
      } else {
        draft.id = "c" + Math.random().toString(36).slice(2, 10);
        state.board.characters.push(draft);
      }
      close();
      markDirty();
      await saveNow();
      render();
    };
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
      // A recorded thumbnail can outlive the file it names — a folder cleaned
      // out, a project copied without its renders. Fall back rather than
      // showing a broken image.
      img.onerror = () => {
        img.remove();
        const ph = el("img", "shot-placeholder");
        ph.src = "assets/shot-placeholder.png";
        ph.alt = "";
        thumb.insertBefore(ph, thumb.firstChild);
      };
      thumb.appendChild(img);
    } else {
      // A generated placeholder rather than empty space, so an unrendered
      // card still reads as a shot. Deliberately low contrast: it is a hint
      // about what is missing, not something competing with real frames.
      const ph = el("img", "shot-placeholder");
      ph.src = "assets/shot-placeholder.png";
      ph.alt = "";
      thumb.appendChild(ph);
      thumb.appendChild(el("div", "shot-thumb-empty", "not rendered"));
    }
    thumb.appendChild(el("span", "shot-index", String(i + 1).padStart(2, "0")));
    if (raw.renderedAs === "draft") {
      thumb.appendChild(el("span", "draft-badge", "DRAFT"));
    }
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
        `${projectResolution()}` +
          (cap && cap.kind === "image" ? " · still" : ` · ${fmtDur(raw.frames)}`) +
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
  let panelDubRow = null;

  const head = el("div", "editor-head");
  const h = el("div", "section-label", `Shot ${idx + 1}`);
  h.style.margin = "0";
  head.append(h, chip(shot.status));
  const sp = el("div");
  sp.style.flex = "1";
  head.appendChild(sp);

  if ((raw.dialogue || "").trim()) {
    const dubRow = el("div");
    dubRow.style.cssText = "display:flex;gap:var(--sp-2);align-items:center;margin-top:var(--sp-2)";
    const engNow = state.tts.find((e) => e.id === (state.board.defaults.tts || "none"));
    const dubBtn = el("button", "btn btn-sm", raw.dubUrl ? "Re-dub" : "Speak this line");
    dubBtn.disabled = !engNow || !engNow.healthy;
    dubBtn.title = engNow && !engNow.healthy ? engNow.message : "Synthesise and mux";
    dubBtn.onclick = async () => {
      dubBtn.disabled = true;
      dubBtn.textContent = "speaking…";
      try {
        const r = await API.dub(state.slug, raw.id);
        raw.dubUrl = r.dubUrl;
        if (r.warning) toast(r.warning, "warn");
        else toast(`Dubbed with ${r.engine} (${r.seconds}s of speech).`);
        render();
      } catch (err) {
        toast(`Dub failed: ${err.message}`, "error");
        render();
      }
    };
    dubRow.appendChild(dubBtn);
    if (raw.dubUrl) dubRow.appendChild(el("span", "field-note", "dubbed version available"));
    if (engNow && !engNow.healthy) {
      dubRow.appendChild(el("span", "field-warn", "speech engine unavailable"));
    }
    panelDubRow = dubRow;
  }

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

  /* The four long-form fields share one tall pane instead of stacking four
     short boxes down the page. Each was 2-5 rows before, which is not enough
     to see a paragraph you are actually writing. Tabs cost a click but the
     content markers on each tab keep what is hidden visible. */
  const tabs = [
    { id: "prompt", label: "Shot prompt", has: () => (raw.prompt || "").trim() },
    ...(!cap || cap.supportsAudio
      ? [
          { id: "dialogue", label: "Dialogue", has: () => (raw.dialogue || "").trim() },
          { id: "sound", label: "Sound accents", has: () => (raw.soundNote || "").trim() },
        ]
      : []),
    { id: "resolved", label: "Resolved", has: () => false },
  ];
  if (!tabs.some((t) => t.id === state.tab)) state.tab = "prompt";

  const bar = el("div", "tabs");
  const panes = el("div", "tab-panes");

  const showTab = (id) => {
    state.tab = id;
    [...bar.children].forEach((b) => (b.dataset.on = String(b.dataset.tab === id)));
    [...panes.children].forEach((pn) => (pn.hidden = pn.dataset.tab !== id));
    if (id === "resolved") paintResolvedInto(resolved, raw);
  };

  tabs.forEach((t) => {
    const b = el("button", "tab");
    b.dataset.tab = t.id;
    b.appendChild(el("span", null, t.label));
    // A tab with something in it says so, so tabbing away never hides the
    // fact that a shot has dialogue or sound notes.
    if (t.has()) b.appendChild(el("span", "tab-dot"));
    b.addEventListener("click", () => showTab(t.id));
    bar.appendChild(b);
  });
  panel.appendChild(bar);

  const pane = (id, node) => {
    const pn = el("div", "tab-pane");
    pn.dataset.tab = id;
    pn.appendChild(node);
    panes.appendChild(pn);
    return pn;
  };

  /* --- shot prompt, with the rewrite button ----------------------------- */
  const ta = el("textarea", "ta-tall");
  ta.value = raw.prompt || "";  ta.placeholder =
    "What happens in THIS shot — action, camera move, mood.\n" +
    "The scene description is prepended automatically; don't restate the subject.";
  ta.addEventListener("input", () => {
    raw.prompt = ta.value;
    markDirty();
    updateResolvedPreview();
    syncTabDots();
  });

  const promptPane = el("div");
  const promptHead = el("div", "pane-head");
  promptHead.appendChild(
    el("span", "pane-hint", "action / camera / mood only — not the subject or the style")
  );
  promptHead.appendChild(el("div", "header-spacer"));
  promptHead.appendChild(wandButton(raw, ta));
  promptPane.append(promptHead, ta, el("div", "proposal-slot"));
  pane("prompt", promptPane);

  if (!cap || cap.supportsAudio) {
    const dlg = el("textarea", "ta-tall");
    dlg.value = raw.dialogue || "";
    dlg.placeholder =
      "A line someone speaks in this clip.\n" +
      "Synthesised by the speech engine and muxed over the finished clip — " +
      "the video model itself does not produce intelligible dialogue.";
    dlg.addEventListener("input", () => {
      raw.dialogue = dlg.value;
      markDirty();
      syncTabDots();
    });
    dlg.dataset.fkey = "dialogue";
    const dp = el("div");
    dp.append(
      paneHint("spoken separately, then mixed over the finished clip"),
      dlg
    );
    pane("dialogue", dp);

    const sa = el("textarea", "ta-tall");
    sa.value = raw.soundNote || "";
    sa.placeholder =
      "Sound accents for THIS clip — what happens sonically here.\n" +
      "The project background sound is already applied to every shot.";
    sa.addEventListener("input", () => {
      raw.soundNote = sa.value;
      markDirty();
      updateResolvedPreview();
      syncTabDots();
    });
    sa.dataset.fkey = "sound-accents";
    const sp2 = el("div");
    sp2.append(
      paneHint("this clip only — the project background sound is already applied"),
      sa
    );
    pane("sound", sp2);
  }

  /* --- resolved -------------------------------------------------------- */
  const resolved = el("div", "resolved-pane");
  resolved.id = "resolvedPrompt";
  const rp = el("div");
  rp.append(
    paneHint("exactly what the backend assembles, in order, from these sources"),
    resolved
  );
  pane("resolved", rp);

  panel.appendChild(panes);
  ta.dataset.fkey = "shot-prompt";
  showTab(state.tab);
  if (panelDubRow) panel.appendChild(panelDubRow);
  host.appendChild(panel);

  // which cast members appear in this shot
  const cast = state.board.characters || [];
  if (cast.length) {
    const castPanel = el("div", "panel");
    castPanel.style.marginTop = "var(--sp-3)";
    const cl = el("div", "section-label", "Characters in this shot");
    cl.appendChild(el("span", "hint", "— refer to them by name in the prompt"));
    castPanel.appendChild(cl);

    const picks = el("div", "cast-picks");
    cast.forEach((ch) => {
      const on = (raw.characterIds || []).includes(ch.id);
      const pill = el("div", "cast-pick");
      pill.dataset.on = String(on);
      if (ch.image && (ch.image.url || ch.image.path)) {
        const img = el("img");
        img.src = ch.image.url || ch.image.path;
        pill.appendChild(img);
      }
      pill.appendChild(el("span", null, ch.name || "(unnamed)"));
      pill.onclick = () => {
        const ids = new Set(raw.characterIds || []);
        if (ids.has(ch.id)) ids.delete(ch.id);
        else ids.add(ch.id);
        raw.characterIds = [...ids];
        markDirty();
        renderEditor();
      };
      picks.appendChild(pill);
    });
    castPanel.appendChild(picks);

    // Honest about what actually reaches the model on this shot's model.
    const chosen = cast.filter((c) => (raw.characterIds || []).includes(c.id));
    const withMedia = chosen.filter((c) => c.image || c.voice);
    if (chosen.length && withMedia.length && cap && !cap.supportsStyleRefs) {
      castPanel.appendChild(
        el(
          "div",
          "inline-warn",
          `⚠ ${withMedia.length} of these have a reference image or voice, but ` +
            `this model takes no reference list — only their descriptions will ` +
            `be used. Switch the shot to Ref2VA to use the media.`
        )
      );
    }
    host.appendChild(castPanel);
  }

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
  const row = el("div", "field-row");
  if (!isImage) {
    // A duration picker rather than a frames box. Clip length is not free —
    // H3 only accepts 17n+5 frames and will not decode below 39 — so offering
    // every integer just invites a value that gets silently snapped. People
    // think in seconds anyway; the frame count stays visible so nothing is
    // hidden.
    const options = validLengths(cap);
    const exact = options.some(([f]) => f === raw.frames);
    if (!exact) options.push([raw.frames, `${fmtDur(raw.frames)} (${raw.frames}f, custom)`]);
    options.sort((a, b) => a[0] - b[0]);

    row.appendChild(
      field(
        "Duration",
        select(
          options.map(([f, label]) => [String(f), label]),
          String(raw.frames),
          (v) => {
            raw.frames = Number(v);
            markDirty();
            renderEditor();   // safe: a select commits on change, not per key
            renderStrip();
          }
        ),
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
      "Length",
      readOnly(isImage ? "still image" : `${raw.frames} frames @ 24fps`)
    )
  );
  params.appendChild(row2);

  const projRes = projectResolution();
  if (cap && cap.resolutions.length && !cap.resolutions.includes(projRes)) {
    params.appendChild(
      el(
        "div",
        "inline-warn",
        `⚠ the project resolution ${projRes} is not one this model lists ` +
          `(${cap.resolutions.join(", ")}) — change it in Clip settings`
      )
    );
  }
  if (cap && !cap.available) {
    params.appendChild(el("div", "inline-warn", `⚠ ${cap.unavailableReason}`));
  }
  host.appendChild(params);
}

/** The one resolution every shot renders at — a storyboard makes one video. */
function projectResolution() {
  return (state.board && state.board.defaults && state.board.defaults.resolution)
    || "960x544";
}

/** Every resolution any available model offers, for the project-level picker. */
function allResolutions() {
  const seen = [];
  state.models.forEach((m) => {
    if (!m.available) return;
    (m.resolutions || []).forEach((r) => {
      if (!seen.includes(r)) seen.push(r);
    });
  });
  return seen.length ? seen : ["960x544"];
}

/** Nearest common ratio name for a WxH string. */
function aspectOf(res) {
  const [w, h] = res.split("x").map(Number);
  if (!w || !h) return "other";
  const r = w / h;
  const named = [
    ["16:9", 16 / 9],
    ["4:3", 4 / 3],
    ["1:1", 1],
    ["9:16", 9 / 16],
    ["3:4", 3 / 4],
  ];
  let best = "other";
  let bestErr = 0.06;   // within ~6% counts as that ratio
  named.forEach(([name, target]) => {
    const err = Math.abs(r - target) / target;
    if (err < bestErr) {
      bestErr = err;
      best = name;
    }
  });
  return best;
}

/** Aspect -> resolutions, from what the models actually offer. */
function resolutionsByAspect() {
  const groups = {};
  allResolutions().forEach((r) => {
    const a = aspectOf(r);
    (groups[a] = groups[a] || []).push(r);
  });
  return groups;
}

/** Is this size one the selected model's own docs cite? */
function isTestedResolution(res) {
  const vids = state.models.filter((m) => m.available && m.kind === "video");
  if (!vids.length) return true;
  return vids.some((m) => (m.testedResolutions || []).includes(res));
}

/** Legal clip lengths for a model, as [frames, label] up to ~10s. */
function validLengths(cap) {
  const out = [];
  if (!cap) return out;
  const r = cap.frameRule;
  if (r.kind !== "affine") {
    for (const f of [24, 48, 72, 96, 120, 144, 192, 240]) {
      if (f >= r.minimum) out.push([f, `${fmtDur(f)} (${f}f)`]);
    }
    return out;
  }
  for (let n = 0; n < 40; n += 1) {
    const f = r.step * n + r.offset;
    if (f < r.minimum) continue;
    if (f > 250) break;
    out.push([f, `${fmtDur(f)} (${f}f)`]);
  }
  return out;
}

function fmtDur(frames, fps = 24) {
  return `${(frames / fps).toFixed(1)}s`;
}

/**
 * Mirror of the backend's prompt assembly: scene, then this shot, then the
 * soundscape as a trailing clause — and no soundscape at all for a model with
 * no audio, where it would only compete with the visual description.
 */
function updateResolvedPreview() {
  // Only touches the DOM if the node is actually in the document — during a
  // render the element still lives in a detached subtree, which is why the
  // text is set directly there instead of through this function.
  if (state.tab === "resolved") paintResolvedPane();
}

/* The assembled prompt, broken into the pieces it came from. Both the joined
   string and the segmented view are built from this one list, so the preview
   cannot drift from what is sent — and where each clause came from is on
   screen, rather than the reader having to guess whether a line about a
   character came from the scene description or the cast. */
function resolvedParts(raw) {
  const cap = modelCap(raw.model);
  const out = [];
  const push = (source, text) => {
    text = (text || "").trim();
    if (text) out.push({ source, text });
  };

  push("scene", state.board.sceneDescription);

  // Only the cast this shot actually uses. A character in the board's cast who
  // is not cast in this shot contributes nothing.
  (state.board.characters || [])
    .filter((c) => (raw.characterIds || []).includes(c.id))
    .forEach((c) => {
      const n = (c.name || "").trim();
      const d = (c.description || "").trim();
      if (d) push(`cast · ${n || "unnamed"}`, n ? `${n}: ${d}` : d);
    });

  push("shot", raw.prompt);

  if (!cap || cap.supportsAudio) {
    push("sound bed", state.board.soundscape);   // project-wide
    push("accents", raw.soundNote);              // this clip only
  }
  return out;
}

/** Exactly what the backend will assemble, so the preview cannot drift. */
function resolvedPromptText(raw) {
  const joined = resolvedParts(raw)
    .map(({ text }) => (".!?;:,".includes(text.slice(-1)) ? text : text + "."))
    .join(" ");
  return joined || "(empty)";
}

/* Takes the node rather than looking it up: during renderEditor the pane is
   still in a detached subtree, so a document query would find nothing and the
   tab would paint blank. */
function paintResolvedInto(host, raw) {
  if (!host || !raw) return;
  host.innerHTML = "";

  const parts = resolvedParts(raw);
  if (!parts.length) {
    host.appendChild(el("div", "empty-state", "Nothing to send yet."));
    return;
  }
  parts.forEach(({ source, text }) => {
    const seg = el("div", "seg");
    seg.appendChild(el("span", "seg-source", source));
    seg.appendChild(el("span", "seg-text", text));
    host.appendChild(seg);
  });

  const total = el("div", "seg-total");
  const words = resolvedPromptText(raw).split(/\s+/).filter(Boolean).length;
  total.textContent = `${parts.length} parts · ${words} words, joined in this order`;
  host.appendChild(total);
}

/** Repaint whatever is already on screen, for live edits after a render. */
function paintResolvedPane() {
  paintResolvedInto($("#resolvedPrompt"), selectedShot());
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

  // Rebuilding a <video> reloads the file and restarts playback, and
  // rebuilding an <img> re-decodes it — both read as a flash. When the source
  // has not changed, carry the existing element across instead.
  const prev = host.querySelector(".preview-stage > video, .preview-stage > img");
  const prevSrc = prev ? prev.getAttribute("src") : null;
  const reuse = (want, make) => {
    if (prev && prevSrc === want) return prev;
    return make();
  };

  host.innerHTML = "";
  const raw = selectedShot();
  if (!raw) return;
  const shot = view(raw);

  host.appendChild(el("div", "section-label", "Output"));

  const stage = el("div", "preview-stage");
  const video = raw.dubUrl || (shot.outputs || []).find((u) => u.endsWith(".mp4"));
  const image = (shot.outputs || []).find((u) => /\.(jpe?g|png|webp)$/i.test(u));
  if (video) {
    stage.appendChild(
      reuse(video, () => {
        const v = el("video");
        v.src = video;
        v.controls = true;
        v.loop = true;
        v.muted = false;
        return v;
      })
    );
  } else if (image) {
    stage.appendChild(
      reuse(image, () => {
        const img = el("img");
        img.src = image;
        return img;
      })
    );
  } else if (raw.thumb) {
    stage.appendChild(
      reuse(raw.thumb, () => {
        const img = el("img");
        img.src = raw.thumb;
        return img;
      })
    );
  } else {
    const ph = el("img", "preview-placeholder");
    ph.src = "assets/shot-placeholder.png";
    ph.alt = "";
    stage.appendChild(ph);
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
    ["Rendered", raw.renderedAs === "draft" ? "draft (half size, 6 steps)"
                 : raw.renderedAs === "final" ? "final" : "—"],
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
    // No live log — the batch may have finished in an earlier server session,
    // so fall back to the run.log written next to the outputs.
    if (raw.logUrl) {
      box.appendChild(el("span", "log-empty", "loading saved log…"));
      fetch(raw.logUrl)
        .then((r) => (r.ok ? r.text() : Promise.reject()))
        .then((text) => {
          box.innerHTML = "";
          text.trimEnd().split("\n").slice(-200)
            .forEach((t) => box.appendChild(logLine(t)));
          box.scrollTop = box.scrollHeight;
        })
        .catch(() => {
          box.innerHTML = "";
          box.appendChild(el("span", "log-empty", "No saved log for this run."));
        });
    } else {
      box.appendChild(el("span", "log-empty", "No run yet."));
    }
  } else {
    lines.slice(-LOG_WINDOW).forEach((e) => box.appendChild(logLine(e)));
    // paintLog appends from here rather than rebuilding, so it needs to know
    // how much of the log is already on screen.
    box.dataset.count = String(lines.length);
  }
  host.appendChild(box);
  box.scrollTop = box.scrollHeight;
}

/* Two shapes reach here: live lines arrive as {level, text} from the
   orchestrator, saved run.log lines as raw strings with a bracketed level. */
function logLine(entry) {
  let level = "INFO";
  let text = "";
  if (entry && typeof entry === "object") {
    level = entry.level || "INFO";
    text = entry.text || "";
  } else {
    const m = String(entry).match(/^\[([A-Z]+)\]\s*(.*)$/);
    level = m ? m[1] : "INFO";
    text = m ? m[2] : String(entry);
  }
  const line = el("div", "log-line");
  line.dataset.lvl = level;
  line.appendChild(el("span", "lvl", `[${level}] `));
  line.appendChild(el("span", null, text));
  return line;
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

/* --- prompt rewriting ---------------------------------------------------- */

function paneHint(text) {
  const h = el("div", "pane-head");
  h.appendChild(el("span", "pane-hint", text));
  return h;
}

/** Refresh the "this tab has content" markers without rebuilding the editor. */
function syncTabDots() {
  const raw = selectedShot();
  if (!raw) return;
  const has = {
    prompt: (raw.prompt || "").trim(),
    dialogue: (raw.dialogue || "").trim(),
    sound: (raw.soundNote || "").trim(),
  };
  document.querySelectorAll("#editor .tab").forEach((b) => {
    const want = !!has[b.dataset.tab];
    const dot = b.querySelector(".tab-dot");
    if (want && !dot) b.appendChild(el("span", "tab-dot"));
    if (!want && dot) dot.remove();
  });
}

/* A rewrite is a proposal, never an edit. The prompt is the user's authorship
   and it took thought to write, so the model's version appears alongside it
   with an explicit Use this — replacing the text outright would destroy work
   with no way back. */
function wandButton(raw, textarea) {
  const svc = currentLLM();
  const btn = el("button", "btn btn-sm wand");
  btn.append(el("span", "wand-icon", "🪄"), el("span", null, "Rewrite"));

  if (!svc || svc.id === "none") {
    btn.disabled = true;
    btn.title =
      "No language model is configured. Pick one in ⚙ Settings, or add it to " +
      "llm-services.json.";
    return btn;
  }
  if (!svc.healthy) {
    btn.disabled = true;
    btn.title = svc.message || `${svc.label} is not reachable`;
    return btn;
  }
  btn.title =
    `Restyle this shot prompt for ${modelLabel(raw.model)} using ` +
    `${svc.label} (${svc.model}). Takes up to a minute on a local model, and ` +
    `shows you the result before changing anything.`;

  btn.addEventListener("click", async () => {
    const slot = btn.closest(".tab-pane").querySelector(".proposal-slot");
    slot.innerHTML = "";
    btn.disabled = true;
    btn.classList.add("busy");
    const label = btn.lastChild;
    label.textContent = "rewriting…";
    try {
      const r = await API.rewrite(state.slug, raw.id, textarea.value);
      slot.appendChild(proposalBox(r, raw, textarea, slot));
    } catch (err) {
      slot.appendChild(el("div", "inline-warn", `⚠ Rewrite failed: ${err.message}`));
    } finally {
      btn.disabled = false;
      btn.classList.remove("busy");
      label.textContent = "Rewrite";
    }
  });
  return btn;
}

function proposalBox(r, raw, textarea, slot) {
  const box = el("div", "proposal");
  const head = el("div", "proposal-head");
  head.appendChild(el("strong", null, "Proposed rewrite"));
  head.appendChild(el("span", "hint", `— ${r.service}${r.model ? ` · ${r.model}` : ""}`));
  box.appendChild(head);

  const body = el("div", "proposal-body mono", r.text);
  box.appendChild(body);

  const acts = el("div", "proposal-acts");
  const use = el("button", "btn btn-sm btn-primary", "Use this");
  use.addEventListener("click", () => {
    textarea.value = r.text;
    raw.prompt = r.text;
    markDirty();
    updateResolvedPreview();
    syncTabDots();
    slot.innerHTML = "";
    toast("Shot prompt replaced. Undo by editing it back — the old text is above.");
  });
  const drop = el("button", "btn btn-sm btn-ghost", "Discard");
  drop.addEventListener("click", () => (slot.innerHTML = ""));
  acts.append(use, drop);
  box.appendChild(acts);
  return box;
}

function currentLLM() {
  const want = (state.board && state.board.defaults && state.board.defaults.llm) ||
    (state.info.llm && state.info.llm.default);
  const all = (state.info.llm && state.info.llm.services) || [];
  return all.find((s) => s.id === want) || all.find((s) => s.id !== "none") || null;
}

function modelLabel(id) {
  const c = modelCap(id);
  return c ? c.label.split("·")[0].trim() : id;
}

/* --- small controls ------------------------------------------------------ */

function field(label, control, hint, warn) {
  const f = el("div", "field");
  const l = el("label", null, label);
  if (hint) {
    const s = el("span", "hint-inline", "  " + hint);
    l.appendChild(s);
  }
  // A stable handle for focusRestore: the editor's controls have no ids, so
  // the label they sit under names them.
  const ctl = /^(INPUT|TEXTAREA|SELECT)$/.test(control.tagName)
    ? control
    : control.querySelector && control.querySelector("input, textarea, select");
  if (ctl && !ctl.dataset.fkey) {
    ctl.dataset.fkey = String(label).toLowerCase().replace(/[^a-z0-9]+/g, "-");
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
    l.append(el("strong", null, label), el("span", null, "click to choose · or drop an image"));
    slot.appendChild(l);
    slot.addEventListener("click", () => pickRef(shot, key));
    slot.ondragover = (e) => {
      e.preventDefault();
      slot.classList.add("dropping");
    };
    slot.ondragleave = () => slot.classList.remove("dropping");
    slot.ondrop = async (e) => {
      e.preventDefault();
      slot.classList.remove("dropping");
      const f = [...(e.dataTransfer.files || [])].find((x) => x.type.startsWith("image/"));
      if (!f) return;
      try {
        shot[key] = await API.uploadRef(state.slug, f);
        markDirty();
        await saveNow();
        render();
      } catch (err) {
        toast(`Upload failed: ${err.message}`, "error");
      }
    };
  }
  return slot;
}

/**
 * Choose an image: pick one already in the workspace, or upload a new one.
 * Picking matters because references usually already exist on disk — having
 * only an upload button means re-uploading a file that is right there.
 */
async function chooseImage(title) {
  return chooseMedia(title, "image");
}

/* One picker for both kinds. Audio needed the same "already in the project"
   route images had: without it the only way to attach a voice clip was to
   upload it again from the filesystem, so a clip already sitting in a
   project's refs/ could not be linked to a character at all. */
async function chooseMedia(title, kind) {
  const isAudio = kind === "audio";
  const modal = $("#picker");
  const grid = $("#pickerGrid");
  $("#pickerTitle").textContent = title;
  grid.classList.toggle("picker-list", isAudio);
  $("#pickerFoot").textContent = isAudio
    ? "Audio already in your projects. Uploading adds it to this project's refs/."
    : "Images already in your projects. Per-frame folders are excluded — use “chain from previous shot” for that.";
  grid.innerHTML = "";
  grid.appendChild(el("div", "empty-state", "loading…"));
  modal.hidden = false;

  return new Promise((resolve) => {
    const finish = (value) => {
      modal.hidden = true;
      grid.innerHTML = "";
      resolve(value);
    };

    $("#pickerClose").onclick = () => finish(null);
    modal.onclick = (e) => {
      if (e.target === modal) finish(null);
    };
    $("#pickerUpload").onclick = () => {
      const inp = el("input");
      inp.type = "file";
      inp.accept = isAudio
        ? "audio/wav,audio/mpeg,audio/mp4,audio/aac,audio/flac,audio/ogg,.wav,.mp3,.m4a,.aac,.flac,.ogg,.opus"
        : "image/png,image/jpeg,image/webp";
      inp.onchange = async () => {
        const f = inp.files[0];
        if (!f) return;
        try {
          finish(await API.uploadRef(state.slug, f));
        } catch (err) {
          toast(`Upload failed: ${err.message}`, "error");
          finish(null);
        }
      };
      inp.click();
    };

    API.library(kind)
      .then(({ items }) => {
        grid.innerHTML = "";
        if (!items.length) {
          grid.appendChild(
            el("div", "empty-state",
               isAudio
                 ? "No audio in your projects yet — upload a clip."
                 : "No images in your projects yet — upload one.")
          );
          return;
        }
        items.forEach((img) => {
          const card = el("div", isAudio ? "pick pick-audio" : "pick");
          if (isAudio) {
            const icon = el("div", "pick-audio-icon", "♪");
            card.appendChild(icon);
          } else {
            const im = el("img");
            im.src = img.url;
            im.loading = "lazy";
            im.alt = img.label;
            card.appendChild(im);
          }
          const meta = el("div", "pick-meta");
          meta.appendChild(el("div", "pick-name", img.label));
          meta.appendChild(el("div", "pick-project", img.project));
          card.appendChild(meta);
          if (isAudio) {
            // Audible before you commit to it: one clip of dialogue sounds
            // much like another in a filename.
            const au = el("audio");
            au.src = img.url;
            au.controls = true;
            au.preload = "none";
            au.className = "pick-audio-player";
            au.onclick = (e) => e.stopPropagation();
            card.appendChild(au);
          }
          card.onclick = async () => {
            // copy it into this project rather than pointing at another
            // project's folder, which would break if that project went away
            try {
              const ref = await API.adoptRef(state.slug, img.path);
              finish({ kind: "upload", ...ref });
            } catch (err) {
              toast(`Could not use that file: ${err.message}`, "error");
              finish(null);
            }
          };
          grid.appendChild(card);
        });
      })
      .catch((err) => {
        grid.innerHTML = "";
        grid.appendChild(
          el("div", "empty-state", `Could not list ${kind} files: ${err.message}`)
        );
      });
  });
}

async function pickRef(shot, key) {
  const chosen = await chooseImage(
    key === "startRef" ? "Choose a start frame" : "Choose an end frame"
  );
  if (!chosen) return;
  shot[key] = chosen;
  markDirty();
  await saveNow();
  render();
}

document.addEventListener("DOMContentLoaded", boot);
