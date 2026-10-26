//! Viego. Blade of the Ruined King rides every basic attack (a current-health
//! on-hit rider, plus a second strike whenever a pending mark is consumed);
//! Q's active thrust, W's blast, and R's arrival damage all apply or consume
//! that mark. Casts go one at a time: Q (0.25 s) and R (0.5 s) each hold a
//! single `busy_until` in state while they resolve; W and E have no cast time
//! of their own but still wait for one in progress. W is charged for the
//! minimum time (its damage does not scale with charge) and immediately
//! recast for its attack-timer reset. E is a pure attack-speed self-buff for
//! the duration of its mist. R deals its arrival damage after its cast time,
//! applies Blade of the Ruined King's on-hit effects, and forces a follow-up
//! basic attack.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// R's arrival damage (delayed by its cast time); its (re)cast.
const EV_R_LAND: u8 = 0;
const EV_R_CAST: u8 = 1;
/// W's charge+recast, treated as instantaneous.
const EV_W: u8 = 2;
/// E's cast, on cooldown.
const EV_E: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    q_cd: f64,
    q_cast_s: f64,
    q_active_dmg: f64,
    q_active_critcoef: f64,
    q_onhit_pct: f64,
    q_onhit_min: f64,
    q_onhit_critcoef: f64,
    mark_duration: f64,
    q_mark_ad_ratio: f64,
    q_mark_ap_ratio: f64,

    w_cd: f64,
    w_dmg: f64,

    e_cd: f64,
    e_as_pct: f64,
    e_mist_dur: f64,

    r_cd: f64,
    r_cast_s: f64,
    r_missing_pct: f64,
    r_bonus_ad_coef: f64,
    r_swing_dmg: f64,
    r_critcoef: f64,

    src_q_onhit: SourceId,
    src_q_mark: SourceId,
    src_r_missing: SourceId,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    /// When a pending mark (from Q's active or W) expires; consumed by the
    /// next on-hit application. -INF: none pending.
    mark_until: f64,
    w_ready: f64,
    e_ready: f64,
    /// When the current Harrowed Path buff ends; -INF: not active.
    e_buff_until: f64,
    /// R's own cooldown-ready time (set once fired).
    r_ready: f64,
    /// A pending R cast still to land; INF: none pending.
    r_land_at: f64,
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

    /// Blade of the Ruined King's on-hit application: the percent-health
    /// rider (with its own partial crit term and a floor), and, if a mark is
    /// pending, the second strike that consumes it (a full, independently
    /// rolled crit).
    fn apply_q_onhit(&mut self, e: &mut Engine) {
        let cur_hp = pymax(e.st.hp, 0.0);
        let raw = self.q_onhit_pct * cur_hp;
        let base = pymax(raw, self.q_onhit_min);
        let crit_chance = e.p.sheet.crit_chance / 100.0;
        let crit_bonus = e.p.sheet.crit_damage / 100.0 - 1.0;
        let mult = 1.0 + self.q_onhit_critcoef * crit_chance * crit_bonus;
        e.deal(base * mult, DType::Physical, self.src_q_onhit, false, false, 1.0);

        if e.st.t < self.s.mark_until {
            self.s.mark_until = -INF;
            let amt = self.q_mark_ad_ratio * e.p.ad + self.q_mark_ap_ratio * e.p.sheet.ap;
            e.deal(amt, DType::Physical, self.src_q_mark, true, false, 1.0);
        }
    }

    fn fire_w(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.w_ready = t + e.basic_cd(self.w_cd);
        e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
        self.s.mark_until = t + self.mark_duration;
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        let b = self.bonus_as(t);
        e.st.next_attack = pymax(e.st.next_attack, t + e.attack_windup(b, self.windup_fraction));
    }

    fn fire_e(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.e_buff_until = t + self.e_mist_dur;
        self.s.e_ready = self.s.e_buff_until + e.basic_cd(self.e_cd);
        e.prime_spellblade();
    }

    /// Heartbreaker's blink: a real 0.25 s (rank-independent) cast time keeps
    /// Viego busy; the damage lands via a separate delayed event.
    fn fire_r(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        self.s.r_land_at = t + self.r_cast_s;
        e.prime_spellblade();
        self.busy_for(e, self.r_cast_s);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            busy_until: 0.0,
            mark_until: -INF,
            w_ready: 0.0,
            e_ready: 0.0,
            e_buff_until: -INF,
            r_ready: INF,
            r_land_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("viego kit needs attack.windupFraction")?,

            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            q_active_dmg: kit.hit("gen.Q.active.damage", ranks.q, sheet)?,
            q_active_critcoef: kit.num("gen.Q.active.critCoef")?,
            q_onhit_pct: kit.at_rank("gen.Q.onhit.pctPercent", ranks.q)? / 100.0,
            q_onhit_min: kit.at_rank("gen.Q.onhit.min", ranks.q)?,
            q_onhit_critcoef: kit.num("gen.Q.onhit.critCoef")?,
            mark_duration: kit.num("gen.Q.mark.durationS")?,
            q_mark_ad_ratio: kit.num("gen.Q.mark.adRatio")?,
            q_mark_ap_ratio: kit.num("gen.Q.mark.apRatio")?,

            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,

            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_as_pct: kit.at_rank("gen.E.asPct", ranks.e)?,
            e_mist_dur: kit.num("gen.E.mistDurationS")?,

            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_missing_pct: kit.at_rank("gen.R.missingHpPct", ranks.r)? / 100.0,
            r_bonus_ad_coef: kit.num("gen.R.bonusAdCoefPer100")?,
            r_swing_dmg: kit.hit("gen.R.swing.damage", ranks.r, sheet)?,
            r_critcoef: kit.num("gen.R.critCoef")?,

            src_q_onhit: intern("Q onhit"),
            src_q_mark: intern("Q mark"),
            src_r_missing: intern("R missing"),

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
        if t < self.s.e_buff_until {
            self.e_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        self.apply_q_onhit(e);
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
        let crit_chance = e.p.sheet.crit_chance / 100.0;
        let crit_bonus = e.p.sheet.crit_damage / 100.0 - 1.0;
        let dmg = self.q_active_dmg * (1.0 + self.q_active_critcoef * crit_chance * crit_bonus);
        e.deal(dmg, DType::Physical, SRC_Q, false, true, 1.0);
        self.s.mark_until = t + self.mark_duration;
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r > 0 {
            self.fire_r(e);
        }
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.r_land_at != INF {
            out[n] = (self.s.r_land_at, Kind::Ev(EV_R_LAND));
            n += 1;
        } else if self.ranks.r > 0 {
            out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R_CAST));
            n += 1;
        }
        if self.ranks.w > 0 {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_LAND) => {
                self.s.r_land_at = INF;
                let crit_chance = e.p.sheet.crit_chance / 100.0;
                let crit_bonus = e.p.sheet.crit_damage / 100.0 - 1.0;
                let swing = self.r_swing_dmg * (1.0 + self.r_critcoef * crit_chance * crit_bonus);
                e.deal(swing, DType::Physical, SRC_R, false, true, 1.0);

                let bonus_ad_term = self.r_bonus_ad_coef * (e.p.sheet.ad_bonus / 100.0);
                let missing_pct = self.r_missing_pct + bonus_ad_term;
                let missing_hp = e.target_hp - pymax(e.st.hp, 0.0);
                let missing_dmg = missing_pct * missing_hp;
                e.deal(missing_dmg, DType::Physical, self.src_r_missing, false, true, 1.0);

                self.apply_q_onhit(e);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();

                let b = self.bonus_as(t);
                e.st.next_attack = pymax(e.st.next_attack, t + e.attack_windup(b, self.windup_fraction));
            }
            Kind::Ev(EV_R_CAST) => {
                self.fire_r(e);
            }
            Kind::Ev(EV_W) => {
                self.fire_w(e);
            }
            Kind::Ev(EV_E) => {
                self.fire_e(e);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
