//! Zed. Death Mark opens the fight (0.6s delay + 0.35s dash before the mark
//! applies, then a 3s stored-damage detonation), spawning a Shadow that
//! mimics every Razor Shuriken cast; Living Shadow is kept up on cooldown
//! for a second mimicking Shadow, Shadow Slash goes out on cooldown for its
//! single damage instance (and clips Living Shadow's remaining cooldown),
//! and Contempt for the Weak rides basic attacks once the dummy is below
//! half health. Casts go one at a time: Razor Shuriken has a 0.25s cast
//! time that keeps Zed busy, and the Death Mark dash likewise holds every
//! other cast and attack until it ends.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Death Mark applies (the dash ends) and, later, detonates.
const EV_R_DASH_END: u8 = 0;
const EV_R_DETONATE: u8 = 1;
/// Living Shadow is cast; Shadow Slash is cast.
const EV_W_CAST: u8 = 2;
const EV_E_CAST: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    p_pct: f64,
    p_threshold: f64,
    p_icd: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    w_cd: f64,
    w_shadow_dur: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cdr_w: f64,
    r_ad_ratio: f64,
    r_stored_ratio: f64,
    r_dash_delay: f64,
    r_dash_travel: f64,
    r_mark_dur: f64,
    r_shadow_dur: f64,
    src_p: SourceId,
    src_q_mimic: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    w_ready: f64,
    w_shadow_until: f64,
    e_ready: f64,
    /// Time the Death Mark dash finishes (INF once fired).
    r_dash_end: f64,
    r_shadow_until: f64,
    /// Pending detonation time (INF when no mark is active).
    r_mark_end: f64,
    r_stored: f64,
    p_ready: f64,
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

    fn add_stored(&mut self, e: &Engine, amt: f64) {
        if self.s.r_mark_end != INF && e.st.t <= self.s.r_mark_end {
            self.s.r_stored += amt;
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            busy_until: 0.0,
            w_ready: 0.0,
            w_shadow_until: 0.0,
            e_ready: 0.0,
            r_dash_end: INF,
            r_shadow_until: 0.0,
            r_mark_end: INF,
            r_stored: 0.0,
            p_ready: 0.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            p_pct: kit.at_level("gen.P.pctByLevel", level)?,
            p_threshold: kit.num("gen.P.thresholdPct")?,
            p_icd: kit.num("gen.P.icdS")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_shadow_dur: kit.num("gen.W.shadowDurationS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cdr_w: kit.num("gen.E.shadowHitCdrS")?,
            r_ad_ratio: kit.num("gen.R.adRatio")?,
            r_stored_ratio: kit.at_rank("gen.R.storedDamageRatio", ranks.r)?,
            r_dash_delay: kit.num("gen.R.dashDelayS")?,
            r_dash_travel: kit.num("gen.R.dashTravelS")?,
            r_mark_dur: kit.num("gen.R.markDurationS")?,
            r_shadow_dur: kit.num("gen.R.shadowDurationS")?,
            src_p: intern("P"),
            src_q_mimic: intern("Q mimic"),
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

    fn attack_riders(&mut self, e: &mut Engine) {
        // Contempt for the Weak: bonus magic on-hit below 50% HP, 10s ICD
        let t = e.st.t;
        if self.ranks.q >= 0
            && t >= self.s.p_ready
            && pymax(e.st.hp, 0.0) < self.p_threshold * e.target_hp
        {
            self.s.p_ready = t + self.p_icd;
            let amt = self.p_pct * e.target_hp;
            e.deal(amt, DType::Magic, self.src_p, false, false, 1.0);
            self.add_stored(e, amt);
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
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        self.add_stored(e, self.q_dmg);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        if t < self.s.w_shadow_until {
            e.deal(self.q_dmg, DType::Physical, self.src_q_mimic, false, true, 1.0);
            self.add_stored(e, self.q_dmg);
        }
        if t < self.s.r_shadow_until {
            e.deal(self.q_dmg, DType::Physical, self.src_q_mimic, false, true, 1.0);
            self.add_stored(e, self.q_dmg);
        }
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        let dash_end = t + self.r_dash_delay + self.r_dash_travel;
        self.s.r_dash_end = dash_end;
        self.s.r_shadow_until = t + self.r_shadow_dur;
        // no cast time key here (dash timing is modeled with an event), but
        // the dash still keeps Zed from casting or attacking until it ends
        self.s.busy_until = dash_end;
        e.st.next_attack = pymax(e.st.next_attack, dash_end);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.r_dash_end != INF {
            out[n] = (self.s.r_dash_end, Kind::Ev(EV_R_DASH_END));
            n += 1;
        }
        if self.s.r_mark_end != INF {
            out[n] = (self.s.r_mark_end, Kind::Ev(EV_R_DETONATE));
            n += 1;
        }
        if self.ranks.w > 0 {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_DASH_END) => {
                self.s.r_dash_end = INF;
                self.s.r_mark_end = t + self.r_mark_dur;
                self.s.r_stored = 0.0;
            }
            Kind::Ev(EV_R_DETONATE) => {
                let dmg = e.p.ad * self.r_ad_ratio + self.r_stored_ratio * self.s.r_stored;
                e.deal(dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                self.s.r_mark_end = INF;
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.w_shadow_until = t + self.w_shadow_dur;
                e.prime_spellblade();
                e.ability_cast_proc();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Physical, SRC_E, false, true, 1.0);
                self.add_stored(e, self.e_dmg);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                if t < self.s.w_ready {
                    self.s.w_ready = pymax(t, self.s.w_ready - self.e_cdr_w);
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
