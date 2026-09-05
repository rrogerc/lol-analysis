//! Helpers several drivers share (tft_kits' `_heal_over_time`,
//! `_tick_heal`, `_track_shield`, `_shield_broke`).

use crate::driver::Driver;
use crate::fight::Fight;
use crate::pyf::pymin;

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
