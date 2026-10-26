//! Urgot. Opens with Fear Beyond Death's initial hit, then cycles Corrosive
//! Charge and Disdain on cooldown while keeping Purge toggled on whenever it
//! is off cooldown; Purge overrides his attack timer with a fixed-rate burst
//! of shots, and Echoing Flames rides whichever basic attack (a normal swing
//! or a Purge shot) lands while its single modeled leg charge is up. Casts
//! go one at a time: Q, E and R each keep Urgot busy for their cast time, and
//! nothing else (cast or attack) starts inside it.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Corrosive Charge's delayed explosion.
const EV_Q_EXPLODE: u8 = 0;
/// Disdain, cast on cooldown.
const EV_E: u8 = 1;
/// Purge: starting an activation, or its next shot while one is running.
const EV_W: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    /// Echoing Flames: AD ratio, target-max-HP ratio, and the per-leg cooldown.
    p_ad_ratio: f64,
    p_hp_ratio: f64,
    p_leg_cd: f64,
    q_dmg: f64,
    q_cd: f64,
    q_delay: f64,
    q_cast_s: f64,
    w_dmg: f64,
    w_cd: f64,
    w_duration: f64,
    w_period: f64,
    w_effectiveness: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cast_s: f64,
    r_dmg: f64,
    r_cast_s: f64,
    src_p: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    /// Corrosive Charge's pending explosion (INF: none pending).
    q_explode_at: f64,
    e_ready: f64,
    /// Purge: whether an activation is running, when it started/ends, the
    /// next shot's time, and the cooldown ready time (used only when idle).
    w_active: bool,
    w_start: f64,
    w_end_at: f64,
    w_next_shot: f64,
    w_ready: f64,
    /// Echoing Flames' single modeled leg charge.
    leg_ready: f64,
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

    /// Echoing Flames: fires if the leg charge is up, at the given
    /// effectiveness (full on a normal attack, halved during Purge).
    fn try_echo(&mut self, e: &mut Engine, effectiveness: f64) {
        let t = e.st.t;
        if t >= self.s.leg_ready {
            let dmg = (self.p_ad_ratio * e.p.ad + self.p_hp_ratio * e.target_hp) * effectiveness;
            e.deal(dmg, DType::Physical, self.src_p, false, false, 1.0);
            self.s.leg_ready = t + self.p_leg_cd;
        }
    }

    /// One Purge shot: its own damage, plus a chance for Echoing Flames.
    fn fire_purge_shot(&mut self, e: &mut Engine) {
        e.deal(self.w_dmg, DType::Physical, SRC_W, false, false, 1.0);
        self.try_echo(e, self.w_effectiveness);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            busy_until: 0.0,
            q_explode_at: INF,
            e_ready: 0.0,
            w_active: false,
            w_start: 0.0,
            w_end_at: INF,
            w_next_shot: INF,
            w_ready: 0.0,
            leg_ready: 0.0,
        };
        let w_rate = kit.num("gen.W.attacksPerSecond")?;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            p_ad_ratio: kit.at_level("gen.P.adRatioByLevel", level)?,
            p_hp_ratio: kit.at_level("gen.P.hpRatioByLevel", level)?,
            p_leg_cd: kit.at_level("gen.P.perLegCdByLevel", level)?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_delay: kit.num("gen.Q.explodeDelayS")?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_duration: kit.at_rank("gen.W.durationS", ranks.w)?,
            w_period: 1.0 / w_rate,
            w_effectiveness: kit.num("gen.W.onHitEffectiveness")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            src_p: intern("P"),
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
        shave(&mut self.s.e_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        // a normal basic attack: Echoing Flames at full effectiveness
        self.try_echo(e, 1.0);
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
        self.s.q_explode_at = t + self.q_delay;
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        e.deal(self.r_dmg, DType::Physical, SRC_R, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_explode_at != INF {
            out[n] = (self.s.q_explode_at, Kind::Ev(EV_Q_EXPLODE));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E));
            n += 1;
        }
        if self.ranks.w > 0 {
            let due = if self.s.w_active {
                self.s.w_next_shot
            } else {
                self.castable_at(e, self.s.w_ready)
            };
            out[n] = (due, Kind::Ev(EV_W));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_EXPLODE) => {
                self.s.q_explode_at = INF;
                e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_E) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Physical, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_W) => {
                if !self.s.w_active {
                    // start a fresh activation; hold normal attacks until it ends
                    self.s.w_active = true;
                    self.s.w_start = t;
                    self.s.w_end_at = t + self.w_duration;
                    e.st.next_attack = pymax(e.st.next_attack, self.s.w_end_at);
                    e.prime_spellblade();
                    e.ability_cast_proc();
                    e.eclipse_hit();
                    self.fire_purge_shot(e);
                    let next = t + self.w_period;
                    if next >= self.s.w_end_at {
                        self.s.w_active = false;
                        self.s.w_ready = self.s.w_end_at + e.basic_cd(self.w_cd);
                        self.s.w_end_at = INF;
                        self.s.w_next_shot = INF;
                    } else {
                        self.s.w_next_shot = next;
                    }
                } else {
                    self.fire_purge_shot(e);
                    let next = self.s.w_next_shot + self.w_period;
                    if next >= self.s.w_end_at {
                        self.s.w_active = false;
                        self.s.w_ready = self.s.w_end_at + e.basic_cd(self.w_cd);
                        self.s.w_end_at = INF;
                        self.s.w_next_shot = INF;
                    } else {
                        self.s.w_next_shot = next;
                    }
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
