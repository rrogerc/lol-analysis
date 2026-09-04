//! League math primitives, transcribed from builds.py with Python's
//! evaluation order kept (every expression here is bit-for-bit the Python
//! one: same operations, same associativity, ints widened exactly).

pub const AS_CAP: f64 = 2.5;
pub const INF: f64 = f64::INFINITY;
pub const ABILITY_LOCKOUT_S: f64 = 0.25;
pub const MELEE_MAX_RANGE: f64 = 325.0;

/// Python's `max(a, b)`: `a` unless `b` is strictly greater.
#[inline(always)]
pub fn pymax(a: f64, b: f64) -> f64 {
    if b > a {
        b
    } else {
        a
    }
}

/// Python's `min(a, b)`: `a` unless `b` is strictly smaller.
#[inline(always)]
pub fn pymin(a: f64, b: f64) -> f64 {
    if b < a {
        b
    } else {
        a
    }
}

#[inline(always)]
pub fn imin(a: i64, b: i64) -> i64 {
    if b < a {
        b
    } else {
        a
    }
}

#[inline(always)]
pub fn imax(a: i64, b: i64) -> i64 {
    if b > a {
        b
    } else {
        a
    }
}

/// `(level - 1) * (0.7025 + 0.0175 * (level - 1))`
pub fn growth(level: i64) -> f64 {
    let l = (level - 1) as f64;
    l * (0.7025 + 0.0175 * l)
}

pub fn stat_at(base: f64, per_level: f64, level: i64) -> f64 {
    base + per_level * growth(level)
}

pub fn resist_mult(resist: f64) -> f64 {
    if resist >= 0.0 {
        100.0 / (100.0 + resist)
    } else {
        2.0 - 100.0 / (100.0 - resist)
    }
}

pub fn stack_pct_pen(a: f64, b: f64) -> f64 {
    100.0 * (1.0 - (1.0 - a / 100.0) * (1.0 - b / 100.0))
}

pub fn eff_resist(base: f64, flat_reduction: f64, pct_reduction: f64, pct_pen: f64,
                  flat_pen: f64) -> f64 {
    let mut r = (base - flat_reduction) * (1.0 - pct_reduction / 100.0);
    if r > 0.0 {
        r = pymax(0.0, r * (1.0 - pct_pen / 100.0) - flat_pen);
    }
    r
}

/// A `{'from', 'to', 'levels': [lo, hi]}` level-scaled value.
#[derive(Clone, Copy, Debug)]
pub struct ByLevel {
    pub from: f64,
    pub to: f64,
    pub lo: i64,
    pub hi: i64,
}

impl ByLevel {
    pub fn at(&self, level: i64) -> f64 {
        let l = imin(imax(level, self.lo), self.hi);
        let frac = (l - self.lo) as f64 / (self.hi - self.lo) as f64;
        self.from + (self.to - self.from) * frac
    }
}

/// One ability hit's raw damage at `rank` (1-5): base plus ratios.
#[derive(Clone, Debug)]
pub struct DamageSpec {
    pub base: Vec<f64>,
    pub bonus_ad_ratio: f64,
    pub ad_ratio: f64,
    pub ap_ratio: f64,
    pub max_hp_ratio: Option<f64>,
    pub bonus_hp_ratio: Option<f64>,
}

impl DamageSpec {
    pub fn hit(&self, rank: i64, sheet: &crate::sheet::Sheet) -> f64 {
        let mut amt = self.base[(rank - 1) as usize]
            + self.bonus_ad_ratio * sheet.ad_bonus
            + self.ad_ratio * sheet.ad
            + self.ap_ratio * sheet.ap;
        if let Some(r) = self.max_hp_ratio {
            amt += r / 100.0 * sheet.hp;
        }
        if let Some(r) = self.bonus_hp_ratio {
            amt += r / 100.0 * sheet.hp_bonus;
        }
        amt
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum DType {
    Physical,
    Magic,
    True,
}

impl DType {
    /// builds.py tests `== "physical"` and `== "true"` and treats anything
    /// else as magic.
    pub fn parse(s: &str) -> DType {
        match s {
            "physical" => DType::Physical,
            "true" => DType::True,
            _ => DType::Magic,
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct Ranks {
    pub q: i64,
    pub w: i64,
    pub e: i64,
    pub r: i64,
}
