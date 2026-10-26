//! Galio. Hero's Entrance opens the fight (self-targeted, since there are no
//! allies), then Winds of War and Justice Punch go out on cooldown, Shield
//! of Durand is charged to its 1.25s cap and recast for maximum damage, and
//! Colossal Smash periodically empowers a basic attack while granting bonus
//! attack speed whenever it is off cooldown. Casts go one at a time: Winds
//! of War and Justice Punch each keep Galio busy for their cast time, per
//! the guide's cast-time rule.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Winds of War's tornado ticks (up to 4 per cast).
const EV_Q_TICK: u8 = 0;
/// Shield of Durand: starting the channel, then its recast.
const EV_W_ACT: u8 = 1;
/// Justice Punch, cast on cooldown.
const EV_E_CAST: u8 = 2;
/// Hero's Entrance's impact, at the end of its channel.
const EV_R_IMPACT: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    // Colossal Smash
    p_ad_dmg: f64,
    p_rest_dmg: f64,
    p_as_pct: f64,
    p_cd: f64,
    p_cdr_hit: f64,
    src_p: SourceId,
    // Winds of War
    q_gust_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    q_tick_frac: f64,
    q_ticks: i64,
    q_tick_interval: f64,
    src_q_tick: SourceId,
    // Shield of Durand
    w_max_dmg: f64,
    w_cd: f64,
    w_charge_cap: f64,
    w_post_lock: f64,
    // Justice Punch
    e_dmg: f64,
    e_cd: f64,
    e_cast_s: f64,
    // Hero's Entrance
    r_dmg: f64,
    r_channel: f64,
    // rotation state
    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_ready: f64,
    p_attack_empowered: bool,
    q_ticks_left: i64,
    q_tick_next: f64,
    w_channeling: bool,
    w_channel_start: f64,
    w_ready: f64,
    e_ready: f64,
    r_impact_at: f64,
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

    /// Colossal Smash's current cooldown is cut to `t`, down by its flat
    /// reduction, whenever a landed ability caused it; only while it is
    /// actually on cooldown.
    fn p_reduce_cd(&mut self, t: f64) {
        if t < self.s.p_ready {
            self.s.p_ready = pymax(t, self.s.p_ready - self.p_cdr_hit);
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let p_base = kit.at_level("gen.P.damageByLevel", level)?;
        let p_ad_ratio = kit.num("gen.P.adRatio")?;
        let p_ap_ratio = kit.num("gen.P.apRatio")?;
        let p_mr_ratio = kit.num("gen.P.bonusMrRatio")?;

        let base_pct = kit.num("gen.Q.tornadoTickBasePct")?;
        let ap_pct_per100 = kit.num("gen.Q.tornadoTickApPctPer100")?;
        let q_tick_frac = base_pct / 100.0 + (ap_pct_per100 / 100.0) * (sheet.ap / 100.0);

        let r_bonus_mr_ratio = kit.num("gen.R.bonusMrRatio")?;

        let state = State {
            p_ready: 0.0,
            p_attack_empowered: false,
            q_ticks_left: 0,
            q_tick_next: INF,
            w_channeling: false,
            w_channel_start: INF,
            w_ready: 0.0,
            e_ready: 0.0,
            r_impact_at: INF,
            busy_until: 0.0,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("galio kit needs attack.windupFraction")?,
            p_ad_dmg: sheet.ad * p_ad_ratio,
            p_rest_dmg: p_base + sheet.ap * p_ap_ratio + sheet.mr * p_mr_ratio,
            p_as_pct: kit.num("gen.P.asBonusPct")?,
            p_cd: kit.num("gen.P.cooldownS")?,
            p_cdr_hit: kit.num("gen.P.cdrOnAbilityHitS")?,
            src_p: intern("P"),
            q_gust_dmg: kit.hit("gen.Q.gustDamage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            q_tick_frac,
            q_ticks: kit.num("gen.Q.tornadoTicks")? as i64,
            q_tick_interval: kit.num("gen.Q.tornadoTickIntervalS")?,
            src_q_tick: intern("Q tornado"),
            w_max_dmg: kit.hit("gen.W.maxDamage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_charge_cap: kit.num("gen.W.chargeCapS")?,
            w_post_lock: kit.num("gen.W.postRecastLockoutS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)? + r_bonus_mr_ratio * sheet.mr,
            r_channel: kit.num("gen.R.channelS")?,
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
        if t < self.s.p_ready {
            0.0
        } else {
            self.p_as_pct
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        // Colossal Smash empowers the attack whenever it is off cooldown.
        self.s.p_attack_empowered = e.st.t >= self.s.p_ready;
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.p_attack_empowered {
            let t = e.st.t;
            e.deal(self.p_ad_dmg, DType::Magic, self.src_p, true, true, 1.0);
            e.deal(self.p_rest_dmg, DType::Magic, self.src_p, false, true, 1.0);
            self.s.p_ready = t + self.p_cd;
            self.s.p_attack_empowered = false;
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
        e.deal(self.q_gust_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.p_reduce_cd(t);
        self.s.q_ticks_left = self.q_ticks;
        self.s.q_tick_next = t + self.q_tick_interval;
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // self-targeted opening cast: the channel blocks everything else.
        let t = e.st.t;
        self.s.busy_until = pymax(self.s.busy_until, t + self.r_channel);
        self.s.r_impact_at = t + self.r_channel;
        e.st.next_attack = pymax(e.st.next_attack, self.s.busy_until);
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        let t = e.st.t;
        if self.s.q_ticks_left > 0 {
            out[n] = (self.s.q_tick_next, Kind::Ev(EV_Q_TICK));
            n += 1;
        }
        if self.ranks.w > 0 {
            let time = if self.s.w_channeling {
                self.s.w_channel_start + self.w_charge_cap
            } else if t < self.s.busy_until {
                INF
            } else {
                pymax(self.s.w_ready, t)
            };
            out[n] = (time, Kind::Ev(EV_W_ACT));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.r_impact_at != INF {
            out[n] = (self.s.r_impact_at, Kind::Ev(EV_R_IMPACT));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_TICK) => {
                let dmg = self.q_tick_frac * e.target_hp;
                e.deal(dmg, DType::Magic, self.src_q_tick, false, true, 1.0);
                self.s.q_ticks_left -= 1;
                self.s.q_tick_next = if self.s.q_ticks_left > 0 {
                    t + self.q_tick_interval
                } else {
                    INF
                };
            }
            Kind::Ev(EV_W_ACT) => {
                if !self.s.w_channeling {
                    // start the channel; the cooldown starts now (on-cast)
                    self.s.w_channeling = true;
                    self.s.w_channel_start = t;
                    self.s.w_ready = t + e.basic_cd(self.w_cd);
                    self.s.busy_until = pymax(self.s.busy_until, t + self.w_charge_cap);
                    e.st.next_attack = pymax(e.st.next_attack, self.s.busy_until);
                    e.prime_spellblade();
                } else {
                    // recast at the 1.25s scaling cap: maximum damage
                    self.s.w_channeling = false;
                    e.deal(self.w_max_dmg, DType::Magic, SRC_W, false, true, 1.0);
                    e.ability_cast_proc();
                    e.eclipse_hit();
                    e.prime_spellblade();
                    self.p_reduce_cd(t);
                    self.s.busy_until = t + self.w_post_lock;
                    e.st.next_attack = pymax(e.st.next_attack, self.s.busy_until);
                }
            }
            Kind::Ev(EV_E_CAST) => {
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.p_reduce_cd(t);
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_R_IMPACT) => {
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
                self.p_reduce_cd(t);
                self.s.r_impact_at = INF;
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
