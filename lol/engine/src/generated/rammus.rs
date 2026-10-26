//! Rammus. Spiked Shell converts current total armor and magic resistance
//! into bonus AD (higher while Defensive Ball Curl is active), Powerball
//! collides almost instantly against the stationary dummy then holds the
//! channel until its 1s minimum recast, Defensive Ball Curl is kept up on
//! cooldown for its resistances, and Soaring Slam is cast on cooldown.
//! Frenzying Taunt is never cast: it deals no damage to a champion-classified
//! dummy (see "unused" in the kit).

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Powerball ends its channel (manual recast, 1s after cast).
const EV_Q_RECAST: u8 = 0;
/// Defensive Ball Curl starts, and ends (post-effect cooldown starts then).
const EV_W_CAST: u8 = 1;
const EV_W_END: u8 = 2;
/// Soaring Slam recast, if it comes off cooldown within the fight.
const EV_R_CAST: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    /// Spiked Shell's bonus AD with W inactive, and the extra amount while
    /// W's bonus armor/MR are also feeding the conversion.
    p_ad_base: f64,
    p_ad_with_w: f64,
    q_dmg: f64,
    q_cd: f64,
    q_recast_delay: f64,
    w_cd: f64,
    w_duration: f64,
    r_dmg: f64,
    r_cd: f64,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// When the pending Powerball recast happens (INF: not channeling).
    q_recast_at: f64,
    w_ready: f64,
    w_active: bool,
    /// When the active Defensive Ball Curl ends on its own (INF: inactive).
    w_end_at: f64,
    /// When Soaring Slam may next be cast (INF: not cast yet).
    r_ready: f64,
}

impl GenDriver {
    fn cast_r_damage(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.ult_hatefog();
        self.s.r_ready = t + e.ult_cd(self.r_cd);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let p_armor_ratio = kit.num("gen.P.armorRatio")?;
        let p_mr_ratio = kit.num("gen.P.mrRatio")?;
        let p_ad_base = p_armor_ratio * sheet.armor + p_mr_ratio * sheet.mr;

        let w_flat_armor = kit.at_rank("gen.W.flatArmor", ranks.w)?;
        let w_pct_armor = kit.at_rank("gen.W.pctArmor", ranks.w)?;
        let w_flat_mr = kit.at_rank("gen.W.flatMr", ranks.w)?;
        let w_pct_mr = kit.at_rank("gen.W.pctMr", ranks.w)?;
        let w_bonus_armor = w_flat_armor + w_pct_armor * sheet.armor;
        let w_bonus_mr = w_flat_mr + w_pct_mr * sheet.mr;
        let p_ad_with_w =
            p_ad_base + p_armor_ratio * w_bonus_armor + p_mr_ratio * w_bonus_mr;

        let state = State {
            q_recast_at: INF,
            w_ready: 0.0,
            w_active: false,
            w_end_at: INF,
            r_ready: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            p_ad_base,
            p_ad_with_w,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_recast_delay: kit.num("gen.Q.recastDelayS")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_duration: kit.num("gen.W.durationS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
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
        let bonus = if self.s.w_active { self.p_ad_with_w } else { self.p_ad_base };
        e.p.ad + bonus
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 || self.s.q_recast_at != INF {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        // channeling: the ready time is finalized when the recast lands
        e.st.q_ready = INF;
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        // the channel disables attacks until the manual recast
        e.st.next_attack = pymax(e.st.next_attack, t + self.q_recast_delay);
        if self.s.w_active {
            // Defensive Ball Curl ends immediately if Powerball is cast
            self.s.w_active = false;
            self.s.w_end_at = INF;
            self.s.w_ready = t + e.basic_cd(self.w_cd);
        }
        self.s.q_recast_at = t + self.q_recast_delay;
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        self.cast_r_damage(e);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_recast_at != INF {
            out[n] = (self.s.q_recast_at, Kind::Ev(EV_Q_RECAST));
            n += 1;
        }
        if self.ranks.w > 0 {
            if self.s.w_active {
                out[n] = (self.s.w_end_at, Kind::Ev(EV_W_END));
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.r > 0 && self.s.r_ready != INF {
            out[n] = (pymax(self.s.r_ready, e.st.t), Kind::Ev(EV_R_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_RECAST) => {
                // ends the channel: no damage, cooldown starts now (post-effect)
                self.s.q_recast_at = INF;
                e.st.q_ready = t + e.basic_cd(self.q_cd);
                e.prime_spellblade();
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_active = true;
                self.s.w_end_at = t + self.w_duration;
                e.prime_spellblade();
            }
            Kind::Ev(EV_W_END) => {
                self.s.w_active = false;
                self.s.w_end_at = INF;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
            }
            Kind::Ev(EV_R_CAST) => {
                self.cast_r_damage(e);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
