//! Briar. Opens with Certain Death (R) for its explosion and Hematomania,
//! which grants Blood Frenzy's attack-speed buff. Head Rush (Q) and
//! Chilling Scream (E, fully charged) are each cast on cooldown for their
//! own damage, shred and attack resets; E interrupts any active frenzy, so
//! Blood Frenzy (W) is (re)cast on cooldown both to arm Snack Attack and to
//! reapply the frenzy window once Hematomania has ended. Crimson Curse (P)
//! stacks a refreshing bleed off every basic attack and damaging ability
//! cast.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Crimson Curse's bleed ticks.
const EV_BLEED_TICK: u8 = 0;
/// Certain Death's explosion lands after its two cast-time phases.
const EV_R_EXPLOSION: u8 = 1;
/// Blood Frenzy is (re)cast whenever off cooldown.
const EV_W_CAST: u8 = 2;
/// Chilling Scream: charge starts, then the release lands.
const EV_E_START: u8 = 3;
const EV_E_RELEASE: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    q_dmg: f64,
    q_cd: f64,
    q_shred_dur: f64,

    w_cd: f64,
    w_as_pct: f64,
    w_frenzy_dur: f64,
    w_snack_flat: f64,
    w_snack_missing_ratio: f64,

    e_dmg: f64,
    e_cd: f64,
    e_charge_s: f64,
    e_recast_s: f64,

    r_dmg: f64,
    r_cast1_s: f64,
    r_cast2_s: f64,

    p_tick_dmg: f64,
    p_stack_bonus_pct: f64,
    p_max_stacks: i64,
    p_tick_rate: f64,
    ticks_total: i64,

    src_p: SourceId,
    src_w_snack: SourceId,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    bleed_stacks: i64,
    bleed_ticks_left: i64,
    next_tick: f64,
    hematomania_active: bool,
    frenzy_until: f64,
    r_land_at: f64,
    r_busy_until: f64,
    w_ready: f64,
    w_armed: bool,
    e_ready: f64,
    e_land_at: f64,
    reset_pending: bool,
}

impl GenDriver {
    fn in_frenzy(&self, t: f64) -> bool {
        self.s.hematomania_active || t < self.s.frenzy_until
    }

    /// Crimson Curse: add a stack (refreshing the bleed to a full set of
    /// ticks), starting the tick schedule if it was not already running.
    fn add_bleed_stack(&mut self, e: &mut Engine) {
        if self.s.bleed_ticks_left == 0 {
            self.s.bleed_stacks = 0;
        }
        self.s.bleed_stacks = imin(self.s.bleed_stacks + 1, self.p_max_stacks);
        self.s.bleed_ticks_left = self.ticks_total;
        if self.s.next_tick == INF {
            self.s.next_tick = e.st.t + self.p_tick_rate;
        }
    }

    /// Snack Attack's bonus damage, spent on the next basic attack; also
    /// resets the attack timer.
    fn spend_snack(&mut self, e: &mut Engine) {
        self.s.w_armed = false;
        let missing_hp = e.target_hp - pymax(e.st.hp, 0.0);
        let dmg = self.w_snack_flat + self.w_snack_missing_ratio * missing_hp;
        e.deal(dmg, DType::Physical, self.src_w_snack, false, true, 1.0);
        self.s.reset_pending = true;
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let p_base = kit.at_level("gen.P.bleedTotalDamage.base", level)?;
        let p_ad_ratio = kit.num("gen.P.bleedTotalDamage.bonusAdRatio")?;
        let p_bleed_dur = kit.num("gen.P.bleedDurationS")?;
        let p_tick_rate = kit.num("gen.P.bleedTickRateS")?;
        let ticks_total = (p_bleed_dur / p_tick_rate) as i64;
        let p_total = p_base + p_ad_ratio * sheet.ad_bonus;
        let p_tick_dmg = p_total / (ticks_total as f64);
        let p_stack_bonus_pct = kit.num("gen.P.stackBonusPct")?;
        let p_max_stacks = kit.num("gen.P.maxStacks")? as i64;

        let q_dmg = kit.hit("gen.Q.damage", ranks.q, sheet)?;
        let q_cd = kit.at_rank("abilities.Q.cooldownS", ranks.q)?;
        let q_shred_dur = kit.num("abilities.Q.shred.durationS")?;

        let w_cd = kit.at_rank("abilities.W.cooldownS", ranks.w)?;
        let w_as_pct = kit.at_rank("gen.W.berserkASPct", ranks.w)? * 100.0;
        let w_frenzy_dur = kit.num("gen.W.berserkDurationS")?;
        let w_snack_flat = kit.hit("gen.W.snack.damage", ranks.w, sheet)?;
        let w_missing_base = kit.num("gen.W.snack.missingHpRatio")?;
        let w_missing_per_bonus_ad = kit.num("gen.W.snack.missingHpRatioPerBonusAd100")?;
        let w_snack_missing_ratio = w_missing_base + w_missing_per_bonus_ad * (sheet.ad_bonus / 100.0);

        let e_dmg = kit.hit("gen.E.damage", ranks.e, sheet)?;
        let e_cd = kit.at_rank("abilities.E.cooldownS", ranks.e)?;
        let e_charge_s = kit.num("gen.E.chargeMaxS")?;
        let e_recast_s = kit.num("gen.E.recastTimeS")?;

        let r_dmg = kit.hit("gen.R.damage", ranks.r, sheet)?;
        let r_cast1_s = kit.num("gen.R.castTimeS")?;
        let r_cast2_s = kit.num("gen.R.dashCastTimeS")?;

        let state = State {
            bleed_stacks: 0,
            bleed_ticks_left: 0,
            next_tick: INF,
            hematomania_active: false,
            frenzy_until: 0.0,
            r_land_at: INF,
            r_busy_until: 0.0,
            w_ready: 0.0,
            w_armed: false,
            e_ready: 0.0,
            e_land_at: INF,
            reset_pending: false,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("briar kit needs attack.windupFraction")?,
            q_dmg,
            q_cd,
            q_shred_dur,
            w_cd,
            w_as_pct,
            w_frenzy_dur,
            w_snack_flat,
            w_snack_missing_ratio,
            e_dmg,
            e_cd,
            e_charge_s,
            e_recast_s,
            r_dmg,
            r_cast1_s,
            r_cast2_s,
            p_tick_dmg,
            p_stack_bonus_pct,
            p_max_stacks,
            p_tick_rate,
            ticks_total,
            src_p: intern("P bleed"),
            src_w_snack: intern("W snack"),
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
        if self.in_frenzy(t) {
            self.w_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        // Crimson Curse: every basic attack applies (or refreshes) a stack.
        self.add_bleed_stack(e);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.w_armed {
            self.spend_snack(e);
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.s.reset_pending {
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
        let earliest = pymax(e.st.t, self.s.r_busy_until);
        pymax(e.st.q_ready, earliest)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.add_bleed_stack(e);
        e.st.shred_until = t + self.q_shred_dur;
        let b = self.bonus_as(t);
        e.st.next_attack = pymin(e.st.next_attack, t + e.attack_windup(b, self.windup_fraction));
    }

    fn cast_r(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let land = t + self.r_cast1_s + self.r_cast2_s;
        self.s.r_land_at = land;
        self.s.r_busy_until = land;
        e.st.next_attack = pymax(e.st.next_attack, land);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.next_tick != INF {
            out[n] = (self.s.next_tick, Kind::Ev(EV_BLEED_TICK));
            n += 1;
        }
        if self.s.r_land_at != INF {
            out[n] = (self.s.r_land_at, Kind::Ev(EV_R_EXPLOSION));
            n += 1;
        }
        if self.ranks.w > 0 {
            let ready = pymax(self.s.w_ready, pymax(e.st.t, self.s.r_busy_until));
            out[n] = (ready, Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_land_at != INF {
                out[n] = (self.s.e_land_at, Kind::Ev(EV_E_RELEASE));
            } else {
                let ready = pymax(self.s.e_ready, pymax(e.st.t, self.s.r_busy_until));
                out[n] = (ready, Kind::Ev(EV_E_START));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_BLEED_TICK) => {
                let stacks = self.s.bleed_stacks;
                let mult = 1.0 + self.p_stack_bonus_pct * (stacks as f64 - 1.0);
                e.deal(self.p_tick_dmg * mult, DType::Physical, self.src_p, false, false, 1.0);
                self.s.bleed_ticks_left -= 1;
                if self.s.bleed_ticks_left > 0 {
                    self.s.next_tick = t + self.p_tick_rate;
                } else {
                    self.s.next_tick = INF;
                    self.s.bleed_stacks = 0;
                }
            }
            Kind::Ev(EV_R_EXPLOSION) => {
                self.s.r_land_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
                self.add_bleed_stack(e);
                self.s.hematomania_active = true;
            }
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                if !self.s.hematomania_active {
                    self.s.frenzy_until = t + self.w_frenzy_dur;
                }
                self.s.w_armed = true;
                e.prime_spellblade();
                e.ability_cast_proc();
            }
            Kind::Ev(EV_E_START) => {
                let land = t + self.e_charge_s + self.e_recast_s;
                self.s.e_land_at = land;
                e.st.next_attack = pymax(e.st.next_attack, land);
                // Chilling Scream interrupts an active frenzy/Hematomania.
                self.s.hematomania_active = false;
                self.s.frenzy_until = t;
                self.s.w_armed = false;
            }
            Kind::Ev(EV_E_RELEASE) => {
                self.s.e_land_at = INF;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.add_bleed_stack(e);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
