//! Nunu & Willump. An ability-driven rotation: Absolute Zero opens the fight
//! and is channeled to full duration, Biggest Snowball Ever! is channeled to
//! its damage cap then released, Snowball Barrage fires all three volleys
//! back-to-back plus its delayed Snowbound root tick, and Consume (a 0.3s
//! cast) fills the gaps. Call of the Freljord grants a brief attack-speed
//! buff whenever damage lands on the dummy.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Absolute Zero's explosion, once the 3s channel ends.
const EV_R_DAMAGE: u8 = 0;
/// Biggest Snowball Ever!: the channel starting, then its release/explosion.
const EV_W_START: u8 = 1;
const EV_W_RELEASE: u8 = 2;
/// Snowball Barrage: each volley (initial cast and the two free recasts).
const EV_E_VOLLEY: u8 = 3;
/// Snowball Barrage's delayed Snowbound root damage.
const EV_E_ROOT: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Call of the Freljord: bonus AS while active, base/cap duration, retrigger cooldown.
    p_as_pct: f64,
    p_dur: f64,
    p_max_dur: f64,
    p_retrigger_cd: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    w_dmg: f64,
    w_cd: f64,
    w_max_time: f64,
    e_volley_dmg: f64,
    e_root_dmg: f64,
    e_cd: f64,
    e_recast_raw: f64,
    e_root_delay: f64,
    e_max_bursts: i64,
    r_dmg: f64,
    r_channel: f64,
    src_e_root: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// A cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    /// Call of the Freljord: current buff expiry and next time it may retrigger.
    p_buff_until: f64,
    p_next_ok: f64,
    /// Absolute Zero: when its explosion lands (INF: none pending).
    r_damage_at: f64,
    /// Biggest Snowball Ever!
    w_ready: f64,
    w_channeling: bool,
    w_release_at: f64,
    /// Snowball Barrage.
    e_ready: f64,
    e_seq_active: bool,
    e_first_cast_at: f64,
    e_volleys_done: i64,
    e_next_volley_at: f64,
    e_root_at: f64,
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

    /// Call of the Freljord triggers on any damage dealt to the dummy,
    /// subject to its per-target retrigger cooldown.
    fn trigger_passive(&mut self, t: f64) {
        if t >= self.s.p_next_ok {
            let remaining = self.s.p_buff_until - t;
            let new_remaining = if remaining > 0.0 {
                pymin(remaining + self.p_dur, self.p_max_dur)
            } else {
                self.p_dur
            };
            self.s.p_buff_until = t + new_remaining;
            self.s.p_next_ok = t + self.p_retrigger_cd;
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            busy_until: 0.0,
            p_buff_until: 0.0,
            p_next_ok: 0.0,
            r_damage_at: INF,
            w_ready: 0.0,
            w_channeling: false,
            w_release_at: INF,
            e_ready: 0.0,
            e_seq_active: false,
            e_first_cast_at: 0.0,
            e_volleys_done: 0,
            e_next_volley_at: INF,
            e_root_at: INF,
        };
        let _ = level;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("nunu kit needs attack.windupFraction")?,
            p_as_pct: kit.num("gen.P.asIncrease")? * 100.0,
            p_dur: kit.num("gen.P.buffDurationS")?,
            p_max_dur: kit.num("gen.P.maxRemainingDurationS")?,
            p_retrigger_cd: kit.num("gen.P.retriggerCooldownS")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_max_time: kit.num("gen.W.maxDamageTimeS")?,
            e_volley_dmg: kit.hit("gen.E.volleyDamage", ranks.e, sheet)?,
            e_root_dmg: kit.hit("gen.E.rootDamage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_recast_raw: kit.num("gen.E.recastIntervalS")?,
            e_root_delay: kit.num("gen.E.rootDelayS")?,
            e_max_bursts: kit.num("gen.E.maxBursts")? as i64,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_channel: kit.num("gen.R.channelDurationS")?,
            src_e_root: intern("E root"),
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
        if t < self.s.p_buff_until {
            self.p_as_pct
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
        // every attack lands on the dummy (an enemy champion), so it can
        // always trigger Call of the Freljord
        self.trigger_passive(e.st.t);
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
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.trigger_passive(t);
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        let end = t + self.r_channel;
        self.s.busy_until = pymax(self.s.busy_until, end);
        e.st.next_attack = pymax(e.st.next_attack, end);
        self.s.r_damage_at = end;
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.r_damage_at != INF {
            out[n] = (self.s.r_damage_at, Kind::Ev(EV_R_DAMAGE));
            n += 1;
        }
        if self.ranks.w > 0 {
            if self.s.w_channeling {
                out[n] = (self.s.w_release_at, Kind::Ev(EV_W_RELEASE));
            } else {
                out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_START));
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_seq_active {
                out[n] = (self.s.e_next_volley_at, Kind::Ev(EV_E_VOLLEY));
            } else {
                out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_VOLLEY));
            }
            n += 1;
            if self.s.e_root_at != INF {
                out[n] = (self.s.e_root_at, Kind::Ev(EV_E_ROOT));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_DAMAGE) => {
                self.s.r_damage_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
                self.trigger_passive(t);
            }
            Kind::Ev(EV_W_START) => {
                self.s.w_channeling = true;
                let end = t + self.w_max_time;
                self.s.w_release_at = end;
                self.s.busy_until = pymax(self.s.busy_until, end);
                e.st.next_attack = pymax(e.st.next_attack, end);
                e.prime_spellblade();
            }
            Kind::Ev(EV_W_RELEASE) => {
                self.s.w_channeling = false;
                self.s.w_release_at = INF;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.trigger_passive(t);
            }
            Kind::Ev(EV_E_VOLLEY) => {
                if !self.s.e_seq_active {
                    self.s.e_seq_active = true;
                    self.s.e_first_cast_at = t;
                    self.s.e_volleys_done = 0;
                    self.s.e_root_at = t + self.e_root_delay;
                    let span = e.basic_cd(self.e_recast_raw) * ((self.e_max_bursts - 1) as f64);
                    self.s.busy_until = pymax(self.s.busy_until, t + span);
                }
                self.s.e_volleys_done += 1;
                e.deal(self.e_volley_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.trigger_passive(t);
                if self.s.e_volleys_done < self.e_max_bursts {
                    self.s.e_next_volley_at = t + e.basic_cd(self.e_recast_raw);
                } else {
                    self.s.e_seq_active = false;
                    self.s.e_next_volley_at = INF;
                    self.s.e_ready = t + e.basic_cd(self.e_cd);
                }
            }
            Kind::Ev(EV_E_ROOT) => {
                self.s.e_root_at = INF;
                e.deal(self.e_root_dmg, DType::Magic, self.src_e_root, false, true, 1.0);
                self.trigger_passive(t);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
