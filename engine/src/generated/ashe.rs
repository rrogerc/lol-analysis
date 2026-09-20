//! Ashe. An auto-attacker whose passive (Frost Shot) is exactly the engine's
//! own crit-as-expected-value math applied to basic attacks, so it needs no
//! extra code. Ranger's Focus (Q) is a free, instant self-buff activated the
//! moment 4 Focus stacks are up: it raises attack speed and turns every
//! attack, for 6 s, into an empowered flurry dealt as its own "Q" source
//! (the attack's own damage instance is zeroed for that swing) and resets
//! the attack timer. Volley (W) and Enchanted Crystal Arrow (R) are plain
//! cast-then-land nukes on cooldown; R opens the fight.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Volley: cast, then its damage lands at cast-time end.
const EV_W_CAST: u8 = 0;
const EV_W_LAND: u8 = 1;
/// Enchanted Crystal Arrow: recast after the opening cast, then its landing.
const EV_R_CAST: u8 = 2;
const EV_R_LAND: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Ranger's Focus: bonus attack speed (percent) and buff duration while active.
    q_as_bonus: f64,
    q_buff_dur: f64,
    q_max_stacks: i64,
    /// Total flurry damage as a fraction of total AD, replacing the normal hit.
    q_flurry_ratio: f64,
    q_cd: f64,
    w_dmg: f64,
    w_cd: f64,
    w_cast_s: f64,
    r_dmg: f64,
    r_cd: f64,
    r_cast_s: f64,
    src_q: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    focus_stacks: i64,
    /// Ranger's Focus is active while t < buff_until.
    buff_until: f64,
    w_ready: f64,
    /// When pending Volley damage lands (INF: none pending).
    w_land_at: f64,
    /// When R may next be cast (INF: none pending / not yet due).
    r_ready: f64,
    /// When pending R damage lands (INF: none pending).
    r_land_at: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            focus_stacks: 0,
            buff_until: -1.0,
            w_ready: 0.0,
            w_land_at: INF,
            r_ready: INF,
            r_land_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("ashe kit needs attack.windupFraction")?,
            q_as_bonus: kit.at_rank("gen.Q.asBonusPct", ranks.q)?,
            q_buff_dur: kit.num("gen.Q.buffDurationS")?,
            q_max_stacks: kit.num("gen.Q.maxStacks")? as i64,
            q_flurry_ratio: kit.at_rank("gen.Q.flurryAdRatio", ranks.q)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cast_s: kit.num("gen.W.castTimeS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            src_q: intern("Q flurry"),
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
        if t < self.s.buff_until {
            self.q_as_bonus
        } else {
            0.0
        }
    }

    fn attack_damage(&self, e: &Engine) -> f64 {
        if e.st.t < self.s.buff_until {
            // the flurry entirely replaces the normal hit; its damage is
            // dealt separately, in after_attack, under the Q source
            0.0
        } else {
            e.p.ad
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        // Focus stacks build only while Ranger's Focus is inactive.
        if e.st.t >= self.s.buff_until {
            self.s.focus_stacks = imin(self.s.focus_stacks + 1, self.q_max_stacks);
        }
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if e.st.t < self.s.buff_until {
            // the empowered flurry's total damage, given the same
            // expected-value crit treatment a normal attack gets
            e.deal(self.q_flurry_ratio * e.p.ad, DType::Physical, self.src_q, true, false, 1.0);
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if t < self.s.buff_until {
            // Each empowered attack resets the attack timer.
            e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
        } else {
            e.st.next_attack = t + e.attack_period(b);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if e.st.t < self.s.buff_until {
            return INF;
        }
        if self.s.focus_stacks < self.q_max_stacks {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.focus_stacks = 0;
        self.s.buff_until = t + self.q_buff_dur;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        e.prime_spellblade();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        // the opening cast: the engine has already handled the 0.25 s lockout
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        self.s.r_land_at = t + self.r_cast_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            if self.s.w_land_at != INF {
                out[n] = (self.s.w_land_at, Kind::Ev(EV_W_LAND));
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_land_at != INF {
                out[n] = (self.s.r_land_at, Kind::Ev(EV_R_LAND));
                n += 1;
            } else if self.s.r_ready != INF {
                out[n] = (pymax(self.s.r_ready, e.st.t), Kind::Ev(EV_R_CAST));
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
                self.s.w_land_at = t + self.w_cast_s;
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_W_LAND) => {
                self.s.w_land_at = INF;
                e.deal(self.w_dmg, DType::Physical, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_R_CAST) => {
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                self.s.r_land_at = t + self.r_cast_s;
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_R_LAND) => {
                self.s.r_land_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
