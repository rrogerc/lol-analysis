//! The generated drivers (engine/src/generated/) behind one vtable. They
//! implement the same `Driver` trait as the hand-written ones; `DynDriver`
//! wraps whichever one the kit names, so the engine monomorphises one fight
//! for all of them instead of one per champion.

use std::any::Any;
use std::fmt::Debug;

use crate::fight::{Driver, Engine, Events, Kind, St};
use crate::kit::Kit;
use crate::num::Ranks;
use crate::sheet::Sheet;

/// `Driver` without its constructor, object-safe.
pub trait DriverObj {
    fn reset(&mut self);
    fn ranged(&self) -> bool;
    fn attack_range(&self) -> f64;
    fn bonus_as(&self, t: f64) -> f64;
    fn attack_damage(&self, e: &Engine<'_, '_>) -> f64;
    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64);
    fn before_attack(&mut self, e: &mut Engine<'_, '_>);
    fn attack_riders(&mut self, e: &mut Engine<'_, '_>);
    fn after_attack(&mut self, e: &mut Engine<'_, '_>);
    fn schedule_attack(&mut self, e: &mut Engine<'_, '_>);
    fn q_at(&self, e: &Engine<'_, '_>) -> f64;
    fn cast_q(&mut self, e: &mut Engine<'_, '_>);
    fn cast_r(&mut self, e: &mut Engine<'_, '_>);
    fn events(&self, e: &Engine<'_, '_>, out: &mut Events) -> usize;
    fn on_event(&mut self, e: &mut Engine<'_, '_>, kind: Kind);
    fn clone_box(&self) -> Box<dyn DriverObj>;
    fn as_any(&self) -> &dyn Any;
    fn same_as(&self, other: &dyn DriverObj) -> bool;
    fn describe(&self) -> String;
}

impl<D: Driver + Clone + Debug + PartialEq + 'static> DriverObj for D {
    fn reset(&mut self) { Driver::reset(self) }
    fn ranged(&self) -> bool { Driver::ranged(self) }
    fn attack_range(&self) -> f64 { Driver::attack_range(self) }
    fn bonus_as(&self, t: f64) -> f64 { Driver::bonus_as(self, t) }
    fn attack_damage(&self, e: &Engine<'_, '_>) -> f64 { Driver::attack_damage(self, e) }
    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        Driver::shave_cooldowns(self, st, t, factor)
    }
    fn before_attack(&mut self, e: &mut Engine<'_, '_>) { Driver::before_attack(self, e) }
    fn attack_riders(&mut self, e: &mut Engine<'_, '_>) { Driver::attack_riders(self, e) }
    fn after_attack(&mut self, e: &mut Engine<'_, '_>) { Driver::after_attack(self, e) }
    fn schedule_attack(&mut self, e: &mut Engine<'_, '_>) { Driver::schedule_attack(self, e) }
    fn q_at(&self, e: &Engine<'_, '_>) -> f64 { Driver::q_at(self, e) }
    fn cast_q(&mut self, e: &mut Engine<'_, '_>) { Driver::cast_q(self, e) }
    fn cast_r(&mut self, e: &mut Engine<'_, '_>) { Driver::cast_r(self, e) }
    fn events(&self, e: &Engine<'_, '_>, out: &mut Events) -> usize { Driver::events(self, e, out) }
    fn on_event(&mut self, e: &mut Engine<'_, '_>, kind: Kind) { Driver::on_event(self, e, kind) }
    fn clone_box(&self) -> Box<dyn DriverObj> { Box::new(self.clone()) }
    fn as_any(&self) -> &dyn Any { self }
    fn same_as(&self, other: &dyn DriverObj) -> bool {
        other.as_any().downcast_ref::<D>().is_some_and(|o| o == self)
    }
    fn describe(&self) -> String { format!("{self:?}") }
}

pub struct DynDriver(Box<dyn DriverObj>);

impl Clone for DynDriver {
    fn clone(&self) -> Self { DynDriver(self.0.clone_box()) }
}

impl DynDriver {
    pub fn same_as(&self, other: &DynDriver) -> bool { self.0.same_as(other.0.as_ref()) }
    pub fn describe(&self) -> String { self.0.describe() }
}

impl Driver for DynDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, prestacked: bool)
        -> Result<Self, String> {
        crate::generated::build(&kit.champion, kit, sheet, level, ranks, prestacked).map(DynDriver)
    }
    fn reset(&mut self) { self.0.reset() }
    fn ranged(&self) -> bool { self.0.ranged() }
    fn attack_range(&self) -> f64 { self.0.attack_range() }
    fn bonus_as(&self, t: f64) -> f64 { self.0.bonus_as(t) }
    fn attack_damage(&self, e: &Engine) -> f64 { self.0.attack_damage(e) }
    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        self.0.shave_cooldowns(st, t, factor)
    }
    fn before_attack(&mut self, e: &mut Engine) { self.0.before_attack(e) }
    fn attack_riders(&mut self, e: &mut Engine) { self.0.attack_riders(e) }
    fn after_attack(&mut self, e: &mut Engine) { self.0.after_attack(e) }
    fn schedule_attack(&mut self, e: &mut Engine) { self.0.schedule_attack(e) }
    fn q_at(&self, e: &Engine) -> f64 { self.0.q_at(e) }
    fn cast_q(&mut self, e: &mut Engine) { self.0.cast_q(e) }
    fn cast_r(&mut self, e: &mut Engine) { self.0.cast_r(e) }
    fn events(&self, e: &Engine, out: &mut Events) -> usize { self.0.events(e, out) }
    fn on_event(&mut self, e: &mut Engine, kind: Kind) { self.0.on_event(e, kind) }
}
