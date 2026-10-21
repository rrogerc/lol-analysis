// ---------------- trait flex: lobby gold estimates ----------------
// Static, integer opponent holdings. Keep the function self-contained for Workers.
function tftFlexLobbyEstimate(shop, poolUnits, targets, options = {}) {
  if (!shop || typeof shop !== 'object' || !options || typeof options !== 'object') throw new Error('Invalid lobby shop settings.');
  const five = (name, check) => {
    const values = shop[name];
    if (!Array.isArray(values) || values.length !== 5 || !Array.from(values).every(check)) throw new Error('Invalid shop ' + name + '.');
    return values.slice();
  };
  const odds = five('costOdds', value => Number.isFinite(value) && value >= 0 && value <= 1);
  const bags = five('bagCopies', value => Number.isSafeInteger(value) && value > 0);
  const championCounts = five('championCounts', value => Number.isSafeInteger(value) && value >= 0);
  const oddsSum = odds.reduce((sum, value) => sum + value, 0);
  if (Math.abs(oddsSum - 1) > 1e-9) throw new Error('Shop cost probabilities must sum to one.');
  odds.forEach((value, cost) => odds[cost] = value / oddsSum);
  const phases = shop.visibleSlots === undefined ? [4, 5] : shop.visibleSlots.slice?.();
  if (!Array.isArray(phases) || !phases.length || !Array.from(phases).every(value => Number.isSafeInteger(value) && value >= 1 && value <= 5)) {
    throw new Error('Invalid visible shop slots.');
  }
  const refreshCost = shop.refreshCost === undefined ? 2 : shop.refreshCost;
  if (!Number.isFinite(refreshCost) || refreshCost < 0) throw new Error('Invalid shop refresh cost.');
  if (!Array.isArray(poolUnits) || !Array.isArray(targets) || targets.length > 8) throw new Error('Invalid level 8 lobby roster.');
  const poolByApi = new Map(), counted = [0, 0, 0, 0, 0], remainingByCost = [0, 0, 0, 0, 0];
  for (const unit of poolUnits) {
    if (!unit || typeof unit.api !== 'string' || !unit.api || poolByApi.has(unit.api) ||
        !Number.isInteger(unit.cost) || unit.cost < 1 || unit.cost > 5) throw new Error('Invalid or duplicate lobby pool unit.');
    const cost = unit.cost - 1;
    if (!Number.isSafeInteger(unit.heldCopies) || unit.heldCopies < 0 || unit.heldCopies > bags[cost]) {
      throw new Error('Opponent holdings must be whole copies between zero and the champion bag size.');
    }
    const available = bags[cost] - unit.heldCopies;
    poolByApi.set(unit.api, { cost, available });
    ++counted[cost]; remainingByCost[cost] += available;
  }
  if (!counted.every((count, cost) => count === championCounts[cost]) || !remainingByCost.every(Number.isSafeInteger)) {
    throw new Error('The lobby pool must include every shop champion in each cost tier.');
  }
  const seen = new Set(), groupMap = new Map(), unavailableTargets = [], copiesByCost = [0, 0, 0, 0, 0];
  for (const target of targets) {
    const unit = target && poolByApi.get(target.api);
    if (!unit || seen.has(target.api) || target.cost !== unit.cost + 1) throw new Error('Invalid or duplicate lobby target.');
    seen.add(target.api);
    const required = target.copies === undefined ? (target.cost === 4 ? 3 : 1) : target.copies;
    if (![1, 3, 9].includes(required)) throw new Error('Lobby targets require one, three or nine copies per champion.');
    copiesByCost[unit.cost] += required;
    const reason = !remainingByCost[unit.cost] ? 'empty-tier' : unit.available < required ? 'insufficient-copies' : !odds[unit.cost] ? 'zero-odds' : null;
    if (reason) unavailableTargets.push({ api: target.api, cost: target.cost, available: unit.available, required, reason });
    const key = [unit.cost, unit.available, required].join(',');
    if (groupMap.has(key)) ++groupMap.get(key).count;
    else groupMap.set(key, { cost: unit.cost, available: unit.available, required, count: 1 });
  }
  const groups = [...groupMap.values()].sort((a, b) => a.cost - b.cost || a.available - b.available || a.required - b.required);
  const targetGroups = groups.map(group => ({ cost: group.cost + 1, available: group.available, required: group.required, count: group.count }));
  const purchaseGold = copiesByCost.reduce((sum, copies, cost) => sum + copies * (cost + 1), 0);
  const emptyTiers = remainingByCost.flatMap((copies, cost) => copies ? [] : [cost + 1]);
  const signature = JSON.stringify([odds, remainingByCost, phases, refreshCost,
    groups.map(group => [group.cost + 1, group.available, group.required, group.count])]);
  // A group of m exchangeable targets needing r copies has r + 1
  // ownership categories, including completed targets: choose(m + r, r).
  const exactStates = groups.reduce((product, group) => {
    let states = 1;
    for (let count = 1; count <= group.count; ++count) states = states * (group.required + count) / count;
    return product * Math.round(states);
  }, 1);
  const summary = { signature, available: !unavailableTargets.length, unavailableTargets, remainingByCost,
    emptyTiers, targetGroups, copiesByCost, purchaseGold };
  if (options.signatureOnly === true) return summary;
  const finish = (expectedShops, method, samples, standardErrorShops, diagnostics) => {
    const refreshGold = refreshCost === 0 ? 0 : expectedShops * refreshCost;
    const totalGold = refreshGold + purchaseGold;
    const standardErrorGold = standardErrorShops === null ? null : standardErrorShops * refreshCost;
    return { ...summary, expectedShops, refreshGold, totalGold, method, samples, standardErrorGold,
      confidence95: standardErrorGold === null ? null : [Math.max(purchaseGold, totalGold - 1.96 * standardErrorGold), totalGold + 1.96 * standardErrorGold],
      diagnostics: { exactStates, ...diagnostics } };
  };
  if (!summary.available) return finish(Infinity, 'unavailable', 0, null, {});
  if (!targets.length) return finish(0, 'exact', 0, 0, { shopsByInitialPhase: phases.map(() => 0) });
  const exactStateLimit = options.exactStateLimit === undefined ? 560 : options.exactStateLimit;
  const requestedSamples = options.samples === undefined ? 2048 : options.samples;
  if (!Number.isSafeInteger(exactStateLimit) || exactStateLimit < 0 || exactStateLimit > 4096) throw new Error('Invalid exact lobby state limit.');
  if (!Number.isSafeInteger(requestedSamples) || requestedSamples < 16 || requestedSamples > 65536) throw new Error('Invalid lobby sample count.');
  if (options.seed !== undefined && (!Number.isSafeInteger(options.seed) || options.seed < 0 || options.seed > 4294967295)) throw new Error('Invalid lobby sampling seed.');
  const phaseCount = phases.length, maxSlots = Math.max(...phases), base = maxSlots + 1;
  let forms, lookup;
  function prepareForms() {
    if (forms) return;
    const strides = [1];
    for (let cost = 1; cost < 5; ++cost) strides.push(strides[cost - 1] * base);
    const codes = strides[4] * base;
    lookup = new Int32Array(codes * base); lookup.fill(-1); forms = [];
    for (let code = 0; code < codes; ++code) {
      const nuisance = strides.map(stride => Math.floor(code / stride) % base), shown = nuisance.reduce((sum, n) => sum + n, 0);
      if (shown > maxSlots) continue;
      for (let remaining = 0; remaining <= maxSlots - shown; ++remaining) {
        lookup[code * base + remaining] = forms.length;
        forms.push({ code, nuisance, remaining, hit: -1, noise: null });
      }
    }
    for (const form of forms) if (form.remaining) {
      form.hit = lookup[form.code * base + form.remaining - 1];
      form.noise = strides.map(stride => lookup[(form.code + stride) * base + form.remaining - 1]);
    }
  }

  function exact() {
    prepareForms();
    const radices = [], strides = [];
    let initialCode = 0, radixProduct = 1;
    const dimensions = groups.map(group => {
      const axes = [];
      for (let category = 0; category < group.required; ++category) {
        axes.push(radices.length); radices.push(group.count + 1); strides.push(radixProduct);
        if (!category) initialCode += group.count * radixProduct;
        radixProduct *= group.count + 1;
      }
      return axes;
    });
    const states = new Map([[0, { code: 0, expected: new Float64Array(phaseCount) }]]);
    let withinShopStates = 0;
    function stateFor(code) {
      if (states.has(code)) return states.get(code);
      const owned = new Float64Array(5), active = new Float64Array(5), transitions = Array.from({ length: 5 }, () => []);
      groups.forEach((group, index) => {
        let missing = 0;
        dimensions[index].forEach((axis, category) => {
          const count = Math.floor(code / strides[axis]) % radices[axis];
          missing += count * (group.required - category);
          const copies = count * (group.available - category);
          active[group.cost] += copies;
          if (copies) transitions[group.cost].push({ copies, code: code - strides[axis] +
            (category + 1 < group.required ? strides[dimensions[index][category + 1]] : 0) });
        });
        owned[group.cost] += group.count * group.required - missing;
      });
      const state = { code, owned, active, transitions, expected: null, progress: new Float64Array(forms.length),
        constant: new Float64Array(forms.length * phaseCount) };
      state.progress.fill(NaN); state.constant.fill(NaN); states.set(code, state); return state;
    }
    function affine(state, formIndex, futurePhase) {
      const memoIndex = formIndex * phaseCount + futurePhase;
      if (!Number.isNaN(state.constant[memoIndex])) return memoIndex;
      ++withinShopStates;
      const form = forms[formIndex];
      if (!form.remaining) { state.progress[formIndex] = 0; state.constant[memoIndex] = 0; return memoIndex; }
      const calculateProgress = Number.isNaN(state.progress[formIndex]);
      let progress = 0, constant = 0, availableOdds = 0;
      for (let cost = 0; cost < 5; ++cost) if (remainingByCost[cost] - state.owned[cost] - form.nuisance[cost] > 0) availableOdds += odds[cost];
      for (let cost = 0; cost < 5; ++cost) {
        const denominator = remainingByCost[cost] - state.owned[cost] - form.nuisance[cost];
        if (denominator <= 0 || !odds[cost]) continue;
        const costProbability = odds[cost] / availableOdds;
        for (const transition of state.transitions[cost]) {
          const probability = costProbability * transition.copies / denominator, next = solve(transition.code);
          if (next.code) {
            const nextIndex = affine(next, form.hit, futurePhase), failure = 1 - next.progress[form.hit];
            constant += probability * (next.constant[nextIndex] + (failure ? failure * next.expected[futurePhase] : 0));
          }
          if (calculateProgress) progress += probability;
        }
        const noiseProbability = costProbability * (denominator - state.active[cost]) / denominator;
        if (noiseProbability > 0) {
          const nextIndex = affine(state, form.noise[cost], futurePhase);
          constant += noiseProbability * state.constant[nextIndex];
          if (calculateProgress) progress += noiseProbability * state.progress[form.noise[cost]];
        }
      }
      if (calculateProgress) state.progress[formIndex] = Math.min(1, progress);
      state.constant[memoIndex] = constant; return memoIndex;
    }
    function solve(code) {
      const state = stateFor(code);
      if (state.expected) return state;
      const progress = new Float64Array(phaseCount), constants = new Float64Array(phaseCount);
      for (let phase = 0; phase < phaseCount; ++phase) {
        const formIndex = lookup[phases[phase]], memoIndex = affine(state, formIndex, (phase + 1) % phaseCount);
        progress[phase] = state.progress[formIndex]; constants[phase] = 1 + state.constant[memoIndex];
      }
      let denominator = 0, failure = 1;
      for (let phase = 0; phase < phaseCount; ++phase) { denominator += failure * progress[phase]; failure *= 1 - progress[phase]; }
      const expected = new Float64Array(phaseCount);
      for (let start = 0; start < phaseCount; ++start) {
        let numerator = 0, priorFailure = 1;
        for (let offset = 0; offset < phaseCount; ++offset) {
          const phase = (start + offset) % phaseCount;
          numerator += priorFailure * constants[phase]; priorFailure *= 1 - progress[phase];
        }
        expected[start] = denominator ? numerator / denominator : Infinity;
      }
      state.expected = expected; return state;
    }
    const solved = solve(initialCode), expectedShops = solved.expected.reduce((sum, value) => sum + value / phaseCount, 0);
    return finish(expectedShops, 'exact', 0, 0, { states: states.size, withinShopStates, shopsByInitialPhase: Array.from(solved.expected) });
  }
  if (exactStates <= exactStateLimit) return exact();

  // Simulate complete rolldowns. Unbought offers leave the pool only until the
  // current shop ends. Completed target purchases remain out of every later
  // shop, just like opponent holdings. There is no maximum shop count.
  const costs = [], capacities = [], quotas = [], targetStrides = [], byCost = Array.from({ length: 5 }, () => []);
  let codeProduct = 1;
  for (const group of groups) for (let copy = 0; copy < group.count; ++copy) {
    const index = costs.length; costs.push(group.cost); capacities.push(group.available); quotas.push(group.required);
    targetStrides.push(codeProduct); codeProduct *= group.required + 1; byCost[group.cost].push(index);
  }
  const owned = new Uint8Array(costs.length), ownedByCost = new Float64Array(5), active = new Float64Array(5), nuisance = new Uint8Array(5);
  const initialActive = [0, 0, 0, 0, 0]; capacities.forEach((copies, index) => initialActive[costs[index]] += copies);
  const totalRequired = copiesByCost.reduce((sum, value) => sum + value, 0);
  const mayExhaust = remainingByCost.some((copies, cost) => odds[cost] && copies <= copiesByCost[cost] + maxSlots);
  const cumulativeOdds = []; odds.reduce((sum, value) => { cumulativeOdds.push(sum + value); return sum + value; }, 0);
  let seed = 2166136261;
  for (let index = 0; index < signature.length; ++index) seed = Math.imul(seed ^ signature.charCodeAt(index), 16777619) >>> 0;
  seed = (seed ^ (options.seed === undefined ? 0 : options.seed)) >>> 0;
  const initialSeed = seed;
  const nextUint = () => {
    seed = (seed + 0x6d2b79f5) >>> 0;
    let value = Math.imul(seed ^ seed >>> 15, 1 | seed);
    value ^= value + Math.imul(value ^ value >>> 7, 61 | value);
    return (value ^ value >>> 14) >>> 0;
  };
  const random = () => (nextUint() + .5) / 4294967296;
  const randomTail = () => Math.min(1 - Number.EPSILON, (nextUint() * 2097152 + (nextUint() >>> 11) + .5) / 9007199254740992);
  const hazardCache = new Map();
  let stateCode = 0, missing = 0, skippedEmptyShops = 0, directShops = 0;
  function buy(index) {
    const cost = costs[index]; ++owned[index]; ++ownedByCost[cost]; stateCode += targetStrides[index]; --missing;
    --active[cost]; if (owned[index] === quotas[index]) active[cost] -= capacities[index] - quotas[index];
  }
  function chooseTarget(cost, selection) {
    let last = -1;
    for (const index of byCost[cost]) if (owned[index] < quotas[index]) {
      last = index; const copies = capacities[index] - owned[index];
      if (selection < copies) return index;
      selection -= copies;
    }
    return last;
  }
  function ordinaryDraw() {
    let cost;
    if (!mayExhaust) {
      const selection = random(); cost = 0;
      while (cost < 4 && selection >= cumulativeOdds[cost]) ++cost;
    } else {
      let availableOdds = 0;
      for (let tier = 0; tier < 5; ++tier) if (remainingByCost[tier] - ownedByCost[tier] - nuisance[tier] > 0) availableOdds += odds[tier];
      let selection = random() * availableOdds; cost = 4;
      for (let tier = 0; tier < 5; ++tier) if (remainingByCost[tier] - ownedByCost[tier] - nuisance[tier] > 0 && odds[tier]) {
        cost = tier; if (selection < odds[tier]) break; selection -= odds[tier];
      }
    }
    if (active[cost]) {
      const selection = random() * (remainingByCost[cost] - ownedByCost[cost] - nuisance[cost]);
      if (selection < active[cost]) { buy(chooseTarget(cost, selection)); return; }
    }
    ++nuisance[cost];
  }
  function hazardForState() {
    if (hazardCache.has(stateCode)) return hazardCache.get(stateCode);
    prepareForms();
    const held = Array.from(ownedByCost), needed = Array.from(active), memo = new Float64Array(forms.length); memo.fill(NaN);
    function progress(formIndex) {
      if (!Number.isNaN(memo[formIndex])) return memo[formIndex];
      const form = forms[formIndex];
      if (!form.remaining) return memo[formIndex] = 0;
      let chance = 0, availableOdds = 0;
      for (let cost = 0; cost < 5; ++cost) if (remainingByCost[cost] - held[cost] - form.nuisance[cost] > 0) availableOdds += odds[cost];
      for (let cost = 0; cost < 5; ++cost) {
        const denominator = remainingByCost[cost] - held[cost] - form.nuisance[cost];
        if (denominator <= 0 || !odds[cost]) continue;
        const costProbability = odds[cost] / availableOdds;
        chance += costProbability * needed[cost] / denominator;
        const noiseProbability = costProbability * (denominator - needed[cost]) / denominator;
        if (noiseProbability > 0) chance += noiseProbability * progress(form.noise[cost]);
      }
      return memo[formIndex] = Math.min(1, chance);
    }
    const chances = phases.map(slots => progress(lookup[slots]));
    const hazard = { progress, chances }; hazardCache.set(stateCode, hazard); return hazard;
  }
  function conditionedShop(hazard, slots) {
    let formIndex = lookup[slots], remaining = slots;
    const hitWeights = new Float64Array(5), noiseWeights = new Float64Array(5);
    while (remaining) {
      const form = forms[formIndex]; let availableOdds = 0, total = 0;
      for (let cost = 0; cost < 5; ++cost) if (remainingByCost[cost] - ownedByCost[cost] - nuisance[cost] > 0) availableOdds += odds[cost];
      for (let cost = 0; cost < 5; ++cost) {
        const denominator = remainingByCost[cost] - ownedByCost[cost] - nuisance[cost];
        const costProbability = denominator > 0 ? odds[cost] / availableOdds : 0;
        hitWeights[cost] = costProbability ? costProbability * active[cost] / denominator : 0;
        noiseWeights[cost] = costProbability && denominator > active[cost]
          ? costProbability * (denominator - active[cost]) / denominator * hazard.progress(form.noise[cost]) : 0;
        total += hitWeights[cost] + noiseWeights[cost];
      }
      if (!(total > 0)) throw new Error('Lobby hit probability is below numeric precision.');
      let selection = random() * total, selectedCost = -1, hit = false, lastCost = -1, lastHit = false;
      for (let cost = 0; cost < 5; ++cost) {
        if (hitWeights[cost]) {
          lastCost = cost; lastHit = true;
          if (selection < hitWeights[cost]) { selectedCost = cost; hit = true; break; }
          selection -= hitWeights[cost];
        }
        if (noiseWeights[cost]) {
          lastCost = cost; lastHit = false;
          if (selection < noiseWeights[cost]) { selectedCost = cost; break; }
          selection -= noiseWeights[cost];
        }
      }
      if (selectedCost < 0) { selectedCost = lastCost; hit = lastHit; }
      --remaining;
      if (hit) {
        buy(chooseTarget(selectedCost, random() * active[selectedCost]));
        while (remaining-- > 0 && missing) ordinaryDraw();
        return;
      }
      ++nuisance[selectedCost]; formIndex = form.noise[selectedCost];
    }
  }
  const samples = Math.ceil(Math.max(requestedSamples, 2 * phaseCount) / phaseCount) * phaseCount;
  const phaseMeans = new Float64Array(phaseCount), phaseM2 = new Float64Array(phaseCount), phaseSamples = new Uint32Array(phaseCount);
  for (let trial = 0; trial < samples; ++trial) {
    const firstPhase = trial % phaseCount;
    let phase = firstPhase, shops = 0;
    owned.fill(0); ownedByCost.fill(0); active.set(initialActive); stateCode = 0; missing = totalRequired;
    while (missing) {
      nuisance.fill(0);
      let initialHitChance = 0, availableOdds = 0;
      for (let cost = 0; cost < 5; ++cost) if (remainingByCost[cost] > ownedByCost[cost]) availableOdds += odds[cost];
      for (let cost = 0; cost < 5; ++cost) if (active[cost]) initialHitChance += odds[cost] / availableOdds * active[cost] / (remainingByCost[cost] - ownedByCost[cost]);
      if (initialHitChance < .02) {
        const hazard = hazardForState();
        const logFailure = hazard.chances.reduce((sum, chance) => sum + Math.log1p(-chance), 0);
        if (!logFailure) throw new Error('Lobby hit probability is below numeric precision.');
        const emptyCycles = Math.floor(Math.log(randomTail()) / logFailure);
        let cycleHitChance = 0, failure = 1;
        for (let offset = 0; offset < phaseCount; ++offset) {
          const current = (phase + offset) % phaseCount;
          cycleHitChance += failure * hazard.chances[current]; failure *= 1 - hazard.chances[current];
        }
        let selection = random() * cycleHitChance, offset = 0; failure = 1;
        for (; offset < phaseCount - 1; ++offset) {
          const current = (phase + offset) % phaseCount, weight = failure * hazard.chances[current];
          if (selection < weight) break;
          selection -= weight; failure *= 1 - hazard.chances[current];
        }
        const skipped = emptyCycles * phaseCount + offset; skippedEmptyShops += skipped; shops += skipped + 1;
        phase = (phase + offset) % phaseCount; conditionedShop(hazard, phases[phase]);
      } else {
        ++shops; ++directShops;
        for (let draw = 0; draw < phases[phase] && missing; ++draw) ordinaryDraw();
      }
      phase = (phase + 1) % phaseCount;
    }
    const count = ++phaseSamples[firstPhase], delta = shops - phaseMeans[firstPhase];
    phaseMeans[firstPhase] += delta / count; phaseM2[firstPhase] += delta * (shops - phaseMeans[firstPhase]);
  }
  const expectedShops = phaseMeans.reduce((sum, mean) => sum + mean / phaseCount, 0);
  const varianceMean = phaseM2.reduce((sum, m2, phase) => sum + m2 / (phaseSamples[phase] - 1) / phaseSamples[phase] / phaseCount / phaseCount, 0);
  return finish(expectedShops, 'monte-carlo', samples, Math.sqrt(varianceMean), {
    shopsByInitialPhase: Array.from(phaseMeans), seed: initialSeed, skippedEmptyShops, directShops,
    rareStates: hazardCache.size, samplingIntervalOnly: true });
}
// ---------------- trait flex: lobby gold estimates end ----------------
if (typeof module !== 'undefined' && module.exports) module.exports = { tftFlexLobbyEstimate };
