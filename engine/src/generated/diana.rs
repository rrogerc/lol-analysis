//! Diana. Melee auto-attacker whose passive rides her attacks: Moonsilver
//! Blade gives always-on bonus attack speed, tripled for 5s after any
//! ability cast, and every third attack in a row cleaves for bonus magic
//! damage. Moonfall opens the fight; Crescent Strike is cast on cooldown and
//! immediately chained into Lunar Rush to consume Moonlight (dropping Lunar
//! Rush's cooldown to 0.25s and resetting the next attack); Pale Cascade is
//! cast on cooldown, its three orbs treated as landing instantly on the
//! adjacent dummy.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Pale Cascade's orbs (treated as landing on cast); Moonfall's delayed beam.
const EV_W: u8 = 0;
const EV_R_EXPLODE: u8 = 1;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    p_as_base_pct: f64,
    p_as_tripled_pct: f64,
    p_tripled_dur: f64,
    p_cleave_dmg: f64,
    p_attack_trigger: i64,
    q_dmg: f64,
    q_cd: f64,
    q_moonlight_dur: f64,
    w_orb_dmg: f64,
    w_cd: f64,
    e_dmg: f64,
    e_cd: f64,
    e_reset_s: f64,
    r_dmg: f64,
    r_cast_s: f64,
    r_delay_s: f64,
    src_p_cleave: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// Basic attacks landed since the last empowered (cleave) attack.
    p_attack_count: i64,
    /// Moonlight is present on the target until this time (-1: none).
    moonlight_until: f64,
    /// Moonsilver Blade's attack speed is tripled until this time.
    tripled_until: f64,
    w_ready: f64,
    e_ready: f64,
    /// Moonfall's delayed beam still to land (INF: none pending).
    r_explode_at: f64,
}

impl GenDriver {
    /// Lunar Rush: dashes and deals its damage now, consumes Moonlight for
    /// the cooldown reset if present, triggers the tripled-AS window, and
    /// resets the next attack to just a windup away (the post-dash swing).
    fn cast_e(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        if self.s.moonlight_until > t {
            self.s.moonlight_until = -1.0;
            self.s.e_ready = t + self.e_reset_s;
        } else {
            self.s.e_ready = t + e.basic_cd(self.e_cd);
        }
        self.s.tripled_until = t + self.p_tripled_dur;
        let b = self.bonus_as(t);
        let reset_at = t + e.attack_windup(b, self.windup_fraction);
        e.st.next_attack = pymin(e.st.next_attack, reset_at);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            p_attack_count: 0,
            moonlight_until: -1.0,
            tripled_until: -1.0,
            w_ready: 0.0,
            e_ready: 0.0,
            r_explode_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("diana kit needs attack.windupFraction")?,
            p_as_base_pct: kit.at_level("gen.P.asBaseByLevel", level)?,
            p_as_tripled_pct: kit.at_level("gen.P.asEmpoweredByLevel", level)?,
            p_tripled_dur: kit.num("gen.P.tripledDurationS")?,
            p_cleave_dmg: kit.at_level("gen.P.cleaveDamageBase", level)?
                + kit.num("gen.P.cleaveApRatio")? * sheet.ap,
            p_attack_trigger: kit.num("gen.P.attackTriggerCount")? as i64,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_moonlight_dur: kit.num("gen.Q.moonlightDurationS")?,
            w_orb_dmg: kit.hit("gen.W.orbDamage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_reset_s: kit.num("gen.E.moonlightResetCdS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_delay_s: kit.num("gen.R.delayS")?,
            src_p_cleave: intern("P cleave"),
            s: state,
            s0: state,
        })
    }

    fn reset(&mut self) {
        self.s = self.s0;
    }

    fn ranged(&self) -> bool {
        false
    }

    fn attack_range(&self) -> f64 {
        self.attack_range
    }

    fn bonus_as(&self, t: f64) -> f64 {
        if t < self.s.tripled_until {
            self.p_as_tripled_pct
        } else {
            self.p_as_base_pct
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        // Moonsilver Blade: every third attack in a row consumes the
        // stacks on-hit to cleave for bonus magic damage
        self.s.p_attack_count += 1;
        if self.s.p_attack_count >= self.p_attack_trigger {
            self.s.p_attack_count = 0;
            e.deal(self.p_cleave_dmg, DType::Magic, self.src_p_cleave, false, false, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.s.moonlight_until = t + self.q_moonlight_dur;
        self.s.tripled_until = t + self.p_tripled_dur;
        e.lockout();
        if self.ranks.e > 0 && e.st.t >= self.s.e_ready {
            self.cast_e(e);
        }
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the beam lands after the cast time plus the
        // delay before it strikes
        let t = e.st.t;
        self.s.r_explode_at = t + self.r_cast_s + self.r_delay_s;
        self.s.tripled_until = t + self.p_tripled_dur;
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W));
            n += 1;
        }
        if self.s.r_explode_at != INF {
            out[n] = (self.s.r_explode_at, Kind::Ev(EV_R_EXPLODE));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                // the three orbs, treated as detonating instantly on the
                // adjacent dummy; the cooldown starts right after
                e.deal(self.w_orb_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.deal(self.w_orb_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.deal(self.w_orb_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.tripled_until = t + self.p_tripled_dur;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
            }
            Kind::Ev(EV_R_EXPLODE) => {
                self.s.r_explode_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
