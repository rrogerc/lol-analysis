// Run: node jobs/test-tft-navigation-ui.cjs
// Exercises the dashboard's real routes and native history events without npm/browser dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script);
const plain = value => JSON.parse(JSON.stringify(value));
const declaration = name => {
  const start = script.search(new RegExp(`^const ${name}\\s*=`, 'm'));
  const tail = script.slice(start), head = tail.slice(0, tail.indexOf('\n'));
  const end = /=>\s*{$/.test(head) ? start + /^};/m.exec(tail).index + 1 : script.indexOf(';', start);
  assert(start >= 0 && end > start, `Missing dashboard declaration: ${name}`);
  return script.slice(start, end + 1);
};
const functionSource = name => {
  const start = script.search(new RegExp(`^(?:async )?function ${name}\\(`, 'm'));
  assert(start >= 0, `Missing dashboard function: ${name}`);
  const tail = script.slice(start), end = /^}/m.exec(tail);
  assert(end, `Missing dashboard function body: ${name}`);
  return tail.slice(0, end.index + 1);
};
const shell = script.slice(script.indexOf('// ---------------- shell: tabs, tier, hash routing ----------------'))
  .replace(/\ninit\(\);\s*$/, '\n');

class Element {
  constructor(tag = 'div', id = '') {
    this.tagName = tag; this.id = id; this.children = []; this.dataset = {}; this.events = {};
    this.attributes = {}; this.value = ''; this.hidden = false; this.disabled = false;
    this.style = {};
    this.className = ''; this._text = ''; this.scrollTop = 0;
    this.classList = { toggle: (name, active) => {
      const classes = new Set(this.className.split(' ').filter(Boolean));
      if (active) classes.add(name); else classes.delete(name);
      this.className = [...classes].join(' ');
    } };
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent ?? child).join(''); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = [...children]; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(type, callback) { (this.events[type] ||= []).push(callback); }
  async fire(type, extra = {}) {
    for (const callback of this.events[type] || []) await callback({ target: this, ...extra });
  }
  querySelectorAll(selector) {
    return this.children.flatMap(child => {
      if (!(child instanceof Element)) return [];
      const matches = selector.startsWith('.') ? child.className.split(' ').includes(selector.slice(1)) : child.tagName === selector;
      return [...(matches ? [child] : []), ...child.querySelectorAll(selector)];
    });
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() { this.focused = true; }
  scrollIntoView() {}
  getBoundingClientRect() { return { height: 0 }; }
}

function harness(initialHash = '') {
  const nodes = new Map(), node = id => {
    if (!nodes.has(id)) nodes.set(id, new Element('div', id));
    return nodes.get(id);
  };
  const views = ['overview', 'champion', 'onetricks', 'builds', 'tft', 'data'];
  const nav = views.map(view => { const button = new Element('button'); button.dataset.view = view; return button; });
  const panels = views.map(view => node('view-' + view));
  const document = {
    hidden: false, getElementById: node, createElement: tag => new Element(tag), addEventListener() {},
    querySelectorAll: selector => {
      if (selector === 'nav button') return nav;
      if (selector === '.view') return panels;
      const buttons = /^#([\w-]+) button$/.exec(selector);
      return buttons ? node(buttons[1]).querySelectorAll('button') : [];
    },
  };
  const location = { hash: initialHash }, listeners = new Map(), entries = [initialHash];
  const writes = [], requests = [], opens = [];
  let index = 0, responder = null;
  const emit = type => { for (const callback of listeners.get(type) || []) callback({ type }); };
  const window = { location, addEventListener: (type, callback) => {
    const callbacks = listeners.get(type) || []; callbacks.push(callback); listeners.set(type, callbacks);
  } };
  const history = {
    pushState: (_, __, hash) => {
      location.hash = String(hash); entries.splice(index + 1); entries.push(location.hash); ++index;
      writes.push({ type: 'push', hash: location.hash });
    },
    replaceState: (_, __, hash) => {
      location.hash = String(hash); entries[index] = location.hash;
      writes.push({ type: 'replace', hash: location.hash });
    },
  };
  const meta = { tiers: ['fixture', 'other'], defaultTier: 'fixture', platforms: [] };
  const context = vm.createContext({ document, window, location, history, URLSearchParams, AbortController,
    META: meta, setTimeout: () => 1, clearTimeout() {},
    api: async url => {
      requests.push(url);
      if (responder) return responder(url);
      if (url === 'api/meta.json') return meta;
      if (url.startsWith('api/tft/leaderboard/')) {
        const key = url.split('/').at(-1).replace(/\.json$/, '');
        return { revision: 'fixture-v1', selection: { key }, damage: [], tanks: [], complete: true };
      }
      return { pending: true };
    },
    openTft: async () => {
      opens.push({ view: context.state.view, mode: context.tstate.mode, unit: context.tstate.unit });
      if (['builds', 'leaderboard'].includes(context.tstate.mode)) context.renderTftMeta();
    },
    openBuilds: async () => {}, loadOverview: async () => {}, loadChampionList: async () => {},
    loadInventory: async () => {}, loadOnetricks: async () => {}, loadChampion: async () => {},
    checkItemHealth() {}, initBucketSeg() {}, renderTable() {},
    hideTftTooltip() {}, scheduleTftStatus() {}, renderTftSelected() {}, renderTftFight() {}, renderTftThreat() {},
    markTftCold() {}, clearTft() {}, clearTftCores() {}, showTftPending: () => { context.tstate.pending = true; },
    renderTftUnits() {}, renderTftLeaderboard() {}, renderTftPool() {}, renderTftRefresh() {},
    renderTftEarly() {}, renderTftEarlyControls() {}, renderTftEarlyMethod() {},
    renderTftFlex() {}, renderTftFlexControls() {}, renderBuildsMode() {}, syncScenarioSeg() {},
    clearBuilds() {}, loadBuildsView: async () => {},
    cancelTftFlex() {}, cancelTftEarly() {},
    loadTftFlex: async () => true, loadTftEarly: async () => true,
    tftFiniteLeaderboardValid: () => true,
  });
  const stateNames = ['state', 'bstate', 'bboard', 'tstate', 'tcore', 'tboard', 'tcomp', 'tearly', 'tflex'];
  vm.runInContext(stateNames.map(declaration).join('\n') + '\n' +
    ['TFT_CLASS', 'TFT_FLEX_SORTS', 'TFT_EARLY_SORTS', 'tkey', 'tftLeaderboardKey', 'tftRevision'].map(declaration).join('\n') + '\n' +
    `Object.assign(globalThis, { ${stateNames.join(', ')} });`, context);
  context.tstate.meta = {
    revision: 'fixture-v1', stars: [1, 2, 3], geometries: { clump: 'Clumped', spread: 'Spread' },
    dummy: { count: 6, tanks: 4, others: 2,
      slots: Array.from({ length: 6 }, (_, i) => ({ kind: i < 4 ? 'tank' : 'non-tank',
        line: i < 4 ? 'frontline' : 'backline', nearby: i < 4 })) },
    traitContexts: { bare: 'None', low: 'First breakpoint', high: 'Max traits' },
    tankThreats: [{ key: 'mixed' }, { key: 'physical' }],
    units: [
      { slug: 'alpha', api: 'Alpha', name: 'Alpha', objective: 'carry', cost: 2, stars: [1, 2, 3], traits: [] },
      { slug: 'beta', api: 'Beta', name: 'Beta', objective: 'tank', cost: 4, stars: [1, 2], traits: [] },
    ],
  };
  context.bstate.meta = {
    champions: [{ slug: 'annie', name: 'Annie', scenarios: ['squishy'] }],
    scenarios: [{ key: 'squishy' }], defaultChampion: 'annie', defaultScenario: 'squishy',
  };
  vm.runInContext([
    'tftText', 'tftActive', 'tftSeg', 'tftStarFor', 'tftThreatFor', 'renderTftStars', 'renderTftMode', 'setTftMode',
    'tftFlexCoreCost', 'restoreTftFlexHash', 'restoreTftEarlyHash', 'championScenarios', 'renderTftMeta', 'renderTftLeaderboardControls',
    'loadTftLeaderboard', 'loadTftScenario', 'openTftLeaderboardEntry',
  ].map(functionSource).join('\n') + '\n' + shell, context);
  const travel = direction => {
    const destination = index + direction;
    assert(destination >= 0 && destination < entries.length, 'Requested history entry exists');
    index = destination; location.hash = entries[index];
    emit('popstate'); emit('hashchange');
  };
  return {
    context, node, location, entries, writes, requests, opens, nav, listeners,
    get index() { return index; },
    back: () => travel(-1), forward: () => travel(1), emit,
    setHash: hash => {
      entries.splice(index + 1); entries.push(hash); ++index; location.hash = hash; emit('hashchange');
    },
    setResponder: value => { responder = value; },
    params: () => Object.fromEntries(new URLSearchParams(location.hash.slice(1))),
  };
}

async function checkInitialRoute() {
  const h = harness('#tab=tft&tftview=early&elevel=5&eunit=Ash%20%26%20Ember%3D%CE%B2&epage=2');
  await h.context.init();
  assert.equal(h.entries.length, 1, 'Bootstrap canonicalizes the existing entry');
  assert.equal(h.writes.filter(write => write.type === 'push').length, 0, 'Bootstrap creates no Back step');
  assert.deepEqual(h.writes.map(write => write.type), ['replace'], 'Bootstrap replaces the original incomplete route once');
  assert.equal(h.params().tier, 'fixture');
  assert.equal(h.context.tstate.mode, 'early');
  assert.equal(h.context.tearly.unit, 'Ash & Ember=β', 'URL restoration preserves encoded delimiters and Unicode');
  assert.equal(h.context.tearly.page, 2);
  assert.equal(h.node('tft-early-panel').hidden, false);
  assert.equal(h.listeners.get('popstate')?.length, 1, 'Native Back/Forward is wired to the page');
  assert.equal(h.listeners.get('hashchange')?.length, 1, 'Edited hash links are wired to the page');
  const writes = h.writes.length, opens = h.opens.length;
  h.emit('popstate'); h.emit('hashchange'); h.context.updateHash(true);
  assert.equal(h.writes.length, writes, 'Revisiting the current route adds no duplicate entry');
  assert.equal(h.opens.length, opens, 'Duplicate events do not reopen the current route');
}

async function checkLeaderboardBackAndForward() {
  const h = harness('#tab=tft&tier=fixture&unit=alpha&star=2&geo=clump&tr=bare');
  h.context.restoreHash();
  const { tstate, tboard } = h.context;
  Object.assign(tboard, { mode: 'tanks', star: 's2', geo: 'clump', traits: 'low', threat: 'physical',
    role: 'fighter', cost: '4', search: 'Ash & Ember=β' });
  await h.context.setTftMode('leaderboard');
  assert.equal(h.entries.length, 2, 'Selecting the leaderboard adds one entry');
  assert.equal(h.params().bq, 'Ash & Ember=β');
  assert.equal(h.node('tft-leaderboard-panel').hidden, false);
  tstate.sel = 2; // A previously viewed champion can leave an item-build selection in memory.
  h.context.openTftLeaderboardEntry({ unit: 'beta', star: 2, scenario: 's2-clump-low-physical' });
  await Promise.resolve();
  assert.equal(h.entries.length, 3, 'Opening a champion adds one entry even when mode and fight both update');
  assert.equal(tstate.mode, 'builds');
  assert.equal(tstate.unit, 'beta');
  assert.equal(h.node('tft-leaderboard-back').hidden, false);
  const writes = h.writes.length, opens = h.opens.length;
  h.back();
  assert.equal(tstate.mode, 'leaderboard');
  assert.equal(tboard.search, 'Ash & Ember=β');
  assert.equal(tboard.cost, '4');
  assert.equal(tboard.role, 'fighter');
  assert.equal(tboard.geo, 'clump');
  assert.equal(tboard.threat, 'physical');
  assert.equal(h.node('tft-leaderboard-panel').hidden, false);
  assert.equal(h.opens.length, opens + 1, 'popstate followed by hashchange restores once');
  assert.equal(h.entries.length, 3, 'Restoration preserves the Forward entry');
  assert.equal(h.writes.slice(writes).filter(write => write.type === 'push').length, 0, 'Back never pushes history');
  h.forward();
  assert.equal(tstate.mode, 'builds');
  assert.equal(tstate.unit, 'beta');
  assert.equal(tstate.star, 2);
  assert.equal(tstate.threat, 'physical');
  assert.equal(tboard.fromLeaderboard, true, 'Forward restores the leaderboard origin');
  h.back(); h.back();
  assert.equal(tstate.unit, 'alpha');
  assert.equal(tstate.threat, 'mixed', 'Omitted threat returns to the earlier default');
  assert.equal(tboard.fromLeaderboard, false, 'An ordinary champion route clears the later origin');
  assert.equal(h.node('tft-leaderboard-back').hidden, true);
}

async function checkLegacySpreadLeaderboardRoute() {
  const h = harness('#tab=tft&tftview=leaderboard&board=damage&bstar=s2&bgeo=spread&btr=low&bthreat=mixed');
  const saved = { revision: 'fixture-v1', selection: { key: 's2-spread-low-mixed' },
    damage: [], tanks: [], complete: true };
  Object.assign(h.context.tboard, { geo: 'spread', key: saved.selection.key, data: saved });
  await h.context.init();
  assert.equal(h.context.tboard.geo, 'clump', 'Legacy leaderboard links restore only the clumped formation');
  assert.equal(h.context.tboard.data, null, 'Legacy spread payloads are invalidated by the real controls');
  assert.equal(h.params().bgeo, 'clump', 'The restored URL records the clumped formation');
  assert.equal(h.entries.length, 1, 'Canonicalizing a legacy formation adds no Back step');
  assert(h.writes.every(write => write.type === 'replace'), 'Legacy normalization replaces the existing history entry');
  await h.context.loadTftLeaderboard();
  assert.equal(h.requests.filter(url => url.startsWith('api/tft/leaderboard/')).at(-1),
    'api/tft/leaderboard/s2-clump-low-mixed.json');
  assert.equal(h.context.tboard.data.selection.key, 's2-clump-low-mixed');
  assert.equal(saved.selection.key, 's2-spread-low-mixed', 'Restoration preserves the original saved payload');

  h.setHash('#tab=tft&tftview=builds&unit=alpha&star=2&geo=spread&tr=bare');
  assert.equal(h.context.tstate.geo, 'spread', 'Individual build diagnostics retain their formation option');
  assert.equal(h.params().geo, 'spread');
}

function checkRestorationDefaults() {
  const h = harness('#tab=tft&tftview=leaderboard&board=damage&bstar=s2&bgeo=clump&btr=low&bthreat=mixed');
  Object.assign(h.context.tboard, { cost: '5', role: 'fighter', search: 'newer', fromLeaderboard: true, returnUnit: 'beta' });
  Object.assign(h.context.tcomp, { fromComposition: true, returnBoard: 'later', returnUnit: 'beta' });
  h.context.restoreHash();
  assert.equal(h.context.tboard.cost, 'all', 'Omitted all-cost filter is not replaced by the initial four-cost filter');
  assert.equal(h.context.tboard.role, 'all');
  assert.equal(h.context.tboard.search, '');
  assert.equal(h.context.tboard.fromLeaderboard, false);
  assert.equal(h.context.tcomp.fromComposition, false);
  assert.equal(h.context.tcomp.returnBoard, null);

  Object.assign(h.context.tflex, { preference: 'vertical', minFrontline: 8, locked: ['alpha'], allowFive: true,
    page: 5, lobbyPlayers: 0, lobbyDevelopment: 1, lobbyOverrides: { alpha: 9 } });
  h.setHash('#tab=tft&tftview=traits');
  assert.equal(h.context.tflex.preference, 'breadth');
  assert.equal(h.context.tflex.minFrontline, 3);
  assert.deepEqual(plain(h.context.tflex.locked), []);
  assert.equal(h.context.tflex.allowFive, false);
  assert.equal(h.context.tflex.page, 1);
  assert.equal(h.context.tflex.lobbyPlayers, 7);
  assert.equal(h.context.tflex.lobbyDevelopment, .5);
  assert.deepEqual(plain(h.context.tflex.lobbyOverrides), {});

  Object.assign(h.context.tearly, { level: 5, allowThree: false, unit: 'beta', sort: 'gold', order: 'asc', page: 4 });
  h.setHash('#tab=tft&tftview=early');
  assert.deepEqual(plain({ level: h.context.tearly.level, allowThree: h.context.tearly.allowThree,
    unit: h.context.tearly.unit, sort: h.context.tearly.sort, order: h.context.tearly.order, page: h.context.tearly.page }),
  { level: 4, allowThree: true, unit: 'all', sort: 'wins', order: 'desc', page: 1 });
}

function checkFiltersAndPages() {
  const h = harness('#tab=tft&tier=fixture&unit=alpha&star=2&geo=clump&tr=bare');
  h.context.restoreHash();
  Object.assign(h.context.tstate, { cls: 'tank', cost: '4', search: 'Ash & Ember=β' });
  h.context.updateHash(true);
  assert.equal(h.params().uclass, 'tank');
  assert.equal(h.params().ucost, '4');
  assert.equal(h.params().uq, 'Ash & Ember=β');
  h.back();
  assert.equal(h.context.tstate.cls, 'all');
  assert.equal(h.context.tstate.cost, '2', 'Returning to an older route selects its champion’s cost');
  assert.equal(h.context.tstate.search, '');
  h.forward();
  assert.equal(h.context.tstate.cls, 'tank');
  assert.equal(h.context.tstate.cost, '4');
  assert.equal(h.context.tstate.search, 'Ash & Ember=β');
  assert.equal(h.node('tft-search').value, 'Ash & Ember=β');

  h.setHash('#tab=tft&tftview=traits&fpage=3&fkeep=alpha%2Cbeta&flplayers=0&fldev=0&flheld=alpha%3A0');
  assert.equal(h.context.tflex.page, 3);
  assert.deepEqual(plain(h.context.tflex.locked), ['alpha', 'beta']);
  assert.equal(h.context.tflex.lobbyPlayers, 0);
  assert.equal(h.context.tflex.lobbyDevelopment, 0);
  assert.deepEqual(plain(h.context.tflex.lobbyOverrides), { alpha: 0 });
  assert.equal(h.params().fpage, '3');
  h.setHash('#tab=tft&tftview=early&elevel=5&epage=4&ethree=0&esort=gold');
  assert.equal(h.context.tearly.page, 4);
  assert.equal(h.context.tearly.level, 5);
  assert.equal(h.context.tearly.allowThree, false);
  assert.equal(h.context.tearly.order, 'asc');
  assert.equal(h.params().epage, '4');
  h.back();
  assert.equal(h.context.tstate.mode, 'traits');
  assert.equal(h.context.tflex.page, 3);
  h.forward();
  assert.equal(h.context.tstate.mode, 'early');
  assert.equal(h.context.tearly.page, 4);
}

async function checkFightControlsAndBuildSelection() {
  const h = harness('#tab=tft&tier=fixture&unit=alpha&star=2&geo=clump&tr=bare');
  h.context.restoreHash();
  const star = h.node('tft-star-seg').querySelectorAll('button').find(button => button.dataset.key === '3');
  await star.fire('click');
  assert.equal(h.params().star, '3', 'The actual star button records its new fight');
  assert.equal(h.entries.length, 2);
  h.back();
  assert.equal(h.context.tstate.star, 2);
  h.forward();
  assert.equal(h.context.tstate.star, 3);

  h.context.tstate.sel = 3;
  h.context.updateHash(true);
  assert.equal(h.params().tbuild, '3');
  h.back();
  assert.equal(h.context.tstate.sel, 0, 'Back restores the omitted first build');
  h.forward();
  assert.equal(h.context.tstate.sel, 3, 'Forward restores the selected item-build row');
  const entries = h.entries.length;
  h.context.tstate.star = 1;
  await h.context.loadTftScenario({ navigate: true });
  assert.equal(h.context.tstate.sel, 0, 'A different fight starts at its first build');
  assert(!('tbuild' in h.params()));
  assert.equal(h.entries.length, entries + 1, 'The fight and selection reset form one navigation');

  const writes = h.writes.length, currentEntries = h.entries.length;
  h.context.tstate.star = 2;
  await h.context.loadTftScenario();
  assert.equal(h.entries.length, currentEntries, 'Background fight normalization does not add a Back step');
  assert.deepEqual(h.writes.slice(writes).map(write => write.type), ['replace']);
}

async function checkPendingFightNavigation() {
  const h = harness('#tab=tft&tier=fixture&unit=alpha&star=2&geo=clump&tr=bare');
  h.context.restoreHash();
  let resolve;
  h.setResponder(() => new Promise(finish => { resolve = finish; }));
  h.context.tstate.star = 3;
  const loading = h.context.loadTftScenario({ navigate: true });
  const request = h.context.tstate.req;
  assert.equal(h.params().star, '3', 'Navigation is available before the fight request finishes');
  h.back();
  assert.equal(h.context.tstate.star, 2);
  resolve({ pending: true });
  await loading;
  assert.equal(h.context.tstate.pending, false, 'The superseded response cannot mark the restored fight pending');
  assert(h.context.tstate.req > request, 'Restoration invalidates the earlier fight request');
  assert.equal(h.params().star, '2');
  assert.equal(h.entries.length, 2, 'Completing an old request preserves Back and Forward entries');
}

async function checkPendingPageChanges() {
  const flex = harness('#tab=tft&tftview=traits&fcore=alpha%2Cbeta&fpage=3');
  flex.context.restoreHash();
  flex.context.tflex.catalog = { revision: 'catalog-fixture' };
  let finishSearch;
  Object.assign(flex.context, {
    tftFlexSortedBoards: boards => boards, renderTftFlexTable: () => new Element('table'), renderTftFlexStatus() {},
    cancelTftFlexGold() {}, cancelTftFlexLobby() {}, beginTftFlexGold() {}, beginTftFlexLobby() {},
    tftFlexBrowse: () => new Promise(resolve => { finishSearch = resolve; }),
  });
  vm.runInContext(['tftFlexSettings', 'renderTftFlex', 'setTftFlexSort', 'runTftFlexSearch'].map(functionSource).join('\n'), flex.context);
  const searching = flex.context.runTftFlexSearch();
  assert.equal(flex.context.tflex.page, 3, 'A restored flex page survives the temporary empty search results');
  flex.context.setTftFlexSort('gold', 'asc');
  assert.equal(flex.context.tflex.page, 1, 'Changing sort while searching starts at the first page');
  finishSearch({ boards: Array.from({ length: 100 }, (_, id) => ({ id })), expanded: 100 });
  await searching;
  assert.equal(flex.context.tflex.page, 1, 'A completed search preserves the newer sort/page selection');
  assert(!('fpage' in flex.params()), 'The completed search cannot restore an old page into the newer route');
  assert.equal(flex.params().fsort, 'gold');

  const early = harness('#tab=tft&tftview=early&elevel=4&epage=3');
  early.context.restoreHash();
  const rows = Array.from({ length: 100 }, () => ({}));
  Object.assign(early.context, {
    tftEarlyCatalog: value => value,
    tftEarlyRows: () => early.context.tearly.data ? rows : [],
    tftEarlyVariant: () => ({ boards: rows }), renderTftEarlyTable() {},
  });
  let finishRefresh;
  early.setResponder(() => new Promise(resolve => { finishRefresh = resolve; }));
  const eventsStart = script.indexOf('for (const [id, field] of [["tft-early-level"');
  const eventsEnd = script.indexOf('// ---------------- early comps: end ----------------', eventsStart);
  assert(eventsStart >= 0 && eventsEnd > eventsStart, 'Early pagination event handlers are present');
  vm.runInContext(declaration('TFT_EARLY_PAGE_SIZE') + '\n' +
    ['renderTftEarly', 'setTftEarlySort', 'loadTftEarly'].map(functionSource).join('\n') + '\n' +
    script.slice(eventsStart, eventsEnd), early.context);
  const initialLoad = early.context.loadTftEarly();
  assert.equal(early.context.tearly.page, 3, 'A restored early page survives loading before any catalog exists');
  finishRefresh({ patch: '18.3', set: 18 });
  await initialLoad;
  assert.equal(early.context.tearly.page, 3);
  assert.equal(early.params().epage, '3');
  const refreshing = early.context.loadTftEarly({ force: true });
  early.context.setTftEarlySort('gold', 'asc');
  assert.equal(early.context.tearly.page, 1);
  finishRefresh({ patch: '18.3', set: 18 });
  await refreshing;
  assert.equal(early.context.tearly.page, 1, 'A completed catalog refresh preserves the newer early sort/page selection');
  assert(!('epage' in early.params()), 'The completed early refresh cannot overwrite the newer route with its earlier page');
  assert.equal(early.params().esort, 'gold');
  await early.node('tft-early-next').fire('click');
  await early.node('tft-early-next').fire('click');
  assert.equal(early.context.tearly.page, 3);
  const nextRefresh = early.context.loadTftEarly({ force: true });
  await early.node('tft-early-next').fire('click');
  assert.equal(early.context.tearly.page, 4);
  finishRefresh({ patch: '18.3', set: 18 });
  await nextRefresh;
  assert.equal(early.context.tearly.page, 4, 'Using the real Next button during a refresh survives its completion');
  assert.equal(early.params().epage, '4');
}

function checkFlexCoreNavigation() {
  const h = harness('#tab=tft&tftview=traits&fcost=2&fcore=beta%2Calpha%2Cbeta');
  h.context.restoreHash();
  assert.equal(h.context.tflex.cost, 2);
  assert.deepEqual(plain(h.context.tflex.cores), ['alpha', 'beta'], 'Core routes normalize duplicates and order');
  assert.equal(h.params().fcore, 'alpha,beta');
  Object.assign(h.context.tflex, { cost: 3, cores: ['third', 'Ash & Ember=β', 'second'], coreSearch: 'temporary' });
  h.context.updateHash(true);
  assert.equal(h.params().fcore, 'Ash & Ember=β,second,third', 'Multiple cores preserve encoded names');
  h.back();
  assert.equal(h.context.tflex.cost, 2);
  assert.deepEqual(plain(h.context.tflex.cores), ['alpha', 'beta'], 'Back restores the earlier core selection');
  assert.equal(h.context.tflex.coreSearch, '', 'Core search does not hide a restored selection');
  h.forward();
  assert.equal(h.context.tflex.cost, 3);
  assert.deepEqual(plain(h.context.tflex.cores), ['Ash & Ember=β', 'second', 'third'], 'Forward restores every core');
  h.setHash('#tab=tft&tftview=traits&ftank=beta&fcarry=alpha');
  assert.deepEqual(plain(h.context.tflex.cores), ['alpha', 'beta'], 'Old pinned pair links become selected cores');
  assert.equal(h.params().fcore, 'alpha,beta');
  assert(!('ftank' in h.params()) && !('fcarry' in h.params()), 'Canonical routes remove the old pair fields');
  h.setHash('#tab=tft&tftview=traits&fcore=&ftank=beta&fcarry=alpha');
  assert.deepEqual(plain(h.context.tflex.cores), [], 'An explicit empty selection takes precedence over legacy fields');
  h.setHash('#tab=tft&tftview=traits');
  assert.deepEqual(plain(h.context.tflex.cores), [], 'An unselected route clears earlier cores');
  assert.equal(h.context.tflex.cost, 4);
  h.context.tflex.cores = ['beta'];
  h.context.updateHash(true);
  assert.equal(h.params().fcore, 'beta', 'A single-champion browse filter is saved in the route');
  h.back();
  assert.deepEqual(plain(h.context.tflex.cores), [], 'Back restores browsing without a core filter');
  h.forward();
  assert.deepEqual(plain(h.context.tflex.cores), ['beta'], 'Forward restores the single-champion browse filter');
}

function checkBuildsAndTierCompatibility() {
  const h = harness();
  h.context.restoreHash();
  h.setHash('#tab=builds&tier=other&bview=leaderboard&champ=annie&sc=squishy&lbq=Ash%20%26%20Ember%3D%CE%B2&lbrev=1&lbs=dps&lbd=d');
  assert.equal(h.context.state.view, 'builds');
  assert.equal(h.context.state.tier, 'other');
  assert.equal(h.node('tier').value, 'other');
  assert.equal(h.context.bstate.mode, 'leaderboard');
  assert.equal(h.context.bboard.search, 'Ash & Ember=β');
  assert.equal(h.context.bboard.reviewedOnly, true);
  assert.equal(h.context.bboard.sortKey, 'dps');
  assert.equal(h.context.bboard.sortDir, -1);
  h.setHash('#tab=builds&tier=fixture');
  assert.equal(h.context.bstate.mode, 'champion');
  assert.equal(h.context.bstate.champion, 'annie');
  assert.equal(h.context.bstate.scenario, 'squishy');
  assert.equal(h.context.bboard.search, '');
  assert.equal(h.context.bboard.reviewedOnly, false);
  assert.equal(h.context.bboard.sortKey, 'rank');
  assert.equal(h.context.bboard.sortDir, 1);
  h.back();
  assert.equal(h.context.bstate.mode, 'leaderboard');
  assert.equal(h.context.state.tier, 'other');
  assert.equal(h.node('tier').value, 'other');
}

async function checkNativeTabsAndInvalidLinks() {
  const h = harness();
  await h.context.init();
  const canonical = h.location.hash;
  await h.nav.find(button => button.dataset.view === 'tft').fire('click');
  assert.equal(h.entries.length, 2);
  assert.equal(h.context.state.view, 'tft');
  h.back();
  assert.equal(h.context.state.view, 'overview');
  assert.equal(h.location.hash, canonical);
  h.forward();
  assert.equal(h.context.state.view, 'tft');
  h.back();
  await h.nav.find(button => button.dataset.view === 'data').fire('click');
  assert.equal(h.entries.length, 2, 'New navigation after Back replaces the abandoned Forward branch');
  assert.equal(h.context.state.view, 'data');

  assert.doesNotThrow(() => h.setHash('#tab=tft&tftview=early&eunit=%E0%A4%A'), 'Malformed percent escapes do not break navigation');
  h.setHash('#tab=missing&tier=missing');
  assert.equal(h.context.state.view, 'overview');
  assert.equal(h.context.state.tier, 'fixture');
  const writes = h.writes.length;
  h.context.META = null;
  h.location.hash = '#tab=tft'; h.emit('popstate'); h.emit('hashchange');
  assert.equal(h.writes.length, writes, 'History events wait until shell metadata is available');
}

async function main() {
  await checkInitialRoute();
  await checkLeaderboardBackAndForward();
  await checkLegacySpreadLeaderboardRoute();
  checkRestorationDefaults();
  checkFiltersAndPages();
  checkFlexCoreNavigation();
  await checkFightControlsAndBuildSelection();
  await checkPendingFightNavigation();
  await checkPendingPageChanges();
  checkBuildsAndTierCompatibility();
  await checkNativeTabsAndInvalidLinks();
  console.log('TFT navigation checks passed: initial replacement, native Back/Forward, duplicate events, leaderboard origins, route defaults, filters, pagination, build selection, fight controls, pending responses, encoded links, history branches and LoL routes.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
