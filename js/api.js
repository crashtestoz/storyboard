/* ==========================================================================
   API client. Thin wrappers over fetch — no state, no caching.
   ========================================================================== */

"use strict";

async function req(method, url, body, headers) {
  const opts = { method, headers: headers || {} };
  if (body !== undefined && !(body instanceof Blob)) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  } else if (body instanceof Blob) {
    opts.body = body;
  }
  const res = await fetch(url, opts);
  const text = await res.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { raw: text };
    }
  }
  if (!res.ok) {
    const err = new Error((data && data.error) || `${res.status} ${res.statusText}`);
    // Carry the body: a failed speech run returns the engine's log with it,
    // and that log is the only thing that explains the failure.
    err.payload = data;
    err.status = res.status;
    throw err;
  }
  return data;
}

/* POST that answers with newline-delimited JSON events (see the server's
   _send_events). Each progress event goes to onEvent; the closing "result"
   event's payload is returned. Resolves like req() for errors that arrive as
   an ordinary HTTP failure, and throws on an in-stream "error" event or a
   connection that ends before a result. */
async function reqEvents(url, body, onEvent, signal) {
  const res = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body), signal,
  });
  if (!res.ok) {
    let data = null;
    try { data = JSON.parse(await res.text()); } catch { /* not JSON */ }
    const err = new Error((data && data.error) || `${res.status} ${res.statusText}`);
    err.payload = data;
    err.status = res.status;
    throw err;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result = null;
  const handle = (line) => {
    if (!line.trim()) return;
    const event = JSON.parse(line);
    if (event.type === "result") result = event.result;
    else if (event.type === "error") throw new Error(event.error);
    else if (onEvent) onEvent(event);
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      handle(buffer.slice(0, nl));
      buffer = buffer.slice(nl + 1);
    }
  }
  handle(buffer);
  if (result === null) throw new Error("The connection closed before the reply finished.");
  return result;
}

const EXT_TYPES = {
  png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", webp: "image/webp",
  wav: "audio/wav", mp3: "audio/mpeg", m4a: "audio/mp4", aac: "audio/aac",
  flac: "audio/flac", ogg: "audio/ogg", opus: "audio/opus",
};

function guessType(name) {
  const ext = (name || "").split(".").pop().toLowerCase();
  return EXT_TYPES[ext] || "application/octet-stream";
}

const API = {
  info: () => req("GET", "/api/info"),

  listBoards: () => req("GET", "/api/boards"),
  getBoard: (slug) => req("GET", `/api/boards/${encodeURIComponent(slug)}`),
  saveBoard: (slug, board) =>
    req("PUT", `/api/boards/${encodeURIComponent(slug)}`, board),
  createBoard: (name) => req("POST", "/api/boards", { name }),
  importBoard: (board, name) => req("POST", "/api/boards", { board, name }),
  deleteBoard: (slug, confirmName) =>
    req("DELETE", `/api/boards/${encodeURIComponent(slug)}`, { confirmName }),

  // A rewrite proposal, for a shot field (`shotId` plus optional `field`) or
  // a project-level field (`field`: "sceneDescription" | "soundscape"). `text` is sent
  // explicitly because the user may not have saved the words they just typed.
  rewrite: (slug, { shotId, field, text, service, speakerId } = {}) =>
    req("POST", "/api/rewrite", { slug, shotId, field, text, service, speakerId }),

  // How long a shot needs: action beats from the prompt plus the dialogue.
  // The editor's current text goes along, since it may not be saved yet.
  lengthEstimate: (slug, shotId, { prompt, dialogue, dialogueStyle } = {}) =>
    req("POST", "/api/length-estimate", { slug, shotId, prompt, dialogue, dialogueStyle }),

  // Streams: onProgress gets {phase, kind, thinking, reply, tps} as the model
  // works, so a slow local model shows life instead of timing out silently.
  // thinking: "" keeps the service's own level, else none|low|medium|high.
  chat: (slug, message, history, selectedShotId, service, onProgress, signal, thinking) =>
    reqEvents("/api/chat", {
      slug, message, history, selectedShotId, service, stream: true, thinking,
    }, onProgress, signal),

  describeCharacter: (image, voice, name, description, service) =>
    req("POST", "/api/describe-character", {
      image, voice, name, description, service,
    }),

  // Returns the NEW slug: renaming moves the project folder, so the caller
  // has to stop using the old one.
  renameBoard: (slug, name) =>
    req("POST", `/api/boards/${encodeURIComponent(slug)}/rename`, { name }),
  exportUrl: (slug) => `/api/boards/${encodeURIComponent(slug)}/export`,
  // Everything the project is made of; all=true also includes the rendered video.
  exportZipUrl: (slug, all = false) =>
    `/api/boards/${encodeURIComponent(slug)}/export.zip${all ? "?video=1" : ""}`,

  // Saved to server-config.json; takes effect on the next restart, not
  // immediately — see restartServer.
  setDataDir: (dataDir) => req("POST", "/api/server-settings", { dataDir }),
  // Only resolves once the request lands; the process re-execs itself right
  // after, so the caller has to poll info() to know when it's back.
  restartServer: () => req("POST", "/api/server-settings/restart"),
  setBackend: (backend) => req("POST", "/api/server-settings", { backend }),

  // Also saved to server-config.json, but read fresh on every chat turn —
  // no restart needed. An empty string disables web search again.
  setSearchUrl: (searchUrl) => req("POST", "/api/server-settings", { searchUrl }),

  // Which downloaded checkpoint an mflux still engine runs on this machine;
  // model "" = automatic (a downloaded one, else mflux fetches the default).
  // A prompt-rewriting service's API key, kept on this machine only; key ""
  // removes it. The reply carries each service's key *status*, never a key.
  setLlmKey: (service, key) =>
    req("POST", "/api/server-settings", { llmKey: { service, key } }),
  setLlmServices: (services) => req("POST", "/api/llm-services", { services }),
  setTtsServices: (services) => req("POST", "/api/tts-services", { services }),
  setMfluxEngines: (engines) => req("POST", "/api/mflux-engines", { engines }),

  setMfluxModel: (engine, model) =>
    req("POST", "/api/server-settings", { mfluxModel: { engine, model } }),

  addShot: (slug, patch) =>
    req("POST", `/api/boards/${encodeURIComponent(slug)}/shots`, patch || {}),

  // Browsers disagree about audio types and sometimes report none at all, so
  // fall back to one derived from the extension rather than sending "".
  // `name` (a file stem, e.g. "alex-voice") saves it under that name; the
  // extension still comes from the file.
  uploadRef: (slug, file, name) =>
    req("POST", `/api/boards/${encodeURIComponent(slug)}/refs`, file, {
      "Content-Type": file.type || guessType(file.name),
      "X-Filename": name ? name + (/\.[^.]+$/.exec(file.name) || [""])[0].toLowerCase() : file.name,
    }),

  // kind is "image" or "audio": a voice clip already uploaded to a project
  // has to be pickable, or it can never be linked to a character.
  library: (kind) =>
    req("GET", `/api/library?kind=${encodeURIComponent(kind || "image")}`),
  deleteLibraryItem: (kind, path) =>
    req(
      "DELETE",
      `/api/library?kind=${encodeURIComponent(kind || "image")}` +
        `&path=${encodeURIComponent(path)}`
    ),

  transcribe: (path, engine) => req("POST", "/api/transcribe", { path, engine }),

  // copy an existing workspace image into this board's refs/ so the project
  // folder stays self-contained
  adoptRef: (slug, path, name) =>
    req("POST", `/api/boards/${encodeURIComponent(slug)}/refs/adopt`, { path, name }),

  // `text` is sent so an unsaved line can still be previewed.
  dub: (slug, shotId, text, style, dubMode) =>
    req("POST", `/api/boards/${encodeURIComponent(slug)}/shots/${encodeURIComponent(shotId)}/dub`,
        text === undefined ? {} : { text, style: style || "", dubMode: dubMode || "mix" }),

  prepareDialogue: (slug) => req("POST", "/api/prepare-dialogue", { slug }),

  // Speaks one Storyboard AD reply aloud, in whatever voice the board's
  // defaults name (see `adSpeakerId`). Returns {audioUrl, seconds, ...}.
  speak: (slug, text) =>
    req("POST", `/api/boards/${encodeURIComponent(slug)}/speak`, { text }),

  render: (slug, shotIds, board) =>
    req("POST", "/api/render", { slug, shotIds, board }),

  // Seed comparison: the same shot at several seeds, rendered into takes
  // beside it — never over its own clip until one is picked.
  renderSeeds: (slug, shotId, count, board) =>
    req("POST", "/api/render-seeds", { slug, shotId, count, board }),
  seedTakes: (slug, shotId) =>
    req("GET", `/api/boards/${encodeURIComponent(slug)}/shots/${encodeURIComponent(shotId)}/seed-takes`),
  adoptSeedTake: (slug, shotId, seed) =>
    req("POST", `/api/boards/${encodeURIComponent(slug)}/shots/${encodeURIComponent(shotId)}/seed-takes/${seed}/adopt`),
  // Auto-refine: draft the shot, have the AD's model review the clip, adjust
  // the prompt, repeat. Progress rides on /api/status's `refine`; the board is
  // not touched until the user applies the proposed prompt.
  refine: (slug, shotId, { requirement, maxAttempts, draft, thinking, service } = {}, board) =>
    req("POST", "/api/refine", { slug, shotId, requirement, maxAttempts, draft, thinking, service, board }),
  deleteSeedTakes: (slug, shotId) =>
    req("DELETE", `/api/boards/${encodeURIComponent(slug)}/shots/${encodeURIComponent(shotId)}/seed-takes`),

  // Start/mid/end Krea-2 stills for one shot — a fast preview, not a render.
  stills: (slug, shotId) => req("POST", "/api/stills", { slug, shotId }),

  // A reference image made from a description -- kind "character" (a
  // portrait), "prop" (the object alone) or "location" (an empty place).
  // Runs as an image job; status().stills.results.image is the new ref.
  characterImage: (slug, { name, description, kind }) =>
    req("POST", "/api/character-image", { slug, name, description, kind }),

  // Render several projects in sequence, each with its own saved settings.
  renderBatch: (slugs) => req("POST", "/api/render-batch", { slugs }),

  // Join the rendered clips into one video. A whole-board render does this at
  // the end; this is the same pass on demand, for when nothing needs
  // re-rendering and only the cut is out of date.
  assemble: (slug) => req("POST", "/api/assemble", { slug }),

  // Where the board's soundtrack stands (waiting for scenes, ready, current,
  // stale…), and generating it now instead of waiting for assembly.
  soundtrackStatus: (slug) =>
    req("GET", `/api/boards/${encodeURIComponent(slug)}/soundtrack`),
  generateSoundtrack: (slug) =>
    req("POST", `/api/boards/${encodeURIComponent(slug)}/soundtrack`, {}),

  // "This clip really is a render of what the board says now" — an assertion
  // the user is entitled to make, and the only alternative to paying for a
  // re-render to prove it.
  acceptReview: (slug, shotId) =>
    req("POST", `/api/boards/${encodeURIComponent(slug)}/shots/${encodeURIComponent(shotId)}/accept-review`, {}),

  // Freeze one shot against edits, renders and project-setting changes (or
  // release it). Returns {board, stale}, the server's copy after the change.
  lockAll: (slug, locked) =>
    req("POST", `/api/boards/${encodeURIComponent(slug)}/lock-all`, { locked }),
  lockShot: (slug, shotId, locked) =>
    req("POST", `/api/boards/${encodeURIComponent(slug)}/shots/${encodeURIComponent(shotId)}/lock`, { locked }),

  accept: (slug, shotId) =>
    req("POST", `/api/boards/${encodeURIComponent(slug)}/shots/${encodeURIComponent(shotId)}/accept`, {}),
  stop: () => req("POST", "/api/stop"),
  // Launch an offline local engine server (Qwen3-TTS, Ollama); see
  // server/services.py for the only commands this can run.
  startService: (group, id) => req("POST", "/api/services/start", { group, id }),
  status: () => req("GET", "/api/status"),
  systemLoad: () => req("GET", "/api/system-load"),
};
