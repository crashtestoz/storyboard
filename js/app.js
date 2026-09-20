/* ==========================================================================
   Storyboard — front end.
   --------------------------------------------------------------------------
   Talks to the server in server/. Board edits are saved back with a short
   debounce; while a render is running the queue is polled once a second and
   the server's view of a shot wins over the local one, since it is the thing
   actually watching the process.

   Frame-count rules and available model capabilities come from /api/info.
   Video shots route to FL2VA when they have frame anchors and Ref2VA
   otherwise.
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
// Video shots route automatically: frame anchors use FL2VA, while shots with
// no Start/End anchors use Ref2VA so character/style/reference media can be
// sent. Krea-2 is still used by the separate Create Stills preview job via its
// synthetic shot copy, but is not a user-selectable shot model.
const STORYBOARD_MODEL = "ref2va";
// Mirrors server/backends/vpipe_backend.py's _effective_video_model: an
// explicitly chosen engine (per-shot, else the project's "Video engine"
// setting) short-circuits the anchor-based H3 routing below it. Only
// krea2-still and wan-i2v are such engines — Wan has no reference-list mode
// to fall back to the way Ref2VA is H3's fallback, so it is never inferred
// from anchors alone, only requested.
function effectiveShotModel(raw) {
  const defaults = state.board && state.board.defaults;
  const requested = (raw && raw.model) || (defaults && defaults.model) || STORYBOARD_MODEL;
  if (requested === "krea2-still" || requested === "wan-i2v") return requested;
  return raw && (raw.startRef || raw.endRef) ? "fl2va" : STORYBOARD_MODEL;
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
      return {code: "native-dialogue-anchors", title: "Native cloned speech needs Ref2VA", body: "Only MiniMax H3 Ref2VA can clone a voice from a reference clip; this shot's effective video engine is something else (a Start/End frame anchor uses FL2VA, or a different engine is selected).", action: "Remove Start/End frame anchors and use the automatic video engine, or switch Dialogue source to a separate recording."};
    }
    if (!speaker) {
      return {
        code: "native-dialogue-no-speaker",
        title: "H3 native speech needs a cast speaker",
        body: "H3 native speech is selected, but this shot is not assigned to a character.",
        action: "Add a character in Cast and assign that character to this shot.",
      };
    }
    if (!hasVoice) {
      return {
        code: "native-dialogue-no-voice",
        title: "H3 native speech needs a reference voice",
        body: `H3 native speech is selected, but ${speaker.name || "the shot's speaker"} has no reference voice clip.`,
        action: "Attach a reference voice to the cast member, or switch Dialogue source to Dialogue-window recording.",
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
        ? "✓ H3 native speech is ready; the cast reference voice will be sent to Ref2VA."
        : raw.dialogueAudioUrl
        ? "✓ Dialogue take is ready; the generated voice will be mixed after video generation."
        : "";
    }
    return;
  }
  host.className = "dialogue-guide inline-warn";
  host.appendChild(el("strong", null, `${issue.title}. `));
  host.appendChild(el("span", null, `${issue.body} ${issue.action}`));
}

function renderDialogueBlockers() {
  return shots()
    .map((raw) => ({ raw, issue: dialogueReadiness(raw) }))
    .filter((entry) => entry.issue);
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
      turn.actions.forEach((action) => list.appendChild(el("div", null, `• ${proposalSummary(action)}`)));
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
    dubMode: "mix", continuityRef: null, startRef: null, endRef: null,
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

  // Both H3 video modes are used automatically, so their absence always
  // matters. Krea is optional and reports its own problem when Create Stills
  // is used. Wan only matters when it is the selected video engine — a board
  // that has never opted into it should not be warned about a model it will
  // never render with.
  const requiredModels = ["fl2va", STORYBOARD_MODEL];
  if (state.board && state.board.defaults && state.board.defaults.model === "wan-i2v") {
    requiredModels.push("wan-i2v");
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
    .filter((entry) => entry.issue);
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
  $("#btnSaveSearchUrl").addEventListener("click", saveSearchUrl);

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
  });
  $("#btnHelp").addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      $("#helpDialog").hidden = false;
    }
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
  $("#batchBannerStop").addEventListener("click", async () => {
    try {
      await API.stop();
    } catch (err) {
      toast(err.message, "error");
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
  $("#settings").hidden = false;
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
  const btn = $("#btnSaveSearchUrl");
  btn.disabled = true;
  const was = btn.textContent;
  try {
    btn.textContent = "saving…";
    const r = await API.setSearchUrl(url);
    if (state.info) {
      state.info.search = { url: r.searchUrl, overridden: false };
    }
    paintSearchUrl();
    toast(url ? "Web search enabled for the Storyboard AD." : "Web search disabled.");
  } catch (err) {
    toast(`Could not save the search URL: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
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
        s.stills.error ? `Stills failed: ${s.stills.error}` : "Stills ready.",
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
  paintBatchBanner();
  paintMeta();
  paintRenderHint();

  shots().forEach((raw) => {
    const shot = view(raw);
    const pctOnly = `${Math.round(shot.progress || 0)}%`;
    const pct = `${pctOnly}${etaSuffix(shot)}`;

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
  paintBatchBanner();

  paintMeta();
  paintRenderHint();
  paintBladeContext();
  positionAssistantBlade();
  focusRestore(snap);
}

/* Shown whenever a multi-project batch is in flight, regardless of which
   project happens to be open — that render may be on a project you are not
   even looking at, so this is the one place its progress is always visible. */
function paintBatchBanner() {
  const pb = state.status && state.status.projectBatch;
  const banner = $("#batchBanner");
  if (!pb || !pb.active) {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  const doneCount = pb.done.length;
  const totalCount = pb.total.length;
  const current = state.boards.find((b) => b.slug === pb.current);
  const currentName = current ? current.name : pb.current || "…";
  $("#batchBannerText").textContent =
    `Batch rendering — ${currentName} (${doneCount + 1} of ${totalCount})…`;
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
    ? "The background sound text is included in every shot render."
    : "Background sound is disabled for rendering. Only per-shot sound accents are included; no background track is added automatically later.";

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

  // video engine — a project-wide override of the automatic FL2VA/Ref2VA
  // anchor routing (see effectiveShotModel). Only alternate ENGINES belong
  // here, not H3's two own modes — those stay implicit, chosen by whether a
  // shot has a Start/End anchor, exactly as before this option existed.
  const engineSel = $("#videoEngine");
  if (engineSel.dataset.built !== "1") {
    const auto = el("option", null, "Automatic (MiniMax H3 — FL2VA / Ref2VA by anchor)");
    auto.value = "";
    engineSel.appendChild(auto);
    state.models
      .filter((m) => m.id === "wan-i2v")
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
      "Every shot routes automatically: Start/End frame anchor uses FL2VA, otherwise Ref2VA.";
    engineNote.className = "field-note";
  } else if (!engineCap || !engineCap.available) {
    engineNote.textContent =
      (engineCap && engineCap.unavailableReason) || `${chosenEngine} is not prepared in this workspace.`;
    engineNote.className = "field-warn";
  } else {
    engineNote.textContent =
      `Every shot renders on ${engineCap.label.split("—")[0].trim()} instead, overriding the ` +
      "automatic FL2VA/Ref2VA routing above — including shots with a Start/End anchor." +
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
  const recordingLines = shots().filter((raw) =>
    (raw.dialogue || "").trim() &&
    (raw.dialogueSource || "auto") === "recording" &&
    !raw.dialogueAudioUrl
  ).length;
  if (eng && !eng.healthy) {
    noteBox.textContent = eng.message || "This speech engine is unavailable.";
  } else if (recordingLines) {
    noteBox.textContent =
      `${recordingLines} dialogue shot${recordingLines === 1 ? "" : "s"} still need a generated recording. ` +
      "Select this engine, then use Generate in each shot's Dialogue panel before rendering.";
  } else {
    noteBox.textContent = "";
  }
  noteBox.className = eng && !eng.healthy || recordingLines ? "field-warn" : "field-note";

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
  wireDragList(q);
  queueShots.forEach((raw, i) => {
    const shot = view(raw);
    const row = el("div", "queue-row");
    row.dataset.id = raw.id;
    row.draggable = true;
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
      row.appendChild(el("span", "queue-pct", `${Math.round(shot.progress)}%${etaSuffix(shot)}`));
    }
    row.addEventListener("click", () => {
      state.selectedId = raw.id;
      state.followRender = false;
      render();
    });
    wireDrag(row, raw, "y");
    q.appendChild(row);
  });

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
        `Restyle the background sound using ${svc.label} (${svc.model}). ` +
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
          "Background sound replaced. Undo by editing it back — the old text is above."
        );
      },
    })
  );
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
  number("Background audio volume (1 = original)", opts().backgroundVolume ?? 0.15, 2, n => { opts().backgroundVolume = n; });
  const normalize = el("input"); normalize.type = "checkbox";
  normalize.checked = !!opts().normalizeAudio; normalize.disabled = busy;
  normalize.addEventListener("change", () => { opts().normalizeAudio = normalize.checked; markDirty(); });
  const normLabel = el("label", "field-note");
  normLabel.append(normalize, el("span", null, " Match scene audio levels")); details.appendChild(normLabel);
  const upload = el("input"); upload.type = "file"; upload.accept = "audio/*"; upload.disabled = busy;
  upload.setAttribute("aria-label", "Continuous background audio");
  upload.addEventListener("change", async () => {
    if (!upload.files[0]) return;
    const slug = state.slug, board = state.board;
    try {
      const ref = await API.uploadRef(slug, upload.files[0]);
      if (state.board !== board) return;
      opts().backgroundAudio = ref; markDirty(); renderFinal();
    } catch (err) { toast(err.message, "error"); }
  });
  details.append(el("div", "field-note", "Continuous background audio (loops across the whole cut)"), upload);
  if (opts().backgroundAudio) {
    const remove = el("button", "btn btn-sm", "Remove background audio"); remove.disabled = busy;
    remove.onclick = () => { delete opts().backgroundAudio; markDirty(); renderFinal(); };
    details.append(el("div", "field-note", opts().backgroundAudio.label || "Background audio attached"), remove);
  }
  const chain = el("button", "btn btn-sm", "Continue all scenes with Ref2VA");
  chain.disabled = busy || shots().length < 2;
  chain.onclick = () => {
    if (shots().some(s => s.startRef || s.endRef)) {
      toast("Remove Start/End frame anchors before enabling Ref2VA continuity.", "warn"); return;
    }
    shots().forEach((s, i, all) => {
      s.continuityRef = i ? {kind: "chain", from: all[i-1].id, mode: "reference", label: `Continue from shot ${i}`} : null;
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
    if ((raw.startRef && raw.startRef.kind === "chain") || raw.continuityRef) {
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

    const foot = el("div", "shot-foot");
    foot.appendChild(chip(shot.status));
    if (shot.status === "running") {
      foot.appendChild(el("span", "queue-pct", `${Math.round(shot.progress)}%${etaSuffix(shot)}`));
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
    if (e.target.closest(".shot-card, .queue-row")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    list.classList.add("dropping");
  });
  list.addEventListener("dragleave", (e) => {
    if (!list.contains(e.relatedTarget)) list.classList.remove("dropping");
  });
  list.addEventListener("drop", (e) => {
    if (e.target.closest(".shot-card, .queue-row")) return;
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

  const stillsBtn = el("button", "btn btn-ghost btn-sm", "Create Stills");
  stillsBtn.title = "Fast Krea-2 previews of this shot's start, middle and end";
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
      "Additional scene-background sounds for THIS clip — not the general Background Sound.\n" +
      "Use local environmental details such as a nearby bird, distant siren, or passing vehicle.";
    sa.addEventListener("input", () => {
      live().soundNote = sa.value;
      markDirty();
      updateResolvedPreview();
      syncTabDots();
    });
    sa.dataset.fkey = "sound-accents";

    const sp2 = el("div");
    const saHead = paneHint("Additional local background sound only", "do not repeat the general Background Sound");
    saHead.appendChild(el("div", "header-spacer"));
    saHead.appendChild(
      wandButton({
        title: (svc) =>
          `Propose local background sound accents for this shot using ${svc.label} (${svc.model}). ` +
          `Adds only local scene-background sounds not already in the general ` +
          `Background Sound; dialogue and foreground action Foley are never included. ` +
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
        modelNow === "fl2va"
          ? `${withMedia.length} character reference set(s) are selected, but FL2VA does not send separate Cast media. Bake the character into the Start/End frame or remove the anchors for Ref2VA.`
          : modelNow === "wan-i2v"
          ? `${withMedia.length} character reference set(s) are selected, but Wan 2.2 does not send separate Cast media either — only the shot prompt and its required Start frame reach the model. Describe appearance in the Cast description instead.`
          : `${withMedia.length} character reference set(s) will be included in the Ref2VA request.`;
      castPanel.appendChild(el("div", "inline-warn", mediaMsg));
    }
    host.appendChild(castPanel);
  }

  // Start/End anchors activate FL2VA (or, for Wan, its required Start-only
  // anchor -- Wan has no text-only mode, unlike FL2VA/Ref2VA). Without
  // anchors, the same panel's other reference material is sent through
  // Ref2VA -- and never through Wan, which has no reference-list mode at all.
  const isVideoShot = cap && cap.kind !== "image";
  if (isVideoShot) {
    const refPanel = el("div", "panel");
    refPanel.style.marginTop = "var(--sp-3)";
    const modelNow = effectiveShotModel(raw);
    const anchorMode = modelNow === "fl2va" || modelNow === "wan-i2v";
    refPanel.appendChild(paneHint(
      "Start & end references",
      modelNow === "fl2va"
        ? "FL2VA hard anchors for the opening and closing composition"
        : modelNow === "wan-i2v"
        ? "Wan 2.2 requires a Start frame; End frame is not sent"
        : "Ref2VA cues for the opening and closing composition"
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
      cb.disabled = !!raw.continuityRef;
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
               el("span", null, `Chain start frame from shot ${idx}`));
      wrap.appendChild(t);
      const soft = el("label", "toggle");
      const softBox = el("input");
      softBox.type = "checkbox";
      softBox.checked = !!raw.continuityRef;
      softBox.disabled = !!(raw.startRef || raw.endRef);
      softBox.addEventListener("change", () => {
        live().continuityRef = softBox.checked
          ? { kind: "chain", from: prev.id, mode: "reference", label: `Continue from shot ${idx}` }
          : null;
        markDirty();
        render();
      });
      soft.append(softBox, el("span", "toggle-track"),
        el("span", null, `Continue from shot ${idx} using Ref2VA references`));
      wrap.append(soft, el("div", "field-note",
        "Keeps cast portraits and native voice references. Guides the opening; does not pin an exact frame. Remove Start/End anchors to enable."));
      if (raw.continuityRef) wrap.appendChild(refSlot("Previous scene reference", raw, "continuityRef"));
      refPanel.appendChild(wrap);
    }
    refPanel.appendChild(
      el(
        "div",
        "hint-body",
        modelNow === "fl2va"
          ? "FL2VA wires Start frame and End frame directly to the model's first/last-frame inputs. Separate Cast, style and shot-reference images are not sent on this path."
          : modelNow === "wan-i2v"
          ? "Wan 2.2 wires Start frame directly to the model's image-to-video input, and requires one — this checkpoint has no text-only mode, and rendering without a Start frame set will fail. End frame is not sent — Wan has no port for one. Separate Cast, style and shot-reference images are not sent either."
          : "With no frame anchors, Ref2VA receives character, style and other reference images as an ordered reference set."
      )
    );
    host.appendChild(refPanel);
    const trim = el("div", "panel");
    trim.appendChild(el("div", "section-label", "Trim for the final cut"));
    for (const [key, title] of [["trimIn", "Remove from start (seconds)"], ["trimOut", "Remove from end (seconds)"]]) {
      const label = el("label", "field-note", title + " ");
      const input = el("input"); input.type = "number"; input.min = "0"; input.step = "0.05";
      input.value = String(raw[key] || 0);
      input.addEventListener("change", () => {
        const n = Number(input.value);
        if (Number.isFinite(n) && n >= 0) { live()[key] = n; markDirty(); }
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
    const note = noteModel === "fl2va"
      ? "FL2VA is active because this shot has a Start/End frame anchor. These separate images are retained on the board but are not sent; remove the anchors to use them through Ref2VA."
      : noteModel === "wan-i2v"
      ? "Wan 2.2 is the selected video engine. These separate images are retained on the board but are not sent — Wan only reads the shot prompt and its required Start frame."
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
  params.appendChild(el("div", "section-label", "Parameters"));

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

const STILL_PHASE_LABELS = { start: "Start", mid: "Middle", end: "End" };

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
    const order = ["start", "mid", "end"];
    const i = Math.max(0, order.indexOf(st.phase));
    wrap.appendChild(
      el("div", "stills-status",
         `Generating ${STILL_PHASE_LABELS[st.phase] || "…"} ` +
         `(${Math.min(i + 1, 3)} of 3)…`)
    );
  } else if (failedHere) {
    wrap.appendChild(el("div", "stills-status stills-error", `Stills failed: ${st.error}`));
  }

  // While a job for this shot is running (or just failed partway through),
  // state.status.stills.results — updated as each phase finishes — wins
  // over the board's own copy, which is only refetched once the whole job
  // ends. That is what lets "start" show up the moment it is done instead
  // of waiting on "mid" and "end" too.
  const live = (runningHere || failedHere) && st.results ? st.results : null;
  const stills = live || raw.stills;

  if (!stills || !stills.start) {
    if (!runningHere) {
      wrap.appendChild(
        el("div", "empty-state",
           "No stills yet — “Create Stills” renders fast Krea-2 previews of " +
           "this shot's start, middle and end.")
      );
    }
    return wrap;
  }

  const grid = el("div", "stills-grid");
  ["start", "mid", "end"].forEach((key) => {
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

  // Stills are a second view of the same output card, on equal footing with
  // the rendered clip — not a debug/detail tab, so this toggle lives right
  // at the top rather than down with Details / Backend output.
  const mainTabs = el("div", "tabs");
  mainTabs.classList.add("output-tabs");
  const activeMainTab = state.previewMainTab || "clip";
  [
    { id: "clip", label: "Clip" },
    { id: "final", label: "Final Video" },
    { id: "stills", label: "Stills" },
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
  host.appendChild(mainTabs);

  if (activeMainTab === "stills") {
    host.appendChild(renderStillsPane(raw));
  } else if (activeMainTab === "final") {
    host.appendChild(renderFinalPane());
  } else {
    const stage = el("div", "preview-stage");
    // A native H3 render already contains the cloned character voice.  A
    // clip-dubbed URL can survive from an older recording-mode take (for
    // example when the server is restarted just as a render finishes), and
    // must not replace that native soundtrack in the preview.
    const video = (raw.renderedDialogueSource !== "native" && raw.dubUrl) ||
      (shot.outputs || []).find((u) => hasExt(u, "mp4"));
    const image = (shot.outputs || []).find((u) => hasExt(u, "jpe?g|png|webp"));
    if (video) {
      stage.appendChild(
        reuse(video, () => {
          const v = el("video");
          v.src = video;
          v.controls = true;
          v.loop = true;
          v.muted = outputMuted();
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
            ? `Rendering — ${Math.round(shot.progress)}%${shot.phase ? ` (${shot.phase})` : ""}${etaSuffix(shot)}`
            : "Not rendered yet"
        )
      );
      stage.appendChild(e);
    }
    host.appendChild(stage);

    if (video) {
      const actions = el("div", "preview-actions");
      const download = el("a", "btn btn-sm btn-primary", "Download clip");
      download.href = video;
      download.download = sceneClipDownloadName(raw, video);
      download.title = "Download this scene clip";
      actions.appendChild(download);
      host.appendChild(actions);
    }
  }

  // A plain-language read of what the render is doing, always in view —
  // the technical log below is the detail view for whoever wants it.
  host.appendChild(renderStageTiles(raw, shot));

  staleNotes(raw, shot.status).forEach((n) => host.appendChild(n));

  const diag = diagnostic(raw, shot);
  if (diag) host.appendChild(diag);

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
  host.append(detailTabs, detailPanes);

  const detailsPane = el("div", "tab-pane");
  detailsPane.dataset.tab = "details";
  const stats = el("dl", "stat-grid");
  [
    ["Status", STATUS_LABELS[shot.status] || shot.status],
    ["Model", (modelCap(effectiveShotModel(raw)) || {}).label || effectiveShotModel(raw)],
    ["Runtime", dur(shot.runtimeSeconds)],
    ["Rendered", raw.renderedAs === "draft" ? "draft (384px long edge, 4 steps)"
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
  detailsPane.appendChild(sp);

  const logPane = el("div", "tab-pane");
  logPane.dataset.tab = "log";
  const lbl = el("div", "section-label", "Backend output");
  lbl.appendChild(el("span", "hint", "— newest first; stills, the render, and any spoken line"));
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
  // not just as a phase label in the Stills tab, so the same vpipe detail
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
  if (text.includes("h3 native speech requires") || text.includes("reference voice clip")) {
    return "H3-native speech needs a cast character assigned to this shot with a reference voice clip. Otherwise choose the separate-recording path.";
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

  const setupIssue = dialogueReadiness(raw);
  const reason =
    shot.reason || (shot.validation && shot.validation.reason) ||
    (setupIssue ? `${setupIssue.title}. ${setupIssue.body}` : "");
  if (reason) d.appendChild(el("div", "diag-body", reason));
  if (shot.status === "failed" || shot.status === "blocked") {
    const next = el("div", "diag-next");
    next.appendChild(el("strong", null, "Next step: "));
    next.appendChild(el("span", null, setupIssue ? setupIssue.action : diagnosticGuidance(reason)));
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

function paneHint(text, description) {
  if (description === undefined) {
    // Legacy single-line hint: plain small text, no heading styling.
    const h = el("div", "pane-head");
    h.appendChild(el("span", "pane-hint", text));
    return h;
  }
  // The heading and its description stay together in one column so an
  // action button appended after (see dialogueHead) sits beside that
  // column instead of wedging between the two lines. Pass `null` for
  // description to get a bare, capitalised heading with no note line.
  const h = el("div", "pane-head pane-head-with-note");
  const col = el("div", "pane-head-text");
  col.appendChild(el("div", "pane-label", text));
  if (description) col.appendChild(el("div", "pane-note", "— " + description));
  h.appendChild(col);
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
