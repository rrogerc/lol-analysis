//! Diana. Melee auto-attacker whose passive rides her attacks: Moonsilver
//! Blade gives always-on bonus attack speed, tripled for 5s after any
//! ability cast, and every third attack in a row cleaves for bonus magic
//! damage. Moonfall opens the fight; Crescent Strike is cast on cooldown
//! (0.25s cast time) and, as soon as its cast time ends, Lunar Rush is cast
//! (via a chase event) to consume Moonlight, dropping Lunar Rush's cooldown
//! to 0.25s and resetting the next attack; Pale Cascade is cast on cooldown,
//! its three orbs treated as landing instantly on the adjacent dummy.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Lunar Rush chasing Moonlight; Pale Cascade's orbs (treated as landing on
/// cast); Moonfall's delayed beam.
const EV_E: u8 = 0;
const EV_W: u8 = 1;
const EV_R_EXPLODE: u8 = 2;

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
    q_cast_s: f64,
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
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
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
    /// The earliest a cast readied at `ready` can start: not before now, and
    /// not inside another cast.
    fn castable_at(&self, e: &Engine, ready: f64) -> f64 {
        pymax(pymax(ready, e.st.t), self.s.busy_until)
    }

    /// A cast with a cast time just started: no other cast and no attack
    /// until it ends (an attack already due later keeps its time).
    fn busy_for(&mut self, e: &mut Engine, cast_s: f64) {
        self.s.busy_until = e.st.t + cast_s;
        e.st.next_attack = pymax(e.st.next_attack, self.s.busy_until);
    }

    /// Lunar Rush: dashes and deals its damage now, consumes Moonlight for
    /// the cooldown reset if present, triggers the tripled-AS window, and
    /// resets the next attack to just a windup away (the post-dash swing).
    /// Lunar Rush itself has no cast time, so it does not call `busy_for`.
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
            busy_until: 0.0,
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
            q_cast_s: kit.num("gen.Q.castTimeS")?,
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
        self.castable_at(e, e.st.q_ready)
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
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the beam lands after the cast time plus the
        // delay before it strikes
        let t = e.st.t;
        self.s.r_explode_at = t + self.r_cast_s + self.r_delay_s;
        self.s.tripled_until = t + self.p_tripled_dur;
        e.prime_spellblade();
        self.busy_for(e, self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            // Lunar Rush has no cast time of its own, but still cannot start
            // inside Crescent Strike's cast time
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E));
            n += 1;
        }
        if self.ranks.w > 0 {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W));
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
            Kind::Ev(EV_E) => {
                self.cast_e(e);
            }
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
