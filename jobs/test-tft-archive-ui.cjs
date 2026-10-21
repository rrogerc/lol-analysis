// node jobs/test-tft-archive-ui.cjs
// Archived composition links use the live leaderboard without composition requests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script);
const section = (from, to) => {
  const start = script.indexOf(from), end = script.indexOf(to, start);
  assert(start >= 0 && end > start, `Missing page section: ${from}`);
  return script.slice(start, end);
};

const modeButtons = html.split('id="tft-mode-seg"')[1].split('</div>')[0];
assert(!modeButtons.includes('data-key="compositions"'));
for (const mode of ['builds', 'leaderboard', 'early', 'traits']) {
  assert(modeButtons.includes(`data-key="${mode}"`));
}

const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, { hidden: false, dataset: {} });
  return elements.get(id);
};
const tstate = { mode: 'builds', meta: { revision: 'saved' } };
const tboard = { fromLeaderboard: false };
const tcomp = { fromComposition: false };
const loads = [];
const context = vm.createContext({ tstate, tboard, tcomp,
  document: { getElementById: element },
  hideTftTooltip() {}, tftActive() {}, updateHash() {}, scheduleTftStatus() {},
  loadTftLeaderboard: async () => loads.push('leaderboard'),
  loadTftScenario: async () => loads.push('builds'),
  loadTftCompositions: async () => { throw new Error('Archived compositions requested'); },
});
vm.runInContext(section('function renderTftMode()', 'function renderTftLeaderboardControls()'), context);

async function checkArchive() {
  await context.setTftMode('compositions');
  assert.equal(tstate.mode, 'leaderboard');
  assert.deepEqual(loads, ['leaderboard']);
  assert.equal(element('tft-leaderboard-panel').hidden, false);
  assert.equal(element('tft-compositions-panel').hidden, true);
  tcomp.fromComposition = true;
  context.renderTftMode();
  assert.equal(element('tft-composition-back').hidden, true);
  assert.equal(element('tft-composition-note').hidden, true);

  // Exercise the real URL restoration block, including older champion links.
  const restore = section('    if (hash.tab === "tft") {', '    if (view === "champion")');
  for (const hash of [
    { tab: 'tft', tftview: 'compositions', cplan: 'c4', citems: '9' },
    { tab: 'tft', cfrom: '1', unit: 'nidalee', star: '2' },
    { tab: 'tft', tftview: 'early' },
    { tab: 'tft', tftview: 'traits' },
  ]) {
    const restoredState = { mode: 'builds' }, restoredComp = { fromComposition: false };
    const ctx = vm.createContext({ hash, tstate: restoredState, tcomp: restoredComp, tboard: {},
      restoreTftEarlyHash() {}, restoreTftFlexHash() {}, setTftMode(mode) { restoredState.mode = mode; },
    });
    vm.runInContext(restore, ctx);
    assert.equal(restoredState.mode, hash.tftview === 'compositions' ? 'leaderboard' : hash.tftview || 'builds');
    assert.equal(restoredComp.fromComposition, false);
    if (hash.unit) assert.equal(restoredState.unit, hash.unit);
  }
  console.log('TFT composition archive UI checks passed');
}
checkArchive().catch(error => { console.error(error); process.exitCode = 1; });
