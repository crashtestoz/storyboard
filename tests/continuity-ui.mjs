// Real browser regression on a temporary board. Requires Chrome CDP on 9222.
import assert from 'node:assert/strict';
const targets = await (await fetch(`http://127.0.0.1:${process.env.CDP_PORT || 9222}/json/list`)).json();
const page = targets.find(t => t.type === 'page' && t.url.includes('9877'));
assert.ok(page, 'Open Storyboard in the debug browser');
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise(resolve => { ws.onopen = resolve; });
let id = 0;
const pending = new Map();
ws.onmessage = event => { const m = JSON.parse(event.data); if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
const evaluate = expression => new Promise(resolve => {
  pending.set(++id, resolve);
  ws.send(JSON.stringify({id, method:'Runtime.evaluate', params:{expression,awaitPromise:true,returnByValue:true}}));
});
const result = await evaluate(`(async () => {
  let slug;
  const assert = (condition,message) => { if (!condition) throw new Error(message); };
  try {
    const created = await API.createBoard('Continuity browser regression ' + Date.now());
    slug = created.slug;
    await API.addShot(slug, {id:'s1',prompt:'A frog stands in a room.'});
    await API.addShot(slug, {id:'s2',prompt:'The frog continues speaking.'});
    const loaded = await API.getBoard(slug);
    state.slug = slug; state.board = loaded.board; state.selectedId = state.board.shots[1].id;
    state.status = {busy:false,stills:{busy:false}}; state.stale = {shots:{}};
    render();
    const label = [...document.querySelectorAll('label')].find(e => e.textContent.includes("Chain start frame from shot"));
    assert(label, 'Chain-start control is visible');
    label.querySelector('input').click();
    assert(state.board.shots[1].startRef.from === state.board.shots[0].id, 'Links previous scene');
    assert(effectiveShotModel(state.board.shots[1]) === 'ref2va', 'Keeps Ref2VA active');
    await saveNow();
    const saved = await API.getBoard(slug);
    assert(saved.board.shots[1].startRef.kind === 'chain', 'Chain-start persists');
    const preview = await req('POST','/api/render-preview',{board:state.board,shot:state.board.shots[1]});
    assert(preview.model === 'ref2va' && preview.references[0].name === 'Previous scene', 'Resolved preview identifies reference');
    const host = document.createElement('div'); document.body.appendChild(host);
    renderCutSettings(host,false);
    const crossfade = [...host.querySelectorAll('label')].find(e => e.textContent.includes('Crossfade'));
    crossfade.querySelector('input').value = '0.25';
    crossfade.querySelector('input').dispatchEvent(new Event('change'));
    await saveNow();
    assert((await API.getBoard(slug)).board.assembly.transitionSeconds === .25, 'Cut settings persist');
    const prepare = [...host.querySelectorAll('button')].find(e => e.textContent === 'Prepare all dialogue');
    assert(prepare, 'Batch speech action exists');
    const status = await API.prepareDialogue(slug);
    assert(status.operation === 'dialogue', 'Speech-only queue is distinct from video rendering');
    for (let n=0; n<20; n++) { if (!(await API.status()).busy) break; await new Promise(r=>setTimeout(r,100)); }
    assert(!(await API.status()).busy, 'Empty dialogue preparation finishes');
    assert(batchOutcome({operation:'dialogue',error:'',cancelRequested:false}).msg.includes('ready'), 'Speech status is readable');
    host.remove();
    return 'pass: live continuity controls, persistence, resolved references, cut settings and dialogue-only queue';
  } finally {
    clearTimeout(state.saveTimer); stopPolling();
    if (slug) await API.deleteBoard(slug, (await API.getBoard(slug)).board.name);
    state.slug = null; state.board = null;
  }
})()`);
ws.close();
assert.ok(!result.result?.exceptionDetails, result.result?.exceptionDetails?.exception?.description);
console.log(result.result.result.value);
