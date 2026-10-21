// Run: node jobs/test-tft-early-ui.cjs [--catalog /path/to/early.json]
// Exercises the published Early comps browser code without browser/npm dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script);
const section = (start, end) => script.slice(script.indexOf(start), script.indexOf(end));
const plain = value => JSON.parse(JSON.stringify(value));
const earlyState = () => ({ data: null, req: 0, controller: null, promise: null, loading: false, pending: false, error: null,
  level: 4, allowThree: true, unit: 'all', sort: 'wins', order: 'desc', page: 1, cards: new Map(), openBoards: new Set() });

function roster(level, { three = false, special = false, cheaper = false } = {}) {
  return Array.from({ length: level }, (_, i) => {
    const cost = three && i === level - 1 ? 3 : cheaper || i % 2 === 0 ? 1 : 2;
    const slug = (special && i === 0 ? 'special' : 'unit') + '-' + level + '-' + i + (cost === 3 ? '-three' : '');
    return { api: slug, slug, name: slug.toUpperCase(), cost, star: 1, slotCost: 1, icon: '/unit.png',
      frontline: i < 2, role: i < 2 ? 'Tank' : 'Caster', objective: i < 2 ? 'tank' : 'carry', traitApis: ['shared'],
      items: [], itemApis: [], dps: 50 + i, damage: 1000 + i * 50, aliveTime: 12 + i };
  });
}
function board(id, level, reference, options = {}) {
  const units = roster(level, options), wins = options.wins ?? 1;
  const matchups = reference.initiatives.map((initiative, i) => ({ key: id + '-trial-' + i, label: 'Early reference', opponentId: 'reference-' + level,
    initiative, laneOffset: 0, roster: plain(reference.boards[0].units), outcome: i < wins ? 'win' : 'loss', duration: 20,
    allyHpFraction: i < wins ? .4 : 0, enemyHpFraction: i < wins ? 0 : .4 }));
  return { id, rank: 1, level, boardSlots: level, slotsUsed: level, unitCount: level, mainCarry: units.at(-1).slug, mainTank: units[0].slug,
    purchaseGold: units.reduce((sum, unit) => sum + unit.cost, 0), itemCount: 0,
    threeCostCount: units.filter(unit => unit.cost === 3).length, activeTraitCount: 1, units,
    traits: [{ api: 'shared', name: 'Shared', icon: '/trait.png', count: level, active: true, breakpoint: 2,
      levels: [2, 4], modeled: true, coverage: 'partial', coverageNotes: ['Some combat effects are omitted.'] }],
    metrics: { benchmarkWins: wins, benchmarkCount: 2, benchmarkWinRate: wins / 2, benchmarkScore: wins * 50,
      benchmarkLosses: 2 - wins, benchmarkDraws: 0, benchmarkTimeouts: 0, damageDps: options.dps ?? 100,
      frontlineTime: options.frontline ?? 10, hpMargin: 0, clearTime: wins ? 20 : null }, matchups };
}
function fixture() {
  const referencePools = Object.fromEntries([4, 5].map(level => ['level' + level, { opponentCount: 1, encounterCount: 2,
    initiatives: [0, 1], laneOffsets: [0], boards: [{ id: 'reference-' + level, label: 'Early reference', units: roster(level) }] }]));
  const raw = { schema: 'tft-early-comps-v1', complete: true, evaluationModel: 'stage-two-reference-fights-v1',
    revision: 'early-v1', baselineRevision: 'baseline-v1', publicationRevision: 'site-v1', set: 18,
    patch: '18.3', stage: 2, starAssumption: 'All units at 1★', itemCount: 0,
    limitations: ['Items and augments are not included.'], search: { description: 'These are promising boards from a bounded deterministic search.' },
    referencePools, variants: [4, 5].map(level => ({ key: 'level' + level, label: level + ' units', level, boardSlots: level,
      maxThreeCosts: level === 5 ? 1 : 0, boards: [] })) };
  raw.variants[0].boards = Array.from({ length: 30 }, (_, i) => board('four-' + String(i).padStart(2, '0'), 4, referencePools.level4,
    { wins: i % 3, dps: 100 + i, frontline: 10 + i, special: i === 0, cheaper: i === 1 }));
  raw.variants[1].boards = [board('five-with-three', 5, referencePools.level5, { three: true, wins: 2 }),
    board('five-without-three', 5, referencePools.level5, { wins: 1 })];
  const units = new Map(raw.variants.flatMap(variant => variant.boards.flatMap(board => board.units)).map(unit => [unit.api, unit]));
  raw.teamPlanner = { format: 'tft-team-planner-v2', slots: 10, set: 18,
    unitCodes: Object.fromEntries([...units.values()].map((unit, i) => [unit.api, (i + 1).toString(16).padStart(3, '0')])) };
  return raw;
}

class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.events = {};
    this.style = { setProperty() {} }; this.className = ''; this.hidden = false; this.disabled = false; this.value = ''; this._text = '';
    this.classList = { add: name => this.className += ' ' + name, remove() {}, toggle() {} }; }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent ?? child).join(''); }
  append(...children) { for (const child of children) { if (typeof child === 'object') child.remove(); this.children.push(child); if (typeof child === 'object') child.parent = this; } }
  replaceChildren(...children) { this._text = ''; this.children = []; this.append(...children); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(type, callback) { (this.events[type] ||= []).push(callback); }
  async fire(type, extra = {}) { for (const callback of this.events[type] || []) await callback({ target: this, ...extra }); }
  querySelectorAll(selector) {
    const matches = child => selector.startsWith('.') ? child.className.split(' ').includes(selector.slice(1)) : child.tagName === selector;
    return this.children.flatMap(child => typeof child === 'object' ? [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)] : []);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
  focus() { this.focused = true; }
  select() { this.selected = true; }
  setSelectionRange() {}
}
function harness() {
  const nodes = new Map(), node = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const document = { hidden: false, getElementById: node, createElement: tag => new Element(tag), body: new Element('body'),
    querySelector: selector => node(selector), querySelectorAll: () => [] };
  const tearly = earlyState(), calls = [], copies = [], savedHashes = [], counters = { status: 0 };
  const location = { hash: '' };
  const saveHash = (unused, unusedTitle, hash) => { location.hash = hash; savedHashes.push(hash); };
  const tstate = { mode: 'early', meta: null, status: null, unit: 'unused-build-unit', star: 3, geo: 'clump', traits: 'bare', threat: 'mixed' };
  let responder = async () => fixture();
  const context = vm.createContext({ tearly, tstate, state: { view: 'tft', tier: 'fixture' }, document, AbortController, setTimeout, clearTimeout,
    tcomp: { meta: null }, tboard: {}, tflex: {}, location, restoringHash: false, restoredHash: null,
    history: { replaceState: saveHash, pushState: saveHash },
    navigator: { clipboard: { writeText: async code => copies.push(code) } },
    api: async (url, options) => { calls.push({ url, options }); return responder(url, options); },
    hideTftTooltip() {}, renderTftRefresh() {}, scheduleTftStatus: () => ++counters.status,
    bindTftTooltip: (anchor, content, tap) => { anchor.tooltip = content; if (tap) anchor.addEventListener('click', () => { anchor.openedTooltip = content(); }); },
    tftTooltipHead: (icon, name, subtitle) => { const el = new Element(); el.textContent = `${name} ${subtitle}`; return el; },
  });
  vm.runInContext(section('function tftText(', 'function tftUnitTraits(') +
    section('const tftPublicationChanged =', 'function scheduleTftStatus()') +
    section('function tftCompositionTeamCode(', 'const tftCompositionValidated') +
    section('// ---------------- early comps: data ----------------', '// ---------------- early comps: end ----------------') +
    section('function updateHash(', 'function showView('), context);
  return { context, tearly, tstate, node, calls, copies, savedHashes, counters, setResponder: value => { responder = value; } };
}

function checkValidation() {
  const h = harness(), raw = fixture(), saved = JSON.stringify(raw);
  assert.equal(h.context.tftEarlyCatalog(raw), raw);
  for (const [name, mutate] of [
    ['wrong schema', data => data.schema = 'other'],
    ['unfinished payload', data => data.complete = false],
    ['old upgrade assumption', data => data.starAssumption = '1-costs at 2★; 2- and 3-costs at 1★'],
    ['upgraded champion', data => data.variants[0].boards[0].units[0].star = 2],
    ['upgraded reference opponent', data => data.referencePools.level4.boards[0].units[0].star = 2],
    ['upgraded fight opponent', data => data.variants[0].boards[0].matchups[0].roster[0].star = 2],
    ['3-cost on four-unit board', data => Object.assign(data.variants[0].boards[0].units[0], { cost: 3, star: 1 })],
    ['two 3-costs on five-unit board', data => Object.assign(data.variants[1].boards[0].units[0], { cost: 3, star: 1 })],
    ['item added', data => data.variants[0].boards[0].units[0].items = ['Item']],
    ['incorrect gold', data => data.variants[0].boards[0].purchaseGold += 1],
    ['wrong unit count', data => data.variants[0].boards[0].unitCount = 5],
    ['duplicate champion', data => data.variants[0].boards[0].units[1] = plain(data.variants[0].boards[0].units[0])],
    ['false win rate', data => data.variants[0].boards[0].metrics.benchmarkWinRate = 1],
    ['false win count', data => data.variants[0].boards[0].metrics.benchmarkWins = 2],
    ['unrecorded benchmark fight', data => data.variants[0].boards[0].matchups.pop()],
    ['unknown opponent', data => data.variants[0].boards[0].matchups[0].opponentId = 'unknown'],
    ['duplicate trial', data => data.variants[0].boards[0].matchups[1].initiative = 0],
    ['changed opponent roster', data => data.variants[0].boards[0].matchups[0].roster[0].api = 'unknown'],
  ]) {
    const invalid = plain(raw); mutate(invalid); assert.throws(() => h.context.tftEarlyCatalog(invalid), undefined, name);
  }
  const drawn = plain(raw), row = drawn.variants[0].boards[0];
  row.matchups[0].outcome = 'draw'; row.matchups[1].outcome = 'timeout';
  Object.assign(row.metrics, { benchmarkLosses: 0, benchmarkDraws: 1, benchmarkTimeouts: 1 });
  assert.equal(h.context.tftEarlyCatalog(drawn), drawn, 'Draws and timeouts count as no win and remain valid evidence');
  assert.equal(JSON.stringify(raw), saved, 'Validation preserves the published artifact');
}

async function checkBrowserControls() {
  const h = harness(), ctx = h.context;
  ctx.restoreTftEarlyHash({ elevel: '4', eunit: 'special-4-0' });
  ctx.renderTftEarly();
  assert.equal(h.tearly.unit, 'special-4-0', 'Loading controls preserve a champion restored from the URL');
  assert.equal(await ctx.loadTftEarly(), true);
  assert.equal(h.calls.length, 1); assert.equal(h.calls[0].url, 'api/tft/early.json', 'The early mode needs no late-game composition or champion cells');
  assert.equal(h.node('#tft-early-table tbody').querySelectorAll('.tft-early-summary').length, 1, 'The saved champion filter applies after loading');
  h.node('tft-early-clear').fire('click');
  assert.equal(h.node('#tft-early-table tbody').querySelectorAll('.tft-early-summary').length, 25, 'Boards paginate at 25 rows');
  assert.match(h.node('tft-early-page').textContent, /1–25 of 30/);
  await h.node('tft-early-next').fire('click');
  assert.equal(h.node('#tft-early-table tbody').querySelectorAll('.tft-early-summary').length, 5);
  assert.match(h.node('tft-early-page').textContent, /26–30 of 30/);
  ctx.setTftEarlySort('dps', 'desc');
  assert.equal(ctx.tftEarlyRows()[0].id, 'four-29');
  ctx.setTftEarlySort('frontline', 'asc'); assert.equal(ctx.tftEarlyRows()[0].id, 'four-00');
  ctx.setTftEarlySort('gold', 'asc'); assert.equal(ctx.tftEarlyRows()[0].id, 'four-01');
  assert.equal(h.tearly.page, 1, 'Sorting returns to the first page');
  ctx.setTftEarlySort('wins', 'desc');
  const before = JSON.stringify(h.tearly.data), tbody = h.node('#tft-early-table tbody');
  const first = tbody.querySelector('.tft-early-summary'), open = first.querySelector('.tft-flex-open');
  await open.fire('click');
  const boardId = first.dataset.board, card = h.tearly.cards.get(boardId);
  assert.equal(open.attributes['aria-expanded'], 'true');
  assert(card.querySelectorAll('.tft-comp-unit').every(unit => ['1', '2'].includes(String(unit.dataset.cost))));
  assert.match(card.textContent, /No items/);
  assert.match(h.node('tft-early-context').textContent, /All units at 1★/);
  ctx.renderTftEarlyMethod();
  assert.match(h.node('tft-early-method-body').textContent, /Every champion is 1★/);
  assert.match(h.node('tft-early-method-body').textContent, /one copy of each champion/);
  const copy = card.querySelector('.tft-comp-copy').querySelector('button'); await copy.fire('click');
  assert.equal(h.copies.length, 1); assert.match(h.copies[0], /^02[0-9a-f]{30}TFTSet18$/);
  const evidence = card.querySelector('.tft-early-evidence'); evidence.open = true; await evidence.fire('toggle');
  assert.equal(evidence.querySelectorAll('.tft-comp-matchup-trial').length, 2);
  assert.match(evidence.textContent, /fixed early boards/); assert.match(evidence.textContent, /unit contributions/i);
  const firstUnit = card.querySelector('.tft-comp-unit-head'); await firstUnit.fire('click');
  assert(firstUnit.openedTooltip.length); assert.equal(h.calls.length, 1, 'Unit details remain within the item-free early comparison');
  ctx.setTftEarlySort('traits', 'desc');
  assert.equal(h.tearly.cards.get(boardId), card, 'Sorting retains expanded cards and their fight evidence');
  assert(h.tearly.openBoards.has(boardId));
  h.node('tft-early-level').value = '5'; await h.node('tft-early-level').fire('change');
  assert.equal(h.node('tft-early-three').disabled, false); assert.equal(ctx.tftEarlyRows().length, 2);
  h.node('tft-early-unit').value = 'unit-5-4-three'; await h.node('tft-early-unit').fire('change');
  assert.deepEqual(Array.from(ctx.tftEarlyRows(), row => row.id), ['five-with-three']);
  h.node('tft-early-three').checked = false; await h.node('tft-early-three').fire('change');
  assert.equal(h.tearly.unit, 'all', 'Excluding 3-costs clears an incompatible champion filter');
  assert.deepEqual(Array.from(ctx.tftEarlyRows(), row => row.id), ['five-without-three']);
  assert.match(h.savedHashes.at(-1), /tftview=early.*elevel=5.*ethree=0/); assert(!h.savedHashes.at(-1).includes('unit=unused-build-unit'));
  ctx.restoreTftEarlyHash({ elevel: '5', ethree: '0', eunit: 'unit-5-0', esort: 'gold', eorder: 'desc' }); ctx.updateHash();
  assert.match(h.savedHashes.at(-1), /eunit=unit-5-0.*esort=gold.*eorder=desc/);
  ctx.restoreTftEarlyHash({ elevel: '-1', esort: 'bad', eorder: 'bad' });
  assert.equal(h.tearly.level, 4); assert.equal(h.tearly.sort, 'wins'); assert.equal(h.tearly.order, 'desc');
  assert.equal(JSON.stringify(h.tearly.data), before, 'Filtering, sorting, tooltips and planner copies preserve saved results');
  assert.equal(await ctx.loadTftEarly(), true); assert.equal(h.calls.length, 1, 'Reopening the same saved section reuses its immutable payload');
}

async function checkErrorsAndCancellation() {
  const h = harness(), ctx = h.context;
  h.setResponder(async () => { throw new Error('HTTP 404'); });
  assert.equal(await ctx.loadTftEarly(), false);
  assert.equal(h.node('tft-early-retry').hidden, false); assert.match(h.node('tft-early-status').textContent, /not been published/);
  h.setResponder(async () => fixture()); await h.node('tft-early-retry').fire('click');
  assert(h.tearly.data && !h.tearly.pending && !h.tearly.error); assert.equal(h.node('tft-early-retry').hidden, true);
  const saved = h.tearly.data;
  h.setResponder(async () => { throw new Error('network unavailable'); });
  assert.equal(await ctx.loadTftEarly({ force: true }), false);
  assert.equal(h.tearly.data, saved); assert.match(h.node('tft-early-status').textContent, /last loaded results/);
  assert.equal(h.node('tft-early-tablewrap').hidden, false, 'A failed refresh retains the last published boards');
  let release;
  h.setResponder(() => new Promise(resolve => { release = resolve; }));
  const pending = ctx.loadTftEarly({ force: true });
  ctx.cancelTftEarly(); assert.equal(h.calls.at(-1).options.signal.aborted, true);
  const other = fixture(); other.revision = 'stale-in-flight'; release(other); await pending;
  assert.equal(h.tearly.data, saved, 'A response from a canceled early request cannot replace the published data');
  assert.equal(h.tearly.loading, false);
  h.setResponder(async () => ({ pending: true })); assert.equal(await ctx.loadTftEarly({ force: true }), false); assert(h.tearly.pending);
  h.setResponder(async () => fixture()); await ctx.loadTftEarly({ force: true });
  h.tstate.status = { patch: '18.3', publicationRevision: 'site-v2' };
  let publications = 0;
  h.setResponder(async () => { ++publications; const raw = fixture(); raw.publicationRevision = 'site-v2'; return raw; });
  await ctx.loadTftEarly(); assert.equal(publications, 1, 'A changed publication reloads the immutable endpoint');
}

async function checkNavigationAndStatus() {
  const h = harness(), ctx = h.context;
  let combatLoads = 0;
  ctx.loadTftMeta = ctx.loadTftScenario = ctx.loadTftLeaderboard = ctx.loadTftCompositions = async () => { ++combatLoads; };
  ctx.tftActive = () => {};
  let status = { patch: '18.3', revision: 'baseline-v1', publicationRevision: 'site-v1' };
  ctx.loadTftStatus = async () => { h.tstate.status = status; return status; };
  vm.runInContext(section('function renderTftMode(', 'function renderTftLeaderboardControls(') +
    section('async function pollTftStatus(', '// Mark unavailable builds') +
    section('async function openTft(', '// ---------------- data view'), ctx);
  h.tstate.mode = 'builds'; await ctx.setTftMode('early');
  assert.equal(h.node('tft-early-panel').hidden, false);
  assert.equal(h.node('tft-champion-panel').hidden, true); assert.equal(h.node('tft-compositions-panel').hidden, true);
  await ctx.openTft(); await ctx.pollTftStatus();
  assert.equal(combatLoads, 0, 'Entering, opening and polling Early comps do not request late-game combat contexts');
  assert.equal(h.calls.length, 1);
  const saved = h.tearly.data;
  status = { ...status, revision: 'changed-baseline-calculations' }; await ctx.pollTftStatus();
  assert.equal(h.tearly.data, saved, 'An unrelated calculation revision preserves the published early boards');
  status = { ...status, publicationRevision: 'site-v2' };
  h.setResponder(async () => ({ ...fixture(), publicationRevision: 'site-v2' })); await ctx.pollTftStatus();
  assert.equal(h.tearly.data.publicationRevision, 'site-v2', 'Status changes reload a newly published early artifact');
  const request = h.tearly.req; await ctx.setTftMode('builds', { load: false });
  assert(h.tearly.req > request, 'Leaving the early mode cancels its request generation');
  assert.equal(h.node('tft-early-panel').hidden, true); assert.equal(h.node('tft-champion-panel').hidden, false);
}

async function main() {
  for (const marker of ['data-key="early"', 'id="tft-early-panel"', 'restoreTftEarlyHash(hash)', 'cancelTftEarly();', 'await loadTftEarly();'])
    assert(html.includes(marker), `Early mode integration is present: ${marker}`);
  checkValidation(); await checkBrowserControls(); await checkErrorsAndCancellation(); await checkNavigationAndStatus();
  const catalogFlag = process.argv.indexOf('--catalog');
  if (catalogFlag >= 0) {
    const raw = JSON.parse(fs.readFileSync(process.argv[catalogFlag + 1], 'utf8')), h = harness();
    h.context.tftEarlyCatalog(raw); h.setResponder(async () => raw); assert.equal(await h.context.loadTftEarly(), true);
    for (const level of [4, 5]) {
      h.tearly.level = level; h.context.renderTftEarly(); h.context.renderTftEarlyMethod();
      const boards = h.context.tftEarlyRows(); assert(boards.length, `Published level ${level} has boards`);
      h.context.tftEarlyCard(boards[0]); assert(boards.every(board => board.units.every(unit => unit.star === 1)));
      if (level === 5) { h.tearly.allowThree = false; assert(h.context.tftEarlyRows().every(board => board.threeCostCount === 0)); }
    }
    console.log(`Published Early comps validated: ${raw.variants.map(variant => `${variant.level} units: ${variant.boards.length} boards`).join(', ')}`);
  }
  console.log('Early comps UI checks passed: cost/star legality, benchmark evidence, filters, sorting, pagination, cards, planner copies, URL state, retries and cancellation.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
