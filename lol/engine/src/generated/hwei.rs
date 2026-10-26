//! Hwei. Three "mood" slots (Q/W/E) each pick one of three sub-spells on
//! cast; here Q always resolves as Severing Bolt, E as Grim Visage, and W
//! as Stirring Lights (the only damaging / damage-relevant options against
//! a lone dummy). Spiraling Despair (R) opens the fight once. Signature of
//! the Visionary marks whatever Q/E/R last hit, and a hit from a different
//! one of those three consumes the mark for a delayed bonus explosion.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Severing Bolt's delayed landing.
const EV_Q_LAND: u8 = 0;
/// Grim Visage is cast, on the E slot's cooldown loop.
const EV_E_CAST: u8 = 1;
/// Stirring Lights is cast, on the W slot's cooldown loop.
const EV_W_CAST: u8 = 2;
/// Spiraling Despair's next DoT tick.
const EV_R_TICK: u8 = 3;
/// Spiraling Despair's final explosion.
const EV_R_EXPLODE: u8 = 4;
/// Signature of the Visionary's delayed explosion.
const EV_P_EXPLODE: u8 = 5;

/// Which of R / Q / E most recently marked the target (for the passive).
const GROUP_R: i64 = 0;
const GROUP_Q: i64 = 1;
const GROUP_E: i64 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,

    q_cd: f64,
    q_dmg: f64,
    q_mult: f64,
    q_delay: f64,

    e_cd: f64,
    e_dmg: f64,

    w_cd: f64,
    w_onhit_dmg: f64,
    w_duration: f64,
    w_charges: i64,

    r_cast_s: f64,
    r_duration: f64,
    r_tick_dmg: f64,
    r_tick_interval: f64,
    r_tick_max: i64,
    r_explosion_dmg: f64,

    p_dmg: f64,
    p_delay: f64,
    mark_duration: f64,

    src_q: SourceId,
    src_e: SourceId,
    src_r_tick: SourceId,
    src_w_onhit: SourceId,
    src_p: SourceId,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// When Severing Bolt's delayed damage lands (INF: none pending).
    q_land_at: f64,
    e_ready: f64,
    w_ready: f64,
    /// Spiraling Despair's DoT ticks (INF once the last one has fired).
    r_tick_next: f64,
    r_tick_count: i64,
    r_explode_at: f64,
    /// Signature of the Visionary's mark on the dummy.
    mark_active: bool,
    mark_group: i64,
    mark_expire: f64,
    /// A detonated mark's pending explosion (INF: none pending).
    p_pending_dmg: f64,
    p_pending_at: f64,
    /// Stirring Lights: hits remaining, and until when they still apply.
    stirring_charges: i64,
    stirring_until: f64,
}

impl GenDriver {
    /// A damaging Q/E/R hit against the target: if a different one of the
    /// three left a live mark, schedule its bonus explosion; then mark the
    /// target for this ability.
    fn try_mark_and_detonate(&mut self, e: &mut Engine, group: i64) {
        let t = e.st.t;
        if self.s.mark_active && t < self.s.mark_expire && self.s.mark_group != group {
            self.s.mark_active = false;
            if self.s.p_pending_at != INF {
                let dmg = self.s.p_pending_dmg;
                e.deal(dmg, DType::Magic, self.src_p, false, true, 1.0);
            }
            self.s.p_pending_dmg = self.p_dmg;
            self.s.p_pending_at = t + self.p_delay;
        }
        self.s.mark_active = true;
        self.s.mark_group = group;
        self.s.mark_expire = t + self.mark_duration;
    }

    /// Spend one Stirring Lights charge on a basic attack or Q/E hit.
    fn consume_stirring(&mut self, e: &mut Engine) {
        if self.s.stirring_charges > 0 && e.st.t < self.s.stirring_until {
            self.s.stirring_charges -= 1;
            e.deal(self.w_onhit_dmg, DType::Magic, self.src_w_onhit, false, false, 1.0);
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let r_tick_interval = kit.num("gen.R.tick.intervalS")?;
        let r_duration = kit.num("gen.R.durationS")?;
        let r_tick_max = (r_duration / r_tick_interval) as i64;

        let state = State {
            q_land_at: INF,
            e_ready: 0.0,
            w_ready: 0.0,
            r_tick_next: INF,
            r_tick_count: 0,
            r_explode_at: INF,
            mark_active: false,
            mark_group: 0,
            mark_expire: 0.0,
            p_pending_dmg: 0.0,
            p_pending_at: INF,
            stirring_charges: 0,
            stirring_until: 0.0,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,

            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_dmg: kit.hit("gen.Q.qw.damage", ranks.q, sheet)?,
            q_mult: kit.at_rank("gen.Q.qw.mult", ranks.q)?,
            q_delay: kit.num("gen.Q.qw.delayS")?,

            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_dmg: kit.hit("gen.E.eq.damage", ranks.e, sheet)?,

            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_onhit_dmg: kit.hit("gen.W.we.onhit", ranks.w, sheet)?,
            w_duration: kit.num("gen.W.we.durationS")?,
            w_charges: kit.num("gen.W.we.charges")? as i64,

            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_duration,
            r_tick_dmg: kit.hit("gen.R.tick.damage", ranks.r, sheet)?,
            r_tick_interval,
            r_tick_max,
            r_explosion_dmg: kit.hit("gen.R.explosion.damage", ranks.r, sheet)?,

            p_dmg: kit.at_level("gen.P.explosion.baseByLevel", level)?
                + kit.num("gen.P.explosion.apRatio")? * sheet.ap,
            p_delay: kit.num("gen.P.explosion.delayS")?,
            mark_duration: kit.num("gen.P.markDurationS")?,

            src_q: SRC_Q,
            src_e: SRC_E,
            src_r_tick: intern("R tick"),
            src_w_onhit: intern("W onhit"),
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
        self.consume_stirring(e);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // Severing Bolt: cooldown starts now, damage lands after its delay.
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        self.s.q_land_at = e.st.t + self.q_delay;
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // The opening cast: the engine has primed Spellblade and held the
        // first attack past the cast; the globule lands when the cast ends.
        let land = e.st.t + self.r_cast_s;
        self.s.r_tick_next = land;
        self.s.r_tick_count = 0;
        self.s.r_explode_at = land + self.r_duration;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_land_at != INF {
            out[n] = (self.s.q_land_at, Kind::Ev(EV_Q_LAND));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.s.r_tick_next != INF {
            out[n] = (self.s.r_tick_next, Kind::Ev(EV_R_TICK));
            n += 1;
        }
        if self.s.r_explode_at != INF {
            out[n] = (self.s.r_explode_at, Kind::Ev(EV_R_EXPLODE));
            n += 1;
        }
        if self.s.p_pending_at != INF {
            out[n] = (self.s.p_pending_at, Kind::Ev(EV_P_EXPLODE));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_LAND) => {
                self.s.q_land_at = INF;
                let missing_frac = (e.target_hp - pymax(e.st.hp, 0.0)) / e.target_hp;
                let mult = 1.0 + (self.q_mult - 1.0) * missing_frac;
                let dmg = self.q_dmg * mult;
                self.try_mark_and_detonate(e, GROUP_Q);
                e.deal(dmg, DType::Magic, self.src_q, false, true, 1.0);
                self.consume_stirring(e);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.try_mark_and_detonate(e, GROUP_E);
                e.deal(self.e_dmg, DType::Magic, self.src_e, false, true, 1.0);
                self.consume_stirring(e);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.stirring_charges = self.w_charges;
                self.s.stirring_until = t + self.w_duration;
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_R_TICK) => {
                self.try_mark_and_detonate(e, GROUP_R);
                e.deal(self.r_tick_dmg, DType::Magic, self.src_r_tick, false, true, 1.0);
                if self.s.r_tick_count == 0 {
                    e.ability_cast_proc();
                    e.eclipse_hit();
                }
                e.ult_hatefog();
                self.s.r_tick_count += 1;
                if self.s.r_tick_count >= self.r_tick_max {
                    self.s.r_tick_next = INF;
                } else {
                    self.s.r_tick_next = t + self.r_tick_interval;
                }
            }
            Kind::Ev(EV_R_EXPLODE) => {
                self.s.r_explode_at = INF;
                self.try_mark_and_detonate(e, GROUP_R);
                e.deal(self.r_explosion_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ult_hatefog();
            }
            Kind::Ev(EV_P_EXPLODE) => {
                let dmg = self.s.p_pending_dmg;
                self.s.p_pending_at = INF;
                e.deal(dmg, DType::Magic, self.src_p, false, true, 1.0);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
