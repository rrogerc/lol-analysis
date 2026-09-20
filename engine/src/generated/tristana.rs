//! Tristana. Buster Shot opens the fight (its 100 s cooldown never comes
//! back up); Explosive Charge is tossed on cooldown and stacks from basic
//! attacks and landed abilities, detonating instantly at four stacks (which
//! resets Rocket Jump's cooldown) or after its 4 s attach duration; Rocket
//! Jump goes out on cooldown; Rapid Fire refreshes her attack speed buff
//! every time it is off cooldown.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Rocket Jump is cast and lands in one instant (its dash is not modeled).
const EV_W: u8 = 0;
/// Explosive Charge is tossed; then it either expires or is detonated early
/// by `add_e_stack` reaching the stack cap.
const EV_E_CAST: u8 = 1;
const EV_E_EXPIRE: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Rapid Fire: percent bonus attack speed, and its buff duration.
    q_as_pct: f64,
    q_buff_dur: f64,
    q_cd: f64,
    w_dmg: f64,
    w_cd: f64,
    /// Explosive Charge's detonation base damage, its per-stack amp, its
    /// crit-chance amplification coefficient, its stack cap and attach
    /// duration, and its own cooldown.
    e_active_dmg: f64,
    e_stack_amp: f64,
    e_crit_mod: f64,
    e_max_stacks: i64,
    e_duration: f64,
    e_cd: f64,
    r_dmg: f64,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// Rapid Fire's attack-speed buff runs until this time.
    q_buff_until: f64,
    w_ready: f64,
    e_ready: f64,
    /// Whether a charge is currently attached, its stacks, and when it
    /// auto-detonates if it never reaches the stack cap first.
    e_active: bool,
    e_stacks: i64,
    e_expire_at: f64,
}

impl GenDriver {
    /// The detonation of Explosive Charge, from either the stack cap or the
    /// 4 s expiry: base damage amplified by stacks and by the build's crit.
    fn detonate(&mut self, e: &mut Engine, stacks: i64) {
        let crit_chance = e.p.sheet.crit_chance / 100.0;
        let crit_dmg = e.p.sheet.crit_damage / 100.0;
        let crit_amp = 1.0 + self.e_crit_mod * crit_chance * (crit_dmg - 1.0);
        let stack_amp = 1.0 + self.e_stack_amp * (stacks as f64);
        let amt = self.e_active_dmg * stack_amp * crit_amp;
        e.deal(amt, DType::Physical, SRC_E, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
    }

    /// A basic attack or a landed ability hitting the charged target: adds
    /// a stack, or forces an instant detonation at the cap and resets
    /// Rocket Jump's cooldown.
    fn add_e_stack(&mut self, e: &mut Engine) {
        if !self.s.e_active {
            return;
        }
        if self.s.e_stacks + 1 >= self.e_max_stacks {
            self.detonate(e, self.e_max_stacks);
            self.s.e_active = false;
            self.s.e_expire_at = INF;
            self.s.e_stacks = 0;
            self.s.w_ready = e.st.t;
        } else {
            self.s.e_stacks += 1;
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            q_buff_until: 0.0,
            w_ready: 0.0,
            e_ready: 0.0,
            e_active: false,
            e_stacks: 0,
            e_expire_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range + kit.at_level("gen.P.rangeByLevel", level)?,
            windup_fraction: kit.windup_fraction.ok_or("tristana kit needs attack.windupFraction")?,
            q_as_pct: kit.at_rank("gen.Q.asBonus", ranks.q)? * 100.0,
            q_buff_dur: kit.num("gen.Q.buffDurationS")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_active_dmg: kit.hit("gen.E.activeDamage", ranks.e, sheet)?,
            e_stack_amp: kit.num("gen.E.perStackAmp")?,
            e_crit_mod: kit.num("gen.E.critChanceModifier")?,
            e_max_stacks: kit.num("gen.E.maxStacks")? as i64,
            e_duration: kit.num("gen.E.activeDurationS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
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

    fn bonus_as(&self, t: f64) -> f64 {
        if t < self.s.q_buff_until {
            self.q_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        // Explosive Charge stacks are an on-hit effect
        self.add_e_stack(e);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // Rapid Fire: no cast time, deals no damage, its cooldown always
        // outlasts its own buff so recasting on cooldown never clashes
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        self.s.q_buff_until = t + self.q_buff_dur;
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the engine has already primed Spellblade and
        // delayed the first attack past the 0.25 s cast
        if self.ranks.r == 0 {
            return;
        }
        e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.ult_hatefog();
        self.add_e_stack(e);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_active {
                out[n] = (self.s.e_expire_at, Kind::Ev(EV_E_EXPIRE));
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
                self.add_e_stack(e);
            }
            Kind::Ev(EV_E_CAST) => {
                // the toss deals 0 damage; its cast time is 100% of the
                // current attack windup, recomputed live
                let b = self.bonus_as(t);
                let cast_time = e.attack_windup(b, self.windup_fraction);
                e.st.next_attack = pymax(e.st.next_attack, t + cast_time);
                e.prime_spellblade();
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_active = true;
                self.s.e_stacks = 0;
                self.s.e_expire_at = t + self.e_duration;
            }
            Kind::Ev(EV_E_EXPIRE) => {
                let stacks = self.s.e_stacks;
                self.detonate(e, stacks);
                self.s.e_active = false;
                self.s.e_expire_at = INF;
                self.s.e_stacks = 0;
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
