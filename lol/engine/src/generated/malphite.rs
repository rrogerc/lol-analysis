//! Malphite. Unstoppable Force opens (and recasts on cooldown), Seismic
//! Shard and Ground Slam go out on cooldown, and Thunderclap is cast on
//! cooldown to arm its empowered on-hit and cone-splash windows: the next
//! attack takes the empowered bonus and resets the attack timer, and every
//! attack for 5 s (including that one) also gets the cone. Malphite does one
//! thing at a time: Seismic Shard and Ground Slam have real cast times that
//! keep him busy (tracked in one `busy_until`), while Unstoppable Force (a
//! dash, so it locks out the next attack) and Thunderclap (a pure
//! attack-modifier) have none but still cannot start inside another cast.
//! Thunderclap's own passive bonus armor is assumed permanently tripled
//! (Granite Shield never breaks against a dummy that never attacks) and read
//! recursively, feeding its own on-hit/cone and Ground Slam's armor ratio.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Thunderclap is cast on cooldown (arms its windows).
const EV_W: u8 = 0;
/// Ground Slam is cast on cooldown.
const EV_E: u8 = 1;
/// Unstoppable Force is recast on cooldown (t=0's cast is in `cast_r`).
const EV_R: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    w_onhit_dmg: f64,
    w_cone_dmg: f64,
    w_cd: f64,
    w_arm_window: f64,
    w_cone_window: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cast_s: f64,
    r_dmg: f64,
    r_cd: f64,
    src_w_onhit: SourceId,
    src_w_cone: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    w_ready: f64,
    e_ready: f64,
    r_ready: f64,
    /// Whether the next basic attack still owes Thunderclap's empowered
    /// on-hit bonus, and until when.
    w_armed: bool,
    w_arm_until: f64,
    /// Until when a basic attack also gets the cone-splash on-hit.
    w_cone_until: f64,
    /// Set when this attack consumed the empowerment: `schedule_attack`
    /// then resets the attack timer instead of using the full period.
    reset_pending: bool,
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
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let w_rank_frac = kit.at_rank("gen.W.armPctByRank", ranks.w)?;
        let triple_mult = kit.num("gen.W.tripleMult")?;
        let k = w_rank_frac * triple_mult;
        let armor_total = sheet.armor / (1.0 - k);

        let state = State {
            busy_until: 0.0,
            w_ready: 0.0,
            e_ready: 0.0,
            r_ready: 0.0,
            w_armed: false,
            w_arm_until: 0.0,
            w_cone_until: 0.0,
            reset_pending: false,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("malphite kit needs attack.windupFraction")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_onhit_dmg: kit.hit("gen.W.onhit", ranks.w, sheet)? + kit.num("gen.W.onhitArmorRatio")? * armor_total,
            w_cone_dmg: kit.hit("gen.W.cone", ranks.w, sheet)? + kit.num("gen.W.coneArmorRatio")? * armor_total,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_arm_window: kit.num("gen.W.armWindowS")?,
            w_cone_window: kit.num("gen.W.coneWindowS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)? + kit.num("gen.E.armorRatio")? * armor_total,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            src_w_onhit: intern("W onhit"),
            src_w_cone: intern("W cone"),
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
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.ranks.w == 0 {
            return;
        }
        let t = e.st.t;
        let mut reset = false;
        if self.s.w_armed {
            if t <= self.s.w_arm_until {
                self.s.w_armed = false;
                e.deal(self.w_onhit_dmg, DType::Physical, self.src_w_onhit, false, false, 1.0);
                reset = true;
            } else {
                // the arm window lapsed before an attack could consume it
                self.s.w_armed = false;
            }
        }
        if t <= self.s.w_cone_until {
            e.deal(self.w_cone_dmg, DType::Physical, self.src_w_cone, false, false, 1.0);
        }
        if reset {
            self.s.reset_pending = true;
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.s.reset_pending {
            // Thunderclap's empowered attack resets Malphite's attack timer
            self.s.reset_pending = false;
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
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.ult_hatefog();
        // a dash with no cast time: it lands with the cast, but still holds
        // the next attack back
        e.lockout();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E));
            n += 1;
        }
        if self.ranks.r > 0 {
            out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                // a pure attack-modifier: no cast time, delays nothing of
                // its own, but still could not start inside another cast
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.s.w_armed = true;
                self.s.w_arm_until = t + self.w_arm_window;
                self.s.w_cone_until = t + self.w_cone_window;
                e.prime_spellblade();
            }
            Kind::Ev(EV_E) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_R) => {
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
                e.lockout();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
