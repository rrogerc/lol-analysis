//! Sejuani. Opens with Glacial Prison (assumed empowered by opening range),
//! holds Arctic Assault to land right after the bola for the dash, the
//! knockup-collision attack reset, and to consume the Icebreaker mark, then
//! recasts it on cooldown; Winter's Wrath is cast on cooldown with its two
//! separately timed hits; Permafrost fires the instant the dummy holds 4
//! Frost stacks and is off cooldown, resetting the attack timer on landing;
//! basic attacks and Winter's Wrath build Frost stacks or consume an active
//! Icebreaker mark for bonus true-max-hp magic damage.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Glacial Prison's bola lands (after its cast time).
const EV_R_LAND: u8 = 0;
/// Winter's Wrath: the cast starts, the swing lands, the lash lands.
const EV_W_CAST: u8 = 1;
const EV_W_SWING: u8 = 2;
const EV_W_LASH: u8 = 3;
/// Permafrost: the cast starts (stacks committed), the hit lands.
const EV_E_CAST: u8 = 4;
const EV_E_LAND: u8 = 5;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    icebreaker_ratio: f64,
    q_dmg: f64,
    q_cd: f64,
    /// Holds the opening Q until Glacial Prison's cast time ends.
    q_open_hold: f64,
    w_dmg1: f64,
    w_dmg2: f64,
    w_cd: f64,
    w_cast_s: f64,
    w_swing_delay: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cast_s: f64,
    e_stun_s: f64,
    e_lockout_s: f64,
    e_need: i64,
    r_dmg: f64,
    r_cast_s: f64,
    r_stun_s: f64,
    src_icebreaker: SourceId,
    src_w1: SourceId,
    src_w2: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    frost_stacks: i64,
    /// The target cannot regain Frost stacks before this time.
    frost_lockout_until: f64,
    /// Icebreaker's mark is available to consume up to this time (-INF: none).
    frozen_until: f64,
    w_ready: f64,
    w_swing_at: f64,
    w_lash_at: f64,
    e_ready: f64,
    e_land_at: f64,
    r_land_at: f64,
}

impl GenDriver {
    /// An ability hit against a Frozen target consumes the mark for bonus
    /// magic damage; otherwise it has no on-hit side effect here.
    fn consume_mark(&mut self, e: &Engine, t: f64) -> f64 {
        if t <= self.s.frozen_until {
            self.s.frozen_until = -INF;
            self.icebreaker_ratio * e.target_hp
        } else {
            0.0
        }
    }

    /// A basic attack or Winter's Wrath hit: consumes an active Icebreaker
    /// mark, or otherwise applies a Frost stack (unless locked out).
    fn on_hit(&mut self, e: &Engine, t: f64) -> f64 {
        let bonus = self.consume_mark(e, t);
        if bonus > 0.0 {
            return bonus;
        }
        if t >= self.s.frost_lockout_until {
            self.s.frost_stacks = imin(self.s.frost_stacks + 1, self.e_need);
        }
        0.0
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, prestacked: bool)
        -> Result<Self, String> {
        let e_need = kit.num("gen.E.maxStacks")? as i64;
        let r_cast_s = kit.num("gen.R.castTimeS")?;
        let state = State {
            frost_stacks: if prestacked { e_need } else { 0 },
            frost_lockout_until: -INF,
            frozen_until: -INF,
            w_ready: 0.0,
            w_swing_at: INF,
            w_lash_at: INF,
            e_ready: 0.0,
            e_land_at: INF,
            r_land_at: INF,
        };
        let _ = level;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("sejuani kit needs attack.windupFraction")?,
            icebreaker_ratio: kit.num("gen.P.icebreakerTargetHpRatio")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_open_hold: r_cast_s,
            w_dmg1: kit.hit("gen.W.damage1", ranks.w, sheet)?,
            w_dmg2: kit.hit("gen.W.damage2", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cast_s: kit.num("gen.W.castTimeS")?,
            w_swing_delay: kit.num("gen.W.swingDelayS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            e_stun_s: kit.num("gen.E.stunS")?,
            e_lockout_s: kit.num("gen.E.perChampionLockoutS")?,
            e_need,
            r_dmg: kit.hit("gen.R.damageEmpowered", ranks.r, sheet)?,
            r_cast_s,
            r_stun_s: kit.num("gen.R.stunEmpoweredS")?,
            src_icebreaker: intern("P Icebreaker"),
            src_w1: intern("W swing"),
            src_w2: intern("W lash"),
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

    fn attack_riders(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let bonus = self.on_hit(e, t);
        if bonus > 0.0 {
            e.deal(bonus, DType::Magic, self.src_icebreaker, false, false, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(pymax(e.st.q_ready, self.q_open_hold), e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        let bonus = self.consume_mark(e, t);
        if bonus > 0.0 {
            e.deal(bonus, DType::Magic, self.src_icebreaker, false, false, 1.0);
        }
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        // the dash collides with the dummy: Sejuani is ordered to attack it
        let b = self.bonus_as(t);
        e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the engine has already primed Spellblade and
        // delayed the first attack past the 0.25s cast; the bola lands then
        self.s.r_land_at = e.st.t + self.r_cast_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.r_land_at != INF {
            out[n] = (self.s.r_land_at, Kind::Ev(EV_R_LAND));
            n += 1;
        }
        if self.ranks.w > 0 {
            if self.s.w_swing_at != INF {
                out[n] = (self.s.w_swing_at, Kind::Ev(EV_W_SWING));
                n += 1;
            } else if self.s.w_lash_at != INF {
                out[n] = (self.s.w_lash_at, Kind::Ev(EV_W_LASH));
                n += 1;
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
                n += 1;
            }
        }
        if self.ranks.e > 0 {
            if self.s.e_land_at != INF {
                out[n] = (self.s.e_land_at, Kind::Ev(EV_E_LAND));
                n += 1;
            } else if self.s.frost_stacks >= self.e_need && e.st.t >= self.s.e_ready {
                out[n] = (e.st.t, Kind::Ev(EV_E_CAST));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_LAND) => {
                self.s.r_land_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                self.s.frozen_until = t + self.r_stun_s;
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.w_swing_at = t + self.w_swing_delay;
                self.s.w_lash_at = t + self.w_cast_s;
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_W_SWING) => {
                self.s.w_swing_at = INF;
                let bonus = self.on_hit(e, t);
                if bonus > 0.0 {
                    e.deal(bonus, DType::Magic, self.src_icebreaker, false, false, 1.0);
                }
                e.deal(self.w_dmg1, DType::Physical, self.src_w1, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_W_LASH) => {
                self.s.w_lash_at = INF;
                let bonus = self.on_hit(e, t);
                if bonus > 0.0 {
                    e.deal(bonus, DType::Magic, self.src_icebreaker, false, false, 1.0);
                }
                e.deal(self.w_dmg2, DType::Physical, self.src_w2, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_land_at = t + self.e_cast_s;
                self.s.frost_stacks = 0;
                self.s.frost_lockout_until = t + self.e_lockout_s;
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_E_LAND) => {
                self.s.e_land_at = INF;
                let bonus = self.consume_mark(e, t);
                if bonus > 0.0 {
                    e.deal(bonus, DType::Magic, self.src_icebreaker, false, false, 1.0);
                }
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.frozen_until = t + self.e_stun_s;
                // Permafrost resets Sejuani's basic attack timer
                let b = self.bonus_as(t);
                e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
