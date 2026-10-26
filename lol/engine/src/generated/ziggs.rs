//! Ziggs. A poke mage whose kit is almost entirely ability damage: Short
//! Fuse periodically empowers the next basic attack (its cooldown starting
//! on-attack and getting cut whenever an ability is cast), Bouncing Bomb and
//! Mega Inferno Bomb are single plain-cast AoE hits, Satchel Charge is
//! modeled as an instant throw-then-detonate, and Hexplosive Minefield lands
//! its whole mine cluster as one burst shortly after casting. Casts go one
//! at a time: a single busy_until keeps every cast (Q, W, E, R) from
//! starting inside another one's cast time.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Satchel Charge: cast + detonate, modeled as one instant action.
const EV_W_CAST: u8 = 0;
/// Hexplosive Minefield: the cast, then the mine burst once armed.
const EV_E_CAST: u8 = 1;
const EV_E_ARM: u8 = 2;
/// Mega Inferno Bomb: a recast on cooldown, then its landing damage.
const EV_R_CAST: u8 = 3;
const EV_R_LAND: u8 = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    /// Short Fuse: bonus on-hit damage, its own cooldown, and the flat
    /// cooldown reduction granted by casting any ability.
    p_dmg: f64,
    p_cd: f64,
    p_cdr: f64,
    q_dmg: f64,
    q_cd: f64,
    q_cast_s: f64,
    w_dmg: f64,
    w_cd: f64,
    w_cast_s: f64,
    /// Hexplosive Minefield's total damage, all mines assumed to land.
    e_dmg_total: f64,
    e_cd: f64,
    e_arm_s: f64,
    e_cast_s: f64,
    r_dmg: f64,
    r_cd: f64,
    r_travel_s: f64,
    r_cast_s: f64,
    src_p: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    /// When Short Fuse next arms an empowered attack.
    p_ready: f64,
    w_ready: f64,
    e_ready: f64,
    /// When a pending mine burst lands (INF: none pending).
    e_arm_at: f64,
    r_ready: f64,
    /// When a pending Mega Inferno Bomb lands (INF: none pending).
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

    /// Casting any ability cuts Short Fuse's remaining cooldown, granted at
    /// the start of that ability's cast; it cannot pull the ready time
    /// earlier than now.
    fn apply_p_cdr(&mut self, t: f64) {
        if self.s.p_ready > t {
            self.s.p_ready = pymax(t, self.s.p_ready - self.p_cdr);
        }
    }

    /// Throws Mega Inferno Bomb: the cast, then schedules its landing.
    fn cast_r_now(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.apply_p_cdr(t);
        e.prime_spellblade();
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        self.s.r_land_at = t + self.r_travel_s;
        self.busy_for(e, self.r_cast_s);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            busy_until: 0.0,
            p_ready: 0.0,
            w_ready: 0.0,
            e_ready: 0.0,
            e_arm_at: INF,
            r_ready: INF,
            r_land_at: INF,
        };
        let p_base = kit.at_level("gen.P.baseByLevel", level)?;
        let p_ap_ratio = kit.num("gen.P.apRatio")?;
        let e_dmg_full = kit.hit("gen.E.damage", ranks.e, sheet)?;
        let e_sub_mod = kit.num("gen.E.subsequentMod")?;
        let e_mine_count = kit.num("gen.E.mineCount")?;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            p_dmg: p_base + p_ap_ratio * sheet.ap,
            p_cd: kit.num("gen.P.cooldownS")?,
            p_cdr: kit.at_level("gen.P.cdrByLevel", level)?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cast_s: kit.num("gen.W.castTimeS")?,
            e_dmg_total: e_dmg_full * (1.0 + e_sub_mod * (e_mine_count - 1.0)),
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_arm_s: kit.num("gen.E.armDelayS")?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            r_dmg: kit.hit("gen.R.epicenter", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_travel_s: kit.num("gen.R.travelS")?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
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

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn before_attack(&mut self, e: &mut Engine) {
        // Short Fuse: an armed attack carries the bonus and starts its own
        // 12 s cooldown at the moment it fires
        if self.s.p_ready <= e.st.t {
            e.deal(self.p_dmg, DType::Magic, self.src_p, false, false, 1.0);
            self.s.p_ready = e.st.t + self.p_cd;
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
        self.apply_p_cdr(t);
        e.st.q_ready = t + e.basic_cd(self.q_cd);
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
        self.cast_r_now(e);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
            if self.s.e_arm_at != INF {
                out[n] = (self.s.e_arm_at, Kind::Ev(EV_E_ARM));
                n += 1;
            }
        }
        if self.ranks.r > 0 {
            out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R_CAST));
            n += 1;
            if self.s.r_land_at != INF {
                out[n] = (self.s.r_land_at, Kind::Ev(EV_R_LAND));
                n += 1;
            }
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                // modeled as one instant action: cast, then detonation; the
                // initial throw's cast time keeps Ziggs busy
                self.apply_p_cdr(t);
                e.prime_spellblade();
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.busy_for(e, self.w_cast_s);
            }
            Kind::Ev(EV_E_CAST) => {
                self.apply_p_cdr(t);
                e.prime_spellblade();
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_arm_at = t + self.e_arm_s;
                self.busy_for(e, self.e_cast_s);
            }
            Kind::Ev(EV_E_ARM) => {
                // the mine burst landing: not a cast, just a delayed hit
                self.s.e_arm_at = INF;
                e.deal(self.e_dmg_total, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
            }
            Kind::Ev(EV_R_CAST) => {
                self.cast_r_now(e);
            }
            Kind::Ev(EV_R_LAND) => {
                // the bomb's landing damage: not a cast, just a delayed hit
                self.s.r_land_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
