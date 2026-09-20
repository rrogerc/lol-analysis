//! Talon. Shadow Assault opens the fight and recasts on cooldown for its
//! two damage passes; Noxian Diplomacy is cast close-range on cooldown for
//! its guaranteed critical strike; Rake is cast on cooldown for its two
//! passes; Blade's End stacks Wound on every ability hit and, once a basic
//! attack lands at max stacks, starts the passive's ticking bleed.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Shadow Assault: outward cast (recasts) and the convergence recast.
const EV_R_CAST: u8 = 0;
const EV_R_CONVERGE: u8 = 1;
/// Rake: outgoing cast and its homing return pass.
const EV_W_CAST: u8 = 2;
const EV_W_RETURN: u8 = 3;
/// Blade's End bleed tick.
const EV_BLEED_TICK: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    stack_cap: i64,
    stack_duration: f64,
    bleed_interval: f64,
    bleed_ticks: i64,
    bleed_tick_dmg: f64,
    q_crit_dmg: f64,
    q_cd: f64,
    w_initial_dmg: f64,
    w_return_dmg: f64,
    w_return_delay: f64,
    w_cd: f64,
    r_dmg: f64,
    r_min_lifetime: f64,
    r_cd: f64,
    src_p_bleed: SourceId,
    src_w_initial: SourceId,
    src_w_return: SourceId,
    src_r_converge: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    wound_stacks: i64,
    wound_expire: f64,
    /// Ticks of the passive bleed still to land (0: no bleed running).
    bleed_ticks_left: i64,
    bleed_next_tick: f64,
    w_ready: f64,
    /// When Rake's pending return pass lands (INF: none pending).
    w_return_at: f64,
    r_ready: f64,
    /// When Shadow Assault's pending convergence lands (INF: none pending).
    r_converge_at: f64,
}

impl GenDriver {
    /// An ability hit against the target: adds (or refreshes) a Wound
    /// stack, unless the target is currently bleeding.
    fn apply_wound(&mut self, t: f64) {
        if self.s.bleed_ticks_left > 0 {
            return;
        }
        if t > self.s.wound_expire {
            self.s.wound_stacks = 0;
        }
        self.s.wound_stacks = imin(self.s.wound_stacks + 1, self.stack_cap);
        self.s.wound_expire = t + self.stack_duration;
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let stack_cap = kit.num("gen.P.stackCap")? as i64;
        let stack_duration = kit.num("gen.P.stackDurationS")?;
        let bleed_duration = kit.num("gen.P.bleedDurationS")?;
        let bleed_interval = kit.num("gen.P.bleedIntervalS")?;
        let bleed_ticks = ((bleed_duration / bleed_interval) + 0.5) as i64;
        let bleed_base = kit.at_level("gen.P.bleedTotal.byLevel", level)?;
        let bleed_adratio = kit.num("gen.P.bleedTotal.bonusAdRatio")?;
        let bleed_total = bleed_base + bleed_adratio * sheet.ad_bonus;
        let bleed_tick_dmg = bleed_total / (bleed_ticks as f64);

        let state = State {
            wound_stacks: 0,
            wound_expire: 0.0,
            bleed_ticks_left: 0,
            bleed_next_tick: 0.0,
            w_ready: 0.0,
            w_return_at: INF,
            r_ready: 0.0,
            r_converge_at: INF,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            stack_cap,
            stack_duration,
            bleed_interval,
            bleed_ticks,
            bleed_tick_dmg,
            q_crit_dmg: kit.hit("gen.Q.critDamage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_initial_dmg: kit.hit("gen.W.initial", ranks.w, sheet)?,
            w_return_dmg: kit.hit("gen.W.return", ranks.w, sheet)?,
            w_return_delay: kit.num("gen.W.returnDelayS")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_min_lifetime: kit.num("gen.R.minLifetimeS")?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            src_p_bleed: intern("P bleed"),
            src_w_initial: intern("W initial"),
            src_w_return: intern("W return"),
            src_r_converge: intern("R converge"),
            s: state,
            s0: state,
        })
    }

    fn reset(&mut self) {
        self.s = self.s0;
    }

    fn ranged(&self) -> bool {
        self.attack_range > MELEE_MAX_RANGE
    }

    fn attack_range(&self) -> f64 {
        self.attack_range
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        // Blade's End: a landed basic attack either refreshes existing
        // Wound stacks, or, at 3 stacks, consumes them and starts the bleed
        let t = e.st.t;
        if self.s.bleed_ticks_left == 0 {
            if t > self.s.wound_expire {
                self.s.wound_stacks = 0;
            }
            if self.s.wound_stacks >= self.stack_cap {
                self.s.wound_stacks = 0;
                self.s.bleed_ticks_left = self.bleed_ticks;
                self.s.bleed_next_tick = t + self.bleed_interval;
            } else if self.s.wound_stacks > 0 {
                self.s.wound_expire = t + self.stack_duration;
            }
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // always cast close-range, for the guaranteed critical strike
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        e.deal(self.q_crit_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        self.apply_wound(t);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the outward pass, then wait for the minimum
        // lifetime before the convergence recast
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        e.deal(self.r_dmg, DType::Physical, SRC_R, false, true, 1.0);
        self.apply_wound(t);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.s.r_converge_at = t + self.r_min_lifetime;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.r > 0 {
            if self.s.r_converge_at != INF {
                out[n] = (self.s.r_converge_at, Kind::Ev(EV_R_CONVERGE));
            } else {
                out[n] = (pymax(self.s.r_ready, e.st.t), Kind::Ev(EV_R_CAST));
            }
            n += 1;
        }
        if self.ranks.w > 0 {
            if self.s.w_return_at != INF {
                out[n] = (self.s.w_return_at, Kind::Ev(EV_W_RETURN));
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.s.bleed_ticks_left > 0 {
            out[n] = (self.s.bleed_next_tick, Kind::Ev(EV_BLEED_TICK));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_CAST) => {
                // a full outward-pass recast after the ult's cooldown
                e.deal(self.r_dmg, DType::Physical, SRC_R, false, true, 1.0);
                self.apply_wound(t);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.r_converge_at = t + self.r_min_lifetime;
            }
            Kind::Ev(EV_R_CONVERGE) => {
                self.s.r_converge_at = INF;
                e.deal(self.r_dmg, DType::Physical, self.src_r_converge, false, true, 1.0);
                self.apply_wound(t);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
                self.s.r_ready = t + e.ult_cd(self.r_cd);
            }
            Kind::Ev(EV_W_CAST) => {
                e.deal(self.w_initial_dmg, DType::Physical, self.src_w_initial, false, true, 1.0);
                self.apply_wound(t);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.w_return_at = t + self.w_return_delay;
                self.s.w_ready = INF;
                e.lockout();
            }
            Kind::Ev(EV_W_RETURN) => {
                self.s.w_return_at = INF;
                e.deal(self.w_return_dmg, DType::Physical, self.src_w_return, false, true, 1.0);
                self.apply_wound(t);
                self.s.w_ready = t + e.basic_cd(self.w_cd);
            }
            Kind::Ev(EV_BLEED_TICK) => {
                e.deal(self.bleed_tick_dmg, DType::Physical, self.src_p_bleed, false, false, 1.0);
                self.s.bleed_ticks_left -= 1;
                if self.s.bleed_ticks_left > 0 {
                    self.s.bleed_next_tick += self.bleed_interval;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
