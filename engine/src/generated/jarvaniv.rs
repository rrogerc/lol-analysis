//! Jarvan IV. Martial Cadence rides every basic attack, gated by an internal
//! per-target cooldown that scales with level; Dragon Strike (Q) goes out on
//! cooldown and shreds armor after its own hit; Demacian Standard (E) goes
//! out on cooldown for magic damage and doubles Jarvan's bonus attack speed
//! from its passive while its flag is deployed; Cataclysm (R) opens the
//! fight, its damage landing after a short leap, and is recast on cooldown.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Demacian Standard is cast (no travel: damage lands immediately).
const EV_E_CAST: u8 = 0;
/// Cataclysm's activation (the leap begins) and its impact (damage lands).
const EV_R_CAST: u8 = 1;
const EV_R_LAND: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    /// Martial Cadence: fraction of current health, its floor, and its
    /// per-target cooldown at this level.
    p_pct: f64,
    p_min: f64,
    p_cd: f64,
    q_dmg: f64,
    q_cd: f64,
    q_shred_dur: f64,
    e_dmg: f64,
    e_cd: f64,
    /// Demacian Standard's bonus attack speed, in percent (its passive; the
    /// deployed flag's aura adds the same amount again additively).
    e_as_pct: f64,
    e_flag_dur: f64,
    r_dmg: f64,
    r_cd: f64,
    r_travel: f64,
    src_p: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// Martial Cadence: when it may next proc, and the target's health
    /// snapshotted just before the current attack's own damage.
    p_next_ready: f64,
    p_snapshot_hp: f64,
    e_ready: f64,
    /// While t < flag_until, Demacian Standard's aura doubles Jarvan's
    /// bonus attack speed from it.
    flag_until: f64,
    /// Cataclysm: the pending leap's landing time (INF: none pending), and
    /// when it may next be cast.
    r_land_at: f64,
    r_ready: f64,
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            p_next_ready: 0.0,
            p_snapshot_hp: 0.0,
            e_ready: 0.0,
            flag_until: -1.0,
            r_land_at: INF,
            r_ready: 0.0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            p_pct: kit.num("gen.P.pctCurrentHp")?,
            p_min: kit.num("gen.P.minDamage")?,
            p_cd: kit.at_level("gen.P.cooldownByLevel", level)?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_shred_dur: kit.num("abilities.Q.shred.durationS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_as_pct: kit.at_rank("gen.E.asPctByRank", ranks.e)? * 100.0,
            e_flag_dur: kit.num("gen.E.flagDurationS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_travel: kit.num("gen.R.travelTimeS")?,
            src_p: intern("P onhit"),
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
        if self.ranks.e == 0 {
            return 0.0;
        }
        let mut pct = self.e_as_pct;
        if t < self.s.flag_until {
            pct += self.e_as_pct;
        }
        pct
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        // snapshot the target's health before the attack's own damage lands
        self.s.p_snapshot_hp = pymax(e.st.hp, 0.0);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if t < self.s.p_next_ready {
            return;
        }
        if e.st.hp <= 0.0 {
            // the target died to the main attack damage
            return;
        }
        self.s.p_next_ready = t + self.p_cd;
        let amt = pymax(self.p_pct * self.s.p_snapshot_hp, self.p_min);
        e.deal(amt, DType::Physical, self.src_p, false, false, 1.0);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        e.deal(self.q_dmg, DType::Physical, SRC_Q, false, true, 1.0);
        // the shred applies after Dragon Strike's own hit, so it is switched
        // on only now that the damage has already been dealt
        e.st.shred_until = t + self.q_shred_dur;
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        // the opening cast: the engine has already primed Spellblade; the
        // leap lands after its travel time
        let t = e.st.t;
        self.s.r_land_at = t + self.r_travel;
        self.s.r_ready = INF;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_land_at != INF {
                out[n] = (self.s.r_land_at, Kind::Ev(EV_R_LAND));
            } else {
                out[n] = (pymax(self.s.r_ready, e.st.t), Kind::Ev(EV_R_CAST));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.flag_until = t + self.e_flag_dur;
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_CAST) => {
                // the recast's activation: this counts as an ability cast
                // for Spellblade, but the damage only lands after the leap
                self.s.r_land_at = t + self.r_travel;
                self.s.r_ready = INF;
                e.prime_spellblade();
            }
            Kind::Ev(EV_R_LAND) => {
                self.s.r_land_at = INF;
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                e.deal(self.r_dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
