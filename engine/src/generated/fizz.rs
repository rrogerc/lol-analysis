//! Fizz. Opens with Chum the Waters thrown to max range for the strongest
//! shark tier, then rotates Urchin Strike on cooldown (magic + physical on
//! landing), Seastone Trident armed after every attack for its empowered
//! hit and 5s on-hit window, its passive bleed refreshing on every basic
//! attack, and Playful canceled into Trickster for the same splash damage
//! on a shorter timer.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// The passive bleed's periodic tick.
const EV_W_BLEED_TICK: u8 = 0;
/// Playful is cast (instant); its damage lands after the Trickster cancel delay.
const EV_E_CAST: u8 = 1;
const EV_E_DAMAGE: u8 = 2;
/// Chum the Waters' shark erupts after the cast, travel and detonation delay.
const EV_R_ERUPT: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    q_magic: f64,
    q_phys: f64,
    q_cd: f64,

    w_bleed_total: f64,
    w_active: f64,
    w_onhit: f64,
    w_cd: f64,
    w_tick_interval: f64,
    w_ticks_count: i64,
    w_onhit_buff_dur: f64,

    e_dmg: f64,
    e_cd: f64,
    e_trick_delay: f64,
    e_q_lockout: f64,

    r_dmg: f64,
    r_cast_s: f64,
    r_detonate_s: f64,
    r_travel_s: f64,

    src_w_active: SourceId,
    src_w_bleed: SourceId,
    src_w_onhit: SourceId,
    src_e: SourceId,

    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    w_ready: f64,
    w_armed: bool,
    w_onhit_buff_until: f64,
    bleed_next_tick: f64,
    bleed_ticks_left: i64,
    e_ready: f64,
    e_q_lockout_until: f64,
    e_pending_damage_at: f64,
    r_erupt_at: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let r_travel_s = kit.num("gen.R.maxDistance")? / kit.num("gen.R.missileSpeed")?;
        let state = State {
            w_ready: 0.0,
            w_armed: false,
            w_onhit_buff_until: -1.0,
            bleed_next_tick: INF,
            bleed_ticks_left: 0,
            e_ready: 0.0,
            e_q_lockout_until: 0.0,
            e_pending_damage_at: INF,
            r_erupt_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("fizz kit needs attack.windupFraction")?,
            q_magic: kit.hit("gen.Q.magicDamage", ranks.q, sheet)?,
            q_phys: kit.hit("gen.Q.physicalDamage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_bleed_total: kit.hit("gen.W.bleed", ranks.w, sheet)?,
            w_active: kit.hit("gen.W.active", ranks.w, sheet)?,
            w_onhit: kit.hit("gen.W.onhitBuff", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_tick_interval: kit.num("gen.W.tickIntervalS")?,
            w_ticks_count: kit.num("gen.W.ticksCount")? as i64,
            w_onhit_buff_dur: kit.num("gen.W.onHitBuffDurationS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_trick_delay: kit.num("gen.E.trickCastDelayS")?,
            e_q_lockout: kit.num("gen.E.qLockoutS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_detonate_s: kit.num("gen.R.detonationDelayS")?,
            r_travel_s,
            src_w_active: intern("W"),
            src_w_bleed: intern("W bleed"),
            src_w_onhit: intern("W onhit"),
            src_e: intern("E"),
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
        if self.s.e_ready != INF {
            shave(&mut self.s.e_ready, t, factor);
        }
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        let t = e.st.t;
        // the bleed refreshes (restarts its 6 ticks) on every hit
        self.s.bleed_next_tick = t + self.w_tick_interval;
        self.s.bleed_ticks_left = self.w_ticks_count;
        if t < self.s.w_onhit_buff_until {
            e.deal(self.w_onhit, DType::Magic, self.src_w_onhit, false, false, 1.0);
        }
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.w_armed {
            self.s.w_armed = false;
            e.deal(self.w_active, DType::Magic, self.src_w_active, false, true, 1.0);
            self.s.w_onhit_buff_until = e.st.t + self.w_onhit_buff_dur;
            self.s.w_ready = e.st.t + e.basic_cd(self.w_cd);
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.ranks.w > 0 && !self.s.w_armed && t >= self.s.w_ready {
            // Seastone Trident arms right after an attack lands and resets
            // the attack timer to a single windup, like an Empower
            self.s.w_armed = true;
            e.prime_spellblade();
            e.ability_cast_proc();
            e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
        } else {
            e.st.next_attack = t + e.attack_period(b);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        e.deal(self.q_magic, DType::Magic, SRC_Q, false, true, 1.0);
        e.deal(self.q_phys, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.s.e_q_lockout_until = t + self.e_q_lockout;
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: thrown to max range for the Gigalodon tier; the
        // shark erupts after the cast, the lure's flight and the detonation delay
        let t = e.st.t;
        self.s.r_erupt_at = t + self.r_cast_s + self.r_travel_s + self.r_detonate_s;
        e.prime_spellblade();
        e.lockout();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.bleed_next_tick != INF {
            out[n] = (self.s.bleed_next_tick, Kind::Ev(EV_W_BLEED_TICK));
            n += 1;
        }
        if self.s.r_erupt_at != INF {
            out[n] = (self.s.r_erupt_at, Kind::Ev(EV_R_ERUPT));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_pending_damage_at != INF {
                out[n] = (self.s.e_pending_damage_at, Kind::Ev(EV_E_DAMAGE));
            } else {
                let ready = pymax(pymax(self.s.e_ready, self.s.e_q_lockout_until), e.st.t);
                out[n] = (ready, Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_BLEED_TICK) => {
                e.deal(
                    self.w_bleed_total / (self.w_ticks_count as f64),
                    DType::Magic,
                    self.src_w_bleed,
                    false,
                    false,
                    1.0,
                );
                self.s.bleed_ticks_left -= 1;
                if self.s.bleed_ticks_left > 0 {
                    self.s.bleed_next_tick = t + self.w_tick_interval;
                } else {
                    self.s.bleed_next_tick = INF;
                }
            }
            Kind::Ev(EV_R_ERUPT) => {
                self.s.r_erupt_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            Kind::Ev(EV_E_CAST) => {
                // Playful is cast now, immediately canceled into Trickster:
                // the wiki says this early cancel is not an ability
                // activation, so no on-cast procs fire here
                self.s.e_pending_damage_at = t + self.e_trick_delay;
                self.s.e_ready = INF;
            }
            Kind::Ev(EV_E_DAMAGE) => {
                self.s.e_pending_damage_at = INF;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, self.src_e, false, true, 1.0);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
