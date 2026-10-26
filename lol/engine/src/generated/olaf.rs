//! Olaf. A mixed attacker/ability caster: Ragnarok (R) opens the fight and its
//! bonus-AD buff is kept alive almost indefinitely by attacks and Reckless
//! Swing casts, Undertow (Q) and Reckless Swing (E) go out on cooldown and
//! read Olaf's live AD (including Ragnarok's dynamic bonus), Tough It Out (W)
//! is woven in on cooldown for its attack-speed buff and attack reset, and
//! Berserker Rage's attack speed rises with Olaf's own missing health, which
//! only moves via Reckless Swing's self-inflicted health cost. Casts go one
//! at a time: Undertow and Reckless Swing keep Olaf busy for their cast time
//! (one `busy_until` in the state), while Tough It Out and Ragnarok are
//! instant but still cannot start inside another cast.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Tough It Out is cast; Reckless Swing is cast.
const EV_W: u8 = 0;
const EV_E: u8 = 1;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    max_hp: f64,
    /// Berserker Rage: max bonus AS at this level, and the missing-health
    /// fraction (0-1) at which it is fully reached.
    p_as_max: f64,
    p_missing_cap_frac: f64,
    q_base: f64,
    q_bonus_ad_ratio: f64,
    q_cd: f64,
    q_cast_s: f64,
    q_shred_dur: f64,
    w_as_pct: f64,
    w_dur: f64,
    w_cd: f64,
    e_base: f64,
    e_ad_ratio: f64,
    e_cost_pct: f64,
    e_cdr_per_attack: f64,
    e_cd: f64,
    e_cast_s: f64,
    r_flat_ad: f64,
    r_pct_ad_ratio: f64,
    r_dur: f64,
    r_extension: f64,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// Olaf's own current health (only Reckless Swing's cost moves it).
    cur_hp: f64,
    /// A cast with a cast time in progress ends here: no other cast, no
    /// attack before it.
    busy_until: f64,
    w_ready: f64,
    /// Tough It Out's attack-speed buff runs until this time.
    w_until: f64,
    e_ready: f64,
    /// Ragnarok's bonus-AD buff runs until this time (negative: not cast).
    r_until: f64,
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

    fn missing_hp_frac(&self) -> f64 {
        (self.max_hp - self.s.cur_hp) / self.max_hp
    }

    /// Berserker Rage's live bonus attack speed, percent.
    fn p_bonus_as(&self) -> f64 {
        let ratio = pymin(self.missing_hp_frac() / self.p_missing_cap_frac, 1.0);
        ratio * self.p_as_max
    }

    /// Ragnarok's live bonus AD (0 while not active), computed off the
    /// caster's current total AD so it amplifies dynamically.
    fn r_bonus_ad(&self, e: &Engine) -> f64 {
        if self.ranks.r > 0 && e.st.t < self.s.r_until {
            self.r_flat_ad + self.r_pct_ad_ratio * e.p.ad
        } else {
            0.0
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let max_hp = sheet.hp;
        let state = State {
            cur_hp: max_hp,
            busy_until: 0.0,
            w_ready: 0.0,
            w_until: -1.0,
            e_ready: 0.0,
            r_until: -1.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("olaf kit needs attack.windupFraction")?,
            max_hp,
            p_as_max: kit.at_level("gen.P.asMaxByLevel", level)?,
            p_missing_cap_frac: kit.num("gen.P.missingHpCapPct")? / 100.0,
            q_base: kit.at_rank("gen.Q.damage.base", ranks.q)?,
            q_bonus_ad_ratio: kit.num("gen.Q.damage.bonusAdRatio")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            q_shred_dur: kit.num("abilities.Q.shred.durationS")?,
            w_as_pct: kit.at_rank("gen.W.asPctByRank", ranks.w)?,
            w_dur: kit.num("gen.W.durationS")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_base: kit.at_rank("gen.E.damage.base", ranks.e)?,
            e_ad_ratio: kit.num("gen.E.damage.adRatio")?,
            e_cost_pct: kit.num("gen.E.healthCostPercent")?,
            e_cdr_per_attack: kit.num("gen.E.cdrPerAttackS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            r_flat_ad: kit.at_rank("gen.R.flatAd", ranks.r)?,
            r_pct_ad_ratio: kit.num("gen.R.pctAdRatio")?,
            r_dur: kit.num("gen.R.durationS")?,
            r_extension: kit.num("gen.R.extensionS")?,
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
        let mut b = self.p_bonus_as();
        if t < self.s.w_until {
            b += self.w_as_pct;
        }
        b
    }

    fn attack_damage(&self, e: &Engine) -> f64 {
        e.p.ad + self.r_bonus_ad(e)
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        // Reckless Swing's cooldown is cut by 1s per landed basic attack.
        if self.ranks.e > 0 {
            self.s.e_ready = pymax(t, self.s.e_ready - self.e_cdr_per_attack);
        }
        // Ragnarok's duration is extended by each on-hit attack while active.
        if self.ranks.r > 0 && t < self.s.r_until {
            self.s.r_until += self.r_extension;
        }
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
        let bonus_ad = e.p.sheet.ad_bonus + self.r_bonus_ad(e);
        let dmg = self.q_base + self.q_bonus_ad_ratio * bonus_ad;
        e.deal(dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.st.shred_until = t + self.q_shred_dur;
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
        self.s.r_until = t + self.r_dur;
        e.prime_spellblade();
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
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                // no cast time: instant, but still cannot start inside
                // another cast (handled by `castable_at` above)
                self.s.w_until = t + self.w_dur;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                // Tough It Out resets Olaf's basic attack timer.
                let b = self.bonus_as(t);
                e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
                e.prime_spellblade();
            }
            Kind::Ev(EV_E) => {
                let total_ad = e.p.ad + self.r_bonus_ad(e);
                let dmg = self.e_base + self.e_ad_ratio * total_ad;
                let cost = self.e_cost_pct * dmg;
                self.s.cur_hp = pymax(self.s.cur_hp - cost, 1.0);
                e.deal(dmg, DType::True, SRC_E, false, true, 1.0);
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                if self.ranks.r > 0 && t < self.s.r_until {
                    self.s.r_until += self.r_extension;
                }
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.e_cast_s);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
