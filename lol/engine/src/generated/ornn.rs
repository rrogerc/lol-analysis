//! Ornn. An auto-attacking tank whose damage comes from his abilities:
//! Volcanic Rupture on cooldown, Bellows Breath's 5-tick march on cooldown,
//! Searing Charge on cooldown (its own stun self-consumes the Brittle it
//! just applied for Master Craftsman's bonus magic damage), and Call of the
//! Forge God opening the fight with its first pass then recasting as soon
//! as it can (its own stun likewise self-consumes its fresh Brittle). Every
//! cast keeps Ornn busy for its own cast time (or, for Bellows Breath's
//! lockout march and Call of the Forge God's post-dash lockout, that real
//! time), one `busy_until` shared by every cast so only one is ever in
//! progress.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Bellows Breath: the march starts, then each of its 5 ticks.
const EV_W_CAST: u8 = 0;
const EV_W_TICK: u8 = 1;
/// Searing Charge is cast.
const EV_E_CAST: u8 = 2;
/// Call of the Forge God's first pass lands, then its recast.
const EV_R_PASS1: u8 = 3;
const EV_R_RECAST: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    p_brittle_pct: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    w_tick_flat: f64,
    w_tick_pct: f64,
    w_num_ticks: i64,
    w_tick_interval: f64,
    w_march_duration: f64,
    w_cd: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cast_s: f64,
    r_dmg: f64,
    r_cast_s: f64,
    r_recast_delay: f64,
    r_recast_lockout_s: f64,
    r_cd: f64,
    src_w: SourceId,
    src_e: SourceId,
    src_p: SourceId,
    src_r_recast: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    w_ready: f64,
    /// Time of the next Bellows Breath tick, INF while not marching.
    w_tick_next: f64,
    w_tick_idx: i64,
    e_ready: f64,
    /// Time Call of the Forge God's first pass lands, INF once resolved.
    r_pass1_at: f64,
    /// Time the recast may be thrown, INF once resolved.
    r_recast_at: f64,
}

impl GenDriver {
    /// The earliest a cast readied at `ready` can start: not before now, and
    /// not inside another cast.
    fn castable_at(&self, e: &Engine, ready: f64) -> f64 {
        pymax(pymax(ready, e.st.t), self.s.busy_until)
    }

    /// A cast (or lockout period) just started: no other cast and no attack
    /// until it ends (an attack already due later keeps its time).
    fn busy_for(&mut self, e: &mut Engine, cast_s: f64) {
        self.s.busy_until = e.st.t + cast_s;
        e.st.next_attack = pymax(e.st.next_attack, self.s.busy_until);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            busy_until: 0.0,
            w_ready: 0.0,
            w_tick_next: INF,
            w_tick_idx: 0,
            e_ready: 0.0,
            r_pass1_at: INF,
            r_recast_at: INF,
        };
        let e_armor_ratio = kit.num("gen.E.armorRatio")?;
        let e_mr_ratio = kit.num("gen.E.mrRatio")?;
        let e_base = kit.at_rank("gen.E.damageBase", ranks.e)?;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            p_brittle_pct: kit.at_level("gen.P.brittleDamagePctByLevel", level)? / 100.0,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_tick_flat: kit.at_rank("gen.W.tickFlat", ranks.w)?,
            w_tick_pct: kit.at_rank("gen.W.tickPctMaxHp", ranks.w)?,
            w_num_ticks: kit.num("gen.W.numTicks")? as i64,
            w_tick_interval: kit.num("gen.W.tickIntervalS")?,
            w_march_duration: kit.num("gen.W.marchDurationS")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_dmg: e_base + e_armor_ratio * sheet.armor + e_mr_ratio * sheet.mr,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_recast_delay: kit.num("gen.R.recastDelayS")?,
            r_recast_lockout_s: kit.num("gen.R.recastLockoutS")?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            src_w: intern("W tick"),
            src_e: intern("E"),
            src_p: intern("P consume"),
            src_r_recast: intern("R recast"),
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

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.r_pass1_at = t + self.r_cast_s;
        self.s.r_recast_at = t + self.r_recast_delay;
        self.busy_for(e, self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            if self.s.w_tick_next != INF {
                out[n] = (self.s.w_tick_next, Kind::Ev(EV_W_TICK));
            } else {
                out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_pass1_at != INF {
                out[n] = (self.s.r_pass1_at, Kind::Ev(EV_R_PASS1));
                n += 1;
            } else if self.s.r_recast_at != INF {
                out[n] = (self.castable_at(e, self.s.r_recast_at), Kind::Ev(EV_R_RECAST));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.w_tick_idx = 1;
                self.s.w_tick_next = t + self.w_tick_interval;
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.w_march_duration);
            }
            Kind::Ev(EV_W_TICK) => {
                let tick_dmg = pymax(self.w_tick_flat, self.w_tick_pct * e.target_hp);
                e.deal(tick_dmg, DType::Magic, self.src_w, false, true, 1.0);
                if self.s.w_tick_idx >= self.w_num_ticks {
                    self.s.w_tick_next = INF;
                    self.s.w_tick_idx = 0;
                } else {
                    self.s.w_tick_idx += 1;
                    self.s.w_tick_next = t + self.w_tick_interval;
                }
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Physical, self.src_e, false, true, 1.0);
                let bonus = self.p_brittle_pct * e.target_hp;
                e.deal(bonus, DType::Magic, self.src_p, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_R_PASS1) => {
                self.s.r_pass1_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
            }
            Kind::Ev(EV_R_RECAST) => {
                self.s.r_recast_at = INF;
                e.deal(self.r_dmg, DType::Magic, self.src_r_recast, false, true, 1.0);
                let bonus = self.p_brittle_pct * e.target_hp;
                e.deal(bonus, DType::Magic, self.src_p, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.r_recast_lockout_s);
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
