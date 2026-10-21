// Run: node jobs/test-tft-flex-gold.cjs [--engine /path/to/engine.js]
// Pure regression checks for the inline, dependency-free expected gold model.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { performance } = require('node:perf_hooks');
const args = process.argv.slice(2);
assert(args.length === 0 || (args.length === 2 && args[0] === '--engine'), 'Expected --engine /path/to/engine.js');
const startMarker = '// ---------------- trait flex: gold estimates ----------------';
const endMarker = '// ---------------- trait flex: gold estimates end ----------------';
const source = fs.readFileSync(args.length ? args[1] : path.join(__dirname, '../web/index.html'), 'utf8');
const start = source.indexOf(startMarker), end = source.indexOf(endMarker);
assert(start >= 0 && end > start, 'The gold estimate engine must have its extraction markers');
const context = vm.createContext({});
vm.runInContext(source.slice(start, end), context);
const lobbyStartMarker = '// ---------------- trait flex: lobby gold estimates ----------------';
const lobbyEndMarker = '// ---------------- trait flex: lobby gold estimates end ----------------';
const lobbySource = source.includes(lobbyStartMarker) ? source : fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const lobbyStart = lobbySource.indexOf(lobbyStartMarker), lobbyEnd = lobbySource.indexOf(lobbyEndMarker);
assert(lobbyStart >= 0 && lobbyEnd > lobbyStart, 'Reroll gold needs the self-contained generic quota solver');
vm.runInContext(lobbySource.slice(lobbyStart, lobbyEnd), context);
const estimate = context.tftFlexGoldEstimate;
assert.equal(typeof estimate, 'function');
const plain = value => JSON.parse(JSON.stringify(value));
function near(actual, expected, description, relative = 2e-11) {
  assert(Number.isFinite(actual), description + ': must be finite');
  assert(Math.abs(actual - expected) <= relative * Math.max(1, Math.abs(expected)),
    description + ': expected ' + expected + ', received ' + actual);
}
const shop = { costOdds: [.15, .20, .32, .30, .03], championCounts: [14, 13, 14, 14, 10],
  bagCopies: [30, 25, 18, 10, 9], refreshCost: 2, visibleSlots: [4, 5] };
const one = cost => Array.from({ length: 5 }, (_, index) => index === cost ? 1 : 0);
const started = performance.now();

function checkGoldAndInputs() {
  const requirements = [2, 1, 2, 3, 0], snapshot = JSON.stringify({ shop, requirements });
  const result = estimate(shop, requirements);
  assert.equal(JSON.stringify({ shop, requirements }), snapshot, 'The estimator preserves inputs');
  assert.deepEqual(plain(result.copiesByCost), [2, 1, 2, 9, 0], 'Every 4-cost needs three copies; every other champion needs one');
  assert.equal(result.purchaseGold, 46);
  near(result.refreshGold, result.expectedShops * 2, 'Every generated shop costs 2 gold');
  near(result.totalGold, result.refreshGold + result.purchaseGold, 'Purchase and refresh gold add up');
  assert.deepEqual(plain(result), plain(estimate(shop, requirements)), 'Expected values are deterministic');
  result.copiesByCost[3] = 99;
  assert.equal(estimate(shop, requirements).copiesByCost[3], 9, 'Output mutation cannot corrupt another estimate');
  const frozenShop = Object.freeze(Object.fromEntries(Object.entries(shop).map(([name, value]) =>
    [name, Array.isArray(value) ? Object.freeze(value.slice()) : value])));
  near(estimate(frozenShop, Object.freeze(requirements.slice())).expectedShops,
    result.expectedShops, 'Read-only inputs are supported');
  const empty = estimate(shop, [0, 0, 0, 0, 0]);
  assert.equal(empty.expectedShops, 0); assert.equal(empty.totalGold, 0);
  assert.deepEqual(plain(empty.copiesByCost), [0, 0, 0, 0, 0]);
  near(estimate({ ...shop, visibleSlots: undefined }, requirements).expectedShops,
    result.expectedShops, 'The default visibility cycle is four and five');
  const free = estimate({ ...shop, refreshCost: 0 }, one(0));
  assert.equal(free.refreshGold, 0); assert.equal(free.totalGold, 1);
  for (const impossibleShop of [
    { ...shop, championCounts: [0, 13, 14, 14, 10] },
    { ...shop, costOdds: [0, .35, .32, .30, .03] },
  ]) {
    const impossible = estimate(impossibleShop, one(0));
    assert.equal(impossible.expectedShops, Infinity);
    assert.equal(impossible.totalGold, Infinity);
    assert(impossible.diagnostics.impossible);
  }
  assert.equal(estimate({ ...shop, bagCopies: [30, 25, 18, 2, 9] }, one(3)).expectedShops, Infinity);
  assert.equal(estimate({ ...shop, championCounts: [1, 13, 14, 14, 10] }, [2, 0, 0, 0, 0]).expectedShops, Infinity);
  assert.throws(() => estimate(shop, [1, 0, -1, 0, 0]), /nonnegative/);
  assert.throws(() => estimate(shop, [9, 0, 0, 0, 0]), /at most eight/);
  assert.throws(() => estimate({ ...shop, costOdds: [.1, .1, .1, .1, .1] }, one(0)), /sum to one/);
  assert.throws(() => estimate({ ...shop, visibleSlots: [] }, one(0)), /visible shop/);
  assert.throws(() => estimate({ ...shop, bagCopies: [0, 25, 18, 10, 9] }, one(0)), /bagCopies/);

  // A Blob Worker can evaluate the function's source without any page globals.
  const isolated = vm.createContext({});
  vm.runInContext(estimate.toString(), isolated);
  near(isolated.tftFlexGoldEstimate(shop, one(3)).refreshGold,
    estimate(shop, one(3)).refreshGold, 'The function is self-contained for a Worker');
}

function checkIndependentSingleChampionValues() {
  // Independently derived exact, finite-pool values for the published bags.
  const alternating = [42.2424644521, 29.6484442234, 20.1975118701, 69.7028503288, 148.8480170819];
  const fiveSlots = [38.1168676, 26.7823261, 18.2761402, 62.8042186, 134.0546009];
  for (let cost = 0; cost < 5; ++cost) {
    near(estimate(shop, one(cost)).refreshGold, alternating[cost], 'Alternating-shop golden cost ' + (cost + 1));
    near(estimate({ ...shop, visibleSlots: [5] }, one(cost)).refreshGold,
      fiveSlots[cost], 'Five-slot golden cost ' + (cost + 1), 4e-9);
  }
  const onlyOneCost = cost => ({ ...shop, costOdds: one(cost), visibleSlots: [1] });
  near(estimate(onlyOneCost(0), one(0)).expectedShops, shop.championCounts[0],
    'One visible slot finds a non-4-cost in one champion-count waiting time');
  const count = shop.championCounts[3], bag = shop.bagCopies[3];
  const fourWait = [0, 1, 2].reduce((sum, owned) => sum + (count * bag - owned) / (bag - owned), 0);
  near(estimate(onlyOneCost(3), one(3)).expectedShops, fourWait,
    'Three copies have three independently derived depleted-pool waiting times');
  const certainFour = { ...onlyOneCost(3), championCounts: [14, 13, 14, 1, 10] };
  assert.equal(estimate(certainFour, one(3)).expectedShops, 3, 'There is no free initial shop');
  assert.equal(estimate({ ...certainFour, visibleSlots: [2] }, one(3)).expectedShops, 2);
  assert.equal(estimate({ ...certainFour, visibleSlots: [5] }, one(3)).expectedShops, 1);

  // Hypergeometric failure probabilities exercise without-replacement draws.
  const hypergeometricFailure = slots => {
    let failure = 1;
    for (let draw = 0; draw < slots; ++draw) {
      failure *= (14 * 30 - 30 - draw) / (14 * 30 - draw);
    }
    return failure;
  };
  const q4 = hypergeometricFailure(4), q5 = hypergeometricFailure(5);
  const tierOnly = { ...onlyOneCost(0), visibleSlots: [4, 5] };
  const expectedCycle = (2 + q4 + q5) / (2 * (1 - q4 * q5));
  const cycle = estimate(tierOnly, one(0));
  near(cycle.expectedShops, expectedCycle, 'A one-unit hypergeometric four/five cycle');
  near(cycle.diagnostics.shopsByInitialPhase[0], (1 + q4) / (1 - q4 * q5), 'Four-slot first phase');
  near(cycle.diagnostics.shopsByInitialPhase[1], (1 + q5) / (1 - q4 * q5), 'Five-slot first phase');
  near(estimate({ ...tierOnly, visibleSlots: [5] }, one(0)).expectedShops,
    1 / (1 - q5), 'A one-unit hypergeometric fixed five-slot shop');
  const exact = estimate({ ...shop, visibleSlots: [5] }, one(0)).expectedShops;
  const replacementApproximation = 1 / (1 - Math.pow(1 - shop.costOdds[0] / shop.championCounts[0], 5));
  assert(exact < replacementApproximation - .001, 'Sampling the shop without replacement differs from independent slots');

  const rare = { costOdds: [1e-16, 1 - 1e-16, 0, 0, 0], championCounts: [1, 1, 0, 0, 0],
    bagCopies: [30, 25, 1, 1, 1], visibleSlots: [1], refreshCost: 2 };
  near(estimate(rare, one(0)).expectedShops, 1e16, 'Tiny success probabilities avoid cancellation');
}

// Independent reference: track each target's identity, enumerate every ordered
// champion sequence in each shop, then solve the complete Markov linear system.
// It does not use grouped states, affine continuation or the engine's recurrence.
function exhaustiveReference(model, requirements) {
  const champions = [], targets = [];
  for (let cost = 0; cost < 5; ++cost) for (let index = 0; index < model.championCounts[cost]; ++index) {
    const target = index < requirements[cost] ? targets.length : -1;
    if (target >= 0) targets.push({ cost, required: cost === 3 ? 3 : 1 });
    champions.push({ cost, copies: model.bagCopies[cost], target });
  }
  const radices = targets.map(target => target.required + 1), strides = [1];
  for (let axis = 1; axis < radices.length; ++axis) strides.push(strides[axis - 1] * radices[axis - 1]);
  const stateCount = radices.reduce((product, radix) => product * radix, 1), terminal = stateCount - 1;
  const codeFor = owned => owned.reduce((code, count, axis) => code + count * strides[axis], 0);
  const ownedFor = code => radices.map((radix, axis) => Math.floor(code / strides[axis]) % radix);
  function shopDistribution(code, slots) {
    const owned = ownedFor(code), available = champions.map(champion =>
      champion.copies - (champion.target >= 0 ? owned[champion.target] : 0));
    const distribution = new Map();
    function visit(remaining, probability) {
      const after = codeFor(owned);
      if (!remaining || after === terminal) {
        distribution.set(after, (distribution.get(after) || 0) + probability);
        return;
      }
      const totals = new Array(5).fill(0);
      champions.forEach((champion, index) => totals[champion.cost] += available[index]);
      for (let index = 0; index < champions.length; ++index) {
        const champion = champions[index], copies = available[index];
        if (!copies || !model.costOdds[champion.cost]) continue;
        const chance = model.costOdds[champion.cost] * copies / totals[champion.cost];
        const buy = champion.target >= 0 && owned[champion.target] < targets[champion.target].required;
        --available[index]; if (buy) ++owned[champion.target];
        visit(remaining - 1, probability * chance);
        ++available[index]; if (buy) --owned[champion.target];
      }
    }
    visit(slots, 1);
    near([...distribution.values()].reduce((sum, chance) => sum + chance, 0), 1,
      'Exhaustive shop transition probabilities conserve mass');
    return distribution;
  }
  const phases = model.visibleSlots.length, rows = terminal * phases;
  const matrix = Array.from({ length: rows }, () => new Float64Array(rows + 1));
  for (let code = 0; code < terminal; ++code) for (let phase = 0; phase < phases; ++phase) {
    const row = code * phases + phase;
    matrix[row][row] = 1; matrix[row][rows] = 1;
    const nextPhase = (phase + 1) % phases;
    for (const [after, chance] of shopDistribution(code, model.visibleSlots[phase])) {
      if (after !== terminal) matrix[row][after * phases + nextPhase] -= chance;
    }
  }
  for (let column = 0; column < rows; ++column) {
    let pivot = column;
    for (let row = column + 1; row < rows; ++row) if (Math.abs(matrix[row][column]) > Math.abs(matrix[pivot][column])) pivot = row;
    assert(Math.abs(matrix[pivot][column]) > 1e-14, 'Reference Markov system has a unique solution');
    [matrix[column], matrix[pivot]] = [matrix[pivot], matrix[column]];
    const divisor = matrix[column][column];
    for (let entry = column; entry <= rows; ++entry) matrix[column][entry] /= divisor;
    for (let row = 0; row < rows; ++row) if (row !== column && matrix[row][column]) {
      const factor = matrix[row][column];
      for (let entry = column; entry <= rows; ++entry) matrix[row][entry] -= factor * matrix[column][entry];
    }
  }
  return Array.from({ length: phases }, (_, phase) => matrix[phase][rows]);
}

function checkExhaustiveTinyPools() {
  const models = [
    { model: { costOdds: [.35, 0, 0, .65, 0], championCounts: [2, 0, 0, 3, 0],
      bagCopies: [3, 1, 1, 4, 1], refreshCost: 2, visibleSlots: [2, 3] }, requirements: [1, 0, 0, 2, 0] },
    { model: { costOdds: [1, 0, 0, 0, 0], championCounts: [3, 0, 0, 0, 0],
      bagCopies: [2, 1, 1, 1, 1], refreshCost: 2, visibleSlots: [2] }, requirements: [2, 0, 0, 0, 0] },
    { model: { costOdds: [0, 0, 0, 1, 0], championCounts: [0, 0, 0, 3, 0],
      bagCopies: [1, 1, 1, 4, 1], refreshCost: 2, visibleSlots: [2] }, requirements: [0, 0, 0, 2, 0] },
  ];
  for (const { model, requirements } of models) {
    const independent = exhaustiveReference(model, requirements), result = estimate(model, requirements);
    near(result.expectedShops, independent.reduce((sum, value) => sum + value / independent.length, 0),
      'Grouped DP agrees with exhaustive champion-identity Markov chains');
    for (let phase = 0; phase < independent.length; ++phase) {
      near(result.diagnostics.shopsByInitialPhase[phase], independent[phase], 'Independent initial shop phase ' + phase);
    }
  }
}

function checkJointCollectionAndBoundedStates() {
  const requirements = [1, 1, 1, 2, 1], joint = estimate(shop, requirements);
  const separate = requirements.reduce((sum, count, cost) => sum + count * estimate(shop, one(cost)).expectedShops, 0);
  assert(joint.expectedShops < separate * .75, 'One rolldown collects multiple targets together; summing separate costs overcounts');
  const maximum = estimate(shop, [1, 1, 1, 4, 1]);
  assert.equal(maximum.diagnostics.states, 560, 'The largest eight-unit grouped state grid has 560 states');
  near(maximum.purchaseGold, 59, 'All additional 4-cost supports also require 2 stars');
  near(estimate({ ...shop, refreshCost: 3 }, requirements).expectedShops,
    joint.expectedShops, 'Refresh prices do not change hit probabilities');
  near(estimate({ ...shop, refreshCost: 3 }, requirements).refreshGold,
    joint.expectedShops * 3, 'Refresh gold scales with price');
}

function checkRerollProfilesAndWorkerBundle() {
  const pool = shop.championCounts.flatMap((count, cost) => Array.from({ length: count }, (_, index) =>
    ({ api: 'pool-' + cost + '-' + index, cost: cost + 1, heldCopies: 0 })));
  for (const coreCost of [2, 3]) {
    const requirements = coreCost === 2 ? [1, 4, 1, 2, 0] : [1, 1, 4, 2, 0];
    const before = JSON.stringify({ shop, requirements });
    const result = estimate(shop, requirements, coreCost);
    const expectedCopies = coreCost === 2 ? [1, 36, 1, 2, 0] : [1, 1, 36, 2, 0];
    assert.deepEqual(plain(result.copiesByCost), expectedCopies,
      'Every selected-tier champion, including supports, needs nine copies');
    assert.equal(result.purchaseGold, coreCost === 2 ? 84 : 119,
      'Purchase gold charges all reroll copies and one copy of four-cost supports');
    near(result.totalGold, result.purchaseGold + result.expectedShops * 2, 'Reroll refresh and purchase gold add up');
    assert.equal(result.method, 'monte-carlo');
    assert(result.standardErrorGold > 0 && Number.isFinite(result.standardErrorGold));
    assert(result.confidence95 && result.diagnostics.samplingIntervalOnly);
    assert.equal(JSON.stringify({ shop, requirements }), before, 'Reroll estimation preserves its inputs');
    const targets = requirements.flatMap((count, cost) => pool.filter(unit => unit.cost === cost + 1).slice(0, count)
      .map(unit => ({ api: unit.api, cost: cost + 1, copies: cost + 1 === coreCost ? 9 : 1 })));
    const reference = context.tftFlexLobbyEstimate(shop, pool, targets);
    near(result.expectedShops, reference.expectedShops, 'Reroll baseline is the zero-held finite-pool model');
    near(result.totalGold, reference.totalGold, 'Baseline and zero-held lobby estimate agree');
    near(estimate(shop, requirements, String(coreCost)).totalGold, result.totalGold, 'Dropdown cost strings normalize');
    const frozenShop = Object.freeze(Object.fromEntries(Object.entries(shop).map(([key, value]) =>
      [key, Array.isArray(value) ? Object.freeze(value.slice()) : value])));
    near(estimate(frozenShop, Object.freeze(requirements.slice()), coreCost).totalGold,
      result.totalGold, 'Read-only reroll inputs are supported');
    const oneSlot = { ...shop, visibleSlots: [1] }, wanted = one(coreCost - 1);
    const bag = shop.bagCopies[coreCost - 1], total = shop.championCounts[coreCost - 1] * bag;
    const hand = Array.from({ length: 9 }, (_, owned) =>
      (total - owned) / (shop.costOdds[coreCost - 1] * (bag - owned))).reduce((sum, waiting) => sum + waiting, 0);
    const single = estimate(oneSlot, wanted, coreCost);
    assert.equal(single.method, 'exact');
    near(single.expectedShops, hand, 'Nine-copy native one-visible-slot analytic expectation at cost ' + coreCost);
    assert.equal(single.purchaseGold, coreCost * 9);
    const tooFew = shop.bagCopies.slice(); tooFew[coreCost - 1] = 8;
    assert.equal(estimate({ ...shop, bagCopies: tooFew }, wanted, coreCost).totalGold, Infinity,
      'A nine-copy target cannot be completed from an eight-copy bag');
    const impossibleOdds = shop.costOdds.slice(); impossibleOdds[0] += impossibleOdds[coreCost - 1]; impossibleOdds[coreCost - 1] = 0;
    assert.equal(estimate({ ...shop, costOdds: impossibleOdds }, wanted, coreCost).expectedShops, Infinity);
    assert.equal(estimate(shop, [0, 0, 0, 0, 0], coreCost).totalGold, 0);
  }
  const fourSupport = estimate(shop, one(3), 2), fourCore = estimate(shop, one(3), 4);
  assert.equal(fourSupport.purchaseGold, 4);
  assert.equal(fourCore.purchaseGold, 12);
  assert(fourSupport.totalGold < fourCore.totalGold, 'A one-copy support is cheaper than the old two-star four-cost target');
  assert.deepEqual(plain(estimate(shop, [1, 1, 1, 4, 1], 4)), plain(estimate(shop, [1, 1, 1, 4, 1])),
    'The explicit four-cost profile preserves the old exact output');
  for (const coreCost of [1, 5, 2.5, NaN, 'mixed'])
    assert.throws(() => estimate(shop, one(0), coreCost), /core cost/);

  // Both functions are bundled for the reroll Worker; the pre-existing standalone
  // four-cost Worker test above still executes the baseline function alone.
  const isolated = vm.createContext({});
  vm.runInContext(context.tftFlexLobbyEstimate.toString() + '\n' + estimate.toString(), isolated);
  for (const cost of [2, 3, 4]) near(isolated.tftFlexGoldEstimate(shop, one(cost - 1), cost).totalGold,
    estimate(shop, one(cost - 1), cost).totalGold, 'Bundled Worker estimate for core cost ' + cost);
}

checkGoldAndInputs();
checkIndependentSingleChampionValues();
checkExhaustiveTinyPools();
checkJointCollectionAndBoundedStates();
checkRerollProfilesAndWorkerBundle();
console.log('Trait flex gold checks passed (' + (performance.now() - started).toFixed(0) + ' ms).');
