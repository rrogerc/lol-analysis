// Run with node jobs/test-tft-composition-ui.cjs. No browser or npm packages required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../web/index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script);
const tcomp = { budget: 9, structure: 'auto', data: { results: { 9: { single: [], duoTank: [] } } } };
const context = vm.createContext({ tcomp });
vm.runInContext(script.slice(script.indexOf('const tftCompositionKey'), script.indexOf('async function loadTftCompositionMeta'))
  + script.slice(script.indexOf('function tftCompositionRows('), script.indexOf('function tftCompositionItem(')), context);

const roster = Array.from({ length: 8 }, (_, i) => ({ api: 'enemy-' + i, slug: 'enemy-' + i,
  name: 'Enemy ' + i, cost: 2, star: 2, items: [], itemApis: [], frontline: i < 3 }));
function result(wins, count, split, margin = .2, time = 12) {
  return { poolRevision: 'fixture-pool', poolSplit: split, opponentCount: count / 2,
    metrics: { benchmarkWins: wins, benchmarkCount: count, benchmarkWinRate: wins / count,
      benchmarkScore: 100 * wins / count, hpMargin: margin, clearTime: time, damageDps: 1000, frontlineTime: 10 },
    matchups: Array.from({ length: count }, (_, i) => ({ key: split + '-' + i,
      opponentId: split + '-' + Math.floor(i / 2), label: split + ' board ' + Math.floor(i / 2),
      initiative: i % 2, formation: 'clump', roster, outcome: i < wins ? 'win' : 'loss',
      duration: 20, damage: 20000, frontlineTime: 10, allyHpFraction: i < wins ? .4 : 0,
      enemyHpFraction: i < wins ? 0 : .4 })) };
}
function board(id, { wins = 6, count = 12, margin = .2, time = 12, heldOut = 3, density = 4 } = {}) {
  const row = { id, sameCostCount: density, purchaseGold: 80, ...result(wins, count, 'search', margin, time),
    validation: result(heldOut, 6, 'validation'),
    units: Array.from({ length: 8 }, (_, i) => ({ api: id + '-' + i, slug: id + '-' + i, name: 'Ally ' + i,
      items: i === 0 ? ['Selected'] : [], itemApis: i === 0 ? ['selected'] : [],
      dps: 125, damage: 2500, aliveTime: 10, damageTaken: 1000, healing: 0, shielding: 0, allyHealing: 0, allyShielding: 0 })) };
  row.assumptionCheck = { ...structuredClone(row.validation), healingPolicy: 'restricted', winDelta: 0 };
  row.itemAnalysis = { model: 'team-item-replacements-v1', evaluatedOn: 'search', poolRevision: row.poolRevision,
    matches: count, holders: [{ api: row.units[0].api, slug: row.units[0].slug, name: row.units[0].name,
      items: [{ slot: 0, itemApi: 'selected', item: 'Selected', testedAlternatives: 1, equivalentAlternatives: 1,
        bestWinDelta: 0, alternatives: [{ itemApi: 'alternative', item: 'Alternative', wins, winDelta: 0,
          lostMatchups: wins > 0 && wins < count ? ['search-0'] : [],
          gainedMatchups: wins > 0 && wins < count ? ['search-' + wins] : [] }] }] }] };
  return row;
}

const boardPlanModel = 'level8-core-level9-cap-v1';
function levelBoard(id, options = {}) {
  const row = board(id, options);
  Object.assign(row, { level: 8, boardSlots: 8, slotsUsed: 8, unitCount: 8,
    mainCarry: row.units[0].slug, mainTank: row.units[1].slug, itemCount: 2, structure: 'single',
    traits: [{ api: 'shared-trait', name: 'Shared trait', count: 2, breakpoint: 2, active: true, modeled: true }] });
  row.units.forEach((unit, i) => Object.assign(unit, { cost: i < 4 ? 4 : 2, star: 2, slotCost: 1,
    objective: i === 1 ? 'tank' : 'carry', assignment: i === 0 ? 'mainCarry' : i === 1 ? 'mainTank' : 'support' }));
  Object.assign(row.units[7], { items: ['Support item'], itemApis: ['support-item'] });
  for (const result of [row, row.validation, row.assumptionCheck]) {
    for (const matchup of result.matchups) {
      Object.assign(matchup, { level: 8, boardSlots: 8, slotsUsed: 8, unitCount: 8 });
      matchup.roster = matchup.roster.map(unit => ({ ...unit, slotCost: 1 }));
    }
  }
  return row;
}

function withUpgrade(parent, { elder = false, wins = 10, star = 2, declareStar = true } = {}) {
  const capped = structuredClone(parent);
  delete capped.itemAnalysis;
  capped.id += '-level9';
  const removed = capped.units.pop();
  const added = Array.from({ length: elder ? 1 : 2 }, (_, i) => ({ ...structuredClone(removed),
    api: elder ? 'TFT18_ElderDragon' : `${parent.id}-legendary-${i}`, slug: elder ? 'elder-dragon' : `${parent.id}-legendary-${i}`,
    name: elder ? 'Elder Dragon' : `Legendary ${i}`, cost: 5, star, slotCost: elder ? 2 : 1,
    items: i ? [] : ['Support item'], itemApis: i ? [] : ['support-item'] }));
  capped.units.push(...added);
  Object.assign(capped, { level: 9, boardSlots: 9, slotsUsed: 9, unitCount: capped.units.length });
  Object.assign(capped.metrics, { benchmarkWins: wins, benchmarkWinRate: wins / 12, benchmarkScore: 100 * wins / 12 });
  capped.matchups.forEach((matchup, i) => { matchup.outcome = i < wins ? 'win' : 'loss'; });
  capped.traits = [{ api: 'shared-trait', name: 'Shared trait', count: 1, breakpoint: null, active: false, modeled: true },
    { api: 'new-trait', name: 'New trait', count: 2, breakpoint: 2, active: true, modeled: true }];
  parent.level9Upgrade = { parentId: parent.id, board: capped,
    transition: { removed: [structuredClone(removed)], added: structuredClone(added),
      retained: parent.units.filter(unit => unit.slug !== removed.slug).map(unit => unit.slug),
      itemTransfers: [{ fromApi: removed.api, fromSlug: removed.slug, toApi: added[0].api,
        toSlug: added[0].slug, itemApi: 'support-item', item: 'Support item' }],
      traitChanges: [
        { api: 'shared-trait', name: 'Shared trait', beforeCount: 2, afterCount: 1,
          beforeBreakpoint: 2, afterBreakpoint: null, beforeActive: true, afterActive: false },
        { api: 'new-trait', name: 'New trait', beforeCount: 0, afterCount: 2,
          beforeBreakpoint: null, afterBreakpoint: 2, beforeActive: false, afterActive: true },
      ] },
    selection: { evaluatedOn: 'search', parentRanking: 'level8', preferredFiveCostSlots: 2, rostersCompared: 20, allocationsCompared: 40,
      ...(declareStar ? { fiveCostStar: star } : {}) },
    benchmarkWinDelta: wins - parent.metrics.benchmarkWins };
  return parent;
}

function withSingleUpgrade(parent, options = {}) {
  withUpgrade(parent, options);
  const upgrade = parent.level9Upgrade, added = upgrade.board.units.at(-1);
  upgrade.board.units = [...structuredClone(parent.units), added];
  Object.assign(upgrade.transition, { removed: [], added: [structuredClone(added)],
    retained: parent.units.map(unit => unit.slug), itemTransfers: [] });
  return parent;
}

function checkLevelPlans() {
  const previous = { data: tcomp.data, meta: tcomp.meta, profile: tcomp.profile, structure: tcomp.structure };
  tcomp.meta = { boardPlanModel, profiles: [{ key: 'c4', level9FiveCostStar: 2 }] }; tcomp.profile = 'c4'; tcomp.structure = 'auto';
  const base = withUpgrade(levelBoard('level8'), { wins: 10 });
  const elderCap = withUpgrade(levelBoard('elder-cap'), { elder: true });
  assert.equal(context.tftCompositionValid(base), true, 'Nine ordinary champions form a legal linked level 9 cap');
  assert.equal(context.tftCompositionBoardValid(base.level9Upgrade.board, 9, false), true);
  assert.equal(context.tftCompositionValid(elderCap), true, 'Elder plus seven other champions fills nine slots');
  assert.equal(context.tftCompositionValid(withSingleUpgrade(levelBoard('single-add'))), true,
    'A stronger one-legendary fallback can add a champion without selling a support');
  assert.equal(context.tftCompositionValid(base.level9Upgrade.board), false, 'A cap cannot appear in the level 8 leaderboard');
  assert.equal(context.tftCompositionValid(levelBoard('no-cap')), true, 'A legal level 8 core needs no cap');

  for (const [label, mutate] of [
    ['five-cost in a four-cost level 8 core', row => { row.units[6].cost = 5; row.units[6].star = 1; }],
    ['eight champions including Elder at level 8', row => { Object.assign(row.units[6], { api: 'TFT18_ElderDragon', cost: 5, star: 1, slotCost: 2 }); }],
    ['wrong core occupancy summary', row => { row.slotsUsed = 7; }],
    ['wrong core unit count', row => { row.unitCount = 7; }],
    ['wrong parent identity', row => { row.level9Upgrade.parentId = 'some-other-board'; }],
    ['wrong cap capacity', row => { row.level9Upgrade.board.boardSlots = 8; }],
    ['replaced main carry', row => { row.level9Upgrade.board.mainCarry = row.level9Upgrade.board.units[2].slug; }],
    ['wrong removed champion', row => { row.level9Upgrade.transition.removed[0].slug = row.mainTank; }],
    ['invented replacement items', row => { row.level9Upgrade.board.units.at(-1).items = ['Extra']; row.level9Upgrade.board.units.at(-1).itemApis = ['extra']; }],
    ['retained holder reitemized', row => { row.level9Upgrade.board.units[0].itemApis = ['other-item']; }],
    ['retained champion upgraded', row => { row.level9Upgrade.board.units[3].star = 1; }],
    ['one-star incoming legendary', row => { row.level9Upgrade.board.units.at(-1).star = 1; row.level9Upgrade.transition.added.at(-1).star = 1; }],
    ['three-star incoming legendary', row => { row.level9Upgrade.board.units.at(-1).star = 3; row.level9Upgrade.transition.added.at(-1).star = 3; }],
    ['wrong selection star assumption', row => { row.level9Upgrade.selection.fiveCostStar = 1; }],
    ['invalid selection star assumption', row => { row.level9Upgrade.selection.fiveCostStar = '2'; }],
    ['wrong incoming legendary identity', row => { row.level9Upgrade.transition.added[0].api = 'different'; }],
    ['missing retained champion', row => { row.level9Upgrade.transition.retained.pop(); }],
    ['wrong item transfer source', row => { row.level9Upgrade.transition.itemTransfers[0].fromSlug = row.mainCarry; }],
    ['wrong item transfer destination', row => { row.level9Upgrade.transition.itemTransfers[0].toSlug = row.mainCarry; }],
    ['missing transfer evidence', row => { row.level9Upgrade.transition.itemTransfers = []; }],
    ['wrong trait count', row => { row.level9Upgrade.transition.traitChanges[0].afterCount = 3; }],
    ['missing changed trait', row => { row.level9Upgrade.transition.traitChanges.pop(); }],
    ['wrong improvement comparison', row => { row.level9Upgrade.benchmarkWinDelta = 0; }],
    ['cap-rank selection', row => { row.level9Upgrade.selection.parentRanking = 'level9'; }],
    ['held-out cap selection', row => { row.level9Upgrade.selection.evaluatedOn = 'validation'; }],
    ['fake item alternatives on cap', row => { row.level9Upgrade.board.itemAnalysis = structuredClone(row.itemAnalysis); }],
  ]) {
    const invalid = structuredClone(base); mutate(invalid);
    assert.equal(context.tftCompositionValid(invalid), false, label + ' is rejected');
  }
  const badElder = structuredClone(elderCap);
  badElder.level9Upgrade.board.units.at(-1).slotCost = 1;
  assert.equal(context.tftCompositionValid(badElder), false, 'Elder cannot be relabeled as a single-slot champion');
  const oneStarElder = withUpgrade(levelBoard('one-star-elder'), { elder: true, star: 1 });
  assert.equal(context.tftCompositionValid(oneStarElder), false, 'Elder follows the same two-star cap assumption as other five-costs');
  const undeclared = structuredClone(base);
  delete undeclared.level9Upgrade.selection.fiveCostStar;
  assert.equal(context.tftCompositionValid(undeclared), true, 'The profile determines the required stars when selection metadata is absent');
  delete tcomp.meta.profiles[0].level9FiveCostStar;
  const previousCap = withUpgrade(levelBoard('previous-cap'), { star: 1, declareStar: false });
  assert.equal(context.tftCompositionValid(previousCap), true, 'Published one-star caps remain valid when older metadata has no cap star field');
  assert.equal(context.tftCompositionValid(base), false, 'New two-star results cannot be accepted with older one-star metadata');
  previousCap.level9Upgrade.selection.fiveCostStar = 2;
  assert.equal(context.tftCompositionValid(previousCap), false, 'Declared selection stars must agree with the legacy default too');
  tcomp.meta.profiles[0].level9FiveCostStar = 2;

  tcomp.profile = 'c3';
  const elderEight = levelBoard('elder-eight');
  elderEight.units.splice(6, 1);
  Object.assign(elderEight.units.at(-1), { api: 'TFT18_ElderDragon', slug: 'elder-dragon', name: 'Elder Dragon', cost: 5, star: 1, slotCost: 2 });
  elderEight.unitCount = 7;
  assert.equal(context.tftCompositionValid(elderEight), true, 'Elder plus six other champions fills eight slots');
  assert.equal(context.tftCompositionValid(base), false, 'Only four-cost plans have a linked level 9 cap');
  for (const result of [elderEight, elderEight.validation, elderEight.assumptionCheck]) {
    for (const matchup of result.matchups) {
      matchup.roster.pop();
      Object.assign(matchup.roster.at(-1), { api: 'TFT18_ElderDragon', slug: 'elder-dragon', name: 'Elder Dragon', cost: 5, star: 1, slotCost: 2 });
      matchup.unitCount = 7;
    }
  }
  assert.equal(context.tftCompositionValid(elderEight), true, 'Opponent occupancy also accepts seven actors with Elder');
  const invalidEnemy = structuredClone(elderEight);
  invalidEnemy.matchups[0].roster.push({ ...roster[7], slotCost: 1 });
  invalidEnemy.matchups[0].unitCount = 8;
  assert.equal(context.tftCompositionValid(invalidEnemy), false, 'Eight opponents including Elder exceeds the level 8 pool capacity');

  tcomp.profile = 'c4';
  const strongCore = withUpgrade(levelBoard('strong-core', { wins: 9 }), { wins: 9 });
  const strongerCap = withUpgrade(levelBoard('stronger-cap', { wins: 8 }), { wins: 12 });
  const tiedCore = withUpgrade(levelBoard('tied-core', { wins: 9 }), { wins: 12 });
  tcomp.data = { boardPlanModel, results: { 9: { single: [strongerCap, strongCore, tiedCore] } } };
  const snapshot = JSON.stringify(tcomp.data);
  assert.deepEqual(Array.from(context.tftCompositionRows(), row => row.id), ['strong-core', 'tied-core', 'stronger-cap']);
  assert.deepEqual(Array.from(context.tftCompositionRanks(context.tftCompositionRows())), [1, 1, 3],
    'Only the level 8 core determines ordering and shared ranks, regardless of the optional cap');
  assert.equal(JSON.stringify(tcomp.data), snapshot, 'Level plan validation and ranking preserve the saved artifact');
  Object.assign(tcomp, previous);
}

const tiedA = board('a', { wins: 8, margin: -.9, time: 30, heldOut: 0, density: 4 });
const tiedB = board('b', { wins: 8, margin: .9, time: 1, heldOut: 6, density: 8 });
const weaker = board('c', { wins: 7, margin: 1, time: .1, heldOut: 6, density: 8 });
const equivalentRate = board('d', { wins: 4, count: 6 });
tcomp.data.results[9].single = [weaker, tiedB, tiedA, equivalentRate];
let rows = context.tftCompositionRows();
assert.deepEqual(Array.from(rows, row => row.id), ['a', 'b', 'd', 'c'], 'Only win rate affects strength ordering');
assert.deepEqual(Array.from(context.tftCompositionRanks(rows)), [1, 1, 1, 4], 'Equal rates share competition ranks despite HP, speed, held-out results and cost density');
assert.equal(context.tftCompositionValid(tiedA), true, 'A tied replacement may exchange winning matchups');
const mutualElimination = structuredClone(tiedA);
mutualElimination.matchups.at(-1).outcome = 'draw';
mutualElimination.metrics.benchmarkDraws = 1;
assert.equal(context.tftCompositionValid(mutualElimination), true, 'Mutual elimination draws remain valid counted trials');
assert.equal(context.tftCompositionWinOrder(mutualElimination, tiedA), 0, 'A draw earns no half win or rank advantage over a loss');
mutualElimination.validation.matchups.at(-1).outcome = 'draw';
mutualElimination.validation.metrics.benchmarkDraws = 1;
assert.equal(context.tftCompositionValid(mutualElimination), true, 'Held-out draws are also accepted without affecting the rank');
assert.equal(context.tftCompositionHealingSummary(tiedA), 'Healing assumptions: same test outcomes.');
const sensitivity = board('sensitivity', { heldOut: 3 });
sensitivity.assumptionCheck.matchups[0].outcome = 'loss';
sensitivity.assumptionCheck.matchups[3].outcome = 'win';
assert.equal(context.tftCompositionHealingSummary(sensitivity), 'Healing assumptions: same win count; 2 test outcomes change.', 'Exchanged outcomes are not called identical results');
sensitivity.assumptionCheck.matchups[3].outcome = 'loss';
Object.assign(sensitivity.assumptionCheck.metrics, { benchmarkWins: 2, benchmarkWinRate: 2 / 6, benchmarkScore: 100 * 2 / 6 });
sensitivity.assumptionCheck.winDelta = -1;
assert.equal(context.tftCompositionValid(sensitivity), true);
assert.equal(context.tftCompositionHealingSummary(sensitivity), 'Healing assumptions: 1 fewer test win.');
assert.equal(context.tftCompositionWinOrder(sensitivity, board('ordinary')), 0, 'Healing sensitivity results do not affect win-rate ranks');
const unresolved = board('unresolved', { wins: 0 });
const unresolvedItem = unresolved.itemAnalysis.holders[0].items[0];
assert.equal(context.tftCompositionValid(unresolved), true);
assert.equal(context.tftCompositionItemVerdict(unresolvedItem, unresolved),
  'Unresolved in these tests. No tested replacement wins a search fight, so these results do not establish which item is core.',
  'Zero-win ties do not establish flexible or preferred core items');
Object.assign(unresolvedItem, { bestWinDelta: 1, equivalentAlternatives: 0 });
Object.assign(unresolvedItem.alternatives[0], { wins: 1, winDelta: 1, gainedMatchups: ['search-0'] });
assert.equal(context.tftCompositionValid(unresolved), true);
assert.equal(context.tftCompositionItemVerdict(unresolvedItem, unresolved), 'A tested replacement gains 1 win.',
  'An improving replacement from a zero-win baseline is still disclosed');
const strongerAllocation = structuredClone(tiedA);
strongerAllocation.id = 'stronger-allocation';
Object.assign(strongerAllocation, result(10, 12, 'search'));
Object.assign(strongerAllocation.itemAnalysis.holders[0].items[0].alternatives[0], { wins: 10, lostMatchups: ['search-0'], gainedMatchups: ['search-10'] });
tcomp.data.results[9].duoTank = [strongerAllocation];
rows = context.tftCompositionRows();
assert.equal(rows[0].id, 'stronger-allocation');
assert(!rows.some(row => row.id === 'a'), 'Repeated rosters retain their strongest allocation');
assert.deepEqual(Array.from(context.tftCompositionRanks(rows)), [1, 2, 2, 4]);

for (const [label, mutate] of [
  ['wrong model rate', row => { row.metrics.benchmarkWinRate = 1; }],
  ['missing held-out results', row => { delete row.validation; }],
  ['missing healing sensitivity check', row => { delete row.assumptionCheck; }],
  ['wrong sensitivity delta', row => { row.assumptionCheck.winDelta = 2; }],
  ['wrong sensitivity opponents', row => { row.assumptionCheck.poolRevision = 'different-pool'; }],
  ['overlapping held-out opponents', row => { row.validation.matchups[0].opponentId = row.matchups[0].opponentId; row.validation.matchups[1].opponentId = row.matchups[0].opponentId; }],
  ['missing initiative counterpart', row => { row.matchups[1].initiative = 0; }],
  ['legacy synthetic opponent', row => { delete row.matchups[0].roster; }],
  ['wrong item-evidence pool', row => { row.itemAnalysis.poolRevision = 'old-pool'; }],
  ['wrong holder identity', row => { row.itemAnalysis.holders[0].slug = row.units[1].slug; }],
  ['wrong equipped item', row => { row.itemAnalysis.holders[0].items[0].itemApi = 'other'; }],
  ['unknown changed matchup', row => { row.itemAnalysis.holders[0].items[0].alternatives[0].lostMatchups = ['missing']; }],
  ['incorrect replacement win delta', row => { row.itemAnalysis.holders[0].items[0].alternatives[0].winDelta = 1; }],
  ['held-out item optimization', row => { row.itemAnalysis.evaluatedOn = 'validation'; }],
]) {
  const invalid = structuredClone(tiedA); mutate(invalid);
  assert.equal(context.tftCompositionValid(invalid), false, label + ' is rejected');
}
const before = JSON.stringify(tcomp.data); context.tftCompositionRows();
assert.equal(JSON.stringify(tcomp.data), before, 'Rendering does not mutate cached records');
tcomp.profile = 'c2'; tcomp.geo = 'spread'; tcomp.threat = 'physical';
assert.equal(vm.runInContext('tftCompositionKey()', context), 'c2-spread-mixed', 'Old pressure contexts use the single reference pool');
assert(!html.includes('id="tft-comp-threat-seg"'), 'Obsolete composition pressure controls are absent');
assert(html.includes('id="tft-comp-geo-label">Fight formation'), 'Composition geometry describes both teams');
assert(html.includes('formation applies to both teams.'), 'Composition methodology makes the shared geometry assumption explicit');
assert(html.includes('id="tft-geo-label">Enemy formation') && html.includes('id="tft-board-geo-label">Enemy formation'),
  'Individual champion and leaderboard geometry labels remain unchanged');
console.log('Symmetric composition UI checks passed: win-only shared ranks, held-out separation, paired opponents, item evidence, cache guards and canonical contexts.');

function loadHarness() {
  const meta = { revision: 'composition-v1', baselineRevision: 'champions-v1',
    methodology: { evaluationModel: 'symmetric-reference-pool-v1' },
    opponentPool: { hash: 'pool-v1', version: 1 } };
  const state = { meta, profile: 'c2', geo: 'spread', threat: 'mixed', budget: 9,
    structure: 'auto', data: null, key: null, req: 0, pending: false, error: null, shown: 3 };
  const harness = { state, baseline: 'champions-v1', calls: [], renders: 0, polls: 0,
    response: null, metaRefresh: null, retry: null };
  harness.payload = () => ({ revision: state.meta.revision, baselineRevision: harness.baseline,
    boardSize: 8, key: `${state.profile}-${state.geo}-mixed`, profile: { key: state.profile },
    geometry: state.geo, threat: 'mixed', methodology: { evaluationModel: 'symmetric-reference-pool-v1' },
    opponentPool: structuredClone(state.meta.opponentPool), results: { 9: { single: [board('cached')] } } });
  const ctx = vm.createContext({ tcomp: state, AbortController,
    tftRevision: () => harness.baseline,
    api: async (path, options) => {
      harness.calls.push({ path, options });
      return harness.response ? harness.response() : harness.payload();
    },
    loadTftCompositionMeta: async force => {
      if (force && harness.metaRefresh) harness.metaRefresh();
      return true;
    },
    renderTftCompositions: () => { ++harness.renders; }, updateHash: () => {},
    scheduleTftStatus: () => { ++harness.polls; },
    document: { getElementById: id => {
      assert.equal(id, 'tft-comp-retry');
      return { addEventListener: (event, callback) => {
        assert.equal(event, 'click'); harness.retry = callback;
      } };
    } },
  });
  vm.runInContext(script.slice(script.indexOf('const tftCompositionKey'), script.indexOf('async function loadTftCompositionMeta'))
    + script.slice(script.indexOf('async function loadTftCompositions('), script.indexOf('async function pollTftCompositions('))
    + script.split('\n').find(line => line.startsWith('document.getElementById("tft-comp-retry").addEventListener')), ctx);
  harness.load = options => ctx.loadTftCompositions(options);
  harness.context = ctx;
  return harness;
}

function levelLoadHarness({ legacy = false } = {}) {
  const h = loadHarness();
  Object.assign(h.state, { profile: 'c4' });
  Object.assign(h.state.meta, { boardPlanModel,
    profiles: [{ key: 'c4', cost: 4, level: 8, boardSlots: 8, maxFiveCosts: 0,
      ...(legacy ? {} : { level9FiveCostStar: 2 }) }] });
  const oldPayload = h.payload;
  h.payload = () => ({ ...oldPayload(), boardPlanModel,
    results: { 9: { single: [withUpgrade(levelBoard('cached-levels'), { star: legacy ? 1 : 2, declareStar: !legacy })] } } });
  return h;
}

async function checkSavedLevelPlans() {
  const h = levelLoadHarness();
  await h.load();
  assert.equal(h.state.error, null, 'New level plans pass the complete artifact response guard');
  const saved = h.state.data;
  await h.load({ preserve: true });
  assert.equal(h.calls.length, 1, 'Returning from a cap reuses the same validated saved generation');
  assert.equal(h.state.data, saved);
  for (const [label, corrupt] of [
    ['missing artifact model', d => { delete d.boardPlanModel; }],
    ['different artifact model', d => { d.boardPlanModel = 'some-other-plan'; }],
    ['missing occupancy', d => { delete d.results[9].single[0].slotsUsed; }],
    ['incorrect unit slot cost', d => { d.results[9].single[0].units[4].slotCost = 2; }],
    ['bad optional cap', d => { d.results[9].single[0].level9Upgrade.parentId = 'another-parent'; }],
    ['wrong cap star assumption', d => { d.results[9].single[0].level9Upgrade.selection.fiveCostStar = 1; }],
  ]) {
    const invalid = levelLoadHarness();
    invalid.response = () => { const d = invalid.payload(); corrupt(d); return d; };
    await invalid.load();
    assert.equal(invalid.state.data, null, `${label} cannot enter the reusable cache`);
    assert(invalid.state.error);
  }
  const legacy = loadHarness();
  legacy.response = () => ({ ...legacy.payload(), boardPlanModel });
  await legacy.load();
  assert.equal(legacy.state.data, null, 'New results cannot be interpreted with legacy metadata');
  const transition = loadHarness();
  await transition.load();
  transition.state.meta.boardPlanModel = boardPlanModel;
  transition.response = () => ({ ...transition.payload(), boardPlanModel,
    results: { 9: { single: [levelBoard('new-publication')] } } });
  await transition.load({ preserve: true });
  assert.equal(transition.calls.length, 2, 'Changing the board model invalidates old cached data even if a revision was reused');
  assert.equal(transition.state.error, null);

  const oldStars = levelLoadHarness({ legacy: true });
  await oldStars.load();
  const previous = oldStars.state.data;
  await oldStars.load({ preserve: true });
  assert.equal(oldStars.calls.length, 1, 'An unchanged old one-star publication remains reusable');
  assert.equal(previous.results[9].single[0].level9Upgrade.board.units.at(-1).star, 1);
  Object.assign(oldStars.state.meta, { revision: 'composition-two-stars' });
  oldStars.state.meta.profiles[0].level9FiveCostStar = 2;
  oldStars.response = () => ({ ...oldStars.payload(), results: { 9: { single: [withUpgrade(levelBoard('two-star-publication'))] } } });
  await oldStars.load({ preserve: true });
  assert.equal(oldStars.calls.length, 2, 'The new composition revision loads its two-star calculation');
  assert.equal(oldStars.state.error, null);
  assert.equal(oldStars.state.data.results[9].single[0].level9Upgrade.board.units.at(-1).star, 2);
  assert.equal(previous.results[9].single[0].level9Upgrade.board.units.at(-1).star, 1, 'The handoff never relabels old one-star results');

  const policy = levelLoadHarness({ legacy: true });
  await policy.load();
  policy.state.meta.profiles[0].level9FiveCostStar = 2;
  policy.response = () => ({ ...policy.payload(), results: { 9: { single: [withUpgrade(levelBoard('updated-star-policy'))] } } });
  await policy.load({ preserve: true });
  assert.equal(policy.calls.length, 2, 'Validated cache reuse also requires the same cap star policy');
  assert.equal(policy.state.error, null);
  await policy.load({ preserve: true });
  assert.equal(policy.calls.length, 2, 'Matching two-star data remains reusable after the policy update');

  for (const star of [0, 3, '2', null]) {
    const invalid = publicationHarness();
    Object.assign(invalid.serverComposition, { boardPlanModel,
      profiles: [{ key: 'c4', cost: 4, level: 8, boardSlots: 8, maxFiveCosts: 0, level9FiveCostStar: star }] });
    assert.equal(await invalid.context.loadTftCompositionMeta(true), false, `Invalid declared cap star ${JSON.stringify(star)} is rejected in metadata`);
  }
}

function domHarness({ legacy = false } = {}) {
  let focused = null;
  const createElement = tag => ({ tag, dataset: {}, attributes: {}, children: [], events: {}, open: false,
    className: '', ownText: '', style: { values: {}, setProperty(name, value) { this.values[name] = value; } },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(event, listener) { this.events[event] = listener; },
    focus() { focused = this; }, scrollIntoView() {},
    querySelectorAll(selector) {
      const result = [];
      for (const child of this.children) {
        if (child.className?.split(' ').includes(selector.slice(1))) result.push(child);
        if (child.querySelectorAll) result.push(...child.querySelectorAll(selector));
      }
      return result;
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    get textContent() { return this.ownText + this.children.map(child => child.textContent || '').join(''); },
    set textContent(text) { this.ownText = text; this.children = []; },
  });
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, Object.assign(createElement('div'), { id }));
    return elements.get(id);
  };
  const parent = withUpgrade(levelBoard('dom-core'), { star: legacy ? 1 : 2, declareStar: !legacy });
  const composition = { profile: 'c4', geo: 'clump', threat: 'mixed', budget: 9, structure: 'auto', shown: 3,
    key: 'c4-clump-mixed', req: 0,
    meta: { revision: 'composition-v1', boardPlanModel, profiles: [{ key: 'c4', cost: 4, description: 'Level 8 first',
      ...(legacy ? {} : { level9FiveCostStar: 2 }) }],
      methodology: { evaluationModel: 'symmetric-reference-pool-v1' } },
    data: { revision: 'composition-v1', boardPlanModel, baselineRevision: 'champions-v1',
      methodology: { evaluationModel: 'symmetric-reference-pool-v1' }, results: { 9: { single: [parent] } } } };
  const champion = { mode: 'compositions', meta: { revision: 'champions-v1', units:
    [...parent.units, ...parent.level9Upgrade.board.units].map(unit => ({ ...unit, stars: [1, 2] })), traits: [] } };
  const h = { parent, composition, champion, createElement, element, items: [], details: [], scenarios: [], tooltips: new Map(), refreshes: 0,
    get focused() { return focused; }, card: null };
  const ctx = vm.createContext({ tcomp: composition, tstate: champion, tboard: { req: 0 },
    document: { createElement, createTextNode: textContent => ({ textContent }), getElementById: element,
      querySelectorAll: selector => h.card?.querySelectorAll(selector) || [] },
    tftText: (tag, text, className = '') => Object.assign(createElement(tag), { textContent: text, className }),
    tftIcon: () => createElement('span'), bindTftTooltip(anchor, contents) { h.tooltips.set(anchor, contents); },
    tftCompositionItem: (name, api, unit) => {
      h.items.push({ name, api, unit: unit.slug });
      return Object.assign(createElement('button'), { className: 'tft-comp-item', textContent: name });
    },
    tftCompositionDetailContents: row => { h.details.push(row); return createElement('div'); },
    tftRevision: () => champion.meta.revision,
    renderTftUnits() {}, renderTftStars() {}, renderTftMode() {}, hideTftTooltip() {}, updateHash() {}, scheduleTftStatus() {}, tftActive() {},
    loadTftScenario: () => { h.scenarios.push({ unit: champion.unit, star: champion.star, geo: champion.geo, traits: champion.traits, threat: champion.threat }); },
    loadTftCompositions: async () => { ++h.refreshes; h.card = ctx.tftCompositionCard(parent, 1); },
    setTftMode: mode => { champion.mode = mode; },
  });
  vm.runInContext(script.slice(script.indexOf('const tftCompositionKey'), script.indexOf('async function loadTftCompositionMeta'))
    + script.slice(script.indexOf('function tftCompositionRows('), script.indexOf('function tftCompositionItem('))
    + script.slice(script.indexOf('function tftUnitTraits('), script.indexOf('const tftTip'))
    + script.slice(script.indexOf('function tftTooltipHead('), script.indexOf('function tftChampionTooltip('))
    + script.slice(script.indexOf('function tftCompositionChampionTooltip('), script.indexOf('function tftCompositionMatchups('))
    + script.slice(script.indexOf('function tftCompositionItemEvidence('), script.indexOf('function tftCompositionDetailContents('))
    + script.slice(script.indexOf('function tftCompositionCard('), script.indexOf('async function loadTftCompositions('))
    + script.slice(script.indexOf('function openTftCompositionUnit('), script.indexOf('function openTftLeaderboardEntry(')), ctx);
  h.context = ctx;
  return h;
}

function checkChampionTooltips() {
  const h = domHarness(), parent = h.parent, cap = parent.level9Upgrade.board;
  const unit = parent.units[0], metadata = h.champion.meta.units.find(info => info.api === unit.api);
  Object.assign(metadata, { ability: 'Correct champion ability', role: 'Magic Marksman',
    traits: ['Shared trait'], traitApis: ['shared-trait'],
    traitBonuses: { high: [{ api: 'shared-trait', breakpoint: 6 }] } });
  h.champion.meta.units.unshift({ api: 'different-champion', slug: 'different-champion',
    name: 'Different champion', ability: 'Wrong ability', traits: [] });
  h.champion.meta.traits = [{ api: 'shared-trait', name: 'Shared trait', levels: [2, 4, 6] }];
  Object.assign(h.champion, { unit: 'different-champion', star: 3, traits: 'high' });
  Object.assign(cap.units[0], { dps: 987, healing: 123, allyHealing: 456 });
  const original = JSON.stringify([parent, h.champion.meta]);
  const tooltip = (entry, board) => {
    const card = h.context.tftCompositionUnit(entry, board);
    const head = card.querySelector('.tft-comp-unit-head');
    assert.equal(head.title, undefined, 'The browser title does not cover the rich tooltip');
    assert.equal(typeof head.events.click, 'function', 'Hover details preserve item-analysis navigation');
    assert(h.tooltips.has(head), 'Champion portraits and names use the shared hover/focus behavior');
    return h.tooltips.get(head)().map(node => node.textContent).join('\n');
  };
  const coreText = tooltip(unit, parent), capText = tooltip(cap.units[0], cap);
  assert(coreText.includes('2★ · 4 cost · Magic Marksman'));
  assert(coreText.includes('Correct champion ability') && !coreText.includes('Wrong ability'),
    'Details belong to the hovered champion, independent of the selected champion');
  assert(coreText.includes('Shared trait2 · 2 active') && !coreText.includes('6 active'),
    'Core trait activity comes from this board, not standalone trait controls');
  assert(coreText.includes('125 DPS') && coreText.includes('Items: Selected'));
  assert(capText.includes('Shared trait1 · inactive') && capText.includes('987 DPS'),
    'The same champion uses the cap\'s changed traits and contributions');
  assert(capText.includes('123 healing received') && capText.includes('456 ally healing provided'));
  const legendary = cap.units.at(-1);
  assert(tooltip(legendary, cap).includes('2★ · 5 cost'), 'Cap legends use their actual two-star board assumption');
  const elder = withUpgrade(levelBoard('hover-elder'), { elder: true }).level9Upgrade.board;
  assert(tooltip(elder.units.at(-1), elder).includes('2★ · 5 cost · 2 slots'));
  const legacy = domHarness({ legacy: true }), legacyCap = legacy.parent.level9Upgrade.board;
  const legacyTooltip = legacy.context.tftCompositionChampionTooltip(legacyCap.units.at(-1), legacyCap).map(node => node.textContent).join('\n');
  assert(legacyTooltip.includes('1★ · 5 cost'), 'Old published caps retain their own one-star tooltip during the handoff');
  const opponent = { ...roster[0], items: [], itemApis: [] };
  const details = h.context.tftCompositionOpponent([opponent]);
  details.open = true; details.events.toggle();
  const opponentHead = details.querySelector('.tft-comp-opponent-head');
  assert.equal(opponentHead.tabIndex, 0, 'Opponent details are keyboard accessible');
  const opponentText = h.tooltips.get(opponentHead)().map(node => node.textContent).join('\n');
  assert(opponentText.includes(opponent.name));
  assert(!opponentText.includes('ranking fights') && !opponentText.includes('Click to explore'),
    'Reference tooltips do not invent contribution data or a navigation action');
  assert.equal(h.scenarios.length, 0, 'Hovering does not request champion calculations');
  assert.equal(h.refreshes, 0, 'Hovering does not reload composition results');
  assert.equal(JSON.stringify([parent, h.champion.meta]), original, 'Tooltips leave saved results unchanged');
}

async function checkLevelRenderingAndNavigation() {
  const h = domHarness(), ctx = h.context, parent = h.parent;
  const snapshot = JSON.stringify(parent);
  const card = h.card = ctx.tftCompositionCard(parent, 1);
  assert.equal(card.dataset.level, 8);
  assert(card.textContent.includes('Level 8 core'));
  assert(card.textContent.includes('8 champions · 8 / 8 slots'));
  const upgrade = card.querySelector('.tft-comp-upgrade');
  assert.equal(upgrade.children[0].textContent, 'Optional level 9 upgrade');
  assert.equal(upgrade.open, false);
  assert.equal(upgrade.querySelector('.tft-comp-upgrade-body'), null, 'Collapsed upgrades create no hidden roster or fight diagnostics');
  assert.equal(card.querySelectorAll('.tft-comp-unit-head').length, 8);
  assert.equal(card.querySelector('.tft-comp-units').style.values['--tft-comp-columns'], 8);
  upgrade.open = true;
  upgrade.events.toggle();
  const cap = upgrade.querySelector('.tft-comp-upgrade-body');
  assert.equal(cap.dataset.level, 9);
  assert(cap.textContent.includes('9 champions · 9 / 9 slots · 2 existing items'));
  assert(cap.textContent.includes('Replace Ally 7 2★ → Legendary 0 2★ + Legendary 1 2★.'));
  assert(cap.textContent.includes('Support item: Ally 7 → Legendary 0'));
  assert(cap.textContent.includes('Level 8 results determine this composition’s rank.'));
  assert(cap.textContent.includes('Shared trait: 2 → 1 · active breakpoint 2 → none'));
  assert.equal(cap.querySelector('.tft-comp-units').style.values['--tft-comp-columns'], 9);
  assert.equal(cap.querySelectorAll('.tft-comp-unit-head').length, 9);
  assert.equal(cap.querySelectorAll('.tft-comp-card').length, 0, 'An optional cap is not a nested ranked composition card');
  assert.equal(ctx.tftCompositionItemEvidence(parent.level9Upgrade.board.units[0], parent.level9Upgrade.board), null,
    'A transition has no fabricated full item-replacement analysis');
  assert.equal(h.details.length, 0);
  const tests = cap.querySelector('.tft-comp-upgrade-tests');
  tests.open = true; tests.events.toggle(); tests.events.toggle();
  assert.deepEqual(h.details, [parent.level9Upgrade.board], 'Cap fight diagnostics remain lazy and use the cap board');
  upgrade.open = false; upgrade.events.toggle(); upgrade.open = true; upgrade.events.toggle();
  assert.equal(upgrade.querySelector('.tft-comp-upgrade-body'), cap, 'Reopening reuses the rendered cap');

  const legendary = parent.level9Upgrade.board.units.at(-1);
  const button = cap.querySelectorAll('.tft-comp-unit-head').find(head => head.dataset.unit === legendary.slug);
  button.events.click();
  assert.deepEqual(h.scenarios, [{ unit: legendary.slug, star: 2, geo: 'clump', traits: 'bare', threat: 'mixed' }],
    'The clicked cap champion opens its own two-star item analysis in the selected formation');
  assert.equal(h.composition.returnBoard, parent.id);
  assert.equal(h.composition.returnUpgrade, parent.level9Upgrade.board.id);
  assert.equal(h.composition.returnUnit, legendary.slug);
  assert.equal(h.composition.budget, 9);
  assert.equal(h.composition.structure, 'auto');
  assert.equal(h.refreshes, 0, 'Opening a champion does not recompute or reload the composition');

  vm.runInContext(script.slice(script.indexOf('async function setTftMode('), script.indexOf('function renderTftLeaderboardControls(')), ctx);
  await ctx.setTftMode('compositions', { focus: true });
  const returned = h.card.querySelector('.tft-comp-upgrade');
  assert.equal(returned.open, true, 'Returning from a cap restores its expanded roster');
  assert.equal(h.focused.dataset.unit, legendary.slug, 'Keyboard focus returns to the exact legendary');
  assert.equal(h.focused.dataset.composition, parent.level9Upgrade.board.id);
  assert.equal(JSON.stringify(parent), snapshot, 'Rendering and navigation never modify saved boards or item evidence');
  const calls = h.scenarios.length;
  ctx.openTftCompositionUnit({ ...legendary, star: 3 }, parent.level9Upgrade.board);
  assert.equal(h.scenarios.length, calls, 'Uncomputed three-star legendaries cannot navigate to a different scenario');
  ctx.openTftCompositionUnit(parent.units[0], parent);
  assert.equal(h.composition.returnUpgrade, null, 'Navigating from the level 8 core clears the cap return target');
  assert.equal(h.composition.returnBoard, parent.id);

  const elder = withUpgrade(levelBoard('elder-render'), { elder: true });
  const elderSection = ctx.tftCompositionUpgradeContents(elder);
  assert(elderSection.textContent.includes('8 champions · 9 / 9 slots'));
  assert(elderSection.textContent.includes('Elder Dragon 2★ (2 slots)'));
  const elderUnit = elderSection.querySelectorAll('.tft-comp-unit').find(unit => unit.dataset.unit === 'elder-dragon');
  assert(elderUnit.textContent.includes('2★ · 5 cost · 2 slots'));
  assert.equal(elderSection.querySelector('.tft-comp-units').style.values['--tft-comp-columns'], 8);
  h.champion.meta.units.push({ ...elder.level9Upgrade.board.units.at(-1), stars: [1, 2] });
  h.composition.data.results[9].single.push(elder);
  elderUnit.querySelector('.tft-comp-unit-head').events.click();
  assert.deepEqual(h.scenarios.at(-1), { unit: 'elder-dragon', star: 2, geo: 'clump', traits: 'bare', threat: 'mixed' },
    'Elder opens its own two-star item analysis while retaining both occupied slots on the board');
  const addOnly = ctx.tftCompositionUpgradeContents(withSingleUpgrade(levelBoard('add-only')));
  assert(addOnly.textContent.includes('Add Legendary 1 2★.'));
  assert(addOnly.textContent.includes('Keep every item on its current holder.'));
  assert(!addOnly.textContent.includes('Replace '), 'Adding the ninth champion is not described as selling anyone');

  ctx.renderTftCompositions();
  assert(h.element('tft-comp-level-note').textContent.includes('Level 8 cores'));
  assert(h.element('tft-comp-plan-note').textContent.includes('Four-cost cores contain no 5-cost champions'));
  assert(h.element('tft-comp-plan-note').textContent.includes('including Elder Dragon, are assumed to be 2★'));
  const older = domHarness({ legacy: true }), olderCap = older.parent.level9Upgrade.board;
  older.context.renderTftCompositions();
  assert(older.element('tft-comp-plan-note').textContent.includes('including Elder Dragon, are assumed to be 1★'),
    'Methodology follows the older published assumption until the new metadata arrives');
  const olderContents = older.context.tftCompositionUpgradeContents(older.parent);
  assert(olderContents.textContent.includes('Legendary 0 1★ + Legendary 1 1★'));
  const olderLegendary = olderCap.units.at(-1);
  olderContents.querySelectorAll('.tft-comp-unit-head').find(head => head.dataset.unit === olderLegendary.slug).events.click();
  assert.deepEqual(older.scenarios, [{ unit: olderLegendary.slug, star: 1, geo: 'clump', traits: 'bare', threat: 'mixed' }],
    'Legacy cap navigation still requests the one-star scenario that was actually calculated');
  delete h.composition.meta.boardPlanModel;
  delete h.composition.data.boardPlanModel;
  ctx.renderTftCompositions();
  assert.equal(h.element('tft-comp-level-note').textContent, '8 units · theoretical boards');
  assert(!h.element('tft-comp-plan-note').textContent.includes('contain no 5-cost'), 'Legacy saved results never receive the new no-legendary promise');
}

async function checkSavedCompositions() {
  const h = loadHarness();
  await h.load();
  assert.equal(h.calls.length, 1, 'The first visit loads and validates the saved artifact');
  assert.equal(h.calls[0].options.cache, undefined, 'Artifact requests allow the server ETag to revalidate cached bytes');
  assert.equal(h.state.error, null);
  const saved = h.state.data;
  const original = JSON.stringify(saved);
  h.state.shown = 9;
  h.state.structure = 'single';
  await h.load({ preserve: true });
  await h.load();
  assert.equal(h.calls.length, 1, 'Returning from a champion or reentering the same context reuses loaded data');
  assert.equal(h.state.data, saved);
  assert.equal(h.state.shown, 9, 'Reuse preserves expanded board counts');
  assert.equal(h.state.structure, 'single', 'Reuse preserves composition controls');
  assert.equal(JSON.stringify(saved), original, 'Reuse leaves results and item evidence untouched');
  assert.equal(h.state.loading, false);
  assert.equal(h.polls, 3, 'Reusing data still schedules checks for a new published revision');
  await h.retry();
  assert.equal(h.calls.length, 2, 'The explicit retry button forces a fresh request');
  await h.load({ preserve: true, force: true });
  assert.equal(h.calls.length, 3, 'Callers can explicitly refresh an already loaded context');
  h.state.error = 'Temporary update failure';
  await h.load({ preserve: true });
  assert.equal(h.calls.length, 4, 'A prior load error is retried even when saved data remains');
  h.state.pending = true;
  await h.load({ preserve: true });
  assert.equal(h.calls.length, 5, 'Pending results are retried instead of hidden by reuse');

  for (const [label, change] of [
    ['composition revision', h => { h.state.meta.revision = 'composition-v2'; }],
    ['champion revision', h => { h.baseline = 'champions-v2'; h.state.meta.baselineRevision = h.baseline; }],
    ['profile', h => { h.state.profile = 'c3'; }],
    ['formation', h => { h.state.geo = 'clump'; }],
    ['opponent hash', h => { h.state.meta.opponentPool.hash = 'pool-v2'; }],
    ['opponent version', h => { h.state.meta.opponentPool.version = 2; }],
  ]) {
    const next = loadHarness();
    await next.load();
    change(next);
    await next.load({ preserve: true });
    assert.equal(next.calls.length, 2, `A changed ${label} must fetch matching results`);
    assert.equal(next.state.error, null, `Fresh results for changed ${label} remain valid`);
  }

  const unvalidated = loadHarness();
  unvalidated.state.data = unvalidated.payload();
  unvalidated.state.key = unvalidated.state.data.key;
  await unvalidated.load({ preserve: true });
  assert.equal(unvalidated.calls.length, 1, 'Data never accepted by the response validator cannot be reused');

  for (const [label, corrupt] of [
    ['baseline revision', d => { d.baselineRevision = 'old-champions'; }],
    ['opponent pool', d => { d.opponentPool.hash = 'old-pool'; }],
    ['evaluation model', d => { d.methodology.evaluationModel = 'old-model'; }],
    ['item evidence', d => { d.results[9].single[0].itemAnalysis.evaluatedOn = 'validation'; }],
  ]) {
    const invalid = loadHarness();
    invalid.response = () => { const d = invalid.payload(); corrupt(d); return d; };
    await invalid.load();
    assert.equal(invalid.state.data, null, `Wrong ${label} is rejected before caching`);
    assert(invalid.state.error, `Wrong ${label} exposes a load error`);
    invalid.response = null;
    await invalid.load({ preserve: true });
    assert.equal(invalid.calls.length, 2, `Wrong ${label} cannot suppress a corrective request`);
    assert.equal(invalid.state.error, null);
  }

  const pending = loadHarness();
  pending.response = () => ({ pending: true, revision: pending.state.meta.revision });
  await pending.load();
  assert.equal(pending.state.pending, true);
  assert.equal(pending.state.data, null);
  pending.response = null;
  await pending.load({ preserve: true });
  assert.equal(pending.calls.length, 2, 'An initially missing artifact loads when publication finishes');
  assert.equal(pending.state.pending, false);

  const racing = loadHarness();
  await racing.load();
  const prior = racing.state.data;
  let finish, begin;
  const started = new Promise(resolve => { begin = resolve; });
  racing.response = () => new Promise(resolve => { finish = resolve; begin(); });
  const oldRequest = racing.load({ preserve: true, force: true });
  await started;
  await racing.load({ preserve: true });
  const late = racing.payload();
  late.results[9].single[0].id = 'late-response';
  finish(late);
  await oldRequest;
  assert.equal(racing.state.data, prior, 'A late aborted request cannot replace the reused current result');

  const changed = loadHarness();
  changed.response = () => ({ ...changed.payload(), revision: 'composition-v2' });
  changed.metaRefresh = () => { changed.state.meta.revision = 'composition-v2'; };
  await changed.load();
  assert.equal(changed.state.error, null, 'An in-flight publication still refreshes metadata before acceptance');
  await changed.load({ preserve: true });
  assert.equal(changed.calls.length, 1, 'Only the newly validated publication is reused');
}

function checkLazyDetails() {
  const createElement = tag => ({ tag, dataset: {}, children: [], events: {}, open: false,
    append(...children) { this.children.push(...children); },
    addEventListener(event, listener) { this.events[event] = listener; },
  });
  let built = 0;
  const composition = board('lazy');
  const contents = createElement('div');
  const ctx = vm.createContext({ document: { createElement },
    tftText: (tag, text) => Object.assign(createElement(tag), { text }),
    tftCompositionDetailContents: value => {
      assert.equal(value, composition, 'Lazy details use the selected board');
      ++built;
      return contents;
    },
  });
  vm.runInContext(script.slice(script.indexOf('function tftCompositionDetails('),
    script.indexOf('function tftCompositionDetailContents(')), ctx);
  const details = ctx.tftCompositionDetails(composition);
  assert.equal(built, 0, 'Collapsed cards do not construct hidden fights or item details');
  assert.equal(details.children[0].tag, 'summary');
  assert.equal(details.children[0].text, 'Fights, items and board details');
  details.events.toggle();
  assert.equal(built, 0, 'A closed toggle leaves details deferred');
  details.open = true;
  details.events.toggle();
  assert.equal(built, 1, 'Opening the card creates its original detail contents');
  assert.equal(details.children[1], contents);
  details.open = false;
  details.events.toggle();
  details.open = true;
  details.events.toggle();
  assert.equal(built, 1, 'Reopening does not duplicate or reconstruct details');
  assert.equal(details.children.length, 2);
}

function checkRefreshMessages() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, { dataset: {}, textContent: '',
      setAttribute() {}, replaceChildren() {} });
    return elements.get(id);
  };
  const champion = { meta: { patch: '18.1d' }, req: 1, status: { warmer: 'idle', refresh: {} } };
  const composition = { meta: null, data: null, profile: 'c2', geo: 'spread',
    budget: 9, shown: 3, pending: true, loading: false, status: null };
  const ctx = vm.createContext({ tstate: champion, tcomp: composition,
    document: { getElementById: element }, tftActive() {}, hideTftTooltip() {},
    tftCompositionRanks: () => [], clearTftCores() {}, scheduleTftStatus() {},
  });
  vm.runInContext(script.slice(script.indexOf('function refreshMessage('), script.indexOf('const tftRevision ='))
    + script.slice(script.indexOf('function renderTftCompositions('), script.indexOf('async function loadTftCompositions('))
    + script.slice(script.indexOf('function showTftPending('), script.indexOf('function tftHeldTime(')), ctx);
  for (const [phase, title] of [
    ['warming-builds', 'Updating saved TFT results for patch 18.2…'],
    ['warming-compositions', 'Updating saved TFT results for patch 18.2…'],
    ['preparing-responses', 'Preparing saved TFT results for display…'],
    ['publishing', 'Publishing saved TFT results…'],
    ['fetching', 'Checking for patch updates…'],
  ]) {
    champion.status.refresh = { status: 'running', phase, targetPatch: '18.2' };
    ctx.renderTftRefresh();
    assert.equal(element('tft-refresh-title').textContent, title, `Refresh phase ${phase} has an accurate title`);
    assert(element('tft-refresh-detail').textContent.includes('Saved TFT results for patch 18.1d remain available.'));
    assert(element('tft-refresh-detail').textContent.includes('Champion builds and compositions update together'),
      'Refresh messaging includes both published analyses');
  }
  champion.status.refresh = { status: 'ok' };
  ctx.showTftPending(champion.req);
  ctx.renderTftCompositions();
  assert.equal(element('tft-result-summary').textContent, 'Saved builds will appear after a completed scheduled refresh.');
  assert.equal(element('tft-comp-status').textContent, 'Saved compositions will appear after a completed scheduled refresh.');
  champion.status.refresh = { status: 'running', phase: 'preparing-responses' };
  ctx.showTftPending(champion.req);
  ctx.renderTftCompositions();
  assert(element('tft-result-summary').textContent.startsWith('A scheduled TFT refresh is running.'));
  assert(element('tft-comp-status').textContent.startsWith('A scheduled TFT refresh is running.'),
    'A running refresh is recognized even while the published warmer status is idle');
  champion.status.refresh = { status: 'failed' };
  ctx.showTftPending(champion.req);
  ctx.renderTftCompositions();
  assert(element('tft-result-summary').textContent.startsWith('The last TFT refresh did not finish.'));
  assert(element('tft-comp-status').textContent.startsWith('The last TFT refresh did not finish.'));
  composition.status = { warmer: 'idle', refresh: { status: 'running' } };
  ctx.renderTftCompositions();
  assert(element('tft-comp-status').textContent.startsWith('A scheduled TFT refresh is running.'),
    'The composition status response supplies its current scheduled refresh state');
}

function publicationHarness(publication = 'site-v1') {
  const h = loadHarness(), ctx = h.context;
  h.state.metaReq = 0;
  h.state.metaLoading = null;
  h.champion = { meta: { revision: h.baseline, patch: '18.1d' }, mode: 'compositions', req: 1,
    data: { unit: 'cached' }, dataRevision: h.baseline };
  Object.assign(h.state.meta, { boardSize: 8, profiles: [{ key: 'c2', cost: 2 }],
    items: [{ api: 'item', icon: 'old-icon' }] });
  Object.assign(h.state.meta.opponentPool, { searchBoards: 6, validationBoards: 3, initiatives: [0, 1] });
  if (publication) {
    h.champion.meta.publicationRevision = publication;
    h.state.meta.publicationRevision = publication;
  }
  h.serverChampion = structuredClone(h.champion.meta);
  h.serverComposition = structuredClone(h.state.meta);
  h.controlsRendered = 0;
  h.scenariosLoaded = 0;
  h.boardsLoaded = 0;
  Object.assign(ctx, {
    state: { view: 'tft' }, tstate: h.champion, tboard: { data: { revision: h.baseline } },
    clearTimeout() {}, renderTftRefresh() {},
    renderTftCompositionControls() { ++h.controlsRendered; },
    loadTftMeta: async () => {
      h.champion.meta = await ctx.api('api/tft/meta.json', { cache: 'no-store' });
      h.baseline = h.champion.meta.revision;
      return true;
    },
    loadTftStatus: async () => {
      h.champion.status = await ctx.api('api/tft/status.json', { cache: 'no-store' });
      return h.champion.status;
    },
    loadTftScenario: async () => { ++h.scenariosLoaded; h.champion.dataRevision = h.champion.meta.revision; },
    loadTftLeaderboard: async () => { ++h.boardsLoaded; },
  });
  ctx.document.hidden = false;
  ctx.api = async (path, options) => {
    h.calls.push({ path, options });
    if (path === 'api/tft/status.json') return structuredClone(h.serverChampion);
    if (path === 'api/tft/meta.json') return structuredClone(h.serverChampion);
    if (path === 'api/tft/compositions/status.json') return {
      revision: h.serverComposition.revision, publicationRevision: h.serverComposition.publicationRevision };
    if (path === 'api/tft/compositions/meta.json') return h.metadataResponse
      ? h.metadataResponse() : structuredClone(h.serverComposition);
    return { ...h.payload(), revision: h.serverComposition.revision,
      baselineRevision: h.serverComposition.baselineRevision, opponentPool: structuredClone(h.serverComposition.opponentPool) };
  };
  vm.runInContext(script.slice(script.indexOf('const tftRevision ='), script.indexOf('function scheduleTftStatus('))
    + script.slice(script.indexOf('async function loadTftCompositionMeta('), script.indexOf('function renderTftCompositionControls('))
    + script.slice(script.indexOf('async function pollTftCompositions('), script.indexOf('document.getElementById("tft-comp-budget").addEventListener'))
    + script.slice(script.indexOf('async function pollTftStatus('), script.indexOf('// Mark unavailable builds')), ctx);
  h.poll = () => ctx.pollTftStatus();
  h.count = path => h.calls.filter(call => call.path === path).length;
  h.largeRequests = () => h.calls.filter(call => /^api\/tft\/compositions\/c\d-/.test(call.path)).length;
  h.publish = revision => {
    h.serverChampion.publicationRevision = revision;
    h.serverComposition.publicationRevision = revision;
  };
  return h;
}

async function checkPublicationRefresh() {
  const h = publicationHarness();
  await h.load();
  const saved = h.state.data;
  h.publish('site-v2');
  h.serverComposition.items[0].icon = 'new-icon';
  await h.poll();
  assert.equal(h.count('api/tft/meta.json'), 1, 'A new publication refreshes champion metadata even when calculations match');
  assert.equal(h.count('api/tft/compositions/meta.json'), 1, 'A new publication refreshes composition metadata');
  assert.equal(h.state.meta.items[0].icon, 'new-icon');
  assert.equal(h.controlsRendered, 1, 'New presentation metadata rerenders controls, icons and notes');
  assert.equal(h.largeRequests(), 1, 'A presentation-only publication does not redownload the large composition artifact');
  assert.equal(h.state.data, saved, 'A validated result remains reusable across presentation publications');
  await h.poll();
  assert.equal(h.count('api/tft/meta.json'), 1, 'An unchanged publication does not repeatedly load metadata');
  assert.equal(h.count('api/tft/compositions/meta.json'), 1);

  h.publish('site-v3');
  h.serverComposition.revision = 'composition-v2';
  await h.poll();
  assert.equal(h.largeRequests(), 2, 'Changed composition calculations still fetch new results');
  assert.equal(h.state.data.revision, 'composition-v2');
  assert.notEqual(h.state.data, saved);
  h.publish('site-v4');
  h.serverChampion.revision = 'champions-v2';
  h.serverComposition.baselineRevision = 'champions-v2';
  h.serverComposition.revision = 'composition-v3';
  await h.poll();
  assert.equal(h.largeRequests(), 3, 'Changed baseline calculations also invalidate saved composition data');
  assert.equal(h.state.data.baselineRevision, 'champions-v2');
  h.publish('site-v5');
  h.serverComposition.opponentPool.hash = 'different-pool';
  await h.poll();
  assert.equal(h.largeRequests(), 4, 'Metadata publication cannot bypass the existing opponent-pool reuse guard');

  const legacy = publicationHarness(null);
  await legacy.load();
  await legacy.poll();
  assert.equal(legacy.count('api/tft/meta.json'), 0, 'Legacy status and metadata need no publication field');
  assert.equal(legacy.largeRequests(), 1);
  legacy.publish('first-site-revision');
  await legacy.poll();
  assert.equal(legacy.count('api/tft/meta.json'), 1, 'The first publication marker upgrades a legacy page');
  assert.equal(legacy.count('api/tft/compositions/meta.json'), 1);
  assert.equal(legacy.largeRequests(), 1);
  delete legacy.serverChampion.publicationRevision;
  delete legacy.serverComposition.publicationRevision;
  await legacy.poll();
  assert.equal(legacy.count('api/tft/meta.json'), 1, 'A legacy response does not cause a metadata reload loop');

  const inactive = publicationHarness();
  await inactive.load();
  inactive.champion.mode = 'builds';
  inactive.publish('site-v2');
  await inactive.poll();
  assert.equal(inactive.scenariosLoaded, 1, 'Champion notes and icons refresh for a presentation-only publication');
  assert.equal(inactive.count('api/tft/compositions/meta.json'), 0, 'Inactive composition metadata can wait until its next visit');
  inactive.champion.mode = 'compositions';
  await inactive.load({ preserve: true });
  assert.equal(inactive.count('api/tft/compositions/meta.json'), 1, 'Returning to compositions refreshes metadata cached before the new publication');
  assert.equal(inactive.largeRequests(), 1, 'Returning from a newly presented champion still reuses matching composition calculations');

  const race = publicationHarness();
  await race.load();
  let finish, begin;
  const started = new Promise(resolve => { begin = resolve; });
  race.publish('site-v2');
  const olderMeta = structuredClone(race.serverComposition);
  race.metadataResponse = () => new Promise(resolve => { finish = resolve; begin(); });
  const older = race.context.loadTftCompositionMeta(true);
  await started;
  race.metadataResponse = null;
  race.publish('site-v3');
  await race.context.loadTftCompositionMeta(true);
  finish(olderMeta);
  await older;
  assert.equal(race.state.meta.publicationRevision, 'site-v3', 'Late metadata cannot replace a newer publication');
  assert.equal(race.champion.meta.publicationRevision, 'site-v3', 'Both metadata domains share the accepted publication');
  await race.load({ preserve: true });
  assert.equal(race.largeRequests(), 1, 'Metadata request races do not invalidate unchanged verified calculations');
}

checkLevelPlans();
checkLazyDetails();
checkRefreshMessages();
checkChampionTooltips();
assert(html.includes('Loading saved compositions…'), 'Loading accurately describes reading precomputed results');
assert(!html.includes('Calculating the comparison…'), 'A fetch is not presented as a fresh simulation');
checkSavedCompositions().then(checkPublicationRefresh).then(checkSavedLevelPlans).then(checkLevelRenderingAndNavigation).then(() => {
  console.log('Saved composition UI checks passed: validated reuse, explicit retry, revision and publication guards, request races, deferred details and scheduled publication messages.');
  console.log('Composition level-plan UI checks passed: both teams\' slot occupancy, level 8 ranking, valid level 9 transitions, conserved items, changed traits, lazy cards and exact champion navigation.');
  console.log('Composition champion tooltip checks passed: exact identity and stars, board-specific traits and contributions, opponent details, and saved-data reuse.');
}).catch(error => { console.error(error); process.exitCode = 1; });
