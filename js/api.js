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
    throw new Error((data && data.error) || `${res.status} ${res.statusText}`);
  }
  return data;
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
  deleteBoard: (slug) => req("DELETE", `/api/boards/${encodeURIComponent(slug)}`),

  // A rewrite proposal. `text` is sent explicitly because the user may not
  // have saved the words they just typed.
  rewrite: (slug, shotId, text, service) =>
    req("POST", "/api/rewrite", { slug, shotId, text, service }),

  // Returns the NEW slug: renaming moves the project folder, so the caller
  // has to stop using the old one.
  renameBoard: (slug, name) =>
    req("POST", `/api/boards/${encodeURIComponent(slug)}/rename`, { name }),
  exportUrl: (slug) => `/api/boards/${encodeURIComponent(slug)}/export`,

  addShot: (slug, patch) =>
    req("POST", `/api/boards/${encodeURIComponent(slug)}/shots`, patch || {}),

  // Browsers disagree about audio types and sometimes report none at all, so
  // fall back to one derived from the extension rather than sending "".
  uploadRef: (slug, file) =>
    req("POST", `/api/boards/${encodeURIComponent(slug)}/refs`, file, {
      "Content-Type": file.type || guessType(file.name),
      "X-Filename": file.name,
    }),

  library: () => req("GET", "/api/library"),

  transcribe: (path, engine) => req("POST", "/api/transcribe", { path, engine }),

  // copy an existing workspace image into this board's refs/ so the project
  // folder stays self-contained
  adoptRef: (slug, path) =>
    req("POST", `/api/boards/${encodeURIComponent(slug)}/refs/adopt`, { path }),

  dub: (slug, shotId) =>
    req("POST", `/api/boards/${encodeURIComponent(slug)}/shots/${encodeURIComponent(shotId)}/dub`, {}),

  render: (slug, shotIds) => req("POST", "/api/render", { slug, shotIds }),
  stop: () => req("POST", "/api/stop"),
  status: () => req("GET", "/api/status"),
};
