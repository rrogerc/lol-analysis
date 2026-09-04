//! The enumeration's inner loop — builds.py's `_enum_task`: every item
//! combination of a block, each boots class against every target, pruned
//! by the bounds the parent publishes in shared memory (see _Bounds there).
//! The parent still splits the work, merges and publishes; only the loop
//! moved here.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use std::collections::{HashMap, HashSet};
use std::rc::Rc;
use std::sync::atomic::{AtomicU64, Ordering};

use crate::fight::{simulate, FightResult, Opts, Target};
use crate::fsum::geo_mean;
use crate::fx::{Fx, ItemFx};
use crate::kit::Kit;
use crate::num::*;
use crate::pyget::*;
use crate::sheet::{parse_stat_pairs, resolve, ChampBase, Sheet, SK};

/// Slack on the kill-time cuts: a stopped fight must belong to a build that
/// is worse than the keep-th best by more than rounding could account for.
const PRUNE_SLACK: f64 = 1.0 + 1e-9;
/// Doubles per key in the bounds table (see builds._Bounds.WIDTH).
const WIDTH: usize = 8;

struct Item {
    stats: Vec<(SK, f64)>,
    fx: ItemFx,
    price: i64,
    groups: Vec<usize>,
}

/// A build's place in the enumeration: its items' places, then its boots'.
type Place = (Vec<i64>, i64);

struct Row {
    key: [f64; 3],
    place: Place,
    ids: Vec<u32>,
    rs: Rc<Vec<Option<FightResult>>>,
}

#[pyclass(frozen)]
pub struct Ctx {
    base: ChampBase,
    level: i64,
    ranks: Ranks,
    kit: Kit,
    use_ult: bool,
    prestacked: bool,
    items: HashMap<u32, Item>,
    caps: Vec<i64>,
    targets: Vec<Target>,
    target_keys: Vec<String>,
    overall: Option<String>,
    keep: usize,
    budget: Option<i64>,
    required: Vec<u32>,
    free: Vec<u32>,
    boots: Vec<u32>,
    grouped_boots: HashSet<u32>,
    partitions_busy: Vec<Vec<u32>>,
    partitions_calm: Vec<Vec<u32>>,
    energized: HashSet<u32>,
    order: HashMap<u32, i64>,
}

unsafe impl Send for Ctx {}
unsafe impl Sync for Ctx {}

fn kill_time(r: &FightResult, duration: f64) -> Option<f64> {
    if r.ttk.is_some() {
        return r.ttk_exp;
    }
    if r.dps > 0.0 {
        Some(duration + r.hp_left / r.dps)
    } else {
        None
    }
}

fn rank_key(r: &FightResult) -> [f64; 3] {
    match r.ttk {
        Some(_) => [0.0, r.ttk_exp.unwrap(), r.ttk_eff.unwrap()],
        None => [1.0, INF, -r.total],
    }
}

fn overall_key(rs: &[Option<FightResult>], targets: &[Target]) -> [f64; 3] {
    let fights: Vec<&FightResult> = rs.iter().map(|r| r.as_ref().expect("all fought")).collect();
    let unkilled = fights.iter().filter(|r| r.ttk.is_none()).count() as f64;
    let times: Vec<Option<f64>> =
        fights.iter().zip(targets).map(|(r, t)| kill_time(r, t.duration)).collect();
    if times.iter().any(|t| t.is_none()) {
        return [unkilled, INF, INF];
    }
    let times: Vec<f64> = times.into_iter().map(|t| t.unwrap()).collect();
    let effs: Vec<f64> = fights
        .iter()
        .zip(&times)
        .map(|(r, &t)| if r.ttk.is_some() { r.ttk_eff.unwrap() } else { t })
        .collect();
    [unkilled, geo_mean(&times), geo_mean(&effs)]
}

fn cmp_rows(a: &Row, b: &Row) -> std::cmp::Ordering {
    for i in 0..3 {
        match a.key[i].partial_cmp(&b.key[i]).expect("no NaN keys") {
            std::cmp::Ordering::Equal => {}
            o => return o,
        }
    }
    a.place.cmp(&b.place)
}

/// Sort a result list best-first and cut it to `keep`.
fn cut(lst: &mut Vec<Row>, keep: usize) {
    lst.sort_by(cmp_rows);
    lst.truncate(keep);
}

/// Bound a running result list once it has grown past 4x `keep`.
fn keep_best(lst: &mut Vec<Row>, keep: usize) {
    if lst.len() > 4 * keep {
        cut(lst, keep);
    }
}

impl Ctx {
    fn place(&self, ids: &[u32]) -> Place {
        if ids.is_empty() {
            return (Vec::new(), -1);
        }
        let items = ids[1..].iter().map(|&i| *self.order.get(&i).unwrap_or(&(i as i64))).collect();
        let boots = *self.order.get(&ids[0]).unwrap_or(&(ids[0] as i64));
        (items, boots)
    }

    fn legal(&self, ids: &[u32]) -> bool {
        let mut counts = [0i64; 64];
        for &i in ids {
            for &g in &self.items[&i].groups {
                counts[g] += 1;
                if counts[g] > self.caps[g] {
                    return false;
                }
            }
        }
        true
    }

    fn resolve_sheet(&self, ids: &[u32]) -> Sheet {
        let items: Vec<(&[(SK, f64)], &ItemFx)> =
            ids.iter().map(|i| { let it = &self.items[i]; (it.stats.as_slice(), &it.fx) }).collect();
        resolve(&self.base, self.level, &items, self.kit.crimson_pact)
    }
}

#[pymethods]
impl Ctx {
    #[new]
    #[allow(clippy::too_many_arguments)]
    fn new(base: &Bound<'_, PyDict>, level: i64, ranks: &Bound<'_, PyDict>, kit: &Bound<'_, PyDict>,
           items: &Bound<'_, PyDict>, groups: &Bound<'_, PyDict>, caps: &Bound<'_, PyDict>,
           targets: &Bound<'_, PyAny>, overall: Option<String>, keep: usize, budget: Option<i64>,
           required: Vec<u32>, free: Vec<u32>, boots: Vec<u32>, partitions: &Bound<'_, PyAny>,
           energized: Vec<u32>, order: &Bound<'_, PyDict>, use_ult: bool, prestacked: bool)
        -> PyResult<Ctx> {
        let ranks = Ranks { q: reqi(ranks, "Q")?, w: reqi(ranks, "W")?, e: reqi(ranks, "E")?,
                            r: reqi(ranks, "R")? };
        // group names -> dense indices, caps per index (99 when unknown)
        let mut group_ix: HashMap<String, usize> = HashMap::new();
        let mut cap_list: Vec<i64> = Vec::new();
        let mut item_map = HashMap::new();
        let mut group_of: HashMap<u32, Vec<String>> = HashMap::new();
        for (k, v) in groups.iter() {
            group_of.insert(k.extract::<u32>()?, v.extract::<Vec<String>>()?);
        }
        // a group only matters if the pool holds more of it than a build may
        // own (8 of Riot's 81 groups here); the rest can never be violated
        let mut members: HashMap<&str, i64> = HashMap::new();
        for (k, _) in items.iter() {
            let id: u32 = k.extract()?;
            if let Some(gl) = group_of.get(&id) {
                for g in gl {
                    *members.entry(g.as_str()).or_insert(0) += 1;
                }
            }
        }
        let mut cap_of: HashMap<String, i64> = HashMap::new();
        for name in members.keys() {
            let cap = match get(caps, name)? {
                Some(c) => c.extract::<i64>()?,
                None => 99,
            };
            cap_of.insert(name.to_string(), cap);
        }
        for (k, v) in items.iter() {
            let id: u32 = k.extract()?;
            let t = v.cast::<PyTuple>()?;
            let stats = parse_stat_pairs(&t.get_item(0)?)?;
            let fxd = dict_of(&t.get_item(1)?)?;
            let price: i64 = t.get_item(2)?.extract()?;
            let mut gs = Vec::new();
            if let Some(gl) = group_of.get(&id) {
                for name in gl {
                    let cap = cap_of[name];
                    if members[name.as_str()] <= cap {
                        continue;
                    }
                    let ix = match group_ix.get(name) {
                        Some(&ix) => ix,
                        None => {
                            let ix = cap_list.len();
                            cap_list.push(cap);
                            group_ix.insert(name.clone(), ix);
                            ix
                        }
                    };
                    gs.push(ix);
                }
            }
            item_map.insert(id, Item { stats, fx: ItemFx::from_py(&fxd)?, price, groups: gs });
        }
        if cap_list.len() > 64 {
            return Err(PyValueError::new_err("more than 64 violable item groups in the pool"));
        }
        let mut tg = Vec::new();
        let mut keys = Vec::new();
        for t in targets.try_iter()? {
            let t = t?;
            let tup = t.cast::<PyTuple>()?;
            keys.push(tup.get_item(0)?.extract::<String>()?);
            tg.push(Target {
                hp: tup.get_item(1)?.extract()?,
                armor: tup.get_item(2)?.extract()?,
                mr: tup.get_item(3)?.extract()?,
                duration: tup.get_item(4)?.extract()?,
                bonus_hp: tup.get_item(5)?.extract()?,
            });
        }
        let parts = partitions.cast::<PyTuple>()?;
        let busy: Vec<Vec<u32>> = parts.get_item(0)?.extract()?;
        let calm: Vec<Vec<u32>> = parts.get_item(1)?.extract()?;
        let mut ord = HashMap::new();
        for (k, v) in order.iter() {
            ord.insert(k.extract::<u32>()?, v.extract::<i64>()?);
        }
        let grouped_boots: HashSet<u32> =
            boots.iter().copied().filter(|b| !item_map[b].groups.is_empty()).collect();
        Ok(Ctx {
            base: ChampBase::from_py(base)?,
            level,
            ranks,
            kit: Kit::from_py(kit)?,
            use_ult,
            prestacked,
            items: item_map,
            caps: cap_list,
            targets: tg,
            target_keys: keys,
            overall,
            keep,
            budget,
            required,
            free,
            boots,
            grouped_boots,
            partitions_busy: busy,
            partitions_calm: calm,
            energized: energized.into_iter().collect(),
            order: ord,
        })
    }

    /// One task's builds: ({key: [(sort key, ids, {target: fight})]}, ranked
    /// count). `task` is ("block", size, prefix) or ("builds", [ids, ...]);
    /// `bounds_addr` is the address of the parent's bounds table.
    fn run_block<'py>(&self, py: Python<'py>, task: &Bound<'py, PyAny>, bounds_addr: usize)
        -> PyResult<(Bound<'py, PyDict>, i64)> {
        let tup = task.cast::<PyTuple>()?;
        let kind: String = tup.get_item(0)?.extract()?;
        let n_t = self.targets.len();
        let has_overall = self.overall.is_some();
        let n_keys = n_t + if has_overall { 1 } else { 0 };
        let table = bounds_addr as *const u64;
        let read = |i: usize| -> f64 {
            // the parent writes aligned doubles; a torn read is impossible
            // and a stale one only ever looser (see _Bounds.update)
            let a = unsafe { AtomicU64::from_ptr(table.add(i) as *mut u64) };
            f64::from_bits(a.load(Ordering::Relaxed))
        };
        let mut out: Vec<Vec<Row>> = (0..n_keys).map(|_| Vec::new()).collect();
        let mut n: i64 = 0;

        // the work: (rest items, boots classes or None for the partitions)
        let mut work: Vec<(Vec<u32>, Option<Vec<Vec<u32>>>)> = Vec::new();
        if kind == "block" {
            let size: usize = tup.get_item(1)?.extract()?;
            let prefix: Vec<usize> = tup.get_item(2)?.extract()?;
            let mut stem: Vec<u32> = self.required.clone();
            stem.extend(prefix.iter().map(|&i| self.free[i]));
            let tail: &[u32] = if prefix.is_empty() { &self.free } else { &self.free[prefix[prefix.len() - 1] + 1..] };
            let k = size - prefix.len();
            for combo in combinations(tail, k) {
                let mut rest = stem.clone();
                rest.extend(combo);
                work.push((rest, None));
            }
        } else if kind == "builds" {
            for b in tup.get_item(1)?.try_iter()? {
                let ids: Vec<u32> = b?.extract()?;
                work.push((ids[1..].to_vec(), Some(vec![vec![ids[0]]])));
            }
        } else {
            return Err(PyValueError::new_err(format!("unknown task kind {kind}")));
        }

        for (rest, classes) in work {
            if !self.legal(&rest) {
                continue;
            }
            let classes: &[Vec<u32>] = match &classes {
                Some(c) => c,
                None => if self.energized.iter().any(|e| rest.contains(e)) { &self.partitions_busy } else { &self.partitions_calm },
            };
            // the bounds, once per item combination
            let tb: Vec<(f64, f64, f64)> =
                (0..n_t).map(|i| (read(i * WIDTH), read(i * WIDTH + 1), read(i * WIDTH + 2))).collect();
            let (mut o_max, mut o_g, mut o_ids_place) = (INF, INF, (Vec::new(), -1i64));
            if has_overall {
                let b = n_t * WIDTH;
                o_max = read(b);
                o_g = read(b + 1);
                let ids: Vec<u32> = (0..6).map(|j| read(b + 2 + j) as i64).filter(|&x| x != 0).map(|x| x as u32).collect();
                o_ids_place = self.place(&ids);
            }
            for members in classes {
                let mut legal: Vec<Vec<u32>> = Vec::with_capacity(members.len());
                for &b in members {
                    let mut ids = Vec::with_capacity(rest.len() + 1);
                    ids.push(b);
                    ids.extend_from_slice(&rest);
                    if !self.grouped_boots.contains(&b) || self.legal(&ids) {
                        legal.push(ids);
                    }
                }
                if let Some(budget) = self.budget {
                    legal.retain(|ids| ids.iter().map(|i| self.items[i].price).sum::<i64>() <= budget);
                }
                if legal.is_empty() {
                    continue;
                }
                n += legal.len() as i64;
                let sheet = self.resolve_sheet(&legal[0]);
                let fx = Fx::merge(legal[0].iter().map(|i| &self.items[i].fx));
                let mut rs: Vec<Option<FightResult>> = Vec::with_capacity(n_t);
                let (mut unkilled, mut prod) = (0i64, 1.0f64);
                // once the build can no longer make the overall list, each
                // fight only has its own target's list to make
                let mut out_of_overall = !has_overall;
                let min_place = || legal.iter().map(|ids| self.place(ids)).min().unwrap();
                for i in 0..n_t {
                    let tg = &self.targets[i];
                    let mut stop = INF;
                    if !out_of_overall
                        && ((unkilled as f64) > o_max
                            || ((unkilled as f64) == o_max && o_max > 0.0 && min_place() > o_ids_place))
                    {
                        out_of_overall = true;
                    }
                    if out_of_overall {
                        stop = tb[i].0 * PRUNE_SLACK;
                    } else if o_max == 0.0 {
                        // every target has to die: the geometric mean of the
                        // kill times bounds this fight
                        let mut rem = prod;
                        for j in i + 1..n_t {
                            rem *= tb[j].2;
                        }
                        if rem > 0.0 {
                            stop = pymax(tb[i].0, o_g.powf(n_t as f64) / rem) * PRUNE_SLACK;
                        }
                    }
                    let r = simulate(&sheet, &self.kit, &fx, self.level, self.ranks, tg,
                                     Opts { use_ult: self.use_ult, prestacked: self.prestacked,
                                            stop_after: stop, breakdown: false, blend: true })
                        .map_err(PyValueError::new_err)?;
                    match &r {
                        None => {
                            // cut: off this target's list and the overall
                            out_of_overall = true;
                        }
                        Some(f) => {
                            if f.ttk.is_none() {
                                unkilled += 1;
                            }
                            prod *= kill_time(f, tg.duration).unwrap_or(0.0);
                        }
                    }
                    rs.push(r);
                }
                let rs = Rc::new(rs);
                for i in 0..n_t {
                    let Some(r) = &rs[i] else { continue };
                    let (t_max, tot_min, _) = tb[i];
                    if r.ttk.is_some() {
                        if r.ttk_exp.unwrap() > t_max {
                            continue;
                        }
                    } else if r.total < tot_min {
                        continue;
                    }
                    let key = rank_key(r);
                    for ids in &legal {
                        out[i].push(Row { key, place: self.place(ids), ids: ids.clone(), rs: rs.clone() });
                    }
                    keep_best(&mut out[i], self.keep);
                }
                if has_overall && !out_of_overall {
                    let key = overall_key(&rs, &self.targets);
                    let lead = (key[0], key[1]);
                    let take: Vec<&Vec<u32>> = if lead < (o_max, o_g) {
                        legal.iter().collect()
                    } else if lead == (o_max, o_g) {
                        if o_max == 0.0 {
                            legal.iter().collect()
                        } else {
                            legal.iter().filter(|ids| self.place(ids) <= o_ids_place).collect()
                        }
                    } else {
                        Vec::new()
                    };
                    for ids in take {
                        out[n_t].push(Row { key, place: self.place(ids), ids: ids.clone(), rs: rs.clone() });
                    }
                    keep_best(&mut out[n_t], self.keep);
                }
            }
        }
        for lst in out.iter_mut() {
            cut(lst, self.keep);
        }

        // hand the rows to Python: one fight dict per class, shared by its
        // members (the post-pass re-fights each distinct one once)
        let result = PyDict::new(py);
        let mut shared: HashMap<*const Vec<Option<FightResult>>, Bound<'py, PyDict>> = HashMap::new();
        for (ki, lst) in out.iter().enumerate() {
            let name = if ki < n_t { &self.target_keys[ki] } else { self.overall.as_ref().unwrap() };
            let rows = PyList::empty(py);
            for row in lst {
                let ptr = Rc::as_ptr(&row.rs);
                let rs_dict = match shared.get(&ptr) {
                    Some(d) => d.clone(),
                    None => {
                        let d = PyDict::new(py);
                        for (ti, r) in row.rs.iter().enumerate() {
                            match r {
                                Some(f) => d.set_item(&self.target_keys[ti], fight_to_py(py, f)?)?,
                                None => d.set_item(&self.target_keys[ti], py.None())?,
                            }
                        }
                        shared.insert(ptr, d.clone());
                        d
                    }
                };
                let key = PyTuple::new(py, [
                    (row.key[0] as i64).into_pyobject(py)?.into_any(),
                    row.key[1].into_pyobject(py)?.into_any(),
                    row.key[2].into_pyobject(py)?.into_any(),
                ])?;
                let ids = PyList::new(py, row.ids.iter().copied())?;
                rows.append(PyTuple::new(py, [key.into_any(), ids.into_any(), rs_dict.into_any()])?)?;
            }
            result.set_item(name, rows)?;
        }
        Ok((result, n))
    }
}

/// itertools.combinations(items, k), in the same order.
fn combinations(items: &[u32], k: usize) -> Vec<Vec<u32>> {
    let n = items.len();
    let mut out = Vec::new();
    if k > n {
        return out;
    }
    let mut idx: Vec<usize> = (0..k).collect();
    loop {
        out.push(idx.iter().map(|&i| items[i]).collect());
        // advance
        let mut i = k;
        loop {
            if i == 0 {
                return out;
            }
            i -= 1;
            if idx[i] != i + n - k {
                break;
            }
            if i == 0 {
                return out;
            }
        }
        idx[i] += 1;
        for j in i + 1..k {
            idx[j] = idx[j - 1] + 1;
        }
    }
}

pub fn fight_to_py<'py>(py: Python<'py>, f: &FightResult) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("total", f.total)?;
    d.set_item("dps", f.dps)?;
    d.set_item("ttk", f.ttk)?;
    d.set_item("ttk_eff", f.ttk_eff)?;
    d.set_item("ttk_exp", f.ttk_exp)?;
    d.set_item("attacks", f.attacks)?;
    d.set_item("phantom_hits", f.phantom_hits)?;
    d.set_item("hp_left", f.hp_left)?;
    let bd = PyDict::new(py);
    for (src, dmg) in &f.breakdown {
        bd.set_item(crate::fx::source_name(*src), *dmg)?;
    }
    d.set_item("breakdown", bd)?;
    Ok(d)
}
