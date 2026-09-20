//! Gragas. Explosive Cask opens the fight; Barrel Roll is thrown and always
//! held to its 2 s fermentation cap before being detonated for maximum
//! damage; Drunken Rage is cast on cooldown and its channel-empowered attack
//! is consumed by the next basic attack; Body Slam is cast on cooldown and
//! always gets its 40% current-cooldown refund since it always connects.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Barrel Roll's detonation, once fully fermented.
const EV_Q_DETONATE: u8 = 0;
/// Drunken Rage: the cast that starts the channel, and the channel's end.
const EV_W_CAST: u8 = 1;
const EV_W_CHANNEL_END: u8 = 2;
/// Body Slam is cast on cooldown.
const EV_E_CAST: u8 = 3;
/// Explosive Cask's impact, after its cast time and travel.
const EV_R_IMPACT: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    q_dmg_max: f64,
    q_cd: f64,
    q_ferment_s: f64,
    q_travel_s: f64,

    w_dmg_base: f64,
    w_target_hp_ratio: f64,
    w_cd: f64,
    w_channel_s: f64,
    w_reduction_s: f64,
    w_attack_window_s: f64,

    e_dmg: f64,
    e_cd: f64,
    e_refund_frac: f64,

    r_dmg: f64,
    r_cast_s: f64,
    r_travel_s: f64,

    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The thrown cask is out and cannot be re-thrown until detonated.
    q_pending: bool,
    /// When it will be detonated (INF: none pending).
    q_detonate_at: f64,

    w_ready: f64,
    /// The channel's end, while it runs (INF otherwise).
    w_channel_end_at: f64,
    /// The next basic attack is empowered, until this time.
    w_empowered: bool,
    w_empowered_until: f64,

    e_ready: f64,

    /// Explosive Cask's impact, still to land (INF once it has).
    r_impact_at: f64,
}

impl Driver for GenDriver {
    fn new(
        kit: &Kit,
        sheet: &Sheet,
        _level: i64,
        ranks: Ranks,
        _prestacked: bool,
    ) -> Result<Self, String> {
        let state = State {
            q_pending: false,
            q_detonate_at: INF,
            w_ready: 0.0,
            w_channel_end_at: INF,
            w_empowered: false,
            w_empowered_until: 0.0,
            e_ready: 0.0,
            r_impact_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit
                .windup_fraction
                .ok_or("gragas kit needs attack.windupFraction")?,

            q_dmg_max: kit.hit("gen.Q.damageMax", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_ferment_s: kit.num("gen.Q.fermentS")?,
            q_travel_s: kit.num("gen.Q.travelS")?,

            w_dmg_base: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_target_hp_ratio: kit.num("gen.W.targetMaxHpRatio")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_channel_s: kit.num("gen.W.channelS")?,
            w_reduction_s: kit.num("gen.W.reductionDurationS")?,
            w_attack_window_s: kit.num("gen.W.attackWindowS")?,

            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_refund_frac: kit.num("gen.E.cooldownRefundPct")? / 100.0,

            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_travel_s: kit.num("gen.R.travelS")?,

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

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.w_empowered {
            self.s.w_empowered = false;
            if e.st.t <= self.s.w_empowered_until {
                let dmg = self.w_dmg_base + self.w_target_hp_ratio * e.target_hp;
                e.deal(dmg, DType::Magic, SRC_W, true, false, 1.0);
            }
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 || self.s.q_pending {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        self.s.q_pending = true;
        let land_at = t + self.q_travel_s;
        self.s.q_detonate_at = land_at + self.q_ferment_s;
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.r_impact_at = t + self.r_cast_s + self.r_travel_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_detonate_at != INF {
            out[n] = (self.s.q_detonate_at, Kind::Ev(EV_Q_DETONATE));
            n += 1;
        }
        if self.ranks.w > 0 {
            if self.s.w_channel_end_at != INF {
                out[n] = (self.s.w_channel_end_at, Kind::Ev(EV_W_CHANNEL_END));
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
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
            Kind::Ev(EV_Q_DETONATE) => {
                // manual detonation does not count as an on-cast activation
                self.s.q_pending = false;
                self.s.q_detonate_at = INF;
                e.deal(self.q_dmg_max, DType::Magic, SRC_Q, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_channel_end_at = t + self.w_channel_s;
                e.st.next_attack = pymax(e.st.next_attack, self.s.w_channel_end_at);
                e.prime_spellblade();
            }
            Kind::Ev(EV_W_CHANNEL_END) => {
                self.s.w_channel_end_at = INF;
                self.s.w_empowered = true;
                self.s.w_empowered_until = t + self.w_attack_window_s;
                self.s.w_ready = t + self.w_reduction_s + e.basic_cd(self.w_cd);
            }
            Kind::Ev(EV_E_CAST) => {
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                let base_cd = e.basic_cd(self.e_cd);
                self.s.e_ready = t + base_cd * (1.0 - self.e_refund_frac);
            }
            Kind::Ev(EV_R_IMPACT) => {
                self.s.r_impact_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
