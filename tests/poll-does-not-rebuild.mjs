// Regression test: a poll tick must not rebuild the page.
//
// The status poll runs once a second while a render is in flight. It used to
// call render(), which rebuilt the rail, strip, editor and preview from
// scratch — so every image re-decoded and any <video> reloaded (a visible
// flicker once a second), and whatever field you were typing in was destroyed
// and recreated, dropping the caret. Two separate bugs, one cause.
//
// This drives the real page over the DevTools protocol with a faked "running"
// status and asserts the nodes survive the tick, while the progress figures
// still update — and that a genuine status change does still rebuild, so a
// finished shot's thumbnail and video appear.
//
// Run:
//   ./serve.sh &
//   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
//      --headless=new --remote-debugging-port=9223 --remote-allow-origins='*' \
//      --user-data-dir=/tmp/cdp http://localhost:9877/ &
//   node tests/poll-does-not-rebuild.mjs
// Drive the live storyboard page and prove two things about a running render:
//   1. a poll tick does not rebuild the DOM (no flicker)
//   2. a poll tick does not steal focus or the caret (editing stays possible)
const base = "http://127.0.0.1:9223";
const targets = await (await fetch(base + "/json/list")).json();
const page = targets.find(t => t.type === "page" && t.url.includes("9877"));
if (!page) { console.log("NO PAGE", targets.map(t=>t.url)); process.exit(1); }
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pending = new Map();
const send = (method, params={}) => new Promise(res => { pending.set(++id, res); ws.send(JSON.stringify({id, method, params})); });
ws.onmessage = (m) => { const d = JSON.parse(m.data); if (pending.has(d.id)) { pending.get(d.id)(d.result); pending.delete(d.id); } };
await new Promise(r => ws.onopen = r);

const evalJS = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) return { __error: r.exceptionDetails.exception?.description || "threw" };
  return r.result.value;
};

const out = await evalJS(`(async () => {
  const log = [];

  // Pretend a render is in flight, exactly as the server would report it.
  const sid = state.board.shots[0].id;
  // runs is a dict keyed by shot id, matching orchestrator.status()
  const busy = () => ({
    busy: true,
    runs: { [sid]: { shotId: sid, status: "running", progress: 41,
                     phase: "denoise", log: [{level:"PROGRESS", text:"tick"}] } },
  });
  const realStatus = API.status;
  API.status = async () => busy();
  state.status = busy();

  // Focus a shot field and put the caret mid-word, as if mid-edit. The tab is
  // set explicitly: a field in a hidden tab pane cannot take focus, so the
  // test would otherwise depend on whichever tab was last open.
  state.selectedId = sid;
  state.tab = "prompt";
  render();
  // Capture the signature only once the page is in the state under test —
  // otherwise the setup itself counts as a structural change and the first
  // tick legitimately rebuilds.
  state.sig = structuralSig();
  const box = document.querySelector('#editor [data-fkey="shot-prompt"]')
           || document.querySelector('#editor textarea');
  box.focus();
  box.setSelectionRange(7, 7);
  const before = {
    fkey: document.activeElement.dataset.fkey,
    caret: box.selectionStart,
    node: box,
    strip: document.querySelector('.shot-card .shot-thumb img'),
    preview: document.querySelector('.preview-stage > video, .preview-stage > img'),
  };

  // Three poll ticks, the same call the 1s interval makes.
  for (let i = 0; i < 3; i++) await refreshStatus();

  const after = document.activeElement;
  log.push(["focus kept on the field", after === before.node]);
  log.push(["caret still at 7", after.selectionStart === 7]);
  log.push(["editor field is the same node (not rebuilt)",
            document.querySelector('#editor [data-fkey="shot-prompt"]') === before.node]);
  log.push(["strip <img> is the same node (no re-decode)",
            document.querySelector('.shot-card .shot-thumb img') === before.strip]);
  log.push(["preview media is the same node (no reload)",
            document.querySelector('.preview-stage > video, .preview-stage > img') === before.preview]);
  log.push(["progress actually updated to 41%",
            !!document.querySelector('.progress-fill') &&
            document.querySelector('.progress-fill').style.width === "41%"]);

  // And prove the gate still fires a full rebuild when something real changes.
  API.status = async () => ({ busy: true, runs: { [sid]: { shotId: sid,
      status: "done", progress: 100, outputs: ["x.mp4"] } } });
  await refreshStatus();
  log.push(["a real status change DOES rebuild",
            document.querySelector('#editor [data-fkey="shot-prompt"]') !== before.node]);

  API.status = realStatus;
  return log;
})()`);
ws.close();
if (out && out.__error) {
  console.error("probe threw:", out.__error);
  process.exit(1);
}
let failed = 0;
for (const [name, ok] of out) {
  console.log(`${ok ? "pass" : "FAIL"}  ${name}`);
  if (!ok) failed++;
}
process.exit(failed ? 1 : 0);
