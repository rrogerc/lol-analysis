//! Quinn. Behind Enemy Lines (R) opens the fight and recasts on cooldown all
//! fight long: its 0.25 s cast time keeps Quinn busy, then its 2 s channel
//! blocks attacks and Q/E, and the first attack after the channel both lands
//! and auto-triggers Skystrike (R's damage), which marks the target and
//! starts R's post-effect cooldown. Between R cycles, Blinding Assault (Q,
//! 0.25 s cast time) and Vault (E, no cast time) go out on cooldown, each
//! applying a Harrier mark; the next attack that lands on a still-marked
//! target consumes it for Harrier's on-hit bonus damage and Heightened
//! Senses' attack-speed buff.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Vault is cast on cooldown.
const EV_E_CAST: u8 = 0;
/// Behind Enemy Lines' channel ends (BEL becomes active, awaiting a trigger).
const EV_R_CHANNEL_END: u8 = 1;
/// Behind Enemy Lines is (re)cast once its post-effect cooldown expires.
const EV_R_START: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Harrier's on-hit bonus damage (byLevel base + 40% bonus AD, fixed for the fight).
    p_dmg: f64,
    /// How long a Harrier mark applied by Q/E lasts, by R's Skystrike, and the 1s reapplication lockout.
    mark_dur_qe: f64,
    mark_dur_r: f64,
    harrier_lock_s: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    e_dmg: f64,
    e_cd: f64,
    /// Heightened Senses' bonus attack speed (percent) and its buff duration.
    w_as_pct: f64,
    w_buff_s: f64,
    r_dmg: f64,
    r_cd: f64,
    r_channel_s: f64,
    r_cast_s: f64,
    src_p: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    mark_active: bool,
    mark_expiry: f64,
    /// Harrier cannot reapply a mark before this time.
    harrier_lock_until: f64,
    /// Heightened Senses' attack-speed buff runs until this time.
    w_buff_until: f64,
    e_ready: f64,
    /// Behind Enemy Lines: while channeling this is the channel's end time
    /// (INF otherwise); `r_active` is true once united, awaiting the
    /// triggering attack; `r_ready` is when it may next be cast.
    r_channel_until: f64,
    r_active: bool,
    r_ready: f64,
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

    /// Applies a Harrier mark at `t` lasting `dur`, respecting the 1s
    /// reapplication lockout after a mark is consumed or times out.
    fn try_mark(&mut self, t: f64, dur: f64) {
        if self.s.mark_active && t > self.s.mark_expiry {
            // the old mark timed out unconsumed: start its lockout
            self.s.harrier_lock_until = self.s.mark_expiry + self.harrier_lock_s;
            self.s.mark_active = false;
        }
        if t >= self.s.harrier_lock_until {
            self.s.mark_active = true;
            self.s.mark_expiry = t + dur;
        }
    }

    /// Begins Behind Enemy Lines' cast time and channel: holds attacks (and,
    /// via q_at / events, Q and E) until it completes.
    fn start_r_channel(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.busy_for(e, self.r_cast_s);
        self.s.r_channel_until = t + self.r_cast_s + self.r_channel_s;
        self.s.r_active = false;
        e.st.next_attack = pymax(e.st.next_attack, self.s.r_channel_until);
        e.prime_spellblade();
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let p_base = kit.at_level("gen.P.byLevel", level)?;
        let p_adr = kit.num("gen.P.bonusAdRatio")?;
        let p_dmg = p_base + p_adr * sheet.ad_bonus;

        let q_base = kit.at_rank("gen.Q.damage.base", ranks.q)?;
        let q_adr = kit.at_rank("gen.Q.damage.bonusAdRatio", ranks.q)?;
        let q_apr = kit.num("gen.Q.damage.apRatio")?;
        let q_dmg = q_base + q_adr * sheet.ad_bonus + q_apr * sheet.ap;

        let e_base = kit.at_rank("gen.E.damage.base", ranks.e)?;
        let e_adr = kit.num("gen.E.damage.bonusAdRatio")?;
        let e_dmg = e_base + e_adr * sheet.ad_bonus;

        let r_base = kit.at_rank("gen.R.damage.base", ranks.r)?;
        let r_adr = kit.num("gen.R.damage.bonusAdRatio")?;
        let r_dmg = r_base + r_adr * sheet.ad_bonus;

        let w_as_pct = kit.at_rank("gen.W.asBonus", ranks.w)? * 100.0;

        let state = State {
            busy_until: 0.0,
            mark_active: false,
            mark_expiry: 0.0,
            harrier_lock_until: 0.0,
            w_buff_until: 0.0,
            e_ready: 0.0,
            r_channel_until: INF,
            r_active: false,
            r_ready: 0.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("quinn kit needs attack.windupFraction")?,
            p_dmg,
            mark_dur_qe: kit.num("gen.P.markDurationS")?,
            mark_dur_r: kit.num("gen.R.markDurationS")?,
            harrier_lock_s: kit.num("gen.P.harrierLockoutS")?,
            q_dmg,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            e_dmg,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            w_as_pct,
            w_buff_s: kit.num("gen.W.buffDurationS")?,
            r_dmg,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_channel_s: kit.num("gen.R.channelS")?,
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
        self.attack_range > MELEE_MAX_RANGE
    }

    fn attack_range(&self) -> f64 {
        self.attack_range
    }

    fn bonus_as(&self, t: f64) -> f64 {
        if t < self.s.w_buff_until {
            self.w_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        // Behind Enemy Lines: this attack's declaration auto-triggers
        // Skystrike, which still lets the attack itself land normally.
        if self.s.r_active {
            self.s.r_active = false;
            let t = e.st.t;
            e.deal(self.r_dmg, DType::Physical, SRC_R, false, true, 1.0);
            e.ability_cast_proc();
            e.eclipse_hit();
            e.ult_hatefog();
            self.try_mark(t, self.mark_dur_r);
            self.s.r_ready = t + e.ult_cd(self.r_cd);
        }
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.mark_active && t <= self.s.mark_expiry {
            self.s.mark_active = false;
            self.s.harrier_lock_until = t + self.harrier_lock_s;
            self.s.w_buff_until = t + self.w_buff_s;
            e.deal(self.p_dmg, DType::Physical, self.src_p, false, false, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if self.s.r_channel_until != INF {
            // Behind Enemy Lines' channel holds Q
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.try_mark(t, self.mark_dur_qe);
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r > 0 {
            self.start_r_channel(e);
        }
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 && self.s.r_channel_until == INF {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_channel_until != INF {
                out[n] = (self.s.r_channel_until, Kind::Ev(EV_R_CHANNEL_END));
                n += 1;
            } else if !self.s.r_active {
                out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R_START));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Physical, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.try_mark(t, self.mark_dur_qe);
            }
            Kind::Ev(EV_R_CHANNEL_END) => {
                self.s.r_channel_until = INF;
                self.s.r_active = true;
            }
            Kind::Ev(EV_R_START) => {
                self.start_r_channel(e);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
