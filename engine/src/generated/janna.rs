//! Janna. Howling Gale is a two-phase cast (charge to its full 3s, then a
//! recast that launches the whirlwind and hits 1.25s later); Zephyr is cast
//! on cooldown for direct magic damage plus Tailwind's movement-speed-derived
//! bonus; Eye of the Storm is cast on herself on cooldown purely for its
//! bonus attack damage while the shield holds; Tailwind also rides every
//! basic attack as bonus magic damage; Monsoon is never cast.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Zephyr is cast on cooldown.
const EV_W_CAST: u8 = 0;
/// Eye of the Storm is cast on cooldown (self-target, for the AD buff).
const EV_E_CAST: u8 = 1;
/// Howling Gale: the (automatic, full-charge) recast, then the hit 1.25s later.
const EV_Q_RECAST: u8 = 2;
const EV_Q_HIT: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    /// Howling Gale: total damage at full charge, its cooldown, the charge
    /// duration and the missile's travel time.
    q_dmg: f64,
    q_cd: f64,
    q_max_charge: f64,
    q_travel_s: f64,
    /// Zephyr: its damage (including Tailwind's bonus) and cooldown.
    w_dmg: f64,
    w_cd: f64,
    /// Eye of the Storm: the bonus AD it grants, its cooldown and shield duration.
    e_ad_bonus: f64,
    e_cd: f64,
    e_shield_dur: f64,
    /// Tailwind: bonus magic damage on every basic attack.
    tailwind_dmg: f64,
    src_p_onhit: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// Howling Gale: when the pending charge matures into the recast, and
    /// when the recast's missile lands (INF: nothing pending).
    q_recast_at: f64,
    q_hit_at: f64,
    w_ready: f64,
    e_ready: f64,
    /// Eye of the Storm's self bonus-AD buff runs until this time.
    e_buff_until: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let ms_ratio = kit.num("gen.P.msToDamageRatio")?;
        let base_ms = kit.num("gen.P.baseMoveSpeed")?;
        let bonus_ms = pymax(sheet.move_speed - base_ms, 0.0);
        let tailwind_dmg = ms_ratio * bonus_ms;

        let q_min = kit.hit("gen.Q.minDamage", ranks.q, sheet)?;
        let q_bonus_per_s = kit.hit("gen.Q.bonusPerSecond", ranks.q, sheet)?;
        let q_max_charge = kit.num("gen.Q.maxChargeS")?;

        let w_base = kit.hit("gen.W.damage", ranks.w, sheet)?;

        let state = State {
            q_recast_at: INF,
            q_hit_at: INF,
            w_ready: 0.0,
            e_ready: 0.0,
            e_buff_until: -1.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg: q_min + q_bonus_per_s * q_max_charge,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_max_charge,
            q_travel_s: kit.num("gen.Q.missileTravelS")?,
            w_dmg: w_base + tailwind_dmg,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_ad_bonus: kit.hit("gen.E.bonusAD", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_shield_dur: kit.num("gen.E.shieldDurationS")?,
            tailwind_dmg,
            src_p_onhit: intern("P onhit"),
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

    fn attack_damage(&self, e: &Engine) -> f64 {
        if self.ranks.e > 0 && e.st.t < self.s.e_buff_until {
            e.p.ad + self.e_ad_bonus
        } else {
            e.p.ad
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        e.deal(self.tailwind_dmg, DType::Magic, self.src_p_onhit, false, false, 1.0);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // the initial cast: no cast time, no damage; charges up and starts
        // the real (14s) cooldown immediately
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        self.s.q_recast_at = t + self.q_max_charge;
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.q_recast_at != INF {
            out[n] = (self.s.q_recast_at, Kind::Ev(EV_Q_RECAST));
            n += 1;
        }
        if self.s.q_hit_at != INF {
            out[n] = (self.s.q_hit_at, Kind::Ev(EV_Q_HIT));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_buff_until = t + self.e_shield_dur;
                e.prime_spellblade();
            }
            Kind::Ev(EV_Q_RECAST) => {
                // the (automatic, full-charge) recast: still no damage, the
                // whirlwind now travels for 1.25s before it lands
                self.s.q_recast_at = INF;
                self.s.q_hit_at = t + self.q_travel_s;
                e.prime_spellblade();
            }
            Kind::Ev(EV_Q_HIT) => {
                self.s.q_hit_at = INF;
                e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
