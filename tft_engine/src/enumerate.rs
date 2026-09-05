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
    let fx = build_fx(spec.role, items, &spec.traits, spec.unit.has_forms, spec.unit.attack);
    let kit = spec.kit_for(fx.form);
    let drv = drivers.for_form(fx.form).clone();
    let sheet = Sheet::new(spec, kit, &fx);
    let dummies = make_dummies(spec);
    let mut f = Fight::new(spec, kit, sheet, fx, dummies, drv);
    if trace {
        f.trace = Some(Vec::new());
    }
    // the rows report the opening attack damage, ability power, attack
    // speed, health and resists, but the sheet's crit, precision, omnivamp
    // and mana as they stand after the fight (a driver's init or cast may
    // have changed them): what tft._sim_task read off the Python sheet
    let mut opening = f.opening();
    let res = f.run();
    opening.crit = f.sheet.crit_chance;
    opening.crit_mult = f.sheet.crit_mult;
    opening.precision = f.sheet.precision;
    opening.durability = f.sheet.durability;
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
/// spare, the rest by damage dealt; tanks by hold time, then denied, then
/// damage. Compared lexicographically like Python's tuples.
pub fn rank_key(res: &FightResult, objective: Objective) -> [f64; 4] {
    if objective == Objective::Tank {
        return [-res.alive_time, -res.denied, -res.total, 0.0];
    }
    match res.kill_time {
        Some(kt) => [0.0, kt, -res.raw_total, 0.0],
        None => [1.0, 0.0, -res.total, -res.raw_total],
    }
}

pub fn cmp_key(a: &[f64; 4], b: &[f64; 4]) -> Ordering {
    for i in 0..4 {
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

/// tft.enumerate_builds: every build of the pool, sorted best first; the
/// top `top` rows come back with the build count.
pub fn run_cell<D: Driver>(spec: &CellSpec, top: usize, workers: usize) -> (usize, Vec<Row>) {
    let combos = combos(&spec.pool);
    let n = combos.len();
    if n == 0 {
        return (0, Vec::new());
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
    let keys: Vec<[f64; 4]> = results.iter()
        .map(|r| rank_key(&r.as_ref().unwrap().1, spec.unit.objective)).collect();
    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by(|&a, &b| {
        cmp_key(&keys[a], &keys[b]).then_with(|| {
            let ra = combos[a].map(|i| api_rank[i]);
            let rb = combos[b].map(|i| api_rank[i]);
            ra.cmp(&rb)
        })
    });
    let rows = order.iter().take(top).map(|&i| {
        let (opening, res) = results[i].take().unwrap();
        Row { combo: combos[i], opening, res }
    }).collect();
    (n, rows)
}
