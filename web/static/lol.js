"use strict";

// ---------------- builds view ----------------
// mode: "champion" (one champion's ranked builds) or "leaderboard" (every
// champion's best build, side by side: see the leaderboard section below)
const bstate = { mode: "champion", meta: null, items: null, champion: null, scenario: null,
                 data: null, sel: 0, status: null, req: 0, timer: null };
const fmtDur = s => s < 90 ? `${Math.round(s)}s` : `${Math.round(s / 60)} min`;

// ---- the Survival tier: a tank's builds, ranked on how long they last ----
// A champion's own scenarios: a tank sees the Survival tier, a carry the
// damage tiers. A serve older than the field lists every scenario.
function championScenarios(meta, slug) {
  const all = (meta?.scenarios || []).map(s => s.key);
  const champ = meta?.champions?.find(c => c.slug === slug);
  return champ?.scenarios?.length ? champ.scenarios.filter(k => all.includes(k)) : all;
}

// "survival" for a Survival cell (and for a tank's scenarios), else "damage".
function scenarioObjective(sc) {
  return sc?.objective === "survival" ? "survival" : "damage";
}

// A Survival fight's time to die: the kill time, or ≈ the time the damage
// taken would have needed when the tank outlived the fight.
function ttdText(v) {
  if (!v || v.ttd == null) return "—";
  return v.died ? `${v.ttd.toFixed(2)}s` : `≈${v.ttd.toFixed(1)}s`;
}

// How far short of the attacker's longest-lived build this one falls.
function shareText(share) {
  if (share == null) return "";
  return share > 0.995 ? " best" : ` −${Math.round((1 - share) * 100)}%`;
}

// What kept the tank up, largest first: heals, shields and damage kept off.
const DEFENSE_LABELS = [
  ["healedR", "Maximum Dosage heal"], ["rBaseHealth", "Maximum Dosage base health"],
  ["healedW", "Heart Zapper heal"], ["regen", "Regeneration"],
  ["healedLifeline", "Protoplasm heal"], ["lifelineHealth", "Protoplasm bonus health"],
  ["healedDrain", "Unending Despair"], ["reviveHealth", "Guardian Angel revive"],
  ["shieldMagic", "Magic shield (Kaenic)"], ["shieldLifeline", "Lifeline shield"],
  ["reducedCrit", "Randuin's (crits)"], ["reducedAttack", "Steelcaps (attacks)"],
  ["blocked", "Spell shield"], ["negated", "Stasis"],
  ["deferred", "Death's Dance (delayed)"],
];
function defenseRows(d) {
  return DEFENSE_LABELS.filter(([k]) => d?.[k] > 0).map(([k, label]) => [label, d[k]])
    .sort((a, b) => b[1] - a[1]);
}

// The moments that decided a fight, in the order they happened. `v` is a
// row's fight against one attacker (its `defense` report and threshold).
function defenseEvents(v) {
  const d = v?.defense;
  if (!d) return [];
  const at = (t, what) => [t, `${what} at ${t.toFixed(2)}s`];
  const out = [];
  if (d.rAt != null) {
    const [, text] = at(d.rAt, "Maximum Dosage");
    out.push([d.rAt, text + (v.threshold ? ` (at or below ${Math.round(v.threshold * 100)}% health)`
                                         : " (to live through a killing blow)")]);
  }
  if (d.lifelineAt != null) out.push(at(d.lifelineAt, "lifeline"));
  if (d.zhonyaAt != null) out.push(at(d.zhonyaAt, "Zhonya's"));
  if (d.reviveAt != null) out.push(at(d.reviveAt, "Guardian Angel"));
  if (d.voidbornAt != null) out.push(at(d.voidbornAt, "Jak'Sho's resists"));
  if (d.steadfastAt != null) out.push(at(d.steadfastAt, "Force of Nature's 70 MR"));
  if (d.wCasts) out.push([Infinity, `Heart Zapper ×${d.wCasts}`]);
  return out.sort((a, b) => a[0] - b[0]).map(([, text]) => text);
}

async function loadBuildsMeta() {
  if (bstate.meta) return true;
  try {
    bstate.meta = await api("api/builds/meta.json");
  } catch (e) {
    document.getElementById("builds-note").textContent =
      "Builds API not reachable: restart `cli.py serve` to pick up the new endpoints.";
    return false;
  }
  bstate.items = buildItemLookup(bstate.meta);
  if (!bstate.meta.champions.some(c => c.slug === bstate.champion))
    bstate.champion = bstate.meta.champions[0]?.slug;
  if (!bstate.meta.scenarios.some(s => s.key === bstate.scenario))
    bstate.scenario = bstate.meta.scenarios[0]?.key;
  renderChampionPicker();
  const seg = document.getElementById("scenario-seg");
  seg.replaceChildren(...bstate.meta.scenarios.map(sc => {
    const btn = document.createElement("button");
    btn.textContent = sc.label;
    btn.dataset.key = sc.key;
    btn.addEventListener("click", () => { bstate.scenario = sc.key; loadBuildsView(); });
    return btn;
  }));
  syncScenarioSeg();
  renderPool();
  return true;
}

function pickChampion(slug) {
  bstate.champion = slug;
  renderChampionPicker();
  syncScenarioSeg();
  renderPool();
  loadScenario();
}

// The hand-encoded, reviewed champions are buttons. The machine-written ones
// (a kit and a rotation driver written by a model, compiled
// and sanity-checked but not reviewed) are too many for that: they are found
// by name, and the one selected joins the buttons with an "unreviewed" tag.
function renderChampionPicker() {
  const champs = bstate.meta.champions;
  const generated = champs.filter(c => c.generated);
  const shown = champs.filter(c => !c.generated || c.slug === bstate.champion);
  document.getElementById("champion-seg").replaceChildren(...shown.map(c => {
    const btn = document.createElement("button");
    btn.textContent = c.name || c.slug;
    btn.dataset.key = c.slug;
    btn.classList.toggle("active", c.slug === bstate.champion);
    if (c.generated) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = "unreviewed";
      btn.append(tag);
    }
    btn.addEventListener("click", () => pickChampion(c.slug));
    return btn;
  }));
  const more = document.getElementById("champion-more");
  more.hidden = !generated.length;
  if (!generated.length) return;
  const input = document.getElementById("champion-search");
  input.placeholder = `${generated.length} more champions, machine-written and unreviewed — type a name…`;
  document.getElementById("champion-list").replaceChildren(...generated.map(c => {
    const o = document.createElement("option");
    o.value = c.name || c.slug;
    return o;
  }));
  if (!input.dataset.wired) {
    input.dataset.wired = "1";
    const choose = () => {
      const q = input.value.trim().toLowerCase();
      const hit = bstate.meta.champions.find(c => c.generated &&
        ((c.name || "").toLowerCase() === q || c.slug === q.replace(/[^a-z0-9]/g, "")));
      if (!hit) return;
      input.value = "";
      input.blur();
      pickChampion(hit.slug);
    };
    input.addEventListener("change", choose);
    input.addEventListener("input", choose);
  }
}

// What a machine-written kit is, beside its rankings: the reader decides from
// the kit's own assumptions whether to trust them. The leaderboard shows it
// with the selected row's breakdown instead (`id`), below the table, where
// its changing height cannot move the rows under the pointer.
function renderGeneratedBanner(id = "builds-generated", slug = bstate.champion) {
  const el = document.getElementById(id);
  const champ = bstate.meta?.champions?.find(c => c.slug === slug);
  el.hidden = !champ?.generated;
  if (el.hidden) return;
  const parts = [
    `${champ.name || champ.slug}'s kit and rotation were written by a model from the wiki and ` +
    "Riot's game file, compiled and sanity-checked, and NOT reviewed by a person: treat this " +
    "ranking as a draft. A driver written this way for a hand-modeled champion ranked builds " +
    "much like the hand-written one when its rotation agreed, and very differently when it did " +
    "not, so read the rotation it assumes (the notes under the item pool).",
  ];
  if (champ.assumed?.length) parts.push(`Assumed numbers: ${champ.assumed.join("; ")}.`);
  if (champ.unused?.length) parts.push(`Abilities it does not use: ${champ.unused.join("; ")}.`);
  el.textContent = parts.join(" ");
}

// Show the selected champion's scenarios only (a tank's are the Survival
// tier's), keeping the scenario if it is one of them. The leaderboard shows
// the scenarios it has a board for.
function syncScenarioSeg() {
  const keys = bstate.mode === "leaderboard" ? leaderboardScenarios(bstate.meta)
                                             : championScenarios(bstate.meta, bstate.champion);
  document.querySelectorAll("#scenario-seg button").forEach(b => {
    b.hidden = !keys.includes(b.dataset.key);
  });
  if (!keys.includes(bstate.scenario)) bstate.scenario = keys[0];
}

// `slug`: the champion the card is about — the selected one, or in the
// leaderboard the selected row's.
function renderPool(slug = bstate.champion) {
  const m = bstate.meta;
  const chip = it => {
    const s = document.createElement("span");
    s.className = "chip";
    s.append(it.name);
    const g = document.createElement("span");
    g.className = "gold";
    g.textContent = fmtInt(it.gold) + "g";
    s.append(g);
    return s;
  };
  // the pool is per champion: a kit can rule items out (mana items on Vladimir)
  const champ = m.champions.find(c => c.slug === slug);
  const ids = champ ? new Set(champ.pool) : null;
  const survival = champ?.objective === "survival";
  // a tank's pool is the Survival tier's (tank items, then the damage
  // pool's defensive ones)
  const every = [...m.pool, ...(m.tankPool || []).filter(t => !m.pool.some(p => p.id === t.id))]
    .sort((a, b) => a.name.localeCompare(b.name));
  const pool = ids ? every.filter(it => ids.has(it.id)) : m.pool;
  document.getElementById("pool-hint-damage").hidden = survival;
  document.getElementById("pool-hint-survival").hidden = !survival;
  const excluded = [...(champ?.excluded || []), ...m.excluded];
  document.getElementById("pool-title").textContent =
    `Item pool${champ ? " for " + (champ.name || champ.slug) : ""} — ` +
    `${pool.length} items + ${m.boots.length} boots (patch ${m.itemsPatch})`;
  const notes = champ?.notes || [];
  const noteEl = document.getElementById("pool-notes");
  noteEl.textContent = notes.join(" ");
  noteEl.hidden = !notes.length;
  document.getElementById("pool-chips").replaceChildren(...pool.map(chip));
  document.getElementById("boots-chips").replaceChildren(...m.boots.map(chip));
  document.querySelector("#pool-excluded summary").textContent =
    `Excluded items and why (${excluded.length})`;
  document.getElementById("pool-excluded-list").replaceChildren(
    ...excluded.map(txt => {
      const li = document.createElement("li");
      li.textContent = txt;
      return li;
    }));
}

// Scenario cells are precomputed (builds.warm), never simulated on request.
// The status endpoint says which cells exist and whether a warmer is running.
async function loadStatus() {
  bstate.status = await api("api/builds/status.json");
  markCold();
  return bstate.status;
}

// Dim the scenario buttons whose cell isn't computed for the selected champion
// (the leaderboard says how many champions it is still waiting for instead).
function markCold() {
  const ready = bstate.status?.ready;
  document.querySelectorAll("#scenario-seg button").forEach(b => {
    const cold = bstate.mode !== "leaderboard" && !!ready &&
      !ready[`${bstate.champion}/${b.dataset.key}`];
    b.classList.toggle("cold", cold);
    b.title = cold ? "not computed yet" : "";
  });
}

function clearBreakdown() {
  document.getElementById("bd-title").textContent = "Damage breakdown";
  document.getElementById("bd-hint").textContent = "";
  document.getElementById("bd-bars").replaceChildren();
}

function clearBuilds() {
  bstate.data = null;
  document.querySelector("#btbl tbody").replaceChildren();
  clearBreakdown();
}

async function loadScenario() {
  if (!bstate.champion) {
    document.getElementById("builds-note").textContent =
      "No champion kit encodings yet: add lol/data/builds/<champ>.json.";
    return;
  }
  if (!document.querySelector(`#champion-seg button[data-key="${bstate.champion}"]`))
    renderChampionPicker();  // a machine-written champion arriving by URL
  document.querySelectorAll("#champion-seg button").forEach(b =>
    b.classList.toggle("active", b.dataset.key === bstate.champion));
  document.querySelectorAll("#scenario-seg button").forEach(b =>
    b.classList.toggle("active", b.dataset.key === bstate.scenario));
  renderGeneratedBanner();
  markCold();
  updateHash();
  const req = ++bstate.req;  // a newer click supersedes this load
  clearTimeout(bstate.timer);
  const desc = document.getElementById("scenario-desc");
  desc.textContent = "Loading…";
  let d;
  try {
    d = await api(`api/builds/${bstate.champion}/${bstate.scenario}.json`);
  } catch (e) {
    if (req !== bstate.req) return;
    clearBuilds();
    desc.textContent = `Could not load this scenario (${e.message}).`;
    return;
  }
  if (req !== bstate.req) return;
  if (d.pending) {
    clearBuilds();
    showPending(req);
    return;
  }
  if (d.error) {
    // a machine-written driver that failed in the enumeration (builds.warm)
    clearBuilds();
    desc.textContent = `This champion's machine-written driver failed, so nothing is ranked: ${d.error}`;
    return;
  }
  bstate.data = d;
  bstate.sel = 0;
  const sc = d.scenario, rk = d.ranks;
  const survival = scenarioObjective(sc) === "survival";
  document.getElementById("builds-note").hidden = survival;
  document.getElementById("builds-note-survival").hidden = !survival;
  document.getElementById("builds-title").textContent =
    `${d.championName || d.champion} — ${survival ? "tankiest builds" : "ranked builds"}, ` +
    sc.label.toLowerCase();
  const when = d.computedAt
    ? new Date(d.computedAt).toLocaleString([], { dateStyle: "short", timeStyle: "short" })
    : null;
  if (survival) {
    const foe = a => `${a.name}'s best build (${a.items.join(", ")}), ${a.duration}s fight`;
    const me = (sc.attackers || []).find(a => a.key === sc.key);
    const what = sc.overall
      ? `ranked on every attacker at once — ${(sc.attackers || []).map(foe).join("; ")} — ` +
        "by the geometric mean of the times to die, builds fewer attackers kill first"
      : `vs ${me ? foe(me) : sc.attacker} · ranked by time to die, longest first`;
    desc.textContent =
      `Level ${sc.level} (Q${rk.Q}/W${rk.W}/E${rk.E}/R${rk.R}) · ${what}` +
      ` · ${fmtInt(d.buildsEvaluated)} builds ranked` +
      (when ? ` · computed ${when} in ${fmtDur(d.computeSeconds)}` : "") +
      ` · items patch ${d.itemsPatch}, kit ${d.kitPatch} · ${bstate.meta.survivalNote || ""}`;
    renderBuildsTable();
    return;
  }
  const dummy = t => `${fmtInt(t.targetHp)} HP, ${t.armor} armor, ${t.mr} MR, ${t.duration}s fight`;
  const what = sc.overall
    ? `ranked on every target at once — ${sc.targets.map(t => `${t.target} (${dummy(t)})`).join("; ")} — ` +
      "by the geometric mean of expected kill times, builds that kill all of them first"
    : `target ${dummy(sc)}`;
  desc.textContent =
    `Level ${sc.level} (Q${rk.Q}/W${rk.W}/E${rk.E}/R${rk.R}) · ${what}` +
    (sc.budget ? ` · ${fmtInt(sc.budget)}g budget` : "") +
    ` · ${fmtInt(d.buildsEvaluated)} builds ranked` +
    (when ? ` · computed ${when} in ${fmtDur(d.computeSeconds)}` : "") +
    ` · items patch ${d.itemsPatch}, kit ${d.kitPatch} · ${bstate.meta.note}`;
  renderBuildsTable();
}

// Why cells stay cold when no warm is running, by the status endpoint's word.
const WARMER_WHY = {
  idle: "no warmer is running: run `cli.py builds warm`, or restart serve without --no-warm",
  failed: "the last warm failed or was killed: see .cache/builds/warm.log, then restart serve",
  stale: "serve is running older code than is on disk: restart it (after web/ops/build-engine.sh if engine/ changed)",
};

// Say why the selected cell is missing, and poll until it lands.
async function showPending(req) {
  let st = null;
  try { st = await loadStatus(); } catch (e) { /* shown as unavailable */ }
  if (req !== bstate.req) return;
  if (st?.ready[`${bstate.champion}/${bstate.scenario}`]) { loadScenario(); return; }
  const n = st ? Object.values(st.ready).filter(Boolean).length : 0;
  const total = st ? Object.keys(st.ready).length : 0;
  const why = {
    running: `warming in the background, ${n} of ${total} scenarios ready — this one will appear when it lands`,
    ...WARMER_WHY,
  }[st?.warmer] || "status unavailable";
  document.getElementById("scenario-desc").textContent =
    `Not computed yet for the current code and data (${why}).`;
  bstate.timer = setTimeout(() => showPending(req), 10000);
}

const EXPECTED_TITLE = "Ranking metric. Same as kill time unless the build relies on an execute: expected-value crit always lands in the execute window, but real crit only would some of the time, so this blends the executing and non-executing timelines by that probability";
const MEAN_TITLE = "Ranking metric: the geometric mean of the expected kill times against every target, so each target counts equally in percentage terms. Builds that kill every target rank above builds that leave one standing; a ≈ time is extrapolated past the fight at its average DPS.";
const vsTitle = t =>
  `The same build's expected kill time against the ${t.target} (${fmtInt(t.targetHp)} HP, ` +
  `${t.armor} armor, ${t.mr} MR, ${t.duration}s), and how far behind that target's own best build it is`;

// One build's fight against one target: expected kill time (≈ extrapolated
// past the fight if the dummy survived) and its distance from the best.
function vsCell(r, t) {
  const v = r.vs?.[t.target];
  const td = document.createElement("td");
  if (!v) { td.textContent = "—"; return td; }
  if (v.ttkExp != null) td.append(v.ttkExp.toFixed(2) + "s");
  else if (v.killTime != null) {
    td.append("≈" + v.killTime.toFixed(1) + "s");
    td.title = `survives the ${t.duration}s fight (${fmtInt(v.total)} damage dealt) — ` +
      "time extrapolated at the fight's average DPS";
  } else td.append("—");
  if (v.loss != null) {
    const s = document.createElement("span");
    s.className = "ci";
    s.textContent = v.loss < 1.005 ? " best" : ` +${Math.round((v.loss - 1) * 100)}%`;
    td.append(s);
  }
  return td;
}

// The item catalog behind the table's icons, by the name a row carries. An
// entry holds the ddragon icon, the in-game tooltip as styled text runs and
// what the engine models of the item; a serve older than the catalog gives
// none, and the icons then fall back to initials with the name on hover.
function buildItemLookup(meta) {
  return new Map((meta?.items || []).map(it => [it.name, it]));
}

// Initials shown while an icon has no image: "RD" for Rabadon's Deathcap,
// "BO" for Boots.
function itemAbbrev(name) {
  const words = name.replace(/'s\b/g, "").split(/[\s-]+/)
    .filter(w => /^[A-Za-z0-9]/.test(w) && !/^(of|the|and|a)$/i.test(w));
  return (words.length > 1 ? words.slice(0, 2).map(w => w[0]) : [name.slice(0, 2)])
    .join("").toUpperCase();
}

// What the engine makes of the item, as sentences: item-effects.json's
// covered passives and note, and any passive it leaves as text.
function itemModelText(modeled) {
  if (!modeled) return ["No model notes for this item."];
  const lines = [];
  if (modeled.covers?.length) lines.push(`Accounts for: ${modeled.covers.join(", ")}.`);
  if (modeled.unmodeled?.length) lines.push(`Text-only passives outside the numbers: ${modeled.unmodeled.join(", ")}.`);
  if (modeled.note) lines.push(modeled.note.replace(/\.?$/, "."));
  return lines.length ? lines : ["Stats only."];
}

// ddragon's styled runs ([tag, text]) as nodes: a span per tag, text otherwise.
function ddRuns(runs) {
  return (runs || []).map(([style, text]) => {
    if (!style) return document.createTextNode(text);
    return tftText("span", text, "dd-" + style);
  });
}

const DD_KIND_LABEL = { passive: "Passive", active: "Active" };

function itemTooltip(it, name) {
  if (!it) return [tftTooltipHead(null, name, "No item details in this snapshot")];
  const out = [tftTooltipHead(it.icon, it.name,
    `${fmtInt(it.gold)}g` + (it.plaintext ? ` · ${it.plaintext}` : ""))];
  if (it.stats?.length) {
    const ul = document.createElement("ul");
    ul.className = "item-tt-stats";
    ul.append(...it.stats.map(line => tftText("li", line)));
    out.push(ul);
  }
  for (const fx of it.effects || []) {
    const p = document.createElement("p");
    p.className = "item-tt-effect";
    if (fx.name) {
      const b = document.createElement("b");
      b.append(fx.name);
      if (DD_KIND_LABEL[fx.kind]) b.append(" ", tftText("small", DD_KIND_LABEL[fx.kind], "item-tt-kind"));
      p.append(b, document.createElement("br"));
    }
    p.append(...ddRuns(fx.text));
    out.push(p);
  }
  const model = document.createElement("div");
  model.className = "item-tt-model";
  model.append(tftText("b", "In this model"), ...itemModelText(it.modeled).map(t => tftText("div", t)));
  out.push(model);
  return out;
}

// One item of a build: its icon, named for assistive tech, with the tooltip
// on hover, keyboard focus and tap. Clicks still reach the row (selecting
// the build); Enter or Space on a focused icon selects it too.
function itemIcon(name) {
  const it = bstate.items?.get(name);
  const el = document.createElement("span");
  el.className = "item-icon";
  el.tabIndex = 0;
  el.setAttribute("role", "img");
  el.setAttribute("aria-label", name);
  el.append(tftText("span", itemAbbrev(name), "item-icon-fallback"));
  if (it?.icon) {
    const img = document.createElement("img");
    img.alt = "";
    img.loading = "lazy";
    img.decoding = "async";
    img.addEventListener("load", () => el.classList.add("has-image"));
    img.addEventListener("error", () => { img.remove(); el.classList.remove("has-image"); });
    img.src = it.icon;
    el.append(img);
  }
  const contents = () => itemTooltip(it, name);
  bindTftTooltip(el, contents);
  el.addEventListener("pointerdown", e => { el.dataset.pointer = e.pointerType; });
  el.addEventListener("click", () => {  // no hover on touch: a tap opens the details
    if (el.dataset.pointer !== "mouse" && tftTip.anchor !== el) showTftTooltip(el, contents);
  });
  el.addEventListener("keydown", e => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); el.closest("tr")?.click(); }
  });
  return el;
}

// The top rows of a cell carry a suggested buy order (builds.py buy_order)
// of the items after the boots; null for the other rows, for cells computed
// before orders existed, and for an order that isn't the row's own items.
function buyOrderItems(row) {
  const rest = row.items.slice(1);
  const order = row.buyOrder;
  const same = Array.isArray(order) && order.length === rest.length &&
    [...order].sort().join("\n") === [...rest].sort().join("\n");
  return same ? order : null;
}

// Boots first (bought early), then the items numbered in buy order when
// the row has one, else in the enumeration's order.
function buildCell(row) {
  const td = document.createElement("td");
  const list = document.createElement("span");
  list.className = "build-items";
  const order = buyOrderItems(row);
  list.append(...(order ? [row.items[0], ...order] : row.items).map((name, i) => {
    const icon = itemIcon(name);
    if (order && i > 0) {
      const step = document.createElement("span");
      step.className = "item-step";
      step.textContent = i;
      icon.append(step);
      icon.setAttribute("aria-label", `${name}, buy ${i} of ${order.length}`);
    }
    return icon;
  }));
  td.append(list);
  return td;
}

// Columns depend on the cell: a per-target cell leads with its own fight and
// adds the build's kill time against the tier's other targets; the overall
// cell shows every target, then the mean it is ranked by.
function buildColumns(sc) {
  if (scenarioObjective(sc) === "survival") return survivalColumns(sc);
  const targets = sc.targets || [];
  const cols = [
    { th: "#", td: r => cell(String(r.rank)) },
    { th: "Build", title: "One icon per item — hover, focus or tap an icon for its name and details",
      td: r => buildCell(r) },
  ];
  if (sc.overall) {
    targets.forEach(t => cols.push({ th: `vs ${t.target}`, title: vsTitle(t), td: r => vsCell(r, t) }));
    cols.push({ th: "Mean", title: MEAN_TITLE,
                td: r => cell(r.mean != null ? r.mean.toFixed(2) + "s" : "—") });
  } else {
    cols.push(
      { th: "Kill time", td: r => cell(r.ttk != null ? r.ttk.toFixed(2) + "s" : "survives") },
      { th: "Expected", title: EXPECTED_TITLE,
        td: r => cell(r.ttkExp != null ? r.ttkExp.toFixed(2) + "s" : "—") });
    targets.filter(t => t.target !== sc.target).forEach(t =>
      cols.push({ th: `vs ${t.target}`, title: vsTitle(t), td: r => vsCell(r, t) }));
    cols.push({ th: "DPS", td: r => cell(fmtInt(r.dps)) },
              { th: "Damage", td: r => cell(fmtInt(r.total)) });
  }
  cols.push({ th: "Gold", td: r => cell(fmtInt(r.gold)) });
  return cols;
}

function renderBuildsTable() {
  const rows = bstate.data.rows, cols = buildColumns(bstate.data.scenario);
  document.querySelector("#btbl thead tr").replaceChildren(...cols.map(c => {
    const th = document.createElement("th");
    th.textContent = c.th;
    if (c.title) th.title = c.title;
    return th;
  }));
  const body = document.querySelector("#btbl tbody");
  body.replaceChildren(...rows.map((r, i) => {
    const tr = document.createElement("tr");
    if (i === bstate.sel) tr.className = "sel";
    tr.append(...cols.map(c => c.td(r)));
    tr.addEventListener("click", () => {
      bstate.sel = i;
      [...body.children].forEach((row, j) => row.classList.toggle("sel", j === i));
      renderBreakdown(r);
    });
    return tr;
  }));
  renderBreakdown(rows[bstate.sel]);
}

// The selected build's damage sources, one group per target of the tier:
// every cell carries the build's fight against each of them. A leaderboard
// row is a cell's top row and breaks down the same way: `sc` is then the
// board's scenario and `who` the row's champion.
function renderBreakdown(r, sc = bstate.data.scenario, who = "") {
  if (!r) return;
  if (scenarioObjective(sc) === "survival") { renderSurvivalBreakdown(r); return; }
  document.getElementById("bd-title").textContent =
    `Damage breakdown — #${r.rank}${who && " " + who}: ${r.items.join(", ")}`;
  document.getElementById("bd-hint").textContent =
    `${fmtInt(r.ap)} AP · ${fmtInt(r.ad)} AD · ${r.attackSpeed} attack speed · ${fmtInt(r.gold)}g · ` +
    "where the damage comes from over the whole fight (post-mitigation), against each target";
  const groups = (sc.targets || []).filter(t => r.vs?.[t.target]);
  document.getElementById("bd-bars").replaceChildren(...groups.flatMap(t => {
    const v = r.vs[t.target];
    const head = document.createElement("div");
    head.className = "bd-group";
    const b = document.createElement("b");
    b.textContent = `vs ${t.target}`;
    const fight = v.ttk != null ? `dies at ${v.ttk.toFixed(2)}s` : `survives the ${t.duration}s fight`;
    head.append(b, ` (${fmtInt(t.targetHp)} HP, ${t.armor} armor, ${t.mr} MR) — ` +
      `${fmtInt(v.total)} damage · ${fmtInt(v.dps)} DPS · ${v.attacks} attacks · ${fight}`);
    const entries = Object.entries(v.breakdown);
    const max = Math.max(...entries.map(([, x]) => x));
    return [head, ...entries.map(([src, dmg], i) => bar(src, dmg, max, v.total, i))];
  }));
}

const TTD_TITLE = "Ranking metric: how long the build lasts against this attacker — the kill time, or ≈ the time the damage it took would have needed when it is still standing at the end of the fight — and how far short of the longest-lived build it falls";
const SURV_MEAN_TITLE = "Ranking metric: the geometric mean of the times to die against every attacker, so each counts equally in percentage terms. Builds fewer attackers kill rank first.";

// One build's fight against one attacker: its time to die and its distance
// from that attacker's longest-lived build.
function survCell(v, a) {
  const td = document.createElement("td");
  if (!v) { td.textContent = "—"; return td; }
  td.append(ttdText(v));
  if (!v.died) td.title = `still standing after the ${a?.duration ?? ""}s fight with ` +
    `${fmtInt(v.hpLeft)} health — the time the damage it took would have needed`;
  const s = document.createElement("span");
  s.className = "ci";
  s.textContent = shareText(v.share);
  td.append(s);
  return td;
}

// A Survival cell's columns: the cell's own attacker (or every attacker and
// the mean, overall), then the build's defenses.
function survivalColumns(sc) {
  const attackers = sc.attackers || [];
  const cols = [
    { th: "#", td: r => cell(String(r.rank)) },
    { th: "Build", title: "One icon per item — hover, focus or tap an icon for its name and details",
      td: r => buildCell(r) },
  ];
  const vs = a => ({ th: `vs ${a.name}`, title: `${TTD_TITLE} — ${a.name}: ${a.items.join(", ")}`,
                     td: r => survCell(r.vs?.[a.champion], a) });
  if (sc.overall) {
    attackers.forEach(a => cols.push(vs(a)));
    cols.push({ th: "Mean", title: SURV_MEAN_TITLE,
                td: r => cell(r.mean != null ? r.mean.toFixed(2) + "s" : "—") });
  } else {
    const me = attackers.find(a => a.key === sc.key);
    cols.push({ th: "Time to die", title: TTD_TITLE, td: r => survCell(r.vs?.[sc.attacker], me) });
    attackers.filter(a => a.key !== sc.key).forEach(a => cols.push(vs(a)));
  }
  cols.push({ th: "Health", td: r => cell(fmtInt(r.hp)) },
            { th: "Armor", td: r => cell(fmtInt(r.armor)) },
            { th: "MR", td: r => cell(fmtInt(r.mr)) },
            { th: "Gold", td: r => cell(fmtInt(r.gold)) });
  return cols;
}

// The selected tank build against each attacker: what hit it, and what kept
// it up (heals, shields, damage kept off), with the moments that decided it.
function renderSurvivalBreakdown(r) {
  const sc = bstate.data.scenario;
  document.getElementById("bd-title").textContent =
    `Survival breakdown — #${r.rank}: ${r.items.join(", ")}`;
  document.getElementById("bd-hint").textContent =
    `${fmtInt(r.hp)} health · ${fmtInt(r.armor)} armor · ${fmtInt(r.mr)} MR · ${fmtInt(r.gold)}g · ` +
    "what hit it (post-mitigation) and what kept it up, against each attacker";
  const groups = (sc.attackers || []).filter(a => r.vs?.[a.champion]);
  document.getElementById("bd-bars").replaceChildren(...groups.flatMap(a => {
    const v = r.vs[a.champion];
    const head = document.createElement("div");
    head.className = "bd-group";
    const b = document.createElement("b");
    b.textContent = `vs ${a.name}`;
    const fate = v.died ? `dies at ${v.ttd.toFixed(2)}s`
                        : `still standing at ${a.duration}s with ${fmtInt(v.hpLeft)} health`;
    head.append(b, ` (${a.items.join(", ")}) — ${fate} · ${fmtInt(v.taken)} damage taken`);
    const events = defenseEvents(v);
    const when = document.createElement("div");
    when.className = "hint";
    when.textContent = events.join(" · ");
    const taken = Object.entries(v.breakdown);
    const kept = defenseRows(v.defense);
    const max = Math.max(1, ...taken.map(([, x]) => x), ...kept.map(([, x]) => x));
    const sub = text => {
      const el = document.createElement("div");
      el.className = "hint";
      el.textContent = text;
      return el;
    };
    const keptTotal = kept.reduce((s, [, x]) => s + x, 0);
    return [head, ...(events.length ? [when] : []),
            sub("Damage taken, by source"),
            ...taken.map(([src, dmg], i) => bar(src, dmg, max, v.taken, i)),
            ...(kept.length ? [sub("Kept off and healed"),
                               ...kept.map(([label, x], i) => bar(label, x, max, keptTotal, i + 3))]
                            : [])];
  }));
}

async function openBuilds() {
  if (await loadBuildsMeta()) {
    renderBuildsMode();
    loadStatus().catch(() => {});
    loadBuildsView();
  }
}

// ---- the leaderboard: every champion's best build under one scenario ----
// A row per champion: the top row of its cell, ranked against the others'
// (builds_leaderboard.py). Saved cells only, like everything on this tab.
const bboard = { data: null, error: null, search: "", reviewedOnly: false, scroll: 0,
                 sortKey: "rank", sortDir: 1 };  // the column the rows are ordered by: see boardSort

// The scenarios with a board: the damage tiers' (a single tank compares nothing).
function leaderboardScenarios(meta) {
  return (meta?.scenarios || []).filter(s => scenarioObjective(s) === "damage").map(s => s.key);
}

function loadBuildsView() {
  return bstate.mode === "leaderboard" ? loadLeaderboard() : loadScenario();
}

// Which of the two views the first card shows.
function renderBuildsMode() {
  const board = bstate.mode === "leaderboard";
  document.querySelectorAll("#builds-mode-seg button").forEach(b => {
    const on = b.dataset.key === bstate.mode;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", String(on));
  });
  const survival = !board &&
    bstate.meta.champions.find(c => c.slug === bstate.champion)?.objective === "survival";
  const hide = (id, hidden) => { document.getElementById(id).hidden = hidden; };
  hide("builds-note-board", !board);
  hide("builds-note", board || survival);
  hide("builds-note-survival", !survival);
  hide("champion-seg", board);
  hide("btbl-wrap", board);
  hide("bboard", !board);
  if (board) {
    hide("champion-more", true);
    hide("builds-generated", true);  // the selected row's is with its breakdown
    document.getElementById("bboard-search").value = bboard.search;
    document.getElementById("bboard-reviewed").checked = bboard.reviewedOnly;
  } else {
    hide("bd-generated", true);
    renderChampionPicker();  // a champion opened from the board joins the buttons
    renderPool();
  }
}

function setBuildsMode(mode) {
  if (mode === bstate.mode || !["champion", "leaderboard"].includes(mode)) return;
  const wrap = document.getElementById("bboard-wrap");
  if (bstate.mode === "leaderboard") bboard.scroll = wrap.scrollTop;  // a hidden box forgets it
  bstate.mode = mode;
  if (!bstate.meta) { updateHash(); return; }  // openBuilds shows the mode once the meta is in
  syncScenarioSeg();
  if (mode === "champion") {
    // while the cell loads: not another champion's rows under the board's title
    if (bstate.data?.champion !== bstate.champion || bstate.data?.scenario?.key !== bstate.scenario)
      clearBuilds();
    const champ = bstate.meta.champions.find(c => c.slug === bstate.champion);
    document.getElementById("builds-title").textContent = `${champ?.name || bstate.champion} — ` +
      (champ?.objective === "survival" ? "tankiest builds" : "ranked builds");
  }
  renderBuildsMode();
  loadBuildsView();  // the board renders what it holds before it asks again
  if (mode === "leaderboard") wrap.scrollTop = bboard.scroll;
}

// A row's champion in the Champion builds view, under the board's scenario.
function openBoardChampion(slug) {
  bstate.champion = slug;
  setBuildsMode("champion");
}

const BOARD_TIME_TITLE = "Ranking metric: the expected kill time of the champion's best build against this target (the kill time, unless the build relies on an execute). A ≈ time is extrapolated past the fight at its average DPS: the target survived.";
const BOARD_MEAN_TITLE = "Ranking metric: the geometric mean of the best build's expected kill times against every target, so each target counts equally in percentage terms. Champions that kill every target rank above those that leave one standing.";
const INSTANT_TITLE = "Dead at 0.00s: this kit lands its whole opening combo in the fight's first instant, with no cast or travel time between the hits. The time — and an overall mean built on it — says more about the draft kit than about the champion.";
const UNREVIEWED_TITLE = "This kit and its rotation were written by a model and not reviewed by a person: treat the rank as a draft. Select the row for what the kit assumes.";

// Whether any of the row's fights ends the instant it starts.
function boardInstant(r) {
  return Object.values(r.vs || {}).some(v => v.ttkExp === 0);
}

function boardInstantTag(td) {
  td.append(tftText("span", " instant", "ci"));
  td.title = INSTANT_TITLE;
  return td;
}

// A best build's fight against one target: its expected kill time, ≈ when
// extrapolated past a fight the target survived.
function boardTimeCell(v, t) {
  const td = document.createElement("td");
  if (!v) { td.textContent = "—"; return td; }
  if (v.ttkExp != null) {
    td.append(v.ttkExp.toFixed(2) + "s");
    if (v.ttkExp === 0) boardInstantTag(td);
  } else if (v.killTime != null) {
    td.append("≈" + v.killTime.toFixed(1) + "s");
    td.title = `survives the ${t.duration}s fight (${fmtInt(v.total)} damage dealt) — ` +
      "time extrapolated at the fight's average DPS";
  } else td.append("—");
  return td;
}

// What that cell sorts on, fastest first: the time it shows, a build that
// kills the target before one it survives (as the target's own board ranks
// them). null, a fight with no time to show, sorts last in either direction.
function boardTimeSort(v) {
  const time = v?.ttkExp ?? v?.killTime;
  return time == null ? null : [v.ttkExp == null ? 1 : 0, time];
}

// The champion: its name opens its own ranked builds; a kit nobody has
// reviewed says so.
function boardChampionCell(r) {
  const td = document.createElement("td");
  const icon = document.createElement("img");
  icon.className = "champ-icon";
  icon.alt = "";
  icon.loading = "lazy";
  icon.onerror = () => { icon.style.visibility = "hidden"; };
  icon.src = iconUrl(r.champion);
  const open = document.createElement("button");
  open.className = "bboard-champion";
  open.dataset.champion = r.champion;
  open.textContent = r.championName || r.champion;
  open.title = "Open this champion's ranked builds";
  open.addEventListener("click", e => { e.stopPropagation(); openBoardChampion(r.champion); });
  td.append(icon, open);
  if (!r.reviewed) {
    const tag = tftText("span", "unreviewed", "bboard-tag");
    tag.title = UNREVIEWED_TITLE;
    td.append(tag);
  }
  return td;
}

// A target's board leads with that fight, then the same build against the
// tier's other targets; the overall board shows every target and the mean.
// A column with `sort` (the value a row is ordered by, null = nothing to order
// on) can order the table: ascending at first, `desc` the other way; `first`
// names what either direction puts on top. A target's column has one `key` on
// every board, so its order outlasts a change of scenario.
function boardColumns(sc) {
  const targets = sc.targets || [];
  const time = (t, col) => ({ key: `vs-${t.target}`, sort: r => boardTimeSort(r.vs?.[t.target]),
    first: ["fastest", "slowest"], td: r => boardTimeCell(r.vs?.[t.target], t), ...col });
  const vs = t => time(t, { th: `vs ${t.target}`,
    title: `The same build's expected kill time against the ${t.target} (${fmtInt(t.targetHp)} HP, ` +
           `${t.armor} armor, ${t.mr} MR, ${t.duration}s)` });
  const cols = [
    { key: "rank", th: "#", title: "Among every champion of this scenario; equal results share a rank. It stays this scenario's rank whichever column orders the table.",
      sort: r => r.rank, first: ["best", "worst"], td: r => cell(String(r.rank)) },
    // as a champion select lists them: K'Sante after Kog'Maw
    { key: "champion", th: "Champion", sort: r => (r.championName || r.champion).toLowerCase().replace(/[^a-z0-9]/g, ""),
      first: ["A", "Z"], td: r => boardChampionCell(r) },
    { th: "Best build", title: "The top row of the champion's own ranking, numbered in its suggested buy order — hover, focus or tap an icon for the item",
      td: r => buildCell(r) },
  ];
  if (sc.overall) {
    targets.forEach(t => cols.push(vs(t)));
    cols.push({ key: "mean", th: "Mean", title: BOARD_MEAN_TITLE, sort: r => r.mean ?? null, first: ["fastest", "slowest"], td: r => {
      const td = cell(r.mean != null ? r.mean.toFixed(2) + "s" : "—");
      return boardInstant(r) ? boardInstantTag(td) : td;
    } });
  } else {
    cols.push(time(sc, { th: "Kill time", title: BOARD_TIME_TITLE }));
    targets.filter(t => t.target !== sc.target).forEach(t => cols.push(vs(t)));
    // a fight over at 0.00s has no length to average the damage over
    cols.push({ key: "dps", th: "DPS", sort: r => r.ttkExp === 0 ? null : r.dps ?? null, desc: true, first: ["lowest", "highest"],
                td: r => cell(r.ttkExp === 0 ? "—" : fmtInt(r.dps)) },
              { key: "damage", th: "Damage", sort: r => r.total ?? null, desc: true, first: ["least", "most"],
                td: r => cell(fmtInt(r.total)) });
  }
  cols.push({ key: "gold", th: "Gold", sort: r => r.gold ?? null, first: ["cheapest", "most expensive"],
              td: r => cell(fmtInt(r.gold)) });
  return cols;
}

// The rows the filters leave, in rank order (ranks stay the full board's).
function boardRows(d) {
  const q = bboard.search.trim().toLowerCase();
  return (d?.rows || []).filter(r => (!bboard.reviewedOnly || r.reviewed) &&
    (!q || (r.championName || "").toLowerCase().includes(q) || r.champion.includes(q.replace(/[^a-z0-9]/g, ""))));
}

// The column the table is ordered by and the direction (1 ascending): the one
// chosen where this scenario's board has it — Mean is the overall board's, DPS
// and Damage a target's — else the rank.
function boardSort(cols) {
  const col = cols.find(c => c.sort && c.key === bboard.sortKey);
  return col ? { col, dir: bboard.sortDir } : { col: cols[0], dir: 1 };
}

// Rows in that order. The sort is stable over the rank order they come in, so
// equal values stay in rank order in either direction.
function boardSorted(rows, { col, dir }) {
  const compare = (a, b) => Array.isArray(a) ? a.reduce((c, x, i) => c || compare(x, b[i]), 0)
    : a < b ? -1 : a > b ? 1 : 0;
  return rows.map(r => [col.sort(r), r])
    .sort(([a], [b]) => a == null || b == null ? (a == null) - (b == null) : compare(a, b) * dir)
    .map(([, r]) => r);
}

// A column's heading: one the table can be ordered by is a button (the
// keyboard reaches it) that shows the direction while it is.
function boardHeadCell(c, sorted) {
  const th = document.createElement("th");
  if (c.title) th.title = c.title;
  if (!c.sort) { th.textContent = c.th; return th; }
  const on = c === sorted.col;
  if (on) th.setAttribute("aria-sort", sorted.dir === 1 ? "ascending" : "descending");
  const button = document.createElement("button");
  button.className = "bboard-sort";
  button.dataset.k = c.key;
  button.append(c.th + " ", tftText("span", on ? (sorted.dir === 1 ? "▲" : "▼") : "", "arrow"));
  // best first; the other way round when it already orders the table
  button.addEventListener("click", () => sortBoard(c.key, on ? -sorted.dir : c.desc ? -1 : 1));
  th.append(button);
  return th;
}

function sortBoard(key, dir) {
  bboard.sortKey = key;
  bboard.sortDir = dir;
  renderLeaderboard();
  document.getElementById("bboard-wrap").scrollTop = 0;  // a new order is read from its top
  updateHash();
}

// What the cards below the table show: the selected row's damage sources,
// what its kit assumes, and its item pool with the rotation's notes.
function renderBoardSelection(r) {
  if (!r) {
    clearBreakdown();
    document.getElementById("bd-generated").hidden = true;
    return;
  }
  renderBreakdown(r, bboard.data.scenario, r.championName || r.champion);
  renderGeneratedBanner("bd-generated", r.champion);
  renderPool(r.champion);
}

function renderLeaderboard() {
  const d = bboard.data, sc = d?.scenario;
  const label = bstate.meta?.scenarios?.find(s => s.key === bstate.scenario)?.label || bstate.scenario || "";
  document.getElementById("builds-title").textContent = `Champion leaderboard, ${label.toLowerCase()}`;
  const desc = document.getElementById("scenario-desc");
  if (!d) desc.textContent = bboard.error ? "" : "Loading…";
  else {
    const dummy = t => `${fmtInt(t.targetHp)} HP, ${t.armor} armor, ${t.mr} MR, ${t.duration}s fight`;
    const drafts = d.rows.filter(r => !r.reviewed).length;
    desc.textContent = `Level ${sc.level} · ` + (sc.overall
      ? `ranked on every target at once — ${sc.targets.map(t => `${t.target} (${dummy(t)})`).join("; ")} — ` +
        "by the geometric mean of expected kill times, champions that kill all of them first"
      : `target ${dummy(sc)} · ranked by expected kill time`) +
      ` · each champion with the best build of its own ranking` +
      (drafts ? ` · ${fmtInt(drafts)} of ${fmtInt(d.rows.length)} kits machine-written and unreviewed` : "") +
      ` · items patch ${bstate.meta.itemsPatch} · ${bstate.meta.note}`;
  }
  const cols = sc ? boardColumns(sc) : [], sorted = boardSort(cols);
  const rows = sorted.col ? boardSorted(boardRows(d), sorted) : [];
  const filtered = !!bboard.search.trim() || bboard.reviewedOnly;
  let status = "";
  if (bboard.error) status = bboard.error + (d ? " Showing the board as last loaded." : "");
  else if (d) {
    status = !rows.length && d.rows.length ? "No champion matches these filters."
      : `${fmtInt(rows.length)}${filtered ? ` of ${fmtInt(d.rows.length)}` : ""} champions` +
        (filtered ? " · ranks are from the full board" : "") +
        (sorted.col.key === "rank" && sorted.dir === 1 ? ""  // the board as it is ranked
          : ` · sorted by ${sorted.col.th}, ${sorted.col.first[sorted.dir === 1 ? 0 : 1]} first`);
    if (!d.complete) {
      const why = { running: "the rest are warming in the background", ...WARMER_WHY }[bstate.status?.warmer];
      status += ` · ${fmtInt(d.readyCount)} of ${fmtInt(d.expectedCount)} computed for the current code and data` +
        `${why ? ` (${why})` : ""}: ranks are provisional`;
    }
  }
  document.getElementById("bboard-status").textContent = status;
  const failed = document.getElementById("bboard-failed");
  failed.hidden = !d?.failed?.length;
  failed.textContent = failed.hidden ? "" :
    "Not ranked: " +
    d.failed.map(f => `${f.championName || f.champion} (${f.error})`).join("; ") + ".";
  // the headings are rebuilt with every render (a sort, a board filling in):
  // the keyboard stays on the sort button it was on
  const active = document.activeElement;
  const held = active?.className === "bboard-sort" ? active.dataset.k : null;
  document.querySelector("#bboard-tbl thead tr").replaceChildren(...cols.map(c => boardHeadCell(c, sorted)));
  if (held) document.querySelector(`#bboard-tbl thead button[data-k="${held}"]`)?.focus();
  // the champion the rest of the tab is on, else the top of what is shown
  const selected = rows.find(r => r.champion === bstate.champion) || rows[0];
  const wrap = document.getElementById("bboard-wrap"), body = document.querySelector("#bboard-tbl tbody");
  const top = wrap.scrollTop;  // refilling the body would reset it: a board filling in must not jump
  body.replaceChildren(...rows.map(r => {
    const tr = document.createElement("tr");
    tr.dataset.champion = r.champion;
    if (r === selected) tr.className = "sel";
    tr.append(...cols.map(c => c.td(r)));
    tr.addEventListener("click", () => {
      bstate.champion = r.champion;
      [...body.children].forEach(row => row.classList.toggle("sel", row === tr));
      renderBoardSelection(r);
      updateHash();
    });
    return tr;
  }));
  wrap.scrollTop = top;
  renderBoardSelection(selected);
}

async function loadLeaderboard() {
  const key = bstate.scenario, req = ++bstate.req;  // a newer click supersedes this load
  clearTimeout(bstate.timer);
  document.querySelectorAll("#scenario-seg button").forEach(b =>
    b.classList.toggle("active", b.dataset.key === key));
  markCold();
  updateHash();
  if (bboard.data?.scenario?.key !== key) bboard.data = null;  // another scenario's board
  bboard.error = null;
  renderLeaderboard();
  let d = null;
  try {
    d = await api(`api/builds/leaderboard/${key}.json`);
    if (!Array.isArray(d?.rows) || d.scenario?.key !== key) throw new Error("unexpected response");
  } catch (e) {
    if (req !== bstate.req) return;
    bboard.error = `The leaderboard could not be loaded (${e.message}). Retrying shortly.`;
    d = null;
  }
  if (req !== bstate.req) return;
  if (d) bboard.data = d;
  if (d && !d.complete) await loadStatus().catch(() => {});  // why it is incomplete
  if (req !== bstate.req) return;
  renderLeaderboard();
  // cells still to come (or a failed request): the board fills in as they land
  if (!d || !d.complete) bstate.timer = setTimeout(() => { if (req === bstate.req) loadLeaderboard(); }, 10000);
}

document.querySelectorAll("#builds-mode-seg button").forEach(b =>
  b.addEventListener("click", () => setBuildsMode(b.dataset.key)));
document.getElementById("bboard-search").addEventListener("input", e => {
  bboard.search = e.target.value; renderLeaderboard(); updateHash();
});
document.getElementById("bboard-reviewed").addEventListener("change", e => {
  bboard.reviewedOnly = e.target.checked; renderLeaderboard(); updateHash();
});

// ---------------- matchups & draft identity ----------------
const MU_LANES = ["top", "jungle", "middle", "bottom", "support"];
const MU_LANE_SHORT = { top: "Top", jungle: "Jgl", middle: "Mid", bottom: "Bot", support: "Sup" };
const MU_LANE_FULL = { top: "Top", jungle: "Jungle", middle: "Middle", bottom: "Bottom", support: "Support" };

const mstate = {
  mode: "identity",
  data: null,
  loading: false,
  error: null,
  req: 0,
  champion: null,
  lane: null,
  search: "",
  roleFilter: "all",
  archFilter: "all",
  ctrScope: "lane",
  ctrRole: "all",
  ctrLayout: "archetype",
  synMode: "kit",
  synRole: "all",
  synLayout: "archetype",
  matMode: "counter",
  matCell: null,
  archRole: "all",
  archSearch: "",
  highlightArch: null,
};

function fmtPp(v) {
  const s = v >= 0 ? "+" : "";
  return s + v.toFixed(2) + "pp";
}

function unpackPairMatrix(vec, nc, antisym) {
  const mat = new Float32Array(nc * nc);
  if (!Array.isArray(vec)) return mat;
  let k = 0;
  for (let i = 0; i < nc; i++) {
    for (let j = i + 1; j < nc; j++) {
      const v = (vec[k++] || 0) / 100;
      mat[i * nc + j] = v;
      mat[j * nc + i] = antisym ? -v : v;
    }
  }
  return mat;
}

function prepareMatchupsData(raw) {
  if (!raw || !Array.isArray(raw.champions) || !raw.champions.length) return raw;
  const nc = raw.champions.length;
  const roleTotals = [0, 0, 0, 0, 0];
  const champions = raw.champions.map((c, idx) => {
    const roles = c.roles || [0, 0, 0, 0, 0];
    let totalRoles = 0;
    let primaryLaneIdx = 0;
    for (let r = 0; r < 5; r++) {
      totalRoles += roles[r];
      roleTotals[r] += roles[r];
      if (roles[r] > roles[primaryLaneIdx]) primaryLaneIdx = r;
    }
    const viableLanes = [];
    for (let r = 0; r < 5; r++) {
      if (r === primaryLaneIdx || (roles[r] >= 25 && roles[r] >= totalRoles * 0.05)) {
        viableLanes.push(r);
      }
    }
    return {
      ...c,
      idx,
      totalRoles,
      primaryLaneIdx,
      primaryLane: MU_LANES[primaryLaneIdx],
      viableLanes,
    };
  });
  const totalGames = roleTotals.reduce((a, b) => a + b, 0) || 1;
  const sortedRoster = champions.slice().sort((a, b) => a.name.localeCompare(b.name));
  const champBySlug = new Map(champions.map(c => [c.slug, c]));
  const pairs = raw.pairs || {};
  const mats = {
    ctr: unpackPairMatrix(pairs.ctr, nc, true),
    lane: unpackPairMatrix(pairs.lane, nc, true),
    syn: unpackPairMatrix(pairs.syn, nc, false),
    duo: unpackPairMatrix(pairs.duo, nc, false),
    jgl: unpackPairMatrix(pairs.jgl, nc, false),
  };
  return {
    ...raw,
    champions,
    sortedRoster,
    champBySlug,
    roleTotals,
    totalGames,
    mats,
    defaultChamp: sortedRoster[0]?.slug || champions[0]?.slug || null,
  };
}

function populateMatchupsArchSelect(d) {
  const sel = document.getElementById("mu-arch-filter");
  const allOpt = document.createElement("option");
  allOpt.value = "all";
  allOpt.textContent = `All archetypes (${d.archetypes.length})`;
  const opts = d.archetypes.map(a => {
    const o = document.createElement("option");
    o.value = String(a.id);
    o.textContent = `${a.name} (${a.members.length})`;
    return o;
  });
  sel.replaceChildren(allOpt, ...opts);
  sel.value = mstate.archFilter;
}

function defaultMatrixCell(d, mode) {
  const archs = d.archetypes;
  let best = [0, archs.length > 1 ? 1 : 0];
  let bestAbs = -1;
  for (let r = 0; r < archs.length; r++) {
    const arr = mode === "counter" ? archs[r].ctr : mode === "kit" ? archs[r].kitSyn : archs[r].syn;
    for (let c = 0; c < archs.length; c++) {
      if (mode === "counter" && r === c) continue;
      if (Math.abs(arr[c]) > bestAbs) {
        bestAbs = Math.abs(arr[c]);
        best = [r, c];
      }
    }
  }
  return best;
}

function ensureMatchupSelection() {
  const d = mstate.data;
  if (!d?.champions?.length) return;
  if (!mstate.champion || !d.champBySlug.has(mstate.champion)) {
    mstate.champion = d.defaultChamp;
  }
  const ch = d.champBySlug.get(mstate.champion);
  if (!mstate.lane || !MU_LANES.includes(mstate.lane)) {
    mstate.lane = ch.primaryLane;
  }
  const nArch = d.archetypes.length;
  if (!mstate.matCell || mstate.matCell[0] < 0 || mstate.matCell[0] >= nArch ||
      mstate.matCell[1] < 0 || mstate.matCell[1] >= nArch ||
      (mstate.matMode === "counter" && mstate.matCell[0] === mstate.matCell[1] && nArch > 1)) {
    mstate.matCell = defaultMatrixCell(d, mstate.matMode);
  }
}

function restoreMatchupsHash(hash) {
  mstate.mode = hash.mview === "archetypes" ? "archetypes" : "identity";
  if (mstate.mode === "archetypes") {
    mstate.matMode = ["counter", "kit", "synergy"].includes(hash.mmat) ? hash.mmat : "counter";
    if (typeof hash.mcell === "string" && /^\d+-\d+$/.test(hash.mcell)) {
      const [r, c] = hash.mcell.split("-").map(Number);
      mstate.matCell = [r, c];
    } else {
      mstate.matCell = null;
    }
    mstate.archRole = ["all", ...MU_LANES].includes(hash.marole) ? hash.marole : "all";
  } else {
    mstate.champion = hash.mchamp ? hash.mchamp.toLowerCase() : null;
    mstate.lane = MU_LANES.includes(hash.mlane) ? hash.mlane : null;
    mstate.ctrScope = hash.mscope === "team" ? "team" : "lane";
    mstate.synMode = hash.msyn === "total" ? "total" : "kit";
  }
}

function muImg(slug, cls) {
  const img = document.createElement("img");
  if (cls) img.className = cls;
  img.alt = "";
  img.loading = "lazy";
  img.onerror = () => { img.style.visibility = "hidden"; };
  img.src = iconUrl(slug);
  return img;
}

function muDivergingRow(label, valText, normVal, posColor, negColor, valCls = "", title = "") {
  const row = document.createElement("div");
  row.className = "mu-bar-row";
  if (title) row.title = title;
  const lbl = document.createElement("span");
  lbl.className = "mu-bar-lbl";
  lbl.textContent = label;
  const track = document.createElement("div");
  track.className = "mu-div-track";
  const fill = document.createElement("div");
  fill.className = "mu-div-fill";
  const clamped = Math.max(-1, Math.min(1, normVal));
  if (clamped >= 0) {
    fill.style.left = "50%";
    fill.style.width = (clamped * 50).toFixed(1) + "%";
    fill.style.background = posColor;
  } else {
    const w = -clamped * 50;
    fill.style.left = (50 - w).toFixed(1) + "%";
    fill.style.width = w.toFixed(1) + "%";
    fill.style.background = negColor;
  }
  track.append(fill);
  const val = document.createElement("span");
  val.className = "mu-bar-val" + (valCls ? " " + valCls : "");
  val.textContent = valText;
  row.append(lbl, track, val);
  return row;
}

function weightedCorr(xs, ys, ws) {
  let sw = 0, mx = 0, my = 0;
  for (let i = 0; i < xs.length; i++) {
    sw += ws[i];
    mx += ws[i] * xs[i];
    my += ws[i] * ys[i];
  }
  if (sw <= 0) return 0;
  mx /= sw;
  my /= sw;
  let cov = 0, vx = 0, vy = 0;
  for (let i = 0; i < xs.length; i++) {
    const dx = xs[i] - mx, dy = ys[i] - my;
    cov += ws[i] * dx * dy;
    vx += ws[i] * dx * dx;
    vy += ws[i] * dy * dy;
  }
  const den = Math.sqrt(vx * vy);
  return den > 1e-9 ? cov / den : 0;
}

function selectMatchupChampion(slug, laneOverride = null, push = true) {
  const d = mstate.data;
  const ch = d?.champBySlug.get(slug);
  if (!ch) return;
  mstate.champion = ch.slug;
  if (laneOverride && MU_LANES.includes(laneOverride)) {
    mstate.lane = laneOverride;
  } else if (mstate.roleFilter !== "all" && ch.viableLanes.includes(MU_LANES.indexOf(mstate.roleFilter))) {
    mstate.lane = mstate.roleFilter;
  } else {
    mstate.lane = ch.primaryLane;
  }
  if (mstate.mode !== "identity") mstate.mode = "identity";
  renderMatchups();
  updateHash(push);
}

function openArchetypeFocus(archId, push = true) {
  const d = mstate.data;
  if (!d) return;
  mstate.mode = "archetypes";
  mstate.highlightArch = archId;
  const a = d.archetypes[archId];
  if (a) {
    let bestCol = archId === 0 ? 1 : 0;
    let bestAbs = -1;
    const arr = mstate.matMode === "counter" ? a.ctr : mstate.matMode === "kit" ? a.kitSyn : a.syn;
    for (let c = 0; c < d.archetypes.length; c++) {
      if (mstate.matMode === "counter" && c === archId) continue;
      if (Math.abs(arr[c]) > bestAbs) {
        bestAbs = Math.abs(arr[c]);
        bestCol = c;
      }
    }
    mstate.matCell = [archId, bestCol];
  }
  renderMatchups();
  updateHash(push);
  const card = document.querySelector(`.mu-arch-card[data-arch="${archId}"]`);
  card?.scrollIntoView({ block: "nearest" });
}

function renderMatchupsBrowser() {
  const d = mstate.data;
  if (!d?.champions?.length) return;
  document.getElementById("mu-search").value = mstate.search;
  document.getElementById("mu-arch-filter").value = mstate.archFilter;
  document.querySelectorAll("#mu-role-filter button").forEach(b =>
    b.classList.toggle("active", b.dataset.role === mstate.roleFilter));

  const q = mstate.search.trim().toLowerCase();
  const rIdx = MU_LANES.indexOf(mstate.roleFilter);
  const filtered = d.sortedRoster.filter(c => {
    if (rIdx >= 0) {
      const share = c.roles[rIdx] / Math.max(1, d.roleTotals[rIdx]);
      if (!c.viableLanes.includes(rIdx) && share < 0.004) return false;
    }
    if (mstate.archFilter !== "all" && String(c.arch) !== mstate.archFilter) return false;
    if (q) {
      const arch = d.archetypes[c.arch];
      const hay = `${c.name} ${c.slug} ${arch?.name || ""} ${arch?.short || ""}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  const total = d.champions.length;
  document.getElementById("mu-champ-count").textContent =
    filtered.length === total ? `${fmtInt(total)} champions` : `${fmtInt(filtered.length)} of ${fmtInt(total)} champions`;

  const grid = document.getElementById("mu-champ-grid");
  grid.replaceChildren(...filtered.map(c => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "mu-champ-btn" + (c.slug === mstate.champion ? " active" : "");
    btn.dataset.champion = c.slug;
    const shownLane = rIdx >= 0 && c.viableLanes.includes(rIdx) ? mstate.roleFilter : c.primaryLane;
    btn.append(
      muImg(c.slug),
      tftText("span", c.name, "mu-champ-btn-name"),
      tftText("span", MU_LANE_SHORT[shownLane], "mu-champ-btn-lane"),
    );
    btn.addEventListener("click", () => selectMatchupChampion(c.slug));
    return btn;
  }));
}

function counterOpponents(d, ch, rIdx) {
  const nc = d.champions.length;
  const { ctr, lane } = d.mats;
  const opps = [];
  if (mstate.ctrScope === "lane") {
    const laneTot = Math.max(1, d.roleTotals[rIdx]);
    for (let j = 0; j < nc; j++) {
      if (j === ch.idx) continue;
      const cj = d.champions[j];
      const w = cj.roles[rIdx];
      if (w / laneTot < 0.004) continue;
      opps.push({ champ: cj, val: ctr[ch.idx * nc + j] + lane[ch.idx * nc + j], w });
    }
  } else {
    const rf = MU_LANES.indexOf(mstate.ctrRole);
    const den = rf >= 0 ? Math.max(1, d.roleTotals[rf]) : d.totalGames;
    const minShare = rf >= 0 ? 0.004 : 0.0015;
    for (let j = 0; j < nc; j++) {
      if (j === ch.idx) continue;
      const cj = d.champions[j];
      const w = rf >= 0 ? cj.roles[rf] : cj.totalRoles;
      if (w / den < minShare) continue;
      opps.push({ champ: cj, val: ctr[ch.idx * nc + j], w });
    }
  }
  return opps;
}

function synergyAllies(d, ch, rIdx) {
  const nc = d.champions.length;
  const { syn, duo, jgl } = d.mats;
  const roleMode = mstate.synRole;
  const specificLane = MU_LANES.indexOf(roleMode);
  const allies = [];

  for (let j = 0; j < nc; j++) {
    if (j === ch.idx) continue;
    const cj = d.champions[j];
    let w = 0;
    let bonus = 0;

    if (specificLane >= 0) {
      w = cj.roles[specificLane];
      if (w / Math.max(1, d.roleTotals[specificLane]) < 0.004) continue;
      if ((rIdx === 3 && specificLane === 4) || (rIdx === 4 && specificLane === 3)) {
        bonus = duo[ch.idx * nc + j];
      } else if ((rIdx === 1 && (specificLane === 0 || specificLane === 2)) ||
                 ((rIdx === 0 || rIdx === 2) && specificLane === 1)) {
        bonus = jgl[ch.idx * nc + j];
      }
    } else if (roleMode === "pair") {
      if (rIdx === 3) {
        w = cj.roles[4];
        if (w / Math.max(1, d.roleTotals[4]) < 0.004) continue;
        bonus = duo[ch.idx * nc + j];
      } else if (rIdx === 4) {
        w = cj.roles[3];
        if (w / Math.max(1, d.roleTotals[3]) < 0.004) continue;
        bonus = duo[ch.idx * nc + j];
      } else if (rIdx === 0 || rIdx === 2) {
        w = cj.roles[1];
        if (w / Math.max(1, d.roleTotals[1]) < 0.004) continue;
        bonus = jgl[ch.idx * nc + j];
      } else {
        const soloGames = cj.roles[0] + cj.roles[2];
        const soloTot = Math.max(1, d.roleTotals[0] + d.roleTotals[2]);
        w = soloGames;
        if (w / soloTot < 0.004) continue;
        bonus = jgl[ch.idx * nc + j];
      }
    } else {
      let otherGames = 0;
      for (let r = 0; r < 5; r++) {
        if (r !== rIdx) otherGames += cj.roles[r];
      }
      if (otherGames <= 0 || cj.totalRoles / d.totalGames < 0.0015) continue;
      w = otherGames;
      if (rIdx === 3) bonus = (cj.roles[4] / otherGames) * duo[ch.idx * nc + j];
      else if (rIdx === 4) bonus = (cj.roles[3] / otherGames) * duo[ch.idx * nc + j];
      else if (rIdx === 0 || rIdx === 2) bonus = (cj.roles[1] / otherGames) * jgl[ch.idx * nc + j];
      else if (rIdx === 1) bonus = ((cj.roles[0] + cj.roles[2]) / otherGames) * jgl[ch.idx * nc + j];
    }

    const totalVal = syn[ch.idx * nc + j] + bonus;
    const kitVal = totalVal - ch.apSlope * cj.dmgTilt;
    allies.push({
      champ: cj,
      val: mstate.synMode === "kit" ? kitVal : totalVal,
      totalVal,
      kitVal,
      w,
    });
  }
  return allies;
}

function makeChampChip(item, isOutlier = false) {
  const btn = document.createElement("button");
  btn.type = "button";
  const cls = ["mu-chip"];
  if (item.val >= 0.75) cls.push("good");
  else if (item.val <= -0.75) cls.push("bad");
  if (isOutlier) cls.push("outlier");
  btn.className = cls.join(" ");
  btn.dataset.champion = item.champ.slug;
  const delta = tftText("span", fmtPp(item.val), "mu-chip-delta " + (item.val >= 0 ? "pos" : "neg"));
  btn.append(muImg(item.champ.slug), document.createTextNode(item.champ.name + " "), delta);
  btn.addEventListener("click", () => selectMatchupChampion(item.champ.slug));
  return btn;
}

function renderArchetypeGroupRows(container, d, groups, sortDir, outlierSlugs = null, maxChips = 8) {
  if (!groups.length) {
    container.replaceChildren(tftText("p", "No eligible matchups for this filter.", "sub"));
    return;
  }
  const ordered = groups.slice().sort((a, b) => (a.mean - b.mean) * sortDir);
  const maxAbs = Math.max(1.2, ...ordered.map(g => Math.abs(g.mean)));
  const posColor = css("--s2") || "#059669";
  const negColor = css("--s6") || "#dc2626";

  const wrap = document.createElement("div");
  wrap.className = "mu-arch-rows";
  for (const g of ordered) {
    const row = document.createElement("div");
    row.className = "mu-arch-row";
    row.dataset.arch = String(g.arch.id);

    const head = document.createElement("button");
    head.type = "button";
    head.className = "mu-arch-row-head";
    head.style.setProperty("--arch-c", css(g.arch.color) || "var(--s1)");
    head.title = "Inspect this archetype in the Archetypes view";
    head.append(
      tftText("strong", g.arch.name),
      tftText("span", `${g.items.length} ${g.items.length === 1 ? "pick" : "picks"}`),
    );
    head.addEventListener("click", () => openArchetypeFocus(g.arch.id));

    const meter = document.createElement("div");
    meter.className = "mu-arch-meter";
    const track = document.createElement("div");
    track.className = "mu-div-track";
    const fill = document.createElement("div");
    fill.className = "mu-div-fill";
    const norm = Math.max(-1, Math.min(1, g.mean / maxAbs));
    if (norm >= 0) {
      fill.style.left = "50%";
      fill.style.width = (norm * 50).toFixed(1) + "%";
      fill.style.background = posColor;
    } else {
      const w = -norm * 50;
      fill.style.left = (50 - w).toFixed(1) + "%";
      fill.style.width = w.toFixed(1) + "%";
      fill.style.background = negColor;
    }
    track.append(fill);
    const valSpan = tftText("span", fmtPp(g.mean), "mu-arch-meter-val " + (g.mean >= 0 ? "pos" : "neg"));
    meter.append(track, valSpan);

    const chips = document.createElement("div");
    chips.className = "mu-chip-list";
    const sortedItems = g.items.slice().sort((a, b) =>
      g.mean < 0 ? a.val - b.val : b.val - a.val
    );
    chips.append(...sortedItems.slice(0, maxChips).map(it =>
      makeChampChip(it, outlierSlugs?.has(it.champ.slug))
    ));
    row.append(head, meter, chips);
    wrap.append(row);
  }
  container.replaceChildren(wrap);
}

function renderFlatSplit(container, d, items, leftTitle, rightTitle, leftAsc = true) {
  if (!items.length) {
    container.replaceChildren(tftText("p", "No eligible matchups for this filter.", "sub"));
    return;
  }
  const split = document.createElement("div");
  split.className = "mu-flat-split";

  const makeCol = (title, list) => {
    const col = document.createElement("div");
    col.className = "mu-flat-col";
    col.append(tftText("h3", title));
    const box = document.createElement("div");
    box.className = "mu-flat-list";
    for (const it of list) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "mu-flat-item";
      btn.dataset.champion = it.champ.slug;
      const arch = d.archetypes[it.champ.arch];
      btn.append(
        muImg(it.champ.slug),
        tftText("span", it.champ.name, "mu-flat-name"),
        tftText("span", arch ? arch.short : "", "mu-flat-arch"),
        tftText("span", fmtPp(it.val), "mu-flat-val " + (it.val >= 0 ? "pos" : "neg")),
      );
      btn.addEventListener("click", () => selectMatchupChampion(it.champ.slug));
      box.append(btn);
    }
    col.append(box);
    return col;
  };

  const byLow = items.slice().sort((a, b) => a.val - b.val);
  const byHigh = items.slice().sort((a, b) => b.val - a.val);
  split.append(
    makeCol(leftTitle, (leftAsc ? byLow : byHigh).slice(0, 12)),
    makeCol(rightTitle, (leftAsc ? byHigh : byLow).slice(0, 12)),
  );
  container.replaceChildren(split);
}

function groupItemsByArchetype(d, items) {
  const buckets = new Map();
  for (const it of items) {
    let b = buckets.get(it.champ.arch);
    if (!b) {
      b = { arch: d.archetypes[it.champ.arch], items: [], sumW: 0, sumWV: 0 };
      buckets.set(it.champ.arch, b);
    }
    b.items.push(it);
    b.sumW += it.w;
    b.sumWV += it.w * it.val;
  }
  const groups = [];
  for (const b of buckets.values()) {
    if (!b.items.length || b.sumW <= 0) continue;
    b.mean = b.sumWV / b.sumW;
    groups.push(b);
  }
  return groups;
}

function renderPoolCompanions(d, ch, rIdx) {
  const nc = d.champions.length;
  const { ctr, lane } = d.mats;
  const laneTot = Math.max(1, d.roleTotals[rIdx]);
  const peers = d.champions.filter(c => c.idx === ch.idx || c.roles[rIdx] / laneTot >= 0.005);
  const twinsBox = document.getElementById("mu-twins");
  const coversBox = document.getElementById("mu-covers");

  if (peers.length < 4) {
    twinsBox.replaceChildren(tftText("p", "Not enough lane peers to compare.", "sub"));
    coversBox.replaceChildren(tftText("p", "Not enough lane peers to compare.", "sub"));
    return;
  }

  const myMatchup = j => ctr[ch.idx * nc + j] + lane[ch.idx * nc + j];
  const peerMatchup = (bIdx, j) => ctr[bIdx * nc + j] + lane[bIdx * nc + j];

  const opponents = peers.filter(p => p.idx !== ch.idx);
  const sortedWorst = opponents.slice().sort((a, b) => myMatchup(a.idx) - myMatchup(b.idx));
  const hardCounters = sortedWorst.filter(o => myMatchup(o.idx) < -0.75);
  const counterTargets = hardCounters.length >= 3 ? hardCounters : sortedWorst.slice(0, Math.min(6, sortedWorst.length));

  const twinScores = [];
  const coverScores = [];

  for (const cand of opponents) {
    const xs = [], ys = [], ws = [];
    const sharedPrey = [];
    for (const opp of opponents) {
      if (opp.idx === cand.idx) continue;
      const vMe = myMatchup(opp.idx);
      const vCand = peerMatchup(cand.idx, opp.idx);
      xs.push(vMe);
      ys.push(vCand);
      ws.push(opp.roles[rIdx]);
      if (vMe > 0.7 && vCand > 0.7) {
        sharedPrey.push({ name: opp.name, score: Math.min(vMe, vCand) });
      }
    }
    const corr = weightedCorr(xs, ys, ws);
    sharedPrey.sort((a, b) => b.score - a.score);
    twinScores.push({
      champ: cand,
      corr,
      note: sharedPrey.length
        ? `Also beats ${sharedPrey.slice(0, 2).map(s => s.name).join(", ")}`
        : d.archetypes[cand.arch]?.name || "",
    });

    let cNum = 0, cDen = 0;
    const covered = [];
    for (const cnt of counterTargets) {
      if (cnt.idx === cand.idx) continue;
      const vMe = myMatchup(cnt.idx);
      const vCand = peerMatchup(cand.idx, cnt.idx);
      const weight = Math.max(0.25, -vMe) * Math.sqrt(Math.max(1, cnt.roles[rIdx]));
      cNum += weight * vCand;
      cDen += weight;
      if (vCand > 0.4) {
        covered.push({ name: cnt.name, val: vCand, swing: vCand - vMe });
      }
    }
    if (cDen > 0) {
      covered.sort((a, b) => b.swing - a.swing);
      coverScores.push({
        champ: cand,
        gain: cNum / cDen,
        note: covered.length
          ? "Beats " + covered.slice(0, 2).map(c => `${c.name} (${fmtPp(c.val)})`).join(", ")
          : d.archetypes[cand.arch]?.name || "",
      });
    }
  }

  twinScores.sort((a, b) => b.corr - a.corr);
  coverScores.sort((a, b) => b.gain - a.gain);

  const makePoolBtn = (champ, note, metricText, metricCls) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "mu-pool-item";
    btn.dataset.champion = champ.slug;
    const text = document.createElement("div");
    text.className = "mu-pool-text";
    text.append(tftText("strong", champ.name), tftText("span", note));
    const metric = tftText("span", metricText, "mu-pool-corr " + metricCls);
    btn.append(muImg(champ.slug), text, metric);
    btn.addEventListener("click", () => selectMatchupChampion(champ.slug, mstate.lane));
    return btn;
  };

  twinsBox.replaceChildren(...twinScores.slice(0, 5).map(t =>
    makePoolBtn(t.champ, t.note, (t.corr >= 0 ? "+" : "") + t.corr.toFixed(2) + " r", t.corr >= 0 ? "pos" : "neg")
  ));
  coversBox.replaceChildren(...coverScores.slice(0, 5).map(c =>
    makePoolBtn(c.champ, c.note, fmtPp(c.gain), c.gain >= 0 ? "pos" : "neg")
  ));
}

function renderMatchupIdentity() {
  const d = mstate.data;
  if (!d?.champions?.length) return;
  const ch = d.champBySlug.get(mstate.champion) || d.champions[0];
  const rIdx = MU_LANES.includes(mstate.lane) ? MU_LANES.indexOf(mstate.lane) : ch.primaryLaneIdx;
  const lane = MU_LANES[rIdx];
  const arch = d.archetypes[ch.arch];

  // Hero
  const icon = document.getElementById("mu-hero-icon");
  icon.src = iconUrl(ch.slug);
  icon.alt = ch.name;
  document.getElementById("mu-hero-name").textContent = ch.name;

  const badge = document.getElementById("mu-hero-arch");
  badge.textContent = arch ? arch.name : "Archetype";
  badge.style.setProperty("--arch-c", css(arch?.color || "--s1") || "var(--s1)");

  const laneGames = ch.roles[rIdx];
  const roleShare = ch.totalRoles > 0 ? Math.round((laneGames / ch.totalRoles) * 100) : 0;
  const lanePick = d.roleTotals[rIdx] > 0 ? ((laneGames / d.roleTotals[rIdx]) * 100).toFixed(1) : "0.0";
  const baseDelta = ch.main?.[rIdx] ?? 0;
  document.getElementById("mu-hero-meta").textContent =
    `${MU_LANE_FULL[lane]} base role strength ${fmtPp(baseDelta)} · ${roleShare}% of ${ch.name}'s games (${lanePick}% lane pick share)`;

  // Lane selector buttons (viable lanes + active lane if non-viable)
  const laneIndices = ch.viableLanes.includes(rIdx)
    ? ch.viableLanes
    : [...ch.viableLanes, rIdx].sort((a, b) => a - b);
  document.getElementById("mu-lane-seg").replaceChildren(...laneIndices.map(idx => {
    const lKey = MU_LANES[idx];
    const btn = document.createElement("button");
    btn.type = "button";
    btn.dataset.lane = lKey;
    btn.className = lKey === lane ? "active" : "";
    const pct = ch.totalRoles > 0 ? Math.round((ch.roles[idx] / ch.totalRoles) * 100) : 0;
    btn.textContent = `${MU_LANE_FULL[lKey]} (${pct}%)`;
    btn.addEventListener("click", () => {
      mstate.lane = lKey;
      renderMatchupIdentity();
      updateHash(true);
    });
    return btn;
  }));

  // 1. Statistical fingerprint (top 8 traits by |z|, ordered from highest +z to lowest -z)
  const traitBlue = css("--s1") || "#2563eb";
  const traitSlate = css("--s8") || "#64748b";
  const ownTraits = d.traits.map((t, k) => ({ t, z: ch.traits[k] || 0 }))
    .sort((a, b) => Math.abs(b.z) - Math.abs(a.z))
    .slice(0, 8)
    .sort((a, b) => b.z - a.z);
  document.getElementById("mu-own-traits").replaceChildren(...ownTraits.map(({ t, z }) =>
    muDivergingRow(
      t.label,
      (z >= 0 ? "+" : "") + z.toFixed(1) + "z",
      z / 2.6,
      traitBlue,
      traitSlate,
      "",
      t.desc,
    )
  ));

  // 2. Team composition appetite
  const posGreen = css("--s2") || "#059669";
  const negRed = css("--s6") || "#dc2626";
  const compRows = [
    {
      label: "AP vs. AD allies",
      val: ch.apSlope,
      unit: "pp",
      desc: "Synergy slope on teammate magic-vs-physical damage share. Positive prefers AP teammates; negative prefers AD teammates.",
    },
    {
      label: "Frontline setup",
      val: ch.frontSlope,
      unit: "pp/z",
      desc: "Synergy slope per z-score of ally frontline tanking.",
    },
    {
      label: "Peel & shields",
      val: ch.peelSlope,
      unit: "pp/z",
      desc: "Synergy slope per z-score of ally healing and shielding.",
    },
  ];
  document.getElementById("mu-comp-needs").replaceChildren(...compRows.map(r => {
    const sign = r.val >= 0 ? "+" : "";
    const cls = Math.abs(r.val) >= 0.15 ? (r.val >= 0 ? "pos" : "neg") : "";
    return muDivergingRow(r.label, `${sign}${r.val.toFixed(2)}${r.unit}`, r.val / 1.4, posGreen, negRed, cls, r.desc);
  }));

  // 3. Counters & Matchup Trait Drivers
  const opps = counterOpponents(d, ch, rIdx);
  const ctrGroups = groupItemsByArchetype(d, opps);

  // Variance explained by opponent archetype membership
  let totalW = 0, meanY = 0;
  for (const o of opps) { totalW += o.w; meanY += o.w * o.val; }
  meanY = totalW > 0 ? meanY / totalW : 0;
  let ssTot = 0, ssBetween = 0;
  for (const o of opps) ssTot += o.w * (o.val - meanY) * (o.val - meanY);
  for (const g of ctrGroups) ssBetween += g.sumW * (g.mean - meanY) * (g.mean - meanY);
  const archR2 = ssTot > 1e-9 ? Math.max(0, Math.min(1, ssBetween / ssTot)) : 0;

  document.getElementById("mu-drivers-title").textContent =
    mstate.ctrScope === "lane"
      ? `Matchup trait drivers (${MU_LANE_FULL[lane]})`
      : "Matchup trait drivers (enemy comp)";
  document.getElementById("mu-drivers-r2").textContent =
    `Archetypes explain ${Math.round(archR2 * 100)}% of variance`;

  const ys = opps.map(o => o.val);
  const ws = opps.map(o => o.w);
  const oppCorrs = d.traits.map((t, k) => {
    const xs = opps.map(o => o.champ.traits[k] || 0);
    return { t, r: weightedCorr(xs, ys, ws) };
  }).sort((a, b) => Math.abs(b.r) - Math.abs(a.r)).slice(0, 11);

  document.getElementById("mu-opp-traits").replaceChildren(...oppCorrs.map(({ t, r }) => {
    const cls = Math.abs(r) >= 0.12 ? (r >= 0 ? "pos" : "neg") : "";
    return muDivergingRow(
      t.label,
      (r >= 0 ? "+" : "") + r.toFixed(2) + " r",
      r / 0.55,
      posGreen,
      negRed,
      cls,
      `${t.desc} (${r < 0 ? "higher enemy trait counters " + ch.name : "higher enemy trait favors " + ch.name})`,
    );
  }));

  // Counter toolbar state
  document.querySelectorAll("#mu-ctr-scope button").forEach(b =>
    b.classList.toggle("active", b.dataset.key === mstate.ctrScope));
  const ctrRoleSel = document.getElementById("mu-ctr-role");
  ctrRoleSel.hidden = mstate.ctrScope !== "team";
  ctrRoleSel.value = mstate.ctrRole;
  document.querySelectorAll("#mu-ctr-layout button").forEach(b =>
    b.classList.toggle("active", b.dataset.key === mstate.ctrLayout));

  document.getElementById("mu-ctr-title").textContent =
    mstate.ctrScope === "lane"
      ? `In-lane counters (${MU_LANE_FULL[lane]})`
      : "Enemy comp counters";

  // Leave-one-out class-breakers
  const minGroupSize = mstate.ctrScope === "lane" ? 2 : 3;
  const outlierCandidates = [];
  for (const g of ctrGroups) {
    if (g.items.length < minGroupSize) continue;
    for (const it of g.items) {
      const remW = g.sumW - it.w;
      if (remW <= 0) continue;
      const looMean = (g.sumWV - it.w * it.val) / remW;
      const diff = it.val - looMean;
      const signFlip = (it.val <= -0.6 && looMean >= 0.25) || (it.val >= 0.6 && looMean <= -0.25);
      if (signFlip || (Math.abs(diff) >= 1.75 && Math.abs(it.val) >= 1.2)) {
        outlierCandidates.push({
          champ: it.champ,
          arch: g.arch,
          val: it.val,
          looMean,
          diff,
          rankScore: Math.abs(diff) * (signFlip ? 1.45 : 1.0),
        });
      }
    }
  }
  outlierCandidates.sort((a, b) => b.rankScore - a.rankScore);
  const topOutliers = outlierCandidates.slice(0, 6);
  const outlierSlugs = new Set(topOutliers.map(o => o.champ.slug));

  const outliersWrap = document.getElementById("mu-outliers-wrap");
  outliersWrap.hidden = !topOutliers.length;
  if (topOutliers.length) {
    document.getElementById("mu-outliers").replaceChildren(...topOutliers.map(o => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "mu-outlier-card";
      btn.dataset.champion = o.champ.slug;
      const info = document.createElement("div");
      info.className = "mu-outlier-info";
      const top = document.createElement("div");
      top.className = "mu-outlier-top";
      top.append(
        tftText("span", o.champ.name),
        tftText("span", fmtPp(o.val), o.val >= 0 ? "pos" : "neg"),
      );
      const sub = tftText(
        "div",
        `${o.arch.short} rest ${fmtPp(o.looMean)} (${fmtPp(o.diff)})`,
        "mu-outlier-sub",
      );
      info.append(top, sub);
      btn.append(muImg(o.champ.slug), info);
      btn.addEventListener("click", () => selectMatchupChampion(o.champ.slug));
      return btn;
    }));
  }

  const ctrBody = document.getElementById("mu-ctr-body");
  if (mstate.ctrLayout === "archetype") {
    renderArchetypeGroupRows(ctrBody, d, ctrGroups, 1, outlierSlugs, 9);
  } else {
    renderFlatSplit(ctrBody, d, opps, "Hardest counters", "Best matchups", true);
  }

  // 4. Synergies & Best Partners
  document.querySelectorAll("#mu-syn-mode button").forEach(b =>
    b.classList.toggle("active", b.dataset.key === mstate.synMode));
  document.getElementById("mu-syn-role").value = mstate.synRole;
  document.querySelectorAll("#mu-syn-layout button").forEach(b =>
    b.classList.toggle("active", b.dataset.key === mstate.synLayout));

  document.getElementById("mu-syn-hint").textContent =
    mstate.synMode === "kit"
      ? `Kit synergy strips out generic AD/AP team-damage balance (${fmtPp(ch.apSlope)} AP ally slope) so mechanical combos surface cleanly.`
      : "Total draft synergy combines mechanical combo value with AD/AP team damage balance.";

  const allies = synergyAllies(d, ch, rIdx);
  const synBody = document.getElementById("mu-syn-body");
  if (mstate.synLayout === "archetype") {
    const synGroups = groupItemsByArchetype(d, allies);
    renderArchetypeGroupRows(synBody, d, synGroups, -1, null, 8);
  } else {
    renderFlatSplit(synBody, d, allies, "Best partners", "Weakest / anti-synergy partners", false);
  }

  // 5. Same-lane pool companions
  renderPoolCompanions(d, ch, rIdx);
}

function renderMatrixDetail() {
  const d = mstate.data;
  const detail = document.getElementById("mu-matrix-detail");
  if (!d?.archetypes?.length || !mstate.matCell) {
    detail.hidden = true;
    return;
  }
  const [r, c] = mstate.matCell;
  const archR = d.archetypes[r], archC = d.archetypes[c];
  if (!archR || !archC) {
    detail.hidden = true;
    return;
  }
  detail.hidden = false;

  const mode = mstate.matMode;
  const cellVal = mode === "counter" ? archR.ctr[c] : mode === "kit" ? archR.kitSyn[c] : archR.syn[c];
  const nc = d.champions.length;
  const { ctr, syn } = d.mats;

  const pairs = [];
  const seen = new Set();
  for (const i of archR.members) {
    const ci = d.champions[i];
    for (const j of archC.members) {
      if (i === j) continue;
      if (mode !== "counter" && r === c) {
        const key = i < j ? `${i}-${j}` : `${j}-${i}`;
        if (seen.has(key)) continue;
        seen.add(key);
      }
      const cj = d.champions[j];
      let val;
      if (mode === "counter") {
        val = ctr[i * nc + j];
      } else if (mode === "kit") {
        val = syn[i * nc + j] - 0.5 * (ci.apSlope * cj.dmgTilt + cj.apSlope * ci.dmgTilt);
      } else {
        val = syn[i * nc + j];
      }
      pairs.push({ ci, cj, val });
    }
  }

  const head = document.createElement("div");
  head.className = "mu-matrix-detail-head";
  const sep = mode === "counter" ? "vs." : "+";
  const titleStrong = tftText("strong", `${archR.name} ${sep} ${archC.name}: ${fmtPp(cellVal)}`);
  const metaSub = tftText("span", `${fmtInt(pairs.length)} champion pairings · select any pair to open its draft identity`, "sub");
  head.append(titleStrong, metaSub);

  if (!pairs.length) {
    detail.replaceChildren(head, tftText("p", "No distinct champion pairs in this cell.", "sub"));
    return;
  }

  const makePairCol = (heading, list) => {
    const col = document.createElement("div");
    col.className = "mu-flat-col";
    col.append(tftText("h3", heading));
    const box = document.createElement("div");
    box.className = "mu-flat-list";
    for (const p of list) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "mu-flat-item";
      btn.dataset.champion = p.ci.slug;
      const pairLabel = mode === "counter"
        ? `${p.ci.name} vs. ${p.cj.name}`
        : `${p.ci.name} + ${p.cj.name}`;
      btn.append(
        muImg(p.ci.slug),
        muImg(p.cj.slug),
        tftText("span", pairLabel, "mu-flat-name"),
        tftText("span", "", "mu-flat-arch"),
        tftText("span", fmtPp(p.val), "mu-flat-val " + (p.val >= 0 ? "pos" : "neg")),
      );
      btn.addEventListener("click", () => selectMatchupChampion(p.ci.slug));
      box.append(btn);
    }
    col.append(box);
    return col;
  };

  const split = document.createElement("div");
  split.className = "mu-flat-split";
  const byHigh = pairs.slice().sort((a, b) => b.val - a.val).slice(0, 6);
  const byLow = pairs.slice().sort((a, b) => a.val - b.val).slice(0, 6);
  if (mode === "counter") {
    split.append(
      makePairCol(`Best ${archR.short} picks into ${archC.short}`, byHigh),
      makePairCol(`Best ${archC.short} answers`, byLow),
    );
  } else {
    split.append(
      makePairCol("Strongest ally combos", byHigh),
      makePairCol("Weakest / clashing pairs", byLow),
    );
  }
  detail.replaceChildren(head, split);
}

function renderArchetypesPanel() {
  const d = mstate.data;
  if (!d?.archetypes?.length) return;
  const archs = d.archetypes;
  const mode = mstate.matMode;

  document.querySelectorAll("#mu-mat-mode button").forEach(b =>
    b.classList.toggle("active", b.dataset.key === mode));

  document.getElementById("mu-mat-hint").textContent =
    mode === "counter"
      ? "Row vs. column counter delta (in percentage points): green means the row archetype beats the column archetype. Select any cell to inspect top champion pairs."
      : mode === "kit"
        ? "Comp-adjusted mechanical kit synergy between ally archetypes (AD/AP damage balance removed). Select any cell to inspect top champion pairs."
        : "Total same-team draft synergy between ally archetypes, including AD/AP damage balance. Select any cell to inspect top champion pairs.";

  let maxCell = 0.6;
  for (let r = 0; r < archs.length; r++) {
    const arr = mode === "counter" ? archs[r].ctr : mode === "kit" ? archs[r].kitSyn : archs[r].syn;
    for (let c = 0; c < archs.length; c++) {
      if (mode === "counter" && r === c) continue;
      if (Math.abs(arr[c]) > maxCell) maxCell = Math.abs(arr[c]);
    }
  }

  const thead = document.querySelector("#mu-matrix-tbl thead");
  const headRow = document.createElement("tr");
  headRow.append(tftText("th", mode === "counter" ? "Row vs. Col" : "Archetype"));
  for (const a of archs) {
    const th = document.createElement("th");
    th.textContent = a.short;
    th.title = a.name;
    headRow.append(th);
  }
  thead.replaceChildren(headRow);

  const tbody = document.querySelector("#mu-matrix-tbl tbody");
  tbody.replaceChildren(...archs.map((a, r) => {
    const tr = document.createElement("tr");
    const rowHead = document.createElement("td");
    rowHead.textContent = a.short;
    rowHead.title = a.name;
    rowHead.style.borderInlineStart = `3px solid ${css(a.color) || "var(--s1)"}`;
    tr.append(rowHead);

    const arr = mode === "counter" ? a.ctr : mode === "kit" ? a.kitSyn : a.syn;
    for (let c = 0; c < archs.length; c++) {
      const td = document.createElement("td");
      td.className = "mu-mat-cell";
      td.dataset.row = String(r);
      td.dataset.col = String(c);
      if (mstate.matCell && mstate.matCell[0] === r && mstate.matCell[1] === c) {
        td.classList.add("sel");
      }
      if (mode === "counter" && r === c) {
        td.textContent = "·";
        td.style.color = "var(--muted)";
      } else {
        const v = arr[c] || 0;
        td.textContent = (v >= 0 ? "+" : "") + v.toFixed(2);
        td.tabIndex = 0;
        td.setAttribute("role", "button");
        td.title = `${a.name} ${mode === "counter" ? "vs." : "+"} ${archs[c].name}: ${fmtPp(v)}`;
        const pct = Math.round(Math.min(1, Math.abs(v) / maxCell) * 34);
        if (pct >= 3) {
          const tint = v >= 0 ? "var(--s2)" : "var(--s6)";
          td.style.background = `color-mix(in srgb, ${tint} ${pct}%, var(--surface-1))`;
        }
        const activate = () => {
          mstate.matCell = [r, c];
          document.querySelectorAll("#mu-matrix-tbl .mu-mat-cell").forEach(el =>
            el.classList.toggle("sel", el === td));
          renderMatrixDetail();
          updateHash(true);
        };
        td.addEventListener("click", activate);
        td.addEventListener("keydown", e => {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(); }
        });
      }
      tr.append(td);
    }
    return tr;
  }));

  renderMatrixDetail();

  // Archetype cards directory
  document.getElementById("mu-arch-search").value = mstate.archSearch;
  document.querySelectorAll("#mu-arch-role button").forEach(b =>
    b.classList.toggle("active", b.dataset.role === mstate.archRole));

  const q = mstate.archSearch.trim().toLowerCase();
  const rIdx = MU_LANES.indexOf(mstate.archRole);
  const cards = [];

  for (const a of archs) {
    const archMatch = q && (`${a.name} ${a.short}`).toLowerCase().includes(q);
    const members = a.members.map(i => d.champions[i]).filter(c => {
      if (rIdx >= 0) {
        const share = c.roles[rIdx] / Math.max(1, d.roleTotals[rIdx]);
        if (!c.viableLanes.includes(rIdx) && share < 0.004) return false;
      }
      if (q && !archMatch && !c.name.toLowerCase().includes(q) && !c.slug.includes(q)) return false;
      return true;
    });
    if (!members.length) continue;

    const card = document.createElement("div");
    card.className = "mu-arch-card" + (mstate.highlightArch === a.id ? " highlight" : "");
    card.dataset.arch = String(a.id);
    card.style.setProperty("--arch-c", css(a.color) || "var(--s1)");

    const topLanes = a.roles
      .map((sh, idx) => ({ sh, idx }))
      .filter(x => x.sh >= 0.14)
      .sort((x, y) => y.sh - x.sh)
      .map(x => `${MU_LANE_SHORT[MU_LANES[x.idx]]} ${Math.round(x.sh * 100)}%`)
      .join(" · ");

    const head = document.createElement("div");
    head.className = "mu-arch-card-head";
    head.append(
      tftText("h3", a.name),
      tftText("span", `${members.length} champions${topLanes ? " · " + topLanes : ""}`, "sub"),
    );

    const sig = document.createElement("div");
    sig.className = "mu-arch-card-sig";
    for (const [tIdx, z] of a.sig) {
      const trMeta = d.traits[tIdx];
      if (!trMeta) continue;
      const pill = tftText("span", `${trMeta.short} ${z >= 0 ? "+" : ""}${z.toFixed(1)}z`, "mu-sig-pill");
      pill.title = trMeta.desc;
      sig.append(pill);
    }

    const otherArchs = archs.filter(o => o.id !== a.id);
    const beats = otherArchs.slice().sort((x, y) => a.ctr[y.id] - a.ctr[x.id]).filter(o => a.ctr[o.id] > 0.05).slice(0, 2);
    const loses = otherArchs.slice().sort((x, y) => a.ctr[x.id] - a.ctr[y.id]).filter(o => a.ctr[o.id] < -0.05).slice(0, 2);
    const bestAlly = archs.slice().sort((x, y) => a.kitSyn[y.id] - a.kitSyn[x.id])[0];

    const ecoLines = [];
    if (beats.length) ecoLines.push("Strong into: " + beats.map(o => `${o.short} (${fmtPp(a.ctr[o.id])})`).join(", "));
    if (loses.length) ecoLines.push("Weak into: " + loses.map(o => `${o.short} (${fmtPp(a.ctr[o.id])})`).join(", "));
    if (bestAlly) ecoLines.push(`Best kit partner: ${bestAlly.short} (${fmtPp(a.kitSyn[bestAlly.id])})`);

    const eco = document.createElement("div");
    eco.className = "mu-arch-card-eco";
    ecoLines.forEach((line, idx) => {
      if (idx > 0) eco.append(document.createElement("br"));
      eco.append(document.createTextNode(line));
    });

    const chipList = document.createElement("div");
    chipList.className = "mu-chip-list";
    for (const m of members) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "mu-chip";
      btn.dataset.champion = m.slug;
      btn.append(muImg(m.slug), document.createTextNode(m.name));
      btn.addEventListener("click", () => {
        const targetLane = rIdx >= 0 && m.viableLanes.includes(rIdx) ? MU_LANES[rIdx] : m.primaryLane;
        selectMatchupChampion(m.slug, targetLane);
      });
      chipList.append(btn);
    }

    card.append(head, sig, eco, chipList);
    cards.push(card);
  }

  document.getElementById("mu-arch-grid").replaceChildren(
    ...(cards.length ? cards : [tftText("p", "No archetypes match these filters.", "sub")])
  );
}

function renderMatchups() {
  const d = mstate.data;
  const prov = document.getElementById("mu-provenance");
  const isIdentity = mstate.mode === "identity";

  document.querySelectorAll("#mu-mode-seg button").forEach(b => {
    const on = b.dataset.key === mstate.mode;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", String(on));
  });

  if (mstate.error) {
    prov.textContent = mstate.error;
    document.getElementById("mu-browser").hidden = true;
    document.getElementById("mu-identity-panel").hidden = true;
    document.getElementById("mu-archetypes-panel").hidden = true;
    return;
  }
  if (!d) {
    prov.textContent = "Loading…";
    document.getElementById("mu-browser").hidden = true;
    document.getElementById("mu-identity-panel").hidden = true;
    document.getElementById("mu-archetypes-panel").hidden = true;
    return;
  }
  if (!d.synced || !d.champions?.length) {
    prov.textContent = "No matchups snapshot yet: run cli.py matchups sync";
    document.getElementById("mu-browser").hidden = true;
    document.getElementById("mu-identity-panel").hidden = true;
    document.getElementById("mu-archetypes-panel").hidden = true;
    return;
  }

  ensureMatchupSelection();

  const bits = [];
  if (d.games) bits.push(`${fmtCompact(d.games)} Master+ games`);
  if (d.cutoff) bits.push(`cutoff ${d.cutoff}`);
  bits.push("synced " + new Date(d.synced).toLocaleString([], { dateStyle: "short", timeStyle: "short" }));
  prov.textContent = bits.join(" · ");

  document.getElementById("mu-browser").hidden = !isIdentity;
  document.getElementById("mu-identity-panel").hidden = !isIdentity;
  document.getElementById("mu-archetypes-panel").hidden = isIdentity;

  if (isIdentity) {
    renderMatchupsBrowser();
    renderMatchupIdentity();
  } else {
    renderArchetypesPanel();
  }
}

async function openMatchups() {
  if (mstate.data) {
    mstate.error = null;
    ensureMatchupSelection();
    renderMatchups();
    updateHash();
    return;
  }
  if (mstate.loading) return;
  mstate.loading = true;
  mstate.error = null;
  const req = ++mstate.req;
  renderMatchups();
  try {
    const raw = await api("api/matchups.json");
    if (req !== mstate.req) return;
    mstate.data = prepareMatchupsData(raw);
    if (mstate.data?.archetypes?.length) populateMatchupsArchSelect(mstate.data);
    ensureMatchupSelection();
  } catch (e) {
    if (req !== mstate.req) return;
    mstate.error = `Matchups data unavailable (${e.message}). Restart the dashboard server if this tab was just added.`;
  } finally {
    if (req === mstate.req) mstate.loading = false;
  }
  renderMatchups();
  updateHash();
}

document.getElementById("mu-hero-arch").addEventListener("click", () => {
  const ch = mstate.data?.champBySlug.get(mstate.champion);
  if (ch) openArchetypeFocus(ch.arch);
});

document.querySelectorAll("#mu-mode-seg button").forEach(b =>
  b.addEventListener("click", () => {
    const next = b.dataset.key;
    if (next === mstate.mode || !["identity", "archetypes"].includes(next)) return;
    mstate.mode = next;
    mstate.highlightArch = null;
    ensureMatchupSelection();
    renderMatchups();
    updateHash(true);
  }));

document.getElementById("mu-search").addEventListener("input", e => {
  mstate.search = e.target.value;
  renderMatchupsBrowser();
});

document.querySelectorAll("#mu-role-filter button").forEach(b =>
  b.addEventListener("click", () => {
    mstate.roleFilter = b.dataset.role;
    renderMatchupsBrowser();
  }));

document.getElementById("mu-arch-filter").addEventListener("change", e => {
  mstate.archFilter = e.target.value;
  renderMatchupsBrowser();
});

document.querySelectorAll("#mu-ctr-scope button").forEach(b =>
  b.addEventListener("click", () => {
    if (mstate.ctrScope === b.dataset.key) return;
    mstate.ctrScope = b.dataset.key;
    renderMatchupIdentity();
    updateHash(true);
  }));

document.getElementById("mu-ctr-role").addEventListener("change", e => {
  mstate.ctrRole = e.target.value;
  renderMatchupIdentity();
});

document.querySelectorAll("#mu-ctr-layout button").forEach(b =>
  b.addEventListener("click", () => {
    mstate.ctrLayout = b.dataset.key;
    renderMatchupIdentity();
  }));

document.querySelectorAll("#mu-syn-mode button").forEach(b =>
  b.addEventListener("click", () => {
    if (mstate.synMode === b.dataset.key) return;
    mstate.synMode = b.dataset.key;
    renderMatchupIdentity();
    updateHash(true);
  }));

document.getElementById("mu-syn-role").addEventListener("change", e => {
  mstate.synRole = e.target.value;
  renderMatchupIdentity();
});

document.querySelectorAll("#mu-syn-layout button").forEach(b =>
  b.addEventListener("click", () => {
    mstate.synLayout = b.dataset.key;
    renderMatchupIdentity();
  }));

document.querySelectorAll("#mu-mat-mode button").forEach(b =>
  b.addEventListener("click", () => {
    if (mstate.matMode === b.dataset.key) return;
    mstate.matMode = b.dataset.key;
    if (mstate.data) {
      if (mstate.matMode === "counter" && mstate.matCell && mstate.matCell[0] === mstate.matCell[1]) {
        mstate.matCell = defaultMatrixCell(mstate.data, mstate.matMode);
      }
    }
    renderArchetypesPanel();
    updateHash(true);
  }));

document.getElementById("mu-arch-search").addEventListener("input", e => {
  mstate.archSearch = e.target.value;
  renderArchetypesPanel();
});

document.querySelectorAll("#mu-arch-role button").forEach(b =>
  b.addEventListener("click", () => {
    mstate.archRole = b.dataset.role;
    renderArchetypesPanel();
    updateHash();
  }));

