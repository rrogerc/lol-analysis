//! Nidalee. Opens with Primal Surge on herself for its attack-speed buff,
//! then Javelin Toss (assumed at max travel distance) to nuke and apply
//! Hunted, then swaps into Cougar Form (free, via Hunted's cooldown reset)
//! and stays there: Takedown arms her next attack (post-effect cooldown,
//! attack-timer reset), Pounce and Swipe go out on their flat cooldown.
//! Aspect of the Cougar (R) itself is a non-damaging stance swap: its
//! damage is attributed to the Q/W/E it empowers, see "unused" in the kit.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// The opening Primal Surge cast (self attack-speed buff).
const EV_SURGE: u8 = 0;
/// Cougar Form's Pounce, cast on cooldown.
const EV_W: u8 = 1;
/// Cougar Form's Swipe, cast on cooldown.
const EV_E: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Hunted mark duration (Prowl).
    hunted_dur: f64,
    /// Javelin Toss's damage, already scaled by the assumed max-distance bonus.
    q_human_dmg: f64,
    /// Javelin Toss's (Human Q) own cooldown.
    q_cd: f64,
    /// Takedown's own scaling bonus, before the missing-health and Hunted multipliers.
    q_cougar_dmg: f64,
    /// Takedown's missing-health damage-amp coefficient at Aspect of the Cougar's rank.
    q_cougar_amp: f64,
    /// Takedown's Hunted damage bonus, as a fraction.
    q_cougar_hunted_bonus: f64,
    /// Takedown's own (post-effect) cooldown.
    q_cougar_cd: f64,
    /// Pounce's impact damage.
    w_dmg: f64,
    /// Pounce's own (on-cast) cooldown.
    w_cd: f64,
    /// Swipe's damage.
    e_cougar_dmg: f64,
    /// Swipe's own cooldown.
    e_cougar_cd: f64,
    /// Primal Surge's bonus attack speed, in percent.
    e_surge_as_pct: f64,
    /// Primal Surge's attack-speed buff duration.
    e_surge_dur: f64,
    src_q_takedown: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// True while still in Human Form (before the opening Javelin + swap).
    human: bool,
    /// Takedown is armed, waiting for the next basic attack to consume it.
    q_armed: bool,
    /// Set when Takedown just paid out: the next schedule_attack resets the timer.
    q_reset_pending: bool,
    /// When the Hunted mark from the opening Javelin Toss expires.
    hunted_until: f64,
    /// Whether the opening Primal Surge has been cast.
    surge_cast: bool,
    /// When Primal Surge's attack-speed buff expires.
    e_as_until: f64,
    /// Cougar Form's Pounce and Swipe ready times.
    w_ready: f64,
    e_ready: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            human: true,
            q_armed: false,
            q_reset_pending: false,
            hunted_until: -INF,
            surge_cast: false,
            e_as_until: -INF,
            w_ready: 0.0,
            e_ready: 0.0,
        };
        let range_mult = kit.num("gen.Q.human.rangeMultiplierAssumed")?;
        let q_human_base = kit.hit("gen.Q.human.damage", ranks.q, sheet)?;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("nidalee kit needs attack.windupFraction")?,
            hunted_dur: kit.num("gen.P.huntedDurationS")?,
            q_human_dmg: q_human_base * range_mult,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cougar_dmg: kit.hit("gen.Q.cougar.damage", ranks.r, sheet)?,
            q_cougar_amp: kit.at_rank("gen.Q.cougar.missingHpAmpCoef", ranks.r)?,
            q_cougar_hunted_bonus: kit.num("gen.Q.cougar.huntedBonusPct")? / 100.0,
            q_cougar_cd: kit.num("gen.Q.cougar.cooldownS")?,
            w_dmg: kit.hit("gen.W.cougar.damage", ranks.r, sheet)?,
            w_cd: kit.num("gen.W.cougar.cooldownS")?,
            e_cougar_dmg: kit.hit("gen.E.cougar.damage", ranks.r, sheet)?,
            e_cougar_cd: kit.num("gen.E.cougar.cooldownS")?,
            e_surge_as_pct: kit.at_rank("gen.E.human.bonusAsPct", ranks.e)? * 100.0,
            e_surge_dur: kit.num("gen.E.human.durationS")?,
            src_q_takedown: intern("Q takedown"),
            s: state,
            s0: state,
        })
    }

    fn reset(&mut self) {
        self.s = self.s0;
    }

    fn ranged(&self) -> bool {
        true
    }

    fn attack_range(&self) -> f64 {
        self.attack_range
    }

    fn bonus_as(&self, t: f64) -> f64 {
        if t < self.s.e_as_until {
            self.e_surge_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        // Takedown pays out on the next attack after it is armed.
        if self.s.q_armed {
            self.s.q_armed = false;
            let t = e.st.t;
            let missing_frac = (e.target_hp - pymax(e.st.hp, 0.0)) / e.target_hp;
            let mut mult = 1.0 + missing_frac * self.q_cougar_amp;
            if t < self.s.hunted_until {
                mult *= 1.0 + self.q_cougar_hunted_bonus;
            }
            let dmg = self.q_cougar_dmg * mult;
            e.deal(dmg, DType::Magic, self.src_q_takedown, false, true, 1.0);
            e.st.q_ready = t + e.basic_cd(self.q_cougar_cd);
            self.s.q_reset_pending = true;
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.s.q_reset_pending {
            // Takedown resets Nidalee's basic attack timer.
            self.s.q_reset_pending = false;
            e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
        } else {
            e.st.next_attack = t + e.attack_period(b);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if !self.s.human && self.s.q_armed {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.human {
            // Javelin Toss.
            e.deal(self.q_human_dmg, DType::Magic, SRC_Q, false, true, 1.0);
            e.ability_cast_proc();
            e.eclipse_hit();
            e.prime_spellblade();
            e.lockout();
            self.s.hunted_until = t + self.hunted_dur;
            e.st.q_ready = t + e.basic_cd(self.q_cd);
            if self.ranks.r > 0 {
                // The Hunted mark just applied resets Aspect of the Cougar's
                // cooldown: swap into Cougar Form for free, Takedown ready now.
                self.s.human = false;
                e.st.q_ready = t;
            }
        } else {
            // Takedown: arm the next basic attack (no cast time, no lockout).
            self.s.q_armed = true;
            e.prime_spellblade();
            e.ability_cast_proc();
        }
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 && !self.s.surge_cast {
            out[n] = (0.0, Kind::Ev(EV_SURGE));
            n += 1;
        }
        if !self.s.human {
            if self.ranks.w > 0 {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W));
                n += 1;
            }
            if self.ranks.e > 0 {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_SURGE) => {
                self.s.surge_cast = true;
                self.s.e_as_until = t + self.e_surge_dur;
                e.prime_spellblade();
                e.ability_cast_proc();
                e.lockout();
            }
            Kind::Ev(EV_W) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_E) => {
                self.s.e_ready = t + e.basic_cd(self.e_cougar_cd);
                e.deal(self.e_cougar_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
