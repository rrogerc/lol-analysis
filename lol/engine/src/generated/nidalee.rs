//! Nidalee. The opening casts Primal Surge (Human E) on herself for its
//! attack-speed buff, then Javelin Toss (assumed at max travel distance,
//! Human Q) nukes and applies Hunted; since Hunted resets Aspect of the
//! Cougar's cooldown, she swaps into Cougar Form for free and stays there.
//! Takedown arms the next basic attack (post-effect cooldown, attack-timer
//! reset); once it resolves, Pounce and Swipe are unlocked and thereafter
//! recast on their own cooldowns. Aspect of the Cougar (R) itself deals no
//! damage (it only sets the rank Cougar Form's abilities scale at), so
//! Takedown/Pounce/Swipe's damage is credited under their own native slots:
//! "Q takedown", "W pounce", "E swipe". Every cast serializes through one
//! busy_until: it reports castable_at = max(ready, now, busy_until), and a
//! cast with a real cast time extends busy_until and holds the next attack
//! to it. Javelin Toss deliberately waits for Primal Surge (its q_at reports
//! INF until Surge has been cast), and Pounce/Swipe are not unlocked until
//! (Takedown's resolution time + their own cast time), so no two of them
//! ever land in the same instant.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Primal Surge, cast once at the opening (Human Form only).
const EV_SURGE: u8 = 0;
/// Pounce, cast on cooldown once unlocked (Cougar Form).
const EV_POUNCE: u8 = 1;
/// Swipe, cast on cooldown once unlocked (Cougar Form).
const EV_SWIPE: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Hunted mark duration (Prowl).
    hunted_dur: f64,
    /// Javelin Toss's damage, already scaled by the assumed max-distance bonus.
    q_human_dmg: f64,
    /// Javelin Toss's own cooldown and cast time.
    q_human_cd: f64,
    q_human_cast_s: f64,
    /// Takedown's own scaling bonus, before the missing-health and Hunted multipliers.
    q_cougar_dmg: f64,
    /// Takedown's missing-health damage-amp coefficient at Aspect of the Cougar's rank.
    q_cougar_amp: f64,
    /// Takedown's Hunted damage bonus, as a fraction.
    q_cougar_hunted_bonus: f64,
    /// Takedown's own (post-effect) cooldown and cast time.
    q_cougar_cd: f64,
    q_cougar_cast_s: f64,
    /// Pounce's impact damage, cooldown and cast time.
    w_dmg: f64,
    w_cd: f64,
    w_cast_s: f64,
    /// Swipe's damage and cooldown.
    e_cougar_dmg: f64,
    e_cougar_cd: f64,
    /// Shared cast time for Primal Surge and Swipe (both 0.25 s per their own entries).
    e_cast_s: f64,
    /// Primal Surge's bonus attack speed (percent) and buff duration.
    e_surge_as_pct: f64,
    e_surge_dur: f64,
    /// Cougar Form's abilities are credited under their own native slot.
    src_q_takedown: SourceId,
    src_w_pounce: SourceId,
    src_e_swipe: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    /// True while still in Human Form (before the opening Javelin swaps her).
    human: bool,
    /// Primal Surge has been cast (gates Javelin Toss until it has).
    surge_cast: bool,
    /// Takedown is armed, waiting for the next basic attack to consume it.
    q_armed: bool,
    /// Set when Takedown just paid out: the next schedule_attack resets the timer.
    q_reset_pending: bool,
    /// When the Hunted mark from the opening Javelin Toss expires.
    hunted_until: f64,
    /// When Primal Surge's attack-speed buff expires.
    e_as_until: f64,
    /// Pounce and Swipe's ready times; INF until Takedown's first resolution unlocks them.
    w_ready: f64,
    e_ready: f64,
}

impl GenDriver {
    /// The earliest a cast readied at `ready` can start: not before now, and
    /// not inside another cast.
    fn castable_at(&self, e: &Engine, ready: f64) -> f64 {
        pymax(pymax(ready, e.st.t), self.s.busy_until)
    }

    /// A cast with a cast time just started (0 for one with none): no other
    /// cast and no attack until it ends (an attack already due later keeps
    /// its time).
    fn busy_for(&mut self, e: &mut Engine, cast_s: f64) {
        self.s.busy_until = e.st.t + cast_s;
        e.st.next_attack = pymax(e.st.next_attack, self.s.busy_until);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            busy_until: 0.0,
            human: true,
            surge_cast: false,
            q_armed: false,
            q_reset_pending: false,
            hunted_until: -INF,
            e_as_until: -INF,
            w_ready: INF,
            e_ready: INF,
        };
        let range_mult = kit.num("gen.Q.human.rangeMultiplierAssumed")?;
        let q_human_base = kit.hit("gen.Q.human.damage", ranks.q, sheet)?;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("nidalee kit needs attack.windupFraction")?,
            hunted_dur: kit.num("gen.P.huntedDurationS")?,
            q_human_dmg: q_human_base * range_mult,
            q_human_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_human_cast_s: kit.num("gen.Q.castTimeS")?,
            q_cougar_dmg: kit.hit("gen.Q.cougar.damage", ranks.r, sheet)?,
            q_cougar_amp: kit.at_rank("gen.Q.cougar.missingHpAmpCoef", ranks.r)?,
            q_cougar_hunted_bonus: kit.num("gen.Q.cougar.huntedBonusPct")? / 100.0,
            q_cougar_cd: kit.num("gen.Q.cougar.cooldownS")?,
            q_cougar_cast_s: kit.num("gen.Q.cougar.castTimeS")?,
            w_dmg: kit.hit("gen.W.cougar.damage", ranks.r, sheet)?,
            w_cd: kit.num("gen.W.cougar.cooldownS")?,
            w_cast_s: kit.num("gen.W.castTimeS")?,
            e_cougar_dmg: kit.hit("gen.E.cougar.damage", ranks.r, sheet)?,
            e_cougar_cd: kit.num("gen.E.cougar.cooldownS")?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            e_surge_as_pct: kit.at_rank("gen.E.human.bonusAsPct", ranks.e)? * 100.0,
            e_surge_dur: kit.num("gen.E.human.durationS")?,
            src_q_takedown: intern("Q takedown"),
            src_w_pounce: intern("W pounce"),
            src_e_swipe: intern("E swipe"),
            s: state,
            s0: state,
        })
    }

    fn reset(&mut self) {
        self.s = self.s0;
    }

    fn ranged(&self) -> bool {
        true
    }

    fn attack_range(&self) -> f64 {
        self.attack_range
    }

    fn bonus_as(&self, t: f64) -> f64 {
        if t < self.s.e_as_until {
            self.e_surge_as_pct
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
        // Takedown pays out on the next attack after it is armed.
        if self.s.q_armed {
            self.s.q_armed = false;
            let t = e.st.t;
            let missing_frac = (e.target_hp - pymax(e.st.hp, 0.0)) / e.target_hp;
            let mut mult = 1.0 + missing_frac * self.q_cougar_amp;
            if t < self.s.hunted_until {
                mult *= 1.0 + self.q_cougar_hunted_bonus;
            }
            let dmg = self.q_cougar_dmg * mult;
            e.deal(dmg, DType::Magic, self.src_q_takedown, false, true, 1.0);
            e.st.q_ready = t + e.basic_cd(self.q_cougar_cd);
            self.s.q_reset_pending = true;
            // Pounce and Swipe are not unlocked until this empowered attack
            // has resolved, each at (now + its own cast time), so neither
            // lands in the same instant as this attack or each other.
            if self.ranks.w > 0 && self.s.w_ready == INF {
                self.s.w_ready = t + self.w_cast_s;
            }
            if self.ranks.e > 0 && self.s.e_ready == INF {
                self.s.e_ready = t + self.e_cast_s;
            }
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.s.q_reset_pending {
            // Takedown resets Nidalee's basic attack timer.
            self.s.q_reset_pending = false;
            e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
        } else {
            e.st.next_attack = t + e.attack_period(b);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if self.s.human {
            if self.ranks.e > 0 && !self.s.surge_cast {
                // Primal Surge opens the fight; Javelin Toss waits for it.
                return INF;
            }
            return self.castable_at(e, e.st.q_ready);
        }
        if self.s.q_armed {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.human {
            // Javelin Toss.
            e.deal(self.q_human_dmg, DType::Magic, SRC_Q, false, true, 1.0);
            e.ability_cast_proc();
            e.eclipse_hit();
            e.prime_spellblade();
            self.s.hunted_until = t + self.hunted_dur;
            e.st.q_ready = t + e.basic_cd(self.q_human_cd);
            if self.ranks.r > 0 {
                // The Hunted mark just applied resets Aspect of the Cougar's
                // cooldown: swap into Cougar Form for free, Takedown ready now.
                self.s.human = false;
                e.st.q_ready = t;
            }
            self.busy_for(e, self.q_human_cast_s);
        } else {
            // Takedown: arm the next basic attack. No cast time of its own.
            self.s.q_armed = true;
            e.prime_spellblade();
            self.busy_for(e, self.q_cougar_cast_s);
        }
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 && self.s.human && !self.s.surge_cast {
            out[n] = (self.castable_at(e, 0.0), Kind::Ev(EV_SURGE));
            n += 1;
        }
        if !self.s.human {
            if self.ranks.w > 0 && self.s.w_ready != INF {
                out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_POUNCE));
                n += 1;
            }
            if self.ranks.e > 0 && self.s.e_ready != INF {
                out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_SWIPE));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_SURGE) => {
                // Primal Surge on herself: no damage, just the AS buff, cast
                // before anything else (Javelin Toss waits behind it).
                self.s.surge_cast = true;
                self.s.e_as_until = t + self.e_surge_dur;
                e.prime_spellblade();
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_POUNCE) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Magic, self.src_w_pounce, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.w_cast_s);
            }
            Kind::Ev(EV_SWIPE) => {
                self.s.e_ready = t + e.basic_cd(self.e_cougar_cd);
                e.deal(self.e_cougar_dmg, DType::Magic, self.src_e_swipe, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.e_cast_s);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
