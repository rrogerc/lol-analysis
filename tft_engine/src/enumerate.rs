//! One build's fight from a cell spec, and every build of the pool: the
//! port of tft.simulate / enumerate_builds / rank_key. The enumeration
//! runs on all cores; results are written by build index, so the order
//! never depends on the scheduling.

use std::cmp::Ordering;
use std::sync::atomic::{AtomicUsize, Ordering as AtomicOrdering};

use crate::driver::Driver;
use crate::fight::{make_dummies, Fight, FightResult, Opening, Sheet};
use crate::fx::{build_fx, Form, ItemFx};
use crate::spec::{CellSpec, Objective};

/// A driver instance per kit (the form's rows and calcs differ), cloned
/// for every fight.
#[derive(Clone)]
pub struct Drivers<D> {
    base: D,
    ad: Option<D>,
    ap: Option<D>,
}

impl<D: Driver> Drivers<D> {
    pub fn new(spec: &CellSpec) -> Drivers<D> {
        Drivers {
            base: D::new(&spec.kit_base, &spec.unit),
            ad: spec.kit_ad.as_ref().map(|k| D::new(k, &spec.unit)),
            ap: spec.kit_ap.as_ref().map(|k| D::new(k, &spec.unit)),
        }
    }

    fn for_form(&self, form: Option<Form>) -> &D {
        match form {
            Some(Form::AD) => self.ad.as_ref().unwrap_or(&self.base),
            Some(Form::AP) => self.ap.as_ref().unwrap_or(&self.base),
            None => &self.base,
        }
    }
}

/// tft.simulate: one build's fight.
pub fn run_fight<D: Driver>(spec: &CellSpec, drivers: &Drivers<D>, items: &[&ItemFx], trace: bool)
    -> (Opening, FightResult) {
    let (opening, mut res) = run_fight_raw(spec, drivers, items, trace, 1.0);
    if spec.unit.objective == Objective::Tank && res.survival_capped {
        let (_, stress) = run_fight_raw(spec, drivers, items, false, 2.0);
        res.stress_alive_time = Some(stress.alive_time);
        res.stress_capped = stress.survival_capped;
    }
    (opening, res)
}

/// One pass of the chosen scenario; the stress run only scales incoming damage.
fn run_fight_raw<D: Driver>(spec: &CellSpec, drivers: &Drivers<D>, items: &[&ItemFx],
                            trace: bool, incoming_mult: f64) -> (Opening, FightResult) {
    let fx = build_fx(spec.role, items, &spec.traits, spec.unit.has_forms, spec.unit.attack);
    let kit = spec.kit_for(fx.form);
    let drv = drivers.for_form(fx.form).clone();
    let sheet = Sheet::new(spec, kit, &fx);
    let mut dummies = make_dummies(spec);
    if incoming_mult != 1.0 {
        for dummy in &mut dummies {
            dummy.ad *= incoming_mult;
            dummy.ability *= incoming_mult;
        }
    }
    let mut f = Fight::new(spec, kit, sheet, fx, dummies, drv);
    if trace {
        f.trace = Some(Vec::new());
    }
    D::init(&mut f);
    // the rows report the opening attack damage, ability power, attack
    // speed, health and resists, but the sheet's crit, precision, omnivamp
    // and mana as they stand after the fight (a driver's init or cast may
    // have changed them): what tft._sim_task read off the Python sheet
    let mut opening = f.opening();
    let res = f.run();
    opening.crit = f.sheet.crit_chance;
    opening.crit_mult = f.sheet.crit_mult;
    opening.precision = f.sheet.precision;
    opening.omnivamp = f.sheet.omnivamp;
    opening.form = f.sheet.form;
    opening.mana_start = f.sheet.mana_start;
    opening.mana_max = f.sheet.mana_max;
    (opening, res)
}

/// Every multiset of three pool items (a unique item at most once), in
/// itertools.combinations_with_replacement order.
pub fn combos(pool: &[ItemFx]) -> Vec<[usize; 3]> {
    let n = pool.len();
    let mut out = Vec::new();
    for i in 0..n {
        for j in i..n {
            for k in j..n {
                let c = [i, j, k];
                let ok = c.iter().all(|&x| !pool[x].unique
                                      || c.iter().filter(|&&y| y == x).count() <= 1);
                if ok {
                    out.push(c);
                }
            }
        }
    }
    out
}

/// tft.rank_key: carries and fighters by kill time then the damage to
/// spare, the rest by damage dealt. Capped tanks get a second survival check
/// at double pressure; tanks capped in both are tied. Other tank ties use
/// denied damage, then damage dealt. Compared like Python's tuples.
pub fn rank_key(res: &FightResult, objective: Objective) -> [f64; 6] {
    if objective == Objective::Tank {
        let capped = res.survival_capped;
        let both_capped = capped && res.stress_capped;
        return [-res.alive_time,
                if capped { -1.0 } else { 0.0 },
                if capped { -res.stress_alive_time.unwrap_or(0.0) } else { 0.0 },
                if capped && res.stress_capped { -1.0 } else { 0.0 },
                if both_capped { 0.0 } else { -res.denied },
                if both_capped { 0.0 } else { -res.total }];
    }
    match res.kill_time {
        Some(kt) => [0.0, kt, -res.raw_total, 0.0, 0.0, 0.0],
        None => [1.0, 0.0, -res.total, -res.raw_total, 0.0, 0.0],
    }
}

pub fn cmp_key(a: &[f64; 6], b: &[f64; 6]) -> Ordering {
    for i in 0..6 {
        match a[i].partial_cmp(&b[i]) {
            Some(Ordering::Equal) | None => continue,
            Some(o) => return o,
        }
    }
    Ordering::Equal
}

pub struct Row {
    pub combo: [usize; 3],
    pub opening: Opening,
    pub res: FightResult,
}

/// One exact zero-, one-, two- or three-item build for composition budgets.
pub struct LoadoutRow {
    pub combo: Vec<usize>,
    pub opening: Opening,
    pub res: FightResult,
}

/// Every legal loadout with at most three items, including the empty build.
/// No placeholder item fills an unused slot; unique items remain per-holder.
fn loadout_combos(pool: &[ItemFx]) -> Vec<Vec<usize>> {
    let mut out = vec![Vec::new()];
    for i in 0..pool.len() {
        out.push(vec![i]);
    }
    for i in 0..pool.len() {
        for j in i..pool.len() {
            if i != j || !pool[i].unique {
                out.push(vec![i, j]);
            }
        }
    }
    out.extend(combos(pool).into_iter().map(|combo| combo.to_vec()));
    out
}

/// Exhaustive item-budget options under one fully resolved trait context.
/// Each item count has its own best-first rows; the existing fight, stress
/// pass and API-name tie-break remain the same as ordinary full builds.
pub fn optimize_loadouts<D: Driver>(spec: &CellSpec, top: usize, workers: usize)
    -> (usize, [Vec<LoadoutRow>; 4]) {
    let combos = loadout_combos(&spec.pool);
    let n = combos.len();
    const CHUNK: usize = 32;
    let workers = if workers == 0 {
        std::thread::available_parallelism().map(|x| x.get()).unwrap_or(1)
    } else {
        workers
    }.clamp(1, n.div_ceil(CHUNK));
    let drivers = Drivers::<D>::new(spec);
    let evaluate = |idx: usize, drivers: &Drivers<D>| {
        let combo = &combos[idx];
        let items: Vec<&ItemFx> = combo.iter().map(|&i| &spec.pool[i]).collect();
        let (opening, res) = run_fight::<D>(spec, drivers, &items, false);
        (rank_key(&res, spec.unit.objective),
         LoadoutRow { combo: combo.clone(), opening, res })
    };
    // A composition warmer can parallelize unit contexts itself. A request
    // for one worker stays on the calling thread; other calls share bounded
    // batches rather than starting one OS thread for every combination.
    let evaluated: Vec<_> = if workers == 1 {
        (0..n).map(|idx| evaluate(idx, &drivers)).collect()
    } else {
        let counter = AtomicUsize::new(0);
        std::thread::scope(|s| {
            let handles: Vec<_> = (0..workers).map(|_| {
                let drivers = drivers.clone();
                let counter = &counter;
                let evaluate = &evaluate;
                s.spawn(move || {
                    let mut local = Vec::new();
                    loop {
                        let start = counter.fetch_add(CHUNK, AtomicOrdering::Relaxed);
                        if start >= n {
                            break;
                        }
                        for idx in start..(start + CHUNK).min(n) {
                            local.push(evaluate(idx, &drivers));
                        }
                    }
                    local
                })
            }).collect();
            handles.into_iter().flat_map(|h| h.join().expect("a fight panicked")).collect()
        })
    };

    let mut apis: Vec<(&str, usize)> = spec.pool.iter().enumerate()
        .map(|(i, item)| (item.api.as_str(), i)).collect();
    apis.sort();
    let mut api_rank = vec![0usize; spec.pool.len()];
    for (rank, (_, i)) in apis.iter().enumerate() {
        api_rank[*i] = rank;
    }
    let mut groups: [Vec<_>; 4] = std::array::from_fn(|_| Vec::new());
    for evaluated in evaluated {
        groups[evaluated.1.combo.len()].push(evaluated);
    }
    let rows = groups.map(|mut group| {
        group.sort_by(|(a_key, a), (b_key, b)| {
            cmp_key(a_key, b_key).then_with(|| {
                a.combo.iter().map(|&i| api_rank[i])
                    .cmp(b.combo.iter().map(|&i| api_rank[i]))
            })
        });
        group.truncate(top);
        group.into_iter().map(|(_, row)| row).collect()
    });
    (n, rows)
}

/// The unrounded measurements needed to compare item cores across every
/// legal completion, including builds below the displayed rows.
pub struct Score<const N: usize = 3> {
    pub combo: [usize; N],
    pub kill_time: Option<f64>,
    pub total: f64,
    pub alive_time: f64,
    pub survival_capped: bool,
    pub stress_alive_time: Option<f64>,
    pub stress_capped: bool,
}

impl<const N: usize> Score<N> {
    fn from_result(combo: [usize; N], res: &FightResult) -> Self {
        Score {
            combo,
            kill_time: res.kill_time,
            total: res.total,
            alive_time: res.alive_time,
            survival_capped: res.survival_capped,
            stress_alive_time: res.stress_alive_time,
            stress_capped: res.stress_capped,
        }
    }
}

/// tft.enumerate_builds: every build of the pool, sorted best first; the
/// top `top` rows come back with the build count.
pub fn run_cell<D: Driver>(spec: &CellSpec, top: usize, workers: usize) -> (usize, Vec<Row>) {
    let mut rows = ranked_rows::<D>(spec, workers);
    let n = rows.len();
    rows.truncate(top);
    (n, rows)
}

/// The same fights and ranking as run_cell, with compact scores for every
/// legal build. Scores are captured before the rich rows are truncated.
pub fn analyze_cell<D: Driver>(spec: &CellSpec, top: usize, workers: usize)
    -> (usize, Vec<Row>, Vec<Score>) {
    let mut rows = ranked_rows::<D>(spec, workers);
    let n = rows.len();
    let scores = rows.iter().map(|row| Score::from_result(row.combo, &row.res)).collect();
    rows.truncate(top);
    (n, rows, scores)
}

/// Every legal two-item multiset, fought with exactly those two items in
/// the cell's unchanged scenario. Keep the same ranking as full builds so
/// a core's actual two-item spike can decide between shared completions.
pub fn score_pairs<D: Driver>(spec: &CellSpec, workers: usize) -> Vec<Score<2>> {
    let mut pairs = Vec::new();
    for i in 0..spec.pool.len() {
        for j in i..spec.pool.len() {
            if i != j || !spec.pool[i].unique {
                pairs.push([i, j]);
            }
        }
    }
    let n = pairs.len();
    if n == 0 {
        return Vec::new();
    }
    const CHUNK: usize = 32;
    let workers = if workers == 0 {
        std::thread::available_parallelism().map(|x| x.get()).unwrap_or(1)
    } else {
        workers
    }.clamp(1, n.div_ceil(CHUNK));
    let drivers = Drivers::<D>::new(spec);
    let evaluate = |idx: usize, drivers: &Drivers<D>| {
        let combo = pairs[idx];
        let items = [&spec.pool[combo[0]], &spec.pool[combo[1]]];
        let (_, res) = run_fight::<D>(spec, drivers, &items, false);
        (rank_key(&res, spec.unit.objective), Score::from_result(combo, &res))
    };
    // Cell workers already run in parallel during a warm. Avoid starting
    // another OS thread when this call requests one worker or one batch.
    let mut scores: Vec<_> = if workers == 1 {
        (0..n).map(|idx| evaluate(idx, &drivers)).collect()
    } else {
        let counter = AtomicUsize::new(0);
        std::thread::scope(|s| {
            let handles: Vec<_> = (0..workers).map(|_| {
                let drivers = drivers.clone();
                let counter = &counter;
                let evaluate = &evaluate;
                s.spawn(move || {
                    let mut local = Vec::new();
                    loop {
                        let start = counter.fetch_add(CHUNK, AtomicOrdering::Relaxed);
                        if start >= n {
                            break;
                        }
                        for idx in start..(start + CHUNK).min(n) {
                            local.push(evaluate(idx, &drivers));
                        }
                    }
                    local
                })
            }).collect();
            handles.into_iter().flat_map(|h| h.join().expect("a fight panicked")).collect()
        })
    };
    let mut apis: Vec<(&str, usize)> = spec.pool.iter().enumerate()
        .map(|(i, it)| (it.api.as_str(), i)).collect();
    apis.sort();
    let mut api_rank = vec![0usize; spec.pool.len()];
    for (rank, (_, i)) in apis.iter().enumerate() {
        api_rank[*i] = rank;
    }
    scores.sort_by(|(a_key, a), (b_key, b)| {
        cmp_key(a_key, b_key).then_with(|| {
            a.combo.map(|i| api_rank[i]).cmp(&b.combo.map(|i| api_rank[i]))
        })
    });
    scores.into_iter().map(|(_, score)| score).collect()
}

/// Shared enumeration and sorting for both public cell entry points.
fn ranked_rows<D: Driver>(spec: &CellSpec, workers: usize) -> Vec<Row> {
    let combos = combos(&spec.pool);
    let n = combos.len();
    if n == 0 {
        return Vec::new();
    }
    let workers = if workers == 0 {
        std::thread::available_parallelism().map(|x| x.get()).unwrap_or(1)
    } else {
        workers
    }.clamp(1, n);
    let counter = AtomicUsize::new(0);
    const CHUNK: usize = 32;
    let outs: Vec<Vec<(usize, Opening, FightResult)>> = std::thread::scope(|s| {
        let handles: Vec<_> = (0..workers).map(|_| {
            let counter = &counter;
            let combos = &combos;
            s.spawn(move || {
                let drivers = Drivers::<D>::new(spec);
                let mut local = Vec::new();
                loop {
                    let start = counter.fetch_add(CHUNK, AtomicOrdering::Relaxed);
                    if start >= n {
                        break;
                    }
                    for idx in start..(start + CHUNK).min(n) {
                        let c = combos[idx];
                        let items = [&spec.pool[c[0]], &spec.pool[c[1]], &spec.pool[c[2]]];
                        let (o, r) = run_fight::<D>(spec, &drivers, &items, false);
                        local.push((idx, o, r));
                    }
                }
                local
            })
        }).collect();
        handles.into_iter().map(|h| h.join().expect("a fight panicked")).collect()
    });
    let mut results: Vec<Option<(Opening, FightResult)>> = (0..n).map(|_| None).collect();
    for local in outs {
        for (idx, o, r) in local {
            results[idx] = Some((o, r));
        }
    }
    // the tie-break: the build's api names as a tuple, i.e. each item's
    // rank among the pool's apis
    let mut apis: Vec<(&str, usize)> = spec.pool.iter().enumerate().map(|(i, it)| (it.api.as_str(), i)).collect();
    apis.sort();
    let mut api_rank = vec![0usize; spec.pool.len()];
    for (r, (_, i)) in apis.iter().enumerate() {
        api_rank[*i] = r;
    }
    let keys: Vec<[f64; 6]> = results.iter()
        .map(|r| rank_key(&r.as_ref().unwrap().1, spec.unit.objective)).collect();
    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by(|&a, &b| {
        cmp_key(&keys[a], &keys[b]).then_with(|| {
            let ra = combos[a].map(|i| api_rank[i]);
            let rb = combos[b].map(|i| api_rank[i]);
            ra.cmp(&rb)
        })
    });
    order.iter().map(|&i| {
        let (opening, res) = results[i].take().unwrap();
        Row { combo: combos[i], opening, res }
    }).collect()
}

#[cfg(test)]
mod loadout_tests {
    use super::loadout_combos;
    use crate::fx::ItemFx;
    use std::collections::BTreeSet;

    fn item(unique: bool) -> ItemFx {
        ItemFx { unique, ..ItemFx::default() }
    }

    fn counts(pool: &[ItemFx]) -> [usize; 4] {
        let mut counts = [0; 4];
        for combo in loadout_combos(pool) {
            counts[combo.len()] += 1;
        }
        counts
    }

    #[test]
    fn full_craftable_pool_counts_every_item_budget_once() {
        let pool = vec![item(false); 35];
        let combos = loadout_combos(&pool);
        assert_eq!(counts(&pool), [1, 35, 630, 7770]);
        assert_eq!(combos.len(), 8436);
        assert_eq!(combos.iter().collect::<BTreeSet<_>>().len(), combos.len());
        assert!(combos.iter().all(|combo| combo.windows(2).all(|p| p[0] <= p[1])));
    }

    #[test]
    fn unique_items_do_not_repeat_at_any_item_budget() {
        let pool = vec![item(true), item(false), item(true)];
        let combos = loadout_combos(&pool);
        assert_eq!(counts(&pool), [1, 3, 4, 4]);
        assert!(combos.contains(&vec![1, 1, 1]));
        assert!(combos.contains(&vec![0, 1, 2]));
        for combo in combos {
            assert!(combo.iter().filter(|&&i| i == 0).count() <= 1);
            assert!(combo.iter().filter(|&&i| i == 2).count() <= 1);
        }
    }

    #[test]
    fn empty_and_small_pools_keep_an_actual_empty_build() {
        assert_eq!(loadout_combos(&[]), vec![Vec::<usize>::new()]);
        assert_eq!(counts(&[item(true)]), [1, 1, 0, 0]);
        assert_eq!(counts(&[item(false)]), [1, 1, 1, 1]);
        assert_eq!(counts(&[item(true), item(true)]), [1, 2, 1, 0]);
    }
}
