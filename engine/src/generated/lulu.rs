//! Lulu. Pix rides every landed basic attack for 3 magic bolts, Glitterlance
//! fires two bolts on cast (Lulu's and Pix's, both assumed to connect),
//! Whimsy is self-cast on cooldown for its attack-speed buff, and Help, Pix!
//! is cast on the dummy on cooldown for its own damage while sending Pix
//! away (suspending his on-attack bolts) for its duration. Wild Growth deals
//! no damage and is never cast.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Whimsy self-cast comes off cooldown.
const EV_W: u8 = 0;
/// Help, Pix! comes off cooldown.
const EV_E: u8 = 1;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Pix's on-attack bolts: damage per bolt and how many.
    p_bolt_dmg: f64,
    p_bolts: i64,
    src_p: SourceId,
    /// Glitterlance: Lulu's bolt damage, and Pix's bolt as a fraction of it.
    q_dmg1: f64,
    q_second_mult: f64,
    q_cd: f64,
    /// Whimsy: percent bonus attack speed, its duration, and its cooldown.
    w_as_pct: f64,
    w_dur: f64,
    w_cd: f64,
    e_dmg: f64,
    e_cd: f64,
    /// How long Pix is away from Lulu (no on-attack bolts) after an
    /// enemy-cast Help, Pix!.
    e_pix_away_s: f64,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    w_ready: f64,
    /// Whimsy's bonus attack speed lasts until this time.
    w_active_until: f64,
    e_ready: f64,
    /// Pix does not fire his on-attack bolts until this time.
    pix_away_until: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            w_ready: 0.0,
            w_active_until: -1.0,
            e_ready: 0.0,
            pix_away_until: -1.0,
        };
        let p_base = kit.at_level("gen.P.damagePerBoltByLevel", level)?;
        let p_ap_ratio = kit.num("gen.P.apRatioPerBolt")?;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("lulu kit needs attack.windupFraction")?,
            p_bolt_dmg: p_base + p_ap_ratio * sheet.ap,
            p_bolts: kit.num("gen.P.bolts")? as i64,
            src_p: intern("P"),
            q_dmg1: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_second_mult: kit.num("gen.Q.secondBoltMult")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_as_pct: kit.at_rank("gen.W.asBonusPct", ranks.w)?,
            w_dur: kit.at_rank("gen.W.durationS", ranks.w)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_pix_away_s: kit.num("gen.E.pixOnEnemyDurationS")?,
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
            self.w_as_pct
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
        // Pix's bolts fire once per landed basic attack, unless he is away
        // helping a target of Help, Pix!
        if e.st.t < self.s.pix_away_until {
            return;
        }
        let mut i: i64 = 0;
        while i < self.p_bolts {
            e.deal(self.p_bolt_dmg, DType::Magic, self.src_p, false, false, 1.0);
            i += 1;
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        // Lulu's bolt, then Pix's reduced-value bolt, both on the target.
        e.deal(self.q_dmg1, DType::Magic, SRC_Q, false, true, 1.0);
        let second = self.q_dmg1 * self.q_second_mult;
        e.deal(second, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
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
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                // instant self-cast: no cast time, no lockout
                self.s.w_active_until = t + self.w_dur;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.prime_spellblade();
            }
            Kind::Ev(EV_E) => {
                // instant enemy-cast: no cast time, no lockout
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                self.s.pix_away_until = t + self.e_pix_away_s;
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
