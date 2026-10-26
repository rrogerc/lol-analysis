//! Gwen. Auto-attacks carry A Thousand Cuts (percent-max-health on-hit magic
//! damage); Needlework opens the fight and free-recasts twice at a fixed
//! 1s interval to unload all 9 needles; Skip 'n Slash is woven in on
//! cooldown for its attack-speed/on-hit window and attack-timer reset; Snip
//! Snip! is cast on cooldown (a 0.5s cast time), its mini-snip count driven
//! by Snippy stacks built up from the actual attack timeline. Casts go one
//! at a time: Snip Snip!'s cast time keeps Gwen busy, and every other cast
//! waits for it.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Skip 'n Slash comes off cooldown and is cast.
const EV_E_CAST: u8 = 0;
/// A pending Needlework cast's needles land.
const EV_R_LAND: u8 = 1;
/// The next Needlework recast is allowed to start.
const EV_R_NEXT: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// A Thousand Cuts: fraction of the target's max health, and its AP scaling.
    p_pct: f64,
    q_cd: f64,
    q_cast_s: f64,
    q_mini_dmg: f64,
    q_final_dmg: f64,
    q_true_conv: f64,
    q_base_mini: i64,
    q_max_bonus: i64,
    q_stack_dur: f64,
    e_cd: f64,
    e_onhit_dmg: f64,
    e_as_pct: f64,
    e_cd_refund_pct: f64,
    e_buff_dur: f64,
    r_per_needle_dmg: f64,
    r_needle_count1: i64,
    r_needle_count2: i64,
    r_needle_count3: i64,
    r_cast_time_first: f64,
    r_cast_time_recast: f64,
    r_recast_interval: f64,
    src_p: SourceId,
    src_q_true: SourceId,
    src_e_onhit: SourceId,
    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    q_stack_count: i64,
    q_stack_expire: f64,
    e_ready: f64,
    e_buff_until: f64,
    e_refund_pending: bool,
    r_stage: i64,
    r_land_at: f64,
    r_next_at: f64,
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
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let p_base = kit.num("gen.P.onhitPct.base")?;
        let p_ap_per100 = kit.num("gen.P.onhitPct.apPer100")?;
        let state = State {
            busy_until: 0.0,
            q_stack_count: 0,
            q_stack_expire: -1.0,
            e_ready: 0.0,
            e_buff_until: -1.0,
            e_refund_pending: false,
            r_stage: 0,
            r_land_at: INF,
            r_next_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("gwen kit needs attack.windupFraction")?,
            p_pct: p_base + p_ap_per100 * (sheet.ap / 100.0),
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            q_mini_dmg: kit.hit("gen.Q.miniDamage", ranks.q, sheet)?,
            q_final_dmg: kit.hit("gen.Q.finalDamage", ranks.q, sheet)?,
            q_true_conv: kit.num("gen.Q.trueDamageConversion")?,
            q_base_mini: kit.num("gen.Q.baseMiniSnips")? as i64,
            q_max_bonus: kit.num("gen.Q.maxBonusSnips")? as i64,
            q_stack_dur: kit.num("gen.Q.stackDurationS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_onhit_dmg: kit.hit("gen.E.onhitDamage", ranks.e, sheet)?,
            e_as_pct: kit.at_rank("gen.E.bonusAsPct", ranks.e)?,
            e_cd_refund_pct: kit.at_rank("gen.E.cdRefundPct", ranks.e)?,
            e_buff_dur: kit.num("gen.E.buffDurationS")?,
            r_per_needle_dmg: kit.hit("gen.R.perNeedle", ranks.r, sheet)?,
            r_needle_count1: kit.num("gen.R.needleCount1")? as i64,
            r_needle_count2: kit.num("gen.R.needleCount2")? as i64,
            r_needle_count3: kit.num("gen.R.needleCount3")? as i64,
            r_cast_time_first: kit.num("gen.R.castTimeFirstS")?,
            r_cast_time_recast: kit.num("gen.R.castTimeRecastS")?,
            r_recast_interval: kit.num("gen.R.recastIntervalS")?,
            src_p: intern("P"),
            src_q_true: intern("Q true"),
            src_e_onhit: intern("E onhit"),
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
        if self.ranks.e > 0 && t < self.s.e_buff_until {
            self.e_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        // Snippy: a stack per landed attack, refreshing a shared 6s timer
        if t > self.s.q_stack_expire {
            self.s.q_stack_count = 0;
        }
        self.s.q_stack_count = imin(self.s.q_stack_count + 1, self.q_max_bonus);
        self.s.q_stack_expire = t + self.q_stack_dur;

        // Skip 'n Slash's cooldown refund on the first empowered attack
        if self.ranks.e > 0 && self.s.e_refund_pending && t < self.s.e_buff_until {
            let remaining = self.s.e_ready - t;
            if remaining > 0.0 {
                self.s.e_ready = t + remaining * (1.0 - self.e_cd_refund_pct);
            }
            self.s.e_refund_pending = false;
        }
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        let amt_p = self.p_pct * e.target_hp;
        e.deal(amt_p, DType::Magic, self.src_p, false, false, 1.0);
        if self.ranks.e > 0 && e.st.t < self.s.e_buff_until {
            e.deal(self.e_onhit_dmg, DType::Magic, self.src_e_onhit, false, false, 1.0);
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

        let stacks = if t > self.s.q_stack_expire { 0 } else { self.s.q_stack_count };
        let mini_count = self.q_base_mini + stacks;
        self.s.q_stack_count = 0;
        self.s.q_stack_expire = -1.0;

        let mc = mini_count as f64;
        let mini_total = self.q_mini_dmg * mc;
        let mini_true = mini_total * self.q_true_conv;
        let mini_magic = mini_total - mini_true;
        e.deal(mini_magic, DType::Magic, SRC_Q, false, true, 1.0);
        e.deal(mini_true, DType::True, self.src_q_true, false, true, 1.0);
        let mini_p = self.p_pct * e.target_hp * mc;
        e.deal(mini_p, DType::Magic, self.src_p, false, true, 1.0);

        let final_true = self.q_final_dmg * self.q_true_conv;
        let final_magic = self.q_final_dmg - final_true;
        e.deal(final_magic, DType::Magic, SRC_Q, false, true, 1.0);
        e.deal(final_true, DType::True, self.src_q_true, false, true, 1.0);
        let final_p = self.p_pct * e.target_hp;
        e.deal(final_p, DType::Magic, self.src_p, false, true, 1.0);

        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the engine has already primed Spellblade and
        // held the first attack past the cast; the needle lands at cast end
        let t = e.st.t;
        self.s.r_stage = 1;
        self.s.r_land_at = t + self.r_cast_time_first;
        self.s.r_next_at = t + self.r_recast_interval;
        self.busy_for(e, self.r_cast_time_first);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.r_land_at != INF {
            out[n] = (self.s.r_land_at, Kind::Ev(EV_R_LAND));
            n += 1;
        }
        if self.s.r_next_at != INF {
            out[n] = (self.castable_at(e, self.s.r_next_at), Kind::Ev(EV_R_NEXT));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                // no cast time: an instant cast that resets the attack timer,
                // but it still waits for any cast in progress (castable_at)
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_buff_until = t + self.e_buff_dur;
                self.s.e_refund_pending = true;
                let b = self.bonus_as(t);
                e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_LAND) => {
                self.s.r_land_at = INF;
                let count = match self.s.r_stage {
                    1 => self.r_needle_count1,
                    2 => self.r_needle_count2,
                    _ => self.r_needle_count3,
                };
                let cf = count as f64;
                let magic = self.r_per_needle_dmg * cf;
                e.deal(magic, DType::Magic, SRC_R, false, true, 1.0);
                let p_amt = self.p_pct * e.target_hp * cf;
                e.deal(p_amt, DType::Magic, self.src_p, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            Kind::Ev(EV_R_NEXT) => {
                if self.s.r_stage < 3 {
                    self.s.r_stage += 1;
                    self.s.r_land_at = t + self.r_cast_time_recast;
                    if self.s.r_stage < 3 {
                        self.s.r_next_at = t + self.r_recast_interval;
                    } else {
                        self.s.r_next_at = INF;
                    }
                    e.prime_spellblade();
                    self.busy_for(e, self.r_cast_time_recast);
                } else {
                    self.s.r_next_at = INF;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
