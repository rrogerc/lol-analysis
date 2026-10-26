//! Kog'Maw. A ranged auto-attacker with mixed damage: Caustic Spittle (Q) is
//! cast on cooldown for magic damage, an armor/MR shred, and grants a
//! continuous attack-speed passive once ranked; Bio-Arcane Barrage (W) is
//! recast on cooldown to arm attacks with an 8 s on-hit rider dealing magic
//! damage based on the target's maximum health; Void Ooze (E) and Living
//! Artillery (R) each have a real 0.25 s cast time that keeps Kog'Maw busy
//! (no other cast, no attack) until it ends; R's damage lands 0.6 s later,
//! via its own event, amplified by the target's missing (or low) health at
//! that moment, and R is recast the instant its short cooldown allows.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Bio-Arcane Barrage comes off cooldown and is recast.
const EV_W_CAST: u8 = 0;
/// Void Ooze comes off cooldown and is recast.
const EV_E_CAST: u8 = 1;
/// Living Artillery comes off cooldown and is recast.
const EV_R_CAST: u8 = 2;
/// A cast Living Artillery globule lands (0.6 s after cast).
const EV_R_IMPACT: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_as_pct: f64,
    q_dmg: f64,
    q_cd: f64,
    q_shred_dur: f64,
    w_cd: f64,
    /// Precomputed fraction of the target's max health per landed on-hit
    /// (base% + AP coefficient, already folded together for this build's AP).
    w_onhit_frac: f64,
    w_duration: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cast_time: f64,
    r_dmg_base: f64,
    r_cd: f64,
    r_cast_time: f64,
    r_impact_delay: f64,
    r_amp_coef: f64,
    r_amp_cap: f64,
    /// Fraction (0..1) of max health below which R's damage is doubled.
    r_low_thresh: f64,
    r_low_mult: f64,
    src_w_onhit: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    w_ready: f64,
    /// Bio-Arcane Barrage's on-hit rider is active until this time.
    w_until: f64,
    e_ready: f64,
    r_ready: f64,
    /// When a cast Living Artillery globule lands (INF: none pending).
    r_impact_at: f64,
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

    fn cast_w_now(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.w_ready = t + e.basic_cd(self.w_cd);
        self.s.w_until = t + self.w_duration;
        e.prime_spellblade();
        e.ability_cast_proc();
        // an instant toggle: no cast time, no lockout
    }

    fn cast_e_now(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.e_ready = t + e.basic_cd(self.e_cd);
        e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.e_cast_time);
    }

    fn cast_r_now(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        self.s.r_impact_at = t + self.r_impact_delay;
        e.prime_spellblade();
        self.busy_for(e, self.r_cast_time);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let w_base = kit.at_rank("gen.W.onhit.base", ranks.w)?;
        let w_ap_coef = kit.num("gen.W.onhit.apCoefPct")?;
        let w_pct_scale = kit.num("gen.W.onhit.pctScale")?;
        let w_onhit_frac = (w_base + w_ap_coef * sheet.ap) * w_pct_scale;

        let r_base = kit.at_rank("gen.R.damage.base", ranks.r)?;
        let r_ap_ratio = kit.at_rank("gen.R.damage.apRatio", ranks.r)?;
        let r_bonus_ad_ratio = kit.num("gen.R.damage.bonusAdRatio")?;
        let r_dmg_base = r_base + r_ap_ratio * sheet.ap + r_bonus_ad_ratio * sheet.ad_bonus;

        let state = State {
            busy_until: 0.0,
            w_ready: 0.0,
            w_until: -1.0,
            e_ready: 0.0,
            r_ready: 0.0,
            r_impact_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_as_pct: kit.at_rank("gen.Q.asPct", ranks.q)?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_shred_dur: kit.num_or("abilities.Q.shred.durationS", 0.0),
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_onhit_frac,
            w_duration: kit.num("gen.W.durationS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_time: kit.num("gen.E.castTimeS")?,
            r_dmg_base,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_time: kit.num("gen.R.castTimeS")?,
            r_impact_delay: kit.num("gen.R.impactDelayS")?,
            r_amp_coef: kit.num("gen.R.missingHpAmpCoefPct")?,
            r_amp_cap: kit.num("gen.R.missingHpAmpCapPct")?,
            r_low_thresh: kit.num("gen.R.lowHpThresholdPct")? / 100.0,
            r_low_mult: kit.num("gen.R.lowHpMult")?,
            src_w_onhit: intern("W onhit"),
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

    fn bonus_as(&self, _t: f64) -> f64 {
        self.q_as_pct
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        if e.st.t < self.s.w_until {
            let dmg = self.w_onhit_frac * e.target_hp;
            e.deal(dmg, DType::Magic, self.src_w_onhit, false, false, 1.0);
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
        e.st.shred_until = t + self.q_shred_dur;
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        // "basic attack timer" cast: costs no time of its own
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        self.cast_r_now(e);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.r > 0 {
            out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R_CAST));
            n += 1;
            if self.s.r_impact_at != INF {
                out[n] = (self.s.r_impact_at, Kind::Ev(EV_R_IMPACT));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        match kind {
            Kind::Ev(EV_W_CAST) => self.cast_w_now(e),
            Kind::Ev(EV_E_CAST) => self.cast_e_now(e),
            Kind::Ev(EV_R_CAST) => self.cast_r_now(e),
            Kind::Ev(EV_R_IMPACT) => {
                self.s.r_impact_at = INF;
                let cur_hp = pymax(e.st.hp, 0.0);
                let max_hp = e.target_hp;
                let missing_pct = (max_hp - cur_hp) / max_hp * 100.0;
                let mult = if cur_hp < self.r_low_thresh * max_hp {
                    self.r_low_mult
                } else {
                    let amp = pymin(self.r_amp_coef * missing_pct, self.r_amp_cap);
                    1.0 + amp / 100.0
                };
                let dmg = self.r_dmg_base * mult;
                e.deal(dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
