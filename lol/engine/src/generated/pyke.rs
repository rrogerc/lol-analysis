//! Pyke. A caster whose damage comes from abilities: Bone Skewer's press has
//! the wiki's 0.25s cast time and is quick-released 0.4s later, Phantom
//! Undertow is cast on cooldown with its damage/stun landing 1s later, and
//! Death from Below opens the fight for its non-execute physical damage.
//! Gift of the Drowned Ones converts bonus health into bonus AD, feeding
//! attacks and the bonus-AD ratios on Q, E and R. Ghostwater Dive is never
//! cast (no damage, no effect in a stationary fight). Basic attacks fill the
//! remaining time.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Bone Skewer's quick-release, 0.4s after the press.
const EV_Q_RELEASE: u8 = 0;
/// Phantom Undertow: its cast (cooldown check), then its delayed hit.
const EV_E_CAST: u8 = 1;
const EV_E_HIT: u8 = 2;
/// Death from Below's damage, 0.5s after the opening cast.
const EV_R_HIT: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Gift of the Drowned Ones: flat bonus AD from bonus health.
    p_bonus_ad: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    q_release_delay: f64,
    e_dmg: f64,
    e_cd: f64,
    e_hit_delay: f64,
    r_dmg: f64,
    r_cast_s: f64,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    q_charging: bool,
    /// When the pending Q recast lands (INF: none pending).
    q_release_at: f64,
    e_ready: f64,
    /// When the pending E hit lands (INF: none pending).
    e_hit_at: f64,
    /// When the pending R hit lands (INF: none pending).
    r_hit_at: f64,
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
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let bonus_ad_per_bonus_hp = kit.num("gen.P.bonusAdPerBonusHp")?;
        let p_bonus_ad = sheet.hp_bonus * bonus_ad_per_bonus_hp;

        let q_bonus_ad_ratio = kit.num("gen.Q.damage.bonusAdRatio")?;
        let q_extra = if ranks.q > 0 { q_bonus_ad_ratio * p_bonus_ad } else { 0.0 };
        let q_dmg = kit.hit("gen.Q.damage", ranks.q, sheet)? + q_extra;

        let e_bonus_ad_ratio = kit.num("gen.E.damage.bonusAdRatio")?;
        let e_extra = if ranks.e > 0 { e_bonus_ad_ratio * p_bonus_ad } else { 0.0 };
        let e_dmg = kit.hit("gen.E.damage", ranks.e, sheet)? + e_extra;

        let r_base = kit.at_level("gen.R.damage.baseByLevel", level)?;
        let r_bonus_ad_ratio = kit.num("gen.R.damage.bonusAdRatio")?;
        let r_lethality_ratio = kit.num("gen.R.damage.lethalityRatio")?;
        let r_dmg = if ranks.r > 0 {
            r_base + r_bonus_ad_ratio * (sheet.ad_bonus + p_bonus_ad)
                + r_lethality_ratio * sheet.lethality
        } else {
            0.0
        };

        let state = State {
            busy_until: 0.0,
            q_charging: false,
            q_release_at: INF,
            e_ready: 0.0,
            e_hit_at: INF,
            r_hit_at: INF,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("pyke kit needs attack.windupFraction")?,
            p_bonus_ad,
            q_dmg,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            q_release_delay: kit.num("gen.Q.releaseDelayS")?,
            e_dmg,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_hit_delay: kit.num("gen.E.hitDelayS")?,
            r_dmg,
            r_cast_s: kit.num("gen.R.castTimeS")?,
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

    fn attack_damage(&self, e: &Engine) -> f64 {
        e.p.ad + self.p_bonus_ad
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 || self.s.q_charging {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        self.s.q_charging = true;
        self.s.q_release_at = e.st.t + self.q_release_delay;
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.r_hit_at = t + self.r_cast_s;
        e.prime_spellblade();
        self.busy_for(e, self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.q > 0 && self.s.q_charging {
            out[n] = (self.s.q_release_at, Kind::Ev(EV_Q_RELEASE));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_hit_at != INF {
                out[n] = (self.s.e_hit_at, Kind::Ev(EV_E_HIT));
            } else {
                out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        if self.s.r_hit_at != INF {
            out[n] = (self.s.r_hit_at, Kind::Ev(EV_R_HIT));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_RELEASE) => {
                self.s.q_charging = false;
                self.s.q_release_at = INF;
                e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.st.q_ready = t + e.basic_cd(self.q_cd);
            }
            Kind::Ev(EV_E_CAST) => {
                // no cast time: the phantom's damage/stun resolves 1s later
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_hit_at = t + self.e_hit_delay;
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_HIT) => {
                self.s.e_hit_at = INF;
                e.deal(self.e_dmg, DType::Physical, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_R_HIT) => {
                self.s.r_hit_at = INF;
                e.deal(self.r_dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
