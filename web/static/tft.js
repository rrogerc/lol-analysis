"use strict";

// ---------------- tft view ----------------
// Same shape as the builds tab: precomputed cells keyed by unit and
// scenario (star, geometry, trait context, tank threat), a status endpoint saying which
// exist, a breakdown card for the selected build.
const TSRC_COLOR = { auto: "--s3", ability: "--s1", burn: "--s8", bleed: "--s6",
                     solar: "--s5", trail: "--s2", thorns: "--s7", "ionic spark": "--s4" };
const tstate = { meta: null, metaReq: 0, mode: "builds", unit: null, star: 2, geo: "clump", traits: "bare", threat: "mixed",
                 data: null, dataRevision: null, sel: 0, status: null, req: 0, timer: null,
                 polling: null, opening: null, pending: false, statusUnavailable: false,
                 cls: "all", cost: null, search: "" };
const tcore = { selected: null, summaries: new Map(), variedComponents: true };
const tboard = { mode: "damage", star: "s2", geo: "clump", traits: "low", threat: "mixed",
                 role: "all", cost: "4", search: "", data: null, key: null, req: 0,
                 loading: false, pending: false, error: null, controller: null,
                 fromLeaderboard: false, returnUnit: null };
const tearly = { data: null, req: 0, controller: null, promise: null, loading: false, pending: false, error: null,
                 level: 4, allowThree: true, unit: "all", sort: "wins", order: "desc", page: 1,
                 cards: new Map(), openBoards: new Set() };
const tflex = { catalog: null, metaReq: 0, metaLoading: null, controller: null, loading: false,
                cost: 4, cores: [], coreSearch: "", preference: "breadth", minCost: 1, minFrontline: 3, allowFive: false,
                locked: [], lockSearch: "", searchReq: 0, searching: false, boards: [], key: null,
                expanded: 0, pairsDone: 0, pairsTotal: 0, sort: "score", order: "desc", page: 1,
                lobbyProfile: null, lobbyPlayers: 7, lobbyDevelopment: 0.5, lobbyOverrides: {}, lobbySearch: "", lobbyReq: 0,
                lobbySwapBoards: new Map(),
                cards: new Map(), openBoards: new Set(), pending: false, error: null };
const TFT_CLASS = { all: "every unit", carry: "carries: marksmen, casters and specialists — the dummies never hit back",
                    fighter: "fighters and assassins — the dummies hit back, ranked by kill time then damage before dying",
                    tank: "tanks — the dummies hit back, ranked by how long the unit holds them" };
function tftThreatFor(unit, requested = tstate.threat) {
  return unit?.objective === "tank" && tstate.meta?.tankThreats?.some(t => t.key === requested)
    ? requested : "mixed";
}

function tkey(o = {}) {
  const unit = tstate.meta?.units.find(u => u.slug === (o.unit ?? tstate.unit));
  const threat = tftThreatFor(unit, o.threat ?? tstate.threat);
  return `s${o.star ?? tstate.star}-${o.geo ?? tstate.geo}-${o.traits ?? tstate.traits}` +
    (threat === "mixed" ? "" : `-${threat}`);
}

function tftTankDebuffNote(debuffs) {
  if (!debuffs) return "";
  const percent = n => Math.round(n * 100);
  return `Enemy heal cut reduces healing by ${percent(debuffs.wound)}%; sunder reduces armor by ${percent(debuffs.sunder)}%; shred reduces MR by ${percent(debuffs.shred)}%. All three stay active for the entire fight.`;
}

function tftTargetDebuffNote(debuffs) {
  if (!debuffs || !(debuffs.sunder || debuffs.shred)) return "";
  return `All targets have ${Math.round((debuffs.sunder || 0) * 100)}% Sunder and ${Math.round((debuffs.shred || 0) * 100)}% Shred from combat start, supplied by the team. Defenses shown include this reduction. Last Whisper and Void Staff do not apply it again; their other stats and effects still count.`;
}

function renderTftThreat(unit) {
  tstate.threat = tftThreatFor(unit);
  document.getElementById("tft-threat-setting").hidden = unit.objective !== "tank" || !tstate.meta.tankThreats?.length;
  tftActive("tft-threat-seg", tstate.threat);
  const threat = tstate.meta.tankThreats?.find(t => t.key === tstate.threat);
  document.getElementById("tft-threat-hint").textContent = threat
    ? `${threat.description} Heal cut, sunder and shred stay active throughout.` : "";
}


function tftUnitTraits(unit) {
  return (unit.traits || []).map((name, i) =>
    (tstate.meta.traits || []).find(t => t.api === unit.traitApis?.[i] || t.name === name) ||
    { api: name, name, icon: null, levels: [], styles: [] });
}

function tftTraitTier(trait, index) {
  // The snapshot uses MetaTFT style codes, which differ from CDragon's codes.
  return ({ 1: "Bronze", 3: "Silver", 4: "Gold", 5: "Prismatic", 6: "Unique" })[trait.styles?.[index]] || "Active";
}

function tftTraitStatus(unit, trait, context) {
  const bonus = unit.traitBonuses?.[context]?.find(b => b.api === trait.api);
  if (!bonus) return { bonus: null, count: "", tier: "", label:
    (unit.traitsModeled || []).includes(trait.name) ? "Inactive" : "Not modeled" };
  const next = (trait.levels || []).find(level => level > bonus.breakpoint) ?? bonus.breakpoint;
  const count = `${bonus.breakpoint}/${next}`;
  const tier = tftTraitTier(trait, (trait.levels || []).indexOf(bonus.breakpoint));
  return { bonus, count, tier, label: `${count} · ${tier}` };
}


function tftChampionTooltip(unit, entry = null) {
  const subtitle = [entry && `${entry.star}★`, `${unit.cost} cost`, entry?.form && `${entry.form} form`,
    entry?.role || unit.role, entry && Number.isFinite(entry.range) && `${entry.range} range`].filter(Boolean).join(" · ");
  return [tftTooltipHead(unit.icon, unit.name, subtitle),
    tftText("p", `Ability: ${unit.ability || "—"}`),
    ...tftUnitTraits(unit).map(trait => {
      const row = document.createElement("div");
      row.className = "tft-tooltip-trait";
      row.append(tftIcon(trait.icon, trait.name, true), tftText("strong", trait.name),
        tftText("span", (trait.levels || []).join(" / "), "sub"));
      return row;
    }),
    tftText("p", "Select this champion to inspect its trait bonuses below.", "sub")];
}

function tftTraitTooltip(unit, trait, context = tstate.traits) {
  const status = tftTraitStatus(unit, trait, context), bonus = status.bonus;
  const modeled = (unit.traitsModeled || []).includes(trait.name);
  const content = [tftTooltipHead(trait.icon, trait.name, bonus ? `Simulated: ${status.label}` : status.label, true)];
  if (trait.description) content.push(tftText("p", trait.description));
  if (trait.levels?.length) {
    content.push(tftText("p", "Breakpoints · units needed", "sub"));
    const levels = document.createElement("div");
    levels.className = "tft-breakpoints";
    levels.append(...trait.levels.map((level, i) => {
      const tier = tftTraitTier(trait, i), prismatic = tier === "Prismatic";
      const el = tftText("span", `${level} · ${tier}`, "tft-breakpoint tft-tier" +
        (bonus?.breakpoint === level ? " active" : "") + (prismatic ? " prismatic" : ""));
      el.dataset.tier = tier.toLowerCase();
      el.title = `${level} units · ${tier}`;
      return el;
    }));
    content.push(levels);
  }
  const heading = bonus ? `Applied in this fight: ${bonus.breakpoint} ${trait.name}`
    : modeled ? "No bonus applied with the current trait setting."
    : "This trait is not included in the build calculation.";
  content.push(tftText("p", heading, "tft-bonus-note"));
  if (bonus) content.push(tftText("p", "Count = simulated units / next breakpoint; at the final breakpoint both numbers match.", "sub"));
  for (const note of bonus?.notes || []) content.push(tftText("p", note));
  const preview = !bonus && !trait.description && unit.traitBonuses?.high?.find(b => b.api === trait.api);
  if (preview) {
    content.push(tftText("p", `Preview at ${preview.breakpoint} units (currently inactive):`, "sub"));
    preview.notes.forEach(note => content.push(tftText("p", note)));
  } else if (!bonus && trait.note) content.push(tftText("p", trait.note, "sub"));
  return content;
}

function renderTftSelected(unit) {
  hideTftTooltip();
  document.getElementById("tft-selected").dataset.cost = unit.cost;
  document.getElementById("tft-selected-name").textContent = unit.name;
  document.getElementById("tft-selected-icon").replaceChildren(tftIcon(unit.icon, unit.name));
  document.getElementById("tft-selected-meta").textContent = `${unit.cost} cost · ${unit.role} · ${unit.ability}`;
  document.getElementById("tft-selected-traits").replaceChildren(...tftUnitTraits(unit).map(trait => {
    const bonus = unit.traitBonuses?.[tstate.traits]?.find(b => b.api === trait.api);
    const modeled = (unit.traitsModeled || []).includes(trait.name);
    const btn = document.createElement("button");
    btn.className = "tft-trait-chip";
    btn.dataset.trait = trait.api;
    btn.dataset.active = String(!!bonus);
    const status = bonus ? `${bonus.breakpoint} active` : modeled ? "inactive" : "not modeled";
    btn.setAttribute("aria-label", `${trait.name}, ${status}. Show trait details.`);
    btn.append(tftIcon(trait.icon, trait.name, true), tftText("span", trait.name), tftText("small", status));
    bindTftTooltip(btn, () => tftTraitTooltip(unit, trait), true);
    return btn;
  }));
}

function tftEnemyFormation(dummy, geometry) {
  const raw = dummy?.slots || [];
  const explicit = raw.some(slot => "lane" in slot || "row" in slot);
  const published = tstate.meta?.formations?.[geometry];
  const coordinates = explicit ? raw : Array.isArray(published) && published.length === raw.length ? published : null;
  const positioned = raw.length > 0 && !!coordinates?.every(position =>
    Number.isFinite(position?.lane) && Number.isFinite(position?.row));
  const slots = raw.map((slot, index) => {
    const frontline = slot.line ? slot.line === "frontline" : slot.nearby !== false;
    const main = frontline && !!slot.fixedDefenses;
    return { slot, index, frontline, main,
      label: main ? "Main tank" : slot.label || (frontline ? "Supporting frontliner" : "Backline target"),
      position: positioned ? { lane: coordinates[index].lane, row: coordinates[index].row } : null };
  });
  return { slots, frontline: slots.filter(target => target.frontline),
    backline: slots.filter(target => !target.frontline), mainTanks: slots.filter(target => target.main), positioned };
}

function renderTftFight(unit, scenario = null, build = null) {
  const objective = build?.objective || unit.objective;
  const tank = objective === "tank", pressured = typeof build?.pressure === "boolean" ? build.pressure : objective !== "carry";
  const nearestThree = tank && unit.name === "Hecarim";
  const threatKey = scenario?.threat || tstate.threat;
  const dm = scenario?.dummy || (tank ? tstate.meta.tankDummies?.[threatKey] : null) || tstate.meta.dummy;
  const preview = document.querySelector(".tft-fight-preview");
  preview.hidden = !dm?.slots?.length;
  if (preview.hidden) return;
  const geometry = scenario?.geometry || tstate.geo, clump = geometry === "clump";
  const formation = tftEnemyFormation(dm, geometry);
  const frontCount = formation.frontline.length, backCount = formation.backline.length;
  const mainCount = formation.mainTanks.length, supportCount = frontCount - mainCount;
  const group = (count, name) => `${count} ${name}${count === 1 ? "" : "s"}`;
  const frontSummary = mainCount ? [group(mainCount, "main tank"),
    ...(supportCount ? [group(supportCount, "supporting frontliner")] : [])].join(" and ") : group(frontCount, "frontliner");
  const summary = `${frontSummary} in front${backCount ? `, with ${group(backCount, "backline target")} behind` : ""}`;
  const coverage = "Each spell’s radius and target-selection rules determine which enemies it reaches. A wide enough radius can include backliners; nearby-only effects follow the frontline grouping.";
  const duration = scenario?.duration || unit.duration;
  const changedRole = preview.dataset.objective !== objective || preview.dataset.pressure !== String(pressured);
  preview.dataset.objective = objective;
  preview.dataset.pressure = String(pressured);
  const threat = tank ? dm.threat || tstate.meta.tankThreats?.find(t => t.key === threatKey) : null;
  const debuffs = tank ? dm.enemyDebuffs || tstate.meta.tankDebuffs : null;
  const targetDebuffs = tank ? {} : dm.targetDebuffs || tstate.meta.targetDebuffs || {};
  document.getElementById("tft-fight-title").textContent = tank ? "Tank survival test" : "Finite-target clear diagnostic";
  document.getElementById("tft-formation-label").textContent =
    `${clump ? "Clumped" : "Spread"}${tank ? " frontline" : ""}${threat ? ` · ${threat.label}` : ""} · up to ${duration}s`;
  const svg = document.getElementById("tft-formation-diagram");
  svg.setAttribute("viewBox", "0 0 420 320");
  svg.replaceChildren();
  const node = (tag, attrs = {}, text) => {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, value));
    if (text !== undefined) el.textContent = text;
    svg.append(el);
    return el;
  };
  const arrow = (x1, y1, x2, y2, color, direction, { width = 2, line, bend, target } = {}) => {
    const [fromX, fromY] = bend || [x1, y1];
    const length = Math.hypot(x2 - fromX, y2 - fromY);
    const dx = (x2 - fromX) / length, dy = (y2 - fromY) / length;
    const path = bend ? `M ${x1} ${y1} Q ${fromX} ${fromY} ${x2} ${y2}` : `M ${x1} ${y1} L ${x2} ${y2}`;
    node("path", { d: path, fill: "none", stroke: color,
      "stroke-width": width, "data-direction": direction, ...(line ? { "data-line": line } : {}),
      ...(target ? { "data-target": target } : {}) });
    node("path", { d: `M ${x2} ${y2} L ${x2 - 8 * dx + 4 * dy} ${y2 - 8 * dy - 4 * dx} L ${x2 - 8 * dx - 4 * dy} ${y2 - 8 * dy + 4 * dx} Z`, fill: color });
  };
  const knownPositions = formation.slots.map(target => target.position).filter(Boolean);
  const catalogPositions = Object.values(tstate.meta?.formations || {}).flat()
    .filter(position => Number.isFinite(position?.lane) && Number.isFinite(position?.row));
  const positions = [...knownPositions, ...catalogPositions];
  const minLane = Math.min(...positions.map(position => position.lane));
  const maxLane = Math.max(...positions.map(position => position.lane));
  const minRow = Math.min(...knownPositions.map(position => position.row));
  const maxRow = Math.max(...knownPositions.map(position => position.row));
  const spaced = (index, count, span) => 210 + (count > 1 ? (index / (count - 1) - .5) * span : 0);
  const plotted = formation.slots.map(target => {
    if (target.position) return { ...target,
      x: 50 + (target.position.lane - minLane) / Math.max(1, maxLane - minLane) * 320,
      y: 150 - (target.position.row - minRow) / Math.max(1, maxRow - minRow) * 90 };
    const peers = target.frontline ? formation.frontline : formation.backline;
    return { ...target, x: spaced(peers.indexOf(target), peers.length, target.frontline && clump ? 160 : 320),
      y: target.frontline ? 150 : 60 };
  });
  node("title", { id: "tft-diagram-title" }, tank
    ? `${group(frontCount, "frontliner")} and ${group(backCount, "backliner")} focus ${unit.name}`
    : `${clump ? "Clumped" : "Spread"} enemy formation: ${formation.slots.length} targets`);
  node("desc", { id: "tft-diagram-desc" }, `${formation.slots.length} enemies: ${summary}. ` +
    (tank ? "Arrows show incoming damage; enemies stay alive throughout the survival test. "
      : `Default attacks follow target order ${formation.slots.map(target => target.index + 1).join(" → ")}. `) +
    (nearestThree ? "Highlights show Hecarim’s three nearest targets. " : "") + coverage);
  const backlineShare = threat?.dps > 0 ? Math.round(threat.backlineDps / threat.dps * 100) : null;
  node("text", { x: 210, y: 18 }, `Backline · ${group(backCount, tank ? "carry" : "target")}` +
    (tank && Number.isFinite(backlineShare) ? ` · ${backlineShare}% of damage` : ""));
  node("text", { x: 210, y: 116 }, `Frontline · ${frontCount}${mainCount ? ` (${group(mainCount, "main tank")})` : ""}`);
  if (nearestThree) plotted.slice(0, 3).forEach(target => node("ellipse", {
    cx: target.x, cy: target.y, rx: 24, ry: 24,
    fill: "color-mix(in srgb, var(--s1) 10%, transparent)", stroke: "var(--s1)",
    "stroke-dasharray": "4 4", "data-targeting": "nearest", "data-target": target.index + 1 }));
  plotted.forEach(target => {
    const { x, y, index, frontline, main, position, slot } = target;
    const line = frontline ? "frontline" : "backline";
    const streams = tank ? dm.board?.[index] ?? slot.streams ?? 1 : frontline ? slot.streams ?? 1 : 0;
    if (pressured && streams > 0) arrow(x, y + 20, 210, 232,
      frontline ? "var(--baseline)" : "var(--s8)", "incoming", {
        width: frontline ? 1.5 : 3.5, line, target: index + 1,
        ...(!frontline ? { bend: [x < 210 ? -30 : 450, 176] } : {}) });
    node("circle", { cx: x, cy: y, r: 18, fill: "var(--surface-1)",
      stroke: frontline ? "var(--s1)" : tank ? "var(--s8)" : "var(--s3)",
      "stroke-width": main ? 3 : 2, "data-line": line, "data-target": index + 1,
      ...(position ? { "data-lane": position.lane, "data-row": position.row } : {}) });
    node("text", { x, y: y + 4, class: "tft-diagram-number" }, String(index + 1));
    node("text", { x, y: y + 33, class: "tft-diagram-role" }, main ? "Main tank" : frontline ? "Frontliner" : tank ? "Carry" : "Backliner");
  });
  if (!tank && plotted.length) arrow(210, 240, plotted[0].x, plotted[0].y + 20,
    "var(--s1)", "outgoing", { target: plotted[0].index + 1 });
  if (tank) node("path", { d: "M 210 232 L 236 241 L 232 265 Q 226 282 210 289 Q 194 282 188 265 L 184 241 Z", fill: "var(--s1)" });
  else node("circle", { cx: 210, cy: 260, r: 18, fill: "var(--s1)" });
  node("text", { x: 210, y: tank ? 262 : 264, class: "tft-diagram-you" }, tank ? "Tank" : "You");
  node("text", { x: 210, y: 315, class: "tft-diagram-number" }, unit.name);
  const movementDelay = !tank && Number.isFinite(dm.meleeRepositionSeconds) && dm.meleeRepositionSeconds > 0
    ? dm.meleeRepositionSeconds : null;
  const layoutNote = formation.positioned ? "Enemy positions come from the simulation’s fixed formation. " : "Enemy positions are schematic. ";
  document.getElementById("tft-formation-caption").textContent = layoutNote + (tank
    ? "Arrows show incoming damage from the individual attackers. " +
      (nearestThree ? "Highlights show Hecarim’s three nearest targets; other attackers continue dealing damage. " : "") +
      "Other spell coverage depends on radius and targeting."
    : (movementDelay !== null
      ? "Schematic layout showing target order. " +
        (Number.isFinite(build?.range) && build.range > 2
          ? `This ${build.range}-range build has no movement delay. `
          : `Builds with 1–2 attack range use an approximate ${movementDelay}s movement delay. `) +
        "Exact hex paths are not simulated."
      : "Schematic layout showing target order. Exact hex paths and movement are not simulated."));
  const medianNote = `Other target defenses use the saved ${dm.star ? `${dm.star}★ ` : ""}group medians.`;
  document.getElementById("tft-dummy-intro").textContent = tank
    ? `${formation.slots.length} attackers focus ${unit.name}: ${summary}. Enemies stay alive while your tank and any on-death body hold them. ${coverage}`
    : `${formation.slots.length} test targets: ${summary}. ` +
      (mainCount ? `${mainCount === 1 ? "The main tank has" : "The main tanks have"} fixed benchmark HP and resistances. ` : "") +
      `${medianNote} ${coverage}`;
  const number = n => Number.isFinite(n) ? n.toLocaleString("en-US", { maximumFractionDigits: 2 }) : "—";
  const statRow = (label, value) => {
    const row = document.createElement("div");
    row.className = "tft-dummy";
    row.append(tftText("strong", label), tftText("span", value));
    return row;
  };
  const slotLabel = (slot, index) => formation.slots[index].label;
  const defenseRows = () => dm.slots.map((slot, i) => statRow(
    `${i + 1} · ${slotLabel(slot, i)}`,
    `${number(slot.hp)} HP · ${number(slot.armor * (1 - (targetDebuffs.sunder || 0)))} armor · ${number(slot.mr * (1 - (targetDebuffs.shred || 0)))} MR`));
  const physicalShare = threat?.physicalShare;
  document.getElementById("tft-dummy-stats").replaceChildren(...(tank ? threat ? [
    statRow("Incoming damage", `${number(threat.dps)} per second on average, before defenses`),
    statRow("Frontline pressure", `${frontCount} tanks · ${number(threat.frontlineDps)}/s · ${Math.round(threat.frontlineDps / threat.dps * 100)}% of damage`),
    statRow("Backline pressure", `${backCount} carries · ${number(threat.backlineDps)}/s · ${Math.round(threat.backlineDps / threat.dps * 100)}% of damage`),
    statRow("Damage mix", `${Math.round(physicalShare * 100)}% physical · ${Math.round((1 - physicalShare) * 100)}% magic`),
    statRow("Backline attacks", `Every ${number(threat.attackInterval)}s per carry · ${Math.round(threat.backlineAttackShare * 100)}% of backline damage`),
    statRow("Backline casts", threat.burst ? `The carries cast together every ${number(threat.castInterval)}s`
      : `Each carry casts every ${number(threat.castInterval)}s, staggered`),
  ] : [statRow("Incoming damage", "Loading the selected damage profile…")] : defenseRows()));
  const targetDefenses = document.getElementById("tft-target-defenses");
  targetDefenses.hidden = !tank;
  if (changedRole) targetDefenses.open = false;
  document.getElementById("tft-target-defense-stats").replaceChildren(...(tank ? defenseRows() : []));
  document.getElementById("tft-pressure-note").textContent = tank
    ? tftTankDebuffNote(debuffs)
    : (pressured ? `The ${frontCount} frontline dummies attack and cast (~${number(dm.pressureDps)} damage/s before defenses); the backline is a damage target only. Defeating a frontliner removes its pressure.`
      : "The dummies do not attack this protected build. Clear-test builds rank by how quickly they defeat the targets, then damage dealt.")
      + " " + tftTargetDebuffNote(targetDebuffs);
  document.getElementById("tft-pressure-details").hidden = !pressured;
  const offense = tank ? [
    ...(threat ? [
      tftText("p", `The ${frontCount} frontline tanks contribute ${number(threat.frontlineDps)} damage/s in every preset. Each attacks every ${number(threat.frontlineAttackInterval)}s and casts every ${number(threat.frontlineCastInterval)}s, staggered across the frontline.`),
      tftText("p", `The ${backCount} backline carries contribute ${number(threat.backlineDps)} damage/s. ${Math.round(threat.backlineAttackShare * 100)}% comes from physical attacks every ${number(threat.attackInterval)}s per carry; the rest comes from casts every ${number(threat.castInterval)}s (${Math.round(threat.backlineSpellPhysicalShare * 100)}% physical, ${Math.round((1 - threat.backlineSpellPhysicalShare) * 100)}% magic). ${threat.burst ? "The carries cast together for a burst of damage." : "Backline attacks and casts are staggered."}`),
      tftText("p", "Stuns interrupt only the enemies they reach. Scheduled casts resume after the stun ends."),
    ] : []),
    ...(dm.threat ? dm.slots.map((slot, i) => tftText("p", `${i + 1} · ${slotLabel(slot, i)}: ${number(slot.ad * dm.critEv)} average physical damage per attack including crit, first at ${number(slot.attackStart)}s and then every ${number(1 / slot.as)}s; ${number(slot.ability)} damage per cast (${Math.round(slot.physicalShare * 100)}% physical), first at ${number(slot.castStart)}s and then every ${number(slot.castInterval)}s. Damage is shown before your defenses.`)) : []),
    ...(threat?.referenceCarries?.length ? [
      tftText("p", "Backline pressure is calibrated from these 2★, three-item carry builds. Each reference attacks one target with zero armor and MR, no trait bonuses and no enemy deaths. All presets share this damage budget; the selected preset changes the backline damage mix and timing."),
      ...threat.referenceCarries.map(carry => tftText("p", `${carry.star}★ ${carry.name} with ${carry.items.join(", ")}: ${number(carry.dps)} damage/s over ${number(carry.duration)}s.`)),
    ] : []),
  ] : pressured ? dm.slots.map((slot, i) =>
    tftText("p", `${i + 1}: ${number(slot.ad)} attack damage at ${number(slot.as)} attacks/s; ${number(slot.ability)} damage per cast (${Math.round(slot.physicalShare * 100)}% physical). Mana: ${number(slot.manaStart)}/${number(slot.manaMax)}, +${number(slot.manaPerAttack)} per attack${slot.manaFromDamage ? " and mana from damage taken" : ""}.`)) : [];
  document.getElementById("tft-dummy-offense").replaceChildren(...offense);
  document.getElementById("tft-targeting-note").textContent = tank
    ? coverage + " " +
      (nearestThree ? `Hecarim targets the first ${Math.min(3, formation.slots.length)} enemies in the nearest-target order shown, in either formation. ` : "") +
      `Builds still holding at ${duration}s are compared again with twice the incoming damage.`
    : `Normal attacks target the first living dummy: ${formation.slots.map(target => target.index + 1).join(" → ")}. ` +
    coverage + " Individual abilities can use different targeting." +
    (movementDelay !== null && !(Number.isFinite(build?.range) && build.range > 2)
      ? ` Movement takes ${movementDelay}s at initial engagement and after the current target dies. Attack cooldowns overlap movement; ongoing damage and incoming pressure continue.` : "");
}

function tftActive(id, key) {
  document.querySelectorAll(`#${id} button`).forEach(b => {
    const selected = b.dataset.key === key;
    b.classList.toggle("active", selected);
    b.setAttribute("aria-pressed", String(selected));
  });
}

function tftSeg(id, opts, set, reload = true) {
  document.getElementById(id).replaceChildren(...opts.map(o => {
    const btn = document.createElement("button");
    btn.textContent = o.label;
    if (o.tag) {
      const t = document.createElement("span");
      t.className = "tag";
      t.textContent = o.tag;
      btn.append(t);
    }
    btn.dataset.key = o.key;
    if (o.cost) btn.dataset.cost = o.cost;
    btn.dataset.title = btn.title = o.title || "";
    btn.setAttribute("aria-pressed", "false");
    btn.addEventListener("click", () => { set(o.key); if (reload) loadTftScenario({ navigate: true }); });
    return btn;
  }));
}

function renderTftUnits() {
  const m = tstate.meta;
  if (!m) return;
  hideTftTooltip();
  const query = tstate.search.trim().toLocaleLowerCase();
  const units = m.units.filter(u => (tstate.cls === "all" || u.objective === tstate.cls) &&
    String(u.cost) === tstate.cost &&
    (!query || [u.name, ...u.traits].some(s => s.toLocaleLowerCase().includes(query))));
  const costs = [...new Set(units.map(u => u.cost))].sort((a, b) => a - b);
  const list = document.getElementById("tft-unit-seg");
  list.replaceChildren(...costs.map(cost => {
    const group = document.createElement("section");
    group.className = "tft-cost-group";
    group.dataset.cost = cost;
    group.setAttribute("aria-labelledby", `tft-cost-${cost}`);
    const heading = document.createElement("div");
    heading.className = "tft-cost-heading";
    const title = document.createElement("h3");
    title.id = `tft-cost-${cost}`;
    title.textContent = `${cost} cost`;
    const members = units.filter(u => u.cost === cost).sort((a, b) => a.name.localeCompare(b.name));
    const count = document.createElement("span");
    count.textContent = `${members.length} ${members.length === 1 ? "champion" : "champions"}`;
    heading.append(title, count);
    const grid = document.createElement("div");
    grid.className = "tft-unit-grid";
    grid.append(...members.map(u => {
      const btn = document.createElement("button");
      btn.className = "tft-unit";
      btn.dataset.key = u.slug;
      btn.dataset.richTooltip = "true";
      btn.setAttribute("aria-label", `${u.name}, ${u.cost} cost, ${u.role}. Traits: ${u.traits.join(", ")}.`);
      const text = document.createElement("span");
      text.className = "tft-unit-text";
      const traits = document.createElement("span");
      traits.className = "tft-unit-traits";
      traits.setAttribute("aria-hidden", "true");
      traits.append(...tftUnitTraits(u).map(t => tftIcon(t.icon, t.name, true)));
      text.append(tftText("span", u.name, "tft-unit-name"), traits);
      btn.append(tftIcon(u.icon, u.name), text);
      bindTftTooltip(btn, () => tftChampionTooltip(u));
      btn.addEventListener("click", () => {
        tstate.unit = u.slug;
        renderTftStars();
        loadTftScenario({ navigate: true });
      });
      return btn;
    }));
    group.append(heading, grid);
    return group;
  }));
  if (!units.length) {
    const empty = document.createElement("p");
    empty.className = "tft-empty";
    empty.textContent = "No champions match these filters. Try another name or trait, or clear the filters.";
    list.append(empty);
  }
  list.scrollTop = 0;
  document.getElementById("tft-unit-count").textContent = `${units.length} of ${m.units.length} champions · grouped by cost`;
  document.getElementById("tft-clear-filters").hidden = !query && tstate.cls === "all";
  tftActive("tft-class-seg", tstate.cls);
  tftActive("tft-cost-seg", tstate.cost);
  tftActive("tft-unit-seg", tstate.unit);
  markTftCold();
}

function renderTftStars() {
  // the star levels this unit is simulated at (no 3★ for 4- and 5-costs)
  const m = tstate.meta, u = m.units.find(x => x.slug === tstate.unit);
  const stars = u?.stars || m.stars;
  tstate.star = tftStarFor(u);
  tftSeg("tft-star-seg", stars.map(s => ({ key: String(s), label: `${s}★` })),
    k => { tstate.star = +k; });
  document.getElementById("tft-star-hint").textContent = stars.includes(3)
    ? "Compare one, two or three stars." : "Builds available at one and two stars.";
}

function tftStarFor(unit) {
  const stars = unit?.stars || tstate.meta.stars;
  return stars.includes(tstate.star) ? tstate.star : stars.includes(2) ? 2 : stars[0];
}

const tftLeaderboardKey = () => `${tboard.star}-clump-${tboard.traits}-${tboard.threat}`;

function tftMovementSummary(performance, range) {
  if (!Number.isFinite(range) || range > 2 || !Number.isFinite(performance?.movementTime)
      || performance.movementTime < 0) return "";
  const moves = Number.isInteger(performance.repositions) && performance.repositions >= 0
    ? ` · ${performance.repositions} ${performance.repositions === 1 ? "move" : "moves"}` : "";
  return `${performance.movementTime.toFixed(2)}s moving${moves}`;
}

function tftFiniteLeaderboardValid(data) {
  return (data.damage || []).every(entry => {
    // A pinned generation may still carry potential curves. Only its saved
    // finite diagnostic belongs in the restored clear-time leaderboard.
    if (entry.damageCurve && !entry.finiteDiagnostic) return false;
    const row = entry.finiteDiagnostic || entry, p = row.performance;
    return Array.isArray(row.items) && Array.isArray(row.itemApis) &&
      Number.isFinite(p?.total) && p.total >= 0 && Number.isFinite(p?.dps) && p.dps >= 0 &&
      (p.killTime === null || Number.isFinite(p.killTime) && p.killTime >= 0);
  });
}

function tftLeaderboardDamageRows(data) {
  const entries = data?.damage || [];
  if (!entries.some(entry => entry.finiteDiagnostic)) return entries;
  const rows = entries.map(entry => ({ ...entry, ...(entry.finiteDiagnostic || {}),
    secondBest: (entry.finiteDiagnostic || entry).secondBest }));
  const key = row => Number.isFinite(row.performance.killTime)
    ? [0, row.performance.killTime] : [1, -row.performance.total];
  rows.sort((a, b) => {
    const ka = key(a), kb = key(b);
    return ka[0] - kb[0] || ka[1] - kb[1] || a.unitName.localeCompare(b.unitName) || a.unit.localeCompare(b.unit);
  });
  let previous = null, rank = 0;
  return rows.map((row, index) => {
    const current = key(row);
    if (!previous || current.some((value, i) => value !== previous[i])) rank = index + 1;
    previous = current;
    return { ...row, rank };
  });
}

function tftLeaderboardGap(entry) {
  const second = entry.secondBest, p = second?.performance;
  const cell = document.createElement("td");
  cell.className = "tft-board-metric";
  cell.dataset.label = "2nd BIS gap";
  const labels = { killTime: "slower clear", damage: "less damage", aliveTime: "less time held",
    stressAliveTime: "less held at 2×", capped: "same limits" };
  const comparable = p?.status === "comparable" && p.metric in labels
    && Number.isFinite(p.lossPct) && p.lossPct >= 0;
  const percent = comparable ? p.lossPct === 0 ? "0.0%"
    : p.lossPct < .1 ? "<0.1%" : `${p.lossPct.toFixed(1)}%` : "—";
  const unavailable = { noClear: "Does not clear", shortSurvival: "Below regular limit",
    belowCap: "Below best limit", unavailable: "Unavailable" };
  const detail = comparable ? labels[p.metric] : unavailable[p?.status] || "Unavailable";
  cell.append(tftText("strong", percent), tftText("small", detail));
  const reasons = {
    killTime: "Percentage is extra clear time for the second build relative to BIS.",
    damage: "Neither build clears. Percentage is less damage for the second build relative to BIS.",
    aliveTime: "Percentage is less time held for the second build relative to BIS.",
    stressAliveTime: "Both builds reach the regular survival limit. Percentage is less time held at 2× pressure for the second build relative to BIS.",
    capped: "Both builds reach both survival limits: tied in this benchmark. The percentage beyond those limits is unknown.",
    noClear: "The second build does not clear. A comparable clear-time percentage is unavailable.",
    shortSurvival: "The second build falls below the regular survival limit. A comparable percentage is unavailable.",
    belowCap: "The second build falls below a survival limit reached by BIS. A comparable percentage is unavailable.",
    unavailable: "Second-best build comparison is unavailable for this saved result.",
  };
  const tank = ["aliveTime", "stressAliveTime", "capped"].includes(p?.metric);
  const outcome = performance => {
    if (!performance) return "unavailable";
    if (!tank) return Number.isFinite(performance.killTime) ? `cleared in ${performance.killTime}s`
      : Number.isFinite(performance.total) ? `did not clear (${performance.total} damage)` : "unavailable";
    const held = (time, capped) => Number.isFinite(time)
      ? capped ? `at least ${time}s (fight limit)` : `${time}s` : "unavailable";
    return `held ${held(performance.aliveTime, performance.survivalCapped)}`
      + (Number.isFinite(performance.stressAliveTime)
        ? `; ${held(performance.stressAliveTime, performance.stressCapped)} at 2× pressure` : "");
  };
  const reason = comparable ? reasons[p.metric] : reasons[p?.status] || reasons.unavailable;
  cell.title = (Array.isArray(second?.items) && second.items.length
    ? `Second-best three-item build: ${second.items.join(" · ")}.\n` : "") + reason
    + (p ? `\nBIS: ${outcome(entry.performance)}.\nSecond best: ${outcome(p)}.` : "");
  cell.setAttribute("aria-label", `2nd BIS gap: ${percent}, ${detail}. ${cell.title}`);
  return cell;
}

function renderTftMode() {
  const board = tstate.mode === "leaderboard";
  document.getElementById("view-tft").dataset.mode = tstate.mode;
  document.getElementById("tft-champion-browser").hidden = tstate.mode !== "builds";
  document.getElementById("tft-champion-panel").hidden = tstate.mode !== "builds";
  document.getElementById("tft-leaderboard-panel").hidden = !board;
  document.getElementById("tft-early-panel").hidden = tstate.mode !== "early";
  document.getElementById("tft-traits-panel").hidden = tstate.mode !== "traits";
  document.getElementById("tft-leaderboard-back").hidden = !tboard.fromLeaderboard;
  tftActive("tft-mode-seg", tstate.mode);
}

async function setTftMode(mode, { load = true, focus = false } = {}) {
  if (mode === "compositions") mode = "leaderboard";
  if (!["builds", "leaderboard", "early", "traits"].includes(mode)) return;
  if (mode !== tstate.mode) tstate.opening = null;
  if (mode !== "builds" && tstate.mode === "builds") ++tstate.req;
  if (mode !== "leaderboard" && tstate.mode === "leaderboard") {
    ++tboard.req;
    tboard.controller?.abort();
    tboard.loading = false;
  }
  if (mode !== "traits" && tstate.mode === "traits") cancelTftFlex();
  if (mode !== "early" && tstate.mode === "early") cancelTftEarly();
  tstate.mode = mode;
  hideTftTooltip();
  renderTftMode();
  updateHash(true);
  if (load && mode === "traits") await loadTftFlex();
  else if (load && mode === "early") await loadTftEarly();
  else if (load && tstate.meta) {
    if (mode === "leaderboard") await loadTftLeaderboard({ preserve: true });
    else if (!tstate.data || tstate.data.unit !== tstate.unit ||
      tstate.data.scenario.key !== tkey() || tstate.dataRevision !== tftRevision()) await loadTftScenario();
  } else if (load) await openTft();
  if (focus && tstate.mode === mode) {
    const target = mode === "leaderboard"
      ? [...document.querySelectorAll("#tft-board-table .tft-board-champion")].find(b => b.dataset.unit === tboard.returnUnit)
        || document.getElementById("tft-board-title")
      : document.getElementById(mode === "traits" ? "tft-flex-title" : mode === "early" ? "tft-early-title" : "tft-title");
    target.focus();
    target.scrollIntoView({ block: "nearest" });
  }
  scheduleTftStatus();
}

function renderTftLeaderboardControls() {
  const m = tstate.meta;
  if (!["damage", "tanks"].includes(tboard.mode)) tboard.mode = "damage";
  if (!["best", "s1", "s2", "s3"].includes(tboard.star)) tboard.star = "s2";
  tboard.geo = "clump";
  if (!["low", "high"].includes(tboard.traits)) tboard.traits = "low";
  if (!(m.tankThreats || []).some(t => t.key === tboard.threat)) tboard.threat = "mixed";
  if (!["all", "carry", "fighter"].includes(tboard.role)) tboard.role = "all";
  if (tboard.cost !== "all" && !m.units.some(u => String(u.cost) === tboard.cost)) tboard.cost = "all";
  const scenario = (field, value) => { tboard[field] = value; loadTftLeaderboard({ navigate: true }); };
  tftSeg("tft-board-mode-seg", [{ key: "damage", label: "Damage" }, { key: "tanks", label: "Tanks" }],
    k => { tboard.mode = k; renderTftLeaderboard(); updateHash(true); }, false);
  tftSeg("tft-board-star-seg", [{ key: "best", label: "Best available" },
    ...[1, 2, 3].map(s => ({ key: `s${s}`, label: `${s}★` }))], k => scenario("star", k), false);
  tftSeg("tft-board-trait-seg", ["low", "high"].filter(key => key in m.traitContexts).map(key => ({
    key, label: { low: "First breakpoint", high: "Max non-prismatic" }[key], title: m.traitContexts[key] })),
    k => scenario("traits", k), false);
  tftSeg("tft-board-threat-seg", (m.tankThreats || []).map(t => ({ key: t.key, label: t.label, title: t.description })),
    k => scenario("threat", k), false);
  tftSeg("tft-board-role-seg", [{ key: "all", label: "All damage roles" },
    { key: "carry", label: "Carries", title: "The targets do not hit back." },
    { key: "fighter", label: "Fighters", title: "Fighters and assassins take attacks and casts from their targets." }],
    k => { tboard.role = k; renderTftLeaderboard(); updateHash(true); }, false);
  const cost = document.getElementById("tft-board-cost");
  cost.replaceChildren(...[{ key: "all", label: "All costs" },
    ...[...new Set(m.units.map(u => u.cost))].sort((a, b) => a - b).map(c => ({ key: String(c), label: `${c} cost` }))]
    .map(o => { const option = tftText("option", o.label); option.value = o.key; return option; }));
  cost.value = tboard.cost;
  document.getElementById("tft-board-search").value = tboard.search;
  if (tboard.data?.revision !== tftRevision() || tboard.data?.selection?.key !== tftLeaderboardKey()) tboard.data = null;
  renderTftMode();
  renderTftLeaderboard();
}

function renderTftLeaderboard() {
  const tank = tboard.mode === "tanks", d = tboard.data;
  const focusedUnit = document.activeElement?.matches("#tft-board-table .tft-board-champion")
    ? document.activeElement.dataset.unit : null;
  hideTftTooltip();
  const query = tboard.search.trim().toLocaleLowerCase();
  const allRows = tank ? d?.tanks || [] : tftLeaderboardDamageRows(d);
  const units = new Map((tstate.meta?.units || []).map(u => [u.slug, u]));
  const rows = allRows.filter(r => (tank || tboard.role === "all" || r.objective === tboard.role) &&
    (tboard.cost === "all" || String(r.cost) === tboard.cost) && (!query ||
      [r.unitName, ...(units.get(r.unit)?.traits || [])].some(v => v.toLocaleLowerCase().includes(query))));
  tftActive("tft-board-mode-seg", tboard.mode);
  tftActive("tft-board-star-seg", tboard.star);
  tftActive("tft-board-trait-seg", tboard.traits);
  tftActive("tft-board-threat-seg", tboard.threat);
  tftActive("tft-board-role-seg", tboard.role);
  document.getElementById("tft-board-threat-setting").hidden = !tank;
  document.getElementById("tft-board-role-seg").hidden = tank;
  document.getElementById("tft-board-star-hint").textContent = tboard.star === "best"
    ? "3★ for 1–3 costs · 2★ for 4–5 costs."
    : tboard.star === "s3" ? "Only 1–3 cost champions are included at 3★." : `Every champion at ${tboard.star.slice(1)}★.`;
  const enemyFormation = tftEnemyFormation(tstate.meta?.dummy, "clump");
  const formation = document.getElementById("tft-board-formation");
  formation.textContent = enemyFormation.slots.length
    ? `Clumped · ${enemyFormation.frontline.length} frontliners + ${enemyFormation.backline.length} backliners (${enemyFormation.slots.length} enemies)`
    : "Clumped";
  formation.title = "Frontliners occupy neighboring lanes. Each spell’s radius determines which enemies it hits.";
  const traits = { low: "First trait breakpoints", high: "Highest non-prismatic trait breakpoints" }[tboard.traits];
  const threat = tstate.meta?.tankThreats?.find(t => t.key === tboard.threat);
  document.getElementById("tft-board-context").textContent =
    `Clumped enemies · ${traits}. ` +
    (tank ? `${threat?.label || "Mixed"} damage; ranked by time held.`
      : tboard.role === "carry" ? "Protected carries; ranked by clear time, then damage."
      : tboard.role === "fighter" ? "Fighters take incoming damage; ranked by clear time, then damage."
      : "Carries are protected; fighters take incoming damage. Ranked by clear time, then damage.");
  const movementDelays = tank ? [] : [...new Set(rows.filter(row => Number.isFinite(row.range) && row.range <= 2
    && Number.isFinite(row.meleeRepositionSeconds) && row.meleeRepositionSeconds > 0)
    .map(row => row.meleeRepositionSeconds))];
  const movementNote = document.getElementById("tft-board-movement-note");
  movementNote.hidden = !movementDelays.length;
  movementNote.textContent = movementDelays.length
    ? `Builds with 1–2 attack range use an approximate ${movementDelays.map(delay => `${delay}s`).join(" / ")} delay at initial engagement and after their current target dies. Attack cooldowns overlap movement; ongoing damage and incoming pressure continue. Hex paths are not simulated.` : "";
  const filtered = !!query || tboard.cost !== "all" || (!tank && tboard.role !== "all");
  document.getElementById("tft-board-clear").hidden = !filtered;
  const label = tank ? "tanks" : "damage champions";
  let status;
  if (tboard.loading) status = d ? "Updating the leaderboard…" : "Loading the leaderboard…";
  else if (tboard.error) status = tboard.error + (d ? " Showing the last loaded results." : "");
  else if (!d) status = "Choose a leaderboard to compare champions.";
  else {
    status = `${rows.length}${filtered ? ` of ${allRows.length}` : ""} ${label} · each with its best three-item build.`;
    if (!d.complete) status += ` Calculating: ${d.readyCount} of ${d.expectedCount} champions ready. Ranks are provisional.`;
    else if (filtered) status += " Ranks are from the full board.";
  }
  document.getElementById("tft-board-status").textContent = status;
  document.getElementById("tft-leaderboard-panel").setAttribute("aria-busy", String(tboard.loading));
  const retry = document.getElementById("tft-board-retry");
  retry.hidden = !tboard.error && (!d || d.complete);
  retry.disabled = tboard.loading;
  retry.textContent = tboard.error ? "Retry" : "Refresh";
  const stress = tank && allRows.some(r => r.performance.stressAliveTime != null);
  const headings = ["#", "Champion", "Optimal items", "2nd BIS gap", tank ? "Held" : "Clear time", ...(tank ? stress ? ["2× pressure"] : [] : ["DPS"])];
  document.querySelector("#tft-board-table thead tr").replaceChildren(...headings.map(label => {
    const th = tftText("th", label); th.scope = "col";
    if (label === "2nd BIS gap") th.title = "Performance lost when using this champion’s second-best distinct three-item build instead of BIS.";
    return th;
  }));
  const number = value => Number.isFinite(value) ? Math.round(value).toLocaleString("en-US") : "—";
  document.querySelector("#tft-board-table tbody").replaceChildren(...rows.map(r => {
    const row = document.createElement("tr"), unit = units.get(r.unit), p = r.performance;
    row.dataset.unit = r.unit;
    row.dataset.star = r.star;
    row.dataset.rank = r.rank;
    row.dataset.scenario = r.scenario;
    row.dataset.cost = r.cost;
    const champion = document.createElement("button");
    champion.className = "tft-board-champion";
    champion.dataset.unit = r.unit;
    const unitTraits = unit ? tftUnitTraits(unit) : [];
    champion.setAttribute("aria-label", `${r.unitName}, ${r.star} stars, ${r.cost} cost.`
      + (unitTraits.length ? ` Traits: ${unitTraits.map(trait => trait.name).join(", ")}.` : "")
      + " Open this champion’s builds.");
    const text = document.createElement("span");
    text.className = "tft-board-champion-text";
    text.append(tftText("span", r.unitName, "tft-unit-name"),
      tftText("span", `${r.star}★ · ${r.cost} cost · ${r.form
        ? `${r.form} form · ${r.role || r.objective}${Number.isFinite(r.range) ? ` · ${r.range} range` : ""}`
        : r.objective === "fighter" ? "Fighter" : tank ? "Tank" : "Carry"}`, "tft-board-unit-meta"));
    champion.append(tftIcon(unit?.icon, r.unitName), text);
    if (unit) bindTftTooltip(champion, () => tftChampionTooltip(unit, r));
    champion.addEventListener("click", () => openTftLeaderboardEntry(r));
    const championCell = document.createElement("td"), items = document.createElement("td");
    championCell.append(champion);
    if (unitTraits.length) {
      const traits = document.createElement("div");
      traits.className = "tft-board-traits";
      traits.append(...unitTraits.map(trait => {
        const status = tftTraitStatus(unit, trait, tboard.traits);
        const label = document.createElement("button");
        label.type = "button";
        label.className = "tft-board-trait tft-tier";
        label.dataset.trait = trait.api;
        label.dataset.tier = status.tier.toLowerCase();
        label.setAttribute("aria-label", `${trait.name}, ${status.label}. Show trait details.`);
        label.append(tftIcon(trait.icon, trait.name, true),
          tftText("span", `${status.count ? `${status.count} ` : ""}${trait.name}`),
          tftText("small", status.tier || status.label, "tft-board-trait-tier"));
        bindTftTooltip(label, () => tftTraitTooltip(unit, trait, tboard.traits), true);
        return label;
      }));
      championCell.append(traits);
    }
    items.append(tftCoreItems(r.items, r.itemApis));
    const metric = (label, value, detail = "", exactTime = null) => {
      const td = document.createElement("td"); td.className = "tft-board-metric"; td.dataset.label = label;
      td.append(tftText("strong", value));
      if (detail) td.append(tftText("small", detail));
      if (Number.isFinite(exactTime)) td.title = `Unrounded time: ${exactTime}s`;
      return td;
    };
    row.append(tftText("td", String(r.rank)), championCell, items, tftLeaderboardGap(r));
    if (tank) {
      row.append(metric("Held", tftHeldTime(p.aliveTime, p.survivalCapped, 60), "", p.survivalCapped ? null : p.aliveTime));
      if (stress) row.append(metric("2× pressure", tftHeldTime(p.stressAliveTime, p.stressCapped, 60), "", p.stressCapped ? null : p.stressAliveTime));
    } else {
      row.append(metric("Clear time", Number.isFinite(p.killTime) ? `${p.killTime.toFixed(2)}s` : "Did not clear",
        p.killTime == null ? `${number(p.total)} damage` : "", p.killTime), metric("DPS", number(p.dps),
        Number.isFinite(r.meleeRepositionSeconds) && r.meleeRepositionSeconds > 0 ? tftMovementSummary(p, r.range) : ""));
    }
    return row;
  }));
  const wrap = document.getElementById("tft-board-tablewrap");
  wrap.hidden = !rows.length && !tboard.loading;
  if (!tboard.loading) wrap.style.minHeight = "";
  if (focusedUnit && tstate.mode === "leaderboard") {
    const focus = [...document.querySelectorAll("#tft-board-table .tft-board-champion")].find(b => b.dataset.unit === focusedUnit);
    (focus || document.getElementById("tft-board-title")).focus({ preventScroll: true });
  }
  const empty = document.getElementById("tft-board-empty");
  empty.hidden = rows.length > 0 || tboard.loading || !!tboard.error || !d;
  empty.textContent = filtered ? d?.complete
    ? "No champions match these filters. Try another name or clear the filters."
    : "No ready champions match these filters yet. Results update as fights finish."
    : d?.complete ? "No champions are available for these settings." : "Results will appear as these fights finish calculating.";
}

async function loadTftLeaderboard({ preserve = false, navigate = false } = {}) {
  if (!tstate.meta) return;
  tboard.geo = "clump";
  const key = tftLeaderboardKey(), req = ++tboard.req;
  tboard.controller?.abort();
  const controller = new AbortController();
  tboard.controller = controller;
  // Keep the page from shrinking while the previous rows are replaced.
  const wrap = document.getElementById("tft-board-tablewrap");
  const height = wrap.hidden ? 0 : wrap.getBoundingClientRect().height;
  wrap.style.minHeight = height ? `${height}px` : "";
  if (!preserve || tboard.key !== key || tboard.data?.revision !== tftRevision()
      || tboard.data?.selection?.key !== key) tboard.data = null;
  tboard.key = key;
  tboard.loading = true;
  tboard.pending = false;
  tboard.error = null;
  renderTftLeaderboard();
  updateHash(navigate);
  try {
    const d = await api(`api/tft/leaderboard/${key}.json`, { cache: "no-store", signal: controller.signal });
    if (req !== tboard.req || key !== tftLeaderboardKey()) return;
    if (d.revision !== tftRevision()) {
      tboard.data = null;
      if (!await loadTftMeta(true)) throw new Error("Could not refresh leaderboard metadata");
      if (req !== tboard.req || key !== tftLeaderboardKey()) {
        if (tstate.mode === "leaderboard" && !tboard.loading && !tboard.data) await loadTftLeaderboard();
        return;
      }
      if (d.revision !== tftRevision()) throw new Error("Revision changed during leaderboard request");
    }
    if (d.selection?.key !== key || !Array.isArray(d.damage) || !Array.isArray(d.tanks) || !tftFiniteLeaderboardValid(d))
      throw new Error("Unexpected leaderboard response");
    tboard.data = d;
    tboard.pending = !d.complete;
  } catch (error) {
    if (req !== tboard.req || error.name === "AbortError") return;
    tboard.error = "The leaderboard could not be loaded. Try again shortly.";
  } finally {
    if (req === tboard.req) {
      tboard.loading = false;
      renderTftLeaderboard();
      scheduleTftStatus();
    }
  }
}

function tftCompositionTeamCode(composition, planner) {
  if (planner?.format !== "tft-team-planner-v2" || planner.slots !== 10 ||
      !Number.isInteger(planner.set) || planner.set < 1)
    throw new Error("In-game copying is unavailable for this set.");
  const units = composition?.units;
  if (!Array.isArray(units) || !units.length || units.length > planner.slots ||
      units.some(unit => typeof unit?.api !== "string") ||
      new Set(units.map(unit => unit.api)).size !== units.length)
    throw new Error("This board cannot be represented by a TFT Team Planner code.");
  const codes = units.map(unit => planner.unitCodes?.[unit.api]);
  if (codes.some(code => typeof code !== "string" || !/^[0-9a-f]{3}$/.test(code) || code === "000") ||
      new Set(codes).size !== codes.length)
    throw new Error("An in-game champion ID is unavailable for this board.");
  // Planner slots are champions, not occupied team slots: Elder appears once.
  // V2 contains roster IDs only, without items, stars, positions or Alpha marks.
  return `02${codes.join("").padEnd(30, "0")}TFTSet${planner.set}`;
}

async function tftCopyTeamCode(code) {
  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(code);
      return;
    } catch (_) { /* Local HTTP or browser permissions may require the fallback. */ }
  }
  const previous = document.activeElement;
  const field = document.createElement("textarea");
  field.value = code;
  field.readOnly = true;
  field.setAttribute("aria-label", "TFT Team Planner code");
  field.style.position = "fixed";
  field.style.opacity = "0";
  field.style.top = "0";
  field.style.left = "0";
  document.body.append(field);
  try {
    field.focus({ preventScroll: true });
    field.select();
    field.setSelectionRange(0, code.length);
    if (!document.execCommand?.("copy")) throw new Error("Clipboard copying was blocked.");
  } finally {
    field.remove();
    previous?.focus({ preventScroll: true });
  }
}

function tftCompositionCopyButton(composition, planner = tflex.catalog?.teamPlanner || tflex.data?.teamPlanner || tearly.data?.teamPlanner || null) {
  const control = document.createElement("div");
  control.className = "tft-comp-copy";
  const button = tftText("button", "Copy to TFT");
  button.type = "button";
  const carry = composition.units.find(unit => unit.slug === composition.mainCarry);
  const tank = composition.units.find(unit => unit.slug === composition.mainTank);
  const names = composition.cores?.length
    ? composition.cores.map(slug => composition.units.find(unit => unit.slug === slug)?.name || slug).join(" + ")
    : `${carry?.name || composition.mainCarry} and ${tank?.name || composition.mainTank}`;
  button.setAttribute("aria-label", `Copy level ${composition.level || 8} board for ${names} to TFT Team Planner`);
  button.title = "Paste into TFT’s Team Planner. Copies champions; items, stars, positions and Alpha marks are not included.";
  const status = tftText("span", "", "tft-comp-copy-status");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  control.append(button, status);
  let code;
  try {
    code = tftCompositionTeamCode(composition, planner);
  } catch (error) {
    button.disabled = true;
    button.title = error.message;
    return control;
  }
  let manual;
  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true;
    status.textContent = "";
    try {
      await tftCopyTeamCode(code);
      status.textContent = "Copied! Paste in Team Planner.";
      manual?.remove();
      manual = null;
    } catch (_) {
      status.textContent = "Copy blocked. Copy the selected code manually.";
      if (!manual) {
        manual = document.createElement("input");
        manual.type = "text";
        manual.className = "tft-comp-copy-code";
        manual.readOnly = true;
        manual.value = code;
        manual.setAttribute("aria-label", "TFT Team Planner code to copy manually");
        control.append(manual);
      }
      manual.focus({ preventScroll: true });
      manual.select();
    } finally {
      button.disabled = false;
    }
  });
  return control;
}

function openTftLeaderboardEntry(entry) {
  const unit = tstate.meta?.units.find(u => u.slug === entry.unit);
  if (!unit || !(unit.stars || tstate.meta.stars).includes(entry.star)) return;
  if (tboard.data?.revision !== tftRevision()) { loadTftLeaderboard(); return; }
  const [, geo, traits, threat] = entry.scenario.split("-");
  tboard.fromLeaderboard = true;
  tboard.returnUnit = entry.unit;
  tstate.unit = entry.unit;
  tstate.star = entry.star;
  tstate.geo = geo;
  tstate.traits = traits;
  tstate.threat = threat || "mixed";
  tstate.sel = 0;
  tstate.cost = String(unit.cost);
  renderTftUnits();
  renderTftStars();
  setTftMode("builds", { load: false });
  loadTftScenario();
  document.getElementById("tft-title").focus();
  document.getElementById("tft-results").scrollIntoView({ block: "start" });
}

document.querySelectorAll("#tft-mode-seg button").forEach(button =>
  button.addEventListener("click", () => setTftMode(button.dataset.key)));
document.getElementById("tft-leaderboard-back").addEventListener("click", () => setTftMode("leaderboard", { focus: true }));
document.getElementById("tft-board-retry").addEventListener("click", () => loadTftLeaderboard({ preserve: true }));
document.getElementById("tft-board-cost").addEventListener("change", e => {
  tboard.cost = e.target.value; renderTftLeaderboard(); updateHash(true);
});
document.getElementById("tft-board-search").addEventListener("input", e => {
  tboard.search = e.target.value; renderTftLeaderboard(); updateHash();
});
document.getElementById("tft-board-clear").addEventListener("click", () => {
  tboard.cost = tboard.role = "all";
  tboard.search = "";
  document.getElementById("tft-board-cost").value = "all";
  document.getElementById("tft-board-search").value = "";
  renderTftLeaderboard();
  updateHash(true);
  document.getElementById("tft-board-search").focus();
});

document.getElementById("tft-search").addEventListener("input", e => {
  tstate.search = e.target.value;
  renderTftUnits();
  updateHash();
});
document.getElementById("tft-clear-filters").addEventListener("click", () => {
  tstate.cls = "all";
  tstate.search = "";
  document.getElementById("tft-search").value = "";
  renderTftUnits();
  updateHash(true);
  document.getElementById("tft-search").focus();
});
document.getElementById("tft-show-stats").addEventListener("change", () => {
  if (tstate.data) renderTftTable();
});
document.getElementById("tft-jump-to-builds").addEventListener("click", () => {
  document.getElementById("tft-title").focus();
  document.getElementById("tft-results").scrollIntoView({ block: "start" });
});

// ---------------- trait flex: unit search ----------------
// This catalog and search deliberately use only units, occupancy and trait thresholds.
// They neither read item/star assumptions nor call any combat endpoint.
function tftFlexCatalog(raw) {
  if (raw?.schema !== "tft-trait-flex-v1" || !raw.revision || raw.level !== 8 || raw.boardSlots !== 8 ||
      !Array.isArray(raw.units) || !raw.units.length || !Array.isArray(raw.traits))
    throw new Error("Unexpected trait catalog. Try again after the next TFT publication.");
  const traits = raw.traits.map(trait => {
    if (typeof trait?.api !== "string" || !trait.api || typeof trait.name !== "string" || !Array.isArray(trait.levels))
      throw new Error("Unexpected trait definitions.");
    const levels = [...new Set(trait.levels.filter(level => Number.isInteger(level) && level > 0))].sort((a, b) => a - b);
    return { api: trait.api, name: trait.name, icon: trait.icon, levels, sharedLevels: levels.filter(level => level >= 2) };
  }).sort((a, b) => a.api.localeCompare(b.api));
  if (new Set(traits.map(trait => trait.api)).size !== traits.length) throw new Error("Duplicate trait definitions.");
  const traitsByApi = new Map(traits.map((trait, index) => [trait.api, { ...trait, index }]));
  const units = raw.units.map(unit => {
    if (typeof unit?.api !== "string" || !unit.api || typeof unit.slug !== "string" || !unit.slug ||
        typeof unit.name !== "string" || !Number.isInteger(unit.cost) || unit.cost < 1 || unit.cost > 5 ||
        !Number.isInteger(unit.slotCost) || unit.slotCost < 1 || unit.slotCost > 2 || !Array.isArray(unit.traitApis))
      throw new Error("Unexpected unit definitions.");
    const traitApis = [...new Set(unit.traitApis)].sort();
    const traitContributions = {}, contributions = [];
    for (const api of traitApis) {
      const trait = traitsByApi.get(api), count = unit.traitContributions?.[api] ?? 1;
      if (!trait || !Number.isInteger(count) || count < 1 || count > 8) throw new Error("Unexpected unit trait contributions.");
      traitContributions[api] = count;
      contributions.push([trait.index, count]);
    }
    return { api: unit.api, slug: unit.slug, name: unit.name, cost: unit.cost, icon: unit.icon,
      role: unit.role || unit.kind || unit.objective || "", kind: unit.kind, objective: unit.objective, range: unit.range,
      frontline: unit.frontline === true, backline: unit.backline === true, slotCost: unit.slotCost,
      traitApis, traitContributions, contributions };
  }).sort((a, b) => a.api.localeCompare(b.api));
  if (new Set(units.map(unit => unit.api)).size !== units.length || new Set(units.map(unit => unit.slug)).size !== units.length)
    throw new Error("Duplicate units in the trait catalog.");
  return { schema: raw.schema, revision: raw.revision, publicationRevision: raw.publicationRevision,
    set: raw.set, patch: raw.patch, level: 8, boardSlots: 8, units, traits, traitsByApi,
    unitsBySlug: new Map(units.map(unit => [unit.slug, unit])), teamPlanner: raw.teamPlanner,
    shop: raw.shop ? JSON.parse(JSON.stringify(raw.shop)) : null,
    lobbyStats: raw.lobbyStats ? JSON.parse(JSON.stringify(raw.lobbyStats)) : null };
}

function tftFlexCoreCost(value) {
  const cost = Number(value);
  return [2, 3, 4].includes(cost) ? cost : 4;
}

function tftFlexFourCostLimit(cost) {
  return cost === 2 ? 2 : cost === 3 ? 3 : 8;
}

function tftFlexGoldCoreCost(board) {
  const firstCore = board.cores?.[0] || board.mainTank || board.mainCarry;
  return tftFlexCoreCost(board.coreCost ?? (firstCore ? board.units.find(unit => unit.slug === firstCore)?.cost : undefined));
}

function tftFlexCopyCount(unitCost, coreCost = 4) {
  return coreCost === 2 || coreCost === 3 ? unitCost === coreCost ? 9 : 1 : unitCost === 4 ? 3 : 1;
}

function tftFlexOptions(catalog, options = {}) {
  const cost = tftFlexCoreCost(options.cost), maxFourCosts = tftFlexFourCostLimit(cost);
  const explicitCores = options.cores !== undefined;
  let selected;
  if (explicitCores) {
    if (!Array.isArray(options.cores)) throw new Error(`Choose at least two distinct ${cost}-cost core champions.`);
    const cores = [...new Set(options.cores)].sort();
    if (cores.length < 2) throw new Error(`Choose at least two distinct ${cost}-cost core champions.`);
    for (const slug of cores) {
      const unit = catalog.unitsBySlug.get(slug);
      if (!unit) throw new Error("A selected core is no longer in this set. Choose your core champions again.");
      if (unit.cost !== cost) throw new Error(`All core champions must cost ${cost}.`);
    }
    selected = { cores };
  } else {
    const tank = catalog.unitsBySlug.get(options.tank), carry = catalog.unitsBySlug.get(options.carry);
    if (tank?.cost !== cost || !tank.frontline) throw new Error(`Choose a ${cost}-cost frontline anchor.`);
    if (carry?.cost !== cost || !carry.backline) throw new Error(`Choose a ${cost}-cost backline carry.`);
    if (tank.slug === carry.slug) throw new Error("Choose two distinct anchors.");
    selected = { tank: tank.slug, carry: carry.slug };
  }
  const requestedMinCost = Math.max(1, Math.floor(Number(options.minCost) || 1));
  const minCost = Math.min(3, requestedMinCost);
  if (cost < 4 && requestedMinCost > cost) throw new Error(`Lower the minimum unit cost to include your ${cost}-cost core units.`);
  const locked = [...new Set(options.locked || [])].sort();
  for (const slug of locked) {
    const unit = catalog.unitsBySlug.get(slug);
    if (!unit) throw new Error("A kept unit is no longer in this set. Choose your units again.");
    if (unit.cost < minCost) throw new Error(`Unlock ${unit.name} or lower the minimum unit cost.`);
    if (unit.cost === 5 && options.allowFive !== true) throw new Error(`Enable 5-cost supports or unlock ${unit.name}.`);
  }
  if (cost < 4 && locked.filter(slug => catalog.unitsBySlug.get(slug).cost === 4).length > maxFourCosts)
    throw new Error(`${cost}-cost comps allow at most ${maxFourCosts} 4-cost units. Unlock a 4-cost unit.`);
  return { ...options, cost, maxFourCosts, ...selected, locked, minCost,
    preference: options.preference === "vertical" ? "vertical" : "breadth",
    minFrontline: Math.min(8, Math.max(1, Math.floor(Number(options.minFrontline) || 3))), allowFive: options.allowFive === true };
}

function tftFlexMeasure(catalog, counts, preference) {
  const activeTraits = [];
  let extraTiers = 0;
  catalog.traits.forEach((trait, index) => {
    const reached = trait.levels.filter(level => level <= counts[index]);
    if (!reached.length) return;
    extraTiers += reached.length - 1;
    activeTraits.push({ ...trait, count: counts[index], breakpoint: reached[reached.length - 1], tiers: reached.length });
  });
  const activeCount = activeTraits.length;
  return { activeTraits, activeCount, extraTiers,
    score: preference === "vertical" ? activeCount + 3 * extraTiers : 3 * activeCount + extraTiers };
}

function tftFlexCounts(catalog, units) {
  const counts = new Int16Array(catalog.traits.length);
  for (const unit of units) for (const [index, contribution] of unit.contributions) counts[index] += contribution;
  return counts;
}

function tftFlexEvaluate(catalog, roster, options) {
  const settings = tftFlexOptions(catalog, options);
  const units = roster.map(unit => catalog.unitsBySlug.get(typeof unit === "string" ? unit : unit.slug));
  if (units.some(unit => !unit)) throw new Error("A board contains an unknown unit.");
  const counts = tftFlexCounts(catalog, units), measured = tftFlexMeasure(catalog, counts, settings.preference);
  const slugs = new Set(units.map(unit => unit.slug));
  const slotsUsed = units.reduce((sum, unit) => sum + unit.slotCost, 0), frontlineCount = units.filter(unit => unit.frontline).length;
  const fourCostCount = units.filter(unit => unit.cost === 4).length;
  const cores = settings.cores || [settings.tank, settings.carry], coreSet = new Set(cores);
  const coreCoverage = Object.fromEntries(cores.map(slug => [slug,
    measured.activeTraits.filter(trait => catalog.unitsBySlug.get(slug).traitApis.includes(trait.api))]));
  const valid = slugs.size === units.length && slotsUsed === 8 &&
    cores.every(slug => slugs.has(slug) && coreCoverage[slug].length > 0) && settings.locked.every(slug => slugs.has(slug)) &&
    units.every(unit => unit.cost >= settings.minCost) &&
    (settings.allowFive || units.every(unit => unit.cost <= 4)) && fourCostCount <= settings.maxFourCosts && frontlineCount >= settings.minFrontline;
  const ordered = [...units].sort((a, b) => {
    const order = settings.cores
      ? unit => coreSet.has(unit.slug) ? 0 : unit.frontline ? 1 : 2
      : unit => unit.slug === settings.tank ? 0 : unit.slug === settings.carry ? 1 : unit.frontline ? 2 : 3;
    return order(a) - order(b) || b.cost - a.cost || a.name.localeCompare(b.name) || a.api.localeCompare(b.api);
  });
  const legacy = settings.cores ? {} : { mainTank: settings.tank, mainCarry: settings.carry,
    anchorCoverage: { tank: coreCoverage[settings.tank], carry: coreCoverage[settings.carry] } };
  return { id: [...slugs].sort().join("|"), level: 8, coreCost: settings.cost, units: ordered, cores: [...cores].sort(), coreCoverage,
    slotsUsed, frontlineCount, ...legacy, ...measured, valid };
}

const tftFlexYield = () => new Promise(resolve => setTimeout(resolve, 0));
function tftFlexCompare(a, b) {
  return b.score - a.score || b.activeCount - a.activeCount || b.extraTiers - a.extraTiers || a.id.localeCompare(b.id);
}

function tftFlexDiverse(boards, options, limit) {
  const fixed = new Set([...(options.cores || [options.tank, options.carry]), ...options.locked]);
  const supports = board => new Set(board.units.filter(unit => !fixed.has(unit.slug)).map(unit => unit.slug));
  const selected = [], packages = [];
  for (const board of [...boards].sort(tftFlexCompare)) {
    const support = supports(board);
    if (packages.some(previous => {
      const overlap = [...support].filter(slug => previous.has(slug)).length;
      return overlap > Math.min(previous.size, support.size) - Math.min(2, previous.size, support.size);
    })) continue;
    selected.push(board); packages.push(support);
    if (selected.length === limit) break;
  }
  return selected;
}

async function tftFlexSearch(catalog, requested = {}) {
  const options = tftFlexOptions(catalog, requested), cores = options.cores || [options.tank, options.carry];
  const fixed = new Set([...cores, ...options.locked]);
  const seed = [...fixed].map(slug => catalog.unitsBySlug.get(slug));
  const seedSlots = seed.reduce((sum, unit) => sum + unit.slotCost, 0);
  if (seedSlots > 8) throw new Error("The core champions and kept units use more than 8 team slots. Remove a core or unlock a unit.");
  const seedFourCosts = seed.filter(unit => unit.cost === 4).length;
  const pool = catalog.units.filter(unit => !fixed.has(unit.slug) && unit.cost >= options.minCost && (options.allowFive || unit.cost <= 4)
    && (unit.cost !== 4 || seedFourCosts < options.maxFourCosts));
  const beamWidth = Math.min(800, Math.max(1, Math.floor(requested.beamWidth || 360)));
  const maxExpansions = Math.min(300000, Math.max(1, Math.floor(requested.maxExpansions || 160000)));
  const limit = Math.min(12, Math.max(1, Math.floor(requested.limit || 6)));
  const yieldEvery = Math.min(1500, Math.max(1, Math.floor(requested.yieldEvery || 600)));
  const yieldTask = requested.yieldTask || tftFlexYield, cancelled = requested.cancelled || (() => false);
  const anchors = cores.map(slug => catalog.unitsBySlug.get(slug));
  // Suffix bounds only prune impossible continuations; unfinished breakpoints never add score.
  const suffix = Array.from({ length: pool.length + 1 }, () => new Int16Array(catalog.traits.length));
  const suffixSlots = new Int16Array(pool.length + 1), suffixFrontline = new Int16Array(pool.length + 1);
  for (let index = pool.length - 1; index >= 0; --index) {
    suffix[index].set(suffix[index + 1]);
    for (const [trait, contribution] of pool[index].contributions) suffix[index][trait] += contribution;
    suffixSlots[index] = suffixSlots[index + 1] + pool[index].slotCost;
    suffixFrontline[index] = suffixFrontline[index + 1] + Number(pool[index].frontline);
  }
  let expanded = 0, truncated = false;
  const decorate = row => {
    const measured = tftFlexMeasure(catalog, row.counts, options.preference);
    row.score = measured.score; row.activeCount = measured.activeCount; row.extraTiers = measured.extraTiers;
    row.covered = anchors.filter(anchor => measured.activeTraits.some(trait => anchor.traitApis.includes(trait.api))).length;
    row.id = row.units.map(unit => unit.slug).sort().join("|");
    return row;
  };
  const compare = (a, b) => b.score - a.score || b.covered - a.covered ||
    Math.min(options.minFrontline, b.frontlineCount) - Math.min(options.minFrontline, a.frontlineCount) ||
    b.activeCount - a.activeCount || b.extraTiers - a.extraTiers || a.id.localeCompare(b.id);
  const trim = (rows, width) => {
    if (rows.length <= width) return rows;
    truncated = true;
    const profiles = new Map(), selected = [];
    for (const row of rows.sort(compare)) {
      const profile = `${row.counts.join(",")};${Math.min(options.minFrontline, row.frontlineCount)}` +
        (options.cost < 4 ? `;${row.fourCostCount}` : "");
      const seen = profiles.get(profile) || 0;
      if (seen >= 3) continue;
      selected.push(row); profiles.set(profile, seen + 1);
      if (selected.length === width) break;
    }
    return selected;
  };
  const possible = row => {
    const remaining = 8 - row.slotsUsed, next = row.last + 1;
    if (suffixSlots[next] < remaining || row.frontlineCount + Math.min(remaining, suffixFrontline[next]) < options.minFrontline) return false;
    return anchors.every(anchor => anchor.traitApis.some(api => {
      const trait = catalog.traitsByApi.get(api);
      return trait.levels.length && row.counts[trait.index] + suffix[next][trait.index] >= trait.levels[0];
    }));
  };
  const buckets = Array.from({ length: 9 }, () => []);
  buckets[seedSlots].push(decorate({ units: seed, counts: tftFlexCounts(catalog, seed), slotsUsed: seedSlots,
    frontlineCount: seed.filter(unit => unit.frontline).length, fourCostCount: seedFourCosts, last: -1 }));
  await yieldTask();
  for (let slots = seedSlots; slots < 8; ++slots) {
    if (cancelled()) return { boards: [], expanded, truncated, cancelled: true };
    const frontier = trim(buckets[slots], beamWidth);
    buckets[slots] = [];
    for (const previous of frontier) {
      if (!possible(previous)) continue;
      for (let index = previous.last + 1; index < pool.length; ++index) {
        const unit = pool[index], occupied = slots + unit.slotCost;
        if (occupied > 8) continue;
        const fourCostCount = previous.fourCostCount + Number(unit.cost === 4);
        if (fourCostCount > options.maxFourCosts) continue;
        if (expanded >= maxExpansions) { truncated = true; break; }
        ++expanded;
        const counts = new Int16Array(previous.counts);
        for (const [trait, contribution] of unit.contributions) counts[trait] += contribution;
        const row = { units: [...previous.units, unit], counts, slotsUsed: occupied,
          frontlineCount: previous.frontlineCount + Number(unit.frontline), fourCostCount, last: index };
        if (possible(row)) buckets[occupied].push(decorate(row));
        if (buckets[occupied].length > beamWidth * 8) buckets[occupied] = trim(buckets[occupied], beamWidth * 2);
        if (expanded % yieldEvery === 0) {
          await yieldTask();
          if (cancelled()) return { boards: [], expanded, truncated, cancelled: true };
        }
      }
      if (expanded >= maxExpansions) break;
    }
    if (expanded >= maxExpansions) break;
  }
  if (cancelled()) return { boards: [], expanded, truncated, cancelled: true };
  const boards = buckets[8].map(row => tftFlexEvaluate(catalog, row.units, options)).filter(board => board.valid);
  return { boards: tftFlexDiverse(boards, options, limit), expanded, truncated, cancelled: false };
}

async function tftFlexBrowse(catalog, requested = {}) {
  const cost = tftFlexCoreCost(requested.cost), explicitCores = requested.cores !== undefined;
  let pairs;
  if (explicitCores) {
    if (!Array.isArray(requested.cores)) throw new Error("Choose core champions as a list.");
    const cores = [...new Set(requested.cores)].sort();
    if (cores.length >= 2) {
      const result = await tftFlexSearch(catalog, { ...requested, cores });
      if (!result.cancelled) requested.onProgress?.({ expanded: result.expanded, boardsFound: result.boards.length });
      if (requested.cancelled?.()) return { ...result, boards: [], cancelled: true };
      return result;
    }
    for (const slug of cores) {
      const unit = catalog.unitsBySlug.get(slug);
      if (!unit) throw new Error("A selected core is no longer in this set. Choose your core champions again.");
      if (unit.cost !== cost) throw new Error(`All core champions must cost ${cost}.`);
    }
    const pool = catalog.units.filter(unit => unit.cost === cost).sort((a, b) => a.slug.localeCompare(b.slug));
    pairs = cores.length ? pool.filter(unit => unit.slug !== cores[0])
      .map(unit => ({ cores: [cores[0], unit.slug].sort() }))
      : pool.flatMap((unit, index) => pool.slice(index + 1).map(other => ({ cores: [unit.slug, other.slug] })));
    if (!pairs.length) throw new Error(`This cost has fewer than two distinct ${cost}-cost core champions.`);
  } else {
    const choose = (key, role) => {
      const pool = catalog.units.filter(unit => unit.cost === cost && unit[role]);
      if (!requested[key] || requested[key] === "all") return pool;
      const selected = pool.filter(unit => unit.slug === requested[key]);
      if (!selected.length) throw new Error(key === "tank" ? `Choose a ${cost}-cost frontline anchor.` : `Choose a ${cost}-cost backline carry.`);
      return selected;
    };
    const tanks = choose("tank", "frontline"), carries = choose("carry", "backline");
    pairs = tanks.flatMap(tank => carries.filter(carry => carry.slug !== tank.slug).map(carry => ({ tank: tank.slug, carry: carry.slug })));
    if (!pairs.length) throw new Error("This filter has no distinct frontline and backline anchor pairs.");
  }
  // Validate shared constraints before starting so an excluded kept unit does not
  // become a silent empty result across every pair.
  const options = tftFlexOptions(catalog, { ...requested, ...pairs[0] });
  const cancelled = requested.cancelled || (() => false), found = new Map();
  let expanded = 0, pairsDone = 0, truncated = false;
  for (const pair of pairs) {
    if (cancelled()) return { boards: [], expanded, pairsDone, pairsTotal: pairs.length, truncated, cancelled: true };
    const fixed = [...new Set([...(pair.cores || [pair.tank, pair.carry]), ...options.locked])];
    const occupied = fixed.reduce((sum, slug) => sum + catalog.unitsBySlug.get(slug).slotCost, 0);
    if (occupied <= 8 || pairs.length === 1) {
      const result = await tftFlexSearch(catalog, { ...options, ...pair, yieldEvery: requested.yieldEvery || 1500 });
      expanded += result.expanded; truncated ||= result.truncated;
      if (result.cancelled || cancelled()) return { boards: [], expanded, pairsDone, pairsTotal: pairs.length, truncated, cancelled: true };
      for (const board of result.boards) {
        const existing = found.get(board.id);
        const field = explicitCores ? "corePairs" : "anchorPairs", group = explicitCores ? pair.cores : pair;
        if (existing) existing[field].push(group);
        else { board[field] = [group]; found.set(board.id, board); }
      }
    }
    ++pairsDone;
    requested.onProgress?.({ pairsDone, pairsTotal: pairs.length, expanded, boardsFound: found.size });
  }
  if (cancelled()) return { boards: [], expanded, pairsDone, pairsTotal: pairs.length, truncated, cancelled: true };
  return { boards: [...found.values()].sort(tftFlexCompare), expanded, pairsDone, pairsTotal: pairs.length, truncated, cancelled: false };
}

const TFT_FLEX_SORTS = ["score", "gold", "lobbyGold", "activeCount", "extraTiers", "frontlineCount", "purchaseGold", "fourCosts"];
function tftFlexSortValue(board, sort) {
  if (sort === "gold") return Number.isFinite(board.gold?.totalGold) ? board.gold.totalGold : null;
  if (sort === "lobbyGold") return Number.isFinite(board.lobbyGold?.totalGold) ? board.lobbyGold.totalGold : null;
  if (sort === "purchaseGold") return board.units.reduce((sum, unit) => sum + unit.cost * tftFlexCopyCount(unit.cost, tftFlexGoldCoreCost(board)), 0);
  if (sort === "fourCosts") return board.units.filter(unit => unit.cost === 4).length;
  return board[sort];
}

function tftFlexSortedBoards(boards, sort = "score", direction = "desc") {
  if (!TFT_FLEX_SORTS.includes(sort)) sort = "score";
  const sign = direction === "asc" ? 1 : -1;
  return [...boards].sort((a, b) => {
    const av = tftFlexSortValue(a, sort), bv = tftFlexSortValue(b, sort);
    if (av == null || bv == null) return av == null && bv == null ? tftFlexCompare(a, b) : av == null ? 1 : -1;
    return sign * (av - bv) || tftFlexCompare(a, b);
  });
}

function tftFlexLobbyProfiles(catalog) {
  const stats = catalog?.lobbyStats;
  if (stats?.schema !== "tft-lobby-stats-v1" || !Array.isArray(stats.profiles)) return [];
  return stats.profiles.filter(profile => {
    if (typeof profile.id !== "string" || !profile.id || profile.patch !== catalog.patch || typeof profile.rankLabel !== "string" ||
        !Array.isArray(profile.units) || profile.units.length !== catalog.units.length) return false;
    const units = new Map(profile.units.map(unit => [unit.api, unit]));
    return units.size === catalog.units.length && catalog.units.every(unit => {
      const meta = units.get(unit.api);
      return meta?.cost === unit.cost && Number.isFinite(meta.copiesPerPlayer) && meta.copiesPerPlayer >= 0;
    });
  });
}

function tftFlexLobbyProfile(catalog, selected = null) {
  const profiles = tftFlexLobbyProfiles(catalog);
  return profiles.find(profile => profile.id === selected) ||
    profiles.find(profile => profile.id === catalog?.lobbyStats?.defaultProfile) || profiles[0] || null;
}

function tftFlexLobbyPool(catalog, profile, settings = {}) {
  if (!catalog.shop || !profile) throw new Error("Typical lobby needs matching patch statistics and shop inputs.");
  const players = Math.min(7, Math.max(0, Math.floor(Number.isFinite(Number(settings.players)) ? Number(settings.players) : 7)));
  const development = Math.min(1, Math.max(0, Number.isFinite(Number(settings.development)) ? Number(settings.development) : 0.5));
  const overrides = settings.overrides || {}, meta = new Map(profile.units.map(unit => [unit.api, unit]));
  return catalog.units.map(unit => {
    const copiesPerPlayer = meta.get(unit.api)?.copiesPerPlayer, bag = catalog.shop.bagCopies[unit.cost - 1];
    if (!Number.isFinite(copiesPerPlayer) || copiesPerPlayer < 0) throw new Error(`Missing typical-lobby statistics for ${unit.name}.`);
    const observed = Object.prototype.hasOwnProperty.call(overrides, unit.slug) ? Number(overrides[unit.slug]) : null;
    if (observed !== null && (!Number.isFinite(observed) || !Number.isInteger(observed))) throw new Error(`Enter whole scouted copies for ${unit.name}.`);
    const heldCopies = Math.min(bag, Math.max(0, observed === null ? Math.round(copiesPerPlayer * players * development) : observed));
    return { api: unit.api, cost: unit.cost, heldCopies };
  });
}

function tftFlexLobbyTargets(board) {
  const coreCost = tftFlexGoldCoreCost(board);
  return board.units.map(unit => ({ api: unit.api, cost: unit.cost, copies: tftFlexCopyCount(unit.cost, coreCost) }));
}

function tftFlexTraitChanges(before, after) {
  const previous = new Map(before.activeTraits.map(trait => [trait.api, trait]));
  const next = new Map(after.activeTraits.map(trait => [trait.api, trait]));
  return [...new Set([...previous.keys(), ...next.keys()])].sort().flatMap(api => {
    const from = previous.get(api), to = next.get(api);
    return from?.breakpoint === to?.breakpoint ? [] : [{ api, name: (to || from).name,
      beforeBreakpoint: from?.breakpoint || null, afterBreakpoint: to?.breakpoint || null,
      beforeCount: before.units.reduce((sum, unit) => sum + (unit.traitContributions[api] || 0), 0),
      afterCount: after.units.reduce((sum, unit) => sum + (unit.traitContributions[api] || 0), 0) }];
  });
}

async function tftFlexSwaps(catalog, board, requested = {}) {
  const options = tftFlexOptions(catalog, requested);
  if (!tftFlexEvaluate(catalog, board.units, options).valid) throw new Error("Choose a valid board before exploring swaps.");
  const fixed = new Set([...(options.cores || [options.tank, options.carry]), ...options.locked]);
  const supports = board.units.filter(unit => !fixed.has(unit.slug)).sort((a, b) => a.api.localeCompare(b.api));
  const onBoard = new Set(board.units.map(unit => unit.slug));
  const pool = catalog.units.filter(unit => !onBoard.has(unit.slug) && unit.cost >= options.minCost && (options.allowFive || unit.cost <= 4));
  const groups = units => [...units.map(unit => [unit]), ...units.flatMap((unit, index) => units.slice(index + 1).map(other => [unit, other]))];
  const additions = new Map();
  for (const group of groups(pool)) {
    const slots = group.reduce((sum, unit) => sum + unit.slotCost, 0);
    if (!additions.has(slots)) additions.set(slots, []);
    additions.get(slots).push(group);
  }
  const maxExpansions = Math.min(120000, Math.max(1, Math.floor(requested.maxExpansions || 70000)));
  const yieldEvery = Math.min(1500, Math.max(1, Math.floor(requested.yieldEvery || 500)));
  const yieldTask = requested.yieldTask || tftFlexYield, cancelled = requested.cancelled || (() => false);
  const limit = Math.min(24, Math.max(1, Math.floor(requested.swapLimit || 10)));
  const maxScoreLoss = Number.isFinite(requested.maxScoreLoss) ? requested.maxScoreLoss : 4;
  let expanded = 0, truncated = false;
  const candidates = [];
  await yieldTask();
  for (const removed of groups(supports)) {
    if (cancelled()) return { swaps: [], expanded, cancelled: true, truncated };
    const slots = removed.reduce((sum, unit) => sum + unit.slotCost, 0);
    const removedSlugs = new Set(removed.map(unit => unit.slug));
    const retained = board.units.filter(unit => !removedSlugs.has(unit.slug));
    const retainedFourCosts = retained.filter(unit => unit.cost === 4).length;
    for (const added of additions.get(slots) || []) {
      if (retainedFourCosts + added.filter(unit => unit.cost === 4).length > options.maxFourCosts) continue;
      if (expanded >= maxExpansions) { truncated = true; break; }
      ++expanded;
      const alternative = tftFlexEvaluate(catalog, [...retained, ...added], options);
      if (alternative.valid && alternative.score >= board.score - maxScoreLoss) candidates.push({
        id: `${removed.map(unit => unit.slug).join("+")}>${added.map(unit => unit.slug).join("+")}`,
        removed, added, board: alternative, scoreDelta: alternative.score - board.score,
        traitChanges: tftFlexTraitChanges(board, alternative) });
      if (expanded % yieldEvery === 0) {
        await yieldTask();
        if (cancelled()) return { swaps: [], expanded, cancelled: true, truncated };
      }
    }
    if (expanded >= maxExpansions) break;
  }
  if (cancelled()) return { swaps: [], expanded, cancelled: true, truncated };
  candidates.sort((a, b) => b.scoreDelta - a.scoreDelta || a.removed.length - b.removed.length || a.id.localeCompare(b.id));
  const swaps = [], removals = new Map();
  // Reserve examples for both sizes so two-unit connections do not hide direct swaps.
  const take = candidate => {
    const key = candidate.removed.map(unit => unit.slug).join("+");
    if ((removals.get(key) || 0) >= 2 || swaps.some(swap => swap.id === candidate.id)) return;
    swaps.push(candidate); removals.set(key, (removals.get(key) || 0) + 1);
  };
  for (const size of [1, 2]) {
    for (const candidate of candidates.filter(candidate => Math.max(candidate.removed.length, candidate.added.length) === size)) {
      take(candidate);
      if (swaps.filter(swap => Math.max(swap.removed.length, swap.added.length) === size).length >= Math.ceil(limit / 2)) break;
    }
  }
  for (const candidate of candidates) { if (swaps.length >= limit) break; take(candidate); }
  return { swaps: swaps.slice(0, limit).sort((a, b) => a.removed.length - b.removed.length || b.scoreDelta - a.scoreDelta || a.id.localeCompare(b.id)),
    expanded, truncated, cancelled: false };
}

// ---------------- trait flex: interface ----------------
// ---------------- trait flex: lobby interface ----------------
function tftFlexSettings() {
  return { cost: tftFlexCoreCost(tflex.cost), cores: [...new Set(tflex.cores || [])].sort(), preference: tflex.preference, minCost: tflex.minCost, minFrontline: tflex.minFrontline,
    allowFive: tflex.allowFive, locked: [...tflex.locked] };
}

function cancelTftFlex() {
  ++tflex.searchReq; ++tflex.metaReq;
  tflex.controller?.abort(); tflex.controller = null; tflex.metaLoading = null;
  tflex.loading = tflex.searching = false;
  cancelTftFlexGold();
  cancelTftFlexLobby();
}

const tftFlexGoldCache = new Map(), tftFlexGoldPending = new Map();
let tftFlexGoldWorker = null, tftFlexGoldJob = 0;

function tftFlexGoldRequirements(board) {
  const counts = [0, 0, 0, 0, 0];
  for (const unit of board.units) ++counts[unit.cost - 1];
  return counts;
}

function cancelTftFlexGold() {
  tftFlexGoldWorker?.terminate(); tftFlexGoldWorker = null;
  for (const pending of tftFlexGoldPending.values()) pending.reject(Object.assign(new Error("Gold calculation cancelled"), { name: "AbortError" }));
  tftFlexGoldPending.clear();
}

function tftEnsureFlexWorker() {
  if (typeof Worker !== "function") return null;
  if (!tftFlexGoldWorker) {
    tftFlexGoldWorker = new Worker("tft-flex-worker.js");
    tftFlexGoldWorker.onmessage = ({ data }) => {
      const pending = tftFlexGoldPending.get(data.id);
      if (!pending) return;
      tftFlexGoldPending.delete(data.id);
      if (data.ok === false || data.error) pending.reject(new Error(data.error || "Gold estimate could not be calculated."));
      else { tftFlexGoldCache.set(pending.key, data.result); pending.resolve(data.result); }
    };
    tftFlexGoldWorker.onerror = () => {
      for (const pending of tftFlexGoldPending.values()) pending.reject(new Error("Gold estimate could not be calculated."));
      tftFlexGoldPending.clear(); tftFlexGoldWorker?.terminate(); tftFlexGoldWorker = null;
    };
  }
  return tftFlexGoldWorker;
}

function tftFlexGoldRequest(shop, requirements, coreCost = 4) {
  coreCost = Number(coreCost);
  const key = JSON.stringify([shop, requirements, coreCost]);
  if (tftFlexGoldCache.has(key)) return Promise.resolve(tftFlexGoldCache.get(key));
  const existing = [...tftFlexGoldPending.values()].find(pending => pending.key === key);
  if (existing) return existing.promise;
  const worker = tftEnsureFlexWorker();
  if (!worker) {
    try {
      const result = tftFlexGoldEstimate(shop, requirements, coreCost);
      tftFlexGoldCache.set(key, result);
      return Promise.resolve(result);
    } catch (error) {
      return Promise.reject(error);
    }
  }
  const id = ++tftFlexGoldJob;
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  tftFlexGoldPending.set(id, { key, promise, resolve, reject });
  worker.postMessage({ id, shop, requirements, coreCost });
  return promise;
}

function tftFlexGoldDescription(board) {
  const gold = board.gold;
  if (!gold) return board.goldError || "Calculating expected gold to hit…";
  if (gold.available === false || gold.diagnostics?.impossible) return "These star targets cannot be completed from this shop pool.";
  return `${Math.round(gold.refreshGold)}g refreshes + ${gold.purchaseGold}g units` +
    (gold.method === "monte-carlo" && Number.isFinite(gold.standardErrorGold)
      ? ` · Sampling error: ±${(1.96 * gold.standardErrorGold).toFixed(1)}g at 95% confidence for the estimated mean` : "");
}

function renderTftFlexGold(container, board) {
  if (board.gold) {
    const gold = board.gold;
    const unavailable = gold.available === false || gold.diagnostics?.impossible;
    container.replaceChildren(tftText("strong", unavailable ? "Not enough copies" : `${gold.method === "monte-carlo" ? "≈" : ""}${Math.round(gold.totalGold)}g`),
      tftText("span", `Uncontested · Expected gold to hit · ${tftFlexGoldDescription(board)}`));
  } else container.replaceChildren(tftText("span", board.goldError || (tflex.catalog?.shop
    ? "Calculating expected gold to hit…" : "Gold estimates will appear with the updated unit catalog.")));
}

function renderTftFlexGoldCell(container, board) {
  const gold = board.gold;
  container.textContent = gold ? gold.available === false || gold.diagnostics?.impossible ? "Not enough copies"
    : `${gold.method === "monte-carlo" ? "≈" : ""}${Math.round(gold.totalGold)}g` : board.goldError ? "Unavailable" : tflex.catalog?.shop ? "Calculating…" : "Unavailable";
  container.title = gold ? tftFlexGoldDescription(board) : board.goldError || "";
}

function beginTftFlexGold(catalog, boards, request) {
  if (!catalog.shop) return;
  const jobs = boards.map(board => {
    const requirements = tftFlexGoldRequirements(board), coreCost = tftFlexGoldCoreCost(board), key = JSON.stringify([catalog.shop, requirements, coreCost]);
    const update = () => {
      document.querySelectorAll(".tft-flex-gold").forEach(container => {
        if (container.dataset.board === board.id) renderTftFlexGold(container, board);
      });
      document.querySelectorAll(".tft-flex-gold-cell").forEach(container => {
        if (container.dataset.board === board.id) renderTftFlexGoldCell(container, board);
      });
      renderTftFlexStatus();
    };
    if (tftFlexGoldCache.has(key)) { board.gold = tftFlexGoldCache.get(key); delete board.goldError; update(); return; }
    delete board.goldError;
    return tftFlexGoldRequest(catalog.shop, requirements, coreCost).then(gold => {
      if (request !== tflex.searchReq || catalog !== tflex.catalog) return;
      board.gold = gold; update();
    }).catch(error => {
      if (error.name === "AbortError" || request !== tflex.searchReq || catalog !== tflex.catalog) return;
      board.goldError = `Gold estimate unavailable: ${error.message}`; update();
    });
  });
  // Finish the gold order once for the whole result set, including off-page
  // comps, without moving rows each time a cost profile completes.
  return Promise.all(jobs).then(() => {
    if (request === tflex.searchReq && catalog === tflex.catalog && tflex.sort === "gold") renderTftFlex();
  });
}

function normalizeTftFlexLobby(catalog) {
  const profile = tftFlexLobbyProfile(catalog, tflex.lobbyProfile);
  tflex.lobbyProfile = profile?.id || null;
  tflex.lobbyPlayers = Math.min(7, Math.max(0, Math.floor(Number.isFinite(Number(tflex.lobbyPlayers)) ? Number(tflex.lobbyPlayers) : 7)));
  tflex.lobbyDevelopment = Math.round(100 * Math.min(1, Math.max(0, Number.isFinite(Number(tflex.lobbyDevelopment)) ? Number(tflex.lobbyDevelopment) : 0.5))) / 100;
  tflex.lobbyOverrides = Object.fromEntries(Object.entries(tflex.lobbyOverrides || {}).flatMap(([slug, value]) => {
    const unit = catalog.unitsBySlug.get(slug), number = Number(value);
    return unit && Number.isFinite(number) ? [[slug, Math.min(catalog.shop?.bagCopies[unit.cost - 1] || 30, Math.max(0, Math.floor(number)))]] : [];
  }));
  tflex.lobbySearch ||= "";
  return profile;
}

function renderTftFlexLobbyControls() {
  const catalog = tflex.catalog;
  if (!catalog) return;
  const profile = normalizeTftFlexLobby(catalog), profiles = tftFlexLobbyProfiles(catalog), select = document.getElementById("tft-flex-lobby-profile");
  select.replaceChildren(...(profiles.length ? profiles.map(item => {
    const option = tftText("option", `${item.patch} · ${item.rankLabel}`); option.value = item.id; return option;
  }) : [tftText("option", "No matching patch statistics")]));
  select.disabled = !profiles.length; select.value = profile?.id || "";
  document.getElementById("tft-flex-lobby-players").value = String(tflex.lobbyPlayers);
  document.getElementById("tft-flex-lobby-development").value = String(Math.round(tflex.lobbyDevelopment * 100));
  document.getElementById("tft-flex-lobby-players").disabled = !profile;
  document.getElementById("tft-flex-lobby-development").disabled = !profile;
  const context = document.getElementById("tft-flex-lobby-context");
  if (!profile) context.textContent = `Typical lobby estimates are unavailable for patch ${catalog.patch}.` +
    (catalog.lobbyStats?.error ? ` ${catalog.lobbyStats.error}` : " Matching statistics need to be published.");
  else {
    const updated = new Date(profile.updatedAt), label = Number.isFinite(updated.getTime()) ? updated.toLocaleDateString() : "unknown date";
    const inferred = tftFlexLobbyPool(catalog, profile, { players: tflex.lobbyPlayers, development: tflex.lobbyDevelopment, overrides: tflex.lobbyOverrides });
    const counts = inferred.reduce((sum, unit) => sum + unit.heldCopies, 0);
    context.replaceChildren(tftText("span", `${profile.rankLabel} · Patch ${profile.patch} · Source updated ${label}. ${counts} opponent copies assumed across the whole pool. `));
    const link = tftText("a", "tactics.tools unit statistics"); link.href = profile.sourceUrl; link.target = "_blank"; link.rel = "noopener noreferrer"; context.append(link);
  }
  renderTftFlexLobbyScouts();
}

function renderTftFlexLobbyScouts() {
  const catalog = tflex.catalog;
  if (!catalog) return;
  const profile = tftFlexLobbyProfile(catalog, tflex.lobbyProfile), query = (tflex.lobbySearch || "").trim().toLowerCase();
  const inferred = profile ? new Map(tftFlexLobbyPool(catalog, profile, { players: tflex.lobbyPlayers, development: tflex.lobbyDevelopment }).map(unit => [unit.api, unit.heldCopies])) : new Map();
  const units = catalog.units.filter(unit => !query || [unit.name, ...unit.traitApis.map(api => catalog.traitsByApi.get(api).name)].join(" ").toLowerCase().includes(query))
    .sort((a, b) => a.cost - b.cost || a.name.localeCompare(b.name));
  document.getElementById("tft-flex-lobby-list").replaceChildren(...units.map(unit => {
    const row = document.createElement("div"); row.className = "tft-flex-lobby-unit";
    const label = document.createElement("label"); label.htmlFor = `tft-flex-scout-${unit.slug}`;
    label.append(tftText("span", `${unit.name} · ${unit.cost} cost`), tftText("small", profile ? `Estimate: ${inferred.get(unit.api)} copies held` : "Statistics unavailable"));
    const input = document.createElement("input"); input.type = "number"; input.id = label.htmlFor; input.min = "0";
    input.max = String(catalog.shop?.bagCopies[unit.cost - 1] || 30); input.step = "1"; input.placeholder = "Auto";
    input.dataset.unit = unit.slug; input.disabled = !profile; input.setAttribute("aria-label", `Opponent copies of ${unit.name}`);
    input.value = Object.prototype.hasOwnProperty.call(tflex.lobbyOverrides || {}, unit.slug) ? String(tflex.lobbyOverrides[unit.slug]) : "";
    input.addEventListener("change", () => {
      if (input.value.trim() === "") delete tflex.lobbyOverrides[unit.slug];
      else {
        const value = Number(input.value);
        if (!Number.isFinite(value)) { input.value = ""; delete tflex.lobbyOverrides[unit.slug]; }
        else { const copies = Math.min(Number(input.max), Math.max(0, Math.floor(value))); tflex.lobbyOverrides[unit.slug] = copies; input.value = String(copies); }
      }
      recalculateTftFlexLobby();
    });
    row.append(label, input); return row;
  }));
  document.getElementById("tft-flex-lobby-reset").disabled = !Object.keys(tflex.lobbyOverrides || {}).length;
}

const tftFlexLobbyCache = new Map(), tftFlexLobbyPending = new Map();
let tftFlexLobbyWorker = null, tftFlexLobbyJob = 0;

function cancelTftFlexLobby() {
  tflex.lobbyReq = (tflex.lobbyReq || 0) + 1;
  tftFlexLobbyWorker?.terminate(); tftFlexLobbyWorker = null;
  for (const pending of tftFlexLobbyPending.values()) pending.reject(Object.assign(new Error("Lobby calculation cancelled"), { name: "AbortError" }));
  tftFlexLobbyPending.clear();
}

function tftEnsureFlexLobbyWorker() {
  if (typeof Worker !== "function") return null;
  if (!tftFlexLobbyWorker) {
    tftFlexLobbyWorker = new Worker("tft-flex-worker.js");
    tftFlexLobbyWorker.onmessage = ({ data }) => {
      const pending = tftFlexLobbyPending.get(data.id);
      if (!pending) return;
      tftFlexLobbyPending.delete(data.id);
      if (data.ok === false || data.error) pending.reject(new Error(data.error || "Typical lobby estimate could not be calculated."));
      else { tftFlexLobbyCache.set(pending.key, data.result); pending.resolve(data.result); }
    };
    tftFlexLobbyWorker.onerror = () => {
      for (const pending of tftFlexLobbyPending.values()) pending.reject(new Error("Typical lobby estimate could not be calculated."));
      tftFlexLobbyPending.clear(); tftFlexLobbyWorker?.terminate(); tftFlexLobbyWorker = null;
    };
  }
  return tftFlexLobbyWorker;
}

function tftFlexLobbyRequest(shop, pool, targets) {
  const summary = tftFlexLobbyEstimate(shop, pool, targets, { signatureOnly: true });
  if (summary.available === false) return Promise.resolve({ ...summary, method: "unavailable" });
  const key = summary.signature;
  if (tftFlexLobbyCache.has(key)) return Promise.resolve(tftFlexLobbyCache.get(key));
  const existing = [...tftFlexLobbyPending.values()].find(pending => pending.key === key);
  if (existing) return existing.promise;
  const worker = tftEnsureFlexLobbyWorker();
  if (!worker) {
    try {
      const result = tftFlexLobbyEstimate(shop, pool, targets);
      tftFlexLobbyCache.set(key, result);
      return Promise.resolve(result);
    } catch (error) {
      return Promise.reject(error);
    }
  }
  const id = ++tftFlexLobbyJob;
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  tftFlexLobbyPending.set(id, { key, promise, resolve, reject });
  worker.postMessage({ id, shop, pool, targets });
  return promise;
}

function tftFlexLobbyDescription(board) {
  const gold = board.lobbyGold;
  if (gold?.available === false) return (gold.unavailableTargets || []).map(target => {
    const name = tflex.catalog?.units.find(unit => unit.api === target.api)?.name || target.api;
    return `${name}: ${target.available} copies left, ${target.required} required`;
  }).join("; ") || "These holdings prevent the roster from being completed.";
  if (!gold) return board.lobbyError || (tftFlexLobbyProfile(tflex.catalog, tflex.lobbyProfile) ? "Calculating typical lobby gold…" : "Typical lobby statistics are unavailable for this patch.");
  return `${Math.round(gold.refreshGold)}g refreshes + ${gold.purchaseGold}g units` +
    (gold.method === "monte-carlo" && Number.isFinite(gold.standardErrorGold) ? ` · Sampling error: ±${(1.96 * gold.standardErrorGold).toFixed(1)}g at 95% confidence for the estimated mean` : "");
}

function renderTftFlexLobbyGold(container, board) {
  if (board.lobbyGold?.available === false) container.replaceChildren(tftText("strong", "Not enough copies"), tftText("span", ` · Typical lobby · ${tftFlexLobbyDescription(board)}`));
  else if (Number.isFinite(board.lobbyGold?.totalGold)) container.replaceChildren(tftText("strong", `≈${Math.round(board.lobbyGold.totalGold)}g`), tftText("span", ` · Typical lobby · ${tftFlexLobbyDescription(board)}`));
  else container.replaceChildren(tftText("span", tftFlexLobbyDescription(board)));
}

function renderTftFlexLobbyCell(container, board) {
  container.textContent = board.lobbyGold?.available === false ? "Not enough copies" : Number.isFinite(board.lobbyGold?.totalGold)
    ? `≈${Math.round(board.lobbyGold.totalGold)}g` : board.lobbyError ? "Unavailable" : tftFlexLobbyProfile(tflex.catalog, tflex.lobbyProfile) ? "Calculating…" : "Unavailable";
  container.title = tftFlexLobbyDescription(board);
}

function updateTftFlexLobbyBoard(board) {
  document.querySelectorAll(".tft-flex-lobby-gold").forEach(container => {
    if (container.dataset.board === board.id) renderTftFlexLobbyGold(container, board);
  });
  document.querySelectorAll(".tft-flex-lobby-cell").forEach(container => {
    if (container.dataset.board === board.id) renderTftFlexLobbyCell(container, board);
  });
  renderTftFlexStatus();
}

function beginTftFlexLobby(catalog, boards, request, { sort = true } = {}) {
  const profile = tftFlexLobbyProfile(catalog, tflex.lobbyProfile);
  if (!catalog.shop || !profile) return Promise.resolve();
  const epoch = tflex.lobbyReq || 0;
  let pool;
  try { pool = tftFlexLobbyPool(catalog, profile, { players: tflex.lobbyPlayers, development: tflex.lobbyDevelopment, overrides: tflex.lobbyOverrides }); }
  catch (error) { for (const board of boards) { board.lobbyError = error.message; updateTftFlexLobbyBoard(board); } return Promise.resolve(); }
  const current = () => request === tflex.searchReq && epoch === (tflex.lobbyReq || 0) && catalog === tflex.catalog;
  const jobs = boards.map(board => {
    delete board.lobbyError;
    return Promise.resolve().then(() => {
      if (!current()) throw Object.assign(new Error("Lobby calculation cancelled"), { name: "AbortError" });
      return tftFlexLobbyRequest(catalog.shop, pool, tftFlexLobbyTargets(board));
    }).then(gold => {
      if (!current()) return;
      board.lobbyGold = gold; updateTftFlexLobbyBoard(board);
    }).catch(error => {
      if (error.name === "AbortError" || !current()) return;
      board.lobbyError = `Typical lobby estimate unavailable: ${error.message}`; updateTftFlexLobbyBoard(board);
    });
  });
  return Promise.all(jobs).then(() => { if (sort && current() && tflex.sort === "lobbyGold") renderTftFlex(); });
}

function recalculateTftFlexLobby() {
  if (!tflex.catalog) return;
  cancelTftFlexLobby(); normalizeTftFlexLobby(tflex.catalog); renderTftFlexLobbyControls(); updateHash(true);
  const boards = [...tflex.boards, ...(tflex.lobbySwapBoards || new Map()).values()];
  for (const board of boards) { delete board.lobbyGold; delete board.lobbyError; updateTftFlexLobbyBoard(board); }
  return beginTftFlexLobby(tflex.catalog, boards, tflex.searchReq);
}

function renderTftFlexLocks() {
  const catalog = tflex.catalog;
  if (!catalog) return;
  const query = tflex.lockSearch.trim().toLowerCase(), locked = new Set(tflex.locked);
  const maxFourCosts = tftFlexFourCostLimit(tftFlexCoreCost(tflex.cost));
  const lockedFourCosts = tflex.locked.filter(slug => catalog.unitsBySlug.get(slug)?.cost === 4).length;
  document.getElementById("tft-flex-lock-summary").textContent = tflex.locked.length
    ? `Keep units you already own (${tflex.locked.length} locked)` : "Keep units you already own (optional)";
  const units = catalog.units.filter(unit => !(tflex.cores || []).includes(unit.slug) &&
    (!query || [unit.name, ...unit.traitApis.map(api => catalog.traitsByApi.get(api).name)].join(" ").toLowerCase().includes(query)))
    .sort((a, b) => a.cost - b.cost || a.name.localeCompare(b.name));
  document.getElementById("tft-flex-lock-list").replaceChildren(...units.map(unit => {
    const label = document.createElement("label"); label.className = "tft-flex-lock"; label.dataset.cost = unit.cost;
    const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.checked = locked.has(unit.slug);
    const excluded = unit.cost < tflex.minCost || unit.cost === 5 && !tflex.allowFive ||
      unit.cost === 4 && lockedFourCosts >= maxFourCosts;
    checkbox.disabled = excluded && !checkbox.checked;
    if (unit.cost < tflex.minCost) checkbox.title = `Excluded by the ${tflex.minCost}+ minimum unit cost.`;
    else if (unit.cost === 4 && lockedFourCosts >= maxFourCosts) checkbox.title = `This comp allows at most ${maxFourCosts} 4-cost units.`;
    checkbox.setAttribute("aria-label", `Keep ${unit.name}`);
    checkbox.addEventListener("change", () => {
      tflex.locked = checkbox.checked ? [...new Set([...tflex.locked, unit.slug])].sort() : tflex.locked.filter(slug => slug !== unit.slug);
      checkbox.disabled = (unit.cost < tflex.minCost || unit.cost === 5 && !tflex.allowFive) && !checkbox.checked;
      const fourCostCount = tflex.locked.filter(slug => catalog.unitsBySlug.get(slug)?.cost === 4).length;
      for (const label of document.getElementById("tft-flex-lock-list").children) {
        if (Number(label.dataset.cost) !== 4) continue;
        const input = label.querySelector("input");
        input.disabled = !input.checked && fourCostCount >= maxFourCosts;
        input.title = fourCostCount >= maxFourCosts ? `This comp allows at most ${maxFourCosts} 4-cost units.` : "";
      }
      document.getElementById("tft-flex-lock-summary").textContent = tflex.locked.length
        ? `Keep units you already own (${tflex.locked.length} locked)` : "Keep units you already own (optional)";
      renderTftFlexPicker();
      runTftFlexSearch({ navigate: true });
    });
    label.append(checkbox, tftIcon(unit.icon, unit.name), tftText("span", `${unit.name} · ${unit.cost} cost${unit.slotCost > 1 ? " · 2 slots" : ""}`));
    return label;
  }));
}

function renderTftFlexPicker() {
  const catalog = tflex.catalog;
  if (!catalog) return;
  const selected = new Set(tflex.cores || []), query = (tflex.coreSearch || "").trim().toLocaleLowerCase();
  const fixed = new Set([...selected, ...tflex.locked]);
  const slotsUsed = [...fixed].reduce((sum, slug) => sum + (catalog.unitsBySlug.get(slug)?.slotCost || 0), 0);
  const grid = document.getElementById("tft-flex-unit-seg"), scroll = grid.scrollTop;
  grid.dataset.cost = tflex.cost;
  const choose = slug => {
    tflex.cores = selected.has(slug) ? tflex.cores.filter(core => core !== slug) : [...tflex.cores, slug].sort();
    renderTftFlexControls(); runTftFlexSearch({ navigate: true });
    const focusTarget = [...grid.children].find(button => button.dataset?.key === slug) || document.getElementById("tft-flex-search");
    focusTarget.focus({ preventScroll: true });
  };
  const units = catalog.units.filter(unit => unit.cost === tflex.cost && (!query ||
    [unit.name, ...unit.traitApis.map(api => catalog.traitsByApi.get(api).name)].some(name => name.toLocaleLowerCase().includes(query))))
    .sort((a, b) => a.name.localeCompare(b.name));
  grid.replaceChildren(...units.map(unit => {
    const button = tftText("button", "", "tft-unit" + (selected.has(unit.slug) ? " active" : ""));
    button.type = "button"; button.dataset.key = unit.slug; button.dataset.cost = unit.cost;
    button.setAttribute("aria-pressed", String(selected.has(unit.slug)));
    button.setAttribute("aria-label", `${unit.name}, ${unit.cost} cost, ${unit.role}. Traits: ${unit.traitApis.map(api => catalog.traitsByApi.get(api).name).join(", ")}. ${selected.has(unit.slug) ? "Selected core." : "Select as a core."}`);
    button.disabled = !selected.has(unit.slug) && !fixed.has(unit.slug) && slotsUsed + unit.slotCost > 8;
    if (button.disabled) button.title = "Unlock a kept unit or remove a core to make room.";
    const text = tftText("span", "", "tft-unit-text"), traits = tftText("span", "", "tft-unit-traits");
    traits.setAttribute("aria-hidden", "true");
    traits.append(...unit.traitApis.map(api => { const trait = catalog.traitsByApi.get(api); return tftIcon(trait.icon, trait.name, true); }));
    text.append(tftText("span", unit.name, "tft-unit-name"), traits);
    button.append(tftIcon(unit.icon, unit.name), text);
    bindTftTooltip(button, () => tftFlexUnitTooltip(unit, null));
    button.addEventListener("click", () => choose(unit.slug)); return button;
  }));
  if (!units.length) grid.append(tftText("p", "No champions match this name or trait.", "tft-empty"));
  grid.scrollTop = scroll;
  const chips = document.getElementById("tft-flex-selected");
  chips.replaceChildren(tftText("span", selected.size
    ? `${selected.size} core${selected.size === 1 ? "" : "s"} selected${selected.size === 1 ? " · showing all same-cost partners" : ""}`
    : `All ${tflex.cost}-cost core pairs`, "sub"),
    ...tflex.cores.map(slug => {
      const unit = catalog.unitsBySlug.get(slug), chip = tftText("button", `${unit.name} ×`, "tft-flex-core-chip");
      chip.type = "button"; chip.dataset.key = slug;
      chip.setAttribute("aria-label", `Remove ${unit.name} from the core`);
      chip.addEventListener("click", () => choose(slug)); return chip;
    }));
}

function renderTftFlexControls() {
  const catalog = tflex.catalog;
  if (!catalog) return;
  tflex.cost = tftFlexCoreCost(tflex.cost);
  const costSelect = document.getElementById("tft-flex-cost");
  costSelect.value = String(tflex.cost); costSelect.disabled = false;
  tflex.cores = [...new Set(tflex.cores || [])].filter(slug => catalog.unitsBySlug.get(slug)?.cost === tflex.cost).sort();
  document.getElementById("tft-flex-search").value = tflex.coreSearch || "";
  tflex.preference = tflex.preference === "vertical" ? "vertical" : "breadth";
  tflex.minCost = Math.min(tflex.cost, 3, Math.max(1, Math.floor(Number(tflex.minCost) || 1)));
  tflex.minFrontline = Math.min(8, Math.max(1, Math.floor(Number(tflex.minFrontline) || 3)));
  tflex.locked = [...new Set(tflex.locked)].filter(slug => catalog.unitsBySlug.has(slug) && !tflex.cores.includes(slug)).sort();
  document.getElementById("tft-flex-frontline").value = String(tflex.minFrontline);
  const minimumSelect = document.getElementById("tft-flex-min-cost");
  minimumSelect.value = String(tflex.minCost);
  for (const option of minimumSelect.children) option.disabled = Number(option.value) > tflex.cost;
  document.getElementById("tft-flex-five").checked = tflex.allowFive;
  tflex.sort = TFT_FLEX_SORTS.includes(tflex.sort) ? tflex.sort : "score";
  tflex.order = tflex.order === "asc" ? "asc" : "desc";
  document.getElementById("tft-flex-sort").value = tflex.sort;
  document.getElementById("tft-flex-order").value = tflex.order;
  tftActive("tft-flex-preference-seg", tflex.preference);
  document.getElementById("tft-flex-formula").textContent = tflex.preference === "vertical"
    ? "Score = active traits + 3 × extra reached tiers."
    : "Score = 3 × active traits + extra reached tiers.";
  document.getElementById("tft-flex-context").textContent = `Browse all ${tflex.cost}-cost core pairs, pick one champion to see every partner, or select several to keep them all. Any roles can be selected. ` +
    (tflex.cost < 4 ? `At most ${tftFlexFourCostLimit(tflex.cost)} 4-cost units on the whole board, including kept units and swaps. ` : "") +
    `Units use ${tflex.minCost}–${tflex.allowFive ? 5 : 4} costs. ` +
    "Frontline count includes tanks and fighters with range 2 or less; supports can fill this requirement.";
  document.getElementById("tft-flex-gold-target-note").hidden = tflex.cost === 4;
  document.getElementById("tft-flex-gold-target-note").textContent = tflex.cost < 4
    ? `Gold targets: every ${tflex.cost}-cost unit needs 9 copies for 3★; every other unit needs 1 copy for 1★.`
    : "Gold targets: every 4-cost unit needs 3 copies for 2★; every other unit needs 1 copy for 1★.";
  document.getElementById("tft-set-label").textContent = `Set ${catalog.set} · Patch ${catalog.patch}`;
  const shop = catalog.shop;
  const tableWrap = document.getElementById("tft-flex-shop-table"), sources = document.getElementById("tft-flex-shop-sources");
  tableWrap.replaceChildren(); sources.replaceChildren();
  if (shop) {
    const table = document.createElement("table"), header = document.createElement("tr"), body = document.createElement("tbody");
    header.append(...["Cost", "Level 8 odds", "Champions", "Copies per champion", "Copies required"].map(label => tftText("th", label)));
    const head = document.createElement("thead"); head.append(header); table.append(head);
    for (let index = 0; index < 5; ++index) {
      const row = document.createElement("tr");
      const copies = tftFlexCopyCount(index + 1, tflex.cost);
      row.append(...[index + 1, `${Math.round(100 * shop.costOdds[index])}%`, shop.championCounts[index], shop.bagCopies[index], `${copies} (${copies === 9 ? 3 : copies === 3 ? 2 : 1}★)`].map(value => tftText("td", String(value)))); body.append(row);
    }
    table.append(body); tableWrap.append(table);
    sources.append(tftText("span", "Shop inputs: "));
    for (const [index, source] of (shop.sources || []).entries()) {
      if (index) sources.append(tftText("span", " · "));
      const link = tftText("a", source.name); link.href = source.url; link.target = "_blank"; link.rel = "noopener noreferrer"; sources.append(link);
    }
  }
  renderTftFlexPicker();
  renderTftFlexLocks();
  renderTftFlexLobbyControls();
}

function tftFlexCoreSlugs(board) {
  return board.cores || [board.mainTank, board.mainCarry].filter(Boolean);
}

function tftFlexCoreNames(board) {
  return tftFlexCoreSlugs(board).map(slug => board.units.find(unit => unit.slug === slug)?.name || slug).join(" + ");
}

function tftFlexUnitTooltip(unit, board) {
  const catalog = tflex.catalog;
  const assignment = board ? tftFlexCoreSlugs(board).includes(unit.slug) ? "Core unit" : "Support" : "Core champion choice";
  const copies = tftFlexCopyCount(unit.cost, board ? tftFlexGoldCoreCost(board) : tflex.cost);
  const contents = [tftTooltipHead(unit.icon, unit.name, `${unit.cost} cost · ${assignment} · ${unit.slotCost} team slot${unit.slotCost === 1 ? "" : "s"}`),
    tftText("p", `Gold target: ${copies} cop${copies === 1 ? "y" : "ies"} for ${copies === 9 ? 3 : copies === 3 ? 2 : 1}★.`, "sub"),
    tftText("p", `${unit.role}${Number.isFinite(unit.range) ? ` · Range ${unit.range}` : ""}${unit.frontline ? " · Frontline" : ""}`, "sub")];
  const counts = board ? tftFlexCounts(catalog, board.units) : null;
  for (const api of unit.traitApis) {
    const trait = catalog.traitsByApi.get(api), active = board?.activeTraits.find(row => row.api === api);
    if (!board) { contents.push(tftText("p", `${trait.name}: breakpoints ${trait.levels.join(" / ") || "none"}.`)); continue; }
    contents.push(tftText("p", `${trait.name}: ${counts[trait.index]} contribution${counts[trait.index] === 1 ? "" : "s"}. ` +
      (active ? `Reached ${active.breakpoint}; breakpoints ${trait.levels.join(" / ")}.${trait.levels[0] === 1 ? " Solo activation counts toward traits, score and core coverage." : ""}`
        : trait.levels.length ? `Breakpoints ${trait.levels.join(" / ")}; none reached.`
          : "No activation breakpoints are defined.")));
  }
  return contents;
}

function tftFlexPackage(units, board) {
  const group = document.createElement("div"); group.className = "tft-flex-package";
  for (const unit of units) {
    const button = tftText("button", ""); button.type = "button"; button.dataset.cost = unit.cost;
    button.append(tftIcon(unit.icon, unit.name), tftText("span", unit.name));
    bindTftTooltip(button, () => tftFlexUnitTooltip(unit, board), true); group.append(button);
  }
  return group;
}

function tftFlexSwapDetails(board) {
  const details = document.createElement("details"); details.className = "tft-details tft-flex-swaps";
  details.append(tftText("summary", "Interchangeable one-unit and two-unit support packages"));
  const body = document.createElement("div"); details.append(body);
  details.addEventListener("toggle", async () => {
    if (!details.open || details.dataset.loaded === "true") return;
    details.dataset.loaded = "true";
    body.replaceChildren(tftText("p", "Finding legal support swaps…", "hint"));
    const request = tflex.searchReq, catalog = tflex.catalog, options = { ...tftFlexSettings(), cores: tftFlexCoreSlugs(board) };
    try {
      const result = await tftFlexSwaps(catalog, board, { ...options, cancelled: () => request !== tflex.searchReq || tstate.mode !== "traits" });
      if (result.cancelled || request !== tflex.searchReq) return;
      body.replaceChildren(tftText("p", `Each swap keeps all ${options.cores.length} cores and your locked units, fills 8 team slots, activates a trait for each core (solo traits count), uses only ${options.minCost}+ cost units, and retains at least ${options.minFrontline} frontline unit${options.minFrontline === 1 ? "" : "s"}.` +
        (options.cost < 4 ? ` The whole board keeps at most ${tftFlexFourCostLimit(options.cost)} 4-cost units.` : ""), "hint"));
      if (!result.swaps.length) { body.append(tftText("p", "No nearby one-unit or two-unit swaps meet these settings. Try fewer locks or a lower frontline minimum.", "hint")); return; }
      const wrap = document.createElement("div"); wrap.className = "tablewrap"; wrap.tabIndex = 0;
      wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", "Legal support swaps and trait changes");
      const table = document.createElement("table"), head = document.createElement("thead"), header = document.createElement("tr");
      header.append(...["Replace", "Use instead", "Reached breakpoints", "Score change", ...(catalog.shop ? ["Expected gold (uncontested)", "Typical lobby gold"] : [])].map(text => tftText("th", text)));
      head.append(header); table.append(head);
      const rows = document.createElement("tbody");
      for (const swap of result.swaps) {
        const row = document.createElement("tr"), removed = document.createElement("td"), added = document.createElement("td");
        removed.append(tftFlexPackage(swap.removed, board)); added.append(tftFlexPackage(swap.added, swap.board));
        const changes = swap.traitChanges.length ? swap.traitChanges.map(change =>
          `${change.name}: ${change.beforeBreakpoint || "off"} → ${change.afterBreakpoint || "off"}`).join("; ") : "Same reached breakpoints.";
        const delta = swap.scoreDelta > 0 ? `+${swap.scoreDelta}` : swap.scoreDelta < 0 ? `−${-swap.scoreDelta}` : "0";
        row.append(removed, added, tftText("td", changes), tftText("td", `${delta} (${board.score} → ${swap.board.score})`));
        if (catalog.shop) {
          const goldCell = tftText("td", "Calculating…"); row.append(goldCell);
          Promise.all([tftFlexGoldRequest(catalog.shop, tftFlexGoldRequirements(board), tftFlexGoldCoreCost(board)),
            tftFlexGoldRequest(catalog.shop, tftFlexGoldRequirements(swap.board), tftFlexGoldCoreCost(swap.board))]).then(([before, after]) => {
              if (request !== tflex.searchReq || catalog !== tflex.catalog) return;
              goldCell.title = tftFlexGoldDescription({ gold: after });
              if (!Number.isFinite(after.totalGold)) { goldCell.textContent = "Not enough copies"; return; }
              const prefix = after.method === "monte-carlo" ? "≈" : "";
              if (!Number.isFinite(before.totalGold)) { goldCell.textContent = `${prefix}${Math.round(after.totalGold)}g`; return; }
              const change = Math.round(after.totalGold - before.totalGold);
              goldCell.textContent = `${prefix}${Math.round(after.totalGold)}g (${change > 0 ? "+" : change < 0 ? "−" : ""}${Math.abs(change)}g)`;
            }).catch(error => { if (request === tflex.searchReq && error.name !== "AbortError") goldCell.textContent = "Unavailable"; });
          const lobbyCell = document.createElement("td"); lobbyCell.className = "tft-flex-lobby-cell"; lobbyCell.dataset.board = swap.board.id;
          renderTftFlexLobbyCell(lobbyCell, swap.board); row.append(lobbyCell);
          tflex.lobbySwapBoards ||= new Map(); tflex.lobbySwapBoards.set(swap.board.id, swap.board);
        }
        rows.append(row);
      }
      table.append(rows); wrap.append(table); body.append(wrap);
      beginTftFlexLobby(catalog, result.swaps.map(swap => swap.board), request, { sort: false });
    } catch (error) { if (request === tflex.searchReq) body.replaceChildren(tftText("p", error.message, "hint")); }
  });
  return details;
}

function tftFlexBoardCard(board, rank) {
  const card = document.createElement("article"); card.className = "tft-comp-card tft-flex-card";
  const coreSlugs = tftFlexCoreSlugs(board);
  const heading = document.createElement("div"); heading.className = "tft-comp-heading";
  heading.append(tftText("h3", `${rank}. ${tftFlexCoreNames(board)}`), tftCompositionCopyButton(board, tflex.catalog.teamPlanner)); card.append(heading);
  const metrics = document.createElement("div"); metrics.className = "tft-comp-metrics tft-flex-metrics";
  for (const [value, label] of [[board.score, "Trait score"], [board.activeCount, "Active traits"], [board.extraTiers, "Extra reached tiers"], [board.frontlineCount, "Frontline units"]]) {
    const metric = document.createElement("div"); metric.className = "tft-comp-metric";
    metric.append(tftText("strong", String(value)), tftText("span", label)); metrics.append(metric);
  }
  card.append(tftText("p", `${board.units.length} distinct units · ${board.slotsUsed}/8 team slots`, "tft-comp-meta"), metrics);
  const gold = document.createElement("div"); gold.className = "tft-flex-gold"; gold.dataset.board = board.id;
  gold.setAttribute("role", "status"); renderTftFlexGold(gold, board); card.append(gold);
  const lobby = document.createElement("div"); lobby.className = "tft-flex-lobby-gold"; lobby.dataset.board = board.id;
  lobby.setAttribute("role", "status"); renderTftFlexLobbyGold(lobby, board); card.append(lobby);
  for (const slug of coreSlugs) {
    const unit = board.units.find(unit => unit.slug === slug);
    const coverage = board.coreCoverage?.[slug] || board.anchorCoverage?.[slug === board.mainTank ? "tank" : "carry"] || [];
    const covered = tftText("p", "", "tft-flex-coverage");
    covered.append(tftText("strong", `${unit.name}: `), tftText("span", coverage.map(trait => `${trait.breakpoint} ${trait.name}`).join(" · ")));
    card.append(covered);
  }
  const units = document.createElement("div"); units.className = "tft-comp-units";
  for (const unit of board.units) {
    const tile = document.createElement("div"); tile.className = "tft-comp-unit"; tile.dataset.cost = unit.cost;
    const assignment = coreSlugs.includes(unit.slug) ? "Core unit" :
      tflex.locked.includes(unit.slug) ? "Kept unit" : unit.frontline ? "Frontline support" : "Support";
    tile.dataset.assignment = coreSlugs.includes(unit.slug) ? "core" : "support";
    const button = tftText("button", "", "tft-comp-unit-head"); button.type = "button"; button.dataset.unit = unit.slug;
    button.append(tftIcon(unit.icon, unit.name), tftText("span", unit.name, "tft-comp-unit-name"));
    button.setAttribute("aria-label", `${unit.name}, ${unit.cost} cost, ${assignment}${unit.slotCost > 1 ? ", 2 team slots" : ""}`);
    bindTftTooltip(button, () => tftFlexUnitTooltip(unit, board), true);
    const copies = tftFlexCopyCount(unit.cost, tftFlexGoldCoreCost(board));
    tile.append(button, tftText("span", `${unit.cost} cost${unit.slotCost > 1 ? " · 2 slots" : ""} · ${copies === 9 ? "3★ target" : copies === 3 ? "2★ target" : "1 copy"}`, "tft-comp-assignment"), tftText("span", assignment, "tft-comp-assignment")); units.append(tile);
  }
  card.append(units);
  const traits = document.createElement("div"); traits.className = "tft-comp-traits";
  for (const trait of [...board.activeTraits].sort((a, b) => b.breakpoint - a.breakpoint || a.name.localeCompare(b.name))) {
    const chip = tftText("span", "", "tft-comp-trait");
    chip.append(tftIcon(trait.icon, trait.name, true), tftText("span", `${trait.breakpoint} ${trait.name}`));
    chip.title = `${trait.count} contributions; breakpoints ${trait.levels.join(" / ")}${trait.levels[0] === 1 ? "; solo activation counts" : ""}`; traits.append(chip);
  }
  card.append(traits, tftFlexSwapDetails(board));
  return card;
}

function renderTftFlexTable(boards, offset) {
  const wrap = document.createElement("div"); wrap.className = "tablewrap tft-flex-tablewrap"; wrap.tabIndex = 0;
  wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", "Trait flex comps and sorting metrics");
  const table = document.createElement("table"); table.className = "tft-flex-table";
  const head = document.createElement("thead"), header = document.createElement("tr");
  for (const [label, sort] of [["#"], ["Comp"], ["Units"], ["Trait score", "score"], ["Active traits", "activeCount"],
    ["Extra tiers", "extraTiers"], ["Frontline", "frontlineCount"], ["Uncontested gold", "gold"], ["Typical lobby gold", "lobbyGold"], ["Unit gold", "purchaseGold"]]) {
    const cell = document.createElement("th"); cell.scope = "col";
    if (sort) {
      cell.setAttribute("aria-sort", tflex.sort === sort ? tflex.order === "asc" ? "ascending" : "descending" : "none");
      const button = tftText("button", label + (tflex.sort === sort ? tflex.order === "asc" ? " ↑" : " ↓" : ""));
      button.type = "button"; button.setAttribute("aria-label", `Sort by ${label.toLowerCase()}`);
      button.addEventListener("click", () => setTftFlexSort(sort, tflex.sort === sort ? tflex.order === "asc" ? "desc" : "asc" : undefined));
      cell.append(button);
    } else cell.textContent = label;
    header.append(cell);
  }
  head.append(header); table.append(head);
  const body = document.createElement("tbody");
  tflex.cards ||= new Map(); tflex.openBoards ||= new Set();
  for (const [index, board] of boards.entries()) {
    const rank = offset + index + 1, row = document.createElement("tr"); row.className = "tft-flex-summary"; row.dataset.board = board.id;
    const name = tftFlexCoreNames(board), comp = document.createElement("td"), roster = document.createElement("td"); roster.className = "tft-flex-roster";
    const detail = document.createElement("tr"); detail.className = "tft-flex-detail";
    const detailCell = document.createElement("td"); detailCell.colSpan = 10; detailCell.id = `tft-flex-detail-${rank}`; detail.append(detailCell);
    const button = tftText("button", "", "tft-flex-open"); button.type = "button";
    button.append(tftText("strong", name), tftText("span", "View comp and swaps"));
    button.setAttribute("aria-label", `View ${name}, comp ${rank}`); button.setAttribute("aria-controls", detailCell.id);
    const show = open => {
      detail.hidden = !open; button.setAttribute("aria-expanded", String(open));
      button.children[1].textContent = open ? "Hide comp details" : "View comp and swaps";
      if (!open) { tflex.openBoards.delete(board.id); return; }
      tflex.openBoards.add(board.id);
      if (!tflex.cards.has(board.id)) tflex.cards.set(board.id, tftFlexBoardCard(board, rank));
      const card = tflex.cards.get(board.id); card.querySelector("h3").textContent = `${rank}. ${name}`;
      renderTftFlexGold(card.querySelector(".tft-flex-gold"), board);
      renderTftFlexLobbyGold(card.querySelector(".tft-flex-lobby-gold"), board);
      detailCell.replaceChildren(card);
    };
    button.addEventListener("click", () => show(detail.hidden)); show(tflex.openBoards.has(board.id)); comp.append(button);
    for (const unit of board.units) {
      const unitButton = tftText("button", unit.name); unitButton.type = "button"; unitButton.dataset.cost = unit.cost;
      unitButton.setAttribute("aria-label", `${unit.name}, ${unit.cost} cost`);
      bindTftTooltip(unitButton, () => tftFlexUnitTooltip(unit, board), true); roster.append(unitButton);
    }
    const metric = (value, label) => { const cell = tftText("td", String(value)); cell.dataset.label = label; return cell; };
    const gold = document.createElement("td"); gold.className = "tft-flex-gold-cell"; gold.dataset.board = board.id; gold.dataset.label = "Uncontested gold"; renderTftFlexGoldCell(gold, board);
    const lobby = document.createElement("td"); lobby.className = "tft-flex-lobby-cell"; lobby.dataset.board = board.id; lobby.dataset.label = "Typical lobby gold"; renderTftFlexLobbyCell(lobby, board);
    row.append(tftText("td", String(rank)), comp, roster, metric(board.score, "Trait score"), metric(board.activeCount, "Active traits"),
      metric(board.extraTiers, "Extra tiers"), metric(board.frontlineCount, "Frontline"), gold, lobby, metric(`${tftFlexSortValue(board, "purchaseGold")}g`, "Unit purchase gold"));
    body.append(row, detail);
  }
  table.append(body); wrap.append(table); return wrap;
}

function renderTftFlexStatus() {
  const status = document.getElementById("tft-flex-status"), empty = document.getElementById("tft-flex-empty");
  document.getElementById("tft-flex-retry").hidden = !tflex.error && !tflex.pending;
  document.getElementById("tft-flex-results").setAttribute("aria-busy", String(tflex.searching));
  empty.hidden = true;
  if (tflex.error) { status.textContent = tflex.error + (tflex.boards.length ? " Your previous boards remain available." : ""); return; }
  if (tflex.loading) { status.textContent = "Loading the unit and trait catalog…"; return; }
  if (tflex.pending) { status.textContent = "Trait flex will be available after a completed TFT publication. Retry shortly; an older dashboard server may need a restart."; return; }
  if (!tflex.catalog) { status.textContent = "Loading level 8 trait combinations."; return; }
  const coreCount = (tflex.cores || []).length;
  const selection = coreCount === 0 ? `across all ${tflex.cost}-cost core pairs` : coreCount === 1
    ? `including ${tflex.catalog.unitsBySlug.get(tflex.cores[0])?.name || tflex.cores[0]}` : `for ${coreCount} selected core champions`;
  if (tflex.searching) {
    status.textContent = `Finding level 8 comps ${selection}` +
      (tflex.pairsTotal ? ` · ${tflex.pairsDone}/${tflex.pairsTotal} core groups checked` : "") + "…";
    return;
  }
  const goldPending = tflex.catalog.shop ? tflex.boards.filter(board => !board.gold && !board.goldError).length : 0;
  const lobbyPending = tflex.catalog.shop && tftFlexLobbyProfile(tflex.catalog, tflex.lobbyProfile) ? tflex.boards.filter(board => !board.lobbyGold && !board.lobbyError).length : 0;
  status.textContent = `${tflex.boards.length} comps ${selection}.` +
    (goldPending ? ` Calculating gold for ${goldPending} comps${tflex.sort === "gold" ? "; gold order will update when all estimates are ready" : ""}.` : "") +
    (lobbyPending ? ` Calculating typical lobby gold for ${lobbyPending} comps${tflex.sort === "lobbyGold" ? "; lobby order will update when all estimates are ready" : ""}.` : "");
  if (!tflex.boards.length) {
    empty.hidden = false;
    empty.textContent = "No complete boards found for these settings. Try fewer cores or kept units, a lower minimum unit cost or frontline minimum, or different cores.";
  }
}

function renderTftFlex() {
  const sorted = tftFlexSortedBoards(tflex.boards, tflex.sort, tflex.order), pageSize = 25;
  const pages = Math.max(1, Math.ceil(sorted.length / pageSize));
  tflex.page = Math.max(1, Math.floor(Number(tflex.page) || 1));
  if (!tflex.searching && !tflex.loading) tflex.page = Math.min(pages, tflex.page);
  const offset = (tflex.page - 1) * pageSize;
  document.getElementById("tft-flex-results").replaceChildren(...(sorted.length ? [renderTftFlexTable(sorted.slice(offset, offset + pageSize), offset)] : []));
  document.getElementById("tft-flex-pagination").hidden = !sorted.length;
  document.getElementById("tft-flex-page").textContent = sorted.length ? `Showing ${offset + 1}–${Math.min(offset + pageSize, sorted.length)} of ${sorted.length} comps · Page ${tflex.page} of ${pages}` : "";
  document.getElementById("tft-flex-prev").disabled = tflex.page === 1;
  document.getElementById("tft-flex-next").disabled = tflex.page === pages;
  renderTftFlexStatus();
}

function setTftFlexSort(sort, order) {
  tflex.sort = TFT_FLEX_SORTS.includes(sort) ? sort : "score";
  tflex.order = order === "asc" || order === "desc" ? order : ["gold", "lobbyGold", "purchaseGold", "fourCosts"].includes(tflex.sort) ? "asc" : "desc";
  tflex.page = 1;
  document.getElementById("tft-flex-sort").value = tflex.sort;
  document.getElementById("tft-flex-order").value = tflex.order;
  updateHash(true); hideTftTooltip(); renderTftFlex();
}

async function runTftFlexSearch({ force = false, navigate = false } = {}) {
  if (!tflex.catalog || tstate.mode !== "traits") return;
  const options = tftFlexSettings(), catalog = tflex.catalog;
  const key = JSON.stringify([catalog.revision, options]);
  if (navigate) tflex.page = 1;
  updateHash(navigate);
  if (!force && key === tflex.key && !tflex.searching) { renderTftFlex(); beginTftFlexGold(catalog, tflex.boards, tflex.searchReq); beginTftFlexLobby(catalog, tflex.boards, tflex.searchReq); return; }
  const request = ++tflex.searchReq;
  cancelTftFlexGold();
  cancelTftFlexLobby();
  tflex.key = null; tflex.boards = []; tflex.error = null; tflex.pending = false; tflex.searching = true;
  tflex.expanded = 0; tflex.pairsDone = tflex.pairsTotal = 0; tflex.cards = new Map(); tflex.openBoards = new Set();
  tflex.lobbySwapBoards = new Map();
  hideTftTooltip(); renderTftFlex();
  try {
    const result = await tftFlexBrowse(catalog, { ...options,
      cancelled: () => request !== tflex.searchReq || tstate.mode !== "traits" || state.view !== "tft" || document.hidden,
      onProgress: progress => {
        if (request !== tflex.searchReq) return;
        tflex.pairsDone = progress.pairsDone || 0; tflex.pairsTotal = progress.pairsTotal || 0;
        renderTftFlexStatus();
      } });
    if (request !== tflex.searchReq || result.cancelled) return;
    tflex.boards = result.boards; tflex.expanded = result.expanded; tflex.key = key;
    tflex.pairsDone = result.pairsDone || 0; tflex.pairsTotal = result.pairsTotal || 0;
    beginTftFlexGold(catalog, tflex.boards, request);
    beginTftFlexLobby(catalog, tflex.boards, request);
  } catch (error) { if (request === tflex.searchReq) tflex.error = error.message; }
  finally { if (request === tflex.searchReq) { tflex.searching = false; renderTftFlex(); updateHash(); scheduleTftStatus(); } }
}

async function loadTftFlex({ force = false } = {}) {
  force = force || (tflex.catalog && (tftPublicationChanged(tstate.status, tflex.catalog) ||
    tstate.status?.patch && tstate.status.patch !== tflex.catalog.patch));
  if (tflex.catalog && !force) { renderTftFlexControls(); await runTftFlexSearch(); return true; }
  if (tflex.metaLoading && !force) return tflex.metaLoading;
  const request = ++tflex.metaReq;
  tflex.controller?.abort(); tflex.controller = new AbortController();
  tflex.loading = true; tflex.error = null; renderTftFlex();
  const loading = (async () => {
    try {
      const raw = await api("api/tft/traits.json", { cache: "no-store", signal: tflex.controller.signal });
      if (request !== tflex.metaReq) return !!tflex.catalog;
      if (raw?.pending) { tflex.pending = true; return false; }
      const catalog = tftFlexCatalog(raw);
      tflex.catalog = catalog; tflex.pending = false; tflex.key = null; tflex.loading = false;
      renderTftFlexControls();
      if (tstate.mode === "traits") await runTftFlexSearch({ force: true });
      return true;
    } catch (error) {
      if (request !== tflex.metaReq) return !!tflex.catalog;
      if (error.message === "HTTP 404" && !tflex.catalog) tflex.pending = true;
      else tflex.error = error.message === "HTTP 404"
        ? "The new trait catalog has not been published yet. Retry after the next TFT refresh."
        : `Trait flex could not be loaded (${error.message}). Try again shortly.`;
      return false;
    } finally {
      if (request === tflex.metaReq) { tflex.metaLoading = null; tflex.loading = false; renderTftFlex(); updateHash(); scheduleTftStatus(); }
    }
  })();
  tflex.metaLoading = loading;
  return loading;
}

function restoreTftFlexHash(hash) {
  tflex.cost = tftFlexCoreCost(hash.fcost);
  tflex.minCost = Math.min(tflex.cost, 3, Math.max(1, Math.floor(Number(hash.fmincost) || 1)));
  tflex.cores = [...new Set((hash.fcore !== undefined ? hash.fcore.split(",")
    : [hash.ftank, hash.fcarry]).filter(slug => slug && slug !== "all"))].sort();
  tflex.coreSearch = "";
  tflex.sort = TFT_FLEX_SORTS.includes(hash.fsort) ? hash.fsort : "score";
  tflex.order = ["asc", "desc"].includes(hash.forder) ? hash.forder : ["gold", "lobbyGold", "purchaseGold", "fourCosts"].includes(tflex.sort) ? "asc" : "desc";
  tflex.lobbyProfile = hash.flprofile || null;
  tflex.lobbyPlayers = hash.flplayers !== undefined && Number.isFinite(Number(hash.flplayers)) ? Math.min(7, Math.max(0, Math.floor(Number(hash.flplayers)))) : 7;
  tflex.lobbyDevelopment = hash.fldev !== undefined && Number.isFinite(Number(hash.fldev)) ? Math.min(100, Math.max(0, Number(hash.fldev))) / 100 : 0.5;
  tflex.lobbyOverrides = Object.fromEntries((hash.flheld || "").split(",").flatMap(part => {
    const [slug, count] = part.split(":"), number = Number(count);
    return slug && count !== undefined && count.trim() !== "" && Number.isFinite(number) && Number.isInteger(number) && number >= 0 ? [[slug, Math.min(30, number)]] : [];
  }));
  tflex.preference = hash.fpref === "vertical" ? "vertical" : "breadth";
  tflex.minFrontline = Math.min(8, Math.max(1, Math.floor(Number(hash.ffront) || 3)));
  tflex.allowFive = hash.ffive === "1";
  tflex.locked = [...new Set((hash.fkeep || "").split(",").filter(Boolean))].sort();
  tflex.page = Math.max(1, Math.floor(Number(hash.fpage) || 1));
}

// ---------------- trait flex: events ----------------
document.getElementById("tft-flex-cost").addEventListener("change", event => {
  tflex.cost = tftFlexCoreCost(event.target.value); tflex.coreSearch = ""; renderTftFlexControls(); runTftFlexSearch({ navigate: true });
});
document.getElementById("tft-flex-search").addEventListener("input", event => {
  tflex.coreSearch = event.target.value; renderTftFlexPicker();
});
document.querySelectorAll("#tft-flex-preference-seg button").forEach(button => button.addEventListener("click", () => {
  tflex.preference = button.dataset.key; renderTftFlexControls(); runTftFlexSearch({ navigate: true });
}));
document.getElementById("tft-flex-frontline").addEventListener("change", event => {
  tflex.minFrontline = Number(event.target.value); runTftFlexSearch({ navigate: true });
});
document.getElementById("tft-flex-min-cost").addEventListener("change", event => {
  tflex.minCost = Number(event.target.value); renderTftFlexControls(); runTftFlexSearch({ navigate: true });
});
document.getElementById("tft-flex-five").addEventListener("change", event => {
  tflex.allowFive = event.target.checked; renderTftFlexControls(); runTftFlexSearch({ navigate: true });
});
document.getElementById("tft-flex-lock-search").addEventListener("input", event => {
  tflex.lockSearch = event.target.value; renderTftFlexLocks();
});
document.getElementById("tft-flex-lobby-profile").addEventListener("change", event => {
  tflex.lobbyProfile = event.target.value; recalculateTftFlexLobby();
});
document.getElementById("tft-flex-lobby-players").addEventListener("change", event => {
  tflex.lobbyPlayers = Number(event.target.value); recalculateTftFlexLobby();
});
document.getElementById("tft-flex-lobby-development").addEventListener("change", event => {
  tflex.lobbyDevelopment = Number(event.target.value) / 100; recalculateTftFlexLobby();
});
document.getElementById("tft-flex-lobby-search").addEventListener("input", event => {
  tflex.lobbySearch = event.target.value; renderTftFlexLobbyScouts();
});
document.getElementById("tft-flex-lobby-reset").addEventListener("click", () => {
  tflex.lobbyOverrides = {}; recalculateTftFlexLobby();
});
document.getElementById("tft-flex-sort").addEventListener("change", event => setTftFlexSort(event.target.value));
document.getElementById("tft-flex-order").addEventListener("change", event => setTftFlexSort(tflex.sort, event.target.value));
for (const [id, step] of [["tft-flex-prev", -1], ["tft-flex-next", 1]]) document.getElementById(id).addEventListener("click", () => {
  tflex.page += step; hideTftTooltip(); renderTftFlex(); updateHash(true);
});
document.getElementById("tft-flex-retry").addEventListener("click", () => loadTftFlex({ force: true }));
// ---------------- trait flex: end ----------------

// ---------------- early comps: data ----------------
const TFT_EARLY_SORTS = ["wins", "dps", "frontline", "gold", "traits"];
const TFT_EARLY_PAGE_SIZE = 25;

function tftEarlyRosterValid(units, level) {
  return Array.isArray(units) && units.length === level && new Set(units.map(unit => unit?.api)).size === level &&
    units.every(unit => unit && typeof unit.api === "string" && typeof unit.name === "string" &&
      Number.isInteger(unit.cost) && unit.cost >= 1 && unit.cost <= (level === 4 ? 2 : 3) &&
      unit.star === 1 && (unit.slotCost == null || unit.slotCost === 1) &&
      (!unit.items || Array.isArray(unit.items) && unit.items.length === 0) &&
      (!unit.itemApis || Array.isArray(unit.itemApis) && unit.itemApis.length === 0)) &&
    units.filter(unit => unit.cost === 3).length <= (level === 5 ? 1 : 0);
}

function tftEarlyBoardValid(board, variant, reference) {
  const metrics = board?.metrics, units = board?.units, matchups = board?.matchups;
  if (!board || typeof board.id !== "string" || !board.id || board.level !== variant.level ||
      board.boardSlots !== variant.level || board.unitCount !== variant.level || board.slotsUsed !== variant.level ||
      board.itemCount !== 0 || !tftEarlyRosterValid(units, variant.level) ||
      !units.every(unit => typeof unit.slug === "string" && unit.slug && typeof unit.frontline === "boolean") ||
      new Set(units.map(unit => unit.slug)).size !== units.length ||
      !units.some(unit => unit.slug === board.mainCarry) || !units.some(unit => unit.slug === board.mainTank) ||
      board.threeCostCount !== units.filter(unit => unit.cost === 3).length ||
      board.purchaseGold !== units.reduce((sum, unit) => sum + unit.cost, 0) ||
      !Array.isArray(board.traits) || board.activeTraitCount !== board.traits.filter(trait => trait.active).length ||
      !metrics || !Array.isArray(matchups) || matchups.length !== reference.encounterCount ||
      !Number.isInteger(metrics.benchmarkCount) || metrics.benchmarkCount <= 0 || metrics.benchmarkCount !== matchups.length ||
      ![metrics.damageDps, metrics.frontlineTime].every(value => Number.isFinite(value) && value >= 0)) return false;
  const outcomes = { win: "benchmarkWins", loss: "benchmarkLosses", draw: "benchmarkDraws", timeout: "benchmarkTimeouts" };
  if (!matchups.every(matchup => matchup && Object.hasOwn(outcomes, matchup.outcome) &&
      typeof matchup.key === "string" && reference.boards.some(opponent => opponent.id === matchup.opponentId) &&
      reference.initiatives.includes(matchup.initiative) && reference.laneOffsets.includes(matchup.laneOffset) &&
      Number.isFinite(matchup.duration) && matchup.duration >= 0 && tftEarlyRosterValid(matchup.roster, variant.level)) ||
      new Set(matchups.map(matchup => matchup.key)).size !== matchups.length ||
      new Set(matchups.map(matchup => `${matchup.opponentId}:${matchup.initiative}:${matchup.laneOffset}`)).size !== matchups.length ||
      !matchups.every(matchup => {
        const expected = reference.boards.find(opponent => opponent.id === matchup.opponentId).units;
        return matchup.roster.every(unit => expected.some(other => other.api === unit.api && other.star === unit.star));
      })) return false;
  return Object.entries(outcomes).every(([outcome, field]) => metrics[field] === matchups.filter(matchup => matchup.outcome === outcome).length) &&
    Number.isFinite(metrics.benchmarkWinRate) && Math.abs(metrics.benchmarkWinRate - metrics.benchmarkWins / metrics.benchmarkCount) < 1e-8;
}

function tftEarlyCatalog(raw) {
  if (raw?.schema !== "tft-early-comps-v1" || raw.complete !== true || raw.evaluationModel !== "stage-two-reference-fights-v1" ||
      typeof raw.revision !== "string" || !raw.revision ||
      typeof raw.patch !== "string" || !Number.isInteger(raw.set) || raw.stage !== 2 || raw.itemCount !== 0 ||
      raw.starAssumption !== "All units at 1★" || !Array.isArray(raw.variants) || raw.variants.length !== 2 ||
      ![4, 5].every(level => raw.variants.some(variant => variant?.key === `level${level}` && variant.level === level)))
    throw new Error("The saved early composition data is incomplete or unsupported.");
  for (const variant of raw.variants) {
    const reference = raw.referencePools?.[variant.key];
    if (variant.boardSlots !== variant.level || variant.maxThreeCosts !== (variant.level === 5 ? 1 : 0) ||
        !Array.isArray(variant.boards) || new Set(variant.boards.map(board => board?.id)).size !== variant.boards.length ||
        !reference || !Array.isArray(reference.boards) || reference.opponentCount !== reference.boards.length ||
        new Set(reference.boards.map(board => board?.id)).size !== reference.boards.length ||
        !reference.opponentCount || !Array.isArray(reference.initiatives) || !reference.initiatives.length ||
        !Array.isArray(reference.laneOffsets) || !reference.laneOffsets.length ||
        !Number.isInteger(reference.encounterCount) || reference.encounterCount <= 0 ||
        reference.encounterCount !== reference.opponentCount * reference.initiatives.length * reference.laneOffsets.length ||
        !reference.boards.every(board => typeof board.id === "string" && tftEarlyRosterValid(board.units, variant.level)) ||
        !variant.boards.every(board => tftEarlyBoardValid(board, variant, reference)))
      throw new Error("The saved early boards do not match the cost, upgrade or benchmark assumptions.");
  }
  return raw;
}

function tftEarlyVariant() {
  return tearly.data?.variants.find(variant => variant.level === tearly.level);
}

function tftEarlyRows() {
  const boards = tftEarlyVariant()?.boards || [];
  const value = board => ({ wins: board.metrics.benchmarkWinRate, dps: board.metrics.damageDps,
    frontline: board.metrics.frontlineTime, gold: board.purchaseGold, traits: board.activeTraitCount })[tearly.sort];
  return boards.filter(board => (tearly.level !== 5 || tearly.allowThree || !board.threeCostCount) &&
      (tearly.unit === "all" || board.units.some(unit => unit.slug === tearly.unit)))
    .slice().sort((a, b) => (value(a) - value(b)) * (tearly.order === "asc" ? 1 : -1) ||
      (tearly.sort === "wins" ? a.purchaseGold - b.purchaseGold : b.metrics.benchmarkWinRate - a.metrics.benchmarkWinRate) || a.id.localeCompare(b.id));
}

function restoreTftEarlyHash(hash) {
  tearly.level = hash.elevel === "5" ? 5 : 4;
  tearly.allowThree = hash.ethree !== "0";
  tearly.unit = hash.eunit || "all";
  tearly.sort = TFT_EARLY_SORTS.includes(hash.esort) ? hash.esort : "wins";
  tearly.order = ["asc", "desc"].includes(hash.eorder) ? hash.eorder : tearly.sort === "gold" ? "asc" : "desc";
  tearly.page = Math.max(1, Math.floor(Number(hash.epage) || 1));
}

function cancelTftEarly() {
  ++tearly.req;
  tearly.controller?.abort();
  tearly.controller = null; tearly.promise = null; tearly.loading = false;
}

// ---------------- early comps: interface ----------------
const tftEarlyNumber = (value, digits = 0) => Number.isFinite(value)
  ? value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "—";
const tftEarlyTime = value => `${tftEarlyNumber(value, 1)}s`;
const tftEarlyWins = board => `${board.metrics.benchmarkWins} / ${board.metrics.benchmarkCount}`;
function tftEarlyTitle(board) {
  const name = slug => board.units.find(unit => unit.slug === slug)?.name || slug;
  return board.mainCarry === board.mainTank ? name(board.mainCarry) : `${name(board.mainCarry)} + ${name(board.mainTank)}`;
}

function tftEarlyUnitTooltip(unit, board) {
  const assignment = unit.slug === board?.mainCarry ? "Main damage dealer" : unit.slug === board?.mainTank ? "Main frontliner" : "Support";
  const traits = (board?.traits || []).filter(trait => unit.traitApis?.includes(trait.api));
  const content = [tftTooltipHead(unit.icon, unit.name, `${unit.star}★ · ${unit.cost} cost · ${unit.frontline ? "Frontline" : "Backline"}`),
    tftText("p", `${unit.role || unit.objective || ""}${board ? ` · ${assignment}${board.alphaHolder === unit.slug ? " · Alpha" : ""}` : ""}`)];
  if (traits.length) content.push(tftText("p", traits.map(trait => `${trait.name}${trait.active ? ` (${trait.count}, active)` : ` (${trait.count})`}`).join(" · ")));
  if (Number.isFinite(unit.dps)) content.push(tftText("p", `${tftEarlyNumber(unit.dps)} encounter DPS · ${tftEarlyNumber(unit.damage)} damage · ${tftEarlyTime(unit.aliveTime)} alive, averaged across reference fights.`));
  return content;
}

function tftEarlyUnit(unit, board) {
  const card = document.createElement("div"), head = document.createElement("button"), name = document.createElement("span");
  card.className = "tft-comp-unit";
  card.dataset.unit = unit.slug; card.dataset.cost = unit.cost; card.dataset.star = unit.star;
  card.dataset.assignment = unit.slug === board.mainCarry ? "mainCarry" : unit.slug === board.mainTank ? "mainTank" : "support";
  head.type = "button"; head.className = "tft-comp-unit-head";
  head.setAttribute("aria-label", `${unit.name}, ${unit.star} stars: show early fight contribution`);
  bindTftTooltip(head, () => tftEarlyUnitTooltip(unit, board), true);
  name.append(tftText("span", unit.name, "tft-comp-unit-name"), tftText("span", `${unit.star}★ · ${unit.cost} cost`, "tft-comp-unit-star"));
  head.append(tftIcon(unit.icon, unit.name), name);
  const assignment = unit.slug === board.mainCarry ? "Main damage dealer" : unit.slug === board.mainTank ? "Main frontliner" : unit.frontline ? "Frontline support" : "Backline support";
  card.append(head, tftText("span", `${assignment}${board.alphaHolder === unit.slug ? " · Alpha" : ""}`, "tft-comp-assignment"));
  return card;
}

function tftEarlyTraits(traits) {
  const list = document.createElement("div"); list.className = "tft-comp-traits";
  for (const trait of traits.filter(trait => trait.active)) {
    const chip = document.createElement("button"); chip.type = "button"; chip.className = "tft-comp-trait";
    chip.dataset.trait = trait.api; chip.dataset.modeled = String(trait.modeled);
    chip.dataset.coverage = trait.coverage || (trait.modeled ? "supported" : "unmodeled");
    chip.append(tftIcon(trait.icon, trait.name, true), tftText("span", `${trait.count} ${trait.name}${trait.coverage === "partial" ? " · Partial" : ""}`));
    const coverage = { supported: "Modeled combat effects contribute to these fights.", partial: "Some combat effects are modeled; omitted effects can change the result.",
      structural: "Board slots or trait counts are modeled.", economy: "Economy effects do not contribute to combat.", unmodeled: "This trait is active, but its combat effect is not modeled." }[chip.dataset.coverage];
    chip.setAttribute("aria-label", `${trait.name}: ${trait.count} units, breakpoint ${trait.breakpoint}. ${coverage || ""}`);
    bindTftTooltip(chip, () => [tftTooltipHead(trait.icon, trait.name, `${trait.count} units · ${trait.breakpoint} breakpoint active`, true),
      ...(trait.note ? [tftText("p", trait.note)] : []), ...(coverage ? [tftText("p", coverage, "sub")] : []),
      ...(trait.coverageNotes || []).map(note => tftText("p", note, "sub"))], true);
    list.append(chip);
  }
  return list;
}

function tftEarlyMatchups(board) {
  const list = document.createElement("div"); list.className = "tft-comp-matchup-list";
  const reference = tearly.data.referencePools[`level${board.level}`], groups = new Map();
  for (const matchup of board.matchups) {
    const group = groups.get(matchup.opponentId) || []; group.push(matchup); groups.set(matchup.opponentId, group);
  }
  for (const [id, matches] of groups) {
    const row = document.createElement("div"), header = document.createElement("div");
    row.className = "tft-comp-matchup"; row.dataset.opponent = id; header.className = "tft-comp-matchup-head";
    header.append(tftText("strong", matches[0].label), tftText("span", `${matches.filter(match => match.outcome === "win").length} / ${matches.length} wins`)); row.append(header);
    const roster = document.createElement("details"); roster.className = "tft-details";
    roster.append(tftText("summary", "Opponent champions"), tftText("p", matches[0].roster.map(unit => `${unit.name} ${unit.star}★`).join(" · ")));
    const opponent = reference.boards.find(board => board.id === id);
    if (opponent?.traits?.length) roster.append(tftEarlyTraits(opponent.traits));
    if (opponent?.limitations?.length) {
      const assumptions = document.createElement("details"); assumptions.className = "tft-details";
      assumptions.append(tftText("summary", "Opponent model assumptions"), ...opponent.limitations.map(note => tftText("p", note)));
      roster.append(assumptions);
    }
    row.append(roster);
    for (const matchup of matches.slice().sort((a, b) => a.laneOffset - b.laneOffset || a.initiative - b.initiative)) {
      const trial = document.createElement("div"), heading = document.createElement("div");
      trial.className = "tft-comp-matchup-trial"; trial.dataset.outcome = matchup.outcome; heading.className = "tft-comp-matchup-head";
      heading.append(tftText("span", `Position ${reference.laneOffsets.indexOf(matchup.laneOffset) + 1} · ${matchup.initiative === 0 ? "Allies act first" : "Opponents act first"}`),
        tftText("span", { win: "Win", loss: "Loss", draw: "Draw", timeout: "Time limit" }[matchup.outcome], "tft-comp-matchup-outcome"));
      trial.append(heading, tftText("p", `${tftEarlyTime(matchup.duration)}` +
        (Number.isFinite(matchup.allyHpFraction) && Number.isFinite(matchup.enemyHpFraction)
          ? ` · Allies ${tftEarlyNumber(100 * matchup.allyHpFraction)}% HP · Enemies ${tftEarlyNumber(100 * matchup.enemyHpFraction)}% HP` : "")));
      row.append(trial);
    }
    list.append(row);
  }
  return list;
}

function tftEarlyCard(board) {
  const card = document.createElement("article"); card.className = "tft-comp-card"; card.dataset.earlyBoard = board.id;
  const heading = document.createElement("div"); heading.className = "tft-comp-heading";
  heading.append(tftText("h3", tftEarlyTitle(board)), tftCompositionCopyButton(board, tearly.data.teamPlanner)); card.append(heading);
  card.append(tftText("p", `${board.level} units · No items · ${board.activeTraitCount} active traits · ${board.purchaseGold}g to buy the required copies`, "tft-comp-meta"));
  const metrics = document.createElement("div"); metrics.className = "tft-comp-metrics tft-early-metrics";
  for (const [label, value, help] of [
    ["Benchmark wins", `${tftEarlyWins(board)} (${tftEarlyNumber(100 * board.metrics.benchmarkWinRate, 1)}%)`, "Saved fights against the fixed early reference pool. This is not a live stage 2 win probability."],
    ["Encounter DPS", tftEarlyNumber(board.metrics.damageDps), "Actual team damage divided by fight duration, averaged across reference fights."],
    ["Frontline held", tftEarlyTime(board.metrics.frontlineTime), "Time until the last allied frontliner dies or combat ends, averaged across reference fights."],
    ["Purchase gold", `${board.purchaseGold}g`, "Buying one copy of each champion. Shop refreshes and leveling are excluded."],
  ]) {
    const metric = document.createElement("div"); metric.className = "tft-comp-metric"; metric.title = help;
    metric.append(tftText("strong", value), tftText("span", label)); metrics.append(metric);
  }
  card.append(metrics);
  const units = document.createElement("div"); units.className = "tft-comp-units"; units.style.setProperty("--tft-comp-columns", board.level);
  units.setAttribute("aria-label", "Champions grouped from frontline to backline; not hex positioning");
  units.append(...board.units.slice().sort((a, b) => Number(b.frontline) - Number(a.frontline) || a.name.localeCompare(b.name)).map(unit => tftEarlyUnit(unit, board)));
  card.append(units, tftEarlyTraits(board.traits));
  if (board.primalBlessings?.length) card.append(tftText("p", `Primal blessings: ${board.primalBlessings.join(" + ")}.`, "tft-comp-contribution-note"));
  const evidence = document.createElement("details"); evidence.className = "tft-details tft-early-evidence";
  evidence.append(tftText("summary", `Reference fights and unit contributions · ${tftEarlyWins(board)} wins`));
  evidence.addEventListener("toggle", () => {
    if (!evidence.open || evidence.dataset.built) return;
    const reference = tearly.data.referencePools[`level${board.level}`];
    evidence.append(tftText("p", `${reference.encounterCount} simulated fights against ${reference.opponentCount} fixed early boards, across ${reference.laneOffsets.length} opposing positions and ${reference.initiatives.length} initiative orders. Draws and timeouts count as no win.`, "tft-comp-contribution-note"));
    const scroll = document.createElement("div"), table = document.createElement("table"), head = document.createElement("thead"), body = document.createElement("tbody"), headers = document.createElement("tr");
    scroll.className = "tablewrap"; table.className = "tft-early-contributions";
    headers.append(...["Champion", "Damage", "DPS", "Time alive", "Role"].map(label => { const th = tftText("th", label); th.scope = "col"; return th; })); head.append(headers);
    body.append(...board.units.slice().sort((a, b) => (b.damage || 0) - (a.damage || 0)).map(unit => {
      const row = document.createElement("tr");
      row.append(...[`${unit.name} ${unit.star}★`, tftEarlyNumber(unit.damage), tftEarlyNumber(unit.dps), tftEarlyTime(unit.aliveTime), unit.frontline ? "Frontline" : "Backline"].map(text => tftText("td", text))); return row;
    }));
    table.append(head, body); scroll.append(table); evidence.append(scroll,
      tftText("p", "Unit contributions are averages across these reference fights. Items, augments and your actual opponents can change which unit performs best.", "tft-comp-contribution-note"), tftEarlyMatchups(board));
    evidence.dataset.built = "true";
  });
  card.append(evidence);
  if (board.limitations?.length) {
    const assumptions = document.createElement("details"); assumptions.className = "tft-details";
    assumptions.append(tftText("summary", "Model assumptions for this board"), ...board.limitations.map(note => tftText("p", note)));
    card.append(assumptions);
  }
  return card;
}

function renderTftEarlyControls() {
  const variant = tftEarlyVariant(), units = new Map();
  for (const board of variant?.boards || []) for (const unit of board.units) if (tearly.allowThree || unit.cost !== 3) units.set(unit.slug, unit);
  if (variant && tearly.unit !== "all" && !units.has(tearly.unit)) tearly.unit = "all";
  const select = document.getElementById("tft-early-unit");
  select.replaceChildren(...[{ slug: "all", name: "Any champion" }, ...[...units.values()].sort((a, b) => a.cost - b.cost || a.name.localeCompare(b.name))].map(unit => {
    const option = tftText("option", unit.slug === "all" ? unit.name : `${unit.name} · ${unit.cost} cost`); option.value = unit.slug; return option;
  }));
  select.value = tearly.unit; select.disabled = !variant;
  document.getElementById("tft-early-level").value = String(tearly.level);
  const three = document.getElementById("tft-early-three"); three.checked = tearly.allowThree; three.disabled = tearly.level !== 5;
  document.getElementById("tft-early-sort").value = tearly.sort;
  document.getElementById("tft-early-order").value = tearly.order;
  document.getElementById("tft-early-clear").hidden = tearly.unit === "all" && tearly.allowThree;
  const reference = tearly.data?.referencePools[variant?.key];
  document.getElementById("tft-early-context").textContent = `${tearly.level} units · ${tearly.level === 4 || !tearly.allowThree ? "Only 1- and 2-costs" : "At most one 3-cost"} · All units at 1★ · No items.` +
    (reference ? ` Tested against ${reference.opponentCount} fixed early boards (${reference.encounterCount} fights). These are benchmark results, not live stage 2 win rates.` : "");
}

function renderTftEarlyMethod() {
  if (!tearly.data) return;
  const body = document.getElementById("tft-early-method-body");
  body.replaceChildren(tftText("p", "Four-unit boards use only 1- and 2-costs. Five-unit boards may include one 3-cost. Every champion is 1★; neither team has items."),
    tftText("p", "Saved simulations compare boards against a fixed early reference pool, testing each opposing position in both initiative orders. Benchmark win percentages describe these tests; live stage 2 results depend on items, augments, upgrades and opponents."),
    tftText("p", tearly.data.search?.description || "These are promising boards from a bounded search. They do not cover every legal roster or establish a global optimum."),
    tftText("p", "Purchase gold buys one copy of each champion. It excludes shop refreshes and leveling."),
    ...(tearly.data.limitations || []).map(note => tftText("p", note)));
  const reference = tearly.data.referencePools[`level${tearly.level}`], details = document.createElement("details"); details.className = "tft-details";
  details.append(tftText("summary", `Level ${tearly.level} reference opponents · ${reference.opponentCount} boards`));
  const list = document.createElement("ul");
  list.append(...reference.boards.map(board => tftText("li", `${board.label}: ${board.units.map(unit => `${unit.name} ${unit.star}★`).join(" + ")}`))); details.append(list); body.append(details);
}

function setTftEarlySort(sort, order) {
  if (!TFT_EARLY_SORTS.includes(sort)) return;
  tearly.order = ["asc", "desc"].includes(order) ? order : sort === tearly.sort ? tearly.order === "desc" ? "asc" : "desc" : sort === "gold" ? "asc" : "desc";
  tearly.sort = sort; tearly.page = 1; hideTftTooltip(); renderTftEarly(); updateHash(true);
}

function renderTftEarlyTable(rows, offset) {
  const headings = [["#", null], ["Composition", null], ["Champions", null], ["Benchmark wins", "wins"], ["Encounter DPS", "dps"], ["Frontline held", "frontline"], ["Purchase gold", "gold"], ["Active traits", "traits"]];
  document.querySelector("#tft-early-table thead tr").replaceChildren(...headings.map(([label, sort]) => {
    const th = document.createElement("th"); th.scope = "col";
    if (!sort) th.textContent = label;
    else {
      const button = tftText("button", label + (tearly.sort === sort ? tearly.order === "asc" ? " ↑" : " ↓" : "")); button.type = "button";
      button.addEventListener("click", () => setTftEarlySort(sort)); th.append(button);
      th.setAttribute("aria-sort", tearly.sort !== sort ? "none" : tearly.order === "asc" ? "ascending" : "descending");
    }
    return th;
  }));
  const result = [];
  for (const [index, board] of rows.entries()) {
    const row = document.createElement("tr"), detail = document.createElement("tr"), detailCell = document.createElement("td"), open = document.createElement("button");
    row.className = "tft-early-summary"; row.dataset.board = board.id; detail.className = "tft-early-detail";
    detail.id = `tft-early-board-${board.id.replace(/[^a-zA-Z0-9_-]/g, "-")}`; detail.hidden = !tearly.openBoards.has(board.id); detailCell.colSpan = headings.length; detail.append(detailCell);
    open.type = "button"; open.className = "tft-flex-open"; open.setAttribute("aria-controls", detail.id); open.setAttribute("aria-expanded", String(!detail.hidden));
    open.append(tftText("strong", tftEarlyTitle(board)), tftText("span", "Show units, traits and fights"));
    const build = () => {
      if (!tearly.cards.has(board.id)) tearly.cards.set(board.id, tftEarlyCard(board));
      detailCell.replaceChildren(tearly.cards.get(board.id));
    };
    if (!detail.hidden) build();
    open.addEventListener("click", () => {
      detail.hidden = !detail.hidden; open.setAttribute("aria-expanded", String(!detail.hidden));
      if (detail.hidden) tearly.openBoards.delete(board.id); else { tearly.openBoards.add(board.id); build(); }
      hideTftTooltip();
    });
    const title = document.createElement("td"); title.append(open);
    const roster = document.createElement("td"); roster.className = "tft-flex-roster";
    for (const unit of board.units) {
      const button = tftText("button", `${unit.name} ${unit.star}★`); button.type = "button";
      button.setAttribute("aria-label", `${unit.name}, ${unit.star} stars: show early fight contribution`);
      bindTftTooltip(button, () => tftEarlyUnitTooltip(unit, board), true); roster.append(button);
    }
    const values = [`${tftEarlyWins(board)} (${tftEarlyNumber(board.metrics.benchmarkWinRate * 100, 1)}%)`, tftEarlyNumber(board.metrics.damageDps),
      tftEarlyTime(board.metrics.frontlineTime), `${board.purchaseGold}g`, String(board.activeTraitCount)];
    row.append(tftText("td", String(offset + index + 1)), title, roster, ...values.map((value, i) => { const td = tftText("td", value); td.dataset.label = headings[i + 3][0]; return td; }));
    result.push(row, detail);
  }
  document.querySelector("#tft-early-table tbody").replaceChildren(...result);
}

function renderTftEarly() {
  renderTftEarlyControls();
  const rows = tftEarlyRows(), total = tftEarlyVariant()?.boards.length || 0, pages = Math.max(1, Math.ceil(rows.length / TFT_EARLY_PAGE_SIZE));
  tearly.page = Math.max(1, tearly.page);
  if (!tearly.loading) tearly.page = Math.min(pages, tearly.page);
  const offset = (tearly.page - 1) * TFT_EARLY_PAGE_SIZE;
  let status = tearly.loading ? tearly.data ? "Updating early comps…" : "Loading early comps…"
    : tearly.error ? `${tearly.error}${tearly.data ? " Showing the last loaded results." : ""}`
    : tearly.pending ? "Early comps are being prepared for this patch. The page will retry shortly."
    : tearly.data ? `${rows.length} of ${total} saved ${tearly.level}-unit boards. Expand a board for its units, active traits and reference fights.`
    : "Open Early comps to compare stage 2 boards.";
  document.getElementById("tft-early-status").textContent = status;
  document.getElementById("tft-early-panel").setAttribute("aria-busy", String(tearly.loading));
  const retry = document.getElementById("tft-early-retry"); retry.hidden = !tearly.error && !tearly.pending; retry.disabled = tearly.loading; retry.textContent = tearly.error ? "Retry" : "Refresh";
  document.getElementById("tft-early-tablewrap").hidden = !rows.length;
  renderTftEarlyTable(rows.slice(offset, offset + TFT_EARLY_PAGE_SIZE), offset);
  const empty = document.getElementById("tft-early-empty"); empty.hidden = !tearly.data || !!rows.length || tearly.loading;
  empty.textContent = "No saved boards match these filters. Choose another champion or allow the 3-cost support.";
  document.getElementById("tft-early-pagination").hidden = rows.length <= TFT_EARLY_PAGE_SIZE;
  document.getElementById("tft-early-page").textContent = rows.length ? `${offset + 1}–${Math.min(offset + TFT_EARLY_PAGE_SIZE, rows.length)} of ${rows.length} boards · Page ${tearly.page} of ${pages}` : "";
  document.getElementById("tft-early-prev").disabled = tearly.page <= 1;
  document.getElementById("tft-early-next").disabled = tearly.page >= pages;
  if (tearly.data) document.getElementById("tft-set-label").textContent = `Set ${tearly.data.set} · Patch ${tearly.data.patch}`;
  renderTftRefresh();
}

async function loadTftEarly({ force = false } = {}) {
  force = force || (tearly.data && (tftPublicationChanged(tstate.status, tearly.data) || tstate.status?.patch && tstate.status.patch !== tearly.data.patch));
  if (tearly.data && !force) { renderTftEarly(); renderTftEarlyMethod(); return true; }
  if (tearly.promise && !force) return tearly.promise;
  const request = ++tearly.req; tearly.controller?.abort(); const controller = new AbortController(); tearly.controller = controller;
  tearly.loading = true; tearly.error = null; renderTftEarly();
  const promise = (async () => {
    try {
      const raw = await api("api/tft/early.json", { cache: "no-store", signal: controller.signal });
      if (request !== tearly.req) return !!tearly.data;
      if (raw?.pending) { tearly.pending = true; return false; }
      const data = tftEarlyCatalog(raw);
      tearly.data = data; tearly.pending = false; tearly.cards.clear(); tearly.openBoards.clear();
      renderTftEarlyControls(); renderTftEarlyMethod(); updateHash(); return true;
    } catch (error) {
      if (request !== tearly.req) return !!tearly.data;
      tearly.pending = error.message === "HTTP 404";
      tearly.error = error.message === "HTTP 404" ? "Early comps have not been published for this patch yet. Retry after the next TFT refresh."
        : `Early comps could not be loaded (${error.message}). Try again shortly.`;
      return false;
    } finally {
      if (request === tearly.req) { tearly.promise = null; tearly.controller = null; tearly.loading = false; renderTftEarly(); updateHash(); scheduleTftStatus(); }
    }
  })();
  tearly.promise = promise; return promise;
}

for (const [id, field] of [["tft-early-level", "level"], ["tft-early-three", "allowThree"], ["tft-early-unit", "unit"]]) {
  document.getElementById(id).addEventListener("change", event => {
    tearly[field] = field === "level" ? Number(event.target.value) : field === "allowThree" ? event.target.checked : event.target.value;
    tearly.page = 1; hideTftTooltip(); renderTftEarly(); renderTftEarlyMethod(); updateHash(true);
  });
}
document.getElementById("tft-early-clear").addEventListener("click", () => {
  tearly.unit = "all"; tearly.allowThree = true; tearly.page = 1; hideTftTooltip(); renderTftEarly(); updateHash(true);
});
document.getElementById("tft-early-sort").addEventListener("change", event => setTftEarlySort(event.target.value, event.target.value === "gold" ? "asc" : "desc"));
document.getElementById("tft-early-order").addEventListener("change", event => setTftEarlySort(tearly.sort, event.target.value));
for (const [id, step] of [["tft-early-prev", -1], ["tft-early-next", 1]]) document.getElementById(id).addEventListener("click", () => {
  tearly.page += step; hideTftTooltip(); renderTftEarly(); updateHash(true);
});
document.getElementById("tft-early-retry").addEventListener("click", () => loadTftEarly({ force: true }));
// ---------------- early comps: end ----------------

async function loadTftMeta(force = false) {
  if (tstate.meta && !force) return renderTftMeta();
  const req = ++tstate.metaReq;
  try {
    const meta = await api("api/tft/meta.json", { cache: "no-store" });
    if (req !== tstate.metaReq) return !!tstate.meta;
    tstate.meta = meta;
  } catch (e) {
    if (req !== tstate.metaReq) return !!tstate.meta;
    const note = document.getElementById("tft-note");
    note.hidden = false;
    note.textContent = tstate.meta
      ? "New TFT data could not be loaded yet. Your current builds are still available; the page will retry shortly."
      : "TFT data is temporarily unavailable. Please try again shortly.";
    return false;
  }
  return renderTftMeta();
}

function renderTftMeta() {
  const m = tstate.meta;
  const cur = m.units.find(u => u.slug === tstate.unit) || m.units[0];
  tstate.unit = cur?.slug;
  if (!(tstate.geo in m.geometries)) tstate.geo = Object.keys(m.geometries)[0];
  if (!(tstate.traits in m.traitContexts)) tstate.traits = Object.keys(m.traitContexts)[0];
  if (!m.units.some(u => String(u.cost) === tstate.cost)) tstate.cost = String(cur?.cost);
  if (!(tstate.cls in TFT_CLASS)) tstate.cls = "all";
  document.getElementById("tft-note").hidden = true;
  document.getElementById("tft-set-label").textContent = `Set ${m.set} · Patch ${m.patch}`;
  document.getElementById("tft-set-label").title = m.verifiedAt
    ? `Patch-note checks passed ${new Date(m.verifiedAt).toLocaleString()}. ` +
      "MetaTFT game definitions with Riot patch corrections; CommunityDragon supplies asset references and timings."
    : "";
  const counts = {};
  m.units.forEach(u => { counts[u.objective] = (counts[u.objective] || 0) + 1; });
  const labels = { all: "All roles", carry: "Carries", fighter: "Fighters", tank: "Tanks" };
  tftSeg("tft-class-seg", Object.keys(TFT_CLASS).map(k => ({
    key: k, label: labels[k], tag: String(k === "all" ? m.units.length : counts[k] || 0), title: TFT_CLASS[k] })),
    k => { tstate.cls = k; renderTftUnits(); updateHash(true); }, false);
  tftSeg("tft-cost-seg", [...new Set(m.units.map(u => u.cost))].sort((a, b) => a - b).map(cost => ({
      key: String(cost), label: `${cost} cost`, cost })),
    k => { tstate.cost = k; renderTftUnits(); updateHash(true); }, false);
  document.getElementById("tft-search").value = tstate.search;
  renderTftUnits();
  renderTftStars();
  tftSeg("tft-geo-seg", Object.entries(m.geometries).map(([k, v]) => ({ key: k, label: k === "clump" ? "Clumped" : "Spread", title: v })),
    k => { tstate.geo = k; });
  tftSeg("tft-trait-seg", Object.entries(m.traitContexts).map(([k, v]) => ({
    key: k, label: { bare: "None", low: "First breakpoint", high: "Max non-prismatic" }[k] || k, title: v })),
    k => { tstate.traits = k; });
  tftSeg("tft-threat-seg", (m.tankThreats || []).map(t => ({ key: t.key, label: t.label, title: t.description })),
    k => { tstate.threat = k; });
  const unit = m.units.find(u => u.slug === tstate.unit);
  if (unit) renderTftThreat(unit);
  renderTftLeaderboardControls();
  renderTftPool();
  renderTftRefresh();
  return true;
}

const STAT_LABEL = { adPct: v => `+${Math.round(v * 100)}% AD`, ap: v => `+${Math.round(v)} AP`,
                     asPct: v => `+${Math.round(v * 100)}% AS`, crit: v => `+${Math.round(v * 100)}% crit`,
                     amp: v => `+${Math.round(v * 100)}% damage amp`, hp: v => `+${fmtInt(v)} HP`,
                     armor: v => `+${v} armor`, mr: v => `+${v} MR`, manaRegen: v => `+${v} mana/s`,
                     omnivamp: v => `+${Math.round(v * 100)}% omnivamp`, durability: v => `+${Math.round(v * 100)}% durability` };

function renderTftPool() {
  const m = tstate.meta;
  document.getElementById("tpool-title").textContent =
    `Item pool — ${m.items.length} craftable completed items (set ${m.set}, patch ${m.patch})`;
  document.getElementById("tpool-chips").replaceChildren(...m.items.map(it => {
    const s = document.createElement("span");
    s.className = "chip" + (it.modeled.length ? "" : " dim");
    s.append(it.name);
    if (it.modeled.length) {
      const t = document.createElement("span");
      t.className = "tag";
      t.textContent = it.modeled.join(", ");
      s.append(t);
    }
    const stats = Object.entries(it.stats).map(([k, v]) => STAT_LABEL[k] ? STAT_LABEL[k](v) : `${k} ${v}`);
    s.title = stats.join(", ") + (it.note ? ` — ${it.note}` : "");
    return s;
  }));
  const note = document.getElementById("tpool-note");
  note.textContent = m.note || "";
  note.hidden = !m.note;
  const excluded = Object.values(m.excluded || {});
  document.querySelector("#tpool-excluded summary").textContent =
    `Excluded items and why (${excluded.length})`;
  document.getElementById("tpool-excluded-list").replaceChildren(...excluded.map(txt => {
    const li = document.createElement("li");
    li.textContent = txt;
    return li;
  }));
}

async function loadTftStatus() {
  tstate.status = await api("api/tft/status.json", { cache: "no-store" });
  tstate.statusUnavailable = false;
  markTftCold();
  renderTftRefresh();
  return tstate.status;
}

function renderTftRefresh() {
  const el = document.getElementById("tft-refresh");
  const refresh = tstate.status?.refresh;
  if (!refresh?.status && !tstate.statusUnavailable) { el.hidden = true; return; }
  const patch = (tstate.mode === "traits" ? tflex.catalog?.patch : tstate.mode === "early" ? tearly.data?.patch : tstate.meta?.patch) || tstate.status?.patch;
  const target = refresh?.targetPatch;
  const when = refreshWhen(refresh);
  const lastCheck = when ? `Last automatic check: ${when.toLocaleString([], { dateStyle: "short", timeStyle: "short" })}.` : "";
  const current = patch ? `Saved TFT results for patch ${patch} remain available.` : "Current saved TFT results remain available.";
  let status = refresh?.status || "failed", title, detail;
  if (tstate.statusUnavailable) {
    status = "failed";
    title = "Update status is temporarily unavailable";
    detail = `${current} The page will check again shortly.`;
  } else if (status === "running") {
    const phase = refresh.phase || "";
    title = phase === "preparing-responses" ? "Preparing saved TFT results for display…"
      : phase === "publishing" ? "Publishing saved TFT results…"
      : /warm|calculat|comput/.test(phase) ? `Updating saved TFT results${target ? ` for patch ${target}` : ""}…`
      : "Checking for patch updates…";
    detail = `${current} TFT results update after the refresh completes.`;
  } else if (status === "needs-review") {
    title = target ? `Patch ${target} needs review` : "New patch data needs review";
    detail = `${current} ${refreshMessage(refresh) || "Some changes need checking before the updated TFT results can be used."}`;
  } else if (status === "failed") {
    title = refresh.transport?.exhausted && refresh.transport?.retryable
      ? "Patch download was interrupted" : "Automatic update did not finish";
    detail = `${current} The next scheduled check will retry.`;
  } else if (status === "waiting-not-before") {
    const retryAt = new Date(refresh.retryNotBefore);
    title = "Patch source requested a pause";
    detail = `${current} ${Number.isFinite(retryAt.getTime())
      ? `Downloads can resume after ${retryAt.toLocaleString([], { dateStyle: "short", timeStyle: "short" })}.`
      : "The next scheduled check will retry when the source is ready."}`;
  } else if (when && Date.now() - when.getTime() > 18 * 3.6e6) {
    status = "stale";
    title = "No recent automatic update check";
    detail = current;
  } else {
    title = "Checked for patch updates";
    detail = refreshMessage(refresh) || `Using patch ${patch}.`;
  }
  if (status !== "needs-review" && refresh?.reviewBlocker) {
    const blockedPatch = refresh.reviewBlocker.targetPatch;
    detail += ` ${blockedPatch ? `Patch ${blockedPatch}` : "Downloaded patch data"} still has an unresolved review from an earlier check.`;
  }
  el.hidden = false;
  el.dataset.state = status;
  document.getElementById("tft-refresh-title").textContent = title;
  document.getElementById("tft-refresh-detail").textContent = [detail, lastCheck, "Checks every 6 hours."].filter(Boolean).join(" ");
}

function tftRevision() { return tstate.meta?.revision || tstate.meta?.patch; }
function tftPublicationChanged(next, current) {
  return typeof next?.publicationRevision === "string" &&
    !!next.publicationRevision && next.publicationRevision !== current?.publicationRevision;
}

function scheduleTftStatus() {
  clearTimeout(tstate.timer);
  if (state.view === "tft" && !document.hidden) {
    const pending = tstate.mode === "leaderboard" ? tboard.pending || tboard.error
      : tstate.mode === "early" ? tearly.pending || tearly.error
      : tstate.mode === "traits" ? tflex.pending || tflex.error : tstate.pending;
    tstate.timer = setTimeout(pollTftStatus, pending ? 10000 : 60000);
  }
}

async function pollTftStatus() {
  clearTimeout(tstate.timer);
  if (state.view !== "tft" || document.hidden || !tstate.meta && !["traits", "early"].includes(tstate.mode)) return;
  if (tstate.polling) return tstate.polling;
  tstate.polling = (async () => {
    try {
      const st = await loadTftStatus();
      if (state.view !== "tft" || document.hidden) return;
      if (tstate.mode === "early") {
        const changed = !tearly.data || tftPublicationChanged(st, tearly.data) || st.patch && st.patch !== tearly.data.patch;
        if (!tearly.loading && (changed || tearly.pending || tearly.error)) await loadTftEarly({ force: true });
        return;
      }
      if (tstate.mode === "traits") {
        const changed = !tflex.catalog || tftPublicationChanged(st, tflex.catalog) ||
          st.patch && st.patch !== tflex.catalog.patch;
        if (!tflex.loading && (changed || tflex.pending || tflex.error)) await loadTftFlex({ force: true });
        return;
      }
      const changed = st.revision && tstate.meta.revision
        ? st.revision !== tstate.meta.revision : st.patch && st.patch !== tstate.meta.patch;
      const publicationChanged = tftPublicationChanged(st, tstate.meta);
      if ((changed || publicationChanged) && !await loadTftMeta(true)) return;
      if (state.view !== "tft" || document.hidden) return;
      if (tstate.mode === "leaderboard") {
        if (!tboard.loading && (!tboard.data || tboard.data.revision !== tftRevision() || publicationChanged || tboard.pending || tboard.error))
          await loadTftLeaderboard({ preserve: true });
      } else if (tstate.data && (tstate.dataRevision !== tftRevision() || publicationChanged)) {
        await loadTftScenario({ preserve: true });
      } else if (tstate.pending) {
        if (st.ready?.[`${tstate.unit}/${tkey()}`]) await loadTftScenario();
        else showTftPending(tstate.req);
      }
    } catch {
      tstate.statusUnavailable = true;
      renderTftRefresh();
    } finally {
      tstate.polling = null;
      scheduleTftStatus();
    }
  })();
  return tstate.polling;
}

// Mark unavailable builds without hiding a champion's name or description.
function markTftCold() {
  const ready = tstate.status?.ready;
  if (!ready) return;
  const mark = (id, keyOf) => document.querySelectorAll(`#${id} button`).forEach(b => {
    const cold = !ready[keyOf(b.dataset.key)];
    b.classList.toggle("cold", cold);
    if (!b.dataset.richTooltip) b.title = (b.dataset.title || "") + (cold ? " · not computed yet" : "");
  });
  mark("tft-unit-seg", k => `${k}/${tkey({ unit: k, star: tftStarFor(tstate.meta.units.find(u => u.slug === k)) })}`);
  mark("tft-star-seg", k => `${tstate.unit}/${tkey({ star: k })}`);
  mark("tft-geo-seg", k => `${tstate.unit}/${tkey({ geo: k })}`);
  mark("tft-trait-seg", k => `${tstate.unit}/${tkey({ traits: k })}`);
  mark("tft-threat-seg", k => `${tstate.unit}/${tkey({ threat: k })}`);
}

function clearTftCores(message = "Select a champion to compare two-item cores.", loading = false) {
  tcore.selected = null;
  document.getElementById("tft-core-panel").setAttribute("aria-busy", String(loading));
  document.getElementById("tft-core-status").textContent = message;
  const content = document.getElementById("tft-core-content");
  content.hidden = true;
  content.replaceChildren();
}

function tftCoreItems(names, apis = [], existing = []) {
  const list = document.createElement("div"), copies = new Map();
  list.className = "tft-core-items";
  existing.forEach(api => copies.set(api, (copies.get(api) || 0) + 1));
  names.forEach((name, i) => {
    const api = apis[i] || name, count = (copies.get(api) || 0) + 1;
    copies.set(api, count);
    const suffix = count === 2 ? " · 2nd copy" : count === 3 ? " · 3rd copy" : "";
    list.append(tftText("span", name + suffix, "tft-item"));
  });
  return list;
}

function tftComponentUse(apis) {
  const items = new Map((tstate.meta?.items || []).map(item => [item.api, item]));
  const counts = new Map();
  if (!apis?.length) return null;
  for (const api of apis) {
    const recipe = items.get(api)?.components;
    if (!Array.isArray(recipe) || recipe.length !== 2) return null;
    for (const component of recipe) {
      const entry = counts.get(component.api) || { ...component, count: 0 };
      ++entry.count;
      counts.set(component.api, entry);
    }
  }
  const components = [...counts.values()].sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  return { components, maxCopies: Math.max(...components.map(c => c.count)),
    repeatedPairs: components.reduce((sum, c) => sum + c.count * (c.count - 1) / 2, 0),
    signature: JSON.stringify([...counts].map(([api, c]) => [api, c.count]).sort()) };
}

function tftCoreComponents(apis) {
  const use = tftComponentUse(apis), line = tftText("span", "", "tft-core-components");
  if (use) {
    const short = { "B.F. Sword": "Sword", "Recurve Bow": "Bow", "Needlessly Large Rod": "Rod",
      "Tear Of The Goddess": "Tear", "Tear of the Goddess": "Tear", "Chain Vest": "Vest",
      "Negatron Cloak": "Cloak", "Giant's Belt": "Belt", "Sparring Gloves": "Glove" };
    line.textContent = use.components.map(c => `${c.count}× ${short[c.name] || c.name}`).join(" · ");
    line.title = use.components.map(c => `${c.count} × ${c.name}`).join(" · ");
  }
  return line;
}

function tftCoreCompletionOrder(core, threshold, varied = tcore.variedComponents) {
  const rows = core.completions || [];
  // Always anchor the preview to this core's actual best full build.
  if (!varied || rows.length < 2) return rows.slice();
  const bestUse = tftComponentUse(rows[0].itemApis);
  if (!bestUse) return rows.slice();
  const candidates = rows.slice(1).filter(row => tftCoreNear(row.performance, threshold))
    .map(row => ({ row, use: tftComponentUse(row.itemApis) })).filter(entry => entry.use)
    .sort((a, b) => a.use.maxCopies - b.use.maxCopies || a.use.repeatedPairs - b.use.repeatedPairs
      || a.row.performance.lossPct - b.row.performance.lossPct || rows.indexOf(a.row) - rows.indexOf(b.row));
  const preview = [rows[0]], recipes = new Set([bestUse.signature]);
  for (const { row, use } of candidates) {
    if (recipes.has(use.signature)) continue;
    preview.push(row);
    recipes.add(use.signature);
    if (preview.length === 3) break;
  }
  // Any unfilled slots and expanded rows keep their original performance order.
  return [...preview, ...rows.filter(row => !preview.includes(row))];
}

function tftCoreLoss(p) {
  if (!p || p.status === "unavailable") return "No comparable result";
  if (p.status === "noClear") return "Does not clear";
  if (p.status === "shortSurvival") return "Below the regular survival limit";
  if (p.status === "belowCap") return "Below the best survival limit";
  if (p.status !== "comparable" || !Number.isFinite(p.lossPct)) return "No comparable result";
  if (p.metric === "capped") return "Tied at both survival limits";
  if (p.lossPct === 0) return "No loss";
  const n = p.lossPct < .1 ? "<0.1" : p.lossPct.toFixed(1).replace(/\.0$/, "");
  const label = { killTime: "slower", damage: "less damage", aliveTime: "less hold time",
    stressAliveTime: "less hold time at 2×" }[p.metric] || "loss";
  return `${n}% ${label}`;
}

function tftCoreOutcome(p) {
  if (!p) return "—";
  if (["aliveTime", "stressAliveTime", "capped"].includes(p.metric)) {
    const duration = tstate.data?.scenario?.duration || 60;
    const held = (time, capped) => !capped && Number.isFinite(time) && time < duration && time.toFixed(1) === duration.toFixed(1)
      ? `<${duration}s` : tftHeldTime(time, capped, duration);
    const regular = `Holds ${held(p.aliveTime, p.survivalCapped)}`;
    return regular + (Number.isFinite(p.stressAliveTime)
      ? ` · ${held(p.stressAliveTime, p.stressCapped)} at 2× pressure` : "");
  }
  if (Number.isFinite(p.killTime)) return `Clears in ${p.killTime.toFixed(2)}s`;
  return Number.isFinite(p.total) ? `${fmtInt(Math.round(p.total))} damage · no clear` : "No result";
}

function tftCoreNear(p, threshold) {
  return p?.status === "comparable" && Number.isFinite(p.lossPct) && p.lossPct <= threshold + 1e-9;
}

function tftCoreRequirement(requiredFor, secondCopy = false) {
  const benchmark = { clear: "clear all enemies", survivalLimit: "reach the regular survival limit",
    bothSurvivalLimits: "reach both survival limits" }[requiredFor];
  return benchmark ? `${secondCopy ? "Second copy needed" : "Needed"} to ${benchmark}` : null;
}

function tftCoreExclusionVerdict(item, threshold, secondCopy = false) {
  const build = secondCopy ? item.fewerCopies : item.without;
  if (!build) return "No legal alternative to compare";
  const required = tftCoreRequirement(secondCopy ? item.copiesRequiredFor : item.requiredFor, secondCopy);
  if (required) return `${required} · percentage cost unknown`;
  if (secondCopy ? item.copiesEssential : item.essential)
    return secondCopy ? `Replacing the second copy loses more than ${threshold}%` : `Essential here: more than ${threshold}% to replace`;
  if (tftCoreNear(build.performance, threshold))
    return secondCopy ? "Second copy can be replaced within the range" : "Can be replaced within the range";
  return "No comparable replacement cost";
}

function tftCoreTable(headers, rows, label) {
  const wrap = document.createElement("div"), table = document.createElement("table");
  wrap.className = "tft-core-tablewrap";
  wrap.tabIndex = 0;
  wrap.setAttribute("role", "region");
  wrap.setAttribute("aria-label", label);
  table.className = "tft-core-table";
  const head = document.createElement("thead"), tr = document.createElement("tr");
  tr.append(...headers.map(title => {
    const th = tftText("th", title);
    th.scope = "col";
    return th;
  }));
  head.append(tr);
  const body = document.createElement("tbody");
  body.append(...rows);
  table.append(head, body);
  wrap.append(table);
  return wrap;
}

function renderTftCores() {
  const d = tstate.data, a = d?.coreAnalysis;
  const panel = document.getElementById("tft-core-panel");
  panel.hidden = a ? a.coreSize !== 2 : d?.objective === "tank";
  if (panel.hidden) {
    clearTftCores();
    return;
  }
  if (!a) {
    clearTftCores("Core item comparisons are not available for these results yet.");
    return;
  }
  if (a.status !== "ok") {
    clearTftCores(a.status === "noDamage"
      ? "No build deals damage in this fight, so there is no meaningful item core to recommend."
      : "There are no complete builds to compare for this fight.");
    return;
  }
  document.getElementById("tft-core-panel").setAttribute("aria-busy", "false");
  document.getElementById("tft-core-status").textContent =
    `${fmtInt(a.buildsEvaluated)} complete three-item builds compared · ${fmtInt(a.nearBuildCount)} within ${a.thresholdPct}% of the best.`;
  const content = document.getElementById("tft-core-content"), sc = d.scenario;
  content.hidden = false;
  const trait = { bare: "no traits", low: "first trait breakpoint", high: "max non-prismatic traits" }[sc.traits] || sc.traits;
  const threat = d.objective === "tank" && tstate.meta.tankThreats?.find(t => t.key === sc.threat);
  const context = tftText("p", `${d.unitName} · ${sc.star}★ · ${sc.geometry === "clump" ? "clumped" : "spread"} · ${trait}${threat ? ` · ${threat.label}` : ""}`, "tft-core-context");
  const optimal = document.createElement("section");
  optimal.className = "tft-core-optimal";
  if (a.optimal) {
    optimal.append(tftText("strong", "Optimal full build"),
      tftCoreItems(a.optimal.items, a.optimal.itemApis), tftCoreComponents(a.optimal.itemApis),
      tftText("p", `${tftCoreOutcome(a.optimal.performance)} · ${tftCoreLoss(a.optimal.performance)}`));
  } else optimal.hidden = true;
  if (!a.candidates?.length) {
    tcore.selected = null;
    content.replaceChildren(context, optimal, tftText("p", a.coreSize === 2 && a.minCompletions > 1
      ? `No pair has ${a.minCompletions} third-item choices within ${a.thresholdPct}% of optimal in this fight.`
      : "No flexible core qualifies in this fight.", "tft-core-note"));
    return;
  }
  const insight = document.createElement("div");
  insight.className = "tft-core-insight";
  const essential = a.essentialItems || [], required = a.requiredItems || [];
  insight.append(tftText("p", essential.length
    ? `Essential here: ${essential.map(item => item.item).join(" + ")}. Removing all copies of any one of these puts even the best alternative outside the ${a.thresholdPct}% range.`
    : required.length ? `No item has a measured replacement loss above ${a.thresholdPct}% here.`
    : `No single item is essential here. For each item, a build within ${a.thresholdPct}% of the best exists without it.`));
  if (required.length) insight.append(tftText("p",
    required.map(item => `${item.item}: ${tftCoreRequirement(item.requiredFor)?.toLowerCase() || "needed for the best fight outcome"}.`).join(" ")
      + " Alternatives fall short of that outcome; their percentage cost is unknown."));
  const cores = a.qualifyingCoreCount === 1 ? "pair" : "pairs";
  const minimum = a.minCompletions || 1;
  const spikeSelection = a.selectionMode === "twoItemSpike" && a.coreSize === 2
    && Number.isFinite(a.pairBuildsEvaluated) && Number.isFinite(a.groupedCoreCount)
    && a.candidates.every(core => core.spike
      && (Number.isFinite(core.spike.killTime) || Number.isFinite(core.spike.total)));
  if (spikeSelection) insight.append(tftText("p", "The strongest two-item start comes first. Pairs that share a strong three-item finish are grouped."));
  if (a.metric === "damage") insight.append(tftText("p", "No build clears all enemies here. These comparisons use total damage dealt."));
  if (["stressAliveTime", "capped"].includes(a.metric)) insight.append(tftText("p", a.metric === "capped"
    ? "The best builds reach both survival limits and remain tied. Builds below a limit are shown by their held time."
    : "The best builds reach the regular survival limit, so their remaining strength is compared at twice the incoming damage."));
  const label = tftText("p", "Choose a two-item core", "tft-label");
  label.id = "tft-core-choice-label";
  const choices = document.createElement("div");
  choices.id = "tft-core-choices";
  choices.className = "tft-core-choices";
  choices.setAttribute("role", "group");
  choices.setAttribute("aria-labelledby", label.id);
  if (!a.candidates.some(c => c.id === tcore.selected)) tcore.selected = a.candidates[0].id;
  choices.append(...a.candidates.map(core => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tft-core-choice";
    button.dataset.core = core.id;
    button.setAttribute("aria-pressed", String(core.id === tcore.selected));
    button.setAttribute("aria-controls", "tft-core-detail");
    button.append(tftText("strong", core.items.join(" + ")),
      tftText("span", spikeSelection
        ? `2 items: ${tftCoreOutcome(core.spike).replace(/^Clears/, "clears")}`
        : `Best full build: ${tftCoreLoss(core.best.performance).toLowerCase()}`),
      tftText("span", `${fmtInt(core.nearCount)} ${a.coreSize === 2 ? "third item" : "completion"}${core.nearCount === 1 ? "" : "s"} within ${a.thresholdPct}%`));
    button.addEventListener("click", () => {
      tcore.selected = core.id;
      choices.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.core === core.id)));
      renderTftCoreDetail();
    });
    return button;
  }));
  const detail = document.createElement("div");
  detail.id = "tft-core-detail";
  const method = document.createElement("details");
  method.className = "tft-details";
  method.append(tftText("summary", "How core strength and flexibility are measured"),
    tftText("p", spikeSelection
      ? `${fmtInt(a.pairBuildsEvaluated)} legal two-item pairs were simulated on their own, in the selected fight. Of ${fmtInt(a.qualifyingCoreCount)} ${cores} with at least ${minimum} third-item ${minimum === 1 ? "choice" : "choices"} within ${a.thresholdPct}% of the best full build, ${fmtInt(a.groupedCoreCount)} distinct ${a.groupedCoreCount === 1 ? "core remains" : "cores remain"} after grouping; showing ${a.candidates.length}. Pairs are ordered by their actual two-item clear time, or damage when they do not clear, then by flexibility. A weaker pair is grouped with an earlier pair if they share any three-item finish within the range.`
      : `${fmtInt(a.qualifyingCoreCount)} ${cores} can finish within ${a.thresholdPct}% of the best; showing ${a.candidates.length}. These cores are ordered by the number of strong completions, then by their strongest completion.${a.coreSize === 2 ? " Two-item fight results are not available for these results." : ""}`),
    tftText("p", `Completion results, replacement costs and comparisons across other fights all use full three-item builds. Each core holds two slots; the third item can vary. Every legal completion is counted, including repeated items. Counts within ${a.thresholdPct}% of the best full build measure flexibility, not item drop chances.`),
    tftText("p", `The preview always starts with the selected core’s best completion. When varied components are preferred, the next two slots favor different recipes within ${a.thresholdPct}% of the optimal full build: first fewer copies of the most repeated component, then fewer repeated component pairs overall, then performance. Any remaining slots and the expanded list keep performance order. This does not constrain inventory or change the optimal build or core selection.`),
    tftText("p", "Clear-time loss is 100 × (time ÷ best time − 1). Damage or hold-time loss is 100 × (1 − result ÷ best result). Carries and fighters use clear time when any build clears, otherwise total damage. Tanks use time holding enemies, then a second test at twice the damage for builds that reach the regular limit. Reaching both limits is a tie; failing a required clear or survival limit is reported directly."),
    tftText("p", `An item is essential in this fight when excluding every copy and choosing the best new three-item build has a measured loss above ${a.thresholdPct}%. Only matching clear or survival outcomes yield a percentage comparison. If every alternative fails to clear or reach a required survival limit, the item is labeled as needed for that outcome; its percentage cost is unknown. A duplicate core also tests builds with at most one copy, to distinguish the value of the second copy. These findings apply to the selected fight.`));
  content.replaceChildren(context, optimal, insight, label, choices, detail, method);
  renderTftCoreDetail();
}

function renderTftCoreDetail() {
  const a = tstate.data?.coreAnalysis;
  if (a?.coreSize !== 2) return;
  const core = a?.candidates.find(c => c.id === tcore.selected);
  if (!core) return;
  const detail = document.getElementById("tft-core-detail");
  const flex = document.createElement("section");
  flex.className = "tft-core-section";
  if (tftComponentUse(core.best?.itemApis)) {
    const label = document.createElement("label"), toggle = document.createElement("input");
    label.className = "tft-core-component-toggle";
    toggle.type = "checkbox";
    toggle.id = "tft-core-varied-components";
    toggle.checked = tcore.variedComponents;
    toggle.addEventListener("change", () => {
      tcore.variedComponents = toggle.checked;
      renderTftCoreDetail();
      document.getElementById(toggle.id)?.focus({ preventScroll: true });
    });
    label.append(toggle, document.createTextNode(`Prefer varied components within ${a.thresholdPct}% of optimal`));
    label.title = "The best completion stays first. Alternatives favor fewer copies of any one component, then fewer repeated components overall. This is a recipe preference, not an item-drop probability.";
    flex.append(label);
  }
  const completionTable = completions => tftCoreTable(
    ["Full build", "Fight result", "Loss vs optimal"],
    completions.map(build => {
      const row = document.createElement("tr"), items = cell("");
      items.append(tftCoreItems([...core.items, ...build.flexItems], [...core.itemApis, ...build.flexApis]),
        tftCoreComponents(build.itemApis));
      row.dataset.near = String(tftCoreNear(build.performance, a.thresholdPct));
      row.append(items, cell(tftCoreOutcome(build.performance)), cell(tftCoreLoss(build.performance)));
      return row;
    }), `Completions for ${core.items.join(" + ")}`);
  const visibleCount = 3;
  const completions = tftCoreCompletionOrder(core, a.thresholdPct);
  const preview = completionTable(completions.slice(0, visibleCount));
  preview.classList.add("tft-core-preview");
  flex.append(preview);
  if (core.completions.length > visibleCount) {
    const more = document.createElement("details");
    more.className = "tft-details";
    more.append(tftText("summary", `Show the remaining ${fmtInt(core.completions.length - visibleCount)} completions`),
      tftText("p", `${fmtInt(core.nearCount)} of ${fmtInt(core.totalCount)} completions finish within ${a.thresholdPct}% of the best three-item build. Loss compares the complete build with the best build in this fight.`, "tft-core-note"));
    more.addEventListener("toggle", () => {
      if (more.open && !more.querySelector("table")) more.append(completionTable(completions.slice(visibleCount)));
    });
    flex.append(more);
  }
  const consistency = document.createElement("section");
  consistency.id = "tft-core-consistency";
  consistency.className = "tft-core-section";
  detail.replaceChildren(flex, consistency);
  loadTftCoreScenarios();
}

async function loadTftCoreScenarios(refresh = false) {
  const unit = tstate.unit, revision = tstate.dataRevision, req = tstate.req;
  const key = JSON.stringify([unit, revision]);
  let entry = tcore.summaries.get(key);
  if (!entry || refresh) {
    // A summary includes every core in every fight; retain only recent champions.
    if (!entry && tcore.summaries.size >= 6) tcore.summaries.delete(tcore.summaries.keys().next().value);
    entry = { data: null, loading: true, error: false };
    tcore.summaries.set(key, entry);
    entry.promise = api(`api/tft/${encodeURIComponent(unit)}/cores.json`, { cache: "no-store" })
      .then(data => {
        if (data.unit !== unit || data.revision !== revision) throw new Error("Core comparison revision changed");
        entry.data = data;
      }).catch(() => { entry.error = true; })
      .finally(() => { entry.loading = false; });
  } else {
    tcore.summaries.delete(key);
    tcore.summaries.set(key, entry);
  }
  renderTftCoreConsistency(entry);
  await entry.promise;
  if (req !== tstate.req || unit !== tstate.unit || revision !== tstate.dataRevision || revision !== tftRevision()
    || tcore.summaries.get(key) !== entry) return;
  renderTftCoreConsistency(entry);
}

function renderTftCoreConsistency(entry) {
  const el = document.getElementById("tft-core-consistency");
  const a = tstate.data?.coreAnalysis, core = a?.candidates.find(c => c.id === tcore.selected);
  if (!el || !core) return;
  el.replaceChildren(tftText("h4", "Does this core hold up in other fights?"));
  if (entry.loading) {
    el.append(tftText("p", "Comparing star levels, traits and enemy setups…", "tft-core-note"));
    return;
  }
  const retry = text => {
    const button = tftText("button", text, "tft-core-retry");
    button.type = "button";
    button.addEventListener("click", () => loadTftCoreScenarios(true));
    return button;
  };
  if (entry.error || !entry.data) {
    el.append(tftText("p", "Other fight comparisons could not be loaded. The core results above apply to the selected fight.", "tft-core-note"), retry("Retry other fights"));
    return;
  }
  const d = entry.data, scenarios = d.scenarios || [];
  const comparable = scenarios.filter(sc => sc.coreStats?.[core.id]?.totalCount > 0
    && sc.coreStats[core.id].best?.status !== "unavailable");
  const near = comparable.filter(sc => sc.coreStats[core.id].nearCount > 0).length;
  const missing = Math.max(0, d.totalScenarios - scenarios.length), unavailable = scenarios.length - comparable.length;
  el.append(tftText("p", comparable.length
    ? `${near} of ${comparable.length} comparable fights have a completion within ${d.thresholdPct}% of that fight’s best.`
    : "No comparable fights are available yet.", "tft-core-note"));
  const coverage = [`${scenarios.length} of ${d.totalScenarios} fights checked.`];
  if (missing) coverage.push(`${missing} fights are not available yet.`);
  if (unavailable) coverage.push(`${unavailable} checked fights have no comparable result for this core.`);
  coverage.push("The remaining items can change in each fight.");
  el.append(tftText("p", coverage.join(" "), "tft-core-note"));
  if (scenarios.length) {
    const details = document.createElement("details");
    details.className = "tft-details";
    details.append(tftText("summary", "Compare star levels, traits and enemy setups"));
    details.append(tftCoreTable(["Fight", "Best with this core", "Completions within range", "Loss vs that fight’s best"], scenarios.map(sc => {
      const stats = sc.coreStats?.[core.id], row = document.createElement("tr");
      const label = sc.label || `${sc.star}★ · ${sc.geometry} · ${sc.traits}${sc.threat && sc.threat !== "mixed" ? ` · ${sc.threat}` : ""}`;
      row.dataset.near = String(stats?.nearCount > 0);
      const current = sc.key === tkey();
      if (current) row.setAttribute("aria-current", "true");
      row.append(cell(label + (current ? " · current" : "")), cell(tftCoreOutcome(stats?.best)),
        cell(stats ? `${fmtInt(stats.nearCount)} of ${fmtInt(stats.totalCount)}` : "Unavailable"), cell(tftCoreLoss(stats?.best)));
      return row;
    }), "Core strength across fights"));
    el.append(details);
  }
  if (missing) el.append(retry("Refresh other fights"));
}

function clearTft() {
  tstate.data = null;
  clearTftCores();
  document.querySelector("#ttbl thead tr").replaceChildren();
  document.querySelector("#ttbl tbody").replaceChildren();
  document.getElementById("tft-scenario-details").hidden = true;
  document.getElementById("tft-desc").textContent = "";
  document.getElementById("tft-show-stats").disabled = true;
  document.getElementById("tbd-title").textContent = "Damage breakdown";
  document.getElementById("tbd-hint").textContent = "";
  document.getElementById("tbd-bars").replaceChildren();
}

async function loadTftScenario({ preserve = false, navigate = false } = {}) {
  if (!tstate.unit) {
    document.getElementById("tft-note").hidden = false;
    document.getElementById("tft-note").textContent = "No champions are available in this snapshot.";
    return;
  }
  tftActive("tft-unit-seg", tstate.unit);
  tftActive("tft-star-seg", String(tstate.star));
  tftActive("tft-geo-seg", tstate.geo);
  tftActive("tft-trait-seg", tstate.traits);
  const unit = tstate.meta.units.find(u => u.slug === tstate.unit);
  renderTftThreat(unit);
  renderTftSelected(unit);
  renderTftFight(unit);
  document.getElementById("tft-geo-hint").textContent =
    (tstate.geo === "clump" ? "Frontliners grouped; backliners stand farther behind. "
      : "Frontliners separated; backliners stand farther behind. ") +
    "Spell coverage depends on radius and targeting; a wide enough radius can reach backliners.";
  document.getElementById("tft-trait-hint").textContent = {
    bare: "No trait bonuses applied.", low: "Each modeled trait at its first active breakpoint.",
    high: "Each modeled trait at its highest non-prismatic breakpoint."
  }[tstate.traits] || "";
  markTftCold();
  if (navigate) tstate.sel = 0;
  updateHash(navigate);
  const req = ++tstate.req;
  const revision = tftRevision();
  const requestedUnit = tstate.unit, requestedKey = tkey();
  const previous = preserve && tstate.data?.unit === requestedUnit && tstate.data?.scenario?.key === requestedKey
    ? tstate.data : null;
  const selectedRow = tstate.sel;
  tstate.pending = false;
  if (!previous) clearTft();
  clearTftCores(previous ? "Updating core item comparisons…" : "Loading core item comparisons…", true);
  const formation = tstate.geo === "clump" ? "clumped" : "spread";
  const traitLabel = { bare: "no traits", low: "first trait breakpoint", high: "max non-prismatic traits" }[tstate.traits];
  const threat = unit.objective === "tank" && tstate.meta.tankThreats?.find(t => t.key === tstate.threat);
  document.getElementById("tft-title").textContent = `${unit.name} — ${tstate.star}★ builds · ${formation} · ${traitLabel}${threat ? ` · ${threat.label}` : ""}`;
  const desc = document.getElementById("tft-desc");
  const summary = document.getElementById("tft-result-summary");
  summary.textContent = previous ? `Updating ${unit.name}'s builds…` : `Loading ${unit.name}'s builds…`;
  document.getElementById("tft-results").setAttribute("aria-busy", "true");
  let d;
  try {
    d = await api(`api/tft/${requestedUnit}/${requestedKey}.json`, { cache: "no-store" });
    if (req !== tstate.req) return;
    if (!d.pending && (d.unit !== requestedUnit || d.unitApi !== unit.api ||
        d.scenario?.key !== requestedKey || d.objective !== unit.objective))
      throw new Error("Champion build response does not match the selected fight");
  } catch (e) {
    if (req !== tstate.req) return;
    if (!previous) clearTft();
    clearTftCores("Core item comparisons could not be loaded for this fight.");
    document.getElementById("tft-results").setAttribute("aria-busy", "false");
    summary.textContent = previous
      ? "Updated builds could not be loaded yet. Showing the previous results; the page will retry shortly."
      : "Could not load these builds. Select a champion or fight setting to retry.";
    scheduleTftStatus();
    return;
  }
  if (req !== tstate.req) return;
  document.getElementById("tft-results").setAttribute("aria-busy", "false");
  if (d.pending) {
    if (!previous) clearTft();
    showTftPending(req);
    return;
  }
  const selectedItems = previous?.rows[tstate.sel]?.items;
  tstate.data = d;
  tstate.dataRevision = revision;
  tstate.sel = selectedItems ? Math.max(0, d.rows.findIndex(r => JSON.stringify(r.items) === JSON.stringify(selectedItems)))
    : Math.max(0, Math.min(selectedRow, d.rows.length - 1));
  updateHash();
  document.getElementById("tft-show-stats").disabled = false;
  document.getElementById("tft-scenario-details").hidden = false;
  const sc = d.scenario;
  renderTftFight(unit, sc, d.rows[tstate.sel] || d.best);
  const ranking = d.objective === "tank" ? `Ranked by survival; ${sc.duration}s+ builds compared at 2× incoming damage`
    : d.objective === "fighter" ? "Ranked by kill time, then damage before dying" : "Ranked by kill time, then damage";
  summary.textContent = d.rows.length
    ? `${fmtInt(d.rows.length)} of ${fmtInt(d.buildsEvaluated)} builds · ${ranking}. Select a row for ${d.objective === "tank" ? "survival and damage details" : "its damage breakdown"}.`
    : "No ranked builds are available for this fight.";
  const when = d.computedAt
    ? new Date(d.computedAt).toLocaleString([], { dateStyle: "short", timeStyle: "short" })
    : null;
  const traits = sc.traitsActive.length
    ? sc.traitsActive.map(t => `${t.trait} ${t.breakpoint}`).join(", ")
    : "none";
  const objective = d.pressureByBuild
    ? "Pressure depends on the equipped form: AD builds receive attacks and casts; AP builds are protected."
    : (tstate.meta.objectives || {})[d.objective] || d.objective;
  desc.replaceChildren(
    tftText("p", `${d.unitName} at ${sc.star}★${d.pressureByBuild ? " · role depends on equipped form" : ` · ${d.role}`}. ${objective}${objective.endsWith(".") ? "" : "."}`),
    ...(d.objective !== "tank" ? [tftText("p", tftTargetDebuffNote(sc.dummy?.targetDebuffs))] : []),
    ...(d.objective === "tank" ? [
      tftText("p", `${threat?.label || "Mixed"} enemy damage. ${tftTankDebuffNote(sc.dummy?.enemyDebuffs || tstate.meta.tankDebuffs)}`),
      tftText("p", `Survival includes on-death bodies. ${sc.duration}s+ means the unit or its body is still holding enemies at the fight limit. These builds are compared with twice the incoming damage for the same ${sc.duration}s; builds that reach both limits remain tied.`),
      tftText("p", "Opening physical/magic EHP uses armor and MR after enemy sunder and shred, plus starting durability. It measures ability damage capacity and excludes heals, shields and attack-only protection. The Armor/MR column also shows reduced values. Effective healing and shields consumed are measured during the regular fight. Team utility is shown separately and does not change the survival ranking."),
    ] : []),
    tftText("p", `Trait bonuses applied: ${traits}.`),
    ...(sc.traitsUnmodeled.length ? [tftText("p", `Traits not included: ${sc.traitsUnmodeled.join(", ")}.`)] : []),
    ...(sc.notes || []).map(note => tftText("p", note)),
    tftText("p", `${fmtInt(d.buildsEvaluated)} builds evaluated · Set ${d.set}, patch ${d.patch}` +
      (when ? ` · calculated ${when} in ${d.computeSeconds}s.` : ".")));
  if (unit.note) {
    const assumptions = document.createElement("details");
    assumptions.append(tftText("summary", "Champion ability assumptions"), tftText("p", unit.note));
    desc.append(assumptions);
  }
  renderTftTable();
  renderTftCores();
  scheduleTftStatus();
}

function showTftPending(req) {
  if (req !== tstate.req) return;
  const st = tstate.status;
  tstate.pending = true;
  const why = st?.refresh?.status === "running"
    ? "A scheduled TFT refresh is running. Saved builds will appear after it completes."
    : {
    running: "Background TFT calculations are running. Saved builds will appear after a completed scheduled refresh.",
    idle: "Saved builds will appear after a completed scheduled refresh.",
    failed: "The last TFT refresh did not finish. Saved builds will appear after a completed scheduled refresh.",
    stale: "The dashboard needs to restart to finish applying an update.",
  }[st?.refresh?.status === "failed" ? "failed" : st?.warmer] || "Saved builds will appear after a completed scheduled refresh.";
  document.getElementById("tft-result-summary").textContent = (tstate.data ? "Showing the previous results. " : "") + why;
  clearTftCores("Core item comparisons will appear when these builds are ready.");
  scheduleTftStatus();
}

function tftHeldTime(time, capped, duration = tstate.data?.scenario?.duration || 60) {
  if (!Number.isFinite(time)) return "—";
  return capped ? `${duration}s+` : `${time.toFixed(1)}s`;
}

function tftColumns() {
  const d = tstate.data, objective = d.objective || "carry";
  const pressured = objective === "fighter" || d.pressureByBuild || d.rows.some(row => row.pressure === true);
  const itemApis = new Map((tstate.meta?.items || []).map(item => [item.name, item.api]));
  const items = { th: "Items", title: "Component counts below each build include all three items.", td: r => {
    const td = cell("");
    const list = document.createElement("div");
    list.className = "tft-item-list";
    r.items.forEach(name => {
      const item = document.createElement("span");
      item.className = "tft-item";
      item.textContent = name;
      list.append(item);
    });
    td.append(list);
    if (r.form) td.append(` ${r.form} form`);
    const components = tftCoreComponents(r.items.map((name, i) => r.itemApis?.[i] || itemApis.get(name)));
    if (!components.textContent) components.textContent = "Components unavailable";
    td.append(components);
    return td;
  } };
  const kill = { th: "Kill time", title: "Time until all enemy targets are dead; the rest are ranked by damage dealt",
    td: r => {
      const td = cell(r.killTime != null ? r.killTime.toFixed(2) + "s" : (r.died ? "died" : "survive"));
      if (r.killTime == null) td.title = `${r.left.map(fmtInt).join(" / ")} HP left on the dummies`;
      return td;
    } };
  const held = { th: objective === "tank" ? "Held" : "Alive", title: objective === "tank"
      ? `How long the unit or its on-death body held enemies. ${d.scenario.duration}s+ means it reached the fight limit.`
      : "How long the unit lived under the dummies' attacks",
    td: r => {
      if (r.pressure === false) return cell("Protected");
      const td = cell(objective === "tank" ? tftHeldTime(r.aliveTime, r.survivalCapped)
        : r.died ? `${r.diedAt.toFixed(1)}s ✝` + (r.aliveTime > r.diedAt ? ` (held ${r.aliveTime.toFixed(1)}s)` : "")
                             : `${r.aliveTime.toFixed(0)}s`);
      td.title = r.died ? `Died at ${r.diedAt}s` + (r.aliveTime > r.diedAt
        ? `; its body ${r.survivalCapped ? "was still holding enemies at the fight limit" : `held enemies until ${r.aliveTime}s`}` : "")
        : `Alive with ${fmtInt(r.hpLeft)} HP`;
      return td;
    } };
  const stress = objective === "tank" && d.rows.some(r => r.stressAliveTime != null) ? [{
    th: "2× pressure", title: `Survival with twice the incoming damage, for builds reaching ${d.scenario.duration}s in the regular fight. Builds reaching both limits remain tied.`,
    td: r => cell(tftHeldTime(r.stressAliveTime, r.stressCapped)),
  }] : [];
  const ehp = { th: "EHP Phys/Magic", title: "Opening effective HP against physical / magic ability damage, after enemy sunder and shred. Includes starting durability; excludes healing, shields and attack-only protection.",
    td: r => cell(r.physicalEhp != null && r.magicEhp != null ? `${fmtInt(r.physicalEhp)} / ${fmtInt(r.magicEhp)}` : "—") };
  const dmg = { th: "Damage", td: r => cell(fmtInt(r.total)) };
  const dps = { th: "DPS", td: r => cell(fmtInt(r.dps)) };
  const stats = [
    { th: "AD", td: r => cell(fmtInt(r.ad)) },
    { th: "AP", td: r => cell(fmtInt(r.ap)) },
    { th: "AS", td: r => cell(r.attackSpeed.toFixed(2)) },
    { th: "Crit", td: r => cell(r.crit + "%" + (r.precision ? " ✦" : "")) },
  ];
  const body = [
    { th: "HP", title: "Max health with items and traits", td: r => cell(Number.isFinite(r.hp) ? fmtInt(r.hp) : "—") },
    { th: "Armor/MR", title: objective === "tank" ? "Opening armor and MR after enemy sunder and shred" : "Armor and magic resist",
      td: r => cell(Number.isFinite(r.armor) && Number.isFinite(r.mr) ? `${fmtInt(r.armor)}/${fmtInt(r.mr)}` : "—") },
    { th: "Dur", title: "Durability: post-mitigation damage prevented", td: r => cell(r.durability ? r.durability + "%" : "–") },
  ];
  const soak = [
    { th: "Absorbed", title: "Pre-mitigation damage the unit (and its body) took", td: r => cell(Number.isFinite(r.absorbed) ? fmtInt(r.absorbed) : "—") },
    { th: "Healed", title: objective === "tank" ? "Health actually restored after enemy heal cut; excludes overhealing" : "Health actually restored; excludes overhealing",
      td: r => cell(Number.isFinite(r.healed) ? fmtInt(r.healed) : "—") },
    { th: "Shielded", title: "Damage that landed on shields", td: r => cell(Number.isFinite(r.shielded) ? fmtInt(r.shielded) : "—") },
    { th: "Denied", title: "Damage the dummies could not land while stunned or the unit was untargetable",
      td: r => cell(fmtInt(r.denied)) },
  ];
  const ally = { th: "Allies", title: "Potential healing / shielding for allies. Their health and survival are not simulated; these totals do not affect the survival ranking.",
    td: r => cell((r.allyHeal || r.allyShield) ? `${fmtInt(r.allyHeal)} / ${fmtInt(r.allyShield)}` : "–") };
  const rank = { th: "#", td: r => cell(String(r.rank)) };
  const counts = [{ th: "Casts", td: r => cell(String(r.casts)) }, { th: "Attacks", td: r => cell(String(r.attacks)) }];
  if (!document.getElementById("tft-show-stats").checked) {
    if (objective === "tank") return [rank, items, held, ...stress, ehp, ...soak.slice(1, 3), counts[0], soak[3]];
    if (pressured) return [rank, items, kill, dmg, dps, held, ...(d.pressureByBuild ? [body[0]] : [])];
    return [rank, items, kill, dps, dmg];
  }
  if (objective === "tank") return [rank, items, held, ...stress, ehp, ...soak, ally, ...body, ...counts, dmg, ...stats];
  if (pressured) return [rank, items, kill, dmg, dps, held, ...soak.slice(0, 3), ...body, ...counts, ...stats];
  return [rank, items, kill, dps, dmg, ...counts, ...stats];
}

function renderTftTable() {
  const rows = tstate.data.rows, cols = tftColumns();
  document.querySelector("#ttbl thead tr").replaceChildren(...cols.map(c => {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = c.th;
    if (c.title) th.title = c.title;
    return th;
  }));
  const body = document.querySelector("#ttbl tbody");
  body.replaceChildren(...rows.map((r, i) => {
    const tr = document.createElement("tr");
    tr.tabIndex = 0;
    tr.setAttribute("aria-label", `Build ${r.rank}: ${r.items.join(", ")}. Show ${tstate.data.objective === "tank" ? "survival and damage details" : "damage breakdown"}.`);
    if (i === tstate.sel) { tr.className = "sel"; tr.setAttribute("aria-current", "true"); }
    tr.append(...cols.map(c => c.td(r)));
    const select = () => {
      tstate.sel = i;
      [...body.children].forEach((row, j) => {
        row.classList.toggle("sel", j === i);
        if (j === i) row.setAttribute("aria-current", "true");
        else row.removeAttribute("aria-current");
      });
      renderTftBreakdown(r);
      updateHash(true);
    };
    tr.addEventListener("click", select);
    tr.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(); }
    });
    return tr;
  }));
  renderTftBreakdown(rows[tstate.sel]);
}

function renderTftBreakdown(r) {
  if (!r) return;
  const tank = tstate.data.objective === "tank";
  const unit = tstate.meta?.units.find(unit => unit.slug === tstate.data.unit);
  if (unit && typeof r.pressure === "boolean") {
    renderTftFight(unit, tstate.data.scenario, r);
    document.getElementById("tft-selected-meta").textContent =
      `${unit.cost} cost · ${r.role || unit.role} · ${r.form ? `${r.form} form · ${r.range} range` : unit.ability}`;
  }
  document.getElementById("tbd-title").textContent =
    `${tank ? "Build details" : "Damage breakdown"} — #${r.rank}: ${r.items.join(", ")}`;
  const fight = r.killTime != null ? `all enemies defeated at ${r.killTime.toFixed(2)}s`
                                   : `the dummies survive with ${r.left.map(fmtInt).join(" / ")} HP left`;
  const body = r.aliveTime == null ? "" :
    ` · ${r.died ? `died at ${r.diedAt}s` : `alive with ${fmtInt(r.hpLeft)} of ${fmtInt(r.hp)} HP`}` +
    (r.died && r.aliveTime > r.diedAt ? `, its body held on until ${r.aliveTime}s` : "") +
    ` · took ${fmtInt(r.absorbed)} pre-mitigation (${fmtInt(r.taken)} through ${fmtInt(r.armor)} armor / ${fmtInt(r.mr)} MR` +
    (r.durability ? ` / ${r.durability}% durability` : "") + `), ${fmtInt(r.shielded)} on shields, healed ${fmtInt(r.healed)}` +
    (r.denied ? `, denied ${fmtInt(r.denied)} (${r.ccTime}s of stuns)` : "") +
    (r.allyHeal || r.allyShield ? `, allies healed ${fmtInt(r.allyHeal)} / shielded ${fmtInt(r.allyShield)}` : "");
  const stats = `${fmtInt(r.ad)} AD · ${fmtInt(r.ap)} AP · ${r.attackSpeed} attack speed · ${r.crit}% crit` +
    (r.precision ? " with Precision" : "") + (r.form ? ` · ${r.form} form` : "") +
    (typeof r.pressure === "boolean" ? r.pressure ? " · takes incoming damage" : " · protected" : "") +
    ` · ${fmtInt(r.total)} damage · ${fmtInt(r.dps)} DPS · ` +
    `${r.attacks} attacks · ${r.casts} casts`;
  const movement = !tank && tstate.data.scenario?.dummy?.meleeRepositionSeconds > 0
    ? tftMovementSummary(r, r.range) : "";
  const hint = document.getElementById("tbd-hint");
  if (tank) {
    const survival = `${tftHeldTime(r.aliveTime, r.survivalCapped)} holding enemies` +
      (r.stressAliveTime != null ? ` · ${tftHeldTime(r.stressAliveTime, r.stressCapped)} at 2× pressure` : "");
    const status = r.died ? `Died at ${r.diedAt}s` + (r.aliveTime > r.diedAt
      ? r.survivalCapped ? "; its body was still holding enemies at the fight limit" : `; its body held enemies until ${r.aliveTime}s`
      : "") : `Alive with ${fmtInt(r.hpLeft)} of ${fmtInt(r.hp)} HP`;
    hint.replaceChildren(
      tftText("p", `${survival}. ${status}.`),
      tftText("p", `Opening EHP: ${fmtInt(r.physicalEhp)} physical / ${fmtInt(r.magicEhp)} magic ability damage. ${fmtInt(r.armor)} armor / ${fmtInt(r.mr)} MR after enemy sunder and shred` +
        (r.durability ? ` · ${r.durability}% starting durability` : "") + ". EHP excludes healing, shields and attack-only protection."),
      tftText("p", `Regular fight: ${fmtInt(r.healed)} effective healing after heal cut · ${fmtInt(r.shielded)} damage absorbed by shields · ${r.casts} casts · ${fmtInt(r.absorbed)} damage received before defenses (${fmtInt(r.taken)} after defenses)` +
        (r.denied ? ` · ${fmtInt(r.denied)} damage denied (${r.ccTime}s of stuns)` : "") + "."),
      ...(r.allyHeal || r.allyShield ? [tftText("p", `Potential team utility: ${fmtInt(r.allyHeal)} ally healing / ${fmtInt(r.allyShield)} ally shielding. Ally health and survival are not simulated; these totals do not affect the ranking.`)] : []),
      tftText("p", `Damage breakdown for the regular fight: ${stats}.`));
  } else {
    hint.textContent = `${stats}${movement ? ` · ${movement}` : ""} · ${fight}${body}`;
  }
  const entries = Object.entries(r.breakdown);
  const max = Math.max(...entries.map(([, x]) => x));
  document.getElementById("tbd-bars").replaceChildren(...entries.map(([src, dmg], i) => {
    const row = bar(src, dmg, max, r.total, i);
    row.querySelector(".bar-fill").style.background = css(TSRC_COLOR[src] || SLOTS[i % SLOTS.length]);
    return row;
  }));
}

async function openTft() {
  if (tstate.opening) return tstate.opening;
  const mode = tstate.mode, request = tstate.req;
  const active = () => state.view === "tft" && tstate.mode === mode && request === tstate.req && !document.hidden;
  const opening = (async () => {
    if (tstate.mode === "early") {
      if (!active()) return;
      await loadTftEarly();
      if (active()) await pollTftStatus();
      return;
    }
    if (tstate.mode === "traits") {
      if (!active()) return;
      await loadTftFlex();
      if (active()) await pollTftStatus();
      return;
    }
    if (!await loadTftMeta() || !active()) return;
    await pollTftStatus();
    if (active()) {
      if (tstate.mode === "leaderboard") {
        if (!tboard.data && !tboard.loading && !tboard.error) await loadTftLeaderboard();
      } else if (!tstate.pending && (!tstate.data || tstate.data.unit !== tstate.unit ||
          tstate.data.scenario.key !== tkey() || tstate.dataRevision !== tftRevision())) await loadTftScenario();
      else if (tstate.data) renderTftTable();
    }
  })();
  tstate.opening = opening;
  try { await opening; }
  finally { if (tstate.opening === opening) tstate.opening = null; scheduleTftStatus(); }
}

if (!globalThis.__LOL_NO_AUTO_INIT__) init();
