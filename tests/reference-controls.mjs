// Exercise the actual UI handlers and save path with an isolated DOM/API.
// Run: node tests/reference-controls.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
class Element {
  children = []; listeners = {}; style = {}; dataset = {};
  classList = { add() {}, remove() {} };
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { this.children.push(...children); }
  setAttribute(key, value) { this[key] = value; }
  addEventListener(event, fn) { this.listeners[event] = fn; }
}
const indicator = new Element();
let saved;
const context = vm.createContext({
  document: { createElement: () => new Element(), addEventListener() {}, querySelector: () => indicator },
  setTimeout: () => 1, clearTimeout() {},
  API: {
    saveBoard: async (_, board) => {
      saved = structuredClone(board);
      return { board: saved };
    },
    uploadRef: async () => ({ path: 'uploaded.png' }),
  },
  assert,
});
vm.runInContext(fs.readFileSync(new URL('../js/app.js', import.meta.url), 'utf8'), context);
await vm.runInContext(`(async () => {
  render = () => {}; paintStale = () => {};
  toast = (message) => { throw new Error(message); };
  let chosen = { path: 'replacement.png' };
  chooseImage = async () => chosen;
  state.slug = 'test';
  const reset = () => {
    state.board = { shots: [{ id: 's1', startRef: { path: 'start.png' },
      endRef: { path: 'end.png' }, referenceImages: [{ path: 'one.png' }, { path: 'two.png' }] }] };
    return state.board.shots[0];
  };
  const click = (slot, text) => text === 'Replace'
    ? slot.listeners.click()
    : slot.children.find(c => c.className === 'pick-delete').listeners.click({ stopPropagation() {} });
  for (const key of ['startRef', 'endRef']) {
    const original = reset();
    const slot = refSlot(key, original, key);
    // A server refresh replaces the board while these controls are mounted.
    state.board = JSON.parse(JSON.stringify(state.board));
    await click(slot, 'Remove');
    assert.equal(state.board.shots[0][key], null);
    assert.ok(original[key]);
    await click(slot, 'Replace');
    assert.equal(state.board.shots[0][key].path, 'replacement.png');
    assert.equal(state.board.shots[0].model, 'fl2va');
    chosen = null;
    await click(slot, 'Replace');
    assert.equal(state.board.shots[0][key].path, 'replacement.png');
    chosen = { path: 'replacement.png' };
    await slot.ondrop({ preventDefault() {}, stopPropagation() {}, dataTransfer: { files: [{ type: 'image/png' }] } });
    assert.equal(state.board.shots[0][key].path, 'uploaded.png');
  }
  let original = reset();
  original.startRef = { kind: 'chain', from: 's0', label: 'previous shot' };
  await click(refSlot('Start frame', original, 'startRef'), 'Remove');
  assert.equal(original.startRef, null);
  original = reset();
  const slots = shotReferenceImages(original);
  state.board = JSON.parse(JSON.stringify(state.board));
  await click(slots.children[0], 'Replace');
  assert.equal(state.board.shots[0].referenceImages[0].path, 'replacement.png');
  assert.equal(state.board.shots[0].model, 'fl2va');
  await click(slots.children[1], 'Remove');
  assert.equal(state.board.shots[0].referenceImages.length, 1);
  assert.equal(original.referenceImages.length, 2);
})()`, context);
assert.equal(saved.shots[0].referenceImages.length, 1);
assert.equal(saved.shots[0].referenceImages[0].path, 'replacement.png');
assert.equal(indicator.textContent, 'saved');
console.log('pass: remove, replace, cancel, drop, chain removal, refreshed boards and persistence');
