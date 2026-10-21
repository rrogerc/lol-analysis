// node jobs/test-tft-damage-ui.cjs [--payload leaderboard.json] [--cell finite-cell.json]
// Exercises the finite leaderboard, formation preview and per-loadout exposure with the real page functions.
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
    unitApi: 'TFT18_' + unit, star: 2, cost: 4, scenario: 's2-clump-low', rank, objective,
    items: ['One', 'Two', 'Three'], itemApis: ['one', 'two', 'three'],
    performance: { killTime, total: killTime === null ? 7000 : 9000, dps: killTime === null ? 350 : 9000 / killTime } });
  return { revision: 'finite-revision', selection: { key: 's2-clump-low-mixed' }, complete: true,
    expectedCount: 4, readyCount: 4,
    damage: [Object.assign(row('alpha', 1, 8, 'fighter'), { form: 'AD', role: 'Attack Assassin', range: 1, pressure: true }),
      row('beta', 2, 10, 'carry'), row('gamma', 3, null, 'carry')],
    tanks: [{ unit: 'tank', unitName: 'Tank', rank: 1, star: 2, cost: 3, objective: 'tank',
      scenario: 's2-clump-low', items: ['Armor', 'Health', 'Shield'], itemApis: ['armor', 'health', 'shield'],
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
    traits: data.selection.key.split('-')[2], threat: 'mixed', role: 'all', cost: 'all', search: '',
    data, key: data.selection.key, req: 0, loading: false, error: null };
  const champion = { mode: 'leaderboard', meta: { revision: data.revision, stars: [1, 2, 3],
    units: [...data.damage, ...data.tanks].map(row => ({ slug: row.unit, name: row.unitName, cost: row.cost,
      stars: [1, 2], traits: ['Shared trait'], icon: null })),
    geometries: { spread: 'Spread', clump: 'Clumped' },
    dummy: { count: 6, tanks: 4, others: 2,
      slots: Array.from({ length: 6 }, (_, i) => ({ kind: i < 4 ? 'tank' : 'non-tank',
        line: i < 4 ? 'frontline' : 'backline', nearby: i < 4 })) },
    traitContexts: { bare: 'No traits', low: 'First trait breakpoints', high: 'Highest trait breakpoints' },
    tankThreats: [{ key: 'mixed', label: 'Mixed' }] } };
  const h = { state, champion, create, element, queryAll, navigations: [], requests: [], tooltips: new Map(), hash: '',
    get focused() { return focused; } };
  const context = vm.createContext({ tboard: state, tstate: champion, tcomp: { fromComposition: false, req: 0 },
    document: { createElement: create, createElementNS: (ns, tag) => create(tag),
      getElementById: element, querySelectorAll: queryAll,
      querySelector: selector => selector === '#tft-board-table thead tr' ? element('thead')
        : selector === '#tft-board-table tbody' ? element('tbody') : queryAll(selector)[0],
      get activeElement() { return focused; } },
    tftText: (tag, text, className = '') => Object.assign(create(tag), { textContent: text, className }),
    tftIcon: (url, name, trait = false) => Object.assign(create('span'), {
      className: trait ? 'tft-icon tft-trait-icon' : 'tft-icon', dataset: { icon: url || '', name } }),
    tftActive() {}, hideTftTooltip() {}, renderTftUnits() {}, renderTftStars() {}, scheduleTftStatus() {},
    bindTftTooltip(anchor, contents, tap = false) { h.tooltips.set(anchor, { contents, tap }); },
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
  vm.runInContext(section('function tftUnitTraits(', 'const tftTip')
    + section('function tftTooltipHead(', 'function renderTftSelected(')
    + section('function tftEnemyFormation(', 'function renderTftFight(')
    + section('const tftLeaderboardKey', 'const tftCompositionKey')
    + section('function tftCoreItems(', 'function tftCoreCompletionOrder(')
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
  assert(!html.includes('id="tft-board-geo-seg"'), 'The leaderboard uses only clumped enemies');
  assert(html.includes('id="tft-board-formation"'), 'The fixed enemy formation remains visible');
  assert.equal(h.element('tft-board-formation').querySelectorAll('button').length, 0,
    'The fixed formation is a read-only description');
  assert.equal(h.element('tft-board-formation').textContent,
    'Clumped · 4 frontliners + 2 backliners (6 enemies)');
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

function checkBisGaps() {
  const data = payload();
  const runnerUp = performance => ({ items: ['Runner one', 'Runner two', 'Runner three'],
    itemApis: ['runner-one', 'runner-two', 'runner-three'], performance });
  data.damage[0].secondBest = runnerUp({ metric: 'killTime', status: 'comparable', lossPct: 10.1543125,
    value: 8.812345, killTime: 8.812345, total: 9000 });
  data.damage[1].secondBest = runnerUp({ metric: 'killTime', status: 'comparable', lossPct: 0,
    value: 10, killTime: 10, total: 9000 });
  data.damage[2].secondBest = runnerUp({ metric: 'damage', status: 'comparable', lossPct: 5,
    value: 6650, killTime: null, total: 6650 });
  const before = JSON.stringify(data), h = harness(data), c = h.context;
  const cells = () => h.element('tbody').children.map(row => row.children[3]);
  const value = cell => cell.querySelector('strong').textContent;
  const detail = cell => cell.querySelector('small').textContent;
  assert.equal(h.element('thead').children[3].textContent, '2nd BIS gap', 'The new gap sits beside optimal items');
  assert(h.element('thead').children[3].title.includes('second-best distinct three-item build'));
  let [clear, tied, damage] = cells();
  assert.deepEqual([value(clear), value(tied), value(damage)], ['10.2%', '0.0%', '5.0%']);
  assert.deepEqual([detail(clear), detail(damage)], ['slower clear', 'less damage']);
  assert.equal(clear.dataset.label, '2nd BIS gap', 'The mobile table retains the gap label');
  assert(clear.title.includes('Runner one · Runner two · Runner three'));
  assert(clear.title.includes('extra clear time') && clear.title.includes('BIS: cleared in 8s')
    && clear.title.includes('Second best: cleared in 8.812345s'), 'Hover exposes the clear-time metric and unrounded results');
  assert(damage.title.includes('Neither build clears') && damage.title.includes('7000 damage') && damage.title.includes('6650 damage'));
  assert(!damage.title.includes('6650s'), 'Damage uses damage units rather than time');
  assert(clear.attributes['aria-label'].includes(clear.title), 'The same explanation is available to assistive technology');
  assert.deepEqual(h.element('tbody').children.map(row => row.dataset.rank), [1, 2, 3], 'Build gaps preserve champion ranks');
  assert.equal(JSON.stringify(data), before, 'Gap rendering preserves the saved result');

  data.damage[0].secondBest.performance.lossPct = .00001;
  c.renderTftLeaderboard();
  assert.equal(value(cells()[0]), '<0.1%', 'A positive gap never rounds down to a tie');
  data.damage[0].secondBest.performance.lossPct = .1;
  c.renderTftLeaderboard();
  assert.equal(value(cells()[0]), '0.1%');
  data.damage[0].secondBest.performance = { metric: 'killTime', status: 'noClear', lossPct: null,
    value: null, killTime: null, total: 7000 };
  c.renderTftLeaderboard();
  clear = cells()[0];
  assert.equal(value(clear), '—'); assert.equal(detail(clear), 'Does not clear');
  assert(clear.title.includes('does not clear') && clear.title.includes('percentage is unavailable')
    && clear.title.includes('7000 damage'));

  h.state.mode = 'tanks';
  const tank = data.tanks[0];
  tank.secondBest = runnerUp({ metric: 'stressAliveTime', status: 'comparable', lossPct: 10,
    value: 33.41106, aliveTime: 60, survivalCapped: true, stressAliveTime: 33.41106, stressCapped: false });
  c.renderTftLeaderboard();
  let held = cells()[0];
  assert.equal(value(held), '10.0%'); assert.equal(detail(held), 'less held at 2×');
  assert(held.title.includes('at least 60s (fight limit)') && held.title.includes('37.1234s at 2× pressure')
    && held.title.includes('33.41106s at 2× pressure'));
  Object.assign(tank.performance, { aliveTime: 50.12345, survivalCapped: false, stressAliveTime: null, stressCapped: false });
  tank.secondBest.performance = { metric: 'aliveTime', status: 'comparable', lossPct: 20,
    value: 40.09876, aliveTime: 40.09876, survivalCapped: false, stressAliveTime: null, stressCapped: false };
  c.renderTftLeaderboard();
  held = cells()[0];
  assert.equal(value(held), '20.0%'); assert.equal(detail(held), 'less time held');
  assert(held.title.includes('50.12345s') && held.title.includes('40.09876s'));
  assert(!held.title.includes('40.09876 damage'));

  Object.assign(tank.performance, { aliveTime: 60, survivalCapped: true, stressAliveTime: 60, stressCapped: true });
  for (const [metric, status, expected, performance] of [
    ['stressAliveTime', 'shortSurvival', 'Below regular limit', { aliveTime: 50, survivalCapped: false }],
    ['capped', 'belowCap', 'Below best limit', { aliveTime: 60, survivalCapped: true, stressAliveTime: 50, stressCapped: false }],
    ['aliveTime', 'unavailable', 'Unavailable', {}],
  ]) {
    tank.secondBest.performance = { metric, status, lossPct: null, value: null, ...performance };
    c.renderTftLeaderboard();
    held = cells()[0];
    assert.equal(value(held), '—'); assert.equal(detail(held), expected);
    assert(held.title.includes('unavailable'), `${status} explains why the percentage is missing`);
    assert(!/undefined|NaN/.test(held.textContent + held.title));
  }
  tank.secondBest.performance = { metric: 'capped', status: 'comparable', lossPct: 0,
    value: 60, aliveTime: 60, survivalCapped: true, stressAliveTime: 60, stressCapped: true };
  c.renderTftLeaderboard();
  held = cells()[0];
  assert.equal(value(held), '0.0%'); assert.equal(detail(held), 'same limits');
  assert(held.title.includes('tied in this benchmark') && held.title.includes('beyond those limits is unknown'));

  const legacy = harness();
  for (const row of legacy.element('tbody').children) {
    const gap = row.children[3];
    assert.equal(value(gap), '—'); assert.equal(detail(gap), 'Unavailable');
    assert(gap.title.includes('unavailable for this saved result'));
  }
}

async function checkTraitContexts() {
  const h = harness(), c = h.context;
  assert.deepEqual(h.element('tft-board-trait-seg').children.map(button => button.dataset.key), ['low', 'high']);
  assert(!h.element('tft-board-trait-seg').textContent.includes('None'));

  const previous = { ...payload(), selection: { key: 's2-clump-bare-mixed' } };
  h.state.traits = 'bare'; h.state.data = previous;
  c.renderTftLeaderboardControls();
  assert.equal(h.state.traits, 'low', 'Older no-trait selections open first breakpoints');
  assert.equal(h.state.data, null, 'No-trait results are cleared before displaying the new context');
  assert.equal(h.element('tbody').children.length, 0);
  await c.loadTftLeaderboard();
  assert.deepEqual(h.requests, ['api/tft/leaderboard/s2-clump-low-mixed.json']);
  assert.equal(h.state.data.selection.key, 's2-clump-low-mixed');
  assert.equal(previous.selection.key, 's2-clump-bare-mixed', 'Saved payloads remain unchanged');

  h.response = () => ({ ...payload(), selection: { key: 's2-clump-high-mixed' } });
  const loaded = new Promise(resolve => { c.scheduleTftStatus = resolve; });
  h.element('tft-board-trait-seg').children.find(button => button.dataset.key === 'high').events.click();
  await loaded;
  assert.equal(h.state.traits, 'high');
  assert.equal(h.requests.at(-1), 'api/tft/leaderboard/s2-clump-high-mixed.json');
  assert.equal(h.state.data.selection.key, 's2-clump-high-mixed');

  const restore = section('    if (hash.tab === "tft") {', '    if (view === "champion")');
  for (const traits of ['bare', 'low', 'high']) {
    const board = { traits: 'low' };
    vm.runInContext(restore, vm.createContext({
      hash: { tab: 'tft', tftview: 'leaderboard', btr: traits },
      tstate: { mode: 'builds' }, tboard: board, tcomp: {}, setTftMode() {},
    }));
    assert.equal(board.traits, traits === 'bare' ? 'low' : traits);
  }
}

async function checkClumpedFormation() {
  const h = harness(), c = h.context;
  const previous = { ...payload(), selection: { key: 's2-spread-low-mixed' } };
  Object.assign(h.state, { geo: 'spread', key: previous.selection.key, data: previous });
  assert.equal(vm.runInContext('tftLeaderboardKey()', c), 's2-clump-low-mixed',
    'Stale in-memory geometry cannot create a spread request');
  c.renderTftLeaderboardControls();
  assert.equal(h.state.geo, 'clump', 'An older separated formation selection opens clumped enemies');
  assert.equal(h.state.data, null, 'Saved spread results are cleared before displaying the clumped context');
  assert.equal(h.element('tbody').children.length, 0);
  await c.loadTftLeaderboard();
  assert.deepEqual(h.requests, ['api/tft/leaderboard/s2-clump-low-mixed.json']);
  assert.equal(h.state.data.selection.key, 's2-clump-low-mixed');
  assert.equal(previous.selection.key, 's2-spread-low-mixed', 'Saved spread payloads remain unchanged');

  let resolve;
  h.response = () => new Promise(finish => { resolve = finish; });
  Object.assign(h.state, { geo: 'spread', data: previous });
  const loading = c.loadTftLeaderboard({ preserve: true });
  assert.equal(h.state.geo, 'clump', 'Reloading normalizes a stale in-memory formation');
  assert.equal(h.state.data, null, 'Preserve cannot retain a spread payload while loading clumped results');
  resolve(payload());
  await loading;

  // Older metadata must describe its actual benchmark while the new generation is being prepared.
  h.champion.meta.dummy.slots = Array.from({ length: 5 }, (_, i) => ({ nearby: i < 3 }));
  c.renderTftLeaderboardControls();
  assert.equal(h.element('tft-board-formation').textContent,
    'Clumped · 3 frontliners + 2 backliners (5 enemies)', 'Enemy counts come from the published slots');
  delete h.champion.meta.dummy;
  c.renderTftLeaderboardControls();
  assert.equal(h.element('tft-board-formation').textContent, 'Clumped', 'Missing formation metadata does not invent enemy counts');

  h.response = () => previous;
  await c.loadTftLeaderboard();
  assert.equal(h.state.data, null, 'A spread response cannot be displayed as a clumped benchmark');
  assert(h.state.error, 'A mismatched response exposes a load failure');
}

async function checkLeaderboardLayout() {
  const h = harness(), c = h.context, wrap = h.element('tft-board-tablewrap');
  assert.equal(h.element('view-tft').dataset.mode, 'leaderboard');
  assert.match(html, /#view-tft\[data-mode="leaderboard"\]\s*\{\s*padding-bottom:\s*max\(560px,\s*100vh\)/,
    'Short leaderboards have room below them even on a small viewport');
  wrap.getBoundingClientRect = () => ({ height: wrap.hidden ? 0
    : Math.max(parseFloat(wrap.style.minHeight) || 0, h.element('tbody').children.length > 1 ? 560 : 100) });
  const requests = [];
  h.response = () => new Promise((resolve, reject) => { requests.push({ resolve, reject }); });

  const loaded = new Promise(resolve => { c.scheduleTftStatus = resolve; });
  h.element('tft-board-star-seg').children.find(button => button.dataset.key === 's1').events.click();
  assert.equal(h.state.data, null, 'Changing stars clears the previous scenario');
  assert.equal(h.element('tbody').children.length, 0, 'Old rows are removed while the new setting loads');
  assert.equal(wrap.hidden, false, 'The table region stays on the page during loading');
  assert.equal(wrap.style.minHeight, '560px', 'Loading reserves the previous table height');
  const single = payload();
  single.selection.key = 's1-clump-low-mixed';
  single.damage = [{ ...single.damage[0], star: 1, scenario: 's1-clump-low' }];
  requests[0].resolve(single);
  await loaded;
  assert.equal(h.element('tbody').children.length, 1);
  assert.equal(wrap.hidden, false);
  assert.equal(wrap.style.minHeight, '', 'The loaded one-unit table uses its own height');
  assert.equal(h.element('view-tft').dataset.mode, 'leaderboard', 'Bottom padding remains for a single result');

  h.state.star = 's2';
  const superseded = c.loadTftLeaderboard();
  assert.equal(wrap.style.minHeight, '100px');
  h.state.traits = 'high';
  const latest = c.loadTftLeaderboard();
  requests[1].resolve(payload());
  await superseded;
  assert.equal(h.state.loading, true);
  assert.equal(wrap.hidden, false);
  assert.equal(wrap.style.minHeight, '100px', 'A superseded request cannot release the current loading space');
  requests[2].resolve({ ...payload(), selection: { key: 's2-clump-high-mixed' }, damage: [] });
  await latest;
  assert.equal(wrap.style.minHeight, '', 'Empty results release the loading height');
  assert.equal(wrap.hidden, true);
  assert.equal(h.element('tft-board-empty').hidden, false);
  assert.equal(h.element('view-tft').dataset.mode, 'leaderboard', 'Empty results keep the page padding');

  const failed = c.loadTftLeaderboard();
  requests[3].reject(new Error('Fixture network failure'));
  await failed;
  assert(h.state.error);
  assert.equal(wrap.style.minHeight, '', 'A failed request releases the loading height');
  assert.equal(wrap.hidden, true);
  assert.equal(h.element('tft-board-retry').disabled, false);
  await c.setTftMode('builds', { load: false });
  assert.equal(h.element('view-tft').dataset.mode, 'builds', 'Leaderboard padding stays scoped to that page');
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
  const potentialGap = pinnedCurvePayload();
  potentialGap.damage[0].secondBest = { items: ['Potential runner', 'Two', 'Three'],
    itemApis: ['p1', 'two', 'three'], performance: { metric: 'damage', status: 'comparable', lossPct: 75, total: 15000 } };
  const pinned = harness(potentialGap);
  assert(pinned.element('tbody').children.every(row => row.children[3].querySelector('strong').textContent === '—'),
    'A potential runner-up cannot masquerade as the saved finite build comparison');
}

async function checkNavigationAndLoad() {
  const h = harness(), c = h.context;
  h.element('tbody').children[0].querySelector('.tft-board-champion').events.click();
  assert.deepEqual(h.navigations.at(-1), { unit: 'alpha', star: 2, geo: 'clump', traits: 'low' });
  assert.equal(h.champion.mode, 'builds');
  await c.setTftMode('leaderboard', { load: false, focus: true });
  assert.equal(h.focused.dataset.unit, 'alpha');
  c.state = { view: 'tft', tier: 'test' };
  c.location = { hash: '' }; c.restoringHash = false; c.restoredHash = null;
  c.history = { replaceState: (state, title, hash) => { h.hash = hash; c.location.hash = hash; } };
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
    killTime: 12, total: 3000, dps: 250, casts: 3, attacks: 12, breakdown: {}, left: [0, 0, 0, 0, 0] };
  const ad = { ...common, form: 'AD', objective: 'fighter', pressure: true, role: 'Attack Assassin', range: 1,
    hp: 2000, hpLeft: 400, armor: 50, mr: 50, aliveTime: 12, died: false, taken: 1600, absorbed: 2400,
    shielded: 0, healed: 0, denied: 0 };
  const ap = { ...common, form: 'AP', objective: 'carry', pressure: false, role: 'Magic Marksman', range: 4 };
  h.champion.data = { unit: 'alpha', objective: 'carry', pressureByBuild: true, rows: [ad, ap],
    scenario: { geometry: 'clump', duration: 30, dummy: { count: 5, star: 2, pressureDps: 100,
      // three frontliners within reach of nearby effects, two backline targets
      slots: Array.from({ length: 5 }, (_, i) => ({ hp: 1000, armor: 100, mr: 100, ad: 50, as: 1,
        ability: 100, nearby: i < 3, kind: i < 3 ? 'tank' : 'non-tank',
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
  assert(h.element('tft-pressure-note').textContent.includes('The 3 frontline dummies attack and cast'));
  assert(h.element('tft-pressure-note').textContent.includes('backline is a damage target only'));
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

function checkEnemyFormationPreviews() {
  // Keep the earlier five-target Nidalee fixture as an independent compatibility check.
  const h = checkFiniteEquippedPressure(), c = h.context;
  const unit = h.champion.meta.units.find(entry => entry.slug === 'alpha');
  const [fighter, carry] = h.champion.data.rows;
  unit.name = 'Sivir';
  const legacy = h.champion.data.scenario.dummy;
  const catalog = {
    clump: [{ lane: 0, row: 0 }, { lane: 1, row: 0 }, { lane: 2, row: 0 },
      { lane: 3, row: 0 }, { lane: 0, row: 2 }, { lane: 3, row: 2 }],
    spread: [{ lane: -3, row: 0 }, { lane: 0, row: 0 }, { lane: 3, row: 0 },
      { lane: 6, row: 0 }, { lane: -3, row: 2 }, { lane: 6, row: 2 }],
  };
  h.champion.meta.formations = catalog;
  const dummy = (front, back, count = front + back) => ({ count, star: 2, pressureDps: 100,
    slots: Array.from({ length: front + back }, (_, i) => ({
      hp: i === 0 ? 3000 : i < front ? 1800 : 1440, armor: 100, mr: 100,
      ad: 50, as: 1, ability: 100, manaStart: 0, manaMax: 100, manaPerAttack: 10,
      fixedDefenses: i === 0, nearby: i < front,
      line: i < front ? 'frontline' : 'backline', kind: i < front ? 'tank' : 'non-tank',
    })) });
  const diagram = () => h.element('tft-formation-diagram');
  const enemies = () => diagram().children.filter(node => node.tag === 'circle' && node.attributes['data-target']);
  const targets = nodes => nodes.map(node => Number(node.attributes['data-target'])).sort((a, b) => a - b);
  const render = (dm, build = carry, geometry = 'clump') => {
    Object.assign(h.champion.data.scenario, { dummy: dm, geometry });
    c.renderTftBreakdown(build);
  };
  const assertTargets = (front, back) => {
    assert.deepEqual(targets(enemies()), Array.from({ length: front + back }, (_, i) => i + 1),
      'Every published slot appears exactly once with the simulation target number');
    assert.equal(enemies().filter(node => node.attributes['data-line'] === 'frontline').length, front);
    assert.equal(enemies().filter(node => node.attributes['data-line'] === 'backline').length, back);
    assert.deepEqual(diagram().children.filter(node => node.attributes.class === 'tft-diagram-number'
      && /^\d+$/.test(node.textContent)).map(node => Number(node.textContent)).sort((a, b) => a - b),
    Array.from({ length: front + back }, (_, i) => i + 1), 'Visible diagram numbering agrees with target stats');
  };
  const assertNoUniversalSplash = () => {
    const notes = ['tft-diagram-desc', 'tft-formation-caption', 'tft-dummy-intro', 'tft-targeting-note']
      .map(id => id.startsWith('tft-diagram') ? diagram().children.find(node => node.attributes.id === id)?.textContent
        : h.element(id).textContent).join(' ');
    assert(!/outside nearby effects|only the frontline is within reach|backline is reached only|only whole-board and nearest-N/i.test(notes),
      'Formation prose does not turn a schematic row into a universal ability-radius rule');
    assert.equal(diagram().children.filter(node => node.tag === 'ellipse').length, 0,
      'A general preview does not mark the entire frontline as one ability’s coverage');
    assert(!/undefined|NaN/.test(diagram().textContent + notes));
  };

  // A stale aggregate count cannot override the actual slots loaded with the selected cell.
  const six = dummy(4, 2, 5), before = JSON.stringify({ six, catalog, unit, fighter, carry });
  render(six);
  assertTargets(4, 2); assertNoUniversalSplash();
  assert(h.element('tft-dummy-intro').textContent.includes('6 test targets'));
  assert(h.element('tft-targeting-note').textContent.includes('1 → 2 → 3 → 4 → 5 → 6'));
  assert.equal(h.element('tft-dummy-stats').children.length, 6);
  assert(h.element('tft-dummy-stats').children[0].textContent.includes('Main tank'));
  assert.deepEqual(enemies().map(node => [Number(node.attributes['data-lane']), Number(node.attributes['data-row'])]),
    catalog.clump.map(position => [position.lane, position.row]), 'Diagram locations use published engine coordinates');
  assert.equal(c.tftEnemyFormation(six, 'clump').mainTanks.length, 1);
  render(six, fighter);
  assert(h.element('tft-pressure-note').textContent.includes('The 4 frontline dummies attack and cast'));
  assert.equal(h.element('tft-pressure-details').hidden, false);
  render(six, carry, 'spread');
  assertTargets(4, 2); assertNoUniversalSplash();
  assert.deepEqual(enemies().map(node => [Number(node.attributes['data-lane']), Number(node.attributes['data-row'])]),
    catalog.spread.map(position => [position.lane, position.row]), 'Changing geometry reads its own published coordinates');
  assert.equal(JSON.stringify({ six, catalog, unit, fighter, carry }), before, 'Formation rendering preserves source metadata and builds');

  // Metadata is additive: the current six-slot catalog must never reposition a saved five-slot fight.
  render(legacy);
  assertTargets(3, 2); assertNoUniversalSplash();
  assert.equal(c.tftEnemyFormation(legacy, 'clump').positioned, false);
  assert(enemies().every(node => !('data-lane' in node.attributes) && !('data-row' in node.attributes)));
  assert(h.element('tft-dummy-intro').textContent.includes('5 test targets'));
  assert(h.element('tft-targeting-note').textContent.includes('1 → 2 → 3 → 4 → 5'));

  const seven = dummy(5, 2), sevenBefore = JSON.stringify(seven);
  render(seven);
  assertTargets(5, 2); assertNoUniversalSplash();
  assert.equal(c.tftEnemyFormation(seven, 'clump').positioned, false, 'Unsupported counts retain a labeled schematic');
  assert(h.element('tft-dummy-intro').textContent.includes('7 test targets'));
  assert.equal(h.element('tft-dummy-stats').children.length, 7);
  assert.equal(JSON.stringify(seven), sevenBefore);

  // A supplied custom formation wins over the shared standard, including its role and numbering order.
  const custom = dummy(2, 1);
  custom.slots = [custom.slots[2], custom.slots[0], custom.slots[1]];
  custom.slots.forEach((slot, i) => Object.assign(slot, { lane: [-2, 1, 4][i], row: [4, 0, 0][i] }));
  custom.slots[1].label = 'An imported label';
  const customBefore = JSON.stringify(custom);
  render(custom);
  assertTargets(2, 1); assertNoUniversalSplash();
  const formation = c.tftEnemyFormation(custom, 'clump');
  assert.equal(formation.positioned, true);
  assert.deepEqual(plain(formation.mainTanks.map(slot => [slot.index, slot.label])), [[1, 'Main tank']]);
  const ordered = [...enemies()].sort((a, b) => Number(a.attributes['data-target']) - Number(b.attributes['data-target']));
  assert.deepEqual(ordered.map(node => [Number(node.attributes['data-lane']), Number(node.attributes['data-row'])]),
    custom.slots.map(slot => [slot.lane, slot.row]));
  assert.equal(ordered[0].attributes['data-line'], 'backline');
  assert.equal(JSON.stringify(custom), customBefore, 'Custom coordinates are read without mutation');
  custom.slots[0].row = NaN;
  render(custom);
  assert.equal(c.tftEnemyFormation(custom, 'clump').positioned, false,
    'Partially invalid explicit coordinates do not silently switch to a different formation');
  assertTargets(2, 1);
  assert(enemies().every(node => !('data-lane' in node.attributes) && !('data-row' in node.attributes)));
  assert(!enemies().some(node => /NaN|undefined/.test(node.attributes.cx + ' ' + node.attributes.cy)));

  const tankDummy = dummy(4, 2);
  tankDummy.threat = { label: 'Mixed', attackers: 6, frontlineAttackers: 4, backlineAttackers: 2,
    dps: 1000, frontlineDps: 200, backlineDps: 800, physicalShare: .5, attackInterval: 1,
    castInterval: 6, backlineAttackShare: .8, backlineSpellPhysicalShare: .5,
    frontlineAttackInterval: 1, frontlineCastInterval: 6 };
  const tankBuild = { ...fighter, objective: 'tank', pressure: true };
  const tankScenario = { geometry: 'clump', duration: 60, threat: 'mixed', dummy: tankDummy };
  c.tftTankDebuffNote = () => 'Tank defenses.';
  c.renderTftFight(unit, tankScenario, tankBuild);
  assertTargets(4, 2); assertNoUniversalSplash();
  const incoming = () => diagram().children.filter(node => node.attributes['data-direction'] === 'incoming');
  assert.deepEqual(targets(incoming()), [1, 2, 3, 4, 5, 6], 'Each of the six attackers has an incoming arrow');
  assert.equal(incoming().filter(node => node.attributes['data-line'] === 'frontline').length, 4);
  assert.equal(incoming().filter(node => node.attributes['data-line'] === 'backline').length, 2);
  assert(h.element('tft-dummy-intro').textContent.includes('6 attackers'));
  unit.name = 'Hecarim';
  c.renderTftFight(unit, tankScenario, tankBuild);
  assertTargets(4, 2);
  assert.deepEqual(targets(diagram().children.filter(node => node.tag === 'ellipse')), [1, 2, 3],
    'Hecarim marks three nearest targets; adding a fourth frontliner does not expand his target limit');
  assert.deepEqual(targets(incoming()), [1, 2, 3, 4, 5, 6], 'The unmarked fourth frontliner and both carries still attack');
  c.renderTftFight(unit, { ...tankScenario, geometry: 'spread' }, tankBuild);
  assert.deepEqual(targets(diagram().children.filter(node => node.tag === 'ellipse')), [1, 2, 3],
    'Nearest-N targeting retains its cap when the frontline is separated');
  c.renderTftFight(unit, { ...tankScenario, dummy: { slots: [] } }, tankBuild);
  assert.equal(h.element('fight-preview').hidden, true, 'Missing targets hide the preview rather than inventing an enemy board');
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
  const before = JSON.stringify(data);
  const h = checkFiniteEquippedPressure(), c = h.context;
  h.champion.data = data;
  Object.assign(h.champion.meta.units[0], { slug: data.unit, name: data.unitName, cost: data.cost, objective: data.objective });
  h.champion.geo = data.scenario.geometry;
  c.TSRC_COLOR = {}; c.SLOTS = ['color']; c.css = value => value;
  c.bar = () => { const node = h.create('div'); node.append(Object.assign(h.create('span'), { className: 'bar-fill' })); return node; };
  h.element('tft-show-stats').checked = true;
  const columns = c.tftColumns(), states = new Set();
  const aliveColumn = columns.find(column => column.th === 'Alive');
  if (!aliveColumn) {
    assert(!data.pressureByBuild && data.rows.every(row => row.pressure === false),
      'A fully protected carry has no survival column');
  }
  for (const row of data.rows) {
    c.renderTftBreakdown(row);
    assert.equal(h.element('tft-formation-diagram').children.filter(node => node.tag === 'circle'
      && node.attributes['data-target']).length, data.scenario.dummy.slots.length,
    'Every real loadout retains the complete published enemy formation');
    assert.equal(h.element('tft-pressure-details').hidden, !row.pressure);
    assert(!/undefined|NaN/.test(h.element('tbd-hint').textContent + h.element('tft-selected-meta').textContent));
    const movement = data.objective !== 'tank' && data.scenario.dummy?.meleeRepositionSeconds > 0
      && Number.isFinite(row.range) && row.range <= 2 && Number.isFinite(row.movementTime);
    assert.equal(h.element('tbd-hint').textContent.includes('s moving'), movement);
    assert(columns.every(column => !/undefined|NaN/.test(column.td(row).textContent)));
    if (!row.pressure && aliveColumn) assert.equal(aliveColumn.td(row).textContent, 'Protected');
    else if (row.pressure) assert(Number.isFinite(row.hp) && columns.find(column => column.th === 'HP').td(row).textContent !== '—');
    if (data.unit === 'nidalee') {
      assert(['AD', 'AP'].includes(row.form));
      assert.equal(row.pressure, row.form === 'AD', 'Nidalee’s equipped form determines its actual exposure');
      assert.equal(row.objective, row.form === 'AD' ? 'fighter' : 'carry');
    }
    states.add(`${row.form}:${row.pressure}`);
  }
  assert(states.size > 0);
  assert.equal(JSON.stringify(data), before, 'Rendering real loadouts preserves their saved results');
  assert.equal(data.objective, 'carry');
  console.log(`Real finite-form UI checks passed: ${filename}; ${data.rows.length} rows retain actual pressure, roles, health and target counts; observed forms/exposure: ${[...states].join(', ')}.`);
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
  checkFiniteLeaderboard(); checkBisGaps(); checkPinnedCompatibility();
  await checkTraitContexts(); await checkClumpedFormation(); await checkLeaderboardLayout();
  await checkNavigationAndLoad(); checkFiniteEquippedPressure();
  checkEnemyFormationPreviews(); checkMovementLeaderboard(); checkMovementDetails();
  for (let i = 2; i < process.argv.length; i += 2) {
    assert(process.argv[i + 1]);
    if (process.argv[i] === '--payload') checkRealPayload(process.argv[i + 1]);
    else if (process.argv[i] === '--cell') checkRealCell(process.argv[i + 1]);
    else throw new Error(`Unknown argument ${process.argv[i]}`);
  }
  console.log('Finite damage UI checks passed: clear-time ranks, second-BIS build gaps with exact results, metric units, ties, tiny gaps, censored and missing comparisons, tank preservation, pinned diagnostic compatibility, filters, navigation, stable loading layout and short-table spacing, metadata-driven formation counts/numbering/coordinates with saved and custom boards, Hecarim’s three-target cap, Nidalee per-build roles/pressure/health, and optional movement assumptions/time with ranged and old-payload compatibility.');
})().catch(error => { console.error(error); process.exitCode = 1; });
