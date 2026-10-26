//! Lulu. Pix rides every landed basic attack for 3 magic bolts, Glitterlance
//! fires two bolts on cast (Lulu's and Pix's, both assumed to connect) and
//! keeps Lulu busy for its real 0.25 s cast time, Whimsy is self-cast on
//! cooldown for its attack-speed buff (instant: the 0.2419 s figure only
//! applies to an enemy cast, never used here), and Help, Pix! is cast on the
//! dummy on cooldown for its own damage while sending Pix away (suspending
//! his on-attack bolts) for its duration. Wild Growth deals no damage and is
//! never cast.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Whimsy self-cast comes off cooldown.
const EV_W: u8 = 0;
/// Help, Pix! comes off cooldown.
const EV_E: u8 = 1;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Pix's on-attack bolts: damage per bolt and how many.
    p_bolt_dmg: f64,
    p_bolts: i64,
    src_p: SourceId,
    /// Glitterlance: Lulu's bolt damage, Pix's bolt as a fraction of it, and
    /// the cast time that keeps Lulu busy until it lands and a bit longer.
    q_dmg1: f64,
    q_second_mult: f64,
    q_cd: f64,
    q_cast_s: f64,
    /// Whimsy: percent bonus attack speed, its duration, its cooldown, and
    /// the (self-cast) cast time.
    w_as_pct: f64,
    w_dur: f64,
    w_cd: f64,
    w_cast_s: f64,
    e_dmg: f64,
    e_cd: f64,
    /// How long Pix is away from Lulu (no on-attack bolts) after an
    /// enemy-cast Help, Pix!.
    e_pix_away_s: f64,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
    w_ready: f64,
    /// Whimsy's bonus attack speed lasts until this time.
    w_active_until: f64,
    e_ready: f64,
    /// Pix does not fire his on-attack bolts until this time.
    pix_away_until: f64,
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
        let state = State {
            busy_until: 0.0,
            w_ready: 0.0,
            w_active_until: -1.0,
            e_ready: 0.0,
            pix_away_until: -1.0,
        };
        let p_base = kit.at_level("gen.P.damagePerBoltByLevel", level)?;
        let p_ap_ratio = kit.num("gen.P.apRatioPerBolt")?;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("lulu kit needs attack.windupFraction")?,
            p_bolt_dmg: p_base + p_ap_ratio * sheet.ap,
            p_bolts: kit.num("gen.P.bolts")? as i64,
            src_p: intern("P"),
            q_dmg1: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_second_mult: kit.num("gen.Q.secondBoltMult")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            w_as_pct: kit.at_rank("gen.W.asBonusPct", ranks.w)?,
            w_dur: kit.at_rank("gen.W.durationS", ranks.w)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cast_s: kit.num("gen.W.castTimeS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_pix_away_s: kit.num("gen.E.pixOnEnemyDurationS")?,
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
        if t < self.s.w_active_until {
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

    fn after_attack(&mut self, e: &mut Engine) {
        // Pix's bolts fire once per landed basic attack, unless he is away
        // helping a target of Help, Pix!
        if e.st.t < self.s.pix_away_until {
            return;
        }
        let mut i: i64 = 0;
        while i < self.p_bolts {
            e.deal(self.p_bolt_dmg, DType::Magic, self.src_p, false, false, 1.0);
            i += 1;
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        // Lulu's bolt, then Pix's reduced-value bolt, both on the target;
        // the damage lands at the cast, then the cast time keeps Lulu busy.
        e.deal(self.q_dmg1, DType::Magic, SRC_Q, false, true, 1.0);
        let second = self.q_dmg1 * self.q_second_mult;
        e.deal(second, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
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
                // self-cast: instant per the sources, but still keeps the
                // cast-time bookkeeping consistent (0 s here)
                self.s.w_active_until = t + self.w_dur;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.prime_spellblade();
                self.busy_for(e, self.w_cast_s);
            }
            Kind::Ev(EV_E) => {
                // no cast time, so no `busy_for`: Lulu keeps attacking
                // through it, but it still cannot start inside another cast
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                self.s.pix_away_until = t + self.e_pix_away_s;
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
