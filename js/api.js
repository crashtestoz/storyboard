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

const API = {
  info: () => req("GET", "/api/info"),

  listBoards: () => req("GET", "/api/boards"),
  getBoard: (slug) => req("GET", `/api/boards/${encodeURIComponent(slug)}`),
  saveBoard: (slug, board) =>
    req("PUT", `/api/boards/${encodeURIComponent(slug)}`, board),
  createBoard: (name) => req("POST", "/api/boards", { name }),
  importBoard: (board, name) => req("POST", "/api/boards", { board, name }),
  deleteBoard: (slug) => req("DELETE", `/api/boards/${encodeURIComponent(slug)}`),
  exportUrl: (slug) => `/api/boards/${encodeURIComponent(slug)}/export`,

  addShot: (slug, patch) =>
    req("POST", `/api/boards/${encodeURIComponent(slug)}/shots`, patch || {}),

  uploadRef: (slug, file) =>
    req("POST", `/api/boards/${encodeURIComponent(slug)}/refs`, file, {
      "Content-Type": file.type,
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
