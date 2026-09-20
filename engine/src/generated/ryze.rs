//! Ryze. A caster whose Overload (Q) has its cooldown reset to ready by
//! every Rune Prison (W) or Spell Flux (E) cast; Spell Flux marks the
//! target with Flux, and the next Overload consumes that mark for bonus
//! damage. The driver casts Spell Flux on cooldown, lets the freshly
//! reset Overload fire immediately to consume the mark, and only weaves
//! in Rune Prison when no live Flux mark would be wasted on it.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Spell Flux is cast on cooldown; Rune Prison only while no Flux mark
/// is live and uncomsumed (else Overload should consume it instead).
const EV_E: u8 = 0;
const EV_W: u8 = 1;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_cd: f64,
    w_cd: f64,
    e_cd: f64,
    cast_q_s: f64,
    cast_w_s: f64,
    cast_e_s: f64,
    e_flux_dur: f64,
    /// The Flux damage amplification Overload gets against a marked
    /// target, as a fraction (0.40 = +40%).
    q_flux_amp: f64,
    /// Fully precomputed per-cast damage (base + AP ratio + bonus-mana
    /// ratio); AP and mana are constant for the whole fight.
    q_dmg: f64,
    w_dmg: f64,
    e_dmg: f64,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    e_ready: f64,
    w_ready: f64,
    /// The time Ryze may begin his next cast (each cast takes 0.25 s).
    busy_until: f64,
    /// When the current Flux mark on the target expires; a cast consumes
    /// it early by setting this to -INF.
    flux_until: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        // Arcane Mastery: bonus max mana equal to (pct% per 100 AP) of
        // Ryze's total mana; that increase is itself treated as bonus
        // mana and feeds Q/W/E's bonus-mana damage ratios.
        let mana_pct_per_100ap = kit.num("gen.P.bonusManaPctPer100Ap")?;
        let mana_increase = sheet.mana * (mana_pct_per_100ap / 100.0) * (sheet.ap / 100.0);
        let bonus_mana_total = sheet.mana_bonus + mana_increase;

        let q_base = kit.hit("gen.Q.damage", ranks.q, sheet)?;
        let q_bmr = kit.num("gen.Q.bonusManaRatio")?;
        let w_base = kit.hit("gen.W.damage", ranks.w, sheet)?;
        let w_bmr = kit.num("gen.W.bonusManaRatio")?;
        let e_base = kit.hit("gen.E.damage", ranks.e, sheet)?;
        let e_bmr = kit.num("gen.E.bonusManaRatio")?;

        let state = State {
            e_ready: 0.0,
            w_ready: 0.0,
            busy_until: 0.0,
            flux_until: -INF,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            cast_q_s: kit.num("gen.Q.castTimeS")?,
            cast_w_s: kit.num("gen.W.castTimeS")?,
            cast_e_s: kit.num("gen.E.castTimeS")?,
            e_flux_dur: kit.num("gen.E.fluxDurationS")?,
            q_flux_amp: kit.at_rank("gen.Q.fluxDamageAmpPct", ranks.q)? / 100.0,
            q_dmg: q_base + bonus_mana_total * q_bmr,
            w_dmg: w_base + bonus_mana_total * w_bmr,
            e_dmg: e_base + bonus_mana_total * e_bmr,
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

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, pymax(self.s.busy_until, e.st.t))
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.busy_until = t + self.cast_q_s;
        let mut dmg = self.q_dmg;
        if self.s.flux_until > t {
            // consume the live Flux mark for the damage amplification
            dmg *= 1.0 + self.q_flux_amp;
            self.s.flux_until = -INF;
        }
        e.deal(dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        e.lockout();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let t = e.st.t;
        let mut n = 0;
        if self.ranks.e > 0 {
            let at = pymax(self.s.e_ready, pymax(self.s.busy_until, t));
            out[n] = (at, Kind::Ev(EV_E));
            n += 1;
        }
        if self.ranks.w > 0 && self.s.flux_until <= t {
            // withheld entirely while a Flux mark is live and unconsumed,
            // so it never spends the mark instead of Overload; it
            // reappears as soon as Overload consumes it or it expires
            let at = pymax(self.s.w_ready, pymax(self.s.busy_until, t));
            out[n] = (at, Kind::Ev(EV_W));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E) => {
                self.s.busy_until = t + self.cast_e_s;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.flux_until = t + self.e_flux_dur;
                e.st.q_ready = t;
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            Kind::Ev(EV_W) => {
                self.s.busy_until = t + self.cast_w_s;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.st.q_ready = t;
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.lockout();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
