// Run with node jobs/test-builds-survival-ui.cjs [--cell survival-cell.json].
// No browser or npm packages required. The Builds tab's Survival tier: a
// tank's scenarios, the time-to-die cells, the defense report's rows and
// moments, and the table and breakdown the page renders from a cell (a
// synthetic one, or a real cache file with --cell).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script); // the complete dashboard script parses
const section = (from, to) => script.slice(script.indexOf(from), script.indexOf(to));
const plain = value => JSON.parse(JSON.stringify(value));

// a minimal DOM: elements with children, text, classes and attributes
const create = tag => ({ tag, className: '', dataset: {}, attributes: {}, children: [], ownText: '',
  hidden: false, title: '', style: {},
  append(...children) {
    for (const child of children) this.children.push(typeof child === 'string' ? { textContent: child } : child);
  },
  replaceChildren(...children) { this.children = []; this.ownText = ''; this.append(...children); },
  setAttribute(key, value) { this.attributes[key] = String(value); },
  addEventListener() {},
  get textContent() { return this.ownText + this.children.map(child => child.textContent || '').join(''); },
  set textContent(value) { this.ownText = String(value); this.children = []; } });
const elements = new Map();
const element = id => { if (!elements.has(id)) elements.set(id, create('div')); return elements.get(id); };

const context = vm.createContext({
  document: { createElement: create, getElementById: element },
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  bstate: {},
  // the Build column's icons belong to the damage tier's tests
  buildCell: row => { const td = create('td'); td.textContent = row.items.join(' + '); return td; },
});
vm.runInContext([
  'const SLOTS = ["--s1","--s2","--s3","--s4","--s5","--s6","--s7","--s8"];',
  'const css = k => "";',
  'const fmtInt = n => n.toLocaleString("en-US");',
  'const SRC_COLOR = {};',
  section('function cell(text, cls) {', 'function metricCell('),
  section('function championScenarios(', 'async function loadBuildsMeta('),
  section('function bar(', 'function renderBreakdown('),
  section('const TTD_TITLE', 'async function openBuilds('),
  // the const-declared helpers, handed out of the script's scope
  'this.api = { DEFENSE_LABELS, TTD_TITLE, SURV_MEAN_TITLE };',
].join('\n'), context);
const { championScenarios, scenarioObjective, ttdText, shareText, defenseRows, defenseEvents,
        survivalColumns, renderSurvivalBreakdown } = context;

// ---- a tank sees the Survival tier; a carry the damage tiers ----
const meta = {
  scenarios: ['full-squishy', 'full-overall', 'survive-kayle', 'survive-overall'].map(key => ({ key })),
  champions: [{ slug: 'kayle', scenarios: ['full-squishy', 'full-overall'] },
              { slug: 'drmundo', scenarios: ['survive-kayle', 'survive-overall', 'gone'] }],
};
assert.deepEqual(plain(championScenarios(meta, 'drmundo')), ['survive-kayle', 'survive-overall'],
  "a tank's own scenarios, and only ones the meta lists");
assert.deepEqual(plain(championScenarios(meta, 'kayle')), ['full-squishy', 'full-overall']);
assert.deepEqual(plain(championScenarios({ scenarios: meta.scenarios, champions: [{ slug: 'kayle' }] }, 'kayle')),
  ['full-squishy', 'full-overall', 'survive-kayle', 'survive-overall'],
  'a serve older than the field shows every scenario');
assert.equal(scenarioObjective({ objective: 'survival' }), 'survival');
assert.equal(scenarioObjective({ key: 'full-tank' }), 'damage', 'a damage cell carries no objective');
assert.equal(scenarioObjective(null), 'damage');

// ---- time to die and distance from the best ----
assert.equal(ttdText({ ttd: 22.2346, died: true }), '22.23s');
assert.equal(ttdText({ ttd: 32.49, died: false }), '≈32.5s', 'still standing: extrapolated');
assert.equal(ttdText({ ttd: null, died: false }), '—', 'nothing got through');
assert.equal(ttdText(undefined), '—');
assert.equal(shareText(1), ' best');
assert.equal(shareText(0.9971), ' best', 'within rounding of the best');
assert.equal(shareText(0.8765), ' −12%');
assert.equal(shareText(null), '');

// ---- what kept it up, and when ----
const defense = { healedR: 4466, rBaseHealth: 1221, healedW: 1111, regen: 693, reducedCrit: 5196,
  reducedAttack: 1045, shieldMagic: 0, negated: 0, rAt: 8.17, lifelineAt: 4.81, voidbornAt: 5,
  steadfastAt: 8.76, zhonyaAt: null, reviveAt: null, wCasts: 2 };
assert.deepEqual(plain(defenseRows(defense)).map(([label]) => label),
  ["Randuin's (crits)", 'Maximum Dosage heal', 'Maximum Dosage base health', 'Heart Zapper heal',
   'Steelcaps (attacks)', 'Regeneration'], 'largest first, nothing that did nothing');
assert.deepEqual(plain(defenseRows(null)), []);
for (const [key] of context.api.DEFENSE_LABELS) assert.ok(typeof key === 'string');
assert.deepEqual(plain(defenseEvents({ defense, threshold: 0.1 })), [
  'lifeline at 4.81s', "Jak'Sho's resists at 5.00s", 'Maximum Dosage at 8.17s (at or below 10% health)',
  "Force of Nature's 70 MR at 8.76s", 'Heart Zapper ×2'], 'in the order they happened');
assert.match(plain(defenseEvents({ defense, threshold: 0 }))[2], /to live through a killing blow/,
  'threshold 0: cast only to survive a killing blow');
assert.deepEqual(plain(defenseEvents({})), []);

// ---- the table and the breakdown, from a cell ----
function syntheticCell(overall) {
  const attackers = [
    { key: 'survive-kayle', champion: 'kayle', name: 'Kayle', items: ['Infinity Edge'], duration: 30, level: 16 },
    { key: 'survive-kassadin', champion: 'kassadin', name: 'Kassadin', items: ['Malignance'], duration: 30, level: 16 }];
  const fight = (ttd, died, share) => ({ ttd, died, share, hpLeft: died ? 0 : 812, taken: 12678, dps: 570,
    attacks: 40, breakdown: { auto: 9405, wave: 1157 }, threshold: 0.1, defense });
  const row = (rank, kayle, kassadin) => ({ rank, items: ['Plated Steelcaps', "Randuin's Omen"], gold: 17000,
    hp: 4231, armor: 242, mr: 212, vs: { kayle, kassadin },
    ...(overall ? { survived: kassadin.died ? 0 : 1, mean: 26.13 }
                : { ttd: kayle.ttd, died: kayle.died }) });
  return { objective: 'survival', championName: 'Dr. Mundo',
    scenario: overall ? { key: 'survive-overall', overall: true, objective: 'survival', attackers }
                      : { key: 'survive-kayle', attacker: 'kayle', objective: 'survival', attackers },
    rows: [row(1, fight(22.35, true, 1), fight(30.56, false, 0.94)),
           row(2, fight(19.51, true, 0.873), fight(30.41, false, 0.935))] };
}

function check(d) {
  const sc = d.scenario;
  const cols = survivalColumns(sc);
  const heads = plain(cols.map(c => c.th));
  const names = sc.attackers.map(a => `vs ${a.name}`);
  if (sc.overall) {
    assert.deepEqual(heads, ['#', 'Build', ...names, 'Mean', 'Health', 'Armor', 'MR', 'Gold']);
  } else {
    const me = sc.attackers.find(a => a.key === sc.key);
    assert.deepEqual(heads, ['#', 'Build', 'Time to die',
      ...names.filter(n => n !== `vs ${me.name}`), 'Health', 'Armor', 'MR', 'Gold']);
  }
  const text = row => cols.map(c => c.td(row).textContent);
  for (const row of d.rows.slice(0, 50)) {
    const cells = text(row);
    assert.equal(cells[0], String(row.rank));
    for (const a of sc.attackers) {
      const v = row.vs[a.champion];
      assert.ok(v, `row ${row.rank} fought ${a.name}`);
      const shown = ttdText(v) + shareText(v.share);
      assert.ok(cells.includes(shown), `row ${row.rank} shows ${shown} for ${a.name}: ${cells}`);
    }
    if (sc.overall) assert.ok(cells.includes(row.mean.toFixed(2) + 's'));
    assert.equal(cells.at(-4), row.hp.toLocaleString('en-US'));
  }
  context.bstate.data = d;
  renderSurvivalBreakdown(d.rows[0]);
  const bars = element('bd-bars').textContent;
  assert.match(element('bd-title').textContent, /^Survival breakdown — #1: /);
  for (const a of sc.attackers) {
    const v = d.rows[0].vs[a.champion];
    assert.ok(bars.includes(`vs ${a.name}`), `a group per attacker: ${a.name}`);
    assert.ok(bars.includes(v.died ? `dies at ${v.ttd.toFixed(2)}s` : `still standing at ${a.duration}s`));
    for (const src of Object.keys(v.breakdown)) assert.ok(bars.includes(src), `bar for ${src}`);
    for (const [label] of plain(defenseRows(v.defense))) assert.ok(bars.includes(label), `bar for ${label}`);
  }
}

const arg = process.argv.indexOf('--cell');
if (arg > 0) {
  const d = JSON.parse(fs.readFileSync(process.argv[arg + 1], 'utf8'));
  assert.equal(scenarioObjective(d.scenario), 'survival', 'a Survival cell');
  check(d);
  console.log(`Builds survival UI checks passed on ${path.basename(process.argv[arg + 1])} (${d.rows.length} rows).`);
} else {
  check(syntheticCell(false));
  check(syntheticCell(true));
  console.log('Builds survival UI checks passed: scenarios per champion, time-to-die cells, defense rows and moments, table and breakdown.');
}
