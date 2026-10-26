//! Jax. An auto-attacker whose kit rides his attacks: Relentless Assault
//! stacks attack speed per attack, Empower is woven in after an attack for
//! its reset and rides the next one, Grandmaster-at-Arms opens the fight and
//! puts an on-hit on every third attack (every second while it runs), Leap
//! Strike and Counter Strike go out on cooldown. Casts go one at a time: the
//! ult's cast time keeps Jax busy, and every other cast waits for it.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Counter Strike is cast; then its recast, which deals the damage.
const EV_E_CAST: u8 = 0;
const EV_E_RECAST: u8 = 1;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Relentless Assault: percent attack speed a stack, and the cap.
    p_as_pct: f64,
    p_max: i64,
    q_dmg: f64,
    q_cd: f64,
    w_dmg: f64,
    w_cd: f64,
    e_dmg: f64,
    /// Counter Strike's share of the TARGET's maximum health, as a fraction.
    e_target_hp: f64,
    e_cd: f64,
    e_recast_s: f64,
    r_onhit: f64,
    r_swing: f64,
    r_cast_s: f64,
    r_active_s: f64,
    r_need: i64,
    r_need_active: i64,
    src_w: SourceId,
    src_e: SourceId,
    src_r_onhit: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    p_stacks: i64,
    w_ready: f64,
    w_armed: bool,
    e_ready: f64,
    /// When the pending Counter Strike may be recast (INF: none pending).
    e_recast_at: f64,
    /// Grandmaster-at-Arms: how long it runs, and the on-hit's stacks.
    r_until: f64,
    r_stacks: i64,
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

    /// Empower rides this hit (an attack or a Leap Strike): its own damage
    /// instance, and the cooldown starts now.
    fn spend_empower(&mut self, e: &mut Engine) {
        self.s.w_armed = false;
        self.s.w_ready = e.st.t + e.basic_cd(self.w_cd);
        e.deal(self.w_dmg, DType::Magic, self.src_w, false, true, 1.0);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            busy_until: 0.0,
            p_stacks: 0,
            w_ready: 0.0,
            w_armed: false,
            e_ready: 0.0,
            e_recast_at: INF,
            r_until: -1.0,
            r_stacks: 0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("jax kit needs attack.windupFraction")?,
            p_as_pct: kit.at_level("gen.P.asPerStackByLevel", level)? * 100.0,
            p_max: kit.num("gen.P.maxStacks")? as i64,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_target_hp: kit.num("gen.E.targetMaxHpRatio")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_recast_s: kit.num("gen.E.recastDelayS")?,
            r_onhit: kit.hit("gen.R.onhit", ranks.r, sheet)?,
            r_swing: kit.hit("gen.R.swing", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_active_s: kit.num("gen.R.activeDurationS")?,
            r_need: kit.num("gen.R.stacksNeeded")? as i64,
            r_need_active: kit.num("gen.R.stacksNeededActive")? as i64,
            src_w: intern("W"),
            src_e: intern("E"),
            src_r_onhit: intern("R onhit"),
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
        self.s.p_stacks as f64 * self.p_as_pct
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, _e: &mut Engine) {
        // Relentless Assault stacks on-attack; attacking never lets it lapse
        self.s.p_stacks = imin(self.s.p_stacks + 1, self.p_max);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.w_armed {
            self.spend_empower(e);
        }
        if self.ranks.r > 0 {
            // Grandmaster-at-Arms: at enough stacks this attack consumes them
            // for the on-hit, otherwise it adds one
            let need = if e.st.t < self.s.r_until { self.r_need_active } else { self.r_need };
            if self.s.r_stacks >= need {
                self.s.r_stacks = 0;
                e.deal(self.r_onhit, DType::Magic, self.src_r_onhit, false, false, 1.0);
            } else {
                self.s.r_stacks += 1;
            }
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.ranks.w > 0 && !self.s.w_armed && t >= self.s.w_ready {
            // Empower, woven in right after an attack: its reset brings the
            // next attack one windup away
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
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // a dash with no cast time: it lands with the cast and keeps nothing
        // else waiting, but the leap holds the next attack back
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        if self.s.w_armed {
            self.spend_empower(e);
        }
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast (the engine has primed Spellblade): the swing
        // lands with the cast, which then keeps Jax busy for its cast time
        self.s.r_until = e.st.t + self.r_active_s;
        e.deal(self.r_swing, DType::Magic, SRC_R, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.ult_hatefog();
        self.busy_for(e, self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            // both halves are casts: neither starts inside another cast
            if self.s.e_recast_at != INF {
                out[n] = (self.castable_at(e, self.s.e_recast_at), Kind::Ev(EV_E_RECAST));
            } else {
                out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                // no cast time, so no `busy_for`: Jax keeps attacking through it;
                // the cooldown waits for the recast, so park it until then
                self.s.e_recast_at = t + self.e_recast_s;
                self.s.e_ready = INF;
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_RECAST) => {
                self.s.e_recast_at = INF;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                let amt = self.e_dmg + self.e_target_hp * e.target_hp;
                e.deal(amt, DType::Magic, self.src_e, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
