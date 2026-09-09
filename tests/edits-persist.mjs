// Regression test: an edit must reach the server every time, not just once.
//
// saveNow() used to do `state.board = board` with the server's response. That
// response is only the board we just sent, migrated and re-stamped — but
// assigning it replaced every shot object, so the references the editor's
// handlers had closed over became orphans. The first autosave worked; every
// keystroke after it was written into a detached object and dropped, while the
// indicator read "saved". It also discarded anything typed while the request
// was in flight.
//
// Two things are asserted here:
//   1. repeated edits with autosaves between them all reach the server
//   2. an edit still lands after state.board is legitimately replaced —
//      which happens on rename, on opening another board, and when a render
//      batch finishes and the board is re-read
//
// This runs against a throwaway board of its own and deletes it afterwards,
// so it cannot touch real work.
//
// Run:
//   ./serve.sh &
//   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
//      --headless=new --remote-debugging-port=9222 --remote-allow-origins='*' \
//      --user-data-dir=/tmp/cdp http://localhost:9877/ &
//   node tests/edits-persist.mjs        # CDP_PORT=9222 by default

const base = `http://127.0.0.1:${process.env.CDP_PORT || 9222}`;
const targets = await (await fetch(base + "/json/list")).json();
const page = targets.find((t) => t.type === "page" && t.url.includes("9877"));
if (!page) {
  console.error("no storyboard page open in the debug browser");
  process.exit(1);
}

const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0;
const pending = new Map();
ws.onmessage = (m) => {
  const d = JSON.parse(m.data);
  if (pending.has(d.id)) {
    pending.get(d.id)(d.result);
    pending.delete(d.id);
  }
};
await new Promise((r) => (ws.onopen = r));

const evaluate = (expression) =>
  new Promise((res) => {
    pending.set(++id, (r) =>
      res(
        r.exceptionDetails
          ? { __error: r.exceptionDetails.exception?.description || "threw" }
          : r.result.value
      )
    );
    ws.send(
      JSON.stringify({
        id,
        method: "Runtime.evaluate",
        params: { expression, awaitPromise: true, returnByValue: true },
      })
    );
  });

const out = await evaluate(`(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const log = [];

  // --- a board of this test's own -----------------------------------------
  const made = await API.createBoard("Autosave Regression Test");
  const slug = made.slug;
  await openBoard(slug);
  const added = await API.addShot(slug, {});
  await openBoard(slug);
  const shotId = state.board.shots[state.board.shots.length - 1].id;
  state.selectedId = shotId;
  state.tab = "prompt";
  render();

  const field = () => document.querySelector('#editor [data-fkey="shot-prompt"]');
  const type = (text) => {
    const ta = field();
    ta.value = text;
    ta.dispatchEvent(new Event("input", { bubbles: true }));
  };
  // what the server actually has, not what the page thinks it has
  const onServer = async () => {
    const { board } = await API.getBoard(slug);
    const sh = board.shots.find((s) => s.id === shotId);
    return (sh && sh.prompt) || "";
  };

  try {
    // --- 1. three edits, an autosave between each --------------------------
    for (const word of ["alpha", "beta", "gamma"]) {
      type("take " + word);
      await sleep(1400);                       // autosave debounce is 700ms
      const server = await onServer();
      log.push(["edit \\"" + word + "\\" reached the server", server === "take " + word,
                "server has: " + JSON.stringify(server)]);
    }

    // --- 2. an edit after the board object is replaced ---------------------
    // Exactly what rename and the batch-finished reload do.
    const { board: reread } = await API.getBoard(slug);
    state.board = reread;
    type("after the board was replaced");
    await sleep(1400);
    const server = await onServer();
    log.push(["edit after state.board was replaced still saves",
              server === "after the board was replaced",
              "server has: " + JSON.stringify(server)]);

    // --- 3. the indicator must not claim success it did not have ----------
    log.push(['save indicator reads "saved"',
              document.getElementById("saveState").textContent === "saved",
              "reads: " + document.getElementById("saveState").textContent]);
  } finally {
    await API.deleteBoard(slug);
    state.boards = (await API.listBoards()).boards;
    const first = state.boards[0];
    if (first) await openBoard(first.slug);
  }

  return log;
})()`);

ws.close();

if (out && out.__error) {
  console.error("probe threw:", out.__error);
  process.exit(1);
}

let failed = 0;
for (const [name, ok, detail] of out) {
  console.log(`${ok ? "pass" : "FAIL"}  ${name}${ok ? "" : `  — ${detail}`}`);
  if (!ok) failed++;
}
process.exit(failed ? 1 : 0);
