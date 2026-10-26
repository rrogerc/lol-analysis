"use strict";
const SLOTS = ["--s1","--s2","--s3","--s4","--s5","--s6","--s7","--s8"];
const css = k => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
const fmtInt = n => n.toLocaleString("en-US");
// 95% CI half-width in percentage points for a win rate over n games.
const ci95 = (wr, n) => n ? 1.96 * Math.sqrt(wr * (100 - wr) / n) : null;
const fmtCompact = n => n >= 1e6 ? (n/1e6).toFixed(1) + "M" : n >= 1e3 ? (n/1e3).toFixed(0) + "K" : "" + n;
function api(path, options) {
  return fetch(path, options).then(r => {
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  });
}

let META = null;
let DATA = { rows: [], patches: [], totalGames: 0, totalMatches: null, tier: null };
const state = { tier: null, view: "overview" };

// ---------------- overview ----------------
const rid = r => r.c + "/" + r.l;
// CommunityDragon resolves lowercase champion aliases; our only renamed slug
// is wukong (Riot-internal monkeyking).
const iconUrl = c =>
  `https://cdn.communitydragon.org/latest/champion/${c === "wukong" ? "monkeyking" : c}/square`;
// A table cell with the champion's icon and slug.
function champCell(c) {
  const icon = document.createElement("img");
  icon.className = "champ-icon";
  icon.src = iconUrl(c);
  icon.alt = "";
  icon.loading = "lazy";
  icon.onerror = () => { icon.style.visibility = "hidden"; };
  const td = document.createElement("td");
  td.append(icon, document.createTextNode(c));
  return td;
}

// Bucket 0 (0-15 min) is excluded: games that end that early are too rare
// for the win rate to mean much.
const RANK_OPTIONS = [
  { label: "15–20", b: 1 }, { label: "20–25", b: 2 }, { label: "25–30", b: 3 },
  { label: "30–35", b: 4 }, { label: "35–40", b: 5 }, { label: "40+", b: 6 },
];
let rankBucket = 6;   // 40+ min

function visibleRows() {
  const q = document.getElementById("search").value.trim().toLowerCase();
  const lane = document.getElementById("lane").value;
  const min = +document.getElementById("mingames").value;
  return DATA.rows.filter(r => {
    if (lane && r.l !== lane) return false;
    if (q && !r.c.includes(q)) return false;
    return r.wr[rankBucket] != null && r.g[rankBucket] >= min;
  });
}
function cell(text, cls) {
  const td = document.createElement("td");
  td.textContent = text;
  if (cls) td.className = cls;
  return td;
}
function metricCell(text, ci) {
  const td = cell(text);
  if (ci != null) {
    const s = document.createElement("span");
    s.className = "ci";
    s.textContent = " ±" + ci.toFixed(1);
    td.append(s);
  }
  return td;
}
let sortKey = "rank", sortDir = 1;
function renderTable() {
  const metric = r => r.wr[rankBucket];
  const gamesOf = r => r.g[rankBucket];
  // Rank is always the position by the bucket's metric, regardless of the
  // column the table happens to be sorted by.
  const base = visibleRows().sort((a, b) => metric(b) - metric(a));
  const rankOf = new Map(base.map((r, i) => [rid(r), i + 1]));
  const get = { rank: r => rankOf.get(rid(r)), c: r => r.c, l: r => r.l,
                metric, games: gamesOf }[sortKey];
  const rows = base.slice().sort((a, b) => {
    const va = get(a), vb = get(b);
    return (va < vb ? -1 : va > vb ? 1 : 0) * sortDir;
  });
  document.querySelector("#tbl tbody").replaceChildren(...rows.map(r => {
    const tr = document.createElement("tr");
    tr.append(
      cell(String(rankOf.get(rid(r)))), champCell(r.c), cell(r.l, "lane-tag"),
      metricCell(r.wr[rankBucket].toFixed(2) + "%",
                 ci95(r.wr[rankBucket], r.g[rankBucket])),
      cell(fmtInt(gamesOf(r))));
    return tr;
  }));
  document.querySelectorAll("#bucket-seg button").forEach((btn, i) =>
    btn.classList.toggle("active", RANK_OPTIONS[i].b === rankBucket));
  document.querySelectorAll("#tbl th").forEach(th => {
    th.querySelector(".arrow").textContent =
      th.dataset.k === sortKey ? (sortDir === 1 ? "▲" : "▼") : "";
  });
}
function initBucketSeg() {
  document.getElementById("bucket-seg").replaceChildren(...RANK_OPTIONS.map(o => {
    const btn = document.createElement("button");
    btn.textContent = o.label;
    btn.addEventListener("click", () => { rankBucket = o.b; renderTable(); });
    return btn;
  }));
}
async function loadOverview() {
  DATA = await api(`api/rows/${encodeURIComponent(state.tier)}.json`);
  // "synced" is when lol.db last pulled from the crawl: the date that can
  // go stale. The request date always looks fresh and hid a 10-day gap once.
  const synced = DATA.synced
    ? "synced " + new Date(DATA.synced).toLocaleString([], { dateStyle: "short", timeStyle: "short" })
    : DATA.generated;
  const sub = document.getElementById("subtitle");
  sub.textContent =
    `Patches ${DATA.patches[0]}–${DATA.patches[DATA.patches.length - 1]} · ${synced}`;
  sub.title = DATA.synced || "";
  document.getElementById("kpi-roles").textContent = fmtInt(DATA.rows.length);
  document.getElementById("kpi-games").textContent =
    DATA.totalMatches != null ? fmtCompact(DATA.totalMatches)
                              : "≈" + fmtCompact(Math.round(DATA.totalGames / 10));
  document.getElementById("kpi-patches").textContent = DATA.patches.length;
  renderTable();
}

// ---------------- one-tricks view ----------------
const OT_LANES = ["top", "jungle", "middle", "bottom", "support"];
let OT = null, otSortKey = "rank", otSortDir = 1;
// Champions ranked by one-trick count (equal counts share a rank), then
// ordered by the chosen column, ties in rank order.
function onetrickRows(rows, key, dir) {
  const byName = (a, b) => a.c < b.c ? -1 : a.c > b.c ? 1 : 0;
  const byCount = rows.slice().sort((a, b) => b.n - a.n || byName(a, b));
  const ranked = [];
  byCount.forEach((r, i) => ranked.push({ ...r,
    rank: i && r.n === byCount[i - 1].n ? ranked[i - 1].rank : i + 1 }));
  return ranked.sort((a, b) => {
    const va = a[key], vb = b[key];
    return (va < vb ? -1 : va > vb ? 1 : 0) * dir || a.rank - b.rank || byName(a, b);
  });
}
function renderOnetricks() {
  if (!OT?.synced) return;
  document.querySelector("#otbl tbody").replaceChildren(
    ...onetrickRows(OT.rows, otSortKey, otSortDir).map(r => {
      const tr = document.createElement("tr");
      tr.append(cell(String(r.rank)), champCell(r.c), cell(fmtInt(r.n)),
        ...OT_LANES.map(l => r[l] ? cell(fmtInt(r[l])) : cell("–", "none")));
      return tr;
    }));
  document.querySelectorAll("#otbl th").forEach(th => {
    th.querySelector(".arrow").textContent =
      th.dataset.k === otSortKey ? (otSortDir === 1 ? "▲" : "▼") : "";
  });
}
async function loadOnetricks() {
  const hint = document.getElementById("ot-hint");
  try {
    OT = await api("api/onetricks.json");
  } catch (e) {
    OT = null;
    hint.textContent = `One-trick counts are unavailable (${e.message}). ` +
      "A dashboard started before this tab existed needs a restart.";
    document.querySelector("#otbl tbody").replaceChildren();
    return;
  }
  if (!OT.synced) {
    hint.textContent = "No one-trick counts yet: run cli.py onetricks sync " +
      "(the six-hourly crawl sync runs it too).";
    document.querySelector("#otbl tbody").replaceChildren();
    return;
  }
  // Some locales end a time with a period ("p.m."): the times get their own
  // line rather than ending a sentence.
  const when = t => new Date(t).toLocaleString([], { dateStyle: "short", timeStyle: "short" });
  hint.replaceChildren(
    `A player one-tricks a champion when it is more than ${OT.minShare}% of ` +
    `their ranked solo games across all roles this season (${OT.firstPatch}–${OT.lastPatch}), ` +
    `with at least ${OT.minGames} games on it. Of the ${fmtInt(OT.ladderPlayers)} players ` +
    `on the Master+ ladders of ${OT.platforms.map(regionName).join(", ")}, ` +
    `${fmtInt(OT.onetrickPlayers)} one-trick a champion, each counted once under their most-played role on it.`,
    document.createElement("br"),
    (OT.ladderAt ? `Ladder as of ${when(OT.ladderAt)} · ` : "") + `synced ${when(OT.synced)}`);
  renderOnetricks();
}


// ---------------- shared breakdown & tooltip helpers ----------------
// Colors per damage source; anything unmapped cycles through the slots.
const SRC_COLOR = { auto: "--s3", onhit: "--s2", wave: "--s8", "E onhit": "--s5",
                    "E active": "--s7", Q: "--s1", R: "--s6", spellblade: "--s4",
                    "Q empowered": "--s7", E: "--s5", W: "--s2",
                    kraken: "--s1", botrk: "--s6", burn: "--s8", malignance: "--s5",
                    execute: "--s6", stormsurge: "--s1", blackfire: "--s8",
                    ludens: "--s5", muramana: "--s4", energized: "--s3",
                    shiv: "--s3", eclipse: "--s6", hullbreaker: "--s8",
                    titanic: "--s8", active: "--s4", umbral: "--s6" };

function bar(src, dmg, max, total, i) {
  const row = document.createElement("div");
  row.className = "bar-row";
  const label = document.createElement("span");
  label.className = "bar-label";
  label.textContent = src;
  const track = document.createElement("div");
  track.className = "bar-track";
  const fill = document.createElement("div");
  fill.className = "bar-fill";
  fill.style.width = (dmg / max * 100).toFixed(1) + "%";
  fill.style.background = css(SRC_COLOR[src] || SLOTS[i % SLOTS.length]);
  track.append(fill);
  const val = document.createElement("span");
  val.className = "bar-val";
  val.textContent = `${fmtInt(dmg)} (${(dmg / total * 100).toFixed(1)}%)`;
  row.append(label, track, val);
  return row;
}

function tftText(tag, text, className = "") {
  const el = document.createElement(tag);
  el.textContent = text;
  el.className = className;
  return el;
}

function tftIcon(url, name, trait = false) {
  const wrap = document.createElement("span");
  wrap.className = "tft-icon" + (trait ? " tft-trait-icon" : "");
  wrap.setAttribute("aria-hidden", "true");
  wrap.append(tftText("span", (name || "?").slice(0, 1), "tft-icon-fallback"));
  if (url) {
    const img = document.createElement("img");
    img.alt = "";
    img.loading = "lazy";
    img.decoding = "async";
    img.addEventListener("load", () => wrap.classList.add("has-image"));
    img.addEventListener("error", () => { img.remove(); wrap.classList.remove("has-image"); });
    img.src = url;
    wrap.append(img);
  }
  return wrap;
}

const tftTip = { anchor: null, timer: null };
function hideTftTooltip() {
  clearTimeout(tftTip.timer);
  tftTip.anchor?.removeAttribute("aria-describedby");
  tftTip.anchor = null;
  document.getElementById("tft-tooltip").hidden = true;
}

function showTftTooltip(anchor, contents) {
  hideTftTooltip();
  const tip = document.getElementById("tft-tooltip");
  tip.replaceChildren(...contents());
  tip.hidden = false;
  tftTip.anchor = anchor;
  anchor.setAttribute("aria-describedby", tip.id);
  positionTftTooltip();
}

function positionTftTooltip() {
  const anchor = tftTip.anchor, tip = document.getElementById("tft-tooltip");
  if (!anchor || tip.hidden) return;
  tip.style.maxHeight = `${Math.max(120, innerHeight - 24)}px`;
  const rect = anchor.getBoundingClientRect();
  const left = Math.max(12, Math.min(rect.left, innerWidth - tip.offsetWidth - 12));
  const below = rect.bottom + 8;
  const preferredTop = below + tip.offsetHeight <= innerHeight - 12 ? below : rect.top - tip.offsetHeight - 8;
  const top = Math.max(12, Math.min(preferredTop, innerHeight - tip.offsetHeight - 12));
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

function deferTftTooltipClose() {
  clearTimeout(tftTip.timer);
  tftTip.timer = setTimeout(() => {
    const tip = document.getElementById("tft-tooltip");
    if (!tip.matches(":hover") && !tftTip.anchor?.matches(":hover, :focus-visible")) hideTftTooltip();
  }, 150);
}

function bindTftTooltip(anchor, contents, tap = false) {
  anchor.addEventListener("pointerenter", e => {
    if (e.pointerType === "mouse") showTftTooltip(anchor, contents);
  });
  anchor.addEventListener("pointerleave", deferTftTooltipClose);
  anchor.addEventListener("focus", () => {
    if (anchor.matches(":focus-visible")) showTftTooltip(anchor, contents);
  });
  anchor.addEventListener("blur", deferTftTooltipClose);
  if (tap) anchor.addEventListener("click", () => {
    if (tftTip.anchor === anchor && !document.getElementById("tft-tooltip").hidden) hideTftTooltip();
    else showTftTooltip(anchor, contents);
  });
}

const tftTooltip = document.getElementById("tft-tooltip");
tftTooltip.addEventListener("pointerenter", () => clearTimeout(tftTip.timer));
tftTooltip.addEventListener("pointerleave", deferTftTooltipClose);
document.addEventListener("pointerdown", e => {
  if (!tftTooltip.contains(e.target) && !tftTip.anchor?.contains(e.target)) hideTftTooltip();
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !tftTooltip.hidden) { hideTftTooltip(); e.preventDefault(); }
});
document.addEventListener("scroll", e => {
  if (!tftTooltip.contains(e.target)) {
    if (tftTip.anchor?.matches(":focus-visible")) positionTftTooltip();
    else hideTftTooltip();
  }
}, true);
window.addEventListener("resize", hideTftTooltip);

function tftTooltipHead(icon, name, subtitle, trait = false) {
  const head = document.createElement("div");
  head.className = "tft-tooltip-head";
  const text = document.createElement("div");
  text.append(tftText("strong", name));
  if (subtitle) text.append(tftText("span", subtitle, "sub"));
  head.append(tftIcon(icon, name, trait), text);
  return head;
}

function refreshMessage(refresh) {
  const message = typeof refresh?.message === "string" ? refresh.message.trim() : "";
  // A failed job's technical details belong in its log, not the build picker.
  return message.length <= 360 && !/[\r\n]|Traceback|\b\w*(?:Error|Exception):/.test(message) ? message : "";
}

function refreshWhen(refresh) {
  const stamp = refresh?.checkedAt || refresh?.finishedAt;
  const date = stamp ? new Date(stamp) : null;
  return date && Number.isFinite(date.getTime()) ? date : null;
}

// ---------------- data view ----------------
async function loadInventory() {
  const meta = await api("api/meta.json");
  META = meta;
  const svcs = meta.services || [];
  document.getElementById("services-card").hidden = !svcs.length;
  document.querySelector("#services tbody").replaceChildren(...svcs.map(s => {
    const st = svcState(s);
    const tr = document.createElement("tr");
    tr.append(cell(s.unit), cell(s.runs), cell(st.text, st.cls),
              cell(svcWhen(s)), cell(s.desc));
    return tr;
  }));
  document.querySelector("#jobs tbody").replaceChildren(...(meta.jobs || []).map(j => {
    const tr = document.createElement("tr");
    const st = jobStatus(j);
    tr.append(cell(j.name), cell(j.runs), cell(st.text, st.cls), cell(j.desc));
    return tr;
  }));
  document.querySelector("#inv tbody").replaceChildren(...meta.inventory.map(r => {
    const tr = document.createElement("tr");
    tr.append(cell(r.tier), cell(r.patch), cell(fmtInt(r.champLanes)),
              cell(fmtInt(r.games)), cell(r.scrapedAt));
    tr.firstChild.style.textAlign = "left";
    return tr;
  }));
}

// ---------------- health: systemd units ----------------
// serve reads these with `systemctl show`; the static export drops them
// entirely, since frozen unit state could only ever be a lie once viewed.
// Restarts are the signal worth watching: a service that cannot bind its
// port restarts forever, and nothing else here would reveal it: the page
// you are reading is served by whichever process did get the port.
function svcState(s) {
  if (!s.loaded) return { text: "not found", cls: "neg" };
  if (s.unit.endsWith(".timer"))
    return s.state === "active" ? { text: "armed", cls: "pos" }
                                : { text: s.state || "?", cls: "neg" };
  if (s.state !== "active") return { text: s.state || "?", cls: "neg" };
  if (s.restarts) return { text: `running · ${s.restarts} restarts`, cls: "amber" };
  return { text: "running", cls: "pos" };
}

// "40 min ago" / "in 9.6h" from a unix timestamp.
function relTime(ts) {
  const d = (Date.now() - ts * 1000) / 1000, m = Math.abs(d);
  const n = m < 90 ? `${Math.round(m)}s` : m < 5400 ? `${Math.round(m / 60)} min`
          : m < 172800 ? `${(m / 3600).toFixed(1)}h` : `${(m / 86400).toFixed(1)}d`;
  return d >= 0 ? `${n} ago` : `in ${n}`;
}

function svcWhen(s) {
  if (!s.loaded) return "-";
  if (!s.unit.endsWith(".timer"))
    return s.since ? `up ${relTime(s.since)}` : "-";
  const parts = [s.last ? `last ${relTime(s.last)}` : "never run"];
  if (s.running) parts.push("running now");
  else if (s.result && s.result !== "success") parts.push(s.result);
  else if (s.result === "success") parts.push("ok");
  if (s.next) parts.push(`next ${relTime(s.next)}`);
  return parts.join(" · ");
}

// ---------------- health: job heartbeats + item-data staleness ----------------
// Local jobs write a heartbeat (web/ops/.state/) that serve exposes as
// lastRun/lastExit; the static export strips it, leaving a dash on the published site.
function jobStatus(j) {
  if (j.status) {
    const date = refreshWhen({ checkedAt: j.checkedAt, finishedAt: j.lastRun });
    const when = date ? ` · ${relTime(date.getTime() / 1000)}` : "";
    const message = refreshMessage(j);
    if (j.status === "running") return { text: `${/warm|calculat|comput/.test(j.phase || "") ? "recalculating builds" : "checking for updates"}${j.targetPatch ? ` · patch ${j.targetPatch}` : ""}`, cls: "amber" };
    if (j.status === "needs-review") return { text: `needs review${when}${message ? ` · ${message}` : ""}`, cls: "amber" };
    if (j.status === "failed") return { text: `update failed${when} · current builds kept`, cls: "neg" };
    if (date && Date.now() - date.getTime() > 18 * 3.6e6) return { text: `no recent check${when}`, cls: "amber" };
    if (j.status === "ok") return { text: `ok${when}${message ? ` · ${message}` : ""}`, cls: "pos" };
  }
  if (j.lastRun == null) return { text: "-" };
  const hrs = (Date.now() - Date.parse(j.lastRun)) / 3.6e6;
  const when = hrs < 1.5 ? "just now" : hrs < 48 ? Math.round(hrs) + "h ago"
                                                 : Math.round(hrs / 24) + "d ago";
  if (j.lastExit !== 0) return { text: `failed ${when} (exit ${j.lastExit})`, cls: "neg" };
  if (hrs > 48) return { text: `last ran ${when}; timer may be dead`, cls: "neg" };
  return { text: `ok, ran ${when}`, cls: "pos" };
}

// Outcome check, independent of the job: is the newest snapshot the live
// patch? Grace period so patch day shows a heads-up, not an alarm: this
// browser remembers when it first saw the mismatch.
async function checkItemHealth() {
  const el = document.getElementById("health");
  const snap = META?.itemsSnapshot;
  if (!snap) return;
  let live;
  try {
    const versions = await api("https://ddragon.leagueoflegends.com/api/versions.json");
    live = versions[0].split(".").slice(0, 2).join(".");
  } catch { return; }   // offline: no verdict, say nothing
  const showHealth = (cls, msg) => { el.className = cls; el.textContent = msg; el.hidden = false; };
  if (live === snap.patch) {
    if (!snap.merakiItems)
      showHealth("warn", `Patch ${snap.patch} item snapshot has no meraki stats yet; the daily job should fill them in.`);
    return;
  }
  const key = "itemsMismatchSince:" + live;
  Object.keys(localStorage).forEach(k => {
    if (k.startsWith("itemsMismatchSince:") && k !== key) localStorage.removeItem(k);
  });
  const first = +localStorage.getItem(key) || Date.now();
  localStorage.setItem(key, first);
  if ((Date.now() - first) / 864e5 > 3)
    showHealth("bad", `Item data is stale: live patch is ${live} but the newest snapshot is ` +
      `${snap.patch} (fetched ${snap.fetchedAt.slice(0, 10)}). Check the refresh job: ` +
      `journalctl --user -u lol-items-refresh`);
  else
    showHealth("warn", `New patch ${live} is live, while item snapshot is still on ${snap.patch}; ` +
      `the daily job should catch up shortly.`);
}

// ---------------- shell: tabs, tier, hash routing ----------------
// Tier definitions mirror the thresholds in cli.py sync.
const TIER_DESC = {
  soloq_masters_plus: "All crawled Masters+ solo-queue games.",
  soloq_mastery: "Only games where the pilot has ≥ 20 season games on the champion.",
  soloq_otp: "One-tricks only: the champion is ≥ 80% of the pilot's ranked solo games.",
};
const regionName = p => p.replace(/\d+$/, "").toUpperCase();
let restoringHash = false;
let restoredHash = null;
function renderTierDesc() {
  const bits = [];
  if (TIER_DESC[state.tier]) bits.push(TIER_DESC[state.tier]);
  if (META?.platforms?.length)
    bits.push("Regions: " + META.platforms.map(regionName).join(", "));
  document.getElementById("tier-desc").textContent = bits.join(" · ");
}
function updateHash(push = false) {
  if (restoringHash) return;
  const parts = [`tab=${state.view}`, `tier=${state.tier}`];
  if (state.view === "builds" && bstate.champion) parts.push(`champ=${bstate.champion}`);
  if (state.view === "builds" && bstate.scenario) parts.push(`sc=${bstate.scenario}`);
  if (state.view === "builds" && bstate.mode === "leaderboard") {
    parts.push("bview=leaderboard");
    if (bboard.search.trim()) parts.push(`lbq=${encodeURIComponent(bboard.search.trim())}`);
    if (bboard.reviewedOnly) parts.push("lbrev=1");
    if (bboard.sortKey !== "rank" || bboard.sortDir !== 1)
      parts.push(`lbs=${encodeURIComponent(bboard.sortKey)}`, ...(bboard.sortDir === 1 ? [] : ["lbd=d"]));
  }
  if (state.view === "tft" && tstate.unit && !["early", "traits"].includes(tstate.mode)) {
    parts.push(`unit=${tstate.unit}`, `star=${tstate.star}`, `geo=${tstate.geo}`, `tr=${tstate.traits}`);
    if (tstate.threat !== "mixed") parts.push(`threat=${tstate.threat}`);
  }
  if (state.view === "tft" && tstate.mode === "builds") {
    if (tstate.cls !== "all") parts.push(`uclass=${encodeURIComponent(tstate.cls)}`);
    if (tstate.cost && tstate.cost !== "all") parts.push(`ucost=${encodeURIComponent(tstate.cost)}`);
    if (tstate.search) parts.push(`uq=${encodeURIComponent(tstate.search)}`);
    if (tstate.sel > 0) parts.push(`tbuild=${tstate.sel}`);
  }
  if (state.view === "tft" && (tstate.mode === "leaderboard" || tstate.mode === "builds" && tboard.fromLeaderboard)) {
    if (tstate.mode === "leaderboard") parts.push("tftview=leaderboard");
    parts.push(`board=${tboard.mode}`, `bstar=${tboard.star}`, `bgeo=${tboard.geo}`,
      `btr=${tboard.traits}`, `bthreat=${tboard.threat}`);
    if (tboard.role !== "all") parts.push(`brole=${tboard.role}`);
    if (tboard.cost !== "all") parts.push(`bcost=${tboard.cost}`);
    if (tboard.search) parts.push(`bq=${encodeURIComponent(tboard.search)}`);
  }
  if (state.view === "tft" && tstate.mode === "early") {
    parts.push("tftview=early", `elevel=${tearly.level}`);
    if (!tearly.allowThree) parts.push("ethree=0");
    if (tearly.unit !== "all") parts.push(`eunit=${encodeURIComponent(tearly.unit)}`);
    if (tearly.sort !== "wins") parts.push(`esort=${tearly.sort}`);
    if (tearly.order !== (tearly.sort === "gold" ? "asc" : "desc")) parts.push(`eorder=${tearly.order}`);
    if (tearly.page > 1) parts.push(`epage=${tearly.page}`);
  }
  if (state.view === "tft" && tstate.mode === "traits") {
    parts.push("tftview=traits", `fpref=${tflex.preference}`, `ffront=${tflex.minFrontline}`);
    if (tflex.cores?.length) parts.push(`fcore=${encodeURIComponent([...new Set(tflex.cores)].sort().join(","))}`);
    if (tflex.sort && tflex.sort !== "score") parts.push(`fsort=${encodeURIComponent(tflex.sort)}`);
    if (tflex.order) parts.push(`forder=${tflex.order}`);
    if (tflex.lobbyProfile) parts.push(`flprofile=${encodeURIComponent(tflex.lobbyProfile)}`);
    if (tflex.lobbyPlayers !== undefined && tflex.lobbyPlayers !== 7) parts.push(`flplayers=${tflex.lobbyPlayers}`);
    if (tflex.lobbyDevelopment !== undefined && tflex.lobbyDevelopment !== 0.5) parts.push(`fldev=${Math.round(tflex.lobbyDevelopment * 100)}`);
    const scoutOverrides = Object.entries(tflex.lobbyOverrides || {}).sort(([a], [b]) => a.localeCompare(b));
    if (scoutOverrides.length) parts.push(`flheld=${encodeURIComponent(scoutOverrides.map(([slug, copies]) => `${slug}:${copies}`).join(","))}`);
    if (tflex.allowFive) parts.push("ffive=1");
    if (tflex.cost === 2 || tflex.cost === 3) parts.push(`fcost=${tflex.cost}`);
    if (tflex.minCost > 1) parts.push(`fmincost=${tflex.minCost}`);
    if (tflex.locked.length) parts.push(`fkeep=${encodeURIComponent([...tflex.locked].sort().join(","))}`);
    if (tflex.page > 1) parts.push(`fpage=${tflex.page}`);
  }
  const hash = "#" + parts.join("&");
  if (hash !== location.hash) {
    if (push) history.pushState(null, "", hash);
    else history.replaceState(null, "", hash);
  }
  restoredHash = location.hash;
}
function showView(v, push = true) {
  state.view = v;
  document.querySelectorAll("nav button").forEach(b =>
    b.classList.toggle("active", b.dataset.view === v));
  document.querySelectorAll(".view").forEach(s =>
    s.classList.toggle("active", s.id === "view-" + v));
  updateHash(push);
  // (re)load what the tab's view lacks: a champion's cell, or a complete board
  if (v === "builds" && (restoringHash || !(bstate.mode === "leaderboard" ? bboard.data?.complete : bstate.data))) openBuilds();
  if (v !== "builds") clearTimeout(bstate.timer);  // stop polling off-tab
  if (v === "tft") { renderTftMode(); openTft(); }
  if (v !== "tft") { clearTimeout(tstate.timer); ++tstate.req; tstate.opening = null;
    if (tstate.mode === "traits") cancelTftFlex(); if (tstate.mode === "early") cancelTftEarly(); }
  if (v === "data") loadInventory().catch(() => {});
  if (v === "onetricks") loadOnetricks();
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearTimeout(tstate.timer);
  else if (state.view === "tft") openTft();
});
document.querySelectorAll("nav button").forEach(b =>
  b.addEventListener("click", () => showView(b.dataset.view)));

document.getElementById("tier").addEventListener("change", async e => {
  state.tier = e.target.value;
  updateHash(true);
  renderTierDesc();
  await loadOverview();
});
["search", "lane", "mingames"].forEach(id =>
  document.getElementById(id).addEventListener("input", renderTable));
document.querySelectorAll("#tbl th").forEach(th =>
  th.addEventListener("click", () => {
    const k = th.dataset.k;
    if (sortKey === k) sortDir = -sortDir;
    else { sortKey = k; sortDir = (k === "metric" || k === "games") ? -1 : 1; }
    renderTable();
  }));
document.querySelectorAll("#otbl th").forEach(th =>
  th.addEventListener("click", () => {
    const k = th.dataset.k;
    if (otSortKey === k) otSortDir = -otSortDir;
    else { otSortKey = k; otSortDir = (k === "rank" || k === "c") ? 1 : -1; }
    renderOnetricks();
  }));
initBucketSeg();

function restoreHash() {
  if (!META) return;
  const hash = Object.fromEntries(new URLSearchParams(location.hash.slice(1)));
  const tab = hash.tab || hash.view;
  const view = ["overview", "onetricks", "builds", "tft", "data"].includes(tab) ? tab : "overview";
  const previousTier = state.tier;
  restoringHash = true;
  try {
    state.tier = META.tiers.includes(hash.tier) ? hash.tier : META.defaultTier;
    document.getElementById("tier").value = state.tier;
    renderTierDesc();
    // Requests started by a later route must not repaint this history entry.
    ++tstate.req;
    tstate.opening = null;
    if (tab === "builds") {
      bstate.mode = hash.bview === "leaderboard" ? "leaderboard" : "champion";
      bstate.champion = hash.champ || bstate.meta?.champions[0]?.slug || null;
      bstate.scenario = hash.sc || bstate.meta?.scenarios[0]?.key || null;
      Object.assign(bboard, { search: "", reviewedOnly: false, sortKey: "rank", sortDir: 1 });
    }
    if (tab === "builds" && hash.bview === "leaderboard") {
      bstate.mode = "leaderboard";
      if (hash.lbq) bboard.search = hash.lbq;
      bboard.reviewedOnly = hash.lbrev === "1";
      // a column this board lacks leaves it in rank order: boardSort
      if (hash.lbs) { bboard.sortKey = hash.lbs; bboard.sortDir = hash.lbd === "d" ? -1 : 1; }
    }
    if (tab === "tft") {
      const mode = ["leaderboard", "early", "traits"].includes(hash.tftview) ? hash.tftview : "builds";
      if (tstate.mode === "traits") cancelTftFlex();
      if (tstate.mode === "early") cancelTftEarly();
      ++tboard.req;
      tboard.controller?.abort();
      Object.assign(tstate, { unit: hash.unit || tstate.meta?.units[0]?.slug || null,
        star: Number(hash.star) || 2, geo: hash.geo || "clump", traits: hash.tr || "bare",
        threat: hash.threat || "mixed", cls: hash.uclass || "all", cost: hash.ucost || null,
        search: hash.uq || "", sel: Math.max(0, Math.floor(Number(hash.tbuild) || 0)), pending: false });
      Object.assign(tboard, { mode: hash.board || "damage", star: hash.bstar || "s2", geo: "clump",
        traits: hash.btr === "bare" ? "low" : hash.btr || "low", threat: hash.bthreat || "mixed",
        role: hash.brole || "all", cost: hash.bcost || "all", search: hash.bq || "", loading: false,
        error: null, fromLeaderboard: !!hash.bstar && mode === "builds", returnUnit: hash.unit || null });
      if (mode === "early") restoreTftEarlyHash(hash);
      if (mode === "traits") restoreTftFlexHash(hash);
      setTftMode(mode, { load: false });
    }
    showView(view, false);
  } finally {
    restoringHash = false;
  }
  updateHash();
  if (state.tier !== previousTier) {
    loadOverview();
  }
}

function handleHistoryNavigation() {
  // Back/Forward can emit both events for one fragment change.
  if (META && location.hash !== restoredHash) restoreHash();
}
window.addEventListener("popstate", handleHistoryNavigation);
window.addEventListener("hashchange", handleHistoryNavigation);

async function init() {
  const meta = await api("api/meta.json");
  META = meta;
  const hash = Object.fromEntries(new URLSearchParams(location.hash.slice(1)));
  state.tier = meta.tiers.includes(hash.tier) ? hash.tier : meta.defaultTier;
  const tierSel = document.getElementById("tier");
  tierSel.replaceChildren(...meta.tiers.map(t => {
    const o = document.createElement("option");
    o.value = o.textContent = t;
    return o;
  }));
  tierSel.value = state.tier;
  renderTierDesc();
  await Promise.all([loadOverview(), loadInventory()]);
  checkItemHealth();
  restoreHash();
}
