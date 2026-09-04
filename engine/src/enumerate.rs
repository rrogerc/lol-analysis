//! The enumeration's inner loop — builds.py's `_enum_task`: every item
//! combination of a block, each boots class against every target, pruned
//! by the bounds the parent publishes in shared memory (see _Bounds there).
//! The parent still splits the work, merges and publishes; only the loop
//! moved here.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use std::collections::HashMap;
use std::rc::Rc;
use std::sync::atomic::{AtomicU64, Ordering};

use crate::fight::{FightResult, Opts, Sim, Target};
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

/// A pool item's dense index. Every id a build can hold is interned once in
/// `Ctx::new`, so the loop indexes flat arrays instead of hashing Riot ids.
type Dense = u16;
/// Most items one build may hold, boots included (builds.py's `slots`).
const MAX_ITEMS: usize = 8;
/// Most targets one enumeration may score against.
const MAX_T: usize = 8;
/// Most members one boots class may hold.
const MAX_CLASS: usize = 16;

/// One pool item as it comes off the Python dict, before it is split into
/// the per-field arrays the loop reads.
struct PoolItem {
    stats: Vec<(SK, f64)>,
    fx: ItemFx,
    price: i64,
    groups: Vec<usize>,
}

/// A build's place in the enumeration: its items' places, then its boots'.
/// Ordered exactly like the `(Vec<i64>, i64)` this used to be — the item
/// places lexicographically (a prefix sorts first), then the boots'.
#[derive(Clone, Copy, Debug)]
struct Place {
    items: [i64; MAX_ITEMS],
    n: u8,
    boots: i64,
}

impl Place {
    /// What `place()` returns for an empty build.
    const EMPTY: Place = Place { items: [0; MAX_ITEMS], n: 0, boots: -1 };

    #[inline]
    fn items(&self) -> &[i64] {
        &self.items[..self.n as usize]
    }
}

impl Ord for Place {
    #[inline]
    fn cmp(&self, other: &Place) -> std::cmp::Ordering {
        self.items().cmp(other.items()).then_with(|| self.boots.cmp(&other.boots))
    }
}

impl PartialOrd for Place {
    #[inline]
    fn partial_cmp(&self, other: &Place) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl PartialEq for Place {
    #[inline]
    fn eq(&self, other: &Place) -> bool {
        self.cmp(other) == std::cmp::Ordering::Equal
    }
}

impl Eq for Place {}

struct Row {
    key: [f64; 3],
    place: Place,
    ids: [Dense; MAX_ITEMS],
    n_ids: u8,
    rs: Rc<Vec<Option<FightResult>>>,
}

/// One item combination's shared state: the bounds as they stood when it
/// was reached, and the effects its boots classes all merge to.
struct Combo<'r> {
    rest: &'r [Dense],
    tb: [(f64, f64, f64); MAX_T],
    o_max: f64,
    o_g: f64,
    check_boots: bool,
    shared_fx: bool,
}

/// One task in flight: the result lists it fills and the bounds table it
/// reads. Everything here is per-call; `Ctx` itself stays frozen.
struct Block<'a> {
    ctx: &'a Ctx,
    n_t: usize,
    has_overall: bool,
    table: *const u64,
    out: Vec<Vec<Row>>,
    n: i64,
    /// the overall row's tie-break ids as raw bits, and the place they
    /// resolve to: the parent rewrites them a few thousand times a run,
    /// not once per combination
    o_bits: [u64; 6],
    o_place: Place,
    o_seen: bool,
    /// Stormsurge's rolling damage window, lent to every fight of the block
    /// so the buffer is grown at most once for the whole task
    dmg_log: Vec<(f64, f64)>,
    /// `o_g.powf(n_t)`, keyed on `o_g`'s bits — the same libm call on the
    /// same operand returns the same double
    pow_bits: u64,
    pow_val: f64,
    pow_seen: bool,
}

impl Block<'_> {
    #[inline]
    fn read_bits(&self, i: usize) -> u64 {
        // the parent writes aligned doubles; a torn read is impossible
        // and a stale one only ever looser (see _Bounds.update)
        let a = unsafe { AtomicU64::from_ptr(self.table.add(i) as *mut u64) };
        a.load(Ordering::Relaxed)
    }

    #[inline]
    fn read(&self, i: usize) -> f64 {
        f64::from_bits(self.read_bits(i))
    }

    /// `o_g.powf(n_t as f64)`, recomputed only when `o_g` moves.
    #[inline]
    fn pow_n(&mut self, o_g: f64, n_t: usize) -> f64 {
        let bits = o_g.to_bits();
        if !self.pow_seen || bits != self.pow_bits {
            self.pow_val = o_g.powf(n_t as f64);
            self.pow_bits = bits;
            self.pow_seen = true;
        }
        self.pow_val
    }

    /// One item combination: its boots classes against every target.
    /// `only` forces a single-member class (the explicit-builds task).
    /// `stem` is (the group mask of the first n items of `rest`, n) when
    /// the caller folded a shared prefix already; (0, 0) otherwise.
    fn score(&mut self, rs: &mut [Option<FightResult>; MAX_T], rest: &[Dense],
             stem: (u64, usize), only: Option<Dense>, check_boots: bool, shared_fx: bool)
        -> PyResult<()> {
        let ctx = self.ctx;
        let ok = if ctx.caps_all_one {
            ctx.legal_from(stem.0, &rest[stem.1..])
        } else {
            ctx.legal(rest)
        };
        if !ok {
            return Ok(());
        }
        let n_t = self.n_t;
        // the bounds, once per item combination
        let mut c = Combo { rest, tb: [(0.0, 0.0, 0.0); MAX_T], o_max: INF, o_g: INF,
                            check_boots, shared_fx };
        // the rest's effects, merged at most once for the whole combination
        let mut rest_fx: Option<Fx> = None;
        for i in 0..n_t {
            c.tb[i] = (self.read(i * WIDTH), self.read(i * WIDTH + 1), self.read(i * WIDTH + 2));
        }
        if self.has_overall {
            let b = n_t * WIDTH;
            c.o_max = self.read(b);
            c.o_g = self.read(b + 1);
            let mut bits = [0u64; 6];
            for j in 0..6 {
                bits[j] = self.read_bits(b + 2 + j);
            }
            if !self.o_seen || bits != self.o_bits {
                let mut oids = [0u32; 6];
                let mut n_o = 0;
                for j in 0..6 {
                    let x = f64::from_bits(bits[j]) as i64;
                    if x != 0 {
                        oids[n_o] = x as u32;
                        n_o += 1;
                    }
                }
                self.o_place = ctx.place_raw(&oids[..n_o]);
                self.o_bits = bits;
                self.o_seen = true;
            }
        }
        match only {
            Some(b) => self.class(&c, &mut rest_fx, rs, std::slice::from_ref(&b))?,
            None => {
                let parts = if rest.iter().any(|&d| ctx.is_energized[d as usize]) {
                    &ctx.partitions_busy
                } else {
                    &ctx.partitions_calm
                };
                for members in parts {
                    self.class(&c, &mut rest_fx, rs, members)?;
                }
            }
        }
        Ok(())
    }

    /// One boots class: its legal members, one set of fights for the class,
    /// then the rows each member earns.
    fn class(&mut self, c: &Combo<'_>, rest_fx: &mut Option<Fx>,
             rs: &mut [Option<FightResult>; MAX_T], members: &[Dense]) -> PyResult<()> {
        let ctx = self.ctx;
        let n_t = self.n_t;
        let rest = c.rest;
        let (o_max, o_g) = (c.o_max, c.o_g);
        let tb = &c.tb;
        let n_ids = rest.len() + 1;
        let mut ids = [0 as Dense; MAX_ITEMS];
        ids[1..n_ids].copy_from_slice(rest);
        let mut members_ok = [0 as Dense; MAX_CLASS];
        let mut n_legal = 0usize;
        for &b in members {
            ids[0] = b;
            if c.check_boots && ctx.is_grouped_boot[b as usize] && !ctx.legal(&ids[..n_ids]) {
                continue;
            }
            if let Some(budget) = ctx.budget {
                if ids[..n_ids].iter().map(|&i| ctx.price[i as usize]).sum::<i64>() > budget {
                    continue;
                }
            }
            members_ok[n_legal] = b;
            n_legal += 1;
        }
        if n_legal == 0 {
            return Ok(());
        }
        self.n += n_legal as i64;
        let legal = &members_ok[..n_legal];
        ids[0] = legal[0];
        let sheet = ctx.resolve_sheet(&ids[..n_ids]);
        let own_fx;
        let fx: &Fx = if c.shared_fx {
            rest_fx.get_or_insert_with(|| Fx::merge(rest.iter().map(|&i| &ctx.fx[i as usize])))
        } else {
            own_fx = Fx::merge(ids[..n_ids].iter().map(|&i| &ctx.fx[i as usize]));
            &own_fx
        };
        // the target-independent half of the fight and the driver, built once
        // for the class instead of once per target (see fight::Prep)
        let mut sim = Sim::new(&sheet, &ctx.kit, fx, ctx.level, ctx.ranks, ctx.prestacked)
            .map_err(PyValueError::new_err)?;
        // `rs` is the caller's scratch buffer: every slot below `n_t` is
        // written before it is read, so nothing carries over between classes
        let (mut unkilled, mut prod) = (0i64, 1.0f64);
        // once the build can no longer make the overall list, each fight
        // only has its own target's list to make
        let mut out_of_overall = !self.has_overall;
        // every member of a class shares `rest`, so the class's least place
        // is the rest's places with its least boot (`place` of any member,
        // with the smallest boot) — built only when the tie-break asks
        let min_place = || {
            let mut items = [0i64; MAX_ITEMS];
            for (k, &i) in rest.iter().enumerate() {
                items[k] = ctx.order_of[i as usize];
            }
            Place { items, n: rest.len() as u8,
                    boots: legal.iter().map(|&b| ctx.order_of[b as usize]).min().unwrap() }
        };
        for i in 0..n_t {
            let tg = &ctx.targets[i];
            let mut stop = INF;
            if !out_of_overall
                && ((unkilled as f64) > o_max
                    || ((unkilled as f64) == o_max && o_max > 0.0 && min_place() > self.o_place))
            {
                out_of_overall = true;
            }
            if out_of_overall {
                stop = tb[i].0 * PRUNE_SLACK;
            } else if o_max == 0.0 {
                // every target has to die: the geometric mean of the kill
                // times bounds this fight
                let mut rem = prod;
                for j in i + 1..n_t {
                    rem *= tb[j].2;
                }
                if rem > 0.0 {
                    stop = pymax(tb[i].0, self.pow_n(o_g, n_t) / rem) * PRUNE_SLACK;
                }
            }
            let r = sim.fight(tg, Opts { use_ult: ctx.use_ult, prestacked: ctx.prestacked,
                                        stop_after: stop, breakdown: false, blend: true },
                              &mut self.dmg_log)
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
            rs[i] = r;
        }
        // the fights only reach Python if a row actually places, and then
        // every row of the class shares the one dict
        let mut rc: Option<Rc<Vec<Option<FightResult>>>> = None;
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
            let fights = rc.get_or_insert_with(|| Rc::new(rs[..n_t].to_vec())).clone();
            for &b in legal {
                ids[0] = b;
                self.out[i].push(Row { key, place: ctx.place(&ids[..n_ids]), ids,
                                       n_ids: n_ids as u8, rs: fights.clone() });
            }
            keep_best(&mut self.out[i], ctx.keep);
        }
        if self.has_overall && !out_of_overall {
            let key = overall_key(&rs[..n_t], &ctx.targets);
            let lead = (key[0], key[1]);
            // all of them, only those that still beat the tie-break, or none
            let (all, filtered) = if lead < (o_max, o_g) {
                (true, false)
            } else if lead == (o_max, o_g) {
                if o_max == 0.0 { (true, false) } else { (false, true) }
            } else {
                (false, false)
            };
            if all || filtered {
                let fights = rc.get_or_insert_with(|| Rc::new(rs[..n_t].to_vec())).clone();
                for &b in legal {
                    ids[0] = b;
                    let place = ctx.place(&ids[..n_ids]);
                    if filtered && place > self.o_place {
                        continue;
                    }
                    self.out[n_t].push(Row { key, place, ids, n_ids: n_ids as u8,
                                             rs: fights.clone() });
                }
            }
            keep_best(&mut self.out[n_t], ctx.keep);
        }
        Ok(())
    }
}

#[pyclass(frozen)]
pub struct Ctx {
    base: ChampBase,
    level: i64,
    ranks: Ranks,
    kit: Kit,
    use_ult: bool,
    prestacked: bool,
    // the pool, one array per field, indexed by dense item index
    id_of: Vec<u32>,
    dense_of: HashMap<u32, Dense>,
    order_of: Vec<i64>,
    price: Vec<i64>,
    groups: Vec<Vec<usize>>,
    /// bit per violable group index (only meaningful while there are <= 64)
    gmask: Vec<u64>,
    is_energized: Vec<bool>,
    is_grouped_boot: Vec<bool>,
    /// items `rest_mask` (and so `boots_can_bind`) accounts for
    can_be_rest: Vec<bool>,
    /// false when no boot's groups can ever collide with the rest of a
    /// build, which makes the per-member legality check dead code
    boots_can_bind: bool,
    /// every violable cap is 1 and no item lists a group twice, so
    /// legality is a bit-conflict test on `gmask` instead of a count
    caps_all_one: bool,
    /// every boot (and every class member) has an empty overlay, so a
    /// combination's merged `Fx` is the same for all of its classes
    boots_fx_empty: bool,
    stats: Vec<Vec<(SK, f64)>>,
    fx: Vec<ItemFx>,
    caps: Vec<i64>,
    targets: Vec<Target>,
    target_keys: Vec<String>,
    overall: Option<String>,
    keep: usize,
    budget: Option<i64>,
    required: Vec<Dense>,
    free: Vec<Dense>,
    partitions_busy: Vec<Vec<Dense>>,
    partitions_calm: Vec<Vec<Dense>>,
}

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
    let mut unkilled = 0usize;
    for r in rs {
        if r.as_ref().expect("all fought").ttk.is_none() {
            unkilled += 1;
        }
    }
    let unkilled = unkilled as f64;
    // the zips below stop at the shorter of the two, as they always did
    let m = rs.len().min(targets.len());
    let mut times = [0.0f64; MAX_T];
    for k in 0..m {
        let r = rs[k].as_ref().expect("all fought");
        match kill_time(r, targets[k].duration) {
            Some(t) => times[k] = t,
            None => return [unkilled, INF, INF],
        }
    }
    let mut effs = [0.0f64; MAX_T];
    for k in 0..m {
        let r = rs[k].as_ref().expect("all fought");
        effs[k] = if r.ttk.is_some() { r.ttk_eff.unwrap() } else { times[k] };
    }
    [unkilled, geo_mean(&times[..m]), geo_mean(&effs[..m])]
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

/// Intern one Riot id, first occurrence wins.
fn intern(id: u32, id_of: &mut Vec<u32>, dense_of: &mut HashMap<u32, Dense>) -> PyResult<Dense> {
    if let Some(&d) = dense_of.get(&id) {
        return Ok(d);
    }
    if id_of.len() >= Dense::MAX as usize {
        return Err(PyValueError::new_err("more pool items than the engine can index"));
    }
    let d = id_of.len() as Dense;
    id_of.push(id);
    dense_of.insert(id, d);
    Ok(d)
}

impl Ctx {
    /// The enumeration order of a Riot id, whether or not it is in the pool
    /// (the bounds table's tie-break ids need not be).
    fn order_raw(&self, id: u32) -> i64 {
        self.dense_of.get(&id).map(|&d| self.order_of[d as usize]).unwrap_or(id as i64)
    }

    /// `place()` over raw Riot ids — only the bounds table's tie-break row.
    fn place_raw(&self, ids: &[u32]) -> Place {
        if ids.is_empty() {
            return Place::EMPTY;
        }
        let mut items = [0i64; MAX_ITEMS];
        for (k, &i) in ids[1..].iter().enumerate() {
            items[k] = self.order_raw(i);
        }
        Place { items, n: (ids.len() - 1) as u8, boots: self.order_raw(ids[0]) }
    }

    fn place(&self, ids: &[Dense]) -> Place {
        if ids.is_empty() {
            return Place::EMPTY;
        }
        let mut items = [0i64; MAX_ITEMS];
        for (k, &i) in ids[1..].iter().enumerate() {
            items[k] = self.order_of[i as usize];
        }
        Place { items, n: (ids.len() - 1) as u8, boots: self.order_of[ids[0] as usize] }
    }

    fn legal(&self, ids: &[Dense]) -> bool {
        if self.caps_all_one {
            return self.legal_from(0, ids);
        }
        // a build holds at most MAX_ITEMS items, so a u8 counter cannot wrap
        let mut counts = [0u8; 64];
        for &i in ids {
            for &g in &self.groups[i as usize] {
                counts[g] += 1;
                if counts[g] as i64 > self.caps[g] {
                    return false;
                }
            }
        }
        true
    }

    /// Cap-1 legality of `ids` on top of an accumulator that already holds
    /// the groups of items folded in earlier (and legal among themselves).
    #[inline]
    fn legal_from(&self, mut acc: u64, ids: &[Dense]) -> bool {
        for &i in ids {
            let m = self.gmask[i as usize];
            if acc & m != 0 {
                return false;
            }
            acc |= m;
        }
        true
    }

    fn resolve_sheet(&self, ids: &[Dense]) -> Sheet {
        if ids.is_empty() {
            return resolve(&self.base, self.level, &[], self.kit.crimson_pact);
        }
        // filled with the first item, then overwritten slot by slot: only
        // `ids.len()` of it is ever handed on
        let mut items: [(&[(SK, f64)], &ItemFx); MAX_ITEMS] =
            [(&[], &self.fx[ids[0] as usize]); MAX_ITEMS];
        for (k, &i) in ids.iter().enumerate() {
            items[k] = (self.stats[i as usize].as_slice(), &self.fx[i as usize]);
        }
        resolve(&self.base, self.level, &items[..ids.len()], self.kit.crimson_pact)
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
        let mut item_map: HashMap<u32, PoolItem> = HashMap::new();
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
            item_map.insert(id, PoolItem { stats, fx: ItemFx::from_py(&fxd)?, price, groups: gs });
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
        if tg.len() > MAX_T {
            return Err(PyValueError::new_err("more targets than the engine can score at once"));
        }
        let parts = partitions.cast::<PyTuple>()?;
        let busy: Vec<Vec<u32>> = parts.get_item(0)?.extract()?;
        let calm: Vec<Vec<u32>> = parts.get_item(1)?.extract()?;
        if busy.iter().chain(calm.iter()).any(|c| c.len() > MAX_CLASS) {
            return Err(PyValueError::new_err(
                "a boots class has more members than the engine can hold"));
        }
        let mut ord = HashMap::new();
        for (k, v) in order.iter() {
            ord.insert(k.extract::<u32>()?, v.extract::<i64>()?);
        }

        // Intern every id a build can hold: boots, then required, then free,
        // first occurrence wins. The classes and anything left in the pool
        // follow, so every id the loop can meet has a dense index.
        let mut id_of: Vec<u32> = Vec::new();
        let mut dense_of: HashMap<u32, Dense> = HashMap::new();
        for &id in boots.iter().chain(required.iter()).chain(free.iter()) {
            intern(id, &mut id_of, &mut dense_of)?;
        }
        for cls in busy.iter().chain(calm.iter()) {
            for &id in cls {
                intern(id, &mut id_of, &mut dense_of)?;
            }
        }
        let mut spare: Vec<u32> =
            item_map.keys().copied().filter(|i| !dense_of.contains_key(i)).collect();
        spare.sort_unstable();
        for id in spare {
            intern(id, &mut id_of, &mut dense_of)?;
        }

        let n = id_of.len();
        let (mut stats, mut fxs) = (Vec::with_capacity(n), Vec::with_capacity(n));
        let (mut price, mut gs, mut gmask) =
            (Vec::with_capacity(n), Vec::with_capacity(n), Vec::with_capacity(n));
        let mut order_of = Vec::with_capacity(n);
        for &id in &id_of {
            let it = item_map
                .remove(&id)
                .ok_or_else(|| PyValueError::new_err(format!("item {id} is not in the pool")))?;
            let mut m = 0u64;
            for &g in &it.groups {
                m |= 1u64 << g;
            }
            gmask.push(m);
            gs.push(it.groups);
            stats.push(it.stats);
            fxs.push(it.fx);
            price.push(it.price);
            order_of.push(ord.get(&id).copied().unwrap_or(id as i64));
        }
        let mut is_energized = vec![false; n];
        for id in energized {
            if let Some(&d) = dense_of.get(&id) {
                is_energized[d as usize] = true;
            }
        }
        let dense = |ids: &[u32]| -> Vec<Dense> { ids.iter().map(|i| dense_of[i]).collect() };
        let dboots = dense(&boots);
        let mut is_grouped_boot = vec![false; n];
        for &b in &dboots {
            is_grouped_boot[b as usize] = !gs[b as usize].is_empty();
        }
        let dbusy: Vec<Vec<Dense>> = busy.iter().map(|c| dense(c)).collect();
        let dcalm: Vec<Vec<Dense>> = calm.iter().map(|c| dense(c)).collect();
        let drequired = dense(&required);
        let dfree = dense(&free);

        // `legal` can only ever reject a boot because one of its groups is
        // already spoken for by the rest of the build. With every violable
        // cap at 1 that is a mask test against everything the rest can hold,
        // so when no boot's groups meet it the per-member check never fires
        // (all seven boots share the Boots groups, and nothing else does).
        let mut can_be_rest = vec![false; n];
        let mut rest_mask = 0u64;
        for &d in drequired.iter().chain(dfree.iter()) {
            can_be_rest[d as usize] = true;
            rest_mask |= gmask[d as usize];
        }
        // a mask stands in for a count only while every cap is 1 and every
        // item's groups are distinct (so it cannot violate one on its own)
        let caps_all_one = cap_list.iter().all(|&c| c == 1)
            && (0..n).all(|d| gmask[d].count_ones() as usize == gs[d].len());
        let boots_can_bind = cap_list.iter().any(|&c| c > 1)
            || dboots.iter().any(|&b| gmask[b as usize] & rest_mask != 0);
        // no boot carries a modeled effect (item-effects.json has none), and
        // merging an empty overlay is a no-op wherever it sits, so the whole
        // `Fx` of a combination is its rest's, shared by every class
        let boots_fx_empty = dboots
            .iter()
            .chain(dbusy.iter().flatten())
            .chain(dcalm.iter().flatten())
            .all(|&b| fxs[b as usize].is_empty());

        Ok(Ctx {
            base: ChampBase::from_py(base)?,
            level,
            ranks,
            kit: Kit::from_py(kit)?,
            use_ult,
            prestacked,
            id_of,
            dense_of,
            order_of,
            price,
            groups: gs,
            gmask,
            is_energized,
            is_grouped_boot,
            can_be_rest,
            boots_can_bind,
            caps_all_one,
            boots_fx_empty,
            stats,
            fx: fxs,
            caps: cap_list,
            targets: tg,
            target_keys: keys,
            overall,
            keep,
            budget,
            required: drequired,
            free: dfree,
            partitions_busy: dbusy,
            partitions_calm: dcalm,
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
        let mut blk = Block {
            ctx: self,
            n_t,
            has_overall,
            table: bounds_addr as *const u64,
            out: (0..n_keys).map(|_| Vec::new()).collect(),
            n: 0,
            o_bits: [0; 6],
            o_place: Place::EMPTY,
            o_seen: false,
            dmg_log: Vec::new(),
            pow_bits: 0,
            pow_val: 0.0,
            pow_seen: false,
        };
        // one fight buffer for the whole task, refilled per boots class
        let mut rs: [Option<FightResult>; MAX_T] = std::array::from_fn(|_| None);

        if kind == "block" {
            let size: usize = tup.get_item(1)?.extract()?;
            let prefix: Vec<usize> = tup.get_item(2)?.extract()?;
            let stem_len = self.required.len() + prefix.len();
            if self.required.len() + size + 1 > MAX_ITEMS || stem_len + 1 > MAX_ITEMS {
                return Err(PyValueError::new_err(
                    "more items in a build than the engine can hold"));
            }
            // one buffer per block: the stem is written once, the odometer
            // only ever rewrites the tail slots it advances
            let mut buf = [0 as Dense; MAX_ITEMS];
            buf[..self.required.len()].copy_from_slice(&self.required);
            for (j, &i) in prefix.iter().enumerate() {
                buf[self.required.len() + j] = self.free[i];
            }
            // the stem's groups fold once for the whole block
            let mut stem_acc = 0u64;
            let mut stem_ok = true;
            if self.caps_all_one {
                for &d in &buf[..stem_len] {
                    let m = self.gmask[d as usize];
                    if stem_acc & m != 0 {
                        stem_ok = false;
                        break;
                    }
                    stem_acc |= m;
                }
            }
            let tail: &[Dense] = if prefix.is_empty() { &self.free } else { &self.free[prefix[prefix.len() - 1] + 1..] };
            let k = size - prefix.len();
            let n_tail = tail.len();
            // itertools.combinations(tail, k) in place, same sequence
            if k <= n_tail && stem_ok {
                let mut idx = [0usize; MAX_ITEMS];
                for j in 0..k {
                    idx[j] = j;
                    buf[stem_len + j] = tail[j];
                }
                loop {
                    blk.score(&mut rs, &buf[..stem_len + k], (stem_acc, stem_len), None,
                              self.boots_can_bind, self.boots_fx_empty)?;
                    // advance
                    let mut i = k;
                    let done = loop {
                        if i == 0 {
                            break true;
                        }
                        i -= 1;
                        if idx[i] != i + n_tail - k {
                            break false;
                        }
                        if i == 0 {
                            break true;
                        }
                    };
                    if done {
                        break;
                    }
                    idx[i] += 1;
                    buf[stem_len + i] = tail[idx[i]];
                    for j in i + 1..k {
                        idx[j] = idx[j - 1] + 1;
                        buf[stem_len + j] = tail[idx[j]];
                    }
                }
            }
        } else if kind == "builds" {
            for b in tup.get_item(1)?.try_iter()? {
                let raw: Vec<u32> = b?.extract()?;
                if raw.is_empty() {
                    return Err(PyValueError::new_err("an explicit build has no items"));
                }
                if raw.len() > MAX_ITEMS {
                    return Err(PyValueError::new_err(
                        "more items in a build than the engine can hold"));
                }
                let ids = self.to_dense(&raw)?;
                // an explicit build may hold something `rest_mask` never saw
                let check = self.boots_can_bind
                    || ids[1..].iter().any(|&d| !self.can_be_rest[d as usize]);
                let shared_fx = self.fx[ids[0] as usize].is_empty();
                blk.score(&mut rs, &ids[1..], (0, 0), Some(ids[0]), check, shared_fx)?;
            }
        } else {
            return Err(PyValueError::new_err(format!("unknown task kind {kind}")));
        }

        let (mut out, n) = (blk.out, blk.n);
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
                let ids = PyList::new(py, row.ids[..row.n_ids as usize].iter()
                                          .map(|&d| self.id_of[d as usize]))?;
                rows.append(PyTuple::new(py, [key.into_any(), ids.into_any(), rs_dict.into_any()])?)?;
            }
            result.set_item(name, rows)?;
        }
        Ok((result, n))
    }
}

impl Ctx {
    /// Riot ids from Python -> dense indices (the explicit-builds task).
    fn to_dense(&self, ids: &[u32]) -> PyResult<Vec<Dense>> {
        ids.iter()
            .map(|i| {
                self.dense_of
                    .get(i)
                    .copied()
                    .ok_or_else(|| PyValueError::new_err(format!("item {i} is not in the pool")))
            })
            .collect()
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
