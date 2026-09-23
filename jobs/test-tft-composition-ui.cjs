// Run with node jobs/test-tft-composition-ui.cjs. No browser or npm packages required.
// Optional generated-data checks: --theory-fixture fixture.json (contains meta/payload)
// or --theory-meta meta.json --theory-payload context.json [--theory-payload another.json].
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
const saturated = board('saturated', { wins: 12 });
assert.equal(context.tftCompositionItemVerdict(saturated.itemAnalysis.holders[0].items[0], saturated),
  'Inconclusive: every legal replacement wins the same matchups. These tests provide no preference for this item.');
assert.equal(context.tftCompositionEvidenceSummary(saturated),
  'Benchmark ceiling reached; a perfect score does not establish a best build. 1 of 1 item slots have equally scoring alternatives.');
assert(!context.tftCompositionEvidenceSummary(tiedA).includes('ceiling'), 'Partial wins do not imply saturation');
{
  const previousMeta = tcomp.meta;
  const positioned = result(6, 12, 'search');
  positioned.opponentCount = 2;
  positioned.laneOffsets = [0, 2, 4];
  positioned.matchups.forEach((matchup, i) => Object.assign(matchup, {
    opponentId: `search-${Math.floor(i / 6)}`, laneOffset: positioned.laneOffsets[Math.floor(i / 2) % 3],
  }));
  tcomp.meta = { opponentPool: { laneOffsets: [0, 2, 4], searchBoards: 2 } };
  assert.equal(context.tftCompositionResultValid(positioned, 'search'), true,
    'Distinct positions of one opponent are valid paired fights, not extra independent boards');
  for (const mutate of [
    row => { row.matchups[0].laneOffset = 2; },
    row => { row.matchups[0].initiative = 1; },
    row => { row.laneOffsets = [0]; },
    row => { row.laneOffsets = [0, 2, 2]; },
    row => { row.matchups[0].laneOffset = 6; },
  ]) {
    const invalid = structuredClone(positioned); mutate(invalid);
    assert.equal(context.tftCompositionResultValid(invalid, 'search'), false,
      'Missing or duplicate initiative/position pairs cannot masquerade as a full evaluation');
  }
  tcomp.meta.opponentPool.searchBoards = 12;
  assert.equal(context.tftCompositionResultValid(positioned, 'search'), false,
    'A partial opponent pool cannot publish a complete-looking ranking');
  tcomp.meta = previousMeta;
}
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
    append(...children) { children.forEach(child => { child.parent = this; }); this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(event, listener) { this.events[event] = listener; },
    focus() { focused = this; }, scrollIntoView() {},
    select() { this.selection = [0, this.value.length]; },
    setSelectionRange(start, end) { this.selection = [start, end]; },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); },
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
      body: createElement('body'), get activeElement() { return focused; },
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

function checkTeamCodes() {
  const planner = JSON.parse(fs.readFileSync(require('node:path').join(__dirname, '../data/tft/set18/team-planner.json'), 'utf8'));
  const members = apis => ({ units: apis.map(api => ({ api })) });
  const board = members(['TFT18_Ivern', 'TFT18_Lux_Base', 'TFT18_ElderDragon']);
  const code = context.tftCompositionTeamCode(board, planner);
  assert.equal(code, '024044053fc000000000000000000000TFTSet18');
  assert.equal(code.length, 40);
  assert.deepEqual(code.slice(2, 32).match(/.{3}/g), ['404', '405', '3fc', ...Array(7).fill('000')]);
  const equipped = structuredClone(board);
  equipped.units.forEach(unit => Object.assign(unit, { star: 2, items: ['Warmogs Armor'], form: 'AD' }));
  equipped.units[2].slotCost = 2;
  equipped.alphaHolder = 'TFT18_ElderDragon';
  assert.equal(context.tftCompositionTeamCode(equipped, planner), code, 'Elder has one roster entry; items, stars, forms and Alpha are not encoded');
  const apis = Object.keys(planner.unitCodes);
  const full = context.tftCompositionTeamCode(members(apis.slice(0, 10)), planner);
  assert.equal(full, '02' + apis.slice(0, 10).map(api => planner.unitCodes[api]).join('') + 'TFTSet18');
  for (const units of [[], [null], [{ api: 'missing' }], [board.units[0], board.units[0]], members(apis.slice(0, 11)).units]) {
    assert.throws(() => context.tftCompositionTeamCode({ units }, planner), undefined, 'Never export an incomplete or truncated roster');
  }
  for (const bad of ['000', '1234', 'not-code', '404', null, 1043]) {
    const wrong = structuredClone(planner);
    wrong.unitCodes.TFT18_Lux_Base = bad;
    assert.throws(() => context.tftCompositionTeamCode(board, wrong));
  }
  assert.throws(() => context.tftCompositionTeamCode(board, null));
}

function checkAntihealSources() {
  const h = domHarness(), ctx = h.context;
  ctx.tftCoreItems = names => Object.assign(h.createElement('div'), { textContent: names.join(' · ') });
  ctx.tftCompositionMatchups = () => h.createElement('div');
  vm.runInContext(script.slice(script.indexOf('function tftCompositionDetailContents('),
    script.indexOf('function tftCompositionCard(')), ctx);
  const core = h.parent, cap = core.level9Upgrade.board;
  const unusualSource = '<img src=x onerror=alert(1)>', unusualHolder = '<script>holder</script>';
  core.units[1].name = unusualHolder;
  core.antihealSources = [
    { type: 'item', api: 'DA_RedBuff', unitApi: core.units[0].api, name: 'Red Buff' },
    { type: 'trait', api: 'DA_Inferno18', name: 'Inferno (2)' },
    { type: 'ability', api: 'source-ability', unitApi: core.units[1].api, name: unusualSource },
  ];
  cap.antihealSources = [{ type: 'item', api: 'DA_Morellonomicon', unitApi: cap.units.at(-1).api, name: 'Morellonomicon' }];
  const before = JSON.stringify(core);
  const details = ctx.tftCompositionDetailContents(core), source = details.querySelector('.tft-comp-antiheal');
  assert(source.textContent.includes(`Red Buff on ${core.units[0].name}`));
  assert(source.textContent.includes('Inferno (2) (trait)'), 'A team trait does not need a holder');
  assert(source.textContent.includes(`${unusualSource} on ${unusualHolder}`), 'Source and holder names render as literal text');
  assert(!source.textContent.includes('DA_RedBuff'), 'Internal source IDs are not substituted for readable names');
  const nodes = node => [node, ...(node.children || []).flatMap(nodes)];
  assert(nodes(source).every(node => !Object.hasOwn(node, 'innerHTML') && !['img', 'script'].includes(node.tag)),
    'Markup-like source names never create HTML or scripts');
  const upgrade = ctx.tftCompositionUpgradeContents(core), evidence = upgrade.querySelector('.tft-comp-upgrade-tests');
  evidence.open = true; evidence.events.toggle();
  const capSource = evidence.querySelector('.tft-comp-antiheal');
  assert.equal(capSource.textContent, `Morellonomicon on ${cap.units.at(-1).name}`);
  assert(!capSource.textContent.includes('Red Buff'), 'Level-nine details use the cap’s source, not the core’s source');
  assert.equal(JSON.stringify(core), before, 'Source presentation does not modify the board, scores or upgrade');
  const old = structuredClone(core); delete old.antihealSources;
  assert.equal(ctx.tftCompositionDetailContents(old).querySelector('.tft-comp-antiheal'), null,
    'Older payloads receive no invented antiheal promise');
  old.antihealSources = [];
  assert.equal(ctx.tftCompositionDetailContents(old).querySelector('.tft-comp-antiheal'), null);

  for (const description of ['Level 8: same-cost main carry and tank. Antiheal required.',
    'At most one melee carry at level 8; optional level 9 upgrades allow two.']) {
    h.composition.meta.profiles[0].description = description;
    ctx.renderTftCompositions();
    assert.equal(h.element('tft-comp-profile-hint').textContent, description,
      'Display the declared description for both unrestricted new results and an older pinned generation');
  }
}

async function checkCompositionClipboard() {
  const ready = ({ elder = false } = {}) => {
    const h = domHarness();
    if (elder) withUpgrade(h.parent, { elder: true });
    const apis = [...new Set([...h.parent.units, ...h.parent.level9Upgrade.board.units].map(unit => unit.api))];
    h.composition.meta.teamPlanner = { set: 18, slots: 10, format: 'tft-team-planner-v2',
      unitCodes: Object.fromEntries(apis.map((api, i) => [api, (0x100 + i).toString(16)])) };
    h.card = h.context.tftCompositionCard(h.parent, 1);
    h.core = h.card.querySelector('.tft-comp-copy');
    h.expected = board => '02' + board.units.map(unit => h.composition.meta.teamPlanner.unitCodes[unit.api]).join('').padEnd(30, '0') + 'TFTSet18';
    return h;
  };
  const h = ready();
  const writes = [];
  let finish;
  h.context.navigator = { clipboard: { writeText: code => { writes.push(code); return new Promise(resolve => { finish = resolve; }); } } };
  const button = h.core.children[0], status = h.core.querySelector('.tft-comp-copy-status');
  const copying = button.events.click();
  assert.equal(button.disabled, true);
  assert(!status.textContent.includes('Copied'), 'Do not report success before clipboard write succeeds');
  await button.events.click();
  assert.equal(writes.length, 1, 'An in-flight copy cannot be submitted twice');
  finish();
  await copying;
  assert.equal(button.disabled, false);
  assert.equal(writes[0], h.expected(h.parent));
  assert(status.textContent.includes('Copied!'));
  assert.equal(status.attributes['aria-live'], 'polite');
  assert(button.title.includes('items, stars, positions and Alpha marks'));
  assert(button.attributes['aria-label'].includes('level 8'));

  // The upgraded board gets its own control/code, including Elder once.
  for (const elder of [false, true]) {
    const cap = ready({ elder }), copied = [];
    cap.context.navigator = { clipboard: { writeText: async code => { copied.push(code); } } };
    const upgrade = cap.card.querySelector('.tft-comp-upgrade');
    assert.equal(upgrade.querySelector('.tft-comp-copy'), null, 'Copy control follows lazy cap rendering');
    upgrade.open = true;
    upgrade.events.toggle();
    const control = upgrade.querySelector('.tft-comp-copy');
    assert(control.children[0].attributes['aria-label'].includes('level 9'));
    await control.children[0].events.click();
    assert.equal(copied[0], cap.expected(cap.parent.level9Upgrade.board));
    assert.notEqual(copied[0], cap.expected(cap.parent));
    assert.equal(cap.core.querySelector('.tft-comp-copy-status').textContent, '', 'Cap copy leaves the core feedback alone');
  }

  for (const modernDenied of [false, true]) {
    const fallback = ready();
    if (modernDenied) fallback.context.navigator = { clipboard: { writeText: async () => { throw new Error('permission denied'); } } };
    const trigger = fallback.core.children[0];
    let attempts = 0;
    fallback.context.document.execCommand = command => {
      ++attempts;
      assert.equal(command, 'copy');
      const field = fallback.context.document.body.children[0];
      assert.equal(field.tag, 'textarea');
      assert.equal(field.value, fallback.expected(fallback.parent));
      assert.deepEqual(Array.from(field.selection), [0, field.value.length]);
      return true;
    };
    trigger.focus();
    await trigger.events.click();
    assert.equal(attempts, 1);
    assert.equal(fallback.context.document.body.children.length, 0, 'Temporary clipboard textarea is removed');
    assert.equal(fallback.focused, trigger, 'Clipboard fallback restores focus');
    assert(fallback.core.querySelector('.tft-comp-copy-status').textContent.includes('Copied!'));
  }

  const blocked = ready();
  blocked.context.document.execCommand = () => false;
  await blocked.core.children[0].events.click();
  const manual = blocked.core.querySelector('.tft-comp-copy-code');
  assert.equal(manual.value, blocked.expected(blocked.parent));
  assert.equal(manual.readOnly, true);
  assert.equal(blocked.focused, manual);
  assert.deepEqual(Array.from(manual.selection), [0, manual.value.length]);
  assert(blocked.core.querySelector('.tft-comp-copy-status').textContent.includes('Copy blocked'));
  assert.equal(blocked.context.document.body.children.length, 0);
  blocked.context.navigator = { clipboard: { writeText: async () => {} } };
  await blocked.core.children[0].events.click();
  assert.equal(blocked.core.querySelector('.tft-comp-copy-code'), null, 'A successful retry removes the manual fallback');
  assert(blocked.core.querySelector('.tft-comp-copy-status').textContent.includes('Copied!'));

  const missing = domHarness();
  const unavailable = missing.context.tftCompositionCard(missing.parent, 1).querySelector('.tft-comp-copy');
  assert.equal(unavailable.children[0].disabled, true);
  assert(unavailable.children[0].title.includes('unavailable'));
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
  const transformed = { ...unit, form: 'AD', kind: 'Assassin', frontline: true, abilityName: "Prowler's Pounce" };
  const transformedText = tooltip(transformed, parent);
  assert(transformedText.includes('AD form · Assassin') && transformedText.includes("Ability: Prowler's Pounce"));
  assert(!transformedText.includes('Correct champion ability'), 'The equipped form supplies its actual role and ability');
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
    tftCompositionTheory: () => false,
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

function checkTraitCoverage() {
  const h = domHarness(), ctx = h.context;
  const base = { api: 'trait', name: 'Trait', active: true, count: 2, breakpoint: 2, modeled: true };
  for (const [coverage, status] of [['supported', 'Supported scoring effects'],
    ['partial', 'Partial scoring coverage'], ['structural', 'Board slots or trait counts modeled'],
    ['economy', 'Economy effect; no direct combat score'], ['unmodeled', 'Combat effect not modeled']]) {
    const note = 'The six-plant condition is not included.';
    const list = ctx.tftCompositionTraits([{ ...base, coverage, coverageNotes: [note] }]);
    const chip = list.querySelector('.tft-comp-trait');
    assert.equal(chip.dataset.coverage, coverage);
    assert(chip.attributes['aria-label'].includes(status), `${coverage}: coverage is announced accessibly`);
    assert(chip.attributes['aria-label'].includes(note), `${coverage}: applicable omission is announced`);
    const tooltip = h.tooltips.get(chip)().map(node => node.textContent).join('\n');
    assert(tooltip.includes(note), `${coverage}: tooltip exposes the actual missing condition`);
    if (coverage === 'partial') {
      assert(chip.textContent.includes('Partial'), 'Partial traits are visible on the board without hovering');
      assert(tooltip.includes('partial scoring coverage'));
      assert(!tooltip.includes('active breakpoint contributes to the calculations'), 'Partial support is not described as complete scoring coverage');
    }
    if (coverage === 'economy') assert(tooltip.includes('no direct combat score'));
    if (coverage === 'structural') assert(tooltip.includes('board slots or trait counts'));
  }
  for (const modeled of [true, false]) {
    const list = ctx.tftCompositionTraits([{ ...base, modeled }]);
    const chip = list.querySelector('.tft-comp-trait');
    assert.equal(chip.dataset.coverage, modeled ? 'supported' : 'unmodeled', 'Older artifacts preserve their existing modeled flag');
    const tooltip = h.tooltips.get(chip)().map(node => node.textContent).join('\n');
    assert(tooltip.includes(modeled ? 'active breakpoint contributes' : 'combat effect is not included'));
  }
  assert.equal(ctx.tftCompositionTraits([{ ...base, active: false, coverage: 'partial' }]).children.length, 0,
    'Inactive traits are still excluded from active board chips');
}

const theoryModel = 'ehp-damage-capacity-v2';
const theoryRevision = 'theory-fixture-v1';
const theoryScenarios = [
  { key: 'mixed-low', label: 'Mixed, lower pressure · no enemy control', incomingDps: 1000, physicalShare: .5, armor: 100, mr: 100, wound: 0,
    controlInterval: 8, controlDuration: 0, pressureInterval: .5, pressureAllocation: 'shared-live-frontline', incomingSourceCount: 3 },
  { key: 'physical-high', label: 'Physical, higher pressure · 1.5s frontline stun every 8s', incomingDps: 4000, physicalShare: .8, armor: 150, mr: 100, wound: .33,
    controlInterval: 8, controlDuration: 1.5, pressureInterval: .5, pressureAllocation: 'shared-live-frontline', incomingSourceCount: 3 },
];

function asTheory(row, score = 12000000) {
  for (const key of ['poolRevision', 'poolSplit', 'opponentCount', 'matchups', 'validation', 'assumptionCheck', 'laneOffsets']) delete row[key];
  Object.assign(row, { evaluationModel: theoryModel, modelRevision: theoryRevision, profileCount: theoryScenarios.length,
    metrics: { theoryScore: score, frontlineEhp: score / 1000, damageDps: 1000, damageCapacity: score / 2000, protectionTime: score / 2000000 },
    scenarios: theoryScenarios.map(scenario => ({ ...scenario, frontlineEhp: score / 1000, damageDps: 1000,
      targetHp: 3000, targetCount: 3, measurementWindow: .8 * score / 1000 / scenario.incomingDps,
      plannedMeasurementWindow: .8 * score / 1000 / scenario.incomingDps, frontlineCollapsed: false,
      incomingBudget: .8 * score / 1000, spentPressure: .7 * score / 1000,
      deniedPressure: .1 * score / 1000, unspentPressure: 0,
      protectionTime: score / 1000 / scenario.incomingDps, damageCapacity: score / scenario.incomingDps, score })) });
  row.units.forEach(unit => { unit.ehp = score / 4000; unit.damage = score / 2000 / row.units.length; unit.measuredDps = 120; });
  if (row.itemAnalysis) row.itemAnalysis = { model: 'theory-item-replacements-v1', evaluatedOn: 'theory', modelRevision: theoryRevision,
    baselineScore: score, holders: [{ api: row.units[0].api, slug: row.units[0].slug, name: row.units[0].name,
      items: [{ slot: 0, itemApi: 'selected', item: 'Selected', testedAlternatives: 1, equivalentAlternatives: 0,
        bestScoreDelta: -.02 * score, alternatives: [{ itemApi: 'alternative', item: 'Alternative', score: .98 * score,
          scoreDelta: -.02 * score, pctDelta: score ? -2 : null, improvedScenarios: [], degradedScenarios: score ? theoryScenarios.map(scenario => scenario.key) : [] }] }] }] };
  if (score === 0 && row.itemAnalysis) row.itemAnalysis.holders[0].items[0].equivalentAlternatives = 1;
  if (row.level9Upgrade) {
    asTheory(row.level9Upgrade.board, score * 1.2);
    row.level9Upgrade.selection.evaluatedOn = 'theory';
    delete row.level9Upgrade.benchmarkWinDelta;
    row.level9Upgrade.theoryScoreDelta = score * .2;
  }
  return row;
}

function theoryMetadata() {
  return { revision: 'composition-theory-v1', baselineRevision: 'champions-v1', boardPlanModel,
    modelRevision: theoryRevision, scenarios: structuredClone(theoryScenarios), boardSize: 8,
    profiles: [{ key: 'c4', cost: 4, level: 8, boardSlots: 8, maxFiveCosts: 0, level9FiveCostStar: 2 }],
    methodology: { evaluationModel: theoryModel } };
}

const persistentTheoryModel = 'ehp-damage-capacity-v3';
const persistentTheoryRevision = 'persistent-theory-fixture-v3';
const persistentTheoryScenarios = [1000, 2000].flatMap(incomingDps => [0, .5, 1].flatMap(physicalShare =>
  [0, .33].flatMap(wound => [0, 1.5].flatMap(controlDuration => ['main-first', 'secondary-first'].map(targeting => ({
    key: `p${incomingDps}-physical${physicalShare}-wound${wound}-control${controlDuration}-${targeting}`,
    label: `${incomingDps} raw DPS · ${physicalShare * 100}% physical · ${wound * 100}% antiheal · ${controlDuration}s control · ${targeting}`,
    incomingDps, physicalShare, wound, controlDuration, targeting, controlInterval: 8,
    armor: 100, mr: 100, targetHp: 3000, targetCount: 3,
    pressureInterval: .5, pressureAllocation: 'persistent-source-targets', incomingSourceCount: 3,
  }))))));

function persistentTheoryMetadata() {
  return { ...theoryMetadata(), revision: 'composition-persistent-v3',
    modelRevision: persistentTheoryRevision, scenarios: structuredClone(persistentTheoryScenarios),
    methodology: { evaluationModel: persistentTheoryModel } };
}

function asPersistentTheory(row, score = 12000000, fronts = [1, 2, 3]) {
  asTheory(row, score);
  row.units.forEach((unit, index) => { unit.frontline = fronts.includes(index); });
  const main = row.units.find(unit => unit.slug === row.mainTank).api;
  const order = [main, ...row.units.filter(unit => unit.frontline && unit.api !== main).map(unit => unit.api)];
  Object.assign(row, { evaluationModel: persistentTheoryModel, modelRevision: persistentTheoryRevision,
    profileCount: persistentTheoryScenarios.length,
    metrics: { theoryScore: score, frontlineEhp: score / 1000, damageDps: 1000,
      damageCapacity: score / Math.sqrt(1000 * 2000), protectionTime: score / 1000 / Math.sqrt(1000 * 2000) },
    scenarios: persistentTheoryScenarios.map(definition => {
      const pressureTargetOrder = [...order];
      if (definition.targeting === 'secondary-first' && pressureTargetOrder.length > 1)
        [pressureTargetOrder[0], pressureTargetOrder[1]] = [pressureTargetOrder[1], pressureTargetOrder[0]];
      return { ...definition, pressureTargetOrder,
        initialPressureTargets: [pressureTargetOrder[0], pressureTargetOrder[1] || pressureTargetOrder[0], pressureTargetOrder[0]],
        frontlineEhp: score / 1000, damageDps: 1000, score,
        plannedMeasurementWindow: .8 * score / 1000 / definition.incomingDps,
        measurementWindow: .8 * score / 1000 / definition.incomingDps, frontlineCollapsed: false,
        incomingBudget: .8 * score / 1000, spentPressure: .7 * score / 1000,
        deniedPressure: .1 * score / 1000, unspentPressure: 0,
        protectionTime: score / 1000 / definition.incomingDps, damageCapacity: score / definition.incomingDps };
    }) });
  if (row.itemAnalysis) {
    row.itemAnalysis.modelRevision = persistentTheoryRevision;
    row.itemAnalysis.holders.forEach(holder => holder.items.forEach(item => item.alternatives.forEach(alternative => {
      alternative.degradedScenarios = score ? persistentTheoryScenarios.map(scenario => scenario.key) : [];
    })));
  }
  if (row.level9Upgrade) asPersistentTheory(row.level9Upgrade.board, score * 1.2, fronts);
  return row;
}

async function checkPersistentTheoryTargeting() {
  const d = domHarness(), ctx = d.context;
  const modelsMatch = vm.runInContext('tftCompositionModelsMatch', ctx);
  const row = asPersistentTheory(d.parent);
  d.composition.meta = { ...persistentTheoryMetadata(), primalBlessings: structuredClone(primalBlessings),
    geometries: { clump: 'Clumped', spread: 'Spread' }, structures: [{ key: 'single' }] };
  d.composition.data = { ...structuredClone(d.composition.meta), results: { 9: { single: [row] } } };
  const before = JSON.stringify([row, d.composition.meta]);
  assert.equal(ctx.tftCompositionMetadataModelValid(d.composition.meta), true);
  assert.equal(ctx.tftCompositionValid(row), true, 'All 48 v3 conditions include coherent persistent target diagnostics');
  const missingPopulation = structuredClone(d.composition.meta);
  missingPopulation.scenarios.forEach(scenario => { delete scenario.targetCount; });
  assert.equal(ctx.tftCompositionMetadataModelValid(missingPopulation), true, 'Geometry-independent metadata can omit outgoing population');
  assert.equal(modelsMatch(d.composition.data, missingPopulation), true);
  const withBlessing = withPrimal(asPersistentTheory(levelBoard('persistent-primal')), ['turtle']);
  assert.equal(ctx.tftCompositionValid(withBlessing), true, 'Primal chooses one aggregate across all 48 targeting and pressure conditions');
  for (const [label, mutate] of [
    ['missing target priority', board => { delete board.scenarios[0].pressureTargetOrder; }],
    ['missing initial assignments', board => { delete board.scenarios[0].initialPressureTargets; }],
    ['missing front role', board => { delete board.units[1].frontline; }],
    ['text front role', board => { board.units[1].frontline = 'true'; }],
    ['missing priority member', board => { board.scenarios[0].pressureTargetOrder.pop(); }],
    ['duplicate priority member', board => { board.scenarios[0].pressureTargetOrder[2] = board.scenarios[0].pressureTargetOrder[1]; }],
    ['unknown priority member', board => { board.scenarios[0].pressureTargetOrder[2] = 'unknown'; }],
    ['backline in priority', board => { board.scenarios[0].pressureTargetOrder[2] = board.units[0].api; }],
    ['wrong main tank priority', board => { board.scenarios[0].pressureTargetOrder.reverse(); }],
    ['secondary order does not swap first two', board => { board.scenarios[1].pressureTargetOrder = [...board.scenarios[0].pressureTargetOrder]; }],
    ['neutral priority changes with pressure', board => {
      const [tank, second, third] = board.scenarios[2].pressureTargetOrder;
      board.scenarios[2].pressureTargetOrder = [tank, third, second];
      board.scenarios[2].initialPressureTargets = [tank, third, tank];
      board.scenarios[3].pressureTargetOrder = [third, tank, second];
      board.scenarios[3].initialPressureTargets = [third, tank, third];
    }],
    ['wrong source count', board => { board.scenarios[0].incomingSourceCount = 4; }],
    ['two initial targets', board => { board.scenarios[0].initialPressureTargets.pop(); }],
    ['unknown target', board => { board.scenarios[0].initialPressureTargets[1] = 'unknown'; }],
    ['backline target', board => { board.scenarios[0].initialPressureTargets[1] = board.units[0].api; }],
    ['mixed assigned and null targets', board => { board.scenarios[0].initialPressureTargets[1] = null; }],
    ['wrong alternating assignments', board => { board.scenarios[0].initialPressureTargets[2] = board.scenarios[0].initialPressureTargets[1]; }],
    ['assignments reverse priority', board => {
      const [first, second] = board.scenarios[0].pressureTargetOrder;
      board.scenarios[0].initialPressureTargets = [second, first, second];
    }],
    ['old pressure routing', board => { board.scenarios[0].pressureAllocation = 'shared-live-frontline'; }],
    ['wrong pulse cadence', board => { board.scenarios[0].pressureInterval = .25; }],
    ['unknown targeting mode', board => { board.scenarios[0].targeting = 'random'; }],
    ['missing targeting mode', board => { delete board.scenarios[0].targeting; }],
    ['only 24 conditions', board => { board.scenarios = board.scenarios.slice(0, 24); board.profileCount = 24; }],
    ['old model label', board => { board.evaluationModel = theoryModel; }],
    ['missing cap assignments', board => { delete board.level9Upgrade.board.scenarios[0].initialPressureTargets; }],
  ]) {
    const invalid = structuredClone(row); mutate(invalid);
    assert.equal(ctx.tftCompositionValid(invalid), false, `${label} is rejected for v3`);
  }
  for (const [label, mutate] of [
    ['missing condition pair', meta => { meta.scenarios.splice(0, 2); }],
    ['duplicate targeting mode', meta => { meta.scenarios[0].targeting = meta.scenarios[1].targeting; }],
    ['unpaired pressure assumptions', meta => { meta.scenarios[0].incomingDps += 1; }],
    ['old routing in metadata', meta => { meta.scenarios[0].pressureAllocation = 'shared-live-frontline'; }],
    ['missing targeting input', meta => { delete meta.scenarios[0].targeting; }],
    ['invalid source count', meta => { meta.scenarios[0].incomingSourceCount = 4; }],
  ]) {
    const invalid = structuredClone(d.composition.meta); mutate(invalid);
    assert.equal(ctx.tftCompositionMetadataModelValid(invalid), false, `${label} is rejected`);
  }
  const swappedMetadata = structuredClone(d.composition.meta);
  swappedMetadata.scenarios.forEach(scenario => { scenario.targeting = scenario.targeting === 'main-first' ? 'secondary-first' : 'main-first'; });
  assert.equal(ctx.tftCompositionMetadataModelValid(swappedMetadata), true);
  assert.equal(modelsMatch(d.composition.data, swappedMetadata), false,
    'Changing targeting assumptions invalidates cached calculations even if revisions were reused');
  const one = asPersistentTheory(levelBoard('persistent-one-front'), 12000000, [1]);
  assert.equal(ctx.tftCompositionValid(one), true);
  assert(one.scenarios.every(scenario => new Set(scenario.initialPressureTargets).size === 1),
    'A sole available tank receives all three assignments');
  const initiallyUnavailable = asPersistentTheory(levelBoard('persistent-unavailable'));
  initiallyUnavailable.scenarios.forEach(scenario => { scenario.initialPressureTargets = [null, null, null]; });
  assert.equal(ctx.tftCompositionValid(initiallyUnavailable), true,
    'The native scorer may report no initially targetable frontliner; the browser cannot reconstruct initial immunity');
  assert.equal(JSON.stringify([row, d.composition.meta]), before, 'Schema validation preserves board data and target order');

  ctx.tftCoreItems = items => Object.assign(d.createElement('div'), { textContent: items.join(' · ') });
  ctx.tftSeg = () => {};
  vm.runInContext(script.slice(script.indexOf('function tftCompositionTheoryScenarios('), script.indexOf('function tftCompositionItemEvidence('))
    + script.slice(script.indexOf('function tftCompositionDetailContents('), script.indexOf('function tftCompositionCard('))
    + script.slice(script.indexOf('function renderTftCompositionControls('), script.indexOf('function tftCompositionRows(')), ctx);
  const card = ctx.tftCompositionCard(row, 1);
  assert(card.querySelector('.tft-comp-metrics').dataset.model === persistentTheoryModel);
  const details = ctx.tftCompositionDetailContents(row);
  const assignments = details.querySelectorAll('.tft-comp-initial-targets');
  assert.equal(assignments.length, 48);
  const mainName = row.units[1].name, secondaryName = row.units[2].name;
  assert.equal(assignments[0].textContent, `${mainName}: 2 attackers · ${secondaryName}: 1 attacker`);
  assert.equal(assignments[1].textContent, `${secondaryName}: 2 attackers · ${mainName}: 1 attacker`);
  assert(assignments[0].title.includes(`Attacker 1: ${mainName}`) && assignments[0].title.includes(`Attacker 3: ${mainName}`));
  assert(assignments[0].attributes['aria-label'].includes('Target priority:'));
  assert(details.textContent.includes('Main tank first') && details.textContent.includes('Second frontliner first'));
  assert(details.textContent.includes('keep their targets until death or untargetability'));
  assert(details.textContent.includes('including between hits'));
  const sole = ctx.tftCompositionTheoryScenarios(one).querySelector('.tft-comp-initial-targets');
  assert.equal(sole.textContent, `${one.units[1].name}: 3 attackers`);
  assert(ctx.tftCompositionTheoryScenarios(initiallyUnavailable).textContent.includes('No target available at start'));
  const upgrade = ctx.tftCompositionUpgradeContents(row), evidence = upgrade.querySelector('.tft-comp-upgrade-tests');
  evidence.open = true; evidence.events.toggle();
  assert.equal(evidence.querySelectorAll('.tft-comp-initial-targets').length, 48);
  ctx.renderTftCompositionControls();
  assert(d.element('tft-comp-context').textContent.includes('Persistent enemy targets'));
  assert(d.element('tft-comp-pool-note').textContent.includes('48 pressure assumptions'));
  assert.equal(JSON.stringify([row, d.composition.meta]), before, 'Rendering preserves assignments, scores, items and copy inputs');

  const h = theoryLoadHarness();
  await h.load();
  const previous = structuredClone(h.state.data);
  h.state.meta = persistentTheoryMetadata();
  const payload = () => ({ ...structuredClone(h.state.meta), key: `c4-${h.state.geo}-mixed`, profile: { key: 'c4' },
    geometry: h.state.geo, threat: 'mixed', results: { 9: { single: [asPersistentTheory(withUpgrade(levelBoard('persistent-cached')))] } } });
  h.payload = () => ({ ...payload(), results: previous.results });
  await h.load({ preserve: true });
  assert(h.state.error && h.state.data === null, 'A v2 cached board cannot be relabeled as v3');
  h.payload = payload;
  await h.load({ preserve: true });
  assert.equal(h.state.error, null);
  assert.equal(h.state.data.results[9].single[0].profileCount, 48);
  const calls = h.calls.length, saved = h.state.data;
  await h.load({ preserve: true });
  assert.equal(h.calls.length, calls);
  assert.equal(h.state.data, saved, 'Returning to an unchanged v3 board reuses its validated calculation');
}

const primalBlessings = [
  { key: 'tiger', name: 'Tiger', description: 'Attack speed after the opening delay.', scoreLimitation: null },
  { key: 'turtle', name: 'Turtle', description: 'Defensive stats.', scoreLimitation: null },
  { key: 'bear', name: 'Bear', description: 'An execute threshold.', scoreLimitation: 'Executes are not valued against immortal targets.' },
  { key: 'phoenix', name: 'Phoenix', description: 'A component reward.', scoreLimitation: 'Component rewards are not valued with a fixed item budget.' },
];

function withPrimal(row, selected = ['tiger'], required) {
  const keys = primalBlessings.map(blessing => blessing.key);
  const activeRequired = (required || []).slice(0, selected.length);
  const choices = (selected.length === 1 ? keys.map(key => [key])
    : keys.flatMap((key, index) => keys.slice(index + 1).map(next => [key, next])))
    .filter(choice => activeRequired.every(key => choice.includes(key)));
  row.traits = [...row.traits.filter(trait => trait.api !== 'DA_Primal18'),
    { api: 'DA_Primal18', name: 'Primal', active: true, count: selected.length * 2,
      breakpoint: selected.length * 2, blessings: [...selected], modeled: true }];
  const choiceKey = choice => choice.slice().sort().join('+');
  row.primal = { selected: [...selected], ...(required === undefined ? {} : { required: [...required] }),
    alternatives: choices.map(blessings => ({ blessings,
      score: row.metrics.theoryScore * (choiceKey(blessings) === choiceKey(selected) ? 1 : .9) })) };
  return row;
}

function checkPrimalBlessings() {
  const h = domHarness(), ctx = h.context;
  h.composition.meta = { ...theoryMetadata(), primalBlessings: structuredClone(primalBlessings) };
  asTheory(h.parent);
  h.composition.data = { ...structuredClone(h.composition.meta), results: { 9: { single: [h.parent] } } };
  const single = withPrimal(asTheory(levelBoard('primal-single')));
  const pair = withPrimal(asTheory(levelBoard('primal-pair')), ['tiger', 'turtle']);
  const before = JSON.stringify([single, pair, h.composition.meta]);
  assert.equal(ctx.tftCompositionValid(single), true);
  assert.equal(ctx.tftCompositionValid(pair), true);
  const capFixture = id => asTheory(withUpgrade(levelBoard(id))).level9Upgrade.board;
  const retainedSingle = withPrimal(capFixture('primal-retained-single'), ['turtle'], ['turtle']);
  const addedSecond = withPrimal(capFixture('primal-added-second'), ['tiger', 'turtle'], ['turtle']);
  const retainedPair = withPrimal(capFixture('primal-retained-pair'), ['turtle', 'tiger'], ['turtle', 'tiger']);
  const droppedTier = withPrimal(capFixture('primal-dropped-tier'), ['turtle'], ['turtle', 'tiger']);
  const retainedBefore = JSON.stringify([retainedSingle, addedSecond, retainedPair, droppedTier]);
  for (const [row, count] of [[retainedSingle, 1], [addedSecond, 3], [retainedPair, 1], [droppedTier, 1]]) {
    assert.equal(ctx.tftCompositionBoardValid(row, 9, false), true, 'Retained choices leave exactly the permitted cap alternatives');
    assert.equal(row.primal.alternatives.length, count);
  }
  const legacy = structuredClone(single); delete legacy.primal;
  assert.equal(ctx.tftCompositionValid(legacy), true, 'Older active-Primal boards without a chosen-blessing payload remain valid');
  const olderTrait = structuredClone(single); delete olderTrait.traits.at(-1).blessings;
  assert.equal(ctx.tftCompositionValid(olderTrait), true, 'The optional trait annotation is not required by older producers');
  for (const [label, mutate, source = single] of [
    ['null payload', row => { row.primal = null; }],
    ['unknown choice', row => { row.primal.selected = ['dragon']; }],
    ['empty choice', row => { row.primal.selected = []; }],
    ['duplicate choice', row => { row.primal.selected = ['tiger', 'tiger']; }],
    ['too many choices', row => { row.primal.selected = ['tiger', 'turtle', 'bear']; }],
    ['missing alternative', row => { row.primal.alternatives.pop(); }],
    ['duplicate alternative', row => { row.primal.alternatives[1] = structuredClone(row.primal.alternatives[0]); }],
    ['unknown alternative', row => { row.primal.alternatives[1].blessings = ['dragon']; }],
    ['mixed alternative sizes', row => { row.primal.alternatives[1].blessings = ['tiger', 'turtle']; }],
    ['negative score', row => { row.primal.alternatives[1].score = -1; }],
    ['nonfinite score', row => { row.primal.alternatives[1].score = Infinity; }],
    ['text score', row => { row.primal.alternatives[1].score = '100'; }],
    ['selected aggregate mismatch', row => { row.primal.alternatives[0].score += 1000; }],
    ['selection is not globally best', row => { row.primal.alternatives[1].score = row.metrics.theoryScore + 1; }],
    ['inactive Primal', row => { row.traits.at(-1).active = false; }],
    ['missing Primal', row => { row.traits.pop(); }],
    ['wrong tier for one blessing', row => { row.traits.at(-1).breakpoint = 4; }],
    ['wrong tier for two blessings', row => { row.traits.at(-1).breakpoint = 2; }, pair],
    ['contradictory resolved trait choice', row => { row.traits.at(-1).blessings = ['bear']; }],
    ['reversed duplicate pair', row => { row.primal.alternatives[1].blessings = ['turtle', 'tiger']; }, pair],
    ['null retained choices', row => { row.primal.required = null; }, retainedSingle],
    ['unknown retained choice', row => { row.primal.required = ['dragon']; }, retainedSingle],
    ['duplicated retained choices', row => { row.primal.required = ['turtle', 'turtle']; }, retainedSingle],
    ['too many retained choices', row => { row.primal.required = ['turtle', 'tiger', 'bear']; }, retainedSingle],
    ['free respec of a retained single', row => { row.primal.required = ['bear']; }, retainedSingle],
    ['free respec by keeping all six pairs', row => { row.primal.alternatives = structuredClone(pair.primal.alternatives); }, addedSecond],
    ['missing legal second blessing', row => { row.primal.alternatives.pop(); }, addedSecond],
    ['pair drops the retained first blessing', row => { row.primal.alternatives[1].blessings = ['tiger', 'bear']; }, addedSecond],
    ['dropping tiers activates the wrong retained choice', row => { row.primal.required.reverse(); }, droppedTier],
  ]) {
    const invalid = structuredClone(source); mutate(invalid);
    assert.equal(invalid.level === 9 ? ctx.tftCompositionBoardValid(invalid, 9, false)
      : ctx.tftCompositionValid(invalid), false, `${label} is rejected`);
  }
  for (const [label, mutate] of [
    ['null catalog', meta => { meta.primalBlessings = null; }],
    ['missing blessing', meta => { meta.primalBlessings.pop(); }],
    ['unknown key', meta => { meta.primalBlessings[0].key = 'dragon'; }],
    ['duplicate key', meta => { meta.primalBlessings[1].key = 'tiger'; }],
    ['missing name', meta => { meta.primalBlessings[0].name = ''; }],
    ['invalid description', meta => { meta.primalBlessings[0].description = 4; }],
    ['invalid score limitation', meta => { meta.primalBlessings[0].scoreLimitation = []; }],
  ]) {
    const invalid = structuredClone(h.composition.meta); mutate(invalid);
    assert.equal(ctx.tftCompositionMetadataModelValid(invalid), false, `${label} is rejected`);
  }
  assert.equal(ctx.tftCompositionMetadataModelValid(theoryMetadata()), true, 'A legacy catalog remains optional');
  const catalog = h.composition.meta.primalBlessings;
  delete h.composition.meta.primalBlessings;
  assert.equal(ctx.tftCompositionValid(single), false, 'A new choice payload requires its declared catalog');
  assert.equal(ctx.tftCompositionValid(legacy), true);
  h.composition.meta.primalBlessings = catalog;
  assert.equal(JSON.stringify([single, pair, h.composition.meta]), before, 'Choice validation never mutates data or catalog order');

  ctx.tftCoreItems = items => Object.assign(h.createElement('div'), { textContent: items.join(' · ') });
  ctx.tftCompositionTheoryScenarios = () => h.createElement('div');
  vm.runInContext(script.slice(script.indexOf('function tftCompositionDetailContents('),
    script.indexOf('function tftCompositionCard(')), ctx);
  withPrimal(h.parent, ['turtle']);
  withPrimal(h.parent.level9Upgrade.board, ['tiger', 'turtle'], ['turtle']);
  const saved = JSON.stringify([h.parent, h.composition.meta]);
  const card = ctx.tftCompositionCard(h.parent, 1);
  assert.equal(card.querySelector('.tft-comp-primal-choice').textContent, 'Primal: Turtle');
  assert.equal(card.querySelector('.tft-comp-primal-choice').title, 'Defensive stats.');
  const details = ctx.tftCompositionDetailContents(h.parent);
  const section = details.querySelector('.tft-comp-primal-details');
  const rows = section.querySelectorAll('.tft-comp-primal-alternative');
  assert.equal(rows.length, 4);
  assert.equal(rows.filter(row => row.dataset.selected === 'true').length, 1);
  assert(rows.find(row => row.dataset.selected === 'true').textContent.includes('Turtle · selected'));
  assert(section.textContent.includes(`One choice across all ${h.parent.profileCount} pressure assumptions`));
  assert(section.textContent.includes('Bear’s executes are not valued against immortal targets'));
  assert(section.textContent.includes('Phoenix’s component rewards are not valued with a fixed item budget'));
  assert(section.textContent.includes('Those benefits can still matter in a real game'));
  const upgrade = ctx.tftCompositionUpgradeContents(h.parent);
  assert.equal(upgrade.querySelector('.tft-comp-primal-choice').textContent, 'Primal: Tiger + Turtle');
  const evidence = upgrade.querySelector('.tft-comp-upgrade-tests');
  evidence.open = true; evidence.events.toggle();
  assert.equal(evidence.querySelectorAll('.tft-comp-primal-alternative').length, 3);
  assert(evidence.querySelector('.tft-comp-primal-retained').textContent.includes('Retained from level 8: Turtle'));
  assert(evidence.querySelector('.tft-comp-primal-retained').textContent.includes('reset behavior is unverified'));
  assert.equal(ctx.tftCompositionPrimalDetails(pair).querySelectorAll('.tft-comp-primal-alternative').length, 6,
    'A board with no prior choices still compares all six pairs');
  assert.equal(ctx.tftCompositionPrimalDetails(retainedSingle).querySelectorAll('.tft-comp-primal-alternative').length, 1);
  assert(ctx.tftCompositionPrimalDetails(droppedTier).textContent.includes('only the first retained choice is active'));
  const oldDetails = ctx.tftCompositionDetailContents(legacy);
  assert.equal(oldDetails.querySelector('.tft-comp-primal-details'), null, 'Do not invent a blessing choice for a pinned board');
  assert.equal(JSON.stringify([h.parent, h.composition.meta]), saved, 'Card, cap and alternatives preserve scores, choices, items and catalog order');
  assert.equal(JSON.stringify([retainedSingle, addedSecond, retainedPair, droppedTier]), retainedBefore,
    'Filtering legal cap alternatives preserves required-choice order and latent second choices');

  h.composition.meta.primalBlessings[1].name = '<script>Turtle</script>';
  const literal = ctx.tftCompositionPrimalChoice(h.parent);
  assert.equal(literal.textContent, 'Primal: <script>Turtle</script>');
  assert(!Object.hasOwn(literal, 'innerHTML'), 'Blessing labels remain literal text');
}

function checkTheoryValidationAndRanks() {
  const previous = { ...tcomp };
  Object.assign(tcomp, { meta: theoryMetadata(), profile: 'c4', structure: 'auto', budget: 9 });
  const best = asTheory(withUpgrade(levelBoard('theory-best')), 12500);
  const tied = asTheory(levelBoard('theory-tied'), 12500);
  const weaker = asTheory(withUpgrade(levelBoard('theory-weaker')), 12499.99);
  asTheory(weaker.level9Upgrade.board, 25000);
  weaker.level9Upgrade.theoryScoreDelta = 25000 - weaker.metrics.theoryScore;
  tcomp.data = { ...theoryMetadata(), results: { 9: { single: [weaker, best, tied] } } };
  const snapshot = JSON.stringify(tcomp.data);
  assert.equal(context.tftCompositionValid(best), true, 'Theory boards need pressure assumptions and legal items, without authored opponents or validation fights');
  const doubleMelee = asTheory(levelBoard('theory-double-melee'));
  doubleMelee.structure = 'duoCarry';
  for (const [index, kind, objective] of [[0, 'Assassin', 'fighter'], [1, 'Tank', 'tank'], [2, 'Fighter', 'fighter']]) {
    Object.assign(doubleMelee.units[index], { kind, objective, form: 'AD', frontline: true,
      assignment: index === 0 ? 'mainCarry' : index === 1 ? 'mainTank' : 'secondCarry' });
    doubleMelee.units[index].items = [index ? 'Other' : 'Selected', 'Other'];
    doubleMelee.units[index].itemApis = [index ? 'other' : 'selected', 'other'];
  }
  doubleMelee.itemCount = doubleMelee.units.reduce((count, unit) => count + unit.itemApis.length, 0);
  assert.equal(context.tftCompositionValid(doubleMelee), true, 'A level-eight board can contain two melee carries');
  doubleMelee.maxMeleeCarries = 1;
  assert.equal(context.tftCompositionValid(doubleMelee), true, 'An obsolete saved limit field does not impose a current validation rule');
  assert.equal(context.tftCompositionValid(asTheory(withUpgrade(levelBoard('theory-elder'), { elder: true }))), true,
    'Theory keeps Elder occupancy and cap item conservation');
  assert.equal(context.tftCompositionValid(asTheory(withSingleUpgrade(levelBoard('theory-add')))), true);
  assert.deepEqual(Array.from(context.tftCompositionRows(), row => row.id), ['theory-best', 'theory-tied', 'theory-weaker']);
  assert.deepEqual(Array.from(context.tftCompositionRanks(context.tftCompositionRows())), [1, 1, 3],
    'Scores remain continuous above 100; stronger caps and old benchmark outcomes cannot alter level 8 rank');
  tcomp.structure = 'single';
  assert.equal(context.tftCompositionRows()[0].id, 'theory-best', 'A specific allocation also sorts by capacity');
  assert.equal(JSON.stringify(tcomp.data), snapshot, 'Validation and rank display preserve theory artifacts');
  const resultWithoutOwnModel = structuredClone(best);
  delete resultWithoutOwnModel.evaluationModel;
  assert.equal(context.tftCompositionValid(resultWithoutOwnModel), true, 'Enclosing methodology may identify the theory model');
  assert.equal(context.tftCompositionValid(asTheory(levelBoard('zero-theory'), 0)), true, 'Zero capacity is represented explicitly without a fake percentage');
  for (const [label, mutate] of [
    ['mixed old model', row => { row.evaluationModel = 'symmetric-reference-pool-v1'; }],
    ['wrong revision', row => { row.modelRevision = 'old-theory'; }],
    ['fake benchmark metrics', row => { row.metrics.benchmarkWins = 12; }],
    ['old matchups', row => { row.matchups = []; }],
    ['infinite capacity', row => { row.metrics.theoryScore = Infinity; }],
    ['negative EHP', row => { row.metrics.frontlineEhp = -1; }],
    ['inconsistent aggregate EHP', row => { row.metrics.frontlineEhp += 1; }],
    ['wrong damage capacity formula', row => { row.scenarios[0].damageCapacity += 10; }],
    ['wrong score formula', row => { row.scenarios[0].score *= 2; }],
    ['missing scenario', row => { row.scenarios.pop(); }],
    ['duplicate scenario', row => { row.scenarios[1].key = row.scenarios[0].key; }],
    ['wrong pressure input', row => { row.scenarios[0].incomingDps = 1001; }],
    ['impossible damage mixture', row => { row.scenarios[0].physicalShare = 2; }],
    ['changed control duration', row => { row.scenarios[0].controlDuration = 1.5; }],
    ['wrong pressure allocation', row => { row.scenarios[0].pressureAllocation = 'independent'; }],
    ['missing incoming source count', row => { delete row.scenarios[0].incomingSourceCount; }],
    ['outgoing coverage used as incoming source count', row => { row.scenarios[0].incomingSourceCount = 1; }],
    ['different cap source count', row => { row.level9Upgrade.board.scenarios[0].incomingSourceCount = 2; }],
    ['missing planned window', row => { delete row.scenarios[0].plannedMeasurementWindow; }],
    ['observation after planned end', row => { row.scenarios[0].measurementWindow = row.scenarios[0].plannedMeasurementWindow + 1; }],
    ['early stop without collapse', row => { row.scenarios[0].measurementWindow /= 2; }],
    ['invalid collapse flag', row => { row.scenarios[0].frontlineCollapsed = 'false'; }],
    ['negative raw budget', row => { row.scenarios[0].incomingBudget = -1; }],
    ['missing spent pressure', row => { delete row.scenarios[0].spentPressure; }],
    ['unconserved pressure', row => { row.scenarios[0].deniedPressure += 1; }],
    ['self-consistent budget for wrong observed time', row => {
      row.scenarios[0].incomingBudget /= 2;
      row.scenarios[0].spentPressure /= 2;
      row.scenarios[0].deniedPressure /= 2;
    }],
    ['unspent exceeds budget', row => { row.scenarios[0].unspentPressure = row.scenarios[0].incomingBudget + 1; }],
    ['stale item analysis', row => { row.itemAnalysis.modelRevision = 'old-theory'; }],
    ['wrong item baseline', row => { row.itemAnalysis.baselineScore += 1; }],
    ['wrong item delta', row => { row.itemAnalysis.holders[0].items[0].alternatives[0].scoreDelta = 1; }],
    ['wrong item percentage', row => { row.itemAnalysis.holders[0].items[0].alternatives[0].pctDelta = 2; }],
    ['unknown item scenario', row => { row.itemAnalysis.holders[0].items[0].alternatives[0].improvedScenarios = ['invented']; }],
    ['duplicated holder evidence', row => { row.itemAnalysis.holders.push(structuredClone(row.itemAnalysis.holders[0])); }],
    ['wrong cap delta', row => { row.level9Upgrade.theoryScoreDelta = 0; }],
    ['changed cap conditions', row => { row.level9Upgrade.board.scenarios[0].incomingDps += 1; }],
    ['cap searched fights', row => { row.level9Upgrade.selection.evaluatedOn = 'search'; }],
    ['reitemized retained holder', row => { row.level9Upgrade.board.units[0].itemApis = ['invented']; }],
    ['added item budget', row => { row.level9Upgrade.board.itemCount += 1; }],
  ]) {
    const invalid = structuredClone(best); mutate(invalid);
    assert.equal(context.tftCompositionValid(invalid), false, `${label} is rejected for theoretical results`);
  }
  const verdict = context.tftCompositionItemVerdict(best.itemAnalysis.holders[0].items[0], best);
  const invalidSources = theoryMetadata();
  invalidSources.scenarios[0].incomingSourceCount = 1;
  assert.equal(context.tftCompositionMetadataModelValid(invalidSources), false,
    'Metadata cannot quietly change the declared three incoming channels');
  const missingProfiles = theoryMetadata();
  delete missingProfiles.scenarios;
  assert.equal(context.tftCompositionMetadataModelValid(missingProfiles), false,
    'V2 metadata must declare the pressure/control profiles used by every board');
  assert(verdict.includes('2.0%') && !verdict.includes('win'));
  assert(!context.tftCompositionEvidenceSummary(best).includes('ceiling'));
  Object.assign(tcomp, previous);
}

function theoryLoadHarness() {
  const h = loadHarness();
  Object.assign(h.state, { profile: 'c4', meta: theoryMetadata() });
  h.payload = () => {
    const row = asTheory(withUpgrade(levelBoard('cached-theory')));
    for (const result of [row, row.level9Upgrade.board]) {
      result.modelRevision = h.state.meta.modelRevision;
      result.scenarios.forEach((scenario, i) => {
        Object.assign(scenario, h.state.meta.scenarios[i]);
        scenario.protectionTime = scenario.frontlineEhp / scenario.incomingDps;
        scenario.damageCapacity = scenario.score / scenario.incomingDps;
        scenario.plannedMeasurementWindow = scenario.measurementWindow = .8 * scenario.frontlineEhp / scenario.incomingDps;
        scenario.incomingBudget = .8 * scenario.frontlineEhp;
      });
      for (const metric of ['damageCapacity', 'protectionTime']) result.metrics[metric] = Math.sqrt(result.scenarios[0][metric] * result.scenarios[1][metric]);
      if (result.itemAnalysis) result.itemAnalysis.modelRevision = result.modelRevision;
    }
    return { ...structuredClone(h.state.meta), key: `c4-${h.state.geo}-mixed`, profile: { key: 'c4' },
      geometry: h.state.geo, threat: 'mixed', results: { 9: { single: [row] } } };
  };
  return h;
}

async function checkTheoryCacheAndRendering() {
  const h = theoryLoadHarness();
  await h.load();
  assert.equal(h.state.error, null, 'Theory artifacts load through the real cache guard');
  const saved = h.state.data;
  await h.load({ preserve: true });
  assert.equal(h.calls.length, 1, 'Matching theory results reuse the validated artifact');
  assert.equal(h.state.data, saved);
  h.state.meta.modelRevision = 'theory-fixture-v2';
  await h.load({ preserve: true });
  assert.equal(h.calls.length, 2, 'A changed capacity formula revision fetches a new artifact');
  assert.equal(h.state.error, null);
  h.state.meta.scenarios[0].incomingDps += 100;
  await h.load({ preserve: true });
  assert.equal(h.calls.length, 3, 'Changed pressure assumptions also invalidate reusable data');
  assert.equal(h.state.error, null);
  for (const [label, mutate] of [
    ['legacy model', data => { data.methodology.evaluationModel = 'symmetric-reference-pool-v1'; }],
    ['conflicting top-level model', data => { data.evaluationModel = 'ehp-damage-capacity-v1'; }],
    ['wrong model revision', data => { data.modelRevision = 'old-theory'; }],
    ['mismatched scenario inputs', data => { data.scenarios[0].incomingDps += 1; }],
    ['bad board evidence', data => { data.results[9].single[0].itemAnalysis.evaluatedOn = 'search'; }],
  ]) {
    const invalid = theoryLoadHarness();
    invalid.response = () => { const data = invalid.payload(); mutate(data); return data; };
    await invalid.load();
    assert.equal(invalid.state.data, null, `${label} cannot mix with a theory publication`);
    assert(invalid.state.error);
  }

  const d = domHarness(), ctx = d.context;
  asTheory(d.parent);
  d.composition.meta = { ...theoryMetadata(), geometries: { clump: 'Clumped', spread: 'Spread' }, structures: [{ key: 'single' }] };
  d.composition.data = { ...structuredClone(d.composition.meta), results: { 9: { single: [d.parent] } } };
  ctx.tftCoreItems = (items) => Object.assign(d.createElement('div'), { textContent: items.join(' · ') });
  ctx.tftSeg = () => {};
  vm.runInContext(script.slice(script.indexOf('function tftCompositionTheoryScenarios('), script.indexOf('function tftCompositionItemEvidence('))
    + script.slice(script.indexOf('function tftCompositionDetailContents('), script.indexOf('function tftCompositionCard('))
    + script.slice(script.indexOf('function renderTftCompositionControls('), script.indexOf('function tftCompositionRows(')), ctx);
  const card = d.card = ctx.tftCompositionCard(d.parent, 1);
  const metrics = card.querySelector('.tft-comp-metrics');
  assert(metrics.textContent.includes('EHP × DPS score') && metrics.textContent.includes('Frontline EHP') && metrics.textContent.includes('Team DPS') && metrics.textContent.includes('Est. protection'));
  assert(!/benchmark|held-out|wins|undefined|NaN/i.test(card.textContent));
  const details = ctx.tftCompositionDetailContents(d.parent);
  const scenarios = details.querySelector('.tft-comp-scenario-table');
  assert(scenarios.textContent.includes('1,000') && scenarios.textContent.includes('50% / 50%') && scenarios.textContent.includes('100 / 100') && scenarios.textContent.includes('33%'));
  assert(scenarios.textContent.includes('12,000') && scenarios.textContent.includes('3,000'));
  assert(scenarios.textContent.includes('3 × 3,000 HP') && scenarios.textContent.includes('Measured over'));
  assert(scenarios.textContent.includes('Planned window') && scenarios.textContent.includes('Held through window'));
  assert(scenarios.textContent.includes('Incoming budget') && scenarios.textContent.includes('Unspent pressure'));
  assert(scenarios.textContent.includes('no enemy control') && scenarios.textContent.includes('1.5s frontline stun every 8s'));
  assert(!/benchmark|held-out|wins|undefined|NaN/i.test(details.textContent));
  const replacements = details.querySelector('.tft-comp-replacements');
  replacements.open = true; replacements.events.toggle();
  assert(replacements.textContent.includes('-2.0%') && replacements.textContent.includes('same 2 pressure assumptions'));
  assert(replacements.textContent.includes('no enemy control') && replacements.textContent.includes('1.5s frontline stun every 8s'));
  const cap = ctx.tftCompositionUpgradeContents(d.parent);
  assert(cap.textContent.includes('+20.0% EHP × DPS score') && cap.textContent.includes('Level 8 results determine this composition’s rank.'));
  assert(!/benchmark|held-out|wins|undefined|NaN/i.test(cap.textContent));
  const legendary = d.parent.level9Upgrade.board.units.at(-1);
  cap.querySelectorAll('.tft-comp-unit-head').find(head => head.dataset.unit === legendary.slug).events.click();
  assert.equal(d.scenarios.at(-1).star, 2, 'Theory caps still open the exact saved champion star');
  assert.equal(d.composition.returnUpgrade, d.parent.level9Upgrade.board.id);
  const tooltip = ctx.tftCompositionChampionTooltip(d.parent.units[0], d.parent).map(node => node.textContent).join('\n');
  assert(tooltip.includes('pressure assumptions') && !tooltip.includes('ranking fights'));
  assert(tooltip.includes('125 DPS contribution') && tooltip.includes('120 mean measured DPS') && tooltip.includes('1,000 raw pressure spent'));
  assert(tooltip.includes('arithmetic means') && tooltip.includes('excludes overkill'));
  assert(details.textContent.includes('DPS contribution') && details.textContent.includes('mean measured DPS') && details.textContent.includes('raw pressure spent'));
  ctx.renderTftCompositionControls();
  assert.equal(d.element('tft-comp-geo-label').textContent, 'Target coverage');
  assert(d.element('tft-comp-context').textContent.includes('geometric mean of EHP × DPS'));
  assert(!/benchmark|held-out|win rate/i.test(d.element('tft-comp-ranking-note').textContent));
  assert(d.element('tft-comp-pool-note').textContent.includes('not statistical confidence'));
}

async function checkTheoryV2ObservationAndLegacyRollout() {
  const h = theoryLoadHarness();
  const currentPayload = h.payload;
  h.payload = () => {
    const payload = currentPayload(), row = payload.results[9].single[0];
    for (const result of [row, row.level9Upgrade.board]) {
      result.scenarios[0].measurementWindow /= 2;
      result.scenarios[0].frontlineCollapsed = true;
      result.scenarios[0].spentPressure /= 2;
      result.scenarios[0].deniedPressure /= 2;
      result.scenarios[0].incomingBudget = result.scenarios[0].spentPressure + result.scenarios[0].deniedPressure;
    }
    return payload;
  };
  await h.load();
  assert.equal(h.state.error, null, 'A measured early collapse is valid; the score identities still hold');
  const d = domHarness(), ctx = d.context;
  Object.assign(d.composition, { meta: h.state.meta, data: h.state.data });
  vm.runInContext(script.slice(script.indexOf('function tftCompositionTheoryScenarios('), script.indexOf('function tftCompositionItemEvidence(')), ctx);
  const section = ctx.tftCompositionTheoryScenarios(h.state.data.results[9].single[0]);
  assert(section.textContent.includes('Collapsed') && section.textContent.includes('Held through window'));
  assert(section.textContent.includes('all protected damage stops there') && section.textContent.includes('DPS uses this observed time'));

  const previous = theoryLoadHarness();
  previous.state.meta.methodology.evaluationModel = 'ehp-damage-capacity-v1';
  previous.state.meta.modelRevision = 'previous-theory-formula';
  for (const scenario of previous.state.meta.scenarios) {
    for (const key of ['controlInterval', 'controlDuration', 'pressureInterval', 'pressureAllocation', 'incomingSourceCount']) delete scenario[key];
  }
  const legacyPayload = previous.payload;
  previous.payload = () => {
    const payload = legacyPayload();
    for (const row of [payload.results[9].single[0], payload.results[9].single[0].level9Upgrade.board]) {
      row.evaluationModel = 'ehp-damage-capacity-v1';
      for (const scenario of row.scenarios) {
        for (const key of ['plannedMeasurementWindow', 'frontlineCollapsed', 'incomingBudget', 'spentPressure', 'deniedPressure', 'unspentPressure',
          'controlInterval', 'controlDuration', 'pressureInterval', 'pressureAllocation', 'incomingSourceCount']) delete scenario[key];
      }
    }
    return payload;
  };
  await previous.load();
  assert.equal(previous.state.error, null, 'The pinned v1 generation stays readable during the v2 rollout');
  const old = domHarness();
  Object.assign(old.composition, { meta: previous.state.meta, data: previous.state.data });
  vm.runInContext(script.slice(script.indexOf('function tftCompositionTheoryScenarios('), script.indexOf('function tftCompositionItemEvidence(')), old.context);
  const older = old.context.tftCompositionTheoryScenarios(previous.state.data.results[9].single[0]);
  assert(older.textContent.includes('saved v1 results') && !older.textContent.includes('Planned window'));
  assert(!older.textContent.includes('Pressure is reassigned') && !older.textContent.includes('Control immunity is respected'));
  previous.state.meta.methodology.evaluationModel = theoryModel;
  previous.response = previous.payload;
  await previous.load({ preserve: true });
  assert(previous.state.error && previous.state.data === null, 'Old board metrics cannot masquerade as the new model');
}

async function checkRealTheoryArtifact(meta, payload, label) {
  const h = loadHarness();
  h.baseline = payload.baselineRevision;
  Object.assign(h.state, { meta, profile: payload.profile?.key || payload.profile,
    geo: payload.geometry, threat: payload.threat, budget: Number(Object.keys(payload.results)[0]) });
  h.payload = () => structuredClone(payload);
  assert.equal(h.context.tftCompositionMetadataModelValid(meta), true, `${label}: real theory metadata passes the model guard`);
  await h.load();
  assert.equal(h.state.error, null, `${label}: the full generated payload loads and validates`);
  assert(h.state.data, `${label}: validated payload is available`);
  await h.load({ preserve: true });
  assert.equal(h.calls.length, 1, `${label}: returning from a champion reuses the same validated calculation`);
  const rows = Object.values(payload.results).flatMap(groups => Object.values(groups).flat());
  if (meta.teamPlanner) {
    const source = JSON.parse(fs.readFileSync(require('node:path').join(__dirname, '../data/tft/set18/team-planner.json'), 'utf8'));
    const names = new Map(Object.values(source.sourceRecords).map(record => [record.teamPlannerCode, record.displayName]));
    for (const board of rows.flatMap(row => [row, ...(row.level9Upgrade ? [row.level9Upgrade.board] : [])])) {
      const code = h.context.tftCompositionTeamCode(board, meta.teamPlanner);
      assert(/^02[0-9a-f]{30}TFTSet18$/.test(code), `${label}: generated board has a valid complete planner code`);
      const importedNames = code.slice(2, 32).match(/.{3}/g).filter(part => part !== '000').map(part => names.get(parseInt(part, 16)));
      assert.deepEqual(importedNames, board.units.map(unit => unit.name), `${label}: planner IDs import exactly the displayed champions`);
    }
  }
  const representatives = Object.entries(payload.results).flatMap(([budget, groups]) =>
    Object.entries(groups).filter(([, boards]) => boards.length).map(([structure, boards]) => ({ budget, structure, board: boards[0] })));
  let rendered = 0;
  for (const { budget, structure, board } of representatives) {
    const d = domHarness(), ctx = d.context;
    Object.assign(d.composition, { meta, data: payload, profile: h.state.profile, geo: payload.geometry,
      key: payload.key, budget: Number(budget), structure });
    d.champion.meta = { revision: payload.baselineRevision, traits: [], units:
      [...board.units, ...(board.level9Upgrade?.board.units || [])].map(unit => ({ ...unit, stars: [unit.star] })) };
    ctx.tftCoreItems = items => Object.assign(d.createElement('div'), { textContent: items.join(' · ') });
    ctx.tftSeg = () => {};
    vm.runInContext(script.slice(script.indexOf('function tftCompositionTheoryScenarios('), script.indexOf('function tftCompositionItemEvidence('))
      + script.slice(script.indexOf('function tftCompositionDetailContents('), script.indexOf('function tftCompositionCard('))
      + script.slice(script.indexOf('function renderTftCompositionControls('), script.indexOf('function tftCompositionRows(')), ctx);
    const snapshot = JSON.stringify(board);
    const card = d.card = ctx.tftCompositionCard(board, 1);
    const copied = [];
    if (meta.teamPlanner) {
      ctx.navigator = { clipboard: { writeText: async code => { copied.push(code); } } };
      const control = card.querySelector('.tft-comp-copy');
      assert.notEqual(control.children[0].disabled, true, `${label}: real core copy is available`);
      await control.children[0].events.click();
      assert.equal(copied[0], ctx.tftCompositionTeamCode(board, meta.teamPlanner));
    }
    assert(card.textContent.includes('EHP × DPS score'), `${label}: generated board displays the new score`);
    assert(!/Benchmark wins|Held-out|undefined|NaN/.test(card.textContent), `${label}: old metrics never leak into a theory card`);
    const details = ctx.tftCompositionDetailContents(board);
    const table = details.querySelector('.tft-comp-scenario-table');
    assert(table && table.textContent.includes('Measured over'), `${label}: measured windows and pressure inputs render`);
    assert(details.textContent.includes('DPS contribution') && details.textContent.includes('mean measured DPS') &&
      details.textContent.includes('raw pressure spent'), `${label}: real unit diagnostics have the correct meanings`);
    for (const evidence of details.querySelectorAll('.tft-comp-replacements')) {
      evidence.open = true;
      evidence.events.toggle();
    }
    assert(!/Benchmark wins|Held-out|undefined|NaN/.test(details.textContent), `${label}: real item evidence renders without missing or obsolete metrics`);
    const unit = board.units.find(unit => unit.frontline) || board.units[0];
    const tooltip = ctx.tftCompositionChampionTooltip(unit, board).map(node => node.textContent).join('\n');
    assert(tooltip.includes('raw pressure spent') && tooltip.includes('DPS contribution'), `${label}: frontline tooltip matches theoretical measurements`);
    card.querySelectorAll('.tft-comp-unit-head').find(head => head.dataset.unit === unit.slug).events.click();
    assert.equal(d.scenarios.at(-1).star, unit.star, `${label}: real champions open their exact saved star level`);
    if (board.level9Upgrade) {
      const cap = ctx.tftCompositionUpgradeContents(board);
      if (meta.teamPlanner) {
        const control = cap.querySelector('.tft-comp-copy');
        assert.notEqual(control.children[0].disabled, true, `${label}: real upgrade copy is available`);
        await control.children[0].events.click();
        assert.equal(copied[1], ctx.tftCompositionTeamCode(board.level9Upgrade.board, meta.teamPlanner));
      }
      const evidence = cap.querySelector('.tft-comp-upgrade-tests');
      evidence.open = true;
      evidence.events.toggle();
      assert(!/Benchmark wins|Held-out|undefined|NaN/.test(cap.textContent), `${label}: real cap transitions and metrics render`);
      const legendary = board.level9Upgrade.board.units.find(unit => !board.units.some(previous => previous.slug === unit.slug));
      cap.querySelectorAll('.tft-comp-unit-head').find(head => head.dataset.unit === legendary.slug).events.click();
      assert.equal(d.scenarios.at(-1).star, legendary.star, `${label}: actual level 9 additions keep their saved star assumption`);
      assert.equal(d.composition.returnUpgrade, board.level9Upgrade.board.id, `${label}: champion navigation remembers the actual cap`);
    }
    ctx.renderTftCompositionControls();
    assert(d.element('tft-comp-pool-note').textContent.includes('Raw incoming DPS:'), `${label}: scenario axes are visible`);
    assert.equal(JSON.stringify(board), snapshot, `${label}: display and navigation preserve the computed board`);
    ++rendered;
  }
  console.log(`Real theoretical composition UI checks passed: ${label}; ${rows.length} ${rows.length === 1 ? 'board' : 'boards'} validated, ${rendered} budget/allocation examples rendered.`);
}

async function checkRequestedTheoryArtifacts() {
  let metadataPath = null;
  const requested = [];
  const args = process.argv.slice(2);
  for (let index = 0; index < args.length; ++index) {
    const flag = args[index], path = args[++index];
    assert(path, `Missing path for ${flag}`);
    if (flag === '--theory-meta') metadataPath = path;
    else if (flag === '--theory-fixture') requested.push({ path });
    else if (flag === '--theory-payload') {
      assert(metadataPath, '--theory-meta must precede --theory-payload');
      requested.push({ path, metadataPath });
    } else throw new Error(`Unknown argument ${flag}`);
  }
  for (const { path, metadataPath } of requested) {
    const data = JSON.parse(fs.readFileSync(path, 'utf8'));
    const meta = metadataPath ? JSON.parse(fs.readFileSync(metadataPath, 'utf8')) : data.meta;
    await checkRealTheoryArtifact(meta, metadataPath ? data : data.payload, path);
  }
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
  champion.status.refresh = { status: 'failed', exit: 75,
    transport: { exhausted: true, retryable: true },
    reviewBlocker: { targetPatch: '18.2', message: 'Technical diagnostic in the journal.' } };
  ctx.renderTftRefresh();
  assert.equal(element('tft-refresh-title').textContent, 'Patch download was interrupted');
  assert(element('tft-refresh-detail').textContent.includes('Patch 18.2 still has an unresolved review'),
    'A later transport failure must not hide a pending patch review');
  champion.status.refresh = { status: 'waiting-not-before', retryNotBefore: '2026-09-10T20:00:00+00:00' };
  ctx.renderTftRefresh();
  assert.equal(element('tft-refresh-title').textContent, 'Patch source requested a pause');
  assert(element('tft-refresh-detail').textContent.includes('Downloads can resume after'));
  champion.status.refresh = { status: 'failed' };
  ctx.renderTftRefresh();
  assert.equal(element('tft-refresh-title').textContent, 'Automatic update did not finish',
    'Older status payloads without transport/history remain supported');
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
checkTraitCoverage();
checkTheoryValidationAndRanks();
checkPrimalBlessings();
checkTeamCodes();
checkAntihealSources();
assert(html.includes('Loading saved compositions…'), 'Loading accurately describes reading precomputed results');
assert(!html.includes('Calculating the comparison…'), 'A fetch is not presented as a fresh simulation');
checkSavedCompositions().then(checkPublicationRefresh).then(checkSavedLevelPlans).then(checkLevelRenderingAndNavigation).then(checkTheoryCacheAndRendering).then(checkTheoryV2ObservationAndLegacyRollout).then(checkPersistentTheoryTargeting).then(checkRequestedTheoryArtifacts).then(checkCompositionClipboard).then(() => {
  console.log('Saved composition UI checks passed: validated reuse, explicit retry, revision and publication guards, request races, deferred details and scheduled publication messages.');
  console.log('Composition level-plan UI checks passed: both teams\' slot occupancy, level 8 ranking, valid level 9 transitions, conserved items, changed traits, lazy cards and exact champion navigation.');
  console.log('Composition champion tooltip checks passed: exact identity and stars, board-specific traits and contributions, opponent details, and saved-data reuse.');
  console.log('Composition trait coverage checks passed: visible partial labels, accessible omissions, structural/economy distinctions and legacy fallback.');
  console.log('Composition copy checks passed: Riot planner IDs, exact roster codes, separate core/cap controls, clipboard success, HTTP fallback and manual recovery.');
  console.log('Composition antiheal checks passed: readable sources and holders, literal text safety, separate core/cap sources and old-payload fallback.');
  console.log('Composition carry checks passed: level-eight double melee, unrestricted new metadata and older pinned descriptions.');
  console.log('Composition Primal checks passed: chosen core/cap blessings, complete legal alternatives with retained choices, score limits, invalid payload guards, immutability and legacy fallback.');
  console.log('Theoretical composition UI checks passed: continuous ranking, visible EHP/DPS and pressure inputs, item percentages, cap conservation, exact champion navigation and model/revision cache isolation.');
  console.log('Theory v2 UI checks passed: observed collapse versus planned windows, conserved raw pressure, explicit control conditions and isolated v1 rollout support.');
  console.log('Theory v3 UI checks passed: 48 paired focus assumptions, persistent initial targets, source counts, malformed-data guards, v1/v2 rollout and cache transition.');
}).catch(error => { console.error(error); process.exitCode = 1; });
