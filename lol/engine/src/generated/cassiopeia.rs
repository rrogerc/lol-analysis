//! Cassiopeia. A pure caster: Noxious Blast, Miasma and Twin Fang are cast
//! on cooldown (through a shared casting lock so two casts never land at
//! once), Twin Fang's bonus damage applies while the target is poisoned by
//! either DoT, Petrifying Gaze opens the fight, and a non-regenerating mana
//! pool eventually stops Twin Fang's spam.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Noxious Blast: the blast exploding (after its delay), then its ticks.
const EV_Q_EXPLODE: u8 = 0;
const EV_Q_TICK: u8 = 1;
/// Miasma: the cast landing, then its ticks.
const EV_W_CAST: u8 = 2;
const EV_W_TICK: u8 = 3;
/// Twin Fang, cast on cooldown.
const EV_E_CAST: u8 = 4;
/// Petrifying Gaze's damage, landing at the end of its cast.
const EV_R_DAMAGE: u8 = 5;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,

    q_cd: f64,
    q_cost: f64,
    q_cast_s: f64,
    q_explode_delay_s: f64,
    q_poison_dur_s: f64,
    q_num_ticks: f64,
    q_tick_interval: f64,
    q_tick_dmg: f64,

    w_cd: f64,
    w_cost: f64,
    w_cast_s: f64,
    w_cloud_dur_s: f64,
    w_poison_grace_s: f64,
    w_ticks_nominal: f64,
    w_ticks_real: i64,
    w_tick_interval: f64,
    w_tick_dmg: f64,

    e_cd: f64,
    e_cost: f64,
    e_cast_s: f64,
    e_basic_dmg: f64,
    e_bonus_dmg: f64,

    r_cost: f64,
    r_cast_s: f64,
    r_dmg: f64,

    src_e_basic: SourceId,
    src_e_bonus: SourceId,

    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// A single non-regenerating mana pool.
    mana: f64,
    /// Shared casting lock: no new cast may start before this time.
    busy_until: f64,

    q_explode_at: f64,
    q_tick_next: f64,
    q_ticks_done: i64,

    w_ready: f64,
    w_tick_next: f64,
    w_ticks_done: i64,

    e_ready: f64,

    r_damage_at: f64,

    /// The target counts as poisoned (for Twin Fang's bonus) until this time.
    poisoned_until: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let q_poison_dur_s = kit.num("gen.Q.poisonDurationS")?;
        let q_num_ticks = kit.num("gen.Q.numTicks")?;
        let q_total = kit.hit("gen.Q.damage", ranks.q, sheet)?;

        let w_cloud_dur_s = kit.num("gen.W.cloudDurationS")?;
        let w_ticks_nominal = kit.num("gen.W.ticksNominal")?;
        let w_per_second = kit.hit("gen.W.damage", ranks.w, sheet)?;
        let w_tick_interval = w_cloud_dur_s / w_ticks_nominal;

        let e_basic_by_level = kit.at_level("gen.E.basicByLevel", level)?;
        let e_basic_ap_ratio = kit.num("gen.E.basicApRatio")?;
        let e_basic_dmg = if ranks.e > 0 { e_basic_by_level + e_basic_ap_ratio * sheet.ap } else { 0.0 };

        let state = State {
            // mana is not modelled: nothing spends, so this never falls
            mana: sheet.mana,
            busy_until: 0.0,
            q_explode_at: INF,
            q_tick_next: INF,
            q_ticks_done: 0,
            w_ready: 0.0,
            w_tick_next: INF,
            w_ticks_done: 0,
            e_ready: 0.0,
            r_damage_at: INF,
            poisoned_until: 0.0,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,

            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cost: kit.at_rank("gen.Q.costMana", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            q_explode_delay_s: kit.num("gen.Q.explodeDelayS")?,
            q_poison_dur_s,
            q_num_ticks,
            q_tick_interval: q_poison_dur_s / q_num_ticks,
            q_tick_dmg: q_total / q_num_ticks,

            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cost: kit.at_rank("gen.W.costMana", ranks.w)?,
            w_cast_s: kit.num("gen.W.castTimeS")?,
            w_cloud_dur_s,
            w_poison_grace_s: kit.num("gen.W.poisonGraceS")?,
            w_ticks_nominal,
            w_ticks_real: kit.num("gen.W.ticksReal")? as i64,
            w_tick_interval,
            w_tick_dmg: w_per_second * w_tick_interval,

            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cost: kit.at_rank("gen.E.costMana", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            e_basic_dmg,
            e_bonus_dmg: kit.hit("gen.E.bonusDamage", ranks.e, sheet)?,

            r_cost: kit.at_rank("gen.R.costMana", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,

            src_e_basic: intern("E basic"),
            src_e_bonus: intern("E bonus"),

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

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if self.s.mana < self.q_cost {
            return INF;
        }
        pymax(pymax(e.st.q_ready, e.st.t), self.s.busy_until)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        self.s.busy_until = t + self.q_cast_s;
        e.prime_spellblade();
        self.s.q_explode_at = t + self.q_explode_delay_s;
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        if self.s.mana < self.r_cost {
            return;
        }
        let t = e.st.t;
        self.s.busy_until = t + self.r_cast_s;
        self.s.r_damage_at = t + self.r_cast_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;

        if self.s.q_explode_at != INF {
            out[n] = (self.s.q_explode_at, Kind::Ev(EV_Q_EXPLODE));
            n += 1;
        } else if self.s.q_tick_next != INF {
            out[n] = (self.s.q_tick_next, Kind::Ev(EV_Q_TICK));
            n += 1;
        }

        if self.ranks.w > 0 {
            if self.s.w_tick_next != INF {
                out[n] = (self.s.w_tick_next, Kind::Ev(EV_W_TICK));
                n += 1;
            } else if self.s.mana >= self.w_cost {
                out[n] = (pymax(pymax(self.s.w_ready, e.st.t), self.s.busy_until), Kind::Ev(EV_W_CAST));
                n += 1;
            }
        }

        if self.ranks.e > 0 && self.s.mana >= self.e_cost {
            out[n] = (pymax(pymax(self.s.e_ready, e.st.t), self.s.busy_until), Kind::Ev(EV_E_CAST));
            n += 1;
        }

        if self.s.r_damage_at != INF {
            out[n] = (self.s.r_damage_at, Kind::Ev(EV_R_DAMAGE));
            n += 1;
        }

        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_EXPLODE) => {
                self.s.q_explode_at = INF;
                self.s.poisoned_until = pymax(self.s.poisoned_until, t + self.q_poison_dur_s);
                self.s.q_ticks_done = 0;
                self.s.q_tick_next = t + self.q_tick_interval;
            }
            Kind::Ev(EV_Q_TICK) => {
                self.s.q_ticks_done += 1;
                e.deal(self.q_tick_dmg, DType::Magic, SRC_Q, false, true, 1.0);
                if self.s.q_ticks_done == 1 {
                    e.ability_cast_proc();
                    e.eclipse_hit();
                }
                if (self.s.q_ticks_done as f64) < self.q_num_ticks {
                    self.s.q_tick_next = t + self.q_tick_interval;
                } else {
                    self.s.q_tick_next = INF;
                }
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.busy_until = t + self.w_cast_s;
                e.prime_spellblade();
                self.s.poisoned_until = pymax(self.s.poisoned_until, t + self.w_cloud_dur_s + self.w_poison_grace_s);
                self.s.w_ticks_done = 0;
                self.s.w_tick_next = t + self.w_tick_interval;
            }
            Kind::Ev(EV_W_TICK) => {
                self.s.w_ticks_done += 1;
                e.deal(self.w_tick_dmg, DType::Magic, SRC_W, false, true, 1.0);
                if self.s.w_ticks_done == 1 {
                    e.ability_cast_proc();
                    e.eclipse_hit();
                }
                if self.s.w_ticks_done < self.w_ticks_real {
                    self.s.w_tick_next = t + self.w_tick_interval;
                } else {
                    self.s.w_tick_next = INF;
                }
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.busy_until = t + self.e_cast_s;
                let poisoned = t < self.s.poisoned_until;
                e.deal(self.e_basic_dmg, DType::Magic, self.src_e_basic, false, true, 1.0);
                if poisoned {
                    e.deal(self.e_bonus_dmg, DType::Magic, self.src_e_bonus, false, true, 1.0);
                }
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_DAMAGE) => {
                self.s.r_damage_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
