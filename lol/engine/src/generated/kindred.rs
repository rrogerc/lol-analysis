//! Kindred. Opens with Q (arrow damage, attack-speed buff, attack-timer
//! reset), sends Mounting Dread's mark with E and Wolf's Frenzy's zone with
//! W, then keeps basic-attacking the dummy: each landed attack stacks
//! Mounting Dread, whose 3rd stack detonates Wolf's pounce, while Wolf
//! independently ticks the dummy for the whole zone duration and Q is
//! recast on its (zone-reduced) cooldown throughout. Casts go one at a
//! time: Mounting Dread's 0.25s cast time keeps Lamb busy, and every other
//! cast (Q, Wolf's Frenzy, though neither has a cast time of its own)
//! still waits for it to end.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Wolf's Frenzy is cast; its zone ticks; its zone ends (post-effect cd).
const EV_W_CAST: u8 = 0;
const EV_W_TICK: u8 = 1;
const EV_W_END: u8 = 2;
/// Mounting Dread is cast (only while the target carries no mark).
const EV_E_CAST: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    q_dmg: f64,
    q_cd_base: f64,
    q_cd_static: f64,
    q_as_bonus_pct: f64,
    q_as_duration: f64,

    w_dmg_base: f64,
    w_hp_frac: f64,
    w_cd_base: f64,
    w_zone_duration: f64,
    w_as_conversion: f64,
    w_base_rate: f64,

    e_dmg_flat: f64,
    e_hp_frac: f64,
    e_cd: f64,
    e_stacks_needed: i64,
    e_crit_mult: f64,
    e_cast_time: f64,

    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    busy_until: f64,
    q_buff_until: f64,
    w_ready: f64,
    w_active: bool,
    w_effect_end: f64,
    w_next_tick: f64,
    e_ready: f64,
    e_mark_active: bool,
    e_stacks: i64,
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
        let assumed_stacks = kit.num("gen.P.assumedStacks")?;

        let q_as_base = kit.num("gen.Q.baseBonusAS")?;
        let q_as_per_mark = kit.num("gen.Q.asPerMark")?;

        let w_pct_base = kit.num("gen.W.percentDamageBase")?;
        let w_pct_per_mark = kit.num("gen.W.percentDamagePerMark")?;

        let e_pct_base = kit.num("gen.E.percentDamageBase")?;
        let e_pct_per_mark = kit.num("gen.E.percentDamagePerMark")?;

        let crit_chance_frac = sheet.crit_chance / 100.0;
        let crit_dmg_mult = sheet.crit_damage / 100.0;
        let e_crit_mult = 1.0 + 0.5 * crit_chance_frac * (crit_dmg_mult - 1.0);

        let state = State {
            busy_until: 0.0,
            q_buff_until: 0.0,
            w_ready: 0.0,
            w_active: false,
            w_effect_end: INF,
            w_next_tick: INF,
            e_ready: 0.0,
            e_mark_active: false,
            e_stacks: 0,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("kindred kit needs attack.windupFraction")?,

            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd_base: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cd_static: kit.at_rank("gen.Q.cdNewValue", ranks.q)?,
            q_as_bonus_pct: (q_as_base + q_as_per_mark * assumed_stacks) * 100.0,
            q_as_duration: kit.num("gen.Q.baseASDuration")?,

            w_dmg_base: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_hp_frac: w_pct_base + w_pct_per_mark * assumed_stacks,
            w_cd_base: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_zone_duration: kit.num("gen.W.zoneDurationS")?,
            w_as_conversion: kit.num("gen.W.asConversion")?,
            w_base_rate: kit.num("gen.W.assumedBaseAttacksPerSecond")?,

            e_dmg_flat: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_hp_frac: e_pct_base + e_pct_per_mark * assumed_stacks,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_stacks_needed: kit.num("gen.E.stacksToProc")? as i64,
            e_crit_mult,
            e_cast_time: kit.num("gen.E.castTimeS")?,

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
        if t < self.s.q_buff_until { self.q_as_bonus_pct } else { 0.0 }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.ranks.e > 0 && self.s.e_mark_active {
            self.s.e_stacks += 1;
            if self.s.e_stacks >= self.e_stacks_needed {
                self.s.e_mark_active = false;
                self.s.e_stacks = 0;
                let missing = e.target_hp - pymax(e.st.hp, 0.0);
                let amt = self.e_crit_mult * (self.e_dmg_flat + self.e_hp_frac * missing);
                e.deal(amt, DType::Physical, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
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
        let cd = if self.s.w_active { self.q_cd_static } else { self.q_cd_base };
        e.st.q_ready = t + e.basic_cd(cd);
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.s.q_buff_until = t + self.q_as_duration;
        let b = self.bonus_as(t);
        e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
    }

    fn cast_r(&mut self, _e: &mut Engine) {
        // Lamb's Respite deals no damage; it is never cast in this fight.
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            if !self.s.w_active {
                out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_CAST));
                n += 1;
            } else {
                if self.s.w_next_tick != INF {
                    out[n] = (self.s.w_next_tick, Kind::Ev(EV_W_TICK));
                    n += 1;
                }
                out[n] = (self.s.w_effect_end, Kind::Ev(EV_W_END));
                n += 1;
            }
        }
        if self.ranks.e > 0 && !self.s.e_mark_active {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                e.prime_spellblade();
                // casting Wolf's Frenzy cuts Q's current cooldown to the
                // static value if that would actually reduce it
                e.st.q_ready = pymin(e.st.q_ready, t + e.basic_cd(self.q_cd_static));
                self.s.w_active = true;
                self.s.w_effect_end = t + self.w_zone_duration;
                self.s.w_next_tick = t;
            }
            Kind::Ev(EV_W_TICK) => {
                let cur_hp = pymax(e.st.hp, 0.0);
                let amt = self.w_dmg_base + self.w_hp_frac * cur_hp;
                e.deal(amt, DType::Magic, SRC_W, false, true, 1.0);
                let total_bonus_as = e.p.sheet.bonus_as_pct + self.bonus_as(t);
                let rate = self.w_base_rate * (1.0 + self.w_as_conversion * total_bonus_as / 100.0);
                let period = 1.0 / rate;
                let next = t + period;
                if next < self.s.w_effect_end {
                    self.s.w_next_tick = next;
                } else {
                    self.s.w_next_tick = INF;
                }
            }
            Kind::Ev(EV_W_END) => {
                self.s.w_active = false;
                self.s.w_next_tick = INF;
                let remaining_base = pymax(self.w_cd_base - self.w_zone_duration, 0.0);
                let factor = e.basic_cd(1.0);
                self.s.w_ready = t + remaining_base * factor;
                self.s.w_effect_end = INF;
            }
            Kind::Ev(EV_E_CAST) => {
                e.prime_spellblade();
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_mark_active = true;
                self.s.e_stacks = 0;
                self.busy_for(e, self.e_cast_time);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
