//! Kennen. Thundering Shuriken goes out on cooldown; Slicing Maelstrom opens
//! the fight and strikes six ramping bolts; Lightning Rush is cast on
//! cooldown and recast at its earliest opportunity for the attack-speed
//! buff; Electrical Surge's passive rides every fifth basic attack for a
//! bonus on-hit and its active is cast whenever a marked/stormed target is
//! available; Mark of the Storm is tracked only to gate the active. Casts
//! go one at a time: Q, W-active and R each have a cast time that keeps
//! Kennen busy (tracked in one `busy_until`); E has none but still cannot
//! start inside another cast, and its dash form blocks attacks for its own
//! 0.5s window.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Electrical Surge's active cast.
const EV_W: u8 = 0;
/// Lightning Rush: the initial cast, then its recast.
const EV_E_CAST: u8 = 1;
const EV_E_RECAST: u8 = 2;
/// Slicing Maelstrom's periodic bolts.
const EV_R_TICK: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    mark_duration: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_time: f64,
    w_onhit_dmg: f64,
    w_active_dmg: f64,
    w_cd: f64,
    w_stack_cap: i64,
    w_cast_time: f64,
    e_dmg: f64,
    e_cd: f64,
    e_recast_delay: f64,
    e_as_pct: f64,
    e_as_dur: f64,
    r_bolt_dmg: f64,
    r_damage_amp: f64,
    r_tick_rate: f64,
    r_duration: f64,
    r_cast_time: f64,
    r_max_ticks: i64,
    src_w_onhit: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast (or channel) in progress ends here: no other cast, no
    /// attack before it.
    busy_until: f64,
    w_ready: f64,
    /// Electrical Surge stacks (0..cap); at cap the next attack consumes them.
    w_stacks: i64,
    /// Until when the dummy carries a Mark of the Storm stack.
    mark_until: f64,
    e_ready: f64,
    /// When the pending Lightning Rush may be recast (INF: none pending).
    e_recast_at: f64,
    as_buff_until: f64,
    /// Slicing Maelstrom's next bolt (INF: none pending) and how many have landed.
    r_next_tick: f64,
    r_tick_idx: i64,
    r_until: f64,
}

impl GenDriver {
    /// The earliest a cast readied at `ready` can start: not before now, and
    /// not inside another cast.
    fn castable_at(&self, e: &Engine, ready: f64) -> f64 {
        pymax(pymax(ready, e.st.t), self.s.busy_until)
    }

    /// A cast with a cast time (or a channel) just started: no other cast
    /// and no attack until it ends (an attack already due later keeps its
    /// time).
    fn busy_for(&mut self, e: &mut Engine, cast_s: f64) {
        self.s.busy_until = e.st.t + cast_s;
        e.st.next_attack = pymax(e.st.next_attack, self.s.busy_until);
    }

    /// Any ability hit refreshes the target's Mark of the Storm.
    fn apply_mark(&mut self, t: f64) {
        self.s.mark_until = t + self.mark_duration;
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let _ = level;
        let w_onhit_base = kit.at_rank("gen.W.onhit.base", ranks.w)?;
        let w_onhit_ap = kit.num("gen.W.onhit.apRatio")?;
        let w_onhit_bad = kit.at_rank("gen.W.onhit.bonusAdRatioByRank", ranks.w)?;
        let w_onhit_dmg = w_onhit_base + w_onhit_ap * sheet.ap + w_onhit_bad * sheet.ad_bonus;

        let r_duration = kit.num("gen.R.durationS")?;
        let r_tick_rate = kit.num("gen.R.tickRateS")?;
        let r_max_ticks = (r_duration / r_tick_rate).round() as i64;

        let state = State {
            busy_until: 0.0,
            w_ready: 0.0,
            w_stacks: 0,
            mark_until: 0.0,
            e_ready: 0.0,
            e_recast_at: INF,
            as_buff_until: 0.0,
            r_next_tick: INF,
            r_tick_idx: 0,
            r_until: 0.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("kennen kit needs attack.windupFraction")?,
            mark_duration: kit.num("gen.P.markDurationS")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_time: kit.num("gen.Q.castTimeS")?,
            w_onhit_dmg,
            w_active_dmg: kit.hit("gen.W.active.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_stack_cap: kit.num("gen.W.stackCap")? as i64,
            w_cast_time: kit.num("gen.W.castTimeS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_recast_delay: kit.num("gen.E.recastDelayS")?,
            e_as_pct: kit.at_rank("gen.E.asBonusPctByRank", ranks.e)?,
            e_as_dur: kit.num("gen.E.asBuffDurationS")?,
            r_bolt_dmg: kit.hit("gen.R.perBolt", ranks.r, sheet)?,
            r_damage_amp: kit.num("gen.R.damageAmpPerStrike")?,
            r_tick_rate,
            r_duration,
            r_cast_time: kit.num("gen.R.castTimeS")?,
            r_max_ticks,
            src_w_onhit: intern("W onhit"),
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
        if t < self.s.as_buff_until {
            self.e_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        // Electrical Surge: 4 stack-building attacks, then the 5th consumes
        // them all for a bonus on-hit (can crit) and a Mark of the Storm.
        if self.ranks.w > 0 {
            if self.s.w_stacks >= self.w_stack_cap {
                self.s.w_stacks = 0;
                e.deal(self.w_onhit_dmg, DType::Magic, self.src_w_onhit, true, false, 1.0);
                self.apply_mark(e.st.t);
            } else {
                self.s.w_stacks += 1;
            }
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        self.apply_mark(e.st.t);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_time);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // Slicing Maelstrom's only cast: its bolts start after the cast time.
        let t = e.st.t;
        self.s.r_until = t + self.r_cast_time + self.r_duration;
        self.s.r_next_tick = t + self.r_cast_time + self.r_tick_rate;
        self.s.r_tick_idx = 0;
        e.prime_spellblade();
        self.busy_for(e, self.r_cast_time);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            let castable = e.st.t < self.s.mark_until || e.st.t < self.s.r_until;
            if castable {
                out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W));
                n += 1;
            }
        }
        if self.ranks.e > 0 {
            if self.s.e_recast_at != INF {
                out[n] = (self.castable_at(e, self.s.e_recast_at), Kind::Ev(EV_E_RECAST));
            } else {
                out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.s.r_next_tick != INF {
            out[n] = (self.s.r_next_tick, Kind::Ev(EV_R_TICK));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_active_dmg, DType::Magic, SRC_W, false, true, 1.0);
                self.apply_mark(t);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.w_cast_time);
            }
            Kind::Ev(EV_E_CAST) => {
                // no cast time, but the dash form cannot attack or be
                // interrupted by another cast until its recast window opens
                self.s.e_recast_at = t + self.e_recast_delay;
                self.s.e_ready = INF;
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                self.apply_mark(t);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.e_recast_delay);
            }
            Kind::Ev(EV_E_RECAST) => {
                // cooldown starts post-effect, at the recast; the recast
                // itself is instant (no further busy time)
                self.s.e_recast_at = INF;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.as_buff_until = pymax(self.s.as_buff_until, t + self.e_as_dur);
            }
            Kind::Ev(EV_R_TICK) => {
                let idx = self.s.r_tick_idx;
                let mult = 1.0 + self.r_damage_amp * (idx as f64);
                e.deal(self.r_bolt_dmg * mult, DType::Magic, SRC_R, false, true, 1.0);
                self.apply_mark(t);
                if idx == 0 {
                    e.ability_cast_proc();
                    e.eclipse_hit();
                }
                e.ult_hatefog();
                self.s.r_tick_idx = idx + 1;
                if self.s.r_tick_idx < self.r_max_ticks {
                    self.s.r_next_tick = t + self.r_tick_rate;
                } else {
                    self.s.r_next_tick = INF;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
