//! Katarina. A caster whose damage is almost entirely her abilities: Bouncing
//! Blade and Shunpo go out on cooldown, Death Lotus is cast the moment it is
//! available and channels through the whole fight blocking attacks and other
//! casts, and Shunpo carries an attack-timer reset. Sinister Steel (walking
//! over dropped daggers) and Preparation (pure movement) are not modeled: see
//! the kit's notes.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Shunpo is cast (and its recast).
const EV_E_CAST: u8 = 0;
/// Death Lotus is cast (after its cooldown, mid-fight).
const EV_R_CAST: u8 = 1;
/// One Death Lotus dagger tick.
const EV_R_TICK: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_dmg: f64,
    q_cd: f64,
    e_dmg: f64,
    e_cd: f64,
    /// Death Lotus per-tick magic damage, per-tick physical damage (from
    /// static bonus AD and bonus attack speed), its cooldown, its tick
    /// interval and total tick count.
    r_magic_dmg: f64,
    r_phys_dmg: f64,
    r_cd: f64,
    r_tick_interval: f64,
    r_ticks_total: i64,
    src_r_magic: SourceId,
    src_r_phys: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    e_ready: f64,
    r_ready: f64,
    r_active: bool,
    r_ticks_done: i64,
    r_next_tick_at: f64,
}

impl GenDriver {
    /// Starts (or restarts) the Death Lotus channel: its cooldown begins on
    /// cast, and attacks/other casts are held for its full duration.
    fn start_r_channel(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        self.s.r_active = true;
        self.s.r_ticks_done = 0;
        self.s.r_next_tick_at = t;
        let channel_end = t + self.r_ticks_total as f64 * self.r_tick_interval;
        e.st.next_attack = pymax(e.st.next_attack, channel_end);
        e.ability_cast_proc();
        e.prime_spellblade();
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let bonus_ad = sheet.ad_bonus;
        let bonus_as_pct = sheet.bonus_as_pct;
        let ad_ratio = kit.num("gen.R.adRatio")?;
        let as_ratio_per_100 = kit.num("gen.R.asRatioPer100Pct")?;
        let r_tick_interval = kit.num("gen.R.tickIntervalS")?;
        let r_duration_s = kit.num("gen.R.durationS")?;
        let state = State {
            e_ready: 0.0,
            r_ready: 0.0,
            r_active: false,
            r_ticks_done: 0,
            r_next_tick_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("katarina kit needs attack.windupFraction")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_magic_dmg: kit.hit("gen.R.magicDamage", ranks.r, sheet)?,
            r_phys_dmg: bonus_ad * (ad_ratio + as_ratio_per_100 * (bonus_as_pct / 100.0)),
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_tick_interval,
            r_ticks_total: (r_duration_s / r_tick_interval) as i64,
            src_r_magic: intern("R magic"),
            src_r_phys: intern("R physical"),
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

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 || self.s.r_active {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        self.start_r_channel(e);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 && !self.s.r_active {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_active {
                out[n] = (self.s.r_next_tick_at, Kind::Ev(EV_R_TICK));
            } else {
                out[n] = (pymax(self.s.r_ready, e.st.t), Kind::Ev(EV_R_CAST));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
                // Shunpo resets the basic attack timer on landing.
                let b = self.bonus_as(t);
                e.st.next_attack = pymax(e.st.next_attack, t + e.attack_windup(b, self.windup_fraction));
            }
            Kind::Ev(EV_R_CAST) => {
                self.start_r_channel(e);
            }
            Kind::Ev(EV_R_TICK) => {
                e.deal(self.r_magic_dmg, DType::Magic, self.src_r_magic, false, true, 1.0);
                e.deal(self.r_phys_dmg, DType::Physical, self.src_r_phys, false, true, 1.0);
                e.eclipse_hit();
                e.ult_hatefog();
                self.s.r_ticks_done += 1;
                if self.s.r_ticks_done >= self.r_ticks_total {
                    self.s.r_active = false;
                    self.s.r_next_tick_at = INF;
                } else {
                    self.s.r_next_tick_at = t + self.r_tick_interval;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
