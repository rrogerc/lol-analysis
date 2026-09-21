// Run with node jobs/test-builds-leaderboard-ui.cjs [--board leaderboard.json].
// No browser or npm packages required. The Builds tab's leaderboard: its
// scenarios, the columns of a target's and the overall board, the rows the
// page renders from a payload (a synthetic one, or a saved
// /api/builds/leaderboard/<scenario>.json with --board), the filters, sorting
// by a column, what a selected row shows below the table, opening a champion,
// and loading.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script); // the complete dashboard script parses
const section = (from, to) => script.slice(script.indexOf(from), script.indexOf(to));
const plain = value => JSON.parse(JSON.stringify(value));

for (const id of ['builds-mode-seg', 'builds-note-board', 'btbl-wrap', 'bboard', 'bboard-search',
                  'bboard-reviewed', 'bboard-status', 'bboard-wrap', 'bboard-tbl', 'bboard-failed',
                  'bd-generated'])
  assert.ok(html.includes(`id="${id}"`), `the page has #${id}`);

// a minimal DOM: elements with children, text, classes, attributes, listeners
const create = tag => ({ tag, className: '', dataset: {}, attributes: {}, children: [], ownText: '',
  hidden: false, title: '', style: {}, listeners: {}, value: '', checked: false, scrollTop: 0,
  classList: { set: new Set(), toggle(c, on) { on ? this.set.add(c) : this.set.delete(c); },
               contains(c) { return this.set.has(c); } },
  append(...children) {
    for (const child of children) this.children.push(typeof child === 'string' ? { textContent: child } : child);
  },
  replaceChildren(...children) { this.children = []; this.ownText = ''; this.append(...children); },
  setAttribute(key, value) { this.attributes[key] = String(value); },
  addEventListener(type, fn) { this.listeners[type] = fn; },
  focus() { calls.push(['focus', this]); context.document.activeElement = this; },
  get textContent() { return this.ownText + this.children.map(child => child.textContent || '').join(''); },
  set textContent(value) { this.ownText = String(value); this.children = []; } });
const elements = new Map();
const element = id => { if (!elements.has(id)) elements.set(id, create('div')); return elements.get(id); };
const button = key => { const b = create('button'); b.dataset.key = key; return b; };
const lists = { '#builds-mode-seg button': [button('champion'), button('leaderboard')],
                '#scenario-seg button': [button('full-squishy'), button('full-overall')] };

const calls = [];
const log = name => (...args) => { calls.push([name, ...args]); };
const timers = [];
let respond = null;  // what the next api() call answers
const context = vm.createContext({
  document: { createElement: create, getElementById: element, activeElement: null,
              // a heading's sort button is looked up among the headings the page built
              querySelector: sel => {
                const k = /^#bboard-tbl thead button\[data-k="(.+)"\]$/.exec(sel)?.[1];
                return !k ? element(sel)
                  : element('#bboard-tbl thead tr').children.map(th => th.children[0]).find(b => b?.dataset.k === k) || null;
              },
              querySelectorAll: sel => lists[sel] || [] },
  setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout: log('clearTimeout'),
  bstate: { mode: 'leaderboard', champion: 'kayle', scenario: 'full-squishy', req: 0, timer: null, status: null,
            meta: { itemsPatch: '16.18', note: 'Theoretical damage model.',
                    champions: [{ slug: 'kayle', name: 'Kayle' }, { slug: 'ahri', name: 'Ahri', generated: true },
                                { slug: 'drmundo', name: 'Dr. Mundo', objective: 'survival' }],
                    scenarios: [{ key: 'full-squishy', label: 'Full build vs squishy', objective: 'damage' },
                                { key: 'full-overall', label: 'Full build — overall', objective: 'damage' },
                                { key: 'survive-kayle', label: 'Survive Kayle', objective: 'survival' }] } },
  api: url => { calls.push(['api', url]); return respond(url); },
  // the rest of the tab, recorded: the cards below the table and the other view
  renderBreakdown: log('renderBreakdown'), renderGeneratedBanner: log('renderGeneratedBanner'),
  renderPool: log('renderPool'), clearBreakdown: log('clearBreakdown'), updateHash: log('updateHash'),
  clearBuilds: log('clearBuilds'),
  markCold() {}, syncScenarioSeg: log('syncScenarioSeg'), renderChampionPicker: log('renderChampionPicker'),
  loadScenario: log('loadScenario'), loadStatus: async () => { calls.push(['loadStatus']); },
  // the Build column's icons belong to the item tests
  buildCell: row => { const td = create('td'); td.textContent = row.items.join(' + '); return td; },
});
vm.runInContext([
  'const fmtInt = n => n.toLocaleString("en-US");',
  'const iconUrl = c => `icon:${c}`;',
  section('// Why cells stay cold when no warm is running', '// Say why the selected cell is missing'),
  section('function cell(text, cls) {', 'function metricCell('),
  section('function tftText(', 'function tftIcon('),
  section('function scenarioObjective(', '// A Survival fight'),
  section('// ---- the leaderboard: every champion', '// ---------------- tft view'),
  // the const-declared state, handed out of the script's scope
  'this.handedOut = { bboard, INSTANT_TITLE, UNREVIEWED_TITLE };',
].join('\n'), context);
const { leaderboardScenarios, boardColumns, boardRows, renderLeaderboard, loadLeaderboard,
        setBuildsMode, bstate } = context;
const { bboard, INSTANT_TITLE, UNREVIEWED_TITLE } = context.handedOut;
const took = name => calls.filter(c => c[0] === name);

// ---- the damage scenarios have a board; one tank compares nothing ----
assert.deepEqual(plain(leaderboardScenarios(bstate.meta)), ['full-squishy', 'full-overall']);
assert.deepEqual(plain(leaderboardScenarios(null)), []);

// ---- a synthetic payload, shaped as builds_leaderboard.cached_leaderboard's ----
const targets = [
  { key: 'full-squishy', target: 'squishy', targetHp: 2800, armor: 110, mr: 60, duration: 8 },
  { key: 'full-bruiser', target: 'bruiser', targetHp: 3800, armor: 180, mr: 120, duration: 12 },
  { key: 'full-tank', target: 'tank', targetHp: 4800, armor: 220, mr: 160, duration: 15 }];
const fight = (ttk, extra = {}) => ({ ttk, ttkExp: ttk, killTime: ttk, dps: ttk ? Math.round(3000 / ttk) : 0,
  total: 3000, attacks: 3, breakdown: { Q: 3000 }, ...extra });
const champ = (rank, champion, championName, reviewed, times, extra = {}) => ({
  rank, champion, championName, generated: !reviewed, reviewed, items: ['Boots', `${championName}'s item`],
  gold: 16000 + rank, ap: 0, ad: 300, attackSpeed: 1.2,
  vs: { squishy: fight(times[0]), bruiser: fight(times[1]), tank: times[2] == null
    ? fight(null, { ttkExp: null, killTime: 17.26, total: 4100 }) : fight(times[2]) }, ...extra });
const squishy = {
  scenario: { ...targets[0], label: 'Full build vs squishy', tier: 'full', level: 16, targets },
  complete: true, expectedCount: 4, readyCount: 4, pending: [], failed: [],
  rows: [[1, 'ahri', 'Ahri', false, [0, 4.49, 7.69]], [2, 'kassadin', 'Kassadin', true, [0.75, 2.2, 5.1]],
         [2, 'leesin', 'Lee Sin', false, [0.75, 3.5, null]], [4, 'kayle', 'Kayle', true, [1.07, 2.76, 4.46]]]
    .map(([rank, slug, name, reviewed, times]) => champ(rank, slug, name, reviewed, times,
      { ttk: times[0], ttkExp: times[0], dps: times[0] ? Math.round(3000 / times[0]) : 0, total: 3000 })),
};
const overall = {
  scenario: { key: 'full-overall', label: 'Full build — overall', tier: 'full', overall: true, level: 16, targets },
  complete: true, expectedCount: 2, readyCount: 2, pending: [], failed: [],
  rows: [champ(1, 'ahri', 'Ahri', false, [0, 4.49, 7.69], { kills: 3, mean: 0 }),
         champ(2, 'kayle', 'Kayle', true, [1.21, 2.5, 4.21], { kills: 3, mean: 2.34 })],
};

// ---- columns: a target's board leads with its own fight; overall shows them all ----
const heads = sc => plain(boardColumns(sc).map(c => c.th));
assert.deepEqual(heads(squishy.scenario),
  ['#', 'Champion', 'Best build', 'Kill time', 'vs bruiser', 'vs tank', 'DPS', 'Damage', 'Gold']);
assert.deepEqual(heads(overall.scenario),
  ['#', 'Champion', 'Best build', 'vs squishy', 'vs bruiser', 'vs tank', 'Mean', 'Gold']);

// ---- the table ----
const body = () => element('#bboard-tbl tbody').children;
const texts = () => body().map(tr => tr.children.map(td => td.textContent));
const show = (data, state = {}) => {
  Object.assign(bboard, { data, error: null, search: '', reviewedOnly: false, sortKey: 'rank', sortDir: 1 }, state);
  calls.length = 0;
  renderLeaderboard();
};
show(squishy);
assert.equal(element('builds-title').textContent, 'Champion leaderboard, full build vs squishy');
const head = () => element('#bboard-tbl thead tr').children;
const headTexts = () => head().map(th => th.textContent.trim());
assert.deepEqual(headTexts(), ['# ▲', ...heads(squishy.scenario).slice(1)], 'as ranked: the arrow is on #');
assert.deepEqual(texts(), [
  ['1', 'Ahriunreviewed', "Boots + Ahri's item", '0.00s instant', '4.49s', '7.69s', '—', '3,000', '16,001'],
  ['2', 'Kassadin', "Boots + Kassadin's item", '0.75s', '2.20s', '5.10s', '4,000', '3,000', '16,002'],
  ['2', 'Lee Sinunreviewed', "Boots + Lee Sin's item", '0.75s', '3.50s', '≈17.3s', '4,000', '3,000', '16,002'],
  ['4', 'Kayle', "Boots + Kayle's item", '1.07s', '2.76s', '4.46s', '2,804', '3,000', '16,004'],
]);
const [ahri, , leesin, kayle] = body();
assert.equal(ahri.children[3].title, INSTANT_TITLE, 'a kill at 0.00s says what it is');
assert.equal(ahri.children[1].children[2].title, UNREVIEWED_TITLE);
assert.equal(ahri.children[1].children[0].src, 'icon:ahri');
assert.match(leesin.children[5].title, /^survives the 15s fight \(4,100 damage dealt\)/);
assert.equal(kayle.children[3].title, '');
const desc = element('scenario-desc').textContent;
assert.match(desc, /^Level 16 · target 2,800 HP, 110 armor, 60 MR, 8s fight · ranked by expected kill time/);
assert.match(desc, /2 of 4 kits machine-written and unreviewed · items patch 16\.18/);
assert.equal(element('bboard-status').textContent, '4 champions');
assert.equal(element('bboard-failed').hidden, true);

// the champion the tab is on is the selected row; the cards below are its
assert.deepEqual(body().map(tr => tr.className), ['', '', '', 'sel']);
assert.deepEqual(plain(took('renderBreakdown')), [['renderBreakdown', plain(squishy.rows[3]), plain(squishy.scenario), 'Kayle']]);
assert.deepEqual(plain(took('renderGeneratedBanner')), [['renderGeneratedBanner', 'bd-generated', 'kayle']]);
assert.deepEqual(plain(took('renderPool')), [['renderPool', 'kayle']]);
// a champion that is not on the board (a tank) leaves the top row selected, and stays the tab's
bstate.champion = 'drmundo';
show(squishy);
assert.deepEqual(body().map(tr => tr.className), ['sel', '', '', '']);
assert.equal(took('renderPool')[0][1], 'ahri');
assert.equal(bstate.champion, 'drmundo');

// selecting a row makes its champion the tab's
calls.length = 0;
body()[1].listeners.click();
assert.equal(bstate.champion, 'kassadin');
assert.deepEqual(body().map(tr => tr.classList.contains('sel')), [false, true, false, false]);
assert.equal(took('renderBreakdown')[0][3], 'Kassadin');
assert.equal(took('updateHash').length, 1);

// ---- the overall board: every target, the mean, and what a 0.00s does to it ----
bstate.scenario = 'full-overall';
show(overall);
assert.equal(element('builds-title').textContent, 'Champion leaderboard, full build — overall');
assert.deepEqual(texts(), [
  ['1', 'Ahriunreviewed', "Boots + Ahri's item", '0.00s instant', '4.49s', '7.69s', '0.00s instant', '16,001'],
  ['2', 'Kayle', "Boots + Kayle's item", '1.21s', '2.50s', '4.21s', '2.34s', '16,002'],
]);
assert.equal(body()[0].children[6].title, INSTANT_TITLE);
assert.match(element('scenario-desc').textContent,
  /ranked on every target at once — squishy \(2,800 HP, 110 armor, 60 MR, 8s fight\); bruiser .*champions that kill all of them first/);
bstate.scenario = 'full-squishy';

// ---- filters keep the full board's ranks ----
show(squishy, { search: ' LEE' });
assert.deepEqual(texts().map(r => r.slice(0, 2)), [['2', 'Lee Sinunreviewed']]);
assert.equal(element('bboard-status').textContent, '1 of 4 champions · ranks are from the full board');
show(squishy, { search: "lee sin" });
assert.equal(body().length, 1, 'a name with a space matches too');
show(squishy, { reviewedOnly: true });
assert.deepEqual(texts().map(r => r.slice(0, 2)), [['2', 'Kassadin'], ['4', 'Kayle']]);
assert.equal(element('bboard-status').textContent, '2 of 4 champions · ranks are from the full board');
show(squishy, { search: 'zzz' });
assert.equal(body().length, 0);
assert.equal(element('bboard-status').textContent, 'No champion matches these filters.');
assert.deepEqual(plain(took('clearBreakdown')), [['clearBreakdown']], 'nothing selected: the cards are cleared');
assert.equal(element('bd-generated').hidden, true);
assert.deepEqual(plain(boardRows(null)), []);
// the filter boxes drive the same render
show(squishy);
element('bboard-search').listeners.input({ target: { value: 'kay' } });
assert.deepEqual(texts().map(r => r[1]), ['Kayle']);
element('bboard-search').listeners.input({ target: { value: '' } });
element('bboard-reviewed').listeners.change({ target: { checked: true } });
assert.equal(body().length, 2);
element('bboard-reviewed').listeners.change({ target: { checked: false } });

// ---- sorting by a column: a target's time, and everything else with a value ----
const names = () => body().map(tr => tr.children[1].children[1].textContent);
const ranks = () => body().map(tr => tr.children[0].textContent);
const sortButton = label => head().find(th => th.textContent.trim().replace(/ [▲▼]$/, '') === label).children[0];
const sortBy = label => sortButton(label).listeners.click();
const ariaSort = () => head().map(th => th.attributes['aria-sort'] || '');
bstate.champion = 'kayle';
show(squishy);
assert.deepEqual(head().map(th => th.children[0]?.tag === 'button'),
  [true, true, false, true, true, true, true, true, true], 'every heading but the build is a sort button');
assert.deepEqual(ariaSort(), ['ascending', '', '', '', '', '', '', '', '']);
assert.match(head()[5].title, /^The same build's expected kill time against the tank/, 'a heading keeps its explanation');
// vs tank, fastest first: the build that leaves the tank standing is last
element('bboard-wrap').scrollTop = 250;
calls.length = 0;
context.document.activeElement = sortButton('vs tank');  // as a click or the keyboard leaves it
sortBy('vs tank');
assert.deepEqual(names(), ['Kayle', 'Kassadin', 'Ahri', 'Lee Sin']);
assert.deepEqual(ranks(), ['4', '2', '1', '2'], "# stays the scenario's rank");
assert.deepEqual(headTexts(), ['#', 'Champion', 'Best build', 'Kill time', 'vs bruiser', 'vs tank ▲', 'DPS', 'Damage', 'Gold']);
assert.deepEqual(ariaSort(), ['', '', '', '', '', 'ascending', '', '', '']);
assert.equal(element('bboard-status').textContent, '4 champions · sorted by vs tank, fastest first');
assert.equal(element('bboard-wrap').scrollTop, 0, 'a new order is read from its top');
assert.deepEqual(took('focus').map(c => c[1]), [sortButton('vs tank')], 'the rebuilt heading keeps the keyboard');
assert.equal(took('updateHash').length, 1);
assert.deepEqual([bboard.sortKey, bboard.sortDir], ['vs-tank', 1]);
assert.deepEqual(body().map(tr => tr.className), ['sel', '', '', ''], "the selection follows the tab's champion, not a position");
assert.equal(took('renderBreakdown')[0][3], 'Kayle');
// so does a render the keyboard did not cause (a board filling in); focus elsewhere is left alone
calls.length = 0;
renderLeaderboard();
assert.deepEqual(took('focus').map(c => c[1]), [sortButton('vs tank')]);
context.document.activeElement = element('bboard-search');
renderLeaderboard();
assert.equal(took('focus').length, 1);
context.document.activeElement = null;
// again: the other way round
sortBy('vs tank');
assert.deepEqual(names(), ['Lee Sin', 'Ahri', 'Kassadin', 'Kayle']);
assert.equal(headTexts()[5], 'vs tank ▼');
assert.equal(ariaSort()[5], 'descending');
assert.equal(element('bboard-status').textContent, '4 champions · sorted by vs tank, slowest first');
// a target left standing sorts after every kill whatever its extrapolated time says
const odd = plain(squishy);
odd.rows[2].vs.tank.killTime = 1;
show(odd, { sortKey: 'vs-tank' });
assert.deepEqual(names(), ['Kayle', 'Kassadin', 'Ahri', 'Lee Sin']);
// equal values stay in rank order, in both directions
show(squishy, { sortKey: 'vs-squishy' });
assert.deepEqual(names(), ['Ahri', 'Kassadin', 'Lee Sin', 'Kayle']);
assert.equal(headTexts()[3], 'Kill time ▲', "a target's own column is the same sort on its board");
sortBy('Kill time');
assert.deepEqual(names(), ['Kayle', 'Kassadin', 'Lee Sin', 'Ahri']);
// numbers lead with the most; a row with nothing to sort on is last either way
show(squishy);
sortBy('DPS');
assert.deepEqual(names(), ['Kassadin', 'Lee Sin', 'Kayle', 'Ahri']);
assert.equal(element('bboard-status').textContent, '4 champions · sorted by DPS, highest first');
sortBy('DPS');
assert.deepEqual(names(), ['Kayle', 'Kassadin', 'Lee Sin', 'Ahri'], 'a 0.00s fight has no DPS: last in both directions');
const blank = plain(squishy);
delete blank.rows[0].vs.bruiser;
show(blank, { sortKey: 'vs-bruiser', sortDir: -1 });
assert.deepEqual(names(), ['Lee Sin', 'Kayle', 'Kassadin', 'Ahri']);
show(squishy);
sortBy('Damage');
assert.deepEqual([bboard.sortKey, bboard.sortDir], ['damage', -1]);
sortBy('Gold');
assert.deepEqual(names(), ['Ahri', 'Kassadin', 'Lee Sin', 'Kayle']);
assert.equal(element('bboard-status').textContent, '4 champions · sorted by Gold, cheapest first');
sortBy('Champion');
assert.deepEqual(names(), ['Ahri', 'Kassadin', 'Kayle', 'Lee Sin']);
const byName = boardColumns(squishy.scenario)[1].sort;
assert.ok(byName({ championName: "Kog'Maw" }) < byName({ championName: "K'Sante" }), 'as a champion select lists them');
sortBy('#');
assert.deepEqual(ranks(), ['1', '2', '2', '4']);
assert.equal(element('bboard-status').textContent, '4 champions', 'the board as it is ranked says nothing more');
sortBy('#');
assert.deepEqual(names(), ['Kayle', 'Kassadin', 'Lee Sin', 'Ahri']);
assert.equal(element('bboard-status').textContent, '4 champions · sorted by #, worst first');
// the filters keep the order, and say both
show(squishy, { search: 'ka', sortKey: 'vs-tank' });
assert.deepEqual(names(), ['Kayle', 'Kassadin']);
assert.equal(element('bboard-status').textContent,
  '2 of 4 champions · ranks are from the full board · sorted by vs tank, fastest first');
// a target's column keeps the order on the overall board; its own is the mean
bstate.scenario = 'full-overall';
show(overall, { sortKey: 'vs-tank' });
assert.deepEqual(names(), ['Kayle', 'Ahri']);
sortBy('Mean');
assert.deepEqual(names(), ['Ahri', 'Kayle']);
assert.equal(element('bboard-status').textContent, '2 champions · sorted by Mean, fastest first');
sortBy('Mean');
assert.deepEqual(names(), ['Kayle', 'Ahri']);
// a column this board lacks leaves it as ranked, and # is what a click then reverses
show(overall, { sortKey: 'dps', sortDir: -1 });
assert.deepEqual(names(), ['Ahri', 'Kayle']);
assert.equal(headTexts()[0], '# ▲');
assert.equal(element('bboard-status').textContent, '2 champions');
sortBy('#');
assert.deepEqual(names(), ['Kayle', 'Ahri']);
assert.deepEqual([bboard.sortKey, bboard.sortDir], ['rank', -1]);
bstate.scenario = 'full-squishy';

// the hash carries the order, and init reads it back
context.state = { view: 'builds', tier: 'soloq_masters_plus' };
let written = '';
context.history = { replaceState: (state, title, hash) => { written = hash; } };
const writeHash = vm.runInContext(
  `(() => { ${section('function updateHash(', 'function showView(')}; return updateHash; })()`, context);
const readHash = text => {
  const hash = Object.fromEntries(text.slice(1).split('&').filter(Boolean).map(kv => kv.split('=').map(decodeURIComponent)));
  Object.assign(bboard, { search: '', reviewedOnly: false, sortKey: 'rank', sortDir: 1 });
  vm.runInContext(`(hash => { ${section('    if (hash.tab === "builds" && hash.bview === "leaderboard") {', '    if (hash.tab === "tft") {')} })`, context)(hash);
  return hash;
};
Object.assign(bboard, { search: '', reviewedOnly: false, sortKey: 'rank', sortDir: 1 });
writeHash();
assert.ok(!/lbs|lbd/.test(written), `the board as ranked adds nothing: ${written}`);
for (const [sortKey, sortDir, lbs, lbd] of [['vs-tank', 1, 'vs-tank', undefined], ['dps', -1, 'dps', 'd'], ['rank', -1, 'rank', 'd']]) {
  Object.assign(bboard, { sortKey, sortDir });
  writeHash();
  const hash = readHash(written);
  assert.deepEqual([hash.bview, hash.lbs, hash.lbd], ['leaderboard', lbs, lbd], written);
  assert.deepEqual([bboard.sortKey, bboard.sortDir], [sortKey, sortDir], written);
}
readHash('#tab=builds&bview=leaderboard&lbs=nonsense');
show(squishy, { sortKey: bboard.sortKey });
assert.deepEqual(ranks(), ['1', '2', '2', '4'], 'an unknown column in a link is the board as ranked');

// ---- a board still filling in, and drivers that failed ----
bstate.status = { warmer: 'running' };
show({ ...squishy, complete: false, expectedCount: 9, readyCount: 6,
       failed: [{ champion: 'zed', championName: 'Zed', error: 'PanicException: boom' }] });
assert.equal(element('bboard-status').textContent,
  '4 champions · 6 of 9 computed for the current code and data (the rest are warming in the background): ranks are provisional');
assert.equal(element('bboard-failed').hidden, false);
assert.match(element('bboard-failed').textContent, /driver failed in the enumeration: Zed \(PanicException: boom\)\.$/);
bstate.status = { warmer: 'stale' };
renderLeaderboard();
assert.match(element('bboard-status').textContent, /\(serve is running older code than is on disk/);

// a refill keeps the table where it was scrolled to
element('bboard-wrap').scrollTop = 420;
renderLeaderboard();
assert.equal(element('bboard-wrap').scrollTop, 420);
element('bboard-wrap').scrollTop = 0;

// ---- a champion's name opens its ranked builds, under the board's scenario ----
show(squishy);
calls.length = 0;
let stopped = false;
const open = body()[0].children[1].children[1];
assert.equal(open.tag, 'button');
open.listeners.click({ stopPropagation() { stopped = true; } });
assert.ok(stopped, 'the row under the button is not selected as well');
assert.deepEqual([bstate.mode, bstate.champion, bstate.scenario], ['champion', 'ahri', 'full-squishy']);
assert.equal(took('loadScenario').length, 1);
assert.equal(took('clearBuilds').length, 1, "not another champion's rows while the cell loads");
assert.equal(element('builds-title').textContent, 'Ahri — ranked builds', "nor the board's title");
assert.equal(took('renderChampionPicker').length, 1, 'a machine-written champion joins the buttons');
const hidden = ids => ids.map(id => element(id).hidden);
assert.deepEqual(hidden(['bboard', 'builds-note-board', 'bd-generated']), [true, true, true]);
assert.deepEqual(hidden(['champion-seg', 'btbl-wrap', 'builds-note']), [false, false, false]);
assert.deepEqual(lists['#builds-mode-seg button'].map(b => b.attributes['aria-pressed']), ['true', 'false']);

// ---- loading ----
(async () => {
  // back to the board: what it holds shows at once, where it was scrolled, then it asks again
  bboard.scroll = 0;
  element('bboard-wrap').scrollTop = 0;
  respond = async () => squishy;
  calls.length = 0;
  setBuildsMode('leaderboard');
  assert.deepEqual(hidden(['bboard', 'champion-seg', 'btbl-wrap', 'champion-more', 'builds-generated', 'builds-note']),
    [false, true, true, true, true, true]);
  assert.equal(body().length, 4, 'rendered before the request answers');
  assert.deepEqual(plain(took('api')), [['api', 'api/builds/leaderboard/full-squishy.json']]);
  assert.ok(lists['#scenario-seg button'][0].classList.contains('active'));
  await new Promise(setImmediate);
  assert.equal(timers.length, 0, 'a complete board is not polled');
  assert.equal(took('loadStatus').length, 0);

  // leaving remembers the scroll offset a hidden box forgets
  element('bboard-wrap').scrollTop = 333;
  bstate.data = { champion: 'ahri', scenario: { key: 'full-squishy' } };  // the cell held from before
  calls.length = 0;
  setBuildsMode('champion');
  assert.equal(took('clearBuilds').length, 0, 'the same cell stays up while it refreshes');
  element('bboard-wrap').scrollTop = 0;
  setBuildsMode('leaderboard');
  assert.equal(element('bboard-wrap').scrollTop, 333);
  await new Promise(setImmediate);

  // another scenario: the old board is dropped, not shown under the new name
  bstate.scenario = 'full-overall';
  respond = async () => ({ ...overall, complete: false, readyCount: 1 });
  const loading = loadLeaderboard();
  assert.equal(body().length, 0);
  assert.equal(element('scenario-desc').textContent, 'Loading…');
  await loading;
  assert.equal(body().length, 2);
  assert.equal(took('loadStatus').length, 1, 'an incomplete board asks the warmer why');
  assert.equal(timers.length, 1, 'and is polled until the cells land');
  respond = async () => overall;
  timers.pop()();
  await new Promise(setImmediate);
  assert.equal(timers.length, 0);
  assert.equal(element('bboard-status').textContent, '2 champions');

  // a serve from before the endpoint answers 404: say so, keep what is shown, retry
  respond = async () => { throw new Error('HTTP 404'); };
  await loadLeaderboard();
  assert.equal(body().length, 2);
  assert.match(element('bboard-status').textContent,
    /^The leaderboard could not be loaded \(HTTP 404\)\. A dashboard started before the leaderboard existed needs a restart\. Showing the board as last loaded\.$/);
  assert.equal(timers.length, 1);
  timers.length = 0;
  // and a payload for another scenario is refused
  bboard.data = null;
  respond = async () => squishy;
  await loadLeaderboard();
  assert.match(element('bboard-status').textContent, /could not be loaded \(unexpected response\)/);
  assert.equal(body().length, 0);
  timers.length = 0;

  // a slow answer to a superseded request is dropped
  let release;
  respond = () => new Promise(resolve => { release = () => resolve(overall); });
  const slow = loadLeaderboard();
  bstate.scenario = 'full-squishy';
  const fast = (respond = async () => squishy, loadLeaderboard());
  release();
  await Promise.all([slow, fast]);
  assert.equal(bboard.data.scenario.key, 'full-squishy');
  assert.equal(body().length, 4);

  // ---- a saved payload renders whole ----
  const arg = process.argv.indexOf('--board');
  let real = '';
  if (arg > 0) {
    const d = JSON.parse(fs.readFileSync(process.argv[arg + 1], 'utf8'));
    bstate.scenario = d.scenario.key;
    show(d);
    assert.equal(body().length, d.rows.length);
    const width = boardColumns(d.scenario).length;
    body().forEach((tr, i) => {
      assert.equal(tr.children.length, width);
      assert.equal(tr.children[0].textContent, String(d.rows[i].rank));
      assert.ok(tr.children[1].textContent.startsWith(d.rows[i].championName));
      for (const td of tr.children) assert.ok(!/undefined|NaN/.test(td.textContent), `${d.rows[i].champion}: ${td.textContent}`);
    });
    const ranks = d.rows.map(r => r.rank);
    assert.deepEqual(ranks, [...ranks].sort((a, b) => a - b));
    // every column orders the whole board, both ways: nobody lost, values in order, blanks last
    const bySlug = new Map(d.rows.map(r => [r.champion, r]));
    const scalar = v => Array.isArray(v) ? v[0] * 1e6 + v[1] : v;
    const sortable = boardColumns(d.scenario).filter(c => c.sort);
    for (const c of sortable) for (const sortDir of [1, -1]) {
      show(d, { sortKey: c.key, sortDir });
      const shown = body().map(tr => tr.dataset.champion);
      assert.deepEqual([...shown].sort(), [...bySlug.keys()].sort(), `${c.th}: every champion once`);
      const values = shown.map(slug => c.sort(bySlug.get(slug)));
      const blank = values.findIndex(v => v == null);
      assert.ok(values.slice(blank < 0 ? values.length : blank).every(v => v == null), `${c.th}: blanks last`);
      values.filter(v => v != null).map(scalar).forEach((v, i, all) => {
        if (i) assert.ok(sortDir === 1 ? all[i - 1] <= v : all[i - 1] >= v, `${c.th} ${sortDir}: ${all[i - 1]} then ${v}`);
      });
    }
    real = ` and the saved ${d.scenario.key} board (${d.rows.length} champions, sorted by ${sortable.length} columns both ways)`;
  }
  console.log(`Builds leaderboard UI checks passed: scenarios, columns, rows, filters, sorting, selection, opening a champion, loading${real}.`);
})().catch(error => { console.error(error); process.exit(1); });
