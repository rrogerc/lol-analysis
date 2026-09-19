// node jobs/test-tft-damage-ui.cjs [--payload leaderboard.json] [--cell finite-cell.json]
// Exercises the finite leaderboard and per-loadout exposure with the real page functions.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script);
const plain = value => JSON.parse(JSON.stringify(value));
const section = (from, to) => script.slice(script.indexOf(from), script.indexOf(to));

function payload() {
  const row = (unit, rank, killTime, objective) => ({ unit, unitName: unit[0].toUpperCase() + unit.slice(1),
    unitApi: 'TFT18_' + unit, star: 2, cost: 4, scenario: 's2-clump-bare-mixed', rank, objective,
    items: ['One', 'Two', 'Three'], itemApis: ['one', 'two', 'three'],
    performance: { killTime, total: killTime === null ? 7000 : 9000, dps: killTime === null ? 350 : 9000 / killTime } });
  return { revision: 'finite-revision', selection: { key: 's2-clump-bare-mixed' }, complete: true,
    expectedCount: 4, readyCount: 4,
    damage: [Object.assign(row('alpha', 1, 8, 'fighter'), { form: 'AD', role: 'Attack Assassin', range: 1, pressure: true }),
      row('beta', 2, 10, 'carry'), row('gamma', 3, null, 'carry')],
    tanks: [{ unit: 'tank', unitName: 'Tank', rank: 1, star: 2, cost: 3, objective: 'tank',
      scenario: 's2-clump-bare-mixed', items: ['Armor', 'Health', 'Shield'], itemApis: ['armor', 'health', 'shield'],
      performance: { aliveTime: 60, survivalCapped: true, stressAliveTime: 37.1234, stressCapped: false } }] };
}

function pinnedCurvePayload() {
  const data = payload();
  data.damageEvaluationModel = 'unpressured-damage-curves-v1';
  data.damage = data.damage.map((row, index) => ({ ...row, rank: 3 - index,
    objective: 'carry', form: 'AP', role: 'Magic Marksman', range: 5, pressure: false,
    items: ['Potential one', 'Potential two', 'Potential three'], itemApis: ['p1', 'p2', 'p3'],
    performance: { time: 20, total: 20000 * (index + 1), dps: 1000 * (index + 1) },
    damageCurve: { evaluationModel: data.damageEvaluationModel },
    finiteDiagnostic: { items: row.items, itemApis: row.itemApis, performance: row.performance,
      objective: row.objective, form: row.form, role: row.role, range: row.range, pressure: row.pressure } })).reverse();
  return data;
}

function harness(data = payload()) {
  let focused = null;
  const create = tag => ({ tag, id: '', className: '', dataset: {}, attributes: {}, children: [], events: {}, ownText: '', hidden: false,
    style: { setProperty() {} },
    get classList() { return { contains: name => this.className.split(' ').includes(name) }; },
    append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } },
    replaceChildren(...children) { this.children = []; this.ownText = ''; this.append(...children); },
    setAttribute(key, value) { this.attributes[key] = String(value); },
    addEventListener(key, fn) { this.events[key] = fn; },
    focus() { focused = this; }, scrollIntoView() {},
    getBoundingClientRect() { return { left: 0, top: 0, width: 760, height: 310 }; },
    get textContent() { return this.ownText + this.children.map(child => child.textContent || '').join(''); },
    set textContent(value) { this.ownText = String(value); this.children = []; },
    matches(selector) {
      const last = selector.split(' ').at(-1);
      return last.startsWith('.') ? this.className.split(' ').includes(last.slice(1))
        : last.startsWith('#') ? this.id === last.slice(1) : this.tag === last;
    },
    querySelectorAll(selector) { return this.children.flatMap(child =>
      [...(child.matches?.(selector) ? [child] : []), ...(child.querySelectorAll?.(selector) || [])]); },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
  });
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, Object.assign(create('div'), { id }));
    return elements.get(id);
  };
  const queryAll = selector => {
    if (selector.startsWith('#tft-board-table ')) {
      if (selector.endsWith('tbody tr')) return element('tbody').children;
      return element('tbody').querySelectorAll(selector);
    }
    if (selector.startsWith('#')) {
      const [id, ...rest] = selector.split(' ');
      return rest.length ? element(id.slice(1)).querySelectorAll(rest.join(' ')) : [element(id.slice(1))];
    }
    return [...elements.values()].flatMap(node => node.querySelectorAll(selector));
  };
  const state = { mode: 'damage', star: data.selection.key.split('-')[0], geo: data.selection.key.split('-')[1],
    traits: 'bare', threat: 'mixed', role: 'all', cost: 'all', search: '',
    data, key: data.selection.key, req: 0, loading: false, error: null };
  const champion = { mode: 'leaderboard', meta: { revision: data.revision, stars: [1, 2, 3],
    units: [...data.damage, ...data.tanks].map(row => ({ slug: row.unit, name: row.unitName, cost: row.cost,
      stars: [1, 2], traits: ['Shared trait'], icon: null })),
    geometries: { spread: 'Spread', clump: 'Clumped' }, traitContexts: { bare: 'No traits' },
    tankThreats: [{ key: 'mixed', label: 'Mixed' }] } };
  const h = { state, champion, create, element, queryAll, navigations: [], requests: [], hash: '',
    get focused() { return focused; } };
  const context = vm.createContext({ tboard: state, tstate: champion, tcomp: { fromComposition: false, req: 0 },
    document: { createElement: create, createElementNS: (ns, tag) => create(tag),
      getElementById: element, querySelectorAll: queryAll,
      querySelector: selector => selector === '#tft-board-table thead tr' ? element('thead')
        : selector === '#tft-board-table tbody' ? element('tbody') : queryAll(selector)[0],
      get activeElement() { return focused; } },
    tftText: (tag, text, className = '') => Object.assign(create(tag), { textContent: text, className }),
    tftIcon: () => create('span'), tftActive() {}, hideTftTooltip() {}, renderTftStars() {}, scheduleTftStatus() {},
    tftHeldTime: (time, capped) => !Number.isFinite(time) ? '—' : capped ? '60s+' : time.toFixed(2) + 's',
    tftSeg(id, options, callback) {
      element(id).replaceChildren(...options.map(option => {
        const button = Object.assign(create('button'), { textContent: option.label, dataset: { key: option.key } });
        button.addEventListener('click', () => callback(option.key)); return button;
      }));
    },
    tftRevision: () => champion.meta.revision, updateHash: () => { h.hash = state.mode; },
    loadTftScenario: () => h.navigations.push({ unit: champion.unit, star: champion.star, geo: champion.geo, traits: champion.traits }),
    loadTftMeta: async () => true, AbortController,
    api: async (url, options) => { h.requests.push(url); return h.response ? h.response() : structuredClone(data); },
    loadTftCompositions: async () => {}, openTft: async () => {},
  });
  vm.runInContext(section('const tftLeaderboardKey', 'const tftCompositionKey')
    + section('function tftCoreItems(', 'function tftComponentUse(')
    + section('function openTftLeaderboardEntry(', 'document.querySelectorAll("#tft-mode-seg button")'), context);
  context.renderTftLeaderboardControls();
  h.context = context;
  return h;
}

function checkFiniteLeaderboard() {
  const h = harness(), c = h.context;
  assert(c.tftFiniteLeaderboardValid(h.state.data));
  assert.deepEqual(h.element('tbody').children.map(row => [row.dataset.unit, row.dataset.rank]), [['alpha', 1], ['beta', 2], ['gamma', 3]]);
  assert(h.element('thead').textContent.includes('Clear time') && h.element('thead').textContent.includes('DPS'));
  assert(h.element('tbody').textContent.includes('Did not clear'));
  assert(h.element('tbody').textContent.includes('AD form · Attack Assassin · 1 range'));
  assert.equal(h.element('tft-board-movement-note').hidden, true, 'Old payloads have no new movement assumption');
  assert(!h.element('tbody').textContent.includes('s moving'));
  assert.equal(h.queryAll('#tft-board-geo-seg button')[0].textContent, 'Spread');
  assert.equal(h.queryAll('#tft-board-geo-seg button')[1].textContent, 'Clumped');
  assert(!html.includes('id="tft-damage-time"') && !html.includes('id="tft-damage-comparison"'), 'The slider and curve comparison are removed');
  h.state.role = 'fighter'; c.renderTftLeaderboard();
  assert.deepEqual(h.element('tbody').children.map(row => row.dataset.unit), ['alpha']);
  h.state.role = 'all'; h.state.search = 'beta'; c.renderTftLeaderboard();
  assert.deepEqual(h.element('tbody').children.map(row => row.dataset.rank), [2], 'Filters retain the full leaderboard rank');
  h.state.search = ''; h.state.mode = 'tanks'; c.renderTftLeaderboard();
  assert(h.element('tbody').textContent.includes('60s+') && h.element('tbody').textContent.includes('37.12s'));
  assert(h.element('thead').textContent.includes('2× pressure'));
  assert.equal(h.element('tft-board-role-seg').hidden, true);
}

function checkPinnedCompatibility() {
  const data = pinnedCurvePayload(), before = JSON.stringify(data), h = harness(data), c = h.context;
  assert(c.tftFiniteLeaderboardValid(data));
  const rows = c.tftLeaderboardDamageRows(data);
  assert.deepEqual(plain(rows.map(row => [row.unit, row.rank])), [['alpha', 1], ['beta', 2], ['gamma', 3]]);
  assert.equal(rows[0].form, 'AD'); assert.equal(rows[0].objective, 'fighter');
  assert(h.element('tbody').textContent.includes('8.00s'));
  assert(!h.element('tbody').textContent.includes('Potential'));
  h.state.role = 'fighter'; c.renderTftLeaderboard();
  assert.deepEqual(h.element('tbody').children.map(row => row.dataset.unit), ['alpha'], 'Pinned data uses the finite winner’s actual form and role');
  assert.equal(JSON.stringify(data), before, 'The compatibility projection preserves the pinned payload');
  const tied = pinnedCurvePayload();
  tied.damage.find(row => row.unit === 'beta').finiteDiagnostic.performance.killTime = 8;
  assert.deepEqual(plain(c.tftLeaderboardDamageRows(tied).map(row => row.rank)), [1, 1, 3]);
  for (const mutate of [
    data => { delete data.damage[0].finiteDiagnostic; },
    data => { data.damage[0].finiteDiagnostic.performance.killTime = undefined; },
    data => { data.damage[0].finiteDiagnostic.performance.total = -1; },
    data => { data.damage[0].finiteDiagnostic.performance.dps = NaN; },
  ]) {
    const invalid = pinnedCurvePayload(); mutate(invalid);
    assert.equal(c.tftFiniteLeaderboardValid(invalid), false, 'Potential metrics cannot masquerade as finite results');
  }
}

async function checkNavigationAndLoad() {
  const h = harness(), c = h.context;
  h.element('tbody').children[0].querySelector('.tft-board-champion').events.click();
  assert.deepEqual(h.navigations.at(-1), { unit: 'alpha', star: 2, geo: 'clump', traits: 'bare' });
  assert.equal(h.champion.mode, 'builds');
  await c.setTftMode('leaderboard', { load: false, focus: true });
  assert.equal(h.focused.dataset.unit, 'alpha');
  c.state = { view: 'tft', tier: 'test' };
  c.history = { replaceState: (state, title, hash) => { h.hash = hash; } };
  vm.runInContext(section('function updateHash(', 'function showView('), c);
  c.updateHash();
  const hash = Object.fromEntries(new URLSearchParams(h.hash.slice(1)));
  assert.equal(hash.board, 'damage'); assert.equal(hash.bgeo, 'clump');
  assert(!('btime' in hash) && !('bcurves' in hash) && !('bdamage' in hash));
  h.response = () => { const wrong = pinnedCurvePayload(); delete wrong.damage[0].finiteDiagnostic; return wrong; };
  await c.loadTftLeaderboard();
  assert(h.state.error && h.state.data === null);
  h.response = pinnedCurvePayload;
  await c.loadTftLeaderboard();
  assert.equal(h.state.error, null);
  assert.equal(h.element('tbody').children[0].dataset.unit, 'alpha');
}

function checkFiniteEquippedPressure() {
  const h = harness(), c = h.context;
  const unit = h.champion.meta.units.find(unit => unit.slug === 'alpha');
  Object.assign(unit, { name: 'Nidalee', role: 'Magic Marksman', objective: 'carry', ability: 'Pounce', duration: 20 });
  const common = { items: ['One', 'Two', 'Three'], rank: 1, ad: 200, ap: 100, attackSpeed: 1, crit: 25,
    killTime: 12, total: 3000, dps: 250, casts: 3, attacks: 12, breakdown: {}, left: [0, 0, 0] };
  const ad = { ...common, form: 'AD', objective: 'fighter', pressure: true, role: 'Attack Assassin', range: 1,
    hp: 2000, hpLeft: 400, armor: 50, mr: 50, aliveTime: 12, died: false, taken: 1600, absorbed: 2400,
    shielded: 0, healed: 0, denied: 0 };
  const ap = { ...common, form: 'AP', objective: 'carry', pressure: false, role: 'Magic Marksman', range: 4 };
  h.champion.data = { unit: 'alpha', objective: 'carry', pressureByBuild: true, rows: [ad, ap],
    scenario: { geometry: 'clump', duration: 20, dummy: { count: 3, star: 2, pressureDps: 100,
      slots: Array.from({ length: 3 }, () => ({ hp: 1000, armor: 100, mr: 100, ad: 50, as: 1, ability: 100,
        manaStart: 0, manaMax: 100, manaPerAttack: 10 })) } } };
  c.fmtInt = value => Number.isFinite(value) ? String(Math.round(value)) : '—';
  c.cell = text => Object.assign(h.create('td'), { textContent: text });
  c.tftTargetDebuffNote = () => 'Finite target defenses.';
  c.document.querySelector = selector => selector === '.tft-fight-preview' ? h.element('fight-preview') : null;
  vm.runInContext(section('function renderTftFight(', 'function tftActive(')
    + section('function tftColumns(', 'function renderTftTable(')
    + section('function renderTftBreakdown(', 'async function openTft('), c);
  const columns = c.tftColumns();
  assert(columns.some(column => column.th === 'Alive') && columns.some(column => column.th === 'HP'));
  assert.equal(columns.find(column => column.th === 'HP').td(ad).textContent, '2000');
  assert.equal(columns.find(column => column.th === 'Alive').td(ap).textContent, 'Protected');
  assert.equal(columns.find(column => column.th === 'HP').td(ap).textContent, '—');
  c.renderTftBreakdown(ad);
  assert(h.element('tft-pressure-note').textContent.includes('All three dummies attack and cast'));
  assert.equal(h.element('tft-pressure-details').hidden, false);
  assert(h.element('tft-selected-meta').textContent.includes('Attack Assassin · AD form · 1 range'));
  assert(h.element('tbd-hint').textContent.includes('takes incoming damage'));
  c.renderTftBreakdown(ap);
  assert(h.element('tft-pressure-note').textContent.includes('do not attack this protected build'));
  assert.equal(h.element('tft-pressure-details').hidden, true);
  assert(h.element('tft-selected-meta').textContent.includes('Magic Marksman · AP form · 4 range'));
  assert(!/undefined|NaN/.test(h.element('tbd-hint').textContent));
  assert.equal(unit.objective, 'carry', 'Per-build rendering preserves base identity used by navigation guards');
  return h;
}

function checkMovementLeaderboard() {
  const data = payload();
  Object.assign(data.damage[0], { meleeRepositionSeconds: 0.5 });
  Object.assign(data.damage[0].performance, { movementTime: 1.5, repositions: 3 });
  Object.assign(data.damage[1], { form: 'AP', range: 5, meleeRepositionSeconds: 0.5 });
  // Even a counter inherited accidentally from another form must not label AP as delayed.
  Object.assign(data.damage[1].performance, { movementTime: 2, repositions: 4 });
  Object.assign(data.damage[2], { range: 2, meleeRepositionSeconds: 0.5 });
  Object.assign(data.damage[2].performance, { movementTime: 0.5, repositions: 1 });
  const before = JSON.stringify(data), h = harness(data), c = h.context;
  assert.equal(h.element('tft-board-movement-note').hidden, false);
  const note = h.element('tft-board-movement-note').textContent;
  assert(note.includes('1–2 attack range') && note.includes('0.5s delay at initial engagement'));
  assert(note.includes('current target dies') && note.includes('cooldowns overlap movement'));
  assert(note.includes('ongoing damage and incoming pressure continue') && note.includes('Hex paths are not simulated'));
  const rows = h.element('tbody').children;
  assert(rows[0].textContent.includes('1.50s moving · 3 moves'));
  assert(!rows[1].textContent.includes('s moving'));
  assert(rows[2].textContent.includes('0.50s moving · 1 move'));
  h.state.search = 'beta'; c.renderTftLeaderboard();
  assert.equal(h.element('tft-board-movement-note').hidden, true, 'Ranged-only filtered results are not labeled penalized');
  h.state.search = ''; h.state.mode = 'tanks'; c.renderTftLeaderboard();
  assert.equal(h.element('tft-board-movement-note').hidden, true);
  assert(!h.element('tbody').textContent.includes('s moving'));
  assert.equal(JSON.stringify(data), before);
}

function checkMovementDetails() {
  const h = checkFiniteEquippedPressure(), c = h.context;
  const [ad, ap] = h.champion.data.rows;
  const legacyCaption = h.element('tft-formation-caption').textContent;
  h.champion.data.scenario.dummy.meleeRepositionSeconds = 0.5;
  Object.assign(ad, { movementTime: 1.25, repositions: 3 });
  ap.range = 5;
  c.renderTftBreakdown(ad);
  assert(h.element('tft-formation-caption').textContent.includes('approximate 0.5s movement delay'));
  assert(h.element('tft-formation-caption').textContent.includes('Exact hex paths are not simulated'));
  const details = h.element('tft-targeting-note').textContent;
  assert(details.includes('initial engagement and after the current target dies'));
  assert(details.includes('cooldowns overlap movement') && details.includes('ongoing damage and incoming pressure continue'));
  assert(h.element('tbd-hint').textContent.includes('1.25s moving · 3 moves'));
  c.renderTftBreakdown(ap);
  assert(h.element('tft-formation-caption').textContent.includes('This 5-range build has no movement delay'));
  assert(!h.element('tft-targeting-note').textContent.includes('Movement takes'));
  assert(!h.element('tbd-hint').textContent.includes('s moving'));
  Object.assign(ap, { movementTime: 3, repositions: 6 });
  c.renderTftBreakdown(ap);
  assert(!h.element('tbd-hint').textContent.includes('s moving'), 'Equipped range governs the affected form');
  Object.assign(ad, { range: 2, movementTime: 0, repositions: 0 });
  c.renderTftBreakdown(ad);
  assert(h.element('tbd-hint').textContent.includes('0.00s moving · 0 moves'));
  delete ad.repositions;
  c.renderTftBreakdown(ad);
  assert(h.element('tbd-hint').textContent.includes('0.00s moving'));
  assert(!/undefined|NaN/.test(h.element('tbd-hint').textContent));
  delete h.champion.data.scenario.dummy.meleeRepositionSeconds;
  c.renderTftBreakdown(ad);
  assert.equal(h.element('tft-formation-caption').textContent, legacyCaption);
  assert(!h.element('tbd-hint').textContent.includes('s moving'), 'Old benchmark metadata keeps old wording');
  h.champion.data.objective = 'tank';
  h.champion.data.scenario.dummy.meleeRepositionSeconds = 0.5;
  ad.objective = 'tank'; c.tftTankDebuffNote = () => 'Tank defenses.';
  c.renderTftBreakdown(ad);
  assert(!h.element('tbd-hint').textContent.includes('s moving'));
  assert(!h.element('tft-formation-caption').textContent.includes('movement delay'));
}

function checkRealCell(filename) {
  const data = JSON.parse(fs.readFileSync(filename, 'utf8'));
  const h = checkFiniteEquippedPressure(), c = h.context;
  h.champion.data = data;
  Object.assign(h.champion.meta.units[0], { slug: data.unit, name: data.unitName, cost: data.cost, objective: data.objective });
  h.champion.geo = data.scenario.geometry;
  c.TSRC_COLOR = {}; c.SLOTS = ['color']; c.css = value => value;
  c.bar = () => { const node = h.create('div'); node.append(Object.assign(h.create('span'), { className: 'bar-fill' })); return node; };
  h.element('tft-show-stats').checked = true;
  const columns = c.tftColumns(), states = new Set();
  for (const row of data.rows) {
    c.renderTftBreakdown(row);
    assert.equal(h.element('tft-pressure-details').hidden, !row.pressure);
    assert(!/undefined|NaN/.test(h.element('tbd-hint').textContent + h.element('tft-selected-meta').textContent));
    const movement = data.objective !== 'tank' && data.scenario.dummy?.meleeRepositionSeconds > 0
      && Number.isFinite(row.range) && row.range <= 2 && Number.isFinite(row.movementTime);
    assert.equal(h.element('tbd-hint').textContent.includes('s moving'), movement);
    assert(columns.every(column => !/undefined|NaN/.test(column.td(row).textContent)));
    if (!row.pressure) assert.equal(columns.find(column => column.th === 'Alive').td(row).textContent, 'Protected');
    else assert(Number.isFinite(row.hp) && columns.find(column => column.th === 'HP').td(row).textContent !== '—');
    states.add(`${row.form}:${row.pressure}`);
  }
  assert(states.has('AD:true') && states.has('AP:false'));
  assert.equal(data.objective, 'carry');
  console.log(`Real finite-form UI checks passed: ${filename}; ${data.rows.length} rows retain actual pressure, roles and health, including AD and AP.`);
}

function checkRealPayload(filename) {
  const data = JSON.parse(fs.readFileSync(filename, 'utf8'));
  const h = harness(data), c = h.context;
  assert(c.tftFiniteLeaderboardValid(data));
  const rows = c.tftLeaderboardDamageRows(data);
  if (!data.damage.some(row => row.finiteDiagnostic)) {
    assert.deepEqual(plain(rows.map(row => [row.unit, row.rank])), data.damage.map(row => [row.unit, row.rank]));
  }
  for (const row of rows) {
    const original = data.damage.find(entry => entry.unit === row.unit), finite = original.finiteDiagnostic || original;
    assert.deepEqual(plain(row.itemApis), finite.itemApis);
    assert.deepEqual(plain(row.performance), finite.performance);
    assert.equal(row.objective, finite.objective);
    const rendered = h.element('tbody').children.find(node => node.dataset.unit === row.unit);
    const movement = row.meleeRepositionSeconds > 0 && Number.isFinite(row.range)
      && row.range <= 2 && Number.isFinite(row.performance.movementTime);
    assert.equal(rendered.textContent.includes('s moving'), movement);
  }
  assert(!/undefined|NaN/.test(h.element('tbody').textContent));
  h.state.mode = 'tanks'; c.renderTftLeaderboard();
  assert(!/undefined|NaN/.test(h.element('tbody').textContent));
  console.log(`Real finite leaderboard UI checks passed: ${filename}; ${data.damage.length} damage champions, ${data.tanks.length} tanks.`);
}

(async () => {
  checkFiniteLeaderboard(); checkPinnedCompatibility();
  await checkNavigationAndLoad(); checkFiniteEquippedPressure();
  checkMovementLeaderboard(); checkMovementDetails();
  for (let i = 2; i < process.argv.length; i += 2) {
    assert(process.argv[i + 1]);
    if (process.argv[i] === '--payload') checkRealPayload(process.argv[i + 1]);
    else if (process.argv[i] === '--cell') checkRealCell(process.argv[i + 1]);
    else throw new Error(`Unknown argument ${process.argv[i]}`);
  }
  console.log('Finite damage UI checks passed: clear-time ranks, tank preservation, pinned diagnostic compatibility, filters, navigation, Nidalee per-build roles/pressure/health, and optional movement assumptions/time with ranged and old-payload compatibility.');
})().catch(error => { console.error(error); process.exitCode = 1; });
