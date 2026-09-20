//! Smolder. Opens with MMOOOMMMM! (R) at t=0 (assumed sweetspot hit), then
//! spams Super Scorcher Breath (Q) and Achooo! (W) on cooldown, weaving
//! Flap, Flap, Flap (E) into its own cooldown windows (E's 1.25s channel
//! pauses Q/W and attacks, per the dossier). Dragon Practice stacks build
//! from 0 as abilities land, feeding P's onhit bonus damage on Q/W/E and
//! (rarely, inside a short fight) Q's stack-gated tiers.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// R's wave lands (after its 0.75 s cast).
const EV_R: u8 = 0;
/// W is cast on cooldown.
const EV_W: u8 = 1;
/// E's channel starts (cast, cooldown starts here) and ends (bolts land).
const EV_E_START: u8 = 2;
const EV_E_END: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,

    q_cd: f64,
    w_cd: f64,
    e_cd: f64,
    r_cast_time: f64,

    // Precomputed, fight-constant damage numbers.
    q_phys: f64,
    q_tier1: i64,
    q_tier2: i64,
    q_tier3: i64,
    q_tier2_mod: f64,
    q_tier2_bolts_base: f64,
    q_tier2_bolts_per_stack: f64,
    q_tier3_ad_coef: f64,
    q_tier3_stack_coef: f64,
    bonus_ad: f64,

    p_q_per_stack: f64,
    p_q_crit_term: f64,
    p_w_per_stack: f64,
    p_e_per_stack: f64,
    p_e_crit_term: f64,

    w1: f64,
    w2: f64,
    w_decay_factor: f64,

    e_phys: f64,
    e_bolts_base: f64,
    e_bolts_per_stack: f64,
    e_duration: f64,

    r_dmg: f64,

    starting_stacks: i64,

    src_p_q: SourceId,
    src_q_t1: SourceId,
    src_p_q_t1: SourceId,
    src_q_t2: SourceId,
    src_p_q_t2: SourceId,
    src_q_t3: SourceId,
    src_w_expl: SourceId,
    src_p_w: SourceId,
    src_p_e: SourceId,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    stacks: i64,
    w_ready: f64,
    w_decay_mult: f64,
    w_stack_granted: bool,
    e_ready: f64,
    e_channel_until: f64,
    e_bolts_n: i64,
    e_cast_stacks: i64,
    r_lock_until: f64,
    r_hit_at: f64,
}

impl GenDriver {
    fn fire_w(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.w_ready = t + e.basic_cd(self.w_cd);
        e.deal(self.w1, DType::Physical, SRC_W, false, true, 1.0);
        let mult = self.s.w_decay_mult;
        e.deal(self.w2 * mult, DType::Physical, self.src_w_expl, false, true, 1.0);
        let p_bonus = self.p_w_per_stack * (self.s.stacks as f64) * mult;
        e.deal(p_bonus, DType::Magic, self.src_p_w, false, false, 1.0);
        self.s.w_decay_mult = self.s.w_decay_mult * self.w_decay_factor;
        if !self.s.w_stack_granted {
            self.s.stacks += 1;
            self.s.w_stack_granted = true;
        }
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let crit_chance_frac = sheet.crit_chance / 100.0;
        let crit_damage_frac = sheet.crit_damage / 100.0;
        let crit_expected = crit_chance_frac * (crit_damage_frac - 1.0);

        let q_crit_ratio = kit.num("gen.Q.critRatio")?;
        let q_phys = kit.hit("gen.Q.damage", ranks.q, sheet)? + q_crit_ratio * crit_expected;

        let p_q_per_stack = kit.num("gen.P.QDamagePerStack")?;
        let p_q_crit_ratio = kit.num("gen.P.QCritRatio")?;
        let p_q_crit_term = p_q_crit_ratio * crit_expected;

        let p_w_per_stack = kit.num("gen.P.WDamagePerStack")?;

        let p_e_per_stack = kit.num("gen.P.EDamagePerStack")?;
        let p_e_crit_ratio = kit.num("gen.P.ECritRatio")?;
        let p_e_crit_term = p_e_crit_ratio * crit_expected;

        let w1 = kit.hit("gen.W.damage1", ranks.w, sheet)?;
        let w2 = kit.hit("gen.W.damage2", ranks.w, sheet)?;

        let e_phys = kit.hit("gen.E.damage", ranks.e, sheet)?;

        let r_base = kit.hit("gen.R.damage", ranks.r, sheet)?;
        let sweetspot = kit.num("gen.R.sweetspotMult")?;
        let r_dmg = r_base * sweetspot;

        let starting_stacks = kit.num("gen.P.startingStacks")? as i64;

        let state = State {
            stacks: starting_stacks,
            w_ready: 0.0,
            w_decay_mult: 1.0,
            w_stack_granted: false,
            e_ready: 0.0,
            e_channel_until: INF,
            e_bolts_n: 0,
            e_cast_stacks: 0,
            r_lock_until: 0.0,
            r_hit_at: INF,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_cast_time: kit.num("gen.R.castTimeS")?,
            q_phys,
            q_tier1: kit.num("gen.Q.tier1Stacks")? as i64,
            q_tier2: kit.num("gen.Q.tier2Stacks")? as i64,
            q_tier3: kit.num("gen.Q.tier3Stacks")? as i64,
            q_tier2_mod: kit.num("gen.Q.tier2Mod")?,
            q_tier2_bolts_base: kit.num("gen.Q.tier2BoltsBase")?,
            q_tier2_bolts_per_stack: kit.num("gen.Q.tier2BoltsPerStack")?,
            q_tier3_ad_coef: kit.num("gen.Q.tier3BurnAdCoef")?,
            q_tier3_stack_coef: kit.num("gen.Q.tier3BurnStackCoef")?,
            bonus_ad: sheet.ad_bonus,
            p_q_per_stack,
            p_q_crit_term,
            p_w_per_stack,
            p_e_per_stack,
            p_e_crit_term,
            w1,
            w2,
            w_decay_factor: kit.num("gen.W.decayFactor")?,
            e_phys,
            e_bolts_base: kit.num("gen.E.boltsBase")?,
            e_bolts_per_stack: kit.num("gen.E.boltsPerStack")?,
            e_duration: kit.num("gen.E.durationS")?,
            r_dmg,
            starting_stacks,
            src_p_q: intern("P Q bonus"),
            src_q_t1: intern("Q tier1 explosion"),
            src_p_q_t1: intern("P Q tier1 bonus"),
            src_q_t2: intern("Q tier2 bolt"),
            src_p_q_t2: intern("P Q tier2 bonus"),
            src_q_t3: intern("Q tier3 burn"),
            src_w_expl: intern("W explosion"),
            src_p_w: intern("P W bonus"),
            src_p_e: intern("P E bonus"),
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

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        let t = e.st.t;
        if t < self.s.r_lock_until || t < self.s.e_channel_until {
            return INF;
        }
        pymax(e.st.q_ready, t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        let stacks_i = self.s.stacks;
        let stacks = stacks_i as f64;

        e.deal(self.q_phys, DType::Physical, SRC_Q, false, true, 1.0);
        let p_bonus = self.p_q_per_stack * stacks + self.p_q_crit_term;
        e.deal(p_bonus, DType::Magic, self.src_p_q, false, false, 1.0);

        if stacks_i >= self.q_tier1 {
            e.deal(self.q_phys, DType::Physical, self.src_q_t1, false, true, 1.0);
            e.deal(p_bonus, DType::Magic, self.src_p_q_t1, false, false, 1.0);
        }
        if stacks_i >= self.q_tier2 {
            let bolts_f = self.q_tier2_bolts_base + self.q_tier2_bolts_per_stack * stacks;
            let mut n = bolts_f as i64;
            if (n as f64) < bolts_f {
                n += 1;
            }
            let bolt_dmg = self.q_tier2_mod * self.q_phys;
            let bolt_p = self.q_tier2_mod * p_bonus;
            let mut i = 0;
            while i < n {
                e.deal(bolt_dmg, DType::Physical, self.src_q_t2, false, true, 1.0);
                e.deal(bolt_p, DType::Magic, self.src_p_q_t2, false, false, 1.0);
                i += 1;
            }
        }
        if stacks_i >= self.q_tier3 {
            let frac = self.q_tier3_ad_coef * self.bonus_ad + self.q_tier3_stack_coef * stacks;
            let burn = frac * e.target_hp;
            e.deal(burn, DType::True, self.src_q_t3, false, true, 1.0);
        }

        self.s.stacks += 1;
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.r_lock_until = t + self.r_cast_time;
        self.s.r_hit_at = t + self.r_cast_time;
        e.lockout();
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        let t = e.st.t;

        if self.s.r_hit_at != INF {
            out[n] = (self.s.r_hit_at, Kind::Ev(EV_R));
            n += 1;
        }

        let r_locked = t < self.s.r_lock_until;

        if self.ranks.w > 0 && self.s.e_channel_until == INF && !r_locked {
            out[n] = (pymax(self.s.w_ready, t), Kind::Ev(EV_W));
            n += 1;
        }

        if self.ranks.e > 0 {
            if self.s.e_channel_until != INF {
                out[n] = (self.s.e_channel_until, Kind::Ev(EV_E_END));
                n += 1;
            } else if !r_locked {
                out[n] = (pymax(self.s.e_ready, t), Kind::Ev(EV_E_START));
                n += 1;
            }
        }

        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R) => {
                self.s.r_hit_at = INF;
                e.deal(self.r_dmg, DType::Physical, SRC_R, false, true, 1.0);
                self.s.stacks += 1;
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            Kind::Ev(EV_W) => {
                self.fire_w(e);
            }
            Kind::Ev(EV_E_START) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_cast_stacks = self.s.stacks;
                let bolts_f = self.e_bolts_base + self.e_bolts_per_stack * (self.s.stacks as f64);
                self.s.e_bolts_n = bolts_f as i64;
                self.s.e_channel_until = t + self.e_duration;
                e.st.next_attack = pymax(e.st.next_attack, self.s.e_channel_until);
                self.s.stacks += 1;
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_END) => {
                let n = self.s.e_bolts_n;
                let stacks = self.s.e_cast_stacks as f64;
                let p_bolt = self.p_e_per_stack * stacks + self.p_e_crit_term;
                let mut i = 0;
                while i < n {
                    e.deal(self.e_phys, DType::Physical, SRC_E, false, true, 1.0);
                    e.deal(p_bolt, DType::Magic, self.src_p_e, false, false, 1.0);
                    i += 1;
                }
                self.s.e_channel_until = INF;
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
