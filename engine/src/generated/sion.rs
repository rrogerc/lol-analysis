//! Sion. Roar of the Slayer opens for its armor shred; Decimating Smash is
//! held for its full 1 s ramp before releasing, Unstoppable Onslaught (cast
//! by the engine at t = 0) is held for its full 3 s ramp before the ending
//! leap-slam lands, and Soul Furnace is cast and detonated at its minimum
//! 3 s recast delay. Q's charge and R's channel/leap both lock out attacks
//! and each other's cast (only Soul Furnace remains castable through them).

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Decimating Smash's release (the auto/manual recast that deals damage).
const EV_Q_RELEASE: u8 = 0;
/// Soul Furnace: the initial cast (shield), then the detonating recast.
const EV_W_CAST: u8 = 1;
const EV_W_DETONATE: u8 = 2;
/// Roar of the Slayer: the 0.25 s cast, then the hit that lands after it.
const EV_E_CAST: u8 = 3;
const EV_E_HIT: u8 = 4;
/// Unstoppable Onslaught: a later cast (if it comes off cooldown again),
/// then the slam that ends the channel/leap.
const EV_R_CAST: u8 = 5;
const EV_R_SLAM: u8 = 6;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_dmg: f64,
    q_cd: f64,
    q_ramp_s: f64,
    w_dmg: f64,
    w_target_ratio: f64,
    w_cd: f64,
    w_recast_delay_s: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cast_s: f64,
    e_shred_dur: f64,
    r_dmg: f64,
    r_cd: f64,
    r_ramp_s: f64,
    r_leap_delay_s: f64,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// When the pending Decimating Smash release lands (INF: not charging).
    q_release_at: f64,
    w_ready: f64,
    /// When the pending Soul Furnace detonation lands (INF: none pending).
    w_detonate_at: f64,
    e_ready: f64,
    /// When the pending Roar of the Slayer hit lands (INF: none pending).
    e_hit_at: f64,
    r_ready: f64,
    /// When the pending Unstoppable Onslaught slam lands (INF: not active).
    r_slam_at: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let _ = level;
        let state = State {
            q_release_at: INF,
            w_ready: 0.0,
            w_detonate_at: INF,
            e_ready: 0.0,
            e_hit_at: INF,
            r_ready: 0.0,
            r_slam_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("sion kit needs attack.windupFraction")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_ramp_s: kit.num("gen.Q.rampS")?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_target_ratio: kit.at_rank("gen.W.targetMaxHpRatio", ranks.w)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_recast_delay_s: kit.num("gen.W.recastDelayS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            e_shred_dur: kit.num("abilities.Q.shred.durationS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_ramp_s: kit.num("gen.R.rampS")?,
            r_leap_delay_s: kit.num("gen.R.leapDelayS")?,
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

    fn bonus_as(&self, _t: f64) -> f64 {
        0.0
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if self.s.q_release_at != INF {
            // already charging; the release event handles the damage
            return INF;
        }
        if self.s.r_slam_at != INF {
            // Unstoppable Onslaught's channel/leap disables Decimating Smash
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // the charge itself has no cast time; the release, 1 s later, is
        // what deals damage and is locked out for 0.25 s
        let t = e.st.t;
        self.s.q_release_at = t + self.q_ramp_s;
        e.st.next_attack = pymax(e.st.next_attack, self.s.q_release_at);
        e.st.q_ready = INF;
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the engine has already primed Spellblade for it.
        // The channel plus the ending leap block attacks and Q/E until the
        // slam lands.
        let t = e.st.t;
        self.s.r_slam_at = t + self.r_ramp_s + self.r_leap_delay_s;
        e.st.next_attack = pymax(e.st.next_attack, self.s.r_slam_at);
        self.s.r_ready = INF;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_release_at != INF {
            out[n] = (self.s.q_release_at, Kind::Ev(EV_Q_RELEASE));
            n += 1;
        }
        if self.ranks.w > 0 {
            if self.s.w_detonate_at != INF {
                out[n] = (self.s.w_detonate_at, Kind::Ev(EV_W_DETONATE));
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_hit_at != INF {
                out[n] = (self.s.e_hit_at, Kind::Ev(EV_E_HIT));
                n += 1;
            } else if self.s.r_slam_at == INF && self.s.q_release_at == INF {
                // Q's charge and R's channel/leap both disable Roar of the Slayer
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
                n += 1;
            }
        }
        if self.ranks.r > 0 {
            if self.s.r_slam_at != INF {
                out[n] = (self.s.r_slam_at, Kind::Ev(EV_R_SLAM));
                n += 1;
            } else if self.s.r_ready != INF && self.s.q_release_at == INF {
                // Q's charge also disables Unstoppable Onslaught
                out[n] = (pymax(self.s.r_ready, e.st.t), Kind::Ev(EV_R_CAST));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_RELEASE) => {
                // the recast that deals the damage; it does not itself
                // count as an ability activation (matches R and W below)
                self.s.q_release_at = INF;
                e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
                e.lockout();
                e.st.q_ready = t + e.basic_cd(self.q_cd);
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_detonate_at = t + self.w_recast_delay_s;
                self.s.w_ready = INF;
                e.prime_spellblade();
            }
            Kind::Ev(EV_W_DETONATE) => {
                // manual detonation does not count as an ability activation
                self.s.w_detonate_at = INF;
                let amt = self.w_dmg + self.w_target_ratio * e.target_hp;
                e.deal(amt, DType::Magic, SRC_W, false, true, 1.0);
                self.s.w_ready = t + e.basic_cd(self.w_cd);
            }
            Kind::Ev(EV_E_CAST) => {
                // cooldown starts at cast; the hit lands after the cast time
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_hit_at = t + self.e_cast_s;
                e.lockout();
            }
            Kind::Ev(EV_E_HIT) => {
                self.s.e_hit_at = INF;
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.st.shred_until = t + self.e_shred_dur;
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_CAST) => {
                // a later activation (R came off cooldown again); unlike the
                // opening cast, the engine has not already primed Spellblade
                self.s.r_slam_at = t + self.r_ramp_s + self.r_leap_delay_s;
                e.st.next_attack = pymax(e.st.next_attack, self.s.r_slam_at);
                self.s.r_ready = INF;
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_SLAM) => {
                // the ending leap-slam; it does not count as an activation
                self.s.r_slam_at = INF;
                e.deal(self.r_dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ult_hatefog();
                self.s.r_ready = t + e.ult_cd(self.r_cd);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
