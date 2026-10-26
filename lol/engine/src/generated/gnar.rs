//! Gnar. Mini Gnar attacks, throws Boomerang Throw (Q) and Hop (E) on
//! cooldown, stacking Hyper (W) on every on-hit / Q hit and building Rage
//! from Q's first hit and on-hit attacks; the instant Rage reaches 100 he
//! transforms into Mega Gnar, who casts Boulder Toss (Q), Wallop (W) and
//! Crunch (E) on the same shared cooldowns plus GNAR! (R) on its own. Q, W
//! and R each keep Gnar busy for their own cast time; Hop/Crunch has none.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// E is cast on cooldown (form decided at the moment it fires); its damage
/// (Hop's bounce, Crunch's impact and its delayed shockwave) lands later.
const EV_E_CAST: u8 = 0;
const EV_E_HOP_LAND: u8 = 1;
const EV_E_CRUNCH_IMPACT: u8 = 2;
const EV_E_CRUNCH_SHOCK: u8 = 3;
/// Wallop (Mega W) is cast on cooldown, only while Mega.
const EV_W_CAST: u8 = 4;
/// GNAR! is cast once available, only while Mega; its damage lands after a delay.
const EV_R_CAST: u8 = 5;
const EV_R_DMG: u8 = 6;
/// Mega Gnar's timer running out: revert to Mini Gnar.
const EV_REVERT: u8 = 7;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    mega_duration: f64,
    tired_duration: f64,
    max_rage: f64,
    rage_base: f64,
    rage_q_frac: f64,
    rage_attack_frac: f64,

    q_mini_dmg: f64,
    q_mini_mult: f64,
    q_mini_keep: f64,
    q_mega_dmg: f64,
    q_mega_keep: f64,
    q_cd: f64,
    q_cast_s: f64,

    w_hyper_dmg: f64,
    w_hyper_target_hp_ratio: f64,
    hyper_cap: i64,
    hyper_dur: f64,
    w_mega_dmg: f64,
    w_cd: f64,
    w_cast_s: f64,

    e_mini_dmg: f64,
    e_mega_dmg: f64,
    e_as_pct: f64,
    e_as_dur: f64,
    travel_time: f64,
    shock_delay: f64,
    e_cd: f64,

    r_dmg: f64,
    r_dmg_delay: f64,
    r_cd: f64,
    r_cast_s: f64,

    src_w_hyper: SourceId,
    src_e_shock: SourceId,

    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    rage: f64,
    form_mega: bool,
    mega_until: f64,
    tired_until: f64,
    hyper_stacks: i64,
    hyper_last_t: f64,
    e_as_until: f64,
    e_ready: f64,
    w_ready: f64,
    r_ready: f64,
    e_hop_land_at: f64,
    e_crunch_impact_at: f64,
    e_crunch_shock_at: f64,
    r_dmg_at: f64,
    /// The cast in progress ends here: no other cast, no attack before it
    /// (also used to hold everything through Crunch's impact+shockwave window).
    busy_until: f64,
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

    /// Hyper: applied by every on-hit attack and damaging ability hit while
    /// Mini Gnar; the third stack (within its duration) consumes them all.
    fn apply_hyper(&mut self, e: &mut Engine, t: f64) {
        if self.ranks.w == 0 || self.s.form_mega {
            return;
        }
        if t - self.s.hyper_last_t > self.hyper_dur {
            self.s.hyper_stacks = 0;
        }
        self.s.hyper_last_t = t;
        self.s.hyper_stacks += 1;
        if self.s.hyper_stacks >= self.hyper_cap {
            self.s.hyper_stacks = 0;
            let dmg = self.w_hyper_dmg + self.w_hyper_target_hp_ratio * e.target_hp;
            e.deal(dmg, DType::Magic, self.src_w_hyper, false, false, 1.0);
        }
    }

    /// Rage from Q's first hit / an on-hit attack; transforms instantly at 100.
    fn add_rage(&mut self, t: f64, amt: f64) {
        if self.s.form_mega {
            return;
        }
        if t < self.s.tired_until {
            return;
        }
        self.s.rage = pymin(self.s.rage + amt, self.max_rage);
        if self.s.rage >= self.max_rage {
            self.s.form_mega = true;
            self.s.mega_until = t + self.mega_duration;
            self.s.rage = 0.0;
            self.s.hyper_stacks = 0;
            self.s.e_as_until = -1.0;
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            rage: 0.0,
            form_mega: false,
            mega_until: INF,
            tired_until: -1.0,
            hyper_stacks: 0,
            hyper_last_t: -INF,
            e_as_until: -1.0,
            e_ready: 0.0,
            w_ready: 0.0,
            r_ready: 0.0,
            e_hop_land_at: INF,
            e_crunch_impact_at: INF,
            e_crunch_shock_at: INF,
            r_dmg_at: INF,
            busy_until: 0.0,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("gnar kit needs attack.windupFraction")?,

            mega_duration: kit.num("gen.P.megaDurationS")?,
            tired_duration: kit.num("gen.P.tiredDurationS")?,
            max_rage: kit.num("gen.P.maxRage")?,
            rage_base: kit.num("gen.P.rageBase")?,
            rage_q_frac: kit.num("gen.P.rageQFrac")?,
            rage_attack_frac: kit.num("gen.P.rageAttackFrac")?,

            q_mini_dmg: kit.hit("gen.Q.miniDamage", ranks.q, sheet)?,
            q_mini_mult: kit.num("gen.Q.miniSubsequentMult")?,
            q_mini_keep: 1.0 - kit.num("gen.Q.miniCDRefund")?,
            q_mega_dmg: kit.hit("gen.Q.megaDamage", ranks.q, sheet)?,
            q_mega_keep: 1.0 - kit.num("gen.Q.megaCDRefund")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,

            w_hyper_dmg: kit.hit("gen.W.hyperDamage", ranks.w, sheet)?,
            w_hyper_target_hp_ratio: kit.at_rank("gen.W.hyperTargetHpRatio", ranks.w)?,
            hyper_cap: kit.num("gen.W.hyperStackCap")? as i64,
            hyper_dur: kit.num("gen.W.hyperStackDurationS")?,
            w_mega_dmg: kit.hit("gen.W.megaDamage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cast_s: kit.num("gen.W.castTimeS")?,

            e_mini_dmg: kit.hit("gen.E.miniDamage", ranks.e, sheet)?,
            e_mega_dmg: kit.hit("gen.E.megaDamage", ranks.e, sheet)?,
            e_as_pct: kit.at_rank("gen.E.miniAsPct", ranks.e)? * 100.0,
            e_as_dur: kit.num("gen.E.miniAsDurationS")?,
            travel_time: kit.num("gen.E.travelTimeS")?,
            shock_delay: kit.num("gen.E.shockDelayS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,

            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_dmg_delay: kit.num("gen.R.dmgDelayS")?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,

            src_w_hyper: intern("W onhit"),
            src_e_shock: intern("E shockwave"),

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
        if !self.s.form_mega && t < self.s.e_as_until {
            self.e_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        if self.s.form_mega {
            return;
        }
        let t = e.st.t;
        self.apply_hyper(e, t);
        self.add_rage(t, self.rage_base * self.rage_attack_frac);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.form_mega {
            e.deal(self.q_mega_dmg, DType::Physical, SRC_Q, false, true, 1.0);
            e.st.q_ready = t + e.basic_cd(self.q_cd) * self.q_mega_keep;
        } else {
            e.deal(self.q_mini_dmg, DType::Physical, SRC_Q, false, true, 1.0);
            self.apply_hyper(e, t);
            let ret = self.q_mini_dmg * self.q_mini_mult;
            e.deal(ret, DType::Physical, SRC_Q, false, true, 1.0);
            self.apply_hyper(e, t);
            e.st.q_ready = t + e.basic_cd(self.q_cd) * self.q_mini_keep;
            self.add_rage(t, self.rage_base * self.rage_q_frac);
        }
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.e_hop_land_at != INF {
            out[n] = (self.s.e_hop_land_at, Kind::Ev(EV_E_HOP_LAND));
            n += 1;
        }
        if self.s.e_crunch_impact_at != INF {
            out[n] = (self.s.e_crunch_impact_at, Kind::Ev(EV_E_CRUNCH_IMPACT));
            n += 1;
        }
        if self.s.e_crunch_shock_at != INF {
            out[n] = (self.s.e_crunch_shock_at, Kind::Ev(EV_E_CRUNCH_SHOCK));
            n += 1;
        }
        if self.ranks.w > 0 && self.s.form_mega {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.r > 0 && self.s.form_mega {
            out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R_CAST));
            n += 1;
        }
        if self.s.r_dmg_at != INF {
            out[n] = (self.s.r_dmg_at, Kind::Ev(EV_R_DMG));
            n += 1;
        }
        if self.s.form_mega {
            out[n] = (self.s.mega_until, Kind::Ev(EV_REVERT));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                if self.s.form_mega {
                    self.s.e_crunch_impact_at = t + self.travel_time;
                    self.s.busy_until = t + self.travel_time + self.shock_delay;
                } else {
                    self.s.e_hop_land_at = t + self.travel_time;
                    self.s.e_as_until = t + self.e_as_dur;
                }
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_HOP_LAND) => {
                self.s.e_hop_land_at = INF;
                e.deal(self.e_mini_dmg, DType::Physical, SRC_E, false, true, 1.0);
                self.apply_hyper(e, t);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_CRUNCH_IMPACT) => {
                self.s.e_crunch_impact_at = INF;
                e.deal(self.e_mega_dmg, DType::Physical, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.e_crunch_shock_at = t + self.shock_delay;
            }
            Kind::Ev(EV_E_CRUNCH_SHOCK) => {
                self.s.e_crunch_shock_at = INF;
                self.s.busy_until = t;
                e.deal(self.e_mega_dmg, DType::Physical, self.src_e_shock, false, true, 1.0);
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_mega_dmg, DType::Physical, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.w_cast_s);
            }
            Kind::Ev(EV_R_CAST) => {
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                self.s.r_dmg_at = t + self.r_dmg_delay;
                e.prime_spellblade();
                self.busy_for(e, self.r_cast_s);
            }
            Kind::Ev(EV_R_DMG) => {
                self.s.r_dmg_at = INF;
                e.deal(self.r_dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            Kind::Ev(EV_REVERT) => {
                self.s.form_mega = false;
                self.s.mega_until = INF;
                self.s.tired_until = t + self.tired_duration;
                self.s.rage = 0.0;
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
