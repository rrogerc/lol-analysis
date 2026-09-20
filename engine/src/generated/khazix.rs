//! Kha'Zix. A melee auto-attacker whose kit rides Unseen Threat: Void
//! Assault (R) is opened purely to arm Unseen Threat and to grant its free
//! recast for a second proc, while Taste Their Fear (Q, evolved and always
//! Isolated-tier against the lone dummy), Void Spike (W) and Leap (E) are
//! each cast as soon as they come off cooldown.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Void Spike and Leap come off cooldown; Void Assault's free recast fires.
const EV_W: u8 = 0;
const EV_E: u8 = 1;
const EV_R_RECAST: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    /// Unseen Threat: flat base by level, and its bonus-AD fraction.
    p_dmg_base: f64,
    p_ad_ratio: f64,
    /// Taste Their Fear's Isolated damage instance (always used: the dummy
    /// is always Isolated), its base cooldown and Evolved Reaper Claws'
    /// isolated-target cooldown reduction fraction.
    q_iso_dmg: f64,
    q_cd_base: f64,
    q_evo_cdr_frac: f64,
    w_dmg: f64,
    w_cd_base: f64,
    e_dmg: f64,
    e_cd_base: f64,
    /// Void Assault's stealth duration and the delay after leaving it before
    /// the free recast is available.
    r_stealth_s: f64,
    r_recast_delay_s: f64,
    src_p: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    w_ready: f64,
    e_ready: f64,
    /// When Void Assault's free recast may be used (INF: none pending).
    r_recast_at: f64,
    /// Unseen Threat: armed and waiting for the next attack to start; and
    /// armed for the attack currently landing (consumed by its on-hit).
    p_pending: bool,
    p_armed_this_attack: bool,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            w_ready: 0.0,
            e_ready: 0.0,
            r_recast_at: INF,
            p_pending: false,
            p_armed_this_attack: false,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            p_dmg_base: kit.at_level("gen.P.dmgByLevel", level)?,
            p_ad_ratio: kit.num("gen.P.bonusAdRatio")?,
            q_iso_dmg: kit.hit("gen.Q.isoDamage", ranks.q, sheet)?,
            q_cd_base: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_evo_cdr_frac: kit.num("gen.Q.evolvedIsolationCdrPct")? / 100.0,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd_base: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd_base: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_stealth_s: kit.num("gen.R.stealthDurationS")?,
            r_recast_delay_s: kit.num("gen.R.recastDelayS")?,
            src_p: intern("P onhit"),
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
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, _e: &mut Engine) {
        // Unseen Threat arms the very next attack that starts
        if self.s.p_pending {
            self.s.p_pending = false;
            self.s.p_armed_this_attack = true;
        }
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        if self.s.p_armed_this_attack {
            self.s.p_armed_this_attack = false;
            let dmg = self.p_dmg_base + self.p_ad_ratio * e.p.sheet.ad_bonus;
            e.deal(dmg, DType::Magic, self.src_p, false, false, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // Isolated target: always true for the lone dummy, so Evolved
        // Reaper Claws' cooldown reduction always applies
        let cd = self.q_cd_base * (1.0 - self.q_evo_cdr_frac);
        e.st.q_ready = e.st.t + e.basic_cd(cd);
        e.deal(self.q_iso_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // The opening cast: no direct damage, just arms Unseen Threat and
        // schedules the free recast for a second proc
        let t = e.st.t;
        self.s.p_pending = true;
        self.s.r_recast_at = t + self.r_stealth_s + self.r_recast_delay_s;
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E));
            n += 1;
        }
        if self.s.r_recast_at != INF {
            out[n] = (self.s.r_recast_at, Kind::Ev(EV_R_RECAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd_base);
                e.deal(self.w_dmg, DType::Physical, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_E) => {
                // no cast time: Leap's landing damage, no attack lockout
                self.s.e_ready = t + e.basic_cd(self.e_cd_base);
                e.deal(self.e_dmg, DType::Physical, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_RECAST) => {
                // the free recast: no damage, just a second Unseen Threat
                self.s.r_recast_at = INF;
                self.s.p_pending = true;
                e.prime_spellblade();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
