//! Trundle. A mixed on-hit/DoT auto-attacker: Chomp (Q) arms the next basic
//! attack for bonus physical damage, resets the attack timer and (post-land)
//! grants Trundle bonus AD; Frozen Domain (W) is a no-cast-time attack-speed
//! buff kept up on cooldown; Subjugate (R) is cast at t=0 and drains %max HP
//! magic damage half on-cast and half over four ticks while shredding the
//! target's armor/MR. Pillar of Ice (E) deals no damage and is never cast
//! (its 0.25 s cast time is kept in the kit for completeness only).

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Frozen Domain's recast; Subjugate's cast landing and its drain ticks.
const EV_W_CAST: u8 = 0;
const EV_R_LAND: u8 = 1;
const EV_R_TICK: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_cd: f64,
    q_base: f64,
    q_adratio: f64,
    q_selfad: f64,
    q_adbuff_dur: f64,
    w_cd: f64,
    w_aspct: f64,
    w_dur: f64,
    r_pct: f64,
    r_upfront_frac: f64,
    r_tick_count: i64,
    r_tick_interval: f64,
    r_cast_time: f64,
    r_shred_dur: f64,
    src_r_tick: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    q_armed: bool,
    /// Trundle's own bonus-AD buff from a landed empowered attack.
    q_adbuff_until: f64,
    w_ready: f64,
    w_active_until: f64,
    /// When Subjugate's cast completes and its up-front half lands (INF: none pending).
    r_land_at: f64,
    r_tick_idx: i64,
    /// When the next drain tick fires (INF: none pending).
    r_next_tick_at: f64,
    r_tick_dmg: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            q_armed: false,
            q_adbuff_until: 0.0,
            w_ready: 0.0,
            w_active_until: 0.0,
            r_land_at: INF,
            r_tick_idx: 0,
            r_next_tick_at: INF,
            r_tick_dmg: 0.0,
        };
        // Pillar of Ice (E) is never cast; its cast time is still read here so
        // the number is validated against the kit even though it goes unused.
        let _e_cast_time_unused = kit.num("gen.E.castTimeS")?;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("trundle kit needs attack.windupFraction")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_base: kit.at_rank("gen.Q.damage.base", ranks.q)?,
            q_adratio: kit.at_rank("gen.Q.damage.adRatio", ranks.q)?,
            q_selfad: kit.at_rank("gen.Q.selfAdBuff.base", ranks.q)?,
            q_adbuff_dur: kit.num("gen.Q.adBuffDurationS")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_aspct: kit.at_rank("gen.W.bonusAsPct", ranks.w)?,
            w_dur: kit.num("gen.W.durationS")?,
            r_pct: kit.at_rank("gen.R.pctHpBase", ranks.r)? + kit.num("gen.R.apRatioTotal")? * sheet.ap,
            r_upfront_frac: kit.num("gen.R.upfrontFrac")?,
            r_tick_count: kit.num("gen.R.tickCount")? as i64,
            r_tick_interval: kit.num("gen.R.drainDurationS")? / kit.num("gen.R.tickCount")?,
            r_cast_time: kit.num("gen.R.castTimeS")?,
            r_shred_dur: kit.num("gen.R.shred.durationS")?,
            src_r_tick: intern("R tick"),
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
        if t < self.s.w_active_until {
            self.w_aspct
        } else {
            0.0
        }
    }

    fn attack_damage(&self, e: &Engine) -> f64 {
        if e.st.t < self.s.q_adbuff_until {
            e.p.ad + self.q_selfad
        } else {
            e.p.ad
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.q_armed {
            self.s.q_armed = false;
            let t = e.st.t;
            e.st.q_ready = t + e.basic_cd(self.q_cd);
            self.s.q_adbuff_until = t + self.q_adbuff_dur;
            let ad = self.attack_damage(e);
            let dmg = self.q_base + self.q_adratio * ad;
            e.deal(dmg, DType::Physical, SRC_Q, false, false, 1.0);
            e.ability_cast_proc();
            e.eclipse_hit();
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        self.s.q_armed = true;
        e.st.q_ready = INF;
        e.prime_spellblade();
        let t = e.st.t;
        let b = self.bonus_as(t);
        // Chomp resets Trundle's basic attack timer
        e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        self.s.r_land_at = e.st.t + self.r_cast_time;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.s.r_land_at != INF {
            out[n] = (self.s.r_land_at, Kind::Ev(EV_R_LAND));
            n += 1;
        }
        if self.s.r_next_tick_at != INF {
            out[n] = (self.s.r_next_tick_at, Kind::Ev(EV_R_TICK));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                self.s.w_active_until = t + self.w_dur;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_LAND) => {
                self.s.r_land_at = INF;
                let total = self.r_pct * e.target_hp;
                let initial = total * self.r_upfront_frac;
                self.s.r_tick_dmg = (total - initial) / (self.r_tick_count as f64);
                self.s.r_tick_idx = 0;
                self.s.r_next_tick_at = t + self.r_tick_interval;
                e.deal(initial, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                e.st.shred_until = t + self.r_shred_dur;
            }
            Kind::Ev(EV_R_TICK) => {
                self.s.r_tick_idx += 1;
                e.deal(self.s.r_tick_dmg, DType::Magic, self.src_r_tick, false, true, 1.0);
                e.ult_hatefog();
                if self.s.r_tick_idx >= self.r_tick_count {
                    self.s.r_next_tick_at = INF;
                } else {
                    self.s.r_next_tick_at = t + self.r_tick_interval;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
