//! Qiyana. A melee bruiser who opens by consuming her starting Rock element
//! with Elemental Wrath, casts Audacity and Supreme Display of Talent on
//! cooldown (its shockwave assumed to always find terrain), and otherwise
//! autoattacks; Royal Privilege rides attacks and Q/E hits on an internal
//! cooldown, and Terrashape's passive attack speed / on-hit magic damage
//! only exist until the starting element is spent. Casts go one at a time:
//! Q, E's landing (no cast time but still gated) and R's cast each keep her
//! busy for their cast time before the next cast or attack.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Audacity is cast, deals its damage and resets Qiyana's attack timer.
const EV_E: u8 = 0;
/// Supreme Display of Talent: the cast (after the opening one) and its
/// shockwave landing after the cast time.
const EV_R_CAST: u8 = 1;
const EV_R_SWING: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    e_dmg: f64,
    e_cd: f64,
    p_dmg: f64,
    p_icd: f64,
    w_onhit_dmg: f64,
    w_as_pct: f64,
    r_dmg: f64,
    r_target_hp_ratio: f64,
    r_cd: f64,
    r_cast_s: f64,
    src_p: SourceId,
    src_w: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    e_ready: f64,
    /// Royal Privilege's per-target internal cooldown: the next time it may
    /// proc again.
    p_ready: f64,
    /// When the starting Rock element was consumed by Elemental Wrath
    /// (INF: still held). Terrashape's passive attack speed and on-hit
    /// magic damage only apply while this is in the future.
    element_consumed_at: f64,
    /// Supreme Display of Talent: when the pending shockwave lands (INF:
    /// none pending), and when the next cast may start (INF: parked until
    /// the pending swing lands).
    r_swing_at: f64,
    r_ready: f64,
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

    /// Royal Privilege: a separate physical on-hit instance, gated by its
    /// per-target internal cooldown.
    fn apply_p(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if t >= self.s.p_ready {
            e.deal(self.p_dmg, DType::Physical, self.src_p, false, false, 1.0);
            self.s.p_ready = t + self.p_icd;
        }
    }

    /// Terrashape's passive bonus magic damage, while the element is held.
    fn apply_w_onhit(&self, e: &mut Engine) {
        e.deal(self.w_onhit_dmg, DType::Magic, self.src_w, false, false, 1.0);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            busy_until: 0.0,
            e_ready: 0.0,
            p_ready: 0.0,
            element_consumed_at: INF,
            r_swing_at: INF,
            r_ready: INF,
        };
        let p_base = kit.at_level("gen.P.damageBase", level)?;
        let p_ad_ratio = kit.num("gen.P.bonusAdRatio")?;
        let p_ap_ratio = kit.num("gen.P.apRatio")?;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("qiyana kit needs attack.windupFraction")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            p_dmg: p_base + p_ad_ratio * sheet.ad_bonus + p_ap_ratio * sheet.ap,
            p_icd: kit.num("gen.P.icdS")?,
            w_onhit_dmg: kit.hit("gen.W.onhit", ranks.w, sheet)?,
            w_as_pct: kit.at_rank("gen.W.attackSpeedPct", ranks.w)?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_target_hp_ratio: kit.num("gen.R.targetMaxHpRatio")?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            src_p: intern("P"),
            src_w: intern("W onhit"),
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
        if t < self.s.element_consumed_at {
            self.w_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if t < self.s.element_consumed_at {
            self.apply_w_onhit(e);
        }
        self.apply_p(e);
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
        // still holding the starting Rock element if it has not yet been
        // spent: this cast consumes it
        let holding = self.s.element_consumed_at == INF;
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        if holding {
            self.apply_w_onhit(e);
        }
        self.apply_p(e);
        if holding {
            self.s.element_consumed_at = t;
        }
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the engine has already primed Spellblade and
        // held the first attack past the cast; the shockwave lands when the
        // cast time ends
        self.s.r_swing_at = e.st.t + self.r_cast_s;
        self.busy_for(e, self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E));
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_swing_at != INF {
                out[n] = (self.s.r_swing_at, Kind::Ev(EV_R_SWING));
                n += 1;
            } else if self.s.r_ready != INF {
                out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R_CAST));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E) => {
                // no cast time: it lands with the cast and keeps nothing
                // else waiting, but it still cannot start inside another cast
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Physical, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                if t < self.s.element_consumed_at {
                    self.apply_w_onhit(e);
                }
                self.apply_p(e);
                // Qiyana is auto-ordered to attack the target as soon as the
                // dash completes: an attack reset
                let b = self.bonus_as(t);
                e.st.next_attack =
                    pymin(e.st.next_attack, t + e.attack_windup(b, self.windup_fraction));
                e.lockout();
            }
            Kind::Ev(EV_R_CAST) => {
                // a recast: unlike the opening one, the engine has not
                // already handled its cast lockout / Spellblade priming
                self.s.r_ready = INF;
                self.s.r_swing_at = t + self.r_cast_s;
                e.prime_spellblade();
                self.busy_for(e, self.r_cast_s);
            }
            Kind::Ev(EV_R_SWING) => {
                self.s.r_swing_at = INF;
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                let amt = self.r_dmg + self.r_target_hp_ratio * e.target_hp;
                e.deal(amt, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
