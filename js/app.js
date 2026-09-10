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
  // The server's account of which renders no longer match the board:
  // {shots: {id: why}, final: why}. Kept beside the board rather than in it,
  // because the board object is round-tripped straight back on the next save
  // and anything added here would be persisted as if the user had written it.
  stale: { shots: {}, final: "" },
  poll: null,
  tab: "prompt",          // which editor tab is open; survives a re-render
  // The last speech run per shot, by shot id. Held for the session so the
  // log window fills the instant a take finishes, before anything is re-read
  // from disk.
  speechLog: {},
  saveTimer: null,
  // Set when we start a batch, cleared when its end has been reported. A
  // whole-board run with nothing to render still has work to do — it
  // assembles the cut — and can finish between two polls, so "was busy last
  // tick" is not enough to notice it ended.
  awaitingBatch: false,
  dirty: false,
  toast: null,
};

/* Mirrors RERUNNABLE in server/orchestrator.py: the statuses a whole-board
   run picks up. Kept here only to say in advance what that run will do — the
   server decides, and a shot whose inputs have changed is picked up too. */
const RERUNNABLE = ["draft", "failed", "blocked", "review", "interrupted"];

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
/* Why this shot's clip is not a render of what the board says now; "" when it
   is current, or when there is nothing rendered to be out of date. */
const staleWhy = (id) => (state.stale && state.stale.shots && state.stale.shots[id]) || "";
const finalWhy = () => (state.stale && state.stale.final) || "";
/* A shot whose spoken line never made it onto its clip; "" when it did, or
   when the shot has no dialogue. */
const dialogueWhy = (id) =>
  (state.stale && state.stale.dialogue && state.stale.dialogue[id]) || "";

function downloadName(...parts) {
  return parts
    .filter(Boolean)
    .join("-")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80) || "download";
}

function setSpeakAudioBusy(row, busy) {
  const au = row.querySelector && row.querySelector(".speak-player");
  const dl = row.querySelector && row.querySelector(".speak-download");
  if (au) {
    if (busy) {
      au.pause();
      au.controls = false;
      au.classList.add("busy");
      au.setAttribute("aria-disabled", "true");
    } else {
      au.controls = true;
      au.classList.remove("busy");
      au.removeAttribute("aria-disabled");
    }
  }
  if (dl) {
    dl.classList.toggle("disabled", !!busy);
    dl.setAttribute("aria-disabled", busy ? "true" : "false");
    dl.tabIndex = busy ? -1 : 0;
  }
}
const takeStale = (payload) => {
  if (payload && payload.stale) state.stale = payload.stale;
};

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
    const sent = state.board;
    const res = await API.saveBoard(state.slug, state.board);
    const board = res.board;
    // Every save re-answers "does the render still match?", so the badges
    // follow the words rather than waiting for the next render.
    const before = JSON.stringify(state.stale);
    takeStale(res);
    if (JSON.stringify(state.stale) !== before) paintStale();

    // Deliberately NOT `state.board = board`. The response is only the board
    // we just sent, migrated and re-stamped — but assigning it replaces every
    // shot object, orphaning the references the editor's handlers hold, so the
    // next keystroke would be written into a detached object and silently
    // dropped while the indicator still read "saved". It also discarded
    // anything typed while the request was in flight. Keep our own object and
    // take only what the server actually decided.
    if (state.board === sent) {
      state.board.updatedAt = board.updatedAt;
      state.dirty = false;
      $("#saveState").textContent = "saved";
    } else {
      // The board was swapped out under us (opened another one, renamed).
      // That save landed; this one is no longer ours to report on.
      $("#saveState").textContent = state.dirty ? "unsaved" : "saved";
    }
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

/* What a finished batch actually produced.

   "Render batch finished." was true and useless. The run that prompted this
   rendered one shot of three — the other two were marked done, so they were
   skipped even though their prompts had been rewritten — and built no video
   at all, while reporting success. A summary has to say what came out. */
function batchOutcome(status) {
  const runs = Object.values((status && status.runs) || {});
  const n = (st) => runs.filter((r) => r.status === st).length;
  // A whole-board run with nothing stale still has work to do — it assembles
  // the cut — so "0 shots" is a real and unhelpful thing to announce.
  const bits = runs.length ? [`${runs.length} shot(s)`] : ["nothing needed re-rendering"];
  if (n("failed")) bits.push(`${n("failed")} failed`);
  if (n("blocked")) bits.push(`${n("blocked")} blocked`);
  if (n("review")) bits.push(`${n("review")} need review`);
  if (n("interrupted")) bits.push(`${n("interrupted")} interrupted`);

  const a = status && status.assembly;
  let kind = n("failed") || n("blocked") ? "error" : "info";
  if (!a) {
    return { msg: `Render finished — ${bits.join(", ")}.`, kind };
  }
  if (a.state === "done") {
    bits.push(`final video assembled (${a.message})`);
  } else if (a.state === "partial") {
    bits.push(`final video assembled but incomplete — ${a.message}`);
    kind = "warn";
  } else {
    bits.push(`final video NOT assembled — ${a.message}`);
    kind = "error";
  }
  return { msg: `Render finished — ${bits.join(", ")}.`, kind };
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
  const res = await API.getBoard(slug);
  setBoard(slug, res.board, res.stale);
}

function setBoard(slug, board, stale) {
  state.slug = slug;
  state.board = board;
  // Belongs to the board being installed, so switching boards cannot leave
  // the previous one's "changed since render" marks on screen.
  state.stale = stale || { shots: {}, final: "" };
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
      state.status = await API.render(state.slug, undefined, state.board);
      state.awaitingBatch = true;
      startPolling();
      render();
    } catch (err) {
      toast(err.message, "error");
    }
  });

  $("#btnAssemble").addEventListener("click", async () => {
    const btn = $("#btnAssemble");
    btn.disabled = true;
    const was = btn.textContent;
    btn.textContent = "assembling…";
    try {
      const res = await API.assemble(state.slug);
      takeStale(res);
      state.board.finalVideo = res.finalVideo;
      const f = res.finalVideo;
      toast(
        f.partial
          ? `Assembled ${f.parts.length} clip(s), but ${f.missing.length} shot(s) are not rendered.`
          : `Assembled ${f.parts.length} clip(s) — ${f.seconds}s.`,
        f.partial ? "warn" : "info"
      );
      render();
    } catch (err) {
      toast(`Could not assemble: ${err.message}`, "error");
    } finally {
      btn.textContent = was;
      btn.disabled = false;
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
  $("#soundscapeInShots").addEventListener("change", (e) => {
    state.board.soundscapeInShots = e.target.checked;
    markDirty();
    render();
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

    if (!s.busy && (wasBusy || state.awaitingBatch)) {
      state.awaitingBatch = false;
      stopPolling();
      // the server has been mutating the board as shots finish; re-read it
      const res = await API.getBoard(state.slug);
      takeStale(res);
      state.board = res.board;
      const done = batchOutcome(s);
      toast(done.msg, done.kind);
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
  const a = state.status && state.status.assembly;
  return JSON.stringify([
    busy,
    state.selectedId,
    // The assembly pass runs after the last shot, so its state moves while
    // nothing else does — without it the final panel would sit on "Joining
    // the clips" until something unrelated forced a rebuild.
    a ? [a.state, a.message, a.url] : null,
    (state.board.finalVideo || {}).url || "",
    finalWhy(),
    shots().map((raw) => {
      const v = view(raw);
      return [
        raw.id,
        v.status,
        raw.thumb || "",
        raw.dubUrl || "",
        raw.renderedAs || "",
        staleWhy(raw.id),
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
  $("#btnAssemble").disabled = busy;
  $("#btnStop").disabled = !busy;
  paintMeta();
  paintRenderHint();

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
  // Appends into the render container, not the whole window: the same window
  // also holds the spoken line's log, and counting those lines as its own
  // would make it skip real output.
  const part = box.querySelector('[data-part="render"]') || box;

  // Only ever append: rewriting would fight the user's scroll and flash a few
  // hundred lines of text once a second.
  const have = Number(part.dataset.count || 0);
  if (lines.length <= have) return;

  // It was showing "No run yet." or a saved log; this run supersedes it.
  if (!have) part.innerHTML = "";

  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
  lines.slice(have).forEach((e) => part.appendChild(logLine(e)));
  part.dataset.count = String(lines.length);

  // Hold the same window renderPreview draws, so the two agree.
  let extra = part.querySelectorAll(".log-line").length - LOG_WINDOW;
  while (extra-- > 0 && part.firstChild) part.removeChild(part.firstChild);

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
  $("#btnAssemble").disabled = busy;
  $("#btnStop").disabled = !busy;

  paintMeta();
  paintRenderHint();
  focusRestore(snap);
}

/* What "Render all" will actually do.

   It never re-renders a shot that is already a current render — that is the
   right rule, since a re-render costs half an hour, but it was also an
   invisible one, and an invisible skip is how a batch reported success having
   rendered one shot of three. So the count is stated before the click. */
function pendingShots() {
  return shots().filter(
    (raw) => RERUNNABLE.includes(view(raw).status) || !!staleWhy(raw.id)
  );
}

function paintRenderHint() {
  const pending = pendingShots();
  const changed = pending.filter((raw) => !!staleWhy(raw.id)).length;
  $("#btnRender").title = pending.length
    ? `Renders ${pending.length} of ${shots().length} shot(s)` +
      (changed
        ? `, ${changed} of them because the board changed since they were rendered`
        : "") +
      ", then joins every clip into the final video. A shot already rendered " +
      "from what the board says now is left alone."
    : "Every shot is already a render of what the board says now — this just " +
      "joins the clips into the final video.";
}

function paintMeta() {
  const counts = shots().reduce((a, s) => {
    const st = view(s).status;
    a[st] = (a[st] || 0) + 1;
    return a;
  }, {});
  const pending = pendingShots().length;
  const changed = shots().filter((raw) => !!staleWhy(raw.id)).length;
  $("#projectTitle").textContent = state.board.name;
  $("#projectMeta").textContent =
    `${shots().length} shots` +
    (Object.keys(counts).length
      ? " · " +
        Object.entries(counts)
          .map(([k, v]) => `${v} ${(STATUS_LABELS[k] || k).toLowerCase()}`)
          .join(", ")
      : "") +
    (changed ? ` · ${changed} changed since rendered` : "") +
    (pending ? ` · ${pending} to render` : "");
}

/* --- rail ---------------------------------------------------------------- */

function renderRail() {
  const sd = $("#sceneDescription");
  if (document.activeElement !== sd) sd.value = state.board.sceneDescription || "";
  const snd = $("#soundscape");
  if (document.activeElement !== snd) snd.value = state.board.soundscape || "";
  const sndMode = $("#soundscapeInShots");
  sndMode.checked = state.board.soundscapeInShots !== false;
  sndMode.title = sndMode.checked
    ? "The background sound text is included in every shot render."
    : "The background sound text is kept for the final mix/add-later stage; only per-shot sound accents render now.";

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

  const queueShots = shots();
  const queueBusy = !!(state.status && state.status.busy);
  const running = queueShots.filter((raw) => view(raw).status === "running").length;
  const queued = queueShots.filter((raw) => view(raw).status === "queued").length;
  const changed = queueShots.filter((raw) => !!staleWhy(raw.id)).length;
  const failed = queueShots.filter((raw) =>
    ["failed", "blocked", "review", "interrupted"].includes(view(raw).status)
  ).length;
  const summary = [`${queueShots.length} shot(s)`];
  if (running) summary.push(`${running} running`);
  if (queued) summary.push(`${queued} queued`);
  if (changed) summary.push(`${changed} changed`);
  if (failed) summary.push(`${failed} need attention`);
  $("#queueSummary").textContent = summary.join(" · ");
  if (queueBusy || failed > 0) $("#queueBox").open = true;

  const q = $("#queueList");
  q.innerHTML = "";
  queueShots.forEach((raw, i) => {
    const shot = view(raw);
    const row = el("div", "queue-row");
    row.dataset.id = raw.id;
    if (raw.id === state.selectedId) row.classList.add("selected");
    row.appendChild(el("span", "queue-num", String(i + 1)));
    const dot = el("span", "queue-dot");
    dot.dataset.status = shot.status;
    row.appendChild(dot);
    row.appendChild(el("span", "queue-name", raw.title));
    const why = staleWhy(raw.id);
    if (why && shot.status !== "running") {
      const m = el("span", "queue-stale", "●");
      m.title = `Changed since it was rendered — ${why}. “Render all” will re-run it.`;
      row.appendChild(m);
    }
    if (shot.status === "running") {
      row.appendChild(el("span", "queue-pct", `${Math.round(shot.progress)}%`));
    }
    row.addEventListener("click", () => {
      state.selectedId = raw.id;
      render();
    });
    q.appendChild(row);
  });

  renderFinal();
}

/* --- the assembled cut --------------------------------------------------- */

/* The board-level artefact, so it sits with the other board-level things
   rather than in the per-shot preview. Its whole job is to be visibly absent
   when it has not been built: a folder of clips and no video is the failure
   this panel exists to make obvious. */
function renderFinal() {
  const host = $("#finalVideo");
  const prev = host.querySelector("video");
  if (prev) prev.remove();     // detached, so innerHTML does not destroy it
  host.innerHTML = "";
  const f = state.board.finalVideo;
  const live = state.status && state.status.assembly;
  const busy = !!(state.status && state.status.busy);
  const why = finalWhy();

  if (live && live.state === "running") {
    host.appendChild(el("div", "final-empty", `Joining the clips — ${live.message}`));
    return;
  }
  if (live && live.state === "failed") {
    host.appendChild(el("div", "final-err", `Not assembled — ${live.message}`));
  }

  if (f && f.url) {
    // Carried across rather than rebuilt when the source has not moved: this
    // panel is repainted on every save that changes a badge, and a fresh
    // <video> reloads the file and drops the playhead each time.
    const v = prev && prev.getAttribute("src") === f.url ? prev : el("video");
    v.src = f.url;
    v.controls = true;
    v.preload = "metadata";
    host.appendChild(v);
    host.appendChild(
      el("div", "final-meta",
         `${f.parts.length} clip(s) · ${dur(f.seconds)} · ${f.url.split("/").pop()}`)
    );
    if (f.missing && f.missing.length) {
      host.appendChild(
        el("div", "final-warn",
           `Incomplete — not rendered: ${f.missing.join(", ")}`)
      );
    }
    if (why) host.appendChild(el("div", "final-warn", `Out of date — ${why}.`));
  }

  const silent = shots().filter((raw) => !!dialogueWhy(raw.id));
  if (silent.length) {
    host.appendChild(
      el("div", "final-warn",
         `${silent.length} shot(s) have a spoken line that is not on the clip, ` +
         `so it is missing from the cut: ${silent.map((r) => r.title).join(", ")}.`)
    );
  }

  if (!(f && f.url)) {
    host.appendChild(
      el("div", "final-empty",
         "Not built yet. “Render all” assembles it after the last shot, or " +
         "press Assemble to join whatever is already rendered.")
    );
  }

  const acts = el("div", "final-actions");
  const btn = el("button", "btn btn-sm", f && f.url ? "Re-assemble" : "Assemble now");
  btn.disabled = busy;
  if (busy) btn.title = "A render is running — the clips are still being written.";
  btn.addEventListener("click", () => $("#btnAssemble").click());
  acts.appendChild(btn);
  if (f && f.url) {
    const name = decodeURIComponent(f.url.split("/").pop() || "final.mp4");
    const download = el("a", "btn btn-sm btn-primary", "Download");
    download.href = f.url;
    download.download = name;
    download.title = "Download the assembled video";
    acts.appendChild(download);

    const open = el("a", "btn btn-sm btn-ghost", "Open");
    open.href = f.url;
    open.target = "_blank";
    acts.appendChild(open);
  }
  host.appendChild(acts);
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
  /* Cloning and transcription are separate capabilities: the board's speech
     engine may clone a voice and have no speech recognition at all. So the
     button reports on whichever configured service can transcribe, not on the
     selected one. */
  const transcriber = (state.tts || []).find(
    (e) => e.supportsTranscription && e.healthy
  );
  const trBtn = $("#castTranscribe");
  trBtn.onclick = () => autoTranscribe(draft);
  trBtn.disabled = !(draft.voice && draft.voice.path) || !transcriber;
  trBtn.title = !transcriber
    ? "No configured speech service can transcribe. Type the transcript instead."
    : `Transcribe with ${transcriber.label}`;
  const descBtn = $("#castDescribe");
  const descProposal = $("#castDescProposal");
  descProposal.innerHTML = "";
  descBtn.onclick = () => improveDescription(draft);
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
    const llm = currentLLM();
    descBtn.disabled = !(draft.image && draft.image.path) || !llm || !llm.healthy;
    descBtn.title = !(draft.image && draft.image.path)
      ? "Add a reference image first."
      : !llm || !llm.healthy
        ? "No prompt rewriting service is available."
        : `Improve from the reference image with ${llm.label}`;
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
        if (key === "image") descProposal.innerHTML = "";
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
      $("#castTranscribe").disabled = !transcriber;
    } else {
      descProposal.innerHTML = "";
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
      const via = r.engine && r.engine !== state.board.defaults.tts
        ? ` via ${r.engine}`
        : "";
      toast(`Transcribed the reference clip${via} — check it reads correctly.`);
    } catch (err) {
      field.value = prev;
      castError(
        `Could not transcribe: ${err.message} — type what the clip says instead.`
      );
    } finally {
      field.disabled = false;
    }
  }

  async function improveDescription(obj) {
    const image = obj.image && obj.image.path;
    if (!image) return;
    const llm = currentLLM();
    const field = $("#castDesc");
    descProposal.innerHTML = "";
    descBtn.disabled = true;
    descBtn.classList.add("busy");
    const label = descBtn.lastChild;
    label.textContent = "AI…";
    try {
      const r = await API.describeCharacter(
        image,
        $("#castName").value.trim(),
        field.value.trim(),
        llm && llm.id
      );
      descProposal.appendChild(characterDescProposal(r.text, field, descProposal));
      toast(`Character description proposed by ${r.service}.`);
    } catch (err) {
      castError(`Could not describe the image: ${err.message}`);
    } finally {
      descBtn.classList.remove("busy");
      label.textContent = "AI";
      paint();
    }
  }

  function characterDescProposal(text, field, slot) {
    const box = el("div", "proposal");
    const head = el("div", "proposal-head");
    head.appendChild(el("strong", null, "Proposed description"));
    box.appendChild(head);
    box.appendChild(el("div", "proposal-body", text));

    const acts = el("div", "proposal-acts");
    const use = el("button", "btn btn-sm btn-primary", "Use this");
    use.addEventListener("click", () => {
      field.value = text;
      draft.description = text;
      slot.innerHTML = "";
    });
    const drop = el("button", "btn btn-sm btn-ghost", "Discard");
    drop.addEventListener("click", () => (slot.innerHTML = ""));
    acts.append(use, drop);
    box.appendChild(acts);
    return box;
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
    const staleReason = staleWhy(raw.id);
    if (staleReason && shot.status !== "running") {
      // Not a status: the clip is real and plays. It is just not a clip of
      // what the board says now, which is exactly the thing that shipped a
      // finished-looking board built from replaced prompts.
      const b = el("span", "stale-badge", "CHANGED");
      b.title = staleReason;
      thumb.appendChild(b);
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

  /* Handlers below fire long after this function returns, and `raw` is a
     reference into the board array as it was at build time. Several paths
     legitimately replace state.board — opening another board, renaming it,
     reloading it when a render batch finishes — and an edit written through a
     stale reference lands in an orphaned object and is lost silently, while
     the indicator still reads "saved". So mutations resolve the shot by id at
     the moment they happen, not when the editor was built. */
  const live = () => shotById(raw.id) || raw;

  const head = el("div", "editor-head");
  const h = el("div", "section-label", `Shot ${idx + 1}`);
  h.style.margin = "0";
  head.append(h, chip(shot.status));
  const sp = el("div");
  sp.style.flex = "1";
  head.appendChild(sp);

  /* Speaking a line is a preview, not a post-production step: it uses the
     speaking character's own reference clip and transcript, and it works
     before the shot has been rendered. Finding out half an hour later that a
     cloned voice reads the line wrong is exactly the wrong order. */
  if ((raw.dialogue || "").trim()) {
    const dubRow = el("div", "speak-row");
    const engNow = state.tts.find((e) => e.id === (state.board.defaults.tts || "none"));

    // who says it — inferred when the shot has one character, chosen when more
    const castHere = (state.board.characters || []).filter((c) =>
      (raw.characterIds || []).includes(c.id)
    );
    const inferred =
      castHere.find((c) => c.voice && c.voice.path) || castHere[0] || null;
    const speaker =
      castHere.find((c) => c.id === raw.speakerId) || inferred;

    if (castHere.length > 1) {
      const pick = select(
        castHere.map((c) => [
          c.id,
          c.voice && c.voice.path ? `${c.name} (cloned voice)` : `${c.name} (no clip)`,
        ]),
        (speaker && speaker.id) || "",
        (v) => {
          live().speakerId = v;
          markDirty();
          renderEditor();
        }
      );
      dubRow.append(el("span", "field-note", "spoken by"), pick);
    } else if (speaker) {
      dubRow.appendChild(
        el("span", "field-note",
           speaker.voice && speaker.voice.path
             ? `in ${speaker.name}’s cloned voice`
             : `as ${speaker.name} — no voice clip, so the engine's own voice`)
      );
    }

    const canClone = !!(engNow && engNow.supportsCloning);
    const willClone = !!(speaker && speaker.voice && speaker.voice.path && canClone);

    const dubBtn = el("button", "btn btn-sm", "Generate");
    dubBtn.disabled = !engNow || !engNow.healthy;
    dubBtn.title = !engNow || !engNow.healthy
      ? (engNow && engNow.message) || "No speech engine selected — see ⚙ Settings"
      : willClone
      ? `Synthesise with ${engNow.label}, cloning ${speaker.name}’s voice`
      : `Synthesise with ${engNow.label}`;

    dubBtn.onclick = async () => {
      dubBtn.disabled = true;
      dubBtn.textContent = "generating…";
      setSpeakAudioBusy(dubRow, true);
      try {
        // send the line as typed; it may not be saved yet
        const sh = live();
        const r = await API.dub(state.slug, raw.id, sh.dialogue, sh.dialogueStyle);
        sh.dialogueAudioUrl = r.audioUrl;
        if (r.dubUrl) sh.dubUrl = r.dubUrl;
        if (r.speechLogUrl) sh.speechLogUrl = r.speechLogUrl;
        if (r.log && r.log.length) state.speechLog[sh.id] = r.log;
        if (r.note) toast(r.note, "warn");
        else if (r.warning) toast(r.warning, "warn");
        else {
          toast(
            `Spoke ${r.seconds}s in ${r.cloned ? `${r.speaker}’s cloned voice` : "the engine voice"}` +
              (r.muxed ? " and mixed it over the clip." : " — the clip is not rendered yet, so nothing was mixed.")
          );
        }
        render();
      } catch (err) {
        // The engine's own log is the useful part of a speech failure, and the
        // error body carries it — a toast alone would throw it away.
        if (err.payload && err.payload.log && err.payload.log.length) {
          state.speechLog[raw.id] = err.payload.log;
        }
        toast(`Could not speak this line: ${err.message}`, "error");
        render();
      }
    };
    dubRow.appendChild(dubBtn);

    if (speaker && speaker.voice && speaker.voice.path && !canClone) {
      dubRow.appendChild(
        el("span", "field-warn",
           `${engNow ? engNow.label : "this engine"} cannot clone — the clip will not be used`)
      );
    }
    if (engNow && !engNow.healthy) {
      dubRow.appendChild(el("span", "field-warn", "speech engine unavailable"));
    }

    // the spoken line, playable right here
    if (raw.dialogueAudioUrl) {
      const au = el("audio", "speak-player");
      au.src = raw.dialogueAudioUrl;
      au.controls = true;
      au.preload = "none";
      dubRow.appendChild(au);
      const dl = el("a", "btn btn-sm speak-download", "Download");
      dl.href = raw.dialogueAudioUrl;
      dl.download = `${downloadName(state.slug, raw.title || "shot", "dialogue")}.wav`;
      dl.title = "Download this generated dialogue take";
      dubRow.appendChild(dl);
    }
    if (raw.dubUrl) {
      dubRow.appendChild(el("span", "field-note", "mixed over the clip — see Output"));
    }
    panelDubRow = dubRow;
  }

  const one = el("button", "btn btn-sm", "Render this shot");
  one.disabled = !!(state.status && state.status.busy);
  one.addEventListener("click", async () => {
    await saveNow();
    try {
      state.status = await API.render(state.slug, [raw.id], state.board);
      state.awaitingBatch = true;
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
      live().title = v;
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
    live().prompt = ta.value;
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
      "Used as a visual cue for mouth movement, then synthesised by the " +
      "speech engine and muxed over the finished clip.";
    dlg.addEventListener("input", () => {
      live().dialogue = dlg.value;
      markDirty();
      updateResolvedPreview();
      syncTabDots();
    });
    dlg.dataset.fkey = "dialogue";
    const style = el("textarea", "ta-compact");
    style.value = raw.dialogueStyle || "";
    style.placeholder =
      "Voice direction for the cloned take, not spoken aloud.\n" +
      "Example: tired, quiet, breathy, slight smile, urgent whisper.\n" +
      "For Qwen-style TTS you can paste a full [STYLE / VOICE DIRECTION] block.";
    style.addEventListener("input", () => {
      live().dialogueStyle = style.value;
      markDirty();
    });
    style.dataset.fkey = "dialogue-style";
    const dp = el("div");
    dp.append(
      paneHint("drives mouth movement; the final voice is mixed in separately"),
      dlg,
      paneHint("voice direction — sent to compatible speech engines, not spoken aloud"),
      style
    );
    // Speaking the line belongs with the line. It used to hang off the panel
    // below the tab strip, which put a speech-generation button and an audio
    // player under the Prompt, Sound accents and Resolved tabs as well —
    // controls for something none of those tabs is about.
    if (panelDubRow) {
      dp.appendChild(panelDubRow);
      panelDubRow = null;
    }
    pane("dialogue", dp);

    const sa = el("textarea", "ta-tall");
    sa.value = raw.soundNote || "";
    sa.placeholder =
      "Sound accents for THIS clip — what happens sonically here.\n" +
      "The project background sound is controlled by the rail switch.";
    sa.addEventListener("input", () => {
      live().soundNote = sa.value;
      markDirty();
      updateResolvedPreview();
      syncTabDots();
    });
    sa.dataset.fkey = "sound-accents";
    const sp2 = el("div");
    sp2.append(
      paneHint("this clip only — independent of the project background sound mode"),
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
  // Only reachable when the shot carries a line but its model has no audio,
  // so there is no Dialogue tab to hold it — usually a line left behind by a
  // model switch. Shown rather than dropped, since a line you cannot see is
  // one you cannot delete.
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
        live().characterIds = [...ids];
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
        live().startRef = cb.checked
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
    const refPanel = el("div", "panel");
    refPanel.style.marginTop = "var(--sp-3)";
    refPanel.appendChild(el("div", "section-label", "Reference images"));

    const slots = el("div", "ref-slots");
    slots.appendChild(
      refSlot(
        "Shot reference image",
        raw,
        "startRef",
        "Choose a shot reference image"
      )
    );
    refPanel.appendChild(slots);

    refPanel.appendChild(
      el(
        "div",
        "hint-body",
        `${cap.label.split("—")[0].trim()} uses reference images rather than ` +
          `start/end frame anchors. Character portraits and project style ` +
          `references are included automatically; this shot image is added as ` +
          `an extra subject/style reference for this clip.`
      )
    );
    host.appendChild(refPanel);
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
          const sh = live();
          sh.model = v;
          const c = modelCap(v);
          if (c) {
            if (!c.resolutions.includes(sh.resolution))
              sh.resolution = c.resolutions[0];
            sh.frames = c.frameRule.minimum > sh.frames
              ? c.frameRule.minimum
              : sh.frames;
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
            live().frames = Number(v);
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
        live().steps = Number(v);
        markDirty();
        renderStrip();
      })
    )
  );
  params.appendChild(row);

  const row2 = el("div", "field-row");
  row2.appendChild(
    field("Seed", input("number", raw.seed, (v) => {
      live().seed = Number(v);
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
      if (d) push(`cast · ${n || "unnamed"}`, characterDescription(n, d));
    });

  push("shot", raw.prompt);

  if (!cap || cap.supportsAudio) {
    push("dialogue movement", dialogueVisualCue(raw));
    if (state.board.soundscapeInShots !== false) {
      push("sound bed", state.board.soundscape);   // project-wide
    } else if ((state.board.soundscape || "").trim()) {
      push("sound bed", "(held for final mix; not sent to this shot render)");
    }
    push("accents", raw.soundNote);              // this clip only
    if ((raw.dialogue || "").trim()) {
      push(
        "speech guard",
        "Generated audio contains ambient sound only: no spoken words, no voice, " +
          "and no intelligible dialogue; the voice line is dubbed separately."
      );
    }
  }
  return out;
}

function dialogueVisualCue(raw) {
  const line = (raw.dialogue || "").trim();
  if (!line) return "";
  return `${speakerName(raw)} speaks the line with natural jaw and lip movement: "${line}"`;
}

function speakerName(raw) {
  const cast = (state.board.characters || []).filter((c) =>
    (raw.characterIds || []).includes(c.id)
  );
  if (raw.speakerId) {
    const explicit = (state.board.characters || []).find((c) => c.id === raw.speakerId);
    if (explicit && (explicit.name || "").trim()) return explicit.name.trim();
  }
  const first = cast.find((c) => (c.name || "").trim());
  return first ? first.name.trim() : "The visible character";
}

/** Exactly what the backend will assemble, so the preview cannot drift. */
function resolvedPromptText(raw) {
  const joined = resolvedParts(raw)
    .map(({ text }) => sentenceText(text))
    .join(" ");
  return joined || "(empty)";
}

function sentenceText(text) {
  text = (text || "").trim();
  if (!text) return "";
  if (".!?;:,".includes(text.slice(-1))) return text;
  if (text.length >= 2 && "\"”'".includes(text.slice(-1)) &&
      ".!?;:,".includes(text.slice(-2, -1))) {
    return text;
  }
  return text + ".";
}

function characterDescription(name, desc) {
  name = (name || "").trim();
  desc = (desc || "").trim();
  if (!desc) return "";
  if (name && desc.toLowerCase().startsWith(name.toLowerCase() + ":")) {
    return desc;
  }
  return name ? `${name}: ${desc}` : desc;
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

  staleNotes(raw, shot.status).forEach((n) => host.appendChild(n));

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
  lbl.appendChild(el("span", "hint", "— the render, and any spoken line"));
  host.appendChild(lbl);

  /* One window, two producers. They go into separate containers inside it so
     each can be filled independently — the render log arrives live or as a
     fetch of run.log, the speech log the same way — while the order on screen
     stays fixed and the render poll's append (which counts lines) only ever
     touches its own. */
  const box = el("div", "log");
  const renderPart = el("div", "log-part");
  renderPart.dataset.part = "render";
  const speechPart = el("div", "log-part");
  speechPart.dataset.part = "speech";
  box.append(renderPart, speechPart);

  const speech = state.speechLog[raw.id];
  const haveSpeech = !!(speech || raw.speechLogUrl);

  const lines = shot.log;
  if (!lines || !lines.length) {
    // No live log — the batch may have finished in an earlier server session,
    // so fall back to the run.log written next to the outputs.
    if (raw.logUrl) {
      renderPart.appendChild(el("span", "log-empty", "loading saved log…"));
      fetch(raw.logUrl)
        .then((r) => (r.ok ? r.text() : Promise.reject()))
        .then((text) => {
          renderPart.innerHTML = "";
          text.trimEnd().split("\n").slice(-LOG_WINDOW)
            .forEach((t) => renderPart.appendChild(logLine(t)));
          box.scrollTop = box.scrollHeight;
        })
        .catch(() => {
          renderPart.innerHTML = "";
          renderPart.appendChild(el("span", "log-empty", "No saved log for this run."));
        });
    } else if (!haveSpeech) {
      renderPart.appendChild(el("span", "log-empty", "No run yet."));
    } else {
      // Speech but no render: say so quietly rather than "No run yet." above
      // output that plainly exists.
      renderPart.appendChild(logLine("# not rendered yet"));
    }
  } else {
    lines.slice(-LOG_WINDOW).forEach((e) => renderPart.appendChild(logLine(e)));
    // paintLog appends from here rather than rebuilding, so it needs to know
    // how much of the log is already on screen.
    renderPart.dataset.count = String(lines.length);
  }

  if (haveSpeech) {
    if (speech) {
      // A session run has no saved header, so it gets one: which process this
      // came from should be readable without inferring it from content.
      speechPart.appendChild(logLine("# spoken line"));
      speech.slice(-LOG_WINDOW).forEach((t) => speechPart.appendChild(logLine(t)));
    } else {
      speechPart.appendChild(el("span", "log-empty", "loading speech log…"));
      fetch(raw.speechLogUrl)
        .then((r) => (r.ok ? r.text() : Promise.reject()))
        .then((text) => {
          speechPart.innerHTML = "";
          text.trimEnd().split("\n").slice(-LOG_WINDOW)
            .forEach((t) => speechPart.appendChild(logLine(t)));
          box.scrollTop = box.scrollHeight;
        })
        .catch(() => {
          speechPart.innerHTML = "";
          speechPart.appendChild(el("span", "log-empty", "No saved speech log."));
        });
    }
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
    const raw = String(entry);
    // A saved log's "# ..." header is metadata, not a log line at INFO; it was
    // being stamped [INFO] and reading as though the engine had said it.
    if (raw.startsWith("#")) {
      const line = el("div", "log-line");
      line.dataset.lvl = "META";
      line.appendChild(el("span", null, raw.replace(/^#\s*/, "")));
      return line;
    }
    const m = raw.match(/^\[([A-Z]+)\]\s*(.*)$/);
    level = m ? m[1] : "INFO";
    text = m ? m[2] : raw;
  }
  const line = el("div", "log-line");
  line.dataset.lvl = level;
  line.appendChild(el("span", "lvl", `[${level}] `));
  line.appendChild(el("span", null, text));
  return line;
}

/* The two ways a finished clip can be wrong without being broken: it is not a
   render of what the board says now, or the line someone speaks in it never
   got mixed in. Built here rather than inline so the save path can put them
   on screen without rebuilding the pane and taking the caret with it. */
function staleNotes(raw, status) {
  const out = [];

  const why = staleWhy(raw.id);
  if (why && status !== "running") {
    const note = el("div", "stale-note");
    note.dataset.note = "stale";
    note.append(
      el("strong", null, "This clip is out of date. "),
      el("span", null, `${why}. It will be re-rendered by “Render all”.`)
    );
    // The judgement is sometimes the user's: a board that predates change
    // tracking cannot be *shown* to match, but they may know that it does,
    // and half an hour of GPU time to prove it is a poor trade. That is an
    // assertion, so it is theirs to make rather than ours to infer.
    const keep = el("button", "btn btn-sm", "Keep this take");
    keep.title =
      "Records this clip as a render of what the board says now, without " +
      "re-rendering it. Use it when you know the clip is current.";
    keep.style.marginTop = "var(--sp-2)";
    keep.addEventListener("click", async () => {
      keep.disabled = true;
      try {
        const r = await API.accept(state.slug, raw.id);
        takeStale(r);
        const live = shotById(raw.id);
        if (live) live.renderFingerprint = r.renderFingerprint;
        render();
      } catch (err) {
        toast(err.message, "error");
        keep.disabled = false;
      }
    });
    note.appendChild(el("div")).appendChild(keep);
    out.push(note);
  }

  const dWhy = dialogueWhy(raw.id);
  if (dWhy) {
    const note = el("div", "stale-note");
    note.dataset.note = "dialogue";
    note.append(
      el("strong", null, "The spoken line is not on this clip. "),
      el("span", null, `It was ${dWhy}.`)
    );
    out.push(note);
  }
  return out;
}

/* Staleness moves on every save — you rewrite a prompt and the clip you have
   stops matching it — and a save lands 700ms after you stop typing, while the
   caret is still in the box. So the marks are painted in place rather than by
   rebuilding: a full render() here would destroy the field being edited. */
function paintStale() {
  if (!state.board) return;
  shots().forEach((raw) => {
    const status = view(raw).status;
    const why = status === "running" ? "" : staleWhy(raw.id);

    const thumb = document.querySelector(`.shot-card[data-id="${raw.id}"] .shot-thumb`);
    if (thumb) {
      const have = thumb.querySelector(".stale-badge");
      if (why && !have) {
        const b = el("span", "stale-badge", "CHANGED");
        b.title = why;
        thumb.appendChild(b);
      } else if (why && have) {
        have.title = why;
      } else if (!why && have) {
        have.remove();
      }
    }

    const row = document.querySelector(`.queue-row[data-id="${raw.id}"]`);
    if (row) {
      const have = row.querySelector(".queue-stale");
      if (why && !have) {
        const m = el("span", "queue-stale", "●");
        m.title = `Changed since it was rendered — ${why}. “Render all” will re-run it.`;
        row.insertBefore(m, row.querySelector(".queue-pct"));
      } else if (!why && have) {
        have.remove();
      }
    }
  });

  const host = $("#preview");
  const stage = host.querySelector(".preview-stage");
  const raw = selectedShot();
  host.querySelectorAll(".stale-note").forEach((n) => n.remove());
  if (raw && stage) {
    const notes = staleNotes(raw, view(raw).status);
    let after = stage;
    notes.forEach((n) => {
      after.after(n);
      after = n;
    });
  }

  renderFinal();
  paintMeta();
  paintRenderHint();
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
      state.status = await API.render(state.slug, [raw.id], state.board);
      state.awaitingBatch = true;
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
      // by id, not through the captured object: state.board may have been
      // replaced since this button was built
      (shotById(raw.id) || raw).status = "done";
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
    (shotById(raw.id) || raw).prompt = r.text;
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

function refSlot(label, shot, key, pickerTitle = null) {
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
    slot.addEventListener("click", () => pickRef(shot, key, pickerTitle));
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

function groupedImageItems(items) {
  const groups = new Map();
  for (const item of items) {
    const key = item.digest || `${item.bytes || ""}:${item.label || ""}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }

  return [...groups.values()].map((group) => {
    const own = group.find((item) => item.path && item.path.includes(`projects/${state.slug}/`));
    const used = group.find((item) => item.used);
    const newest = group.reduce((best, item) =>
      (item.modifiedAt || 0) > (best.modifiedAt || 0) ? item : best
    );
    const main = own || used || newest;
    const uses = [...new Set(group.flatMap((item) => item.uses || []))];
    const projects = [...new Set(group.map((item) => item.project).filter(Boolean))];
    return {
      ...main,
      used: group.some((item) => item.used),
      uses,
      duplicates: group.length,
      project: projects.length > 1
        ? `${projects[0]} +${projects.length - 1}`
        : main.project,
      duplicatePaths: group.map((item) => item.path),
      unusedDuplicatePaths: group
        .filter((item) => !item.used)
        .map((item) => item.path),
    };
  });
}

function usageText(item) {
  const uses = item.uses || [];
  if (!uses.length) return "";
  if (uses.length <= 2) return uses.join(" · ");
  return `${uses.slice(0, 2).join(" · ")} · +${uses.length - 2} more`;
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
        const shown = isAudio ? items : groupedImageItems(items);
        if (!shown.length) {
          grid.appendChild(
            el("div", "empty-state",
               isAudio
                 ? "No audio in your projects yet — upload a clip."
                 : "No images in your projects yet — upload one.")
          );
          return;
        }
        shown.forEach((img) => {
          const card = el("div", isAudio ? "pick pick-audio" : "pick");
          if (!isAudio && img.used) {
            card.classList.add("used");
            card.title = usageText(img) || "Used by a storyboard";
          }
          if (isAudio) {
            const icon = el("div", "pick-audio-icon", "♪");
            card.appendChild(icon);
          } else {
            const im = el("img");
            im.src = img.url;
            im.loading = "lazy";
            im.alt = img.label;
            card.appendChild(im);
            if (img.unusedDuplicatePaths && img.unusedDuplicatePaths.length) {
              const del = el("button", "pick-delete", "🗑");
              del.title = img.unusedDuplicatePaths.length > 1
                ? `Delete ${img.unusedDuplicatePaths.length} unused duplicate images`
                : "Delete unused image";
              del.addEventListener("click", async (e) => {
                e.preventDefault();
                e.stopPropagation();
                const count = img.unusedDuplicatePaths.length;
                const name = count > 1 ? `${count} unused copies of ${img.label}` : img.label;
                if (!confirm(`Delete ${name}?`)) return;
                del.disabled = true;
                try {
                  await Promise.all(
                    img.unusedDuplicatePaths.map((path) =>
                      API.deleteLibraryItem("image", path)
                    )
                  );
                  if (img.used) {
                    img.unusedDuplicatePaths = [];
                    del.remove();
                  } else {
                    card.remove();
                  }
                  toast(`Deleted ${name}.`);
                  if (!grid.querySelector(".pick")) {
                    grid.appendChild(el("div", "empty-state", "No images in your projects yet — upload one."));
                  }
                } catch (err) {
                  del.disabled = false;
                  toast(`Could not delete ${name}: ${err.message}`, "error");
                }
              });
              card.appendChild(del);
            }
          }
          const meta = el("div", "pick-meta");
          meta.appendChild(el("div", "pick-name", img.duplicates > 1
            ? `${img.label} (${img.duplicates})`
            : img.label));
          meta.appendChild(el("div", "pick-project", img.project));
          const usedAt = usageText(img);
          if (!isAudio && usedAt) {
            meta.appendChild(el("div", "pick-usage", usedAt));
          }
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

async function pickRef(shot, key, pickerTitle = null) {
  const chosen = await chooseImage(
    pickerTitle || (key === "startRef" ? "Choose a start frame" : "Choose an end frame")
  );
  if (!chosen) return;
  shot[key] = chosen;
  markDirty();
  await saveNow();
  render();
}

document.addEventListener("DOMContentLoaded", boot);
