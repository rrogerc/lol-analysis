//! The driver: a unit's ability shape, plugged into the fight through
//! these hooks (the port of tft.Driver). A driver is a plain struct of its
//! own state, built once per kit from the rows and calcs it reads
//! (`new`), cloned for every fight; the hooks are associated functions
//! that get the whole fight, with the driver's state at `f.drv`.
//!
//! Hooks: `init` at the start, `cast_time`, `attack` (default: a plain
//! attack on the target), `cast` (when the ability lands: after the
//! animation unless `LANDS_AT_START`), `tick` every 0.25 s, `hit` after
//! the unit takes damage, `kill` when a dummy dies to the unit, `died`
//! when the unit falls, `event` for anything queued with `f.after`.

use crate::fight::Fight;
use crate::kit::Kit;
use crate::spec::UnitSpec;

pub trait Driver: Clone + Sized + Send {
    /// The Python driver class's name, as the cell payload reports it.
    const NAME: &'static str;
    /// False for a placeholder whose port is still to come.
    const PORTED: bool = true;
    /// A channel whose driver spreads its effect over the cast itself
    /// (through `tick` and `f.after`): `cast` runs when the cast starts.
    const LANDS_AT_START: bool = false;

    fn new(kit: &Kit, unit: &UnitSpec) -> Self;

    fn init(_f: &mut Fight<Self>) {}

    fn cast_time(f: &Fight<Self>) -> f64 {
        f.default_cast_time()
    }

    fn attack(f: &mut Fight<Self>, target: usize) {
        f.hit_attack(target, 1.0, "auto");
    }

    fn cast(_f: &mut Fight<Self>) {}

    fn tick(_f: &mut Fight<Self>) {}

    fn hit(_f: &mut Fight<Self>, _attacker: Option<usize>, _damage: f64) {}

    fn kill(_f: &mut Fight<Self>, _target: usize) {}

    fn died(_f: &mut Fight<Self>) {}

    fn event(_f: &mut Fight<Self>, _tag: u32) {}
}

/// tft.Driver itself: attacks are plain, the cast does nothing.
#[derive(Clone, Debug, Default)]
pub struct Plain;

impl Driver for Plain {
    const NAME: &'static str = "Driver";

    fn new(_kit: &Kit, _unit: &UnitSpec) -> Self {
        Plain
    }
}

/// A driver whose port is still to come: dispatch refuses it.
#[derive(Clone, Debug, Default)]
pub struct Unported;

impl Driver for Unported {
    const NAME: &'static str = "Unported";
    const PORTED: bool = false;

    fn new(_kit: &Kit, _unit: &UnitSpec) -> Self {
        Unported
    }
}
