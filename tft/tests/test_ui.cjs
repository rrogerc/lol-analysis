"use strict";

// Consolidated TFT dashboard UI and solver test suite.
// Run: node tft/tests/test_ui.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { performance } = require("node:perf_hooks");
const {
  INDEX_HTML,
  STYLES_CSS,
  WORKER_SOURCE,
  plain,
  readStatic,
  createWorkerClass,
  loadDashboard,
  until,
} = require("../../web/tests/harness.cjs");
const {
  tftFlexGoldEstimate,
  tftFlexLobbyEstimate,
} = require("../../web/static/tft-flex-worker.js");

let passedSuites = 0;

function near(actual, expected, description, relative = 2e-11) {
  assert(Number.isFinite(actual), `${description}: must be finite`);
  assert(
    Math.abs(actual - expected) <= relative * Math.max(1, Math.abs(expected)),
    `${description}: expected ${expected}, received ${actual}`
  );
}

// ============================================================================
// 1. Retired Compositions Normalization and Static Markup Checks
// ============================================================================

async function runCompositionsRetirementSuite() {
  assert(!INDEX_HTML.includes("tft-compositions-panel"), "Retired compositions panel is absent from index.html");
  assert(!INDEX_HTML.includes("tft-composition-back"), "Retired compositions back button is absent from index.html");
  assert(!INDEX_HTML.includes("tft-composition-note"), "Retired compositions note is absent from index.html");
  assert(!STYLES_CSS.includes("#tft-compositions-panel"), "Retired compositions panel CSS is absent");

  const meta = { tiers: ["fixture"], defaultTier: "fixture", platforms: [], inventory: [] };
  const tftMeta = {
    revision: "fixture-v1",
    set: 14,
    patch: "18.3",
    items: [],
    stars: [1, 2, 3],
    geometries: { clump: "Clumped", spread: "Spread" },
    dummy: {
      count: 6,
      tanks: 4,
      others: 2,
      slots: Array.from({ length: 6 }, (_, i) => ({
        kind: i < 4 ? "tank" : "non-tank",
        line: i < 4 ? "frontline" : "backline",
        nearby: i < 4,
      })),
    },
    traitContexts: { bare: "None", low: "First breakpoint", high: "Max traits" },
    tankThreats: [{ key: "mixed", label: "Mixed" }],
    units: [{ slug: "alpha", api: "Alpha", name: "Alpha", objective: "carry", cost: 4, stars: [1, 2], traits: [] }],
  };

  const makeResponder = url => {
    if (url === "api/meta.json") return meta;
    if (url.startsWith("api/rows/")) return { patches: ["15.1"], rows: [], totalGames: 0 };
    if (url === "api/builds/meta.json") return { champions: [], scenarios: [] };
    if (url === "api/tft/meta.json") return tftMeta;
    if (url === "api/tft/status.json") return { revision: "fixture-v1", publicationRevision: "site-v1", patch: "18.3" };
    if (url.startsWith("api/tft/leaderboard/")) {
      const key = url.split("/").at(-1).replace(/\.json$/, "");
      return { revision: "fixture-v1", selection: { key }, damage: [], tanks: [], complete: true };
    }
    return { pending: true };
  };

  // Verify #view=tft&tftview=compositions falls back cleanly to builds on init
  const h1 = loadDashboard({ initialHash: "#view=tft&tftview=compositions", api: async url => makeResponder(url) });
  assert.equal(h1.element("tft-compositions-panel"), null);
  assert.equal(h1.element("tft-composition-back"), null);
  assert.equal(h1.element("tft-composition-note"), null);
  assert.deepEqual(
    h1.queryAll("#tft-mode-seg button").map(b => b.dataset.key),
    ["builds", "leaderboard", "early", "traits"]
  );
  await h1.context.init();
  await until(() => !h1.context.tstate.opening, "Initial TFT builds load completes");
  assert.equal(h1.context.state.view, "tft");
  assert.equal(h1.context.tstate.mode, "builds");
  assert.equal(h1.element("view-tft").dataset.mode, "builds");
  assert.equal(h1.element("tft-champion-panel").hidden, false);
  assert.equal(h1.element("tft-leaderboard-panel").hidden, true);
  const params1 = Object.fromEntries(new URLSearchParams(h1.location.hash.slice(1)));
  assert.equal(params1.tab, "tft");
  assert.equal(params1.tftview, undefined);
  assert(!h1.location.hash.includes("compositions"));
  assert(!h1.location.hash.includes("tcomp"));

  // Verify #tab=tft&tftview=compositions and setTftMode("compositions") also fall back to builds
  await h1.context.setTftMode("leaderboard", { load: false });
  assert.equal(h1.context.tstate.mode, "leaderboard");
  await h1.setHash("#tab=tft&tftview=compositions");
  await until(() => !h1.context.tstate.opening, "Hash navigation to retired compositions settles");
  assert.equal(h1.context.state.view, "tft");
  assert.equal(h1.context.tstate.mode, "builds");
  assert.equal(h1.element("tft-champion-panel").hidden, false);
  assert.equal(h1.element("tft-leaderboard-panel").hidden, true);
  assert(!h1.location.hash.includes("compositions"));

  await h1.context.setTftMode("leaderboard", { load: false });
  await h1.context.setTftMode("compositions", { load: false });
  assert.equal(h1.context.tstate.mode, "builds");
  assert.equal(h1.element("view-tft").dataset.mode, "builds");
  assert.equal(h1.element("tft-champion-panel").hidden, false);
  assert.equal(h1.element("tft-leaderboard-panel").hidden, true);

  ++passedSuites;
}

// ============================================================================
// 2. Trait Flex Gold Solver and Worker Protocol (test-tft-flex-gold.cjs)
// ============================================================================

async function runFlexGoldSuite() {
  const estimate = tftFlexGoldEstimate;
  assert.equal(typeof estimate, "function");

  const shop = {
    costOdds: [0.15, 0.20, 0.32, 0.30, 0.03],
    championCounts: [14, 13, 14, 14, 10],
    bagCopies: [30, 25, 18, 10, 9],
    refreshCost: 2,
    visibleSlots: [4, 5],
  };
  const one = cost => Array.from({ length: 5 }, (_, index) => (index === cost ? 1 : 0));

  function checkGoldAndInputs() {
    const requirements = [2, 1, 2, 3, 0];
    const snapshot = JSON.stringify({ shop, requirements });
    const result = estimate(shop, requirements);
    assert.equal(JSON.stringify({ shop, requirements }), snapshot, "The estimator preserves inputs");
    assert.deepEqual(plain(result.copiesByCost), [2, 1, 2, 9, 0], "Every 4-cost needs three copies; every other champion needs one");
    assert.equal(result.purchaseGold, 46);
    near(result.refreshGold, result.expectedShops * 2, "Every generated shop costs 2 gold");
    near(result.totalGold, result.refreshGold + result.purchaseGold, "Purchase and refresh gold add up");
    assert.deepEqual(plain(result), plain(estimate(shop, requirements)), "Expected values are deterministic");
    result.copiesByCost[3] = 99;
    assert.equal(estimate(shop, requirements).copiesByCost[3], 9, "Output mutation cannot corrupt another estimate");
    const frozenShop = Object.freeze(
      Object.fromEntries(
        Object.entries(shop).map(([name, value]) => [name, Array.isArray(value) ? Object.freeze(value.slice()) : value])
      )
    );
    near(
      estimate(frozenShop, Object.freeze(requirements.slice())).expectedShops,
      result.expectedShops,
      "Read-only inputs are supported"
    );
    const empty = estimate(shop, [0, 0, 0, 0, 0]);
    assert.equal(empty.expectedShops, 0);
    assert.equal(empty.totalGold, 0);
    assert.deepEqual(plain(empty.copiesByCost), [0, 0, 0, 0, 0]);
    near(
      estimate({ ...shop, visibleSlots: undefined }, requirements).expectedShops,
      result.expectedShops,
      "The default visibility cycle is four and five"
    );
    const free = estimate({ ...shop, refreshCost: 0 }, one(0));
    assert.equal(free.refreshGold, 0);
    assert.equal(free.totalGold, 1);
    for (const impossibleShop of [
      { ...shop, championCounts: [0, 13, 14, 14, 10] },
      { ...shop, costOdds: [0, 0.35, 0.32, 0.30, 0.03] },
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
    assert.throws(() => estimate({ ...shop, costOdds: [0.1, 0.1, 0.1, 0.1, 0.1] }, one(0)), /sum to one/);
    assert.throws(() => estimate({ ...shop, visibleSlots: [] }, one(0)), /visible shop/);
    assert.throws(() => estimate({ ...shop, bagCopies: [0, 25, 18, 10, 9] }, one(0)), /bagCopies/);

    const isolated = vm.createContext({});
    vm.runInContext(estimate.toString(), isolated);
    near(
      isolated.tftFlexGoldEstimate(shop, one(3)).refreshGold,
      estimate(shop, one(3)).refreshGold,
      "The function is self-contained for a Worker"
    );
  }

  function checkIndependentSingleChampionValues() {
    const alternating = [42.2424644521, 29.6484442234, 20.1975118701, 69.7028503288, 148.8480170819];
    const fiveSlots = [38.1168676, 26.7823261, 18.2761402, 62.8042186, 134.0546009];
    for (let cost = 0; cost < 5; ++cost) {
      near(estimate(shop, one(cost)).refreshGold, alternating[cost], `Alternating-shop golden cost ${cost + 1}`);
      near(
        estimate({ ...shop, visibleSlots: [5] }, one(cost)).refreshGold,
        fiveSlots[cost],
        `Five-slot golden cost ${cost + 1}`,
        4e-9
      );
    }
    const onlyOneCost = cost => ({ ...shop, costOdds: one(cost), visibleSlots: [1] });
    near(
      estimate(onlyOneCost(0), one(0)).expectedShops,
      shop.championCounts[0],
      "One visible slot finds a non-4-cost in one champion-count waiting time"
    );
    const count = shop.championCounts[3];
    const bag = shop.bagCopies[3];
    const fourWait = [0, 1, 2].reduce((sum, owned) => sum + (count * bag - owned) / (bag - owned), 0);
    near(
      estimate(onlyOneCost(3), one(3)).expectedShops,
      fourWait,
      "Three copies have three independently derived depleted-pool waiting times"
    );
    const certainFour = { ...onlyOneCost(3), championCounts: [14, 13, 14, 1, 10] };
    assert.equal(estimate(certainFour, one(3)).expectedShops, 3, "There is no free initial shop");
    assert.equal(estimate({ ...certainFour, visibleSlots: [2] }, one(3)).expectedShops, 2);
    assert.equal(estimate({ ...certainFour, visibleSlots: [5] }, one(3)).expectedShops, 1);

    const hypergeometricFailure = slots => {
      let failure = 1;
      for (let draw = 0; draw < slots; ++draw) {
        failure *= (14 * 30 - 30 - draw) / (14 * 30 - draw);
      }
      return failure;
    };
    const q4 = hypergeometricFailure(4);
    const q5 = hypergeometricFailure(5);
    const tierOnly = { ...onlyOneCost(0), visibleSlots: [4, 5] };
    const expectedCycle = (2 + q4 + q5) / (2 * (1 - q4 * q5));
    const cycle = estimate(tierOnly, one(0));
    near(cycle.expectedShops, expectedCycle, "A one-unit hypergeometric four/five cycle");
    near(cycle.diagnostics.shopsByInitialPhase[0], (1 + q4) / (1 - q4 * q5), "Four-slot first phase");
    near(cycle.diagnostics.shopsByInitialPhase[1], (1 + q5) / (1 - q4 * q5), "Five-slot first phase");
    near(
      estimate({ ...tierOnly, visibleSlots: [5] }, one(0)).expectedShops,
      1 / (1 - q5),
      "A one-unit hypergeometric fixed five-slot shop"
    );
    const exact = estimate({ ...shop, visibleSlots: [5] }, one(0)).expectedShops;
    const replacementApproximation = 1 / (1 - Math.pow(1 - shop.costOdds[0] / shop.championCounts[0], 5));
    assert(exact < replacementApproximation - 0.001, "Sampling the shop without replacement differs from independent slots");

    const rare = {
      costOdds: [1e-16, 1 - 1e-16, 0, 0, 0],
      championCounts: [1, 1, 0, 0, 0],
      bagCopies: [30, 25, 1, 1, 1],
      visibleSlots: [1],
      refreshCost: 2,
    };
    near(estimate(rare, one(0)).expectedShops, 1e16, "Tiny success probabilities avoid cancellation");
  }

  function exhaustiveReference(model, requirements) {
    const champions = [];
    const targets = [];
    for (let cost = 0; cost < 5; ++cost) {
      for (let index = 0; index < model.championCounts[cost]; ++index) {
        const target = index < requirements[cost] ? targets.length : -1;
        if (target >= 0) targets.push({ cost, required: cost === 3 ? 3 : 1 });
        champions.push({ cost, copies: model.bagCopies[cost], target });
      }
    }
    const radices = targets.map(target => target.required + 1);
    const strides = [1];
    for (let axis = 1; axis < radices.length; ++axis) strides.push(strides[axis - 1] * radices[axis - 1]);
    const stateCount = radices.reduce((product, radix) => product * radix, 1);
    const terminal = stateCount - 1;
    const codeFor = owned => owned.reduce((code, count, axis) => code + count * strides[axis], 0);
    const ownedFor = code => radices.map((radix, axis) => Math.floor(code / strides[axis]) % radix);
    function shopDistribution(code, slots) {
      const owned = ownedFor(code);
      const available = champions.map(champion => champion.copies - (champion.target >= 0 ? owned[champion.target] : 0));
      const distribution = new Map();
      function visit(remaining, probability) {
        const after = codeFor(owned);
        if (!remaining || after === terminal) {
          distribution.set(after, (distribution.get(after) || 0) + probability);
          return;
        }
        const totals = new Array(5).fill(0);
        champions.forEach((champion, index) => { totals[champion.cost] += available[index]; });
        for (let index = 0; index < champions.length; ++index) {
          const champion = champions[index];
          const copies = available[index];
          if (!copies || !model.costOdds[champion.cost]) continue;
          const chance = (model.costOdds[champion.cost] * copies) / totals[champion.cost];
          const buy = champion.target >= 0 && owned[champion.target] < targets[champion.target].required;
          --available[index];
          if (buy) ++owned[champion.target];
          visit(remaining - 1, probability * chance);
          ++available[index];
          if (buy) --owned[champion.target];
        }
      }
      visit(slots, 1);
      near([...distribution.values()].reduce((sum, chance) => sum + chance, 0), 1, "Exhaustive shop transition probabilities conserve mass");
      return distribution;
    }
    const phases = model.visibleSlots.length;
    const rows = terminal * phases;
    const matrix = Array.from({ length: rows }, () => new Float64Array(rows + 1));
    for (let code = 0; code < terminal; ++code) {
      for (let phase = 0; phase < phases; ++phase) {
        const row = code * phases + phase;
        matrix[row][row] = 1;
        matrix[row][rows] = 1;
        const nextPhase = (phase + 1) % phases;
        for (const [after, chance] of shopDistribution(code, model.visibleSlots[phase])) {
          if (after !== terminal) matrix[row][after * phases + nextPhase] -= chance;
        }
      }
    }
    for (let column = 0; column < rows; ++column) {
      let pivot = column;
      for (let row = column + 1; row < rows; ++row) {
        if (Math.abs(matrix[row][column]) > Math.abs(matrix[pivot][column])) pivot = row;
      }
      assert(Math.abs(matrix[pivot][column]) > 1e-14, "Reference Markov system has a unique solution");
      [matrix[column], matrix[pivot]] = [matrix[pivot], matrix[column]];
      const divisor = matrix[column][column];
      for (let entry = column; entry <= rows; ++entry) matrix[column][entry] /= divisor;
      for (let row = 0; row < rows; ++row) {
        if (row !== column && matrix[row][column]) {
          const factor = matrix[row][column];
          for (let entry = column; entry <= rows; ++entry) matrix[row][entry] -= factor * matrix[column][entry];
        }
      }
    }
    return Array.from({ length: phases }, (_, phase) => matrix[phase][rows]);
  }

  function checkExhaustiveTinyPools() {
    const models = [
      {
        model: { costOdds: [0.35, 0, 0, 0.65, 0], championCounts: [2, 0, 0, 3, 0], bagCopies: [3, 1, 1, 4, 1], refreshCost: 2, visibleSlots: [2, 3] },
        requirements: [1, 0, 0, 2, 0],
      },
      {
        model: { costOdds: [1, 0, 0, 0, 0], championCounts: [3, 0, 0, 0, 0], bagCopies: [2, 1, 1, 1, 1], refreshCost: 2, visibleSlots: [2] },
        requirements: [2, 0, 0, 0, 0],
      },
      {
        model: { costOdds: [0, 0, 0, 1, 0], championCounts: [0, 0, 0, 3, 0], bagCopies: [1, 1, 1, 4, 1], refreshCost: 2, visibleSlots: [2] },
        requirements: [0, 0, 0, 2, 0],
      },
    ];
    for (const { model, requirements } of models) {
      const independent = exhaustiveReference(model, requirements);
      const result = estimate(model, requirements);
      near(
        result.expectedShops,
        independent.reduce((sum, value) => sum + value / independent.length, 0),
        "Grouped DP agrees with exhaustive champion-identity Markov chains"
      );
      for (let phase = 0; phase < independent.length; ++phase) {
        near(result.diagnostics.shopsByInitialPhase[phase], independent[phase], `Independent initial shop phase ${phase}`);
      }
    }
  }

  function checkJointCollectionAndBoundedStates() {
    const requirements = [1, 1, 1, 2, 1];
    const joint = estimate(shop, requirements);
    const separate = requirements.reduce((sum, count, cost) => sum + count * estimate(shop, one(cost)).expectedShops, 0);
    assert(joint.expectedShops < separate * 0.75, "One rolldown collects multiple targets together; summing separate costs overcounts");
    const maximum = estimate(shop, [1, 1, 1, 4, 1]);
    assert.equal(maximum.diagnostics.states, 560, "The largest eight-unit grouped state grid has 560 states");
    near(maximum.purchaseGold, 59, "All additional 4-cost supports also require 2 stars");
    near(estimate({ ...shop, refreshCost: 3 }, requirements).expectedShops, joint.expectedShops, "Refresh prices do not change hit probabilities");
    near(estimate({ ...shop, refreshCost: 3 }, requirements).refreshGold, joint.expectedShops * 3, "Refresh gold scales with price");
  }

  function checkRerollProfilesAndWorkerBundle() {
    const pool = shop.championCounts.flatMap((count, cost) =>
      Array.from({ length: count }, (_, index) => ({ api: `pool-${cost}-${index}`, cost: cost + 1, heldCopies: 0 }))
    );
    for (const coreCost of [2, 3]) {
      const requirements = coreCost === 2 ? [1, 4, 1, 2, 0] : [1, 1, 4, 2, 0];
      const before = JSON.stringify({ shop, requirements });
      const result = estimate(shop, requirements, coreCost);
      const expectedCopies = coreCost === 2 ? [1, 36, 1, 2, 0] : [1, 1, 36, 2, 0];
      assert.deepEqual(plain(result.copiesByCost), expectedCopies, "Every selected-tier champion, including supports, needs nine copies");
      assert.equal(result.purchaseGold, coreCost === 2 ? 84 : 119, "Purchase gold charges all reroll copies and one copy of four-cost supports");
      near(result.totalGold, result.purchaseGold + result.expectedShops * 2, "Reroll refresh and purchase gold add up");
      assert.equal(result.method, "monte-carlo");
      assert(result.standardErrorGold > 0 && Number.isFinite(result.standardErrorGold));
      assert(result.confidence95 && result.diagnostics.samplingIntervalOnly);
      assert.equal(JSON.stringify({ shop, requirements }), before, "Reroll estimation preserves its inputs");
      const targets = requirements.flatMap((count, cost) =>
        pool
          .filter(unit => unit.cost === cost + 1)
          .slice(0, count)
          .map(unit => ({ api: unit.api, cost: cost + 1, copies: cost + 1 === coreCost ? 9 : 1 }))
      );
      const reference = tftFlexLobbyEstimate(shop, pool, targets);
      near(result.expectedShops, reference.expectedShops, "Reroll baseline is the zero-held finite-pool model");
      near(result.totalGold, reference.totalGold, "Baseline and zero-held lobby estimate agree");
      near(estimate(shop, requirements, String(coreCost)).totalGold, result.totalGold, "Dropdown cost strings normalize");
      const frozenShop = Object.freeze(
        Object.fromEntries(Object.entries(shop).map(([key, value]) => [key, Array.isArray(value) ? Object.freeze(value.slice()) : value]))
      );
      near(estimate(frozenShop, Object.freeze(requirements.slice()), coreCost).totalGold, result.totalGold, "Read-only reroll inputs are supported");
      const oneSlot = { ...shop, visibleSlots: [1] };
      const wanted = one(coreCost - 1);
      const bag = shop.bagCopies[coreCost - 1];
      const total = shop.championCounts[coreCost - 1] * bag;
      const hand = Array.from({ length: 9 }, (_, owned) => (total - owned) / (shop.costOdds[coreCost - 1] * (bag - owned))).reduce(
        (sum, waiting) => sum + waiting,
        0
      );
      const single = estimate(oneSlot, wanted, coreCost);
      assert.equal(single.method, "exact");
      near(single.expectedShops, hand, `Nine-copy native one-visible-slot analytic expectation at cost ${coreCost}`);
      assert.equal(single.purchaseGold, coreCost * 9);
      const tooFew = shop.bagCopies.slice();
      tooFew[coreCost - 1] = 8;
      assert.equal(estimate({ ...shop, bagCopies: tooFew }, wanted, coreCost).totalGold, Infinity, "A nine-copy target cannot be completed from an eight-copy bag");
      const impossibleOdds = shop.costOdds.slice();
      impossibleOdds[0] += impossibleOdds[coreCost - 1];
      impossibleOdds[coreCost - 1] = 0;
      assert.equal(estimate({ ...shop, costOdds: impossibleOdds }, wanted, coreCost).expectedShops, Infinity);
      assert.equal(estimate(shop, [0, 0, 0, 0, 0], coreCost).totalGold, 0);
    }
    const fourSupport = estimate(shop, one(3), 2);
    const fourCore = estimate(shop, one(3), 4);
    assert.equal(fourSupport.purchaseGold, 4);
    assert.equal(fourCore.purchaseGold, 12);
    assert(fourSupport.totalGold < fourCore.totalGold, "A one-copy support is cheaper than the old two-star four-cost target");
    assert.deepEqual(plain(estimate(shop, [1, 1, 1, 4, 1], 4)), plain(estimate(shop, [1, 1, 1, 4, 1])), "The explicit four-cost profile preserves the old exact output");
    for (const coreCost of [1, 5, 2.5, NaN, "mixed"]) {
      assert.throws(() => estimate(shop, one(0), coreCost), /core cost/);
    }

    const isolated = vm.createContext({});
    vm.runInContext(WORKER_SOURCE, isolated);
    for (const cost of [2, 3, 4]) {
      near(
        isolated.tftFlexGoldEstimate(shop, one(cost - 1), cost).totalGold,
        estimate(shop, one(cost - 1), cost).totalGold,
        `Standalone worker script estimate for core cost ${cost}`
      );
    }
  }

  async function checkWorkerProtocolAndSyncFallback() {
    const { Worker } = createWorkerClass();
    const w = new Worker("tft-flex-worker.js");
    const callWorker = payload =>
      new Promise(resolve => {
        w.onmessage = ({ data }) => resolve(data);
        w.postMessage(payload);
      });
    const baselineReply = await callWorker({ id: 1, shop, requirements: one(3), coreCost: 4 });
    assert.equal(baselineReply.id, 1);
    assert.equal(baselineReply.ok, true);
    near(baselineReply.result.totalGold, estimate(shop, one(3), 4).totalGold, "Worker message protocol computes baseline gold");

    const pool = shop.championCounts.flatMap((count, cost) =>
      Array.from({ length: count }, (_, index) => ({ api: `u-${cost + 1}-${index}`, cost: cost + 1, heldCopies: 0 }))
    );
    const targets = [{ api: "u-4-0", cost: 4, copies: 3 }];
    const lobbyReply = await callWorker({ id: 2, shop, pool, targets });
    assert.equal(lobbyReply.id, 2);
    assert.equal(lobbyReply.ok, true);
    near(lobbyReply.result.totalGold, tftFlexLobbyEstimate(shop, pool, targets).totalGold, "Worker message protocol computes lobby gold");

    const errorReply = await callWorker({ id: 3, shop, requirements: [-1, 0, 0, 0, 0], coreCost: 4 });
    assert.equal(errorReply.id, 3);
    assert.equal(errorReply.ok, false);
    assert.match(errorReply.error, /nonnegative/);
    w.terminate();

    // Verify synchronous fallback when Worker is unavailable in the browser global scope
    const fallback = loadDashboard({ workers: false });
    assert.equal(typeof fallback.context.Worker, "undefined");
    const syncGold = await fallback.context.tftFlexGoldRequest(shop, one(3), 4);
    near(syncGold.totalGold, estimate(shop, one(3), 4).totalGold, "Synchronous fallback computes baseline gold without Worker");
    const syncLobby = await fallback.context.tftFlexLobbyRequest(shop, pool, targets);
    near(syncLobby.totalGold, tftFlexLobbyEstimate(shop, pool, targets).totalGold, "Synchronous fallback computes lobby gold without Worker");
  }

  checkGoldAndInputs();
  checkIndependentSingleChampionValues();
  checkExhaustiveTinyPools();
  checkJointCollectionAndBoundedStates();
  checkRerollProfilesAndWorkerBundle();
  await checkWorkerProtocolAndSyncFallback();
  ++passedSuites;
}

// ============================================================================
// 3. Trait Flex Lobby Gold Solver (test-tft-flex-lobby-gold.cjs)
// ============================================================================

function runFlexLobbyGoldSuite() {
  const baseline = tftFlexGoldEstimate;
  const estimate = tftFlexLobbyEstimate;
  const page = loadDashboard();
  assert.equal(
    page.context.tftFlexLobbyEstimate.toString(),
    estimate.toString(),
    "Browser global solver and Worker solver share the single static source"
  );

  const shop = {
    costOdds: [0.15, 0.20, 0.32, 0.30, 0.03],
    championCounts: [14, 13, 14, 14, 10],
    bagCopies: [30, 25, 18, 10, 9],
    refreshCost: 2,
    visibleSlots: [4, 5],
  };
  const one = cost => Array.from({ length: 5 }, (_, index) => (index === cost ? 1 : 0));
  const poolFor = model =>
    model.championCounts.flatMap((count, cost) =>
      Array.from({ length: count }, (_, index) => ({ api: `unit-${cost + 1}-${index}`, cost: cost + 1, heldCopies: 0 }))
    );
  const targetsFor = (pool, counts) =>
    counts.flatMap((count, cost) =>
      pool
        .filter(unit => unit.cost === cost + 1)
        .slice(0, count)
        .map(unit => ({ api: unit.api, cost: unit.cost, copies: unit.cost === 4 ? 3 : 1 }))
    );
  const held = (pool, api, copies) => pool.map(unit => ({ ...unit, heldCopies: unit.api === api ? copies : unit.heldCopies }));

  function sampledNear(result, exact, label) {
    assert.equal(result.method, "monte-carlo", `${label}: MC was exercised`);
    assert(result.standardErrorGold > 0 && Number.isFinite(result.standardErrorGold), `${label}: sampling error is reported`);
    assert(
      Math.abs(result.totalGold - exact.totalGold) < 6 * result.standardErrorGold + 1e-9,
      `${label}: expected ${exact.totalGold}, got ${result.totalGold} +- ${result.standardErrorGold} SE`
    );
    near(result.confidence95[1] - result.totalGold, 1.96 * result.standardErrorGold, `${label}: 95% interval`);
  }

  function checkZeroContestAndIdentity() {
    const pool = poolFor(shop);
    for (const counts of [...Array.from({ length: 5 }, (_, cost) => one(cost)), [2, 1, 2, 3, 0], [1, 1, 1, 4, 1]]) {
      const targets = targetsFor(pool, counts);
      const result = estimate(shop, pool, targets);
      const reference = baseline(shop, counts);
      assert.equal(result.method, "exact", "Zero contest preserves exact baseline math");
      near(result.expectedShops, reference.expectedShops, "Uncontested shops");
      near(result.totalGold, reference.totalGold, "Uncontested total gold");
      assert.equal(result.purchaseGold, reference.purchaseGold);
      assert.deepEqual(plain(result.copiesByCost), plain(reference.copiesByCost));
      assert.equal(result.standardErrorGold, 0);
      assert.equal(result.samples, 0);
    }
    const targets = targetsFor(pool, [2, 1, 2, 3, 0]);
    const snapshot = JSON.stringify([shop, pool, targets]);
    const first = estimate(shop, pool, targets);
    const reversed = estimate(shop, pool.slice().reverse(), targets.slice().reverse());
    assert.equal(JSON.stringify([shop, pool, targets]), snapshot, "Inputs are unchanged");
    assert.deepEqual(plain(first), plain(reversed), "Pool and roster order are immaterial");
    const renamedPool = pool.map(unit => ({ ...unit, api: `renamed-${unit.api}` }));
    const renamedTargets = targets.map(target => ({ ...target, api: `renamed-${target.api}` }));
    assert.equal(
      estimate(shop, renamedPool, renamedTargets, { signatureOnly: true }).signature,
      first.signature,
      "Numerical cache signatures omit champion names"
    );
    const frozen = value => Object.freeze(value.map(item => Object.freeze({ ...item })));
    near(estimate(shop, frozen(pool), frozen(targets)).totalGold, first.totalGold, "Read-only pools and targets are supported");
    const summary = estimate(shop, pool, targets, { signatureOnly: true });
    assert(
      !("expectedShops" in summary) && !("totalGold" in summary) && !("refreshGold" in summary),
      "Signature-only validation never returns a fake estimate"
    );
    assert.equal(summary.signature, first.signature);
    assert.equal(summary.purchaseGold, first.purchaseGold);
    const isolated = vm.createContext({});
    vm.runInContext(estimate.toString(), isolated);
    near(isolated.tftFlexLobbyEstimate(shop, pool, targets).totalGold, first.totalGold, "The engine works without page globals");
    const empty = estimate(shop, pool, []);
    assert.equal(empty.totalGold, 0);
    assert.equal(empty.expectedShops, 0);
    assert.equal(empty.method, "exact");
  }

  function checkContestAndUnavailable() {
    const pool = poolFor(shop);
    const target = targetsFor(pool, one(3));
    const api = target[0].api;
    const clear = estimate(shop, pool, target);
    const contested = estimate(shop, held(pool, api, 5), target);
    assert(contested.totalGold > clear.totalGold * 1.3, "Holding the target reduces its hit chance");
    const other = pool.find(unit => unit.cost === 4 && unit.api !== api).api;
    const depletedOthers = estimate(shop, held(pool, other, 5), target);
    assert(depletedOthers.totalGold < clear.totalGold, "Non-target depletion in the same tier can improve target odds");
    near(contested.remainingByCost[3], depletedOthers.remainingByCost[3], "Both scenarios deplete the same total tier copies");
    assert.notEqual(contested.signature, depletedOthers.signature, "Target availability distinguishes identical tier totals");
    const unrelated = pool.map(unit => ({ ...unit, heldCopies: unit.cost === 1 ? 10 : 0 }));
    near(estimate(shop, unrelated, target).totalGold, clear.totalGold, "Another nonempty tier cannot change the fixed 4-cost odds");
    const irrelevantSwap = held(held(pool, "unit-4-1", 2), "unit-4-2", 3);
    const irrelevantSwap2 = held(held(pool, "unit-4-1", 3), "unit-4-2", 2);
    assert.equal(
      estimate(shop, irrelevantSwap, target, { signatureOnly: true }).signature,
      estimate(shop, irrelevantSwap2, target, { signatureOnly: true }).signature,
      "Non-target identities affect the tier total only"
    );
    const insufficient = estimate(shop, held(pool, api, 8), target);
    assert.equal(insufficient.available, false);
    assert.equal(insufficient.totalGold, Infinity);
    assert.equal(insufficient.method, "unavailable");
    assert.equal(insufficient.confidence95, null);
    assert.deepEqual(plain(insufficient.unavailableTargets), [{ api, cost: 4, available: 2, required: 3, reason: "insufficient-copies" }]);
    const exhausted = pool.map(unit => ({ ...unit, heldCopies: unit.cost === 4 ? 10 : 0 }));
    const unavailable = estimate(shop, exhausted, target, { signatureOnly: true });
    assert.equal(unavailable.available, false);
    assert.equal(unavailable.unavailableTargets[0].reason, "empty-tier");
    assert(unavailable.emptyTiers.includes(4));
    assert(!("totalGold" in unavailable));
    const impossibleOdds = estimate({ ...shop, costOdds: [0.15, 0.20, 0.62, 0, 0.03] }, pool, target);
    assert.equal(impossibleOdds.unavailableTargets[0].reason, "zero-odds");
    const simple = {
      costOdds: [0.5, 0, 0, 0.5, 0],
      championCounts: [1, 0, 0, 1, 0],
      bagCopies: [1, 1, 1, 3, 1],
      visibleSlots: [1],
      refreshCost: 2,
    };
    const simplePool = poolFor(simple).map(unit => ({ ...unit, heldCopies: unit.cost === 1 ? 1 : 0 }));
    const certain = estimate(simple, simplePool, targetsFor(simplePool, one(3)));
    assert.equal(certain.expectedShops, 3, "An empty tier is redrawn among nonempty tiers");
    assert.equal(certain.purchaseGold, 12);
    assert.equal(certain.totalGold, 18, "Every paid shop and target copy is charged");
  }

  function checkValidation() {
    const pool = poolFor(shop);
    const targets = targetsFor(pool, one(3));
    assert.throws(() => estimate(shop, held(pool, pool[0].api, 0.5), targets), /whole copies/);
    assert.throws(() => estimate(shop, held(pool, pool[0].api, -1), targets), /whole copies/);
    assert.throws(() => estimate(shop, held(pool, pool[0].api, 31), targets), /whole copies/);
    assert.throws(() => estimate(shop, pool.slice(1), targets), /every shop champion/);
    assert.throws(() => estimate(shop, [...pool, pool[0]], targets), /duplicate lobby pool/);
    assert.throws(() => estimate(shop, pool, [...targets, targets[0]]), /duplicate lobby target/);
    assert.throws(() => estimate(shop, pool, [{ ...targets[0], cost: 3 }]), /lobby target/);
    for (const copies of [0, 2, 4, 8, 10, 1.5, NaN, "9", true, null]) {
      assert.throws(() => estimate(shop, pool, [{ ...targets[0], copies }]), /one, three or nine/);
    }
    assert.throws(() => estimate({ ...shop, costOdds: new Array(5) }, pool, targets), /costOdds/);
    assert.throws(() => estimate(shop, pool, targets, { samples: 0 }), /sample count/);
    assert.throws(() => estimate(shop, pool, targets, { exactStateLimit: -1 }), /state limit/);
  }

  function exhaustiveReference(model, pool, targets) {
    const required = targets.map(target => target.copies);
    const strides = [1];
    for (let index = 1; index < targets.length; ++index) strides.push(strides[index - 1] * (required[index - 1] + 1));
    const total = required.reduce((product, copies) => product * (copies + 1), 1);
    const terminal = total - 1;
    const indexes = new Map(targets.map((target, index) => [target.api, index]));
    const codeFor = owned => owned.reduce((code, copies, index) => code + copies * strides[index], 0);
    const ownedFor = code => required.map((copies, index) => Math.floor(code / strides[index]) % (copies + 1));
    function transition(code, slots) {
      const owned = ownedFor(code);
      const offers = pool.map(
        unit => model.bagCopies[unit.cost - 1] - unit.heldCopies - (indexes.has(unit.api) ? owned[indexes.get(unit.api)] : 0)
      );
      const probabilities = new Map();
      function draw(remaining, chance) {
        const after = codeFor(owned);
        if (!remaining || after === terminal) {
          probabilities.set(after, (probabilities.get(after) || 0) + chance);
          return;
        }
        const totals = [0, 0, 0, 0, 0];
        pool.forEach((unit, index) => { totals[unit.cost - 1] += offers[index]; });
        const availableOdds = model.costOdds.reduce((sum, probability, cost) => sum + (totals[cost] ? probability : 0), 0);
        for (let index = 0; index < pool.length; ++index) {
          const unit = pool[index];
          const cost = unit.cost - 1;
          if (!offers[index] || !model.costOdds[cost]) continue;
          const probability = ((model.costOdds[cost] / availableOdds) * offers[index]) / totals[cost];
          const target = indexes.get(unit.api);
          const buy = target !== undefined && owned[target] < required[target];
          --offers[index];
          if (buy) ++owned[target];
          draw(remaining - 1, chance * probability);
          ++offers[index];
          if (buy) --owned[target];
        }
      }
      draw(slots, 1);
      near([...probabilities.values()].reduce((sum, chance) => sum + chance, 0), 1, "Exhaustive probabilities conserve mass");
      return probabilities;
    }
    const phases = model.visibleSlots.length;
    const rows = terminal * phases;
    const matrix = Array.from({ length: rows }, () => new Float64Array(rows + 1));
    for (let code = 0; code < terminal; ++code) {
      for (let phase = 0; phase < phases; ++phase) {
        const row = code * phases + phase;
        matrix[row][row] = 1;
        matrix[row][rows] = 1;
        for (const [next, chance] of transition(code, model.visibleSlots[phase])) {
          if (next !== terminal) matrix[row][next * phases + ((phase + 1) % phases)] -= chance;
        }
      }
    }
    for (let column = 0; column < rows; ++column) {
      let pivot = column;
      for (let row = column + 1; row < rows; ++row) {
        if (Math.abs(matrix[row][column]) > Math.abs(matrix[pivot][column])) pivot = row;
      }
      assert(Math.abs(matrix[pivot][column]) > 1e-14);
      [matrix[column], matrix[pivot]] = [matrix[pivot], matrix[column]];
      const divisor = matrix[column][column];
      for (let entry = column; entry <= rows; ++entry) matrix[column][entry] /= divisor;
      for (let row = 0; row < rows; ++row) {
        if (row !== column && matrix[row][column]) {
          const factor = matrix[row][column];
          for (let entry = column; entry <= rows; ++entry) matrix[row][entry] -= factor * matrix[column][entry];
        }
      }
    }
    return Array.from({ length: phases }, (_, phase) => matrix[phase][rows]);
  }

  function checkTinyExactPoolsAndMonteCarlo() {
    const models = [
      {
        model: { costOdds: [1, 0, 0, 0, 0], championCounts: [3, 0, 0, 0, 0], bagCopies: [2, 1, 1, 1, 1], visibleSlots: [2, 3], refreshCost: 2 },
        holdings: [1, 0, 1],
        counts: [2, 0, 0, 0, 0],
      },
      {
        model: { costOdds: [0, 0, 0, 1, 0], championCounts: [0, 0, 0, 3, 0], bagCopies: [1, 1, 1, 4, 1], visibleSlots: [2, 3], refreshCost: 2 },
        holdings: [1, 1, 0],
        counts: [0, 0, 0, 2, 0],
      },
      {
        model: { costOdds: [0.35, 0, 0, 0.65, 0], championCounts: [2, 0, 0, 3, 0], bagCopies: [3, 1, 1, 4, 1], visibleSlots: [2, 3], refreshCost: 2 },
        holdings: [1, 2, 1, 0, 2],
        counts: [1, 0, 0, 2, 0],
      },
    ];
    for (const { model, holdings, counts } of models) {
      const pool = poolFor(model).map((unit, index) => ({ ...unit, heldCopies: holdings[index] }));
      const targets = targetsFor(pool, counts);
      const phases = exhaustiveReference(model, pool, targets);
      const result = estimate(model, pool, targets);
      near(
        result.expectedShops,
        phases.reduce((sum, value) => sum + value / phases.length, 0),
        "Contested grouped DP agrees with independent identity Markov chains"
      );
      phases.forEach((value, phase) => near(result.diagnostics.shopsByInitialPhase[phase], value, `Independent initial phase ${phase}`));
      const sampled = estimate(model, pool, targets, { exactStateLimit: 0, samples: 16384 });
      sampledNear(sampled, result, "Full shop simulation agrees with the exact tiny pool");
    }
    const oneSlot = { costOdds: [0, 0, 0, 1, 0], championCounts: [0, 0, 0, 3, 0], bagCopies: [1, 1, 1, 4, 1], visibleSlots: [1], refreshCost: 2 };
    const pool = poolFor(oneSlot).map((unit, index) => ({ ...unit, heldCopies: index < 2 ? 1 : 0 }));
    const target = targetsFor(pool, one(3));
    const result = estimate(oneSlot, pool, target);
    near(result.expectedShops, 10 / 3 + 9 / 2 + 8, "Each bought copy depletes both the target and its entire tier");
  }

  function checkLargeAndRareMonteCarlo() {
    let pool = poolFor(shop);
    const targets = targetsFor(pool, [0, 0, 5, 3, 0]);
    for (let index = 0; index < targets.length; ++index) pool = held(pool, targets[index].api, index % 5);
    const exact = estimate(shop, pool, targets, { exactStateLimit: 4096 });
    const sampled = estimate(shop, pool, targets);
    assert(sampled.diagnostics.exactStates > 560, "A genuinely large availability signature selects MC");
    sampledNear(sampled, exact, "Large contested roster MC agrees with exact DP");
    assert.deepEqual(
      plain(sampled),
      plain(estimate(shop, pool.slice().reverse(), targets.slice().reverse())),
      "MC seed, ordering and output depend on the availability signature"
    );
    assert.equal(sampled.samples, 2048);
    assert(sampled.diagnostics.samplingIntervalOnly);
    assert.equal(sampled.purchaseGold, targets.reduce((sum, target) => sum + target.cost * target.copies, 0));

    const rareShop = { ...shop, costOdds: [1e-8, 1 - 1e-8, 0, 0, 0] };
    const rarePool = held(poolFor(rareShop), "unit-1-0", 29);
    const rareTargets = targetsFor(rarePool, one(0));
    const rareExact = estimate(rareShop, rarePool, rareTargets);
    const rareMC = estimate(rareShop, rarePool, rareTargets, { exactStateLimit: 0, samples: 8192 });
    sampledNear(rareMC, rareExact, "Exact empty-shop skipping preserves arbitrarily long rolldowns");
    assert(
      rareMC.expectedShops > 1e9 && rareMC.diagnostics.skippedEmptyShops > 1e12,
      "Rare trials retain the long tail instead of applying a maximum-shop cutoff"
    );
    assert(rareMC.diagnostics.rareStates > 0);
    const realisticPool = poolFor(shop);
    const four = targetsFor(realisticPool, one(3));
    const forced = estimate(shop, realisticPool, four, { exactStateLimit: 0, samples: 8192 });
    sampledNear(forced, estimate(shop, realisticPool, four), "Conditioned finite-pool shops handle multiple copies");
    assert(forced.diagnostics.rareStates > 0 && forced.diagnostics.directShops > 0, "Both ordinary shops and exact rare-state skipping are exercised");
  }

  function oneSlotReference(totalCopies, capacities, quotas, removeCompleted = false) {
    const memo = new Map();
    function expected(owned) {
      if (owned.every((copies, index) => copies === quotas[index])) return 0;
      const key = owned.join(",");
      if (memo.has(key)) return memo.get(key);
      const removed =
        owned.reduce((sum, copies) => sum + copies, 0) +
        (removeCompleted
          ? owned.reduce((sum, copies, index) => sum + (copies === quotas[index] ? capacities[index] - quotas[index] : 0), 0)
          : 0);
      const denominator = totalCopies - removed;
      let progress = 0;
      let numerator = 1;
      for (let index = 0; index < owned.length; ++index) {
        if (owned[index] < quotas[index]) {
          const probability = (capacities[index] - owned[index]) / denominator;
          const next = owned.slice();
          ++next[index];
          progress += probability;
          numerator += probability * expected(next);
        }
      }
      const result = numerator / progress;
      memo.set(key, result);
      return result;
    }
    return expected(quotas.map(() => 0));
  }

  function checkNineCopyExactArithmeticAndSignatures() {
    for (const cost of [2, 3]) {
      const model = { ...shop, costOdds: one(cost - 1), visibleSlots: [1] };
      const pool = poolFor(model);
      const unit = pool.find(entry => entry.cost === cost);
      const target = [{ api: unit.api, cost, copies: 9 }];
      const bag = model.bagCopies[cost - 1];
      const total = model.championCounts[cost - 1] * bag;
      const hand = Array.from({ length: 9 }, (_, owned) => (total - owned) / (bag - owned)).reduce((sum, waiting) => sum + waiting, 0);
      const result = estimate(model, pool, target);
      assert.equal(result.method, "exact");
      near(result.expectedShops, hand, `Nine separately depleted single-target waiting times at cost ${cost}`);
      assert.equal(result.purchaseGold, 9 * cost);
      assert.equal(result.diagnostics.exactStates, 10);
      const contested = estimate(model, held(pool, unit.api, 2), target);
      near(
        contested.expectedShops,
        Array.from({ length: 9 }, (_, owned) => (total - 2 - owned) / (bag - 2 - owned)).reduce((sum, waiting) => sum + waiting, 0),
        "Opponent holdings and bought nine-copy targets deplete the same tier"
      );
      const two = pool
        .filter(entry => entry.cost === cost)
        .slice(0, 2)
        .map(entry => ({ api: entry.api, cost, copies: 9 }));
      const together = estimate(model, pool, two);
      near(together.expectedShops, oneSlotReference(total, [bag, bag], [9, 9]), "Two nine-copy targets agree with the independent identity recurrence");
      assert.equal(together.diagnostics.exactStates, 55, "Two nine-copy targets use choose(11,9), not the three-copy formula");
      assert.equal(together.purchaseGold, 18 * cost);
      const signatures = [1, 3, 9].map(copies => estimate(model, pool, [{ ...target[0], copies }], { signatureOnly: true }).signature);
      assert.equal(new Set(signatures).size, 3, "Copy quota is part of the cache signature");
      const shortage = estimate(model, held(pool, unit.api, bag - 8), target);
      assert.equal(shortage.available, false);
      assert.equal(shortage.totalGold, Infinity);
      assert.equal(shortage.method, "unavailable");
      assert.deepEqual(plain(shortage.unavailableTargets), [{ api: unit.api, cost, available: 8, required: 9, reason: "insufficient-copies" }]);
    }
    const model = { costOdds: [0, 1, 0, 0, 0], championCounts: [0, 3, 0, 0, 0], bagCopies: [1, 12, 1, 1, 1], visibleSlots: [1], refreshCost: 2 };
    const pool = poolFor(model);
    const targets = pool.slice(0, 2).map((unit, index) => ({ api: unit.api, cost: 2, copies: index ? 9 : 1 }));
    const result = estimate(model, pool, targets);
    near(result.expectedShops, oneSlotReference(36, [12, 12], [1, 9]), "Completed-target leftover copies remain in the pool during another three-star collection");
    assert(result.expectedShops > oneSlotReference(36, [12, 12], [1, 9], true), "Finishing a target must not remove its remaining unbought bag copies");
    const normalPool = poolFor(shop);
    const four = normalPool.find(unit => unit.cost === 4);
    const singleFour = estimate(shop, normalPool, [{ api: four.api, cost: 4, copies: 1 }]);
    assert.equal(singleFour.purchaseGold, 4, "A lower-tier profile can request one copy of its four-cost support");
    const legacy = estimate(shop, normalPool, [{ api: four.api, cost: 4 }]);
    assert.equal(legacy.purchaseGold, 12, "Omitted quota preserves the old four-cost default");
  }

  function checkNineCopyMonteCarloAndLargeCollections() {
    const pool = poolFor(shop);
    for (const cost of [2, 3]) {
      const targets = pool
        .filter(unit => unit.cost === cost)
        .slice(0, 2)
        .map(unit => ({ api: unit.api, cost, copies: 9 }));
      const exact = estimate(shop, pool, targets);
      const sampled = estimate(shop, pool, targets, { exactStateLimit: 0, samples: 8192 });
      sampledNear(sampled, exact, `Nine-copy full-shop sampler agrees with exact two-target DP at cost ${cost}`);
      for (const count of [4, 8]) {
        const many = pool
          .filter(unit => unit.cost === cost)
          .slice(0, count)
          .map(unit => ({ api: unit.api, cost, copies: 9 }));
        const result = estimate(shop, pool, many);
        assert.equal(result.method, "monte-carlo");
        assert.equal(result.samples, 2048);
        assert.equal(result.purchaseGold, count * 9 * cost);
        assert.equal(result.diagnostics.exactStates, count === 4 ? 715 : 24310);
        assert(Number.isFinite(result.totalGold) && result.totalGold > result.purchaseGold);
        assert(result.standardErrorGold > 0 && Number.isFinite(result.standardErrorGold));
        near(result.confidence95[1] - result.totalGold, 1.96 * result.standardErrorGold, "Large nine-copy collections report an interval for the sampled mean");
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
  ++passedSuites;
}

// ============================================================================
// 4. Two-Item Core Component Diversity UI (test-tft-item-ui.cjs)
// ============================================================================

function runItemUiSuite() {
  const { context } = loadDashboard();
  const recipe = (api, components) => ({ api, components: components.map(c => ({ api: c, name: c })) });
  context.tstate.meta = {
    items: [
      recipe("Blue", ["Tear", "Tear"]),
      recipe("Cap", ["Rod", "Rod"]),
      recipe("Shojin", ["Sword", "Tear"]),
      recipe("Nashor", ["Bow", "Belt"]),
      recipe("JG", ["Rod", "Glove"]),
      recipe("ShojinCopy", ["Sword", "Tear"]),
    ],
  };
  context.tcore.variedComponents = true;

  const use = context.tftComponentUse;
  const order = context.tftCoreCompletionOrder;
  const completion = (third, lossPct, status = "comparable") => ({
    itemApis: ["Blue", "Cap", third],
    performance: { status, lossPct },
  });
  const best = completion("Blue", 0);
  const duplicate = completion("Cap", 1);
  const shojin = completion("Shojin", 2);
  const sameRecipe = completion("ShojinCopy", 2.1);
  const nashor = completion("Nashor", 4.9);
  const outside = completion("JG", 5.00001);
  const rows = [best, duplicate, shojin, sameRecipe, nashor, outside];
  const core = { completions: rows };
  const snapshot = JSON.stringify(core);
  const result = order(core, 5);
  assert.equal(result[0], best, "Always show the actual best completion first");
  assert.equal(result[1], nashor, "A balanced recipe can precede a stronger repeated-component alternative within tolerance");
  assert.equal(result[2], shojin, "Preview includes another distinct component footprint");
  assert.equal(result.indexOf(outside), 5, "Do not promote a component-diverse build outside tolerance");
  assert.equal(result.length, rows.length, "Expansion retains every completion");
  assert.equal(new Set(result).size, rows.length, "No preview/expansion duplicates");
  assert.equal(JSON.stringify(core), snapshot, "Presentation leaves scores and original rankings unchanged");
  assert.equal(JSON.stringify(order(core, 5, false)), JSON.stringify(rows), "Disabling the preference restores performance order");
  assert.equal(use(["Blue", "Blue", "Blue"]).components[0].count, 6, "Repeated items and duplicate ingredients both count");
  assert.equal(use(["Blue", "Cap", "Nashor"]).maxCopies, 2);
  assert.equal(use(["Blue", "Cap", "Shojin"]).maxCopies, 3);
  assert.equal(use(["Blue", "Cap", "Shojin"]).signature, use(["Shojin", "Cap", "Blue"]).signature, "Component identity is independent of item ordering");
  assert.equal(use(["missing"]), null, "Unknown recipes do not invent a component preference");
  assert.equal(order({ completions: [] }, 5).length, 0);
  assert.equal(order({ completions: [best] }, 5)[0], best);
  const edge = completion("Nashor", 5);
  assert.equal(order({ completions: [best, duplicate, shojin, edge] }, 5)[1], edge, "The 5% boundary is inclusive");
  const censored = completion("Nashor", null, "shortSurvival");
  assert.equal(order({ completions: [best, duplicate, shojin, censored] }, 5)[1], shojin, "Unknown survival loss cannot qualify as a near-optimal alternative");
  const missing = { itemApis: ["missing"], performance: { status: "comparable", lossPct: 0 } };
  assert.equal(order({ completions: [missing, best] }, 5)[0], missing, "Older metadata preserves the existing order");

  ++passedSuites;
}

// ============================================================================
// 5. Finite Damage Leaderboard, Formation Preview, and Pressure (test-tft-damage-ui.cjs)
// ============================================================================

async function runDamageUiSuite() {
  function payload() {
    const row = (unit, rank, killTime, objective) => ({
      unit,
      unitName: unit[0].toUpperCase() + unit.slice(1),
      unitApi: "TFT18_" + unit,
      star: 2,
      cost: 4,
      scenario: "s2-clump-low",
      rank,
      objective,
      items: ["One", "Two", "Three"],
      itemApis: ["one", "two", "three"],
      performance: {
        killTime,
        total: killTime === null ? 7000 : 9000,
        dps: killTime === null ? 350 : 9000 / killTime,
      },
    });
    return {
      revision: "finite-revision",
      selection: { key: "s2-clump-low-mixed" },
      complete: true,
      expectedCount: 4,
      readyCount: 4,
      damage: [
        Object.assign(row("alpha", 1, 8, "fighter"), { form: "AD", role: "Attack Assassin", range: 1, pressure: true }),
        row("beta", 2, 10, "carry"),
        row("gamma", 3, null, "carry"),
      ],
      tanks: [
        {
          unit: "tank",
          unitName: "Tank",
          rank: 1,
          star: 2,
          cost: 3,
          objective: "tank",
          scenario: "s2-clump-low",
          items: ["Armor", "Health", "Shield"],
          itemApis: ["armor", "health", "shield"],
          performance: { aliveTime: 60, survivalCapped: true, stressAliveTime: 37.1234, stressCapped: false },
        },
      ],
    };
  }

  function pinnedCurvePayload() {
    const data = payload();
    data.damageEvaluationModel = "unpressured-damage-curves-v1";
    data.damage = data.damage
      .map((row, index) => ({
        ...row,
        rank: 3 - index,
        objective: "carry",
        form: "AP",
        role: "Magic Marksman",
        range: 5,
        pressure: false,
        items: ["Potential one", "Potential two", "Potential three"],
        itemApis: ["p1", "p2", "p3"],
        performance: { time: 20, total: 20000 * (index + 1), dps: 1000 * (index + 1) },
        damageCurve: { evaluationModel: data.damageEvaluationModel },
        finiteDiagnostic: {
          items: row.items,
          itemApis: row.itemApis,
          performance: row.performance,
          objective: row.objective,
          form: row.form,
          role: row.role,
          range: row.range,
          pressure: row.pressure,
        },
      }))
      .reverse();
    return data;
  }

  function harness(data = payload()) {
    const base = loadDashboard({ initialHash: "#tab=tft&tftview=leaderboard" });
    const { context, document } = base;
    context.META = { tiers: ["test"], defaultTier: "test", platforms: [] };
    context.state.view = "tft";
    context.state.tier = "test";
    const champion = context.tstate;
    const state = context.tboard;
    champion.mode = "leaderboard";
    champion.meta = {
      revision: data.revision,
      stars: [1, 2, 3],
      units: [...data.damage, ...data.tanks].map(row => ({
        slug: row.unit,
        name: row.unitName,
        cost: row.cost,
        stars: [1, 2],
        traits: ["Shared trait"],
        icon: null,
      })),
      geometries: { spread: "Spread", clump: "Clumped" },
      dummy: {
        count: 6,
        tanks: 4,
        others: 2,
        slots: Array.from({ length: 6 }, (_, i) => ({
          kind: i < 4 ? "tank" : "non-tank",
          line: i < 4 ? "frontline" : "backline",
          nearby: i < 4,
        })),
      },
      traitContexts: { bare: "No traits", low: "First trait breakpoints", high: "Highest trait breakpoints" },
      tankThreats: [{ key: "mixed", label: "Mixed" }],
    };
    Object.assign(state, {
      mode: "damage",
      star: data.selection.key.split("-")[0],
      geo: data.selection.key.split("-")[1],
      traits: data.selection.key.split("-")[2],
      threat: "mixed",
      role: "all",
      cost: "all",
      search: "",
      data,
      key: data.selection.key,
      req: 0,
      loading: false,
      error: null,
    });
    base.element("view-tft").dataset.mode = "leaderboard";
    const element = id => {
      if (id === "tbody") return base.query("#tft-board-table tbody");
      if (id === "thead") return base.query("#tft-board-table thead tr");
      if (id === "fight-preview") return base.query(".tft-fight-preview");
      return base.element(id);
    };
    const h = {
      base,
      context,
      state,
      champion,
      element,
      queryAll: sel => base.queryAll(sel),
      navigations: [],
      requests: [],
      response: null,
      get focused() { return document.activeElement; },
    };
    context.loadTftScenario = () => {
      h.navigations.push({ unit: champion.unit, star: champion.star, geo: champion.geo, traits: champion.traits });
    };
    context.loadTftMeta = async () => true;
    context.openTft = async () => {};
    base.setResponder(async url => {
      h.requests.push(url);
      return h.response ? h.response() : structuredClone(data);
    });
    context.renderTftLeaderboardControls();
    return h;
  }

  function checkFiniteLeaderboard() {
    const h = harness();
    const c = h.context;
    assert(c.tftFiniteLeaderboardValid(h.state.data));
    assert.deepEqual(
      h.element("tbody").children.map(row => [row.dataset.unit, row.dataset.rank]),
      [["alpha", 1], ["beta", 2], ["gamma", 3]]
    );
    assert(h.element("thead").textContent.includes("Clear time") && h.element("thead").textContent.includes("DPS"));
    assert(h.element("tbody").textContent.includes("Did not clear"));
    assert(h.element("tbody").textContent.includes("AD form \u00b7 Attack Assassin \u00b7 1 range"));
    assert.equal(h.element("tft-board-movement-note").hidden, true, "Old payloads have no new movement assumption");
    assert(!h.element("tbody").textContent.includes("s moving"));
    assert(!INDEX_HTML.includes('id="tft-board-geo-seg"'), "The leaderboard uses only clumped enemies");
    assert(INDEX_HTML.includes('id="tft-board-formation"'), "The fixed enemy formation remains visible");
    assert.equal(h.element("tft-board-formation").querySelectorAll("button").length, 0, "The fixed formation is a read-only description");
    assert.equal(h.element("tft-board-formation").textContent, "Clumped \u00b7 4 frontliners + 2 backliners (6 enemies)");
    assert(!INDEX_HTML.includes('id="tft-damage-time"') && !INDEX_HTML.includes('id="tft-damage-comparison"'), "The slider and curve comparison are removed");
    h.state.role = "fighter";
    c.renderTftLeaderboard();
    assert.deepEqual(h.element("tbody").children.map(row => row.dataset.unit), ["alpha"]);
    h.state.role = "all";
    h.state.search = "beta";
    c.renderTftLeaderboard();
    assert.deepEqual(h.element("tbody").children.map(row => row.dataset.rank), [2], "Filters retain the full leaderboard rank");
    h.state.search = "";
    h.state.mode = "tanks";
    c.renderTftLeaderboard();
    assert(h.element("tbody").textContent.includes("60s+") && h.element("tbody").textContent.includes("37.1s"));
    assert(h.queryAll("#tft-board-table tbody td").some(td => td.title === "Unrounded time: 37.1234s"));
    assert(h.element("thead").textContent.includes("2\u00d7 pressure"));
    assert.equal(h.element("tft-board-role-seg").hidden, true);
  }

  function checkBisGaps() {
    const data = payload();
    const runnerUp = perf => ({
      items: ["Runner one", "Runner two", "Runner three"],
      itemApis: ["runner-one", "runner-two", "runner-three"],
      performance: perf,
    });
    data.damage[0].secondBest = runnerUp({ metric: "killTime", status: "comparable", lossPct: 10.1543125, value: 8.812345, killTime: 8.812345, total: 9000 });
    data.damage[1].secondBest = runnerUp({ metric: "killTime", status: "comparable", lossPct: 0, value: 10, killTime: 10, total: 9000 });
    data.damage[2].secondBest = runnerUp({ metric: "damage", status: "comparable", lossPct: 5, value: 6650, killTime: null, total: 6650 });
    const before = JSON.stringify(data);
    const h = harness(data);
    const c = h.context;
    const cells = () => h.element("tbody").children.map(row => row.children[3]);
    const value = cell => cell.querySelector("strong").textContent;
    const detail = cell => cell.querySelector("small").textContent;
    assert.equal(h.element("thead").children[3].textContent, "2nd BIS gap", "The new gap sits beside optimal items");
    assert(h.element("thead").children[3].title.includes("second-best distinct three-item build"));
    let [clear, tied, damage] = cells();
    assert.deepEqual([value(clear), value(tied), value(damage)], ["10.2%", "0.0%", "5.0%"]);
    assert.deepEqual([detail(clear), detail(damage)], ["slower clear", "less damage"]);
    assert.equal(clear.dataset.label, "2nd BIS gap", "The mobile table retains the gap label");
    assert(clear.title.includes("Runner one \u00b7 Runner two \u00b7 Runner three"));
    assert(
      clear.title.includes("extra clear time") &&
        clear.title.includes("BIS: cleared in 8s") &&
        clear.title.includes("Second best: cleared in 8.812345s"),
      "Hover exposes the clear-time metric and unrounded results"
    );
    assert(damage.title.includes("Neither build clears") && damage.title.includes("7000 damage") && damage.title.includes("6650 damage"));
    assert(!damage.title.includes("6650s"), "Damage uses damage units rather than time");
    assert(clear.attributes["aria-label"].includes(clear.title), "The same explanation is available to assistive technology");
    assert.deepEqual(h.element("tbody").children.map(row => row.dataset.rank), [1, 2, 3], "Build gaps preserve champion ranks");
    assert.equal(JSON.stringify(data), before, "Gap rendering preserves the saved result");

    data.damage[0].secondBest.performance.lossPct = 0.00001;
    c.renderTftLeaderboard();
    assert.equal(value(cells()[0]), "<0.1%", "A positive gap never rounds down to a tie");
    data.damage[0].secondBest.performance.lossPct = 0.1;
    c.renderTftLeaderboard();
    assert.equal(value(cells()[0]), "0.1%");
    data.damage[0].secondBest.performance = { metric: "killTime", status: "noClear", lossPct: null, value: null, killTime: null, total: 7000 };
    c.renderTftLeaderboard();
    clear = cells()[0];
    assert.equal(value(clear), "\u2014");
    assert.equal(detail(clear), "Does not clear");
    assert(clear.title.includes("does not clear") && clear.title.includes("percentage is unavailable") && clear.title.includes("7000 damage"));

    h.state.mode = "tanks";
    const tank = data.tanks[0];
    tank.secondBest = runnerUp({
      metric: "stressAliveTime",
      status: "comparable",
      lossPct: 10,
      value: 33.41106,
      aliveTime: 60,
      survivalCapped: true,
      stressAliveTime: 33.41106,
      stressCapped: false,
    });
    c.renderTftLeaderboard();
    let held = cells()[0];
    assert.equal(value(held), "10.0%");
    assert.equal(detail(held), "less held at 2\u00d7");
    assert(
      held.title.includes("at least 60s (fight limit)") &&
        held.title.includes("37.1234s at 2\u00d7 pressure") &&
        held.title.includes("33.41106s at 2\u00d7 pressure")
    );
    Object.assign(tank.performance, { aliveTime: 50.12345, survivalCapped: false, stressAliveTime: null, stressCapped: false });
    tank.secondBest.performance = {
      metric: "aliveTime",
      status: "comparable",
      lossPct: 20,
      value: 40.09876,
      aliveTime: 40.09876,
      survivalCapped: false,
      stressAliveTime: null,
      stressCapped: false,
    };
    c.renderTftLeaderboard();
    held = cells()[0];
    assert.equal(value(held), "20.0%");
    assert.equal(detail(held), "less time held");
    assert(held.title.includes("50.12345s") && held.title.includes("40.09876s"));
    assert(!held.title.includes("40.09876 damage"));

    Object.assign(tank.performance, { aliveTime: 60, survivalCapped: true, stressAliveTime: 60, stressCapped: true });
    for (const [metric, status, expected, perf] of [
      ["stressAliveTime", "shortSurvival", "Below regular limit", { aliveTime: 50, survivalCapped: false }],
      ["capped", "belowCap", "Below best limit", { aliveTime: 60, survivalCapped: true, stressAliveTime: 50, stressCapped: false }],
      ["aliveTime", "unavailable", "Unavailable", {}],
    ]) {
      tank.secondBest.performance = { metric, status, lossPct: null, value: null, ...perf };
      c.renderTftLeaderboard();
      held = cells()[0];
      assert.equal(value(held), "\u2014");
      assert.equal(detail(held), expected);
      assert(held.title.includes("unavailable"), `${status} explains why the percentage is missing`);
      assert(!/undefined|NaN/.test(held.textContent + held.title));
    }
    tank.secondBest.performance = {
      metric: "capped",
      status: "comparable",
      lossPct: 0,
      value: 60,
      aliveTime: 60,
      survivalCapped: true,
      stressAliveTime: 60,
      stressCapped: true,
    };
    c.renderTftLeaderboard();
    held = cells()[0];
    assert.equal(value(held), "0.0%");
    assert.equal(detail(held), "same limits");
    assert(held.title.includes("tied in this benchmark") && held.title.includes("beyond those limits is unknown"));

    const legacy = harness();
    for (const row of legacy.element("tbody").children) {
      const gap = row.children[3];
      assert.equal(value(gap), "\u2014");
      assert.equal(detail(gap), "Unavailable");
      assert(gap.title.includes("unavailable for this saved result"));
    }
  }

  async function checkTraitContexts() {
    const h = harness();
    const c = h.context;
    assert.deepEqual(h.element("tft-board-trait-seg").children.map(button => button.dataset.key), ["low", "high"]);
    assert(!h.element("tft-board-trait-seg").textContent.includes("None"));

    const previous = { ...payload(), selection: { key: "s2-clump-bare-mixed" } };
    h.state.traits = "bare";
    h.state.data = previous;
    c.renderTftLeaderboardControls();
    assert.equal(h.state.traits, "low", "Older no-trait selections open first breakpoints");
    assert.equal(h.state.data, null, "No-trait results are cleared before displaying the new context");
    assert.equal(h.element("tbody").children.length, 0);
    await c.loadTftLeaderboard();
    assert.deepEqual(h.requests, ["api/tft/leaderboard/s2-clump-low-mixed.json"]);
    assert.equal(h.state.data.selection.key, "s2-clump-low-mixed");
    assert.equal(previous.selection.key, "s2-clump-bare-mixed", "Saved payloads remain unchanged");

    h.response = () => ({ ...payload(), selection: { key: "s2-clump-high-mixed" } });
    const loaded = new Promise(resolve => { c.scheduleTftStatus = resolve; });
    await h.element("tft-board-trait-seg").children.find(button => button.dataset.key === "high").click();
    await loaded;
    assert.equal(h.state.traits, "high");
    assert.equal(h.requests.at(-1), "api/tft/leaderboard/s2-clump-high-mixed.json");
    assert.equal(h.state.data.selection.key, "s2-clump-high-mixed");

    for (const traits of ["bare", "low", "high"]) {
      const route = harness();
      route.base.location.hash = `#tab=tft&tftview=leaderboard&btr=${traits}`;
      route.context.restoreHash();
      assert.equal(route.state.traits, traits === "bare" ? "low" : traits);
    }
  }

  async function checkClumpedFormation() {
    const h = harness();
    const c = h.context;
    const previous = { ...payload(), selection: { key: "s2-spread-low-mixed" } };
    Object.assign(h.state, { geo: "spread", key: previous.selection.key, data: previous });
    assert.equal(c.tftLeaderboardKey(), "s2-clump-low-mixed", "Stale in-memory geometry cannot create a spread request");
    c.renderTftLeaderboardControls();
    assert.equal(h.state.geo, "clump", "An older separated formation selection opens clumped enemies");
    assert.equal(h.state.data, null, "Saved spread results are cleared before displaying the clumped context");
    assert.equal(h.element("tbody").children.length, 0);
    await c.loadTftLeaderboard();
    assert.deepEqual(h.requests, ["api/tft/leaderboard/s2-clump-low-mixed.json"]);
    assert.equal(h.state.data.selection.key, "s2-clump-low-mixed");
    assert.equal(previous.selection.key, "s2-spread-low-mixed", "Saved spread payloads remain unchanged");

    let resolve;
    h.response = () => new Promise(finish => { resolve = finish; });
    Object.assign(h.state, { geo: "spread", data: previous });
    const loading = c.loadTftLeaderboard({ preserve: true });
    assert.equal(h.state.geo, "clump", "Reloading normalizes a stale in-memory formation");
    assert.equal(h.state.data, null, "Preserve cannot retain a spread payload while loading clumped results");
    resolve(payload());
    await loading;

    h.champion.meta.dummy.slots = Array.from({ length: 5 }, (_, i) => ({ nearby: i < 3 }));
    c.renderTftLeaderboardControls();
    assert.equal(h.element("tft-board-formation").textContent, "Clumped \u00b7 3 frontliners + 2 backliners (5 enemies)", "Enemy counts come from the published slots");
    delete h.champion.meta.dummy;
    c.renderTftLeaderboardControls();
    assert.equal(h.element("tft-board-formation").textContent, "Clumped", "Missing formation metadata does not invent enemy counts");

    h.response = () => previous;
    await c.loadTftLeaderboard();
    assert.equal(h.state.data, null, "A spread response cannot be displayed as a clumped benchmark");
    assert(h.state.error, "A mismatched response exposes a load failure");
  }

  async function checkLeaderboardLayout() {
    const h = harness();
    const c = h.context;
    const wrap = h.element("tft-board-tablewrap");
    assert.equal(h.element("view-tft").dataset.mode, "leaderboard");
    assert.match(
      STYLES_CSS,
      /#view-tft\[data-mode="leaderboard"\]\s*\{\s*padding-bottom:\s*max\(560px,\s*100vh\)/,
      "Short leaderboards have room below them even on a small viewport"
    );
    wrap.getBoundingClientRect = () => ({
      height: wrap.hidden ? 0 : Math.max(parseFloat(wrap.style.minHeight) || 0, h.element("tbody").children.length > 1 ? 560 : 100),
    });
    const requests = [];
    h.response = () => new Promise((resolve, reject) => { requests.push({ resolve, reject }); });

    const loaded = new Promise(resolve => { c.scheduleTftStatus = resolve; });
    h.element("tft-board-star-seg").children.find(button => button.dataset.key === "s1").dispatch("click");
    assert.equal(h.state.data, null, "Changing stars clears the previous scenario");
    assert.equal(h.element("tbody").children.length, 0, "Old rows are removed while the new setting loads");
    assert.equal(wrap.hidden, false, "The table region stays on the page during loading");
    assert.equal(wrap.style.minHeight, "560px", "Loading reserves the previous table height");
    const single = payload();
    single.selection.key = "s1-clump-low-mixed";
    single.damage = [{ ...single.damage[0], star: 1, scenario: "s1-clump-low" }];
    requests[0].resolve(single);
    await loaded;
    assert.equal(h.element("tbody").children.length, 1);
    assert.equal(wrap.hidden, false);
    assert.equal(wrap.style.minHeight, "", "The loaded one-unit table uses its own height");
    assert.equal(h.element("view-tft").dataset.mode, "leaderboard", "Bottom padding remains for a single result");

    h.state.star = "s2";
    const superseded = c.loadTftLeaderboard();
    assert.equal(wrap.style.minHeight, "100px");
    h.state.traits = "high";
    const latest = c.loadTftLeaderboard();
    requests[1].resolve(payload());
    await superseded;
    assert.equal(h.state.loading, true);
    assert.equal(wrap.hidden, false);
    assert.equal(wrap.style.minHeight, "100px", "A superseded request cannot release the current loading space");
    requests[2].resolve({ ...payload(), selection: { key: "s2-clump-high-mixed" }, damage: [] });
    await latest;
    assert.equal(wrap.style.minHeight, "", "Empty results release the loading height");
    assert.equal(wrap.hidden, true);
    assert.equal(h.element("tft-board-empty").hidden, false);
    assert.equal(h.element("view-tft").dataset.mode, "leaderboard", "Empty results keep the page padding");

    const failed = c.loadTftLeaderboard();
    requests[3].reject(new Error("Fixture network failure"));
    await failed;
    assert(h.state.error);
    assert.equal(wrap.style.minHeight, "", "A failed request releases the loading height");
    assert.equal(wrap.hidden, true);
    assert.equal(h.element("tft-board-retry").disabled, false);
    await c.setTftMode("builds", { load: false });
    assert.equal(h.element("view-tft").dataset.mode, "builds", "Leaderboard padding stays scoped to that page");
  }

  function checkPinnedCompatibility() {
    const data = pinnedCurvePayload();
    const before = JSON.stringify(data);
    const h = harness(data);
    const c = h.context;
    assert(c.tftFiniteLeaderboardValid(data));
    const rows = c.tftLeaderboardDamageRows(data);
    assert.deepEqual(plain(rows.map(row => [row.unit, row.rank])), [["alpha", 1], ["beta", 2], ["gamma", 3]]);
    assert.equal(rows[0].form, "AD");
    assert.equal(rows[0].objective, "fighter");
    assert(h.element("tbody").textContent.includes("8.00s"));
    assert(!h.element("tbody").textContent.includes("Potential"));
    h.state.role = "fighter";
    c.renderTftLeaderboard();
    assert.deepEqual(h.element("tbody").children.map(row => row.dataset.unit), ["alpha"], "Pinned data uses the finite winner's actual form and role");
    assert.equal(JSON.stringify(data), before, "The compatibility projection preserves the pinned payload");
    const tied = pinnedCurvePayload();
    tied.damage.find(row => row.unit === "beta").finiteDiagnostic.performance.killTime = 8;
    assert.deepEqual(plain(c.tftLeaderboardDamageRows(tied).map(row => row.rank)), [1, 1, 3]);
    for (const mutate of [
      d => { delete d.damage[0].finiteDiagnostic; },
      d => { d.damage[0].finiteDiagnostic.performance.killTime = undefined; },
      d => { d.damage[0].finiteDiagnostic.performance.total = -1; },
      d => { d.damage[0].finiteDiagnostic.performance.dps = NaN; },
    ]) {
      const invalid = pinnedCurvePayload();
      mutate(invalid);
      assert.equal(c.tftFiniteLeaderboardValid(invalid), false, "Potential metrics cannot masquerade as finite results");
    }
    const potentialGap = pinnedCurvePayload();
    potentialGap.damage[0].secondBest = {
      items: ["Potential runner", "Two", "Three"],
      itemApis: ["p1", "two", "three"],
      performance: { metric: "damage", status: "comparable", lossPct: 75, total: 15000 },
    };
    const pinned = harness(potentialGap);
    assert(
      pinned.element("tbody").children.every(row => row.children[3].querySelector("strong").textContent === "\u2014"),
      "A potential runner-up cannot masquerade as the saved finite build comparison"
    );
  }

  async function checkNavigationAndLoad() {
    const h = harness();
    const c = h.context;
    await h.element("tbody").children[0].querySelector(".tft-board-champion").click();
    assert.deepEqual(h.navigations.at(-1), { unit: "alpha", star: 2, geo: "clump", traits: "low" });
    assert.equal(h.champion.mode, "builds");
    await c.setTftMode("leaderboard", { load: false, focus: true });
    assert.equal(h.focused.dataset.unit, "alpha");
    h.base.location.hash = "";
    c.restoringHash = false;
    c.restoredHash = null;
    c.updateHash();
    const hash = Object.fromEntries(new URLSearchParams(h.base.location.hash.slice(1)));
    assert.equal(hash.board, "damage");
    assert.equal(hash.bgeo, "clump");
    assert(!("btime" in hash) && !("bcurves" in hash) && !("bdamage" in hash));
    h.response = () => {
      const wrong = pinnedCurvePayload();
      delete wrong.damage[0].finiteDiagnostic;
      return wrong;
    };
    await c.loadTftLeaderboard();
    assert(h.state.error && h.state.data === null);
    h.response = pinnedCurvePayload;
    await c.loadTftLeaderboard();
    assert.equal(h.state.error, null);
    assert.equal(h.element("tbody").children[0].dataset.unit, "alpha");
  }

  function checkFiniteEquippedPressure() {
    const h = harness();
    const c = h.context;
    const unit = h.champion.meta.units.find(entry => entry.slug === "alpha");
    Object.assign(unit, { name: "Nidalee", role: "Magic Marksman", objective: "carry", ability: "Pounce", duration: 20 });
    const common = {
      items: ["One", "Two", "Three"],
      rank: 1,
      ad: 200,
      ap: 100,
      attackSpeed: 1,
      crit: 25,
      killTime: 12,
      total: 3000,
      dps: 250,
      casts: 3,
      attacks: 12,
      breakdown: {},
      left: [0, 0, 0, 0, 0],
    };
    const ad = {
      ...common,
      form: "AD",
      objective: "fighter",
      pressure: true,
      role: "Attack Assassin",
      range: 1,
      hp: 2000,
      hpLeft: 400,
      armor: 50,
      mr: 50,
      aliveTime: 12,
      died: false,
      taken: 1600,
      absorbed: 2400,
      shielded: 0,
      healed: 0,
      denied: 0,
    };
    const ap = { ...common, form: "AP", objective: "carry", pressure: false, role: "Magic Marksman", range: 4 };
    h.champion.data = {
      unit: "alpha",
      objective: "carry",
      pressureByBuild: true,
      rows: [ad, ap],
      scenario: {
        geometry: "clump",
        duration: 30,
        dummy: {
          count: 5,
          star: 2,
          pressureDps: 100,
          slots: Array.from({ length: 5 }, (_, i) => ({
            hp: 1000,
            armor: 100,
            mr: 100,
            ad: 50,
            as: 1,
            ability: 100,
            nearby: i < 3,
            kind: i < 3 ? "tank" : "non-tank",
            manaStart: 0,
            manaMax: 100,
            manaPerAttack: 10,
          })),
        },
      },
    };
    c.tftTargetDebuffNote = () => "Finite target defenses.";
    const columns = c.tftColumns();
    assert(columns.some(column => column.th === "Alive") && columns.some(column => column.th === "HP"));
    assert.equal(columns.find(column => column.th === "HP").td(ad).textContent, "2,000");
    assert.equal(columns.find(column => column.th === "Alive").td(ap).textContent, "Protected");
    assert.equal(columns.find(column => column.th === "HP").td(ap).textContent, "\u2014");
    c.renderTftBreakdown(ad);
    assert(h.element("tft-pressure-note").textContent.includes("The 3 frontline dummies attack and cast"));
    assert(h.element("tft-pressure-note").textContent.includes("backline is a damage target only"));
    assert.equal(h.element("tft-pressure-details").hidden, false);
    assert(h.element("tft-selected-meta").textContent.includes("Attack Assassin \u00b7 AD form \u00b7 1 range"));
    assert(h.element("tbd-hint").textContent.includes("takes incoming damage"));
    c.renderTftBreakdown(ap);
    assert(h.element("tft-pressure-note").textContent.includes("do not attack this protected build"));
    assert.equal(h.element("tft-pressure-details").hidden, true);
    assert(h.element("tft-selected-meta").textContent.includes("Magic Marksman \u00b7 AP form \u00b7 4 range"));
    assert(!/undefined|NaN/.test(h.element("tbd-hint").textContent));
    assert.equal(unit.objective, "carry", "Per-build rendering preserves base identity used by navigation guards");
    return h;
  }

  function checkEnemyFormationPreviews() {
    const h = checkFiniteEquippedPressure();
    const c = h.context;
    const unit = h.champion.meta.units.find(entry => entry.slug === "alpha");
    const [fighter, carry] = h.champion.data.rows;
    unit.name = "Sivir";
    const legacy = h.champion.data.scenario.dummy;
    const catalog = {
      clump: [
        { lane: 0, row: 0 }, { lane: 1, row: 0 }, { lane: 2, row: 0 },
        { lane: 3, row: 0 }, { lane: 0, row: 2 }, { lane: 3, row: 2 },
      ],
      spread: [
        { lane: -3, row: 0 }, { lane: 0, row: 0 }, { lane: 3, row: 0 },
        { lane: 6, row: 0 }, { lane: -3, row: 2 }, { lane: 6, row: 2 },
      ],
    };
    h.champion.meta.formations = catalog;
    const dummy = (front, back, count = front + back) => ({
      count,
      star: 2,
      pressureDps: 100,
      slots: Array.from({ length: front + back }, (_, i) => ({
        hp: i === 0 ? 3000 : i < front ? 1800 : 1440,
        armor: 100,
        mr: 100,
        ad: 50,
        as: 1,
        ability: 100,
        manaStart: 0,
        manaMax: 100,
        manaPerAttack: 10,
        fixedDefenses: i === 0,
        nearby: i < front,
        line: i < front ? "frontline" : "backline",
        kind: i < front ? "tank" : "non-tank",
      })),
    });
    const diagram = () => h.element("tft-formation-diagram");
    const enemies = () => diagram().children.filter(node => node.tag === "circle" && node.attributes["data-target"]);
    const targets = nodes => nodes.map(node => Number(node.attributes["data-target"])).sort((a, b) => a - b);
    const render = (dm, build = carry, geometry = "clump") => {
      Object.assign(h.champion.data.scenario, { dummy: dm, geometry });
      c.renderTftBreakdown(build);
    };
    const assertTargets = (front, back) => {
      assert.deepEqual(
        targets(enemies()),
        Array.from({ length: front + back }, (_, i) => i + 1),
        "Every published slot appears exactly once with the simulation target number"
      );
      assert.equal(enemies().filter(node => node.attributes["data-line"] === "frontline").length, front);
      assert.equal(enemies().filter(node => node.attributes["data-line"] === "backline").length, back);
      assert.deepEqual(
        diagram()
          .children.filter(node => node.attributes.class === "tft-diagram-number" && /^\d+$/.test(node.textContent))
          .map(node => Number(node.textContent))
          .sort((a, b) => a - b),
        Array.from({ length: front + back }, (_, i) => i + 1),
        "Visible diagram numbering agrees with target stats"
      );
    };
    const assertNoUniversalSplash = () => {
      const notes = ["tft-diagram-desc", "tft-formation-caption", "tft-dummy-intro", "tft-targeting-note"]
        .map(id => (id.startsWith("tft-diagram") ? diagram().children.find(node => node.attributes.id === id)?.textContent : h.element(id).textContent))
        .join(" ");
      assert(
        !/outside nearby effects|only the frontline is within reach|backline is reached only|only whole-board and nearest-N/i.test(notes),
        "Formation prose does not turn a schematic row into a universal ability-radius rule"
      );
      assert.equal(diagram().children.filter(node => node.tag === "ellipse").length, 0, "A general preview does not mark the entire frontline as one ability's coverage");
      assert(!/undefined|NaN/.test(diagram().textContent + notes));
    };

    const six = dummy(4, 2, 5);
    const before = JSON.stringify({ six, catalog, unit, fighter, carry });
    render(six);
    assertTargets(4, 2);
    assertNoUniversalSplash();
    assert(h.element("tft-dummy-intro").textContent.includes("6 test targets"));
    assert(h.element("tft-targeting-note").textContent.includes("1 \u2192 2 \u2192 3 \u2192 4 \u2192 5 \u2192 6"));
    assert.equal(h.element("tft-dummy-stats").children.length, 6);
    assert(h.element("tft-dummy-stats").children[0].textContent.includes("Main tank"));
    assert.deepEqual(
      enemies().map(node => [Number(node.attributes["data-lane"]), Number(node.attributes["data-row"])]),
      catalog.clump.map(position => [position.lane, position.row]),
      "Diagram locations use published engine coordinates"
    );
    assert.equal(c.tftEnemyFormation(six, "clump").mainTanks.length, 1);
    render(six, fighter);
    assert(h.element("tft-pressure-note").textContent.includes("The 4 frontline dummies attack and cast"));
    assert.equal(h.element("tft-pressure-details").hidden, false);
    render(six, carry, "spread");
    assertTargets(4, 2);
    assertNoUniversalSplash();
    assert.deepEqual(
      enemies().map(node => [Number(node.attributes["data-lane"]), Number(node.attributes["data-row"])]),
      catalog.spread.map(position => [position.lane, position.row]),
      "Changing geometry reads its own published coordinates"
    );
    assert.equal(JSON.stringify({ six, catalog, unit, fighter, carry }), before, "Formation rendering preserves source metadata and builds");

    render(legacy);
    assertTargets(3, 2);
    assertNoUniversalSplash();
    assert.equal(c.tftEnemyFormation(legacy, "clump").positioned, false);
    assert(enemies().every(node => !("data-lane" in node.attributes) && !("data-row" in node.attributes)));
    assert(h.element("tft-dummy-intro").textContent.includes("5 test targets"));
    assert(h.element("tft-targeting-note").textContent.includes("1 \u2192 2 \u2192 3 \u2192 4 \u2192 5"));

    const seven = dummy(5, 2);
    const sevenBefore = JSON.stringify(seven);
    render(seven);
    assertTargets(5, 2);
    assertNoUniversalSplash();
    assert.equal(c.tftEnemyFormation(seven, "clump").positioned, false, "Unsupported counts retain a labeled schematic");
    assert(h.element("tft-dummy-intro").textContent.includes("7 test targets"));
    assert.equal(h.element("tft-dummy-stats").children.length, 7);
    assert.equal(JSON.stringify(seven), sevenBefore);

    const custom = dummy(2, 1);
    custom.slots = [custom.slots[2], custom.slots[0], custom.slots[1]];
    custom.slots.forEach((slot, i) => Object.assign(slot, { lane: [-2, 1, 4][i], row: [4, 0, 0][i] }));
    custom.slots[1].label = "An imported label";
    const customBefore = JSON.stringify(custom);
    render(custom);
    assertTargets(2, 1);
    assertNoUniversalSplash();
    const formation = c.tftEnemyFormation(custom, "clump");
    assert.equal(formation.positioned, true);
    assert.deepEqual(plain(formation.mainTanks.map(slot => [slot.index, slot.label])), [[1, "Main tank"]]);
    const ordered = [...enemies()].sort((a, b) => Number(a.attributes["data-target"]) - Number(b.attributes["data-target"]));
    assert.deepEqual(
      ordered.map(node => [Number(node.attributes["data-lane"]), Number(node.attributes["data-row"])]),
      custom.slots.map(slot => [slot.lane, slot.row])
    );
    assert.equal(ordered[0].attributes["data-line"], "backline");
    assert.equal(JSON.stringify(custom), customBefore, "Custom coordinates are read without mutation");
    custom.slots[0].row = NaN;
    render(custom);
    assert.equal(c.tftEnemyFormation(custom, "clump").positioned, false, "Partially invalid explicit coordinates do not silently switch to a different formation");
    assertTargets(2, 1);
    assert(enemies().every(node => !("data-lane" in node.attributes) && !("data-row" in node.attributes)));
    assert(!enemies().some(node => /NaN|undefined/.test(`${node.attributes.cx} ${node.attributes.cy}`)));

    const tankDummy = dummy(4, 2);
    tankDummy.threat = {
      label: "Mixed",
      attackers: 6,
      frontlineAttackers: 4,
      backlineAttackers: 2,
      dps: 1000,
      frontlineDps: 200,
      backlineDps: 800,
      physicalShare: 0.5,
      attackInterval: 1,
      castInterval: 6,
      backlineAttackShare: 0.8,
      backlineSpellPhysicalShare: 0.5,
      frontlineAttackInterval: 1,
      frontlineCastInterval: 6,
    };
    const tankBuild = { ...fighter, objective: "tank", pressure: true };
    const tankScenario = { geometry: "clump", duration: 60, threat: "mixed", dummy: tankDummy };
    c.tftTankDebuffNote = () => "Tank defenses.";
    c.renderTftFight(unit, tankScenario, tankBuild);
    assertTargets(4, 2);
    assertNoUniversalSplash();
    const incoming = () => diagram().children.filter(node => node.attributes["data-direction"] === "incoming");
    assert.deepEqual(targets(incoming()), [1, 2, 3, 4, 5, 6], "Each of the six attackers has an incoming arrow");
    assert.equal(incoming().filter(node => node.attributes["data-line"] === "frontline").length, 4);
    assert.equal(incoming().filter(node => node.attributes["data-line"] === "backline").length, 2);
    assert(h.element("tft-dummy-intro").textContent.includes("6 attackers"));
    unit.name = "Hecarim";
    c.renderTftFight(unit, tankScenario, tankBuild);
    assertTargets(4, 2);
    assert.deepEqual(
      targets(diagram().children.filter(node => node.tag === "ellipse")),
      [1, 2, 3],
      "Hecarim marks three nearest targets; adding a fourth frontliner does not expand his target limit"
    );
    assert.deepEqual(targets(incoming()), [1, 2, 3, 4, 5, 6], "The unmarked fourth frontliner and both carries still attack");
    c.renderTftFight(unit, { ...tankScenario, geometry: "spread" }, tankBuild);
    assert.deepEqual(
      targets(diagram().children.filter(node => node.tag === "ellipse")),
      [1, 2, 3],
      "Nearest-N targeting retains its cap when the frontline is separated"
    );
    c.renderTftFight(unit, { ...tankScenario, dummy: { slots: [] } }, tankBuild);
    assert.equal(h.element("fight-preview").hidden, true, "Missing targets hide the preview rather than inventing an enemy board");
  }

  function checkMovementLeaderboard() {
    const data = payload();
    Object.assign(data.damage[0], { meleeRepositionSeconds: 0.5 });
    Object.assign(data.damage[0].performance, { movementTime: 1.5, repositions: 3 });
    Object.assign(data.damage[1], { form: "AP", range: 5, meleeRepositionSeconds: 0.5 });
    Object.assign(data.damage[1].performance, { movementTime: 2, repositions: 4 });
    Object.assign(data.damage[2], { range: 2, meleeRepositionSeconds: 0.5 });
    Object.assign(data.damage[2].performance, { movementTime: 0.5, repositions: 1 });
    const before = JSON.stringify(data);
    const h = harness(data);
    const c = h.context;
    assert.equal(h.element("tft-board-movement-note").hidden, false);
    const note = h.element("tft-board-movement-note").textContent;
    assert(note.includes("1\u20132 attack range") && note.includes("0.5s delay at initial engagement"));
    assert(note.includes("current target dies") && note.includes("cooldowns overlap movement"));
    assert(note.includes("ongoing damage and incoming pressure continue") && note.includes("Hex paths are not simulated"));
    const rows = h.element("tbody").children;
    assert(rows[0].textContent.includes("1.50s moving \u00b7 3 moves"));
    assert(!rows[1].textContent.includes("s moving"));
    assert(rows[2].textContent.includes("0.50s moving \u00b7 1 move"));
    h.state.search = "beta";
    c.renderTftLeaderboard();
    assert.equal(h.element("tft-board-movement-note").hidden, true, "Ranged-only filtered results are not labeled penalized");
    h.state.search = "";
    h.state.mode = "tanks";
    c.renderTftLeaderboard();
    assert.equal(h.element("tft-board-movement-note").hidden, true);
    assert(!h.element("tbody").textContent.includes("s moving"));
    assert.equal(JSON.stringify(data), before);
  }

  function checkMovementDetails() {
    const h = checkFiniteEquippedPressure();
    const c = h.context;
    const [ad, ap] = h.champion.data.rows;
    const legacyCaption = h.element("tft-formation-caption").textContent;
    h.champion.data.scenario.dummy.meleeRepositionSeconds = 0.5;
    Object.assign(ad, { movementTime: 1.25, repositions: 3 });
    ap.range = 5;
    c.renderTftBreakdown(ad);
    assert(h.element("tft-formation-caption").textContent.includes("approximate 0.5s movement delay"));
    assert(h.element("tft-formation-caption").textContent.includes("Exact hex paths are not simulated"));
    const details = h.element("tft-targeting-note").textContent;
    assert(details.includes("initial engagement and after the current target dies"));
    assert(details.includes("cooldowns overlap movement") && details.includes("ongoing damage and incoming pressure continue"));
    assert(h.element("tbd-hint").textContent.includes("1.25s moving \u00b7 3 moves"));
    c.renderTftBreakdown(ap);
    assert(h.element("tft-formation-caption").textContent.includes("This 5-range build has no movement delay"));
    assert(!h.element("tft-targeting-note").textContent.includes("Movement takes"));
    assert(!h.element("tbd-hint").textContent.includes("s moving"));
    Object.assign(ap, { movementTime: 3, repositions: 6 });
    c.renderTftBreakdown(ap);
    assert(!h.element("tbd-hint").textContent.includes("s moving"), "Equipped range governs the affected form");
    Object.assign(ad, { range: 2, movementTime: 0, repositions: 0 });
    c.renderTftBreakdown(ad);
    assert(h.element("tbd-hint").textContent.includes("0.00s moving \u00b7 0 moves"));
    delete ad.repositions;
    c.renderTftBreakdown(ad);
    assert(h.element("tbd-hint").textContent.includes("0.00s moving"));
    assert(!/undefined|NaN/.test(h.element("tbd-hint").textContent));
    delete h.champion.data.scenario.dummy.meleeRepositionSeconds;
    c.renderTftBreakdown(ad);
    assert.equal(h.element("tft-formation-caption").textContent, legacyCaption);
    assert(!h.element("tbd-hint").textContent.includes("s moving"), "Old benchmark metadata keeps old wording");
    h.champion.data.objective = "tank";
    h.champion.data.scenario.dummy.meleeRepositionSeconds = 0.5;
    Object.assign(ad, { objective: "tank", physicalEhp: 3000, magicEhp: 3000 });
    c.tftTankDebuffNote = () => "Tank defenses.";
    c.renderTftBreakdown(ad);
    assert(!h.element("tbd-hint").textContent.includes("s moving"));
    assert(!h.element("tft-formation-caption").textContent.includes("movement delay"));
  }

  checkFiniteLeaderboard();
  checkBisGaps();
  checkPinnedCompatibility();
  await checkTraitContexts();
  await checkClumpedFormation();
  await checkLeaderboardLayout();
  await checkNavigationAndLoad();
  checkFiniteEquippedPressure();
  checkEnemyFormationPreviews();
  checkMovementLeaderboard();
  checkMovementDetails();

  ++passedSuites;
}

// ============================================================================
// 6. Early Comps Browser UI (test-tft-early-ui.cjs)
// ============================================================================

async function runEarlyUiSuite() {
  const combinedSource = INDEX_HTML + "\n" + readStatic("app.js") + "\n" + readStatic("tft.js");
  for (const marker of ['data-key="early"', 'id="tft-early-panel"', "restoreTftEarlyHash(hash)", "cancelTftEarly()", "await loadTftEarly()"]) {
    assert(combinedSource.includes(marker), `Early mode integration is present: ${marker}`);
  }

  function roster(level, { three = false, special = false, cheaper = false } = {}) {
    return Array.from({ length: level }, (_, i) => {
      const cost = three && i === level - 1 ? 3 : cheaper || i % 2 === 0 ? 1 : 2;
      const slug = `${special && i === 0 ? "special" : "unit"}-${level}-${i}${cost === 3 ? "-three" : ""}`;
      return {
        api: slug,
        slug,
        name: slug.toUpperCase(),
        cost,
        star: 1,
        slotCost: 1,
        icon: "/unit.png",
        frontline: i < 2,
        role: i < 2 ? "Tank" : "Caster",
        objective: i < 2 ? "tank" : "carry",
        traitApis: ["shared"],
        items: [],
        itemApis: [],
        dps: 50 + i,
        damage: 1000 + i * 50,
        aliveTime: 12 + i,
      };
    });
  }

  function board(id, level, reference, options = {}) {
    const units = roster(level, options);
    const wins = options.wins ?? 1;
    const matchups = reference.initiatives.map((initiative, i) => ({
      key: `${id}-trial-${i}`,
      label: "Early reference",
      opponentId: `reference-${level}`,
      initiative,
      laneOffset: 0,
      roster: plain(reference.boards[0].units),
      outcome: i < wins ? "win" : "loss",
      duration: 20,
      allyHpFraction: i < wins ? 0.4 : 0,
      enemyHpFraction: i < wins ? 0 : 0.4,
    }));
    return {
      id,
      rank: 1,
      level,
      boardSlots: level,
      slotsUsed: level,
      unitCount: level,
      mainCarry: units.at(-1).slug,
      mainTank: units[0].slug,
      purchaseGold: units.reduce((sum, unit) => sum + unit.cost, 0),
      itemCount: 0,
      threeCostCount: units.filter(unit => unit.cost === 3).length,
      activeTraitCount: 1,
      units,
      traits: [
        {
          api: "shared",
          name: "Shared",
          icon: "/trait.png",
          count: level,
          active: true,
          breakpoint: 2,
          levels: [2, 4],
          modeled: true,
          coverage: "partial",
          coverageNotes: ["Some combat effects are omitted."],
        },
      ],
      metrics: {
        benchmarkWins: wins,
        benchmarkCount: 2,
        benchmarkWinRate: wins / 2,
        benchmarkScore: wins * 50,
        benchmarkLosses: 2 - wins,
        benchmarkDraws: 0,
        benchmarkTimeouts: 0,
        damageDps: options.dps ?? 100,
        frontlineTime: options.frontline ?? 10,
        hpMargin: 0,
        clearTime: wins ? 20 : null,
      },
      matchups,
    };
  }

  function fixture() {
    const referencePools = Object.fromEntries(
      [4, 5].map(level => [
        `level${level}`,
        {
          opponentCount: 1,
          encounterCount: 2,
          initiatives: [0, 1],
          laneOffsets: [0],
          boards: [{ id: `reference-${level}`, label: "Early reference", units: roster(level) }],
        },
      ])
    );
    const raw = {
      schema: "tft-early-comps-v1",
      complete: true,
      evaluationModel: "stage-two-reference-fights-v1",
      revision: "early-v1",
      baselineRevision: "baseline-v1",
      publicationRevision: "site-v1",
      set: 18,
      patch: "18.3",
      stage: 2,
      starAssumption: "All units at 1\u2605",
      itemCount: 0,
      limitations: ["Items and augments are not included."],
      search: { description: "These are promising boards from a bounded deterministic search." },
      referencePools,
      variants: [4, 5].map(level => ({
        key: `level${level}`,
        label: `${level} units`,
        level,
        boardSlots: level,
        maxThreeCosts: level === 5 ? 1 : 0,
        boards: [],
      })),
    };
    raw.variants[0].boards = Array.from({ length: 30 }, (_, i) =>
      board(`four-${String(i).padStart(2, "0")}`, 4, referencePools.level4, {
        wins: i % 3,
        dps: 100 + i,
        frontline: 10 + i,
        special: i === 0,
        cheaper: i === 1,
      })
    );
    raw.variants[1].boards = [
      board("five-with-three", 5, referencePools.level5, { three: true, wins: 2 }),
      board("five-without-three", 5, referencePools.level5, { wins: 1 }),
    ];
    const units = new Map(raw.variants.flatMap(variant => variant.boards.flatMap(b => b.units)).map(unit => [unit.api, unit]));
    raw.teamPlanner = {
      format: "tft-team-planner-v2",
      slots: 10,
      set: 18,
      unitCodes: Object.fromEntries([...units.values()].map((unit, i) => [unit.api, (i + 1).toString(16).padStart(3, "0")])),
    };
    return raw;
  }

  function harness() {
    const base = loadDashboard({ api: async () => fixture() });
    const { context } = base;
    context.META = { tiers: ["fixture"], defaultTier: "fixture", platforms: [] };
    context.state.view = "tft";
    context.state.tier = "fixture";
    Object.assign(context.tstate, {
      mode: "early",
      meta: null,
      status: null,
      unit: "unused-build-unit",
      star: 3,
      geo: "clump",
      traits: "bare",
      threat: "mixed",
    });
    const counters = { status: 0 };
    context.scheduleTftStatus = () => ++counters.status;
    const node = sel => (sel.startsWith("#") || sel.includes(" ") ? base.query(sel) : base.element(sel));
    return {
      base,
      context,
      tearly: context.tearly,
      tstate: context.tstate,
      node,
      calls: base.apiCalls,
      copies: base.copied,
      get savedHashes() { return base.historyWrites.map(w => w.hash); },
      counters,
      setResponder: fn => base.setResponder(fn),
    };
  }

  function checkValidation() {
    const h = harness();
    const raw = fixture();
    const saved = JSON.stringify(raw);
    assert.equal(h.context.tftEarlyCatalog(raw), raw);
    for (const [name, mutate] of [
      ["wrong schema", data => { data.schema = "other"; }],
      ["unfinished payload", data => { data.complete = false; }],
      ["old upgrade assumption", data => { data.starAssumption = "1-costs at 2\u2605; 2- and 3-costs at 1\u2605"; }],
      ["upgraded champion", data => { data.variants[0].boards[0].units[0].star = 2; }],
      ["upgraded reference opponent", data => { data.referencePools.level4.boards[0].units[0].star = 2; }],
      ["upgraded fight opponent", data => { data.variants[0].boards[0].matchups[0].roster[0].star = 2; }],
      ["3-cost on four-unit board", data => Object.assign(data.variants[0].boards[0].units[0], { cost: 3, star: 1 })],
      ["two 3-costs on five-unit board", data => Object.assign(data.variants[1].boards[0].units[0], { cost: 3, star: 1 })],
      ["item added", data => { data.variants[0].boards[0].units[0].items = ["Item"]; }],
      ["incorrect gold", data => { data.variants[0].boards[0].purchaseGold += 1; }],
      ["wrong unit count", data => { data.variants[0].boards[0].unitCount = 5; }],
      ["duplicate champion", data => { data.variants[0].boards[0].units[1] = plain(data.variants[0].boards[0].units[0]); }],
      ["false win rate", data => { data.variants[0].boards[0].metrics.benchmarkWinRate = 1; }],
      ["false win count", data => { data.variants[0].boards[0].metrics.benchmarkWins = 2; }],
      ["unrecorded benchmark fight", data => { data.variants[0].boards[0].matchups.pop(); }],
      ["unknown opponent", data => { data.variants[0].boards[0].matchups[0].opponentId = "unknown"; }],
      ["duplicate trial", data => { data.variants[0].boards[0].matchups[1].initiative = 0; }],
      ["changed opponent roster", data => { data.variants[0].boards[0].matchups[0].roster[0].api = "unknown"; }],
    ]) {
      const invalid = plain(raw);
      mutate(invalid);
      assert.throws(() => h.context.tftEarlyCatalog(invalid), undefined, name);
    }
    const drawn = plain(raw);
    const row = drawn.variants[0].boards[0];
    row.matchups[0].outcome = "draw";
    row.matchups[1].outcome = "timeout";
    Object.assign(row.metrics, { benchmarkLosses: 0, benchmarkDraws: 1, benchmarkTimeouts: 1 });
    assert.equal(h.context.tftEarlyCatalog(drawn), drawn, "Draws and timeouts count as no win and remain valid evidence");
    assert.equal(JSON.stringify(raw), saved, "Validation preserves the published artifact");
  }

  async function checkBrowserControls() {
    const h = harness();
    const ctx = h.context;
    ctx.restoreTftEarlyHash({ elevel: "4", eunit: "special-4-0" });
    ctx.renderTftEarly();
    assert.equal(h.tearly.unit, "special-4-0", "Loading controls preserve a champion restored from the URL");
    assert.equal(await ctx.loadTftEarly(), true);
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].url, "api/tft/early.json", "The early mode needs no late-game composition or champion cells");
    assert.equal(h.node("#tft-early-table tbody").querySelectorAll(".tft-early-summary").length, 1, "The saved champion filter applies after loading");
    await h.node("tft-early-clear").fire("click");
    assert.equal(h.node("#tft-early-table tbody").querySelectorAll(".tft-early-summary").length, 25, "Boards paginate at 25 rows");
    assert.match(h.node("tft-early-page").textContent, /1\u201325 of 30/);
    await h.node("tft-early-next").fire("click");
    assert.equal(h.node("#tft-early-table tbody").querySelectorAll(".tft-early-summary").length, 5);
    assert.match(h.node("tft-early-page").textContent, /26\u201330 of 30/);
    ctx.setTftEarlySort("dps", "desc");
    assert.equal(ctx.tftEarlyRows()[0].id, "four-29");
    ctx.setTftEarlySort("frontline", "asc");
    assert.equal(ctx.tftEarlyRows()[0].id, "four-00");
    ctx.setTftEarlySort("gold", "asc");
    assert.equal(ctx.tftEarlyRows()[0].id, "four-01");
    assert.equal(h.tearly.page, 1, "Sorting returns to the first page");
    ctx.setTftEarlySort("wins", "desc");
    const before = JSON.stringify(h.tearly.data);
    const tbody = h.node("#tft-early-table tbody");
    const first = tbody.querySelector(".tft-early-summary");
    const open = first.querySelector(".tft-flex-open");
    await open.fire("click");
    const boardId = first.dataset.board;
    const card = h.tearly.cards.get(boardId);
    assert.equal(open.attributes["aria-expanded"], "true");
    assert(card.querySelectorAll(".tft-comp-unit").every(unit => ["1", "2"].includes(String(unit.dataset.cost))));
    assert.match(card.textContent, /No items/);
    assert.match(h.node("tft-early-context").textContent, /All units at 1\u2605/);
    ctx.renderTftEarlyMethod();
    assert.match(h.node("tft-early-method-body").textContent, /Every champion is 1\u2605/);
    assert.match(h.node("tft-early-method-body").textContent, /one copy of each champion/);
    const copy = card.querySelector(".tft-comp-copy").querySelector("button");
    await copy.fire("click");
    assert.equal(h.copies.length, 1);
    assert.match(h.copies[0], /^02[0-9a-f]{30}TFTSet18$/);
    const evidence = card.querySelector(".tft-early-evidence");
    evidence.open = true;
    await evidence.fire("toggle");
    assert.equal(evidence.querySelectorAll(".tft-comp-matchup-trial").length, 2);
    assert.match(evidence.textContent, /fixed early boards/);
    assert.match(evidence.textContent, /unit contributions/i);
    const firstUnit = card.querySelector(".tft-comp-unit-head");
    await firstUnit.fire("click");
    assert.equal(h.node("tft-tooltip").hidden, false);
    assert(h.node("tft-tooltip").children.length > 0);
    assert.equal(h.calls.length, 1, "Unit details remain within the item-free early comparison");
    ctx.setTftEarlySort("traits", "desc");
    assert.equal(h.tearly.cards.get(boardId), card, "Sorting retains expanded cards and their fight evidence");
    assert(h.tearly.openBoards.has(boardId));
    h.node("tft-early-level").value = "5";
    await h.node("tft-early-level").fire("change");
    assert.equal(h.node("tft-early-three").disabled, false);
    assert.equal(ctx.tftEarlyRows().length, 2);
    h.node("tft-early-unit").value = "unit-5-4-three";
    await h.node("tft-early-unit").fire("change");
    assert.deepEqual(Array.from(ctx.tftEarlyRows(), row => row.id), ["five-with-three"]);
    h.node("tft-early-three").checked = false;
    await h.node("tft-early-three").fire("change");
    assert.equal(h.tearly.unit, "all", "Excluding 3-costs clears an incompatible champion filter");
    assert.deepEqual(Array.from(ctx.tftEarlyRows(), row => row.id), ["five-without-three"]);
    assert.match(h.savedHashes.at(-1), /tftview=early.*elevel=5.*ethree=0/);
    assert(!h.savedHashes.at(-1).includes("unit=unused-build-unit"));
    ctx.restoreTftEarlyHash({ elevel: "5", ethree: "0", eunit: "unit-5-0", esort: "gold", eorder: "desc" });
    ctx.updateHash();
    assert.match(h.savedHashes.at(-1), /eunit=unit-5-0.*esort=gold.*eorder=desc/);
    ctx.restoreTftEarlyHash({ elevel: "-1", esort: "bad", eorder: "bad" });
    assert.equal(h.tearly.level, 4);
    assert.equal(h.tearly.sort, "wins");
    assert.equal(h.tearly.order, "desc");
    assert.equal(JSON.stringify(h.tearly.data), before, "Filtering, sorting, tooltips and planner copies preserve saved results");
    assert.equal(await ctx.loadTftEarly(), true);
    assert.equal(h.calls.length, 1, "Reopening the same saved section reuses its immutable payload");
  }

  async function checkErrorsAndCancellation() {
    const h = harness();
    const ctx = h.context;
    h.setResponder(async () => { throw new Error("HTTP 404"); });
    assert.equal(await ctx.loadTftEarly(), false);
    assert.equal(h.node("tft-early-retry").hidden, false);
    assert.match(h.node("tft-early-status").textContent, /not been published/);
    h.setResponder(async () => fixture());
    await h.node("tft-early-retry").fire("click");
    assert(h.tearly.data && !h.tearly.pending && !h.tearly.error);
    assert.equal(h.node("tft-early-retry").hidden, true);
    const saved = h.tearly.data;
    h.setResponder(async () => { throw new Error("network unavailable"); });
    assert.equal(await ctx.loadTftEarly({ force: true }), false);
    assert.equal(h.tearly.data, saved);
    assert.match(h.node("tft-early-status").textContent, /last loaded results/);
    assert.equal(h.node("tft-early-tablewrap").hidden, false, "A failed refresh retains the last published boards");
    let release;
    h.setResponder(() => new Promise(resolve => { release = resolve; }));
    const pending = ctx.loadTftEarly({ force: true });
    ctx.cancelTftEarly();
    assert.equal(h.calls.at(-1).options.signal.aborted, true);
    const other = fixture();
    other.revision = "stale-in-flight";
    release(other);
    await pending;
    assert.equal(h.tearly.data, saved, "A response from a canceled early request cannot replace the published data");
    assert.equal(h.tearly.loading, false);
    h.setResponder(async () => ({ pending: true }));
    assert.equal(await ctx.loadTftEarly({ force: true }), false);
    assert(h.tearly.pending);
    h.setResponder(async () => fixture());
    await ctx.loadTftEarly({ force: true });
    h.tstate.status = { patch: "18.3", publicationRevision: "site-v2" };
    let publications = 0;
    h.setResponder(async () => {
      ++publications;
      const raw = fixture();
      raw.publicationRevision = "site-v2";
      return raw;
    });
    await ctx.loadTftEarly();
    assert.equal(publications, 1, "A changed publication reloads the immutable endpoint");
  }

  async function checkNavigationAndStatus() {
    const h = harness();
    const ctx = h.context;
    let combatLoads = 0;
    ctx.loadTftMeta = ctx.loadTftScenario = ctx.loadTftLeaderboard = async () => { ++combatLoads; };
    let status = { patch: "18.3", revision: "baseline-v1", publicationRevision: "site-v1" };
    ctx.loadTftStatus = async () => {
      h.tstate.status = status;
      return status;
    };
    ctx.renderTftRefresh = () => {};
    h.tstate.mode = "builds";
    await ctx.setTftMode("early");
    assert.equal(h.node("tft-early-panel").hidden, false);
    assert.equal(h.node("tft-champion-panel").hidden, true);
    assert.equal(h.node("tft-compositions-panel"), null);
    await ctx.openTft();
    await ctx.pollTftStatus();
    assert.equal(combatLoads, 0, "Entering, opening and polling Early comps do not request late-game combat contexts");
    assert.equal(h.calls.length, 1);
    const saved = h.tearly.data;
    status = { ...status, revision: "changed-baseline-calculations" };
    await ctx.pollTftStatus();
    assert.equal(h.tearly.data, saved, "An unrelated calculation revision preserves the published early boards");
    status = { ...status, publicationRevision: "site-v2" };
    h.setResponder(async () => ({ ...fixture(), publicationRevision: "site-v2" }));
    await ctx.pollTftStatus();
    assert.equal(h.tearly.data.publicationRevision, "site-v2", "Status changes reload a newly published early artifact");
    const request = h.tearly.req;
    await ctx.setTftMode("builds", { load: false });
    assert(h.tearly.req > request, "Leaving the early mode cancels its request generation");
    assert.equal(h.node("tft-early-panel").hidden, true);
    assert.equal(h.node("tft-champion-panel").hidden, false);
  }

  checkValidation();
  await checkBrowserControls();
  await checkErrorsAndCancellation();
  await checkNavigationAndStatus();

  ++passedSuites;
}

// ============================================================================
// 7. Trait Flex Search, Interface, and Lobby Workers (test-tft-flex-ui.cjs)
// ============================================================================

async function runFlexUiSuite() {
  const algorithmPage = loadDashboard();
  const algorithmContext = algorithmPage.context;
  const combinedSource = INDEX_HTML + "\n" + readStatic("app.js") + "\n" + readStatic("tft.js");

  function fixture() {
    const trait = (api, levels) => ({ api, name: api.toUpperCase(), levels, icon: "/trait.png" });
    const unit = (slug, traitApis, frontline = false, options = {}) => ({
      api: slug,
      slug,
      name: slug.toUpperCase(),
      cost: 2,
      icon: "/unit.png",
      role: frontline ? "Tank" : "Caster",
      objective: frontline ? "tank" : "carry",
      range: frontline ? 1 : 4,
      frontline,
      backline: !frontline,
      slotCost: 1,
      traitApis,
      traitContributions: Object.fromEntries(traitApis.map(api => [api, 1])),
      ...options,
    });
    const units = [
      unit("tank", ["a", "unique"], true, { cost: 4 }),
      unit("carry", ["b", "unique"], false, { cost: 4 }),
      unit("front-a", ["a", "c"], true),
      unit("front-b", ["b", "c"], true),
      unit("duo-a", ["d", "mixed"]),
      unit("duo-b", ["d", "mixed"]),
      unit("front-d", ["a", "d"], true),
      unit("back-b", ["b", "mixed"]),
      unit("bridge", ["c", "mixed"]),
      unit("connect", ["a", "b"]),
      unit("elder", ["a", "b", "c"], true, { cost: 5, slotCost: 2, traitContributions: { a: 2, b: 1, c: 2 } }),
    ];
    return {
      schema: "tft-trait-flex-v1",
      revision: "units-v1",
      publicationRevision: "site-v1",
      set: 18,
      patch: "18.3",
      level: 8,
      boardSlots: 8,
      units,
      traits: [
        trait("a", [2, 4]),
        trait("b", [2, 4]),
        trait("c", [2, 3]),
        trait("d", [2]),
        trait("unique", [1]),
        trait("mixed", [4, 1, 2, 2, 4]),
      ],
      teamPlanner: {
        format: "tft-team-planner-v2",
        slots: 10,
        set: 18,
        unitCodes: Object.fromEntries(units.map((u, index) => [u.api, (index + 1).toString(16).padStart(3, "0")])),
      },
    };
  }

  const settings = { tank: "tank", carry: "carry", preference: "breadth", minFrontline: 3, allowFive: false, locked: [] };

  function soloAnchorFixture() {
    const raw = fixture();
    raw.traits.push({ api: "tank-solo", name: "Tank solo", levels: [1] }, { api: "carry-solo", name: "Carry solo", levels: [1] });
    for (const unit of raw.units) {
      unit.traitApis = unit.traitApis.filter(api => !["a", "b", "unique"].includes(api));
      if (unit.slug === "tank") unit.traitApis = ["a", "tank-solo"];
      if (unit.slug === "carry") unit.traitApis = ["b", "carry-solo"];
      unit.traitContributions = Object.fromEntries(unit.traitApis.map(api => [api, 1]));
    }
    return raw;
  }

  function lobbyFixture() {
    const raw = fixture();
    const template = raw.units.find(unit => unit.slug === "carry");
    raw.units.push({
      ...template,
      api: "other-four",
      slug: "other-four",
      name: "OTHER FOUR",
      range: 2,
      backline: false,
      traitApis: [],
      traitContributions: {},
    });
    raw.teamPlanner.unitCodes = Object.fromEntries(
      raw.units.map((unit, index) => [unit.api, (index + 1).toString(16).padStart(3, "0")])
    );
    raw.shop = {
      costOdds: [0.15, 0.20, 0.32, 0.30, 0.03],
      championCounts: [1, 2, 3, 4, 5].map(cost => raw.units.filter(unit => unit.cost === cost).length),
      bagCopies: [30, 25, 18, 10, 9],
      refreshCost: 2,
      visibleSlots: [4, 5],
      sources: [],
    };
    const inference = { tank: 1.4, carry: 0.6, "front-a": 0.6, "duo-a": 0.2, "other-four": 1, elder: 3 };
    const profile = (id, rank, rankLabel, factor) => ({
      id,
      patch: "18.3",
      rank,
      rankLabel,
      fetchedAt: "2026-10-11T14:00:00+00:00",
      updatedAt: "2026-10-11T13:00:00+00:00",
      sourceUrl: "https://tactics.tools/",
      units: raw.units.map(unit => ({
        api: unit.api,
        slug: unit.slug,
        name: unit.name,
        cost: unit.cost,
        playRatePerLobby: ((inference[unit.slug] || 0) * factor * 8) / 3,
        meanCopiesWhenPlayed: 3,
        starRates: [0, 1, 0],
        copiesPerPlayer: (inference[unit.slug] || 0) * factor,
      })),
    });
    raw.lobbyStats = {
      schema: "tft-lobby-stats-v1",
      profiles: [profile("emerald-profile", "emerald_plus", "Emerald+", 1), profile("gold-profile", "gold", "Gold", 0.5)],
      defaultProfile: "emerald-profile",
    };
    return raw;
  }

  function multiAnchorFixture({ exactRoster = false } = {}) {
    const raw = fixture();
    if (exactRoster) {
      raw.units = raw.units.filter(unit =>
        ["tank", "carry", "front-a", "front-b", "duo-a", "duo-b", "back-b", "connect"].includes(unit.slug)
      );
      raw.units.find(unit => unit.slug === "front-a").cost = 4;
      raw.units.find(unit => unit.slug === "back-b").cost = 4;
    } else {
      for (const [slug, source] of [["second-tank", "front-a"], ["second-carry", "bridge"]]) {
        const original = raw.units.find(unit => unit.slug === source);
        raw.units.push({ ...original, api: slug, slug, name: slug.toUpperCase(), cost: 4 });
      }
    }
    raw.teamPlanner.unitCodes = Object.fromEntries(
      raw.units.map((unit, index) => [unit.api, (index + 1).toString(16).padStart(3, "0")])
    );
    return raw;
  }

  function costFixture() {
    const raw = fixture();
    for (const unit of raw.units) if (unit.cost < 4) unit.cost = 3;
    raw.units.find(unit => unit.slug === "duo-a").cost = 1;
    raw.units.find(unit => unit.slug === "duo-b").cost = 2;
    for (const [slug, source] of [["alt-front", "front-a"], ["alt-bridge", "bridge"], ["alt-connect", "connect"]]) {
      const unit = raw.units.find(entry => entry.slug === source);
      raw.units.push({ ...unit, api: slug, slug, name: slug.toUpperCase() });
    }
    return raw;
  }

  function coreCostFixture() {
    const raw = fixture();
    for (const unit of raw.units) if (unit.cost < 4) unit.cost = 1;
    const clone = (slug, source, cost) => {
      const original = raw.units.find(unit => unit.slug === source);
      const unit = {
        ...original,
        api: slug,
        slug,
        name: slug.toUpperCase(),
        cost,
        traitApis: [...original.traitApis],
        traitContributions: { ...original.traitContributions },
      };
      raw.units.push(unit);
      return unit;
    };
    for (const cost of [2, 3]) {
      for (const suffix of ["a", "b"]) {
        clone(`tank${cost}-${suffix}`, "tank", cost);
        clone(`carry${cost}-${suffix}`, "carry", cost);
      }
    }
    for (const [slug, source] of [
      ["four-front-a", "front-a"],
      ["four-front-b", "front-b"],
      ["four-back-a", "duo-a"],
      ["four-back-b", "back-b"],
    ]) {
      const unit = clone(slug, source, 4);
      for (let index = 0; index < 2; ++index) {
        const api = `${slug}-solo-${index}`;
        raw.traits.push({ api, name: api.toUpperCase(), levels: [1], icon: "/trait.png" });
        unit.traitApis.push(api);
        unit.traitContributions[api] = 1;
      }
    }
    raw.teamPlanner.unitCodes = Object.fromEntries(
      raw.units.map((unit, index) => [unit.api, (index + 1).toString(16).padStart(3, "0")])
    );
    return raw;
  }

  function enumerate(ctx, catalog, options) {
    const cores = options.cores || [options.tank, options.carry];
    const seed = [...new Set([...cores, ...(options.locked || [])])].map(slug => catalog.unitsBySlug.get(slug));
    const pool = catalog.units.filter(unit => !seed.includes(unit) && (options.allowFive || unit.cost <= 4));
    const result = [];
    function visit(index, selected, slots) {
      if (slots === 8) {
        const b = ctx.tftFlexEvaluate(catalog, selected, options);
        if (b.valid) result.push(b);
        return;
      }
      for (let next = index; next < pool.length; ++next) {
        if (slots + pool[next].slotCost <= 8) visit(next + 1, [...selected, pool[next]], slots + pool[next].slotCost);
      }
    }
    visit(0, seed, seed.reduce((sum, unit) => sum + unit.slotCost, 0));
    return result.sort(ctx.tftFlexCompare);
  }

  function interfaceHarness({ cores = ["tank", "carry"] } = {}) {
    const base = loadDashboard();
    const { context, document } = base;
    context.META = { tiers: ["fixture"], defaultTier: "fixture", platforms: [] };
    context.state.view = "tft";
    context.state.tier = "fixture";
    Object.assign(context.tstate, {
      mode: "traits",
      meta: null,
      status: null,
      unit: "old-carry",
      star: 3,
      geo: "clump",
      traits: "high",
      threat: "mixed",
    });
    context.tflex.cores = [...cores];
    const calls = [];
    const counters = { hashes: 0, schedules: 0, combat: 0 };
    let currentApi = async () => fixture();
    Object.defineProperty(context, "api", {
      get() {
        return async (url, options) => {
          calls.push({ url, options });
          return currentApi(url, options);
        };
      },
      set(fn) { currentApi = fn; },
      configurable: true,
    });
    const realUpdateHash = context.updateHash;
    context.updateHash = push => {
      ++counters.hashes;
      return realUpdateHash(push);
    };
    context.scheduleTftStatus = () => { ++counters.schedules; };
    return {
      base,
      context,
      flex: context.tflex,
      champion: context.tstate,
      counters,
      calls,
      copied: base.copied,
      element: id => base.element(id),
      document,
    };
  }

  function installGoldWorkers(h) {
    const workers = h.base.installWorkers();
    return { workers };
  }

  async function settled(h) {
    for (let count = 0; count < 100 && (h.flex.searching || h.flex.loading); ++count) {
      await new Promise(resolve => setTimeout(resolve, 1));
    }
    assert(!h.flex.searching && !h.flex.loading, "Interface work completes within a bounded wait");
  }

  async function openFirstBoard(h) {
    const results = h.element("tft-flex-results");
    const row = results.querySelector(".tft-flex-summary");
    assert(row, "The summary table contains a board");
    await row.querySelector(".tft-flex-open").fire("click");
    const b = h.flex.boards.find(entry => entry.id === row.dataset.board);
    const card = h.flex.cards.get(b.id);
    assert(card && results.querySelector(".tft-flex-card"), "Expanding a row lazily renders the complete board");
    return { row, board: b, card };
  }

  function coreButton(h, slug) {
    return h.element("tft-flex-unit-seg").querySelectorAll("button").find(button => button.dataset.key === slug);
  }

  async function toggleCore(h, slug) {
    const button = coreButton(h, slug);
    assert(button, `The selected cost provides a portrait button for ${slug}`);
    await button.fire("click");
    await until(() => !h.flex.searching && !h.flex.loading, "Core selection completes a bounded trait search");
  }

  function checkScoring() {
    const raw = fixture();
    const snapshot = JSON.stringify(raw);
    const ctx = algorithmContext;
    const catalog = ctx.tftFlexCatalog(raw);
    assert.equal(JSON.stringify(raw), snapshot, "Catalog normalization preserves published data");
    assert.deepEqual(plain(catalog.traitsByApi.get("mixed").levels), [1, 2, 4], "Scoring keeps solo and distinct shared integer thresholds");
    assert.deepEqual(plain(catalog.traitsByApi.get("mixed").sharedLevels), [2, 4], "Shared-only thresholds remain available as metadata");
    const measure = (api, count, preference = "breadth") => {
      const counts = new Int16Array(catalog.traits.length);
      counts[catalog.traitsByApi.get(api).index] = count;
      return ctx.tftFlexMeasure(catalog, counts, preference);
    };
    for (const [preference, soloScore, nextScore, higherScore] of [["breadth", 3, 4, 5], ["vertical", 1, 4, 7]]) {
      assert.equal(measure("unique", 0, preference).score, 0, "An absent solo trait is inactive");
      assert.equal(measure("unique", 1, preference).score, soloScore, "A reached 1-unit trait scores once");
      assert.equal(measure("unique", 8, preference).score, soloScore, "Extra units do not multiply a solo trait's score");
      assert.equal(measure("unique", 1, preference).activeCount, 1);
      assert.equal(measure("unique", 1, preference).extraTiers, 0);
      assert.equal(measure("mixed", 0, preference).score, 0);
      assert.equal(measure("mixed", 1, preference).score, soloScore, "The first [1, 2, 4] tier activates at one unit");
      assert.equal(measure("mixed", 2, preference).score, nextScore, "A reached shared tier adds one tier above solo activation");
      assert.equal(measure("mixed", 3, preference).score, nextScore, "Incomplete next tiers receive no reward");
      assert.equal(measure("mixed", 4, preference).score, higherScore, "Duplicate thresholds do not add extra vertical tiers");
      assert.equal(measure("mixed", 4, preference).extraTiers, 2);
      assert.equal(measure("a", 1, preference).score, 0, "A trait whose first breakpoint is two remains inactive at one unit");
    }
    assert.equal(measure("a", 8).score, measure("a", 4).score, "Overflow beyond the last breakpoint adds no score");
    const isolated = fixture();
    isolated.units.push(
      ...Array.from({ length: 6 }, (_, index) => ({
        ...isolated.units[4],
        api: `solo${index}`,
        slug: `solo${index}`,
        traitApis: ["unique"],
        traitContributions: { unique: 1 },
      }))
    );
    const isolatedCatalog = ctx.tftFlexCatalog(isolated);
    const soloCovered = ctx.tftFlexEvaluate(
      isolatedCatalog,
      ["tank", "carry", ...Array.from({ length: 6 }, (_, index) => `solo${index}`)],
      { ...settings, minFrontline: 1 }
    );
    assert.equal(soloCovered.valid, true, "An anchor's activated solo trait satisfies its coverage requirement");
    assert.deepEqual(plain(soloCovered.anchorCoverage.tank).map(trait => trait.api), ["unique"]);
    assert.deepEqual(plain(soloCovered.anchorCoverage.carry).map(trait => trait.api), ["unique"]);
    assert.equal(soloCovered.score, 3, "A shared solo trait counts once while covering both owners");
    for (const unit of isolated.units.filter(entry => ["tank", "carry"].includes(entry.slug))) {
      unit.traitApis = unit.traitApis.filter(api => api !== "unique");
      delete unit.traitContributions.unique;
    }
    const noSoloCatalog = ctx.tftFlexCatalog(isolated);
    const invalid = ctx.tftFlexEvaluate(
      noSoloCatalog,
      ["tank", "carry", ...Array.from({ length: 6 }, (_, index) => `solo${index}`)],
      { ...settings, minFrontline: 1 }
    );
    assert.equal(invalid.valid, false, "Solo traits on unrelated supports cannot cover anchors without those traits");
    assert.equal(invalid.anchorCoverage.tank.length, 0);
    assert.equal(invalid.anchorCoverage.carry.length, 0);
    assert.equal(invalid.score, 3, "An active unrelated solo trait still scores even though this roster fails anchor coverage");
    const supported = ctx.tftFlexEvaluate(noSoloCatalog, ["tank", "carry", "front-a", "front-b", "duo-a", "duo-b", "front-d", "back-b"], settings);
    assert(
      supported.valid &&
        supported.anchorCoverage.tank.some(trait => trait.api === "a") &&
        supported.anchorCoverage.carry.some(trait => trait.api === "b"),
      "Anchors without solo traits become covered when their shared breakpoints activate"
    );
    const thinRaw = fixture();
    thinRaw.units.push({ ...thinRaw.units[4], api: "solo", slug: "solo", traitApis: ["unique"], traitContributions: { unique: 1 } });
    const thinCatalog = ctx.tftFlexCatalog(thinRaw);
    const thinUnits = ["tank", "carry", "connect", "duo-a", "duo-b", "back-b", "bridge", "solo"];
    const thin = ctx.tftFlexEvaluate(thinCatalog, thinUnits, { ...settings, minFrontline: 1 });
    assert(thin.valid && thin.frontlineCount === 1, "One frontline anchor is legal when the configurable minimum is one");
    assert.equal(ctx.tftFlexEvaluate(thinCatalog, thinUnits, settings).valid, false, "The same roster fails the default three-frontline requirement");
    assert.throws(() => ctx.tftFlexCatalog({ ...raw, schema: "other" }), /Unexpected trait catalog/);
    const duplicate = fixture();
    duplicate.units.push(duplicate.units[0]);
    assert.throws(() => ctx.tftFlexCatalog(duplicate), /Duplicate units/);
    assert.throws(() => ctx.tftFlexOptions(catalog, { ...settings, carry: "front-a" }), /backline carry/, "A melee or non-4-cost unit cannot be the backline anchor");
  }

  async function checkSearchAndSwaps() {
    const ctx = algorithmContext;
    const raw = fixture();
    const catalog = ctx.tftFlexCatalog(raw);
    const snapshot = JSON.stringify(raw);
    const searchOptions = { ...settings, beamWidth: 800, yieldTask: async () => {} };
    const first = await ctx.tftFlexSearch(catalog, searchOptions);
    const second = await ctx.tftFlexSearch(catalog, searchOptions);
    assert(first.boards.length >= 2, "Search offers distinct support packages");
    assert.deepEqual(plain(first), plain(second), "Results and expansion counts are deterministic");
    assert.equal(first.boards[0].score, enumerate(ctx, catalog, settings)[0].score, "Fixture ranking matches independent complete enumeration");
    assert(first.boards.every(b => b.units.every(unit => unit.cost <= 4)), "Default supports stop at 4 cost");
    assert(first.boards.every(b => b.frontlineCount >= 3 && b.slotsUsed === 8 && b.valid));
    assert(first.boards.every(b => b.units.length === new Set(b.units.map(unit => unit.api)).size), "Every roster contains distinct champions");
    assert(first.boards.every(b => b.anchorCoverage.tank.length && b.anchorCoverage.carry.length));
    assert(first.boards.every((b, index) => index === 0 || ctx.tftFlexCompare(first.boards[index - 1], b) <= 0), "Diverse cards retain score order");
    const support = b => b.units.filter(unit => !["tank", "carry"].includes(unit.slug)).map(unit => unit.slug);
    for (let i = 0; i < first.boards.length; ++i) {
      for (let j = i + 1; j < first.boards.length; ++j) {
        assert(support(first.boards[i]).filter(slug => support(first.boards[j]).includes(slug)).length <= 4, "Cards differ by at least two support units");
      }
    }
    const vertical = await ctx.tftFlexSearch(catalog, { ...searchOptions, preference: "vertical" });
    assert.equal(vertical.boards[0].score, enumerate(ctx, catalog, { ...settings, preference: "vertical" })[0].score);
    const locked = await ctx.tftFlexSearch(catalog, { ...searchOptions, locked: ["duo-a"], minFrontline: 1 });
    assert(locked.boards.every(b => b.units.some(unit => unit.slug === "duo-a")), "Owned units stay on every result");
    const heavy = await ctx.tftFlexSearch(catalog, { ...searchOptions, allowFive: true, locked: ["elder"] });
    assert(heavy.boards.length && heavy.boards.every(b => b.units.length === 7 && b.slotsUsed === 8), "A two-slot unit occupies two slots and appears only once");
    await assert.rejects(() => ctx.tftFlexSearch(catalog, { ...searchOptions, locked: ["elder"] }), /Enable 5-cost supports/);
    await assert.rejects(() => ctx.tftFlexSearch(catalog, { ...searchOptions, locked: catalog.units.filter(unit => unit.cost < 4).map(unit => unit.slug) }), /more than 8/);
    const impossible = await ctx.tftFlexSearch(catalog, { ...searchOptions, minFrontline: 8 });
    assert.equal(impossible.boards.length, 0, "Unsatisfiable frontline requirements produce an honest empty result");
    let yields = 0;
    const cancelled = await ctx.tftFlexSearch(catalog, {
      ...searchOptions,
      yieldEvery: 2,
      yieldTask: async () => { ++yields; },
      cancelled: () => yields >= 2,
    });
    assert(cancelled.cancelled && cancelled.expanded <= 2, "Search yields and promptly stops after cancellation");
    const bounded = await ctx.tftFlexSearch(catalog, { ...searchOptions, maxExpansions: 7 });
    assert(bounded.expanded <= 7 && bounded.truncated, "The expansion budget is a hard bound");
    const decorated = fixture();
    decorated.units.forEach(unit => Object.assign(unit, { items: ["fake"], star: 3, dps: 999999, damage: 123456 }));
    const agnostic = await ctx.tftFlexSearch(ctx.tftFlexCatalog(decorated), searchOptions);
    assert.deepEqual(plain(first), plain(agnostic), "Items, stars and simulated damage cannot influence boards or scores");
    const b = first.boards[0];
    const options = { ...searchOptions, minFrontline: 3, swapLimit: 20, maxScoreLoss: 100 };
    const swaps = await ctx.tftFlexSwaps(catalog, b, options);
    assert(swaps.swaps.some(swap => swap.removed.length === 1 && swap.added.length === 1));
    assert(swaps.swaps.some(swap => swap.removed.length === 2 && swap.added.length === 2));
    for (const swap of swaps.swaps) {
      assert(ctx.tftFlexEvaluate(catalog, swap.board.units, options).valid, "Every swap keeps complete slots, anchors, coverage, cost pool and frontline");
      assert(!swap.removed.some(unit => ["tank", "carry"].includes(unit.slug)), "Swaps preserve anchors");
      assert.equal(swap.scoreDelta, swap.board.score - b.score);
      assert.deepEqual(plain(swap.traitChanges), plain(ctx.tftFlexTraitChanges(b, swap.board)));
      for (const change of swap.traitChanges) {
        assert.equal(change.beforeCount, b.units.reduce((sum, unit) => sum + (unit.traitContributions[change.api] || 0), 0));
        assert.equal(change.afterCount, swap.board.units.reduce((sum, unit) => sum + (unit.traitContributions[change.api] || 0), 0), "Inactive counts are preserved in breakpoint changes");
      }
    }
    const protectedSwaps = await ctx.tftFlexSwaps(catalog, b, { ...options, locked: [b.units[2].slug] });
    assert(protectedSwaps.swaps.every(swap => !swap.removed.some(unit => unit.slug === b.units[2].slug)), "Swaps preserve owned locks");
    const heavySwaps = await ctx.tftFlexSwaps(catalog, b, { ...options, allowFive: true });
    assert(
      heavySwaps.swaps.some(swap => swap.removed.length === 2 && swap.added.length === 1 && swap.added[0].slug === "elder"),
      "Two one-slot supports may exchange for one two-slot unit"
    );
    yields = 0;
    const cancelledSwaps = await ctx.tftFlexSwaps(catalog, b, {
      ...options,
      yieldEvery: 3,
      yieldTask: async () => { ++yields; },
      cancelled: () => yields >= 2,
    });
    assert(cancelledSwaps.cancelled && cancelledSwaps.expanded <= 3, "Swap work also yields and cancels");
    assert.equal(JSON.stringify(raw), snapshot, "Searching and swaps leave immutable published data unchanged");
  }

  async function checkSoloAnchorSearchAndSwaps() {
    const ctx = algorithmContext;
    const raw = soloAnchorFixture();
    const snapshot = JSON.stringify(raw);
    const catalog = ctx.tftFlexCatalog(raw);
    const kept = ["front-a", "front-b", "duo-a", "duo-b", "front-d", "back-b"];
    for (const [preference, expectedScore] of [["breadth", 16], ["vertical", 8]]) {
      const options = { ...settings, preference, yieldTask: async () => {} };
      const evaluated = ctx.tftFlexEvaluate(catalog, ["tank", "carry", ...kept], options);
      assert(evaluated.valid);
      assert.equal(evaluated.activeCount, 5);
      assert.equal(evaluated.extraTiers, 1);
      assert.equal(evaluated.score, expectedScore, "Solo activation and the next mixed trait tier use the chosen formula");
      assert.deepEqual(plain(evaluated.anchorCoverage.tank).map(trait => trait.api), ["tank-solo"]);
      assert.deepEqual(plain(evaluated.anchorCoverage.carry).map(trait => trait.api), ["carry-solo"]);
      const exact = await ctx.tftFlexSearch(catalog, { ...options, locked: kept });
      assert.equal(exact.boards.length, 1);
      assert.equal(exact.expanded, 0, "An exact-eight locked seed is evaluated without requiring trait partners");
      assert.equal(exact.boards[0].score, expectedScore);
      const searched = await ctx.tftFlexSearch(catalog, options);
      assert(searched.boards.length, "Normal search retains solo-covered anchors without any shared-trait partner in the pool");
      assert(
        searched.boards.every(
          b => b.anchorCoverage.tank.some(trait => trait.api === "tank-solo") && b.anchorCoverage.carry.some(trait => trait.api === "carry-solo")
        )
      );
      assert(searched.boards.every(b => !b.activeTraits.some(trait => ["a", "b"].includes(trait.api))), "The anchors' two-unit traits stay inactive");
      const withPartners = soloAnchorFixture();
      for (const [slug, source, trait] of [["tank-partner", "front-a", "a"], ["carry-partner", "back-b", "b"]]) {
        const template = withPartners.units.find(unit => unit.slug === source);
        withPartners.units.push({ ...template, api: slug, slug, name: slug.toUpperCase(), traitApis: [trait], traitContributions: { [trait]: 1 } });
      }
      const partneredCatalog = ctx.tftFlexCatalog(withPartners);
      const partnered = ctx.tftFlexEvaluate(
        partneredCatalog,
        ["tank", "carry", "front-a", "front-b", "duo-a", "duo-b", "tank-partner", "carry-partner"],
        options
      );
      const swaps = await ctx.tftFlexSwaps(partneredCatalog, partnered, { ...options, swapLimit: 24, maxScoreLoss: 100 });
      for (const [slug, key, api] of [["tank-partner", "tank", "a"], ["carry-partner", "carry", "b"]]) {
        const swap = swaps.swaps.find(entry => entry.removed.length === 1 && entry.removed[0].slug === slug);
        assert(swap, `A former shared-trait partner can be removed for ${key}`);
        assert(swap.board.valid && swap.board.anchorCoverage[key].some(trait => trait.api === `${key}-solo`));
        assert(!swap.board.activeTraits.some(trait => trait.api === api), "The solo-covered anchor remains legal after losing its shared trait");
        const change = swap.traitChanges.find(entry => entry.api === api);
        assert.equal(change.beforeBreakpoint, 2);
        assert.equal(change.afterBreakpoint, null);
        assert.equal(change.beforeCount, 2);
        assert.equal(change.afterCount, 1, "Losing a partner shows the remaining anchor contribution");
      }
    }
    assert.equal(JSON.stringify(raw), snapshot);
    const h = interfaceHarness();
    h.context.api = async () => soloAnchorFixture();
    await h.context.loadTftFlex();
    const { board: b, card } = await openFirstBoard(h);
    assert(card.textContent.includes("Active traits") && !card.textContent.includes("Active shared traits"));
    assert(card.textContent.includes("1 Tank solo") && card.textContent.includes("1 Carry solo"), "Solo coverage and active chips are visible on the board");
    const soloChip = card.querySelectorAll(".tft-comp-trait").find(chip => chip.textContent.includes("Tank solo"));
    assert(soloChip.title.includes("breakpoints 1"), "A solo breakpoint is included in the chip's explanation");
    const tooltip = h.context.tftFlexUnitTooltip(b.units.find(unit => unit.slug === "tank"), b).map(node => node.textContent).join(" ");
    assert(tooltip.includes("Reached 1") && tooltip.includes("core coverage") && !tooltip.includes("excluded"), "Solo activation is described as valid scoring and coverage");
    const mixedTooltip = h.context.tftFlexUnitTooltip(h.flex.catalog.unitsBySlug.get("duo-a"), b).map(node => node.textContent).join(" ");
    assert(mixedTooltip.includes("1 / 2 / 4"), "Mixed trait tooltips retain the solo breakpoint before shared tiers");
  }

  async function checkBrowsingAndSorting() {
    const ctx = algorithmContext;
    const raw = multiAnchorFixture();
    const snapshot = JSON.stringify(raw);
    const catalog = ctx.tftFlexCatalog(raw);
    const options = { ...settings, tank: "all", carry: "all", beamWidth: 800, yieldTask: async () => {} };
    const progress = [];
    const browsed = await ctx.tftFlexBrowse(catalog, { ...options, onProgress: update => progress.push(plain(update)) });
    assert.equal(browsed.pairsTotal, 4);
    assert.equal(browsed.pairsDone, 4);
    assert.equal(browsed.cancelled, false);
    assert(browsed.boards.length > 6, "All-pair browsing provides more than a single pair's shortlist");
    assert.equal(new Set(browsed.boards.map(b => b.id)).size, browsed.boards.length, "A roster appears once even when several anchor pairs generate it");
    assert.equal(progress.at(-1).pairsDone, 4);
    assert.equal(progress.at(-1).pairsTotal, 4);
    assert.equal(progress.at(-1).expanded, browsed.expanded);
    assert.equal(progress.at(-1).boardsFound, browsed.boards.length);
    assert(
      progress.every((update, index) => !index || (update.pairsDone >= progress[index - 1].pairsDone && update.expanded >= progress[index - 1].expanded)),
      "Progress advances with completed pairs and actual search work"
    );
    let independentlyExpanded = 0;
    const independentRosters = new Set();
    for (const tank of catalog.units.filter(unit => unit.cost === 4 && unit.frontline)) {
      for (const carry of catalog.units.filter(unit => unit.cost === 4 && unit.backline)) {
        const pair = await ctx.tftFlexSearch(catalog, { ...options, tank: tank.slug, carry: carry.slug });
        independentlyExpanded += pair.expanded;
        pair.boards.forEach(b => independentRosters.add(b.id));
      }
    }
    assert.equal(browsed.expanded, independentlyExpanded, "Browsing reports the actual work from each bounded search");
    assert.deepEqual([...browsed.boards.map(b => b.id)].sort(), [...independentRosters].sort(), "Browsing merges every pair's valid shortlist");
    for (const b of browsed.boards) {
      assert(b.anchorPairs.length, "Every merged board records its generating anchor pairs");
      assert.equal(new Set(b.anchorPairs.map(pair => `${pair.tank}|${pair.carry}`)).size, b.anchorPairs.length);
      assert(
        b.anchorPairs.every(pair => ctx.tftFlexEvaluate(catalog, b.units, { ...settings, ...pair }).valid),
        "Each recorded anchor pair belongs to the roster and has activated trait coverage"
      );
      assert(ctx.tftFlexEvaluate(catalog, b.units, { ...settings, tank: b.mainTank, carry: b.mainCarry }).valid);
    }
    const repeated = await ctx.tftFlexBrowse(catalog, options);
    assert.deepEqual(plain(repeated), plain(browsed), "Cross-pair aggregation is deterministic");
    const allByNull = await ctx.tftFlexBrowse(catalog, { ...options, tank: null, carry: null });
    assert.deepEqual(plain(allByNull), plain(browsed), "Missing anchor selections browse all eligible pairs");
    for (const filter of [{ tank: "tank", carry: "all" }, { tank: "all", carry: "carry" }]) {
      const filtered = await ctx.tftFlexBrowse(catalog, { ...options, ...filter });
      assert.equal(filtered.pairsTotal, 2);
      assert.equal(filtered.pairsDone, 2);
      assert(
        filtered.boards.length &&
          filtered.boards.every(b => b.anchorPairs.every(pair => (filter.tank === "all" ? pair.carry === filter.carry : pair.tank === filter.tank))),
        "A single-anchor filter keeps the other anchor flexible"
      );
    }
    const pinned = await ctx.tftFlexBrowse(catalog, { ...options, tank: "tank", carry: "carry" });
    const direct = await ctx.tftFlexSearch(catalog, { ...options, tank: "tank", carry: "carry" });
    assert.equal(pinned.pairsTotal, 1);
    assert.deepEqual(pinned.boards.map(b => b.id).sort(), direct.boards.map(b => b.id).sort(), "Two filters preserve the direct pair shortlist");
    const deduped = await ctx.tftFlexBrowse(ctx.tftFlexCatalog(multiAnchorFixture({ exactRoster: true })), options);
    assert.equal(deduped.pairsTotal, 4);
    assert.equal(deduped.boards.length, 1);
    assert.equal(deduped.boards[0].anchorPairs.length, 4, "An identical board merges all four valid anchor pairs");
    const cancelledBefore = await ctx.tftFlexBrowse(catalog, { ...options, cancelled: () => true });
    assert(cancelledBefore.cancelled && cancelledBefore.pairsDone === 0 && cancelledBefore.expanded === 0, "Cancellation can stop before any pair search");
    let stop = false;
    const cancelledDuring = await ctx.tftFlexBrowse(catalog, {
      ...options,
      cancelled: () => stop,
      onProgress: update => { if (update.pairsDone >= 1) stop = true; },
    });
    assert(cancelledDuring.cancelled && cancelledDuring.pairsDone === 1 && cancelledDuring.pairsTotal === 4, "Cancellation stops the pair loop without starting the next pair");
    const owned = await ctx.tftFlexBrowse(catalog, { ...options, locked: ["duo-a"] });
    assert(owned.boards.length && owned.boards.every(b => b.units.some(unit => unit.slug === "duo-a")), "All-pair browsing preserves owned units across every pair");
    await assert.rejects(() => ctx.tftFlexBrowse(catalog, { ...options, minCost: 3, locked: ["duo-a"] }), /minimum unit cost/, "A conflicting owned unit is reported before checking all pairs");
    assert.equal(JSON.stringify(raw), snapshot, "Pair browsing leaves immutable catalog and planner data untouched");

    const sortBoards = [
      { id: "a", score: 20, activeCount: 6, extraTiers: 2, frontlineCount: 3, units: [{ cost: 4 }, { cost: 4 }, { cost: 3 }], gold: { totalGold: 100.2 }, lobbyGold: { totalGold: 120 } },
      { id: "b", score: 18, activeCount: 5, extraTiers: 3, frontlineCount: 4, units: [{ cost: 4 }, { cost: 4 }, { cost: 4 }, { cost: 1 }], gold: { totalGold: 80.9 }, lobbyGold: { totalGold: 100.1 } },
      { id: "c", score: 20, activeCount: 7, extraTiers: 1, frontlineCount: 2, units: [{ cost: 4 }, { cost: 4 }, { cost: 2 }], gold: { totalGold: 120.1 }, lobbyGold: { totalGold: 100.2 } },
      { id: "d", score: 10, activeCount: 2, extraTiers: 0, frontlineCount: 1, units: [{ cost: 4 }, { cost: 4 }, { cost: 1 }] },
    ];
    const sortSnapshot = JSON.stringify(sortBoards);
    const ids = (field, direction) => plain(ctx.tftFlexSortedBoards(sortBoards, field, direction)).map(b => b.id);
    assert.deepEqual(ids("gold", "asc"), ["b", "a", "c", "d"]);
    assert.deepEqual(ids("gold", "desc"), ["c", "a", "b", "d"], "Missing gold stays last in either direction");
    assert.deepEqual(ids("lobbyGold", "asc"), ["b", "c", "a", "d"], "Typical-lobby sorting uses its own unrounded estimate");
    assert.deepEqual(ids("lobbyGold", "desc"), ["a", "c", "b", "d"], "Missing typical-lobby gold stays last in either direction");
    assert.deepEqual(ids("purchaseGold", "asc"), ["d", "c", "a", "b"], "Unit-purchase sorting counts three copies for every 4-cost");
    assert.deepEqual(ids("frontlineCount", "desc"), ["b", "a", "c", "d"]);
    assert.deepEqual(ids("activeCount", "desc"), ["c", "a", "b", "d"]);
    assert.deepEqual(ids("extraTiers", "desc"), ["b", "a", "c", "d"]);
    assert.equal(ids("fourCosts", "desc")[0], "b");
    assert.deepEqual(ids("score", "desc"), ["c", "a", "b", "d"], "Trait score preserves the existing full-precision tie rules");
    assert.equal(ids("score", "asc")[0], "d");
    const precision = [{ ...sortBoards[0], id: "near-a", gold: { totalGold: 100.2 } }, { ...sortBoards[0], id: "near-b", gold: { totalGold: 100.1 } }];
    assert.equal(ctx.tftFlexSortedBoards(precision, "gold", "asc")[0].id, "near-b", "Gold sorting uses exact expectations before display rounding");
    assert.deepEqual(plain(ctx.tftFlexSortedBoards(sortBoards.slice().reverse(), "gold", "asc")).map(b => b.id), ids("gold", "asc"), "Sorting does not depend on pair arrival order");
    assert.equal(JSON.stringify(sortBoards), sortSnapshot, "Sorting preserves board objects and their source order");
  }

  async function checkInterface() {
    const h = interfaceHarness();
    const ctx = h.context;
    assert(INDEX_HTML.includes('data-key="traits"') && INDEX_HTML.includes('id="tft-traits-panel"'), "Trait flex has its own navigation and panel");
    const panel = INDEX_HTML.slice(INDEX_HTML.indexOf('<section class="card" id="tft-traits-panel"'), INDEX_HTML.indexOf('<div id="tft-champion-panel"'));
    assert(!/<select[^>]*(?:item|star)|data-key="(?:geo|threat)"/.test(panel), "Unit mode has no item, star or combat controls");
    await ctx.loadTftFlex();
    assert(h.flex.boards.length && plain(h.flex.cores).join("|") === "carry|tank", "Selected same-cost core units generate support packages");
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].url, "api/tft/traits.json");
    assert.equal(h.calls[0].options.cache, "no-store");
    assert(coreButton(h, "tank") && coreButton(h, "carry"));
    assert(h.element("tft-flex-formula").textContent.includes("3 \u00d7 active traits"));
    assert.equal(h.element("tft-flex-frontline").value, "3");
    assert(h.element("tft-flex-results").textContent.includes("Trait score"));
    assert(!/DPS|EHP|undefined|NaN/.test(h.element("tft-flex-results").textContent), "Cards contain no combat scores or malformed values");
    assert.equal(h.element("tft-flex-results").querySelectorAll(".tft-flex-card").length, 0, "The summary view defers detailed cards until selected");
    const { card, board: b } = await openFirstBoard(h);
    assert(card.textContent.includes("2\u2605 target"), "Every 4-cost has the required three-copy gold target");
    const unitButton = card.querySelector(".tft-comp-unit-head");
    await unitButton.fire("click");
    assert.equal(h.element("tft-tooltip").hidden, false);
    assert(h.element("tft-tooltip").textContent.includes("Core unit"), "Champion clicks show a generic core-unit tooltip");
    const copy = card.querySelector(".tft-comp-copy").children[0];
    await copy.fire("click");
    assert.equal(h.copied[0], ctx.tftCompositionTeamCode(b, h.flex.catalog.teamPlanner), "Copy uses the exact unit roster and catalog planner IDs");
    const details = card.querySelector(".tft-flex-swaps");
    details.open = true;
    await details.fire("toggle");
    assert(details.querySelector("table") && details.textContent.includes("Reached breakpoints") && details.textContent.includes("Score change"), "Expanded swaps show before/after breakpoints and score deltas");
    assert.equal(h.calls.length, 1, "Tooltips, swaps and copy require no combat fetch");
    h.element("tft-flex-frontline").value = "1";
    await h.element("tft-flex-frontline").fire("change");
    await settled(h);
    assert.equal(h.flex.minFrontline, 1, "The exact stated minimum of one frontline is selectable");
    const verticalButton = h.element("tft-flex-preference-seg").children[1];
    await verticalButton.fire("click");
    await settled(h);
    assert.equal(h.flex.preference, "vertical");
    assert(h.element("tft-flex-formula").textContent.includes("3 \u00d7 extra reached tiers"));
    h.element("tft-flex-lock-search").value = "front-a";
    await h.element("tft-flex-lock-search").fire("input");
    const lock = h.element("tft-flex-lock-list").querySelector("input");
    lock.checked = true;
    await lock.fire("change");
    await settled(h);
    assert(h.flex.locked.includes("front-a") && h.flex.boards.every(entry => entry.units.some(unit => unit.slug === "front-a")));
    const original = h.flex.boards;
    const revision = h.flex.catalog.revision;
    ctx.api = async () => { throw new Error("HTTP 500"); };
    await ctx.loadTftFlex({ force: true });
    assert.equal(h.flex.boards, original, "A catalog request failure preserves the previous accepted boards");
    assert(h.element("tft-flex-status").textContent.includes("previous boards remain available"));
    assert.equal(h.element("tft-flex-retry").hidden, false);
    ctx.api = async () => ({ ...fixture(), publicationRevision: "site-v2" });
    h.champion.status = { publicationRevision: "site-v2", revision: "unrelated-combat-v2", patch: "18.3" };
    await h.element("tft-flex-retry").fire("click");
    assert.equal(h.flex.catalog.publicationRevision, "site-v2");
    assert.equal(h.flex.catalog.revision, revision, "Publication refresh keeps catalog and combat revisions independent");
    assert.equal(h.element("tft-flex-retry").hidden, true);
    const missing = interfaceHarness();
    missing.context.api = async () => { throw new Error("HTTP 404"); };
    await missing.context.loadTftFlex();
    assert(missing.flex.pending && missing.element("tft-flex-status").textContent.includes("older dashboard server"));
    assert.equal(missing.element("tft-flex-retry").hidden, false, "Old bundles have a clear retry path");
    missing.context.api = async () => ({ pending: true });
    await missing.context.loadTftFlex({ force: true });
    assert(missing.flex.pending && !missing.flex.error, "An unpublished catalog is pending rather than malformed");
    missing.context.api = async () => fixture();
    await missing.element("tft-flex-retry").fire("click");
    assert(missing.flex.catalog && missing.flex.boards.length, "An explicit retry recovers when the catalog publishes");
    const race = interfaceHarness();
    let finish;
    race.context.api = () => new Promise(resolve => { finish = resolve; });
    const old = race.context.loadTftFlex();
    race.context.api = async () => ({ ...fixture(), publicationRevision: "site-new" });
    await race.context.loadTftFlex({ force: true });
    finish({ ...fixture(), publicationRevision: "site-old" });
    await old;
    assert.equal(race.flex.catalog.publicationRevision, "site-new", "Late requests cannot overwrite a newer accepted publication");
  }

  async function checkCoreBrowseInterface() {
    const h = interfaceHarness();
    const ctx = h.context;
    ctx.api = async () => multiAnchorFixture();
    await ctx.loadTftFlex();
    assert(!INDEX_HTML.includes('id="tft-flex-tank"') && !INDEX_HTML.includes('id="tft-flex-carry"'), "Portrait multiselect replaces the role-paired dropdowns");
    assert.equal(coreButton(h, "tank").attributes["aria-pressed"], "true");
    assert.equal(coreButton(h, "second-tank").attributes["aria-pressed"], "false");
    await toggleCore(h, "second-tank");
    assert.deepEqual(plain(h.flex.cores), ["carry", "second-tank", "tank"]);
    assert(h.flex.boards.length && h.flex.boards.every(b => plain(b.cores).join("|") === "carry|second-tank|tank"));
    assert.equal(coreButton(h, "second-tank").attributes["aria-pressed"], "true");
    assert(h.element("tft-flex-selected").textContent.includes("SECOND-TANK"), "Every selected core has a visible selected-unit chip");
    const summary = h.element("tft-flex-results").querySelector(".tft-flex-summary");
    assert(["TANK", "CARRY", "SECOND-TANK"].every(name => summary.textContent.includes(name)), "The summary names every chosen core without assigning tank/carry roles");
    const { card, board: b } = await openFirstBoard(h);
    const swaps = card.querySelector(".tft-flex-swaps");
    const originalSwapSearch = ctx.tftFlexSwaps;
    let captured;
    ctx.tftFlexSwaps = (catalog, selected, options) => {
      captured = options;
      return originalSwapSearch(catalog, selected, options);
    };
    swaps.open = true;
    await swaps.fire("toggle");
    assert.deepEqual(plain(captured.cores), plain(b.cores), "A selected board keeps all three core champions during support swaps");
    assert(!Object.hasOwn(b, "mainTank") && !Object.hasOwn(b, "mainCarry"), "Generic boards do not invent role assignments");
    assert(swaps.querySelector("table"), "Three-core boards still expose interchangeable supports");
    const searchRequest = h.flex.searchReq;
    const apiCalls = h.calls.length;
    h.element("tft-flex-sort").value = "purchaseGold";
    await h.element("tft-flex-sort").fire("change");
    assert.equal(h.flex.sort, "purchaseGold");
    assert.equal(h.flex.order, "asc", "Gold sorts start with the cheapest boards");
    assert.equal(h.flex.searchReq, searchRequest);
    assert.equal(h.calls.length, apiCalls, "Sorting reuses searched boards without extra fetches");
    assert.equal(h.flex.cards.get(b.id), card);
    assert(h.flex.openBoards.has(b.id));
    assert.equal(swaps.open, true, "Reordering preserves an expanded support-swap section");
    h.element("tft-flex-order").value = "desc";
    await h.element("tft-flex-order").fire("change");
    assert.equal(h.flex.order, "desc");

    const example = h.flex.boards[0];
    h.flex.boards = Array.from({ length: 57 }, (_, index) => ({
      ...example,
      id: `page-${String(index).padStart(2, "0")}`,
      score: 100 - index,
      gold: { totalGold: 1000 - index, refreshGold: 900 - index, purchaseGold: 100 },
    }));
    h.flex.cards = new Map();
    h.flex.openBoards = new Set();
    h.flex.sort = "score";
    h.flex.order = "desc";
    h.flex.page = 1;
    ctx.renderTftFlex();
    const visible = () => h.element("tft-flex-results").querySelectorAll(".tft-flex-summary");
    assert.equal(visible().length, 25);
    assert.equal(visible()[0].dataset.board, "page-00");
    assert(h.element("tft-flex-page").textContent.includes("Showing 1\u201325 of 57"));
    assert.equal(h.element("tft-flex-prev").disabled, true);
    assert.equal(h.element("tft-flex-next").disabled, false);
    const first = await openFirstBoard(h);
    const retainedSwaps = first.card.querySelector(".tft-flex-swaps");
    retainedSwaps.open = true;
    await h.element("tft-flex-next").fire("click");
    assert.equal(h.flex.page, 2);
    assert.equal(visible().length, 25);
    assert.equal(visible()[0].dataset.board, "page-25");
    assert.equal(h.flex.cards.get(first.board.id), first.card, "Off-page cards remain cached");
    await h.element("tft-flex-next").fire("click");
    assert.equal(h.flex.page, 3);
    assert.equal(visible().length, 7);
    assert.equal(h.element("tft-flex-next").disabled, true);
    assert(h.element("tft-flex-page").textContent.includes("Showing 51\u201357 of 57"));
    await h.element("tft-flex-prev").fire("click");
    await h.element("tft-flex-prev").fire("click");
    assert.equal(h.flex.page, 1);
    assert.equal(h.element("tft-flex-results").querySelector(".tft-flex-card"), first.card);
    assert.equal(retainedSwaps.open, true, "Returning to a page restores the existing open card and swap section");
    h.element("tft-flex-sort").value = "gold";
    await h.element("tft-flex-sort").fire("change");
    assert.equal(h.flex.page, 1);
    assert.equal(visible()[0].dataset.board, "page-56", "Gold sorting compares the entire board pool before paging");
    assert.equal(h.flex.cards.get(first.board.id), first.card);
    h.element("tft-flex-order").value = "desc";
    await h.element("tft-flex-order").fire("change");
    assert.equal(visible()[0].dataset.board, "page-00");
    assert.equal(h.element("tft-flex-results").querySelector(".tft-flex-card"), first.card);
    const scoreHeader = h.element("tft-flex-results").querySelectorAll("th").find(cell => cell.textContent.startsWith("Trait score"));
    await scoreHeader.querySelector("button").fire("click");
    assert.equal(h.flex.sort, "score");
    assert.equal(h.flex.order, "desc");
    assert.equal(scoreHeader.attributes["aria-sort"], "none");
    const currentScoreHeader = h.element("tft-flex-results").querySelectorAll("th").find(cell => cell.textContent.startsWith("Trait score"));
    assert.equal(currentScoreHeader.attributes["aria-sort"], "descending", "Column sorting exposes its direction accessibly");
    await currentScoreHeader.querySelector("button").fire("click");
    assert.equal(h.flex.order, "asc");
    assert.equal(visible()[0].dataset.board, "page-56", "A second header click reverses the order");
    h.flex.page = 99;
    ctx.renderTftFlex();
    assert.equal(h.flex.page, 3, "Restored or stale page indexes stay within available results");
    h.flex.boards = [];
    ctx.renderTftFlex();
    assert.equal(h.element("tft-flex-pagination").hidden, true);
    assert.equal(h.element("tft-flex-empty").hidden, false);
  }

  async function checkModeAndRouting() {
    const h = interfaceHarness();
    const ctx = h.context;
    ctx.loadTftMeta = ctx.loadTftScenario = ctx.loadTftLeaderboard = async () => { ++h.counters.combat; };
    ctx.loadTftStatus = async () => ({ revision: "combat-v1", publicationRevision: "site-v1", patch: "18.3" });
    ctx.renderTftRefresh = () => {};
    h.champion.mode = "builds";
    await ctx.setTftMode("traits");
    assert.equal(h.counters.combat, 0, "Entering Trait flex loads no champion or combat data");
    assert.equal(h.element("tft-traits-panel").hidden, false);
    assert.equal(h.element("tft-compositions-panel"), null);
    await ctx.openTft();
    await ctx.pollTftStatus();
    assert.equal(h.counters.combat, 0, "Opening and polling the new mode never load simulations");
    const accepted = h.flex.catalog;
    ctx.loadTftStatus = async () => ({ revision: "combat-v2", publicationRevision: "site-v1", patch: "18.3" });
    await ctx.pollTftStatus();
    assert.equal(h.flex.catalog, accepted, "A changed combat calculation revision does not invalidate a unit catalog");
    ctx.loadTftStatus = async () => ({ publicationRevision: "site-v2", patch: "18.3" });
    ctx.api = async () => ({ ...fixture(), publicationRevision: "site-v2" });
    await ctx.pollTftStatus();
    assert.equal(h.flex.catalog.publicationRevision, "site-v2", "Status notices a new catalog publication");
    const request = h.flex.searchReq;
    h.champion.data = { unit: h.champion.unit, scenario: { key: "saved" } };
    h.champion.dataRevision = "fixture";
    ctx.tkey = () => "saved";
    ctx.tftRevision = () => "fixture";
    h.champion.meta = { revision: "fixture" };
    await ctx.setTftMode("builds", { load: false });
    assert(h.flex.searchReq > request, "Leaving the mode cancels in-flight search and swap work");
    h.champion.mode = "traits";
    h.flex.cores = ["tank", "carry"];
    h.flex.preference = "vertical";
    h.flex.minFrontline = 1;
    h.flex.minCost = 3;
    h.flex.allowFive = true;
    h.flex.locked = ["front-a", "elder"];
    h.flex.sort = "gold";
    h.flex.order = "asc";
    h.flex.lobbyProfile = "gold-profile";
    h.flex.lobbyPlayers = 3;
    h.flex.lobbyDevelopment = 0.25;
    h.flex.lobbyOverrides = { tank: 0, "front-a": 5 };
    h.base.location.hash = "";
    ctx.restoringHash = false;
    ctx.restoredHash = null;
    ctx.updateHash();
    const hash = h.base.location.hash;
    assert(hash.includes("tftview=traits") && hash.includes("fpref=vertical") && hash.includes("ffront=1") && hash.includes("ffive=1"));
    assert(hash.includes("fmincost=3"));
    assert(hash.includes("fcore=") && !hash.includes("ftank=") && !hash.includes("fcarry="));
    assert(hash.includes("fsort=gold") && hash.includes("forder=asc"), "Sorting and its direction survive a shared link");
    assert(hash.includes("flprofile=gold-profile") && hash.includes("flplayers=3") && hash.includes("fldev=25"));
    assert(!/star=|citems=|geo=|tr=/.test(hash), "Unit-mode URLs carry no item, star or combat assumptions");
    const saved = Object.fromEntries(hash.slice(1).split("&").map(part => part.split("=").map(decodeURIComponent)));
    h.flex.cores = [];
    h.flex.locked = [];
    h.flex.minFrontline = 3;
    h.flex.minCost = 1;
    h.flex.allowFive = false;
    h.flex.sort = "score";
    h.flex.order = "desc";
    h.flex.lobbyProfile = null;
    h.flex.lobbyPlayers = 7;
    h.flex.lobbyDevelopment = 0.5;
    h.flex.lobbyOverrides = {};
    ctx.restoreTftFlexHash(saved);
    assert.deepEqual(plain(h.flex.cores), ["carry", "tank"]);
    assert.equal(h.flex.minFrontline, 1);
    assert.equal(h.flex.minCost, 3);
    assert.equal(h.flex.sort, "gold");
    assert.equal(h.flex.order, "asc");
    assert.equal(h.flex.lobbyProfile, "gold-profile");
    assert.equal(h.flex.lobbyPlayers, 3);
    assert.equal(h.flex.lobbyDevelopment, 0.25);
    assert.deepEqual(plain(h.flex.lobbyOverrides), { "front-a": 5, tank: 0 }, "A shared link preserves explicit zero scouts");
    assert.equal(h.flex.allowFive, true);
    assert.deepEqual(plain(h.flex.locked), ["elder", "front-a"]);
    const legacy = interfaceHarness();
    legacy.context.restoreTftFlexHash({ ftank: "tank", fcarry: "carry" });
    assert.deepEqual(plain(legacy.flex.cores), ["carry", "tank"], "An earlier explicitly chosen pair restores as generic cores");
    const unspecified = interfaceHarness();
    unspecified.context.restoreTftFlexHash({ ftank: "all", fcarry: "all" });
    assert.deepEqual(plain(unspecified.flex.cores), [], "Legacy All filters restore as no selected core champions");
    const absent = interfaceHarness();
    absent.context.restoreTftFlexHash({});
    assert.deepEqual(plain(absent.flex.cores), [], "A URL with no core choices retains the honest empty initial selection");
    const canonical = interfaceHarness();
    canonical.context.restoreTftFlexHash({ fcore: "carry,tank,carry", ftank: "front-a", fcarry: "duo-a" });
    assert.deepEqual(plain(canonical.flex.cores), ["carry", "tank"], "Explicit fcore choices take precedence over stale legacy role parameters");
    assert.equal(legacy.flex.sort, "score");
    assert.equal(legacy.flex.order, "desc", "Earlier pair-specific links retain the previous trait ranking");
    assert.equal(legacy.flex.lobbyPlayers, 7);
    assert.equal(legacy.flex.lobbyDevelopment, 0.5);
    const zero = interfaceHarness();
    zero.context.restoreTftFlexHash({ flplayers: "0", fldev: "0", flheld: "tank:0" });
    assert.equal(zero.flex.lobbyPlayers, 0);
    assert.equal(zero.flex.lobbyDevelopment, 0);
    assert.equal(zero.flex.lobbyOverrides.tank, 0, "Zero-player, zero-development and zero-scout settings are not replaced by defaults");
    assert(combinedSource.includes('if (mode === "traits") restoreTftFlexHash(hash);'), "Routing restores Trait flex settings before opening");
  }

  async function checkMinimumCost() {
    const raw = costFixture();
    const snapshot = JSON.stringify(raw);
    const ctx = algorithmContext;
    const catalog = ctx.tftFlexCatalog(raw);
    const mixed = ["tank", "carry", "duo-a", "duo-b", "front-a", "front-b", "front-d", "back-b"];
    assert(ctx.tftFlexEvaluate(catalog, mixed, settings).valid, "Default cost range preserves a board containing 1- and 2-costs");
    assert.equal(ctx.tftFlexEvaluate(catalog, mixed, { ...settings, minCost: 2 }).valid, false);
    assert.equal(ctx.tftFlexEvaluate(catalog, mixed, { ...settings, minCost: 3 }).valid, false);
    for (const minCost of [2, 3]) {
      const options = { ...settings, minCost, yieldTask: async () => {} };
      const result = await ctx.tftFlexSearch(catalog, options);
      assert(result.boards.length, `The eligible pool finds boards for minimum cost ${minCost}`);
      assert(result.boards.every(b => b.units.every(unit => unit.cost >= minCost && unit.cost <= 4)), "Excluded costs never enter generated boards");
      const swaps = await ctx.tftFlexSwaps(catalog, result.boards[0], { ...options, maxScoreLoss: 100 });
      assert(swaps.swaps.length);
      assert(swaps.swaps.every(swap => swap.added.every(unit => unit.cost >= minCost) && swap.board.units.every(unit => unit.cost >= minCost)), "Support swaps obey the same minimum cost as full boards");
      const withFive = await ctx.tftFlexSearch(catalog, { ...options, allowFive: true, locked: ["elder"] });
      assert(withFive.boards.length && withFive.boards.every(b => b.units.length === 7 && b.units.every(unit => unit.cost >= minCost)), "Cost filters coexist with two-slot 5-cost supports");
    }
    await assert.rejects(() => ctx.tftFlexSearch(catalog, { ...settings, minCost: 2, locked: ["duo-a"] }), /Unlock DUO-A.*minimum unit cost/);
    await assert.rejects(() => ctx.tftFlexSearch(catalog, { ...settings, minCost: 3, locked: ["duo-b"] }), /Unlock DUO-B.*minimum unit cost/);
    assert.equal(ctx.tftFlexOptions(catalog, settings).minCost, 1, "Earlier settings and links default to all costs");
    assert.equal(JSON.stringify(raw), snapshot, "Cost filtering does not remove units from the catalog or its shop population");
    const h = interfaceHarness();
    h.context.api = async () => costFixture();
    await h.context.loadTftFlex();
    assert.equal(h.element("tft-flex-min-cost").value, "1");
    h.element("tft-flex-min-cost").value = "3";
    await h.element("tft-flex-min-cost").fire("change");
    await settled(h);
    assert.equal(h.flex.minCost, 3);
    assert(h.flex.boards.length && h.flex.boards.every(b => b.units.every(unit => unit.cost >= 3)));
    assert(h.element("tft-flex-context").textContent.includes("3\u20134"));
    const cheapLock = () => h.element("tft-flex-lock-list").children.find(label => label.textContent.includes("DUO-A")).querySelector("input");
    assert(cheapLock().disabled, "Excluded units cannot be newly selected as kept units");
    h.flex.locked = ["duo-a"];
    h.context.renderTftFlexControls();
    await h.context.runTftFlexSearch();
    assert(h.flex.error.includes("Unlock DUO-A") && !cheapLock().disabled, "An excluded kept unit produces a clear conflict and remains unlockable");
    const locked = cheapLock();
    locked.checked = false;
    await locked.fire("change");
    await settled(h);
    assert(locked.disabled && !h.flex.error && h.flex.boards.length, "Unlocking the excluded unit restores valid filtered boards");
    h.element("tft-flex-min-cost").value = "2";
    await h.element("tft-flex-min-cost").fire("change");
    await settled(h);
    assert(h.flex.boards.every(b => b.units.every(unit => unit.cost >= 2)));
    h.element("tft-flex-min-cost").value = "1";
    await h.element("tft-flex-min-cost").fire("change");
    await settled(h);
    assert(!cheapLock().disabled, "Allowing all costs restores low-cost kept-unit choices");
  }

  async function checkCoreCosts() {
    const ctx = algorithmContext;
    const raw = coreCostFixture();
    const snapshot = JSON.stringify(raw);
    const catalog = ctx.tftFlexCatalog(raw);
    const fours = ["four-front-a", "four-front-b", "four-back-a", "four-back-b", "tank", "carry"];
    const cheap = ["front-a", "front-b", "front-d", "duo-a", "back-b", "bridge"];
    const fourCount = b => b.units.filter(unit => unit.cost === 4).length;
    assert.equal(ctx.tftFlexOptions(catalog, settings).cost, 4, "Earlier engine callers retain four-cost cores");
    const uncappedUnits = ["tank", "carry", ...fours.slice(0, 4), "front-a", "front-b"];
    const uncapped = ctx.tftFlexEvaluate(catalog, uncappedUnits, settings);
    assert(uncapped.valid && fourCount(uncapped) === 6, "Four-cost cores retain the existing uncapped support pool");
    for (const cost of [2, 3]) {
      const cap = cost === 2 ? 2 : 3;
      const options = { ...settings, cost, tank: `tank${cost}-a`, carry: `carry${cost}-a`, beamWidth: 800, yieldTask: async () => {} };
      const legal = [options.tank, options.carry, ...fours.slice(0, cap), ...cheap.slice(0, 6 - cap)];
      const overCap = [options.tank, options.carry, ...fours.slice(0, cap + 1), ...cheap.slice(0, 5 - cap)];
      const legalBoard = ctx.tftFlexEvaluate(catalog, legal, options);
      assert.equal(legalBoard.coreCost, cost, "Evaluation stamps the board with the selected gold target tier");
      assert(legalBoard.valid && fourCount(legalBoard) === cap, `Evaluation accepts exactly the stated cap for a ${cost}-cost core`);
      assert.equal(ctx.tftFlexEvaluate(catalog, overCap, options).valid, false, "A complete, covered roster is invalid when its total four-cost count exceeds the cap");
      assert.throws(() => ctx.tftFlexOptions(catalog, { ...options, tank: "tank" }), /frontline anchor/, "The frontline anchor must match the selected core cost");
      assert.throws(() => ctx.tftFlexOptions(catalog, { ...options, carry: `carry${cost === 2 ? 3 : 2}-a` }), /backline carry/, "Mixed-cost anchor pairs are rejected");
      assert.throws(() => ctx.tftFlexOptions(catalog, { ...options, tank: options.carry }), /frontline anchor/, "A matching-cost backline carry cannot occupy the frontline anchor role");
      assert.throws(() => ctx.tftFlexOptions(catalog, { ...options, carry: options.tank }), /backline carry/, "A matching-cost frontliner cannot occupy the backline anchor role");
      assert.throws(() => ctx.tftFlexOptions(catalog, { ...options, minCost: cost + 1 }), /minimum|core|cost/i, "Engine callers cannot exclude their own lower-cost anchors with a higher minimum");
      const exact = await ctx.tftFlexSearch(catalog, { ...options, locked: legal.slice(2) });
      assert.equal(exact.boards.length, 1);
      assert.equal(exact.expanded, 0);
      assert.equal(exact.boards[0].id, legalBoard.id, "An exact-eight locked seed is still accepted at the cap");
      await assert.rejects(() => ctx.tftFlexSearch(catalog, { ...options, locked: overCap.slice(2) }), /4-cost/i, "An over-cap complete locked seed is rejected before returning boards");
      await assert.rejects(() => ctx.tftFlexSearch(catalog, { ...options, locked: fours.slice(0, cap + 1) }), /4-cost/i, "Partial locked seeds count toward the cap before expansion");
      const result = await ctx.tftFlexSearch(catalog, options);
      assert(result.boards.length && result.expanded > 0);
      assert(result.boards.every(b => b.valid && fourCount(b) <= cap), "Expanded boards obey the total four-cost cap");
      assert.equal(fourCount(result.boards[0]), cap, "The search can fill the entire allowed four-cost budget");
      const independent = enumerate(ctx, catalog, options).filter(b => fourCount(b) <= cap);
      assert.equal(result.boards[0].score, independent[0].score, "Cost-capped search matches complete enumeration on the small fixture");
      const kept = await ctx.tftFlexSearch(catalog, { ...options, locked: [fours[0]] });
      assert(kept.boards.length && kept.boards.every(b => b.units.some(unit => unit.slug === fours[0]) && fourCount(b) <= cap), "A four-cost lock consumes one place in the cap throughout expansions");
      const swapped = await ctx.tftFlexSwaps(catalog, legalBoard, { ...options, swapLimit: 24, maxScoreLoss: 100, maxExpansions: 120000 });
      assert(swapped.swaps.length && swapped.swaps.every(swap => swap.board.valid && fourCount(swap.board) <= cap));
      const replacingFour = await ctx.tftFlexSwaps(catalog, legalBoard, {
        ...options,
        locked: legal.slice(2).filter(slug => slug !== fours[0]),
        swapLimit: 24,
        maxScoreLoss: 100,
      });
      assert(
        replacingFour.swaps.some(swap => swap.removed.length === 1 && swap.removed[0].cost === 4 && swap.added.length === 1 && swap.added[0].cost === 4 && fourCount(swap.board) === cap),
        "Being at the cap still permits a four-cost support to replace another four-cost"
      );
      assert(
        !swapped.swaps.some(swap => swap.removed.every(unit => unit.cost !== 4) && swap.added.some(unit => unit.cost === 4)),
        "A swap cannot add another four-cost while retaining all capped four-costs"
      );
      const fixedSwap = await ctx.tftFlexSwaps(catalog, legalBoard, { ...options, locked: [fours[0]], swapLimit: 24, maxScoreLoss: 100 });
      assert(fixedSwap.swaps.every(swap => !swap.removed.some(unit => unit.slug === fours[0]) && fourCount(swap.board) <= cap));
      await assert.rejects(() => ctx.tftFlexSwaps(catalog, { ...legalBoard, units: overCap.map(slug => catalog.unitsBySlug.get(slug)) }, options), /valid board/i);
      const browsed = await ctx.tftFlexBrowse(catalog, { ...options, tank: "all", carry: "all" });
      assert.equal(browsed.pairsTotal, 4);
      assert.equal(browsed.pairsDone, 4);
      assert(
        browsed.boards.length &&
          browsed.boards.every(
            b =>
              fourCount(b) <= cap &&
              b.anchorPairs.every(pair => {
                const tank = catalog.unitsBySlug.get(pair.tank);
                const carry = catalog.unitsBySlug.get(pair.carry);
                return tank.cost === cost && tank.frontline && carry.cost === cost && carry.backline;
              })
          ),
        "All-pair browsing uses the selected cost and role for both anchors"
      );
      const laterPair = { ...options, tank: `tank${cost}-b`, carry: `carry${cost}-b` };
      const laterSeed = [laterPair.tank, laterPair.carry, ...legal.slice(2)];
      const lockedBrowse = await ctx.tftFlexBrowse(catalog, { ...options, tank: "all", carry: "all", locked: laterSeed });
      assert.equal(lockedBrowse.boards.length, 1);
      assert.equal(lockedBrowse.expanded, 0);
      assert.deepEqual(plain(lockedBrowse.boards[0].anchorPairs), [{ tank: laterPair.tank, carry: laterPair.carry }], "An exact-eight owned roster can fit a later anchor pair without being rejected by the first pair");
      const targets = ctx.tftFlexLobbyTargets(legalBoard);
      assert(targets.every(target => target.copies === (target.cost === cost ? 9 : 1)), "Lower-cost profiles require nine copies of their own tier and one of every other tier");
    }
    assert.equal(JSON.stringify(raw), snapshot, "Changing core cost leaves the complete published unit and shop catalog intact");
  }

  async function checkGenericCores() {
    const ctx = algorithmContext;
    const raw = coreCostFixture();
    for (const [slug, source] of [["extra-four-front", "front-a"], ["extra-four-back", "duo-b"], ["ninth-four", "bridge"]]) {
      const template = raw.units.find(unit => unit.slug === source);
      raw.units.push({ ...template, api: slug, slug, name: slug.toUpperCase(), cost: 4 });
    }
    const snapshot = JSON.stringify(raw);
    const catalog = ctx.tftFlexCatalog(raw);
    const normalize = values => [...new Set(values)].sort();
    const containsCores = (b, cores) => {
      assert.deepEqual(plain(b.cores), normalize(cores));
      assert.equal(b.coreCost, catalog.unitsBySlug.get(cores[0]).cost, "Searches and swaps retain the board tier used for every unit's gold target");
      assert.deepEqual(Object.keys(b.coreCoverage).sort(), normalize(cores));
      for (const slug of cores) {
        assert(b.units.some(unit => unit.slug === slug), "Each chosen core appears on the board");
        assert(b.coreCoverage[slug].length && b.coreCoverage[slug].every(trait => catalog.unitsBySlug.get(slug).traitApis.includes(trait.api)), "Each core has its own activated trait coverage");
      }
      for (const field of ["mainTank", "mainCarry", "anchorCoverage", "anchorPairs"]) {
        assert(!Object.hasOwn(b, field), "Explicit cores do not fabricate legacy role-pair fields");
      }
    };
    for (const cost of [2, 3, 4]) {
      const base = { cost, minFrontline: 3, preference: "breadth", locked: [], beamWidth: 800, yieldTask: async () => {} };
      const sameRoleSelections = cost === 4 ? [["carry", "four-back-a"], ["tank", "four-front-a"]] : [[`carry${cost}-a`, `carry${cost}-b`], [`tank${cost}-a`, `tank${cost}-b`]];
      for (const cores of sameRoleSelections) {
        const options = { ...base, cores };
        const result = await ctx.tftFlexSearch(catalog, options);
        assert(result.boards.length, `Two ${cores[0].startsWith("carry") ? "backline" : "frontline"} cores are legal at cost ${cost}`);
        for (const b of result.boards) {
          assert(b.valid && b.slotsUsed === 8 && b.frontlineCount >= 3, "The frontline minimum applies to the whole board instead of assigning roles to cores");
          containsCores(b, cores);
          assert(b.units.filter(unit => unit.cost === 4).length <= (cost === 2 ? 2 : cost === 3 ? 3 : 8));
        }
        const independent = enumerate(ctx, catalog, options);
        assert.equal(result.boards[0].score, independent[0].score, "Generic core search matches full fixture enumeration for the selected role pattern");
      }
      const cores = cost === 4 ? ["tank", "carry", "four-back-a"] : [`tank${cost}-a`, `carry${cost}-a`, `carry${cost}-b`];
      const options = { ...base, cores };
      assert.deepEqual(plain(ctx.tftFlexOptions(catalog, { ...options, cores: [...cores].reverse().concat(cores[0]) }).cores), normalize(cores), "Explicit core lists normalize deterministically and deduplicate before counting");
      const result = await ctx.tftFlexSearch(catalog, options);
      assert(result.boards.length);
      result.boards.forEach(b => containsCores(b, cores));
      const browsed = await ctx.tftFlexBrowse(catalog, options);
      assert.deepEqual(browsed.boards.map(b => b.id), result.boards.map(b => b.id), "Explicit multiselect runs its one selected core search instead of enumerating role pairs");
      const b = result.boards[0];
      const swaps = await ctx.tftFlexSwaps(catalog, b, { ...options, swapLimit: 24, maxScoreLoss: 100 });
      assert(swaps.swaps.length);
      for (const swap of swaps.swaps) {
        assert(!swap.removed.some(unit => cores.includes(unit.slug)), "No one- or two-unit support swap can sell any of three selected cores");
        containsCores(swap.board, cores);
        assert(swap.board.units.filter(unit => unit.cost === 4).length <= (cost === 2 ? 2 : cost === 3 ? 3 : 8));
      }
      const supports = b.units.filter(unit => !cores.includes(unit.slug)).map(unit => unit.slug);
      const seeded = await ctx.tftFlexSearch(catalog, { ...options, locked: [...supports, cores[0]] });
      assert.equal(seeded.boards.length, 1);
      assert.equal(seeded.expanded, 0);
      assert.equal(seeded.boards[0].id, b.id, "Cores already listed among kept units count only once in an exact-eight seed");
      if (cost < 4) {
        const tooManyFours = catalog.units.filter(unit => unit.cost === 4).slice(0, cost + 1).map(unit => unit.slug);
        await assert.rejects(() => ctx.tftFlexSearch(catalog, { ...options, locked: tooManyFours }), /4-cost/i);
        assert.throws(() => ctx.tftFlexOptions(catalog, { ...options, minCost: cost + 1 }), /minimum|core|cost/i);
      }
    }
    const pair = { cost: 4, cores: ["tank", "carry"], minFrontline: 3, locked: [], yieldTask: async () => {} };
    for (const cores of [[], ["tank"], ["tank", "tank"], "tank,carry", ["tank", "missing-core"]]) {
      assert.throws(() => ctx.tftFlexOptions(catalog, { ...pair, cores }), /core|champion|array/i, "Invalid explicit selections produce a clear error instead of silently browsing all units");
    }
    assert.throws(() => ctx.tftFlexOptions(catalog, { ...pair, cores: ["tank", "carry2-a"] }), /core|cost/i);
    const eightCores = catalog.units.filter(unit => unit.cost === 4 && unit.slug !== "ninth-four").map(unit => unit.slug);
    assert.equal(eightCores.length, 8);
    const eight = await ctx.tftFlexSearch(catalog, { ...pair, cores: eightCores });
    assert.equal(eight.boards.length, 1);
    assert.equal(eight.expanded, 0);
    assert.equal(eight.boards[0].units.length, 8);
    containsCores(eight.boards[0], eightCores);
    const noSupports = await ctx.tftFlexSwaps(catalog, eight.boards[0], { ...pair, cores: eightCores });
    assert.equal(noSupports.swaps.length, 0);
    assert.equal(noSupports.expanded, 0, "A complete eight-core roster has no sellable supports");
    await assert.rejects(() => ctx.tftFlexSearch(catalog, { ...pair, cores: [...eightCores, "ninth-four"] }), /more than 8|8 team slots/i);
    const roster = ["tank", "carry", "front-a", "front-b", "front-d", "duo-a", "duo-b", "back-b"];
    const explicit = ctx.tftFlexEvaluate(catalog, roster, pair);
    const legacy = ctx.tftFlexEvaluate(catalog, roster, settings);
    assert.equal(explicit.score, legacy.score);
    assert.equal(explicit.activeCount, legacy.activeCount);
    assert.equal(explicit.extraTiers, legacy.extraTiers);
    assert.equal(explicit.id, legacy.id, "Generic cores preserve trait scoring and roster identity for an existing pair");
    assert.equal(JSON.stringify(raw), snapshot, "Generic selection and support swaps preserve the complete immutable catalog");
  }

  async function checkAutomaticCoreBrowse() {
    const ctx = algorithmContext;
    const raw = coreCostFixture();
    raw.units = raw.units.filter(unit => unit.cost !== 4 || ["tank", "carry", "four-front-a", "four-back-a"].includes(unit.slug));
    const snapshot = JSON.stringify(raw);
    const catalog = ctx.tftFlexCatalog(raw);
    const pairKey = pair => [...pair].sort().join("|");
    for (const cost of [2, 3, 4]) {
      const options = { cost, cores: [], minFrontline: 3, preference: "breadth", locked: [], beamWidth: 800, yieldTask: async () => {} };
      const pool = catalog.units.filter(unit => unit.cost === cost);
      const pairs = pool.flatMap((unit, index) => pool.slice(index + 1).map(other => [unit.slug, other.slug].sort()));
      assert.equal(pairs.length, 6);
      const expected = new Map();
      let expanded = 0;
      for (const cores of pairs) {
        const direct = await ctx.tftFlexSearch(catalog, { ...options, cores });
        assert(direct.boards.length, "The fixture has complete boards for every same-tier pair, including matching roles");
        expanded += direct.expanded;
        for (const b of direct.boards) {
          if (!expected.has(b.id)) expected.set(b.id, new Set());
          expected.get(b.id).add(pairKey(cores));
        }
      }
      const progress = [];
      const all = await ctx.tftFlexBrowse(catalog, { ...options, onProgress: update => progress.push(plain(update)) });
      assert.equal(all.pairsTotal, 6);
      assert.equal(all.pairsDone, 6);
      assert.equal(all.expanded, expanded);
      assert.equal(all.cancelled, false);
      assert.deepEqual([...all.boards.map(b => b.id)].sort(), [...expected.keys()].sort(), "No selection merges all pair shortlists instead of assigning frontline/backline roles");
      assert.equal(new Set(all.boards.map(b => b.id)).size, all.boards.length, "Multiple generated pairs never duplicate a roster");
      const observedPairs = new Set();
      for (const b of all.boards) {
        assert.equal(b.coreCost, cost, "No-selection browsing stamps the requested tier on every merged board");
        assert(
          ctx.tftFlexLobbyTargets(b).every(target => target.copies === (cost < 4 ? (target.cost === cost ? 9 : 1) : target.cost === 4 ? 3 : 1)),
          "Automatic all-pair results include the same-tier supports in the full star target"
        );
        assert(b.corePairs.length);
        const keys = b.corePairs.map(pairKey);
        assert.equal(new Set(keys).size, keys.length);
        assert.deepEqual([...keys].sort(), [...expected.get(b.id)].sort(), "A deduplicated roster retains every pair that generated it");
        assert(keys.includes(pairKey(b.cores)), "Each merged board keeps one of its generated pairs for support swaps");
        for (const cores of b.corePairs) {
          assert.deepEqual(plain(cores), [...cores].sort());
          observedPairs.add(pairKey(cores));
          assert(ctx.tftFlexEvaluate(catalog, b.units, { ...options, cores }).valid);
        }
        assert(b.units.filter(unit => unit.cost === 4).length <= (cost === 2 ? 2 : cost === 3 ? 3 : 8));
      }
      assert.deepEqual([...observedPairs].sort(), pairs.map(pairKey).sort(), "Automatic browsing includes every unordered pair without repeats or reversed duplicates");
      assert([...observedPairs].some(key => key.split("|").every(slug => catalog.unitsBySlug.get(slug).frontline)));
      assert([...observedPairs].some(key => key.split("|").every(slug => catalog.unitsBySlug.get(slug).backline)), "Automatic pairs include two backline cores as well as two frontline cores");
      assert.equal(progress.length, 6);
      assert.equal(progress.at(-1).pairsTotal, 6);
      assert.equal(progress.at(-1).pairsDone, 6);
      assert.equal(progress.at(-1).expanded, all.expanded);
      assert.equal(progress.at(-1).boardsFound, all.boards.length);
      assert(progress.every((update, index) => !index || (update.pairsDone > progress[index - 1].pairsDone && update.expanded >= progress[index - 1].expanded)));
      const chosen = pool.find(unit => unit.backline).slug;
      const one = await ctx.tftFlexBrowse(catalog, { ...options, cores: [chosen] });
      assert(one.boards.every(b => b.coreCost === cost), "One-core browsing retains the requested gold target tier");
      assert(
        one.boards.every(b => ctx.tftFlexLobbyTargets(b).every(target => target.copies === (cost < 4 ? (target.cost === cost ? 9 : 1) : target.cost === 4 ? 3 : 1))),
        "One-core browsing applies star targets to partners and same-tier supports alike"
      );
      assert.equal(one.pairsTotal, 3);
      assert.equal(one.pairsDone, 3);
      const expectedOne = new Map();
      for (const [id, corePairs] of expected) {
        const matching = [...corePairs].filter(key => key.split("|").includes(chosen));
        if (matching.length) expectedOne.set(id, matching);
      }
      assert.deepEqual([...one.boards.map(b => b.id)].sort(), [...expectedOne.keys()].sort());
      assert(one.boards.every(b => b.units.some(unit => unit.slug === chosen) && b.corePairs.every(pair => pair.includes(chosen))), "One selected champion is retained in every result and every generated pair");
      const partners = new Set(one.boards.flatMap(b => b.corePairs.flatMap(pair => pair.filter(slug => slug !== chosen))));
      assert.deepEqual([...partners].sort(), pool.filter(unit => unit.slug !== chosen).map(unit => unit.slug).sort(), "A single core considers every same-cost partner, including its own role");
      const locked = await ctx.tftFlexBrowse(catalog, { ...options, cores: [chosen], locked: ["front-a"] });
      assert(locked.boards.length && locked.boards.every(b => b.units.some(unit => unit.slug === "front-a")));
      const selectedBoard = one.boards[0];
      const swaps = await ctx.tftFlexSwaps(catalog, selectedBoard, { ...options, cores: selectedBoard.cores, maxScoreLoss: 100 });
      assert(
        swaps.swaps.length &&
          swaps.swaps.every(
            swap =>
              selectedBoard.cores.every(slug => swap.board.units.some(unit => unit.slug === slug)) &&
              swap.board.units.filter(unit => unit.cost === 4).length <= (cost === 2 ? 2 : cost === 3 ? 3 : 8)
          ),
        "Browsable results preserve their generated core pair and the tier cap during swaps"
      );
      const repeat = await ctx.tftFlexBrowse(catalog, options);
      assert.deepEqual(plain(repeat), plain(all), "All-pair aggregation and representative cores are deterministic");
    }
    const exactCatalog = ctx.tftFlexCatalog(multiAnchorFixture({ exactRoster: true }));
    const fullRoster = exactCatalog.units.map(unit => unit.slug);
    const exact = await ctx.tftFlexBrowse(exactCatalog, { cost: 4, cores: [], locked: fullRoster, minFrontline: 3, yieldTask: async () => {} });
    assert.equal(exact.boards.length, 1);
    assert.equal(exact.expanded, 0);
    assert.equal(exact.boards[0].corePairs.length, 6, "A complete owned roster merges every same-tier pair without expanding supports");
    const laterRoster = ["tank2-b", "carry2-b", "tank", "carry", "front-a", "front-b", "duo-a", "back-b"];
    const later = await ctx.tftFlexBrowse(catalog, { cost: 2, cores: [], locked: laterRoster, minFrontline: 3, yieldTask: async () => {} });
    assert.equal(later.boards.length, 1);
    assert.equal(later.expanded, 0);
    assert.deepEqual(plain(later.boards[0].corePairs), [["carry2-b", "tank2-b"]], "Pair-plus-lock overflow is skipped until a later eligible pair can use the exact roster");
    const options = { cost: 2, cores: [], minFrontline: 3, yieldTask: async () => {} };
    const early = await ctx.tftFlexBrowse(catalog, { ...options, cancelled: () => true });
    assert(early.cancelled && early.pairsDone === 0 && early.expanded === 0);
    let stop = false;
    const interrupted = await ctx.tftFlexBrowse(catalog, { ...options, cancelled: () => stop, onProgress: () => { stop = true; } });
    assert(interrupted.cancelled && interrupted.pairsDone === 1 && interrupted.pairsTotal === 6, "Cancellation prevents the next automatic pair search from starting");
    for (const cores of [[], ["carry2-a"], ["carry2-a", "tank2-a"]]) {
      let complete = false;
      const lastCallback = await ctx.tftFlexBrowse(catalog, {
        ...options,
        cores,
        cancelled: () => complete,
        onProgress: update => { if (update.pairsDone === update.pairsTotal) complete = true; },
      });
      assert(complete && lastCallback.cancelled && lastCallback.boards.length === 0, "Cancellation in the final progress callback is honored for zero, one and fixed multi-core selections");
    }
    for (const cores of ["tank2-a,carry2-a", ["missing-core"], ["tank"]]) {
      await assert.rejects(() => ctx.tftFlexBrowse(catalog, { ...options, cores }), /core|champion|array|cost/i);
    }
    await assert.rejects(() => ctx.tftFlexBrowse(catalog, { ...options, minCost: 3 }), /minimum|core|cost/i);
    await assert.rejects(() => ctx.tftFlexBrowse(catalog, { ...options, locked: ["tank", "carry", "four-front-a"] }), /4-cost/i);
    assert.equal(JSON.stringify(raw), snapshot, "Automatic pair and partner browsing preserve the published catalog");
  }

  async function checkCorePickerInitial() {
    assert.equal(algorithmContext.tflex.cost, 4);
    assert.deepEqual(plain(algorithmContext.tflex.cores), [], "The real interface starts with no fixed core champions");
    const h = interfaceHarness({ cores: [] });
    const ctx = h.context;
    const original = ctx.tftFlexSearch;
    ctx.api = async () => multiAnchorFixture();
    let searched = 0;
    ctx.tftFlexSearch = (...args) => { ++searched; return original(...args); };
    await ctx.loadTftFlex();
    assert.equal(searched, 6);
    assert(h.flex.boards.length && !h.flex.error, "An empty initial selection automatically browses every same-cost pair");
    assert.equal(h.flex.pairsTotal, 6);
    assert.equal(h.flex.pairsDone, 6);
    assert(h.element("tft-flex-status").textContent.includes("across all 4-cost core pairs"));
    assert(h.element("tft-flex-selected").textContent.includes("All 4-cost core pairs"));
    const { board: b, card } = await openFirstBoard(h);
    let swapOptions;
    const originalSwaps = ctx.tftFlexSwaps;
    ctx.tftFlexSwaps = (catalog, selected, options) => {
      swapOptions = options;
      return originalSwaps(catalog, selected, options);
    };
    const details = card.querySelector(".tft-flex-swaps");
    details.open = true;
    await details.fire("toggle");
    assert.deepEqual(plain(swapOptions.cores), plain(b.cores), "An automatically browsed board uses its own generated core pair for support swaps");
    assert(details.querySelector("table"));
    await toggleCore(h, "carry");
    assert.deepEqual(plain(h.flex.cores), ["carry"]);
    assert.equal(searched, 9);
    assert(h.flex.boards.length && h.flex.boards.every(entry => entry.units.some(unit => unit.slug === "carry") && entry.corePairs.every(pair => pair.includes("carry"))));
    assert.equal(h.flex.pairsTotal, 3);
    assert(h.element("tft-flex-status").textContent.includes("including CARRY"));
    assert(h.element("tft-flex-selected").textContent.includes("all same-cost partners"));
    assert.equal(coreButton(h, "carry").attributes["aria-pressed"], "true");
    const beforeFilter = searched;
    h.element("tft-flex-search").value = "tank";
    await h.element("tft-flex-search").fire("input");
    const visible = h.element("tft-flex-unit-seg").querySelectorAll("button");
    assert(visible.length && visible.every(button => h.flex.catalog.unitsBySlug.get(button.dataset.key).name.includes("TANK")));
    assert.equal(searched, beforeFilter, "Portrait filtering does not run another composition search or restrict the partner pool");
    assert(h.element("tft-flex-selected").textContent.includes("CARRY"), "Filtering portraits does not drop the fixed champion");
    await toggleCore(h, "tank");
    assert.equal(searched, 10);
    assert(h.flex.boards.length && !h.flex.error);
    assert.deepEqual(plain(h.flex.cores), ["carry", "tank"]);
    assert(h.flex.boards.every(entry => plain(entry.cores).join("|") === "carry|tank"), "Two selected champions remain fixed instead of continuing partner browsing");
    assert(!h.element("tft-flex-lock-list").children.some(label => /Keep (?:CARRY|TANK)$/.test(label.querySelector("input")?.attributes["aria-label"] || "")));
    h.element("tft-flex-search").value = "unique";
    await h.element("tft-flex-search").fire("input");
    assert.deepEqual(h.element("tft-flex-unit-seg").querySelectorAll("button").map(button => button.dataset.key).sort(), ["carry", "tank"]);
    await toggleCore(h, "carry");
    assert(h.flex.boards.length && h.flex.boards.every(entry => entry.units.some(unit => unit.slug === "tank")) && !h.flex.error);
    assert(h.element("tft-flex-status").textContent.includes("including TANK"), "Removing one of two selections returns to all partners for the remaining champion");
    await toggleCore(h, "tank");
    assert.deepEqual(plain(h.flex.cores), []);
    assert(h.flex.boards.length && !h.flex.error && h.element("tft-flex-status").textContent.includes("across all 4-cost core pairs"));
  }

  async function checkInitialBrowseProgress() {
    const h = interfaceHarness({ cores: [] });
    const ctx = h.context;
    const raw = multiAnchorFixture();
    ctx.api = async () => raw;
    let options;
    let finish;
    ctx.tftFlexBrowse = (_, requested) => {
      options = requested;
      return new Promise(resolve => { finish = resolve; });
    };
    const loading = ctx.loadTftFlex();
    await until(() => typeof finish === "function", "The initial all-pair browse starts after the catalog arrives");
    assert(!h.flex.loading && h.flex.searching, "Catalog loading finishes before the potentially longer pair search");
    assert(h.element("tft-flex-status").textContent.includes("Finding level 8 comps across all 4-cost core pairs"));
    options.onProgress({ pairsDone: 1, pairsTotal: 6, expanded: 10, boardsFound: 1 });
    assert.equal(h.flex.pairsDone, 1);
    assert.equal(h.flex.pairsTotal, 6);
    assert(h.element("tft-flex-status").textContent.includes("1/6 core groups checked") && !h.element("tft-flex-status").textContent.includes("Loading"));
    const b = ctx.tftFlexEvaluate(
      h.flex.catalog,
      ["tank", "carry", "front-a", "front-b", "front-d", "duo-a", "duo-b", "back-b"],
      { cost: 4, cores: ["carry", "tank"], minFrontline: 3 }
    );
    b.corePairs = [["carry", "tank"]];
    finish({ boards: [b], expanded: 10, pairsDone: 6, pairsTotal: 6, cancelled: false, truncated: false });
    await loading;
    assert(!h.flex.loading && !h.flex.searching && h.flex.boards.length === 1 && !h.flex.error);
  }

  async function checkCoreSelectionCancellation() {
    let latest;
    for (const previousCores of [[], ["carry"]]) {
      const h = interfaceHarness({ cores: previousCores });
      const ctx = h.context;
      ctx.api = async () => multiAnchorFixture();
      await ctx.loadTftFlex();
      const oldBoards = h.flex.boards;
      const original = ctx.tftFlexBrowse;
      assert(oldBoards.length);
      let finish;
      let options;
      let delayNext = true;
      ctx.tftFlexBrowse = (catalog, requested) => {
        if (!delayNext) return original(catalog, requested);
        delayNext = false;
        options = requested;
        return new Promise(resolve => { finish = resolve; });
      };
      const pending = ctx.runTftFlexSearch({ force: true });
      const request = h.flex.searchReq;
      assert(h.flex.searching && typeof finish === "function");
      assert.deepEqual(plain(options.cores), previousCores);
      if (!h.flex.cores.includes("carry")) await toggleCore(h, "carry");
      await toggleCore(h, "tank");
      const accepted = h.flex.boards;
      const key = h.flex.key;
      const progress = [h.flex.pairsDone, h.flex.pairsTotal];
      assert(accepted.length && h.flex.searchReq > request && !h.flex.searching && !h.flex.error);
      assert.deepEqual(plain(h.flex.cores), ["carry", "tank"]);
      options.onProgress({ pairsDone: 999, pairsTotal: 999, expanded: 999, boardsFound: 999 });
      assert.deepEqual([h.flex.pairsDone, h.flex.pairsTotal], progress, "Late progress from the older automatic browse cannot replace newer progress");
      finish({
        boards: oldBoards,
        expanded: 100,
        pairsDone: previousCores.length ? 3 : 6,
        pairsTotal: previousCores.length ? 3 : 6,
        cancelled: false,
        truncated: false,
      });
      await pending;
      assert.equal(h.flex.boards, accepted, "Older empty/one-selection results cannot overwrite a newer fixed-core search");
      assert.equal(h.flex.key, key);
      assert(!h.flex.searching && !h.flex.error);
      assert(h.element("tft-flex-status").textContent.includes("for 2 selected core champions"));
      latest = h;
    }
    const h = latest;
    const ctx = h.context;
    await toggleCore(h, "tank");
    const oldBoards = h.flex.boards;
    let finish;
    let options;
    ctx.tftFlexBrowse = (_, requested) => {
      options = requested;
      return new Promise(resolve => { finish = resolve; });
    };
    const pending = ctx.runTftFlexSearch({ force: true });
    const request = h.flex.searchReq;
    assert.deepEqual(plain(options.cores), ["carry"]);
    ctx.cancelTftFlex();
    assert(h.flex.searchReq > request && !h.flex.searching);
    options.onProgress({ pairsDone: 3, pairsTotal: 3, expanded: 100, boardsFound: 10 });
    finish({ boards: oldBoards, expanded: 100, pairsDone: 3, pairsTotal: 3, cancelled: false, truncated: false });
    await pending;
    assert.equal(h.flex.boards.length, 0);
    assert.equal(h.flex.key, null);
    assert.equal(h.flex.cards.size, 0);
    assert(!h.flex.searching && !h.flex.error, "Cancelling a pending one-core browse prevents its late result from restoring stale boards");
  }

  async function checkCoreCostInterface() {
    const h = interfaceHarness();
    const ctx = h.context;
    assert(INDEX_HTML.includes('for="tft-flex-cost"'), "The core-cost selector has an associated label");
    assert.deepEqual(h.element("tft-flex-cost").children.map(option => option.value), ["2", "3", "4"]);
    ctx.api = async () => coreCostFixture();
    await ctx.loadTftFlex();
    assert.equal(h.flex.cost, 4);
    assert.equal(h.element("tft-flex-cost").value, "4");
    assert.equal(h.element("tft-flex-cost").disabled, false);
    const change = async (id, value) => {
      h.element(id).value = String(value);
      await h.element(id).fire("change");
      await until(() => !h.flex.searching && !h.flex.loading, `Changing ${id} completes a bounded trait search`);
    };
    await change("tft-flex-min-cost", 3);
    h.element("tft-flex-search").value = "tank";
    await h.element("tft-flex-search").fire("input");
    const request = h.flex.searchReq;
    await change("tft-flex-cost", 2);
    assert.equal(h.flex.cost, 2);
    assert(h.flex.searchReq > request);
    assert.equal(h.element("tft-flex-search").value, "", "Changing the core tier clears the previous portrait-name filter");
    assert.deepEqual(plain(h.flex.cores), [], "Changing cost clears core selections from the previous tier");
    assert(
      h.flex.boards.length && !h.flex.error && h.flex.boards.every(b => b.cores.every(slug => h.flex.catalog.unitsBySlug.get(slug).cost === 2)),
      "The cleared two-cost selection automatically browses valid core pairs for its new tier"
    );
    assert.equal(h.flex.minCost, 2);
    assert.equal(h.element("tft-flex-min-cost").value, "2");
    assert(h.element("tft-flex-min-cost").children.find(option => option.value === "3").disabled);
    const portraits = h.element("tft-flex-unit-seg").querySelectorAll("button");
    assert.equal(portraits.length, 4);
    assert(portraits.every(button => h.flex.catalog.unitsBySlug.get(button.dataset.key).cost === 2), "Both frontline and backline portraits use the chosen core tier");
    await toggleCore(h, "carry2-a");
    await toggleCore(h, "carry2-b");
    const fourCount = b => b.units.filter(unit => unit.cost === 4).length;
    assert(
      h.flex.boards.length &&
        h.flex.boards.every(b => fourCount(b) <= 2 && plain(b.cores).join("|") === "carry2-a|carry2-b" && b.frontlineCount >= 3),
      "Two backline cores are legal and supports supply the whole-board frontline minimum"
    );
    assert(h.element("tft-flex-context").textContent.includes("At most 2 4-cost units"));
    const { card, board: b } = await openFirstBoard(h);
    const swaps = card.querySelector(".tft-flex-swaps");
    const originalSwapSearch = ctx.tftFlexSwaps;
    let captured;
    ctx.tftFlexSwaps = (catalog, selected, options) => {
      captured = options;
      return originalSwapSearch(catalog, selected, options);
    };
    swaps.open = true;
    await swaps.fire("toggle");
    assert.equal(captured.cost, 2);
    assert.deepEqual(plain(captured.cores), plain(b.cores));
    assert(swaps.querySelector("table"), "Same-role lower-cost cores can still open interchangeable support packages");
    const lock = slug =>
      h.element("tft-flex-lock-list")
        .children.find(label => label.querySelector("input")?.attributes["aria-label"] === `Keep ${h.flex.catalog.unitsBySlug.get(slug).name}`)
        ?.querySelector("input");
    const first = lock("four-front-a");
    const second = lock("four-front-b");
    const third = lock("four-back-a");
    assert(first && second && third && !third.disabled);
    first.checked = true;
    await first.fire("change");
    await until(() => !h.flex.searching, "The first owned four-cost search completes");
    second.checked = true;
    await second.fire("change");
    await until(() => !h.flex.searching, "The second owned four-cost search completes");
    assert.equal(lock("four-back-a"), third, "Changing a lock preserves the existing controls");
    assert(third.disabled && !first.disabled && !second.disabled);
    const promoted = lock("tank2-a");
    promoted.checked = true;
    await promoted.fire("change");
    await until(() => !h.flex.searching, "The kept two-cost supporter search completes");
    await toggleCore(h, "tank2-a");
    assert(!h.flex.locked.includes("tank2-a") && !lock("tank2-a"), "Promoting a kept supporter to the core removes its redundant lock");
    assert.deepEqual(plain(h.flex.cores), ["carry2-a", "carry2-b", "tank2-a"]);
    assert(
      h.flex.boards.length &&
        h.flex.boards.every(entry => fourCount(entry) === 2 && entry.cores.length === 3 && ["four-front-a", "four-front-b"].every(slug => entry.units.some(unit => unit.slug === slug)))
    );
    const retained = lock("tank2-b");
    retained.checked = true;
    await retained.fire("change");
    await until(() => !h.flex.searching, "The other kept supporter remains available");
    h.base.location.hash = "";
    ctx.restoringHash = false;
    ctx.restoredHash = null;
    ctx.updateHash();
    const hash = h.base.location.hash;
    assert(hash.includes("fcost=2") && hash.includes("fmincost=2") && hash.includes("fcore="));
    assert(!hash.includes("ftank=") && !hash.includes("fcarry="), "Generic shared links do not impose role names");
    const saved = Object.fromEntries(hash.slice(1).split("&").map(part => part.split("=").map(decodeURIComponent)));
    assert.equal(saved.fcore, "carry2-a,carry2-b,tank2-a", "All selected core slugs survive in deterministic shared-link order");
    const restored = interfaceHarness({ cores: [] });
    restored.context.api = async () => coreCostFixture();
    restored.context.restoreTftFlexHash(saved);
    await restored.context.loadTftFlex();
    assert.equal(restored.flex.cost, 2);
    assert.equal(restored.flex.minCost, 2);
    assert.deepEqual(plain(restored.flex.cores), ["carry2-a", "carry2-b", "tank2-a"]);
    assert.deepEqual(plain(restored.flex.locked), ["four-front-a", "four-front-b", "tank2-b"]);
    assert(restored.flex.boards.length && restored.flex.boards.every(entry => fourCount(entry) === 2));
    const clamped = interfaceHarness({ cores: [] });
    clamped.context.api = async () => coreCostFixture();
    clamped.context.restoreTftFlexHash({ fcost: "2", fmincost: "3", fcore: "tank,carry" });
    assert.equal(clamped.flex.minCost, 2);
    await clamped.context.loadTftFlex();
    assert.deepEqual(plain(clamped.flex.cores), []);
    assert(clamped.flex.boards.length && !clamped.flex.error, "Wrong-tier saved cores clear to automatic browsing with a compatible minimum");
    const before = h.flex.searchReq;
    await change("tft-flex-cost", 3);
    assert.equal(h.flex.cost, 3);
    assert.equal(h.flex.minCost, 2);
    assert.deepEqual(plain(h.flex.cores), []);
    assert(h.flex.boards.length && h.flex.boards.every(entry => entry.cores.every(slug => h.flex.catalog.unitsBySlug.get(slug).cost === 3)));
    assert(h.flex.searchReq > before && !h.flex.error);
    assert.deepEqual(plain(h.flex.locked), ["four-front-a", "four-front-b", "tank2-b"], "Other-tier kept supporters survive a core-tier change");
    assert(!h.element("tft-flex-min-cost").children.find(option => option.value === "3").disabled);
    await toggleCore(h, "tank3-a");
    await toggleCore(h, "tank3-b");
    assert(!lock("four-back-a").disabled, "Three-cost cores permit a third owned four-cost");
    assert(h.flex.boards.length && h.flex.boards.every(entry => fourCount(entry) <= 3 && plain(entry.cores).join("|") === "tank3-a|tank3-b"));
    await change("tft-flex-cost", 4);
    assert.deepEqual(plain(h.flex.cores), []);
    assert(h.flex.boards.length);
    await toggleCore(h, "tank");
    await toggleCore(h, "carry");
    assert(h.flex.boards.some(entry => fourCount(entry) > 3), "Returning to four-cost cores restores uncapped support packages");
    assert(!lock("four-back-a").disabled);
    assert.equal(h.element("tft-flex-gold-target-note").hidden, true);
  }

  async function checkCoreGoldTargets() {
    const h = interfaceHarness();
    const ctx = h.context;
    const raw = coreCostFixture();
    const catalog = ctx.tftFlexCatalog(raw);
    catalog.shop = {
      costOdds: [0.15, 0.20, 0.32, 0.30, 0.03],
      championCounts: [1, 2, 3, 4, 5].map(cost => catalog.units.filter(unit => unit.cost === cost).length),
      bagCopies: [30, 25, 18, 10, 9],
      refreshCost: 2,
      visibleSlots: [1],
    };
    h.flex.catalog = catalog;
    const sortBoards = [];
    for (const cost of [2, 3]) {
      const otherCost = cost === 2 ? 3 : 2;
      const cores = [`tank${cost}-a`, `carry${cost}-a`];
      const support = `tank${cost}-b`;
      const roster = [...cores, support, "four-front-a", `carry${otherCost}-a`, "front-a", "front-b", "duo-a"];
      const b = ctx.tftFlexEvaluate(catalog, roster, { cost, cores, minFrontline: 3 });
      assert(b.valid && b.coreCost === cost);
      const targets = plain(ctx.tftFlexLobbyTargets(b));
      const target = slug => targets.find(unit => unit.api === slug);
      assert.equal(target(support).copies, 9, "A support of the selected tier has the same three-star target as the cores");
      assert.equal(target(cores[0]).copies, 9);
      assert.equal(target(cores[1]).copies, 9);
      assert.equal(target("four-front-a").copies, 1, "A four-cost support in a reroll profile needs one copy");
      assert.equal(target(`carry${otherCost}-a`).copies, 1, "A support from the other reroll tier stays a one-copy target");
      assert.equal(target("front-a").copies, 1);
      const counts = plain(ctx.tftFlexGoldRequirements(b));
      assert.deepEqual(counts, cost === 2 ? [3, 3, 1, 1, 0] : [3, 1, 3, 1, 0], "Baseline requirements count distinct champions rather than replacing counts with required copies");
      const pool = catalog.units.map(unit => ({ api: unit.api, cost: unit.cost, heldCopies: 0 }));
      const summary = ctx.tftFlexLobbyEstimate(catalog.shop, pool, targets, { signatureOnly: true });
      assert.deepEqual(plain(summary.copiesByCost), cost === 2 ? [3, 27, 1, 1, 0] : [3, 1, 27, 1, 0]);
      assert.equal(summary.purchaseGold, cost === 2 ? 64 : 90);
      assert.equal(ctx.tftFlexSortValue(b, "purchaseGold"), cost === 2 ? 64 : 90, "Purchase-gold sorting includes all three-star selected-tier units");
      sortBoards.push({ ...b, id: String(cost) });
      h.flex.cost = 4;
      assert.equal(ctx.tftFlexGoldCoreCost(b), cost, "Changing the picker cannot change an existing board's gold target tier");
      assert.deepEqual(plain(ctx.tftFlexLobbyTargets(b)), targets);
      const legacy = { ...b, mainTank: cores[0] };
      delete legacy.coreCost;
      delete legacy.cores;
      assert.equal(ctx.tftFlexGoldCoreCost(legacy), cost, "Legacy boards can derive their tier from their original anchor");
      const explicitLegacy = { ...b };
      delete explicitLegacy.coreCost;
      assert.equal(ctx.tftFlexGoldCoreCost(explicitLegacy), cost, "Earlier explicit-core boards derive their saved core tier");
      const tightPool = pool.map(unit => ({
        ...unit,
        heldCopies: unit.api === support ? catalog.shop.bagCopies[cost - 1] - 8 : unit.api === "four-front-a" ? 9 : 0,
      }));
      const unavailable = ctx.tftFlexLobbyEstimate(catalog.shop, tightPool, targets, { signatureOnly: true });
      assert.deepEqual(
        plain(unavailable.unavailableTargets),
        [{ api: support, cost, available: 8, required: 9, reason: "insufficient-copies" }],
        "Eight available support copies cannot complete a selected-tier three-star, while one four-cost copy is enough"
      );
    }
    const four = ctx.tftFlexEvaluate(catalog, ["tank", "carry", "four-front-a", "front-a", "front-b", "front-d", "duo-a", "duo-b"], settings);
    assert.equal(four.coreCost, 4);
    assert(ctx.tftFlexLobbyTargets(four).every(target => target.copies === (target.cost === 4 ? 3 : 1)), "The four-cost profile still requires three copies of every four-cost and one of other tiers");
    sortBoards.push({ ...four, id: "4" });
    assert.deepEqual(plain(ctx.tftFlexSortedBoards(sortBoards, "purchaseGold", "asc")).map(b => b.id), ["4", "2", "3"]);
    assert.equal(ctx.tftFlexGoldCoreCost({ units: [{ cost: 2 }] }), 4, "Old unanchored test boards retain the four-cost default");

    for (const cost of [2, 3]) {
      const shop = {
        costOdds: [0, 0, 0, 0, 0],
        championCounts: [0, 0, 0, 1, 0],
        bagCopies: [30, 9, 9, 1, 9],
        refreshCost: 2,
        visibleSlots: [1],
      };
      shop.costOdds[cost - 1] = 0.5;
      shop.costOdds[3] = 0.5;
      shop.championCounts[cost - 1] = 1;
      const counts = [0, 0, 0, 1, 0];
      counts[cost - 1] = 1;
      const baseline = ctx.tftFlexGoldEstimate(shop, counts, cost);
      const lobby = ctx.tftFlexLobbyEstimate(
        shop,
        [{ api: "core", cost, heldCopies: 0 }, { api: "support", cost: 4, heldCopies: 0 }],
        [{ api: "core", cost, copies: 9 }, { api: "support", cost: 4, copies: 1 }]
      );
      assert.equal(baseline.expectedShops, 10, "With one visible slot and exactly the ten desired copies in the pool, completing the targets takes ten paid shops");
      assert.equal(baseline.refreshGold, 20);
      assert.equal(baseline.purchaseGold, 9 * cost + 4);
      assert.equal(baseline.totalGold, 24 + 9 * cost);
      assert.equal(lobby.totalGold, baseline.totalGold, "The zero-held lobby and baseline use the same nine-copy target");
      assert.deepEqual(plain(baseline.copiesByCost), plain(lobby.copiesByCost));
    }
  }

  async function checkCoreGoldWorkers() {
    const h = interfaceHarness();
    const ctx = h.context;
    const { workers } = installGoldWorkers(h);
    const shop = {
      costOdds: [0, 0.3, 0.3, 0.4, 0],
      championCounts: [0, 1, 1, 1, 0],
      bagCopies: [30, 25, 18, 10, 9],
      refreshCost: 2,
      visibleSlots: [1],
    };
    const counts = [0, 1, 1, 1, 0];
    const two = ctx.tftFlexGoldRequest(shop, counts, 2);
    const repeatedTwo = ctx.tftFlexGoldRequest(shop, counts, 2);
    const three = ctx.tftFlexGoldRequest(shop, counts, 3);
    const four = ctx.tftFlexGoldRequest(shop, counts, 4);
    assert.equal(two, repeatedTwo, "Only requests with the same roster counts and target tier share a pending calculation");
    const estimates = await Promise.all([two, three, four]);
    assert.deepEqual(estimates.map(est => est.purchaseGold), [25, 33, 17], "The same champion-count vector produces separate two-, three- and four-cost purchase targets");
    assert.deepEqual(estimates.map(est => plain(est.copiesByCost)), [[0, 9, 1, 1, 0], [0, 1, 9, 1, 0], [0, 1, 1, 3, 0]]);
    assert.equal(workers.length, 1);
    assert.equal(workers[0].messages.length, 3);
    assert.deepEqual(workers[0].messages.map(message => message.coreCost), [2, 3, 4], "The isolated worker receives the saved core tier along with unchanged champion counts");
    for (const cost of [2, 3, 4]) {
      const est = await ctx.tftFlexGoldRequest(shop, counts, cost);
      assert.equal(est, estimates[cost - 2], "Completed results are cached separately by target tier");
    }
    assert.equal(workers[0].messages.length, 3, "Reading any completed tier does not schedule another job");
    ctx.cancelTftFlexGold();
  }

  async function checkCoreGoldInterface() {
    for (const cost of [2, 3]) {
      const h = interfaceHarness({ cores: [`tank${cost}-a`, `carry${cost}-a`] });
      const ctx = h.context;
      const raw = coreCostFixture();
      const sameTierSupport = `tank${cost}-b`;
      const otherTierSupport = `carry${cost === 2 ? 3 : 2}-a`;
      h.flex.cost = cost;
      h.flex.locked = [sameTierSupport, otherTierSupport, "four-front-a", "front-a", "front-b", "duo-a"];
      const available = new Set([...h.flex.cores, ...h.flex.locked, `carry${cost}-b`]);
      raw.units = raw.units.filter(unit => available.has(unit.slug));
      raw.shop = {
        costOdds: [0.15, 0.20, 0.32, 0.30, 0.03],
        championCounts: [1, 2, 3, 4, 5].map(tier => raw.units.filter(unit => unit.cost === tier).length),
        bagCopies: [30, 25, 18, 10, 9],
        refreshCost: 2,
        visibleSlots: [1],
      };
      const requests = [];
      ctx.tftFlexGoldRequest = (_shop, counts, coreCost) => {
        requests.push({ counts: plain(counts), coreCost });
        const copiesByCost = counts.map((count, index) => count * (index + 1 === cost ? 9 : 1));
        const purchaseGold = copiesByCost.reduce((sum, count, index) => sum + count * (index + 1), 0);
        return Promise.resolve({ copiesByCost, purchaseGold, refreshGold: 200, totalGold: 200 + purchaseGold });
      };
      ctx.api = async () => raw;
      await ctx.loadTftFlex();
      await until(() => h.flex.boards.every(b => b.gold), "The selected-tier baseline request completes");
      assert.equal(h.flex.boards.length, 1);
      assert.equal(h.flex.boards[0].gold.purchaseGold, cost === 2 ? 64 : 90);
      assert(requests.length && requests.every(req => req.coreCost === cost), "Board baseline jobs pass the selected tier separately from unit counts");
      const note = h.element("tft-flex-gold-target-note");
      assert.equal(note.hidden, false);
      assert(note.textContent.includes("9") && note.textContent.includes("3\u2605"));
      const shopRows = h.element("tft-flex-shop-table").querySelector("tbody").children;
      assert(shopRows[cost - 1].children[4].textContent.includes("9") && shopRows[cost - 1].children[4].textContent.includes("3\u2605"));
      assert(shopRows[3].children[4].textContent.includes("1") && !shopRows[3].children[4].textContent.includes("2\u2605"), "The method table requires one four-cost copy in a lower-cost profile");
      const { board: b, card } = await openFirstBoard(h);
      const tiles = card.querySelectorAll(".tft-comp-unit");
      const tile = slug => tiles.find(node => node.querySelector("button").dataset.unit === slug);
      assert(tile(sameTierSupport).textContent.includes("3\u2605 target") && tile(sameTierSupport).dataset.assignment === "support", "The card shows a selected-tier support at three stars");
      assert(tile(b.cores[0]).textContent.includes("3\u2605 target"));
      assert(tile("four-front-a").textContent.includes("1 copy") && !tile("four-front-a").textContent.includes("2\u2605"));
      assert(tile(otherTierSupport).textContent.includes("1 copy"));
      const tooltip = (slug, selectedBoard = b) =>
        ctx.tftFlexUnitTooltip(h.flex.catalog.unitsBySlug.get(slug), selectedBoard).map(node => node.textContent).join(" ");
      assert(tooltip(sameTierSupport).includes("9 copies for 3\u2605"));
      assert(tooltip("four-front-a").includes("1 copy") && !tooltip("four-front-a").includes("3 copies"));
      assert(tooltip(b.cores[0], null).includes("9 copies for 3\u2605"), "Picker tooltips use the active profile before there is a board");
      h.flex.locked = h.flex.locked.filter(slug => slug !== sameTierSupport);
      const searchSwaps = ctx.tftFlexSwaps;
      let swapResult;
      ctx.tftFlexSwaps = async (...args) => { swapResult = await searchSwaps(...args); return swapResult; };
      const details = card.querySelector(".tft-flex-swaps");
      details.open = true;
      await details.fire("toggle");
      await until(() => !details.textContent.includes("Calculating\u2026"), "Support swap gold requests complete");
      assert(swapResult.swaps.length && swapResult.swaps.every(swap => swap.board.coreCost === cost));
      const sameTierReplacement = swapResult.swaps.find(swap => swap.added.some(unit => unit.cost === cost));
      assert(sameTierReplacement, "The legal support swaps include another selected-tier champion");
      assert(
        ctx.tftFlexLobbyTargets(sameTierReplacement.board).some(
          target => sameTierReplacement.added.some(unit => unit.api === target.api && unit.cost === cost) && target.copies === 9
        ),
        "A newly added selected-tier support carries its own nine-copy requirement"
      );
      assert(requests.length > 1 && requests.every(req => req.coreCost === cost), "Before and after swap jobs retain the board's gold tier");
      assert(
        details.textContent.includes("Expected gold") &&
          details.querySelector("tbody").children.every(row => /\d+g/.test(row.children[4].textContent) && !row.children[4].textContent.includes("Unavailable")),
        "Whole-board baseline gold remains available for the selected-tier support swaps"
      );
      h.flex.cost = 4;
      const retainedCard = ctx.tftFlexBoardCard(b, 1);
      assert(
        retainedCard
          .querySelectorAll(".tft-comp-unit")
          .find(node => node.querySelector("button").dataset.unit === sameTierSupport)
          .textContent.includes("3\u2605 target"),
        "Rendering a saved board uses its stamped tier even after the picker moves to another tier"
      );
      assert(tooltip(sameTierSupport).includes("9 copies for 3\u2605"));
      ctx.cancelTftFlex();
    }
  }

  async function checkGoldInterface() {
    const setup = () => {
      const h = interfaceHarness();
      const { workers } = installGoldWorkers(h);
      const raw = fixture();
      raw.shop = {
        costOdds: [0.15, 0.20, 0.32, 0.30, 0.03],
        championCounts: [14, 13, 14, 14, 10],
        bagCopies: [30, 25, 18, 10, 9],
        refreshCost: 2,
        visibleSlots: [4, 5],
        sources: [],
      };
      raw.units.find(unit => unit.slug === "front-a").cost = 4;
      h.flex.cores = ["tank", "carry"];
      h.flex.locked = ["front-a"];
      h.context.api = async () => raw;
      return { ...h, workers };
    };
    const h = setup();
    await h.context.loadTftFlex();
    const { card, board: opened } = await openFirstBoard(h);
    const swaps = card.querySelector(".tft-flex-swaps");
    swaps.open = true;
    assert(h.element("tft-flex-results").textContent.includes("Calculating expected gold"), "Boards remain available while gold calculates in the worker");
    await until(() => h.flex.boards.every(b => b.gold), "Gold worker finishes calculating board expectations");
    assert(h.flex.boards.every(b => b.gold && b.gold.totalGold > b.gold.purchaseGold));
    assert.equal(h.flex.cards.get(opened.id), card, "Gold updates preserve the existing interactive board nodes");
    assert.equal(swaps.open, true, "Gold updates leave an expanded swap section open");
    assert(h.element("tft-flex-results").textContent.includes("Expected gold to hit"));
    for (const b of h.flex.boards) {
      const fourCosts = b.units.filter(unit => unit.cost === 4).length;
      assert.equal(b.gold.copiesByCost[3], fourCosts * 3, "Four-cost supports need three copies as well as both anchors");
      assert.equal(b.gold.purchaseGold, b.units.reduce((sum, unit) => sum + unit.cost * (unit.cost === 4 ? 3 : 1), 0));
    }
    await swaps.fire("toggle");
    await until(() => !swaps.textContent.includes("Calculating\u2026"), "Support swap gold requests complete");
    assert(swaps.textContent.includes("Expected gold") && /g \([+\u2212]?\d+g\)/.test(swaps.textContent), "Support swaps compare whole-roster gold and its change");
    assert(!swaps.textContent.includes("Calculating\u2026"));
    assert.equal(h.workers.length, 1, "One worker handles all distinct cost profiles");
    const requestsBefore = h.calls.length;
    h.context.cancelTftFlex();
    assert(h.workers[0].terminated, "Leaving the view cancels background gold work and terminates its worker");
    await h.context.loadTftFlex();
    assert.equal(h.workers.length, 1, "Completed expectations are reused from the small result cache");
    assert.equal(h.calls.length, requestsBefore, "Gold work does not fetch additional combat or shop endpoints");
    const cancelled = setup();
    await cancelled.context.loadTftFlex();
    cancelled.context.cancelTftFlex();
    await new Promise(resolve => setTimeout(resolve, 10));
    assert(cancelled.workers.every(worker => worker.terminated));
    assert(!cancelled.flex.boards.some(b => b.gold), "Cancelled jobs cannot fill old boards with late results");
  }

  async function checkGoldSortCompletion() {
    const prepare = async (count = 31) => {
      const h = interfaceHarness();
      await h.context.loadTftFlex();
      h.flex.catalog.shop = {
        costOdds: [0.15, 0.20, 0.32, 0.30, 0.03],
        championCounts: [14, 13, 14, 14, 10],
        bagCopies: [30, 25, 18, 10, 9],
        refreshCost: 2,
        visibleSlots: [4, 5],
      };
      const baseBoard = h.flex.boards[0];
      h.flex.boards = Array.from({ length: count }, (_, index) => ({ ...baseBoard, id: `async-${index}`, score: count - index }));
      h.flex.sort = "gold";
      h.flex.order = "asc";
      h.flex.page = 1;
      h.flex.cards = new Map();
      h.flex.openBoards = new Set();
      const pending = [];
      h.context.tftFlexGoldRequest = () => new Promise(resolve => pending.push(resolve));
      const originalRender = h.context.renderTftFlex;
      let renders = 0;
      h.context.renderTftFlex = () => { ++renders; return originalRender(); };
      h.context.renderTftFlex();
      const completion = h.context.beginTftFlexGold(h.flex.catalog, h.flex.boards, h.flex.searchReq);
      const gold = totalGold => ({ totalGold, refreshGold: totalGold - 36, purchaseGold: 36, expectedShops: (totalGold - 36) / 2 });
      return { ...h, pending, completion, gold, renders: () => renders };
    };
    const h = await prepare();
    const originalTable = h.element("tft-flex-results").children[0];
    const initialRenders = h.renders();
    const opened = await openFirstBoard(h);
    const rowIds = () => h.element("tft-flex-results").querySelectorAll(".tft-flex-summary").map(row => row.dataset.board);
    const before = rowIds();
    h.pending[30](h.gold(60));
    await Promise.resolve();
    await Promise.resolve();
    assert(h.flex.boards[30].gold && !before.includes("async-30"), "An off-page result receives its gold estimate");
    assert.equal(h.renders(), initialRenders);
    assert.equal(h.element("tft-flex-results").children[0], originalTable);
    assert.deepEqual(rowIds(), before, "An early cheap estimate does not reorder incomplete gold results");
    for (let index = 0; index < 29; ++index) h.pending[index](h.gold(100 + index));
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(h.renders(), initialRenders, "Rows stay stable until the complete batch settles");
    h.pending[29](h.gold(129));
    await h.completion;
    assert.equal(h.renders(), initialRenders + 1, "The completed batch performs one final gold reorder");
    assert.equal(rowIds()[0], "async-30", "The cheapest board in the whole pool rises onto the first page");
    assert(h.flex.boards.every(b => b.gold));
    assert.equal(h.flex.cards.get(opened.board.id), opened.card);
    assert.equal(h.element("tft-flex-results").querySelector(".tft-flex-card"), opened.card, "Final gold ordering keeps expanded detail nodes");
    assert(!h.element("tft-flex-status").textContent.includes("Calculating gold"));
    const cancelled = await prepare(4);
    const cancelledTable = cancelled.element("tft-flex-results").children[0];
    const beforeCancel = cancelled.renders();
    cancelled.context.cancelTftFlex();
    cancelled.pending.forEach((resolve, index) => resolve(cancelled.gold(100 + index)));
    await cancelled.completion;
    assert.equal(cancelled.renders(), beforeCancel);
    assert.equal(cancelled.element("tft-flex-results").children[0], cancelledTable, "A cancelled batch cannot perform a final reorder of old results");
    assert(!cancelled.flex.boards.some(b => b.gold), "Late cancelled estimates do not update board data");
    const changedSort = await prepare(4);
    changedSort.context.setTftFlexSort("score");
    const afterChange = changedSort.renders();
    changedSort.pending.forEach((resolve, index) => resolve(changedSort.gold(100 + index)));
    await changedSort.completion;
    assert.equal(changedSort.renders(), afterChange, "A pending gold batch respects a newer sort choice when it finishes");
    assert.equal(changedSort.flex.sort, "score");
    assert(changedSort.flex.boards.every(b => b.gold));
  }

  async function checkLobbyPools() {
    const h = interfaceHarness();
    const ctx = h.context;
    const raw = lobbyFixture();
    const snapshot = JSON.stringify(raw);
    const catalog = ctx.tftFlexCatalog(raw);
    const profiles = ctx.tftFlexLobbyProfiles(catalog);
    assert.equal(profiles.length, 2, "Both same-patch rank profiles are available");
    assert.equal(ctx.tftFlexLobbyProfile(catalog).id, "emerald-profile", "The cached default profile is selected initially");
    assert.equal(ctx.tftFlexLobbyProfile(catalog, "gold-profile").id, "gold-profile");
    assert.equal(ctx.tftFlexLobbyProfile(catalog, "missing").id, "emerald-profile", "An unavailable saved selection falls back to the cached default");
    const profile = profiles.find(entry => entry.id === "emerald-profile");
    const pool = ctx.tftFlexLobbyPool(catalog, profile, { players: 7, development: 0.5, overrides: {} });
    assert.equal(pool.length, catalog.units.length, "The estimate includes the full catalog rather than just the target board or selected cost filter");
    const held = (units, slug) => units.find(unit => unit.api === catalog.unitsBySlug.get(slug).api).heldCopies;
    assert.equal(held(pool, "tank"), 5, "Expected holdings round 1.4 x 7 x 0.5 to the nearest integer");
    assert.equal(held(pool, "carry"), 2);
    assert.equal(held(pool, "front-a"), 2);
    assert.equal(held(pool, "duo-a"), 1);
    assert.equal(held(pool, "other-four"), 4, "A non-target champion contributes to its tier's held population");
    assert.equal(held(pool, "elder"), 9, "Inferred holdings never exceed the individual bag size");
    assert(pool.every(unit => Number.isInteger(unit.heldCopies) && unit.heldCopies >= 0 && unit.heldCopies <= catalog.shop.bagCopies[unit.cost - 1]));
    const overrides = ctx.tftFlexLobbyPool(catalog, profile, { players: 7, development: 0.5, overrides: { tank: 0, carry: 20, "other-four": -4 } });
    assert.equal(held(overrides, "tank"), 0, "An explicit zero replaces inferred copies instead of being treated as an empty input");
    assert.equal(held(overrides, "carry"), 10, "Overrides clamp to the target's bag size");
    assert.equal(held(overrides, "other-four"), 0);
    const noPlayers = ctx.tftFlexLobbyPool(catalog, profile, { players: 0, development: 1, overrides: { "other-four": 3 } });
    assert.equal(held(noPlayers, "other-four"), 3, "Scouted total holdings remain explicit even if expected opponent count is zero");
    assert(noPlayers.filter(unit => unit.api !== "other-four").every(unit => unit.heldCopies === 0));
    const noDevelopment = ctx.tftFlexLobbyPool(catalog, profile, { players: 7, development: 0, overrides: {} });
    assert(noDevelopment.every(unit => unit.heldCopies === 0), "Zero development yields the uncontested starting pool");
    const lowerRank = ctx.tftFlexLobbyPool(catalog, profiles.find(entry => entry.id === "gold-profile"), { players: 7, development: 0.5, overrides: { tank: 0 } });
    assert.equal(held(lowerRank, "carry"), 1);
    assert.equal(held(lowerRank, "tank"), 0, "Manual scouts take precedence in any selected rank profile");
    const stale = lobbyFixture();
    stale.lobbyStats.profiles.push({ ...stale.lobbyStats.profiles[0], id: "old-patch", patch: "18.2" });
    assert.equal(ctx.tftFlexLobbyProfiles(ctx.tftFlexCatalog(stale)).length, 2, "Other gameplay patches cannot supply the typical-lobby estimate");
    const fourthStar = lobbyFixture();
    const row = fourthStar.lobbyStats.profiles[0].units.find(unit => unit.slug === "duo-a");
    row.starRates = [1, 0, 0, 0];
    row.meanCopiesWhenPlayed = 1;
    row.playRatePerLobby = row.copiesPerPlayer * 8;
    assert.equal(ctx.tftFlexLobbyProfiles(ctx.tftFlexCatalog(fourthStar)).length, 2, "Real four-star row shapes remain valid cached profiles");
    assert.equal(ctx.tftFlexLobbyProfiles(ctx.tftFlexCatalog(fixture())).length, 0, "Older catalogs keep baseline support without fabricating a lobby profile");
    const mismatch = lobbyFixture();
    mismatch.patch = "18.4";
    assert.equal(ctx.tftFlexLobbyProfiles(ctx.tftFlexCatalog(mismatch)).length, 0, "A new patch requires matching cached statistics");
    const incomplete = lobbyFixture();
    incomplete.lobbyStats.profiles[0].units.pop();
    const incompleteCatalog = ctx.tftFlexCatalog(incomplete);
    assert.equal(ctx.tftFlexLobbyProfiles(incompleteCatalog).length, 1, "A profile must account for every champion in the pool");
    assert.equal(ctx.tftFlexLobbyProfile(incompleteCatalog).id, "gold-profile", "A valid rank profile can replace an incomplete cached default");
    assert.throws(() => ctx.tftFlexLobbyPool(catalog, profile, { overrides: { tank: 1.5 } }), /whole scouted copies/);
    const b = ctx.tftFlexEvaluate(catalog, ["tank", "carry", "front-a", "front-b", "duo-a", "duo-b", "front-d", "back-b"], settings);
    const targets = ctx.tftFlexLobbyTargets(b);
    assert.equal(targets.length, b.units.length);
    assert(targets.every(target => target.copies === (target.cost === 4 ? 3 : 1)));
    assert(!targets.some(target => target.api === "other-four"), "Scouted unrelated holdings are not purchased as targets");
    const baseline = ctx.tftFlexGoldEstimate(catalog.shop, ctx.tftFlexGoldRequirements(b));
    const emptyLobby = ctx.tftFlexLobbyEstimate(catalog.shop, noDevelopment, targets);
    assert(Math.abs(emptyLobby.totalGold - baseline.totalGold) < 1e-6, "The all-zero held-copy pool matches the uncontested exact expectation");
    const nonTargetHeld = ctx.tftFlexLobbyEstimate(catalog.shop, noPlayers, targets);
    assert(nonTargetHeld.totalGold < emptyLobby.totalGold, "Copies held of another same-tier champion reduce the denominator and improve target availability");
    assert.equal(nonTargetHeld.purchaseGold, emptyLobby.purchaseGold, "Other players' holdings do not change the copies the player buys");
    assert.equal(JSON.stringify(raw), snapshot, "Profile selection and pool construction preserve published catalog statistics");
  }

  async function checkLobbyCompletion() {
    const prepare = async (count = 31) => {
      const h = interfaceHarness();
      await h.context.loadTftFlex();
      h.flex.catalog = h.context.tftFlexCatalog(lobbyFixture());
      h.context.renderTftFlexLobbyControls();
      const baseBoard = h.flex.boards[0];
      h.flex.boards = Array.from({ length: count }, (_, index) => ({
        ...baseBoard,
        id: `lobby-${index}`,
        score: count - index,
        gold: { totalGold: 300 + index, refreshGold: 264 + index, purchaseGold: 36 },
      }));
      h.flex.sort = "lobbyGold";
      h.flex.order = "asc";
      h.flex.page = 1;
      h.flex.cards = new Map();
      h.flex.openBoards = new Set();
      const pending = [];
      h.context.tftFlexLobbyRequest = () => new Promise(resolve => pending.push(resolve));
      const originalRender = h.context.renderTftFlex;
      let renders = 0;
      h.context.renderTftFlex = () => { ++renders; return originalRender(); };
      h.context.renderTftFlex();
      const completion = h.context.beginTftFlexLobby(h.flex.catalog, h.flex.boards, h.flex.searchReq);
      await Promise.resolve();
      const gold = totalGold => ({ available: true, totalGold, refreshGold: totalGold - 36, purchaseGold: 36, method: "exact-dp" });
      return { ...h, pending, completion, gold, renders: () => renders };
    };
    const h = await prepare();
    const initialRenders = h.renders();
    const initialTable = h.element("tft-flex-results").children[0];
    const baselines = h.flex.boards.map(b => b.gold);
    const opened = await openFirstBoard(h);
    const rows = () => h.element("tft-flex-results").querySelectorAll(".tft-flex-summary").map(row => row.dataset.board);
    const before = rows();
    assert.equal(h.pending.length, 31);
    h.pending[30](h.gold(60));
    await Promise.resolve();
    await Promise.resolve();
    assert(h.flex.boards[30].lobbyGold && !before.includes("lobby-30"));
    assert.deepEqual(rows(), before);
    assert.equal(h.renders(), initialRenders, "Partial typical-lobby results do not reorder the table");
    assert.equal(h.element("tft-flex-results").children[0], initialTable);
    for (let index = 0; index < 29; ++index) h.pending[index](h.gold(100 + index));
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(h.renders(), initialRenders);
    h.pending[29](h.gold(129));
    await h.completion;
    assert.equal(h.renders(), initialRenders + 1);
    assert.equal(rows()[0], "lobby-30", "Typical-lobby order compares the entire pool before paging");
    assert(h.flex.boards.every((b, index) => b.gold === baselines[index]), "Lobby gold never replaces uncontested expectations");
    assert.equal(h.flex.cards.get(opened.board.id), opened.card);
    assert.equal(h.element("tft-flex-results").querySelector(".tft-flex-card"), opened.card);
    assert(opened.card.querySelector(".tft-flex-lobby-gold").textContent.includes("\u2248100g"), "Existing card nodes receive the new scenario estimate");
    const swapBoard = { ...h.flex.boards[0], id: "swap-scenario" };
    const renderCount = h.renders();
    const swapBatch = h.context.beginTftFlexLobby(h.flex.catalog, [swapBoard], h.flex.searchReq, { sort: false });
    await Promise.resolve();
    h.pending.at(-1)(h.gold(110));
    await swapBatch;
    assert.equal(h.renders(), renderCount, "A swap-estimate batch does not reorder the main comp table");
    const cancelled = await prepare(4);
    const oldTable = cancelled.element("tft-flex-results").children[0];
    const oldRenders = cancelled.renders();
    cancelled.context.cancelTftFlexLobby();
    cancelled.pending.forEach(resolve => resolve(cancelled.gold(100)));
    await cancelled.completion;
    assert.equal(cancelled.renders(), oldRenders);
    assert.equal(cancelled.element("tft-flex-results").children[0], oldTable);
    assert(cancelled.flex.boards.every(b => !b.lobbyGold && b.gold), "Cancelled lobby results preserve baseline and cannot fill the old scenario");
    const beforeDispatch = interfaceHarness();
    await beforeDispatch.context.loadTftFlex();
    beforeDispatch.flex.catalog = beforeDispatch.context.tftFlexCatalog(lobbyFixture());
    let dispatched = 0;
    beforeDispatch.context.tftFlexLobbyRequest = async () => { ++dispatched; return { totalGold: 100 }; };
    const immediate = beforeDispatch.context.beginTftFlexLobby(beforeDispatch.flex.catalog, beforeDispatch.flex.boards, beforeDispatch.flex.searchReq);
    beforeDispatch.context.cancelTftFlexLobby();
    await immediate;
    assert.equal(dispatched, 0, "Immediate cancellation stops queued requests before they create a replacement worker");
    const failed = await prepare(1);
    failed.pending[0]({ available: false, unavailableTargets: [{ api: "tank", available: 2, required: 3 }] });
    await failed.completion;
    assert(failed.element("tft-flex-results").textContent.includes("Not enough copies"), "An impossible pool has a clear diagnostic instead of a baseline fallback");
    assert(failed.flex.boards[0].gold && !Number.isFinite(failed.flex.boards[0].lobbyGold.totalGold));
  }

  async function checkLobbyInterface() {
    const h = interfaceHarness();
    const ctx = h.context;
    const { workers } = installGoldWorkers(h);
    const raw = lobbyFixture();
    const snapshot = JSON.stringify(raw);
    ctx.api = async () => raw;
    await ctx.loadTftFlex();
    await until(() => h.flex.boards.every(b => b.gold && (b.lobbyGold || b.lobbyError)), "Both gold scenarios complete in their workers");
    assert(
      h.flex.boards.every(b => Number.isFinite(b.lobbyGold?.totalGold)),
      `Valid cached profiles produce finite typical-lobby results: ${h.flex.boards.map(b => b.lobbyError || "").join("; ")}`
    );
    assert.equal(h.flex.lobbyProfile, "emerald-profile");
    assert.equal(Number(h.element("tft-flex-lobby-players").value), 7);
    assert.equal(Number(h.element("tft-flex-lobby-development").value), 50);
    assert.equal(h.element("tft-flex-lobby-profile").disabled, false);
    assert.equal(h.element("tft-flex-lobby-profile").children.length, 2);
    assert(h.element("tft-flex-lobby-profile").children.every(option => option.textContent.includes("18.3")));
    assert(h.element("tft-flex-lobby-context").textContent.includes("Emerald+") && h.element("tft-flex-lobby-context").textContent.includes("Source updated"));
    assert.equal(h.element("tft-flex-lobby-context").querySelector("a").href, "https://tactics.tools/");
    assert(h.element("tft-flex-results").textContent.includes("Uncontested gold") && h.element("tft-flex-results").textContent.includes("Typical lobby gold"));
    const baselineWorker = workers.find(worker => worker.messages.some(message => message.requirements));
    const lobbyWorker = workers.find(worker => worker.messages.some(message => message.pool));
    assert(baselineWorker && lobbyWorker && baselineWorker !== lobbyWorker, "Typical-lobby work uses a separate worker from the uncontested calculation");
    assert(lobbyWorker.messages.every(message => message.pool.length === raw.units.length), "Worker requests include non-target opponents' units as well as the desired roster");
    const boards = h.flex.boards;
    const baselines = boards.map(b => b.gold);
    const request = h.flex.searchReq;
    const calls = h.calls.length;
    const { card } = await openFirstBoard(h);
    const swaps = card.querySelector(".tft-flex-swaps");
    swaps.open = true;
    await swaps.fire("toggle");
    await until(() => h.flex.lobbySwapBoards.size && [...h.flex.lobbySwapBoards.values()].every(b => b.lobbyGold || b.lobbyError), "Opened support swaps receive the same lobby scenario");
    assert(swaps.textContent.includes("Typical lobby gold") && swaps.querySelectorAll(".tft-flex-lobby-cell").length, "Swap rows show both uncontested and typical-lobby gold");
    const swapNodes = swaps.querySelectorAll(".tft-flex-lobby-cell").slice();
    const epoch = h.flex.lobbyReq;
    h.element("tft-flex-lobby-players").value = "4";
    await h.element("tft-flex-lobby-players").fire("change");
    await until(() => [...boards, ...h.flex.lobbySwapBoards.values()].every(b => b.lobbyGold || b.lobbyError), "Opponent-count change completes without a new trait search");
    assert.equal(h.flex.lobbyPlayers, 4);
    assert(h.flex.lobbyReq > epoch);
    assert(lobbyWorker.terminated && !baselineWorker.terminated);
    assert.equal(h.flex.searchReq, request);
    assert.equal(h.flex.boards, boards);
    assert(boards.every((b, index) => b.gold === baselines[index]), "Changing the lobby keeps every uncontested result intact");
    assert.equal(h.flex.cards.get(boards[0].id), card);
    assert.equal(swaps.open, true);
    assert(swaps.querySelectorAll(".tft-flex-lobby-cell").every((node, index) => node === swapNodes[index]), "Scenario changes update existing swap cells");
    h.element("tft-flex-lobby-development").value = "0";
    await h.element("tft-flex-lobby-development").fire("change");
    await until(() => boards.every(b => b.lobbyGold), "Zero development completes");
    assert(boards.every((b, index) => Math.abs(b.lobbyGold.totalGold - baselines[index].totalGold) < 1e-6), "Zero inferred holdings recover each board's exact uncontested gold");
    h.element("tft-flex-lobby-profile").value = "gold-profile";
    await h.element("tft-flex-lobby-profile").fire("change");
    await until(() => boards.every(b => b.lobbyGold), "Changing a cached rank profile completes");
    assert.equal(h.flex.lobbyProfile, "gold-profile");
    assert(h.element("tft-flex-lobby-context").textContent.includes("Gold"));
    h.element("tft-flex-lobby-development").value = "50";
    await h.element("tft-flex-lobby-development").fire("change");
    await until(() => boards.every(b => b.lobbyGold), "Development adjustment completes");
    const scout = slug => h.element("tft-flex-lobby-list").querySelectorAll("input").find(input => input.dataset.unit === slug);
    scout("tank").value = "0";
    await scout("tank").fire("change");
    await until(() => boards.every(b => b.lobbyGold), "Explicit zero scout completes");
    assert(Object.prototype.hasOwnProperty.call(h.flex.lobbyOverrides, "tank") && h.flex.lobbyOverrides.tank === 0);
    assert.equal(scout("tank").value, "0");
    const beforeSearch = h.flex.lobbyReq;
    h.element("tft-flex-lobby-search").value = "OTHER FOUR";
    await h.element("tft-flex-lobby-search").fire("input");
    assert.equal(h.flex.lobbyReq, beforeSearch);
    assert.equal(h.element("tft-flex-lobby-list").querySelectorAll("input").length, 1, "Scouting search filters controls without recalculating estimates");
    scout("other-four").value = "8";
    await scout("other-four").fire("change");
    await until(() => boards.every(b => b.lobbyGold), "Non-target scout completes");
    assert.equal(h.flex.lobbyOverrides["other-four"], 8);
    const latestPool = workers.filter(worker => !worker.terminated && worker.messages.some(message => message.pool)).at(-1).messages.at(-1).pool;
    assert.equal(latestPool.find(unit => unit.api === "tank").heldCopies, 0);
    assert.equal(latestPool.find(unit => unit.api === "other-four").heldCopies, 8);
    h.element("tft-flex-lobby-search").value = "";
    await h.element("tft-flex-lobby-search").fire("input");
    scout("tank").value = "8";
    await scout("tank").fire("change");
    await until(() => boards.every(b => b.lobbyGold), "Insufficient-copy diagnostics complete");
    assert(boards.every(b => b.lobbyGold.available === false && b.gold), "A held target can become impossible while the baseline stays available");
    assert(card.textContent.includes("Not enough copies") && h.element("tft-flex-results").textContent.includes("Not enough copies"));
    scout("tank").value = "";
    await scout("tank").fire("change");
    await until(() => boards.every(b => b.lobbyGold), "Blank scout restores inferred holdings");
    assert(!Object.prototype.hasOwnProperty.call(h.flex.lobbyOverrides, "tank"));
    assert.equal(scout("tank").value, "");
    await h.element("tft-flex-lobby-reset").fire("click");
    await until(() => boards.every(b => b.lobbyGold), "Clearing scouts completes");
    assert.deepEqual(plain(h.flex.lobbyOverrides), {});
    assert.equal(h.element("tft-flex-lobby-reset").disabled, true);
    assert.equal(h.flex.searchReq, request);
    assert.equal(h.calls.length, calls, "Lobby controls read the published cache without any new API or trait-search request");
    assert.equal(JSON.stringify(raw), snapshot);
    ctx.cancelTftFlex();
    assert(workers.every(worker => worker.terminated), "Leaving Trait flex stops both workers");

    const old = interfaceHarness();
    const oldWorkers = installGoldWorkers(old);
    const oldRaw = lobbyFixture();
    delete oldRaw.lobbyStats;
    old.context.api = async () => oldRaw;
    await old.context.loadTftFlex();
    await until(() => old.flex.boards.every(b => b.gold), "Old catalogs still calculate uncontested gold");
    assert(old.flex.boards.every(b => !b.lobbyGold && b.gold));
    assert.equal(old.element("tft-flex-lobby-profile").disabled, true);
    assert(old.element("tft-flex-lobby-context").textContent.includes("unavailable"));
    assert.equal(oldWorkers.workers.length, 1, "Missing statistics never silently run a baseline estimate as typical-lobby gold");
    const oldCard = (await openFirstBoard(old)).card;
    assert(/unavailable/i.test(oldCard.querySelector(".tft-flex-lobby-gold").textContent));
    old.context.cancelTftFlex();

    const failure = interfaceHarness();
    const failedWorkers = installGoldWorkers(failure);
    failure.context.api = async () => lobbyFixture();
    await failure.context.loadTftFlex();
    const brokenWorker = failedWorkers.workers.find(worker => worker.messages.some(message => message.pool));
    brokenWorker.onerror();
    await until(() => failure.flex.boards.every(b => b.lobbyError && b.gold), "A lobby worker failure keeps the baseline available");
    assert(failure.flex.boards.every(b => !b.lobbyGold && b.gold));
    assert(failure.element("tft-flex-results").querySelectorAll(".tft-flex-lobby-cell").every(cell => cell.textContent === "Unavailable"));
    await failure.context.recalculateTftFlexLobby();
    assert(failure.flex.boards.every(b => b.lobbyGold && !b.lobbyError), "Changing or retrying lobby assumptions can recover a failed scenario");
    failure.context.cancelTftFlex();
  }

  checkScoring();
  await checkSearchAndSwaps();
  await checkSoloAnchorSearchAndSwaps();
  await checkBrowsingAndSorting();
  await checkInterface();
  await checkCoreBrowseInterface();
  await checkModeAndRouting();
  await checkMinimumCost();
  await checkCoreCosts();
  await checkGenericCores();
  await checkAutomaticCoreBrowse();
  await checkCorePickerInitial();
  await checkInitialBrowseProgress();
  await checkCoreSelectionCancellation();
  await checkCoreCostInterface();
  await checkCoreGoldTargets();
  await checkCoreGoldWorkers();
  await checkCoreGoldInterface();
  await checkGoldInterface();
  await checkGoldSortCompletion();
  await checkLobbyPools();
  await checkLobbyCompletion();
  await checkLobbyInterface();

  ++passedSuites;
}

// ============================================================================
// 8. Dashboard Routing, History, and Pending Navigation (test-tft-navigation-ui.cjs)
// ============================================================================

async function runNavigationUiSuite() {
  function harness(initialHash = "") {
    const meta = { tiers: ["fixture", "other"], defaultTier: "fixture", platforms: [], inventory: [] };
    let responder = null;
    const requests = [];
    const opens = [];
    const base = loadDashboard({
      initialHash,
      manualTimers: true,
      api: async url => {
        requests.push(url);
        if (responder) return responder(url);
        if (url === "api/meta.json") return meta;
        if (url.startsWith("api/tft/leaderboard/")) {
          const key = url.split("/").at(-1).replace(/\.json$/, "");
          return { revision: "fixture-v1", selection: { key }, damage: [], tanks: [], complete: true };
        }
        return { pending: true };
      },
    });
    const { context } = base;
    context.META = meta;
    context.tstate.meta = {
      revision: "fixture-v1",
      set: 14,
      patch: "18.3",
      items: [],
      stars: [1, 2, 3],
      geometries: { clump: "Clumped", spread: "Spread" },
      dummy: {
        count: 6,
        tanks: 4,
        others: 2,
        slots: Array.from({ length: 6 }, (_, i) => ({
          kind: i < 4 ? "tank" : "non-tank",
          line: i < 4 ? "frontline" : "backline",
          nearby: i < 4,
        })),
      },
      traitContexts: { bare: "None", low: "First breakpoint", high: "Max traits" },
      tankThreats: [{ key: "mixed", label: "Mixed" }, { key: "physical", label: "Physical" }],
      units: [
        { slug: "alpha", api: "Alpha", name: "Alpha", objective: "carry", cost: 2, stars: [1, 2, 3], traits: [] },
        { slug: "beta", api: "Beta", name: "Beta", objective: "tank", cost: 4, stars: [1, 2], traits: [] },
      ],
    };
    context.bstate.meta = {
      champions: [{ slug: "annie", name: "Annie", scenarios: ["squishy"] }],
      scenarios: [{ key: "squishy" }],
      defaultChampion: "annie",
      defaultScenario: "squishy",
    };
    const realLoadTftEarly = context.loadTftEarly;
    context.openTft = async () => {
      opens.push({ view: context.state.view, mode: context.tstate.mode, unit: context.tstate.unit });
      if (["builds", "leaderboard"].includes(context.tstate.mode)) context.renderTftMeta();
    };
    context.openBuilds = async () => {};
    context.loadOverview = async () => {};
    context.loadInventory = async () => {};
    context.loadOnetricks = async () => {};
    context.checkItemHealth = () => {};
    context.loadBuildsView = async () => {};
    context.loadTftFlex = async () => true;
    context.loadTftEarly = async () => true;
    context.tftFiniteLeaderboardValid = () => true;

    return {
      base,
      context,
      realLoadTftEarly,
      node: id => base.element(id),
      location: base.location,
      entries: base.historyEntries,
      writes: base.historyWrites,
      requests,
      opens,
      nav: base.queryAll("nav button"),
      windowEvents: base.windowEvents,
      get index() { return base.historyIndex; },
      back: () => base.back(),
      forward: () => base.forward(),
      emit: type => base.emitWindow(type),
      setHash: hash => base.setHash(hash),
      setResponder: fn => { responder = fn; },
      params: () => Object.fromEntries(new URLSearchParams(base.location.hash.slice(1))),
    };
  }

  async function checkInitialRoute() {
    const h = harness("#tab=tft&tftview=early&elevel=5&eunit=Ash%20%26%20Ember%3D%CE%B2&epage=2");
    await h.context.init();
    assert.equal(h.entries.length, 1, "Bootstrap canonicalizes the existing entry");
    assert.equal(h.writes.filter(write => write.type === "push").length, 0, "Bootstrap creates no Back step");
    assert.deepEqual(h.writes.map(write => write.type), ["replace"], "Bootstrap replaces the original incomplete route once");
    assert.equal(h.params().tier, "fixture");
    assert.equal(h.context.tstate.mode, "early");
    assert.equal(h.context.tearly.unit, "Ash & Ember=\u03b2", "URL restoration preserves encoded delimiters and Unicode");
    assert.equal(h.context.tearly.page, 2);
    assert.equal(h.node("tft-early-panel").hidden, false);
    assert.equal(h.windowEvents.popstate?.length, 1, "Native Back/Forward is wired to the page");
    assert.equal(h.windowEvents.hashchange?.length, 1, "Edited hash links are wired to the page");
    const writes = h.writes.length;
    const opens = h.opens.length;
    await h.emit("popstate");
    await h.emit("hashchange");
    h.context.updateHash(true);
    assert.equal(h.writes.length, writes, "Revisiting the current route adds no duplicate entry");
    assert.equal(h.opens.length, opens, "Duplicate events do not reopen the current route");
  }

  async function checkLeaderboardBackAndForward() {
    const h = harness("#tab=tft&tier=fixture&unit=alpha&star=2&geo=clump&tr=bare");
    h.context.restoreHash();
    const { tstate, tboard } = h.context;
    Object.assign(tboard, {
      mode: "tanks",
      star: "s2",
      geo: "clump",
      traits: "low",
      threat: "physical",
      role: "fighter",
      cost: "4",
      search: "Ash & Ember=\u03b2",
    });
    await h.context.setTftMode("leaderboard");
    assert.equal(h.entries.length, 2, "Selecting the leaderboard adds one entry");
    assert.equal(h.params().bq, "Ash & Ember=\u03b2");
    assert.equal(h.node("tft-leaderboard-panel").hidden, false);
    tstate.sel = 2;
    h.context.openTftLeaderboardEntry({ unit: "beta", star: 2, scenario: "s2-clump-low-physical" });
    await Promise.resolve();
    assert.equal(h.entries.length, 3, "Opening a champion adds one entry even when mode and fight both update");
    assert.equal(tstate.mode, "builds");
    assert.equal(tstate.unit, "beta");
    assert.equal(h.node("tft-leaderboard-back").hidden, false);
    const writes = h.writes.length;
    const opens = h.opens.length;
    await h.back();
    assert.equal(tstate.mode, "leaderboard");
    assert.equal(tboard.search, "Ash & Ember=\u03b2");
    assert.equal(tboard.cost, "4");
    assert.equal(tboard.role, "fighter");
    assert.equal(tboard.geo, "clump");
    assert.equal(tboard.threat, "physical");
    assert.equal(h.node("tft-leaderboard-panel").hidden, false);
    assert.equal(h.opens.length, opens + 1, "popstate followed by hashchange restores once");
    assert.equal(h.entries.length, 3, "Restoration preserves the Forward entry");
    assert.equal(h.writes.slice(writes).filter(write => write.type === "push").length, 0, "Back never pushes history");
    await h.forward();
    assert.equal(tstate.mode, "builds");
    assert.equal(tstate.unit, "beta");
    assert.equal(tstate.star, 2);
    assert.equal(tstate.threat, "physical");
    assert.equal(tboard.fromLeaderboard, true, "Forward restores the leaderboard origin");
    await h.back();
    await h.back();
    assert.equal(tstate.unit, "alpha");
    assert.equal(tstate.threat, "mixed", "Omitted threat returns to the earlier default");
    assert.equal(tboard.fromLeaderboard, false, "An ordinary champion route clears the later origin");
    assert.equal(h.node("tft-leaderboard-back").hidden, true);
  }

  async function checkLegacySpreadLeaderboardRoute() {
    const h = harness("#tab=tft&tftview=leaderboard&board=damage&bstar=s2&bgeo=spread&btr=low&bthreat=mixed");
    const saved = { revision: "fixture-v1", selection: { key: "s2-spread-low-mixed" }, damage: [], tanks: [], complete: true };
    Object.assign(h.context.tboard, { geo: "spread", key: saved.selection.key, data: saved });
    await h.context.init();
    assert.equal(h.context.tboard.geo, "clump", "Legacy leaderboard links restore only the clumped formation");
    assert.equal(h.context.tboard.data, null, "Legacy spread payloads are invalidated by the real controls");
    assert.equal(h.params().bgeo, "clump", "The restored URL records the clumped formation");
    assert.equal(h.entries.length, 1, "Canonicalizing a legacy formation adds no Back step");
    assert(h.writes.every(write => write.type === "replace"), "Legacy normalization replaces the existing history entry");
    await h.context.loadTftLeaderboard();
    assert.equal(h.requests.filter(url => url.startsWith("api/tft/leaderboard/")).at(-1), "api/tft/leaderboard/s2-clump-low-mixed.json");
    assert.equal(h.context.tboard.data.selection.key, "s2-clump-low-mixed");
    assert.equal(saved.selection.key, "s2-spread-low-mixed", "Restoration preserves the original saved payload");

    await h.setHash("#tab=tft&tftview=builds&unit=alpha&star=2&geo=spread&tr=bare");
    assert.equal(h.context.tstate.geo, "spread", "Individual build diagnostics retain their formation option");
    assert.equal(h.params().geo, "spread");
  }

  async function checkRestorationDefaults() {
    const h = harness("#tab=tft&tftview=leaderboard&board=damage&bstar=s2&bgeo=clump&btr=low&bthreat=mixed");
    Object.assign(h.context.tboard, { cost: "5", role: "fighter", search: "newer", fromLeaderboard: true, returnUnit: "beta" });
    h.context.restoreHash();
    assert.equal(h.context.tboard.cost, "all", "Omitted all-cost filter is not replaced by the initial four-cost filter");
    assert.equal(h.context.tboard.role, "all");
    assert.equal(h.context.tboard.search, "");
    assert.equal(h.context.tboard.fromLeaderboard, false);

    Object.assign(h.context.tflex, {
      preference: "vertical",
      minFrontline: 8,
      locked: ["alpha"],
      allowFive: true,
      page: 5,
      lobbyPlayers: 0,
      lobbyDevelopment: 1,
      lobbyOverrides: { alpha: 9 },
    });
    await h.setHash("#tab=tft&tftview=traits");
    assert.equal(h.context.tflex.preference, "breadth");
    assert.equal(h.context.tflex.minFrontline, 3);
    assert.deepEqual(plain(h.context.tflex.locked), []);
    assert.equal(h.context.tflex.allowFive, false);
    assert.equal(h.context.tflex.page, 1);
    assert.equal(h.context.tflex.lobbyPlayers, 7);
    assert.equal(h.context.tflex.lobbyDevelopment, 0.5);
    assert.deepEqual(plain(h.context.tflex.lobbyOverrides), {});

    Object.assign(h.context.tearly, { level: 5, allowThree: false, unit: "beta", sort: "gold", order: "asc", page: 4 });
    await h.setHash("#tab=tft&tftview=early");
    assert.deepEqual(
      plain({
        level: h.context.tearly.level,
        allowThree: h.context.tearly.allowThree,
        unit: h.context.tearly.unit,
        sort: h.context.tearly.sort,
        order: h.context.tearly.order,
        page: h.context.tearly.page,
      }),
      { level: 4, allowThree: true, unit: "all", sort: "wins", order: "desc", page: 1 }
    );
  }

  async function checkFiltersAndPages() {
    const h = harness("#tab=tft&tier=fixture&unit=alpha&star=2&geo=clump&tr=bare");
    h.context.restoreHash();
    Object.assign(h.context.tstate, { cls: "tank", cost: "4", search: "Ash & Ember=\u03b2" });
    h.context.updateHash(true);
    assert.equal(h.params().uclass, "tank");
    assert.equal(h.params().ucost, "4");
    assert.equal(h.params().uq, "Ash & Ember=\u03b2");
    await h.back();
    assert.equal(h.context.tstate.cls, "all");
    assert.equal(h.context.tstate.cost, "2", "Returning to an older route selects its champion's cost");
    assert.equal(h.context.tstate.search, "");
    await h.forward();
    assert.equal(h.context.tstate.cls, "tank");
    assert.equal(h.context.tstate.cost, "4");
    assert.equal(h.context.tstate.search, "Ash & Ember=\u03b2");
    assert.equal(h.node("tft-search").value, "Ash & Ember=\u03b2");

    await h.setHash("#tab=tft&tftview=traits&fpage=3&fkeep=alpha%2Cbeta&flplayers=0&fldev=0&flheld=alpha%3A0");
    assert.equal(h.context.tflex.page, 3);
    assert.deepEqual(plain(h.context.tflex.locked), ["alpha", "beta"]);
    assert.equal(h.context.tflex.lobbyPlayers, 0);
    assert.equal(h.context.tflex.lobbyDevelopment, 0);
    assert.deepEqual(plain(h.context.tflex.lobbyOverrides), { alpha: 0 });
    assert.equal(h.params().fpage, "3");
    await h.setHash("#tab=tft&tftview=early&elevel=5&epage=4&ethree=0&esort=gold");
    assert.equal(h.context.tearly.page, 4);
    assert.equal(h.context.tearly.level, 5);
    assert.equal(h.context.tearly.allowThree, false);
    assert.equal(h.context.tearly.order, "asc");
    assert.equal(h.params().epage, "4");
    await h.back();
    assert.equal(h.context.tstate.mode, "traits");
    assert.equal(h.context.tflex.page, 3);
    await h.forward();
    assert.equal(h.context.tstate.mode, "early");
    assert.equal(h.context.tearly.page, 4);
  }

  async function checkFlexCoreNavigation() {
    const h = harness("#tab=tft&tftview=traits&fcost=2&fcore=beta%2Calpha%2Cbeta");
    h.context.restoreHash();
    assert.equal(h.context.tflex.cost, 2);
    assert.deepEqual(plain(h.context.tflex.cores), ["alpha", "beta"], "Core routes normalize duplicates and order");
    assert.equal(h.params().fcore, "alpha,beta");
    Object.assign(h.context.tflex, { cost: 3, cores: ["third", "Ash & Ember=\u03b2", "second"], coreSearch: "temporary" });
    h.context.updateHash(true);
    assert.equal(h.params().fcore, "Ash & Ember=\u03b2,second,third", "Multiple cores preserve encoded names");
    await h.back();
    assert.equal(h.context.tflex.cost, 2);
    assert.deepEqual(plain(h.context.tflex.cores), ["alpha", "beta"], "Back restores the earlier core selection");
    assert.equal(h.context.tflex.coreSearch, "", "Core search does not hide a restored selection");
    await h.forward();
    assert.equal(h.context.tflex.cost, 3);
    assert.deepEqual(plain(h.context.tflex.cores), ["Ash & Ember=\u03b2", "second", "third"], "Forward restores every core");
    await h.setHash("#tab=tft&tftview=traits&ftank=beta&fcarry=alpha");
    assert.deepEqual(plain(h.context.tflex.cores), ["alpha", "beta"], "Old pinned pair links become selected cores");
    assert.equal(h.params().fcore, "alpha,beta");
    assert(!("ftank" in h.params()) && !("fcarry" in h.params()), "Canonical routes remove the old pair fields");
    await h.setHash("#tab=tft&tftview=traits&fcore=&ftank=beta&fcarry=alpha");
    assert.deepEqual(plain(h.context.tflex.cores), [], "An explicit empty selection takes precedence over legacy fields");
    await h.setHash("#tab=tft&tftview=traits");
    assert.deepEqual(plain(h.context.tflex.cores), [], "An unselected route clears earlier cores");
    assert.equal(h.context.tflex.cost, 4);
    h.context.tflex.cores = ["beta"];
    h.context.updateHash(true);
    assert.equal(h.params().fcore, "beta", "A single-champion browse filter is saved in the route");
    await h.back();
    assert.deepEqual(plain(h.context.tflex.cores), [], "Back restores browsing without a core filter");
    await h.forward();
    assert.deepEqual(plain(h.context.tflex.cores), ["beta"], "Forward restores the single-champion browse filter");
  }

  async function checkFightControlsAndBuildSelection() {
    const h = harness("#tab=tft&tier=fixture&unit=alpha&star=2&geo=clump&tr=bare");
    h.context.restoreHash();
    const star = h.node("tft-star-seg").querySelectorAll("button").find(button => button.dataset.key === "3");
    await star.fire("click");
    assert.equal(h.params().star, "3", "The actual star button records its new fight");
    assert.equal(h.entries.length, 2);
    await h.back();
    assert.equal(h.context.tstate.star, 2);
    await h.forward();
    assert.equal(h.context.tstate.star, 3);

    h.context.tstate.sel = 3;
    h.context.updateHash(true);
    assert.equal(h.params().tbuild, "3");
    await h.back();
    assert.equal(h.context.tstate.sel, 0, "Back restores the omitted first build");
    await h.forward();
    assert.equal(h.context.tstate.sel, 3, "Forward restores the selected item-build row");
    const entries = h.entries.length;
    h.context.tstate.star = 1;
    await h.context.loadTftScenario({ navigate: true });
    assert.equal(h.context.tstate.sel, 0, "A different fight starts at its first build");
    assert(!("tbuild" in h.params()));
    assert.equal(h.entries.length, entries + 1, "The fight and selection reset form one navigation");

    const writes = h.writes.length;
    const currentEntries = h.entries.length;
    h.context.tstate.star = 2;
    await h.context.loadTftScenario();
    assert.equal(h.entries.length, currentEntries, "Background fight normalization does not add a Back step");
    assert.deepEqual(h.writes.slice(writes).map(write => write.type), ["replace"]);
  }

  async function checkPendingFightNavigation() {
    const h = harness("#tab=tft&tier=fixture&unit=alpha&star=2&geo=clump&tr=bare");
    h.context.restoreHash();
    let resolve;
    h.setResponder(() => new Promise(finish => { resolve = finish; }));
    h.context.tstate.star = 3;
    const loading = h.context.loadTftScenario({ navigate: true });
    const request = h.context.tstate.req;
    assert.equal(h.params().star, "3", "Navigation is available before the fight request finishes");
    await h.back();
    assert.equal(h.context.tstate.star, 2);
    resolve({ pending: true });
    await loading;
    assert.equal(h.context.tstate.pending, false, "The superseded response cannot mark the restored fight pending");
    assert.equal(h.context.tstate.req > request, true, "Restoration invalidates the earlier fight request");
    assert.equal(h.params().star, "2");
    assert.equal(h.entries.length, 2, "Completing an old request preserves Back and Forward entries");
  }

  async function checkPendingPageChanges() {
    const flex = harness("#tab=tft&tftview=traits&fcore=alpha%2Cbeta&fpage=3");
    flex.context.restoreHash();
    flex.context.tflex.catalog = { revision: "catalog-fixture" };
    let finishSearch;
    Object.assign(flex.context, {
      tftFlexSortedBoards: boards => boards,
      renderTftFlexTable: () => flex.base.document.createElement("table"),
      renderTftFlexStatus() {},
      cancelTftFlexGold() {},
      cancelTftFlexLobby() {},
      beginTftFlexGold() {},
      beginTftFlexLobby() {},
      tftFlexBrowse: () => new Promise(resolve => { finishSearch = resolve; }),
    });
    const searching = flex.context.runTftFlexSearch();
    assert.equal(flex.context.tflex.page, 3, "A restored flex page survives the temporary empty search results");
    flex.context.setTftFlexSort("gold", "asc");
    assert.equal(flex.context.tflex.page, 1, "Changing sort while searching starts at the first page");
    finishSearch({ boards: Array.from({ length: 100 }, (_, id) => ({ id })), expanded: 100 });
    await searching;
    assert.equal(flex.context.tflex.page, 1, "A completed search preserves the newer sort/page selection");
    assert(!("fpage" in flex.params()), "The completed search cannot restore an old page into the newer route");
    assert.equal(flex.params().fsort, "gold");

    const early = harness("#tab=tft&tftview=early&elevel=4&epage=3");
    early.context.restoreHash();
    const rows = Array.from({ length: 100 }, () => ({}));
    Object.assign(early.context, {
      loadTftEarly: early.realLoadTftEarly,
      tftEarlyCatalog: value => value,
      tftEarlyRows: () => (early.context.tearly.data ? rows : []),
      tftEarlyVariant: () => ({ boards: rows }),
      renderTftEarlyTable() {},
      renderTftEarlyControls() {},
      renderTftEarlyMethod() {},
    });
    let finishRefresh;
    early.setResponder(() => new Promise(resolve => { finishRefresh = resolve; }));
    const initialLoad = early.context.loadTftEarly();
    assert.equal(early.context.tearly.page, 3, "A restored early page survives loading before any catalog exists");
    finishRefresh({ patch: "18.3", set: 18 });
    await initialLoad;
    assert.equal(early.context.tearly.page, 3);
    assert.equal(early.params().epage, "3");
    const refreshing = early.context.loadTftEarly({ force: true });
    early.context.setTftEarlySort("gold", "asc");
    assert.equal(early.context.tearly.page, 1);
    finishRefresh({ patch: "18.3", set: 18 });
    await refreshing;
    assert.equal(early.context.tearly.page, 1, "A completed catalog refresh preserves the newer early sort/page selection");
    assert(!("epage" in early.params()), "The completed early refresh cannot overwrite the newer route with its earlier page");
    assert.equal(early.params().esort, "gold");
    await early.node("tft-early-next").fire("click");
    await early.node("tft-early-next").fire("click");
    assert.equal(early.context.tearly.page, 3);
    const nextRefresh = early.context.loadTftEarly({ force: true });
    await early.node("tft-early-next").fire("click");
    assert.equal(early.context.tearly.page, 4);
    finishRefresh({ patch: "18.3", set: 18 });
    await nextRefresh;
    assert.equal(early.context.tearly.page, 4, "Using the real Next button during a refresh survives its completion");
    assert.equal(early.params().epage, "4");
  }

  async function checkBuildsAndTierCompatibility() {
    const h = harness();
    h.context.restoreHash();
    await h.setHash("#tab=builds&tier=other&bview=leaderboard&champ=annie&sc=squishy&lbq=Ash%20%26%20Ember%3D%CE%B2&lbrev=1&lbs=dps&lbd=d");
    assert.equal(h.context.state.view, "builds");
    assert.equal(h.context.state.tier, "other");
    assert.equal(h.node("tier").value, "other");
    assert.equal(h.context.bstate.mode, "leaderboard");
    assert.equal(h.context.bboard.search, "Ash & Ember=\u03b2");
    assert.equal(h.context.bboard.reviewedOnly, true);
    assert.equal(h.context.bboard.sortKey, "dps");
    assert.equal(h.context.bboard.sortDir, -1);
    await h.setHash("#tab=builds&tier=fixture");
    assert.equal(h.context.bstate.mode, "champion");
    assert.equal(h.context.bstate.champion, "annie");
    assert.equal(h.context.bstate.scenario, "squishy");
    assert.equal(h.context.bboard.search, "");
    assert.equal(h.context.bboard.reviewedOnly, false);
    assert.equal(h.context.bboard.sortKey, "rank");
    assert.equal(h.context.bboard.sortDir, 1);
    await h.back();
    assert.equal(h.context.bstate.mode, "leaderboard");
    assert.equal(h.context.state.tier, "other");
    assert.equal(h.node("tier").value, "other");
  }

  async function checkNativeTabsAndInvalidLinks() {
    const h = harness();
    await h.context.init();
    const canonical = h.location.hash;
    await h.nav.find(button => button.dataset.view === "tft").fire("click");
    assert.equal(h.entries.length, 2);
    assert.equal(h.context.state.view, "tft");
    await h.back();
    assert.equal(h.context.state.view, "overview");
    assert.equal(h.location.hash, canonical);
    await h.forward();
    assert.equal(h.context.state.view, "tft");
    await h.back();
    await h.nav.find(button => button.dataset.view === "data").fire("click");
    assert.equal(h.entries.length, 2, "New navigation after Back replaces the abandoned Forward branch");
    assert.equal(h.context.state.view, "data");

    await assert.doesNotReject(() => h.setHash("#tab=tft&tftview=early&eunit=%E0%A4%A"), "Malformed percent escapes do not break navigation");
    await h.setHash("#tab=missing&tier=missing");
    assert.equal(h.context.state.view, "overview");
    assert.equal(h.context.state.tier, "fixture");
    const writes = h.writes.length;
    h.context.META = null;
    h.location.hash = "#tab=tft";
    await h.emit("popstate");
    await h.emit("hashchange");
    assert.equal(h.writes.length, writes, "History events wait until shell metadata is available");
  }

  await checkInitialRoute();
  await checkLeaderboardBackAndForward();
  await checkLegacySpreadLeaderboardRoute();
  await checkRestorationDefaults();
  await checkFiltersAndPages();
  await checkFlexCoreNavigation();
  await checkFightControlsAndBuildSelection();
  await checkPendingFightNavigation();
  await checkPendingPageChanges();
  await checkBuildsAndTierCompatibility();
  await checkNativeTabsAndInvalidLinks();

  ++passedSuites;
}

// ============================================================================
// Runner
// ============================================================================

async function main() {
  const started = performance.now();
  await runCompositionsRetirementSuite();
  await runFlexGoldSuite();
  runFlexLobbyGoldSuite();
  runItemUiSuite();
  await runDamageUiSuite();
  await runEarlyUiSuite();
  await runFlexUiSuite();
  await runNavigationUiSuite();
  const elapsed = (performance.now() - started).toFixed(0);
  console.log(
    `TFT UI and solver tests passed (${passedSuites} suites: compositions-retirement, flex-gold, flex-lobby-gold, item-ui, damage-ui, early-ui, flex-ui, navigation-ui; ${elapsed} ms).`
  );
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
