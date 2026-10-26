//! Jinx. Opens already toggled to Fishbones for the extra 10% AD on-hit rider
//! (a mana-metered rider), casts Super Mega Death Rocket at t=0 (0.6s cast
//! time, then travel time before it lands assumed from max travel distance),
//! and casts Zap! (0.6s-0.4s cast time scaling with bonus AS) / Flame
//! Chompers! (no cast time) on cooldown. When Fishbones' mana cost can no
//! longer be paid from the build's mana pool, Jinx toggles back to the free
//! Pow-Pow stance and keeps attacking, building Rev'd up attack-speed stacks
//! for the rest of the fight.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

const EV_W_CAST: u8 = 0;
const EV_W_LAND: u8 = 1;
const EV_E_CAST: u8 = 2;
const EV_E_LAND: u8 = 3;
const EV_R_CAST: u8 = 4;
const EV_R_LAND: u8 = 5;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    sheet_bonus_as_pct: f64,

    revved_first_pct: f64,
    revved_sub_pct: f64,
    revved_duration: f64,
    revved_max: i64,

    fishbones_extra_dmg: f64,
    fishbones_as_penalty_pct: f64,
    q_mana_cost: f64,
    q_toggle_cd: f64,
    src_q_onhit: SourceId,

    w_dmg: f64,
    w_cd: f64,
    w_mana_cost: f64,
    w_cast_max: f64,
    w_cast_min: f64,
    w_cast_as_cap: f64,

    e_dmg: f64,
    e_cd: f64,
    e_mana_cost: f64,
    e_delay_s: f64,

    r_dmg_base: f64,
    r_missing_ratio: f64,
    r_cd: f64,
    r_mana_cost: f64,
    r_cast_s: f64,
    r_travel_s: f64,

    mana_max: f64,

    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    mana: f64,
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    use_fishbones: bool,
    revved_stacks: i64,
    revved_deadline: f64,
    q_toggle_ready: f64,
    w_ready: f64,
    w_land_at: f64,
    e_ready: f64,
    e_land_at: f64,
    r_ready: f64,
    r_land_at: f64,
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

    fn revved_pct(&self, t: f64) -> f64 {
        if self.s.revved_stacks <= 0 || t >= self.s.revved_deadline {
            return 0.0;
        }
        self.revved_first_pct + self.revved_sub_pct * ((self.s.revved_stacks - 1) as f64)
    }

    fn w_cast_time(&self, t: f64) -> f64 {
        let total_bonus_as = pymax(0.0, self.sheet_bonus_as_pct + self.bonus_as(t));
        let capped = pymin(total_bonus_as, self.w_cast_as_cap);
        let frac = capped / self.w_cast_as_cap;
        self.w_cast_max - (self.w_cast_max - self.w_cast_min) * frac
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let fishbones_ad_ratio = kit.num("gen.Q.fishbonesAdRatio")?;
        // mana is not modelled: nothing spends, so the pool stays here
        let mana_max = sheet.mana;
        let state = State {
            mana: mana_max,
            busy_until: 0.0,
            use_fishbones: ranks.q > 0,
            revved_stacks: 0,
            revved_deadline: -INF,
            q_toggle_ready: 0.0,
            w_ready: 0.0,
            w_land_at: INF,
            e_ready: 0.0,
            e_land_at: INF,
            r_ready: INF,
            r_land_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            sheet_bonus_as_pct: sheet.bonus_as_pct,

            revved_first_pct: kit.at_rank("gen.Q.revvedFirstStackAsPct", ranks.q)?,
            revved_sub_pct: kit.at_rank("gen.Q.revvedSubStackAsPct", ranks.q)?,
            revved_duration: kit.num("gen.Q.revvedDurationS")?,
            revved_max: kit.num("gen.Q.revvedMaxStacks")? as i64,

            fishbones_extra_dmg: fishbones_ad_ratio * sheet.ad,
            fishbones_as_penalty_pct: kit.num("gen.Q.fishbonesAsPenaltyPct")?,
            q_mana_cost: kit.num("gen.Q.fishbonesManaCostPerAttack")?,
            q_toggle_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            src_q_onhit: intern("Q onhit"),

            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_mana_cost: kit.at_rank("gen.W.manaCost", ranks.w)?,
            w_cast_max: kit.num("gen.W.castTimeMaxS")?,
            w_cast_min: kit.num("gen.W.castTimeMinS")?,
            w_cast_as_cap: kit.num("gen.W.castTimeAsCapPct")?,

            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_mana_cost: kit.at_rank("gen.E.manaCost", ranks.e)?,
            e_delay_s: kit.num("gen.E.landTimeS")? + kit.num("gen.E.armTimeS")?,

            r_dmg_base: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_missing_ratio: kit.at_rank("gen.R.missingHpRatio", ranks.r)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_mana_cost: kit.at_rank("gen.R.manaCost", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_travel_s: kit.num("gen.R.travelTimeS")?,

            mana_max,

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
        if self.s.use_fishbones {
            -self.fishbones_as_penalty_pct / 100.0 * self.sheet_bonus_as_pct
        } else {
            self.revved_pct(t)
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.use_fishbones && self.s.mana < self.q_mana_cost {
            if t >= self.s.q_toggle_ready {
                self.s.use_fishbones = false;
                self.s.q_toggle_ready = t + self.q_toggle_cd;
            }
        }
        if !self.s.use_fishbones && self.ranks.q > 0 {
            if t < self.s.revved_deadline {
                self.s.revved_stacks = imin(self.s.revved_stacks + 1, self.revved_max);
            } else {
                self.s.revved_stacks = 1;
            }
            self.s.revved_deadline = t + self.revved_duration;
        }
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        if self.s.use_fishbones && self.s.mana >= self.q_mana_cost {
            e.deal(self.fishbones_extra_dmg, DType::Physical, self.src_q_onhit, true, false, 1.0);
        }
    }

    fn q_at(&self, _e: &Engine) -> f64 {
        INF
    }

    fn cast_q(&mut self, _e: &mut Engine) {}

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        if self.s.mana < self.r_mana_cost {
            self.s.r_ready = INF;
            return;
        }
        self.s.r_land_at = t + self.r_cast_s + self.r_travel_s;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        self.busy_for(e, self.r_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            if self.s.w_land_at != INF {
                out[n] = (self.s.w_land_at, Kind::Ev(EV_W_LAND));
                n += 1;
            } else if self.s.w_ready != INF {
                out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_CAST));
                n += 1;
            }
        }
        if self.ranks.e > 0 {
            if self.s.e_land_at != INF {
                out[n] = (self.s.e_land_at, Kind::Ev(EV_E_LAND));
                n += 1;
            } else if self.s.e_ready != INF {
                out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
                n += 1;
            }
        }
        if self.ranks.r > 0 {
            if self.s.r_land_at != INF {
                out[n] = (self.s.r_land_at, Kind::Ev(EV_R_LAND));
                n += 1;
            } else if self.s.r_ready != INF {
                out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R_CAST));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                if self.s.mana < self.w_mana_cost {
                    self.s.w_ready = INF;
                    return;
                }
                let ct = self.w_cast_time(t);
                self.s.w_land_at = t + ct;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.busy_for(e, ct);
            }
            Kind::Ev(EV_W_LAND) => {
                self.s.w_land_at = INF;
                e.deal(self.w_dmg, DType::Physical, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_E_CAST) => {
                if self.s.mana < self.e_mana_cost {
                    self.s.e_ready = INF;
                    return;
                }
                self.s.e_land_at = t + self.e_delay_s;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
            }
            Kind::Ev(EV_E_LAND) => {
                self.s.e_land_at = INF;
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_CAST) => {
                if self.s.mana < self.r_mana_cost {
                    self.s.r_ready = INF;
                    return;
                }
                self.s.r_land_at = t + self.r_cast_s + self.r_travel_s;
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                self.busy_for(e, self.r_cast_s);
            }
            Kind::Ev(EV_R_LAND) => {
                self.s.r_land_at = INF;
                let missing = self.r_missing_ratio * (e.target_hp - pymax(e.st.hp, 0.0));
                e.deal(self.r_dmg_base + missing, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
