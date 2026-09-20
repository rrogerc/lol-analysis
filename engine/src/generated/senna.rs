//! Senna. An auto-attacker/ability hybrid: Absolution marks enemies on any
//! attack or ability hit, a following hit consumes the mark for
//! current-health-percent bonus damage and a permanent Mist stack (bonus AD),
//! Relic Cannon adds on-hit damage to attacks and Piercing Darkness, and
//! Piercing Darkness's cooldown is refunded on-attack. Dawning Shadow opens
//! the fight; Piercing Darkness and Last Embrace go out on cooldown.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Last Embrace is cast, and Dawning Shadow's damage lands after its cast.
const EV_W: u8 = 0;
const EV_R: u8 = 1;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Mist: bonus AD per stack, and Relic Cannon's on-hit ratio of AD.
    p_ad_per_stack: f64,
    p_relic_ratio: f64,
    /// Mark duration, the level-scaled consume damage ratio (of current HP)
    /// and the level-scaled re-mark immunity duration.
    p_mark_dur: f64,
    p_mark_pct: f64,
    p_immune_dur: f64,
    q_base: f64,
    q_bad_ratio: f64,
    q_cd: f64,
    q_cdr_onhit: f64,
    w_base: f64,
    w_bad_ratio: f64,
    w_cd: f64,
    r_base: f64,
    r_ap_ratio: f64,
    r_bad_ratio: f64,
    r_cast_s: f64,
    src_p_mark: SourceId,
    src_p_onhit: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    mist_stacks: i64,
    mark_active: bool,
    mark_expire: f64,
    immune_until: f64,
    w_ready: f64,
    /// When Dawning Shadow's damage lands (INF: none pending).
    r_land_at: f64,
}

impl GenDriver {
    /// The bonus AD Mist stacks are currently granting, on top of the
    /// build's own bonus AD.
    fn bonus_ad_now(&self, e: &Engine) -> f64 {
        e.p.sheet.ad_bonus + self.s.mist_stacks as f64 * self.p_ad_per_stack
    }

    /// The Mist mark cycle: mark an unmarked, non-immune target, or consume
    /// an existing mark for its current-health bonus damage and a stack.
    fn mark_hit(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.mark_active && t > self.s.mark_expire {
            self.s.mark_active = false;
        }
        if t < self.s.immune_until {
            return;
        }
        if self.s.mark_active {
            self.s.mark_active = false;
            self.s.immune_until = t + self.p_immune_dur;
            self.s.mist_stacks += 1;
            let hp = pymax(e.st.hp, 0.0);
            let dmg = self.p_mark_pct * hp;
            e.deal(dmg, DType::Physical, self.src_p_mark, false, false, 1.0);
        } else {
            self.s.mark_active = true;
            self.s.mark_expire = t + self.p_mark_dur;
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            mist_stacks: 0,
            mark_active: false,
            mark_expire: 0.0,
            immune_until: 0.0,
            w_ready: 0.0,
            r_land_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("senna kit needs attack.windupFraction")?,
            p_ad_per_stack: kit.num("gen.P.adPerStack")?,
            p_relic_ratio: kit.num("gen.P.relicRatio")?,
            p_mark_dur: kit.num("gen.P.markDurationS")?,
            p_mark_pct: kit.at_level("gen.P.markDamagePctByLevel", level)? / 100.0,
            p_immune_dur: kit.at_level("gen.P.markImmunityByLevel", level)?,
            q_base: kit.at_rank("gen.Q.damage.base", ranks.q)?,
            q_bad_ratio: kit.num("gen.Q.damage.bonusAdRatio")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cdr_onhit: kit.num("gen.Q.cdReductionOnHitS")?,
            w_base: kit.at_rank("gen.W.damage.base", ranks.w)?,
            w_bad_ratio: kit.num("gen.W.damage.bonusAdRatio")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            r_base: kit.at_rank("gen.R.damage.base", ranks.r)?,
            r_ap_ratio: kit.num("gen.R.damage.apRatio")?,
            r_bad_ratio: kit.num("gen.R.damage.bonusAdRatio")?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            src_p_mark: intern("P mark"),
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
        e.p.ad + self.s.mist_stacks as f64 * self.p_ad_per_stack
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        // Relic Cannon: on-hit bonus physical damage
        let relic = self.p_relic_ratio * self.attack_damage(e);
        e.deal(relic, DType::Physical, self.src_p_onhit, false, false, 1.0);
        // the Mist mark cycle
        self.mark_hit(e);
        // Piercing Darkness's cooldown is reduced on-attack
        e.st.q_ready -= self.q_cdr_onhit;
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
        let bad = self.bonus_ad_now(e);
        let dmg = self.q_base + self.q_bad_ratio * bad;
        e.deal(dmg, DType::Physical, SRC_Q, false, true, 1.0);
        // Piercing Darkness also applies on-hit effects against the champion hit
        let relic = self.p_relic_ratio * self.attack_damage(e);
        e.deal(relic, DType::Physical, self.src_p_onhit, false, false, 1.0);
        self.mark_hit(e);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the engine has primed Spellblade and delayed the
        // first attack; Dawning Shadow's 1s cast time is longer, so extend it
        let t = e.st.t;
        self.s.r_land_at = t + self.r_cast_s;
        e.st.next_attack = pymax(e.st.next_attack, t + self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W));
            n += 1;
        }
        if self.s.r_land_at != INF {
            out[n] = (self.s.r_land_at, Kind::Ev(EV_R));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                let bad = self.bonus_ad_now(e);
                let dmg = self.w_base + self.w_bad_ratio * bad;
                e.deal(dmg, DType::Physical, SRC_W, false, true, 1.0);
                self.mark_hit(e);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_R) => {
                self.s.r_land_at = INF;
                let bad = self.bonus_ad_now(e);
                let dmg = self.r_base + self.r_ap_ratio * e.p.sheet.ap + self.r_bad_ratio * bad;
                e.deal(dmg, DType::Physical, SRC_R, false, true, 1.0);
                self.mark_hit(e);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
