//! The Survival tier: how long a defending build lasts against the modeled
//! carries' best builds. The attackers are fixed (their fights run exactly
//! as against a dummy, driver and all); the defender varies — every build of
//! its pool — and the ranking is the attacker's kill time, longest first.
//!
//! Python hands over the defender's pool and the attackers once (`SurvCtx`)
//! and gets the ranked lists back; the loop runs on std threads with the GIL
//! released. `survive` is the one-fight entry point (CLI, tests, the rows'
//! details).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};

use crate::defense::{DefItem, DefReport, Defender, Defense, Guard};
use crate::enumerate::fight_to_py;
use crate::fight::{FightResult, Opts, Sim, Target};
use crate::fsum::geo_mean;
use crate::fx::{Fx, ItemFx};
use crate::kit::Kit;
use crate::num::*;
use crate::pyget::*;
use crate::sheet::{parse_stat_pairs, resolve, ChampBase, Sheet, SK};

/// Most items one build may hold, boots included.
const MAX_ITEMS: usize = 8;
/// Most attackers one pass may score against.
const MAX_A: usize = 6;

/// One attacker, fixed for the whole pass: its build's sheet and effects,
/// its kit, and the fight's length.
pub struct Attacker {
    pub key: String,
    pub sheet: Sheet,
    pub kit: Kit,
    pub fx: Fx,
    pub level: i64,
    pub ranks: Ranks,
    pub duration: f64,
}

fn ranks_of(d: &Bound<'_, PyDict>) -> PyResult<Ranks> {
    Ok(Ranks { q: reqi(d, "Q")?, w: reqi(d, "W")?, e: reqi(d, "E")?, r: reqi(d, "R")? })
}

impl Attacker {
    fn from_py(key: String, d: &Bound<'_, PyDict>) -> PyResult<Attacker> {
        Ok(Attacker {
            key,
            sheet: Sheet::from_py(&reqd(d, "sheet")?)?,
            kit: Kit::from_py(&reqd(d, "kit")?)?,
            fx: Fx::from_py(&reqd(d, "fx")?)?,
            level: reqi(d, "level")?,
            ranks: ranks_of(&reqd(d, "ranks")?)?,
            duration: reqf(d, "duration")?,
        })
    }

    fn sim(&self) -> Result<Sim<'_>, String> {
        Sim::new(&self.sheet, &self.kit, &self.fx, self.level, self.ranks, false)
    }
}

/// The defending champion: its base stats, level, ranks and kit, the parts
/// of a `Defense` no item changes.
#[derive(Clone)]
pub struct DefenderSpec {
    pub base: ChampBase,
    pub level: i64,
    pub guard: Guard,
    pub pact: Option<crate::kit::Pact>,
    pub ranged: bool,
}

impl DefenderSpec {
    fn from_py(d: &Bound<'_, PyDict>) -> PyResult<DefenderSpec> {
        let base = ChampBase::from_py(&reqd(d, "base")?)?;
        let level = reqi(d, "level")?;
        let ranks = ranks_of(&reqd(d, "ranks")?)?;
        let kit_d = reqd(d, "kit")?;
        let kit = Kit::from_py(&kit_d)?;
        Ok(DefenderSpec {
            ranged: base.attack_range > MELEE_MAX_RANGE,
            guard: Guard::from_kit(&kit_d, level, ranks)?,
            pact: kit.crimson_pact,
            base,
            level,
        })
    }

    /// One build's `Defense`: the sheet its items resolve to, their merged
    /// defensive effects and the kit.
    fn defense(&self, items: &[(&[(SK, f64)], &ItemFx)], defs: &[&DefItem]) -> (Sheet, Defense) {
        let sheet = resolve(&self.base, self.level, items, self.pact);
        let fx = DefItem::merge(defs.iter().copied());
        let d = Defense::new(&sheet, &self.base, self.level, fx, self.guard.clone(), self.ranged);
        (sheet, d)
    }
}

/// The target a defended fight starts from: the build's health, bonus
/// health and resists; everything that moves is the `Defender`'s.
fn target_of(d: &Defense, duration: f64) -> Target {
    Target { hp: d.hp, armor: d.base_armor + d.bonus_armor, mr: d.base_mr + d.bonus_mr,
             duration, bonus_hp: d.bonus_hp }
}

/// The attacker's kill time, extended past a fight the defender survived at
/// the pace of the damage it took (as the damage tier's `kill_time`); INF
/// when nothing got through.
fn time_to_die(r: &FightResult, duration: f64) -> f64 {
    if r.ttk.is_some() {
        return r.ttk_exp.unwrap();
    }
    if r.dps > 0.0 {
        duration + r.hp_left / r.dps
    } else {
        INF
    }
}

/// Sort key of one fight, best (longest-lived) first: survivors of the whole
/// fight before anyone who died, the longer extrapolated time first; the
/// dead by their expected death time, then the interpolated one (health to
/// spare on the killing blow).
fn surv_key(r: &FightResult, duration: f64) -> [f64; 3] {
    match r.ttk {
        Some(_) => [1.0, -r.ttk_exp.unwrap(), -r.ttk_eff.unwrap()],
        None => [0.0, -time_to_die(r, duration), -r.hp_left],
    }
}

/// Sort key across every attacker: fewer attackers that kill the build
/// first, then the geometric mean of the times to die, longest first (the
/// damage tier's overall key, turned around).
fn overall_key(rs: &[(FightResult, Option<f64>)], durations: &[f64]) -> [f64; 3] {
    let mut killed = 0usize;
    let mut times = [0.0f64; MAX_A];
    let mut effs = [0.0f64; MAX_A];
    let n = rs.len();
    for k in 0..n {
        let r = &rs[k].0;
        if r.ttk.is_some() {
            killed += 1;
        }
        times[k] = time_to_die(r, durations[k]);
        effs[k] = if r.ttk.is_some() { r.ttk_eff.unwrap() } else { times[k] };
        if !times[k].is_finite() {
            return [killed as f64, -INF, -INF];
        }
    }
    [killed as f64, -geo_mean(&times[..n]), -geo_mean(&effs[..n])]
}

fn cmp_key(a: &[f64; 3], b: &[f64; 3]) -> std::cmp::Ordering {
    for i in 0..3 {
        match a[i].partial_cmp(&b[i]).expect("no NaN keys") {
            std::cmp::Ordering::Equal => {}
            o => return o,
        }
    }
    std::cmp::Ordering::Equal
}

/// The fight against one attacker with Maximum Dosage cast at the best of
/// `thresholds` (share of maximum health at or below which it goes out
/// after a hit; it always goes out when a hit would otherwise kill): the
/// one the defender lasts longest in. A kit without an ult fights once.
///
/// Every threshold's fight is the lethal-only one until its cast, so that
/// fight runs first as a probe and records which hit crosses each
/// threshold; thresholds crossed by the same hit share a fight, which runs
/// once (for the highest of them), and one never crossed before the end is
/// the lethal-only fight itself. Exactly the grid's best, with fewer fights.
/// Returns the fight and the threshold it ran with (0.0: lethal only).
fn best_fight(sim: &mut Sim, a: &Attacker, d: &Defense, thresholds: &[f64], breakdown: bool,
              log: &mut Vec<(f64, f64)>) -> Result<(FightResult, Option<f64>), String> {
    let target = target_of(d, a.duration);
    let range = sim.prep().atk_range;
    let opts = Opts { use_ult: true, prestacked: false, stop_after: INF, breakdown, blend: true };
    let mut grid: Vec<f64> = thresholds.iter().copied().filter(|&x| x > 0.0).collect();
    grid.sort_by(|a, b| b.partial_cmp(a).expect("no NaN thresholds"));
    grid.dedup();
    grid.truncate(crate::defense::MAX_PROBE);
    let mut base = Defender::new(d.clone(), range, a.sheet.mr, 0.0);
    let search = d.guard.r.is_some() && !grid.is_empty();
    if search {
        base.probe(&grid);
    }
    let first = sim.fight_defended(&target, &base, opts, log)?
        .expect("an unbounded fight always returns");
    if !search {
        return Ok((first, d.guard.r.map(|_| 0.0)));
    }
    let cross = first.def.as_ref().expect("a defended fight").cross;
    let mut best_key = surv_key(&first, a.duration);
    let mut best = (first, Some(0.0));
    let mut done: Vec<i64> = Vec::new();
    for (k, &x) in grid.iter().enumerate() {
        let c = cross[k];
        if c < 0 || done.contains(&c) {
            continue;
        }
        done.push(c);
        let dfd = Defender::new(d.clone(), range, a.sheet.mr, x);
        let r = sim.fight_defended(&target, &dfd, opts, log)?
            .expect("an unbounded fight always returns");
        let key = surv_key(&r, a.duration);
        if cmp_key(&key, &best_key) == std::cmp::Ordering::Less {
            best_key = key;
            best = (r, Some(x));
        }
    }
    Ok(best)
}

fn report_to_py<'py>(py: Python<'py>, rep: &DefReport) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    for (k, v) in [
        ("taken", rep.taken), ("hpLost", rep.hp_lost), ("hpSpent", rep.hp_spent),
        ("regen", rep.regen), ("healedW", rep.healed_w), ("healedR", rep.healed_r),
        ("healedLifeline", rep.healed_lifeline), ("healedDrain", rep.healed_drain),
        ("rBaseHealth", rep.r_base_health), ("lifelineHealth", rep.lifeline_health),
        ("reviveHealth", rep.revive_health), ("shieldMagic", rep.shield_magic),
        ("shieldLifeline", rep.shield_lifeline), ("deferred", rep.deferred),
        ("bleed", rep.bleed), ("blocked", rep.blocked), ("negated", rep.negated),
        ("reducedAttack", rep.reduced_attack), ("reducedCrit", rep.reduced_crit),
        ("maxHpPeak", rep.max_hp_peak),
    ] {
        d.set_item(k, v)?;
    }
    d.set_item("wCasts", rep.w_casts)?;
    for (k, v) in [
        ("rAt", rep.r_at), ("lifelineAt", rep.lifeline_at), ("zhonyaAt", rep.zhonya_at),
        ("reviveAt", rep.revive_at), ("steadfastAt", rep.steadfast_at),
        ("voidbornAt", rep.voidborn_at), ("rThreshold", rep.r_threshold),
    ] {
        d.set_item(k, v)?;
    }
    Ok(d)
}

/// A fight dict as `simulate` returns it, plus the defender's report and
/// the fight's time to die.
fn surv_fight_to_py<'py>(py: Python<'py>, r: &FightResult, duration: f64)
    -> PyResult<Bound<'py, PyDict>> {
    let d = fight_to_py(py, r)?;
    if let Some(rep) = &r.def {
        d.set_item("defense", report_to_py(py, rep)?)?;
    }
    d.set_item("time_to_die", time_to_die(r, duration))?;
    Ok(d)
}

fn parse_items(v: &Bound<'_, PyAny>) -> PyResult<(Vec<Vec<(SK, f64)>>, Vec<ItemFx>, Vec<DefItem>)> {
    let (mut stats, mut fxs, mut defs) = (Vec::new(), Vec::new(), Vec::new());
    for it in v.try_iter()? {
        let it = it?;
        stats.push(parse_stat_pairs(&it.get_item(0)?)?);
        let entry = dict_of(&it.get_item(1)?)?;
        fxs.push(ItemFx::from_py(&entry)?);
        defs.push(DefItem::from_entry(&entry)?);
    }
    Ok((stats, fxs, defs))
}

/// One defended fight (the best over `thresholds`): `attacker` is {sheet,
/// kit, fx, level, ranks, duration}, `defender` {base, level, ranks, kit},
/// `items` the defender's build as [(stat pairs, overlay entry)], boots
/// first. Returns the fight dict with `defense` (the report), `time_to_die`
/// and the defender's `sheet`.
#[pyfunction]
#[pyo3(signature = (attacker, defender, items, thresholds, breakdown=true))]
pub fn survive<'py>(py: Python<'py>, attacker: &Bound<'py, PyDict>, defender: &Bound<'py, PyDict>,
                    items: &Bound<'py, PyAny>, thresholds: Vec<f64>, breakdown: bool)
    -> PyResult<Bound<'py, PyDict>> {
    let a = Attacker::from_py(String::new(), attacker)?;
    let spec = DefenderSpec::from_py(defender)?;
    let (stats, fxs, defs) = parse_items(items)?;
    let refs: Vec<(&[(SK, f64)], &ItemFx)> =
        stats.iter().zip(fxs.iter()).map(|(s, f)| (s.as_slice(), f)).collect();
    let drefs: Vec<&DefItem> = defs.iter().collect();
    let (sheet, d) = spec.defense(&refs, &drefs);
    let mut sim = a.sim().map_err(PyValueError::new_err)?;
    let mut log = Vec::new();
    let (r, _) = best_fight(&mut sim, &a, &d, &thresholds, breakdown, &mut log)
        .map_err(PyValueError::new_err)?;
    let out = surv_fight_to_py(py, &r, a.duration)?;
    out.set_item("sheet", sheet.to_py(py)?)?;
    Ok(out)
}

/// One ranked build: its keys (per attacker, then overall), its ids (boots
/// first) and its place in the enumeration for ties. Its fights are run
/// again, in full, once the lists are final.
struct Row {
    keys: [[f64; 3]; MAX_A + 1],
    place: (Vec<i64>, i64),
    ids: Vec<u32>,
}

#[pyclass(frozen)]
pub struct SurvCtx {
    spec: DefenderSpec,
    id_of: Vec<u32>,
    stats: Vec<Vec<(SK, f64)>>,
    fxs: Vec<ItemFx>,
    defs: Vec<DefItem>,
    /// violable group indices per item, and their caps
    groups: Vec<Vec<usize>>,
    caps: Vec<i64>,
    free: Vec<usize>,
    classes: Vec<Vec<usize>>,
    order: Vec<i64>,
    attackers: Vec<Attacker>,
    keep: usize,
    slots: usize,
    thresholds: Vec<f64>,
}

impl SurvCtx {
    /// The group counts of `ids`, or None when they over-fill a group.
    fn counts(&self, ids: &[usize]) -> Option<[u8; 64]> {
        let mut counts = [0u8; 64];
        for &i in ids {
            for &g in &self.groups[i] {
                counts[g] += 1;
                if counts[g] as i64 > self.caps[g] {
                    return None;
                }
            }
        }
        Some(counts)
    }

    /// Whether item `b` fits on top of `counts`.
    fn fits(&self, counts: &[u8; 64], b: usize) -> bool {
        self.groups[b].iter().all(|&g| (counts[g] as i64) < self.caps[g])
    }

    fn place(&self, boots: usize, rest: &[usize]) -> (Vec<i64>, i64) {
        (rest.iter().map(|&i| self.order[i]).collect(), self.order[boots])
    }

    /// Every attacker against one build (a boots class's first member plus
    /// `rest`); None when the build is not legal.
    fn score(&self, sims: &mut [Sim<'_>], log: &mut Vec<(f64, f64)>, boots: usize,
             rest: &[usize], breakdown: bool)
        -> Result<Vec<(FightResult, Option<f64>)>, String> {
        let mut ids = Vec::with_capacity(rest.len() + 1);
        ids.push(boots);
        ids.extend_from_slice(rest);
        let items: Vec<(&[(SK, f64)], &ItemFx)> =
            ids.iter().map(|&i| (self.stats[i].as_slice(), &self.fxs[i])).collect();
        let defs: Vec<&DefItem> = ids.iter().map(|&i| &self.defs[i]).collect();
        let (_, d) = self.spec.defense(&items, &defs);
        let mut out = Vec::with_capacity(self.attackers.len());
        for (a, sim) in self.attackers.iter().zip(sims.iter_mut()) {
            out.push(best_fight(sim, a, &d, &self.thresholds, breakdown, log)?);
        }
        Ok(out)
    }

    fn keys(&self, fights: &[(FightResult, Option<f64>)], durations: &[f64])
        -> [[f64; 3]; MAX_A + 1] {
        let mut keys = [[0.0f64; 3]; MAX_A + 1];
        for (k, (r, _)) in fights.iter().enumerate() {
            keys[k] = surv_key(r, durations[k]);
        }
        keys[self.attackers.len()] = overall_key(fights, durations);
        keys
    }
}

/// Sort a list on key `k`, then place, and cut it to `keep`.
fn cut(lst: &mut Vec<Row>, k: usize, keep: usize) {
    lst.sort_by(|a, b| cmp_key(&a.keys[k], &b.keys[k]).then_with(|| a.place.cmp(&b.place)));
    lst.truncate(keep);
}

#[pymethods]
impl SurvCtx {
    #[new]
    #[allow(clippy::too_many_arguments)]
    fn new(defender: &Bound<'_, PyDict>, items: &Bound<'_, PyDict>, groups: &Bound<'_, PyDict>,
           caps: &Bound<'_, PyDict>, free: Vec<u32>, classes: Vec<Vec<u32>>,
           order: &Bound<'_, PyDict>, attackers: &Bound<'_, PyAny>, keep: usize, slots: usize,
           thresholds: Vec<f64>) -> PyResult<SurvCtx> {
        let spec = DefenderSpec::from_py(defender)?;
        let mut id_of = Vec::new();
        let mut dense: HashMap<u32, usize> = HashMap::new();
        let (mut stats, mut fxs, mut defs, mut gnames) = (Vec::new(), Vec::new(), Vec::new(), Vec::new());
        let mut ids: Vec<u32> = items.iter().map(|(k, _)| k.extract::<u32>()).collect::<PyResult<_>>()?;
        ids.sort_unstable();
        for id in ids {
            let v = items.get_item(id)?.ok_or_else(|| PyValueError::new_err("item vanished"))?;
            let t = v.cast::<PyTuple>()?;
            dense.insert(id, id_of.len());
            id_of.push(id);
            stats.push(parse_stat_pairs(&t.get_item(0)?)?);
            let entry = dict_of(&t.get_item(1)?)?;
            fxs.push(ItemFx::from_py(&entry)?);
            defs.push(DefItem::from_entry(&entry)?);
            let gl: Vec<String> = match groups.get_item(id)? {
                Some(g) => g.extract()?,
                None => Vec::new(),
            };
            gnames.push(gl);
        }
        // only the groups the pool can over-fill matter
        let mut members: HashMap<String, i64> = HashMap::new();
        for gl in &gnames {
            for g in gl {
                *members.entry(g.clone()).or_insert(0) += 1;
            }
        }
        let mut gix: HashMap<String, usize> = HashMap::new();
        let mut cap_list = Vec::new();
        let mut item_groups = Vec::new();
        for gl in &gnames {
            let mut out = Vec::new();
            for g in gl {
                let cap = match caps.get_item(g)? {
                    Some(c) => c.extract::<i64>()?,
                    None => 99,
                };
                if members[g] <= cap {
                    continue;
                }
                let ix = *gix.entry(g.clone()).or_insert_with(|| {
                    cap_list.push(cap);
                    cap_list.len() - 1
                });
                out.push(ix);
            }
            item_groups.push(out);
        }
        if cap_list.len() > 64 {
            return Err(PyValueError::new_err("more than 64 violable item groups in the pool"));
        }
        let to_dense = |i: &u32| -> PyResult<usize> {
            dense.get(i).copied().ok_or_else(|| PyValueError::new_err(format!("item {i} is not in the pool")))
        };
        let free: Vec<usize> = free.iter().map(to_dense).collect::<PyResult<_>>()?;
        let classes: Vec<Vec<usize>> = classes.iter()
            .map(|c| c.iter().map(to_dense).collect::<PyResult<Vec<_>>>())
            .collect::<PyResult<_>>()?;
        let mut ord = vec![0i64; id_of.len()];
        for (k, v) in order.iter() {
            if let Some(&d) = dense.get(&k.extract::<u32>()?) {
                ord[d] = v.extract()?;
            }
        }
        let mut atk = Vec::new();
        for a in attackers.try_iter()? {
            let a = a?;
            let t = a.cast::<PyTuple>()?;
            atk.push(Attacker::from_py(t.get_item(0)?.extract()?, &dict_of(&t.get_item(1)?)?)?);
        }
        if atk.is_empty() || atk.len() > MAX_A {
            return Err(PyValueError::new_err("one to six attackers"));
        }
        if slots < 1 || slots > MAX_ITEMS {
            return Err(PyValueError::new_err("slots out of range"));
        }
        Ok(SurvCtx { spec, id_of, stats, fxs, defs, groups: item_groups, caps: cap_list, free,
                     classes, order: ord, attackers: atk, keep, slots, thresholds })
    }

    /// Every build (boots class x `slots - 1` free items) against every
    /// attacker: ({attacker key | "overall": [(key, ids, {attacker key:
    /// fight dict})]}, builds ranked). `workers` 0 means every core.
    fn run<'py>(&self, py: Python<'py>, overall: String, workers: usize)
        -> PyResult<(Bound<'py, PyDict>, i64)> {
        let n_free = self.slots - 1;
        let nf = self.free.len();
        if n_free > nf {
            return Err(PyValueError::new_err("more slots than pool items"));
        }
        // tasks: the combinations of the first two free items (or one, or none)
        let p = n_free.min(2);
        let mut prefixes: Vec<Vec<usize>> = Vec::new();
        let mut idx: Vec<usize> = (0..p).collect();
        if p == 0 {
            prefixes.push(Vec::new());
        } else {
            loop {
                prefixes.push(idx.clone());
                let mut i = p;
                let mut done = true;
                while i > 0 {
                    i -= 1;
                    if idx[i] != i + nf - p {
                        done = false;
                        break;
                    }
                }
                if done {
                    break;
                }
                idx[i] += 1;
                for j in i + 1..p {
                    idx[j] = idx[j - 1] + 1;
                }
            }
        }
        let n_lists = self.attackers.len() + 1;
        let keep = self.keep;
        let workers = if workers == 0 {
            std::thread::available_parallelism().map(|x| x.get()).unwrap_or(1)
        } else {
            workers
        }.clamp(1, prefixes.len().max(1));
        let counter = AtomicUsize::new(0);
        let result: Result<(Vec<Vec<Row>>, i64), String> = py.detach(|| {
            std::thread::scope(|s| {
                let handles: Vec<_> = (0..workers).map(|_| {
                    let counter = &counter;
                    let prefixes = &prefixes;
                    s.spawn(move || -> Result<(Vec<Vec<Row>>, i64), String> {
                        let mut sims: Vec<Sim> = self.attackers.iter().map(|a| a.sim())
                            .collect::<Result<_, _>>()?;
                        let durations: Vec<f64> =
                            self.attackers.iter().map(|a| a.duration).collect();
                        let mut log = Vec::new();
                        let mut lists: Vec<Vec<Row>> = (0..n_lists).map(|_| Vec::new()).collect();
                        // each list's keep-th key since its last cut: a row
                        // strictly worse can never place
                        let mut bar: Vec<Option<[f64; 3]>> = vec![None; n_lists];
                        let mut count = 0i64;
                        loop {
                            let ti = counter.fetch_add(1, Ordering::Relaxed);
                            if ti >= prefixes.len() {
                                break;
                            }
                            let prefix = &prefixes[ti];
                            let start = prefix.last().map(|&x| x + 1).unwrap_or(0);
                            let k = n_free - prefix.len();
                            let tail: Vec<usize> = (start..nf).collect();
                            if k > tail.len() {
                                continue;
                            }
                            let mut ix: Vec<usize> = (0..k).collect();
                            loop {
                                let mut rest: Vec<usize> =
                                    prefix.iter().map(|&i| self.free[i]).collect();
                                rest.extend(ix.iter().map(|&j| self.free[tail[j]]));
                                if let Some(counts) = self.counts(&rest) {
                                    for members in &self.classes {
                                        let legal: Vec<usize> = members.iter().copied()
                                            .filter(|&b| self.fits(&counts, b)).collect();
                                        if legal.is_empty() {
                                            continue;
                                        }
                                        count += legal.len() as i64;
                                        let fights = self.score(&mut sims, &mut log, legal[0], &rest,
                                                                false)?;
                                        let keys = self.keys(&fights, &durations);
                                        for &b in &legal {
                                            for li in 0..n_lists {
                                                if let Some(k) = &bar[li] {
                                                    if cmp_key(&keys[li], k)
                                                        == std::cmp::Ordering::Greater {
                                                        continue;
                                                    }
                                                }
                                                let mut ids = vec![self.id_of[b]];
                                                ids.extend(rest.iter().map(|&i| self.id_of[i]));
                                                lists[li].push(Row { keys, place: self.place(b, &rest),
                                                                     ids });
                                                // cut to `keep` once past four times that
                                                if lists[li].len() > 4 * keep {
                                                    cut(&mut lists[li], li, keep);
                                                    bar[li] = lists[li].last().map(|r| r.keys[li]);
                                                }
                                            }
                                        }
                                    }
                                }
                                // advance the tail combination
                                let mut i = k;
                                let mut done = true;
                                while i > 0 {
                                    i -= 1;
                                    if ix[i] != i + tail.len() - k {
                                        done = false;
                                        break;
                                    }
                                }
                                if done {
                                    break;
                                }
                                ix[i] += 1;
                                for j in i + 1..k {
                                    ix[j] = ix[j - 1] + 1;
                                }
                            }
                        }
                        for (li, lst) in lists.iter_mut().enumerate() {
                            cut(lst, li, keep);
                        }
                        Ok((lists, count))
                    })
                }).collect();
                let mut all: Vec<Vec<Row>> = (0..n_lists).map(|_| Vec::new()).collect();
                let mut count = 0i64;
                for h in handles {
                    let (lists, n) = h.join().expect("a survival fight panicked")?;
                    count += n;
                    for (li, lst) in lists.into_iter().enumerate() {
                        all[li].extend(lst);
                    }
                }
                for (li, lst) in all.iter_mut().enumerate() {
                    cut(lst, li, keep);
                }
                Ok((all, count))
            })
        });
        let (mut lists, count) = result.map_err(PyValueError::new_err)?;
        // the rows that placed get their fights in full, breakdowns and all;
        // a build on several lists is fought once
        let mut sims: Vec<Sim> = self.attackers.iter().map(|a| a.sim())
            .collect::<Result<_, _>>().map_err(PyValueError::new_err)?;
        let mut log = Vec::new();
        let dense: HashMap<u32, usize> = self.id_of.iter().enumerate().map(|(i, &id)| (id, i)).collect();
        let mut fought: HashMap<Vec<u32>, Bound<'py, PyDict>> = HashMap::new();
        let out = PyDict::new(py);
        for (li, lst) in lists.iter_mut().enumerate() {
            let name = if li < self.attackers.len() { self.attackers[li].key.clone() } else { overall.clone() };
            let rows = PyList::empty(py);
            for row in lst.iter() {
                let fights_d = match fought.get(&row.ids) {
                    Some(d) => d.clone(),
                    None => {
                        let di: Vec<usize> = row.ids.iter().map(|i| dense[i]).collect();
                        let fights = self.score(&mut sims, &mut log, di[0], &di[1..], true)
                            .map_err(PyValueError::new_err)?;
                        let d = PyDict::new(py);
                        for (a, (r, x)) in self.attackers.iter().zip(fights.iter()) {
                            let f = surv_fight_to_py(py, r, a.duration)?;
                            f.set_item("threshold", *x)?;
                            d.set_item(&a.key, f)?;
                        }
                        fought.insert(row.ids.clone(), d.clone());
                        d
                    }
                };
                let k = row.keys[li];
                let key = PyTuple::new(py, [k[0], k[1], k[2]])?;
                let ids = PyList::new(py, row.ids.iter())?;
                rows.append(PyTuple::new(py, [key.into_any(), ids.into_any(), fights_d.into_any()])?)?;
            }
            out.set_item(name, rows)?;
        }
        Ok((out, count))
    }
}
