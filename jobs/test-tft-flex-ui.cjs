// Run: node jobs/test-tft-flex-ui.cjs [--catalog /path/to/traits.json]
// Exercises the inline browser search and interface without npm dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script);
const section = (from, to) => script.slice(script.indexOf(from), script.indexOf(to));
const algorithm = section('// ---------------- trait flex: unit search ----------------', '// ---------------- trait flex: interface ----------------');
const algorithmContext = vm.createContext({ setTimeout });
vm.runInContext(algorithm, algorithmContext);
vm.runInContext(section('// ---------------- trait flex: lobby gold estimates ----------------',
  '// ---------------- trait flex: lobby gold estimates end ----------------'), algorithmContext);
const plain = value => JSON.parse(JSON.stringify(value));

function fixture() {
  const trait = (api, levels) => ({ api, name: api.toUpperCase(), levels, icon: '/trait.png' });
  const unit = (slug, traitApis, frontline = false, options = {}) => ({ api: slug, slug, name: slug.toUpperCase(),
    cost: 2, icon: '/unit.png', role: frontline ? 'Tank' : 'Caster', objective: frontline ? 'tank' : 'carry',
    range: frontline ? 1 : 4, frontline, backline: !frontline, slotCost: 1,
    traitApis, traitContributions: Object.fromEntries(traitApis.map(api => [api, 1])), ...options });
  const units = [unit('tank', ['a', 'unique'], true, { cost: 4 }), unit('carry', ['b', 'unique'], false, { cost: 4 }),
    unit('front-a', ['a', 'c'], true), unit('front-b', ['b', 'c'], true),
    unit('duo-a', ['d', 'mixed']), unit('duo-b', ['d', 'mixed']), unit('front-d', ['a', 'd'], true),
    unit('back-b', ['b', 'mixed']), unit('bridge', ['c', 'mixed']), unit('connect', ['a', 'b']),
    unit('elder', ['a', 'b', 'c'], true, { cost: 5, slotCost: 2, traitContributions: { a: 2, b: 1, c: 2 } })];
  return { schema: 'tft-trait-flex-v1', revision: 'units-v1', publicationRevision: 'site-v1', set: 18, patch: '18.3',
    level: 8, boardSlots: 8, units, traits: [trait('a', [2, 4]), trait('b', [2, 4]), trait('c', [2, 3]),
      trait('d', [2]), trait('unique', [1]), trait('mixed', [4, 1, 2, 2, 4])],
    teamPlanner: { format: 'tft-team-planner-v2', slots: 10, set: 18,
      unitCodes: Object.fromEntries(units.map((unit, index) => [unit.api, (index + 1).toString(16).padStart(3, '0')])) } };
}
const settings = { tank: 'tank', carry: 'carry', preference: 'breadth', minFrontline: 3, allowFive: false, locked: [] };

function soloAnchorFixture() {
  const raw = fixture();
  raw.traits.push({ api: 'tank-solo', name: 'Tank solo', levels: [1] }, { api: 'carry-solo', name: 'Carry solo', levels: [1] });
  for (const unit of raw.units) {
    unit.traitApis = unit.traitApis.filter(api => !['a', 'b', 'unique'].includes(api));
    if (unit.slug === 'tank') unit.traitApis = ['a', 'tank-solo'];
    if (unit.slug === 'carry') unit.traitApis = ['b', 'carry-solo'];
    unit.traitContributions = Object.fromEntries(unit.traitApis.map(api => [api, 1]));
  }
  return raw;
}

function lobbyFixture() {
  const raw = fixture();
  const template = raw.units.find(unit => unit.slug === 'carry');
  raw.units.push({ ...template, api: 'other-four', slug: 'other-four', name: 'OTHER FOUR', range: 2, backline: false,
    traitApis: [], traitContributions: {} });
  raw.teamPlanner.unitCodes = Object.fromEntries(raw.units.map((unit, index) => [unit.api, (index + 1).toString(16).padStart(3, '0')]));
  raw.shop = { costOdds: [.15, .20, .32, .30, .03], championCounts: [1, 2, 3, 4, 5].map(cost => raw.units.filter(unit => unit.cost === cost).length),
    bagCopies: [30, 25, 18, 10, 9], refreshCost: 2, visibleSlots: [4, 5], sources: [] };
  const inference = { tank: 1.4, carry: .6, 'front-a': .6, 'duo-a': .2, 'other-four': 1, elder: 3 };
  const profile = (id, rank, rankLabel, factor) => ({ id, patch: '18.3', rank, rankLabel,
    fetchedAt: '2026-10-11T14:00:00+00:00', updatedAt: '2026-10-11T13:00:00+00:00', sourceUrl: 'https://tactics.tools/',
    units: raw.units.map(unit => ({ api: unit.api, slug: unit.slug, name: unit.name, cost: unit.cost,
      playRatePerLobby: (inference[unit.slug] || 0) * factor * 8 / 3,
      meanCopiesWhenPlayed: 3, starRates: [0, 1, 0], copiesPerPlayer: (inference[unit.slug] || 0) * factor })) });
  raw.lobbyStats = { schema: 'tft-lobby-stats-v1', profiles: [profile('emerald-profile', 'emerald_plus', 'Emerald+', 1),
    profile('gold-profile', 'gold', 'Gold', .5)], defaultProfile: 'emerald-profile' };
  return raw;
}

async function checkLobbyPools() {
  const h = interfaceHarness(), ctx = h.context, raw = lobbyFixture(), snapshot = JSON.stringify(raw), catalog = ctx.tftFlexCatalog(raw);
  const profiles = ctx.tftFlexLobbyProfiles(catalog);
  assert.equal(profiles.length, 2, 'Both same-patch rank profiles are available');
  assert.equal(ctx.tftFlexLobbyProfile(catalog).id, 'emerald-profile', 'The cached default profile is selected initially');
  assert.equal(ctx.tftFlexLobbyProfile(catalog, 'gold-profile').id, 'gold-profile');
  assert.equal(ctx.tftFlexLobbyProfile(catalog, 'missing').id, 'emerald-profile', 'An unavailable saved selection falls back to the cached default');
  const profile = profiles.find(profile => profile.id === 'emerald-profile');
  const pool = ctx.tftFlexLobbyPool(catalog, profile, { players: 7, development: .5, overrides: {} });
  assert.equal(pool.length, catalog.units.length, 'The estimate includes the full catalog rather than just the target board or selected cost filter');
  const held = (units, slug) => units.find(unit => unit.api === catalog.unitsBySlug.get(slug).api).heldCopies;
  assert.equal(held(pool, 'tank'), 5, 'Expected holdings round 1.4 × 7 × 0.5 to the nearest integer');
  assert.equal(held(pool, 'carry'), 2); assert.equal(held(pool, 'front-a'), 2); assert.equal(held(pool, 'duo-a'), 1);
  assert.equal(held(pool, 'other-four'), 4, 'A non-target champion contributes to its tier’s held population');
  assert.equal(held(pool, 'elder'), 9, 'Inferred holdings never exceed the individual bag size');
  assert(pool.every(unit => Number.isInteger(unit.heldCopies) && unit.heldCopies >= 0 && unit.heldCopies <= catalog.shop.bagCopies[unit.cost - 1]));
  const overrides = ctx.tftFlexLobbyPool(catalog, profile, { players: 7, development: .5, overrides: { tank: 0, carry: 20, 'other-four': -4 } });
  assert.equal(held(overrides, 'tank'), 0, 'An explicit zero replaces inferred copies instead of being treated as an empty input');
  assert.equal(held(overrides, 'carry'), 10, 'Overrides clamp to the target’s bag size'); assert.equal(held(overrides, 'other-four'), 0);
  const noPlayers = ctx.tftFlexLobbyPool(catalog, profile, { players: 0, development: 1, overrides: { 'other-four': 3 } });
  assert.equal(held(noPlayers, 'other-four'), 3, 'Scouted total holdings remain explicit even if expected opponent count is zero');
  assert(noPlayers.filter(unit => unit.api !== 'other-four').every(unit => unit.heldCopies === 0));
  const noDevelopment = ctx.tftFlexLobbyPool(catalog, profile, { players: 7, development: 0, overrides: {} });
  assert(noDevelopment.every(unit => unit.heldCopies === 0), 'Zero development yields the uncontested starting pool');
  const lowerRank = ctx.tftFlexLobbyPool(catalog, profiles.find(profile => profile.id === 'gold-profile'), { players: 7, development: .5, overrides: { tank: 0 } });
  assert.equal(held(lowerRank, 'carry'), 1); assert.equal(held(lowerRank, 'tank'), 0, 'Manual scouts take precedence in any selected rank profile');
  const stale = lobbyFixture(); stale.lobbyStats.profiles.push({ ...stale.lobbyStats.profiles[0], id: 'old-patch', patch: '18.2' });
  assert.equal(ctx.tftFlexLobbyProfiles(ctx.tftFlexCatalog(stale)).length, 2, 'Other gameplay patches cannot supply the typical-lobby estimate');
  const fourthStar = lobbyFixture(), row = fourthStar.lobbyStats.profiles[0].units.find(unit => unit.slug === 'duo-a');
  row.starRates = [1, 0, 0, 0]; row.meanCopiesWhenPlayed = 1; row.playRatePerLobby = row.copiesPerPlayer * 8;
  assert.equal(ctx.tftFlexLobbyProfiles(ctx.tftFlexCatalog(fourthStar)).length, 2, 'Real four-star row shapes remain valid cached profiles');
  assert.equal(ctx.tftFlexLobbyProfiles(ctx.tftFlexCatalog(fixture())).length, 0, 'Older catalogs keep baseline support without fabricating a lobby profile');
  const mismatch = lobbyFixture(); mismatch.patch = '18.4';
  assert.equal(ctx.tftFlexLobbyProfiles(ctx.tftFlexCatalog(mismatch)).length, 0, 'A new patch requires matching cached statistics');
  const incomplete = lobbyFixture(); incomplete.lobbyStats.profiles[0].units.pop();
  const incompleteCatalog = ctx.tftFlexCatalog(incomplete);
  assert.equal(ctx.tftFlexLobbyProfiles(incompleteCatalog).length, 1, 'A profile must account for every champion in the pool');
  assert.equal(ctx.tftFlexLobbyProfile(incompleteCatalog).id, 'gold-profile', 'A valid rank profile can replace an incomplete cached default');
  assert.throws(() => ctx.tftFlexLobbyPool(catalog, profile, { overrides: { tank: 1.5 } }), /whole scouted copies/);
  const board = ctx.tftFlexEvaluate(catalog, ['tank', 'carry', 'front-a', 'front-b', 'duo-a', 'duo-b', 'front-d', 'back-b'], settings);
  const targets = ctx.tftFlexLobbyTargets(board);
  assert.equal(targets.length, board.units.length); assert(targets.every(target => target.copies === (target.cost === 4 ? 3 : 1)));
  assert(!targets.some(target => target.api === 'other-four'), 'Scouted unrelated holdings are not purchased as targets');
  const baseline = ctx.tftFlexGoldEstimate(catalog.shop, ctx.tftFlexGoldRequirements(board));
  const emptyLobby = ctx.tftFlexLobbyEstimate(catalog.shop, noDevelopment, targets);
  assert(Math.abs(emptyLobby.totalGold - baseline.totalGold) < 1e-6, 'The all-zero held-copy pool matches the uncontested exact expectation');
  const nonTargetHeld = ctx.tftFlexLobbyEstimate(catalog.shop, noPlayers, targets);
  assert(nonTargetHeld.totalGold < emptyLobby.totalGold, 'Copies held of another same-tier champion reduce the denominator and improve target availability');
  assert.equal(nonTargetHeld.purchaseGold, emptyLobby.purchaseGold, 'Other players’ holdings do not change the copies the player buys');
  assert.equal(JSON.stringify(raw), snapshot, 'Profile selection and pool construction preserve published catalog statistics');
}

async function checkSoloAnchorSearchAndSwaps() {
  const ctx = algorithmContext, raw = soloAnchorFixture(), snapshot = JSON.stringify(raw), catalog = ctx.tftFlexCatalog(raw);
  const kept = ['front-a', 'front-b', 'duo-a', 'duo-b', 'front-d', 'back-b'];
  for (const [preference, expectedScore] of [['breadth', 16], ['vertical', 8]]) {
    const options = { ...settings, preference, yieldTask: async () => {} };
    const evaluated = ctx.tftFlexEvaluate(catalog, ['tank', 'carry', ...kept], options);
    assert(evaluated.valid); assert.equal(evaluated.activeCount, 5); assert.equal(evaluated.extraTiers, 1);
    assert.equal(evaluated.score, expectedScore, 'Solo activation and the next mixed trait tier use the chosen formula');
    assert.deepEqual(plain(evaluated.anchorCoverage.tank).map(trait => trait.api), ['tank-solo']);
    assert.deepEqual(plain(evaluated.anchorCoverage.carry).map(trait => trait.api), ['carry-solo']);
    const exact = await ctx.tftFlexSearch(catalog, { ...options, locked: kept });
    assert.equal(exact.boards.length, 1); assert.equal(exact.expanded, 0, 'An exact-eight locked seed is evaluated without requiring trait partners');
    assert.equal(exact.boards[0].score, expectedScore);
    const searched = await ctx.tftFlexSearch(catalog, options);
    assert(searched.boards.length, 'Normal search retains solo-covered anchors without any shared-trait partner in the pool');
    assert(searched.boards.every(board => board.anchorCoverage.tank.some(trait => trait.api === 'tank-solo') &&
      board.anchorCoverage.carry.some(trait => trait.api === 'carry-solo')));
    assert(searched.boards.every(board => !board.activeTraits.some(trait => ['a', 'b'].includes(trait.api))), 'The anchors’ two-unit traits stay inactive');
    const withPartners = soloAnchorFixture();
    for (const [slug, source, trait] of [['tank-partner', 'front-a', 'a'], ['carry-partner', 'back-b', 'b']]) {
      const template = withPartners.units.find(unit => unit.slug === source);
      withPartners.units.push({ ...template, api: slug, slug, name: slug.toUpperCase(), traitApis: [trait], traitContributions: { [trait]: 1 } });
    }
    const partneredCatalog = ctx.tftFlexCatalog(withPartners);
    const partnered = ctx.tftFlexEvaluate(partneredCatalog,
      ['tank', 'carry', 'front-a', 'front-b', 'duo-a', 'duo-b', 'tank-partner', 'carry-partner'], options);
    const swaps = await ctx.tftFlexSwaps(partneredCatalog, partnered, { ...options, swapLimit: 24, maxScoreLoss: 100 });
    for (const [slug, key, api] of [['tank-partner', 'tank', 'a'], ['carry-partner', 'carry', 'b']]) {
      const swap = swaps.swaps.find(swap => swap.removed.length === 1 && swap.removed[0].slug === slug);
      assert(swap, 'A former shared-trait partner can be removed for ' + key);
      assert(swap.board.valid && swap.board.anchorCoverage[key].some(trait => trait.api === key + '-solo'));
      assert(!swap.board.activeTraits.some(trait => trait.api === api), 'The solo-covered anchor remains legal after losing its shared trait');
      const change = swap.traitChanges.find(change => change.api === api);
      assert.equal(change.beforeBreakpoint, 2); assert.equal(change.afterBreakpoint, null);
      assert.equal(change.beforeCount, 2); assert.equal(change.afterCount, 1, 'Losing a partner shows the remaining anchor contribution');
    }
  }
  assert.equal(JSON.stringify(raw), snapshot);
  const h = interfaceHarness(); h.context.api = async () => soloAnchorFixture(); await h.context.loadTftFlex();
  const { board, card } = await openFirstBoard(h);
  assert(card.textContent.includes('Active traits') && !card.textContent.includes('Active shared traits'));
  assert(card.textContent.includes('1 Tank solo') && card.textContent.includes('1 Carry solo'), 'Solo coverage and active chips are visible on the board');
  const soloChip = card.querySelectorAll('.tft-comp-trait').find(chip => chip.textContent.includes('Tank solo'));
  assert(soloChip.title.includes('breakpoints 1'), 'A solo breakpoint is included in the chip’s explanation');
  const tooltip = h.context.tftFlexUnitTooltip(board.units.find(unit => unit.slug === 'tank'), board).map(node => node.textContent).join(' ');
  assert(tooltip.includes('Reached 1') && tooltip.includes('core coverage') && !tooltip.includes('excluded'), 'Solo activation is described as valid scoring and coverage');
  const mixedTooltip = h.context.tftFlexUnitTooltip(h.flex.catalog.unitsBySlug.get('duo-a'), board).map(node => node.textContent).join(' ');
  assert(mixedTooltip.includes('1 / 2 / 4'), 'Mixed trait tooltips retain the solo breakpoint before shared tiers');
}

function multiAnchorFixture({ exactRoster = false } = {}) {
  const raw = fixture();
  if (exactRoster) {
    raw.units = raw.units.filter(unit => ['tank', 'carry', 'front-a', 'front-b', 'duo-a', 'duo-b', 'back-b', 'connect'].includes(unit.slug));
    raw.units.find(unit => unit.slug === 'front-a').cost = 4;
    raw.units.find(unit => unit.slug === 'back-b').cost = 4;
  } else {
    for (const [slug, source] of [['second-tank', 'front-a'], ['second-carry', 'bridge']]) {
      const original = raw.units.find(unit => unit.slug === source);
      raw.units.push({ ...original, api: slug, slug, name: slug.toUpperCase(), cost: 4 });
    }
  }
  raw.teamPlanner.unitCodes = Object.fromEntries(raw.units.map((unit, index) => [unit.api, (index + 1).toString(16).padStart(3, '0')]));
  return raw;
}

async function checkBrowsingAndSorting() {
  const ctx = algorithmContext, raw = multiAnchorFixture(), snapshot = JSON.stringify(raw), catalog = ctx.tftFlexCatalog(raw);
  const options = { ...settings, tank: 'all', carry: 'all', beamWidth: 800, yieldTask: async () => {} };
  const progress = [];
  const browsed = await ctx.tftFlexBrowse(catalog, { ...options, onProgress: update => progress.push(plain(update)) });
  assert.equal(browsed.pairsTotal, 4); assert.equal(browsed.pairsDone, 4); assert.equal(browsed.cancelled, false);
  assert(browsed.boards.length > 6, 'All-pair browsing provides more than a single pair’s shortlist');
  assert.equal(new Set(browsed.boards.map(board => board.id)).size, browsed.boards.length, 'A roster appears once even when several anchor pairs generate it');
  assert.equal(progress.at(-1).pairsDone, 4); assert.equal(progress.at(-1).pairsTotal, 4);
  assert.equal(progress.at(-1).expanded, browsed.expanded); assert.equal(progress.at(-1).boardsFound, browsed.boards.length);
  assert(progress.every((update, index) => !index || update.pairsDone >= progress[index - 1].pairsDone && update.expanded >= progress[index - 1].expanded),
    'Progress advances with completed pairs and actual search work');
  let independentlyExpanded = 0;
  const independentRosters = new Set();
  for (const tank of catalog.units.filter(unit => unit.cost === 4 && unit.frontline))
    for (const carry of catalog.units.filter(unit => unit.cost === 4 && unit.backline)) {
      const pair = await ctx.tftFlexSearch(catalog, { ...options, tank: tank.slug, carry: carry.slug });
      independentlyExpanded += pair.expanded; pair.boards.forEach(board => independentRosters.add(board.id));
    }
  assert.equal(browsed.expanded, independentlyExpanded, 'Browsing reports the actual work from each bounded search');
  assert.deepEqual([...browsed.boards.map(board => board.id)].sort(), [...independentRosters].sort(), 'Browsing merges every pair’s valid shortlist');
  for (const board of browsed.boards) {
    assert(board.anchorPairs.length, 'Every merged board records its generating anchor pairs');
    assert.equal(new Set(board.anchorPairs.map(pair => pair.tank + '|' + pair.carry)).size, board.anchorPairs.length);
    assert(board.anchorPairs.every(pair => ctx.tftFlexEvaluate(catalog, board.units, { ...settings, ...pair }).valid),
      'Each recorded anchor pair belongs to the roster and has activated trait coverage');
    assert(ctx.tftFlexEvaluate(catalog, board.units, { ...settings, tank: board.mainTank, carry: board.mainCarry }).valid);
  }
  const repeated = await ctx.tftFlexBrowse(catalog, options);
  assert.deepEqual(plain(repeated), plain(browsed), 'Cross-pair aggregation is deterministic');
  const allByNull = await ctx.tftFlexBrowse(catalog, { ...options, tank: null, carry: null });
  assert.deepEqual(plain(allByNull), plain(browsed), 'Missing anchor selections browse all eligible pairs');
  for (const filter of [{ tank: 'tank', carry: 'all' }, { tank: 'all', carry: 'carry' }]) {
    const filtered = await ctx.tftFlexBrowse(catalog, { ...options, ...filter });
    assert.equal(filtered.pairsTotal, 2); assert.equal(filtered.pairsDone, 2);
    assert(filtered.boards.length && filtered.boards.every(board => board.anchorPairs.every(pair =>
      filter.tank === 'all' ? pair.carry === filter.carry : pair.tank === filter.tank)), 'A single-anchor filter keeps the other anchor flexible');
  }
  const pinned = await ctx.tftFlexBrowse(catalog, { ...options, tank: 'tank', carry: 'carry' });
  const direct = await ctx.tftFlexSearch(catalog, { ...options, tank: 'tank', carry: 'carry' });
  assert.equal(pinned.pairsTotal, 1);
  assert.deepEqual(pinned.boards.map(board => board.id).sort(), direct.boards.map(board => board.id).sort(), 'Two filters preserve the direct pair shortlist');
  const deduped = await ctx.tftFlexBrowse(ctx.tftFlexCatalog(multiAnchorFixture({ exactRoster: true })), options);
  assert.equal(deduped.pairsTotal, 4); assert.equal(deduped.boards.length, 1);
  assert.equal(deduped.boards[0].anchorPairs.length, 4, 'An identical board merges all four valid anchor pairs');
  const cancelledBefore = await ctx.tftFlexBrowse(catalog, { ...options, cancelled: () => true });
  assert(cancelledBefore.cancelled && cancelledBefore.pairsDone === 0 && cancelledBefore.expanded === 0, 'Cancellation can stop before any pair search');
  let stop = false;
  const cancelledDuring = await ctx.tftFlexBrowse(catalog, { ...options, cancelled: () => stop,
    onProgress: update => { if (update.pairsDone >= 1) stop = true; } });
  assert(cancelledDuring.cancelled && cancelledDuring.pairsDone === 1 && cancelledDuring.pairsTotal === 4, 'Cancellation stops the pair loop without starting the next pair');
  const owned = await ctx.tftFlexBrowse(catalog, { ...options, locked: ['duo-a'] });
  assert(owned.boards.length && owned.boards.every(board => board.units.some(unit => unit.slug === 'duo-a')), 'All-pair browsing preserves owned units across every pair');
  await assert.rejects(() => ctx.tftFlexBrowse(catalog, { ...options, minCost: 3, locked: ['duo-a'] }), /minimum unit cost/,
    'A conflicting owned unit is reported before checking all pairs');
  assert.equal(JSON.stringify(raw), snapshot, 'Pair browsing leaves immutable catalog and planner data untouched');

  const sortBoards = [
    { id: 'a', score: 20, activeCount: 6, extraTiers: 2, frontlineCount: 3, units: [{ cost: 4 }, { cost: 4 }, { cost: 3 }], gold: { totalGold: 100.2 }, lobbyGold: { totalGold: 120 } },
    { id: 'b', score: 18, activeCount: 5, extraTiers: 3, frontlineCount: 4, units: [{ cost: 4 }, { cost: 4 }, { cost: 4 }, { cost: 1 }], gold: { totalGold: 80.9 }, lobbyGold: { totalGold: 100.1 } },
    { id: 'c', score: 20, activeCount: 7, extraTiers: 1, frontlineCount: 2, units: [{ cost: 4 }, { cost: 4 }, { cost: 2 }], gold: { totalGold: 120.1 }, lobbyGold: { totalGold: 100.2 } },
    { id: 'd', score: 10, activeCount: 2, extraTiers: 0, frontlineCount: 1, units: [{ cost: 4 }, { cost: 4 }, { cost: 1 }] },
  ];
  const sortSnapshot = JSON.stringify(sortBoards);
  const ids = (field, direction) => plain(ctx.tftFlexSortedBoards(sortBoards, field, direction)).map(board => board.id);
  assert.deepEqual(ids('gold', 'asc'), ['b', 'a', 'c', 'd']);
  assert.deepEqual(ids('gold', 'desc'), ['c', 'a', 'b', 'd'], 'Missing gold stays last in either direction');
  assert.deepEqual(ids('lobbyGold', 'asc'), ['b', 'c', 'a', 'd'], 'Typical-lobby sorting uses its own unrounded estimate');
  assert.deepEqual(ids('lobbyGold', 'desc'), ['a', 'c', 'b', 'd'], 'Missing typical-lobby gold stays last in either direction');
  assert.deepEqual(ids('purchaseGold', 'asc'), ['d', 'c', 'a', 'b'], 'Unit-purchase sorting counts three copies for every 4-cost');
  assert.deepEqual(ids('frontlineCount', 'desc'), ['b', 'a', 'c', 'd']);
  assert.deepEqual(ids('activeCount', 'desc'), ['c', 'a', 'b', 'd']);
  assert.deepEqual(ids('extraTiers', 'desc'), ['b', 'a', 'c', 'd']);
  assert.equal(ids('fourCosts', 'desc')[0], 'b');
  assert.deepEqual(ids('score', 'desc'), ['c', 'a', 'b', 'd'], 'Trait score preserves the existing full-precision tie rules');
  assert.equal(ids('score', 'asc')[0], 'd');
  const precision = [{ ...sortBoards[0], id: 'near-a', gold: { totalGold: 100.2 } }, { ...sortBoards[0], id: 'near-b', gold: { totalGold: 100.1 } }];
  assert.equal(ctx.tftFlexSortedBoards(precision, 'gold', 'asc')[0].id, 'near-b', 'Gold sorting uses exact expectations before display rounding');
  assert.deepEqual(plain(ctx.tftFlexSortedBoards(sortBoards.slice().reverse(), 'gold', 'asc')).map(board => board.id), ids('gold', 'asc'),
    'Sorting does not depend on pair arrival order');
  assert.equal(JSON.stringify(sortBoards), sortSnapshot, 'Sorting preserves board objects and their source order');
}

function costFixture() {
  const raw = fixture();
  for (const unit of raw.units) if (unit.cost < 4) unit.cost = 3;
  raw.units.find(unit => unit.slug === 'duo-a').cost = 1;
  raw.units.find(unit => unit.slug === 'duo-b').cost = 2;
  for (const [slug, source] of [['alt-front', 'front-a'], ['alt-bridge', 'bridge'], ['alt-connect', 'connect']]) {
    const unit = raw.units.find(unit => unit.slug === source);
    raw.units.push({ ...unit, api: slug, slug, name: slug.toUpperCase() });
  }
  return raw;
}

function coreCostFixture() {
  const raw = fixture();
  for (const unit of raw.units) if (unit.cost < 4) unit.cost = 1;
  const clone = (slug, source, cost) => {
    const original = raw.units.find(unit => unit.slug === source);
    const unit = { ...original, api: slug, slug, name: slug.toUpperCase(), cost,
      traitApis: [...original.traitApis], traitContributions: { ...original.traitContributions } };
    raw.units.push(unit); return unit;
  };
  for (const cost of [2, 3]) for (const suffix of ['a', 'b']) {
    clone(`tank${cost}-${suffix}`, 'tank', cost);
    clone(`carry${cost}-${suffix}`, 'carry', cost);
  }
  for (const [slug, source] of [['four-front-a', 'front-a'], ['four-front-b', 'front-b'],
                               ['four-back-a', 'duo-a'], ['four-back-b', 'back-b']]) {
    const unit = clone(slug, source, 4);
    // Attractive four-costs make the unrestricted optimum exceed the cap.
    for (let index = 0; index < 2; ++index) {
      const api = `${slug}-solo-${index}`;
      raw.traits.push({ api, name: api.toUpperCase(), levels: [1], icon: '/trait.png' });
      unit.traitApis.push(api); unit.traitContributions[api] = 1;
    }
  }
  raw.teamPlanner.unitCodes = Object.fromEntries(raw.units.map((unit, index) => [unit.api, (index + 1).toString(16).padStart(3, '0')]));
  return raw;
}

async function checkCoreCosts() {
  const ctx = algorithmContext, raw = coreCostFixture(), snapshot = JSON.stringify(raw), catalog = ctx.tftFlexCatalog(raw);
  const fours = ['four-front-a', 'four-front-b', 'four-back-a', 'four-back-b', 'tank', 'carry'];
  const cheap = ['front-a', 'front-b', 'front-d', 'duo-a', 'back-b', 'bridge'];
  const fourCount = board => board.units.filter(unit => unit.cost === 4).length;
  assert.equal(ctx.tftFlexOptions(catalog, settings).cost, 4, 'Earlier engine callers retain four-cost cores');
  const uncappedUnits = ['tank', 'carry', ...fours.slice(0, 4), 'front-a', 'front-b'];
  const uncapped = ctx.tftFlexEvaluate(catalog, uncappedUnits, settings);
  assert(uncapped.valid && fourCount(uncapped) === 6, 'Four-cost cores retain the existing uncapped support pool');
  for (const cost of [2, 3]) {
    const cap = cost === 2 ? 2 : 3;
    const options = { ...settings, cost, tank: `tank${cost}-a`, carry: `carry${cost}-a`,
      beamWidth: 800, yieldTask: async () => {} };
    const legal = [options.tank, options.carry, ...fours.slice(0, cap), ...cheap.slice(0, 6 - cap)];
    const overCap = [options.tank, options.carry, ...fours.slice(0, cap + 1), ...cheap.slice(0, 5 - cap)];
    const legalBoard = ctx.tftFlexEvaluate(catalog, legal, options);
    assert.equal(legalBoard.coreCost, cost, 'Evaluation stamps the board with the selected gold target tier');
    assert(legalBoard.valid && fourCount(legalBoard) === cap, 'Evaluation accepts exactly the stated cap for a ' + cost + '-cost core');
    assert.equal(ctx.tftFlexEvaluate(catalog, overCap, options).valid, false,
      'A complete, covered roster is invalid when its total four-cost count exceeds the cap');
    assert.throws(() => ctx.tftFlexOptions(catalog, { ...options, tank: 'tank' }), /frontline anchor/,
      'The frontline anchor must match the selected core cost');
    assert.throws(() => ctx.tftFlexOptions(catalog, { ...options, carry: `carry${cost === 2 ? 3 : 2}-a` }), /backline carry/,
      'Mixed-cost anchor pairs are rejected');
    assert.throws(() => ctx.tftFlexOptions(catalog, { ...options, tank: options.carry }), /frontline anchor/,
      'A matching-cost backline carry cannot occupy the frontline anchor role');
    assert.throws(() => ctx.tftFlexOptions(catalog, { ...options, carry: options.tank }), /backline carry/,
      'A matching-cost frontliner cannot occupy the backline anchor role');
    assert.throws(() => ctx.tftFlexOptions(catalog, { ...options, minCost: cost + 1 }), /minimum|core|cost/i,
      'Engine callers cannot exclude their own lower-cost anchors with a higher minimum');
    const exact = await ctx.tftFlexSearch(catalog, { ...options, locked: legal.slice(2) });
    assert.equal(exact.boards.length, 1); assert.equal(exact.expanded, 0);
    assert.equal(exact.boards[0].id, legalBoard.id, 'An exact-eight locked seed is still accepted at the cap');
    await assert.rejects(() => ctx.tftFlexSearch(catalog, { ...options, locked: overCap.slice(2) }), /4-cost/i,
      'An over-cap complete locked seed is rejected before returning boards');
    await assert.rejects(() => ctx.tftFlexSearch(catalog, { ...options, locked: fours.slice(0, cap + 1) }), /4-cost/i,
      'Partial locked seeds count toward the cap before expansion');
    const result = await ctx.tftFlexSearch(catalog, options);
    assert(result.boards.length && result.expanded > 0);
    assert(result.boards.every(board => board.valid && fourCount(board) <= cap), 'Expanded boards obey the total four-cost cap');
    assert.equal(fourCount(result.boards[0]), cap, 'The search can fill the entire allowed four-cost budget');
    const independent = enumerate(ctx, catalog, options).filter(board => fourCount(board) <= cap);
    assert.equal(result.boards[0].score, independent[0].score, 'Cost-capped search matches complete enumeration on the small fixture');
    const kept = await ctx.tftFlexSearch(catalog, { ...options, locked: [fours[0]] });
    assert(kept.boards.length && kept.boards.every(board => board.units.some(unit => unit.slug === fours[0]) && fourCount(board) <= cap),
      'A four-cost lock consumes one place in the cap throughout expansions');
    const swapped = await ctx.tftFlexSwaps(catalog, legalBoard,
      { ...options, swapLimit: 24, maxScoreLoss: 100, maxExpansions: 120000 });
    assert(swapped.swaps.length && swapped.swaps.every(swap => swap.board.valid && fourCount(swap.board) <= cap));
    const replacingFour = await ctx.tftFlexSwaps(catalog, legalBoard, { ...options,
      locked: legal.slice(2).filter(slug => slug !== fours[0]), swapLimit: 24, maxScoreLoss: 100 });
    assert(replacingFour.swaps.some(swap => swap.removed.length === 1 && swap.removed[0].cost === 4 &&
      swap.added.length === 1 && swap.added[0].cost === 4 && fourCount(swap.board) === cap),
      'Being at the cap still permits a four-cost support to replace another four-cost');
    assert(!swapped.swaps.some(swap => swap.removed.every(unit => unit.cost !== 4) && swap.added.some(unit => unit.cost === 4)),
      'A swap cannot add another four-cost while retaining all capped four-costs');
    const fixedSwap = await ctx.tftFlexSwaps(catalog, legalBoard,
      { ...options, locked: [fours[0]], swapLimit: 24, maxScoreLoss: 100 });
    assert(fixedSwap.swaps.every(swap => !swap.removed.some(unit => unit.slug === fours[0]) && fourCount(swap.board) <= cap));
    await assert.rejects(() => ctx.tftFlexSwaps(catalog,
      { ...legalBoard, units: overCap.map(slug => catalog.unitsBySlug.get(slug)) }, options), /valid board/i);
    const browsed = await ctx.tftFlexBrowse(catalog, { ...options, tank: 'all', carry: 'all' });
    assert.equal(browsed.pairsTotal, 4); assert.equal(browsed.pairsDone, 4);
    assert(browsed.boards.length && browsed.boards.every(board => fourCount(board) <= cap && board.anchorPairs.every(pair => {
      const tank = catalog.unitsBySlug.get(pair.tank), carry = catalog.unitsBySlug.get(pair.carry);
      return tank.cost === cost && tank.frontline && carry.cost === cost && carry.backline;
    })), 'All-pair browsing uses the selected cost and role for both anchors');
    const laterPair = { ...options, tank: `tank${cost}-b`, carry: `carry${cost}-b` };
    const laterSeed = [laterPair.tank, laterPair.carry, ...legal.slice(2)];
    const lockedBrowse = await ctx.tftFlexBrowse(catalog, { ...options, tank: 'all', carry: 'all', locked: laterSeed });
    assert.equal(lockedBrowse.boards.length, 1); assert.equal(lockedBrowse.expanded, 0);
    assert.deepEqual(plain(lockedBrowse.boards[0].anchorPairs), [{ tank: laterPair.tank, carry: laterPair.carry }],
      'An exact-eight owned roster can fit a later anchor pair without being rejected by the first pair');
    const targets = ctx.tftFlexLobbyTargets(legalBoard);
    assert(targets.every(target => target.copies === (target.cost === cost ? 9 : 1)),
      'Lower-cost profiles require nine copies of their own tier and one of every other tier');
  }
  assert.equal(JSON.stringify(raw), snapshot, 'Changing core cost leaves the complete published unit and shop catalog intact');
}

async function checkGenericCores() {
  const ctx = algorithmContext, raw = coreCostFixture();
  for (const [slug, source] of [['extra-four-front', 'front-a'], ['extra-four-back', 'duo-b'], ['ninth-four', 'bridge']]) {
    const template = raw.units.find(unit => unit.slug === source);
    raw.units.push({ ...template, api: slug, slug, name: slug.toUpperCase(), cost: 4 });
  }
  const snapshot = JSON.stringify(raw), catalog = ctx.tftFlexCatalog(raw);
  const normalize = values => [...new Set(values)].sort();
  const containsCores = (board, cores) => {
    assert.deepEqual(plain(board.cores), normalize(cores));
    assert.equal(board.coreCost, catalog.unitsBySlug.get(cores[0]).cost,
      'Searches and swaps retain the board tier used for every unit’s gold target');
    assert.deepEqual(Object.keys(board.coreCoverage).sort(), normalize(cores));
    for (const slug of cores) {
      assert(board.units.some(unit => unit.slug === slug), 'Each chosen core appears on the board');
      assert(board.coreCoverage[slug].length && board.coreCoverage[slug].every(trait =>
        catalog.unitsBySlug.get(slug).traitApis.includes(trait.api)), 'Each core has its own activated trait coverage');
    }
    for (const field of ['mainTank', 'mainCarry', 'anchorCoverage', 'anchorPairs'])
      assert(!Object.hasOwn(board, field), 'Explicit cores do not fabricate legacy role-pair fields');
  };
  for (const cost of [2, 3, 4]) {
    const base = { cost, minFrontline: 3, preference: 'breadth', locked: [], beamWidth: 800, yieldTask: async () => {} };
    const sameRoleSelections = cost === 4
      ? [['carry', 'four-back-a'], ['tank', 'four-front-a']]
      : [[`carry${cost}-a`, `carry${cost}-b`], [`tank${cost}-a`, `tank${cost}-b`]];
    for (const cores of sameRoleSelections) {
      const options = { ...base, cores }, result = await ctx.tftFlexSearch(catalog, options);
      assert(result.boards.length, 'Two ' + (cores[0].startsWith('carry') ? 'backline' : 'frontline') + ' cores are legal at cost ' + cost);
      for (const board of result.boards) {
        assert(board.valid && board.slotsUsed === 8 && board.frontlineCount >= 3,
          'The frontline minimum applies to the whole board instead of assigning roles to cores');
        containsCores(board, cores);
        assert(board.units.filter(unit => unit.cost === 4).length <= (cost === 2 ? 2 : cost === 3 ? 3 : 8));
      }
      const independent = enumerate(ctx, catalog, options);
      assert.equal(result.boards[0].score, independent[0].score, 'Generic core search matches full fixture enumeration for the selected role pattern');
    }
    const cores = cost === 4 ? ['tank', 'carry', 'four-back-a'] : [`tank${cost}-a`, `carry${cost}-a`, `carry${cost}-b`];
    const options = { ...base, cores };
    assert.deepEqual(plain(ctx.tftFlexOptions(catalog, { ...options, cores: [...cores].reverse().concat(cores[0]) }).cores), normalize(cores),
      'Explicit core lists normalize deterministically and deduplicate before counting');
    const result = await ctx.tftFlexSearch(catalog, options);
    assert(result.boards.length);
    result.boards.forEach(board => containsCores(board, cores));
    const browsed = await ctx.tftFlexBrowse(catalog, options);
    assert.deepEqual(browsed.boards.map(board => board.id), result.boards.map(board => board.id),
      'Explicit multiselect runs its one selected core search instead of enumerating role pairs');
    const board = result.boards[0];
    const swaps = await ctx.tftFlexSwaps(catalog, board, { ...options, swapLimit: 24, maxScoreLoss: 100 });
    assert(swaps.swaps.length);
    for (const swap of swaps.swaps) {
      assert(!swap.removed.some(unit => cores.includes(unit.slug)), 'No one- or two-unit support swap can sell any of three selected cores');
      containsCores(swap.board, cores);
      assert(swap.board.units.filter(unit => unit.cost === 4).length <= (cost === 2 ? 2 : cost === 3 ? 3 : 8));
    }
    const supports = board.units.filter(unit => !cores.includes(unit.slug)).map(unit => unit.slug);
    const seeded = await ctx.tftFlexSearch(catalog, { ...options, locked: [...supports, cores[0]] });
    assert.equal(seeded.boards.length, 1); assert.equal(seeded.expanded, 0);
    assert.equal(seeded.boards[0].id, board.id, 'Cores already listed among kept units count only once in an exact-eight seed');
    if (cost < 4) {
      const tooManyFours = catalog.units.filter(unit => unit.cost === 4).slice(0, cost + 1).map(unit => unit.slug);
      await assert.rejects(() => ctx.tftFlexSearch(catalog, { ...options, locked: tooManyFours }), /4-cost/i);
      assert.throws(() => ctx.tftFlexOptions(catalog, { ...options, minCost: cost + 1 }), /minimum|core|cost/i);
    }
  }
  const pair = { cost: 4, cores: ['tank', 'carry'], minFrontline: 3, locked: [], yieldTask: async () => {} };
  for (const cores of [[], ['tank'], ['tank', 'tank'], 'tank,carry', ['tank', 'missing-core']])
    assert.throws(() => ctx.tftFlexOptions(catalog, { ...pair, cores }), /core|champion|array/i,
      'Invalid explicit selections produce a clear error instead of silently browsing all units');
  assert.throws(() => ctx.tftFlexOptions(catalog, { ...pair, cores: ['tank', 'carry2-a'] }), /core|cost/i);
  const eightCores = catalog.units.filter(unit => unit.cost === 4 && unit.slug !== 'ninth-four').map(unit => unit.slug);
  assert.equal(eightCores.length, 8);
  const eight = await ctx.tftFlexSearch(catalog, { ...pair, cores: eightCores });
  assert.equal(eight.boards.length, 1); assert.equal(eight.expanded, 0);
  assert.equal(eight.boards[0].units.length, 8); containsCores(eight.boards[0], eightCores);
  const noSupports = await ctx.tftFlexSwaps(catalog, eight.boards[0], { ...pair, cores: eightCores });
  assert.equal(noSupports.swaps.length, 0); assert.equal(noSupports.expanded, 0, 'A complete eight-core roster has no sellable supports');
  await assert.rejects(() => ctx.tftFlexSearch(catalog, { ...pair, cores: [...eightCores, 'ninth-four'] }), /more than 8|8 team slots/i);
  const roster = ['tank', 'carry', 'front-a', 'front-b', 'front-d', 'duo-a', 'duo-b', 'back-b'];
  const explicit = ctx.tftFlexEvaluate(catalog, roster, pair), legacy = ctx.tftFlexEvaluate(catalog, roster, settings);
  assert.equal(explicit.score, legacy.score); assert.equal(explicit.activeCount, legacy.activeCount); assert.equal(explicit.extraTiers, legacy.extraTiers);
  assert.equal(explicit.id, legacy.id, 'Generic cores preserve trait scoring and roster identity for an existing pair');
  assert.equal(JSON.stringify(raw), snapshot, 'Generic selection and support swaps preserve the complete immutable catalog');
}

async function checkAutomaticCoreBrowse() {
  const ctx = algorithmContext, raw = coreCostFixture();
  raw.units = raw.units.filter(unit => unit.cost !== 4 || ['tank', 'carry', 'four-front-a', 'four-back-a'].includes(unit.slug));
  const snapshot = JSON.stringify(raw), catalog = ctx.tftFlexCatalog(raw);
  const pairKey = pair => [...pair].sort().join('|');
  for (const cost of [2, 3, 4]) {
    const options = { cost, cores: [], minFrontline: 3, preference: 'breadth', locked: [], beamWidth: 800, yieldTask: async () => {} };
    const pool = catalog.units.filter(unit => unit.cost === cost);
    const pairs = pool.flatMap((unit, index) => pool.slice(index + 1).map(other => [unit.slug, other.slug].sort()));
    assert.equal(pairs.length, 6);
    const expected = new Map(); let expanded = 0;
    for (const cores of pairs) {
      const direct = await ctx.tftFlexSearch(catalog, { ...options, cores });
      assert(direct.boards.length, 'The fixture has complete boards for every same-tier pair, including matching roles');
      expanded += direct.expanded;
      for (const board of direct.boards) {
        if (!expected.has(board.id)) expected.set(board.id, new Set());
        expected.get(board.id).add(pairKey(cores));
      }
    }
    const progress = [], all = await ctx.tftFlexBrowse(catalog, { ...options, onProgress: update => progress.push(plain(update)) });
    assert.equal(all.pairsTotal, 6); assert.equal(all.pairsDone, 6); assert.equal(all.expanded, expanded);
    assert.equal(all.cancelled, false);
    assert.deepEqual([...all.boards.map(board => board.id)].sort(), [...expected.keys()].sort(), 'No selection merges all pair shortlists instead of assigning frontline/backline roles');
    assert.equal(new Set(all.boards.map(board => board.id)).size, all.boards.length, 'Multiple generated pairs never duplicate a roster');
    const observedPairs = new Set();
    for (const board of all.boards) {
      assert.equal(board.coreCost, cost, 'No-selection browsing stamps the requested tier on every merged board');
      assert(ctx.tftFlexLobbyTargets(board).every(target => target.copies ===
        (cost < 4 ? target.cost === cost ? 9 : 1 : target.cost === 4 ? 3 : 1)),
        'Automatic all-pair results include the same-tier supports in the full star target');
      assert(board.corePairs.length);
      const keys = board.corePairs.map(pairKey);
      assert.equal(new Set(keys).size, keys.length);
      assert.deepEqual([...keys].sort(), [...expected.get(board.id)].sort(), 'A deduplicated roster retains every pair that generated it');
      assert(keys.includes(pairKey(board.cores)), 'Each merged board keeps one of its generated pairs for support swaps');
      for (const cores of board.corePairs) {
        assert.deepEqual(plain(cores), [...cores].sort()); observedPairs.add(pairKey(cores));
        assert(ctx.tftFlexEvaluate(catalog, board.units, { ...options, cores }).valid);
      }
      assert(board.units.filter(unit => unit.cost === 4).length <= (cost === 2 ? 2 : cost === 3 ? 3 : 8));
    }
    assert.deepEqual([...observedPairs].sort(), pairs.map(pairKey).sort(), 'Automatic browsing includes every unordered pair without repeats or reversed duplicates');
    assert([...observedPairs].some(key => key.split('|').every(slug => catalog.unitsBySlug.get(slug).frontline)));
    assert([...observedPairs].some(key => key.split('|').every(slug => catalog.unitsBySlug.get(slug).backline)), 'Automatic pairs include two backline cores as well as two frontline cores');
    assert.equal(progress.length, 6); assert.equal(progress.at(-1).pairsTotal, 6); assert.equal(progress.at(-1).pairsDone, 6);
    assert.equal(progress.at(-1).expanded, all.expanded); assert.equal(progress.at(-1).boardsFound, all.boards.length);
    assert(progress.every((update, index) => !index || update.pairsDone > progress[index - 1].pairsDone && update.expanded >= progress[index - 1].expanded));
    const chosen = pool.find(unit => unit.backline).slug;
    const one = await ctx.tftFlexBrowse(catalog, { ...options, cores: [chosen] });
    assert(one.boards.every(board => board.coreCost === cost), 'One-core browsing retains the requested gold target tier');
    assert(one.boards.every(board => ctx.tftFlexLobbyTargets(board).every(target => target.copies ===
      (cost < 4 ? target.cost === cost ? 9 : 1 : target.cost === 4 ? 3 : 1))),
      'One-core browsing applies star targets to partners and same-tier supports alike');
    assert.equal(one.pairsTotal, 3); assert.equal(one.pairsDone, 3);
    const expectedOne = new Map();
    for (const [id, corePairs] of expected) {
      const matching = [...corePairs].filter(key => key.split('|').includes(chosen));
      if (matching.length) expectedOne.set(id, matching);
    }
    assert.deepEqual([...one.boards.map(board => board.id)].sort(), [...expectedOne.keys()].sort());
    assert(one.boards.every(board => board.units.some(unit => unit.slug === chosen) && board.corePairs.every(pair => pair.includes(chosen))), 'One selected champion is retained in every result and every generated pair');
    const partners = new Set(one.boards.flatMap(board => board.corePairs.flatMap(pair => pair.filter(slug => slug !== chosen))));
    assert.deepEqual([...partners].sort(), pool.filter(unit => unit.slug !== chosen).map(unit => unit.slug).sort(), 'A single core considers every same-cost partner, including its own role');
    const locked = await ctx.tftFlexBrowse(catalog, { ...options, cores: [chosen], locked: ['front-a'] });
    assert(locked.boards.length && locked.boards.every(board => board.units.some(unit => unit.slug === 'front-a')));
    const selectedBoard = one.boards[0], swaps = await ctx.tftFlexSwaps(catalog, selectedBoard,
      { ...options, cores: selectedBoard.cores, maxScoreLoss: 100 });
    assert(swaps.swaps.length && swaps.swaps.every(swap => selectedBoard.cores.every(slug => swap.board.units.some(unit => unit.slug === slug)) &&
      swap.board.units.filter(unit => unit.cost === 4).length <= (cost === 2 ? 2 : cost === 3 ? 3 : 8)), 'Browsable results preserve their generated core pair and the tier cap during swaps');
    const repeat = await ctx.tftFlexBrowse(catalog, options);
    assert.deepEqual(plain(repeat), plain(all), 'All-pair aggregation and representative cores are deterministic');
  }
  const exactCatalog = ctx.tftFlexCatalog(multiAnchorFixture({ exactRoster: true }));
  const fullRoster = exactCatalog.units.map(unit => unit.slug);
  const exact = await ctx.tftFlexBrowse(exactCatalog, { cost: 4, cores: [], locked: fullRoster, minFrontline: 3, yieldTask: async () => {} });
  assert.equal(exact.boards.length, 1); assert.equal(exact.expanded, 0); assert.equal(exact.boards[0].corePairs.length, 6,
    'A complete owned roster merges every same-tier pair without expanding supports');
  const laterRoster = ['tank2-b', 'carry2-b', 'tank', 'carry', 'front-a', 'front-b', 'duo-a', 'back-b'];
  const later = await ctx.tftFlexBrowse(catalog, { cost: 2, cores: [], locked: laterRoster, minFrontline: 3, yieldTask: async () => {} });
  assert.equal(later.boards.length, 1); assert.equal(later.expanded, 0);
  assert.deepEqual(plain(later.boards[0].corePairs), [['carry2-b', 'tank2-b']], 'Pair-plus-lock overflow is skipped until a later eligible pair can use the exact roster');
  const options = { cost: 2, cores: [], minFrontline: 3, yieldTask: async () => {} };
  const early = await ctx.tftFlexBrowse(catalog, { ...options, cancelled: () => true });
  assert(early.cancelled && early.pairsDone === 0 && early.expanded === 0);
  let stop = false;
  const interrupted = await ctx.tftFlexBrowse(catalog, { ...options, cancelled: () => stop, onProgress: () => { stop = true; } });
  assert(interrupted.cancelled && interrupted.pairsDone === 1 && interrupted.pairsTotal === 6, 'Cancellation prevents the next automatic pair search from starting');
  for (const cores of [[], ['carry2-a'], ['carry2-a', 'tank2-a']]) {
    let complete = false;
    const lastCallback = await ctx.tftFlexBrowse(catalog, { ...options, cores, cancelled: () => complete,
      onProgress: update => { if (update.pairsDone === update.pairsTotal) complete = true; } });
    assert(complete && lastCallback.cancelled && lastCallback.boards.length === 0,
      'Cancellation in the final progress callback is honored for zero, one and fixed multi-core selections');
  }
  for (const cores of ['tank2-a,carry2-a', ['missing-core'], ['tank']])
    await assert.rejects(() => ctx.tftFlexBrowse(catalog, { ...options, cores }), /core|champion|array|cost/i);
  await assert.rejects(() => ctx.tftFlexBrowse(catalog, { ...options, minCost: 3 }), /minimum|core|cost/i);
  await assert.rejects(() => ctx.tftFlexBrowse(catalog, { ...options, locked: ['tank', 'carry', 'four-front-a'] }), /4-cost/i);
  assert.equal(JSON.stringify(raw), snapshot, 'Automatic pair and partner browsing preserve the published catalog');
}

async function checkMinimumCost() {
  const raw = costFixture(), snapshot = JSON.stringify(raw), ctx = algorithmContext, catalog = ctx.tftFlexCatalog(raw);
  const mixed = ['tank', 'carry', 'duo-a', 'duo-b', 'front-a', 'front-b', 'front-d', 'back-b'];
  assert(ctx.tftFlexEvaluate(catalog, mixed, settings).valid, 'Default cost range preserves a board containing 1- and 2-costs');
  assert.equal(ctx.tftFlexEvaluate(catalog, mixed, { ...settings, minCost: 2 }).valid, false);
  assert.equal(ctx.tftFlexEvaluate(catalog, mixed, { ...settings, minCost: 3 }).valid, false);
  for (const minCost of [2, 3]) {
    const options = { ...settings, minCost, yieldTask: async () => {} };
    const result = await ctx.tftFlexSearch(catalog, options);
    assert(result.boards.length, 'The eligible pool finds boards for minimum cost ' + minCost);
    assert(result.boards.every(board => board.units.every(unit => unit.cost >= minCost && unit.cost <= 4)), 'Excluded costs never enter generated boards');
    const swaps = await ctx.tftFlexSwaps(catalog, result.boards[0], { ...options, maxScoreLoss: 100 });
    assert(swaps.swaps.length);
    assert(swaps.swaps.every(swap => swap.added.every(unit => unit.cost >= minCost) && swap.board.units.every(unit => unit.cost >= minCost)),
      'Support swaps obey the same minimum cost as full boards');
    const withFive = await ctx.tftFlexSearch(catalog, { ...options, allowFive: true, locked: ['elder'] });
    assert(withFive.boards.length && withFive.boards.every(board => board.units.length === 7 && board.units.every(unit => unit.cost >= minCost)),
      'Cost filters coexist with two-slot 5-cost supports');
  }
  await assert.rejects(() => ctx.tftFlexSearch(catalog, { ...settings, minCost: 2, locked: ['duo-a'] }), /Unlock DUO-A.*minimum unit cost/);
  await assert.rejects(() => ctx.tftFlexSearch(catalog, { ...settings, minCost: 3, locked: ['duo-b'] }), /Unlock DUO-B.*minimum unit cost/);
  assert.equal(ctx.tftFlexOptions(catalog, settings).minCost, 1, 'Earlier settings and links default to all costs');
  assert.equal(JSON.stringify(raw), snapshot, 'Cost filtering does not remove units from the catalog or its shop population');
  const h = interfaceHarness(); h.context.api = async () => costFixture();
  await h.context.loadTftFlex();
  assert.equal(h.element('tft-flex-min-cost').value, '1');
  h.element('tft-flex-min-cost').value = '3'; await h.element('tft-flex-min-cost').fire('change'); await settled(h);
  assert.equal(h.flex.minCost, 3);
  assert(h.flex.boards.length && h.flex.boards.every(board => board.units.every(unit => unit.cost >= 3)));
  assert(h.element('tft-flex-context').textContent.includes('3–4'));
  const cheapLock = () => h.element('tft-flex-lock-list').children.find(label => label.textContent.includes('DUO-A')).querySelector('input');
  assert(cheapLock().disabled, 'Excluded units cannot be newly selected as kept units');
  h.flex.locked = ['duo-a']; h.context.renderTftFlexControls(); await h.context.runTftFlexSearch();
  assert(h.flex.error.includes('Unlock DUO-A') && !cheapLock().disabled, 'An excluded kept unit produces a clear conflict and remains unlockable');
  const locked = cheapLock(); locked.checked = false; await locked.fire('change'); await settled(h);
  assert(locked.disabled && !h.flex.error && h.flex.boards.length, 'Unlocking the excluded unit restores valid filtered boards');
  h.element('tft-flex-min-cost').value = '2'; await h.element('tft-flex-min-cost').fire('change'); await settled(h);
  assert(h.flex.boards.every(board => board.units.every(unit => unit.cost >= 2)));
  h.element('tft-flex-min-cost').value = '1'; await h.element('tft-flex-min-cost').fire('change'); await settled(h);
  assert(!cheapLock().disabled, 'Allowing all costs restores low-cost kept-unit choices');
}

function checkScoring() {
  const raw = fixture(), snapshot = JSON.stringify(raw), ctx = algorithmContext, catalog = ctx.tftFlexCatalog(raw);
  assert.equal(JSON.stringify(raw), snapshot, 'Catalog normalization preserves published data');
  assert.deepEqual(plain(catalog.traitsByApi.get('mixed').levels), [1, 2, 4], 'Scoring keeps solo and distinct shared integer thresholds');
  assert.deepEqual(plain(catalog.traitsByApi.get('mixed').sharedLevels), [2, 4], 'Shared-only thresholds remain available as metadata');
  const measure = (api, count, preference = 'breadth') => {
    const counts = new Int16Array(catalog.traits.length); counts[catalog.traitsByApi.get(api).index] = count;
    return ctx.tftFlexMeasure(catalog, counts, preference);
  };
  for (const [preference, soloScore, nextScore, higherScore] of [['breadth', 3, 4, 5], ['vertical', 1, 4, 7]]) {
    assert.equal(measure('unique', 0, preference).score, 0, 'An absent solo trait is inactive');
    assert.equal(measure('unique', 1, preference).score, soloScore, 'A reached 1-unit trait scores once');
    assert.equal(measure('unique', 8, preference).score, soloScore, 'Extra units do not multiply a solo trait’s score');
    assert.equal(measure('unique', 1, preference).activeCount, 1);
    assert.equal(measure('unique', 1, preference).extraTiers, 0);
    assert.equal(measure('mixed', 0, preference).score, 0);
    assert.equal(measure('mixed', 1, preference).score, soloScore, 'The first [1, 2, 4] tier activates at one unit');
    assert.equal(measure('mixed', 2, preference).score, nextScore, 'A reached shared tier adds one tier above solo activation');
    assert.equal(measure('mixed', 3, preference).score, nextScore, 'Incomplete next tiers receive no reward');
    assert.equal(measure('mixed', 4, preference).score, higherScore, 'Duplicate thresholds do not add extra vertical tiers');
    assert.equal(measure('mixed', 4, preference).extraTiers, 2);
    assert.equal(measure('a', 1, preference).score, 0, 'A trait whose first breakpoint is two remains inactive at one unit');
  }
  assert.equal(measure('a', 8).score, measure('a', 4).score, 'Overflow beyond the last breakpoint adds no score');
  const isolated = fixture();
  isolated.units.push(...Array.from({ length: 6 }, (_, index) => ({ ...isolated.units[4], api: 'solo' + index,
    slug: 'solo' + index, traitApis: ['unique'], traitContributions: { unique: 1 } })));
  const isolatedCatalog = ctx.tftFlexCatalog(isolated);
  const soloCovered = ctx.tftFlexEvaluate(isolatedCatalog, ['tank', 'carry', ...Array.from({ length: 6 }, (_, index) => 'solo' + index)],
    { ...settings, minFrontline: 1 });
  assert.equal(soloCovered.valid, true, 'An anchor’s activated solo trait satisfies its coverage requirement');
  assert.deepEqual(plain(soloCovered.anchorCoverage.tank).map(trait => trait.api), ['unique']);
  assert.deepEqual(plain(soloCovered.anchorCoverage.carry).map(trait => trait.api), ['unique']);
  assert.equal(soloCovered.score, 3, 'A shared solo trait counts once while covering both owners');
  for (const unit of isolated.units.filter(unit => ['tank', 'carry'].includes(unit.slug))) {
    unit.traitApis = unit.traitApis.filter(api => api !== 'unique'); delete unit.traitContributions.unique;
  }
  const noSoloCatalog = ctx.tftFlexCatalog(isolated);
  const invalid = ctx.tftFlexEvaluate(noSoloCatalog, ['tank', 'carry', ...Array.from({ length: 6 }, (_, index) => 'solo' + index)],
    { ...settings, minFrontline: 1 });
  assert.equal(invalid.valid, false, 'Solo traits on unrelated supports cannot cover anchors without those traits');
  assert.equal(invalid.anchorCoverage.tank.length, 0);
  assert.equal(invalid.anchorCoverage.carry.length, 0);
  assert.equal(invalid.score, 3, 'An active unrelated solo trait still scores even though this roster fails anchor coverage');
  const supported = ctx.tftFlexEvaluate(noSoloCatalog, ['tank', 'carry', 'front-a', 'front-b', 'duo-a', 'duo-b', 'front-d', 'back-b'], settings);
  assert(supported.valid && supported.anchorCoverage.tank.some(trait => trait.api === 'a') && supported.anchorCoverage.carry.some(trait => trait.api === 'b'),
    'Anchors without solo traits become covered when their shared breakpoints activate');
  const thinRaw = fixture();
  thinRaw.units.push({ ...thinRaw.units[4], api: 'solo', slug: 'solo', traitApis: ['unique'], traitContributions: { unique: 1 } });
  const thinCatalog = ctx.tftFlexCatalog(thinRaw), thinUnits = ['tank', 'carry', 'connect', 'duo-a', 'duo-b', 'back-b', 'bridge', 'solo'];
  const thin = ctx.tftFlexEvaluate(thinCatalog, thinUnits, { ...settings, minFrontline: 1 });
  assert(thin.valid && thin.frontlineCount === 1, 'One frontline anchor is legal when the configurable minimum is one');
  assert.equal(ctx.tftFlexEvaluate(thinCatalog, thinUnits, settings).valid, false, 'The same roster fails the default three-frontline requirement');
  assert.throws(() => ctx.tftFlexCatalog({ ...raw, schema: 'other' }), /Unexpected trait catalog/);
  const duplicate = fixture(); duplicate.units.push(duplicate.units[0]);
  assert.throws(() => ctx.tftFlexCatalog(duplicate), /Duplicate units/);
  assert.throws(() => ctx.tftFlexOptions(catalog, { ...settings, carry: 'front-a' }), /backline carry/,
    'A melee or non-4-cost unit cannot be the backline anchor');
}

function enumerate(ctx, catalog, options) {
  const cores = options.cores || [options.tank, options.carry];
  const seed = [...new Set([...cores, ...(options.locked || [])])].map(slug => catalog.unitsBySlug.get(slug));
  const pool = catalog.units.filter(unit => !seed.includes(unit) && (options.allowFive || unit.cost <= 4));
  const result = [];
  function visit(index, selected, slots) {
    if (slots === 8) { const board = ctx.tftFlexEvaluate(catalog, selected, options); if (board.valid) result.push(board); return; }
    for (let next = index; next < pool.length; ++next) if (slots + pool[next].slotCost <= 8) visit(next + 1, [...selected, pool[next]], slots + pool[next].slotCost);
  }
  visit(0, seed, seed.reduce((sum, unit) => sum + unit.slotCost, 0));
  return result.sort(ctx.tftFlexCompare);
}

async function checkSearchAndSwaps() {
  const ctx = algorithmContext, raw = fixture(), catalog = ctx.tftFlexCatalog(raw), snapshot = JSON.stringify(raw);
  const searchOptions = { ...settings, beamWidth: 800, yieldTask: async () => {} };
  const first = await ctx.tftFlexSearch(catalog, searchOptions), second = await ctx.tftFlexSearch(catalog, searchOptions);
  assert(first.boards.length >= 2, 'Search offers distinct support packages');
  assert.deepEqual(plain(first), plain(second), 'Results and expansion counts are deterministic');
  assert.equal(first.boards[0].score, enumerate(ctx, catalog, settings)[0].score, 'Fixture ranking matches independent complete enumeration');
  assert(first.boards.every(board => board.units.every(unit => unit.cost <= 4)), 'Default supports stop at 4 cost');
  assert(first.boards.every(board => board.frontlineCount >= 3 && board.slotsUsed === 8 && board.valid));
  assert(first.boards.every(board => board.units.length === new Set(board.units.map(unit => unit.api)).size), 'Every roster contains distinct champions');
  assert(first.boards.every(board => board.anchorCoverage.tank.length && board.anchorCoverage.carry.length));
  assert(first.boards.every((board, index) => index === 0 || ctx.tftFlexCompare(first.boards[index - 1], board) <= 0), 'Diverse cards retain score order');
  const support = board => board.units.filter(unit => !['tank', 'carry'].includes(unit.slug)).map(unit => unit.slug);
  for (let i = 0; i < first.boards.length; ++i) for (let j = i + 1; j < first.boards.length; ++j)
    assert(support(first.boards[i]).filter(slug => support(first.boards[j]).includes(slug)).length <= 4, 'Cards differ by at least two support units');
  const vertical = await ctx.tftFlexSearch(catalog, { ...searchOptions, preference: 'vertical' });
  assert.equal(vertical.boards[0].score, enumerate(ctx, catalog, { ...settings, preference: 'vertical' })[0].score);
  const locked = await ctx.tftFlexSearch(catalog, { ...searchOptions, locked: ['duo-a'], minFrontline: 1 });
  assert(locked.boards.every(board => board.units.some(unit => unit.slug === 'duo-a')), 'Owned units stay on every result');
  const heavy = await ctx.tftFlexSearch(catalog, { ...searchOptions, allowFive: true, locked: ['elder'] });
  assert(heavy.boards.length && heavy.boards.every(board => board.units.length === 7 && board.slotsUsed === 8), 'A two-slot unit occupies two slots and appears only once');
  await assert.rejects(() => ctx.tftFlexSearch(catalog, { ...searchOptions, locked: ['elder'] }), /Enable 5-cost supports/);
  await assert.rejects(() => ctx.tftFlexSearch(catalog, { ...searchOptions, locked: catalog.units.filter(unit => unit.cost < 4).map(unit => unit.slug) }), /more than 8/);
  const impossible = await ctx.tftFlexSearch(catalog, { ...searchOptions, minFrontline: 8 });
  assert.equal(impossible.boards.length, 0, 'Unsatisfiable frontline requirements produce an honest empty result');
  let yields = 0;
  const cancelled = await ctx.tftFlexSearch(catalog, { ...searchOptions, yieldEvery: 2,
    yieldTask: async () => { ++yields; }, cancelled: () => yields >= 2 });
  assert(cancelled.cancelled && cancelled.expanded <= 2, 'Search yields and promptly stops after cancellation');
  const bounded = await ctx.tftFlexSearch(catalog, { ...searchOptions, maxExpansions: 7 });
  assert(bounded.expanded <= 7 && bounded.truncated, 'The expansion budget is a hard bound');
  const decorated = fixture();
  decorated.units.forEach(unit => Object.assign(unit, { items: ['fake'], star: 3, dps: 999999, damage: 123456 }));
  const agnostic = await ctx.tftFlexSearch(ctx.tftFlexCatalog(decorated), searchOptions);
  assert.deepEqual(plain(first), plain(agnostic), 'Items, stars and simulated damage cannot influence boards or scores');
  const board = first.boards[0], options = { ...searchOptions, minFrontline: 3, swapLimit: 20, maxScoreLoss: 100 };
  const swaps = await ctx.tftFlexSwaps(catalog, board, options);
  assert(swaps.swaps.some(swap => swap.removed.length === 1 && swap.added.length === 1));
  assert(swaps.swaps.some(swap => swap.removed.length === 2 && swap.added.length === 2));
  for (const swap of swaps.swaps) {
    assert(ctx.tftFlexEvaluate(catalog, swap.board.units, options).valid, 'Every swap keeps complete slots, anchors, coverage, cost pool and frontline');
    assert(!swap.removed.some(unit => ['tank', 'carry'].includes(unit.slug)), 'Swaps preserve anchors');
    assert.equal(swap.scoreDelta, swap.board.score - board.score);
    assert.deepEqual(plain(swap.traitChanges), plain(ctx.tftFlexTraitChanges(board, swap.board)));
    for (const change of swap.traitChanges) {
      assert.equal(change.beforeCount, board.units.reduce((sum, unit) => sum + (unit.traitContributions[change.api] || 0), 0));
      assert.equal(change.afterCount, swap.board.units.reduce((sum, unit) => sum + (unit.traitContributions[change.api] || 0), 0), 'Inactive counts are preserved in breakpoint changes');
    }
  }
  const protectedSwaps = await ctx.tftFlexSwaps(catalog, board, { ...options, locked: [board.units[2].slug] });
  assert(protectedSwaps.swaps.every(swap => !swap.removed.some(unit => unit.slug === board.units[2].slug)), 'Swaps preserve owned locks');
  const heavySwaps = await ctx.tftFlexSwaps(catalog, board, { ...options, allowFive: true });
  assert(heavySwaps.swaps.some(swap => swap.removed.length === 2 && swap.added.length === 1 && swap.added[0].slug === 'elder'),
    'Two one-slot supports may exchange for one two-slot unit');
  yields = 0;
  const cancelledSwaps = await ctx.tftFlexSwaps(catalog, board, { ...options, yieldEvery: 3,
    yieldTask: async () => { ++yields; }, cancelled: () => yields >= 2 });
  assert(cancelledSwaps.cancelled && cancelledSwaps.expanded <= 3, 'Swap work also yields and cancels');
  assert.equal(JSON.stringify(raw), snapshot, 'Searching and swaps leave immutable published data unchanged');
  return { catalog, board: first.boards[0] };
}

class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.dataset = {}; this.style = {}; this.attributes = {};
    this.events = {}; this.className = ''; this.hidden = false; this.disabled = false; this.value = ''; this._text = '';
    this.classList = { add: name => this.className += ' ' + name, remove: name => this.className = this.className.replace(name, ''),
      toggle: (name, selected) => selected ? this.classList.add(name) : this.classList.remove(name) }; }
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
  scrollIntoView() {}
}

function interfaceHarness({ cores = ['tank', 'carry'] } = {}) {
  const elements = new Map(), element = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  for (const [id, keys] of [['tft-flex-preference-seg', ['breadth', 'vertical']], ['tft-mode-seg', ['builds', 'leaderboard', 'compositions', 'traits']]])
    for (const key of keys) { const button = new Element('button'); button.dataset.key = key; element(id).append(button); }
  for (const id of ['tft-flex-cost', 'tft-flex-min-cost']) {
    const select = html.match(new RegExp(`<select[^>]*id="${id}"[^>]*>([\\s\\S]*?)</select>`));
    for (const match of select?.[1].matchAll(/<option\b([^>]*)>([\s\S]*?)<\/option>/g) || []) {
      const option = new Element('option'); option.value = match[1].match(/value="([^"]+)"/)?.[1] || '';
      option.textContent = match[2]; option.selected = /\bselected\b/.test(match[1]);
      element(id).append(option);
    }
  }
  const document = { hidden: false, getElementById: element, createElement: tag => new Element(tag), body: new Element('body'),
    querySelectorAll: selector => { const match = selector.match(/^#([\w-]+) (.+)$/); return match ? element(match[1]).querySelectorAll(match[2])
      : selector.startsWith('.') ? [...elements.values()].flatMap(node => node.querySelectorAll(selector)) : []; } };
  const flex = { catalog: null, metaReq: 0, metaLoading: null, loading: false, cost: 4, cores: [...cores], coreSearch: '', preference: 'breadth',
    minCost: 1, minFrontline: 3, allowFive: false, locked: [], lockSearch: '', searchReq: 0, searching: false, boards: [], key: null,
    expanded: 0, pairsDone: 0, pairsTotal: 0, sort: 'score', order: 'desc', page: 1,
    cards: new Map(), openBoards: new Set(), lobbyProfile: null, lobbyPlayers: 7, lobbyDevelopment: .5, lobbyOverrides: {}, lobbySearch: '', lobbyReq: 0,
    lobbySwapBoards: new Map(),
    pending: false, error: null };
  const champion = { mode: 'traits', meta: null, status: null, unit: 'old-carry', star: 3, geo: 'clump', traits: 'high', threat: 'mixed' };
  const calls = [], copied = [], counters = { hashes: 0, schedules: 0, combat: 0 };
  const context = vm.createContext({ tflex: flex, tstate: champion, tcomp: { meta: null }, tboard: {},
    state: { view: 'tft', tier: 'fixture' }, document, setTimeout, clearTimeout, AbortController,
    navigator: { clipboard: { writeText: async code => copied.push(code) } },
    hideTftTooltip() {}, bindTftTooltip: (anchor, callback, tap) => { anchor.tooltip = callback; if (tap) anchor.addEventListener('click', () => { anchor.openedTooltip = callback(); }); },
    scheduleTftStatus: () => { ++counters.schedules; }, updateHash: () => { ++counters.hashes; },
    tftPublicationChanged: (next, current) => !!next?.publicationRevision && next.publicationRevision !== current?.publicationRevision,
    api: async (url, options) => { calls.push({ url, options }); return fixture(); } });
  vm.runInContext(section('function tftText(', 'function tftUnitTraits(')
    + section('function tftTooltipHead(', 'function tftChampionTooltip(')
    + section('function tftActive(', 'function tftSeg(')
    + section('function tftCompositionTeamCode(', 'const tftCompositionValidated')
    + section('// ---------------- trait flex: unit search ----------------', '// ---------------- trait flex: end ----------------'), context);
  return { context, flex, champion, counters, calls, copied, element, document };
}

async function settled(harness) {
  for (let count = 0; count < 100 && (harness.flex.searching || harness.flex.loading); ++count) await new Promise(resolve => setTimeout(resolve, 1));
  assert(!harness.flex.searching && !harness.flex.loading, 'Interface work completes within a bounded wait');
}

async function until(condition, message) {
  for (let count = 0; count < 1000 && !condition(); ++count) await new Promise(resolve => setTimeout(resolve, 1));
  assert(condition(), message);
}

async function openFirstBoard(harness) {
  const results = harness.element('tft-flex-results'), row = results.querySelector('.tft-flex-summary');
  assert(row, 'The summary table contains a board');
  await row.querySelector('.tft-flex-open').fire('click');
  const board = harness.flex.boards.find(board => board.id === row.dataset.board), card = harness.flex.cards.get(board.id);
  assert(card && results.querySelector('.tft-flex-card'), 'Expanding a row lazily renders the complete board');
  return { row, board, card };
}

function coreButton(harness, slug) {
  return harness.element('tft-flex-unit-seg').querySelectorAll('button').find(button => button.dataset.key === slug);
}

async function toggleCore(harness, slug) {
  const button = coreButton(harness, slug);
  assert(button, 'The selected cost provides a portrait button for ' + slug);
  await button.fire('click');
  await until(() => !harness.flex.searching && !harness.flex.loading, 'Core selection completes a bounded trait search');
}

async function checkInterface() {
  const h = interfaceHarness(), ctx = h.context;
  assert(html.includes('data-key="traits"') && html.includes('id="tft-traits-panel"'), 'Trait flex has its own navigation and panel');
  const panel = html.slice(html.indexOf('<section class="card" id="tft-traits-panel"'), html.indexOf('<div id="tft-champion-panel"'));
  assert(!/<select[^>]*(?:item|star)|data-key="(?:geo|threat)"/.test(panel), 'Unit mode has no item, star or combat controls');
  await ctx.loadTftFlex();
  assert(h.flex.boards.length && plain(h.flex.cores).join('|') === 'carry|tank', 'Selected same-cost core units generate support packages');
  assert.equal(h.calls.length, 1); assert.equal(h.calls[0].url, 'api/tft/traits.json');
  assert.equal(h.calls[0].options.cache, 'no-store');
  assert(coreButton(h, 'tank') && coreButton(h, 'carry'));
  assert(h.element('tft-flex-formula').textContent.includes('3 × active traits'));
  assert.equal(h.element('tft-flex-frontline').value, '3');
  assert(h.element('tft-flex-results').textContent.includes('Trait score'));
  assert(!/DPS|EHP|undefined|NaN/.test(h.element('tft-flex-results').textContent), 'Cards contain no combat scores or malformed values');
  assert.equal(h.element('tft-flex-results').querySelectorAll('.tft-flex-card').length, 0, 'The summary view defers detailed cards until selected');
  const { card, board } = await openFirstBoard(h);
  assert(card.textContent.includes('2★ target'), 'Every 4-cost has the required three-copy gold target');
  const unitButton = card.querySelector('.tft-comp-unit-head');
  await unitButton.fire('click');
  assert(unitButton.openedTooltip.map(node => node.textContent).join(' ').includes('Core unit'), 'Champion clicks show a generic core-unit tooltip');
  const copy = card.querySelector('.tft-comp-copy').children[0]; await copy.fire('click');
  assert.equal(h.copied[0], ctx.tftCompositionTeamCode(board, h.flex.catalog.teamPlanner), 'Copy uses the exact unit roster and catalog planner IDs');
  const details = card.querySelector('.tft-flex-swaps'); details.open = true; await details.fire('toggle');
  assert(details.querySelector('table') && details.textContent.includes('Reached breakpoints') && details.textContent.includes('Score change'), 'Expanded swaps show before/after breakpoints and score deltas');
  assert.equal(h.calls.length, 1, 'Tooltips, swaps and copy require no combat fetch');
  h.element('tft-flex-frontline').value = '1'; await h.element('tft-flex-frontline').fire('change'); await settled(h);
  assert.equal(h.flex.minFrontline, 1, 'The exact stated minimum of one frontline is selectable');
  const verticalButton = h.element('tft-flex-preference-seg').children[1]; await verticalButton.fire('click'); await settled(h);
  assert.equal(h.flex.preference, 'vertical'); assert(h.element('tft-flex-formula').textContent.includes('3 × extra reached tiers'));
  h.element('tft-flex-lock-search').value = 'front-a'; await h.element('tft-flex-lock-search').fire('input');
  const lock = h.element('tft-flex-lock-list').querySelector('input'); lock.checked = true; await lock.fire('change'); await settled(h);
  assert(h.flex.locked.includes('front-a') && h.flex.boards.every(board => board.units.some(unit => unit.slug === 'front-a')));
  const original = h.flex.boards, revision = h.flex.catalog.revision;
  ctx.api = async () => { throw new Error('HTTP 500'); };
  await ctx.loadTftFlex({ force: true });
  assert.equal(h.flex.boards, original, 'A catalog request failure preserves the previous accepted boards');
  assert(h.element('tft-flex-status').textContent.includes('previous boards remain available'));
  assert.equal(h.element('tft-flex-retry').hidden, false);
  ctx.api = async () => ({ ...fixture(), publicationRevision: 'site-v2' });
  h.champion.status = { publicationRevision: 'site-v2', revision: 'unrelated-combat-v2', patch: '18.3' };
  await h.element('tft-flex-retry').fire('click');
  assert.equal(h.flex.catalog.publicationRevision, 'site-v2'); assert.equal(h.flex.catalog.revision, revision, 'Publication refresh keeps catalog and combat revisions independent');
  assert.equal(h.element('tft-flex-retry').hidden, true);
  const missing = interfaceHarness(); missing.context.api = async () => { throw new Error('HTTP 404'); };
  await missing.context.loadTftFlex();
  assert(missing.flex.pending && missing.element('tft-flex-status').textContent.includes('older dashboard server'));
  assert.equal(missing.element('tft-flex-retry').hidden, false, 'Old bundles have a clear retry path');
  missing.context.api = async () => ({ pending: true }); await missing.context.loadTftFlex({ force: true });
  assert(missing.flex.pending && !missing.flex.error, 'An unpublished catalog is pending rather than malformed');
  missing.context.api = async () => fixture(); await missing.element('tft-flex-retry').fire('click');
  assert(missing.flex.catalog && missing.flex.boards.length, 'An explicit retry recovers when the catalog publishes');
  const race = interfaceHarness(); let finish;
  race.context.api = () => new Promise(resolve => { finish = resolve; });
  const old = race.context.loadTftFlex();
  race.context.api = async () => ({ ...fixture(), publicationRevision: 'site-new' });
  await race.context.loadTftFlex({ force: true }); finish({ ...fixture(), publicationRevision: 'site-old' }); await old;
  assert.equal(race.flex.catalog.publicationRevision, 'site-new', 'Late requests cannot overwrite a newer accepted publication');
}

async function checkCoreBrowseInterface() {
  const h = interfaceHarness(), ctx = h.context;
  ctx.api = async () => multiAnchorFixture(); await ctx.loadTftFlex();
  assert(!html.includes('id="tft-flex-tank"') && !html.includes('id="tft-flex-carry"'), 'Portrait multiselect replaces the role-paired dropdowns');
  assert.equal(coreButton(h, 'tank').attributes['aria-pressed'], 'true');
  assert.equal(coreButton(h, 'second-tank').attributes['aria-pressed'], 'false');
  await toggleCore(h, 'second-tank');
  assert.deepEqual(plain(h.flex.cores), ['carry', 'second-tank', 'tank']);
  assert(h.flex.boards.length && h.flex.boards.every(board => plain(board.cores).join('|') === 'carry|second-tank|tank'));
  assert.equal(coreButton(h, 'second-tank').attributes['aria-pressed'], 'true');
  assert(h.element('tft-flex-selected').textContent.includes('SECOND-TANK'), 'Every selected core has a visible selected-unit chip');
  const summary = h.element('tft-flex-results').querySelector('.tft-flex-summary');
  assert(['TANK', 'CARRY', 'SECOND-TANK'].every(name => summary.textContent.includes(name)), 'The summary names every chosen core without assigning tank/carry roles');
  const { card, board } = await openFirstBoard(h);
  const swaps = card.querySelector('.tft-flex-swaps'), originalSwapSearch = ctx.tftFlexSwaps;
  let captured;
  ctx.tftFlexSwaps = (catalog, selected, options) => { captured = options; return originalSwapSearch(catalog, selected, options); };
  swaps.open = true; await swaps.fire('toggle');
  assert.deepEqual(plain(captured.cores), plain(board.cores), 'A selected board keeps all three core champions during support swaps');
  assert(!Object.hasOwn(board, 'mainTank') && !Object.hasOwn(board, 'mainCarry'), 'Generic boards do not invent role assignments');
  assert(swaps.querySelector('table'), 'Three-core boards still expose interchangeable supports');
  const searchRequest = h.flex.searchReq, apiCalls = h.calls.length;
  h.element('tft-flex-sort').value = 'purchaseGold'; await h.element('tft-flex-sort').fire('change');
  assert.equal(h.flex.sort, 'purchaseGold'); assert.equal(h.flex.order, 'asc', 'Gold sorts start with the cheapest boards');
  assert.equal(h.flex.searchReq, searchRequest); assert.equal(h.calls.length, apiCalls, 'Sorting reuses searched boards without extra fetches');
  assert.equal(h.flex.cards.get(board.id), card); assert(h.flex.openBoards.has(board.id));
  assert.equal(swaps.open, true, 'Reordering preserves an expanded support-swap section');
  h.element('tft-flex-order').value = 'desc'; await h.element('tft-flex-order').fire('change');
  assert.equal(h.flex.order, 'desc');

  const example = h.flex.boards[0];
  h.flex.boards = Array.from({ length: 57 }, (_, index) => ({ ...example, id: 'page-' + String(index).padStart(2, '0'),
    score: 100 - index, gold: { totalGold: 1000 - index, refreshGold: 900 - index, purchaseGold: 100 } }));
  h.flex.cards = new Map(); h.flex.openBoards = new Set(); h.flex.sort = 'score'; h.flex.order = 'desc'; h.flex.page = 1;
  ctx.renderTftFlex();
  const visible = () => h.element('tft-flex-results').querySelectorAll('.tft-flex-summary');
  assert.equal(visible().length, 25); assert.equal(visible()[0].dataset.board, 'page-00');
  assert(h.element('tft-flex-page').textContent.includes('Showing 1–25 of 57'));
  assert.equal(h.element('tft-flex-prev').disabled, true); assert.equal(h.element('tft-flex-next').disabled, false);
  const first = await openFirstBoard(h), retainedSwaps = first.card.querySelector('.tft-flex-swaps'); retainedSwaps.open = true;
  await h.element('tft-flex-next').fire('click');
  assert.equal(h.flex.page, 2); assert.equal(visible().length, 25); assert.equal(visible()[0].dataset.board, 'page-25');
  assert.equal(h.flex.cards.get(first.board.id), first.card, 'Off-page cards remain cached');
  await h.element('tft-flex-next').fire('click');
  assert.equal(h.flex.page, 3); assert.equal(visible().length, 7); assert.equal(h.element('tft-flex-next').disabled, true);
  assert(h.element('tft-flex-page').textContent.includes('Showing 51–57 of 57'));
  await h.element('tft-flex-prev').fire('click'); await h.element('tft-flex-prev').fire('click');
  assert.equal(h.flex.page, 1); assert.equal(h.element('tft-flex-results').querySelector('.tft-flex-card'), first.card);
  assert.equal(retainedSwaps.open, true, 'Returning to a page restores the existing open card and swap section');
  h.element('tft-flex-sort').value = 'gold'; await h.element('tft-flex-sort').fire('change');
  assert.equal(h.flex.page, 1); assert.equal(visible()[0].dataset.board, 'page-56', 'Gold sorting compares the entire board pool before paging');
  assert.equal(h.flex.cards.get(first.board.id), first.card);
  h.element('tft-flex-order').value = 'desc'; await h.element('tft-flex-order').fire('change');
  assert.equal(visible()[0].dataset.board, 'page-00'); assert.equal(h.element('tft-flex-results').querySelector('.tft-flex-card'), first.card);
  const scoreHeader = h.element('tft-flex-results').querySelectorAll('th').find(cell => cell.textContent.startsWith('Trait score'));
  await scoreHeader.querySelector('button').fire('click');
  assert.equal(h.flex.sort, 'score'); assert.equal(h.flex.order, 'desc'); assert.equal(scoreHeader.attributes['aria-sort'], 'none');
  const currentScoreHeader = h.element('tft-flex-results').querySelectorAll('th').find(cell => cell.textContent.startsWith('Trait score'));
  assert.equal(currentScoreHeader.attributes['aria-sort'], 'descending', 'Column sorting exposes its direction accessibly');
  await currentScoreHeader.querySelector('button').fire('click');
  assert.equal(h.flex.order, 'asc'); assert.equal(visible()[0].dataset.board, 'page-56', 'A second header click reverses the order');
  h.flex.page = 99; ctx.renderTftFlex(); assert.equal(h.flex.page, 3, 'Restored or stale page indexes stay within available results');
  h.flex.boards = []; ctx.renderTftFlex();
  assert.equal(h.element('tft-flex-pagination').hidden, true); assert.equal(h.element('tft-flex-empty').hidden, false);
}

async function checkCorePickerInitial() {
  const initial = vm.runInNewContext(section('const tflex = ', 'const TFT_CLASS') + '\ntflex');
  assert.equal(initial.cost, 4); assert.deepEqual(plain(initial.cores), [], 'The real interface starts with no fixed core champions');
  const h = interfaceHarness({ cores: [] }), ctx = h.context, original = ctx.tftFlexSearch;
  ctx.api = async () => multiAnchorFixture();
  let searched = 0;
  ctx.tftFlexSearch = (...args) => { ++searched; return original(...args); };
  await ctx.loadTftFlex();
  assert.equal(searched, 6); assert(h.flex.boards.length && !h.flex.error, 'An empty initial selection automatically browses every same-cost pair');
  assert.equal(h.flex.pairsTotal, 6); assert.equal(h.flex.pairsDone, 6);
  assert(h.element('tft-flex-status').textContent.includes('across all 4-cost core pairs'));
  assert(h.element('tft-flex-selected').textContent.includes('All 4-cost core pairs'));
  const { board, card } = await openFirstBoard(h);
  let swapOptions; const originalSwaps = ctx.tftFlexSwaps;
  ctx.tftFlexSwaps = (catalog, selected, options) => { swapOptions = options; return originalSwaps(catalog, selected, options); };
  const details = card.querySelector('.tft-flex-swaps'); details.open = true; await details.fire('toggle');
  assert.deepEqual(plain(swapOptions.cores), plain(board.cores), 'An automatically browsed board uses its own generated core pair for support swaps');
  assert(details.querySelector('table'));
  await toggleCore(h, 'carry');
  assert.deepEqual(plain(h.flex.cores), ['carry']); assert.equal(searched, 9);
  assert(h.flex.boards.length && h.flex.boards.every(board => board.units.some(unit => unit.slug === 'carry') && board.corePairs.every(pair => pair.includes('carry'))));
  assert.equal(h.flex.pairsTotal, 3); assert(h.element('tft-flex-status').textContent.includes('including CARRY'));
  assert(h.element('tft-flex-selected').textContent.includes('all same-cost partners'));
  assert.equal(coreButton(h, 'carry').attributes['aria-pressed'], 'true');
  const beforeFilter = searched;
  h.element('tft-flex-search').value = 'tank'; await h.element('tft-flex-search').fire('input');
  const visible = h.element('tft-flex-unit-seg').querySelectorAll('button');
  assert(visible.length && visible.every(button => h.flex.catalog.unitsBySlug.get(button.dataset.key).name.includes('TANK')));
  assert.equal(searched, beforeFilter, 'Portrait filtering does not run another composition search or restrict the partner pool');
  assert(h.element('tft-flex-selected').textContent.includes('CARRY'), 'Filtering portraits does not drop the fixed champion');
  await toggleCore(h, 'tank');
  assert.equal(searched, 10); assert(h.flex.boards.length && !h.flex.error);
  assert.deepEqual(plain(h.flex.cores), ['carry', 'tank']);
  assert(h.flex.boards.every(board => plain(board.cores).join('|') === 'carry|tank'), 'Two selected champions remain fixed instead of continuing partner browsing');
  assert(!h.element('tft-flex-lock-list').children.some(label => /Keep (?:CARRY|TANK)$/.test(label.querySelector('input')?.attributes['aria-label'] || '')));
  h.element('tft-flex-search').value = 'unique'; await h.element('tft-flex-search').fire('input');
  assert.deepEqual(h.element('tft-flex-unit-seg').querySelectorAll('button').map(button => button.dataset.key).sort(), ['carry', 'tank']);
  await toggleCore(h, 'carry');
  assert(h.flex.boards.length && h.flex.boards.every(board => board.units.some(unit => unit.slug === 'tank')) && !h.flex.error);
  assert(h.element('tft-flex-status').textContent.includes('including TANK'), 'Removing one of two selections returns to all partners for the remaining champion');
  await toggleCore(h, 'tank'); assert.deepEqual(plain(h.flex.cores), []);
  assert(h.flex.boards.length && !h.flex.error && h.element('tft-flex-status').textContent.includes('across all 4-cost core pairs'));
}

async function checkInitialBrowseProgress() {
  const h = interfaceHarness({ cores: [] }), ctx = h.context, raw = multiAnchorFixture();
  ctx.api = async () => raw;
  let options, finish;
  ctx.tftFlexBrowse = (_, requested) => { options = requested; return new Promise(resolve => { finish = resolve; }); };
  const loading = ctx.loadTftFlex();
  await until(() => typeof finish === 'function', 'The initial all-pair browse starts after the catalog arrives');
  assert(!h.flex.loading && h.flex.searching, 'Catalog loading finishes before the potentially longer pair search');
  assert(h.element('tft-flex-status').textContent.includes('Finding level 8 comps across all 4-cost core pairs'));
  options.onProgress({ pairsDone: 1, pairsTotal: 6, expanded: 10, boardsFound: 1 });
  assert.equal(h.flex.pairsDone, 1); assert.equal(h.flex.pairsTotal, 6);
  assert(h.element('tft-flex-status').textContent.includes('1/6 core groups checked') && !h.element('tft-flex-status').textContent.includes('Loading'));
  const board = ctx.tftFlexEvaluate(h.flex.catalog, ['tank', 'carry', 'front-a', 'front-b', 'front-d', 'duo-a', 'duo-b', 'back-b'],
    { cost: 4, cores: ['carry', 'tank'], minFrontline: 3 });
  board.corePairs = [['carry', 'tank']];
  finish({ boards: [board], expanded: 10, pairsDone: 6, pairsTotal: 6, cancelled: false, truncated: false });
  await loading;
  assert(!h.flex.loading && !h.flex.searching && h.flex.boards.length === 1 && !h.flex.error);
}

async function checkCoreSelectionCancellation() {
  let latest;
  for (const previousCores of [[], ['carry']]) {
    const h = interfaceHarness({ cores: previousCores }), ctx = h.context;
    ctx.api = async () => multiAnchorFixture(); await ctx.loadTftFlex();
    const oldBoards = h.flex.boards, original = ctx.tftFlexBrowse;
    assert(oldBoards.length);
    let finish, options, delayNext = true;
    ctx.tftFlexBrowse = (catalog, requested) => {
      if (!delayNext) return original(catalog, requested);
      delayNext = false; options = requested;
      return new Promise(resolve => { finish = resolve; });
    };
    const pending = ctx.runTftFlexSearch({ force: true });
    const request = h.flex.searchReq;
    assert(h.flex.searching && typeof finish === 'function');
    assert.deepEqual(plain(options.cores), previousCores);
    if (!h.flex.cores.includes('carry')) await toggleCore(h, 'carry');
    await toggleCore(h, 'tank');
    const accepted = h.flex.boards, key = h.flex.key, progress = [h.flex.pairsDone, h.flex.pairsTotal];
    assert(accepted.length && h.flex.searchReq > request && !h.flex.searching && !h.flex.error);
    assert.deepEqual(plain(h.flex.cores), ['carry', 'tank']);
    options.onProgress({ pairsDone: 999, pairsTotal: 999, expanded: 999, boardsFound: 999 });
    assert.deepEqual([h.flex.pairsDone, h.flex.pairsTotal], progress, 'Late progress from the older automatic browse cannot replace newer progress');
    finish({ boards: oldBoards, expanded: 100, pairsDone: previousCores.length ? 3 : 6,
      pairsTotal: previousCores.length ? 3 : 6, cancelled: false, truncated: false });
    await pending;
    assert.equal(h.flex.boards, accepted, 'Older empty/one-selection results cannot overwrite a newer fixed-core search');
    assert.equal(h.flex.key, key); assert(!h.flex.searching && !h.flex.error);
    assert(h.element('tft-flex-status').textContent.includes('for 2 selected core champions'));
    latest = h;
  }
  const h = latest, ctx = h.context;
  await toggleCore(h, 'tank');
  const oldBoards = h.flex.boards;
  let finish, options;
  ctx.tftFlexBrowse = (_, requested) => { options = requested; return new Promise(resolve => { finish = resolve; }); };
  const pending = ctx.runTftFlexSearch({ force: true }), request = h.flex.searchReq;
  assert.deepEqual(plain(options.cores), ['carry']);
  ctx.cancelTftFlex();
  assert(h.flex.searchReq > request && !h.flex.searching);
  options.onProgress({ pairsDone: 3, pairsTotal: 3, expanded: 100, boardsFound: 10 });
  finish({ boards: oldBoards, expanded: 100, pairsDone: 3, pairsTotal: 3, cancelled: false, truncated: false });
  await pending;
  assert.equal(h.flex.boards.length, 0); assert.equal(h.flex.key, null); assert.equal(h.flex.cards.size, 0);
  assert(!h.flex.searching && !h.flex.error, 'Cancelling a pending one-core browse prevents its late result from restoring stale boards');
}

async function checkCoreCostInterface() {
  const h = interfaceHarness(), ctx = h.context;
  assert(html.includes('for="tft-flex-cost"'), 'The core-cost selector has an associated label');
  assert.deepEqual(h.element('tft-flex-cost').children.map(option => option.value), ['2', '3', '4']);
  ctx.api = async () => coreCostFixture(); await ctx.loadTftFlex();
  assert.equal(h.flex.cost, 4); assert.equal(h.element('tft-flex-cost').value, '4');
  assert.equal(h.element('tft-flex-cost').disabled, false);
  const change = async (id, value) => {
    h.element(id).value = String(value); await h.element(id).fire('change');
    await until(() => !h.flex.searching && !h.flex.loading, 'Changing ' + id + ' completes a bounded trait search');
  };
  await change('tft-flex-min-cost', 3);
  h.element('tft-flex-search').value = 'tank'; await h.element('tft-flex-search').fire('input');
  const request = h.flex.searchReq;
  await change('tft-flex-cost', 2);
  assert.equal(h.flex.cost, 2); assert(h.flex.searchReq > request);
  assert.equal(h.element('tft-flex-search').value, '', 'Changing the core tier clears the previous portrait-name filter');
  assert.deepEqual(plain(h.flex.cores), [], 'Changing cost clears core selections from the previous tier');
  assert(h.flex.boards.length && !h.flex.error && h.flex.boards.every(board => board.cores.every(slug => h.flex.catalog.unitsBySlug.get(slug).cost === 2)),
    'The cleared two-cost selection automatically browses valid core pairs for its new tier');
  assert.equal(h.flex.minCost, 2); assert.equal(h.element('tft-flex-min-cost').value, '2');
  assert(h.element('tft-flex-min-cost').children.find(option => option.value === '3').disabled);
  const portraits = h.element('tft-flex-unit-seg').querySelectorAll('button');
  assert.equal(portraits.length, 4);
  assert(portraits.every(button => h.flex.catalog.unitsBySlug.get(button.dataset.key).cost === 2), 'Both frontline and backline portraits use the chosen core tier');
  await toggleCore(h, 'carry2-a'); await toggleCore(h, 'carry2-b');
  const fourCount = board => board.units.filter(unit => unit.cost === 4).length;
  assert(h.flex.boards.length && h.flex.boards.every(board => fourCount(board) <= 2 && plain(board.cores).join('|') === 'carry2-a|carry2-b' && board.frontlineCount >= 3),
    'Two backline cores are legal and supports supply the whole-board frontline minimum');
  assert(h.element('tft-flex-context').textContent.includes('At most 2 4-cost units'));
  const { card, board } = await openFirstBoard(h);
  const swaps = card.querySelector('.tft-flex-swaps'), originalSwapSearch = ctx.tftFlexSwaps;
  let captured;
  ctx.tftFlexSwaps = (catalog, selected, options) => { captured = options; return originalSwapSearch(catalog, selected, options); };
  swaps.open = true; await swaps.fire('toggle');
  assert.equal(captured.cost, 2); assert.deepEqual(plain(captured.cores), plain(board.cores));
  assert(swaps.querySelector('table'), 'Same-role lower-cost cores can still open interchangeable support packages');
  const lock = slug => h.element('tft-flex-lock-list').children.find(label => label.querySelector('input')?.attributes['aria-label'] ===
    'Keep ' + h.flex.catalog.unitsBySlug.get(slug).name)?.querySelector('input');
  const first = lock('four-front-a'), second = lock('four-front-b'), third = lock('four-back-a');
  assert(first && second && third && !third.disabled);
  first.checked = true; await first.fire('change'); await until(() => !h.flex.searching, 'The first owned four-cost search completes');
  second.checked = true; await second.fire('change'); await until(() => !h.flex.searching, 'The second owned four-cost search completes');
  assert.equal(lock('four-back-a'), third, 'Changing a lock preserves the existing controls');
  assert(third.disabled && !first.disabled && !second.disabled);
  const promoted = lock('tank2-a'); promoted.checked = true; await promoted.fire('change');
  await until(() => !h.flex.searching, 'The kept two-cost supporter search completes');
  await toggleCore(h, 'tank2-a');
  assert(!h.flex.locked.includes('tank2-a') && !lock('tank2-a'), 'Promoting a kept supporter to the core removes its redundant lock');
  assert.deepEqual(plain(h.flex.cores), ['carry2-a', 'carry2-b', 'tank2-a']);
  assert(h.flex.boards.length && h.flex.boards.every(board => fourCount(board) === 2 && board.cores.length === 3 &&
    ['four-front-a', 'four-front-b'].every(slug => board.units.some(unit => unit.slug === slug))));
  const retained = lock('tank2-b'); retained.checked = true; await retained.fire('change');
  await until(() => !h.flex.searching, 'The other kept supporter remains available');
  let hash;
  ctx.location = { hash: '' }; ctx.restoringHash = false; ctx.restoredHash = null;
  const saveHash = (_, __, next) => { hash = next; ctx.location.hash = next; };
  ctx.history = { replaceState: saveHash, pushState: saveHash };
  vm.runInContext(section('function updateHash(', 'function showView('), ctx); ctx.updateHash();
  assert(hash.includes('fcost=2') && hash.includes('fmincost=2') && hash.includes('fcore='));
  assert(!hash.includes('ftank=') && !hash.includes('fcarry='), 'Generic shared links do not impose role names');
  const saved = Object.fromEntries(hash.slice(1).split('&').map(part => part.split('=').map(decodeURIComponent)));
  assert.equal(saved.fcore, 'carry2-a,carry2-b,tank2-a', 'All selected core slugs survive in deterministic shared-link order');
  const restored = interfaceHarness({ cores: [] }); restored.context.api = async () => coreCostFixture();
  restored.context.restoreTftFlexHash(saved); await restored.context.loadTftFlex();
  assert.equal(restored.flex.cost, 2); assert.equal(restored.flex.minCost, 2);
  assert.deepEqual(plain(restored.flex.cores), ['carry2-a', 'carry2-b', 'tank2-a']);
  assert.deepEqual(plain(restored.flex.locked), ['four-front-a', 'four-front-b', 'tank2-b']);
  assert(restored.flex.boards.length && restored.flex.boards.every(board => fourCount(board) === 2));
  const clamped = interfaceHarness({ cores: [] }); clamped.context.api = async () => coreCostFixture();
  clamped.context.restoreTftFlexHash({ fcost: '2', fmincost: '3', fcore: 'tank,carry' });
  assert.equal(clamped.flex.minCost, 2); await clamped.context.loadTftFlex();
  assert.deepEqual(plain(clamped.flex.cores), []); assert(clamped.flex.boards.length && !clamped.flex.error,
    'Wrong-tier saved cores clear to automatic browsing with a compatible minimum');
  const before = h.flex.searchReq;
  await change('tft-flex-cost', 3);
  assert.equal(h.flex.cost, 3); assert.equal(h.flex.minCost, 2);
  assert.deepEqual(plain(h.flex.cores), []); assert(h.flex.boards.length && h.flex.boards.every(board => board.cores.every(slug => h.flex.catalog.unitsBySlug.get(slug).cost === 3)));
  assert(h.flex.searchReq > before && !h.flex.error);
  assert.deepEqual(plain(h.flex.locked), ['four-front-a', 'four-front-b', 'tank2-b'], 'Other-tier kept supporters survive a core-tier change');
  assert(!h.element('tft-flex-min-cost').children.find(option => option.value === '3').disabled);
  await toggleCore(h, 'tank3-a'); await toggleCore(h, 'tank3-b');
  assert(!lock('four-back-a').disabled, 'Three-cost cores permit a third owned four-cost');
  assert(h.flex.boards.length && h.flex.boards.every(board => fourCount(board) <= 3 && plain(board.cores).join('|') === 'tank3-a|tank3-b'));
  await change('tft-flex-cost', 4);
  assert.deepEqual(plain(h.flex.cores), []); assert(h.flex.boards.length);
  await toggleCore(h, 'tank'); await toggleCore(h, 'carry');
  assert(h.flex.boards.some(board => fourCount(board) > 3), 'Returning to four-cost cores restores uncapped support packages');
  assert(!lock('four-back-a').disabled); assert.equal(h.element('tft-flex-gold-target-note').hidden, true);
}

async function checkModeAndRouting() {
  const h = interfaceHarness(), ctx = h.context;
  ctx.loadTftMeta = ctx.loadTftScenario = ctx.loadTftLeaderboard = ctx.loadTftCompositions = async () => { ++h.counters.combat; };
  ctx.loadTftStatus = async () => ({ revision: 'combat-v1', publicationRevision: 'site-v1', patch: '18.3' });
  ctx.renderTftRefresh = () => {};
  vm.runInContext(section('function renderTftMode(', 'function renderTftLeaderboardControls(')
    + section('async function pollTftStatus(', '// Mark unavailable builds')
    + section('async function openTft(', '// ---------------- data view'), ctx);
  h.champion.mode = 'builds'; await ctx.setTftMode('traits');
  assert.equal(h.counters.combat, 0, 'Entering Trait flex loads no champion or combat data');
  assert.equal(h.element('tft-traits-panel').hidden, false);
  assert.equal(h.element('tft-compositions-panel').hidden, true);
  await ctx.openTft(); await ctx.pollTftStatus();
  assert.equal(h.counters.combat, 0, 'Opening and polling the new mode never load simulations');
  const accepted = h.flex.catalog;
  ctx.loadTftStatus = async () => ({ revision: 'combat-v2', publicationRevision: 'site-v1', patch: '18.3' });
  await ctx.pollTftStatus();
  assert.equal(h.flex.catalog, accepted, 'A changed combat calculation revision does not invalidate a unit catalog');
  ctx.loadTftStatus = async () => ({ publicationRevision: 'site-v2', patch: '18.3' });
  ctx.api = async () => ({ ...fixture(), publicationRevision: 'site-v2' });
  await ctx.pollTftStatus(); assert.equal(h.flex.catalog.publicationRevision, 'site-v2', 'Status notices a new catalog publication');
  const request = h.flex.searchReq;
  h.champion.data = { unit: h.champion.unit, scenario: { key: 'saved' } }; h.champion.dataRevision = 'fixture';
  ctx.tkey = () => 'saved'; ctx.tftRevision = () => 'fixture'; h.champion.meta = { revision: 'fixture' };
  await ctx.setTftMode('builds', { load: false }); assert(h.flex.searchReq > request, 'Leaving the mode cancels in-flight search and swap work');
  h.champion.mode = 'traits'; h.flex.cores = ['tank', 'carry']; h.flex.preference = 'vertical';
  h.flex.minFrontline = 1; h.flex.minCost = 3; h.flex.allowFive = true; h.flex.locked = ['front-a', 'elder'];
  h.flex.sort = 'gold'; h.flex.order = 'asc';
  h.flex.lobbyProfile = 'gold-profile'; h.flex.lobbyPlayers = 3; h.flex.lobbyDevelopment = .25; h.flex.lobbyOverrides = { tank: 0, 'front-a': 5 };
  let hash;
  ctx.location = { hash: '' }; ctx.restoringHash = false; ctx.restoredHash = null;
  const saveHash = (_, __, next) => { hash = next; ctx.location.hash = next; };
  ctx.history = { replaceState: saveHash, pushState: saveHash };
  vm.runInContext(section('function updateHash(', 'function showView('), ctx);
  ctx.updateHash();
  assert(hash.includes('tftview=traits') && hash.includes('fpref=vertical') && hash.includes('ffront=1') && hash.includes('ffive=1'));
  assert(hash.includes('fmincost=3'));
  assert(hash.includes('fcore=') && !hash.includes('ftank=') && !hash.includes('fcarry='));
  assert(hash.includes('fsort=gold') && hash.includes('forder=asc'), 'Sorting and its direction survive a shared link');
  assert(hash.includes('flprofile=gold-profile') && hash.includes('flplayers=3') && hash.includes('fldev=25'));
  assert(!/star=|citems=|geo=|tr=/.test(hash), 'Unit-mode URLs carry no item, star or combat assumptions');
  const saved = Object.fromEntries(hash.slice(1).split('&').map(part => part.split('=').map(decodeURIComponent)));
  h.flex.cores = []; h.flex.locked = []; h.flex.minFrontline = 3; h.flex.minCost = 1; h.flex.allowFive = false;
  h.flex.sort = 'score'; h.flex.order = 'desc';
  h.flex.lobbyProfile = null; h.flex.lobbyPlayers = 7; h.flex.lobbyDevelopment = .5; h.flex.lobbyOverrides = {};
  ctx.restoreTftFlexHash(saved);
  assert.deepEqual(plain(h.flex.cores), ['carry', 'tank']); assert.equal(h.flex.minFrontline, 1);
  assert.equal(h.flex.minCost, 3);
  assert.equal(h.flex.sort, 'gold'); assert.equal(h.flex.order, 'asc');
  assert.equal(h.flex.lobbyProfile, 'gold-profile'); assert.equal(h.flex.lobbyPlayers, 3); assert.equal(h.flex.lobbyDevelopment, .25);
  assert.deepEqual(plain(h.flex.lobbyOverrides), { 'front-a': 5, tank: 0 }, 'A shared link preserves explicit zero scouts');
  assert.equal(h.flex.allowFive, true); assert.deepEqual(plain(h.flex.locked), ['elder', 'front-a']);
  const legacy = interfaceHarness(); legacy.context.restoreTftFlexHash({ ftank: 'tank', fcarry: 'carry' });
  assert.deepEqual(plain(legacy.flex.cores), ['carry', 'tank'], 'An earlier explicitly chosen pair restores as generic cores');
  const unspecified = interfaceHarness(); unspecified.context.restoreTftFlexHash({ ftank: 'all', fcarry: 'all' });
  assert.deepEqual(plain(unspecified.flex.cores), [], 'Legacy All filters restore as no selected core champions');
  const absent = interfaceHarness(); absent.context.restoreTftFlexHash({});
  assert.deepEqual(plain(absent.flex.cores), [], 'A URL with no core choices retains the honest empty initial selection');
  const canonical = interfaceHarness(); canonical.context.restoreTftFlexHash({ fcore: 'carry,tank,carry', ftank: 'front-a', fcarry: 'duo-a' });
  assert.deepEqual(plain(canonical.flex.cores), ['carry', 'tank'], 'Explicit fcore choices take precedence over stale legacy role parameters');
  assert.equal(legacy.flex.sort, 'score'); assert.equal(legacy.flex.order, 'desc', 'Earlier pair-specific links retain the previous trait ranking');
  assert.equal(legacy.flex.lobbyPlayers, 7); assert.equal(legacy.flex.lobbyDevelopment, .5);
  const zero = interfaceHarness(); zero.context.restoreTftFlexHash({ flplayers: '0', fldev: '0', flheld: 'tank:0' });
  assert.equal(zero.flex.lobbyPlayers, 0); assert.equal(zero.flex.lobbyDevelopment, 0); assert.equal(zero.flex.lobbyOverrides.tank, 0,
    'Zero-player, zero-development and zero-scout settings are not replaced by defaults');
  assert(html.includes('if (mode === "traits") restoreTftFlexHash(hash);'), 'Routing restores Trait flex settings before opening');
}

function installGoldWorkers(h) {
  const blobs = new Map(), workers = [];
  h.context.Blob = class { constructor(parts) { this.source = parts.join(''); } };
  h.context.URL = { createObjectURL: blob => { const key = 'worker-' + blobs.size; blobs.set(key, blob.source); return key; },
    revokeObjectURL: key => blobs.delete(key) };
  h.context.Worker = class {
    constructor(url) {
      this.terminated = false; this.messages = []; workers.push(this);
      const self = { postMessage: data => { if (!this.terminated) this.onmessage?.({ data }); } };
      this.self = self;
      vm.runInContext(blobs.get(url), vm.createContext({ self }));
    }
    postMessage(data) { this.messages.push(data); setTimeout(() => { if (!this.terminated) this.self.onmessage({ data }); }, 0); }
    terminate() { this.terminated = true; }
  };
  return { workers, blobs };
}

async function checkCoreGoldTargets() {
  const h = interfaceHarness(), ctx = h.context, raw = coreCostFixture(), catalog = ctx.tftFlexCatalog(raw);
  catalog.shop = { costOdds: [.15, .20, .32, .30, .03],
    championCounts: [1, 2, 3, 4, 5].map(cost => catalog.units.filter(unit => unit.cost === cost).length),
    bagCopies: [30, 25, 18, 10, 9], refreshCost: 2, visibleSlots: [1] };
  h.flex.catalog = catalog;
  const sortBoards = [];
  for (const cost of [2, 3]) {
    const otherCost = cost === 2 ? 3 : 2;
    const cores = [`tank${cost}-a`, `carry${cost}-a`], support = `tank${cost}-b`;
    const roster = [...cores, support, 'four-front-a', `carry${otherCost}-a`, 'front-a', 'front-b', 'duo-a'];
    const board = ctx.tftFlexEvaluate(catalog, roster, { cost, cores, minFrontline: 3 });
    assert(board.valid && board.coreCost === cost);
    const targets = plain(ctx.tftFlexLobbyTargets(board)), target = slug => targets.find(unit => unit.api === slug);
    assert.equal(target(support).copies, 9, 'A support of the selected tier has the same three-star target as the cores');
    assert.equal(target(cores[0]).copies, 9); assert.equal(target(cores[1]).copies, 9);
    assert.equal(target('four-front-a').copies, 1, 'A four-cost support in a reroll profile needs one copy');
    assert.equal(target(`carry${otherCost}-a`).copies, 1, 'A support from the other reroll tier stays a one-copy target');
    assert.equal(target('front-a').copies, 1);
    const counts = plain(ctx.tftFlexGoldRequirements(board));
    assert.deepEqual(counts, cost === 2 ? [3, 3, 1, 1, 0] : [3, 1, 3, 1, 0],
      'Baseline requirements count distinct champions rather than replacing counts with required copies');
    const pool = catalog.units.map(unit => ({ api: unit.api, cost: unit.cost, heldCopies: 0 }));
    const summary = ctx.tftFlexLobbyEstimate(catalog.shop, pool, targets, { signatureOnly: true });
    assert.deepEqual(plain(summary.copiesByCost), cost === 2 ? [3, 27, 1, 1, 0] : [3, 1, 27, 1, 0]);
    assert.equal(summary.purchaseGold, cost === 2 ? 64 : 90);
    assert.equal(ctx.tftFlexSortValue(board, 'purchaseGold'), cost === 2 ? 64 : 90,
      'Purchase-gold sorting includes all three-star selected-tier units');
    sortBoards.push({ ...board, id: String(cost) });
    h.flex.cost = 4;
    assert.equal(ctx.tftFlexGoldCoreCost(board), cost, 'Changing the picker cannot change an existing board’s gold target tier');
    assert.deepEqual(plain(ctx.tftFlexLobbyTargets(board)), targets);
    const legacy = { ...board, mainTank: cores[0] }; delete legacy.coreCost; delete legacy.cores;
    assert.equal(ctx.tftFlexGoldCoreCost(legacy), cost, 'Legacy boards can derive their tier from their original anchor');
    const explicitLegacy = { ...board }; delete explicitLegacy.coreCost;
    assert.equal(ctx.tftFlexGoldCoreCost(explicitLegacy), cost, 'Earlier explicit-core boards derive their saved core tier');
    const tightPool = pool.map(unit => ({ ...unit, heldCopies: unit.api === support
      ? catalog.shop.bagCopies[cost - 1] - 8 : unit.api === 'four-front-a' ? 9 : 0 }));
    const unavailable = ctx.tftFlexLobbyEstimate(catalog.shop, tightPool, targets, { signatureOnly: true });
    assert.deepEqual(plain(unavailable.unavailableTargets), [
      { api: support, cost, available: 8, required: 9, reason: 'insufficient-copies' }],
      'Eight available support copies cannot complete a selected-tier three-star, while one four-cost copy is enough');
  }
  const four = ctx.tftFlexEvaluate(catalog,
    ['tank', 'carry', 'four-front-a', 'front-a', 'front-b', 'front-d', 'duo-a', 'duo-b'], settings);
  assert.equal(four.coreCost, 4);
  assert(ctx.tftFlexLobbyTargets(four).every(target => target.copies === (target.cost === 4 ? 3 : 1)),
    'The four-cost profile still requires three copies of every four-cost and one of other tiers');
  sortBoards.push({ ...four, id: '4' });
  assert.deepEqual(plain(ctx.tftFlexSortedBoards(sortBoards, 'purchaseGold', 'asc')).map(board => board.id), ['4', '2', '3']);
  assert.equal(ctx.tftFlexGoldCoreCost({ units: [{ cost: 2 }] }), 4, 'Old unanchored test boards retain the four-cost default');

  for (const cost of [2, 3]) {
    const shop = { costOdds: [0, 0, 0, 0, 0], championCounts: [0, 0, 0, 1, 0],
      bagCopies: [30, 9, 9, 1, 9], refreshCost: 2, visibleSlots: [1] };
    shop.costOdds[cost - 1] = .5; shop.costOdds[3] = .5; shop.championCounts[cost - 1] = 1;
    const counts = [0, 0, 0, 1, 0]; counts[cost - 1] = 1;
    const baseline = ctx.tftFlexGoldEstimate(shop, counts, cost);
    const lobby = ctx.tftFlexLobbyEstimate(shop, [{ api: 'core', cost, heldCopies: 0 }, { api: 'support', cost: 4, heldCopies: 0 }],
      [{ api: 'core', cost, copies: 9 }, { api: 'support', cost: 4, copies: 1 }]);
    assert.equal(baseline.expectedShops, 10,
      'With one visible slot and exactly the ten desired copies in the pool, completing the targets takes ten paid shops');
    assert.equal(baseline.refreshGold, 20); assert.equal(baseline.purchaseGold, 9 * cost + 4);
    assert.equal(baseline.totalGold, 24 + 9 * cost);
    assert.equal(lobby.totalGold, baseline.totalGold, 'The zero-held lobby and baseline use the same nine-copy target');
    assert.deepEqual(plain(baseline.copiesByCost), plain(lobby.copiesByCost));
  }
}

async function checkCoreGoldWorkers() {
  const h = interfaceHarness(), ctx = h.context, { workers } = installGoldWorkers(h);
  const shop = { costOdds: [0, .3, .3, .4, 0], championCounts: [0, 1, 1, 1, 0],
    bagCopies: [30, 25, 18, 10, 9], refreshCost: 2, visibleSlots: [1] };
  const counts = [0, 1, 1, 1, 0];
  const two = ctx.tftFlexGoldRequest(shop, counts, 2), repeatedTwo = ctx.tftFlexGoldRequest(shop, counts, 2);
  const three = ctx.tftFlexGoldRequest(shop, counts, 3), four = ctx.tftFlexGoldRequest(shop, counts, 4);
  assert.equal(two, repeatedTwo, 'Only requests with the same roster counts and target tier share a pending calculation');
  const estimates = await Promise.all([two, three, four]);
  assert.deepEqual(estimates.map(estimate => estimate.purchaseGold), [25, 33, 17],
    'The same champion-count vector produces separate two-, three- and four-cost purchase targets');
  assert.deepEqual(estimates.map(estimate => plain(estimate.copiesByCost)),
    [[0, 9, 1, 1, 0], [0, 1, 9, 1, 0], [0, 1, 1, 3, 0]]);
  assert.equal(workers.length, 1); assert.equal(workers[0].messages.length, 3);
  assert.deepEqual(workers[0].messages.map(message => message.coreCost), [2, 3, 4],
    'The isolated worker receives the saved core tier along with unchanged champion counts');
  for (const cost of [2, 3, 4]) {
    const estimate = await ctx.tftFlexGoldRequest(shop, counts, cost);
    assert.equal(estimate, estimates[cost - 2], 'Completed results are cached separately by target tier');
  }
  assert.equal(workers[0].messages.length, 3, 'Reading any completed tier does not schedule another job');
  ctx.cancelTftFlexGold();
}

async function checkCoreGoldInterface() {
  for (const cost of [2, 3]) {
    const h = interfaceHarness({ cores: [`tank${cost}-a`, `carry${cost}-a`] }), ctx = h.context;
    const raw = coreCostFixture(), sameTierSupport = `tank${cost}-b`, otherTierSupport = `carry${cost === 2 ? 3 : 2}-a`;
    h.flex.cost = cost;
    h.flex.locked = [sameTierSupport, otherTierSupport, 'four-front-a', 'front-a', 'front-b', 'duo-a'];
    const available = new Set([...h.flex.cores, ...h.flex.locked, `carry${cost}-b`]);
    raw.units = raw.units.filter(unit => available.has(unit.slug));
    raw.shop = { costOdds: [.15, .20, .32, .30, .03],
      championCounts: [1, 2, 3, 4, 5].map(tier => raw.units.filter(unit => unit.cost === tier).length),
      bagCopies: [30, 25, 18, 10, 9], refreshCost: 2, visibleSlots: [1] };
    const requests = [];
    ctx.tftFlexGoldRequest = (shop, counts, coreCost) => {
      requests.push({ counts: plain(counts), coreCost });
      const copiesByCost = counts.map((count, index) => count * (index + 1 === cost ? 9 : 1));
      const purchaseGold = copiesByCost.reduce((sum, count, index) => sum + count * (index + 1), 0);
      return Promise.resolve({ copiesByCost, purchaseGold, refreshGold: 200, totalGold: 200 + purchaseGold });
    };
    ctx.api = async () => raw; await ctx.loadTftFlex();
    await until(() => h.flex.boards.every(board => board.gold), 'The selected-tier baseline request completes');
    assert.equal(h.flex.boards.length, 1); assert.equal(h.flex.boards[0].gold.purchaseGold, cost === 2 ? 64 : 90);
    assert(requests.length && requests.every(request => request.coreCost === cost),
      'Board baseline jobs pass the selected tier separately from unit counts');
    const note = h.element('tft-flex-gold-target-note');
    assert.equal(note.hidden, false); assert(note.textContent.includes('9') && note.textContent.includes('3★'));
    const shopRows = h.element('tft-flex-shop-table').querySelector('tbody').children;
    assert(shopRows[cost - 1].children[4].textContent.includes('9') && shopRows[cost - 1].children[4].textContent.includes('3★'));
    assert(shopRows[3].children[4].textContent.includes('1') && !shopRows[3].children[4].textContent.includes('2★'),
      'The method table requires one four-cost copy in a lower-cost profile');
    const { board, card } = await openFirstBoard(h);
    const tiles = card.querySelectorAll('.tft-comp-unit'), tile = slug => tiles.find(node => node.querySelector('button').dataset.unit === slug);
    assert(tile(sameTierSupport).textContent.includes('3★ target') && tile(sameTierSupport).dataset.assignment === 'support',
      'The card shows a selected-tier support at three stars');
    assert(tile(board.cores[0]).textContent.includes('3★ target'));
    assert(tile('four-front-a').textContent.includes('1 copy') && !tile('four-front-a').textContent.includes('2★'));
    assert(tile(otherTierSupport).textContent.includes('1 copy'));
    const tooltip = (slug, selectedBoard = board) => ctx.tftFlexUnitTooltip(h.flex.catalog.unitsBySlug.get(slug), selectedBoard)
      .map(node => node.textContent).join(' ');
    assert(tooltip(sameTierSupport).includes('9 copies for 3★'));
    assert(tooltip('four-front-a').includes('1 copy') && !tooltip('four-front-a').includes('3 copies'));
    assert(tooltip(board.cores[0], null).includes('9 copies for 3★'), 'Picker tooltips use the active profile before there is a board');
    h.flex.locked = h.flex.locked.filter(slug => slug !== sameTierSupport);
    const searchSwaps = ctx.tftFlexSwaps; let swapResult;
    ctx.tftFlexSwaps = async (...args) => { swapResult = await searchSwaps(...args); return swapResult; };
    const details = card.querySelector('.tft-flex-swaps'); details.open = true; await details.fire('toggle');
    await until(() => !details.textContent.includes('Calculating…'), 'Support swap gold requests complete');
    assert(swapResult.swaps.length && swapResult.swaps.every(swap => swap.board.coreCost === cost));
    const sameTierReplacement = swapResult.swaps.find(swap => swap.added.some(unit => unit.cost === cost));
    assert(sameTierReplacement, 'The legal support swaps include another selected-tier champion');
    assert(ctx.tftFlexLobbyTargets(sameTierReplacement.board).some(target =>
      sameTierReplacement.added.some(unit => unit.api === target.api && unit.cost === cost) && target.copies === 9),
      'A newly added selected-tier support carries its own nine-copy requirement');
    assert(requests.length > 1 && requests.every(request => request.coreCost === cost), 'Before and after swap jobs retain the board’s gold tier');
    assert(details.textContent.includes('Expected gold') && details.querySelector('tbody').children.every(row =>
      /\d+g/.test(row.children[4].textContent) && !row.children[4].textContent.includes('Unavailable')),
      'Whole-board baseline gold remains available for the selected-tier support swaps');
    h.flex.cost = 4;
    const retainedCard = ctx.tftFlexBoardCard(board, 1);
    assert(retainedCard.querySelectorAll('.tft-comp-unit').find(node => node.querySelector('button').dataset.unit === sameTierSupport)
      .textContent.includes('3★ target'), 'Rendering a saved board uses its stamped tier even after the picker moves to another tier');
    assert(tooltip(sameTierSupport).includes('9 copies for 3★'));
    ctx.cancelTftFlex();
  }
}

async function checkGoldInterface() {
  const setup = () => {
    const h = interfaceHarness(), { workers, blobs } = installGoldWorkers(h);
    const raw = fixture();
    raw.shop = { costOdds: [.15, .20, .32, .30, .03], championCounts: [14, 13, 14, 14, 10],
      bagCopies: [30, 25, 18, 10, 9], refreshCost: 2, visibleSlots: [4, 5], sources: [] };
    raw.units.find(unit => unit.slug === 'front-a').cost = 4;
    h.flex.cores = ['tank', 'carry']; h.flex.locked = ['front-a'];
    h.context.api = async () => raw;
    return { ...h, workers, blobs };
  };
  const h = setup();
  await h.context.loadTftFlex();
  const { card, board: opened } = await openFirstBoard(h);
  const swaps = card.querySelector('.tft-flex-swaps'); swaps.open = true;
  assert(h.element('tft-flex-results').textContent.includes('Calculating expected gold'), 'Boards remain available while gold calculates in the worker');
  for (let attempt = 0; attempt < 100 && !h.flex.boards.every(board => board.gold); ++attempt) await new Promise(resolve => setTimeout(resolve, 1));
  assert(h.flex.boards.every(board => board.gold && board.gold.totalGold > board.gold.purchaseGold));
  assert.equal(h.flex.cards.get(opened.id), card, 'Gold updates preserve the existing interactive board nodes');
  assert.equal(swaps.open, true, 'Gold updates leave an expanded swap section open');
  assert(h.element('tft-flex-results').textContent.includes('Expected gold to hit'));
  for (const board of h.flex.boards) {
    const fourCosts = board.units.filter(unit => unit.cost === 4).length;
    assert.equal(board.gold.copiesByCost[3], fourCosts * 3, 'Four-cost supports need three copies as well as both anchors');
    assert.equal(board.gold.purchaseGold, board.units.reduce((sum, unit) => sum + unit.cost * (unit.cost === 4 ? 3 : 1), 0));
  }
  await swaps.fire('toggle');
  for (let attempt = 0; attempt < 100 && swaps.textContent.includes('Calculating…'); ++attempt) await new Promise(resolve => setTimeout(resolve, 1));
  assert(swaps.textContent.includes('Expected gold') && /g \([+−]?\d+g\)/.test(swaps.textContent), 'Support swaps compare whole-roster gold and its change');
  assert(!swaps.textContent.includes('Calculating…'));
  assert.equal(h.workers.length, 1, 'One worker handles all distinct cost profiles');
  const requestsBefore = h.calls.length;
  h.context.cancelTftFlex();
  assert(h.workers[0].terminated && !h.blobs.size, 'Leaving the view cancels background gold work and releases its script');
  await h.context.loadTftFlex();
  assert.equal(h.workers.length, 1, 'Completed expectations are reused from the small result cache');
  assert.equal(h.calls.length, requestsBefore, 'Gold work does not fetch additional combat or shop endpoints');
  const cancelled = setup(); await cancelled.context.loadTftFlex(); cancelled.context.cancelTftFlex();
  await new Promise(resolve => setTimeout(resolve, 10));
  assert(cancelled.workers.every(worker => worker.terminated));
  assert(!cancelled.flex.boards.some(board => board.gold), 'Cancelled jobs cannot fill old boards with late results');
}

async function checkGoldSortCompletion() {
  const prepare = async (count = 31) => {
    const h = interfaceHarness(); await h.context.loadTftFlex();
    h.flex.catalog.shop = { costOdds: [.15, .20, .32, .30, .03], championCounts: [14, 13, 14, 14, 10],
      bagCopies: [30, 25, 18, 10, 9], refreshCost: 2, visibleSlots: [4, 5] };
    const base = h.flex.boards[0];
    h.flex.boards = Array.from({ length: count }, (_, index) => ({ ...base, id: 'async-' + index, score: count - index }));
    h.flex.sort = 'gold'; h.flex.order = 'asc'; h.flex.page = 1; h.flex.cards = new Map(); h.flex.openBoards = new Set();
    const pending = [];
    h.context.tftFlexGoldRequest = () => new Promise(resolve => pending.push(resolve));
    const originalRender = h.context.renderTftFlex; let renders = 0;
    h.context.renderTftFlex = () => { ++renders; return originalRender(); };
    h.context.renderTftFlex();
    const completion = h.context.beginTftFlexGold(h.flex.catalog, h.flex.boards, h.flex.searchReq);
    const gold = totalGold => ({ totalGold, refreshGold: totalGold - 36, purchaseGold: 36, expectedShops: (totalGold - 36) / 2 });
    return { ...h, pending, completion, gold, renders: () => renders };
  };
  const h = await prepare(), originalTable = h.element('tft-flex-results').children[0], initialRenders = h.renders();
  const opened = await openFirstBoard(h);
  const rowIds = () => h.element('tft-flex-results').querySelectorAll('.tft-flex-summary').map(row => row.dataset.board);
  const before = rowIds();
  h.pending[30](h.gold(60)); await Promise.resolve(); await Promise.resolve();
  assert(h.flex.boards[30].gold && !before.includes('async-30'), 'An off-page result receives its gold estimate');
  assert.equal(h.renders(), initialRenders); assert.equal(h.element('tft-flex-results').children[0], originalTable);
  assert.deepEqual(rowIds(), before, 'An early cheap estimate does not reorder incomplete gold results');
  for (let index = 0; index < 29; ++index) h.pending[index](h.gold(100 + index));
  await Promise.resolve(); await Promise.resolve();
  assert.equal(h.renders(), initialRenders, 'Rows stay stable until the complete batch settles');
  h.pending[29](h.gold(129)); await h.completion;
  assert.equal(h.renders(), initialRenders + 1, 'The completed batch performs one final gold reorder');
  assert.equal(rowIds()[0], 'async-30', 'The cheapest board in the whole pool rises onto the first page');
  assert(h.flex.boards.every(board => board.gold));
  assert.equal(h.flex.cards.get(opened.board.id), opened.card);
  assert.equal(h.element('tft-flex-results').querySelector('.tft-flex-card'), opened.card, 'Final gold ordering keeps expanded detail nodes');
  assert(!h.element('tft-flex-status').textContent.includes('Calculating gold'));
  const cancelled = await prepare(4), cancelledTable = cancelled.element('tft-flex-results').children[0], beforeCancel = cancelled.renders();
  cancelled.context.cancelTftFlex(); cancelled.pending.forEach((resolve, index) => resolve(cancelled.gold(100 + index)));
  await cancelled.completion;
  assert.equal(cancelled.renders(), beforeCancel); assert.equal(cancelled.element('tft-flex-results').children[0], cancelledTable,
    'A cancelled batch cannot perform a final reorder of old results');
  assert(!cancelled.flex.boards.some(board => board.gold), 'Late cancelled estimates do not update board data');
  const changedSort = await prepare(4);
  changedSort.context.setTftFlexSort('score'); const afterChange = changedSort.renders();
  changedSort.pending.forEach((resolve, index) => resolve(changedSort.gold(100 + index))); await changedSort.completion;
  assert.equal(changedSort.renders(), afterChange, 'A pending gold batch respects a newer sort choice when it finishes');
  assert.equal(changedSort.flex.sort, 'score'); assert(changedSort.flex.boards.every(board => board.gold));
}

async function checkLobbyCompletion() {
  const prepare = async (count = 31) => {
    const h = interfaceHarness(); await h.context.loadTftFlex();
    h.flex.catalog = h.context.tftFlexCatalog(lobbyFixture());
    h.context.renderTftFlexLobbyControls();
    const base = h.flex.boards[0];
    h.flex.boards = Array.from({ length: count }, (_, index) => ({ ...base, id: 'lobby-' + index, score: count - index,
      gold: { totalGold: 300 + index, refreshGold: 264 + index, purchaseGold: 36 } }));
    h.flex.sort = 'lobbyGold'; h.flex.order = 'asc'; h.flex.page = 1; h.flex.cards = new Map(); h.flex.openBoards = new Set();
    const pending = []; h.context.tftFlexLobbyRequest = () => new Promise(resolve => pending.push(resolve));
    const originalRender = h.context.renderTftFlex; let renders = 0;
    h.context.renderTftFlex = () => { ++renders; return originalRender(); };
    h.context.renderTftFlex();
    const completion = h.context.beginTftFlexLobby(h.flex.catalog, h.flex.boards, h.flex.searchReq);
    await Promise.resolve();
    const gold = totalGold => ({ available: true, totalGold, refreshGold: totalGold - 36, purchaseGold: 36, method: 'exact-dp' });
    return { ...h, pending, completion, gold, renders: () => renders };
  };
  const h = await prepare(), initialRenders = h.renders(), initialTable = h.element('tft-flex-results').children[0];
  const baselines = h.flex.boards.map(board => board.gold), opened = await openFirstBoard(h);
  const rows = () => h.element('tft-flex-results').querySelectorAll('.tft-flex-summary').map(row => row.dataset.board);
  const before = rows(); assert.equal(h.pending.length, 31);
  h.pending[30](h.gold(60)); await Promise.resolve(); await Promise.resolve();
  assert(h.flex.boards[30].lobbyGold && !before.includes('lobby-30'));
  assert.deepEqual(rows(), before); assert.equal(h.renders(), initialRenders, 'Partial typical-lobby results do not reorder the table');
  assert.equal(h.element('tft-flex-results').children[0], initialTable);
  for (let index = 0; index < 29; ++index) h.pending[index](h.gold(100 + index));
  await Promise.resolve(); await Promise.resolve(); assert.equal(h.renders(), initialRenders);
  h.pending[29](h.gold(129)); await h.completion;
  assert.equal(h.renders(), initialRenders + 1); assert.equal(rows()[0], 'lobby-30', 'Typical-lobby order compares the entire pool before paging');
  assert(h.flex.boards.every((board, index) => board.gold === baselines[index]), 'Lobby gold never replaces uncontested expectations');
  assert.equal(h.flex.cards.get(opened.board.id), opened.card); assert.equal(h.element('tft-flex-results').querySelector('.tft-flex-card'), opened.card);
  assert(opened.card.querySelector('.tft-flex-lobby-gold').textContent.includes('≈100g'), 'Existing card nodes receive the new scenario estimate');
  const swapBoard = { ...h.flex.boards[0], id: 'swap-scenario' }, renderCount = h.renders();
  const swapBatch = h.context.beginTftFlexLobby(h.flex.catalog, [swapBoard], h.flex.searchReq, { sort: false });
  await Promise.resolve(); h.pending.at(-1)(h.gold(110)); await swapBatch;
  assert.equal(h.renders(), renderCount, 'A swap-estimate batch does not reorder the main comp table');
  const cancelled = await prepare(4), oldTable = cancelled.element('tft-flex-results').children[0], oldRenders = cancelled.renders();
  cancelled.context.cancelTftFlexLobby(); cancelled.pending.forEach(resolve => resolve(cancelled.gold(100))); await cancelled.completion;
  assert.equal(cancelled.renders(), oldRenders); assert.equal(cancelled.element('tft-flex-results').children[0], oldTable);
  assert(cancelled.flex.boards.every(board => !board.lobbyGold && board.gold), 'Cancelled lobby results preserve baseline and cannot fill the old scenario');
  const beforeDispatch = interfaceHarness(); await beforeDispatch.context.loadTftFlex();
  beforeDispatch.flex.catalog = beforeDispatch.context.tftFlexCatalog(lobbyFixture());
  let dispatched = 0; beforeDispatch.context.tftFlexLobbyRequest = async () => { ++dispatched; return { totalGold: 100 }; };
  const immediate = beforeDispatch.context.beginTftFlexLobby(beforeDispatch.flex.catalog, beforeDispatch.flex.boards, beforeDispatch.flex.searchReq);
  beforeDispatch.context.cancelTftFlexLobby(); await immediate;
  assert.equal(dispatched, 0, 'Immediate cancellation stops queued requests before they create a replacement worker');
  const failed = await prepare(1); failed.pending[0]({ available: false, unavailableTargets: [{ api: 'tank', available: 2, required: 3 }] }); await failed.completion;
  assert(failed.element('tft-flex-results').textContent.includes('Not enough copies'), 'An impossible pool has a clear diagnostic instead of a baseline fallback');
  assert(failed.flex.boards[0].gold && !Number.isFinite(failed.flex.boards[0].lobbyGold.totalGold));
}

async function checkLobbyInterface() {
  const h = interfaceHarness(), ctx = h.context, { workers, blobs } = installGoldWorkers(h), raw = lobbyFixture();
  const snapshot = JSON.stringify(raw); ctx.api = async () => raw;
  await ctx.loadTftFlex();
  await until(() => h.flex.boards.every(board => board.gold && (board.lobbyGold || board.lobbyError)), 'Both gold scenarios complete in their workers');
  assert(h.flex.boards.every(board => Number.isFinite(board.lobbyGold?.totalGold)),
    'Valid cached profiles produce finite typical-lobby results: ' + h.flex.boards.map(board => board.lobbyError || '').join('; '));
  assert.equal(h.flex.lobbyProfile, 'emerald-profile');
  assert.equal(Number(h.element('tft-flex-lobby-players').value), 7); assert.equal(Number(h.element('tft-flex-lobby-development').value), 50);
  assert.equal(h.element('tft-flex-lobby-profile').disabled, false);
  assert.equal(h.element('tft-flex-lobby-profile').children.length, 2);
  assert(h.element('tft-flex-lobby-profile').children.every(option => option.textContent.includes('18.3')));
  assert(h.element('tft-flex-lobby-context').textContent.includes('Emerald+') && h.element('tft-flex-lobby-context').textContent.includes('Source updated'));
  assert.equal(h.element('tft-flex-lobby-context').querySelector('a').href, 'https://tactics.tools/');
  assert(h.element('tft-flex-results').textContent.includes('Uncontested gold') && h.element('tft-flex-results').textContent.includes('Typical lobby gold'));
  const baselineWorker = workers.find(worker => worker.messages.some(message => message.requirements));
  const lobbyWorker = workers.find(worker => worker.messages.some(message => message.pool));
  assert(baselineWorker && lobbyWorker && baselineWorker !== lobbyWorker, 'Typical-lobby work uses a separate worker from the uncontested calculation');
  assert(lobbyWorker.messages.every(message => message.pool.length === raw.units.length), 'Worker requests include non-target opponents’ units as well as the desired roster');
  const boards = h.flex.boards, baselines = boards.map(board => board.gold), request = h.flex.searchReq, calls = h.calls.length;
  const { card } = await openFirstBoard(h), swaps = card.querySelector('.tft-flex-swaps');
  swaps.open = true; await swaps.fire('toggle');
  await until(() => h.flex.lobbySwapBoards.size && [...h.flex.lobbySwapBoards.values()].every(board => board.lobbyGold || board.lobbyError),
    'Opened support swaps receive the same lobby scenario');
  assert(swaps.textContent.includes('Typical lobby gold') && swaps.querySelectorAll('.tft-flex-lobby-cell').length,
    'Swap rows show both uncontested and typical-lobby gold');
  const swapNodes = swaps.querySelectorAll('.tft-flex-lobby-cell').slice();
  const epoch = h.flex.lobbyReq;
  h.element('tft-flex-lobby-players').value = '4'; await h.element('tft-flex-lobby-players').fire('change');
  await until(() => [...boards, ...h.flex.lobbySwapBoards.values()].every(board => board.lobbyGold || board.lobbyError), 'Opponent-count change completes without a new trait search');
  assert.equal(h.flex.lobbyPlayers, 4); assert(h.flex.lobbyReq > epoch); assert(lobbyWorker.terminated && !baselineWorker.terminated);
  assert.equal(h.flex.searchReq, request); assert.equal(h.flex.boards, boards);
  assert(boards.every((board, index) => board.gold === baselines[index]), 'Changing the lobby keeps every uncontested result intact');
  assert.equal(h.flex.cards.get(boards[0].id), card); assert.equal(swaps.open, true);
  assert(swaps.querySelectorAll('.tft-flex-lobby-cell').every((node, index) => node === swapNodes[index]), 'Scenario changes update existing swap cells');
  h.element('tft-flex-lobby-development').value = '0'; await h.element('tft-flex-lobby-development').fire('change');
  await until(() => boards.every(board => board.lobbyGold), 'Zero development completes');
  assert(boards.every((board, index) => Math.abs(board.lobbyGold.totalGold - baselines[index].totalGold) < 1e-6),
    'Zero inferred holdings recover each board’s exact uncontested gold');
  h.element('tft-flex-lobby-profile').value = 'gold-profile'; await h.element('tft-flex-lobby-profile').fire('change');
  await until(() => boards.every(board => board.lobbyGold), 'Changing a cached rank profile completes');
  assert.equal(h.flex.lobbyProfile, 'gold-profile'); assert(h.element('tft-flex-lobby-context').textContent.includes('Gold'));
  h.element('tft-flex-lobby-development').value = '50'; await h.element('tft-flex-lobby-development').fire('change');
  await until(() => boards.every(board => board.lobbyGold), 'Development adjustment completes');
  const scout = slug => h.element('tft-flex-lobby-list').querySelectorAll('input').find(input => input.dataset.unit === slug);
  scout('tank').value = '0'; await scout('tank').fire('change'); await until(() => boards.every(board => board.lobbyGold), 'Explicit zero scout completes');
  assert(Object.prototype.hasOwnProperty.call(h.flex.lobbyOverrides, 'tank') && h.flex.lobbyOverrides.tank === 0);
  assert.equal(scout('tank').value, '0');
  const beforeSearch = h.flex.lobbyReq; h.element('tft-flex-lobby-search').value = 'OTHER FOUR'; await h.element('tft-flex-lobby-search').fire('input');
  assert.equal(h.flex.lobbyReq, beforeSearch); assert.equal(h.element('tft-flex-lobby-list').querySelectorAll('input').length, 1, 'Scouting search filters controls without recalculating estimates');
  scout('other-four').value = '8'; await scout('other-four').fire('change'); await until(() => boards.every(board => board.lobbyGold), 'Non-target scout completes');
  assert.equal(h.flex.lobbyOverrides['other-four'], 8);
  const latestPool = workers.filter(worker => !worker.terminated && worker.messages.some(message => message.pool)).at(-1).messages.at(-1).pool;
  assert.equal(latestPool.find(unit => unit.api === 'tank').heldCopies, 0); assert.equal(latestPool.find(unit => unit.api === 'other-four').heldCopies, 8);
  h.element('tft-flex-lobby-search').value = ''; await h.element('tft-flex-lobby-search').fire('input');
  scout('tank').value = '8'; await scout('tank').fire('change'); await until(() => boards.every(board => board.lobbyGold), 'Insufficient-copy diagnostics complete');
  assert(boards.every(board => board.lobbyGold.available === false && board.gold), 'A held target can become impossible while the baseline stays available');
  assert(card.textContent.includes('Not enough copies') && h.element('tft-flex-results').textContent.includes('Not enough copies'));
  scout('tank').value = ''; await scout('tank').fire('change'); await until(() => boards.every(board => board.lobbyGold), 'Blank scout restores inferred holdings');
  assert(!Object.prototype.hasOwnProperty.call(h.flex.lobbyOverrides, 'tank')); assert.equal(scout('tank').value, '');
  await h.element('tft-flex-lobby-reset').fire('click'); await until(() => boards.every(board => board.lobbyGold), 'Clearing scouts completes');
  assert.deepEqual(plain(h.flex.lobbyOverrides), {}); assert.equal(h.element('tft-flex-lobby-reset').disabled, true);
  assert.equal(h.flex.searchReq, request); assert.equal(h.calls.length, calls, 'Lobby controls read the published cache without any new API or trait-search request');
  assert.equal(JSON.stringify(raw), snapshot);
  ctx.cancelTftFlex(); assert(workers.every(worker => worker.terminated) && !blobs.size, 'Leaving Trait flex stops both workers and releases their scripts');

  const old = interfaceHarness(), oldWorkers = installGoldWorkers(old), oldRaw = lobbyFixture(); delete oldRaw.lobbyStats;
  old.context.api = async () => oldRaw; await old.context.loadTftFlex(); await until(() => old.flex.boards.every(board => board.gold), 'Old catalogs still calculate uncontested gold');
  assert(old.flex.boards.every(board => !board.lobbyGold && board.gold)); assert.equal(old.element('tft-flex-lobby-profile').disabled, true);
  assert(old.element('tft-flex-lobby-context').textContent.includes('unavailable'));
  assert.equal(oldWorkers.workers.length, 1, 'Missing statistics never silently run a baseline estimate as typical-lobby gold');
  const oldCard = (await openFirstBoard(old)).card; assert(/unavailable/i.test(oldCard.querySelector('.tft-flex-lobby-gold').textContent));
  old.context.cancelTftFlex();
  const failure = interfaceHarness(), failedWorkers = installGoldWorkers(failure); failure.context.api = async () => lobbyFixture();
  await failure.context.loadTftFlex();
  const brokenWorker = failedWorkers.workers.find(worker => worker.messages.some(message => message.pool)); brokenWorker.onerror();
  await until(() => failure.flex.boards.every(board => board.lobbyError && board.gold), 'A lobby worker failure keeps the baseline available');
  assert(failure.flex.boards.every(board => !board.lobbyGold && board.gold));
  assert(failure.element('tft-flex-results').querySelectorAll('.tft-flex-lobby-cell').every(cell => cell.textContent === 'Unavailable'));
  await failure.context.recalculateTftFlexLobby();
  assert(failure.flex.boards.every(board => board.lobbyGold && !board.lobbyError), 'Changing or retrying lobby assumptions can recover a failed scenario');
  failure.context.cancelTftFlex();
}

async function checkRealCatalog() {
  const args = process.argv.slice(2);
  if (!args.length) return;
  assert.equal(args[0], '--catalog'); assert.equal(args.length, 2);
  const raw = JSON.parse(fs.readFileSync(args[1], 'utf8')), ctx = algorithmContext, catalog = ctx.tftFlexCatalog(raw);
  const tanks = catalog.units.filter(unit => unit.cost === 4 && unit.frontline);
  const carries = catalog.units.filter(unit => unit.cost === 4 && unit.backline);
  let checked = 0; const rosters = new Set();
  for (const tank of tanks) for (const carry of carries) {
    const options = { tank: tank.slug, carry: carry.slug, minFrontline: 3, preference: 'breadth', yieldTask: async () => {} };
    const result = await ctx.tftFlexSearch(catalog, options);
    assert(result.boards.length, `${tank.name} + ${carry.name} finds a complete board`);
    assert(result.boards.every(board => ctx.tftFlexEvaluate(catalog, board.units, options).valid));
    result.boards.forEach(board => rosters.add(board.id));
    ++checked;
  }
  const browsed = await ctx.tftFlexBrowse(catalog, { minFrontline: 3, preference: 'breadth', yieldTask: async () => {} });
  assert.equal(browsed.pairsDone, checked); assert.equal(browsed.pairsTotal, checked);
  assert.deepEqual([...browsed.boards.map(board => board.id)].sort(), [...rosters].sort(), 'Real all-pair browsing merges the independent shortlists without loss or duplicates');
  assert(browsed.boards.every(board => board.anchorPairs.every(pair => ctx.tftFlexEvaluate(catalog, board.units,
    { ...pair, minFrontline: 3, preference: 'breadth' }).valid)), 'Every real merged anchor pair preserves board legality');
  for (const cost of [2, 3]) {
    const front = catalog.units.find(unit => unit.cost === cost && unit.frontline);
    const back = catalog.units.find(unit => unit.cost === cost && unit.backline);
    assert(front && back, 'The complete real catalog supplies both roles at core cost ' + cost);
    const options = { cost, tank: front.slug, carry: back.slug, minFrontline: 3,
      preference: 'breadth', yieldTask: async () => {} };
    const result = await ctx.tftFlexSearch(catalog, options);
    assert(result.boards.length && result.boards.every(board => ctx.tftFlexEvaluate(catalog, board.units, options).valid &&
      board.units.filter(unit => unit.cost === 4).length <= (cost === 2 ? 2 : 3)),
      'Real lower-cost core searches find legal capped boards without reducing the full catalog');
    assert(result.boards.every(board => board.units.every(unit => catalog.unitsBySlug.has(unit.slug))), 'Every real support comes from the same published catalog');
    const swaps = await ctx.tftFlexSwaps(catalog, result.boards[0], { ...options, maxScoreLoss: 100 });
    assert(swaps.swaps.length && swaps.swaps.every(swap => swap.board.units.filter(unit => unit.cost === 4).length <= (cost === 2 ? 2 : 3)),
      'Real lower-cost support swaps preserve the selected four-cost cap');
    console.log(`Real ${cost}-cost core checks passed: ${front.name} + ${back.name}, ${result.boards.length} boards, ${swaps.swaps.length} legal swaps, full ${catalog.units.length}-unit pool.`);
  }
  const malphite = catalog.units.find(unit => unit.name === 'Malphite' && unit.cost === 4 && unit.frontline);
  const zyra = catalog.units.find(unit => unit.name === 'Zyra' && unit.cost === 4 && unit.backline);
  if (malphite && zyra) {
    const monolith = catalog.traits.find(trait => trait.name === 'Monolith'), thornmaiden = catalog.traits.find(trait => trait.name === 'Thornmaiden');
    assert(monolith?.levels.includes(1) && thornmaiden?.levels.includes(1));
    const otherAnchorTraits = new Set([...malphite.traitApis, ...zyra.traitApis].filter(api => ![monolith.api, thornmaiden.api].includes(api)));
    const withoutPartners = ctx.tftFlexCatalog({ ...raw, units: raw.units.filter(unit => [malphite.slug, zyra.slug].includes(unit.slug) ||
      unit.traitApis.every(api => !otherAnchorTraits.has(api))) });
    for (const preference of ['breadth', 'vertical']) {
      const options = { tank: malphite.slug, carry: zyra.slug, minFrontline: 3, preference, yieldTask: async () => {} };
      const soloBoards = await ctx.tftFlexSearch(withoutPartners, options);
      assert(soloBoards.boards.length, 'Real Malphite + Zyra can build a board without Blackthorn or Summoner helpers');
      for (const board of soloBoards.boards) {
        assert.deepEqual(plain(board.anchorCoverage.tank).map(trait => trait.api), [monolith.api]);
        assert.deepEqual(plain(board.anchorCoverage.carry).map(trait => trait.api), [thornmaiden.api]);
        assert(board.activeTraits.some(trait => trait.api === monolith.api && trait.count === 1 && trait.breakpoint === 1));
        assert(board.activeTraits.some(trait => trait.api === thornmaiden.api && trait.count === 1 && trait.breakpoint === 1));
        assert(!board.units.some(unit => ![malphite.slug, zyra.slug].includes(unit.slug) && unit.traitApis.some(api => otherAnchorTraits.has(api))));
        const reached = catalog.traits.map(trait => ({ api: trait.api, count: board.units.reduce((sum, unit) => sum + (unit.traitContributions[trait.api] || 0), 0),
          levels: trait.levels }));
        const activeCount = reached.filter(trait => trait.levels.some(level => level <= trait.count)).length;
        const extraTiers = reached.reduce((sum, trait) => sum + Math.max(0, trait.levels.filter(level => level <= trait.count).length - 1), 0);
        assert.equal(board.activeCount, activeCount); assert.equal(board.extraTiers, extraTiers);
        assert.equal(board.score, preference === 'breadth' ? 3 * activeCount + extraTiers : activeCount + 3 * extraTiers,
          'The real solo-covered board’s score matches an independent count of all reached tiers');
      }
      const kept = soloBoards.boards[0].units.filter(unit => ![malphite.slug, zyra.slug].includes(unit.slug)).map(unit => unit.slug);
      const seeded = await ctx.tftFlexSearch(catalog, { ...options, locked: kept });
      assert.equal(seeded.boards.length, 1); assert.equal(seeded.expanded, 0, 'The full real catalog accepts the same complete owned roster without partners');
      assert.deepEqual(plain(seeded.boards[0].anchorCoverage.tank).map(trait => trait.api), [monolith.api]);
      assert.deepEqual(plain(seeded.boards[0].anchorCoverage.carry).map(trait => trait.api), [thornmaiden.api]);
    }
    console.log('Real solo-anchor checks passed: Malphite Monolith + Zyra Thornmaiden cover both anchors without Blackthorn or Summoner helpers, under both preferences.');
  }
  const lobbyProfiles = ctx.tftFlexLobbyProfiles(catalog);
  if (lobbyProfiles.length) {
    const board = browsed.boards[0], targets = ctx.tftFlexLobbyTargets(board);
    for (const profile of lobbyProfiles) {
      const pool = ctx.tftFlexLobbyPool(catalog, profile, { players: 7, development: .5 });
      assert.equal(pool.length, catalog.units.length);
      const rows = new Map(profile.units.map(unit => [unit.api, unit]));
      assert(pool.every(unit => unit.heldCopies === Math.min(catalog.shop.bagCopies[unit.cost - 1], Math.round(rows.get(unit.api).copiesPerPlayer * 7 * .5))),
        'Real cached play-rate/copy aggregates produce the documented inferred holdings');
      const summary = ctx.tftFlexLobbyEstimate(catalog.shop, pool, targets, { signatureOnly: true });
      const remaining = [1, 2, 3, 4, 5].map(cost => pool.filter(unit => unit.cost === cost)
        .reduce((sum, unit) => sum + catalog.shop.bagCopies[cost - 1] - unit.heldCopies, 0));
      assert.deepEqual(plain(summary.remainingByCost), remaining, 'Every real same-tier holding contributes to the shop denominator');
    }
    const empty = ctx.tftFlexLobbyPool(catalog, lobbyProfiles[0], { players: 0, development: 0 });
    const emptyEstimate = ctx.tftFlexLobbyEstimate(catalog.shop, empty, targets);
    const baselineRequirements = [1, 2, 3, 4, 5].map(cost => board.units.filter(unit => unit.cost === cost).length);
    const baseline = ctx.tftFlexGoldEstimate(catalog.shop, baselineRequirements);
    assert(Math.abs(emptyEstimate.totalGold - baseline.totalGold) < 1e-6, 'The real full pool recovers the uncontested expectation when no copies are held');
    console.log(`Real lobby profile checks passed: ${lobbyProfiles.length} matching patch/rank caches, full-pool inferred holdings and exact zero-hold baseline.`);
  }
  console.log(`Real catalog checks passed: ${catalog.units.length} units, ${catalog.traits.length} traits, ${checked} frontline/backline anchor pairs, ${browsed.boards.length} distinct browsable comps.`);
}

checkScoring();
checkSearchAndSwaps().then(checkSoloAnchorSearchAndSwaps).then(checkBrowsingAndSorting).then(checkInterface).then(checkCoreBrowseInterface).then(checkModeAndRouting)
  .then(checkMinimumCost).then(checkCoreCosts).then(checkGenericCores).then(checkAutomaticCoreBrowse).then(checkCorePickerInitial).then(checkInitialBrowseProgress).then(checkCoreSelectionCancellation).then(checkCoreCostInterface).then(checkCoreGoldTargets).then(checkCoreGoldWorkers).then(checkCoreGoldInterface).then(checkGoldInterface).then(checkGoldSortCompletion).then(checkLobbyPools).then(checkLobbyCompletion).then(checkLobbyInterface).then(checkRealCatalog).then(() => {
  console.log('Trait flex checks passed: solo and shared breakpoints, completed tiers, anchor coverage, deterministic rankings and diverse packages, full occupancy, cost pools and owned locks.');
  console.log('Trait flex interface checks passed: controls, hash restoration, units-only cards and tooltips, planner copy, legal swaps, bounded yielding/cancellation, pending catalogs, retries and publication races.');
  console.log('Trait flex navigation checks passed: entering, opening and polling load no combat data; catalog revisions are independent of simulation revisions.');
  console.log('Trait flex browser checks passed: explicit multiselect, visible core names, shared-link sorting, full-pool gold order, pagination, cached details and core-preserving swaps; legacy engine pair browsing remains compatible.');
  console.log('Core checks passed: automatic empty/one-core pair browsing, deduplicated corePairs, progress and stale-result cancellation, fixed 2+ same-cost champions of any roles, all-core coverage, caps, portrait filtering, tier resets, kept supports and fcore links.');
  console.log('Core gold checks passed: nine-copy targets for every selected-tier unit, one-copy other-tier supports, saved board tiers, baseline/lobby arithmetic and shortages, isolated worker/cache profiles, purchase sorting, tooltips, method tables and support swaps; four-cost targets remain unchanged.');
  console.log('Typical lobby checks passed: cached patch/rank profiles, rounded held copies and zero scouts, separate worker/epoch, baseline and swap preservation, complete-pool sorting, impossible/failure diagnostics, and old-catalog fallback.');
}).catch(error => { console.error(error); process.exitCode = 1; });
