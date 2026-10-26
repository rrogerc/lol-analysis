//! Taliyah. Opens with Unraveled Earth, then rides Threaded Volley on
//! cooldown for the rest of the fight: standing permanently on her own
//! Worked Ground means every other Q cast is the empowered Boulder. Q's
//! barrage of five Stone Shards is scheduled out over its cast window, with
//! subsequent hits reduced to 40% damage. Casts go one at a time: both E and
//! Q carry a 0.25 s cast time, tracked with a single `busy_until`. W and R
//! are never cast: neither deals damage against a stationary target.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// A later Stone Shard from the current barrage lands.
const EV_Q_SHARD: u8 = 0;
/// Unraveled Earth is cast on cooldown.
const EV_E_CAST: u8 = 1;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_cd: f64,
    q_cd_half_base: f64,
    q_cd_floor: f64,
    q_ground_dur: f64,
    q_cast_s: f64,
    q_dmg_full: f64,
    q_dmg_reduced: f64,
    q_dmg_boulder: f64,
    q_off2: f64,
    q_off3: f64,
    q_off4: f64,
    q_off5: f64,
    e_dmg: f64,
    e_cd: f64,
    e_cast_s: f64,
    src_q_shard: SourceId,
    src_q_boulder: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    /// Worked Ground is up under her (a plain cast planted it and it has
    /// not been consumed or expired yet).
    q_ground_up: bool,
    q_ground_until: f64,
    /// Next Stone Shard still to land from the current barrage (2..=5), or
    /// 0 when none is pending.
    q_shard_idx: i64,
    q_cast_t: f64,
    e_ready: f64,
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

    /// The scheduled offset (from the barrage's cast time) of Stone Shard
    /// number `idx` (2..=5); 0 for anything else (never reached).
    fn shard_offset(&self, idx: i64) -> f64 {
        match idx {
            2 => self.q_off2,
            3 => self.q_off3,
            4 => self.q_off4,
            5 => self.q_off5,
            _ => 0.0,
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let state = State {
            busy_until: 0.0,
            q_ground_up: false,
            q_ground_until: 0.0,
            q_shard_idx: 0,
            q_cast_t: 0.0,
            e_ready: 0.0,
        };
        let q_dmg_full = kit.hit("gen.Q.damage", ranks.q, sheet)?;
        let subsequent_mult = kit.num("gen.Q.subsequentHitMult")?;
        let boulder_mult = kit.num("gen.Q.boulderMult")?;
        let q_cd = kit.at_rank("abilities.Q.cooldownS", ranks.q)?;
        let worked_ground_cdr = kit.num("gen.Q.workedGroundCDR")?;
        // gen.W.castTimeS is kept in the kit even though the driver below
        // never casts W (see kit "unused"); it is not read here.
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_cd,
            q_cd_half_base: q_cd * worked_ground_cdr,
            q_cd_floor: kit.num("gen.Q.minEmpoweredCdS")?,
            q_ground_dur: kit.num("gen.Q.groundDurationS")?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            q_dmg_full,
            q_dmg_reduced: q_dmg_full * subsequent_mult,
            q_dmg_boulder: q_dmg_full * boulder_mult,
            q_off2: kit.num("gen.Q.shard2OffsetS")?,
            q_off3: kit.num("gen.Q.shard3OffsetS")?,
            q_off4: kit.num("gen.Q.shard4OffsetS")?,
            q_off5: kit.num("gen.Q.shard5OffsetS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_cast_s: kit.num("gen.E.castTimeS")?,
            src_q_shard: intern("Q shard"),
            src_q_boulder: intern("Q empowered"),
            s: state,
            s0: state,
        })
    }

    fn reset(&mut self) {
        self.s = self.s0;
    }

    fn ranged(&self) -> bool {
        true
    }

    fn attack_range(&self) -> f64 {
        self.attack_range
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.q_ground_up && t < self.s.q_ground_until {
            // Empowered: hurl the Boulder, consuming the Worked Ground.
            self.s.q_ground_up = false;
            let cd = pymax(e.basic_cd(self.q_cd_half_base), self.q_cd_floor);
            e.st.q_ready = t + cd;
            e.deal(self.q_dmg_boulder, DType::Magic, self.src_q_boulder, false, true, 1.0);
            e.ability_cast_proc();
            e.eclipse_hit();
            e.prime_spellblade();
        } else {
            // Plain cast: the barrage's first Stone Shard lands now, plants
            // Worked Ground under her, and schedules the remaining four.
            e.st.q_ready = t + e.basic_cd(self.q_cd);
            self.s.q_ground_up = true;
            self.s.q_ground_until = t + self.q_ground_dur;
            self.s.q_cast_t = t;
            self.s.q_shard_idx = 2;
            e.deal(self.q_dmg_full, DType::Magic, SRC_Q, false, true, 1.0);
            e.ability_cast_proc();
            e.eclipse_hit();
            e.prime_spellblade();
            // unable to basic attack until the third Stone Shard launches
            e.st.next_attack = pymax(e.st.next_attack, t + self.q_off3);
        }
        self.busy_for(e, self.q_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.q_shard_idx != 0 {
            let at = self.s.q_cast_t + self.shard_offset(self.s.q_shard_idx);
            out[n] = (at, Kind::Ev(EV_Q_SHARD));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_Q_SHARD) => {
                e.deal(self.q_dmg_reduced, DType::Magic, self.src_q_shard, false, true, 1.0);
                let idx = self.s.q_shard_idx;
                if idx < 5 {
                    self.s.q_shard_idx = idx + 1;
                } else {
                    self.s.q_shard_idx = 0;
                }
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.busy_for(e, self.e_cast_s);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
