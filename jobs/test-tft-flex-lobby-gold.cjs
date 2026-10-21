// Run: node jobs/test-tft-flex-lobby-gold.cjs [--inline]
// Checks static contested pools, exact grouping and the complete-roll MC sampler.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { performance } = require('node:perf_hooks');
const args = process.argv.slice(2);
assert(args.length === 0 || (args.length === 1 && args[0] === '--inline'), 'Expected optional --inline');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const extract = (source, name) => {
  const start = source.indexOf('// ---------------- trait flex: ' + name + ' ----------------');
  const end = source.indexOf('// ---------------- trait flex: ' + name + ' end ----------------');
  assert(start >= 0 && end > start, 'Missing ' + name + ' extraction markers');
  return source.slice(start, end);
};
const context = vm.createContext({});
vm.runInContext(extract(html, 'gold estimates'), context);
const baseline = context.tftFlexGoldEstimate;
const canonical = fs.readFileSync(path.join(__dirname, 'tft-flex-lobby-engine.js'), 'utf8');
assert.equal(extract(html, 'lobby gold estimates').trim(), extract(canonical, 'lobby gold estimates').trim(),
  'The browser inline solver and canonical Worker solver remain identical');
const source = args.length ? html : canonical;
vm.runInContext(extract(source, 'lobby gold estimates'), context);
const estimate = context.tftFlexLobbyEstimate;
const plain = value => JSON.parse(JSON.stringify(value));
const shop = { costOdds: [.15, .20, .32, .30, .03], championCounts: [14, 13, 14, 14, 10],
  bagCopies: [30, 25, 18, 10, 9], refreshCost: 2, visibleSlots: [4, 5] };
const one = cost => Array.from({ length: 5 }, (_, index) => index === cost ? 1 : 0);
const poolFor = model => model.championCounts.flatMap((count, cost) => Array.from({ length: count }, (_, index) =>
  ({ api: 'unit-' + (cost + 1) + '-' + index, cost: cost + 1, heldCopies: 0 })));
const targetsFor = (pool, counts) => counts.flatMap((count, cost) => pool.filter(unit => unit.cost === cost + 1).slice(0, count)
  .map(unit => ({ api: unit.api, cost: unit.cost, copies: unit.cost === 4 ? 3 : 1 })));
const held = (pool, api, copies) => pool.map(unit => ({ ...unit, heldCopies: unit.api === api ? copies : unit.heldCopies }));
function near(actual, expected, label, relative = 2e-11) {
  assert(Number.isFinite(actual), label + ': expected a finite result');
  assert(Math.abs(actual - expected) <= relative * Math.max(1, Math.abs(expected)), label + ': expected ' + expected + ', got ' + actual);
}
function sampledNear(result, exact, label) {
  assert.equal(result.method, 'monte-carlo', label + ': MC was exercised');
  assert(result.standardErrorGold > 0 && Number.isFinite(result.standardErrorGold), label + ': sampling error is reported');
  assert(Math.abs(result.totalGold - exact.totalGold) < 6 * result.standardErrorGold + 1e-9,
    label + ': expected ' + exact.totalGold + ', got ' + result.totalGold + ' ± ' + result.standardErrorGold + ' SE');
  near(result.confidence95[1] - result.totalGold, 1.96 * result.standardErrorGold, label + ': 95% interval');
}
const started = performance.now();

function checkZeroContestAndIdentity() {
  const pool = poolFor(shop);
  for (const counts of [...Array.from({ length: 5 }, (_, cost) => one(cost)), [2, 1, 2, 3, 0], [1, 1, 1, 4, 1]]) {
    const targets = targetsFor(pool, counts), result = estimate(shop, pool, targets), reference = baseline(shop, counts);
    assert.equal(result.method, 'exact', 'Zero contest preserves exact baseline math');
    near(result.expectedShops, reference.expectedShops, 'Uncontested shops');
    near(result.totalGold, reference.totalGold, 'Uncontested total gold');
    assert.equal(result.purchaseGold, reference.purchaseGold);
    assert.deepEqual(plain(result.copiesByCost), plain(reference.copiesByCost));
    assert.equal(result.standardErrorGold, 0); assert.equal(result.samples, 0);
  }
  const targets = targetsFor(pool, [2, 1, 2, 3, 0]), snapshot = JSON.stringify([shop, pool, targets]);
  const first = estimate(shop, pool, targets), reversed = estimate(shop, pool.slice().reverse(), targets.slice().reverse());
  assert.equal(JSON.stringify([shop, pool, targets]), snapshot, 'Inputs are unchanged');
  assert.deepEqual(plain(first), plain(reversed), 'Pool and roster order are immaterial');
  const renamedPool = pool.map(unit => ({ ...unit, api: 'renamed-' + unit.api }));
  const renamedTargets = targets.map(target => ({ ...target, api: 'renamed-' + target.api }));
  assert.equal(estimate(shop, renamedPool, renamedTargets, { signatureOnly: true }).signature, first.signature,
    'Numerical cache signatures omit champion names');
  const frozen = value => Object.freeze(value.map(item => Object.freeze({ ...item })));
  near(estimate(shop, frozen(pool), frozen(targets)).totalGold, first.totalGold, 'Read-only pools and targets are supported');
  const summary = estimate(shop, pool, targets, { signatureOnly: true });
  assert(!('expectedShops' in summary) && !('totalGold' in summary) && !('refreshGold' in summary),
    'Signature-only validation never returns a fake estimate');
  assert.equal(summary.signature, first.signature); assert.equal(summary.purchaseGold, first.purchaseGold);
  const isolated = vm.createContext({}); vm.runInContext(estimate.toString(), isolated);
  near(isolated.tftFlexLobbyEstimate(shop, pool, targets).totalGold, first.totalGold, 'The engine works without page globals');
  const empty = estimate(shop, pool, []);
  assert.equal(empty.totalGold, 0); assert.equal(empty.expectedShops, 0); assert.equal(empty.method, 'exact');
}

function checkContestAndUnavailable() {
  const pool = poolFor(shop), target = targetsFor(pool, one(3)), api = target[0].api;
  const clear = estimate(shop, pool, target), contested = estimate(shop, held(pool, api, 5), target);
  assert(contested.totalGold > clear.totalGold * 1.3, 'Holding the target reduces its hit chance');
  const other = pool.find(unit => unit.cost === 4 && unit.api !== api).api;
  const depletedOthers = estimate(shop, held(pool, other, 5), target);
  assert(depletedOthers.totalGold < clear.totalGold, 'Non-target depletion in the same tier can improve target odds');
  near(contested.remainingByCost[3], depletedOthers.remainingByCost[3], 'Both scenarios deplete the same total tier copies');
  assert.notEqual(contested.signature, depletedOthers.signature, 'Target availability distinguishes identical tier totals');
  const unrelated = pool.map(unit => ({ ...unit, heldCopies: unit.cost === 1 ? 10 : 0 }));
  near(estimate(shop, unrelated, target).totalGold, clear.totalGold, 'Another nonempty tier cannot change the fixed 4-cost odds');
  const irrelevantSwap = held(held(pool, 'unit-4-1', 2), 'unit-4-2', 3);
  const irrelevantSwap2 = held(held(pool, 'unit-4-1', 3), 'unit-4-2', 2);
  assert.equal(estimate(shop, irrelevantSwap, target, { signatureOnly: true }).signature,
    estimate(shop, irrelevantSwap2, target, { signatureOnly: true }).signature,
    'Non-target identities affect the tier total only');
  const insufficient = estimate(shop, held(pool, api, 8), target);
  assert.equal(insufficient.available, false); assert.equal(insufficient.totalGold, Infinity);
  assert.equal(insufficient.method, 'unavailable'); assert.equal(insufficient.confidence95, null);
  assert.deepEqual(plain(insufficient.unavailableTargets), [{ api, cost: 4, available: 2, required: 3, reason: 'insufficient-copies' }]);
  const exhausted = pool.map(unit => ({ ...unit, heldCopies: unit.cost === 4 ? 10 : 0 }));
  const unavailable = estimate(shop, exhausted, target, { signatureOnly: true });
  assert.equal(unavailable.available, false); assert.equal(unavailable.unavailableTargets[0].reason, 'empty-tier');
  assert(unavailable.emptyTiers.includes(4)); assert(!('totalGold' in unavailable));
  const impossibleOdds = estimate({ ...shop, costOdds: [.15, .20, .62, 0, .03] }, pool, target);
  assert.equal(impossibleOdds.unavailableTargets[0].reason, 'zero-odds');
  const simple = { costOdds: [.5, 0, 0, .5, 0], championCounts: [1, 0, 0, 1, 0],
    bagCopies: [1, 1, 1, 3, 1], visibleSlots: [1], refreshCost: 2 };
  const simplePool = poolFor(simple).map(unit => ({ ...unit, heldCopies: unit.cost === 1 ? 1 : 0 }));
  const certain = estimate(simple, simplePool, targetsFor(simplePool, one(3)));
  assert.equal(certain.expectedShops, 3, 'An empty tier is redrawn among nonempty tiers');
  assert.equal(certain.purchaseGold, 12); assert.equal(certain.totalGold, 18, 'Every paid shop and target copy is charged');
}

function checkValidation() {
  const pool = poolFor(shop), targets = targetsFor(pool, one(3));
  assert.throws(() => estimate(shop, held(pool, pool[0].api, .5), targets), /whole copies/);
  assert.throws(() => estimate(shop, held(pool, pool[0].api, -1), targets), /whole copies/);
  assert.throws(() => estimate(shop, held(pool, pool[0].api, 31), targets), /whole copies/);
  assert.throws(() => estimate(shop, pool.slice(1), targets), /every shop champion/);
  assert.throws(() => estimate(shop, [...pool, pool[0]], targets), /duplicate lobby pool/);
  assert.throws(() => estimate(shop, pool, [...targets, targets[0]]), /duplicate lobby target/);
  assert.throws(() => estimate(shop, pool, [{ ...targets[0], cost: 3 }]), /lobby target/);
  for (const copies of [0, 2, 4, 8, 10, 1.5, NaN, '9', true, null])
    assert.throws(() => estimate(shop, pool, [{ ...targets[0], copies }]), /one, three or nine/);
  assert.throws(() => estimate({ ...shop, costOdds: new Array(5) }, pool, targets), /costOdds/);
  assert.throws(() => estimate(shop, pool, targets, { samples: 0 }), /sample count/);
  assert.throws(() => estimate(shop, pool, targets, { exactStateLimit: -1 }), /state limit/);
}

// Independent reference tracks every champion identity and enumerates the full
// ordered shop before solving a Markov linear system with no grouped recurrence.
function exhaustiveReference(model, pool, targets) {
  const required = targets.map(target => target.copies), strides = [1];
  for (let index = 1; index < targets.length; ++index) strides.push(strides[index - 1] * (required[index - 1] + 1));
  const total = required.reduce((product, copies) => product * (copies + 1), 1), terminal = total - 1;
  const indexes = new Map(targets.map((target, index) => [target.api, index]));
  const codeFor = owned => owned.reduce((code, copies, index) => code + copies * strides[index], 0);
  const ownedFor = code => required.map((copies, index) => Math.floor(code / strides[index]) % (copies + 1));
  function transition(code, slots) {
    const owned = ownedFor(code), offers = pool.map(unit => model.bagCopies[unit.cost - 1] - unit.heldCopies -
      (indexes.has(unit.api) ? owned[indexes.get(unit.api)] : 0)), probabilities = new Map();
    function draw(remaining, chance) {
      const after = codeFor(owned);
      if (!remaining || after === terminal) { probabilities.set(after, (probabilities.get(after) || 0) + chance); return; }
      const totals = [0, 0, 0, 0, 0]; pool.forEach((unit, index) => totals[unit.cost - 1] += offers[index]);
      const availableOdds = model.costOdds.reduce((sum, probability, cost) => sum + (totals[cost] ? probability : 0), 0);
      for (let index = 0; index < pool.length; ++index) {
        const unit = pool[index], cost = unit.cost - 1;
        if (!offers[index] || !model.costOdds[cost]) continue;
        const probability = model.costOdds[cost] / availableOdds * offers[index] / totals[cost];
        const target = indexes.get(unit.api), buy = target !== undefined && owned[target] < required[target];
        --offers[index]; if (buy) ++owned[target];
        draw(remaining - 1, chance * probability);
        ++offers[index]; if (buy) --owned[target];
      }
    }
    draw(slots, 1);
    near([...probabilities.values()].reduce((sum, chance) => sum + chance, 0), 1, 'Exhaustive probabilities conserve mass');
    return probabilities;
  }
  const phases = model.visibleSlots.length, rows = terminal * phases;
  const matrix = Array.from({ length: rows }, () => new Float64Array(rows + 1));
  for (let code = 0; code < terminal; ++code) for (let phase = 0; phase < phases; ++phase) {
    const row = code * phases + phase; matrix[row][row] = 1; matrix[row][rows] = 1;
    for (const [next, chance] of transition(code, model.visibleSlots[phase])) {
      if (next !== terminal) matrix[row][next * phases + (phase + 1) % phases] -= chance;
    }
  }
  for (let column = 0; column < rows; ++column) {
    let pivot = column;
    for (let row = column + 1; row < rows; ++row) if (Math.abs(matrix[row][column]) > Math.abs(matrix[pivot][column])) pivot = row;
    assert(Math.abs(matrix[pivot][column]) > 1e-14);
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

function checkTinyExactPoolsAndMonteCarlo() {
  const models = [
    { model: { costOdds: [1, 0, 0, 0, 0], championCounts: [3, 0, 0, 0, 0], bagCopies: [2, 1, 1, 1, 1], visibleSlots: [2, 3], refreshCost: 2 },
      holdings: [1, 0, 1], counts: [2, 0, 0, 0, 0] },
    { model: { costOdds: [0, 0, 0, 1, 0], championCounts: [0, 0, 0, 3, 0], bagCopies: [1, 1, 1, 4, 1], visibleSlots: [2, 3], refreshCost: 2 },
      holdings: [1, 1, 0], counts: [0, 0, 0, 2, 0] },
    { model: { costOdds: [.35, 0, 0, .65, 0], championCounts: [2, 0, 0, 3, 0], bagCopies: [3, 1, 1, 4, 1], visibleSlots: [2, 3], refreshCost: 2 },
      holdings: [1, 2, 1, 0, 2], counts: [1, 0, 0, 2, 0] },
  ];
  for (const { model, holdings, counts } of models) {
    const pool = poolFor(model).map((unit, index) => ({ ...unit, heldCopies: holdings[index] })), targets = targetsFor(pool, counts);
    const phases = exhaustiveReference(model, pool, targets), result = estimate(model, pool, targets);
    near(result.expectedShops, phases.reduce((sum, value) => sum + value / phases.length, 0), 'Contested grouped DP agrees with independent identity Markov chains');
    phases.forEach((value, phase) => near(result.diagnostics.shopsByInitialPhase[phase], value, 'Independent initial phase ' + phase));
    const sampled = estimate(model, pool, targets, { exactStateLimit: 0, samples: 16384 });
    sampledNear(sampled, result, 'Full shop simulation agrees with the exact tiny pool');
  }
  const oneSlot = { costOdds: [0, 0, 0, 1, 0], championCounts: [0, 0, 0, 3, 0], bagCopies: [1, 1, 1, 4, 1], visibleSlots: [1], refreshCost: 2 };
  const pool = poolFor(oneSlot).map((unit, index) => ({ ...unit, heldCopies: index < 2 ? 1 : 0 }));
  const target = targetsFor(pool, one(3)), result = estimate(oneSlot, pool, target);
  near(result.expectedShops, 10 / 3 + 9 / 2 + 8, 'Each bought copy depletes both the target and its entire tier');
}

function checkLargeAndRareMonteCarlo() {
  let pool = poolFor(shop), targets = targetsFor(pool, [0, 0, 5, 3, 0]);
  for (let index = 0; index < targets.length; ++index) pool = held(pool, targets[index].api, index % 5);
  const exact = estimate(shop, pool, targets, { exactStateLimit: 4096 });
  const sampled = estimate(shop, pool, targets);
  assert(sampled.diagnostics.exactStates > 560, 'A genuinely large availability signature selects MC');
  sampledNear(sampled, exact, 'Large contested roster MC agrees with exact DP');
  assert.deepEqual(plain(sampled), plain(estimate(shop, pool.slice().reverse(), targets.slice().reverse())),
    'MC seed, ordering and output depend on the availability signature');
  assert.equal(sampled.samples, 2048); assert(sampled.diagnostics.samplingIntervalOnly);
  assert.equal(sampled.purchaseGold, targets.reduce((sum, target) => sum + target.cost * target.copies, 0));

  const rareShop = { ...shop, costOdds: [1e-8, 1 - 1e-8, 0, 0, 0] };
  const rarePool = held(poolFor(rareShop), 'unit-1-0', 29), rareTargets = targetsFor(rarePool, one(0));
  const rareExact = estimate(rareShop, rarePool, rareTargets);
  const rareMC = estimate(rareShop, rarePool, rareTargets, { exactStateLimit: 0, samples: 8192 });
  sampledNear(rareMC, rareExact, 'Exact empty-shop skipping preserves arbitrarily long rolldowns');
  assert(rareMC.expectedShops > 1e9 && rareMC.diagnostics.skippedEmptyShops > 1e12,
    'Rare trials retain the long tail instead of applying a maximum-shop cutoff');
  assert(rareMC.diagnostics.rareStates > 0);
  const realisticPool = poolFor(shop), four = targetsFor(realisticPool, one(3));
  const forced = estimate(shop, realisticPool, four, { exactStateLimit: 0, samples: 8192 });
  sampledNear(forced, estimate(shop, realisticPool, four), 'Conditioned finite-pool shops handle multiple copies');
  assert(forced.diagnostics.rareStates > 0 && forced.diagnostics.directShops > 0,
    'Both ordinary shops and exact rare-state skipping are exercised');
}

// One visible slot gives an independent identity-based recurrence. Completed
// targets stop being bought; their unbought bag copies remain denominator noise.
function oneSlotReference(totalCopies, capacities, quotas, removeCompleted = false) {
  const memo = new Map();
  function expected(owned) {
    if (owned.every((copies, index) => copies === quotas[index])) return 0;
    const key = owned.join(',');
    if (memo.has(key)) return memo.get(key);
    const removed = owned.reduce((sum, copies) => sum + copies, 0) +
      (removeCompleted ? owned.reduce((sum, copies, index) =>
        sum + (copies === quotas[index] ? capacities[index] - quotas[index] : 0), 0) : 0);
    const denominator = totalCopies - removed;
    let progress = 0, numerator = 1;
    for (let index = 0; index < owned.length; ++index) if (owned[index] < quotas[index]) {
      const probability = (capacities[index] - owned[index]) / denominator;
      const next = owned.slice(); ++next[index];
      progress += probability; numerator += probability * expected(next);
    }
    const result = numerator / progress; memo.set(key, result); return result;
  }
  return expected(quotas.map(() => 0));
}

function checkNineCopyExactArithmeticAndSignatures() {
  for (const cost of [2, 3]) {
    const model = { ...shop, costOdds: one(cost - 1), visibleSlots: [1] };
    const pool = poolFor(model), unit = pool.find(unit => unit.cost === cost);
    const target = [{ api: unit.api, cost, copies: 9 }];
    const bag = model.bagCopies[cost - 1], total = model.championCounts[cost - 1] * bag;
    const hand = Array.from({ length: 9 }, (_, owned) => (total - owned) / (bag - owned))
      .reduce((sum, waiting) => sum + waiting, 0);
    const result = estimate(model, pool, target);
    assert.equal(result.method, 'exact');
    near(result.expectedShops, hand, 'Nine separately depleted single-target waiting times at cost ' + cost);
    assert.equal(result.purchaseGold, 9 * cost);
    assert.equal(result.diagnostics.exactStates, 10);
    const contested = estimate(model, held(pool, unit.api, 2), target);
    near(contested.expectedShops, Array.from({ length: 9 }, (_, owned) =>
      (total - 2 - owned) / (bag - 2 - owned)).reduce((sum, waiting) => sum + waiting, 0),
    'Opponent holdings and bought nine-copy targets deplete the same tier');
    const two = pool.filter(unit => unit.cost === cost).slice(0, 2).map(unit => ({ api: unit.api, cost, copies: 9 }));
    const together = estimate(model, pool, two);
    near(together.expectedShops, oneSlotReference(total, [bag, bag], [9, 9]),
      'Two nine-copy targets agree with the independent identity recurrence');
    assert.equal(together.diagnostics.exactStates, 55, 'Two nine-copy targets use choose(11,9), not the three-copy formula');
    assert.equal(together.purchaseGold, 18 * cost);
    const signatures = [1, 3, 9].map(copies => estimate(model, pool, [{ ...target[0], copies }], { signatureOnly: true }).signature);
    assert.equal(new Set(signatures).size, 3, 'Copy quota is part of the cache signature');
    const shortage = estimate(model, held(pool, unit.api, bag - 8), target);
    assert.equal(shortage.available, false); assert.equal(shortage.totalGold, Infinity);
    assert.equal(shortage.method, 'unavailable');
    assert.deepEqual(plain(shortage.unavailableTargets), [{ api: unit.api, cost, available: 8, required: 9, reason: 'insufficient-copies' }]);
  }
  const model = { costOdds: [0, 1, 0, 0, 0], championCounts: [0, 3, 0, 0, 0],
    bagCopies: [1, 12, 1, 1, 1], visibleSlots: [1], refreshCost: 2 };
  const pool = poolFor(model), targets = pool.slice(0, 2).map((unit, index) =>
    ({ api: unit.api, cost: 2, copies: index ? 9 : 1 }));
  const result = estimate(model, pool, targets);
  near(result.expectedShops, oneSlotReference(36, [12, 12], [1, 9]),
    'Completed-target leftover copies remain in the pool during another three-star collection');
  assert(result.expectedShops > oneSlotReference(36, [12, 12], [1, 9], true),
    'Finishing a target must not remove its remaining unbought bag copies');
  const normalPool = poolFor(shop), four = normalPool.find(unit => unit.cost === 4);
  const singleFour = estimate(shop, normalPool, [{ api: four.api, cost: 4, copies: 1 }]);
  assert.equal(singleFour.purchaseGold, 4, 'A lower-tier profile can request one copy of its four-cost support');
  const legacy = estimate(shop, normalPool, [{ api: four.api, cost: 4 }]);
  assert.equal(legacy.purchaseGold, 12, 'Omitted quota preserves the old four-cost default');
}

function checkNineCopyMonteCarloAndLargeCollections() {
  const pool = poolFor(shop);
  for (const cost of [2, 3]) {
    const targets = pool.filter(unit => unit.cost === cost).slice(0, 2)
      .map(unit => ({ api: unit.api, cost, copies: 9 }));
    const exact = estimate(shop, pool, targets);
    const sampled = estimate(shop, pool, targets, { exactStateLimit: 0, samples: 8192 });
    sampledNear(sampled, exact, 'Nine-copy full-shop sampler agrees with exact two-target DP at cost ' + cost);
    for (const count of [4, 8]) {
      const many = pool.filter(unit => unit.cost === cost).slice(0, count)
        .map(unit => ({ api: unit.api, cost, copies: 9 }));
      const start = performance.now(), result = estimate(shop, pool, many);
      assert.equal(result.method, 'monte-carlo');
      assert.equal(result.samples, 2048);
      assert.equal(result.purchaseGold, count * 9 * cost);
      assert.equal(result.diagnostics.exactStates, count === 4 ? 715 : 24310);
      assert(Number.isFinite(result.totalGold) && result.totalGold > result.purchaseGold);
      assert(result.standardErrorGold > 0 && Number.isFinite(result.standardErrorGold));
      near(result.confidence95[1] - result.totalGold, 1.96 * result.standardErrorGold,
        'Large nine-copy collections report an interval for the sampled mean');
      console.log('Nine-copy collection ' + cost + ' cost x ' + count + ': ' +
        result.totalGold.toFixed(2) + 'g ± ' + result.standardErrorGold.toFixed(2) +
        'g SE (' + (performance.now() - start).toFixed(0) + ' ms, ' + result.samples + ' trials).');
    }
  }
}

checkZeroContestAndIdentity();
checkContestAndUnavailable();
checkValidation();
checkTinyExactPoolsAndMonteCarlo();
checkLargeAndRareMonteCarlo();
checkNineCopyExactArithmeticAndSignatures();
checkNineCopyMonteCarloAndLargeCollections();
console.log('Trait flex lobby gold checks passed (' + (performance.now() - started).toFixed(0) + ' ms).');
