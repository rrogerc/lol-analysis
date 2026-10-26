//! Pantheon. Comet Spear is tapped on cooldown (quickcast for its cooldown
//! refund), carrying every Mortal Will empowerment since it is recast far
//! more often than Shield Vault or Aegis Assault; its 0.2 s cast time keeps
//! Pantheon busy so no other cast or attack starts inside it. Shield Vault
//! and Aegis Assault are cast on cooldown for their own damage, Aegis
//! Assault always held for its full channel; Grand Starfall opens the
//! fight, its two channels locking out every other action until the spear
//! and shockwave land, after which it grants a permanent armor shred (its
//! passive pen) and refills Mortal Will.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Shield Vault comes off cooldown.
const EV_W: u8 = 0;
/// Aegis Assault is cast (starts its channel).
const EV_E_CAST: u8 = 1;
/// Aegis Assault's channel ends: the capped DoT lump plus the recast slam.
const EV_E_END: u8 = 2;
/// Grand Starfall's spear lands.
const EV_R_SPEAR: u8 = 3;
/// Grand Starfall's shockwave lands (Pantheon reappears).
const EV_R_SHOCK: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    /// Mortal Will's stack cap.
    p_max: i64,
    q_dmg_normal: f64,
    q_dmg_execute: f64,
    q_empower_bonus: f64,
    q_execute_threshold: f64,
    /// 1 - Comet Spear's tap cooldown refund fraction.
    q_refund_mult: f64,
    q_cd: f64,
    q_cast_s: f64,
    /// Shield Vault's damage as a fraction of the target's maximum health.
    w_pct: f64,
    w_cd: f64,
    e_channel_dmg: f64,
    e_slam_dmg: f64,
    e_channel_dur: f64,
    e_cd: f64,
    r_spear_dmg: f64,
    r_shock_dmg: f64,
    /// Time from the R cast to the spear landing / the shockwave landing.
    r_spear_offset: f64,
    r_shock_offset: f64,
    r_shred_dur: f64,
    src_e_slam: SourceId,
    src_r_spear: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_stacks: i64,
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    w_ready: f64,
    e_ready: f64,
    /// When Aegis Assault's current channel ends (INF: none pending).
    e_channel_end: f64,
    /// Grand Starfall's pending spear / shockwave landings (INF: none).
    r_spear_at: f64,
    r_shock_at: f64,
}

impl GenDriver {
    /// Mortal Will generates a stack (capped), whenever an attack lands or a
    /// basic ability / the ultimate is cast; consumption only happens in
    /// cast_q.
    fn gen_stack(&mut self) {
        if self.s.p_stacks < self.p_max {
            self.s.p_stacks += 1;
        }
    }

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
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let p_max = kit.num("gen.P.maxStacks")? as i64;

        let q_dmg_normal = kit.hit("gen.Q.tapDamage", ranks.q, sheet)?;
        let q_dmg_execute = kit.hit("gen.Q.tapDamageExecute", ranks.q, sheet)?;
        let q_empower_bonus = kit.at_level("gen.Q.empowerBonusByLevel", level)?
            + kit.num("gen.Q.empowerBonusBonusAdRatio")? * sheet.ad_bonus;
        let q_execute_threshold = kit.num("gen.Q.critHealthThreshold")?;
        let q_refund_mult = 1.0 - kit.num("gen.Q.tapCooldownRefund")?;
        let q_cd = kit.at_rank("abilities.Q.cooldownS", ranks.q)?;
        let q_cast_s = kit.num("gen.Q.castTimeS")?;

        let w_base_pct = kit.at_rank("gen.W.maxHealthPct", ranks.w)?;
        let w_pct = w_base_pct
            + kit.num("gen.W.apCoef")? * sheet.ap
            + kit.num("gen.W.bonusHpCoef")? * sheet.hp_bonus;
        let w_cd = kit.at_rank("abilities.W.cooldownS", ranks.w)?;

        let e_channel_dmg = kit.hit("gen.E.channelTotal", ranks.e, sheet)?;
        let e_slam_dmg = kit.hit("gen.E.slamDamage", ranks.e, sheet)?;
        let e_channel_dur = kit.num("gen.E.channelDurationS")?;
        let e_cd = kit.at_rank("abilities.E.cooldownS", ranks.e)?;

        let r_spear_base = kit.at_rank("gen.R.spearBase", ranks.q)?;
        let r_spear_dmg = r_spear_base
            + kit.num("gen.R.spearBonusAdRatio")? * sheet.ad_bonus
            + kit.num("gen.R.spearApRatio")? * sheet.ap;
        let r_shock_dmg = kit.hit("gen.R.shockwave", ranks.r, sheet)?;
        let r_cast_time = kit.num("gen.R.castTimeS")?;
        let r_first_channel = kit.num("gen.R.firstChannelS")?;
        let r_second_channel = kit.num("gen.R.secondChannelS")?;
        let r_spear_timing = kit.num("gen.R.spearTimingS")?;
        let r_spear_travel = kit.num("gen.R.spearTravelS")?;
        let r_spear_offset = r_cast_time + r_first_channel + r_spear_timing + r_spear_travel;
        let r_shock_offset = r_cast_time + r_first_channel + r_second_channel;
        let r_shred_dur = kit.num("abilities.Q.shred.durationS")?;

        let state = State {
            p_stacks: p_max,
            busy_until: 0.0,
            w_ready: 0.0,
            e_ready: 0.0,
            e_channel_end: INF,
            r_spear_at: INF,
            r_shock_at: INF,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            p_max,
            q_dmg_normal,
            q_dmg_execute,
            q_empower_bonus,
            q_execute_threshold,
            q_refund_mult,
            q_cd,
            q_cast_s,
            w_pct,
            w_cd,
            e_channel_dmg,
            e_slam_dmg,
            e_channel_dur,
            e_cd,
            r_spear_dmg,
            r_shock_dmg,
            r_spear_offset,
            r_shock_offset,
            r_shred_dur,
            src_e_slam: intern("E slam"),
            src_r_spear: intern("R spear"),
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

    fn after_attack(&mut self, _e: &mut Engine) {
        // a landed basic attack generates a Mortal Will stack
        self.gen_stack();
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // always the tap (quickcast) release: thrust damage, 60% CDR refund;
        // lands with the cast, then keeps Pantheon busy for its 0.2 s cast time
        let t = e.st.t;
        let empowered = self.s.p_stacks >= self.p_max;
        if empowered {
            self.s.p_stacks = 0;
        } else {
            self.gen_stack();
        }
        let ratio = pymax(e.st.hp, 0.0) / e.target_hp;
        let mut dmg = if ratio < self.q_execute_threshold {
            self.q_dmg_execute
        } else {
            self.q_dmg_normal
        };
        if empowered {
            dmg += self.q_empower_bonus;
        }
        e.deal(dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.st.q_ready = t + e.basic_cd(self.q_cd) * self.q_refund_mult;
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.r_spear_at = t + self.r_spear_offset;
        self.s.r_shock_at = t + self.r_shock_offset;
        let landing = self.s.r_shock_at;
        // the first and second channels block attacks, casts and movement
        e.st.next_attack = pymax(e.st.next_attack, landing);
        e.st.q_ready = pymax(e.st.q_ready, landing);
        self.s.w_ready = pymax(self.s.w_ready, landing);
        self.s.e_ready = pymax(self.s.e_ready, landing);
        self.s.busy_until = pymax(self.s.busy_until, landing);
        // the passive armor penetration is permanent from the opening cast
        e.st.shred_until = t + self.r_shred_dur;
        self.gen_stack();
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_channel_end != INF {
                out[n] = (self.s.e_channel_end, Kind::Ev(EV_E_END));
            } else {
                out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.s.r_spear_at != INF {
            out[n] = (self.s.r_spear_at, Kind::Ev(EV_R_SPEAR));
            n += 1;
        }
        if self.s.r_shock_at != INF {
            out[n] = (self.s.r_shock_at, Kind::Ev(EV_R_SHOCK));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                let dmg = self.w_pct * e.target_hp;
                e.deal(dmg, DType::Physical, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.gen_stack();
                self.s.w_ready = t + e.basic_cd(self.w_cd);
            }
            Kind::Ev(EV_E_CAST) => {
                // the channel blocks attacks and other casts for its duration
                self.s.e_channel_end = t + self.e_channel_dur;
                e.st.next_attack = pymax(e.st.next_attack, self.s.e_channel_end);
                e.st.q_ready = pymax(e.st.q_ready, self.s.e_channel_end);
                self.s.w_ready = pymax(self.s.w_ready, self.s.e_channel_end);
                self.s.busy_until = pymax(self.s.busy_until, self.s.e_channel_end);
                e.prime_spellblade();
                self.gen_stack();
            }
            Kind::Ev(EV_E_END) => {
                // the capped 100% AD DoT lump, then the automatic recast slam
                e.deal(self.e_channel_dmg, DType::Physical, SRC_E, false, true, 1.0);
                e.deal(self.e_slam_dmg, DType::Physical, self.src_e_slam, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.e_channel_end = INF;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
            }
            Kind::Ev(EV_R_SPEAR) => {
                e.deal(self.r_spear_dmg, DType::Physical, self.src_r_spear, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.r_spear_at = INF;
            }
            Kind::Ev(EV_R_SHOCK) => {
                e.deal(self.r_shock_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                // Pantheon gains full Mortal Will stacks upon landing
                self.s.p_stacks = self.p_max;
                self.s.r_shock_at = INF;
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
