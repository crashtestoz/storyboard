/* ==========================================================================
   Phone view. Polls the same endpoints as the desktop app and never saves a
   board: the only writes are starting/stopping GPU jobs (render, batch,
   stills). Everything else is reading and downloading.
   ========================================================================== */

"use strict";

const STATUS_MS = 2000;
const BOARD_MS = 15000;
const FPS = 24;
const LAST_KEY = "sb.mobile.project";

const STATUS_LABEL = {
  draft: "not rendered", queued: "queued", running: "rendering", done: "done",
  failed: "failed", blocked: "blocked", review: "review", interrupted: "interrupted",
};

const state = {
  boards: [],           // /api/boards list
  slug: null,           // selected project
  board: null,          // selected project's board
  stale: {},            // shotId -> why its clip is out of date
  status: null,         // /api/status
  boardCache: {},       // slug -> board, for naming what's rendering elsewhere
  open: new Set(),      // scene ids with their actions showing
  runKey: "",           // detects a run finishing, to refetch the board
  listKey: "",          // what the scene list last drew, to skip no-op redraws
  imgs: new Map(),      // thumb url -> <img>, reused so polling doesn't flicker
  boardAt: 0,
};

const $ = (id) => document.getElementById(id);

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function fmtEta(sec) {
  if (sec == null || !isFinite(sec)) return "";
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m left`;
  if (m) return `${m}m ${String(s).padStart(2, "0")}s left`;
  return `${s}s left`;
}

function fmtRuntime(sec) {
  if (!sec) return "";
  const m = Math.round(sec / 60);
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
}

// "…/scene 01 - clip.mp4?t=123" -> "scene 01 - clip.mp4"
function fileName(url) {
  try {
    return decodeURIComponent(url.split("?")[0].split("/").pop());
  } catch {
    return "download";
  }
}

let toastTimer = 0;
function toast(msg, kind) {
  const t = $("toast");
  t.textContent = msg;
  t.dataset.kind = kind || "";
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), kind === "error" ? 5000 : 2500);
}

function gpuBusy() {
  const s = state.status;
  return !!(s && (s.busy || (s.stills && s.stills.busy)));
}

function boardName(slug) {
  const b = state.boards.find((x) => x.slug === slug);
  return (b && b.name) || slug || "";
}

// --- data ------------------------------------------------------------------

async function loadBoards() {
  const { boards } = await API.listBoards();
  state.boards = boards || [];
  const picker = $("projectPicker");
  picker.replaceChildren(
    ...state.boards.map((b) => {
      const o = el("option", null, b.name);
      o.value = b.slug;
      return o;
    })
  );
  if (!state.slug || !state.boards.some((b) => b.slug === state.slug)) {
    let saved = null;
    try { saved = localStorage.getItem(LAST_KEY); } catch {}
    const busySlug = state.status && state.status.busy && state.status.slug;
    const pick = [busySlug, saved].find((s) => s && state.boards.some((b) => b.slug === s));
    state.slug = pick || (state.boards[0] && state.boards[0].slug) || null;
  }
  picker.value = state.slug || "";
}

async function loadBoard() {
  if (!state.slug) return;
  const slug = state.slug;
  const res = await API.getBoard(slug);
  if (slug !== state.slug) return; // switched while loading
  state.board = res.board;
  state.stale = (res.stale && res.stale.shots) || {};
  state.boardCache[slug] = res.board;
  state.boardAt = Date.now();
  renderScenes();
  renderMenu();
  renderPlay();
}

async function loadStatus() {
  try {
    state.status = await API.status();
    $("connDot").dataset.ok = "true";
  } catch {
    $("connDot").dataset.ok = "false";
    return;
  }
  const s = state.status;

  // Name the scene being worked on even when it's in another project.
  const nowSlug = s.busy ? s.slug : s.stills && s.stills.busy ? s.stills.slug : null;
  if (nowSlug && nowSlug !== state.slug && !state.boardCache[nowSlug]) {
    API.getBoard(nowSlug).then((r) => { state.boardCache[nowSlug] = r.board; renderNow(); }).catch(() => {});
  }

  // A run changing state (finishing, failing, starting) means new thumbs,
  // outputs and staleness on the board — fetch it rather than guess.
  const runs = s.slug === state.slug ? s.runs || {} : {};
  const key = Object.entries(runs).map(([id, r]) => `${id}:${r.status}`).join(",") +
    `|${s.stills && s.stills.busy}`;
  if (key !== state.runKey) {
    state.runKey = key;
    loadBoard().catch(() => {});
  }

  renderNow();
  renderScenes();
  renderMenu();
}

// --- now rendering ---------------------------------------------------------

function sceneOf(slug, shotId) {
  const board = slug === state.slug ? state.board : state.boardCache[slug];
  const shots = (board && board.shots) || [];
  const i = shots.findIndex((sh) => sh.id === shotId);
  return i < 0 ? null : { n: i + 1, title: shots[i].title || "Untitled", total: shots.length };
}

function renderNow() {
  const s = state.status;
  const box = $("now"), chip = $("nowChip"), bar = $("nowBar");
  let st = "idle", label = "Idle", title = "Nothing rendering", sub = "", eta = "", pct = null;

  if (s && s.busy) {
    const run = (s.runs || {})[s.currentShotId];
    const sc = sceneOf(s.slug, s.currentShotId);
    st = "running";
    label = s.operation === "dialogue" ? "Preparing dialogue" : s.operation === "refine" ? "Auto-refining" : "Rendering";
    title = boardName(s.slug);
    if (sc) sub = `Scene ${sc.n} of ${sc.total} · ${sc.title}`;
    if (run) {
      pct = run.progress || 0;
      sub += `${sub ? " · " : ""}${run.phase || run.status} ${Math.round(pct)}%`;
      eta = fmtEta(run.etaSeconds);
    }
    const order = s.order || [];
    if (order.length > 1) {
      const left = order.filter((id) => ["queued", "running"].includes(((s.runs || {})[id] || {}).status)).length;
      sub += ` · ${left} of ${order.length} left`;
    }
    const pb = s.projectBatch;
    if (pb && pb.active && pb.total.length) {
      sub += ` · batch project ${pb.done.length + 1} of ${pb.total.length}`;
    }
  } else if (s && s.stills && s.stills.busy) {
    const sc = sceneOf(s.stills.slug, s.stills.shotId);
    st = "running";
    label = "Creating images";
    title = boardName(s.stills.slug) || "Stills";
    sub = (sc ? `Scene ${sc.n} · ${sc.title} · ` : "") + `${s.stills.phase || ""} ${Math.round(s.stills.progress || 0)}%`;
    pct = s.stills.progress || 0;
  } else if (s && s.error) {
    st = "failed";
    label = "Stopped";
    title = boardName(s.slug) || "Last render";
    sub = s.error;
  } else if (!s) {
    title = "Connecting…";
  }

  box.dataset.state = st;
  chip.dataset.status = st === "idle" ? "draft" : st;
  chip.textContent = label;
  $("nowTitle").textContent = title;
  $("nowSub").textContent = sub;
  $("nowEta").textContent = eta;
  bar.hidden = pct == null;
  if (pct != null) bar.firstElementChild.style.width = `${Math.min(100, pct)}%`;
}

// --- scene list ------------------------------------------------------------

function liveRun(shotId) {
  const s = state.status;
  if (!s || s.slug !== state.slug) return null;
  const run = (s.runs || {})[shotId];
  // Only an active run outranks the board; a finished one is already saved.
  return run && (s.busy || run.status === "running") ? run : null;
}

function downloadLink(label, url) {
  const a = el("a", "btn btn-sm", `⬇ ${label}`);
  a.href = url;
  a.download = fileName(url);
  return a;
}

const VIDEO_EXT = /\.(mp4|m4v|mov|webm)$/i;

/* Plays a clip in the page's own player — no download. */
function playButton(label, url, title) {
  const b = el("button", "btn btn-sm", `▶ ${label}`);
  b.addEventListener("click", (e) => { e.stopPropagation(); openPlayer(url, title); });
  return b;
}

function actionButton(label, onClick, primary) {
  const b = el("button", `btn btn-sm${primary ? " btn-primary" : ""}`, label);
  b.disabled = gpuBusy();
  b.addEventListener("click", (e) => { e.stopPropagation(); onClick(); });
  return b;
}

function sceneActions(raw, n) {
  const box = el("div", "scene-actions");
  box.append(
    actionButton("▶ Render", () => startRender([raw.id], `Render scene ${n}?`), true),
    actionButton("✦ Create images", () => startStills(raw.id, n))
  );
  const title = raw.title || `Scene ${n}`;
  (raw.outputs || []).forEach((url, i, all) => {
    const name = all.length > 1 ? `Clip ${i + 1}` : "Clip";
    // Watch it here first; the download stays for saving it to the phone.
    if (VIDEO_EXT.test(url.split("?")[0])) box.appendChild(playButton(`Play ${name.toLowerCase()}`, url, title));
    box.appendChild(downloadLink(name, url));
  });
  if (raw.thumb) box.appendChild(downloadLink("Frame", raw.thumb));
  for (const [phase, still] of Object.entries(raw.stills || {})) {
    if (still && still.url) box.appendChild(downloadLink(`Image ${phase}`, still.url));
  }
  return box;
}

function renderScenes() {
  const list = $("scenes");
  const b = state.board;
  if (!b || b !== state.boardCache[state.slug]) {
    if (!state.slug) list.replaceChildren(el("li", "project-meta", "No projects yet."));
    return;
  }
  const shots = b.shots || [];
  const done = shots.filter((sh) => sh.status === "done").length;
  const secs = shots.reduce((t, sh) => t + (sh.frames || 0) / FPS, 0);
  const draftOn = !!(b.defaults && b.defaults.draft);
  $("projectMeta").textContent =
    `${shots.length} scenes · ${done} rendered · ${Math.round(secs)}s` + (draftOn ? " · draft mode" : "");

  const stills = state.status && state.status.stills;
  const key = JSON.stringify([
    b.updatedAt, state.slug, state.stale, [...state.open], gpuBusy(),
    state.status && state.status.slug === state.slug ? state.status.runs : null,
    stills && stills.busy ? [stills.slug, stills.shotId, stills.phase, stills.progress] : null,
  ]);
  if (key === state.listKey) return;
  state.listKey = key;
  const scrollY = window.scrollY;
  list.replaceChildren(
    ...shots.map((raw, i) => {
      const run = liveRun(raw.id);
      const making = stills && stills.busy && stills.slug === state.slug && stills.shotId === raw.id;
      const status = run ? run.status : raw.status || "draft";
      const li = el("li", "scene");
      li.dataset.running = String(status === "running" || !!making);

      const row = el("button", "scene-row");
      row.type = "button";
      row.setAttribute("aria-expanded", String(state.open.has(raw.id)));

      const thumb = el("div", "thumb");
      thumb.dataset.done = String(raw.status === "done");
      const src = raw.thumb || "assets/shot-placeholder.png";
      let img = state.imgs.get(src);
      if (!img) {
        img = el("img", raw.thumb ? null : "placeholder");
        img.alt = "";
        img.onerror = () => { img.className = "placeholder"; img.src = "assets/shot-placeholder.png"; };
        img.src = src;
        state.imgs.set(src, img);
      }
      thumb.append(img, el("span", "shot-index", String(i + 1).padStart(2, "0")));
      if (raw.renderedAs === "draft") thumb.appendChild(el("span", "draft-badge", "DRAFT"));
      if (state.stale[raw.id] && status !== "running") {
        const s = el("span", "stale-badge", "CHANGED");
        s.title = state.stale[raw.id];
        thumb.appendChild(s);
      }

      const body = el("div", "scene-body");
      body.appendChild(el("div", "scene-title", raw.title || "Untitled"));
      const size = (raw.resolution || b.defaults?.resolution || "").replace("x", "×");
      body.appendChild(el("div", "scene-sub",
        [size, raw.frames ? `${(raw.frames / FPS).toFixed(1)}s` : "", raw.steps ? `${raw.steps} steps` : ""]
          .filter(Boolean).join(" · ")));

      const tags = el("div", "scene-tags");
      const chip = el("span", "chip", making ? "creating images" : STATUS_LABEL[status] || status);
      chip.dataset.status = making ? "running" : status;
      tags.appendChild(chip);
      body.appendChild(tags);

      if (run && status === "running") {
        const bar = el("div", "bar");
        const fill = el("div", "bar-fill");
        fill.style.width = `${Math.min(100, run.progress || 0)}%`;
        bar.appendChild(fill);
        body.append(bar, el("div", "scene-phase",
          [`${run.phase || "starting"} ${Math.round(run.progress || 0)}%`, fmtEta(run.etaSeconds)]
            .filter(Boolean).join(" · ")));
      } else if (making) {
        body.appendChild(el("div", "scene-phase", `${stills.phase || ""} ${Math.round(stills.progress || 0)}%`));
      } else if (raw.status === "done" && raw.runtimeSeconds) {
        body.appendChild(el("div", "scene-phase", `rendered in ${fmtRuntime(raw.runtimeSeconds)}`));
      } else if (raw.reason && ["failed", "blocked", "review", "interrupted"].includes(status)) {
        body.appendChild(el("div", "scene-phase", raw.reason));
      }

      row.append(thumb, body);
      row.addEventListener("click", () => {
        state.open.has(raw.id) ? state.open.delete(raw.id) : state.open.add(raw.id);
        renderScenes();
      });
      li.appendChild(row);
      if (state.open.has(raw.id)) li.appendChild(sceneActions(raw, i + 1));
      return li;
    })
  );
  window.scrollTo(0, scrollY);
}

// --- actions ---------------------------------------------------------------

async function run(fn, okMsg) {
  try {
    await fn();
    if (okMsg) toast(okMsg);
  } catch (err) {
    toast(err.message || String(err), "error");
  }
  loadStatus();
}

// No board is sent: the phone renders whatever the board on disk says, and
// must never overwrite edits made on the desktop.
function startRender(shotIds, question) {
  if (!state.slug || !confirm(question)) return;
  run(() => API.render(state.slug, shotIds), "Render started");
}

function startStills(shotId, n) {
  if (!confirm(`Create images for scene ${n}?`)) return;
  run(() => API.stills(state.slug, shotId), "Creating images");
}

function renderMenu() {
  const s = state.status;
  const busy = gpuBusy();
  $("mRenderAll").disabled = busy || !state.slug;
  $("mBatch").disabled = busy;
  $("mStop").disabled = !busy || !!(s && s.cancelRequested);
  const video = $("mVideo");
  const url = state.board && state.board.finalVideo && state.board.finalVideo.url;
  if (url) {
    video.href = url;
    video.download = fileName(url);
    video.removeAttribute("aria-disabled");
  } else {
    video.removeAttribute("href");
    video.setAttribute("aria-disabled", "true");
  }
}

function renderPlay() {
  const url = state.board && state.board.finalVideo && state.board.finalVideo.url;
  $("btnPlay").hidden = !url;
}

/* The full-screen player: the assembled video by default, or any clip's URL. */
function openPlayer(url, title) {
  if (typeof url !== "string") {
    url = state.board && state.board.finalVideo && state.board.finalVideo.url;
    title = boardName(state.slug);
  }
  if (!url) return;
  const v = $("playerVideo");
  if (v.dataset.src !== url) { v.src = url; v.dataset.src = url; }
  $("playerTitle").textContent = title || "";
  $("player").hidden = false;
  v.play().catch(() => {}); // a tap started this, so iOS allows sound
}

function closePlayer() {
  const v = $("playerVideo");
  v.pause();
  $("player").hidden = true;
}

function toggleMenu(open) {
  const menu = $("menu");
  open = open ?? menu.hidden;
  menu.hidden = !open;
  $("btnMenu").setAttribute("aria-expanded", String(open));
}

function openBatch() {
  const list = $("batchList");
  list.replaceChildren(
    ...state.boards.map((b) => {
      const row = el("label", "batch-item");
      const box = el("input");
      box.type = "checkbox";
      box.value = b.slug;
      box.checked = b.slug === state.slug;
      const name = el("span", null, b.name);
      row.append(box, name, el("small", null, `${b.rendered}/${b.shots}`));
      return row;
    })
  );
  const sync = () => ($("batchStart").disabled = !list.querySelector("input:checked"));
  list.onchange = sync;
  sync();
  $("batchSheet").hidden = false;
}

function wire() {
  $("btnMenu").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu(); });
  document.addEventListener("click", (e) => {
    if (!$("menu").hidden && !$("menu").contains(e.target)) toggleMenu(false);
  });
  $("menu").addEventListener("click", (e) => {
    if (e.target.closest(".m-menu-item")) toggleMenu(false);
  });

  $("mRenderAll").addEventListener("click", () =>
    startRender(undefined, `Render all scenes of “${boardName(state.slug)}”?`));
  $("mStop").addEventListener("click", () => {
    if (confirm("Stop the current job?")) run(() => API.stop(), "Stopping…");
  });
  $("mBatch").addEventListener("click", openBatch);
  $("mRefresh").addEventListener("click", () => refreshAll());

  $("btnPlay").addEventListener("click", () => openPlayer());
  $("playerClose").addEventListener("click", closePlayer);

  $("batchCancel").addEventListener("click", () => ($("batchSheet").hidden = true));
  $("batchSheet").addEventListener("click", (e) => {
    if (e.target === $("batchSheet")) $("batchSheet").hidden = true;
  });
  $("batchStart").addEventListener("click", () => {
    const slugs = [...$("batchList").querySelectorAll("input:checked")].map((b) => b.value);
    $("batchSheet").hidden = true;
    run(() => API.renderBatch(slugs), `Batch of ${slugs.length} started`);
  });

  $("projectPicker").addEventListener("change", (e) => {
    state.slug = e.target.value;
    state.board = null;
    renderPlay();
    closePlayer();
    state.open.clear();
    state.imgs.clear();
    try { localStorage.setItem(LAST_KEY, state.slug); } catch {}
    $("scenes").replaceChildren();
    $("projectMeta").textContent = "Loading…";
    loadBoard().catch((err) => toast(err.message, "error"));
    renderNow();
  });
}

async function refreshAll() {
  try {
    await loadBoards();
    await loadBoard();
  } catch (err) {
    toast(err.message || String(err), "error");
  }
  loadStatus();
}

// Poll only while the page is visible; a phone in a pocket shouldn't keep
// hitting the server.
function tick() {
  if (document.visibilityState === "visible") {
    loadStatus();
    if (Date.now() - state.boardAt > BOARD_MS) loadBoard().catch(() => {});
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refreshAll();
});

wire();
(async () => {
  try { state.status = await API.status(); } catch {}
  await refreshAll();
  setInterval(tick, STATUS_MS);
})();
