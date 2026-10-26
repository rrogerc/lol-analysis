//! Ivern. A mixed auto-attacker/caster: Brushmaker is cast once at the
//! opening for a permanent on-hit bolt, Rootcaller and Triggerseed go out on
//! cooldown for their magic damage, and Daisy! summons an independent pet
//! that auto-attacks the dummy on her own timer, firing a shockwave every
//! third consecutive hit. Casts go one at a time: Rootcaller, Brushmaker and
//! Daisy! each have a cast time that keeps Ivern busy; Triggerseed has none
//! but still waits for a cast in progress.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Brushmaker's one-time opening cast (creates the brush Ivern stands in).
const EV_W_CAST: u8 = 0;
/// Triggerseed is cast; then its explosion, 2 s later, which deals damage.
const EV_E_CAST: u8 = 1;
const EV_E_EXPLODE: u8 = 2;
/// Daisy's own recurring basic attack.
const EV_DAISY_ATTACK: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    w_dmg: f64,
    w_cast_s: f64,
    e_dmg: f64,
    e_cd: f64,
    e_explode_delay: f64,
    daisy_ad: f64,
    daisy_period: f64,
    shock_dmg: f64,
    r_cast_s: f64,
    src_w: SourceId,
    src_e: SourceId,
    src_daisy: SourceId,
    src_shock: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    e_ready: f64,
    /// When the pending Triggerseed explosion lands (INF: none pending).
    e_explode_at: f64,
    /// Brushmaker's opening cast: fires once, then the on-hit is permanent.
    w_done: bool,
    daisy_summoned: bool,
    daisy_next_attack: f64,
    /// Consecutive attacks Daisy has landed on the target since her last
    /// shockwave (resets at 3, when Daisy Smash! fires).
    daisy_consec: i64,
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
        let daisy_as_pct = kit.at_rank("gen.R.daisy.asPctByRank", ranks.r)?;
        let daisy_base_as = kit.num("gen.R.daisy.baseAttackSpeed")?;
        let state = State {
            busy_until: 0.0,
            e_ready: 0.0,
            e_explode_at: INF,
            w_done: false,
            daisy_summoned: false,
            daisy_next_attack: INF,
            daisy_consec: 0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("ivern kit needs attack.windupFraction")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_dmg: kit.hit("gen.W.onhit", ranks.w, sheet)?,
            w_cast_s: kit.num("gen.W.castTimeS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_explode_delay: kit.num("gen.E.explodeDelayS")?,
            daisy_ad: kit.hit("gen.R.daisy.ad", ranks.r, sheet)?,
            daisy_period: 1.0 / (daisy_base_as * (1.0 + daisy_as_pct / 100.0)),
            shock_dmg: kit.hit("gen.R.shockwave.damage", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            src_w: intern("W onhit"),
            src_e: intern("E"),
            src_daisy: intern("R Daisy"),
            src_shock: intern("R Daisy Smash"),
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
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        // Brushmaker's bolt, permanently active once the opening cast lands
        if self.s.w_done {
            e.deal(self.w_dmg, DType::Magic, self.src_w, false, false, 1.0);
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
        // the opening cast: only this initial cast counts as an activation;
        // its cast time keeps Ivern busy, and Daisy starts attacking once it
        // ends
        self.s.daisy_summoned = true;
        self.s.daisy_consec = 0;
        e.prime_spellblade();
        self.busy_for(e, self.r_cast_s);
        self.s.daisy_next_attack = self.s.busy_until;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 && !self.s.w_done {
            // a one-time cast; readied at 0, but still waits for a cast in
            // progress
            out[n] = (self.castable_at(e, 0.0), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_explode_at != INF {
                out[n] = (self.s.e_explode_at, Kind::Ev(EV_E_EXPLODE));
            } else {
                // no cast time, but still cannot start inside another cast
                out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.ranks.r > 0 && self.s.daisy_summoned {
            out[n] = (self.s.daisy_next_attack, Kind::Ev(EV_DAISY_ATTACK));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                // the one-time cast that grows the brush Ivern stands in;
                // its cast time keeps Ivern busy before anything else
                self.s.w_done = true;
                e.prime_spellblade();
                self.busy_for(e, self.w_cast_s);
            }
            Kind::Ev(EV_E_CAST) => {
                // no cast time (none): costs nothing and delays nothing; the
                // cooldown waits for the explosion, so park it until then
                self.s.e_explode_at = t + self.e_explode_delay;
                self.s.e_ready = INF;
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_EXPLODE) => {
                self.s.e_explode_at = INF;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, self.src_e, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_DAISY_ATTACK) => {
                e.deal(self.daisy_ad, DType::Physical, self.src_daisy, false, false, 1.0);
                self.s.daisy_consec += 1;
                if self.s.daisy_consec >= 2 + 1 {
                    self.s.daisy_consec = 0;
                    e.deal(self.shock_dmg, DType::Physical, self.src_shock, false, false, 1.0);
                }
                self.s.daisy_next_attack = t + self.daisy_period;
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
