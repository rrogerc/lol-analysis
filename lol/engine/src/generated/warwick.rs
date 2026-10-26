//! Warwick. An auto-attacker whose Eternal Hunger (P) rides every attack and
//! re-triggers off Jaws of the Beast (Q) and the odd ticks of Infinite
//! Duress (R); Blood Hunt (W) is recast on cooldown purely to guarantee its
//! attack-speed window against the dummy; Primal Howl (E) is cast and
//! recast on cooldown for its attack-reset; Infinite Duress opens the fight
//! through its own cast time and then channels through 6 magic damage
//! ticks. Casts go one at a time: W's 0.5 s and R's 0.1 s cast times keep
//! Warwick busy, and every other cast waits for them.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Blood Hunt's cast (the mark, no damage).
const EV_W_CAST: u8 = 0;
/// Primal Howl's initial cast, then its 1 s-delayed recast.
const EV_E_CAST: u8 = 1;
const EV_E_RECAST: u8 = 2;
/// One of Infinite Duress's 6 channel ticks.
const EV_R_TICK: u8 = 3;
/// Infinite Duress coming off cooldown again (not expected inside 15 s).
const EV_R_RECAST: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Eternal Hunger's flat magic on-hit, already resolved for this build.
    p_onhit: f64,
    /// Jaws of the Beast's AD/AP portion; the target-max-hp% is applied live.
    q_ad_ap: f64,
    q_hp_pct: f64,
    q_cd: f64,
    w_as_pct: f64,
    w_active_dur: f64,
    w_passive_dur: f64,
    w_first_threshold: f64,
    w_second_threshold: f64,
    w_cd: f64,
    w_cast_s: f64,
    e_recast_delay: f64,
    e_cd: f64,
    /// Infinite Duress's total magic damage, split across its ticks.
    r_total: f64,
    r_tick_interval: f64,
    r_ticks: i64,
    r_big_frac: f64,
    r_small_frac: f64,
    r_duration: f64,
    r_cd: f64,
    r_cast_s: f64,
    src_p: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    w_ready: f64,
    /// The dummy is marked (Blood Hunt's passives apply regardless of health) until this.
    w_marked_until: f64,
    e_ready: f64,
    /// When the pending Primal Howl may be recast (INF: none pending).
    e_recast_at: f64,
    /// Infinite Duress: channel end, tick bookkeeping, and its own cooldown.
    r_channel_until: f64,
    r_tick_idx: i64,
    r_next_tick_at: f64,
    r_ready: f64,
    /// Blood Hunt's bonus attack speed, and until when it applies.
    passive_as_pct: f64,
    passive_as_until: f64,
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

    /// Eternal Hunger's on-hit proc: fires on attacks, Jaws of the Beast,
    /// and Infinite Duress's odd ticks.
    fn p_proc(&mut self, e: &mut Engine) {
        e.deal(self.p_onhit, DType::Magic, self.src_p, false, false, 1.0);
    }

    /// Blood Hunt's attack-speed check: any basic-attack or ability damage
    /// against a marked or sub-50%-health target grants/refreshes the buff,
    /// doubled below 25% health.
    fn maybe_bloodhunt(&mut self, e: &mut Engine) {
        if self.ranks.w == 0 {
            return;
        }
        let t = e.st.t;
        let hp_frac = pymax(e.st.hp, 0.0) / e.target_hp;
        let marked = t < self.s.w_marked_until;
        let below_first = hp_frac < self.w_first_threshold;
        if marked || below_first {
            let below_second = hp_frac < self.w_second_threshold;
            let pct = if below_second { self.w_as_pct * 2.0 } else { self.w_as_pct };
            self.s.passive_as_pct = pct;
            self.s.passive_as_until = t + self.w_passive_dur;
        }
    }

    /// Starts (or restarts) Infinite Duress's 1.5 s channel and schedules
    /// its first tick; the caller handles its cast time around this.
    fn start_r_channel(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.r_channel_until = t + self.r_duration;
        self.s.r_tick_idx = 0;
        self.s.r_next_tick_at = t + self.r_tick_interval;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        e.st.next_attack = pymax(e.st.next_attack, self.s.r_channel_until);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let p_base = kit.at_level("gen.P.onHitBase", level)?;
        let p_ad_ratio = kit.num("gen.P.adRatio")?;
        let p_ap_ratio = kit.num("gen.P.apRatio")?;
        let state = State {
            busy_until: 0.0,
            w_ready: 0.0,
            w_marked_until: -1.0,
            e_ready: 0.0,
            e_recast_at: INF,
            r_channel_until: -1.0,
            r_tick_idx: 0,
            r_next_tick_at: INF,
            r_ready: 0.0,
            passive_as_pct: 0.0,
            passive_as_until: -1.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("warwick kit needs attack.windupFraction")?,
            p_onhit: p_base + p_ad_ratio * sheet.ad_bonus + p_ap_ratio * sheet.ap,
            q_ad_ap: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_hp_pct: kit.at_rank("gen.Q.targetMaxHpPct", ranks.q)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_as_pct: kit.at_rank("gen.W.asBonusPct", ranks.w)?,
            w_active_dur: kit.num("gen.W.activeDurationS")?,
            w_passive_dur: kit.num("gen.W.passiveDurationS")?,
            w_first_threshold: kit.num("gen.W.firstHpThreshold")?,
            w_second_threshold: kit.num("gen.W.secondHpThreshold")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cast_s: kit.num("gen.W.castTimeS")?,
            e_recast_delay: kit.num("gen.E.recastDelayS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_total: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_tick_interval: kit.num("gen.R.tickIntervalS")?,
            r_ticks: kit.num("gen.R.tickCount")? as i64,
            r_big_frac: kit.num("gen.R.bigTickNumerator")? / kit.num("gen.R.tickDenominator")?,
            r_small_frac: kit.num("gen.R.smallTickNumerator")? / kit.num("gen.R.tickDenominator")?,
            r_duration: kit.num("gen.R.channelDurationS")?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            src_p: intern("P onhit"),
            s: state,
            s0: state,
        })
    }

    fn reset(&mut self) {
        self.s = self.s0;
    }

    fn ranged(&self) -> bool {
        false
    }

    fn attack_range(&self) -> f64 {
        self.attack_range
    }

    fn bonus_as(&self, t: f64) -> f64 {
        if t < self.s.passive_as_until {
            self.s.passive_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        self.p_proc(e);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        self.maybe_bloodhunt(e);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if e.st.t < self.s.r_channel_until {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // Jaws of the Beast has no cast time: it lands with the cast (its
        // 0.2475 s figure is a lunge delay, not a cast time), but the
        // 0.25 s lockout keeps the next attack from starting inside it.
        let t = e.st.t;
        let hp_amt = self.q_hp_pct / 100.0 * e.target_hp;
        let dmg = self.q_ad_ap + hp_amt;
        e.deal(dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.p_proc(e);
        self.maybe_bloodhunt(e);
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the engine has already primed Spellblade and
        // delayed the first attack past the 0.25 s cast lockout; Infinite
        // Duress's own 0.1 s cast time then keeps Warwick busy before the
        // channel's first tick
        if self.ranks.r == 0 {
            return;
        }
        self.start_r_channel(e);
        self.busy_for(e, self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            let ready = self.castable_at(e, pymax(self.s.w_ready, self.s.r_channel_until));
            out[n] = (ready, Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_recast_at != INF {
                out[n] = (self.castable_at(e, self.s.e_recast_at), Kind::Ev(EV_E_RECAST));
            } else {
                let ready = self.castable_at(e, pymax(self.s.e_ready, self.s.r_channel_until));
                out[n] = (ready, Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.s.r_next_tick_at != INF {
            out[n] = (self.s.r_next_tick_at, Kind::Ev(EV_R_TICK));
            n += 1;
        } else if self.ranks.r > 0 {
            out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R_RECAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                // a real 0.5 s cast time: no other cast, no attack until it
                // ends, so `busy_for` covers the interruption too
                e.prime_spellblade();
                self.s.w_marked_until = t + self.w_active_dur;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.busy_for(e, self.w_cast_s);
            }
            Kind::Ev(EV_E_CAST) => {
                // no cast time: no lockout, and the recast (which starts
                // the cooldown) comes 1 s later
                e.prime_spellblade();
                self.s.e_recast_at = t + self.e_recast_delay;
            }
            Kind::Ev(EV_E_RECAST) => {
                // the recast's howling animation is a stated lockout, not a
                // cast time
                e.lockout();
                let b = self.bonus_as(t);
                e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
                e.prime_spellblade();
                self.s.e_recast_at = INF;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
            }
            Kind::Ev(EV_R_TICK) => {
                let idx = self.s.r_tick_idx + 1;
                let odd = idx % 2 == 1;
                let frac = if odd { self.r_big_frac } else { self.r_small_frac };
                let amt = self.r_total * frac;
                e.deal(amt, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                if odd {
                    self.p_proc(e);
                }
                self.maybe_bloodhunt(e);
                e.ult_hatefog();
                self.s.r_tick_idx = idx;
                if idx < self.r_ticks {
                    self.s.r_next_tick_at = t + self.r_tick_interval;
                } else {
                    self.s.r_next_tick_at = INF;
                }
            }
            Kind::Ev(EV_R_RECAST) => {
                self.start_r_channel(e);
                self.busy_for(e, self.r_cast_s);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
