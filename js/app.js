/* ==========================================================================
   Storyboard — front end.
   --------------------------------------------------------------------------
   Talks to the server in server/. Board edits are saved back with a short
   debounce; while a render is running the queue is polled once a second and
   the server's view of a shot wins over the local one, since it is the thing
   actually watching the process.

   Frame-count rules and available model capabilities come from /api/info.
   Video shots always route to Ref2VA (unless an explicit engine is
   requested); Start/End frame anchors are sent to it as ordered references.
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
  pendingSaves: new Set(),
  deletingBoard: false,
  // Set when we start a batch, cleared when its end has been reported. A
  // whole-board run with nothing to render still has work to do — it
  // assembles the cut — and can finish between two polls, so "was busy last
  // tick" is not enough to notice it ended.
  awaitingBatch: false,
  dirty: false,
  toast: null,
  // While a batch render is in flight, the Output panel follows whichever
  // shot the orchestrator is actually rendering — this is what makes the big
  // preview (and its stage tiles/log) advance scene to scene on its own
  // instead of sitting on whatever was selected when "Render all" was
  // clicked. Cleared the moment someone clicks a different shot themselves,
  // so inspecting an earlier shot mid-batch does not get yanked away.
  followRender: true,
  // A render may be preceded by several sequential speech-engine calls.
  dialoguePreparing: false,
  // Browser-session conversations, separated by board. Proposed edits remain
  // attached to a turn until the user explicitly applies them.
  chats: {},
  chatBusy: false,
  bladeOpen: false,
  // Whether the AD reads its own replies aloud. A device preference, not
  // board content — kept in localStorage rather than defaults, the same
  // reasoning as CHAT_STORAGE_PREFIX above.
  speechEnabled: false,
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
// Render outputs carry a `?t=<mtime>` cache-buster after render (see
// orchestrator._as_url) so a fresh clip is never mistaken for the take it
// overwrote. Match the extension before that query string, not the end of
// the whole URL.
const hasExt = (url, exts) => new RegExp(`\\.(${exts})(\\?|$)`, "i").test(url);
const shots = () => (state.board && state.board.shots) || [];
const shotById = (id) => shots().find((s) => s.id === id);
const shotIndex = (id) => shots().findIndex((s) => s.id === id);
const selectedShot = () => shotById(state.selectedId);
const modelCap = (id) => state.models.find((m) => m.id === id) || null;
// Video shots always render through Ref2VA now: Start/End frame images
// (manually chosen or chained from the previous shot's last rendered frame)
// are sent as ordered references, never a hard-pinned keyframe, so
// character/style/reference media and native voice cloning are always
// available regardless of whether a shot has one set. Krea-2 is still used
// by the separate Create Stills preview job via its synthetic shot copy, but
// is not a user-selectable shot model.
const STORYBOARD_MODEL = "ref2va";
// Mirrors server/backends/vpipe_backend.py's _effective_video_model: an
// explicitly chosen engine (per-shot, else the project's "Video engine"
// setting) short-circuits automatic H3 routing. krea2-still, wan-i2v and
// ltx-2.5 are such engines — none has a reference-list mode to fall back to
// the way Ref2VA is H3's fallback, so none is ever inferred automatically,
// only requested.
function effectiveShotModel(raw) {
  const defaults = state.board && state.board.defaults;
  const requested = (raw && raw.model) || (defaults && defaults.model) || STORYBOARD_MODEL;
  if (requested === "krea2-still" || requested === "wan-i2v" || requested === "ltx-2.5") return requested;
  return STORYBOARD_MODEL;
}

/* True when this shot's dialogue is spoken directly in the render, in the
   speaking character's own cloned voice (Ref2VA, with that character's own
   voice clip sent in as a reference — see server/backends/vpipe_backend.py's
   _clones_voice). Mirrors that same rule so the UI and the render agree on
   which shots need a separate TTS dub and which don't. */
function shotClonesVoice(raw) {
  if (effectiveShotModel(raw) !== STORYBOARD_MODEL) return false;
  if (raw.dialogueSource === "recording") return false;
  const cast = (state.board.characters || []).filter((c) =>
    (raw.characterIds || []).includes(c.id)
  );
  const inferred = cast.find((c) => c.voice && c.voice.path) || cast[0] || null;
  const speaker = cast.find((c) => c.id === raw.speakerId) || inferred;
  return !!(speaker && speaker.voice && speaker.voice.path);
}

/* A written line has two possible render paths. Keep this explanation in one
   place so the Dialogue panel, render buttons and failed-shot diagnostic all
   describe the same prerequisite instead of making the user discover it by
   spending a render attempt. */
function dialogueReadiness(raw) {
  if (!raw || !(raw.dialogue || "").trim()) return null;

  const source = raw.dialogueSource || "auto";
  const cast = (state.board.characters || []).filter((c) =>
    (raw.characterIds || []).includes(c.id)
  );
  const inferred = cast.find((c) => c.voice && c.voice.path) || cast[0] || null;
  const speaker = cast.find((c) => c.id === raw.speakerId) || inferred;
  const hasVoice = !!(speaker && speaker.voice && speaker.voice.path);
  const engine = state.tts.find((e) => e.id === (state.board.defaults.tts || "none"));

  if (source === "recording") {
    if (raw.dialogueAudioUrl &&
        ((raw.dialogueSpokenText || "").trim() !== raw.dialogue.trim() ||
         (raw.dialogueSpokenStyle || "").trim() !== (raw.dialogueStyle || "").trim())) {
      return {
        code: "dialogue-recording-stale",
        title: "Dialogue recording is out of date",
        body: "This shot uses a separate dialogue recording, but the line or voice direction changed after the take was generated.",
        action: "Rendering will create a fresh take automatically; use Generate in the Dialogue panel only if you want to preview it first.",
      };
    }
    if (!raw.dialogueAudioUrl) {
      const engineLine = !engine || engine.id === "none"
        ? "No speech engine is selected."
        : !engine.healthy
        ? `${engine.label} is unavailable${engine.message ? `: ${engine.message}` : "."}`
        : `${engine.label} is selected and ready.`;
      return {
        code: "dialogue-recording-missing",
        title: "Dialogue recording required before rendering",
        body: `“Use Dialogue-window recording” is selected, but this line has no generated audio take. ${engineLine}`,
        action: engine && engine.healthy
          ? "Rendering will create the take automatically; use Generate in the Dialogue panel if you want to preview it first."
          : "Choose a healthy Speech engine in Settings, then press Generate in the Dialogue panel.",
      };
    }
    return null;
  }

  if (source === "native") {
    if (effectiveShotModel(raw) !== "ref2va") {
      return {
        code: "native-dialogue-no-audio-engine",
        title: "This video engine cannot speak dialogue",
        body: "The project's video engine generates silent video only — MiniMax H3 Ref2VA is required for native H3 speech.",
        action: "Switch Dialogue source to a separate recording, or change the project's video engine to Automatic (MiniMax H3) in Settings.",
        blocking: true,
      };
    }
    if (!speaker) {
      return {
        code: "native-dialogue-no-speaker",
        title: "No cast speaker assigned",
        body: "This line has no assigned character, so MiniMax H3 will choose a voice that fits the scene rather than cloning a specific one.",
        action: "Add a character in Cast and assign them to this shot if you want a specific cloned voice.",
        blocking: false,
      };
    }
    if (!hasVoice) {
      return {
        code: "native-dialogue-no-voice",
        title: `${speaker.name || "The assigned character"} has no reference voice`,
        body: "MiniMax H3 will still speak the line aloud, in a voice that fits the character and scene, rather than cloning one.",
        action: "Attach a reference voice to the cast member if you want a specific cloned voice.",
        blocking: false,
      };
    }
    return null;
  }

  // “auto” is kept for older boards. It can only produce speech reliably when
  // one of the two explicit paths is already prepared; otherwise H3 is told to
  // keep the soundtrack ambient and the written line will not be spoken.
  if (!hasVoice && !raw.dialogueAudioUrl) {
    return {
      code: "dialogue-source-ambiguous",
      title: "Choose how this dialogue should be generated",
      body: "This legacy dialogue setting has neither a prepared recording nor a cast voice reference for H3 native speech.",
      action: "Select Dialogue-window recording and generate a take, or select H3 native speech and add a cast reference voice.",
    };
  }
  return null;
}

function dialogueGuide(host, raw) {
  if (!host) return;
  host.innerHTML = "";
  const issue = dialogueReadiness(raw);
  if (!issue) {
    if ((raw.dialogue || "").trim()) {
      const source = raw.dialogueSource || "auto";
      host.className = "dialogue-guide field-note";
      host.textContent = source === "native"
        ? (shotClonesVoice(raw)
          ? "✓ H3 native speech is ready; the cast reference voice will be sent to Ref2VA."
          : "✓ H3 native speech is ready; H3 will choose a voice that fits the character and scene.")
        : raw.dialogueAudioUrl
        ? "✓ Dialogue take is ready; the generated voice will be mixed after video generation."
        : "";
    }
    return;
  }
  host.className = issue.blocking === false ? "dialogue-guide field-note" : "dialogue-guide inline-warn";
  host.appendChild(el("strong", null, `${issue.blocking === false ? "ℹ " : ""}${issue.title}. `));
  host.appendChild(el("span", null, `${issue.body} ${issue.action}`));
}

function renderDialogueBlockers() {
  return shots()
    .map((raw) => ({ raw, issue: dialogueReadiness(raw) }))
    .filter((entry) => entry.issue && entry.issue.blocking !== false);
}

function canAutoPrepareDialogue(raw) {
  if (!raw || (raw.dialogueSource || "auto") !== "recording") return false;
  const engine = state.tts.find((e) => e.id === (state.board.defaults.tts || "none"));
  return !!(engine && engine.id !== "none" && engine.healthy);
}

function applyDialogueTake(raw, result, text, style) {
  raw.dialogueAudioUrl = result.audioUrl;
  if (result.speechFingerprint) raw.speechFingerprint = result.speechFingerprint;
  if (result.speechEngineFingerprint) raw.speechEngineFingerprint = result.speechEngineFingerprint;
  raw.dialogueSpokenText = (text || "").trim();
  raw.dialogueSpokenStyle = (style || "").trim();
  if (result.dubUrl) raw.dubUrl = result.dubUrl;
  if (result.speechLogUrl) raw.speechLogUrl = result.speechLogUrl;
  if (result.log && result.log.length) state.speechLog[raw.id] = result.log;
}

/* Generate missing or stale recording takes one at a time. Each /dub request
   loads and saves the board, so parallel requests could make one completed
   take disappear when another request saves its copy. Native H3 shots never
   enter this list and therefore never receive a second voice track. */
async function prepareDialogueRecordings(entries) {
  if (!entries.length) return;
  state.dialoguePreparing = true;
  try {
    await saveNow();
    for (let i = 0; i < entries.length; i += 1) {
      const entry = entries[i];
      const raw = shotById(entry.raw.id);
      if (!raw) throw new Error(`Shot ${entry.raw.id} no longer exists.`);
      const text = (raw.dialogue || "").trim();
      const style = raw.dialogueStyle || "";
      toast(`Generating dialogue ${i + 1} of ${entries.length}: ${raw.title || `Shot ${i + 1}`}…`);
      try {
        const result = await API.dub(state.slug, raw.id, text, style, raw.dubMode);
        if (!result || !result.audioUrl) throw new Error("the speech engine returned no audio");
        applyDialogueTake(raw, result, text, style);
        if (result.note) toast(result.note, "warn");
        else if (result.warning) toast(result.warning, "warn");
      } catch (err) {
        if (err.payload && err.payload.log && err.payload.log.length) {
          state.speechLog[raw.id] = err.payload.log;
        }
        throw new Error(`Could not generate dialogue for ${raw.title || raw.id}: ${err.message}`);
      }
    }
  } finally {
    state.dialoguePreparing = false;
  }
}

/* The mechanical half of "speak this line": synthesise, then fold the take
   into the shot. Shared by the per-shot Generate button and Storyboard AD's
   dub_shot action so both go through the exact same call — the caller
   builds whatever toast/message fits its own UI from the result. */
async function runDubShot(shotId) {
  const raw = shotById(shotId);
  if (!raw) throw new Error("That shot no longer exists.");
  const r = await API.dub(state.slug, shotId, raw.dialogue, raw.dialogueStyle, raw.dubMode);
  applyDialogueTake(raw, r, raw.dialogue, raw.dialogueStyle);
  return r;
}

function showRenderDialogueBlocker(blockers) {
  const first = blockers[0];
  if (!first) return;
  state.selectedId = first.raw.id;
  state.tab = "dialogue";
  render();
  toast(
    `${blockers.length} shot${blockers.length === 1 ? "" : "s"} need dialogue setup. ${first.issue.title}: ${first.issue.action}`,
    "error"
  );
}
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

function downloadFileName(base, ext) {
  const clean = (base || "download")
    .replace(/[<>:"/\\|?*\x00-\x1f]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 120) || "download";
  return `${clean}.${ext || "mp4"}`;
}

function extensionFromUrl(url, fallback = "mp4") {
  const file = decodeURIComponent(
    (url || "").split("#")[0].split("?")[0].split("/").pop() || ""
  );
  const match = file.match(/\.([a-z0-9]+)$/i);
  return match ? match[1].toLowerCase() : fallback;
}

function sceneClipDownloadName(raw, url) {
  const n = shotIndex(raw.id);
  const scene = n >= 0 ? n + 1 : "";
  return downloadFileName(
    `${state.board.name || state.slug || "Project"} - scene ${scene}`,
    extensionFromUrl(url)
  );
}

function outputMuted() {
  return !!(state.board && state.board.outputMuted);
}

function paintOutputMute(button) {
  const muted = outputMuted();
  button.replaceChildren(
    stageIconSvg(muted ? "sound-muted" : "sound"),
    el("span", null, muted ? "Muted" : "Sound")
  );
  button.dataset.muted = String(muted);
  button.title = muted
    ? "Unmute project output previews"
    : "Mute project output previews";
  button.setAttribute("aria-pressed", String(muted));
}

function applyOutputMute(muted) {
  state.board.outputMuted = !!muted;
  document.querySelectorAll("#preview video").forEach((video) => {
    video.muted = state.board.outputMuted;
  });
  document.querySelectorAll("#preview .output-mute").forEach(paintOutputMute);
  markDirty();
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

/** Appended next to a running shot's percent once there's enough denoise
 * progress to extrapolate from (see _denoise_eta_seconds on the backend). */
function etaSuffix(shot) {
  return shot.etaSeconds != null ? ` · ~${dur(shot.etaSeconds)} left` : "";
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
  if (!state.slug || !state.board || state.deletingBoard) return;
  clearTimeout(state.saveTimer);
  let request;
  try {
    $("#saveState").textContent = "saving…";
    const sent = state.board;
    request = API.saveBoard(state.slug, state.board);
    state.pendingSaves.add(request);
    const res = await request;
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
  } finally {
    state.pendingSaves.delete(request);
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
    etaSeconds: run.etaSeconds != null ? run.etaSeconds : null,
    runtimeSeconds:
      run.runtimeSeconds != null ? run.runtimeSeconds : shot.runtimeSeconds,
    validation: run.validation || shot.validation,
    reason: run.reason || "",
    phase: run.phase || "",
    outputs: run.outputs && run.outputs.length ? run.outputs : shot.outputs,
    log: run.log || null,
    summary: run.summary || "",
    heartbeat: {
      lastOutputAt: run.lastOutputAt, serverTime: run.serverTime,
      phaseStartedAt: run.phaseStartedAt, phaseReportedAt: run.phaseReportedAt,
      phasePercent: run.phasePercent,
    },
  };
}

/* What a finished batch actually produced.

   "Render batch finished." was true and useless. The run that prompted this
   rendered one shot of three — the other two were marked done, so they were
   skipped even though their prompts had been rewritten — and built no video
   at all, while reporting success. A summary has to say what came out. */
function batchOutcome(status) {
  if (status.operation === "dialogue") {
    return {msg: status.error ? `Dialogue preparation failed: ${status.error}` : status.cancelRequested ? "Dialogue preparation stopped." : "Dialogue takes are ready to preview. Video clips were not regenerated.", kind: status.error ? "error" : "info"};
  }
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

function projectBatchOutcome(status) {
  const pb = status && status.projectBatch;
  const total = (pb && pb.total) || [];
  const errors = (pb && pb.errors) || {};
  const failedCount = Object.keys(errors).length;
  const bits = [`${total.length} project(s)`];
  if (failedCount) bits.push(`${failedCount} could not render — ${Object.keys(errors).join(", ")}`);
  return {
    msg: `Batch render finished — ${bits.join(", ")}.`,
    kind: failedCount ? "error" : "info",
  };
}

/* ==========================================================================
   Storyboard AD blade
   ========================================================================== */

// Kept per browser, not on the board itself: this is a scratchpad for
// talking to Storyboard AD, not authored storyboard content, so it has no
// business in the JSON an MCP client or a collaborator reads. Persisting it
// is purely so a page refresh (or an accidental tab close) doesn't throw an
// in-progress conversation away.
const CHAT_STORAGE_PREFIX = "storyboardToVideo.chat.";
const CHAT_HISTORY_LIMIT = 60;
const AD_SPEECH_STORAGE_KEY = "storyboardToVideo.adSpeech.enabled";

function chatStorageKey(slug) {
  return CHAT_STORAGE_PREFIX + slug;
}

function loadPersistedChat(slug) {
  try {
    const parsed = JSON.parse(localStorage.getItem(chatStorageKey(slug)) || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function persistChat(slug) {
  if (!slug) return;
  try {
    // A "thinking…" bubble mid-request is not worth restoring as itself;
    // the reply (or the "Chat failed" turn) that replaces it is what matters.
    const turns = (state.chats[slug] || []).filter((t) => !t.pending).slice(-CHAT_HISTORY_LIMIT);
    if (turns.length) localStorage.setItem(chatStorageKey(slug), JSON.stringify(turns));
    else localStorage.removeItem(chatStorageKey(slug));
  } catch {
    // Private browsing, storage disabled, or quota exceeded — the
    // conversation just won't survive a refresh this time.
  }
}

function clearPersistedChat(slug) {
  if (!slug) return;
  try { localStorage.removeItem(chatStorageKey(slug)); } catch { /* optional storage */ }
}

function chatTurns() {
  if (!state.slug) return [];
  if (!state.chats[state.slug]) state.chats[state.slug] = loadPersistedChat(state.slug);
  return state.chats[state.slug];
}

function positionAssistantBlade() {
  const strip = document.querySelector(".strip-wrap");
  if (!strip) return;
  const bottom = Math.max(0, Math.min(window.innerHeight - 180, strip.getBoundingClientRect().bottom));
  document.documentElement.style.setProperty("--blade-top", `${Math.round(bottom)}px`);
}

function setBladeOpen(open) {
  state.bladeOpen = !!open;
  const blade = $("#assistantBlade");
  blade.classList.toggle("open", state.bladeOpen);
  blade.style.transform = "";
  $("#bladeHandle").setAttribute("aria-expanded", String(state.bladeOpen));
  $("#bladeHandle").title = state.bladeOpen
    ? "Close Storyboard AD" : "Open Storyboard AD";
  if (state.bladeOpen) {
    positionAssistantBlade();
    renderAssistantChat();
    setTimeout(() => $("#bladeInput").focus(), 190);
  }
}

function paintBladeContext() {
  const box = $("#bladeContext");
  if (!box || !state.board) return;
  const raw = selectedShot();
  box.textContent = `${state.board.name} · ${shots().length} shot${shots().length === 1 ? "" : "s"}` +
    (raw ? ` · focused on ${raw.title || "selected shot"}` : " · whole board");
  const svc = currentLLM();
  $("#bladeModel").textContent = svc
    ? `${svc.label}${svc.model ? ` · ${svc.model}` : ""}`
    : "No prompt rewriting model configured";
  paintBladeSpeechControls();
}

/* The voice list is cast members with a recorded voice clip — the same
   reference a cloning engine would use for their own dialogue. Picking one
   here just tells speak_ad_reply (server/speech.py) to reuse it for the AD's
   replies too, rather than the engine's own default voice. */
function paintBladeSpeechControls() {
  const select = $("#bladeSpeechVoice");
  if (!select || !state.board) return;
  const wanted = state.board.defaults.adSpeakerId || "";
  const voiced = (state.board.characters || []).filter((c) => (c.voice || {}).path);
  select.innerHTML = "";
  select.appendChild(new Option("Engine default voice", ""));
  voiced.forEach((c) => select.appendChild(new Option(c.name || "Unnamed cast member", c.id)));
  // The saved choice may name a character since removed or since stripped of
  // its voice — fall back to the default rather than silently pick another.
  select.value = voiced.some((c) => c.id === wanted) ? wanted : "";
}

/* The fields an action would overwrite, as {label, before, after} pairs, so
   the proposal box can show what the AD's fix actually says before it lands
   — proposalSummary alone only names *which* shot/character/field changes,
   not the text itself. Returns null for actions with nothing to preview
   (an operation like start_render has no text, only a target). */
function proposalDetailFields(action) {
  if (action.tool === "update_shot") {
    const s = shotById(action.shotId);
    return Object.entries(action.fields || {}).map(([k, v]) => ({
      label: k, before: s ? s[k] : undefined, after: v,
    }));
  }
  if (action.tool === "update_character") {
    const c = (state.board.characters || []).find((x) => x.id === action.characterId);
    return Object.entries(action.fields || {}).map(([k, v]) => ({
      label: k, before: c ? c[k] : undefined, after: v,
    }));
  }
  if (action.tool === "add_shot") {
    return Object.entries(action.shot || {}).map(([k, v]) => ({ label: k, before: undefined, after: v }));
  }
  if (action.tool === "add_character") {
    return Object.entries(action.character || {}).map(([k, v]) => ({ label: k, before: undefined, after: v }));
  }
  if (action.tool === "set_board_fields") {
    return Object.entries(action.fields || {}).map(([k, v]) => ({ label: k, before: state.board[k], after: v }));
  }
  return null;
}

function proposalSummary(action) {
  if (action.tool === "set_board_fields") return "Update project scene or sound";
  if (action.tool === "add_character") return `Add cast member “${action.character.name}”`;
  if (action.tool === "update_character") {
    const c = (state.board.characters || []).find((x) => x.id === action.characterId);
    return `Update cast member “${c ? c.name : action.characterId}”`;
  }
  if (action.tool === "add_shot") return `Add shot “${action.shot.title || "Untitled shot"}”`;
  if (action.tool === "update_shot") {
    const s = shotById(action.shotId);
    return `Update shot “${s ? s.title : action.shotId}”`;
  }
  if (action.tool === "start_render") {
    if (action.shotIds && action.shotIds.length) {
      const names = action.shotIds.map((id) => {
        const s = shotById(id);
        return s ? s.title || id : id;
      });
      return `Start rendering ${names.join(", ")}`;
    }
    return "Start rendering every shot that needs it";
  }
  if (action.tool === "dub_shot") {
    const s = shotById(action.shotId);
    return `Generate dialogue for “${s ? s.title : action.shotId}”`;
  }
  if (action.tool === "stop_render") return "Stop the current render";
  if (action.tool === "assemble") return "Assemble the final cut";
  return action.tool;
}

function renderAssistantChat() {
  const host = $("#bladeMessages");
  if (!host) return;
  host.innerHTML = "";
  const turns = chatTurns();
  if (!turns.length) {
    const empty = el("div", "chat-empty");
    empty.append(
      el("strong", null, "Your board is already in context."),
      el("span", null, "Ask for a continuity review, explore a change, or describe a sequence to build. Edits arrive as proposals you can inspect before applying.")
    );
    host.appendChild(empty);
  }
  turns.forEach((turn) => {
    host.appendChild(el("div", `chat-message ${turn.role}${turn.pending ? " pending" : ""}${turn.error ? " error" : ""}`, turn.content));
    if (turn.actions && turn.actions.length && !turn.dismissed) {
      const proposal = el("div", `chat-proposal${turn.applied ? " applied" : ""}`);
      proposal.appendChild(el("div", "chat-proposal-head",
        turn.applied ? `${turn.actions.length} change${turn.actions.length === 1 ? "" : "s"} applied`
          : `${turn.actions.length} proposed change${turn.actions.length === 1 ? "" : "s"}`));
      const list = el("div", "chat-proposal-list");
      turn.actions.forEach((action) => {
        const row = el("div", "chat-proposal-item");
        row.appendChild(el("span", null, `• ${proposalSummary(action)}`));
        const fields = proposalDetailFields(action);
        if (fields && fields.length) {
          const detail = el("div", "chat-proposal-detail");
          detail.style.display = "none";
          fields.forEach(({ label, before, after }) => {
            const field = el("div", "chat-proposal-field");
            field.appendChild(el("div", "chat-proposal-field-label", label));
            const afterText = after === undefined || after === null || after === "" ? "(empty)" : String(after);
            if (before !== undefined && before !== after) {
              const beforeText = before === undefined || before === null || before === "" ? "(empty)" : String(before);
              field.appendChild(el("div", "chat-proposal-before", beforeText));
            }
            field.appendChild(el("div", "chat-proposal-after", afterText));
            detail.appendChild(field);
          });
          const view = el("button", "btn btn-sm btn-ghost", "View");
          view.addEventListener("click", () => {
            const showing = detail.style.display !== "none";
            detail.style.display = showing ? "none" : "block";
            view.textContent = showing ? "View" : "Hide";
          });
          row.appendChild(view);
          list.append(row, detail);
        } else {
          list.appendChild(row);
        }
      });
      proposal.appendChild(list);
      if (!turn.applied) {
        const buttons = el("div", "chat-proposal-actions");
        const apply = el("button", "btn btn-sm btn-primary", "Apply changes");
        apply.addEventListener("click", async () => {
          apply.disabled = true;
          try {
            const messages = await applyAssistantActions(turn.actions);
            turn.applied = true;
            persistChat(state.slug);
            render();
            renderAssistantChat();
            if (messages.length) messages.forEach((m) => toast(m));
            else toast("Storyboard changes applied.");
          } catch (err) {
            apply.disabled = false;
            toast(`Could not apply changes: ${err.message}`, "error");
          }
        });
        const discard = el("button", "btn btn-sm btn-ghost", "Discard");
        discard.addEventListener("click", () => {
          turn.dismissed = true;
          persistChat(state.slug);
          renderAssistantChat();
        });
        buttons.append(apply, discard);
        proposal.appendChild(buttons);
      }
      host.appendChild(proposal);
    }
  });
  host.scrollTop = host.scrollHeight;
  paintBladeContext();
}

function assistantId(prefix) {
  const tail = globalThis.crypto && crypto.randomUUID
    ? crypto.randomUUID().replaceAll("-", "").slice(0, 8)
    : Math.random().toString(16).slice(2, 10);
  return prefix + tail;
}

function assistantShot(fields) {
  const d = state.board.defaults || {};
  return Object.assign({
    id: assistantId("s"), title: "New shot", prompt: "", soundNote: "",
    characterIds: [], dialogue: "", dialogueSource: "recording", dialogueStyle: "",
    dialogueVoice: "", speakerId: "", dialogueAudioUrl: null, dubUrl: null,
    dubMode: "mix", startRef: null, endRef: null,
    referenceImages: [], model: STORYBOARD_MODEL,
    resolution: d.resolution || "960x544", frames: d.frames || 124,
    steps: d.steps || 8, seed: 0, status: "draft", reason: "", progress: 0,
    runtimeSeconds: null, outputs: [], validation: null, thumb: null, logUrl: null,
    renderedAs: null, renderFingerprint: null, dialogueSpokenText: "",
    dialogueSpokenStyle: "",
  }, fields || {});
}

function applyBoardEditActions(actions) {
  let lastAdded = null;
  actions.forEach((action) => {
    if (action.tool === "set_board_fields") Object.assign(state.board, action.fields);
    if (action.tool === "add_character") {
      state.board.characters.push(Object.assign({
        id: assistantId("c"), name: "", description: "", image: null,
        voice: null, voiceText: "",
      }, action.character));
    }
    if (action.tool === "update_character") {
      const target = (state.board.characters || []).find((c) => c.id === action.characterId);
      if (target) Object.assign(target, action.fields);
    }
    if (action.tool === "add_shot") {
      lastAdded = assistantShot(action.shot);
      state.board.shots.push(lastAdded);
    }
    if (action.tool === "update_shot") {
      const target = shotById(action.shotId);
      if (target) Object.assign(target, action.fields);
    }
  });
  if (!state.selectedId && lastAdded) state.selectedId = lastAdded.id;
  if (actions.length) markDirty();
}

// Actions with a real, side-effecting operation behind them, as opposed to a
// board-field edit. Kept as one set so applyAssistantActions can split a
// proposal into "apply these fields" and "then run these" without the two
// kinds of action needing to know about each other.
const OPERATION_TOOLS = new Set(["start_render", "dub_shot", "stop_render", "assemble"]);

/* Runs one operation action and returns a human sentence describing what
   happened, for the toast(s) shown after a proposal is applied. Shares the
   exact same runStartRender/runDubShot/runStopRender/runAssembleNow calls
   the UI's own buttons use — Storyboard AD triggering a render looks, to the
   server, identical to a click. */
async function runAssistantOperation(action) {
  if (action.tool === "start_render") {
    await runStartRender(action.shotIds);
    return action.shotIds && action.shotIds.length
      ? "Started rendering the requested shot(s)."
      : "Started rendering every shot that needs it.";
  }
  if (action.tool === "dub_shot") {
    const r = await runDubShot(action.shotId);
    if (r.warning) return r.warning;
    if (r.note) return r.note;
    return `Spoke the line (${r.seconds}s)${r.muxed ? " and mixed it onto the clip." : "."}`;
  }
  if (action.tool === "stop_render") {
    await runStopRender();
    return "Stopping — waiting for the current shot to wind down.";
  }
  if (action.tool === "assemble") {
    const res = await runAssembleNow();
    const f = res.finalVideo;
    return f.partial
      ? `Assembled ${f.parts.length} clip(s), but ${f.missing.length} shot(s) are not rendered.`
      : `Assembled ${f.parts.length} clip(s) — ${f.seconds}s.`;
  }
  return "";
}

/* Field edits apply first (in memory) so a render or dub proposed in the
   same turn picks up whatever was just changed — e.g. "update the dialogue
   for shot 2 and render it" should render the new line, not the old one. */
async function applyAssistantActions(actions) {
  const editActions = actions.filter((a) => !OPERATION_TOOLS.has(a.tool));
  const opActions = actions.filter((a) => OPERATION_TOOLS.has(a.tool));
  applyBoardEditActions(editActions);
  if (editActions.length) await saveNow();
  const messages = [];
  for (const action of opActions) {
    messages.push(await runAssistantOperation(action));
  }
  return messages.filter(Boolean);
}

const AD_THINKING_LINES = [
  "Flipping through the storyboard…",
  "Consulting the shot list…",
  "Reviewing the dailies…",
  "Checking continuity…",
  "Blocking the next shot…",
  "Calling action…",
  "Scouting the board…",
  "Marking up the script…",
  "Framing this up…",
  "Storyboarding some thoughts…",
  "Taking direction…",
  "Rolling camera…",
  "Slating this take…",
  "Panning for ideas…",
];

function randomADThinkingLine() {
  return AD_THINKING_LINES[Math.floor(Math.random() * AD_THINKING_LINES.length)];
}

/* Fire-and-forget: a reply that fails to speak is not worth blocking the
   chat over, so failures are a quiet toast rather than a broken turn. */
async function speakAssistantReply(text) {
  const audio = $("#bladeSpeechAudio");
  if (!audio || !state.slug || !(text || "").trim()) return;
  try {
    const result = await API.speak(state.slug, text);
    audio.src = result.audioUrl;
    await audio.play();
    if (result.note) toast(result.note);
  } catch (err) {
    toast(`Storyboard AD could not speak that reply: ${err.message}`, "error");
  }
}

async function sendAssistantMessage() {
  if (state.chatBusy || !state.board) return;
  const input = $("#bladeInput");
  const content = input.value.trim();
  if (!content) return;
  const svc = currentLLM();
  if (!svc || svc.id === "none" || !svc.healthy) {
    toast(svc && svc.message ? svc.message : "Choose a healthy Prompt rewriting model in Settings.", "error");
    return;
  }
  const turns = chatTurns();
  const history = turns.filter((t) => !t.pending && !t.error)
    .map((t) => ({role: t.role, content: t.content}));
  turns.push({role: "user", content});
  const pending = {role: "assistant", content: randomADThinkingLine(), pending: true};
  turns.push(pending);
  persistChat(state.slug);
  input.value = "";
  state.chatBusy = true;
  $("#bladeSend").disabled = true;
  renderAssistantChat();
  try {
    await saveNow();
    const result = await API.chat(state.slug, content, history, state.selectedId, svc.id);
    Object.assign(pending, {content: result.message, actions: result.actions || [], pending: false});
    if (state.speechEnabled) speakAssistantReply(result.message);
  } catch (err) {
    Object.assign(pending, {content: `Chat failed: ${err.message}`, pending: false, error: true});
  } finally {
    state.chatBusy = false;
    $("#bladeSend").disabled = false;
    persistChat(state.slug);
    renderAssistantChat();
    input.focus();
  }
}

function wireAssistantBlade() {
  const blade = $("#assistantBlade");
  const handle = $("#bladeHandle");
  let drag = null;
  let suppressClick = false;

  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const width = blade.getBoundingClientRect().width;
    drag = {startX: event.clientX, startOffset: state.bladeOpen ? 0 : width, width, moved: false};
    blade.classList.add("dragging");
    handle.setPointerCapture(event.pointerId);
  });
  handle.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const offset = Math.max(0, Math.min(drag.width, drag.startOffset + event.clientX - drag.startX));
    drag.offset = offset;
    drag.moved ||= Math.abs(event.clientX - drag.startX) > 4;
    blade.style.transform = `translateX(${offset}px)`;
  });
  handle.addEventListener("pointerup", (event) => {
    if (!drag) return;
    handle.releasePointerCapture(event.pointerId);
    suppressClick = true;
    const open = drag.moved ? (drag.offset == null ? drag.startOffset : drag.offset) < drag.width / 2 : !state.bladeOpen;
    drag = null;
    blade.classList.remove("dragging");
    setBladeOpen(open);
  });
  handle.addEventListener("click", (event) => {
    if (suppressClick && event.detail) {
      suppressClick = false;
      event.preventDefault();
      return;
    }
    setBladeOpen(!state.bladeOpen);
  });
  $("#bladeClose").addEventListener("click", () => setBladeOpen(false));
  $("#bladeNewChat").addEventListener("click", () => {
    if (state.slug) {
      state.chats[state.slug] = [];
      clearPersistedChat(state.slug);
    }
    renderAssistantChat();
  });
  $("#bladeComposer").addEventListener("submit", (event) => {
    event.preventDefault();
    sendAssistantMessage();
  });
  $("#bladeInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendAssistantMessage();
    }
  });
  window.addEventListener("resize", positionAssistantBlade);
  window.addEventListener("scroll", positionAssistantBlade, {passive: true});
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.bladeOpen) setBladeOpen(false);
  });
  const speechToggle = $("#bladeSpeechToggle");
  try { state.speechEnabled = localStorage.getItem(AD_SPEECH_STORAGE_KEY) === "1"; }
  catch { /* private browsing, storage disabled */ }
  speechToggle.checked = state.speechEnabled;
  speechToggle.addEventListener("change", (e) => {
    state.speechEnabled = e.target.checked;
    try { localStorage.setItem(AD_SPEECH_STORAGE_KEY, state.speechEnabled ? "1" : "0"); }
    catch { /* optional storage */ }
    if (!state.speechEnabled) $("#bladeSpeechAudio").pause();
  });
  $("#bladeSpeechVoice").addEventListener("change", (e) => {
    if (!state.board) return;
    state.board.defaults.adSpeakerId = e.target.value;
    markDirty();
  });

  positionAssistantBlade();
  renderAssistantChat();
}

/* ==========================================================================
   Boot
   ========================================================================== */

async function boot() {
  wireChrome();
  wireAssistantBlade();

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
    if (!boards.length) {
      const { slug, board } = await API.createBoard("My first storyboard");
      state.boards = [{ slug, name: board.name, shots: 0 }];
      setBoard(slug, board);
      return;
    }
    const remembered = lastOpenedSlug();
    if (remembered && boards.some((b) => b.slug === remembered)) {
      await openBoard(remembered);
    } else {
      // Nothing remembered (first visit, cleared storage, or the last
      // project is gone) — ask rather than guess which one to show.
      renderBoardPicker();
      await openDialog();
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

  // Ref2VA is used automatically, so its absence always matters. FL2VA is
  // dormant (never auto-selected) so a workspace without that checkpoint
  // should not be warned about a model nothing will ever request. Krea is
  // optional and reports its own problem when Create Stills is used. Wan
  // only matters when it is the selected video engine — a board that has
  // never opted into it should not be warned about a model it will never
  // render with.
  const requiredModels = [STORYBOARD_MODEL];
  const engineDefault = state.board && state.board.defaults && state.board.defaults.model;
  if (engineDefault === "wan-i2v" || engineDefault === "ltx-2.5") {
    requiredModels.push(engineDefault);
  }
  const unavailable = state.models.filter(
    (m) => requiredModels.includes(m.id) && !m.available
  );
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

// Remembered per browser, not per project: which storyboard to reopen next
// time this page loads. Deliberately not sent to the server — "last opened"
// is a fact about this browser tab, not about the project.
const LAST_OPENED_KEY = "storyboardToVideo.lastOpenedSlug";

function rememberLastOpened(slug) {
  try {
    localStorage.setItem(LAST_OPENED_KEY, slug);
  } catch {
    // Private browsing or storage disabled — reopening the same project
    // next time is a convenience, not something worth failing over.
  }
}

function lastOpenedSlug() {
  try {
    return localStorage.getItem(LAST_OPENED_KEY) || null;
  } catch {
    return null;
  }
}

function setBoard(slug, board, stale) {
  state.slug = slug;
  state.board = board;
  rememberLastOpened(slug);
  // Belongs to the board being installed, so switching boards cannot leave
  // the previous one's "changed since render" marks on screen.
  state.stale = stale || { shots: {}, final: "" };
  state.selectedId = board.shots.length ? board.shots[0].id : null;
  state.dirty = false;
  $("#saveState").textContent = "saved";
  render();
  if (state.bladeOpen) renderAssistantChat();
  refreshStatus();
}

/* ==========================================================================
   Chrome (header + rail controls)
   ========================================================================== */

/* The mechanical half of starting a render: check/auto-prepare dialogue,
   save, and queue it. Shared by the header Render button, a single shot's
   "Render this shot" button, and Storyboard AD's start_render action, so a
   render proposed in chat goes through exactly the same checks as one
   clicked in the UI. Throws (rather than failing silently) when a blocker
   needs manual attention, so a caller that cannot show its own dialog —
   the chat path — still surfaces that the render did not start. */
async function runStartRender(shotIds) {
  const targets = shotIds && shotIds.length
    ? shotIds.map((id) => shotById(id)).filter(Boolean)
    : shots();
  const blockers = targets
    .map((raw) => ({ raw, issue: dialogueReadiness(raw) }))
    .filter((entry) => entry.issue && entry.issue.blocking !== false);
  const manualBlockers = blockers.filter((entry) => !canAutoPrepareDialogue(entry.raw));
  if (manualBlockers.length) {
    showRenderDialogueBlocker(manualBlockers);
    throw new Error("Some shots need dialogue prepared manually before rendering.");
  }
  if (blockers.length) await prepareDialogueRecordings(blockers);
  await saveNow();
  state.status = await API.render(state.slug, shotIds && shotIds.length ? shotIds : undefined, state.board);
  state.awaitingBatch = true;
  state.followRender = true;
  startPolling();
  render();
}

async function runStopRender() {
  state.status = await API.stop();
  render();
}

async function runAssembleNow() {
  const res = await API.assemble(state.slug);
  takeStale(res);
  state.board.finalVideo = res.finalVideo;
  render();
  return res;
}

function wireChrome() {
  $("#btnRender").addEventListener("click", async () => {
    const btn = $("#btnRender");
    btn.disabled = true;
    let started = false;
    try {
      await runStartRender();
      started = true;
    } catch (err) {
      toast(err.message, "error");
    } finally {
      if (!started) {
        state.dialoguePreparing = false;
        btn.disabled = false;
      }
    }
  });

  $("#btnAssemble").addEventListener("click", async () => {
    const btn = $("#btnAssemble");
    btn.disabled = true;
    const was = btn.textContent;
    btn.textContent = "assembling…";
    try {
      const res = await runAssembleNow();
      const f = res.finalVideo;
      toast(
        f.partial
          ? `Assembled ${f.parts.length} clip(s), but ${f.missing.length} shot(s) are not rendered.`
          : `Assembled ${f.parts.length} clip(s) — ${f.seconds}s.`,
        f.partial ? "warn" : "info"
      );
    } catch (err) {
      toast(`Could not assemble: ${err.message}`, "error");
    } finally {
      btn.textContent = was;
      btn.disabled = false;
    }
  });

  $("#btnStop").addEventListener("click", async () => {
    try {
      await runStopRender();
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
  $("#btnDeleteBoard").addEventListener("click", deleteProject);
  $("#btnSaveDataDir").addEventListener("click", saveDataDirAndRestart);
  // Saved as soon as you leave the field or press Enter — no Save button.
  $("#searchUrlInput").addEventListener("change", saveSearchUrl);

  $("#settingsClose").addEventListener("click", () => {
    $("#settings").hidden = true;
  });
  // Click the backdrop, or press Escape, to close — same as the other dialogs.
  $("#settings").addEventListener("click", (e) => {
    if (e.target === $("#settings")) $("#settings").hidden = true;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#settings").hidden) $("#settings").hidden = true;
    if (e.key === "Escape" && !$("#helpDialog").hidden) $("#helpDialog").hidden = true;
  });

  // The logo doubles as a reference card for shot vocabulary used nowhere
  // else in the UI — there is no other place in the app to look this up.
  $("#btnHelp").addEventListener("click", () => {
    $("#helpDialog").hidden = false;
    $("#helpTabFraming").focus();
  });
  $("#btnHelp").addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      $("#helpDialog").hidden = false;
      $("#helpTabFraming").focus();
    }
  });
  const helpTabs = [...document.querySelectorAll(".help-tab")];
  function activateHelpTab(tab) {
    helpTabs.forEach((item) => {
      const selected = item === tab;
      item.setAttribute("aria-selected", String(selected));
      item.tabIndex = selected ? 0 : -1;
      document.getElementById(item.getAttribute("aria-controls")).hidden = !selected;
    });
    tab.focus();
  }
  helpTabs.forEach((tab, index) => {
    tab.addEventListener("click", () => activateHelpTab(tab));
    tab.addEventListener("keydown", (event) => {
      let nextIndex;
      if (event.key === "ArrowRight") nextIndex = (index + 1) % helpTabs.length;
      else if (event.key === "ArrowLeft") nextIndex = (index - 1 + helpTabs.length) % helpTabs.length;
      else if (event.key === "Home") nextIndex = 0;
      else if (event.key === "End") nextIndex = helpTabs.length - 1;
      else return;
      event.preventDefault();
      activateHelpTab(helpTabs[nextIndex]);
    });
  });
  document.querySelectorAll(".help-copy").forEach((button) => {
    button.addEventListener("click", async () => {
      const direction = button.closest(".help-example")?.querySelector("code")?.textContent?.trim();
      if (!direction) return;
      try {
        await navigator.clipboard.writeText(direction);
        button.textContent = "Copied";
        window.setTimeout(() => { button.textContent = "Copy direction"; }, 1600);
      } catch {
        button.textContent = "Clipboard unavailable";
        window.setTimeout(() => { button.textContent = "Copy direction"; }, 2000);
      }
    });
  });
  $("#helpClose").addEventListener("click", () => {
    $("#helpDialog").hidden = true;
  });
  $("#helpDialog").addEventListener("click", (e) => {
    if (e.target === $("#helpDialog")) $("#helpDialog").hidden = true;
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

  $("#btnBatchRender").addEventListener("click", openBatchDialog);
  $("#batchClose").addEventListener("click", () => ($("#batchDialog").hidden = true));
  $("#batchDialog").addEventListener("click", (e) => {
    if (e.target === $("#batchDialog")) $("#batchDialog").hidden = true;
  });
  $("#batchStart").addEventListener("click", startProjectBatch);

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
    // Preserve the quality tier when rotating/changing shape. Comparing the
    // short edge works for both landscape and portrait; comparing width would
    // jump a 544p landscape project to the largest portrait canvas.
    const opts = resolutionsByAspect()[e.target.value] || [];
    if (!opts.length) return;
    const [cw, ch] = projectResolution().split("x").map(Number);
    const currentShort = Math.min(cw, ch);
    const closest = opts.reduce((best, r) =>
      Math.abs(Math.min(...r.split("x").map(Number)) - currentShort) <
      Math.abs(Math.min(...best.split("x").map(Number)) - currentShort)
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

  $("#videoEngine").addEventListener("change", (e) => {
    const modelId = e.target.value || undefined;
    state.board.defaults.model = modelId;
    // Steps is a single project-wide field, not per-engine, and each
    // engine's useful range is wildly different (H3 is guidance-distilled,
    // 8 steps; Wan is not, wants ~40) -- so switching engines has to move
    // it, or it silently stays at whatever the PREVIOUS engine wanted,
    // which for H3 -> Wan means an unusably-low step count and for
    // Wan -> H3 means a pointlessly slow one.
    const cap = modelCap(modelId || STORYBOARD_MODEL);
    if (cap) state.board.defaults.steps = cap.defaultSteps;
    markDirty();
    render();
  });

  $("#draftToggle").addEventListener("change", (e) => {
    state.board.defaults.draft = e.target.checked;
    markDirty();
    render();
  });

  $("#sketchToggle").addEventListener("change", (e) => {
    state.board.defaults.sketch = e.target.checked;
    markDirty();
    render();
  });

  $("#stillsSize").addEventListener("change", (e) => {
    state.board.defaults.stillsSize = e.target.value;
    markDirty();
    render();
  });

  $("#stillsEngine").addEventListener("change", (e) => {
    if (e.target.value === "") return openStillsEditor("");
    state.board.defaults.stillsEngine = e.target.value;
    markDirty();
    paintStillsEngineManager();
  });

  // Blank means automatic (steps) / random (seed): stored as 0.
  const stillsNumber = (id, key, parse) => {
    $(id).addEventListener("input", (e) => {
      const v = e.target.value.trim();
      state.board.defaults[key] = v === "" ? 0 : parse(v);
      markDirty();
    });
    $(id).addEventListener("change", () => render());
  };
  stillsNumber("#stillsSteps", "stillsSteps", (v) => Math.min(50, Math.max(1, parseInt(v, 10) || 0)));
  stillsNumber("#stillsSeed", "stillsSeed", (v) => Math.max(0, parseInt(v, 10) || 0));
  $("#stillsSeedNote").addEventListener("click", (e) => {
    const seed = e.target.dataset && e.target.dataset.seed;
    if (!seed) return;
    state.board.defaults.stillsSeed = Number(seed);
    markDirty();
    render();
  });

  // A machine setting, not a board one: saved server-side, never markDirty().
  $("#stillsModel").addEventListener("change", async (e) => {
    const sel = e.target;
    try {
      const r = await API.setMfluxModel(sel.dataset.engine, sel.value);
      if (r.mflux && state.info) state.info.mflux = r.mflux;
    } catch (err) {
      toast(err.message, "error");
    }
    render();
  });

  $("#llmService").addEventListener("change", (e) => {
    if (e.target.value === "") return openLlmEditor("");
    state.board.defaults.llm = e.target.value;
    markDirty();
    paintLlmServiceManager();
  });
  $("#ttsEngine").addEventListener("change", (e) => {
    if (e.target.value === "") return openTtsEditor("");
    state.board.defaults.tts = e.target.value;
    markDirty();
    paintTtsServiceManager();
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
    main.appendChild(el("div", "open-path", b.configPath || `${b.slug}/storyboard.json`));
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

/* ==========================================================================
   Batch Render — several projects, one after another, each with its own
   saved settings. Meant for setting a night's worth of renders going at
   once rather than babysitting "Render all" project by project.
   ========================================================================== */

async function openBatchDialog() {
  const host = $("#batchList");
  $("#batchDialog").hidden = false;
  host.innerHTML = "";
  host.appendChild(el("div", "empty-state", "loading…"));
  try {
    state.boards = (await API.listBoards()).boards;
  } catch (err) {
    host.innerHTML = "";
    host.appendChild(el("div", "empty-state", `Could not list storyboards: ${err.message}`));
    return;
  }
  if (!state.batchSelection) state.batchSelection = new Set();
  if (!state.batchOrder) state.batchOrder = [];
  // Prune any selection/order left over from projects that no longer exist,
  // then append any new ones at the end so a fresh board always shows up.
  const known = new Set(state.boards.map((b) => b.slug));
  [...state.batchSelection].forEach((slug) => {
    if (!known.has(slug)) state.batchSelection.delete(slug);
  });
  state.batchOrder = state.batchOrder.filter((slug) => known.has(slug));
  state.boards.forEach((b) => {
    if (!state.batchOrder.includes(b.slug)) state.batchOrder.push(b.slug);
  });
  paintBatchList();
}

function paintBatchList() {
  const host = $("#batchList");
  host.innerHTML = "";
  if (!state.boards.length) {
    host.appendChild(el("div", "empty-state", "No storyboards yet — use New."));
    return;
  }
  wireBatchDragList(host);
  const bySlug = new Map(state.boards.map((b) => [b.slug, b]));
  state.batchOrder.forEach((slug, i) => {
    const b = bySlug.get(slug);
    if (!b) return;
    const row = el("div", "open-row batch-row");
    row.dataset.slug = b.slug;
    row.draggable = true;
    row.appendChild(el("span", "batch-num", String(i + 1)));

    const main = el("div", "open-main");

    const titleRow = el("label", "batch-row-label");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = state.batchSelection.has(b.slug);
    cb.addEventListener("change", () => {
      if (cb.checked) state.batchSelection.add(b.slug);
      else state.batchSelection.delete(b.slug);
    });
    titleRow.appendChild(cb);
    const title = el("div", "open-name");
    title.appendChild(el("span", null, b.name));
    if (b.slug === state.slug) title.appendChild(el("span", "open-tag", "open now"));
    titleRow.appendChild(title);
    main.appendChild(titleRow);

    main.appendChild(
      el("div", "open-meta",
         `${b.shots} shot${b.shots === 1 ? "" : "s"} · ` +
         `${b.rendered} rendered · ${relTime(b.updatedAt)}`)
    );
    row.appendChild(main);
    row.addEventListener("click", (e) => {
      if (e.target === cb) return;
      cb.checked = !cb.checked;
      cb.dispatchEvent(new Event("change"));
    });
    wireBatchDrag(row, b.slug);
    host.appendChild(row);
  });
}

const BATCH_DRAG_TYPE = "application/x-storyboard-batch-project";
let activeBatchDragSlug = null;

function moveBatchRow(dragSlug, targetSlug = null, after = false) {
  if (!dragSlug || !Array.isArray(state.batchOrder)) return false;
  const from = state.batchOrder.indexOf(dragSlug);
  if (from < 0) return false;

  let to = targetSlug ? state.batchOrder.indexOf(targetSlug) : state.batchOrder.length;
  if (to < 0) return false;
  if (targetSlug && after) to += 1;
  if (from < to) to -= 1;
  if (from === to) return false;

  const [moved] = state.batchOrder.splice(from, 1);
  state.batchOrder.splice(to, 0, moved);
  paintBatchList();
  return true;
}

function draggedBatchSlug(e) {
  return (
    e.dataTransfer.getData(BATCH_DRAG_TYPE) ||
    e.dataTransfer.getData("text/plain") ||
    activeBatchDragSlug
  );
}

function hasBatchDrag(e) {
  return (
    !!activeBatchDragSlug ||
    Array.from(e.dataTransfer.types || []).includes(BATCH_DRAG_TYPE)
  );
}

function wireBatchDrag(node, slug) {
  node.addEventListener("dragstart", (e) => {
    activeBatchDragSlug = slug;
    node.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData(BATCH_DRAG_TYPE, slug);
    e.dataTransfer.setData("text/plain", slug);
  });
  node.addEventListener("dragend", () => {
    activeBatchDragSlug = null;
    clearDropClasses($("#batchList"));
  });
  node.addEventListener("dragover", (e) => {
    if (!hasBatchDrag(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const after = dropAfter(node, "y", e);
    node.classList.add("drop-target");
    node.classList.toggle("drop-before", !after);
    node.classList.toggle("drop-after", after);
  });
  node.addEventListener("dragleave", () => {
    node.classList.remove("drop-target", "drop-before", "drop-after");
  });
  node.addEventListener("drop", (e) => {
    if (!hasBatchDrag(e)) return;
    e.preventDefault();
    e.stopPropagation();
    const dragSlug = draggedBatchSlug(e);
    const after = dropAfter(node, "y", e);
    activeBatchDragSlug = null;
    clearDropClasses($("#batchList"));
    if (!dragSlug || dragSlug === slug) return;
    moveBatchRow(dragSlug, slug, after);
  });
}

function wireBatchDragList(list) {
  if (list.dataset.dragListWired === "1") return;
  list.dataset.dragListWired = "1";
  list.addEventListener("dragover", (e) => {
    if (!hasBatchDrag(e)) return;
    if (e.target.closest(".batch-row")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    list.classList.add("dropping");
  });
  list.addEventListener("dragleave", (e) => {
    if (!list.contains(e.relatedTarget)) list.classList.remove("dropping");
  });
  list.addEventListener("drop", (e) => {
    if (!hasBatchDrag(e)) return;
    if (e.target.closest(".batch-row")) return;
    e.preventDefault();
    const dragSlug = draggedBatchSlug(e);
    activeBatchDragSlug = null;
    clearDropClasses(list);
    moveBatchRow(dragSlug);
  });
}

async function startProjectBatch() {
  const order = state.batchOrder || [];
  const slugs = order.filter((slug) => state.batchSelection && state.batchSelection.has(slug));
  if (!slugs.length) {
    toast("Select at least one project first.", "warn");
    return;
  }
  const btn = $("#batchStart");
  btn.disabled = true;
  try {
    await saveNow();
    state.status = await API.renderBatch(slugs);
    $("#batchDialog").hidden = true;
    state.awaitingBatch = true;
    startPolling();
    render();
    toast(`Batch started — ${slugs.length} project(s) queued.`, "info");
  } catch (err) {
    toast(err.message, "error");
  } finally {
    btn.disabled = false;
  }
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
  $("#btnDeleteBoard").disabled = !state.board || state.deletingBoard;
  paintDataDir();
  paintSearchUrl();
  initializeSettingsTabs();
  paintLlmServiceManager();
  paintTtsServiceManager();
  paintStillsEngineManager();
  renderSoundtrackSettings();
  refreshSoundtrackStatus();
  $("#settings").hidden = false;
  // Downloads finish while the app is open, so re-read what mflux has cached.
  API.info().then((i) => {
    if (state.info) { state.info.mflux = i.mflux || []; render(); }
  }).catch(() => {});
}

function initializeSettingsTabs() {
  const body = $(".settings-body");
  const panes = Object.fromEntries([...body.querySelectorAll("[data-settings-pane]")]
    .map((p) => [p.dataset.settingsPane, p]));
  body.querySelectorAll("section").forEach((section) => {
    const title = (section.querySelector(".card-heading-title")?.textContent || "").trim();
    let tab = "general";
    if (/Clip settings|Draft mode/i.test(title)) tab = "video";
    if (/Audio settings/i.test(title)) tab = "audio";
    if (/Create Image/i.test(title)) tab = "image";
    if (/^Soundtrack$/i.test(title)) tab = "soundtrack";
    panes[tab].appendChild(section);
  });
  document.querySelectorAll("[data-settings-tab]").forEach((button) => {
    button.onclick = () => {
      const active = button.dataset.settingsTab;
      document.querySelectorAll("[data-settings-tab]").forEach((b) => {
        const selected = b === button;
        b.classList.toggle("active", selected);
        b.setAttribute("aria-selected", String(selected));
      });
      Object.entries(panes).forEach(([name, pane]) => pane.classList.toggle("active", name === active));
    };
  });
  $("#btnNewLlmService").onclick = () => openLlmEditor("");
  $("#btnEditLlmService").onclick = () => {
    const id = $("#llmService").value;
    // Servers started before service definitions were sent can't be edited
    // from here; saving without them would drop the other services.
    if (!llmConfigs().some((c) => c.id === id)) {
      return toast("Restart the Storyboard server to edit this service.", "warn");
    }
    openLlmEditor(id);
  };
  $("#btnSaveLlmService").onclick = saveLlmServiceFromForm;
  $("#btnCancelLlmService").onclick = () => paintLlmServiceManager();
  $("#btnDeleteLlmService").onclick = deleteLlmService;
  $("#llmServiceAuth").onchange = (e) => { $("#llmServiceKeyWrap").hidden = !e.target.checked; };
  $("#llmServiceKind").onchange = onLlmKindChange;
  $("#btnNewTtsService").onclick = () => openTtsEditor("");
  $("#btnEditTtsService").onclick = () => {
    const id = $("#ttsEngine").value;
    // Servers started before engine definitions were sent can't be edited
    // from here; saving without them would drop the other engines.
    if (!ttsConfigs().some((c) => c.id === id)) {
      return toast("Restart the Storyboard server to edit this engine.", "warn");
    }
    openTtsEditor(id);
  };
  $("#btnSaveTtsService").onclick = saveTtsServiceFromForm;
  $("#btnCancelTtsService").onclick = () => paintTtsServiceManager();
  $("#btnDeleteTtsService").onclick = deleteTtsService;
  $("#ttsServiceKind").onchange = onTtsKindChange;
  $("#btnNewStillsEngine").onclick = () => openStillsEditor("");
  $("#btnEditStillsEngine").onclick = () => {
    const id = $("#stillsEngine").value;
    if (!mfluxConfigs().some((c) => c.id === id)) {
      return toast("Restart the Storyboard server to edit this engine.", "warn");
    }
    openStillsEditor(id);
  };
  $("#btnSaveStillsEngine").onclick = saveStillsEngineFromForm;
  $("#btnCancelStillsEngine").onclick = () => paintStillsEngineManager();
  $("#btnDeleteStillsEngine").onclick = deleteStillsEngine;
  $("#stillsEngPreset").onchange = onStillsPresetChange;
  $("#stillsEngModel").oninput = paintStillsEngineHelp;
  $("#llmServiceUrl").oninput = paintLlmKindHelp;
}

/* What each service type needs: its usual URL, whether it is a cloud API
   (and so needs a key), and where to find a model name that server accepts.
   Kept in step with KNOWN_KINDS / KIND_DEFAULT_URLS in server/llm.py. */
const LLM_KINDS = {
  "ollama": { url: "http://localhost:11434", model: "llama3.1:8b",
    urlHelp: "Where Ollama listens — http://localhost:11434 on this Mac, or http://&lt;ip&gt;:11434 for another machine.",
    modelHelp: "Run <code>ollama list</code> on the machine running Ollama and copy the NAME column exactly, tag included (e.g. <code>llama3.1:8b</code>). Get new models with <code>ollama pull &lt;name&gt;</code>." },
  "lmstudio": { url: "http://localhost:1234", model: "qwen2.5-7b-instruct",
    urlHelp: "Start the server in LM Studio's Developer tab — it listens on port 1234 by default. /v1 is added automatically.",
    modelHelp: "In LM Studio, load the model and copy its API identifier from the Developer tab (e.g. <code>qwen2.5-7b-instruct</code>). Every name the server accepts is listed at {models}." },
  "openai": { url: "", model: "model id",
    urlHelp: "The server's base address, e.g. http://localhost:8080. /v1 is added automatically.",
    modelHelp: "Must match an <code>id</code> listed at {models}. llama.cpp uses its <code>--alias</code> (or the model file name), vLLM its <code>--served-model-name</code>, mlx_lm.server the model path." },
  "openai-api": { cloud: true, url: "https://api.openai.com/v1", model: "gpt-5-mini",
    keyUrl: "https://platform.openai.com/api-keys",
    modelHelp: "An OpenAI model ID from <a href=\"https://platform.openai.com/docs/models\" target=\"_blank\" rel=\"noopener\">platform.openai.com/docs/models</a>, e.g. <code>gpt-5-mini</code>." },
  "anthropic": { cloud: true, url: "https://api.anthropic.com", model: "claude-opus-5",
    keyUrl: "https://console.anthropic.com/settings/keys",
    modelHelp: "A Claude model ID from <a href=\"https://docs.anthropic.com/en/docs/about-claude/models\" target=\"_blank\" rel=\"noopener\">Anthropic's model list</a>, e.g. <code>claude-opus-5</code> or <code>claude-haiku-4-5</code>. Needs the <code>anthropic</code> Python package on the Storyboard server." },
  "gemini": { cloud: true, url: "https://generativelanguage.googleapis.com/v1beta/openai", model: "gemini-2.5-flash",
    keyUrl: "https://aistudio.google.com/apikey",
    modelHelp: "A Gemini model ID from <a href=\"https://ai.google.dev/gemini-api/docs/models\" target=\"_blank\" rel=\"noopener\">ai.google.dev/gemini-api/docs/models</a>, e.g. <code>gemini-2.5-flash</code>." },
  "openrouter": { cloud: true, url: "https://openrouter.ai/api/v1", model: "meta-llama/llama-3.3-70b-instruct",
    keyUrl: "https://openrouter.ai/keys",
    modelHelp: "The <code>provider/model</code> ID shown on <a href=\"https://openrouter.ai/models\" target=\"_blank\" rel=\"noopener\">openrouter.ai/models</a>, e.g. <code>meta-llama/llama-3.3-70b-instruct</code>." },
  "groq": { cloud: true, url: "https://api.groq.com/openai/v1", model: "llama-3.3-70b-versatile",
    keyUrl: "https://console.groq.com/keys",
    modelHelp: "A model ID from <a href=\"https://console.groq.com/docs/models\" target=\"_blank\" rel=\"noopener\">console.groq.com/docs/models</a>, e.g. <code>llama-3.3-70b-versatile</code>." },
  "mistral": { cloud: true, url: "https://api.mistral.ai/v1", model: "mistral-small-latest",
    keyUrl: "https://console.mistral.ai/api-keys",
    modelHelp: "A model ID from <a href=\"https://docs.mistral.ai/getting-started/models/\" target=\"_blank\" rel=\"noopener\">Mistral's model list</a>, e.g. <code>mistral-small-latest</code>." },
};

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// Refresh the URL / model / key help for the chosen type. {models} becomes a
// link to the server's own model list, built from the URL as typed.
function paintLlmKindHelp() {
  const info = LLM_KINDS[$("#llmServiceKind").value] || LLM_KINDS.openai;
  const url = ($("#llmServiceUrl").value.trim() || info.url).replace(/\/+$/, "");
  $("#llmServiceUrl").placeholder = info.url || "http://localhost:8080";
  $("#llmServiceModel").placeholder = `e.g. ${info.model}`;
  $("#llmServiceUrlNote").innerHTML = info.cloud
    ? "Filled in for you — only change it if you use a proxy or a regional endpoint."
    : info.urlHelp;
  let modelsLink = "the server's <code>/v1/models</code> page";
  if (/^https?:\/\//.test(url)) {
    const list = /\/v\d+[a-z]*(\/openai)?$/.test(url) ? `${url}/models` : `${url}/v1/models`;
    modelsLink = `<a href="${escapeHtml(list)}" target="_blank" rel="noopener">${escapeHtml(list)}</a>`;
  }
  $("#llmServiceModelNote").innerHTML = info.modelHelp.replace("{models}", modelsLink) +
    " The name must match exactly — the tag next to the dropdown turns Online once the server finds it.";
  $("#llmServiceKeyHelp").innerHTML = info.keyUrl
    ? `Get a key at <a href="${info.keyUrl}" target="_blank" rel="noopener">${info.keyUrl.replace(/^https:\/\//, "")}</a>.`
    : "";
}

// Switching type: swap in the new type's usual URL unless you typed your own,
// and switch on the API key for cloud services.
function onLlmKindChange() {
  const info = LLM_KINDS[$("#llmServiceKind").value] || {};
  const urlInput = $("#llmServiceUrl");
  const defaults = Object.values(LLM_KINDS).map((k) => k.url);
  if (!urlInput.value.trim() || defaults.includes(urlInput.value.trim())) urlInput.value = info.url || "";
  if (info.cloud) {
    $("#llmServiceAuth").checked = true;
    $("#llmServiceKeyWrap").hidden = false;
  }
  paintLlmKindHelp();
}

/* LLM services share one dropdown with prompt rewriting: picking a service
   uses it for this project. "Edit" opens the chosen service's definition in
   the editor below; "+ Add" opens the same editor blank. */
function llmConfigs() {
  return (state.info?.llm?.configs || []).filter((s) => s.id !== "none");
}

function llmAddingNew() {
  const editor = $("#llmEditor");
  return !!editor && !editor.hidden && !editor.dataset.editing;
}

// Close the editor, dropping any unsaved "New service…" draft, and repaint
// the dropdown.
function paintLlmServiceManager() {
  const sel = $("#llmService");
  if (!sel) return;
  const editor = $("#llmEditor");
  editor.hidden = true;
  editor.dataset.editing = "";
  sel.querySelector("option[value='']")?.remove();
  render();
}

function openLlmEditor(id) {
  const adding = id === "";
  const service = llmConfigs().find((s) => s.id === id);
  const status = (state.info?.llm?.services || []).find((s) => s.id === id);
  const editor = $("#llmEditor");
  if (!service && !adding) {
    editor.hidden = true;
    editor.dataset.editing = "";
    return;
  }
  editor.hidden = false;
  editor.dataset.editing = service ? service.id : "";
  $("#llmEditorTitle").textContent = service ? `Edit “${service.label}”` : "New service";
  $("#llmServiceLabel").value = service?.label || "";
  $("#llmServiceKind").value = !service ? "ollama"
    : service.kind === "openai-compatible" ? "openai"
    : LLM_KINDS[service.kind] ? service.kind : "openai";
  $("#llmServiceUrl").value = service?.url || "";
  $("#llmServiceModel").value = service?.model || "";
  $("#llmServiceAuth").checked = !!service?.requiresKey;
  $("#llmServiceKeyWrap").hidden = !service?.requiresKey;
  $("#llmServiceKey").value = "";
  $("#btnDeleteLlmService").hidden = !service;
  $("#btnSaveLlmService").textContent = service ? "Save changes" : "Add service";
  if (adding) $("#llmServiceUrl").value = LLM_KINDS.ollama.url;
  paintLlmKindHelp();

  // Where the key comes from — never the key itself.
  const keyInput = $("#llmServiceKey");
  const keyNote = $("#llmServiceKeyNote");
  const envName = status?.keyEnv ? `$${status.keyEnv}` : "";
  keyInput.placeholder = status?.keySource === "settings"
    ? "A key is saved — paste a new one to replace it" : "Paste the API key";
  keyNote.className = "field-note";
  if (status?.keySource === "settings") {
    keyNote.textContent = "Using the key saved on this Mac (server-config.json, readable only by you).";
  } else if (status?.keySource === "env") {
    keyNote.textContent = `Using ${envName} from the environment. A key saved here takes priority.`;
  } else if (status?.keySource === "file") {
    keyNote.textContent = "Using a plain-text apiKey from llm-services.json — saving it here is safer and takes priority.";
    keyNote.className = "field-warn";
  } else {
    keyNote.textContent = envName ? `Paste a key, or set ${envName} before starting Storyboard.` : "";
  }
  if (adding) {
    // Stand-in entry so the dropdown says what is being edited.
    const sel = $("#llmService");
    let draft = sel.querySelector("option[value='']");
    if (!draft) { draft = el("option", null, "New service…"); draft.value = ""; sel.prepend(draft); }
    sel.value = "";
    $("#llmStatus").hidden = true;
    $("#llmNote").textContent = "Fill in the details, then Add service.";
    $("#llmNote").className = "field-note";
    $("#llmServiceLabel").focus();
  }
}

/* Speech engines use the same pattern as LLM services: the dropdown picks
   this project's engine, "Edit" opens the chosen engine's definition from
   tts-services.json, "+ Add" opens the same fields blank. Kept in step with
   KNOWN_KINDS in server/tts/__init__.py. */
const TTS_KINDS = {
  "qwen3-clone": { url: "http://127.0.0.1:8790",
    help: "Clones each character's voice from their reference clip, and can transcribe clips. Runs as a separate server — see vendor/README.md.",
    urlHelp: "Start it with <code>vendor/start-qwen3-tts.sh</code>; it listens on port 8790. It binds 127.0.0.1, so for another machine start it with <code>--host 0.0.0.0</code> and use <code>http://&lt;that-machine&gt;:8790</code>, or tunnel with <code>ssh -L 8790:127.0.0.1:8790 &lt;host&gt;</code>." },
  "plain-sherpa": { url: "http://127.0.0.1:3000",
    help: "One fixed voice, no cloning — any server that answers <code>POST /api/tts</code> with a WAV, such as a sherpa-onnx VITS voice.",
    urlHelp: "The server's base address; Storyboard calls <code>&lt;url&gt;/api/tts</code>." },
  "vpipe-moss": { url: "",
    help: "Runs MOSS-TTS 8B on this Mac through vpipe and clones each character's voice from their reference clip. No server needed — the model must be prepared in the vpipe workspace (setup/prepare-moss-tts.vpipeline)." },
};

function ttsConfigs() {
  return (state.info?.tts?.configs || []).filter((s) => s.id !== "none");
}

function ttsAddingNew() {
  const editor = $("#ttsEditor");
  return !!editor && !editor.hidden && !editor.dataset.editing;
}

function paintTtsServiceManager() {
  const sel = $("#ttsEngine");
  if (!sel) return;
  const editor = $("#ttsEditor");
  editor.hidden = true;
  editor.dataset.editing = "";
  sel.querySelector("option[value='']")?.remove();
  render();
}

function paintTtsKindHelp() {
  const info = TTS_KINDS[$("#ttsServiceKind").value] || TTS_KINDS["qwen3-clone"];
  $("#ttsServiceKindNote").innerHTML = info.help;
  $("#ttsServiceUrlWrap").hidden = !info.url;
  $("#ttsServiceUrl").placeholder = info.url;
  $("#ttsServiceUrlNote").innerHTML = info.urlHelp || "";
}

// Switching type swaps in its usual URL unless you typed your own.
function onTtsKindChange() {
  const info = TTS_KINDS[$("#ttsServiceKind").value] || {};
  const urlInput = $("#ttsServiceUrl");
  const defaults = Object.values(TTS_KINDS).map((k) => k.url);
  if (!urlInput.value.trim() || defaults.includes(urlInput.value.trim())) urlInput.value = info.url || "";
  paintTtsKindHelp();
}

function openTtsEditor(id) {
  const adding = id === "";
  const service = ttsConfigs().find((s) => s.id === id);
  const editor = $("#ttsEditor");
  if (!service && !adding) {
    editor.hidden = true;
    editor.dataset.editing = "";
    return;
  }
  editor.hidden = false;
  editor.dataset.editing = service ? service.id : "";
  $("#ttsEditorTitle").textContent = service ? `Edit “${service.label}”` : "New speech engine";
  const kind = TTS_KINDS[service?.kind] ? service.kind : "qwen3-clone";
  $("#ttsServiceLabel").value = service?.label || "";
  $("#ttsServiceKind").value = kind;
  $("#ttsServiceUrl").value = service ? service.url || "" : TTS_KINDS[kind].url;
  $("#btnDeleteTtsService").hidden = !service;
  $("#btnSaveTtsService").textContent = service ? "Save changes" : "Add engine";
  paintTtsKindHelp();
  if (adding) {
    const sel = $("#ttsEngine");
    let draft = sel.querySelector("option[value='']");
    if (!draft) { draft = el("option", null, "New speech engine…"); draft.value = ""; sel.prepend(draft); }
    sel.value = "";
    $("#ttsStatus").hidden = true;
    $("#ttsNote").textContent = "Fill in the details, then Add engine.";
    $("#ttsNote").className = "field-note";
    $("#ttsServiceLabel").focus();
  }
}

async function applyTtsServices(entries) {
  const result = await API.setTtsServices(entries);
  state.info.tts.engines = result.engines;
  state.info.tts.configs = result.configs;
  state.tts = result.engines;
  $("#ttsEngine").dataset.built = "";
  return result;
}

async function saveTtsServiceFromForm() {
  const button = $("#btnSaveTtsService");
  const editing = $("#ttsEditor").dataset.editing || "";
  const label = $("#ttsServiceLabel").value.trim();
  const kind = $("#ttsServiceKind").value;
  const url = TTS_KINDS[kind]?.url ? $("#ttsServiceUrl").value.trim() : "";
  if (!label || (TTS_KINDS[kind]?.url && !url)) return toast("Enter a name and server URL.", "warn");
  const others = ttsConfigs().filter((s) => s.id !== editing);
  let id = editing;
  if (!id) {
    const base = label.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "engine";
    id = base;
    for (let n = 2; others.some((s) => s.id === id) || id === "none"; n++) id = `${base}-${n}`;
  }
  // An edited engine keeps its place in the list; a new one goes last.
  const entry = { id, label, kind, url };
  const entries = ttsConfigs().map((s) => (s.id === editing ? entry : { ...s }));
  if (!editing) entries.push(entry);
  button.disabled = true;
  try {
    await applyTtsServices(entries);
    state.board.defaults.tts = id;
    markDirty();
    paintTtsServiceManager();
    toast(editing ? "Speech engine updated." : "Speech engine added.");
  } catch (err) { toast(err.message, "error"); }
  finally { button.disabled = false; }
}

async function deleteTtsService() {
  const id = $("#ttsEditor").dataset.editing;
  const service = ttsConfigs().find((s) => s.id === id);
  if (!service || !confirm(`Delete the speech engine “${service.label}”?`)) return;
  try {
    await applyTtsServices(ttsConfigs().filter((s) => s.id !== id).map((s) => ({ ...s })));
    if ((state.board.defaults.tts || "none") === id) {
      state.board.defaults.tts = "none";
      markDirty();
    }
    paintTtsServiceManager();
    toast("Speech engine deleted.");
  } catch (err) { toast(err.message, "error"); }
}

/* mflux still engines, same pattern again: the Engine dropdown picks this
   project's still engine; "Edit" opens an mflux engine's definition from
   mflux-engines.json (built-in vpipe engines have none, so Edit is off for
   them); "+ Add" opens the same fields blank. Model names are mflux's own
   aliases (`mflux-generate --help`); steps are typical values, not mflux
   defaults — the CLI publishes none. */
const MFLUX_PRESETS = {
  "z-image-turbo": { label: "Z-Image Turbo", command: "mflux-generate-z-image-turbo", model: "z-image-turbo", steps: 8,
    note: "Fast, distilled model — good previews in about 8 steps." },
  "krea2": { label: "Krea-2 Turbo", command: "mflux-generate-krea2", model: "krea2", steps: 8,
    note: "Fast, distilled model — good previews in about 8 steps." },
  "flux2-klein-4b": { label: "FLUX.2 klein 4B", command: "mflux-generate-flux2", model: "flux2-klein-4b", steps: 4,
    note: "Small, distilled FLUX.2 — quick in about 4 steps." },
  "schnell": { label: "FLUX.1 schnell", command: "mflux-generate", model: "schnell", steps: 4,
    note: "Distilled FLUX.1 — about 4 steps." },
  "dev": { label: "FLUX.1 dev", command: "mflux-generate", model: "dev", steps: 20,
    note: "Higher quality but slower (about 20 steps). Gated on Hugging Face: accept its licence there and set HF_TOKEN before starting Storyboard." },
  "krea-dev": { label: "FLUX.1 Krea dev", command: "mflux-generate", model: "krea-dev", steps: 20,
    note: "Photographic FLUX.1 variant, about 20 steps. Gated on Hugging Face: accept its licence there and set HF_TOKEN." },
  "qwen-image": { label: "Qwen-Image", command: "mflux-generate-qwen", model: "qwen-image", steps: 20,
    note: "Large 20B model — strong prompt following, needs lots of memory; about 20 steps." },
  "qwen-image-edit": { label: "Qwen-Image Edit (uses references)", command: "mflux-generate-qwen-edit", model: "qwen-image-edit", steps: 20,
    note: "Takes the Start Ref, character portraits and shot references as images, so cast look like their portraits. Large 20B model — slow, needs lots of memory." },
  "custom": { label: "Custom…", note: "Any mflux generator and model — a Hugging Face repo or a local folder. Generators ending in -edit are given reference images; the rest use a Start Ref as a starting image." },
};
// --base-model choices for a checkpoint that isn't one of mflux's aliases.
const MFLUX_FAMILIES = ["z-image-turbo", "z-image", "krea2", "schnell", "dev", "krea-dev",
  "flux2-klein-4b", "flux2-klein-9b", "qwen", "fibo", "ernie-image-turbo", "ernie-image", "ideogram4"];

// What an mflux engine is given, by its reference mode (MfluxEngine.references).
function mfluxReferenceNote(mode) {
  if (mode === "edit") {
    return "Uses the shot's prompt, scene and cast descriptions, plus up to 3 reference " +
      "images: the Start Ref, the selected characters' portraits, then the shot's Reference images.";
  }
  if (mode === "img2img") {
    return "Uses the shot's prompt, scene and cast descriptions. A hand-picked Start Ref " +
      "seeds the image; this model can't take character portraits, so cast are described in words.";
  }
  return "Uses the shot's prompt, scene and cast descriptions only — no reference images.";
}

function mfluxConfigs() {
  return state.info?.mfluxConfigs || [];
}

function stillsAddingNew() {
  const editor = $("#stillsEditor");
  return !!editor && !editor.hidden && !editor.dataset.editing;
}

function paintStillsEngineManager() {
  const sel = $("#stillsEngine");
  if (!sel) return;
  const editor = $("#stillsEditor");
  editor.hidden = true;
  editor.dataset.editing = "";
  sel.querySelector("option[value='']")?.remove();
  render();
}

function presetFor(command, model) {
  return Object.keys(MFLUX_PRESETS).find((k) =>
    MFLUX_PRESETS[k].command === command && MFLUX_PRESETS[k].model === model) || "custom";
}

function paintStillsEngineHelp() {
  const preset = MFLUX_PRESETS[$("#stillsEngPreset").value] || MFLUX_PRESETS.custom;
  $("#stillsEngPresetNote").textContent = preset.note || "";
  const model = $("#stillsEngModel").value.trim();
  const custom = model.includes("/");
  $("#stillsEngBaseWrap").hidden = !custom;
  $("#stillsEngModelNote").innerHTML =
    "The command is one of mflux's generators (installed with <code>uv tool install mflux</code>; " +
    "run <code>ls ~/.local/bin | grep mflux-generate</code> to list them). The model is an mflux name such as " +
    "<code>z-image-turbo</code> or <code>schnell</code>, a Hugging Face repo (<code>org/name</code>), or a local " +
    "folder. mflux downloads it the first time you create stills; downloaded copies then appear in the " +
    "Model list below.";
}

function onStillsPresetChange() {
  const preset = MFLUX_PRESETS[$("#stillsEngPreset").value];
  if (preset && preset.command) {
    $("#stillsEngCommand").value = preset.command;
    $("#stillsEngModel").value = preset.model;
    $("#stillsEngSteps").value = preset.steps;
  }
  // A name filled in from the previous preset follows the new one; a name
  // you typed yourself is left alone.
  const labelInput = $("#stillsEngLabel");
  const current = labelInput.value.trim();
  if (!current || current === labelInput.dataset.auto) {
    labelInput.value = preset && preset.command ? `${preset.label} via mflux` : "";
    labelInput.dataset.auto = labelInput.value;
  }
  paintStillsEngineHelp();
}

function openStillsEditor(id) {
  const adding = id === "";
  const engine = mfluxConfigs().find((e) => e.id === id);
  const editor = $("#stillsEditor");
  if (!engine && !adding) {
    editor.hidden = true;
    editor.dataset.editing = "";
    return;
  }
  const presetSel = $("#stillsEngPreset");
  if (!presetSel.options.length) {
    Object.entries(MFLUX_PRESETS).forEach(([k, p]) => {
      const o = el("option", null, p.label); o.value = k; presetSel.appendChild(o);
    });
    MFLUX_FAMILIES.forEach((f) => {
      const o = el("option", null, f); o.value = f; $("#stillsEngBase").appendChild(o);
    });
  }
  editor.hidden = false;
  editor.dataset.editing = engine ? engine.id : "";
  $("#stillsEditorTitle").textContent = engine ? `Edit “${engine.label}”` : "New mflux engine";
  const start = engine || { ...MFLUX_PRESETS["z-image-turbo"], label: "", quantize: 8 };
  $("#stillsEngLabel").value = engine ? engine.label || "" : "";
  $("#stillsEngLabel").dataset.auto = "";
  $("#stillsEngCommand").value = start.command || "";
  $("#stillsEngModel").value = start.model || "";
  $("#stillsEngSteps").value = start.steps || 8;
  $("#stillsEngQuantize").value = start.quantize ? String(start.quantize) : "";
  $("#stillsEngBase").value = engine?.baseModel || "z-image-turbo";
  presetSel.value = presetFor(start.command, start.model);
  $("#btnDeleteStillsEngine").hidden = !engine;
  $("#btnSaveStillsEngine").textContent = engine ? "Save changes" : "Add engine";
  paintStillsEngineHelp();
  if (adding) {
    const sel = $("#stillsEngine");
    let draft = sel.querySelector("option[value='']");
    if (!draft) { draft = el("option", null, "New mflux engine…"); draft.value = ""; sel.prepend(draft); }
    sel.value = "";
    $("#stillsStatus").hidden = true;
    $("#stillsModelWrap").hidden = true;
    $("#stillsEngineNote").textContent = "Pick a model family (or Custom), then Add engine.";
    $("#stillsEngineNote").className = "field-note";
    $("#stillsEngLabel").focus();
  }
}

async function applyMfluxEngines(entries) {
  const r = await API.setMfluxEngines(entries);
  state.info.models = r.models;
  state.models = r.models;
  state.info.mflux = r.mflux;
  state.info.mfluxConfigs = r.configs;
  $("#stillsEngine").dataset.built = "";
  return r;
}

async function saveStillsEngineFromForm() {
  const button = $("#btnSaveStillsEngine");
  const editing = $("#stillsEditor").dataset.editing || "";
  const label = $("#stillsEngLabel").value.trim();
  const command = $("#stillsEngCommand").value.trim();
  const model = $("#stillsEngModel").value.trim();
  const steps = Number($("#stillsEngSteps").value) || 8;
  const quantize = $("#stillsEngQuantize").value;
  if (!label || !command || !model) return toast("Enter a name, mflux command and model.", "warn");
  const taken = new Set([...state.models.map((m) => m.id), "auto", "none"]);
  let id = editing;
  if (!id) {
    const base = "mflux-" + (label.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")
      .replace(/^mflux-/, "").replace(/-via-mflux$/, "") || "engine");
    id = base;
    for (let n = 2; taken.has(id); n++) id = `${base}-${n}`;
  }
  const entry = { id, label, command, model, steps, quantize: quantize ? Number(quantize) : null,
    baseModel: model.includes("/") ? $("#stillsEngBase").value : "" };
  // An edited engine keeps its place in the list; a new one goes last.
  const entries = mfluxConfigs().map((e) => (e.id === editing ? entry : { ...e }));
  if (!editing) entries.push(entry);
  button.disabled = true;
  try {
    await applyMfluxEngines(entries);
    state.board.defaults.stillsEngine = id;
    markDirty();
    paintStillsEngineManager();
    toast(editing ? "Still engine updated." : "Still engine added.");
  } catch (err) { toast(err.message, "error"); }
  finally { button.disabled = false; }
}

async function deleteStillsEngine() {
  const id = $("#stillsEditor").dataset.editing;
  const engine = mfluxConfigs().find((e) => e.id === id);
  if (!engine || !confirm(`Delete the still engine “${engine.label}”?`)) return;
  try {
    await applyMfluxEngines(mfluxConfigs().filter((e) => e.id !== id).map((e) => ({ ...e })));
    if (state.board.defaults.stillsEngine === id) {
      state.board.defaults.stillsEngine = "auto";
      markDirty();
    }
    paintStillsEngineManager();
    toast("Still engine deleted.");
  } catch (err) { toast(err.message, "error"); }
}

async function applyLlmServices(entries) {
  const result = await API.setLlmServices(entries);
  state.info.llm.services = result.llm;
  state.info.llm.configs = result.configs;
  $("#llmService").dataset.built = "";
  return result;
}

async function saveLlmServiceFromForm() {
  const button = $("#btnSaveLlmService");
  const editing = $("#llmEditor").dataset.editing || "";
  const label = $("#llmServiceLabel").value.trim();
  const kind = $("#llmServiceKind").value;
  const url = $("#llmServiceUrl").value.trim();
  const model = $("#llmServiceModel").value.trim();
  const requiresKey = $("#llmServiceAuth").checked;
  const key = $("#llmServiceKey").value.trim();
  if (!label || !url || !model) return toast("Enter a name, server URL and model.", "warn");
  const others = llmConfigs().filter((s) => s.id !== editing);
  let id = editing;
  if (!id) {
    const base = label.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "service";
    id = base;
    for (let n = 2; others.some((s) => s.id === id) || id === "none"; n++) id = `${base}-${n}`;
  }
  // An edited service keeps its place in the list; a new one goes last.
  const entry = { id, label, kind, url, model, requiresKey };
  const entries = llmConfigs().map((s) => (s.id === editing ? entry : { ...s }));
  if (!editing) entries.push(entry);
  button.disabled = true;
  try {
    await applyLlmServices(entries);
    if (requiresKey && key) {
      const r = await API.setLlmKey(id, key);
      if (r.llm) state.info.llm.services = r.llm;
    } else if (!requiresKey) {
      const r = await API.setLlmKey(id, "");
      if (r.llm) state.info.llm.services = r.llm;
    }
    state.board.defaults.llm = id;
    markDirty();
    paintLlmServiceManager();
    toast(editing ? "LLM service updated." : "LLM service added.");
  } catch (err) { toast(err.message, "error"); }
  finally { button.disabled = false; }
}

async function deleteLlmService() {
  const id = $("#llmEditor").dataset.editing;
  const service = llmConfigs().find((s) => s.id === id);
  if (!service || !confirm(`Delete the LLM service “${service.label}”?`)) return;
  try {
    await applyLlmServices(llmConfigs().filter((s) => s.id !== id).map((s) => ({ ...s })));
    if ((state.board.defaults.llm || state.info.llm.default) === id) {
      state.board.defaults.llm = "none";
      markDirty();
    }
    paintLlmServiceManager();
    toast("LLM service deleted.");
  } catch (err) { toast(err.message, "error"); }
}

function paintDataDir() {
  const info = state.info || {};
  $("#dataDirInput").value = info.dataDir || "";
  const warn = $("#dataDirWarn");
  const overridden = info.dataDirSource === "cli" || info.dataDirSource === "env";
  warn.textContent = overridden
    ? "This server was started with --data-dir or SBV_DATA_DIR set, which " +
      "always wins — saving a new folder here won't take effect until " +
      "that flag/variable is removed from however this server is launched."
    : "";
  warn.classList.toggle("field-warn", overridden);
}

function paintSearchUrl() {
  const search = (state.info && state.info.search) || {};
  $("#searchUrlInput").value = search.url || "";
  const warn = $("#searchUrlWarn");
  warn.textContent = search.overridden
    ? "This server was started with SBV_SEARCH_URL set, which always wins — " +
      "saving a URL here won't take effect until that variable is removed " +
      "from however this server is launched."
    : "";
  warn.classList.toggle("field-warn", !!search.overridden);
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
  // Projects live under the data dir, not the vpipe workspace — those can
  // (and by default now do) differ.
  const root = (state.info && state.info.dataDir) || "<projects folder>";
  const note = $("#projPath");
  note.textContent = `${root}/${slug}/`;
  note.classList.toggle("field-warn", slug !== state.slug);
  if (slug !== state.slug) {
    note.textContent += "   ← the folder moves here";
  }
}

async function deleteProject() {
  if (!state.board || state.deletingBoard) return;
  const { slug, board } = state;
  if (state.dialoguePreparing || state.status?.busy || state.status?.stills?.busy) {
    toast("Wait for rendering or dialogue preparation to finish before deleting a storyboard.", "warn");
    return;
  }
  const typed = prompt(
    `Delete storyboard “${board.name}”?\n\nThis removes its scene descriptions and settings. Rendered videos and reference files are kept.\n\nType the exact storyboard name to continue:`,
    ""
  );
  if (typed === null) return;
  if (typed !== board.name) {
    toast("Name did not match. Nothing was deleted.", "warn");
    return;
  }
  if (!confirm(`Permanently delete storyboard “${board.name}”?\n\nThis cannot be undone in the app. Videos and reference files will remain on disk.`)) return;
  state.deletingBoard = true;
  const btn = $("#btnDeleteBoard");
  btn.disabled = true;
  clearTimeout(state.saveTimer);
  try {
    // Let already-sent saves finish before removing the board.
    await Promise.allSettled([...state.pendingSaves]);
    await API.deleteBoard(slug, typed);
    stopPolling();
    state.slug = null;
    state.board = null;
    state.dirty = false;
    try { localStorage.removeItem(LAST_OPENED_KEY); } catch { /* optional storage */ }
    clearPersistedChat(slug);
    window.location.reload();
  } catch (err) {
    state.deletingBoard = false;
    btn.disabled = false;
    toast(`Delete failed: ${err.message}`, "error");
    if (state.dirty) markDirty();
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
    const oldSlug = state.slug;
    const r = await API.renameBoard(state.slug, name);
    state.slug = r.slug;
    state.board = r.board;
    // Renaming moves the project's folder, and the chat's storage key
    // follows it — otherwise a refresh would find no history under the new
    // slug and stale history sitting orphaned under the old one.
    if (r.slug !== oldSlug) {
      state.chats[r.slug] = state.chats[oldSlug] || loadPersistedChat(oldSlug);
      delete state.chats[oldSlug];
      persistChat(r.slug);
      clearPersistedChat(oldSlug);
    }
    state.boards = (await API.listBoards()).boards;
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

async function saveDataDirAndRestart() {
  const path = $("#dataDirInput").value.trim();
  if (!path) return;
  if (
    !confirm(
      `Save "${path}" as the Storyboard data folder and restart the server ` +
        `now?\n\nThis briefly drops every connection to this app (any open ` +
        `tab). Projects are read directly from "${path}/". Existing project ` +
        `files are not moved automatically.`
    )
  ) {
    return;
  }

  const btn = $("#btnSaveDataDir");
  btn.disabled = true;
  const was = btn.textContent;
  try {
    btn.textContent = "saving…";
    await API.setDataDir(path);
    btn.textContent = "restarting…";
    await API.restartServer();

    // The process is re-exec'ing itself: connections drop for a moment, then
    // it comes back up serving the new folder. Poll rather than reload
    // immediately, since an instant reload would just hit the gap and show
    // the browser's own connection-refused page.
    const deadline = Date.now() + 20000;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 700));
      try {
        await API.info();
        window.location.reload();
        return;
      } catch {
        // still restarting — keep polling
      }
    }
    toast(
      "The server didn't come back within 20s — check the terminal it's " +
        "running in, then reload this page.",
      "error"
    );
  } catch (err) {
    toast(`Could not save the Storyboard data folder: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = was;
  }
}

async function saveSearchUrl() {
  const url = $("#searchUrlInput").value.trim();
  if (url === ((state.info && state.info.search && state.info.search.url) || "")) return;
  try {
    const r = await API.setSearchUrl(url);
    if (state.info) {
      state.info.search = { url: r.searchUrl, overridden: false };
    }
    paintSearchUrl();
    toast(url ? "Web search enabled for the Storyboard AD." : "Web search disabled.");
  } catch (err) {
    toast(`Could not save the search URL: ${err.message}`, "error");
  }
}

/* ==========================================================================
   System load (Details card)
   ========================================================================== */

// CPU / GPU / memory %, as a second column beside the shot's own stats.
// Polled on its own slow timer, only while a Details column is on screen and
// the tab is visible, and written into the existing cells in place — the
// render poll rebuilds this panel, and a rebuild must not wait on a fetch.
const SYSTEM_LOAD_MS = 2000;
const SYSTEM_LOAD_ROWS = [
  ["cpu", "CPU"],
  ["gpu", "GPU"],
  ["memory", "Memory"],
  ["powerW", "Power (1-min avg)"],
];

function systemLoadText(key, load) {
  const v = load && load[key];
  if (v == null) return "—";
  if (key === "powerW") return `${Math.round(v)} W`;
  const pct = `${Math.round(v)}%`;
  return key === "memory" && load.memoryTotalGB
    ? `${pct} (${load.memoryUsedGB} / ${load.memoryTotalGB} GB)`
    : pct;
}

function paintSystemLoad() {
  document.querySelectorAll(".sys-load dd[data-load]").forEach((dd) => {
    const key = dd.dataset.load;
    dd.textContent = systemLoadText(key, state.systemLoad);
    const v = state.systemLoad && state.systemLoad[key];
    // Watts have no fixed ceiling to colour against; only percentages do.
    dd.dataset.level = v == null || key === "powerW" ? "" : v >= 90 ? "high" : v >= 70 ? "mid" : "";
  });
}

function systemLoadGrid() {
  const grid = el("dl", "stat-grid sys-load");
  grid.title = "This machine, updated every 2 seconds. Power is the whole machine's draw at the wall: macOS refreshes it about once a minute, so it is that minute's average.";
  SYSTEM_LOAD_ROWS.forEach(([key, label]) => {
    grid.appendChild(el("dt", null, label));
    const dd = el("dd", null, systemLoadText(key, state.systemLoad));
    dd.dataset.load = key;
    grid.appendChild(dd);
  });
  if (!state.systemLoadTimer) {
    state.systemLoadTimer = setInterval(refreshSystemLoad, SYSTEM_LOAD_MS);
    setTimeout(refreshSystemLoad, 0);
  }
  return grid;
}

async function refreshSystemLoad() {
  if (document.hidden || !document.querySelector(".sys-load")) return;
  if (state.systemLoadBusy) return;
  state.systemLoadBusy = true;
  try {
    state.systemLoad = await API.systemLoad();
  } catch {
    state.systemLoad = null;  // shown as "—"; the next tick tries again
  } finally {
    state.systemLoadBusy = false;
  }
  paintSystemLoad();
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
    const wasStillsBusy = !!(state.status && state.status.stills && state.status.stills.busy);
    const wasProjectBatch = !!(state.status && state.status.projectBatch && state.status.projectBatch.active);
    state.status = s;

    const stillsBusy = !!(s.stills && s.stills.busy);
    if ((s.busy || stillsBusy) && !state.poll) startPolling();

    // Advance the Output panel scene to scene with the orchestrator's own
    // idea of what it is rendering, rather than leaving it on whatever shot
    // happened to be selected when the batch started.
    if (s.busy && state.followRender && s.currentShotId &&
        s.currentShotId !== state.selectedId && shotById(s.currentShotId)) {
      state.selectedId = s.currentShotId;
    }

    if (!s.busy && (wasBusy || state.awaitingBatch)) {
      state.awaitingBatch = false;
      if (!stillsBusy) stopPolling();
      // the server has been mutating the board as shots finish; re-read it
      const res = await API.getBoard(state.slug);
      takeStale(res);
      state.board = res.board;
      const done = wasProjectBatch ? projectBatchOutcome(s) : batchOutcome(s);
      toast(done.msg, done.kind);
    }
    if (wasStillsBusy && !stillsBusy) {
      if (!s.busy) stopPolling();
      // the stills, once done, are saved onto the shot server-side
      const res = await API.getBoard(state.slug);
      takeStale(res);
      state.board = res.board;
      toast(
        s.stills.error ? `Image failed: ${s.stills.error}` : "Image ready.",
        s.stills.error ? "error" : "info"
      );
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
  const st = state.status && state.status.stills;
  return JSON.stringify([
    busy,
    state.selectedId,
    st ? [st.busy, st.shotId, st.phase, st.error] : null,
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
        raw.stills ? Object.keys(raw.stills).join(",") : "",
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
  const stillsBusy = !!(state.status && state.status.stills && state.status.stills.busy);
  $("#btnRender").disabled = busy || stillsBusy || state.dialoguePreparing || !state.info.backend.healthy;
  $("#btnBatchRender").disabled = busy || stillsBusy || !state.info.backend.healthy;
  $("#btnAssemble").disabled = busy || stillsBusy;
  $("#btnStop").disabled = !busy && !stillsBusy;
  paintMeta();
  paintRenderHint();

  shots().forEach((raw) => {
    const shot = view(raw);
    const pctOnly = `${Math.round(shot.progress || 0)}%`;
    const pct = `${pctOnly}${etaSuffix(shot)}`;

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
          `Rendering — ${pctOnly}${shot.phase ? ` (${shot.phase})` : ""}${etaSuffix(shot)}`;
      }
      const rt = document.querySelector('#preview .stat-grid dt + dd');
      if (rt) rt.textContent = STATUS_LABELS[shot.status] || shot.status;
      paintLog(shot);
      paintStillsLog(raw);
      paintStageTiles(raw, shot);
    }
  });
}

// vpipe prints a progress line only every 10% of a phase, so a long denoise
// can leave the log still for ten minutes or more while the GPU is flat out.
// While the engine is quiet, a pinned line at the top of Backend output says
// where it last was, an estimate of where it is now, and when the next line
// is due — so a quiet log reads as "working", not "stuck".
const HEARTBEAT_QUIET_S = 30;

function heartbeatText(shot) {
  const hb = shot.heartbeat;
  if (shot.status !== "running" || !hb || !hb.serverTime || !hb.lastOutputAt) return "";
  const quiet = hb.serverTime - hb.lastOutputAt;
  if (quiet < HEARTBEAT_QUIET_S) return "";
  const phase = shot.phase || "render";
  const parts = [`${phase} still running — no engine output for ${dur(quiet)}`];
  const pct = hb.phasePercent;
  if (pct != null && hb.phaseReportedAt) {
    const since = hb.serverTime - hb.phaseReportedAt;
    parts.push(`last reported ${Math.round(pct)}% ${dur(since)} ago`);
    const elapsed = hb.phaseReportedAt - (hb.phaseStartedAt || hb.phaseReportedAt);
    if (pct > 0 && pct < 100 && elapsed > 0) {
      const perPct = elapsed / pct;
      const nextIn = perPct * (Math.floor(pct / 10) * 10 + 10 - pct) - since;
      const now = Math.min(pct + since / perPct, Math.floor(pct / 10) * 10 + 9.9);
      parts.push(`~${Math.round(now)}% now`);
      parts.push(nextIn > 0 ? `next report in ~${dur(nextIn)}` : "next report due any moment");
    }
  }
  const gpu = state.systemLoad && state.systemLoad.gpu;
  if (gpu != null) parts.push(`GPU ${Math.round(gpu)}%`);
  return `⏳ ${parts.join(" · ")} (the engine reports every 10%)`;
}

function paintHeartbeat(box, shot) {
  let hb = box.querySelector(".log-heartbeat");
  const text = heartbeatText(shot);
  if (!text) { if (hb) hb.remove(); return; }
  if (!hb) {
    hb = el("div", "log-heartbeat");
    box.insertBefore(hb, box.firstChild);
  }
  if (hb.textContent !== text) hb.textContent = text;
}

function paintLog(shot) {
  const box = $("#preview .log");
  const lines = shot.log;
  if (!box || !lines) return;
  paintHeartbeat(box, shot);
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

  // Newest first, so the latest status is the one already in view — each new
  // line goes in right above the previous newest, not at the end.
  const atTop = box.scrollTop <= 24;
  lines.slice(have).forEach((e) => part.insertBefore(logLine(e), part.firstChild));
  part.dataset.count = String(lines.length);

  // Hold the same window renderPreview draws, so the two agree. Oldest is
  // now the tail end, so that is what falls off.
  let extra = part.querySelectorAll(".log-line").length - LOG_WINDOW;
  while (extra-- > 0 && part.lastChild) part.removeChild(part.lastChild);

  if (atTop) box.scrollTop = 0;
}

/* Same incremental-append shape as paintLog, for the separate stills job's
   log — it lives on state.status.stills, not on the shot's own run. */
function paintStillsLog(raw) {
  const box = $("#preview .log");
  const st = state.status && state.status.stills;
  if (!box || !st || st.shotId !== raw.id) return;
  const lines = st.log || [];
  const part = box.querySelector('[data-part="stills"]');
  if (!part) return;

  const have = Number(part.dataset.count || 0);
  if (lines.length <= have) return;

  if (!have) {
    part.innerHTML = "";
    part.appendChild(logLine("# stills preview"));
  }

  // The header (above) always stays first; new lines go in right after it,
  // newest on top of the ones already there — not at the very top of the
  // container, which would shove the header down. The anchor is read fresh
  // each time (not cached) so a batch of several new lines still lands in
  // the right order relative to each other, the same trick paintLog uses.
  const atTop = box.scrollTop <= 24;
  lines.slice(have).forEach((e) =>
    part.insertBefore(logLine(e), part.firstChild.nextSibling)
  );
  part.dataset.count = String(lines.length);

  let extra = part.querySelectorAll(".log-line").length - LOG_WINDOW;
  while (extra-- > 0 && part.lastChild) part.removeChild(part.lastChild);

  if (atTop) box.scrollTop = 0;
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
  const stillsBusy = !!(state.status && state.status.stills && state.status.stills.busy);
  $("#btnRender").disabled = busy || stillsBusy || state.dialoguePreparing || !state.info.backend.healthy;
  $("#btnBatchRender").disabled = busy || stillsBusy || !state.info.backend.healthy;
  $("#btnAssemble").disabled = busy || stillsBusy;
  $("#btnStop").disabled = !busy && !stillsBusy;

  paintMeta();
  paintRenderHint();
  paintBladeContext();
  positionAssistantBlade();
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
  const blockers = renderDialogueBlockers();
  const autoCount = blockers.filter((entry) => canAutoPrepareDialogue(entry.raw)).length;
  const manualCount = blockers.length - autoCount;
  const blockerHint = manualCount
    ? ` ${manualCount} shot${manualCount === 1 ? "" : "s"} need dialogue setup before rendering.`
    : autoCount
    ? ` ${autoCount} missing or stale dialogue take${autoCount === 1 ? "" : "s"} will be generated automatically before rendering.`
    : "";
  $("#btnRender").title = pending.length
    ? `Renders ${pending.length} of ${shots().length} shot(s)` +
      (changed
        ? `, ${changed} of them because the board changed since they were rendered`
        : "") +
      ", then joins every clip into the final video. A shot already rendered " +
      "from what the board says now is left alone." + blockerHint
    : "Every shot is already a render of what the board says now — this just " +
      "joins the clips into the final video." + blockerHint;
}

function paintMeta() {
  const counts = shots().reduce((a, s) => {
    const st = view(s).status;
    a[st] = (a[st] || 0) + 1;
    return a;
  }, {});
  const pending = pendingShots().length;
  const changed = shots().filter((raw) => !!staleWhy(raw.id)).length;
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
  ensureSceneWand();
  ensureSoundscapeWand();
  const sndMode = $("#soundscapeInShots");
  sndMode.checked = state.board.soundscapeInShots !== false;
  sndMode.title = sndMode.checked
    ? "The background sound effects are included in every shot render."
    : "Background sound effects are off for rendering. Only per-shot sound accents are included.";

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
  if (!groups[currentAspect]) groups[currentAspect] = [];
  if (!groups[currentAspect].includes(current)) groups[currentAspect].push(current);

  const aspSel = $("#projAspect");
  aspSel.innerHTML = "";
  Object.keys(groups).forEach((a) => {
    const o = el("option", null, aspectLabel(a));
    o.value = a;
    if (a === currentAspect) o.selected = true;
    aspSel.appendChild(o);
  });

  const resSel = $("#projResolution");
  resSel.innerHTML = "";
  (groups[currentAspect] || []).forEach((r) => {
    const o = el("option", null, frameSizeLabel(r, groups[currentAspect]));
    o.value = r;
    if (r === current) o.selected = true;
    resSel.appendChild(o);
  });

  // H3-Base is the local generator. H3's advertised 2K output is produced by
  // a separate regeneration service which this backend does not include.
  const noteHost = $("#resNoteField");
  noteHost.innerHTML = "";
  const supported = allResolutions().includes(current);
  noteHost.appendChild(el(
    "div",
    supported ? "field-note" : "field-warn",
    supported
      ? "H3-Base canvas. The largest option for each ratio uses the official 768px short edge; smaller canvases render faster. 2K requires H3-Regenerate-2K, which is not installed locally."
      : `⚠ ${current.replace("x", " × ")} is a legacy custom size. Choose an H3-Base size before the next render.`
  ));

  // draft mode
  const dt = $("#draftToggle");
  const draftOn = !!state.board.defaults.draft;
  dt.checked = draftOn;
  const [cw, ch] = current.split("x").map(Number);
  const cap = modelCap(effectiveShotModel(selectedShot())) || state.models[0] || null;
  const [dw, dh] = draftGeometry(cw, ch, cap ? cap.sizeAlign : 16);
  $("#draftNote").textContent = draftOn
    ? `Rendering at ${dw}×${dh} and 4 steps, dialogue and sound effects ` +
      `included. Clip length and seed are unchanged, so the camera move is ` +
      `the one you will get.`
    : `Drafts render at ${dw}×${dh} and 4 steps to check framing, motion, ` +
      `dialogue and sound quickly. Full frame dumps are skipped unless ` +
      `needed for chaining.`;

  // sketch preview — a draft-only sub-option, so it is disabled without one
  const skt = $("#sketchToggle");
  const sketchOn = !!state.board.defaults.sketch;
  skt.checked = sketchOn;
  skt.disabled = !draftOn;
  $("#sketchToggleWrap").classList.toggle("disabled", !draftOn);
  const minFrames = cap && cap.frameRule ? cap.frameRule.minimum : 39;
  $("#sketchNote").textContent = !draftOn
    ? `Only applies while Draft mode is on.`
    : sketchOn
    ? `Rendering just ${minFrames} frames as a rough pencil-sketch, silent, ` +
      `then holding them to fill the shot's full length — real motion and ` +
      `camera move from the actual model, sampled far more coarsely, at a ` +
      `fraction of the time.`
    : `Renders only ${minFrames} frames — a rough pencil-sketch pass, ` +
      `silent — then stretches them to the shot's full length by holding ` +
      `frames, so composition and camera move are cheap to check before a ` +
      `full draft.`;

  // video engine — a project-wide override of automatic Ref2VA routing (see
  // effectiveShotModel). Only alternate ENGINES belong here; H3's own mode
  // stays implicit and is always Ref2VA.
  const engineSel = $("#videoEngine");
  if (engineSel.dataset.built !== "1") {
    const auto = el("option", null, "Automatic (MiniMax H3 — Ref2VA)");
    auto.value = "";
    engineSel.appendChild(auto);
    state.models
      .filter((m) => m.id === "wan-i2v" || m.id === "ltx-2.5")
      .forEach((m) => {
        const o = el("option", null, m.available ? m.label : `${m.label} — unavailable`);
        o.value = m.id;
        engineSel.appendChild(o);
      });
    engineSel.dataset.built = "1";
  }
  // "ref2va" is Store.migrate()'s concretized default.setdefault("model", ...)
  // value on the server -- it round-trips back from a save/load exactly like
  // "" (automatic) since effectiveShotModel treats both identically, but the
  // select only has "" and "wan-i2v" options, so it must be normalized back
  // to "" here or the dropdown renders blank after a reload.
  const rawEngine = state.board.defaults.model || "";
  const chosenEngine = rawEngine === "ref2va" ? "" : rawEngine;
  engineSel.value = chosenEngine;
  const engineNote = $("#videoEngineNote");
  const engineCap = chosenEngine ? modelCap(chosenEngine) : null;
  if (!chosenEngine) {
    engineNote.textContent =
      "Every shot renders on Ref2VA automatically. Start/End frame anchors are sent as ordered references, never hard-pinned frames.";
    engineNote.className = "field-note";
  } else if (!engineCap || !engineCap.available) {
    engineNote.textContent =
      (engineCap && engineCap.unavailableReason) || `${chosenEngine} is not prepared in this workspace.`;
    engineNote.className = "field-warn";
  } else {
    engineNote.textContent =
      `Every shot renders on ${engineCap.label.split("—")[0].trim()} instead, overriding the ` +
      "automatic Ref2VA routing above — including shots with a Start/End anchor." +
      (engineCap.supportsAudio ? "" : " Silent: no dialogue or sound effects are generated; " +
        "use a separate dialogue recording for any shot that speaks.");
    engineNote.className = "field-note";
  }

  // stills size — independent of draft/sketch, only "Create Stills" reads it
  const stillsSizeSel = $("#stillsSize");
  stillsSizeSel.value = state.board.defaults.stillsSize || "small";
  const largeStills = stillsSizeSel.value === "large";
  $("#stillsSizeNote").textContent = largeStills
    ? `Renders at this project's real resolution and step count — big enough ` +
      `to use as a reference image, but noticeably slower than the small size.`
    : `Renders at draft size (384px long edge, 4 steps) — fast, good for ` +
      `judging composition, too small to use as a reference image.`;

  // stills engine — mirrors Orchestrator._still_model's "auto" rule
  const stillsEngineSel = $("#stillsEngine");
  const imageModels = state.models.filter((m) => m.kind === "image");
  if (stillsEngineSel.dataset.built !== "1") {
    stillsEngineSel.textContent = "";
    const auto = el("option", null, "Automatic");
    auto.value = "auto";
    stillsEngineSel.appendChild(auto);
    // Status is shown by the Ready/Unavailable chip beside the dropdown.
    imageModels.forEach((m) => {
      const o = el("option", null, m.label.split("—")[0].trim());
      o.value = m.id;
      stillsEngineSel.appendChild(o);
    });
    stillsEngineSel.dataset.built = "1";
  }
  const stillsEngine = state.board.defaults.stillsEngine || "auto";
  const addingStills = stillsAddingNew();
  if (!addingStills) stillsEngineSel.value = stillsEngine;
  const editableStill = mfluxConfigs().some((c) => c.id === stillsEngineSel.value) ||
    (stillsEngineSel.value && stillsEngineSel.value !== "auto" &&
     modelCap(stillsEngineSel.value)?.engine === "mflux");
  $("#btnEditStillsEngine").disabled = !editableStill;
  $("#btnEditStillsEngine").title = editableStill ? ""
    : stillsEngineSel.value === "auto" ? "Pick an mflux engine to edit it"
    : "Built-in vpipe engine — not defined in mflux-engines.json";
  // While "+ Add" is open, the dropdown shows the draft — leave it alone.
  if (!addingStills) {
    const autoPick = imageModels.find((m) => m.available);
    const engineCapStill = stillsEngine === "auto" ? autoPick : modelCap(stillsEngine);
    const stillsChip = $("#stillsStatus");
    stillsChip.hidden = !engineCapStill;
    stillsChip.dataset.status = engineCapStill?.available ? "ready" : "unavailable";
    stillsChip.textContent = engineCapStill?.available ? "Ready" : "Unavailable";
    stillsChip.title = engineCapStill && !engineCapStill.available ? engineCapStill.unavailableReason || "" : "";
    const stillsEngineNote = $("#stillsEngineNote");
    if (!engineCapStill) {
      stillsEngineNote.textContent = "No still engine is ready — download Krea-2 into " +
        "the vpipe workspace, or install mflux (uv tool install mflux).";
      stillsEngineNote.className = "field-warn";
    } else if (!engineCapStill.available) {
      stillsEngineNote.textContent = engineCapStill.unavailableReason || "Not available.";
      stillsEngineNote.className = "field-warn";
    } else {
      const name = engineCapStill.label.split("—")[0].trim();
      stillsEngineNote.textContent = (stillsEngine === "auto" ? `Using ${name}. ` : "") +
        (engineCapStill.engine === "mflux"
          ? mfluxReferenceNote(((state.info && state.info.mflux) || [])
              .find((e) => e.id === engineCapStill.id)?.references)
          : "Uses the shot's prompt, scene and cast, plus its Start Ref or a cast " +
            "portrait as an identity reference.");
      stillsEngineNote.className = "field-note";
    }

    // mflux model: what this Mac already has downloaded, and only when there is
    // nothing does it fall back to letting mflux download the default.
    const mfx = engineCapStill && engineCapStill.available && engineCapStill.engine === "mflux"
      ? ((state.info && state.info.mflux) || []).find((e) => e.id === engineCapStill.id)
      : null;
    $("#stillsModelWrap").hidden = !mfx;
    if (mfx) {
      const gb = (b) => `${(b / 1e9).toFixed(1)} GB`;
      const modelSel = $("#stillsModel");
      const sig = [mfx.id, mfx.chosen, mfx.resolved.source, ...mfx.cached.map((c) => c.model)].join("|");
      if (modelSel.dataset.sig !== sig) {
        modelSel.textContent = "";
        const auto = el("option", null, mfx.cached.length
          ? "Automatic — use a downloaded model"
          : `Download ${mfx.defaultModel} on first use`);
        auto.value = "";
        modelSel.appendChild(auto);
        mfx.cached.forEach((c) => {
          const o = el("option", null, `${c.model} — ${gb(c.sizeBytes)}, ` +
            (c.quantization ? `${c.quantization}-bit` : "full precision"));
          o.value = c.model;
          modelSel.appendChild(o);
        });
        // A chosen local folder is not in the cache list; a chosen repo that is
        // no longer downloaded is simply not offered (resolve() ignores it too).
        if (mfx.resolved.source === "selected" && !mfx.cached.some((c) => c.model === mfx.chosen)) {
          const o = el("option", null, mfx.chosen);
          o.value = mfx.chosen;
          modelSel.appendChild(o);
        }
        modelSel.dataset.sig = sig;
      }
      modelSel.dataset.engine = mfx.id;
      modelSel.value = mfx.resolved.source === "selected" ? mfx.chosen : "";
      const r = mfx.resolved;
      const modelNote = $("#stillsModelNote");
      if (r.source === "download") {
        modelNote.textContent = `Nothing downloaded for this engine yet — mflux will ` +
          `download ${r.model} the first time you create stills.`;
        modelNote.className = "field-warn";
      } else {
        modelNote.textContent = `Using ${r.model}` +
          (r.quantization ? ` (already ${r.quantization}-bit)` : " (full precision)") +
          " — already downloaded, nothing to fetch.";
        modelNote.className = "field-note";
      }
    }

  }

  // stills steps / seed — mirror orchestrator.still_params
  const stillDefaults = state.board.defaults;
  const paintInput = (id, v) => {
    const inp = $(id);
    if (document.activeElement !== inp) inp.value = v;
  };
  paintInput("#stillsSteps", stillDefaults.stillsSteps || "");
  paintInput("#stillsSeed", stillDefaults.stillsSeed || "");
  $("#stillsStepsNote").textContent = stillDefaults.stillsSteps
    ? `${stillDefaults.stillsSteps} steps at both sizes. Turbo models look good at 4–8; more is slower and rarely better.`
    : `Automatic: ${largeStills ? 8 : 4} steps at this size (4 at Small, 8 at Large).`;
  const seedNote = $("#stillsSeedNote");
  seedNote.textContent = stillDefaults.stillsSeed
    ? "Same seed every time, so the same prompt gives the same image."
    : "A new random seed each time you press Create Image.";
  const lastStills = (selectedShot() || {}).stills || {};
  const lastSeed = lastStills.start && lastStills.start.seed;
  if (lastSeed && lastSeed !== stillDefaults.stillsSeed) {
    const keep = el("button", "btn btn-ghost btn-sm", `Keep seed ${lastSeed}`);
    keep.dataset.seed = lastSeed;
    keep.title = "Fix the seed this shot's current stills were made with";
    seedNote.append(" ", keep);
  }

  const ttsSel = $("#ttsEngine");
  if (ttsSel.dataset.built !== "1") {
    ttsSel.textContent = "";
    // Status is shown by the Online/Offline chip beside the dropdown.
    state.tts.forEach((e) => {
      const o = el("option", null, e.label);
      o.value = e.id;
      ttsSel.appendChild(o);
    });
    ttsSel.dataset.built = "1";
  }
  // While "+ Add" is open, the dropdown shows the draft — leave it alone.
  if (!ttsAddingNew()) {
    const chosenTts = state.board.defaults.tts || "none";
    ttsSel.value = chosenTts;
    const eng = state.tts.find((e) => e.id === chosenTts);
    const noteBox = $("#ttsNote");
    const recordingLines = shots().filter((raw) =>
      (raw.dialogue || "").trim() &&
      (raw.dialogueSource || "auto") === "recording" &&
      !raw.dialogueAudioUrl
    ).length;
    if (eng && !eng.healthy && eng.id !== "none") {
      noteBox.textContent = eng.message || "This speech engine is unavailable.";
    } else if (recordingLines) {
      noteBox.textContent =
        `${recordingLines} dialogue shot${recordingLines === 1 ? "" : "s"} still need a generated recording. ` +
        "Select this engine, then use Generate in each shot's Dialogue panel before rendering.";
    } else {
      noteBox.textContent = eng && eng.id === "none" ? (eng.message || "") : "";
    }
    noteBox.className = eng && !eng.healthy && eng.id !== "none" || recordingLines ? "field-warn" : "field-note";
    const chip = $("#ttsStatus");
    chip.hidden = !eng || eng.id === "none";
    chip.dataset.status = eng?.healthy ? "online" : "offline";
    chip.textContent = eng?.healthy ? "Online" : "Offline";
    chip.title = eng && !eng.healthy ? eng.message || "" : "";
  }
  $("#btnEditTtsService").disabled = !ttsSel.value || ttsSel.value === "none";

  const llmSel = $("#llmService");
  const llmAll = (state.info.llm && state.info.llm.services) || [];
  if (llmSel.dataset.built !== "1" && llmAll.length) {
    llmSel.textContent = "";
    llmAll.forEach((sv) => {
      // Status is shown by the Online/Offline chip beside the dropdown.
      const o = el("option", null,
        sv.id === "none" || !sv.model ? sv.label : `${sv.label} · ${sv.model}`);
      o.value = sv.id;
      llmSel.appendChild(o);
    });
    llmSel.dataset.built = "1";
  }
  // While "+ Add" is open, the dropdown shows the draft — leave it alone.
  if (!llmAddingNew()) {
    const chosenLlm =
      state.board.defaults.llm || (state.info.llm && state.info.llm.default) || "none";
    llmSel.value = chosenLlm;
    const sv = llmAll.find((x) => x.id === chosenLlm);
    const llmNote = $("#llmNote");
    llmNote.textContent = sv && !sv.healthy
      ? sv.message
      : sv && sv.id !== "none"
      ? "Rewrites are shown for approval before they replace anything."
      : !llmConfigs().length
      ? "No LLM services yet — click + Add to set one up."
      : "";
    llmNote.className = sv && !sv.healthy ? "field-warn" : "field-note";
    const chip = $("#llmStatus");
    chip.hidden = !sv || sv.id === "none";
    chip.dataset.status = sv?.healthy ? "online" : "offline";
    chip.textContent = sv?.healthy ? "Online" : "Offline";
    chip.title = sv && !sv.healthy ? sv.message || "" : "";
  }
  $("#btnEditLlmService").disabled = !llmSel.value || llmSel.value === "none";


  const stepsInput = $("#defSteps");
  if (document.activeElement !== stepsInput) {
    stepsInput.value = state.board.defaults.steps || 8;
  }

  renderCast();


}

/* Built once and left alone: renderRail() runs on every poll tick while a
   render is in flight, and rebuilding a live wand button on every tick would
   reset its "rewriting…" state mid-request and blow away an open proposal
   the user is still reading. */
function ensureSceneWand() {
  const wandSlot = $("#sceneWandSlot");
  if (!wandSlot || wandSlot.childElementCount) return;
  wandSlot.appendChild(
    wandButton({
      title: (svc) =>
        `Restyle the scene description using ${svc.label} (${svc.model}). ` +
        `Takes up to a minute on a local model, and shows you the result ` +
        `before changing anything.`,
      slot: () => $("#sceneProposalSlot"),
      rewrite: () =>
        API.rewrite(state.slug, {
          field: "sceneDescription",
          text: $("#sceneDescription").value,
        }),
      onUse: (text) => {
        $("#sceneDescription").value = text;
        state.board.sceneDescription = text;
        markDirty();
        updateResolvedPreview();
        toast(
          "Scene description replaced. Undo by editing it back — the old text is above."
        );
      },
    })
  );
}

function ensureSoundscapeWand() {
  const wandSlot = $("#soundscapeWandSlot");
  if (!wandSlot || wandSlot.childElementCount) return;
  wandSlot.appendChild(
    wandButton({
      title: (svc) =>
        `Restyle the background sound effects using ${svc.label} (${svc.model}). ` +
        `Takes up to a minute on a local model, and shows you the result ` +
        `before changing anything.`,
      slot: () => $("#soundscapeProposalSlot"),
      rewrite: () =>
        API.rewrite(state.slug, {
          field: "soundscape",
          text: $("#soundscape").value,
        }),
      onUse: (text) => {
        $("#soundscape").value = text;
        state.board.soundscape = text;
        markDirty();
        updateResolvedPreview();
        toast(
          "Background sound effects replaced. Undo by editing it back — the old text is above."
        );
      },
    })
  );
}

/* --- Settings → Soundtrack ----------------------------------------------- */

/* Music for the whole cut, kept apart from the Background sound effects H3
   renders into each shot. Whether it can be generated yet is the server's
   call — every shot needs a clip first, so the cut's length is known (see
   server/soundtrack/board.py) — and this tab only shows the answer. A board
   from before the tab existed with a background file attached keeps using
   that file as its soundtrack. */
function soundtrackOpts() {
  if (!state.board.soundtrack) {
    const legacy = !!(state.board.assembly || {}).backgroundAudio;
    state.board.soundtrack = legacy ? { enabled: true, source: "upload" } : { enabled: false, source: "generate" };
  }
  return state.board.soundtrack;
}

function soundtrackEngines() {
  return (state.info && state.info.soundtrack && state.info.soundtrack.engines) || [];
}

const SOUNDTRACK_DEFAULTS = { model: "sm-music", referenceStrength: 0.5, duck: true, duckDb: 12, duckAttack: 0.15, duckRelease: 0.5 };

function soundtrackChanged({ repaint = false } = {}) {
  markDirty();
  if (repaint) renderSoundtrackSettings();
  // The status depends on the saved board, so ask again once this lands.
  clearTimeout(soundtrackChanged._t);
  soundtrackChanged._t = setTimeout(() => saveNow().then(refreshSoundtrackStatus), 800);
}

async function refreshSoundtrackStatus() {
  if (!state.slug) return;
  const slug = state.slug;
  try {
    const st = await API.soundtrackStatus(slug);
    if (state.slug !== slug) return;
    state.soundtrackStatus = st;
  } catch (err) {
    state.soundtrackStatus = { state: "blocked", message: err.message };
  }
  paintSoundtrackStatus();
}

function renderSoundtrackSettings() {
  const host = $("#soundtrackSettings");
  if (!host || !state.board) return;
  host.innerHTML = "";
  const s = soundtrackOpts();
  const cut = () => (state.board.assembly ||= {});
  const val = (k) => (s[k] ?? SOUNDTRACK_DEFAULTS[k]);

  const toggle = (text, checked, change) => {
    const label = el("label", "toggle");
    const input = el("input"); input.type = "checkbox"; input.checked = !!checked;
    input.addEventListener("change", () => change(input.checked));
    label.append(input, el("span", "toggle-track"), el("span", null, text));
    return label;
  };
  const field = (labelText, control, note) => {
    const f = el("div", "field");
    const l = el("label", null, labelText);
    f.append(l, control);
    if (note) f.appendChild(el("div", "field-note", note));
    return f;
  };
  const number = (key, min, max, step, onChange) => {
    const input = el("input"); input.type = "number";
    input.min = String(min); input.max = String(max); input.step = String(step);
    input.value = String(val(key));
    input.addEventListener("change", () => {
      const n = Number(input.value);
      if (!Number.isFinite(n) || n < min || n > max) { input.value = String(val(key)); return; }
      (onChange || ((v) => { s[key] = v; }))(n);
      soundtrackChanged();
    });
    return input;
  };
  const upload = (label, onRef) => {
    const input = el("input"); input.type = "file"; input.accept = "audio/*";
    input.setAttribute("aria-label", label);
    input.addEventListener("change", async () => {
      if (!input.files[0]) return;
      const slug = state.slug, board = state.board;
      try {
        const ref = await API.uploadRef(slug, input.files[0]);
        if (state.board !== board) return;
        onRef(ref); soundtrackChanged({ repaint: true });
      } catch (err) { toast(err.message, "error"); }
    });
    return input;
  };

  host.appendChild(toggle("Add a soundtrack to the final cut", s.enabled, (on) => {
    s.enabled = on;
    if (on && !s.seed) s.seed = 1 + Math.floor(Math.random() * 2147483646);
    // Asking H3 to leave music out changes every shot's prompt, so it is only
    // switched on for the user while nothing has been rendered with the old one.
    if (on && s.noMusicInShots === undefined) {
      s.noMusicInShots = !shots().some((raw) => (raw.outputs || []).length);
    }
    soundtrackChanged({ repaint: true });
  }));
  if (!s.enabled) {
    host.appendChild(el("div", "field-note",
      "Music is separate from the Background sound effects, which H3 renders into each shot. " +
      "The soundtrack is one continuous piece laid under the whole cut, ducked under dialogue."));
    return;
  }

  const source = el("select");
  for (const [v, t] of [["generate", "Generate with Stable Audio 3"], ["upload", "Use my own audio file"]]) {
    const o = el("option", null, t); o.value = v; source.appendChild(o);
  }
  source.value = s.source || "generate";
  source.addEventListener("change", () => { s.source = source.value; soundtrackChanged({ repaint: true }); });
  host.appendChild(field("Source", source));

  if ((s.source || "generate") === "generate") {
    const engines = soundtrackEngines();
    if (!engines.length) {
      host.appendChild(el("div", "final-warn",
        "No soundtrack engine is set up on this Mac. Run setup/install-stable-audio-3.sh, then restart Storyboard."));
    } else {
      const engineSel = el("select");
      engines.forEach((e) => {
        const o = el("option", null, e.healthy ? e.label : `${e.label} — unavailable`); o.value = e.id; engineSel.appendChild(o);
      });
      const engine = engines.find((e) => e.id === s.engine) || engines.find((e) => e.healthy) || engines[0];
      engineSel.value = engine.id;
      engineSel.addEventListener("change", () => { s.engine = engineSel.value; soundtrackChanged({ repaint: true }); });
      const row = el("div", "field-row");
      row.appendChild(field("Engine", engineSel, engine.healthy ? "" : engine.message));

      const modelSel = el("select");
      (engine.models || []).forEach((m) => {
        const o = el("option", null, `${m.label} · up to ${dur(m.maxSeconds)}${m.installed ? "" : " · not downloaded"}`);
        o.value = m.id; modelSel.appendChild(o);
      });
      modelSel.value = val("model");
      modelSel.addEventListener("change", () => { s.model = modelSel.value; soundtrackChanged(); });
      row.appendChild(field("Model", modelSel));
      host.appendChild(row);
    }

    const promptField = el("div", "field");
    const head = el("div", "rename-row");
    head.append(el("label", null, "Music prompt"), el("span", null));
    const wandSlot = head.lastChild;
    const prompt = el("textarea"); prompt.id = "soundtrackPrompt"; prompt.rows = 4;
    prompt.placeholder = "Music only — e.g. “Heroic orchestral fanfare, soaring brass melody over driving strings and timpani, " +
      "triumphant 1980s adventure film score, builds to a climax, 110 BPM.”";
    prompt.value = s.prompt || "";
    prompt.addEventListener("input", () => { s.prompt = prompt.value; soundtrackChanged(); });
    const proposalSlot = el("div", "proposal-slot");
    wandSlot.appendChild(wandButton({
      title: (svc) =>
        `Turn this into a Stable Audio 3 music prompt using ${svc.label} (${svc.model}). ` +
        `Names of films or composers are rewritten as the style they stand for.`,
      slot: () => proposalSlot,
      rewrite: () => API.rewrite(state.slug, { field: "soundtrackPrompt", text: prompt.value }),
      onUse: (text) => {
        prompt.value = text; s.prompt = text; soundtrackChanged();
        toast("Music prompt replaced. Undo by editing it back — the old text is above.");
      },
    }));
    promptField.append(head, prompt, proposalSlot, el("div", "field-note",
      "Describe genre, instruments, mood and tempo. The model doesn't know film or composer names — " +
      "write “like the Star Wars theme” and press Rewrite to turn it into a description of that style."));
    host.appendChild(promptField);

    const refField = el("div", "field");
    refField.appendChild(el("label", null, "Reference audio (optional) — sets the tone"));
    if (s.reference) {
      const row = el("div", "rename-row");
      const remove = el("button", "btn btn-sm", "Remove");
      remove.onclick = () => { s.reference = null; soundtrackChanged({ repaint: true }); };
      row.append(el("span", "field-note", s.reference.label || s.reference.path.split("/").pop()), remove);
      refField.appendChild(row);
      const strength = el("input"); strength.type = "range"; strength.min = "0"; strength.max = "1"; strength.step = "0.05";
      strength.value = String(val("referenceStrength"));
      const shown = el("span", "hint-inline");
      const paint = () => {
        const v = Number(strength.value);
        shown.textContent = ` ${Math.round(v * 100)}% — ${v < 0.35 ? "loose: tempo and mood" : v < 0.7 ? "instruments and feel" : "close: may keep the melody"}`;
      };
      paint();
      strength.addEventListener("input", paint);
      strength.addEventListener("change", () => { s.referenceStrength = Number(strength.value); soundtrackChanged(); });
      const sl = el("label", "field-note", "Reference strength"); sl.appendChild(shown);
      refField.append(sl, strength);
    } else {
      refField.appendChild(upload("Soundtrack reference audio", (ref) => { s.reference = ref; }));
    }
    refField.appendChild(el("div", "field-note",
      "Use music you own or are licensed to use. At high strength the result can reproduce the reference's melody."));
    host.appendChild(refField);

    const seedRow = el("div", "rename-row");
    const seed = number("seed", 0, 2147483647, 1);
    const reroll = el("button", "btn btn-sm", "New seed");
    reroll.title = "Same prompt, a different piece of music";
    reroll.onclick = () => { s.seed = 1 + Math.floor(Math.random() * 2147483646); soundtrackChanged({ repaint: true }); };
    seedRow.append(seed, reroll);
    host.appendChild(field("Seed", seedRow, "The same seed and settings always give the same music."));

    const status = el("div", "soundtrack-status"); status.id = "soundtrackStatus";
    host.appendChild(status);
  } else {
    const f = el("div", "field");
    f.appendChild(el("label", null, "Audio file (loops if shorter than the cut)"));
    const ref = cut().backgroundAudio;
    if (ref) {
      const row = el("div", "rename-row");
      const remove = el("button", "btn btn-sm", "Remove");
      remove.onclick = () => { delete cut().backgroundAudio; soundtrackChanged({ repaint: true }); };
      row.append(el("span", "field-note", ref.label || ref.path.split("/").pop()), remove);
      f.appendChild(row);
    } else {
      f.appendChild(upload("Soundtrack audio file", (r) => { cut().backgroundAudio = r; }));
    }
    host.appendChild(f);
  }

  host.appendChild(el("div", "card-heading-title soundtrack-subhead", "Mix"));
  const volume = el("input"); volume.type = "number"; volume.min = "0"; volume.max = "2"; volume.step = "0.05";
  volume.value = String(cut().backgroundVolume ?? 0.15);
  volume.addEventListener("change", () => {
    const n = Number(volume.value);
    if (!Number.isFinite(n) || n < 0 || n > 2) return;
    cut().backgroundVolume = n; soundtrackChanged();
  });
  host.appendChild(field("Soundtrack volume (1 = as generated)", volume));
  host.appendChild(toggle("Duck the music under dialogue", val("duck"), (on) => { s.duck = on; soundtrackChanged({ repaint: true }); }));
  if (val("duck")) {
    const row = el("div", "field-row");
    row.append(
      field("Duck by (dB)", number("duckDb", 0, 30, 1)),
      field("Dip ahead (s)", number("duckAttack", 0, 2, 0.05)),
      field("Recover (s)", number("duckRelease", 0, 5, 0.05)),
    );
    host.appendChild(row);
    host.appendChild(el("div", "field-note",
      "Timed from each recorded line, so the music dips just before the first word and comes back after the last. " +
      "Shots where H3 speaks the line itself are ducked for their whole length."));
  }
  host.appendChild(toggle("Ask H3 to leave music out of each shot", !!s.noMusicInShots, (on) => {
    s.noMusicInShots = on; soundtrackChanged();
  }));
  host.appendChild(el("div", "field-note",
    "Stops H3 composing its own music per shot under the soundtrack. It changes the shot prompt, " +
    "so shots already rendered show as changed until re-rendered."));
  paintSoundtrackStatus();
}

function paintSoundtrackStatus() {
  const box = $("#soundtrackStatus");
  if (!box) return;
  box.innerHTML = "";
  const st = state.soundtrackStatus;
  if (!st) { box.appendChild(el("div", "field-note", "Checking the cut…")); return; }
  const warn = ["waiting", "blocked", "stale"].includes(st.state);
  box.appendChild(el("div", warn ? "final-warn" : "field-note", st.message || ""));
  if (st.seconds != null) {
    let line = `Cut length ${dur(st.seconds)}`;
    if (st.loops) line += ` — this model tops out at ${dur(st.maxSeconds)}, so the music loops after that. Medium goes to ${dur(380)}.`;
    box.appendChild(el("div", "field-note", line));
  }
  const r = st.render;
  if (r && r.url) {
    const audio = el("audio"); audio.controls = true; audio.preload = "none"; audio.src = r.url;
    box.append(audio, el("div", "field-note",
      `${st.state === "current" ? "Current" : "Previous"}: ${dur(r.seconds)} · ${r.model} · seed ${r.seed}`));
  }
  const busy = !!(state.status && (state.status.busy || (state.status.stills && state.status.stills.busy)));
  const go = el("button", "btn btn-sm btn-primary", st.state === "current" ? "Up to date" : "Generate now");
  go.disabled = busy || state.soundtrackGenerating || !["ready", "stale"].includes(st.state);
  if (busy) go.title = "Wait for the render to finish — the music model shares memory with it.";
  if (state.soundtrackGenerating) go.textContent = "Generating…";
  go.onclick = async () => {
    state.soundtrackGenerating = true; paintSoundtrackStatus();
    try {
      await saveNow();
      const res = await API.generateSoundtrack(state.slug);
      state.soundtrackStatus = res.status;
      if (res.status && res.status.render) state.board.soundtrackRender = res.status.render;
      toast(res.note.message);
    } catch (err) {
      toast(err.message, "error");
      if (err.payload && err.payload.status) state.soundtrackStatus = err.payload.status;
    } finally {
      state.soundtrackGenerating = false; paintSoundtrackStatus();
    }
  };
  const acts = el("div", "final-actions"); acts.appendChild(go);
  box.appendChild(acts);
}

/* --- the assembled cut --------------------------------------------------- */

/* The board-level artefact, so it sits with the other board-level things
   rather than in the per-shot preview. Its whole job is to be visibly absent
   when it has not been built: a folder of clips and no video is the failure
   this panel exists to make obvious. */
function renderCutSettings(host, busy) {
  const details = el("details", "panel");
  details.open = !!state.cutSettingsOpen;
  details.addEventListener("toggle", () => { state.cutSettingsOpen = details.open; });
  details.appendChild(el("summary", null, "Continuity, dialogue & cut settings"));
  const opts = () => (state.board.assembly ||= {});
  const number = (label, value, max, change) => {
    const row = el("label", "field-note", label + " ");
    const input = el("input");
    input.type = "number"; input.min = "0"; input.max = String(max); input.step = "0.05";
    input.value = String(value || 0); input.disabled = busy;
    input.addEventListener("change", () => {
      const n = Number(input.value);
      if (!Number.isFinite(n) || n < 0 || n > max) return;
      change(n); markDirty();
    });
    row.appendChild(input); details.appendChild(row);
  };
  number("Crossfade seconds (0 = straight cut)", opts().transitionSeconds, 2, n => { opts().transitionSeconds = n; });
  number("Audio fade at scene edges (seconds)", opts().audioFadeSeconds, 2, n => { opts().audioFadeSeconds = n; });
  const normalize = el("input"); normalize.type = "checkbox";
  normalize.checked = !!opts().normalizeAudio; normalize.disabled = busy;
  normalize.addEventListener("change", () => { opts().normalizeAudio = normalize.checked; markDirty(); });
  const normLabel = el("label", "field-note");
  normLabel.append(normalize, el("span", null, " Match scene audio levels")); details.appendChild(normLabel);
  const music = el("button", "btn btn-sm btn-ghost", "Soundtrack settings…");
  music.title = "Music under the whole cut — Settings → Soundtrack";
  music.onclick = () => { openSettings(); document.querySelector('[data-settings-tab="soundtrack"]')?.click(); };
  details.append(el("div", "field-note", "Music for the whole cut is set up in Settings → Soundtrack."), music);
  const chain = el("button", "btn btn-sm", "Chain all scenes from their previous shot");
  chain.disabled = busy || shots().length < 2;
  chain.onclick = () => {
    const manual = shots().filter((s, i) => i > 0 && s.startRef && s.startRef.kind !== "chain");
    if (manual.length && !confirm(
      `${manual.length} shot(s) already have a manually chosen Start frame. Replace them with a chain from the previous shot?`
    )) return;
    shots().forEach((s, i, all) => {
      s.startRef = i ? { kind: "chain", from: all[i - 1].id, label: `last frame of shot ${i}` } : null;
    });
    markDirty(); render();
  };
  const prepare = el("button", "btn btn-sm", "Prepare all dialogue");
  prepare.disabled = busy || state.dialoguePreparing;
  prepare.onclick = async () => {
    prepare.disabled = true;
    try {
      await saveNow();
      state.status = await API.prepareDialogue(state.slug);
      state.awaitingBatch = true; startPolling(); render();
    } catch (err) { toast(err.message, "error"); prepare.disabled = false; }
  };
  details.append(chain, prepare, el("div", "field-note",
    "Current takes are reused; missing or changed recordings are generated. Native speech is created during video rendering. Crossfades overlap picture and sound and shorten the cut."));
  host.appendChild(details);
}

function renderBoundaryReview(host, raw, idx) {
  if (idx < 1) return;
  const previous = shots()[idx - 1];
  const clip = s => (s.renderedDialogueSource !== "native" && s.dubUrl) || (s.outputs || []).find(u => hasExt(u, "mp4"));
  if (!clip(previous) || !clip(raw)) return;
  const panel = el("details", "panel");
  panel.appendChild(el("summary", null, "Review the cut from the previous scene"));
  const row = el("div", "boundary-review");
  for (const [scene, ending] of [[previous, true], [raw, false]]) {
    const cell = el("div");
    const video = el("video"); video.src = clip(scene); video.controls = true; video.preload = "metadata";
    video.muted = outputMuted();
    let begin = 0, end = 0;
    video.addEventListener("loadedmetadata", () => {
      end = Math.max(0, video.duration - (scene.trimOut || 0));
      begin = ending ? Math.max(scene.trimIn || 0, end - 1.5) : (scene.trimIn || 0);
      if (!ending) end = Math.min(end, begin + 1.5);
      video.currentTime = begin;
    });
    video.addEventListener("play", () => { if (video.currentTime >= end) video.currentTime = begin; });
    video.addEventListener("timeupdate", () => { if (end && video.currentTime >= end) video.pause(); });
    cell.append(el("div", "field-note", ending ? "Previous ending" : "Current opening"), video); row.appendChild(cell);
  }
  panel.append(row, el("div", "field-note", "Review trimmed boundaries before assembling; transitions appear in the final cut."));
  host.appendChild(panel);
}

// From the shots as planned, not any rendered clip, so it works pre-render.
// Still-image shots contribute no clip to the cut (see assemble.py), so they
// are excluded; trims and crossfade are applied the way assemble.py applies them.
function estimatedFinalStats() {
  const transition = Math.min(2, Math.max(0, Number((state.board.assembly || {}).transitionSeconds) || 0));
  const lengths = shots()
    .filter((raw) => (modelCap(effectiveShotModel(raw)) || {}).kind !== "image")
    .map((raw) => Math.max(0, (raw.frames || 0) / 24 - (raw.trimIn || 0) - (raw.trimOut || 0)));
  const overlap = lengths.length > 1 ? transition * (lengths.length - 1) : 0;
  const seconds = Math.max(0, lengths.reduce((a, b) => a + b, 0) - overlap);
  return { clips: lengths.length, seconds, frames: Math.round(seconds * 24) };
}

function renderFinal(host = $("#preview .final-video-content")) {
  if (!host) return;
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
    v.muted = outputMuted();
    const viewport = el("div", "final-video-viewport");
    viewport.appendChild(v);
    host.appendChild(viewport);
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
    const music = f.soundtrack;
    if (music && ["failed", "skipped"].includes(music.state)) {
      host.appendChild(el("div", "final-warn", `No soundtrack in this cut — ${music.message}`));
    } else if (music && ["generated", "current", "upload"].includes(music.state)) {
      host.appendChild(el("div", "final-meta", "Soundtrack mixed in" +
        (((state.board.soundtrack || {}).duck ?? true) ? ", ducked under dialogue." : ".")));
    }
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
    const est = estimatedFinalStats();
    host.appendChild(
      el("div", "final-meta final-estimate",
         est.clips
           ? `Estimated once rendered: ${est.clips} clip(s) · ${dur(est.seconds)} · ${est.frames} frames @ 24fps`
           : "No video shots yet, so there is nothing to estimate.")
    );
  }

  renderCutSettings(host, busy);
  const acts = el("div", "final-actions");
  const btn = el("button", "btn btn-sm", f && f.url ? "Re-assemble" : "Assemble now");
  btn.disabled = busy;
  if (busy) btn.title = "A render is running — the clips are still being written.";
  btn.addEventListener("click", () => $("#btnAssemble").click());
  acts.appendChild(btn);
  if (f && f.url) {
    const download = el("a", "btn btn-sm btn-primary", "Download");
    download.href = f.url;
    download.download = downloadFileName(
      state.board.name || state.slug || "Project",
      extensionFromUrl(f.url)
    );
    download.title = "Download the assembled video";
    acts.appendChild(download);

    const open = el("a", "btn btn-sm btn-ghost", "Open");
    open.href = f.url;
    open.target = "_blank";
    acts.appendChild(open);
  }
  host.appendChild(acts);
}

function renderFinalPane() {
  const pane = el("div", "final-box final-video-pane");
  const label = paneHint("Final video", "every shot, in order");
  const content = el("div", "final-video-content");
  pane.append(label, content);
  renderFinal(content);
  return pane;
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
    setRewriteButtonBusy(descBtn, true);
    try {
      const r = await API.describeCharacter(
        image,
        draft.voice && draft.voice.path,
        $("#castName").value.trim(),
        field.value.trim(),
        llm && llm.id
      );
      descProposal.appendChild(
        characterExtractionProposal(
          {
            character: r.character || r.text || "",
            environment: r.environment || "",
          },
          field,
          descProposal
        )
      );
      toast(`Character and environment descriptions proposed by ${r.service}.`);
    } catch (err) {
      castError(`Could not describe the image: ${err.message}`);
    } finally {
      setRewriteButtonBusy(descBtn, false);
      paint();
    }
  }

  function characterExtractionProposal(result, field, slot) {
    const box = el("div", "proposal");
    const character = (result.character || "").trim();
    const environment = (result.environment || "").trim();

    const characterHead = el("div", "proposal-head");
    characterHead.appendChild(el("strong", null, "Character only"));
    box.appendChild(characterHead);
    box.appendChild(el("div", "proposal-body", character));

    const characterActs = el("div", "proposal-acts");
    const useCharacter = el("button", "btn btn-sm btn-primary", "Use character");
    useCharacter.addEventListener("click", () => {
      field.value = character;
      draft.description = character;
      useCharacter.textContent = "Character applied";
      useCharacter.disabled = true;
    });
    characterActs.appendChild(useCharacter);
    box.appendChild(characterActs);

    if (environment) {
      const environmentPart = el("div", "proposal-section");
      const environmentHead = el("div", "proposal-head");
      environmentHead.appendChild(el("strong", null, "Environment / background"));
      environmentPart.appendChild(environmentHead);
      environmentPart.appendChild(el("div", "proposal-body", environment));

      const environmentActs = el("div", "proposal-acts");
      const useEnvironment = el("button", "btn btn-sm", "Use in scene");
      useEnvironment.title = "Replace the current project scene description with this environment description";
      useEnvironment.addEventListener("click", () => {
        const current = (state.board.sceneDescription || "").trim();
        if (current && !window.confirm("Replace the current scene description with this environment description?")) return;
        state.board.sceneDescription = environment;
        $("#sceneDescription").value = environment;
        markDirty();
        useEnvironment.textContent = "Scene updated";
        useEnvironment.disabled = true;
        toast("Environment copied to the scene description.");
      });
      environmentActs.appendChild(useEnvironment);
      environmentPart.appendChild(environmentActs);
      box.appendChild(environmentPart);
    }

    const discard = el("button", "btn btn-sm btn-ghost", "Discard");
    discard.addEventListener("click", () => (slot.innerHTML = ""));
    const discardActs = el("div", "proposal-acts proposal-dismiss");
    discardActs.appendChild(discard);
    box.appendChild(discardActs);
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
  wireDragList(strip);

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
    const cap = modelCap(effectiveShotModel(raw));
    // What will actually render, not just what's configured: draft mode
    // shrinks the frame and caps steps at 4 (see draftGeometry / the
    // backend's own _draft_geometry), so this must track that toggle and
    // the project resolution live rather than always showing the full-size
    // numbers.
    const draftOn = !!(state.board.defaults && state.board.defaults.draft);
    const sketchOn = draftOn && !!state.board.defaults.sketch;
    const [rw, rh] = projectResolution().split("x").map(Number);
    const resLabel = draftOn
      ? `${draftGeometry(rw, rh, cap ? cap.sizeAlign : 16).join("×")} (${sketchOn ? "sketch" : "draft"})`
      : projectResolution();
    const stepsLabel = draftOn ? 4 : raw.steps;
    // Sketch renders far fewer real frames (see the backend's own comment
    // in prepare()) then stretches the clip back to this same duration, so
    // the length shown here still holds — only the frame count actually
    // generated is different, which is what the (sketch) tag is for.
    body.appendChild(
      el(
        "div",
        "shot-sub",
        `${resLabel}` +
          (cap && cap.kind === "image" ? " · still" : ` · ${fmtDur(raw.frames)}`) +
          ` · ${stepsLabel} steps`
      )
    );

    // The progress bar and phase text go in before the foot row, not after,
    // so the foot row — carrying the running ETA, then the final runtime
    // once done — is always the last child and stays pinned to the same
    // bottom-right spot via .shot-foot's margin-top:auto, in both states.
    // Appending them after foot used to push the ETA up and out of that
    // corner while a shot was still running.
    if (shot.status === "running") {
      const track = el("div", "progress-track");
      const fill = el("div", "progress-fill");
      fill.style.width = `${shot.progress}%`;
      track.appendChild(fill);
      body.appendChild(track);
      if (shot.phase) body.appendChild(el("div", "shot-sub", shot.phase));
    }

    const foot = el("div", "shot-foot");
    foot.appendChild(chip(shot.status));
    if (shot.status === "running") {
      foot.appendChild(el("span", "queue-pct", `${Math.round(shot.progress)}%${etaSuffix(shot)}`));
    } else if (shot.runtimeSeconds != null) {
      foot.appendChild(el("span", "shot-sub", dur(shot.runtimeSeconds)));
    }
    body.appendChild(foot);
    card.appendChild(body);

    card.addEventListener("click", () => {
      state.selectedId = raw.id;
      state.followRender = false;
      render();
    });
    wireDrag(card, raw, "x");
    strip.appendChild(card);
  });

  const add = el("button", "add-shot");
  add.append(el("span", "plus", "+"), el("span", null, "Add shot"));
  add.addEventListener("click", addShot);
  strip.appendChild(add);
}

const SHOT_DRAG_TYPE = "application/x-storyboard-shot";
let activeDragShotId = null;

function clearDropClasses(root = document) {
  root
    .querySelectorAll(".drop-target, .drop-before, .drop-after, .dropping")
    .forEach((n) => {
      n.classList.remove("drop-target", "drop-before", "drop-after", "dropping");
    });
}

function dropAfter(node, axis, e) {
  const r = node.getBoundingClientRect();
  return axis === "y"
    ? e.clientY > r.top + r.height / 2
    : e.clientX > r.left + r.width / 2;
}

function moveShot(dragId, targetId = null, after = false) {
  if (!dragId || !state.board || !Array.isArray(state.board.shots)) return false;
  const from = shotIndex(dragId);
  if (from < 0) return false;

  let to = targetId ? shotIndex(targetId) : state.board.shots.length;
  if (to < 0) return false;
  if (targetId && after) to += 1;
  if (from < to) to -= 1;
  if (from === to) return false;

  const [moved] = state.board.shots.splice(from, 1);
  state.board.shots.splice(to, 0, moved);
  markDirty();
  render();
  return true;
}

function draggedShotId(e) {
  return (
    e.dataTransfer.getData(SHOT_DRAG_TYPE) ||
    e.dataTransfer.getData("text/plain") ||
    activeDragShotId
  );
}

function hasShotDrag(e) {
  return (
    !!activeDragShotId ||
    Array.from(e.dataTransfer.types || []).includes(SHOT_DRAG_TYPE)
  );
}

function wireDrag(node, shot, axis = "x") {
  node.addEventListener("dragstart", (e) => {
    activeDragShotId = shot.id;
    node.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData(SHOT_DRAG_TYPE, shot.id);
    e.dataTransfer.setData("text/plain", shot.id);
  });
  node.addEventListener("dragend", () => {
    activeDragShotId = null;
    clearDropClasses();
  });
  node.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const after = dropAfter(node, axis, e);
    node.classList.add("drop-target");
    node.classList.toggle("drop-before", !after);
    node.classList.toggle("drop-after", after);
  });
  node.addEventListener("dragleave", () => {
    node.classList.remove("drop-target", "drop-before", "drop-after");
  });
  node.addEventListener("drop", (e) => {
    e.preventDefault();
    e.stopPropagation();
    const id = draggedShotId(e);
    const after = dropAfter(node, axis, e);
    activeDragShotId = null;
    clearDropClasses();
    if (!id || id === shot.id) return;
    moveShot(id, shot.id, after);
  });
}

function wireDragList(list) {
  if (list.dataset.dragListWired === "1") return;
  list.dataset.dragListWired = "1";
  list.addEventListener("dragover", (e) => {
    if (!hasShotDrag(e)) return;
    if (e.target.closest(".shot-card")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    list.classList.add("dropping");
  });
  list.addEventListener("dragleave", (e) => {
    if (!list.contains(e.relatedTarget)) list.classList.remove("dropping");
  });
  list.addEventListener("drop", (e) => {
    if (e.target.closest(".shot-card")) return;
    e.preventDefault();
    const id = draggedShotId(e);
    activeDragShotId = null;
    clearDropClasses();
    moveShot(id);
  });
}

async function addShot() {
  try {
    const { board, shot } = await API.addShot(state.slug, {});
    state.board = board;
    state.selectedId = shot.id;
    // Adding a shot is an explicit editing action. Do not let an in-flight
    // batch's output-follow mode immediately move the editor back to the
    // shot the orchestrator is rendering.
    state.followRender = false;
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
  const cap = modelCap(effectiveShotModel(raw));
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
  const h = cardHeading(`Shot ${idx + 1}`);
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
      // A distinct "Auto" option, separate from any real character's id, so
      // picking the character that's already inferred is a genuine value
      // change the native <select> will actually fire — otherwise, with the
      // inferred choice pre-selected, clicking that same name again is a
      // no-op to the browser and speakerId never commits.
      const pick = select(
        [
          ["", `Auto${inferred ? ` (${inferred.name})` : ""}`],
          ...castHere.map((c) => [
            c.id,
            c.voice && c.voice.path ? `${c.name} (cloned voice)` : `${c.name} (no clip)`,
          ]),
        ],
        raw.speakerId || "",
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

    // Ref2VA already sends the speaking character's own voice clip in as a
    // soundtrack reference (see server/backends/vpipe_backend.py), and is
    // asked to speak the line in it directly — verified to work well enough
    // that a separate TTS dub is not just redundant here, it is actively
    // wrong: muxing a second, independently-synthesised take of the same
    // line over a clip that already speaks it is exactly the "doubled
    // dialogue" bug this replaced. So for a shot this applies to, the
    // manual dub controls are replaced with a note instead of shown
    // alongside them.
    const h3ClonesVoice = shotClonesVoice(raw);
    if (h3ClonesVoice) {
      dubRow.appendChild(
        el("span", "field-note",
           `🎙 spoken directly in the render, in ${speaker.name}’s cloned voice — no separate dub needed`)
      );
    } else {
      // The video model is asked not to generate its own dialogue, but it does
      // not always listen — leaving a second, mumbled voice under the real
      // line. "Mix" is right when that instruction held (it keeps engine hum
      // and wind alive under the spoken line); flip to "replace" for a shot
      // where it didn't, and the dub becomes the clip's only audio.
      const replaceToggle = el("label", "toggle");
      const replaceBox = el("input");
      replaceBox.type = "checkbox";
      replaceBox.checked = raw.dubMode === "replace";
      replaceBox.addEventListener("change", () => {
        live().dubMode = replaceBox.checked ? "replace" : "mix";
      markDirty();
      });
      replaceToggle.title =
        "On: the dub replaces the clip's own audio entirely — use this if the " +
        "clip's generated audio already has a (wrong) voice in it. " +
        "Off: the dub is mixed over the clip's audio, keeping its ambience.";
      replaceToggle.append(
        replaceBox,
        el("span", "toggle-track"),
        el("span", null, "Replace clip audio with the dub")
      );
      dubRow.appendChild(replaceToggle);

      const dubBtn = el("button", "btn btn-sm", "Generate");
      dubBtn.disabled = raw.dialogueSource === "native" || !engNow || !engNow.healthy;
      dubBtn.title = raw.dialogueSource === "native"
        ? "H3 generates speech during rendering. Select Dialogue-window recording to generate a separate take."
        : !engNow || !engNow.healthy
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
          const r = await runDubShot(raw.id);
          if (r.note) toast(r.note, "warn");
          else if (r.warning) toast(r.warning, "warn");
          else {
            toast(
              `Spoke ${r.seconds}s in ${r.cloned ? `${r.speaker}’s cloned voice` : "the engine voice"}` +
                (r.muxed
                  ? sh.dubMode === "replace"
                    ? " and replaced the clip's audio with it."
                    : " and mixed it over the clip."
                  : " — the clip is not rendered yet, so nothing was mixed.")
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
    }

    // the spoken line, playable right here
    // dialogueAudioUrl is a cached, separate TTS take. Native H3 speech is
    // regenerated inside clip.mp4 on every render, so an older recording can
    // legitimately remain on disk but must not be presented as the native
    // render's voice preview.
    if (raw.dialogueAudioUrl && raw.dialogueSource !== "native") {
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
    if (raw.dubUrl && raw.renderedDialogueSource !== "native") {
      dubRow.appendChild(
        el("span", "field-note",
           raw.dubMode === "replace"
             ? "replaced the clip's audio — see Output"
             : "mixed over the clip — see Output")
      );
    }
    panelDubRow = dubRow;
  }

  const stillsBusy = !!(state.status && state.status.stills && state.status.stills.busy);
  const one = el("button", "btn btn-sm", "Render this shot");
  one.disabled = !!(state.status && state.status.busy) || stillsBusy;
  const initialDialogueIssue = dialogueReadiness(raw);
  one.title = initialDialogueIssue
    ? `${initialDialogueIssue.title}. ${initialDialogueIssue.action}`
    : "Render this shot";
  one.addEventListener("click", async () => {
    // Disable immediately, not after the save + render round trips —
    // otherwise the button sits clickable for however long those take,
    // which reads as "nothing happened" and invites a second click.
    one.disabled = true;
    let started = false;
    try {
      await runStartRender([raw.id]);
      started = true;
    } catch (err) {
      toast(err.message, "error");
    } finally {
      if (!started) one.disabled = false;
    }
  });
  head.appendChild(one);

  const stillsBtn = el("button", "btn btn-ghost btn-sm", "Create Image");
  stillsBtn.title = "Creates a story board image from the scene prompt.";
  stillsBtn.disabled = !!(state.status && state.status.busy) || stillsBusy;
  stillsBtn.addEventListener("click", async () => {
    stillsBtn.disabled = true;
    await saveNow();
    try {
      state.status = await API.stills(state.slug, raw.id);
      state.previewMainTab = "stills";
      startPolling();
      render();
    } catch (err) {
      toast(err.message, "error");
      stillsBtn.disabled = false;
    }
  });
  head.appendChild(stillsBtn);

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
  const shotNameInput = input("text", raw.title, (v) => {
    live().title = v;
    markDirty();
    renderStrip();
    renderRail();
  });
  shotNameInput.dataset.fkey = "shot-name";
  const shotNameField = el("div", "field");
  shotNameField.append(paneHint("Shot name", null), shotNameInput);
  panel.appendChild(shotNameField);

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
  const promptHead = paneHint("Action / camera / mood only", "not the subject or the style");
  promptHead.appendChild(el("div", "header-spacer"));
  promptHead.appendChild(
    wandButton({
      title: (svc) =>
        `Restyle this shot prompt for ${modelLabel(STORYBOARD_MODEL)} using ` +
        `${svc.label} (${svc.model}). Takes up to a minute on a local model, and ` +
        `shows you the result before changing anything.`,
      slot: () => promptPane.querySelector(".proposal-slot"),
      rewrite: () => API.rewrite(state.slug, { shotId: raw.id, text: ta.value }),
      onUse: (text) => {
        ta.value = text;
        live().prompt = text;
        markDirty();
        updateResolvedPreview();
        syncTabDots();
        toast("Shot prompt replaced. Undo by editing it back — the old text is above.");
      },
    })
  );
  promptPane.append(promptHead, ta, el("div", "proposal-slot"));
  pane("prompt", promptPane);

  if (!cap || cap.supportsAudio) {
    const dlg = el("textarea", "ta-tall");
    const guide = el("div", "dialogue-guide");
    dlg.value = raw.dialogue || "";
    dlg.placeholder =
      "A line someone speaks in this clip.\n" +
      "Used as a visual cue for mouth movement, then synthesised by the " +
      "speech engine and muxed over the finished clip.";
    dlg.addEventListener("input", () => {
      const hadDialogue = !!(live().dialogue || "").trim();
      live().dialogue = dlg.value;
      markDirty();
      updateResolvedPreview();
      syncTabDots();
      dialogueGuide(guide, live());
      // The speaking controls are only useful once a line exists. Rebuild on
      // the empty -> non-empty transition so the Generate button appears,
      // while renderEditor's focus snapshot keeps the caret in the textarea.
      if (!hadDialogue && dlg.value.trim()) renderEditor();
    });
    dlg.dataset.fkey = "dialogue";
    const dialogueSpeaker = (() => {
      const cast = state.board.characters || [];
      const inShot = cast.filter((c) => (raw.characterIds || []).includes(c.id));
      return cast.find((c) => c.id === raw.speakerId) ||
        inShot.find((c) => c.voice && c.voice.path) || inShot[0] || null;
    })();
    const style = el("textarea", "ta-compact");
    style.value = raw.dialogueStyle || "";
    style.placeholder =
      "Voice direction for the cloned take, not spoken aloud.\n" +
      "Example: tired, quiet, breathy, slight smile, urgent whisper.\n" +
      "For Qwen-style TTS you can paste a full [STYLE / VOICE DIRECTION] block.";
    style.addEventListener("input", () => {
      live().dialogueStyle = style.value;
      markDirty();
      dialogueGuide(guide, live());
    });
    style.dataset.fkey = "dialogue-style";
    const dp = el("div");
    const source = el("select");
    source.setAttribute("aria-label", "Dialogue source");
    for (const [value, label] of [["recording", "Use separate Dialogue recording (TTS)"], ["native", "H3 native speech (needs cast voice reference)"], ["auto", "Legacy automatic selection"]]) {
      const option = el("option", null, label); option.value = value; source.appendChild(option);
    }
    source.value = raw.dialogueSource || "auto";
    source.onchange = () => { live().dialogueSource = source.value; markDirty(); renderEditor(); };
    dialogueGuide(guide, raw);
    const dialogueHead = paneHint(
      "Spoken words", "rewritten in the selected speaker’s researched tone"
    );
    dialogueHead.appendChild(el("div", "header-spacer"));
    dialogueHead.appendChild(
      wandButton({
        title: (svc) =>
          `Research ${dialogueSpeaker ? dialogueSpeaker.name : "the selected speaker"} ` +
          `and rewrite this line in character using ${svc.label} (${svc.model}). ` +
          `The result is shown for approval before changing anything.`,
        disabledReason: !dialogueSpeaker
          ? "Choose a cast character for this shot before rewriting dialogue."
          : !(state.info && state.info.search && state.info.search.url)
            ? "Configure Web search in Settings before rewriting dialogue."
            : "",
        slot: () => dp.querySelector(".proposal-slot"),
        rewrite: () => API.rewrite(state.slug, {
          shotId: raw.id,
          field: "dialogue",
          text: dlg.value,
          speakerId: dialogueSpeaker && dialogueSpeaker.id,
        }),
        onUse: (text) => {
          dlg.value = text;
          live().dialogue = text;
          markDirty();
          updateResolvedPreview();
          syncTabDots();
          dialogueGuide(guide, live());
          toast(
            `Dialogue rewritten${dialogueSpeaker ? ` for ${dialogueSpeaker.name}` : ""}. ` +
            "Undo by editing it back — the old line is above."
          );
        },
      })
    );
    dp.append(paneHint("Dialogue source", "recording uses a generated take; H3 native speech uses the cast member's reference voice"), source, guide);
    dp.append(
      dialogueHead,
      dlg,
      el("div", "proposal-slot"),
      paneHint("Voice direction", "sent to compatible speech engines, not spoken aloud"),
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
      "Additional scene-background sounds for THIS clip — not the general Background sound effects.\n" +
      "Use local environmental details such as a nearby bird, distant siren, or passing vehicle.";
    sa.addEventListener("input", () => {
      live().soundNote = sa.value;
      markDirty();
      updateResolvedPreview();
      syncTabDots();
    });
    sa.dataset.fkey = "sound-accents";

    const sp2 = el("div");
    const saHead = paneHint("Additional local background sound only", "do not repeat the general Background sound effects");
    saHead.appendChild(el("div", "header-spacer"));
    saHead.appendChild(
      wandButton({
        title: (svc) =>
          `Propose local background sound accents for this shot using ${svc.label} (${svc.model}). ` +
          `Adds only local scene-background sounds not already in the general ` +
          `Background sound effects; dialogue and foreground action Foley are never included. ` +
          `Takes up to a minute on a local model, and shows you the result ` +
          `before changing anything.`,
        slot: () => sp2.querySelector(".proposal-slot"),
        rewrite: () =>
          API.rewrite(state.slug, { shotId: raw.id, field: "soundNote", text: sa.value }),
        onUse: (text) => {
          sa.value = text;
          live().soundNote = text;
          markDirty();
          updateResolvedPreview();
          syncTabDots();
          toast("Sound accents replaced. Undo by editing it back — the old text is above.");
        },
      })
    );
    sp2.append(saHead, sa, el("div", "proposal-slot"));
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
    castPanel.appendChild(paneHint("Characters in this shot", "refer to them by name in the prompt"));

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
        const current = live();
        current.characterIds = [...ids];
        markDirty();
        renderEditor();
      };
      picks.appendChild(pill);
    });
    castPanel.appendChild(picks);

    const chosen = cast.filter((c) => (raw.characterIds || []).includes(c.id));
    const withMedia = chosen.filter((c) => c.image || c.voice);
    const modelNow = effectiveShotModel(raw);
    if (chosen.length && withMedia.length) {
      const mediaMsg =
        modelNow === "wan-i2v"
          ? `${withMedia.length} character reference set(s) are selected, but Wan 2.2 does not send separate Cast media — only the shot prompt and its required Start frame reach the model. Describe appearance in the Cast description instead.`
          : modelNow === "ltx-2.5"
          ? `${withMedia.length} character reference set(s) are selected, but LTX-2.5 has no reference-image mechanism at all — only the shot prompt and any Start/End frame anchor reach the model. Describe appearance in the Cast description instead.`
          : `${withMedia.length} character reference set(s) will be included in the Ref2VA request.`;
      castPanel.appendChild(el("div", "inline-warn", mediaMsg));
    }
    host.appendChild(castPanel);
  }

  // Ref2VA receives Start/End frame images (manually chosen or chained from
  // the previous shot's last frame) as ordered references, never a hard
  // anchor. Wan and LTX-2.5 are the exceptions: neither has a reference-list
  // mode at all, and each wires Start (and, for LTX-2.5, End) frame directly
  // to its own image-to-video input instead — Wan requires one, LTX-2.5
  // does not.
  const isVideoShot = cap && cap.kind !== "image";
  if (isVideoShot) {
    const refPanel = el("div", "panel");
    refPanel.style.marginTop = "var(--sp-3)";
    const modelNow = effectiveShotModel(raw);
    const anchorMode = modelNow === "wan-i2v";
    refPanel.appendChild(paneHint(
      "Start & end references",
      modelNow === "wan-i2v"
        ? "Wan 2.2 requires a Start frame; End frame is not sent"
        : modelNow === "ltx-2.5"
        ? "sent to LTX-2.5 as hard-pinned anchors, not ordered references — the frame IS that picture"
        : "sent to Ref2VA as ordered references, never hard-pinned frames"
    ));

    const slots = el("div", "ref-slots");
    slots.appendChild(refSlot("Start frame", raw, "startRef"));
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
        const current = live();
        current.startRef = cb.checked
          ? { kind: "chain", from: prev.id, label: `last frame of shot ${idx}` }
          : null;
        markDirty();
        render();
      });
      t.append(cb, el("span", "toggle-track"),
               el("span", null, `Chain start frame from shot ${idx}'s last rendered frame`));
      wrap.appendChild(t);
      wrap.appendChild(el("div", "field-note",
        modelNow === "wan-i2v" || modelNow === "ltx-2.5"
          ? `Sent to ${modelNow === "wan-i2v" ? "Wan 2.2" : "LTX-2.5"} as a hard-pinned anchor — the opening frame IS that picture, not guidance. Upload a still above instead for a hand-picked opening image.`
          : "Sent to Ref2VA as an ordered reference — guides the opening (identity, environment, lighting, direction of motion) without pinning an exact frame. Upload a still above instead for a hand-picked opening image."));
      refPanel.appendChild(wrap);
    }
    refPanel.appendChild(
      el(
        "div",
        "hint-body",
        modelNow === "wan-i2v"
          ? "Wan 2.2 wires Start frame directly to the model's image-to-video input, and requires one — this checkpoint has no text-only mode, and rendering without a Start frame set will fail. End frame is not sent — Wan has no port for one. Separate Cast, style and shot-reference images are not sent either."
          : modelNow === "ltx-2.5"
          ? "LTX-2.5 has a genuine text-only mode, so Start/End frame are optional here — but when set, each is wired straight to the model as a hard anchor (the opening/closing frame IS that picture), not a soft reference. Separate Cast, style and shot-reference images are not sent — describe appearance in the prompt instead."
          : "Ref2VA receives Start frame, End frame, and other reference images (cast, style, shot references) together as an ordered reference set — none of them pin an exact frame."
      )
    );
    host.appendChild(refPanel);
    const trim = el("div", "panel");
    trim.appendChild(paneHint("Trim for the final cut", "removes whole frames from each end, at this shot's 24fps"));
    for (const [key, title] of [["trimIn", "Remove from start (frames)"], ["trimOut", "Remove from end (frames)"]]) {
      const label = el("label", "field-note", title + " ");
      const input = el("input"); input.type = "number"; input.min = "0"; input.step = "1";
      input.value = String(Math.round((raw[key] || 0) * 24));
      input.addEventListener("change", () => {
        const n = Number(input.value);
        if (Number.isFinite(n) && n >= 0) { live()[key] = Math.round(n) / 24; markDirty(); }
      });
      label.appendChild(input); trim.appendChild(label);
    }
    trim.appendChild(el("div", "field-note", "Trims affect assembly only. Continuity references use the original final rendered frame."));
    host.appendChild(trim);
    renderBoundaryReview(host, raw, idx);
  }

  if (isVideoShot) {
    const refPanel = el("div", "panel");
    refPanel.style.marginTop = "var(--sp-3)";
    refPanel.appendChild(paneHint("Reference images", null));
    refPanel.appendChild(shotReferenceImages(raw));

    const noteModel = effectiveShotModel(raw);
    const note = noteModel === "wan-i2v"
      ? "Wan 2.2 is the selected video engine. These separate images are retained on the board but are not sent — Wan only reads the shot prompt and its required Start frame."
      : noteModel === "ltx-2.5"
      ? "LTX-2.5 is the selected video engine. These separate images are retained on the board but are not sent — LTX-2.5 only reads the shot prompt and its Start/End frame anchors."
      : `${cap.label.split("—")[0].trim()} uses these alongside the ` +
        `cast portraits selected for this shot. Style references are a library — ` +
        `add one here to include it in this shot's render. Tag an image as @name to ` +
        `address it directly in the shot prompt.`;
    refPanel.appendChild(el("div", "hint-body", note));
    host.appendChild(refPanel);
  }

  // params
  const params = el("div", "panel");
  params.style.marginTop = "var(--sp-3)";
  params.appendChild(cardHeading("Parameters"));

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
    ["21:9", 21 / 9],
    ["16:9", 16 / 9],
    ["4:3", 4 / 3],
    ["1:1", 1],
    ["3:4", 3 / 4],
    ["9:16", 9 / 16],
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

function frameSizeLabel(res, group) {
  const pixels = res.replace("x", " × ");
  if (group && group[0] === res && allResolutions().includes(res)) {
    return `${pixels}  · H3-Base 768p`;
  }
  if (allResolutions().includes(res)) return `${pixels}  · reduced`;
  return `${pixels}  · legacy custom`;
}

function aspectLabel(aspect) {
  return ({
    "21:9": "21:9 · ultrawide",
    "16:9": "16:9 · widescreen",
    "4:3": "4:3 · landscape",
    "1:1": "1:1 · square",
    "3:4": "3:4 · portrait",
    "9:16": "9:16 · vertical",
  })[aspect] || aspect;
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
  const cap = modelCap(effectiveShotModel(raw));
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
      if (c.image && effectiveShotModel(raw) === STORYBOARD_MODEL) {
        push(`cast guidance · ${n || "unnamed"}`,
          `${n || "The selected character"}: use the character portrait ` +
          "and Cast description together to maintain appearance. Use the " +
          "portrait for visual identity and the Cast description for " +
          "persistent appearance details, clothing, and equipment. Ignore " +
          "pose and background in the portrait or Cast description. " +
          "Follow the scene prompt for actions, expressions, posture, " +
          "camera, environment, and any explicit appearance changes.");
      }
    });

  push("shot", raw.prompt);

  if (!cap || cap.supportsAudio) {
    push("dialogue movement", dialogueVisualCue(raw));
    if (state.board.soundscapeInShots !== false) {
      push("sound bed", state.board.soundscape);   // project-wide
    } else if ((state.board.soundscape || "").trim()) {
      push("sound bed", "(disabled; not sent to this shot render)");
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

  const requestId = String((Number(host.dataset.requestId) || 0) + 1);
  host.dataset.requestId = requestId;
  host.appendChild(el("div", "hint", "Resolving render inputs…"));
  req("POST", "/api/render-preview", {board: state.board, shot: raw}).then((result) => {
    if (host.dataset.requestId !== requestId) return;
    host.innerHTML = "";
    host.appendChild(el("div", "seg-source", `${result.model} · ${result.dialogueSource}`));
    host.appendChild(el("div", "seg-text", result.prompt));
    for (const ref of result.references) host.appendChild(el("div", "hint", `${ref.token} · ${ref.tag ? "@" + ref.tag + " · " : ""}${ref.name} · ${ref.role}`));
    for (const warning of result.warnings) host.appendChild(el("div", "inline-warn", warning));
  }).catch((error) => {
    if (host.dataset.requestId === requestId) { host.innerHTML = ""; host.appendChild(el("div", "inline-warn", error.status === 404 ? "Restart the Storyboard server after the active render queue finishes to enable the updated render preview." : error.message)); }
  });
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

function alignUp(v, align) {
  if (!align || align <= 1) return v;
  return Math.ceil(v / align) * align;
}

function draftGeometry(w, h, align = 16) {
  const scale = Math.min(0.5, 384 / Math.max(w, h));
  return [
    alignUp(Math.max(align, Math.round(w * scale)), align),
    alignUp(Math.max(align, Math.round(h * scale)), align),
  ];
}

/* --- stage tiles ----------------------------------------------------------

   A plain-language view of what a render is actually doing, for someone who
   has never heard of "denoise" or "vae decode". The tile set and the phase
   mapping are grounded in server/backends/vpipe_backend.py's own
   PHASE_ORDER/PHASE_WEIGHTS (encoding references / denoise / vae decode) and
   the orchestrator's steps around them (build the spec, validate the
   result) — not invented busywork. "References" only appears for Ref2VA
   (FL2VA never has that phase), and "Voice" only for a shot whose dialogue
   still needs a separate TTS dub — see shotClonesVoice(). */

const STAGE_PHASE_KEY = {
  "encoding references": "refs",
  "denoise": "gen",
  "vae decode": "dev",
};

function stageTiles(raw, shot) {
  const isRef2va = true;
  const needsDub = !!(raw.dialogue || "").trim() && !shotClonesVoice(raw);

  // icon ids match the <symbol>/<g> ids in index.html's stage-icon sprite —
  // see the comment there for why these are hand-drawn inline SVG rather
  // than an icon font or CDN.
  const tiles = [
    { key: "prep", label: "Preparing" },
    ...(isRef2va ? [{ key: "refs", label: "References" }] : []),
    { key: "gen", label: "Generating" },
    { key: "dev", label: "Developing" },
    ...(needsDub ? [{ key: "voice", label: "Voice" }] : []),
    { key: "check", label: "Checking" },
    { key: "done", label: "Done" },
  ];

  const status = shot.status;
  const order = tiles.map((t) => t.key);

  if (!status || status === "draft") {
    return tiles.map((t) => ({ ...t, state: "pending" }));
  }
  if (status === "queued") {
    return tiles.map((t, i) => ({ ...t, state: i === 0 ? "active" : "pending" }));
  }
  if (status === "running") {
    const activeKey = STAGE_PHASE_KEY[shot.phase] || "prep";
    const activeIdx = Math.max(0, order.indexOf(activeKey));
    return tiles.map((t, i) => ({
      ...t,
      state: i < activeIdx ? "done" : i === activeIdx ? "active" : "pending",
    }));
  }
  // A terminal status: done, failed, blocked, review, interrupted.
  return tiles.map((t) => {
    if (t.key === "voice") return { ...t, state: raw.dubUrl ? "done" : "pending" };
    if (t.key === "done") return { ...t, state: status === "done" ? "done" : "failed" };
    return { ...t, state: "done" };
  });
}

const SVG_NS = "http://www.w3.org/2000/svg";

function stageIconSvg(key) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "stage-tile-icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#stage-icon-${key}`);
  svg.appendChild(use);
  return svg;
}

function renderStageTiles(raw, shot) {
  const row = el("div", "stage-tiles");
  row.dataset.shotId = raw.id;
  stageTiles(raw, shot).forEach((t) => {
    const tile = el("div", "stage-tile");
    tile.dataset.state = t.state;
    tile.dataset.key = t.key;
    tile.title = t.label;
    tile.appendChild(stageIconSvg(t.key));
    tile.appendChild(el("span", "stage-tile-label", t.label));
    row.appendChild(tile);
  });
  return row;
}

/* The poll's path: states patched in place on the existing tiles, never
   rebuilt — so the active tile's pulse animation is not restarted every
   second the way a fresh element would restart it. Only rebuilds if the
   tile *set* itself changed (dialogue added/removed mid-session). */
function paintStageTiles(raw, shot) {
  const row = document.querySelector(`.stage-tiles[data-shot-id="${raw.id}"]`);
  if (!row) return;
  const states = stageTiles(raw, shot);
  const nodes = row.querySelectorAll(".stage-tile");
  if (nodes.length !== states.length) {
    row.replaceWith(renderStageTiles(raw, shot));
    return;
  }
  nodes.forEach((node, i) => {
    if (node.dataset.state !== states[i].state) node.dataset.state = states[i].state;
  });
}

/* --- stills ---------------------------------------------------------------- */

// One still (the opening frame). Boards from before may also hold an "end"
// still; it is simply not shown.
const STILL_PHASE_LABELS = { start: "Opening frame" };
const STILL_PHASE_ORDER = ["start"];

/* A generic full-size viewer, built on demand rather than living in
   index.html, since nothing about it is specific to any one dialog. */
function openLightbox(src, alt) {
  const overlay = el("div", "lightbox-overlay");
  const img = el("img", "lightbox-img");
  img.src = src;
  img.alt = alt || "";
  overlay.appendChild(img);
  const onKey = (e) => {
    if (e.key === "Escape") close();
  };
  const close = () => {
    overlay.remove();
    document.removeEventListener("keydown", onKey);
  };
  overlay.addEventListener("click", close);
  document.addEventListener("keydown", onKey);
  document.body.appendChild(overlay);
}

function renderStillsPane(raw) {
  const wrap = el("div", "stills-pane");
  const st = state.status && state.status.stills;
  const runningHere = !!(st && st.busy && st.shotId === raw.id);
  const failedHere = !!(st && st.error && st.shotId === raw.id && !st.busy);

  if (runningHere) {
    wrap.appendChild(el("div", "stills-status", "Generating still…"));
  } else if (failedHere) {
    wrap.appendChild(el("div", "stills-status stills-error", `Image failed: ${st.error}`));
  }

  // While a job for this shot is running (or just failed), the live
  // state.status.stills.results wins over the board's own copy, which is
  // only refetched once the job ends.
  const live = (runningHere || failedHere) && st.results ? st.results : null;
  const stills = live || raw.stills;

  if (!stills || !stills.start) {
    if (!runningHere) {
      wrap.appendChild(
        el("div", "empty-state",
           "No image yet — “Create Image” creates a storyboard image of " +
           "this shot's opening frame from the scene prompt.")
      );
    }
    return wrap;
  }

  const grid = el("div", "stills-grid");
  STILL_PHASE_ORDER.forEach((key) => {
    const cell = el("div", "stills-cell");
    const s = stills[key];
    if (s && s.url) {
      const img = el("img", "stills-thumb stills-thumb-clickable");
      img.src = s.url;
      img.alt = `${STILL_PHASE_LABELS[key]} still`;
      img.title = "Click to view full size";
      img.addEventListener("click", () => openLightbox(s.url, img.alt));
      cell.appendChild(img);
    } else if (runningHere && key === st.phase) {
      cell.appendChild(el("div", "stills-thumb stills-pending"));
    } else {
      cell.appendChild(el("div", "stills-thumb stills-missing"));
    }
    cell.appendChild(el("div", "stills-label", STILL_PHASE_LABELS[key]));
    grid.appendChild(cell);
  });
  wrap.appendChild(grid);
  return wrap;
}

/* --- preview ------------------------------------------------------------- */

// Media URLs that failed to load, so a missing clip shows its placeholder at
// once on the next repaint instead of retrying (and flashing) every poll. A
// fresh render gets a new URL (its ?t= stamp changes), so it is tried again.
const missingMedia = new Set();

function stagePlaceholder(stage, icon, text) {
  const ph = el("img", "preview-placeholder");
  ph.src = "assets/shot-placeholder.png";
  ph.alt = "";
  stage.appendChild(ph);
  const e = el("div", "preview-empty");
  e.appendChild(el("span", "big", icon));
  e.appendChild(el("span", null, text));
  stage.appendChild(e);
}

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

  // Two cards, like the rail and the shot column: the output itself (clip,
  // final video or stills, plus what the render is doing), then its
  // Details / Backend output.
  const outputCard = el("div", "panel output-card");
  const detailCard = el("div", "panel output-card");
  host.append(outputCard, detailCard);

  outputCard.appendChild(cardHeading("Output"));

  // Stills are a second view of the same output card, on equal footing with
  // the rendered clip — not a debug/detail tab, so this toggle lives right
  // at the top rather than down with Details / Backend output.
  const mainTabs = el("div", "tabs");
  mainTabs.classList.add("output-tabs");
  const activeMainTab = state.previewMainTab || "clip";
  [
    { id: "stills", label: "Image" },
    { id: "clip", label: "Scene Clip" },
    { id: "final", label: "Final Video" },
  ].forEach((t) => {
    const b = el("button", "tab");
    b.dataset.tab = t.id;
    b.dataset.on = String(t.id === activeMainTab);
    b.appendChild(el("span", null, t.label));
    b.addEventListener("click", () => {
      state.previewMainTab = t.id;
      renderPreview();
    });
    mainTabs.appendChild(b);
  });
  const mute = el("button", "btn btn-ghost btn-sm output-mute");
  mute.type = "button";
  paintOutputMute(mute);
  mute.addEventListener("click", () => applyOutputMute(!outputMuted()));
  mainTabs.appendChild(mute);
  outputCard.appendChild(mainTabs);

  if (activeMainTab === "stills") {
    outputCard.appendChild(renderStillsPane(raw));
  } else if (activeMainTab === "final") {
    outputCard.appendChild(renderFinalPane());
  } else {
    const stage = el("div", "preview-stage");
    // A native H3 render already contains the cloned character voice.  A
    // clip-dubbed URL can survive from an older recording-mode take (for
    // example when the server is restarted just as a render finishes), and
    // must not replace that native soundtrack in the preview.
    const video = (raw.renderedDialogueSource !== "native" && raw.dubUrl) ||
      (shot.outputs || []).find((u) => hasExt(u, "mp4"));
    const image = (shot.outputs || []).find((u) => hasExt(u, "jpe?g|png|webp"));
    const rendering = shot.status === "running"
      ? `Rendering — ${Math.round(shot.progress)}%${shot.phase ? ` (${shot.phase})` : ""}${etaSuffix(shot)}`
      : "";
    // A clip or image the board points at but that won't load (deleted,
    // moved with the data folder) shows the placeholder, not a broken player.
    const showMissing = () => {
      stage.textContent = "";
      stagePlaceholder(stage, rendering ? "◐" : "▦", rendering || "Scene clip file is missing");
      outputCard.querySelector(".preview-actions")?.remove();
    };
    const media = (url, make) => {
      if (missingMedia.has(url)) return null;
      const node = reuse(url, make);
      // The element is carried across repaints, so watch it only once.
      if (!node.dataset.watched) {
        node.dataset.watched = "1";
        node.addEventListener("error", () => {
          missingMedia.add(url);
          showMissing();
        }, { once: true });
      }
      return node;
    };
    const shown = video
      ? media(video, () => {
          const v = el("video");
          v.src = video;
          v.controls = true;
          v.loop = true;
          v.muted = outputMuted();
          return v;
        })
      : image
      ? media(image, () => {
          const img = el("img");
          img.src = image;
          return img;
        })
      : raw.thumb
      ? media(raw.thumb, () => {
          const img = el("img");
          img.src = raw.thumb;
          return img;
        })
      : undefined;
    if (shown) {
      stage.appendChild(shown);
    } else if (shown === null) {
      stagePlaceholder(stage, rendering ? "◐" : "▦", rendering || "Scene clip file is missing");
    } else {
      stagePlaceholder(stage, rendering ? "◐" : "▦", rendering || "Not rendered yet");
    }
    outputCard.appendChild(stage);

    if (video && shown) {
      const actions = el("div", "preview-actions");
      const download = el("a", "btn btn-sm btn-primary", "Download clip");
      download.href = video;
      download.download = sceneClipDownloadName(raw, video);
      download.title = "Download this scene clip";
      actions.appendChild(download);
      outputCard.appendChild(actions);
    }
  }

  // A plain-language read of what the render is doing, always in view —
  // the technical log below is the detail view for whoever wants it.
  outputCard.appendChild(renderStageTiles(raw, shot));

  staleNotes(raw, shot.status).forEach((n) => outputCard.appendChild(n));

  const diag = diagnostic(raw, shot);
  if (diag) outputCard.appendChild(diag);

  // Details / Backend output share a small tab strip of their own — the raw
  // log is the least-needed-by-default part of this panel, so it is one
  // click away rather than always taking up space under every shot.
  const detailTabs = el("div", "tabs");
  const detailPanes = el("div", "tab-panes");
  const detailTabDefs = [
    { id: "details", label: "Details" },
    { id: "log", label: "Backend output" },
  ];
  let activeDetailTab = state.previewDetailTab || "details";
  const showDetailTab = (id) => {
    activeDetailTab = id;
    state.previewDetailTab = id;
    [...detailTabs.children].forEach((b) => (b.dataset.on = String(b.dataset.tab === id)));
    [...detailPanes.children].forEach((p) => (p.hidden = p.dataset.tab !== id));
  };
  detailTabDefs.forEach((t) => {
    const b = el("button", "tab");
    b.dataset.tab = t.id;
    b.appendChild(el("span", null, t.label));
    b.addEventListener("click", () => showDetailTab(t.id));
    detailTabs.appendChild(b);
  });
  detailCard.append(detailTabs, detailPanes);

  const detailsPane = el("div", "tab-pane");
  detailsPane.dataset.tab = "details";
  const stats = el("dl", "stat-grid");
  // Details for the Final Video tab describes the whole cut, not one shot.
  const finalStatRows = () => {
    const f = state.board.finalVideo;
    const est = estimatedFinalStats();
    const rows = [
      ["Shots in cut", String(est.clips)],
      ["Estimated length", est.clips ? dur(est.seconds) : "—"],
      ["Estimated frames", est.clips ? `${est.frames} @ 24fps` : "—"],
    ];
    if (f && f.url) {
      rows.push(
        ["Assembled length", dur(f.seconds)],
        ["Assembled frames", `${Math.round((f.seconds || 0) * 24)} @ 24fps`],
        ["File", f.url.split("/").pop()],
      );
    }
    return rows;
  };
  (activeMainTab === "final" ? finalStatRows() : [
    ["Status", STATUS_LABELS[shot.status] || shot.status],
    ["Model", (modelCap(effectiveShotModel(raw)) || {}).label || effectiveShotModel(raw)],
    ["Runtime", dur(shot.runtimeSeconds)],
    ["Rendered", raw.renderedAs === "draft" ? "draft (384px long edge, 4 steps)"
                 : raw.renderedAs === "final" ? "final" : "—"],
    ["Outputs", (shot.outputs || []).length
      ? (shot.outputs || []).map((u) => u.split("/").pop()).join(", ")
      : "—"],
  ]).forEach(([k, v]) => {
    stats.appendChild(el("dt", null, k));
    stats.appendChild(el("dd", null, v));
  });
  // Already inside the Details card, so no second panel border around it.
  const sp = el("div", "details-stats");
  sp.append(stats, systemLoadGrid());
  detailsPane.appendChild(sp);

  const logPane = el("div", "tab-pane");
  logPane.dataset.tab = "log";
  const lbl = cardHeading("Backend output", "newest first; stills, the render, and any spoken line");
  logPane.appendChild(lbl);

  /* One window, two producers. They go into separate containers inside it so
     each can be filled independently — the render log arrives live or as a
     fetch of run.log, the speech log the same way — while the order on screen
     stays fixed and the render poll's append (which counts lines) only ever
     touches its own. */
  const box = el("div", "log");
  const stillsPart = el("div", "log-part");
  stillsPart.dataset.part = "stills";
  const renderPart = el("div", "log-part");
  renderPart.dataset.part = "render";
  const speechPart = el("div", "log-part");
  speechPart.dataset.part = "speech";
  box.append(stillsPart, renderPart, speechPart);

  // "Create Stills" runs outside the normal render queue, so its log lives
  // on state.status.stills rather than on this shot's own run — shown here,
  // not just as a phase label in the Image tab, so the same vpipe detail
  // is one click away for a still as it is for a full render.
  const st = state.status && state.status.stills;
  if (st && st.shotId === raw.id && st.log && st.log.length) {
    stillsPart.appendChild(logLine("# stills preview"));
    st.log.slice(-LOG_WINDOW).reverse().forEach((e) => stillsPart.appendChild(logLine(e)));
    stillsPart.dataset.count = String(st.log.length);
  }

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
          text.trimEnd().split("\n").slice(-LOG_WINDOW).reverse()
            .forEach((t) => renderPart.appendChild(logLine(t)));
          box.scrollTop = 0;
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
    lines.slice(-LOG_WINDOW).reverse().forEach((e) => renderPart.appendChild(logLine(e)));
    // paintLog appends from here rather than rebuilding, so it needs to know
    // how much of the log is already on screen.
    renderPart.dataset.count = String(lines.length);
  }

  if (haveSpeech) {
    if (speech) {
      // A session run has no saved header, so it gets one: which process this
      // came from should be readable without inferring it from content.
      speechPart.appendChild(logLine("# spoken line"));
      speech.slice(-LOG_WINDOW).reverse().forEach((t) => speechPart.appendChild(logLine(t)));
    } else {
      speechPart.appendChild(el("span", "log-empty", "loading speech log…"));
      fetch(raw.speechLogUrl)
        .then((r) => (r.ok ? r.text() : Promise.reject()))
        .then((text) => {
          speechPart.innerHTML = "";
          text.trimEnd().split("\n").slice(-LOG_WINDOW).reverse()
            .forEach((t) => speechPart.appendChild(logLine(t)));
          box.scrollTop = 0;
        })
        .catch(() => {
          speechPart.innerHTML = "";
          speechPart.appendChild(el("span", "log-empty", "No saved speech log."));
        });
    }
  }

  logPane.appendChild(box);
  detailPanes.append(detailsPane, logPane);
  showDetailTab(activeDetailTab);
  box.scrollTop = 0;
}

/* Two shapes reach here: live lines arrive as {level, text} from the
   orchestrator, saved run.log lines as raw strings with a bracketed level. */
function logLine(entry) {
  let level = "INFO";
  let text = "";
  let time = "";
  if (entry && typeof entry === "object") {
    level = entry.level || "INFO";
    text = entry.text || "";
    time = entry.time || "";
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
    // A saved run.log line: "HH:MM:SS [LEVEL] text" (older logs have no time).
    const m = raw.match(/^(?:(\d{2}:\d{2}:\d{2}) )?\[([A-Z]+)\]\s*(.*)$/);
    time = m && m[1] ? m[1] : "";
    level = m ? m[2] : "INFO";
    text = m ? m[3] : raw;
  }
  const line = el("div", "log-line");
  line.dataset.lvl = level;
  if (time) line.appendChild(el("span", "log-time", `${time} `));
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
    // The shot-strip's CHANGED tag already says this clip is out of date —
    // no need to say it again here. What the tag can't do is let the user
    // dismiss it, so that action (only) still lives in this panel.
    const note = el("div", "stale-note stale-note-compact");
    note.dataset.note = "stale";
    note.title = why;
    // The judgement is sometimes the user's: a board that predates change
    // tracking cannot be *shown* to match, but they may know that it does,
    // and half an hour of GPU time to prove it is a poor trade. That is an
    // assertion, so it is theirs to make rather than ours to infer.
    const keep = el("button", "btn btn-sm btn-ghost", "Keep this take");
    keep.title =
      `${why}. Keeping it records this clip as a render of what the board ` +
      "says now, without re-rendering it — use it when you know the clip " +
      "is still current.";
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
    note.appendChild(keep);
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
  });

  const host = $("#preview");
  const stage = host.querySelector(".preview-stage");
  const raw = selectedShot();
  host.querySelectorAll(".stale-note").forEach((n) => n.remove());
  if (raw && stage) {
    const notes = staleNotes(raw, view(raw).status);
    let after = host.querySelector(".preview-actions") || stage;
    notes.forEach((n) => {
      after.after(n);
      after = n;
    });
  }

  const finalPane = document.querySelector("#preview .final-video-content");
  if (finalPane) renderFinal(finalPane);
  paintMeta();
  paintRenderHint();
}

function diagnosticGuidance(reason) {
  const text = (reason || "").toLowerCase();
  if (text.includes("separate recording") || text.includes("dialogue take exists") ||
      text.includes("dialogue-window recording")) {
    return "This line is using the separate-recording path. Choose a healthy Speech engine, press Generate in the Dialogue panel, preview the take, and then render again.";
  }
  if (text.includes("recording is out of date")) {
    return "The dialogue text or voice direction changed after the last take. Press Generate in the Dialogue panel to replace it, then render again.";
  }
  if (text.includes("requires minimax h3 ref2va") || text.includes("generates silent video")) {
    return "H3-native speech needs the project's video engine to be Automatic (MiniMax H3), not an explicit engine that generates silent video. Otherwise choose the separate-recording path.";
  }
  return "Check the details above, correct the shot settings, and re-run this shot.";
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

  // A non-blocking issue (e.g. "no cast speaker assigned" — H3 will just
  // improvise a voice) is informational, not why this shot actually failed
  // or is blocked; only a genuine blocker should override the backend's own
  // reason here.
  const setupIssue = dialogueReadiness(raw);
  const blockingIssue = setupIssue && setupIssue.blocking !== false ? setupIssue : null;
  const reason =
    shot.reason || (shot.validation && shot.validation.reason) ||
    (blockingIssue ? `${blockingIssue.title}. ${blockingIssue.body}` : "");
  if (reason) d.appendChild(el("div", "diag-body", reason));
  if (shot.status === "failed" || shot.status === "blocked") {
    const next = el("div", "diag-next");
    next.appendChild(el("strong", null, "Next step: "));
    next.appendChild(el("span", null, blockingIssue ? blockingIssue.action : diagnosticGuidance(reason)));
    d.appendChild(next);
  }

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
      state.followRender = true;
      startPolling();
      render();
    } catch (err) {
      toast(err.message, "error");
    }
  });
  actions.appendChild(again);

  if (shot.status === "review") {
    const accept = el("button", "btn btn-sm btn-ghost", "Accept anyway");
    accept.addEventListener("click", async () => {
      // Server-side, not a local status edit: the live run state is laid
      // over the board's (view()), and a shot chained from this one is gated
      // on that live state, so both have to change together.
      accept.disabled = true;
      try {
        await saveNow();
        state.status = await API.acceptReview(state.slug, raw.id);
        state.board = await API.getBoard(state.slug);
        toast("Accepted — shots that continue from this one can render now.", "info");
      } catch (err) {
        toast(err.message, "error");
        accept.disabled = false;
      }
      render();
    });
    actions.appendChild(accept);
  }
  d.appendChild(actions);
  return d;
}

/* --- prompt rewriting ---------------------------------------------------- */

/* The one way to build a card heading in JS — see .card-heading in app.css.
   Renders "HEADING — description"; the dash is added by CSS. */
function cardHeading(title, description) {
  const h = el("div", "card-heading");
  h.appendChild(el("span", "card-heading-title", title));
  if (description) h.appendChild(el("span", "card-heading-desc", description));
  return h;
}

function paneHint(text, description) {
  if (description === undefined) {
    // Legacy single-line hint: plain small text, no heading styling.
    const h = el("div", "pane-head");
    h.appendChild(el("span", "pane-hint", text));
    return h;
  }
  // A card heading ("HEADING — description") in a row, so an action button
  // appended after (see dialogueHead) sits to its right. Pass `null` for
  // description to get a bare heading.
  const h = el("div", "pane-head pane-head-with-note");
  h.appendChild(cardHeading(text, description));
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

/* A rewrite is a proposal, never an edit. The text being rewritten is the
   user's authorship and it took thought to write, so the model's version
   appears alongside it with an explicit Use this — replacing the text
   outright would destroy work with no way back.

   Generic across every rewritable field (a shot prompt, the scene
   description, the background sound): the caller supplies the tooltip, where
   the proposal slot lives, how to ask for the rewrite, and what to do with
   the result. */
function setRewriteButtonBusy(btn, busy) {
  btn.disabled = busy;
  btn.classList.toggle("busy", busy);
  btn.lastChild.textContent = busy ? "rewriting…" : "Rewrite";
}

function wandButton({ title, slot, rewrite, onUse, disabledReason = "" }) {
  const svc = currentLLM();
  const btn = el("button", "btn btn-sm wand");
  const icon = stageIconSvg("rewrite");
  icon.classList.add("rewrite-icon");
  btn.append(icon, el("span", null, "Rewrite"));

  if (disabledReason) {
    btn.disabled = true;
    btn.title = disabledReason;
    return btn;
  }

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
  btn.title = title(svc);

  btn.addEventListener("click", async () => {
    const slotEl = slot();
    slotEl.innerHTML = "";
    setRewriteButtonBusy(btn, true);
    try {
      const r = await rewrite();
      slotEl.appendChild(proposalBox(r, onUse, slotEl));
    } catch (err) {
      slotEl.appendChild(el("div", "inline-warn", `⚠ Rewrite failed: ${err.message}`));
    } finally {
      setRewriteButtonBusy(btn, false);
    }
  });
  return btn;
}

function proposalBox(r, onUse, slot) {
  const box = el("div", "proposal");
  const head = el("div", "proposal-head");
  head.appendChild(el("strong", null, "Proposed rewrite"));
  const research = r.research && r.research.attempted
    ? ` · researched ${r.research.sources} source${r.research.sources === 1 ? "" : "s"}`
    : "";
  head.appendChild(el("span", "hint", `— ${r.service}${r.model ? ` · ${r.model}` : ""}${research}`));
  box.appendChild(head);

  const body = el("div", "proposal-body mono", r.text);
  box.appendChild(body);

  const acts = el("div", "proposal-acts");
  const use = el("button", "btn btn-sm btn-primary", "Use this");
  use.addEventListener("click", () => {
    onUse(r.text);
    slot.innerHTML = "";
  });
  const drop = el("button", "btn btn-sm btn-ghost", "Discard");
  drop.addEventListener("click", () => (slot.innerHTML = ""));
  acts.append(use, drop);
  box.appendChild(acts);
  return box;
}

function currentLLM() {
  const want = (state.board && state.board.defaults && state.board.defaults.llm) ||
    (state.info && state.info.llm && state.info.llm.default);
  const all = (state.info && state.info.llm && state.info.llm.services) || [];
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

// Match the library picker: a small trash icon, with the image opening replacement.
function referenceActions(slot, label, replace, remove) {
  slot.title = `Click to replace ${label}`;
  slot.addEventListener("click", async () => {
    try {
      await replace();
    } catch (err) {
      toast(`Could not update reference: ${err.message}`, "error");
    }
  });
  const actions = el("div", "ref-actions");
  actions.addEventListener("click", (event) => event.stopPropagation());
  const button = el("button", "pick-delete", "🗑");
  button.type = "button";
  button.title = `Remove ${label}`;
  button.setAttribute("aria-label", button.title);
  button.addEventListener("click", async (event) => {
    event.stopPropagation();
    button.disabled = true;
    try {
      await remove();
    } catch (err) {
      toast(`Could not update reference: ${err.message}`, "error");
    } finally {
      button.disabled = false;
    }
  });
  actions.appendChild(button);
  return actions;
}

function imageDrop(slot, upload) {
  slot.ondragover = (event) => {
    event.preventDefault();
    slot.classList.add("dropping");
  };
  slot.ondragleave = () => slot.classList.remove("dropping");
  slot.ondrop = async (event) => {
    event.preventDefault();
    event.stopPropagation();
    slot.classList.remove("dropping");
    const file = [...(event.dataTransfer.files || [])].find((f) => f.type.startsWith("image/"));
    if (!file) return;
    try {
      await upload(file);
    } catch (err) {
      toast(`Upload failed: ${err.message}`, "error");
    }
  };
}

function refSlot(label, shot, key, pickerTitle = null) {
  const ref = shot[key];
  const slot = el("div", "ref-slot");
  if (ref) {
    slot.classList.add("filled");
    if (ref.kind === "chain") {
      // Only a chain ref has no user-picked image of its own to protect —
      // it is resolved server-side from the source shot's own last render,
      // so once that exists this is the one slot safe to preview.
      if (ref.resolved) {
        const img = el("img");
        img.src = "/media/" + ref.resolved;
        slot.appendChild(img);
        const chainTag = el("span", "chain-badge");
        chainTag.append(el("span", null, "⛓"), el("span", null, ref.label || "chained"));
        slot.appendChild(chainTag);
      } else {
        const l = el("div", "ref-slot-label");
        l.append(el("strong", null, "⛓ chained"), el("span", null, ref.label || ""));
        slot.appendChild(l);
      }
      const srcWhy = staleWhy(ref.from);
      if (srcWhy) {
        const b = el("span", "stale-badge", "CHANGED");
        const srcIdx = shots().findIndex((s) => s.id === ref.from);
        b.title = srcIdx >= 0
          ? `Shot ${srcIdx + 1}: ${srcWhy}`
          : srcWhy;
        slot.appendChild(b);
      }
    } else {
      const img = el("img");
      img.src = ref.url || ref.path;
      slot.appendChild(img);
    }
    slot.appendChild(referenceActions(slot, label,
      () => pickRef(shot, key, pickerTitle),
      async () => {
        const current = shotById(shot.id);
        if (!current) return;
        current[key] = null;
        markDirty();
        render();
        await saveNow();
      }));
  } else {
    const l = el("div", "ref-slot-label");
    l.append(el("strong", null, label), el("span", null, "click to choose · or drop an image"));
    slot.appendChild(l);
    slot.addEventListener("click", () => pickRef(shot, key, pickerTitle));
  }
  imageDrop(slot, async (file) => {
    const slug = state.slug;
    const chosen = await API.uploadRef(slug, file);
    if (state.slug !== slug) return;
    const current = shotById(shot.id);
    if (!current) return;
    current[key] = chosen;
    markDirty();
    render();
    await saveNow();
  });
  return slot;
}

function shotReferenceImages(shot) {
  if (!Array.isArray(shot.referenceImages)) shot.referenceImages = [];
  const wrap = el("div", "ref-slots");

  shot.referenceImages.forEach((ref, i) => {
    const slot = el("div", "ref-slot filled");
    const img = el("img");
    img.src = ref.url || ref.path;
    slot.appendChild(img);
    const replace = async (chosen) => {
      const current = shotById(shot.id);
      if (!current || !current.referenceImages?.[i]) return;
      current.referenceImages[i] = {
        ...chosen,
        tag: current.referenceImages[i].tag || referenceTagFor(chosen) || "",
        role: current.referenceImages[i].role || "",
      };
      markDirty();
      render();
      await saveNow();
    };
    const actions = referenceActions(slot, `reference image ${i + 1}`,
      async () => {
        const slug = state.slug;
        const chosen = await chooseImage("Replace shot reference image");
        if (chosen && state.slug === slug) await replace(chosen);
      },
      async () => {
        const current = shotById(shot.id);
        if (!current) return;
        current.referenceImages.splice(i, 1);
        markDirty();
        render();
        await saveNow();
      });
    actions.appendChild(referenceTagControl(shot, i, ref));
    slot.appendChild(actions);
    imageDrop(slot, async (file) => {
      const slug = state.slug;
      const chosen = await API.uploadRef(slug, file);
      if (state.slug === slug) await replace(chosen);
    });
    const controls = el("div", "ref-role-control");
    const role = el("select"); role.setAttribute("aria-label", `Reference ${i + 1} role`);
    for (const value of ["environment and composition", "environment", "composition", "style", "character identity", "object appearance"]) {
      const option = el("option", null, value); option.value = value; role.appendChild(option);
    }
    role.value = ref.role || "environment and composition";
    role.onchange = () => { const current = shotById(shot.id)?.referenceImages?.[i]; if (current) current.role = role.value; markDirty(); updateResolvedPreview(); };
    controls.append(el("span", "ref-role-label", "Role"), role);
    controls.addEventListener("click", (event) => event.stopPropagation());
    slot.appendChild(controls);
    wrap.appendChild(slot);
  });

  const add = el("div", "ref-slot ref-slot-add");
  const l = el("div", "ref-slot-label");
  l.append(el("strong", null, "Add viewpoint"), el("span", null, "click to choose · or drop images"));
  add.appendChild(l);
  add.addEventListener("click", () => addShotReferenceImage(shot));
  add.ondragover = (e) => {
    e.preventDefault();
    add.classList.add("dropping");
  };
  add.ondragleave = () => add.classList.remove("dropping");
  add.ondrop = async (e) => {
    e.preventDefault();
    add.classList.remove("dropping");
    const files = [...(e.dataTransfer.files || [])].filter((x) => x.type.startsWith("image/"));
    if (!files.length) return;
    try {
      for (const f of files) {
        shot.referenceImages.push(await API.uploadRef(state.slug, f));
      }
      markDirty();
      await saveNow();
      render();
    } catch (err) {
      toast(`Upload failed: ${err.message}`, "error");
    }
  };
  wrap.appendChild(add);
  return wrap;
}

function referenceTagControl(shot, index, ref) {
  const host = el("span", "ref-tag-control");
  host.addEventListener("click", (event) => event.stopPropagation());
  const button = el("button", "ref-tag-pill", ref.tag ? `@${ref.tag}` : "Add tag");
  button.type = "button";
  button.title = ref.tag ? "Edit reference tag" : "Add a tag for this reference";
  button.setAttribute("aria-label", button.title);
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    const input = el("input", "ref-tag-input");
    input.type = "text";
    input.value = ref.tag || "";
    input.placeholder = "tag-name";
    input.setAttribute("aria-label", "Reference tag");
    const save = el("button", "ref-tag-save", "Save");
    save.type = "button";
    const cancel = () => { host.replaceChildren(button); };
    const commit = async () => {
      const current = shotById(shot.id)?.referenceImages?.[index];
      if (!current) return;
      const tag = input.value.trim().replace(/^@/, "")
        .replace(/\s+/g, "-").replace(/[^A-Za-z0-9_-]/g, "");
      current.tag = tag;
      propagateReferenceTag(current, tag);
      markDirty();
      updateResolvedPreview();
      render();
      await saveNow();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commit(); }
      if (e.key === "Escape") cancel();
    });
    save.addEventListener("click", (e) => { e.stopPropagation(); commit(); });
    host.replaceChildren(input, save);
    input.focus();
    input.select();
  });
  host.appendChild(button);
  return host;
}

async function addShotReferenceImage(shot) {
  const chosen = await chooseImage("Choose an additional shot reference image");
  if (!chosen) return;
  if (!Array.isArray(shot.referenceImages)) shot.referenceImages = [];
  shot.referenceImages.push({
    ...chosen,
    tag: referenceTagFor(chosen) || "",
  });
  markDirty();
  await saveNow();
  render();
}

function referenceKey(ref) {
  return (ref && (ref.path || ref.url || ref.resolved) || "")
    .replace(/^\/media\//, "");
}

function referenceTagFor(ref) {
  const key = referenceKey(ref);
  if (!key || !state.board) return "";
  for (const shot of state.board.shots || []) {
    for (const candidate of shot.referenceImages || []) {
      if (referenceKey(candidate) === key && candidate.tag) return candidate.tag;
    }
  }
  return "";
}

function propagateReferenceTag(source, tag) {
  const key = referenceKey(source);
  if (!key || !state.board) return;
  for (const shot of state.board.shots || []) {
    for (const candidate of shot.referenceImages || []) {
      if (candidate !== source && referenceKey(candidate) === key) candidate.tag = tag;
    }
  }
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
    const own = group.find((item) => item.path && item.path.startsWith(`${state.slug}/`));
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
    ? "Audio already in your projects. Uploading adds it to this project's refs/. " +
      "Use a clip with no embedded cover art — a file that carries one gets read " +
      "as an image instead of a voice, and the clone is silently skipped."
    : "Images already in your projects. Per-frame folders are excluded — use “chain from previous shot” for that." +
      (title === "Choose a style reference"
        ? " Aim for around 1024px on the short edge, any aspect ratio — " +
          "references are downscaled to that before rendering (512px in " +
          "draft mode), so more resolution won't add quality and a much " +
          "smaller image will look soft after being scaled up to fit."
        : "");
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

        const addCard = (img) => {
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
                    grid.innerHTML = "";
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
        };

        // Default to this project's own images. Reuse from elsewhere is
        // still one click away, but it should never just be *there* on open
        // — a project with its own references showing someone else's next
        // to them, unlabeled context, is what reads as contamination, not a
        // deliberate choice to go browse another project's library.
        const mine = shown.filter((img) => img.project === state.slug);
        const others = shown.filter((img) => img.project !== state.slug);
        mine.forEach(addCard);

        if (others.length) {
          if (!mine.length) {
            grid.appendChild(
              el("div", "empty-state", "No images in this project yet.")
            );
          }
          const reveal = el(
            "button", "pick-browse-others",
            `Browse ${others.length} image${others.length === 1 ? "" : "s"} from other projects…`
          );
          reveal.addEventListener("click", () => {
            reveal.remove();
            grid.appendChild(
              el("div", "pick-group-header", "From other projects — picking one copies it in")
            );
            others.forEach(addCard);
          });
          grid.appendChild(reveal);
        } else if (!mine.length) {
          grid.appendChild(
            el("div", "empty-state", "No images in your projects yet — upload one.")
          );
        }
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
  const slug = state.slug;
  const chosen = await chooseImage(
    pickerTitle || (key === "startRef" ? "Choose a start frame" : "Choose an end frame")
  );
  if (!chosen || state.slug !== slug) return;
  const current = shotById(shot.id);
  if (!current) return;
  current[key] = chosen;
  markDirty();
  await saveNow();
  render();
}

document.addEventListener("DOMContentLoaded", boot);
