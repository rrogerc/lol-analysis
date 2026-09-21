//! Rek'Sai. Fury builds from attacks and ability hits (25 each, capped at
//! 100) and gates Furious Bite's true-damage conversion. Queen's Wrath (Q) is
//! cast on cooldown to empower up to 3 attacks with bonus physical damage and
//! attack speed, resetting the attack timer on cast; its cooldown starts only
//! once that window ends. Unburrow (W) and Furious Bite (E) go out on
//! cooldown; Furious Bite has a 0.25s cast time that keeps Rek'Sai busy. Void
//! Rush (R) waits for the dummy to be Marked as Prey (from the opening
//! attack) before it can be cast, then fires as soon as its long cooldown
//! allows, its cast time and pounce delay holding attacks through it.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Unburrow's damage burst, on its 4s cycle cooldown.
const EV_W: u8 = 0;
/// Furious Bite, cast on cooldown.
const EV_E: u8 = 1;
/// Void Rush: the cast (once marked and ready), then the delayed pounce.
const EV_R_CAST: u8 = 2;
const EV_R_DAMAGE: u8 = 3;
/// Queen's Wrath's empower window expiring without using all its attacks.
const EV_Q_EXPIRE: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    fury_per_attack: f64,
    fury_per_ability: f64,
    q_dmg: f64,
    q_as_pct: f64,
    q_buff_dur: f64,
    q_max_attacks: i64,
    q_cd: f64,
    w_dmg: f64,
    w_lockout: f64,
    w_cd: f64,
    e_dmg: f64,
    e_dmg_empowered: f64,
    e_cast_s: f64,
    e_cd: f64,
    r_dmg: f64,
    r_hp_ratio: f64,
    r_cast_s: f64,
    r_delay_s: f64,
    r_cd: f64,
    src_e_emp: SourceId,
    /// The rotation state, and the pristine copy `reset` restores.
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    fury: f64,
    /// Whether the dummy has ever been damaged (needed for Void Rush).
    marked: bool,
    q_active: bool,
    q_charges: i64,
    q_buff_until: f64,
    w_ready: f64,
    e_ready: f64,
    r_ready: f64,
    /// When the pending Void Rush pounce lands (INF: none pending).
    r_damage_at: f64,
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
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
        let empowered_ratio = kit.num("gen.E.empoweredRatio")?;
        let e_dmg = kit.hit("gen.E.damage", ranks.e, sheet)?;
        let state = State {
            fury: 0.0,
            marked: false,
            q_active: false,
            q_charges: 0,
            q_buff_until: 0.0,
            w_ready: 0.0,
            e_ready: 0.0,
            r_ready: 0.0,
            r_damage_at: INF,
            busy_until: 0.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("reksai kit needs attack.windupFraction")?,
            fury_per_attack: kit.num("gen.P.furyPerAttack")?,
            fury_per_ability: kit.num("gen.P.furyPerAbility")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_as_pct: kit.num("gen.Q.attackSpeedPct")?,
            q_buff_dur: kit.num("gen.Q.buffDurationS")?,
            q_max_attacks: kit.num("gen.Q.maxEmpoweredAttacks")? as i64,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_lockout: kit.num("gen.W.lockoutS")?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_dmg,
            e_dmg_empowered: e_dmg * empowered_ratio,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_hp_ratio: kit.at_rank("gen.R.targetMaxHpRatio", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_delay_s: kit.num("gen.R.delayAfterCastS")?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            src_e_emp: intern("E empowered"),
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
        if self.s.q_active { self.q_as_pct } else { 0.0 }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.fury = pymin(self.s.fury + self.fury_per_attack, 100.0);
        self.s.marked = true;
        if self.ranks.q > 0 && self.s.q_active {
            e.deal(self.q_dmg, DType::Physical, SRC_Q, true, true, 1.0);
            e.ability_cast_proc();
            e.eclipse_hit();
            self.s.q_charges -= 1;
            if self.s.q_charges <= 0 {
                self.s.q_active = false;
                self.s.q_buff_until = t;
                e.st.q_ready = t + e.basic_cd(self.q_cd);
            } else {
                self.s.q_buff_until = t + self.q_buff_dur;
            }
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if self.s.q_active {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        // no cast time: it costs nothing, but still cannot start inside
        // another cast (handled by q_at via castable_at)
        let t = e.st.t;
        self.s.q_active = true;
        self.s.q_charges = self.q_max_attacks;
        self.s.q_buff_until = t + self.q_buff_dur;
        e.prime_spellblade();
        let b = self.bonus_as(t);
        e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_active {
            out[n] = (self.s.q_buff_until, Kind::Ev(EV_Q_EXPIRE));
            n += 1;
        }
        if self.ranks.w > 0 {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E));
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_damage_at != INF {
                out[n] = (self.s.r_damage_at, Kind::Ev(EV_R_DAMAGE));
                n += 1;
            } else if self.s.marked {
                out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R_CAST));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_EXPIRE) => {
                self.s.q_active = false;
                e.st.q_ready = t + e.basic_cd(self.q_cd);
            }
            Kind::Ev(EV_W) => {
                // Unburrow does not count as an ability activation: no
                // Spellblade / Muramana / Eclipse on-cast procs, per the wiki.
                // No cast time, so no busy_for; it holds attacks via lockout.
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                self.s.fury = pymin(self.s.fury + self.fury_per_ability, 100.0);
                self.s.marked = true;
                e.st.next_attack = pymax(e.st.next_attack, t + self.w_lockout);
            }
            Kind::Ev(EV_E) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                if self.s.fury >= 100.0 {
                    e.deal(self.e_dmg_empowered, DType::True, self.src_e_emp, false, true, 1.0);
                } else {
                    e.deal(self.e_dmg, DType::Physical, SRC_E, false, true, 1.0);
                }
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.fury = pymin(self.s.fury + self.fury_per_ability, 100.0);
                self.s.marked = true;
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_R_CAST) => {
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                self.s.r_damage_at = t + self.r_delay_s;
                e.prime_spellblade();
                self.busy_for(e, self.r_cast_s);
                // vanished during the pounce delay too: hold attacks further
                e.st.next_attack = pymax(e.st.next_attack, self.s.r_damage_at);
            }
            Kind::Ev(EV_R_DAMAGE) => {
                self.s.r_damage_at = INF;
                let amt = self.r_dmg + self.r_hp_ratio * e.target_hp;
                e.deal(amt, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                self.s.fury = pymin(self.s.fury + self.fury_per_ability, 100.0);
                self.s.marked = true;
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
