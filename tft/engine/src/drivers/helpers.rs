//! Helpers several drivers share (tft_kits' `_heal_over_time`,
//! `_tick_heal`, `_track_shield`, `_shield_broke`), and the mana lock the
//! shield tanks hold.

use crate::driver::Driver;
use crate::fight::Fight;
use crate::pyf::{pymax, pymin};

/// Healing spread over a window: `tick_heal` pays out the elapsed share on
/// every 0.25 s tick (tft_kits `f.state["hot"]`).
#[derive(Clone, Copy, Debug)]
pub struct Hot {
    pub until: f64,
    pub rate: f64,
    pub src: &'static str,
    pub last: f64,
}

/// A driver that keeps one heal-over-time.
pub trait HasHot {
    fn hot_mut(&mut self) -> &mut Option<Hot>;
}

/// `_heal_over_time`: queue `total` healing over `duration` seconds.
pub fn heal_over_time<D: Driver + HasHot>(f: &mut Fight<D>, total: f64, duration: f64,
                                          src: &'static str) {
    if duration > 0.0 {
        let hot = Hot { until: f.t + duration, rate: total / duration, src, last: f.t };
        *f.drv.hot_mut() = Some(hot);
    }
}

/// `_tick_heal`: pay out the share elapsed since the last tick.
pub fn tick_heal<D: Driver + HasHot>(f: &mut Fight<D>) {
    let mut hot = match f.drv.hot_mut().take() {
        Some(h) => h,
        None => return,
    };
    let span = pymin(f.t, hot.until) - hot.last;
    hot.last = f.t;
    if span > 0.0 {
        f.heal(hot.rate * span, hot.src);
    }
    if !(f.t >= hot.until) {
        *f.drv.hot_mut() = Some(hot);
    }
}

/// `_track_shield`: shield the unit and remember the engine's entry, so
/// the driver can tell a shield that was spent from one that merely ran
/// out. Returns what to keep as the tracked shield.
pub fn track_shield<D: Driver>(f: &mut Fight<D>, amount: f64, duration: f64,
                               src: &'static str) -> Option<usize> {
    f.shield(amount, duration, src, false)
}

/// `_shield_broke`: true once — at the moment the tracked shield is fully
/// absorbed. Returns (broke, the tracked shield to keep).
pub fn shield_broke<D: Driver>(f: &Fight<D>, tracked: Option<usize>) -> (bool, Option<usize>) {
    let idx = match tracked {
        Some(i) if i < f.shields.len() => i,
        _ => return (false, None),
    };
    let sh = &f.shields[idx];
    if sh.amount <= 0.0 {
        return (true, None);
    }
    if f.t > sh.until {
        return (false, None);   // expired with health left: no break
    }
    (false, Some(idx))
}

/// The mana lock a shield holds. TFTraits gives Ornn, Rakan, Sejuani,
/// Rammus, Malphite and Sentinel the same rule — "The mana lock lasts as
/// long as the shield holds, at most 4.00 s. Attacks continue." — and its
/// four seconds are each kit's own shield duration row. A third-party
/// statement, adopted like Azir's lock, not verified in game.
#[derive(Clone, Copy, Debug, Default)]
pub struct ShieldLock {
    /// The engine's entry for the shield that holds the lock.
    tracked: Option<usize>,
    /// The cast's own lock, which a shield broken sooner falls back to.
    cast_lock: f64,
}

/// A driver whose cast shields the unit and locks its mana while it stands.
pub trait HasShieldLock {
    fn shield_lock_mut(&mut self) -> &mut ShieldLock;
}

/// The cast's side: shield the unit and hold every source of mana —
/// attacks, the regen ticks and a tank's damage taken alike — until the
/// shield's own duration is out.
pub fn shield_lock<D: Driver + HasShieldLock>(f: &mut Fight<D>, amount: f64, duration: f64,
                                              src: &'static str) {
    let cast_lock = f.lock_until;
    let tracked = track_shield(f, amount, duration, src);
    if tracked.is_some() {
        f.lock_until = pymax(cast_lock, f.t + duration);
    }
    *f.drv.shield_lock_mut() = ShieldLock { tracked, cast_lock };
}

/// The `hit` hook's side: true once, at the moment the shield is spent, and
/// mana comes in again from there (the cast's own lock still floors it). A
/// shield that merely runs out released the lock at its own end.
pub fn shield_lock_broke<D: Driver + HasShieldLock>(f: &mut Fight<D>) -> bool {
    let lock = *f.drv.shield_lock_mut();
    let (broke, keep) = shield_broke(f, lock.tracked);
    f.drv.shield_lock_mut().tracked = keep;
    if broke {
        f.lock_until = pymax(f.t, lock.cast_lock);
    }
    broke
}
