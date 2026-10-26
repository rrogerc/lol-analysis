//! Sona. A ranged auto-attacker whose damage is mixed between Hymn of
//! Valor's direct bolt, its own 5s on-hit Melody tag, and Power Chord's
//! stack-driven empowered attack (Staccato-boosted when Hymn of Valor was
//! the last basic ability cast); Crescendo opens the fight and never
//! recasts. Aria of Perseverance and Song of Celerity deal no damage here
//! but are cast on cooldown purely to build Power Chord stacks faster and
//! to feed Accelerando's (Hymn-of-Valor-only) ability haste.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Which basic ability last landed a Power Chord stack.
const Q_ID: i64 = 1;
const W_ID: i64 = 2;
const E_ID: i64 = 3;

/// Aria and Celerity casts, and Crescendo's delayed swing.
const EV_W_CAST: u8 = 0;
const EV_E_CAST: u8 = 1;
const EV_R_SWING: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    pc_max: i64,
    melody_cd: f64,
    ah_per_stack: f64,
    ah_cap: f64,
    pc_dmg: f64,
    staccato_dmg: f64,
    q_dmg: f64,
    q_onhit_dmg: f64,
    q_tag_dur: f64,
    q_cd: f64,
    w_cd: f64,
    e_cd: f64,
    r_dmg: f64,
    r_cast_s: f64,
    src_pc: SourceId,
    src_staccato: SourceId,
    src_q_onhit: SourceId,
    s: State,
    s0: State,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    w_ready: f64,
    e_ready: f64,
    pc_stacks: i64,
    last_cast: i64,
    pc_armed: bool,
    pc_staccato: bool,
    q_tag_active: bool,
    q_tag_until: f64,
    acc_stacks: i64,
    r_swing_at: f64,
}

impl GenDriver {
    fn accelerando_ah(&self) -> f64 {
        pymin(self.s.acc_stacks as f64 * self.ah_per_stack, self.ah_cap)
    }

    fn ability_cd(&self, e: &Engine, base: f64) -> f64 {
        let total_haste = e.p.sheet.haste + self.accelerando_ah();
        base * 100.0 / (100.0 + total_haste)
    }

    /// A basic ability (Q, W or E) was cast: add a Power Chord stack, arm
    /// the empowered attack at 3, and apply Melody's shared 0.5s cooldown
    /// to the other two basics.
    fn register_cast(&mut self, e: &mut Engine, who: i64) {
        let t = e.st.t;
        self.s.last_cast = who;
        self.s.pc_stacks += 1;
        if self.s.pc_stacks >= self.pc_max {
            self.s.pc_stacks = 0;
            self.s.pc_armed = true;
            self.s.pc_staccato = who == Q_ID;
            let b = self.bonus_as(t);
            let reset_at = t + e.attack_windup(b, self.windup_fraction);
            e.st.next_attack = pymax(e.st.next_attack, reset_at);
        }
        if who != Q_ID {
            e.st.q_ready = pymax(e.st.q_ready, t + self.melody_cd);
        }
        if who != W_ID {
            self.s.w_ready = pymax(self.s.w_ready, t + self.melody_cd);
        }
        if who != E_ID {
            self.s.e_ready = pymax(self.s.e_ready, t + self.melody_cd);
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let pc_dmg = kit.at_level("gen.P.powerChordDamage.base", level)?
            + kit.num("gen.P.powerChordDamage.apRatio")? * sheet.ap;
        let staccato_dmg = kit.at_level("gen.P.staccatoDamage.base", level)?
            + kit.num("gen.P.staccatoDamage.apRatio")? * sheet.ap;
        let state = State {
            w_ready: 0.0,
            e_ready: 0.0,
            pc_stacks: 0,
            last_cast: 0,
            pc_armed: false,
            pc_staccato: false,
            q_tag_active: false,
            q_tag_until: 0.0,
            acc_stacks: 0,
            r_swing_at: INF,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("sona kit needs attack.windupFraction")?,
            pc_max: kit.num("gen.P.maxStacks")? as i64,
            melody_cd: kit.num("gen.P.melodyGlobalCdS")?,
            ah_per_stack: kit.num("gen.P.accelerandoAhPerStack")?,
            ah_cap: kit.num("gen.P.accelerandoAhCap")?,
            pc_dmg,
            staccato_dmg,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_onhit_dmg: kit.hit("gen.Q.onhit", ranks.q, sheet)?,
            q_tag_dur: kit.num("gen.Q.onHitDurationS")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            src_pc: intern("P Power Chord"),
            src_staccato: intern("P Staccato"),
            src_q_onhit: intern("Q onhit"),
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

    fn after_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.pc_armed {
            self.s.pc_armed = false;
            if self.s.pc_staccato {
                e.deal(self.staccato_dmg, DType::Magic, self.src_staccato, false, false, 1.0);
            } else {
                e.deal(self.pc_dmg, DType::Magic, self.src_pc, false, false, 1.0);
            }
        }
        if self.s.q_tag_active {
            self.s.q_tag_active = false;
            if t <= self.s.q_tag_until {
                e.deal(self.q_onhit_dmg, DType::Magic, self.src_q_onhit, false, false, 1.0);
            }
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + self.ability_cd(e, self.q_cd);
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.s.acc_stacks += 1;
        self.s.q_tag_active = true;
        self.s.q_tag_until = t + self.q_tag_dur;
        self.register_cast(e, Q_ID);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        self.s.r_swing_at = e.st.t + self.r_cast_s;
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.r_swing_at != INF {
            out[n] = (self.s.r_swing_at, Kind::Ev(EV_R_SWING));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                self.s.w_ready = t + self.ability_cd(e, self.w_cd);
                e.prime_spellblade();
                self.register_cast(e, W_ID);
            }
            Kind::Ev(EV_E_CAST) => {
                self.s.e_ready = t + self.ability_cd(e, self.e_cd);
                e.prime_spellblade();
                self.register_cast(e, E_ID);
            }
            Kind::Ev(EV_R_SWING) => {
                self.s.r_swing_at = INF;
                e.deal(self.r_dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
