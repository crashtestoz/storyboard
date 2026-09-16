// Exercise the actual delete handler without touching a user's server or boards.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
const handler = source.slice(source.indexOf('async function deleteProject()'), source.indexOf('async function renameProject()'));
function setup({ typed = 'My board', confirmed = true, busy = false, error = false } = {}) {
  const calls = [];
  const button = {};
  const context = vm.createContext({
    state: {slug: 'my-board', board: {name: 'My board'}, status: {busy}, pendingSaves: new Set()},
    prompt: (message, initial) => { assert.equal(initial, ''); calls.push('prompt'); return typed; },
    confirm: () => { calls.push('confirm'); return confirmed; },
    toast: message => calls.push(message),
    $: () => button,
    clearTimeout() {},
    API: {deleteBoard: async (slug, name) => {
      calls.push('delete'); assert.equal(slug, 'my-board'); assert.equal(name, 'My board');
      if (error) throw new Error('Server blocked deletion');
    }},
    stopPolling() {}, markDirty() {}, LAST_OPENED_KEY: 'last',
    localStorage: {removeItem() {}}, window: {location: {reload() { calls.push('reload'); }}},
  });
  vm.runInContext(handler, context);
  return {context, calls, button};
}
for (const options of [{typed: null}, {typed: ''}, {typed: 'wrong'}, {confirmed: false}, {busy: true}]) {
  const t = setup(options);
  await t.context.deleteProject();
  assert.ok(!t.calls.includes('delete'));
}
const success = setup();
let finishSave;
success.context.state.pendingSaves.add(new Promise(resolve => { finishSave = resolve; }));
const deletion = success.context.deleteProject();
await Promise.resolve();
assert.ok(!success.calls.includes('delete'), 'waits for pending saves');
finishSave();
await deletion;
assert.deepEqual(success.calls, ['prompt', 'confirm', 'delete', 'reload']);
assert.equal(success.context.state.board, null);
const failure = setup({error: true});
await failure.context.deleteProject();
assert.equal(failure.context.state.board.name, 'My board');
assert.equal(failure.context.state.deletingBoard, false);
assert.equal(failure.button.disabled, false);
assert.ok(!failure.calls.includes('reload'));
console.log('pass: cancellation, exact name, final confirmation, busy guard, pending saves, success and failure');
