/* ==========================================================================
   Storyboard → Video — POC front end.
   --------------------------------------------------------------------------
   No backend. All state is in memory, all rendering is a full re-render of
   the three dynamic regions (strip / editor / preview) off a single state
   object, which is plenty at this scale and keeps the data flow obvious.

   The render queue is SIMULATED but deliberately follows the real
   orchestrator contract from the design doc:
     - strictly serial, one generation at a time
     - success = exit code AND output manifest AND runtime sanity, not just
       exit code (every real failure hit while driving vpipe exited 0)
     - a shot chained to a failed shot is blocked, not run
   ========================================================================== */

"use strict";

/* --- state --------------------------------------------------------------- */

const state = {
  project: structuredClone(MOCK_PROJECT),
  selectedId: MOCK_PROJECT.shots[0].id,
  queue: { running: false, currentId: null, stopped: false },
  logs: {}, // shotId -> [[level, text], ...]
  timers: [],
};

const MODEL_LABELS = {
  fl2va: "MiniMax H3 · FL2VA (text/anchor → video)",
  ref2va: "MiniMax H3 · Ref2VA (reference → video)",
  "krea2-still": "Krea-2 Turbo (still preview)",
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

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const shotById = (id) => state.project.shots.find((s) => s.id === id);
const shotIndex = (id) => state.project.shots.findIndex((s) => s.id === id);
const selectedShot = () => shotById(state.selectedId);

function chip(status) {
  const c = el("span", "chip", STATUS_LABELS[status] || status);
  c.dataset.status = status;
  return c;
}

function log(shotId, lines) {
  state.logs[shotId] = (state.logs[shotId] || []).concat(lines);
}

/* ==========================================================================
   Render — storyboard strip
   ========================================================================== */

function renderStrip() {
  const strip = $("#strip");
  strip.innerHTML = "";

  state.project.shots.forEach((shot, i) => {
    const card = el("div", "shot-card");
    card.dataset.id = shot.id;
    card.draggable = true;
    if (shot.id === state.selectedId) card.classList.add("selected");

    // thumbnail
    const thumb = el("div", "shot-thumb");
    if (shot.thumb && shot.status === "done") {
      const img = el("img");
      img.src = shot.thumb;
      img.alt = "";
      thumb.appendChild(img);
    } else if (shot.thumb) {
      const img = el("img");
      img.src = shot.thumb;
      img.alt = "";
      img.style.opacity = "0.28";
      img.style.filter = "grayscale(1)";
      thumb.appendChild(img);
    } else {
      thumb.appendChild(
        el("div", "shot-thumb-empty", "no frames yet")
      );
    }
    const idx = el("span", "shot-index", String(i + 1).padStart(2, "0"));
    thumb.appendChild(idx);

    if (shot.startRef && shot.startRef.kind === "chain") {
      const badge = el("span", "chain-badge");
      badge.append(el("span", null, "⛓"), el("span", null, "chained"));
      thumb.appendChild(badge);
    }
    card.appendChild(thumb);

    // body
    const body = el("div", "shot-body");
    body.appendChild(el("div", "shot-title", shot.title));
    body.appendChild(
      el(
        "div",
        "shot-sub",
        `${shot.resolution} · ${shot.frames}f · ${shot.steps} steps`
      )
    );

    const foot = el("div", "shot-foot");
    foot.appendChild(chip(shot.status));
    if (shot.status === "running") {
      foot.appendChild(el("span", "queue-pct", `${shot.progress}%`));
    } else if (shot.runtime) {
      foot.appendChild(el("span", "shot-sub", shot.runtime));
    }
    body.appendChild(foot);

    if (shot.status === "running") {
      const track = el("div", "progress-track");
      const fill = el("div", "progress-fill");
      fill.style.width = `${shot.progress}%`;
      track.appendChild(fill);
      body.appendChild(track);
    }

    card.appendChild(body);

    card.addEventListener("click", () => {
      state.selectedId = shot.id;
      render();
    });

    // drag to reorder
    card.addEventListener("dragstart", (e) => {
      card.classList.add("dragging");
      e.dataTransfer.setData("text/plain", shot.id);
      e.dataTransfer.effectAllowed = "move";
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
      const draggedId = e.dataTransfer.getData("text/plain");
      if (!draggedId || draggedId === shot.id) return;
      const from = shotIndex(draggedId);
      const to = shotIndex(shot.id);
      const [moved] = state.project.shots.splice(from, 1);
      state.project.shots.splice(to, 0, moved);
      render();
    });

    strip.appendChild(card);
  });

  const add = el("button", "add-shot");
  add.append(el("span", "plus", "+"), el("span", null, "Add shot"));
  add.addEventListener("click", addShot);
  strip.appendChild(add);
}

function addShot() {
  const n = state.project.shots.length + 1;
  const d = state.project.defaults;
  const shot = {
    id: "s" + Math.random().toString(36).slice(2, 8),
    title: `Shot ${n}`,
    prompt: "",
    startRef: null,
    endRef: null,
    chainFromPrev: false,
    model: d.model,
    resolution: d.resolution,
    frames: d.frames,
    steps: d.steps,
    seed: 0,
    status: "draft",
    progress: 0,
    thumb: null,
    runtime: null,
    expected: "~28m",
    outputs: [],
    simulate: "ok",
  };
  state.project.shots.push(shot);
  state.selectedId = shot.id;
  render();
}

/* ==========================================================================
   Render — shot editor
   ========================================================================== */

function renderEditor() {
  const host = $("#editor");
  host.innerHTML = "";
  const shot = selectedShot();

  if (!shot) {
    host.appendChild(el("div", "empty-state", "No shot selected."));
    return;
  }

  const head = el("div");
  head.style.display = "flex";
  head.style.alignItems = "center";
  head.style.gap = "var(--sp-3)";
  head.style.marginBottom = "var(--sp-3)";
  const h = el("div", null, `Shot ${shotIndex(shot.id) + 1}`);
  h.className = "section-label";
  h.style.margin = "0";
  head.appendChild(h);
  head.appendChild(chip(shot.status));
  const spacer = el("div");
  spacer.style.flex = "1";
  head.appendChild(spacer);
  const del = el("button", "btn btn-ghost btn-sm", "Delete");
  del.addEventListener("click", () => {
    state.project.shots = state.project.shots.filter((s) => s.id !== shot.id);
    state.selectedId = state.project.shots[0]?.id ?? null;
    render();
  });
  head.appendChild(del);
  host.appendChild(head);

  const panel = el("div", "panel");

  // title
  panel.appendChild(
    field("Shot name", inputText(shot.title, (v) => (shot.title = v)))
  );

  // prompt
  const ta = el("textarea");
  ta.value = shot.prompt;
  ta.rows = 5;
  ta.placeholder =
    "What happens in THIS shot — action, camera move, mood.\n" +
    "The project scene description is prepended automatically, so don't " +
    "restate the subject or style here.";
  ta.addEventListener("input", () => (shot.prompt = ta.value));
  panel.appendChild(
    field(
      "Shot prompt",
      ta,
      "action / camera / mood only — scene description is prepended"
    )
  );

  // resolved prompt preview
  const resolved = el("div", "mono");
  resolved.style.cssText =
    "background:var(--bg-sunken);border:1px solid var(--border);" +
    "border-radius:var(--radius-sm);padding:var(--sp-2);color:var(--text-faint);" +
    "max-height:88px;overflow-y:auto;";
  resolved.textContent =
    (state.project.sceneDescription || "").trim() +
    " " +
    (shot.prompt || "").trim();
  panel.appendChild(field("Resolved prompt sent to vpipe", resolved));

  host.appendChild(panel);

  // --- references ---
  const refPanel = el("div", "panel");
  refPanel.style.marginTop = "var(--sp-3)";
  const refLabel = el("div", "section-label", "Frame anchors");
  refLabel.appendChild(
    el("span", "hint", "— generate-video ports 5 / 6")
  );
  refPanel.appendChild(refLabel);

  const slots = el("div", "ref-slots");
  slots.appendChild(refSlot("Start frame", shot.startRef, shot, "startRef"));
  slots.appendChild(refSlot("End frame", shot.endRef, shot, "endRef"));
  refPanel.appendChild(slots);

  const prevIdx = shotIndex(shot.id) - 1;
  if (prevIdx >= 0) {
    const prev = state.project.shots[prevIdx];
    const wrap = el("div");
    wrap.style.marginTop = "var(--sp-3)";
    const t = el("label", "toggle");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = !!(shot.startRef && shot.startRef.kind === "chain");
    cb.addEventListener("change", () => {
      shot.startRef = cb.checked
        ? { kind: "chain", from: prev.id, label: `last frame of shot ${prevIdx + 1}` }
        : null;
      render();
    });
    t.append(cb, el("span", "toggle-track"));
    t.append(
      el("span", null, `Chain start frame from shot ${prevIdx + 1}`)
    );
    wrap.appendChild(t);
    refPanel.appendChild(wrap);
  }
  host.appendChild(refPanel);

  // --- params ---
  const params = el("div", "panel");
  params.style.marginTop = "var(--sp-3)";
  params.appendChild(el("div", "section-label", "Parameters"));

  params.appendChild(
    field(
      "Model",
      select(
        Object.entries(MODEL_LABELS).map(([v, l]) => [v, l]),
        shot.model,
        (v) => {
          shot.model = v;
          render();
        }
      )
    )
  );

  const row = el("div", "field-row-3");
  row.appendChild(
    field(
      "Resolution",
      select(
        [
          ["960x544", "960 × 544"],
          ["1024x1024", "1024 × 1024"],
          ["1344x768", "1344 × 768"],
        ],
        shot.resolution,
        (v) => (shot.resolution = v)
      )
    )
  );
  row.appendChild(
    field("Frames", inputNum(shot.frames, (v) => (shot.frames = v)), null, framesHint(shot.frames))
  );
  row.appendChild(field("Steps", inputNum(shot.steps, (v) => (shot.steps = v))));
  params.appendChild(row);

  const row2 = el("div", "field-row");
  row2.appendChild(field("Seed", inputNum(shot.seed, (v) => (shot.seed = v))));
  const durMs = (shot.frames / 24) * 1000;
  row2.appendChild(
    field("Duration", readOnly(`${(durMs / 1000).toFixed(2)}s @ 24fps`))
  );
  params.appendChild(row2);

  host.appendChild(params);
}

function framesHint(frames) {
  // MiniMax H3 snaps frame counts to 17n+5 and cannot decode below 8 latent
  // frames, which is what made frames:5 silently produce nothing.
  const valid = (frames - 5) % 17 === 0;
  const latents = ((frames - 5) / 17) * 5 + 2;
  if (!valid) return "⚠ not 17n+5 — will be snapped up";
  if (latents < 8) return "⚠ too short to decode (needs ≥ 39)";
  return `${latents} latent frames — ok`;
}

function field(labelText, control, hint, hint2) {
  const f = el("div", "field");
  const l = el("label", null, labelText);
  if (hint) {
    const s = el("span", "hint", "  " + hint);
    s.style.color = "var(--text-faint)";
    s.style.fontSize = "var(--fs-xs)";
    l.appendChild(s);
  }
  f.appendChild(l);
  f.appendChild(control);
  if (hint2) {
    const h = el("div", null, hint2);
    h.style.cssText =
      "font-size:var(--fs-xs);color:" +
      (hint2.startsWith("⚠") ? "var(--warn)" : "var(--text-faint)") +
      ";margin-top:3px;";
    f.appendChild(h);
  }
  return f;
}

function inputText(value, onChange) {
  const i = el("input");
  i.type = "text";
  i.value = value;
  i.addEventListener("input", () => onChange(i.value));
  return i;
}

function inputNum(value, onChange) {
  const i = el("input");
  i.type = "number";
  i.value = value;
  i.addEventListener("input", () => {
    onChange(Number(i.value));
    renderEditor();
  });
  return i;
}

function readOnly(text) {
  const d = el("div", null, text);
  d.style.cssText =
    "background:var(--bg-sunken);border:1px solid var(--border);" +
    "border-radius:var(--radius-sm);padding:var(--sp-2);color:var(--text-dim);";
  return d;
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

function refSlot(label, ref, shot, key) {
  const slot = el("div", "ref-slot");
  if (ref) {
    slot.classList.add("filled");
    if (ref.src) {
      const img = el("img");
      img.src = ref.src;
      img.alt = "";
      slot.appendChild(img);
    } else {
      const l = el("div", "ref-slot-label");
      l.appendChild(el("strong", null, "⛓ chained"));
      l.appendChild(el("span", null, ref.label));
      slot.appendChild(l);
    }
    const clear = el("button", "clear-ref", "✕");
    clear.title = "Clear";
    clear.addEventListener("click", (e) => {
      e.stopPropagation();
      shot[key] = null;
      render();
    });
    slot.appendChild(clear);
  } else {
    const l = el("div", "ref-slot-label");
    l.appendChild(el("strong", null, label));
    l.appendChild(el("span", null, "click to attach (POC: no upload)"));
    slot.appendChild(l);
  }
  slot.addEventListener("click", () => {
    if (shot[key]) return;
    // POC stand-in for a file picker
    shot[key] = {
      kind: "upload",
      label: "reference.png",
      src: "assets/thumbs/shot-0019.jpg",
    };
    render();
  });
  return slot;
}

/* ==========================================================================
   Render — preview / diagnostics
   ========================================================================== */

function renderPreview() {
  const host = $("#preview");
  host.innerHTML = "";
  const shot = selectedShot();
  if (!shot) return;

  host.appendChild(el("div", "section-label", "Output"));

  // stage
  const stage = el("div", "preview-stage");
  if (shot.status === "done" && shot.thumb) {
    const img = el("img");
    img.src = shot.thumb;
    img.alt = "";
    stage.appendChild(img);
  } else {
    const e = el("div", "preview-empty");
    e.appendChild(
      el("span", "big", shot.status === "running" ? "◐" : "▦")
    );
    e.appendChild(
      el(
        "span",
        null,
        shot.status === "running"
          ? `Rendering — ${shot.progress}%`
          : shot.status === "draft"
          ? "Not rendered yet"
          : "No output"
      )
    );
    stage.appendChild(e);
  }
  host.appendChild(stage);

  // diagnostics for non-clean states
  const diag = diagnosticFor(shot);
  if (diag) host.appendChild(diag);

  // stats
  const stats = el("dl", "stat-grid");
  const rows = [
    ["Status", STATUS_LABELS[shot.status]],
    ["Model", shot.model],
    ["Runtime", shot.runtime || "—"],
    ["Expected", shot.expected],
    ["Outputs", shot.outputs.length ? shot.outputs.join(", ") : "—"],
  ];
  rows.forEach(([k, v]) => {
    stats.appendChild(el("dt", null, k));
    stats.appendChild(el("dd", null, v));
  });
  const statPanel = el("div", "panel");
  statPanel.style.marginBottom = "var(--sp-3)";
  statPanel.appendChild(stats);
  host.appendChild(statPanel);

  // log
  const logLabel = el("div", "section-label", "vpipe stdout");
  logLabel.appendChild(el("span", "hint", "— parsed by the orchestrator"));
  host.appendChild(logLabel);

  const logBox = el("div", "log");
  const lines = state.logs[shot.id];
  if (!lines || !lines.length) {
    logBox.appendChild(el("span", "log-empty", "No run yet."));
  } else {
    lines.forEach(([lvl, text]) => {
      const line = el("div", "log-line");
      line.dataset.lvl = lvl;
      line.appendChild(el("span", "lvl", `[${lvl}] `));
      line.appendChild(el("span", null, text));
      logBox.appendChild(line);
    });
  }
  host.appendChild(logBox);
}

function diagnosticFor(shot) {
  if (!["failed", "blocked", "review", "interrupted"].includes(shot.status)) {
    return null;
  }
  const d = el("div", "diag");
  d.dataset.kind = shot.status;

  const titles = {
    failed: "Silent failure detected",
    blocked: "Blocked by dependency",
    review: "Flagged for review",
    interrupted: "Interrupted",
  };

  const head = el("div", "diag-head");
  head.appendChild(chip(shot.status));
  head.appendChild(el("span", null, titles[shot.status]));
  d.appendChild(head);

  const body = el("div", "diag-body");
  if (shot.status === "failed") {
    body.textContent =
      shot.failReason ||
      "Process exited 0 but the run did not produce what it declared.";
  } else if (shot.status === "blocked") {
    const from = shotById(shot.startRef?.from);
    body.textContent =
      `Start frame chains from ${from ? `“${from.title}”` : "an earlier shot"}, ` +
      `which has no output. Not queued — fix that shot and re-run to release this one.`;
  }
  d.appendChild(body);

  if (shot.checks) {
    const ul = el("ul", "diag-checks");
    shot.checks.forEach(([pass, text]) => {
      const li = el("li");
      const m = el("span", "mark", pass === true ? "✓" : pass === false ? "✕" : "–");
      m.dataset.pass = String(pass);
      li.append(m, el("span", null, text));
      ul.appendChild(li);
    });
    d.appendChild(ul);
  }

  const actions = el("div", "diag-actions");
  if (shot.status === "failed" || shot.status === "review") {
    const retry = el("button", "btn btn-sm", "Re-run this shot");
    retry.addEventListener("click", () => runQueue([shot.id]));
    actions.appendChild(retry);
    const accept = el("button", "btn btn-sm btn-ghost", "Accept anyway");
    accept.addEventListener("click", () => {
      shot.status = "done";
      render();
    });
    actions.appendChild(accept);
  }
  if (actions.children.length) d.appendChild(actions);

  return d;
}

/* ==========================================================================
   Render — rail
   ========================================================================== */

function renderRail() {
  // scene description
  const sd = $("#sceneDescription");
  if (sd.value !== state.project.sceneDescription) {
    sd.value = state.project.sceneDescription;
  }

  // style refs
  const wrap = $("#styleRefs");
  wrap.innerHTML = "";
  state.project.styleRefs.forEach((r) => {
    const d = el("div", "style-ref");
    d.title = r.label;
    const img = el("img");
    img.src = r.src;
    img.alt = r.label;
    d.appendChild(img);
    wrap.appendChild(d);
  });
  const add = el("button", "style-ref-add", "+");
  add.title = "Add style reference (POC: no upload)";
  add.addEventListener("click", () => {
    state.project.styleRefs.push({
      id: "sr" + Math.random().toString(36).slice(2, 6),
      label: "reference",
      src: "assets/thumbs/shot-0002.jpg",
    });
    render();
  });
  wrap.appendChild(add);

  // queue list
  const q = $("#queueList");
  q.innerHTML = "";
  state.project.shots.forEach((shot, i) => {
    const row = el("div", "queue-row");
    if (shot.id === state.selectedId) row.classList.add("selected");
    row.appendChild(el("span", "queue-num", String(i + 1)));
    const dot = el("span", "queue-dot");
    dot.dataset.status = shot.status;
    row.appendChild(dot);
    row.appendChild(el("span", "queue-name", shot.title));
    if (shot.status === "running") {
      row.appendChild(el("span", "queue-pct", `${shot.progress}%`));
    }
    row.addEventListener("click", () => {
      state.selectedId = shot.id;
      render();
    });
    q.appendChild(row);
  });

  // header buttons
  $("#btnRender").disabled = state.queue.running;
  $("#btnStop").disabled = !state.queue.running;

  const counts = state.project.shots.reduce((a, s) => {
    a[s.status] = (a[s.status] || 0) + 1;
    return a;
  }, {});
  $("#projectMeta").textContent =
    `${state.project.shots.length} shots · ` +
    Object.entries(counts)
      .map(([k, v]) => `${v} ${STATUS_LABELS[k].toLowerCase()}`)
      .join(", ");
}

function render() {
  renderRail();
  renderStrip();
  renderEditor();
  renderPreview();
}

/* ==========================================================================
   Simulated render queue
   --------------------------------------------------------------------------
   Serial by construction. Mirrors the orchestrator's validation contract so
   the UI states are exercised honestly rather than always resolving green.
   ========================================================================== */

function clearTimers() {
  state.timers.forEach(clearTimeout);
  state.timers = [];
}

function later(fn, ms) {
  const t = setTimeout(fn, ms);
  state.timers.push(t);
  return t;
}

function runQueue(onlyIds) {
  clearTimers();
  state.queue.stopped = false;

  const targets = state.project.shots.filter((s) =>
    onlyIds ? onlyIds.includes(s.id) : ["draft", "failed", "review", "blocked", "interrupted"].includes(s.status)
  );

  if (!targets.length) return;

  targets.forEach((s) => {
    s.status = "queued";
    s.progress = 0;
    s.runtime = null;
    s.outputs = [];
    s.checks = null;
    s.failReason = null;
    state.logs[s.id] = [];
  });

  state.queue.running = true;
  render();

  const step = (i) => {
    if (state.queue.stopped) return;
    if (i >= targets.length) {
      state.queue.running = false;
      state.queue.currentId = null;
      render();
      return;
    }

    const shot = targets[i];

    // dependency gate — a shot chained to a shot with no output is blocked,
    // never run against a missing/stale frame.
    if (shot.startRef?.kind === "chain") {
      const src = shotById(shot.startRef.from);
      if (!src || src.status !== "done") {
        shot.status = "blocked";
        log(shot.id, MOCK_LOGS.blocked);
        render();
        return later(() => step(i + 1), 420);
      }
    }

    shot.status = "running";
    shot.progress = 0;
    state.queue.currentId = shot.id;
    log(shot.id, MOCK_LOGS.head);
    render();

    // the fast-failure case never reaches denoise
    const isFast = shot.simulate === "fast";
    const ticks = isFast ? 3 : 14;
    let tick = 0;

    const advance = () => {
      if (state.queue.stopped) return;
      tick += 1;
      shot.progress = Math.min(99, Math.round((tick / ticks) * 100));
      if (tick === 4 && !isFast) log(shot.id, MOCK_LOGS.benignWarn);
      render();

      if (tick < ticks) return later(advance, 260);

      // ---- validation, per the design doc's contract ----
      finishShot(shot);
      render();
      later(() => step(i + 1), 500);
    };
    later(advance, 320);
  };

  step(0);
}

function finishShot(shot) {
  const outcome = shot.simulate;

  if (outcome === "ok") {
    shot.status = "done";
    shot.progress = 100;
    shot.runtime = "27m 44s";
    shot.outputs = [`${shot.id}.mp4`, `frames/ (${shot.frames} png)`];
    shot.thumb = shot.thumb || "assets/thumbs/shot-0019.jpg";
    shot.checks = [
      [true, "exit code 0"],
      [true, `output manifest complete — mp4 + ${shot.frames}/${shot.frames} frames`],
      [true, "runtime 27m 44s vs expected ~28m"],
      [true, "denoise progress reported"],
      ["skip", "1 warning classified benign (wired pool)"],
    ];
    log(shot.id, MOCK_LOGS.ok);
    return;
  }

  if (outcome === "empty") {
    shot.status = "failed";
    shot.progress = 100;
    shot.runtime = "24m 10s";
    shot.outputs = [];
    shot.failReason =
      "Exit code 0, but nothing was written. VAE decode refused the request " +
      "(too few latent frames) and skipped, which vpipe reports as a warning " +
      "rather than an error.";
    shot.checks = [
      [true, "exit code 0"],
      [false, "output manifest — mp4 missing, 0 frames written"],
      ["skip", "runtime plausible, so runtime check would not have caught this"],
      [true, "denoise progress reported"],
      [false, "log scan matched a known silent-failure pattern"],
    ];
    log(shot.id, MOCK_LOGS.empty);
    return;
  }

  if (outcome === "fast") {
    shot.status = "failed";
    shot.progress = 100;
    shot.runtime = "1m 25s";
    shot.outputs = [];
    shot.failReason =
      "Exit code 0 after 1m 25s against a ~28m expectation, and denoise never " +
      "started — the reference rows were emitted in the wrong shape and the " +
      "generate stage skipped them.";
    shot.checks = [
      [true, "exit code 0"],
      [false, "output manifest — mp4 missing, 0 frames written"],
      [false, "runtime 1m 25s vs expected ~28m (5%)"],
      [false, "no denoise progress ever reported"],
      [false, "log scan matched a known silent-failure pattern"],
    ];
    log(shot.id, MOCK_LOGS.fast);
    return;
  }
}

function stopQueue() {
  state.queue.stopped = true;
  state.queue.running = false;
  clearTimers();
  state.project.shots.forEach((s) => {
    if (s.status === "running") {
      s.status = "interrupted";
      log(s.id, [
        ["WARN", "SIGTERM sent, no response after 5s"],
        ["WARN", "SIGINT sent, no response after 5s"],
        ["INFO", "no output files written yet — safe to hard kill"],
        ["INFO", "SIGKILL sent, process ended (exit 137)"],
      ]);
    } else if (s.status === "queued") {
      s.status = "draft";
    }
  });
  render();
}

/* ==========================================================================
   Boot
   ========================================================================== */

function boot() {
  $("#sceneDescription").addEventListener("input", (e) => {
    state.project.sceneDescription = e.target.value;
    renderEditor();
  });

  $("#defModel").addEventListener("change", (e) => {
    state.project.defaults.model = e.target.value;
  });
  $("#defRes").addEventListener("change", (e) => {
    state.project.defaults.resolution = e.target.value;
  });
  $("#defSteps").addEventListener("input", (e) => {
    state.project.defaults.steps = Number(e.target.value);
  });

  $("#btnRender").addEventListener("click", () => runQueue());
  $("#btnStop").addEventListener("click", stopQueue);
  $("#btnReset").addEventListener("click", () => {
    clearTimers();
    state.project = structuredClone(MOCK_PROJECT);
    state.logs = {};
    state.queue = { running: false, currentId: null, stopped: false };
    state.selectedId = state.project.shots[0].id;
    render();
  });

  $("#projectTitle").textContent = state.project.name;
  render();

  const params = new URLSearchParams(location.search);

  // ?shot=N deep-links to a shot (1-based).
  const wanted = Number(params.get("shot"));
  if (wanted >= 1 && wanted <= state.project.shots.length) {
    state.selectedId = state.project.shots[wanted - 1].id;
    render();
  }

  // ?demo=run starts the simulated queue on load — handy for showing the
  // POC without narrating "now click Render", and for headless screenshots.
  if (params.get("demo") === "run") {
    later(() => runQueue(), 400);
  }
}

document.addEventListener("DOMContentLoaded", boot);
