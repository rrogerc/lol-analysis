//! Caitlyn. An auto-attacker whose Headshot passive rides her attacks: every
//! 5th attack (no brush assumed) is empowered with bonus physical damage
//! scaling off AD and the build's crit chance/damage. Piltover Peacemaker
//! and 90 Caliber Net go out on cooldown, both as single-target full-damage
//! instances since the fight has only one target. Ace in the Hole opens the
//! fight, channels for 1 s, then fires. Yordle Snap Trap is cast once, its
//! trap placed on the dummy's own stationary location so it springs the
//! instant it arms, granting its bonus Headshot damage as a single instance.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// 90 Caliber Net is cast on cooldown; Ace in the Hole's channel ends and its
/// bullet lands; Yordle Snap Trap is placed once and its bonus damage lands
/// when it arms and springs.
const EV_E_CAST: u8 = 0;
const EV_R_CAST: u8 = 1;
const EV_R_FIRE: u8 = 2;
const EV_W_CAST: u8 = 3;
const EV_W_SPRING: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Headshot: attacks needed to charge it, and its per-hit bonus-damage
    /// coefficient (level term plus the crit chance/damage expected-value
    /// term), multiplied by AD at the moment it lands.
    p_threshold: i64,
    p_headshot_coef: f64,
    q_dmg: f64,
    q_cd: f64,
    w_dmg: f64,
    w_arm_s: f64,
    /// When Yordle Snap Trap's single cast is placed: right when Ace in the
    /// Hole's channel ends, or at t=0 if the ult is not ranked.
    w_cast_at: f64,
    e_dmg: f64,
    e_cd: f64,
    /// Ace in the Hole's damage, already folded with its crit-scaling term.
    r_dmg: f64,
    r_cd: f64,
    r_channel_s: f64,
    src_headshot: SourceId,
    src_w: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    p_stacks: i64,
    p_headshot_pending: bool,
    e_ready: f64,
    /// Ace in the Hole: when it may next be cast, and when its pending
    /// channel's bullet lands (INF: no channel pending).
    r_ready: f64,
    r_fire_at: f64,
    /// Yordle Snap Trap: still pending its single cast, and when its bonus
    /// damage lands once placed (INF: nothing pending).
    w_cast_pending: bool,
    w_spring_at: f64,
}

impl GenDriver {
    /// Starts (or restarts) Ace in the Hole's channel: holds attacks and the
    /// other basics until it ends.
    fn start_r_channel(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.r_fire_at = t + self.r_channel_s;
        e.st.next_attack = pymax(e.st.next_attack, self.s.r_fire_at);
        e.st.q_ready = pymax(e.st.q_ready, self.s.r_fire_at);
        self.s.e_ready = pymax(self.s.e_ready, self.s.r_fire_at);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let crit_chance = sheet.crit_chance / 100.0;
        let crit_damage = sheet.crit_damage / 100.0;
        let level_term = kit.at_level("gen.P.headshotAdRatioByLevel", level)?;
        let r_base = kit.hit("gen.R.damage", ranks.r, sheet)?;
        let r_crit_coef = kit.num("gen.R.critChanceCoef")?;
        let r_mult = 1.0 + r_crit_coef * crit_chance * (crit_damage - 1.0);
        let r_channel_s = kit.num("gen.R.channelDurationS")?;
        let w_cast_at = if ranks.r > 0 { r_channel_s } else { 0.0 };
        let state = State {
            p_stacks: 0,
            p_headshot_pending: false,
            e_ready: 0.0,
            r_ready: INF,
            r_fire_at: INF,
            w_cast_pending: ranks.w > 0,
            w_spring_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("caitlyn kit needs attack.windupFraction")?,
            p_threshold: kit.num("gen.P.attacksPerHeadshot")? as i64,
            p_headshot_coef: level_term + crit_chance * (crit_damage - 1.0),
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_arm_s: kit.num("gen.W.armTimeS")?,
            w_cast_at,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_dmg: r_base * r_mult,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_channel_s,
            src_headshot: intern("P headshot"),
            src_w: intern("W"),
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

    fn bonus_as(&self, _t: f64) -> f64 {
        0.0
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, _e: &mut Engine) {
        // Headshot: stacks on-attack; at the threshold the attack about to
        // land consumes the stacks and becomes a Headshot instead.
        if self.s.p_stacks >= self.p_threshold - 1 {
            self.s.p_headshot_pending = true;
            self.s.p_stacks = 0;
        } else {
            self.s.p_headshot_pending = false;
            self.s.p_stacks += 1;
        }
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.p_headshot_pending {
            self.s.p_headshot_pending = false;
            let amt = self.p_headshot_coef * e.p.ad;
            e.deal(amt, DType::Physical, self.src_headshot, false, false, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
        // Piltover Peacemaker resets Caitlyn's attack timer on hit: the next
        // attack becomes available one windup after this cast.
        let t = e.st.t;
        let b = self.bonus_as(t);
        e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        self.start_r_channel(e);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_fire_at != INF {
                out[n] = (self.s.r_fire_at, Kind::Ev(EV_R_FIRE));
                n += 1;
            } else if self.s.r_ready != INF {
                out[n] = (pymax(self.s.r_ready, e.st.t), Kind::Ev(EV_R_CAST));
                n += 1;
            }
        }
        if self.ranks.w > 0 {
            if self.s.w_cast_pending {
                out[n] = (pymax(self.w_cast_at, e.st.t), Kind::Ev(EV_W_CAST));
                n += 1;
            } else if self.s.w_spring_at != INF {
                out[n] = (self.s.w_spring_at, Kind::Ev(EV_W_SPRING));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        match kind {
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = e.st.t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_R_CAST) => {
                self.s.r_ready = INF;
                self.start_r_channel(e);
            }
            Kind::Ev(EV_R_FIRE) => {
                self.s.r_fire_at = INF;
                e.deal(self.r_dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                self.s.r_ready = e.st.t + e.ult_cd(self.r_cd);
            }
            Kind::Ev(EV_W_CAST) => {
                // Placed directly on the dummy's stationary location: it
                // arms and springs without needing anyone to walk onto it.
                self.s.w_cast_pending = false;
                self.s.w_spring_at = e.st.t + self.w_arm_s;
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_W_SPRING) => {
                self.s.w_spring_at = INF;
                e.deal(self.w_dmg, DType::Physical, self.src_w, false, false, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
