"use strict";

// Consolidated LoL dashboard UI test suite.
// Run: node lol/tests/test_ui.cjs [--board leaderboard.json]

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { INDEX_HTML, STYLES_CSS, loadDashboard, plain, until } = require("../../web/tests/harness.cjs");

let passedSuites = 0;

function checkOnetricks() {
  const { context } = loadDashboard();
  const { onetrickRows } = context;

  const rows = [
    { c: "zed", n: 3, top: 0 },
    { c: "yasuo", n: 5, top: 2 },
    { c: "garen", n: 0, top: 0 },
    { c: "ahri", n: 5, top: 0 },
  ];
  const order = (key, dir) => plain(onetrickRows(rows, key, dir)).map(r => [r.rank, r.c]);

  assert.deepEqual(
    order("rank", 1),
    [[1, "ahri"], [1, "yasuo"], [3, "zed"], [4, "garen"]],
    "Equal counts share a rank alphabetically, and the next rank skips past them"
  );
  assert.deepEqual(order("rank", -1), [[4, "garen"], [3, "zed"], [1, "ahri"], [1, "yasuo"]]);
  assert.deepEqual(order("n", -1), order("rank", 1), "Sorting by count matches the ranking");
  assert.deepEqual(
    order("c", 1),
    [[1, "ahri"], [4, "garen"], [1, "yasuo"], [3, "zed"]],
    "Ranks stay attached when sorted by name"
  );
  assert.deepEqual(
    order("top", -1),
    [[1, "yasuo"], [1, "ahri"], [3, "zed"], [4, "garen"]],
    "Ties in a role column fall back to rank order"
  );
  assert.equal(rows[0].rank, undefined, "The payload rows are not mutated");
  ++passedSuites;
}

function checkBuildsItems() {
  const { context } = loadDashboard();
  const { buildItemLookup, itemAbbrev, itemModelText, buyOrderItems } = context;

  const cap = { id: 3089, name: "Rabadon's Deathcap", icon: "x.png" };
  const lookup = buildItemLookup({ items: [cap, { id: 1001, name: "Boots" }] });
  assert.equal(lookup.get("Rabadon's Deathcap"), cap, "Rows name items exactly as the catalog does");
  assert.equal(lookup.get("Rabadons Deathcap"), undefined);
  assert.equal(buildItemLookup(null).size, 0, "An older serve without the catalog gives an empty lookup");
  assert.equal(buildItemLookup({}).size, 0);

  assert.equal(itemAbbrev("Rabadon's Deathcap"), "RD");
  assert.equal(itemAbbrev("Blade of the Ruined King"), "BR", "Articles and prepositions are skipped");
  assert.equal(itemAbbrev("Lord Dominik's Regards"), "LD");
  assert.equal(itemAbbrev("Ionian Boots of Lucidity"), "IB");
  assert.equal(itemAbbrev("Yun Tal Wildarrows"), "YT");
  assert.equal(itemAbbrev("Boots"), "BO", "A single word shows two letters");
  assert.equal(itemAbbrev("Void Staff"), "VS");

  const notes = modeled => plain(itemModelText(modeled));
  assert.deepEqual(notes({ covers: ["Magical Opus"], unmodeled: [], note: null }), ["Accounts for: Magical Opus."]);
  assert.deepEqual(
    notes({ covers: ["Mist's Edge", "Clawing Shadows"], unmodeled: [], note: "Clawing Shadows is a slow: damage-irrelevant" }),
    ["Accounts for: Mist's Edge, Clawing Shadows.", "Clawing Shadows is a slow: damage-irrelevant."]
  );
  assert.deepEqual(
    notes({ covers: [], unmodeled: [], note: "Stasis is defensive: stats only." }),
    ["Stasis is defensive: stats only."],
    "A note ending in a period is not doubled"
  );
  assert.deepEqual(notes({ covers: [], unmodeled: ["Fervor"], note: null }), ["Text-only passives outside the numbers: Fervor."]);
  assert.deepEqual(notes({ covers: [], unmodeled: [], note: null }), ["Stats only."], "Boots");
  assert.deepEqual(notes(null), ["No model notes for this item."]);

  const row = {
    items: ["Berserker's Greaves", "Infinity Edge", "Yun Tal Wildarrows", "Umbral Glaive"],
    buyOrder: ["Yun Tal Wildarrows", "Umbral Glaive", "Infinity Edge"],
  };
  assert.deepEqual(plain(buyOrderItems(row)), row.buyOrder);
  assert.equal(buyOrderItems({ items: row.items }), null, "Rows past the top 10 have no buy order");
  assert.equal(buyOrderItems({ ...row, buyOrder: ["Yun Tal Wildarrows", "Umbral Glaive", "Kraken Slayer"] }), null);
  assert.equal(buyOrderItems({ ...row, buyOrder: row.buyOrder.slice(1) }), null);
  assert.equal(buyOrderItems({ ...row, buyOrder: [row.items[0], ...row.buyOrder.slice(1)] }), null);

  // the icons: boots first, then the items numbered in buy order
  const cellOf = r => {
    const icons = context.buildCell(r).querySelectorAll(".item-icon");
    return icons.map(icon => [icon.attributes["aria-label"], ...icon.querySelectorAll(".item-step").map(step => step.textContent)]);
  };
  assert.deepEqual(cellOf(row), [
    ["Berserker's Greaves"], ["Yun Tal Wildarrows, buy 1 of 3", "1"],
    ["Umbral Glaive, buy 2 of 3", "2"], ["Infinity Edge, buy 3 of 3", "3"],
  ]);
  assert.deepEqual(cellOf({ items: row.items }), row.items.map(name => [name]), "Without an order the items keep the row's");
  const lone = { items: ["Berserker's Greaves", "Infinity Edge"], buyOrder: ["Infinity Edge"] };
  assert.deepEqual(plain(buyOrderItems(lone)), lone.buyOrder);
  assert.deepEqual(cellOf(lone), [["Berserker's Greaves"], ["Infinity Edge"]], "A one-item build has no order to number");
  ++passedSuites;
}

async function checkBuildsPicker() {
  // Only reviewed kits reach the tab: the unreviewed drafts' search box,
  // banners, tag and filter are gone for good.
  for (const id of ["champion-more", "champion-search", "champion-list", "builds-generated", "bd-generated", "bboard-reviewed"]) {
    assert(!INDEX_HTML.includes(`id="${id}"`), `#${id} is absent from index.html`);
  }
  assert(!STYLES_CSS.includes(".bboard-tag"), "The unreviewed tag's CSS is absent");

  const h = loadDashboard();
  const { context, element } = h;
  assert.ok(element("champion-seg"), "The page has #champion-seg");

  const picked = [];
  context.bstate.champion = "kayle";
  context.bstate.meta = {
    champions: [
      { slug: "aphelios", name: "Aphelios" },
      { slug: "kayle", name: "Kayle" },
      { slug: "zed", name: "Zed" },
    ],
  };
  context.renderPool = () => {};
  context.loadScenario = () => { picked.push(context.bstate.champion); };

  const buttons = () => element("champion-seg").children;
  const active = () => buttons().map(b => b.classList.contains("active"));
  context.renderChampionPicker();
  assert.deepEqual(buttons().map(b => b.textContent), ["Aphelios", "Kayle", "Zed"], "Every champion in the meta is a button, in its order");
  assert.deepEqual(active(), [false, true, false]);
  assert.ok(buttons().every(b => b.children.length === 0), "No button carries a tag");
  await buttons()[2].fire("click");
  assert.deepEqual(picked, ["zed"]);
  assert.deepEqual(active(), [false, false, true]);

  const errDash = loadDashboard({ api: async () => ({ error: "driver panic" }) });
  errDash.context.bstate.champion = "zed";
  errDash.context.bstate.scenario = "full-squishy";
  errDash.context.bstate.meta = { champions: [{ slug: "zed", name: "Zed" }], scenarios: [{ key: "full-squishy", label: "Full" }] };
  await errDash.context.loadScenario();
  assert.equal(errDash.element("scenario-status").textContent, "This champion's driver failed, so nothing is ranked: driver panic");
  assert.deepEqual([errDash.element("scenario-status").hidden, errDash.element("scenario-desc").textContent], [false, ""],
    "What went wrong stays in view; the dropdown has no ranking to describe");
  ++passedSuites;
}

async function checkBuildsSizes() {
  // "Items 1 to 5": the scenario buttons show one build size at a time, and a
  // size button swaps the selected scenario for the same fight at that size.
  assert(INDEX_HTML.includes('id="builds-size-seg"'), "The page has the size buttons");
  // the method and the selected ranking's particulars are folded away under
  // one dropdown, closed until asked for; the status line is outside it
  const about = /<details class="builds-details" id="builds-about">([\s\S]*?)<\/details>/.exec(INDEX_HTML);
  assert(about, "The page has the About this ranking dropdown, closed by default");
  for (const id of ["scenario-desc", "builds-note", "builds-note-board"]) {
    assert(about[1].includes(`id="${id}"`), `#${id} is inside the dropdown`);
  }
  assert(!about[1].includes('id="scenario-status"'), "The status line stays in view");
  assert(INDEX_HTML.indexOf('id="scenario-seg"') < INDEX_HTML.indexOf('id="builds-size"'), "The scenario buttons sit to the left of the size picker");
  assert(!INDEX_HTML.includes('id="builds-six"'), "The five-or-six toggle they replace is gone");
  assert(!INDEX_HTML.includes('id="builds-title"'), "The card has no heading: the lit buttons say what is ranked");
  assert(STYLES_CSS.includes(".builds-size[hidden] { display: none; }"), "The hidden picker, a flex box, is hidden by its own rule");
  const full = [
    { key: "full-squishy", label: "Full build vs squishy", tier: "full", target: "squishy", targetHp: 2800, armor: 110, mr: 60, duration: 120 },
    { key: "full-tank", label: "Full build vs tank", tier: "full", target: "tank", targetHp: 4800, armor: 220, mr: 160, duration: 120 },
    { key: "full-overall", label: "Full build: overall", tier: "full", overall: true },
  ];
  // as builds.py SCENARIOS lists them: the full build, then the other sizes
  const ITEMS = { one: 1, two: 2, three: 3, four: 4 };
  const sized = tier => full.map(sc => ({
    ...sc, key: sc.key.replace("full", tier), tier, slots: ITEMS[tier] + 1,
    label: sc.label.replace("Full build", `${tier[0].toUpperCase()}${tier.slice(1)}-item build`),
  }));
  const [one, two, three, four] = Object.keys(ITEMS).map(sized);
  const all = [...full, ...one, ...two, ...three, ...four];
  // their keys as the buttons order them: the overall ranking leads its size
  const keys = scenarios => [...scenarios.filter(sc => sc.overall), ...scenarios.filter(sc => !sc.overall)].map(sc => sc.key);
  const flush = () => new Promise(setImmediate);

  const open = async scenarios => {
    const requests = [], cold = new Set();  // cold: cells that aren't computed yet
    const h = loadDashboard({
      manualTimers: true,
      api: async url => {
        requests.push(url);
        if (url === "api/builds/meta.json") {
          return {
            champions: [{ slug: "kayle", name: "Kayle", pool: [] }], scenarios,
            pool: [], boots: [], excluded: [], items: [], itemsPatch: "16.23", note: "Theoretical damage model.",
          };
        }
        if (url === "api/builds/status.json") {
          return { ready: Object.fromEntries(scenarios.map(sc => [`kayle/${sc.key}`, !cold.has(sc.key)])), warmer: "idle" };
        }
        const [, board, key] = /^api\/builds\/(leaderboard|kayle)\/(.+)\.json$/.exec(url);
        if (board === "kayle" && cold.has(key)) return { pending: true };
        const scenario = { ...scenarios.find(sc => sc.key === key), level: 16, targets: [] };
        return board === "leaderboard"
          ? { scenario, rows: [], complete: true, expectedCount: 0, readyCount: 0, pending: [], failed: [] }
          : { champion: "kayle", championName: "Kayle", scenario, ranks: { Q: 5, W: 3, E: 5, R: 2 }, rows: [],
              buildsEvaluated: 0, itemsPatch: "16.23", kitPatch: "16.16" };
      },
    });
    h.context.META = { tiers: ["soloq_masters_plus"], defaultTier: "soloq_masters_plus", platforms: [] };
    h.context.state.view = "builds";
    h.context.state.tier = "soloq_masters_plus";
    await h.context.openBuilds();
    await flush();
    const buttons = () => h.queryAll("#scenario-seg button");
    const sizes = () => h.queryAll("#builds-size-seg button");
    const lit = bs => bs.filter(b => b.classList.contains("active"));
    return {
      h, requests, cold,
      shown: () => buttons().filter(b => !b.hidden).map(b => b.dataset.key),
      active: () => lit(buttons()).map(b => b.dataset.key),
      offered: () => sizes().map(b => b.textContent),
      size: () => lit(sizes()).map(b => b.textContent),
      pressed: () => sizes().filter(b => b.attributes["aria-pressed"] === "true").map(b => b.textContent),
      dimmed: () => sizes().filter(b => b.classList.contains("cold")).map(b => b.textContent),
      words: () => h.queryAll("#view-builds .build-size").map(el => el.textContent),
      pickSize: async n => { await sizes().find(b => b.textContent === String(n)).fire("click"); await flush(); },
      pick: async key => { await buttons().find(b => b.dataset.key === key).fire("click"); await flush(); },
    };
  };

  const d = await open(all);
  const { h, requests } = d;
  const { bstate } = h.context;
  assert.equal(d.words().length, 2, "The leaderboard's note and the item pool's both state the build size");
  assert.deepEqual(h.queryAll("#scenario-seg button").map(b => b.dataset.key), keys(all));
  assert.deepEqual(h.queryAll("#scenario-seg button").filter(b => !b.hidden).map(b => b.textContent),
    ["overall", "vs squishy", "vs tank"],
    "A scenario button names the fight, overall first; the Items buttons say the size");
  assert.deepEqual(d.offered(), ["1", "2", "3", "4", "5"], "A button per size, smallest first, whatever order the scenarios come in");
  assert.deepEqual([bstate.scenario, d.shown(), d.active()], ["full-overall", keys(full), ["full-overall"]],
    "Five items overall is the default, and only that size's scenarios are offered");
  assert.deepEqual([h.element("builds-size").hidden, d.size(), d.pressed(), d.dimmed(), d.words()],
    [false, ["5"], ["5"], [], ["five items", "five"]]);
  const titles = Object.fromEntries(h.queryAll("#builds-size-seg button").map(b => [b.textContent, b.title]));
  assert.equal(titles[1], "Rank boots plus one item. Fought at the level, and against the enemies, typical for that many items.");
  assert.equal(titles[5], "Rank boots plus five items: a full inventory. Fought at the level, and against the enemies, typical for that many items.");

  await d.pick("full-squishy");
  requests.length = 0;
  await d.pickSize(1);
  assert.deepEqual([bstate.scenario, d.shown(), d.active()], ["one-squishy", keys(one), ["one-squishy"]],
    "A size opens the same target's scenario at that size");
  assert.deepEqual(requests, ["api/builds/kayle/one-squishy.json"]);
  assert.deepEqual([d.size(), d.pressed(), d.words()], [["1"], ["1"], ["one item", "one"]]);
  assert.match(h.location.hash, /(^|&)sc=one-squishy(&|$)/, "The URL carries the scenario, and with it the size");

  // a cell that is still warming: the note says why the table is empty, and
  // the size is dimmed like the scenario
  await d.pickSize(4);
  assert.equal(bstate.scenario, "four-squishy");
  d.cold.add("four-tank");
  await d.pick("four-tank");
  assert.match(h.element("scenario-status").textContent, /^Not computed yet for the current code and data \(no warmer is running/);
  assert.equal(h.query("#btbl tbody").children.length, 0);
  assert.deepEqual(d.dimmed(), ["4"], "A size is dimmed while the cell its button opens is cold");
  assert.equal(h.queryAll("#builds-size-seg button")[3].title, `${titles[4]} Not computed yet.`);
  await d.pickSize(5);
  assert.deepEqual([bstate.scenario, d.dimmed()], ["full-tank", ["4"]], "Four items against the tank is still cold from here");
  await d.pick("full-overall");
  assert.deepEqual(d.dimmed(), [], "Four items overall is computed");
  d.cold.clear();

  // a scenario picked at one size stays at that size, and another size is
  // the same fight there
  await d.pickSize(4);
  assert.deepEqual([bstate.scenario, d.active(), d.size()], ["four-overall", ["four-overall"], ["4"]]);
  await d.pick("four-tank");
  assert.deepEqual([bstate.scenario, d.shown(), d.size()], ["four-tank", keys(four), ["4"]]);
  requests.length = 0;
  await d.pickSize(3);
  assert.deepEqual([bstate.scenario, d.shown(), d.active()], ["three-tank", keys(three), ["three-tank"]]);
  assert.deepEqual(requests, ["api/builds/kayle/three-tank.json"]);
  assert.deepEqual([d.size(), d.words()], [["3"], ["three items", "three"]]);

  // the leaderboard follows the size the same way; it dims nothing, and says
  // how many champions it is waiting for instead
  d.cold.add("two-tank");
  await h.context.loadStatus();
  assert.deepEqual(d.dimmed(), ["2"]);
  h.context.setBuildsMode("leaderboard");
  await flush();
  assert.deepEqual(d.dimmed(), []);
  requests.length = 0;
  await d.pickSize(2);
  assert.deepEqual([bstate.scenario, d.shown(), d.size()], ["two-tank", keys(two), ["2"]]);
  assert.deepEqual(requests, ["api/builds/leaderboard/two-tank.json"]);
  assert.deepEqual(d.words(), ["two items", "two"]);
  d.cold.clear();
  h.context.setBuildsMode("champion");
  await flush();

  // a link, or Back, restores the build size with the scenario
  for (const [key, size, shown] of [["full-tank", "5", full], ["four-overall", "4", four], ["one-squishy", "1", one]]) {
    h.location.hash = `#tab=builds&tier=soloq_masters_plus&champ=kayle&sc=${key}`;
    h.context.restoredHash = null;
    h.context.restoreHash();
    await flush();
    assert.deepEqual([bstate.scenario, d.size(), d.shown(), d.active()], [key, [size], keys(shown), [key]], key);
  }

  // a size that lacks the selected fight opens the first one it has
  const uneven = await open([...full, one[0]]);
  await uneven.pick("full-overall");
  await uneven.pickSize(1);
  assert.deepEqual([uneven.h.context.bstate.scenario, uneven.shown(), uneven.size()], ["one-squishy", ["one-squishy"], ["1"]]);

  // a serve that ranks fewer sizes offers the ones it has, and one that ranks
  // a single size has nothing to pick
  const fewer = await open([...full, ...four]);
  assert.deepEqual([fewer.offered(), fewer.size(), fewer.h.element("builds-size").hidden], [["4", "5"], ["5"], false]);
  await fewer.pickSize(4);
  assert.deepEqual([fewer.h.context.bstate.scenario, fewer.shown()], ["four-overall", keys(four)]);
  const old = await open(full);
  assert.deepEqual([old.h.element("builds-size").hidden, old.offered(), old.shown(), old.words()],
    [true, ["5"], keys(full), ["five items", "five"]]);
  ++passedSuites;
}

async function checkBuildsFights() {
  // A fight runs until the dummy dies. One the time limit ends instead reads
  // "can't kill" wherever its kill time would stand, in the champion's table
  // and in the breakdown below it.
  const targets = [
    { key: "one-squishy", target: "squishy", targetHp: 2800, targetBonusHp: 300, armor: 110, mr: 60, duration: 120 },
    { key: "one-tank", target: "tank", targetHp: 4800, targetBonusHp: 1200, armor: 220, mr: 160, duration: 120 },
  ];
  const kill = (ttk, total, loss) => ({ ttk, ttkExp: ttk, killTime: ttk, loss, dps: Math.round(total / ttk), total, attacks: 9, breakdown: { auto: total } });
  // still standing at the limit: no kill time, and the time its damage would have needed
  const short = { ttk: null, ttkExp: null, killTime: 131.17, loss: 1.36, dps: 37, total: 4391, attacks: 80, breakdown: { Q: 2391, E: 2000 } };
  const build = (rank, item, vs) => ({ rank, items: ["Berserker's Greaves", item], buyOrder: [item], gold: 4100, ap: 0, ad: 120, attackSpeed: 1.1, vs });
  const fights = [
    { squishy: kill(8.34, 2800, 1), tank: kill(96.5, 4800, 1) },
    { squishy: kill(60.02, 2800, 7.2), tank: short },
  ];
  const own = (v, row) => ({ ...row, ttk: v.ttk, ttkExp: v.ttkExp, dps: v.dps, total: v.total, attacks: v.attacks, breakdown: v.breakdown });
  const cellFor = target => ({
    champion: "kayle", championName: "Kayle", ranks: { Q: 5, W: 3, E: 5, R: 3 }, buildsEvaluated: 525, itemsPatch: "16.23", kitPatch: "16.16",
    scenario: { ...targets.find(t => t.target === target), label: `One-item build vs ${target}`, tier: "one", slots: 2, level: 16, targets },
    rows: [own(fights[0][target], build(1, "Yun Tal Wildarrows", fights[0])), own(fights[1][target], build(2, "Guardian Angel", fights[1]))],
  });
  const h = loadDashboard({ api: async url => cellFor(/one-(\w+)\.json$/.exec(url)[1]) });
  const { context } = h;
  context.META = { tiers: ["soloq_masters_plus"], defaultTier: "soloq_masters_plus", platforms: [] };
  Object.assign(context.bstate, {
    champion: "kayle", items: new Map(),
    meta: { champions: [{ slug: "kayle", name: "Kayle" }], note: "Theoretical damage model.",
            scenarios: targets.map(t => ({ ...t, label: `One-item build vs ${t.target}`, tier: "one", slots: 2 })) },
  });
  const open = async key => { context.bstate.scenario = key; await context.loadScenario(); };
  const heads = () => h.queryAll("#btbl thead th").map(th => th.textContent);
  const rows = () => h.queryAll("#btbl tbody tr").map(tr => tr.children);
  const texts = () => rows().map(tds => tds.map(td => td.textContent).filter((_, i) => i !== 1));  // all but the icons
  const stood = "was still standing after 2 min, the longest a fight runs (4,391 damage dealt)";

  await open("one-tank");
  assert.deepEqual(heads(), ["#", "Build", "Kill time", "Expected", "vs squishy", "DPS", "Damage", "Gold"]);
  assert.deepEqual(texts(), [
    ["1", "96.50s", "96.50s", "8.34s best", "50", "4,800", "4,100"],
    ["2", "can't kill", "\u2014", "60.02s +620%", "37", "4,391", "4,100"],
  ]);
  assert.deepEqual(rows().map(tds => tds[2].title), ["", `The tank ${stood}`], "The cell says what was dealt, and how long the fight ran");
  // what the ranking is, a fact a bullet
  assert.deepEqual(h.element("scenario-desc").children.map(li => [li.tagName, li.textContent]), [
    ["li", "Level 16 (Q5/W3/E5/R3)"],
    ["li", "Squishy: 2,800 HP (300 bonus), 110 armor, 60 MR"],
    ["li", "Tank: 4,800 HP (1,200 bonus), 220 armor, 160 MR"],
    ["li", "Fights run to the kill, 2 min at most"],
    ["li", "525 builds ranked"],
    ["li", "Items patch 16.23, kit 16.16"],
    ["li", "Theoretical damage model."],
  ]);

  await open("one-squishy");
  assert.deepEqual(heads(), ["#", "Build", "Kill time", "Expected", "vs tank", "DPS", "Damage", "Gold"]);
  assert.deepEqual(texts(), [
    ["1", "8.34s", "8.34s", "96.50s best", "336", "2,800", "4,100"],
    ["2", "60.02s", "60.02s", "can't kill", "47", "2,800", "4,100"],
  ], "Against another target it reads the same, with no distance from the best to give");
  assert.deepEqual(rows().map(tds => tds[4].title), ["", `The tank ${stood}`]);
  assert.match(h.queryAll("#btbl thead th")[4].title, /against the tank \(4,800 HP \(1,200 bonus\), 220 armor, 160 MR\), and how far behind/);

  // the selected build's fights, below the table
  const groups = () => h.queryAll("#bd-bars .bd-group").map(el => el.textContent);
  assert.deepEqual(groups(), [
    "vs squishy (2,800 HP (300 bonus), 110 armor, 60 MR) \u2014 2,800 damage \u00b7 336 DPS \u00b7 9 attacks \u00b7 dies at 8.34s",
    "vs tank (4,800 HP (1,200 bonus), 220 armor, 160 MR) \u2014 4,800 damage \u00b7 50 DPS \u00b7 9 attacks \u00b7 dies at 96.50s",
  ]);
  await h.queryAll("#btbl tbody tr")[1].fire("click");
  assert.equal(groups()[1], "vs tank (4,800 HP (1,200 bonus), 220 armor, 160 MR) \u2014 4,391 damage \u00b7 37 DPS \u00b7 80 attacks \u00b7 can't kill within 2 min");

  assert(!/survive|\u2248|undefined|NaN/.test(h.element("view-builds").textContent),
    "Nothing on the tab says survives, or shows an estimated time");
  assert(!/survive|\u2248/.test([context.MEAN_TITLE, context.BOARD_TIME_TITLE, context.BOARD_MEAN_TITLE, context.EXPECTED_TITLE].join(" ")));
  ++passedSuites;
}

async function checkBuildsLeaderboard() {
  const calls = [];
  const log = name => (...args) => { calls.push([name, ...args]); };
  let respond = null;

  const h = loadDashboard({
    manualTimers: true,
    api: url => {
      calls.push(["api", url]);
      return respond(url);
    },
  });
  const { context, element, document } = h;
  const {
    boardColumns,
    boardRows,
    renderLeaderboard,
    loadLeaderboard,
    setBuildsMode,
    bstate,
    bboard,
    INSTANT_TITLE,
  } = context;

  for (const id of [
    "builds-mode-seg", "builds-note-board", "btbl-wrap", "bboard", "bboard-search",
    "bboard-status", "bboard-wrap", "bboard-tbl", "bboard-failed",
  ]) {
    assert.ok(element(id), `The page has #${id}`);
  }

  for (const key of ["full-squishy", "full-overall"]) {
    const btn = document.createElement("button");
    btn.dataset.key = key;
    element("scenario-seg").append(btn);
  }

  context.META = { tiers: ["soloq_masters_plus"], defaultTier: "soloq_masters_plus", platforms: [] };
  context.state.view = "builds";
  context.state.tier = "soloq_masters_plus";

  Object.assign(bstate, {
    mode: "leaderboard",
    champion: "kayle",
    scenario: "full-squishy",
    req: 0,
    timer: null,
    status: null,
    meta: {
      itemsPatch: "16.18",
      note: "Theoretical damage model.",
      champions: [
        { slug: "kayle", name: "Kayle" },
        { slug: "ahri", name: "Ahri" },
        { slug: "twitch", name: "Twitch" },
      ],
      scenarios: [
        { key: "full-squishy", label: "Full build vs squishy" },
        { key: "full-overall", label: "Full build \u2014 overall" },
      ],
    },
  });

  context.renderBreakdown = log("renderBreakdown");
  context.renderPool = log("renderPool");
  context.clearBreakdown = log("clearBreakdown");
  const realUpdateHash = context.updateHash;
  context.updateHash = (...args) => {
    calls.push(["updateHash", ...args]);
    return realUpdateHash(...args);
  };
  context.clearBuilds = log("clearBuilds");
  context.markCold = () => {};
  context.renderChampionPicker = log("renderChampionPicker");
  context.loadScenario = log("loadScenario");
  context.loadStatus = async () => { calls.push(["loadStatus"]); };
  context.buildCell = row => {
    const td = document.createElement("td");
    td.textContent = row.items.join(" + ");
    return td;
  };

  const took = name => calls.filter(c => c[0] === name);

  const targets = [
    { key: "full-squishy", target: "squishy", targetHp: 2800, armor: 110, mr: 60, duration: 120 },
    { key: "full-bruiser", target: "bruiser", targetHp: 3800, armor: 180, mr: 120, duration: 120 },
    { key: "full-tank", target: "tank", targetHp: 4800, armor: 220, mr: 160, duration: 120 },
  ];
  const fight = (ttk, extra = {}) => ({
    ttk, ttkExp: ttk, killTime: ttk, dps: ttk ? Math.round(3000 / ttk) : 0,
    total: 3000, attacks: 3, breakdown: { Q: 3000 }, ...extra,
  });
  const champ = (rank, champion, championName, times, extra = {}) => ({
    rank, champion, championName,
    items: ["Boots", `${championName}'s item`],
    gold: 16000 + rank, ap: 0, ad: 300, attackSpeed: 1.2,
    vs: {
      squishy: fight(times[0]),
      bruiser: fight(times[1]),
      // null: the time limit ended the fight with the tank still standing
      tank: times[2] == null ? fight(null, { ttkExp: null, killTime: 140.49, total: 4100 }) : fight(times[2]),
    },
    ...extra,
  });
  const squishy = {
    scenario: { ...targets[0], label: "Full build vs squishy", tier: "full", level: 16, targets },
    complete: true, expectedCount: 4, readyCount: 4, pending: [], failed: [],
    rows: [
      [1, "ahri", "Ahri", [0, 4.49, 7.69]],
      [2, "kassadin", "Kassadin", [0.75, 2.2, 5.1]],
      [2, "leesin", "Lee Sin", [0.75, 3.5, null]],
      [4, "kayle", "Kayle", [1.07, 2.76, 4.46]],
    ].map(([rank, slug, name, times]) => champ(rank, slug, name, times, {
      ttk: times[0], ttkExp: times[0], dps: times[0] ? Math.round(3000 / times[0]) : 0, total: 3000,
    })),
  };
  const overall = {
    scenario: { key: "full-overall", label: "Full build \u2014 overall", tier: "full", overall: true, level: 16, targets },
    complete: true, expectedCount: 2, readyCount: 2, pending: [], failed: [],
    rows: [
      champ(1, "ahri", "Ahri", [0, 4.49, 7.69], { kills: 3, mean: 0 }),
      champ(2, "kayle", "Kayle", [1.21, 2.5, 4.21], { kills: 3, mean: 2.34 }),
    ],
  };

  const heads = sc => plain(boardColumns(sc).map(c => c.th));
  assert.deepEqual(heads(squishy.scenario), ["#", "Champion", "Best build", "Kill time", "vs bruiser", "vs tank", "DPS", "Damage", "Gold"]);
  assert.deepEqual(heads(overall.scenario), ["#", "Champion", "Best build", "vs squishy", "vs bruiser", "vs tank", "Mean", "Gold"]);

  const body = () => h.query("#bboard-tbl tbody").children;
  const texts = () => body().map(tr => tr.children.map(td => td.textContent));
  const head = () => h.query("#bboard-tbl thead tr").children;
  const headTexts = () => head().map(th => th.textContent.trim());
  const show = (data, st = {}) => {
    Object.assign(bboard, { data, error: null, search: "", sortKey: "rank", sortDir: 1 }, st);
    calls.length = 0;
    document.focusHistory.length = 0;
    renderLeaderboard();
  };

  show(squishy);
  assert.deepEqual(headTexts(), ["# \u25b2", ...heads(squishy.scenario).slice(1)]);
  assert.deepEqual(texts(), [
    ["1", "Ahri", "Boots + Ahri's item", "0.00s instant", "4.49s", "7.69s", "\u2014", "3,000", "16,001"],
    ["2", "Kassadin", "Boots + Kassadin's item", "0.75s", "2.20s", "5.10s", "4,000", "3,000", "16,002"],
    ["2", "Lee Sin", "Boots + Lee Sin's item", "0.75s", "3.50s", "can't kill", "4,000", "3,000", "16,002"],
    ["4", "Kayle", "Boots + Kayle's item", "1.07s", "2.76s", "4.46s", "2,804", "3,000", "16,004"],
  ]);

  const [ahri, , leesin, kayle] = body();
  assert.equal(ahri.children[3].title, INSTANT_TITLE);
  assert.equal(ahri.children[1].children.length, 2, "The champion cell is its icon and name, no tag");
  assert.equal(ahri.children[1].children[0].src, "https://cdn.communitydragon.org/latest/champion/ahri/square");
  assert.equal(leesin.children[5].title, "The tank was still standing after 2 min, the longest a fight runs (4,100 damage dealt)");
  assert.equal(kayle.children[3].title, "");

  const facts = () => element("scenario-desc").children.map(li => li.textContent);
  // every scenario of a size lists the same targets, a bullet each
  const dummies = ["Squishy: 2,800 HP, 110 armor, 60 MR", "Bruiser: 3,800 HP, 180 armor, 120 MR", "Tank: 4,800 HP, 220 armor, 160 MR"];
  assert.deepEqual(facts().slice(0, 8), [
    "Level 16",
    ...dummies,
    "Ranked by expected kill time",
    "Fights run to the kill, 2 min at most",
    "Each champion with the best build of its own ranking",
    "Items patch 16.18",
  ]);
  assert.equal(element("bboard-status").textContent, "4 champions");
  assert.equal(element("bboard-failed").hidden, true);

  assert.deepEqual(body().map(tr => tr.className), ["", "", "", "sel"]);
  assert.deepEqual(plain(took("renderBreakdown")), [["renderBreakdown", plain(squishy.rows[3]), plain(squishy.scenario), "Kayle"]]);
  assert.deepEqual(plain(took("renderPool")), [["renderPool", "kayle"]]);

  bstate.champion = "twitch";
  show(squishy);
  assert.deepEqual(body().map(tr => tr.className), ["sel", "", "", ""]);
  assert.equal(took("renderPool")[0][1], "ahri");
  assert.equal(bstate.champion, "twitch");

  calls.length = 0;
  await body()[1].fire("click");
  assert.equal(bstate.champion, "kassadin");
  assert.deepEqual(body().map(tr => tr.classList.contains("sel")), [false, true, false, false]);
  assert.equal(took("renderBreakdown")[0][3], "Kassadin");
  assert.equal(took("updateHash").length, 1);

  bstate.scenario = "full-overall";
  show(overall);
  assert.deepEqual(texts(), [
    ["1", "Ahri", "Boots + Ahri's item", "0.00s instant", "4.49s", "7.69s", "0.00s instant", "16,001"],
    ["2", "Kayle", "Boots + Kayle's item", "1.21s", "2.50s", "4.21s", "2.34s", "16,002"],
  ]);
  assert.equal(body()[0].children[6].title, INSTANT_TITLE);
  assert.deepEqual(facts().slice(1, 6), [
    ...dummies,
    "Ranked on every target at once, by the geometric mean of expected kill times, champions that kill all of them first",
    "Fights run to the kill, 2 min at most",
  ]);
  bstate.scenario = "full-squishy";

  show(squishy, { search: " LEE" });
  assert.deepEqual(texts().map(r => r.slice(0, 2)), [["2", "Lee Sin"]]);
  assert.equal(element("bboard-status").textContent, "1 of 4 champions \u00b7 ranks are from the full board");
  show(squishy, { search: "lee sin" });
  assert.equal(body().length, 1);
  show(squishy, { search: "zzz" });
  assert.equal(body().length, 0);
  assert.equal(element("bboard-status").textContent, "No champion matches these filters.");
  assert.deepEqual(plain(took("clearBreakdown")), [["clearBreakdown"]]);
  assert.deepEqual(plain(boardRows(null)), []);

  show(squishy);
  element("bboard-search").value = "kay";
  await element("bboard-search").fire("input");
  assert.deepEqual(texts().map(r => r[1]), ["Kayle"]);
  element("bboard-search").value = "";
  await element("bboard-search").fire("input");
  assert.equal(body().length, 4);

  const names = () => body().map(tr => tr.children[1].children[1].textContent);
  const ranks = () => body().map(tr => tr.children[0].textContent);
  const sortButton = label => head().find(th => th.textContent.trim().replace(/ [\u25b2\u25bc]$/, "") === label).children[0];
  const sortBy = label => sortButton(label).fire("click");
  const ariaSort = () => head().map(th => th.attributes["aria-sort"] || "");

  bstate.champion = "kayle";
  show(squishy);
  assert.deepEqual(head().map(th => th.children[0]?.tag === "button"), [true, true, false, true, true, true, true, true, true]);
  assert.deepEqual(ariaSort(), ["ascending", "", "", "", "", "", "", "", ""]);
  assert.match(head()[5].title, /^The same build's expected kill time against the tank/);

  element("bboard-wrap").scrollTop = 250;
  calls.length = 0;
  document.focusHistory.length = 0;
  document.activeElement = sortButton("vs tank");
  await sortBy("vs tank");
  assert.deepEqual(names(), ["Kayle", "Kassadin", "Ahri", "Lee Sin"]);
  assert.deepEqual(ranks(), ["4", "2", "1", "2"]);
  assert.deepEqual(headTexts(), ["#", "Champion", "Best build", "Kill time", "vs bruiser", "vs tank \u25b2", "DPS", "Damage", "Gold"]);
  assert.deepEqual(ariaSort(), ["", "", "", "", "", "ascending", "", "", ""]);
  assert.equal(element("bboard-status").textContent, "4 champions \u00b7 sorted by vs tank, fastest first");
  assert.equal(element("bboard-wrap").scrollTop, 0);
  assert.deepEqual(document.focusHistory, [sortButton("vs tank")]);
  assert.equal(took("updateHash").length, 1);
  assert.deepEqual([bboard.sortKey, bboard.sortDir], ["vs-tank", 1]);
  assert.deepEqual(body().map(tr => tr.className), ["sel", "", "", ""]);
  assert.equal(took("renderBreakdown")[0][3], "Kayle");

  document.focusHistory.length = 0;
  renderLeaderboard();
  assert.deepEqual(document.focusHistory, [sortButton("vs tank")]);
  document.activeElement = element("bboard-search");
  renderLeaderboard();
  assert.equal(document.focusHistory.length, 1);
  document.activeElement = null;

  await sortBy("vs tank");
  assert.deepEqual(names(), ["Lee Sin", "Ahri", "Kassadin", "Kayle"]);
  assert.equal(headTexts()[5], "vs tank \u25bc");
  assert.equal(ariaSort()[5], "descending");
  assert.equal(element("bboard-status").textContent, "4 champions \u00b7 sorted by vs tank, slowest first");

  const odd = plain(squishy);
  odd.rows[2].vs.tank.killTime = 1;
  show(odd, { sortKey: "vs-tank" });
  assert.deepEqual(names(), ["Kayle", "Kassadin", "Ahri", "Lee Sin"]);

  show(squishy, { sortKey: "vs-squishy" });
  assert.deepEqual(names(), ["Ahri", "Kassadin", "Lee Sin", "Kayle"]);
  assert.equal(headTexts()[3], "Kill time \u25b2");
  await sortBy("Kill time");
  assert.deepEqual(names(), ["Kayle", "Kassadin", "Lee Sin", "Ahri"]);

  show(squishy);
  await sortBy("DPS");
  assert.deepEqual(names(), ["Kassadin", "Lee Sin", "Kayle", "Ahri"]);
  assert.equal(element("bboard-status").textContent, "4 champions \u00b7 sorted by DPS, highest first");
  await sortBy("DPS");
  assert.deepEqual(names(), ["Kayle", "Kassadin", "Lee Sin", "Ahri"]);

  const blank = plain(squishy);
  delete blank.rows[0].vs.bruiser;
  show(blank, { sortKey: "vs-bruiser", sortDir: -1 });
  assert.deepEqual(names(), ["Lee Sin", "Kayle", "Kassadin", "Ahri"]);

  show(squishy);
  await sortBy("Damage");
  assert.deepEqual([bboard.sortKey, bboard.sortDir], ["damage", -1]);
  await sortBy("Gold");
  assert.deepEqual(names(), ["Ahri", "Kassadin", "Lee Sin", "Kayle"]);
  assert.equal(element("bboard-status").textContent, "4 champions \u00b7 sorted by Gold, cheapest first");
  await sortBy("Champion");
  assert.deepEqual(names(), ["Ahri", "Kassadin", "Kayle", "Lee Sin"]);
  const byName = boardColumns(squishy.scenario)[1].sort;
  assert.ok(byName({ championName: "Kog'Maw" }) < byName({ championName: "K'Sante" }));
  await sortBy("#");
  assert.deepEqual(ranks(), ["1", "2", "2", "4"]);
  assert.equal(element("bboard-status").textContent, "4 champions");
  await sortBy("#");
  assert.deepEqual(names(), ["Kayle", "Kassadin", "Lee Sin", "Ahri"]);
  assert.equal(element("bboard-status").textContent, "4 champions \u00b7 sorted by #, worst first");

  show(squishy, { search: "ka", sortKey: "vs-tank" });
  assert.deepEqual(names(), ["Kayle", "Kassadin"]);
  assert.equal(element("bboard-status").textContent, "2 of 4 champions \u00b7 ranks are from the full board \u00b7 sorted by vs tank, fastest first");

  bstate.scenario = "full-overall";
  show(overall, { sortKey: "vs-tank" });
  assert.deepEqual(names(), ["Kayle", "Ahri"]);
  await sortBy("Mean");
  assert.deepEqual(names(), ["Ahri", "Kayle"]);
  assert.equal(element("bboard-status").textContent, "2 champions \u00b7 sorted by Mean, fastest first");
  await sortBy("Mean");
  assert.deepEqual(names(), ["Kayle", "Ahri"]);

  show(overall, { sortKey: "dps", sortDir: -1 });
  assert.deepEqual(names(), ["Ahri", "Kayle"]);
  assert.equal(headTexts()[0], "# \u25b2");
  assert.equal(element("bboard-status").textContent, "2 champions");
  await sortBy("#");
  assert.deepEqual(names(), ["Kayle", "Ahri"]);
  assert.deepEqual([bboard.sortKey, bboard.sortDir], ["rank", -1]);
  bstate.scenario = "full-squishy";

  const savedOpenBuilds = context.openBuilds;
  context.openBuilds = () => {};
  Object.assign(bboard, { search: "", sortKey: "rank", sortDir: 1 });
  realUpdateHash();
  assert.ok(!/lbs|lbd/.test(h.location.hash), `Ranked default adds no sort params: ${h.location.hash}`);
  for (const [sk, sd, lbs, lbd] of [["vs-tank", 1, "vs-tank", undefined], ["dps", -1, "dps", "d"], ["rank", -1, "rank", "d"]]) {
    Object.assign(bboard, { sortKey: sk, sortDir: sd });
    realUpdateHash();
    const written = h.location.hash;
    const parsed = Object.fromEntries(new URLSearchParams(written.slice(1)));
    assert.deepEqual([parsed.bview, parsed.lbs, parsed.lbd], ["leaderboard", lbs, lbd], written);
    Object.assign(bboard, { search: "", sortKey: "rank", sortDir: 1 });
    h.location.hash = written;
    context.restoredHash = null;
    context.restoreHash();
    assert.deepEqual([bboard.sortKey, bboard.sortDir], [sk, sd], written);
  }
  h.location.hash = "#tab=builds&bview=leaderboard&lbs=nonsense";
  context.restoredHash = null;
  context.restoreHash();
  context.openBuilds = savedOpenBuilds;
  show(squishy, { sortKey: bboard.sortKey });
  assert.deepEqual(ranks(), ["1", "2", "2", "4"]);

  bstate.status = { warmer: "running" };
  show({
    ...squishy,
    complete: false,
    expectedCount: 9,
    readyCount: 6,
    failed: [{ champion: "zed", championName: "Zed", error: "PanicException: boom" }],
  });
  assert.equal(
    element("bboard-status").textContent,
    "4 champions \u00b7 6 of 9 computed for the current code and data (the rest are warming in the background): ranks are provisional"
  );
  assert.equal(element("bboard-failed").hidden, false);
  assert.equal(element("bboard-failed").textContent, "Not ranked: Zed (PanicException: boom).");
  bstate.status = { warmer: "stale" };
  renderLeaderboard();
  assert.match(element("bboard-status").textContent, /\(serve is running older code than is on disk/);

  element("bboard-wrap").scrollTop = 420;
  renderLeaderboard();
  assert.equal(element("bboard-wrap").scrollTop, 420);
  element("bboard-wrap").scrollTop = 0;

  show({
    ...squishy,
    failed: [{ champion: "vex", championName: "Vex", error: "Saved result could not be read: invalid JSON" }],
  });
  assert.equal(body().length, 4);
  assert.equal(element("bboard-failed").textContent, "Not ranked: Vex (Saved result could not be read: invalid JSON).");

  show(squishy);
  calls.length = 0;
  let stopped = false;
  const open = body()[0].children[1].children[1];
  assert.equal(open.tag, "button");
  await open.fire("click", { stopPropagation() { stopped = true; } });
  assert.ok(stopped);
  assert.deepEqual([bstate.mode, bstate.champion, bstate.scenario], ["champion", "ahri", "full-squishy"]);
  assert.equal(took("loadScenario").length, 1);
  assert.equal(took("clearBuilds").length, 1);
  assert.equal(took("renderChampionPicker").length, 1);
  const hidden = ids => ids.map(id => element(id).hidden);
  assert.deepEqual(hidden(["bboard", "builds-note-board"]), [true, true]);
  assert.deepEqual(hidden(["champion-seg", "btbl-wrap", "builds-note"]), [false, false, false]);
  assert.deepEqual(h.queryAll("#builds-mode-seg button").map(b => b.attributes["aria-pressed"]), ["true", "false"]);

  bboard.scroll = 0;
  element("bboard-wrap").scrollTop = 0;
  respond = async () => squishy;
  calls.length = 0;
  h.timers.length = 0;
  setBuildsMode("leaderboard");
  assert.deepEqual(hidden(["bboard", "champion-seg", "btbl-wrap", "builds-note"]), [false, true, true, true]);
  assert.equal(body().length, 4);
  assert.deepEqual(plain(took("api")), [["api", "api/builds/leaderboard/full-squishy.json"]]);
  assert.ok(h.queryAll("#scenario-seg button")[0].classList.contains("active"));
  await new Promise(setImmediate);
  assert.equal(h.timers.filter(t => !t.cleared).length, 0);
  assert.equal(took("loadStatus").length, 0);

  element("bboard-wrap").scrollTop = 333;
  bstate.data = { champion: "ahri", scenario: { key: "full-squishy" } };
  calls.length = 0;
  setBuildsMode("champion");
  assert.equal(took("clearBuilds").length, 0);
  element("bboard-wrap").scrollTop = 0;
  setBuildsMode("leaderboard");
  assert.equal(element("bboard-wrap").scrollTop, 333);
  await new Promise(setImmediate);

  bstate.scenario = "full-overall";
  h.timers.length = 0;
  calls.length = 0;
  respond = async () => ({ ...overall, complete: false, readyCount: 1 });
  const loading = loadLeaderboard();
  assert.equal(body().length, 0);
  assert.equal(element("scenario-status").textContent, "Loading\u2026");
  await loading;
  assert.equal(body().length, 2);
  assert.equal(took("loadStatus").length, 1);
  assert.equal(h.timers.filter(t => !t.cleared).length, 1);
  respond = async () => overall;
  const pollFn = h.timers.pop().fn;
  pollFn();
  await new Promise(setImmediate);
  assert.equal(h.timers.filter(t => !t.cleared).length, 0);
  assert.equal(element("bboard-status").textContent, "2 champions");

  h.timers.length = 0;
  respond = async () => { throw new Error("HTTP 404"); };
  await loadLeaderboard();
  assert.equal(body().length, 2);
  assert.match(element("bboard-status").textContent, /^The leaderboard could not be loaded \(HTTP 404\)\. Retrying shortly\. Showing the board as last loaded\.$/);
  assert.equal(h.timers.length, 1);

  h.timers.length = 0;
  respond = async () => { throw new Error("HTTP 500"); };
  await loadLeaderboard();
  assert.equal(body().length, 2);
  assert.match(element("bboard-status").textContent, /HTTP 500.*Retrying shortly/);
  assert.equal(h.timers.length, 1);

  h.timers.length = 0;
  bboard.data = null;
  respond = async () => squishy;
  await loadLeaderboard();
  assert.match(element("bboard-status").textContent, /could not be loaded \(unexpected response\)/);
  assert.equal(body().length, 0);

  h.timers.length = 0;
  let release;
  respond = () => new Promise(resolve => { release = () => resolve(overall); });
  const slow = loadLeaderboard();
  bstate.scenario = "full-squishy";
  respond = async () => squishy;
  const fast = loadLeaderboard();
  release();
  await Promise.all([slow, fast]);
  assert.equal(bboard.data.scenario.key, "full-squishy");
  assert.equal(body().length, 4);

  const boardArg = process.argv.indexOf("--board");
  if (boardArg > 0) {
    const d = JSON.parse(fs.readFileSync(process.argv[boardArg + 1], "utf8"));
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
    const rks = d.rows.map(r => r.rank);
    assert.deepEqual(rks, [...rks].sort((a, b) => a - b));
    const bySlug = new Map(d.rows.map(r => [r.champion, r]));
    const scalar = v => (Array.isArray(v) ? v[0] * 1e6 + v[1] : v);
    const sortable = boardColumns(d.scenario).filter(c => c.sort);
    for (const c of sortable) {
      for (const sd of [1, -1]) {
        show(d, { sortKey: c.key, sortDir: sd });
        const shown = body().map(tr => tr.dataset.champion);
        assert.deepEqual([...shown].sort(), [...bySlug.keys()].sort());
        const values = shown.map(slug => c.sort(bySlug.get(slug)));
        const blankIdx = values.findIndex(v => v == null);
        assert.ok(values.slice(blankIdx < 0 ? values.length : blankIdx).every(v => v == null));
        values.filter(v => v != null).map(scalar).forEach((v, i, all) => {
          if (i) assert.ok(sd === 1 ? all[i - 1] <= v : all[i - 1] >= v);
        });
      }
    }
    console.log(`Saved leaderboard verified: ${path.basename(process.argv[boardArg + 1])} (${d.rows.length} rows).`);
  }
  ++passedSuites;
}

async function checkChampionTabRemoved() {
  assert(!INDEX_HTML.includes('data-view="champion"'), "Champion nav button is absent from index.html");
  assert(!INDEX_HTML.includes('id="view-champion"'), "Champion view section is absent from index.html");
  assert(!INDEX_HTML.includes('id="champ-search"'), "Champion search input is absent from index.html");
  assert(!STYLES_CSS.includes(".chartwrap"), "Champion chart CSS is absent");
  assert(!STYLES_CSS.includes("#tbl tbody tr { cursor: pointer"), "Overview row pointer CSS is absent");

  const meta = {
    tiers: ["soloq_masters_plus"],
    defaultTier: "soloq_masters_plus",
    platforms: ["na1"],
    inventory: [],
  };
  const overview = {
    patches: ["16.1"],
    totalGames: 2750,
    rows: [
      { c: "kayle", l: "top", wr: [44, 45, 48, 51, 54, 56, 58], g: [50, 200, 300, 300, 200, 200, 1500], total: 2750 },
    ],
  };

  const h = loadDashboard({
    initialHash: "#view=champion&name=kayle",
    api: async url => {
      assert(!url.startsWith("api/champion"), `Retired champion endpoint requested: ${url}`);
      if (url === "api/meta.json") return meta;
      if (url === "api/rows/soloq_masters_plus.json") return overview;
      if (url === "api/inventory.json") return { tiers: [], jobs: [], services: [] };
      throw new Error(`Unexpected API request: ${url}`);
    },
  });

  assert.equal(h.element("view-champion"), null);
  assert.equal(h.query('nav button[data-view="champion"]'), null);
  await h.context.init();
  assert.equal(h.context.state.view, "overview");
  assert(h.element("view-overview").classList.contains("active"));
  assert.equal(h.location.hash, "#tab=overview&tier=soloq_masters_plus");

  await h.setHash("#tab=champion&name=kayle");
  assert.equal(h.context.state.view, "overview");
  assert(!h.location.hash.includes("champion"));
  assert(!h.location.hash.includes("name="));

  const row = h.query("#tbl tbody tr");
  assert.ok(row, "Overview table rendered a champion row");
  row.click();
  assert.equal(h.context.state.view, "overview");
  assert.equal(h.location.hash, "#tab=overview&tier=soloq_masters_plus");

  ++passedSuites;
}

async function checkMatchups() {
  const archivePath = path.join(__dirname, "..", "data", "matchups", "matchups.json");
  const matchupsArchive = JSON.parse(fs.readFileSync(archivePath, "utf8"));

  const meta = {
    tiers: ["soloq_masters_plus"],
    defaultTier: "soloq_masters_plus",
    platforms: ["na1", "kr"],
    inventory: [],
  };
  const overview = {
    patches: ["16.18"],
    totalGames: 10000,
    rows: [
      { c: "aatrox", l: "top", wr: [50, 50, 50, 50, 50, 50, 51], g: [50, 200, 300, 300, 200, 200, 800], total: 2050 },
    ],
  };

  // 1. Error and empty-snapshot states.
  let mode = "error";
  const hErr = loadDashboard({
    api: async url => {
      if (url === "api/meta.json") return meta;
      if (url === "api/rows/soloq_masters_plus.json") return overview;
      if (url === "api/matchups.json") {
        if (mode === "error") throw new Error("HTTP 503");
        return { synced: null, archetypes: [], champions: [], pairs: {} };
      }
      throw new Error(`Unexpected API request: ${url}`);
    },
  });
  await hErr.context.init();
  await hErr.query('nav button[data-view="matchups"]').click();
  await until(() => !hErr.context.mstate.loading);
  assert.match(hErr.element("mu-provenance").textContent, /HTTP 503/);
  assert.equal(hErr.element("mu-identity-panel").hidden, true);

  mode = "empty";
  await hErr.context.openMatchups();
  assert.match(hErr.element("mu-provenance").textContent, /cli\.py matchups sync/);
  assert.equal(hErr.element("mu-identity-panel").hidden, true);

  // 2. Full real-archive flow + hash routing.
  const h = loadDashboard({
    api: async url => {
      if (url === "api/meta.json") return meta;
      if (url === "api/rows/soloq_masters_plus.json") return overview;
      if (url === "api/matchups.json") return matchupsArchive;
      throw new Error(`Unexpected API request: ${url}`);
    },
  });
  await h.context.init();
  await h.query('nav button[data-view="matchups"]').click();
  await until(() => !h.context.mstate.loading);
  assert.equal(h.context.state.view, "matchups");
  assert(h.element("view-matchups").classList.contains("active"));
  assert.equal(h.element("mu-identity-panel").hidden, false);
  assert.equal(h.element("mu-archetypes-panel").hidden, true);
  assert.match(h.element("mu-provenance").textContent, /Master\+ games/);

  // Default first alphabetical champion is Aatrox (top).
  assert.equal(h.context.mstate.champion, "aatrox");
  assert.equal(h.element("mu-hero-name").textContent, "Aatrox");
  assert.equal(h.element("mu-own-traits").children.length, 8);
  assert.equal(h.element("mu-comp-needs").children.length, 3);
  assert.equal(h.element("mu-opp-traits").children.length, 11);
  assert.match(h.element("mu-drivers-r2").textContent, /Archetypes explain \d+% of variance/);

  // Leave-one-out class-breakers detect Malphite against Aatrox top.
  assert.equal(h.element("mu-outliers-wrap").hidden, false);
  const outlierSlugs = h.queryAll("#mu-outliers .mu-outlier-card").map(el => el.dataset.champion);
  assert(outlierSlugs.includes("malphite"), `Expected malphite in Aatrox outliers: ${outlierSlugs}`);

  // Same-lane pool companions populate 5 twins and 5 cover picks.
  assert.equal(h.element("mu-twins").children.length, 5);
  assert.equal(h.element("mu-covers").children.length, 5);

  // "All" in the roster lane filter shows every playable (champion, role) pair,
  // matching the exact union of Top + Jungle + Middle + Bottom + Support.
  const allTiles = h.queryAll("#mu-champ-grid .mu-champ-btn");
  assert(allTiles.length > matchupsArchive.champions.length, "All lists multi-role champions across all playable lanes");
  const aatroxLanes = allTiles.filter(b => b.dataset.champion === "aatrox").map(b => b.dataset.lane);
  assert(aatroxLanes.length >= 2 && aatroxLanes[0] === "top", `Expected multiple playable lanes for Aatrox, got ${aatroxLanes}`);
  let laneSum = 0;
  for (const lane of ["top", "jungle", "middle", "bottom", "support"]) {
    await h.query(`#mu-role-filter button[data-role="${lane}"]`).click();
    laneSum += h.queryAll("#mu-champ-grid .mu-champ-btn").length;
  }
  await h.query('#mu-role-filter button[data-role="all"]').click();
  assert.equal(h.queryAll("#mu-champ-grid .mu-champ-btn").length, laneSum);

  // Filter roster search + select Yasuo (lists middle, top, bottom under All).
  const searchInput = h.element("mu-search");
  searchInput.value = "yasuo";
  await searchInput.fire("input");
  const visibleBtns = h.queryAll("#mu-champ-grid .mu-champ-btn");
  const yasuoLanes = visibleBtns.filter(b => b.dataset.champion === "yasuo").map(b => b.dataset.lane);
  assert.deepEqual(yasuoLanes, ["middle", "top", "bottom"]);
  await visibleBtns.find(b => b.dataset.champion === "yasuo" && b.dataset.lane === "top").click();
  assert.equal(h.context.mstate.champion, "yasuo");
  assert.equal(h.context.mstate.lane, "top");
  assert.match(h.location.hash, /mchamp=yasuo&mlane=top/);
  await visibleBtns.find(b => b.dataset.champion === "yasuo" && b.dataset.lane === "middle").click();
  assert.equal(h.context.mstate.champion, "yasuo");
  assert.equal(h.context.mstate.lane, "middle");
  assert.equal(h.element("mu-hero-name").textContent, "Yasuo");
  assert.match(h.location.hash, /mchamp=yasuo&mlane=middle/);

  // Switch synergy layout to flat and verify physical knockup partners appear in Kit synergy.
  await h.query('#mu-syn-layout button[data-key="flat"]').click();
  const kitPartners = h.queryAll("#mu-syn-body .mu-flat-col")[0]
    .querySelectorAll(".mu-flat-item")
    .map(b => b.dataset.champion);
  assert(
    kitPartners.includes("wukong") || kitPartners.includes("vi") || kitPartners.includes("rell"),
    `Expected knockup partner in Yasuo top kit synergies, got: ${kitPartners}`
  );

  // Toggle Total draft synergy and Enemy comp counter scope; verify hash updates.
  await h.query('#mu-syn-mode button[data-key="total"]').click();
  assert.match(h.location.hash, /msyn=total/);
  await h.query('#mu-ctr-scope button[data-key="team"]').click();
  assert.match(h.location.hash, /mscope=team/);
  assert.equal(h.element("mu-ctr-role").hidden, false);

  // Jump to Archetypes view via hero archetype badge.
  await h.element("mu-hero-arch").click();
  assert.equal(h.context.mstate.mode, "archetypes");
  assert.equal(h.element("mu-archetypes-panel").hidden, false);
  assert.equal(h.element("mu-identity-panel").hidden, true);
  assert.match(h.location.hash, /mview=archetypes/);

  // 14x14 matrix renders 14 rows and populates detail panel.
  const matRows = h.queryAll("#mu-matrix-tbl tbody tr");
  assert.equal(matRows.length, 14);
  assert.equal(matRows[0].children.length, 15);
  assert.equal(h.element("mu-matrix-detail").hidden, false);

  // Click cell (0, 2) and switch matrix mode to Kit synergy.
  const targetCell = h.query('#mu-matrix-tbl .mu-mat-cell[data-row="0"][data-col="2"]');
  await targetCell.click();
  assert.match(h.location.hash, /mcell=0-2/);
  assert(targetCell.classList.contains("sel"));

  await h.query('#mu-mat-mode button[data-key="kit"]').click();
  assert.match(h.location.hash, /mmat=kit/);

  // Filter archetypes by support lane, then click a member chip back to Identity.
  await h.query('#mu-arch-role button[data-role="support"]').click();
  assert.match(h.location.hash, /marole=support/);
  const supCards = h.queryAll("#mu-arch-grid .mu-arch-card");
  assert(supCards.length > 0 && supCards.length < 14);

  const memberChip = h.query("#mu-arch-grid .mu-chip");
  const pickedSlug = memberChip.dataset.champion;
  await memberChip.click();
  assert.equal(h.context.mstate.mode, "identity");
  assert.equal(h.context.mstate.champion, pickedSlug);

  // Browser Back returns to Archetypes view; Forward returns to picked champion.
  await h.back();
  assert.equal(h.context.mstate.mode, "archetypes");
  await h.forward();
  assert.equal(h.context.mstate.mode, "identity");
  assert.equal(h.context.mstate.champion, pickedSlug);

  // No NaN, undefined, or em dashes in rendered Matchups DOM.
  const viewText = h.element("view-matchups").textContent;
  assert(!/undefined|NaN/.test(viewText), "Rendered Matchups view contains undefined or NaN");
  assert(!viewText.includes("\u2014"), "Rendered Matchups view contains an em dash");

  ++passedSuites;
}

(async () => {
  await checkChampionTabRemoved();
  checkOnetricks();
  checkBuildsItems();
  await checkBuildsPicker();
  await checkBuildsSizes();
  await checkBuildsFights();
  await checkBuildsLeaderboard();
  await checkMatchups();
  console.log(`LoL UI tests passed (${passedSuites} suites: champion-retirement, onetricks, items, picker, build-sizes, fights, leaderboard, matchups).`);
})().catch(err => {
  console.error(err);
  process.exit(1);
});
