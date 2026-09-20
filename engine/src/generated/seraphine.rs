//! Seraphine. A mixed AA/ability attacker: Stage Presence's Echo doubles
//! whichever basic ability cast lands at 2 stacks (starting there), Harmony
//! grants a Note per ability cast that a subsequent attack fires as bonus
//! magic damage, and High Note / Beat Drop / Encore go out on cooldown.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Beat Drop is ready to cast.
const EV_E: u8 = 0;
/// Encore is ready to cast (recast, beyond the opening one).
const EV_R_READY: u8 = 1;
/// Encore's damage lands (its cast time after the cast starts).
const EV_R_DMG: u8 = 2;
/// An Echo-doubled cast resolves.
const EV_ECHO: u8 = 3;

/// Which ability a pending Echo cast mimics.
const AB_Q: i64 = 1;
const AB_E: i64 = 2;
const AB_R: i64 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    /// Stage Presence: Echo cap and its delay, Notes' cap/duration/damage.
    echo_cap: i64,
    echo_delay: f64,
    note_cap: i64,
    note_duration: f64,
    note_dmg: f64,
    q_dmg: f64,
    q_amp_cap: f64,
    q_cd: f64,
    cast_s_q: f64,
    e_dmg: f64,
    e_cd: f64,
    cast_s_e: f64,
    r_dmg: f64,
    r_cd: f64,
    r_cast_s: f64,
    r_total_lock_s: f64,
    src_p_note: SourceId,
    src_q_echo: SourceId,
    src_e_echo: SourceId,
    src_r_echo: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    echo_stacks: i64,
    echo_pending_ability: i64,
    /// When a pending Echo cast resolves (INF: none pending).
    echo_pending_at: f64,
    note_count: i64,
    note_until: f64,
    e_ready: f64,
    r_ready: f64,
    /// When Encore's own damage lands (INF: none pending).
    r_dmg_at: f64,
}

impl GenDriver {
    fn grant_note(&mut self, e: &Engine) {
        self.s.note_count = imin(self.s.note_count + 1, self.note_cap);
        self.s.note_until = e.st.t + self.note_duration;
    }

    /// Registers a basic ability cast against Echo: if it was already at cap
    /// it consumes the stacks and schedules a free extra cast, otherwise it
    /// just adds a stack.
    fn maybe_echo(&mut self, t: f64, cast_time: f64, ability: i64) {
        if self.s.echo_stacks >= self.echo_cap {
            self.s.echo_stacks = 0;
            self.s.echo_pending_ability = ability;
            self.s.echo_pending_at = t + cast_time + self.echo_delay;
        } else {
            self.s.echo_stacks += 1;
        }
    }

    fn q_amp(&self, e: &Engine) -> f64 {
        let missing = e.target_hp - pymax(e.st.hp, 0.0);
        let pct = missing / e.target_hp * 100.0;
        pymin(pct, self.q_amp_cap)
    }

    fn deal_q(&mut self, e: &mut Engine, is_echo: bool) {
        let amp = self.q_amp(e);
        let dmg = self.q_dmg * (1.0 + amp / 100.0);
        let src = if is_echo { self.src_q_echo } else { SRC_Q };
        e.deal(dmg, DType::Magic, src, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.grant_note(e);
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        self.deal_q(e, false);
        e.lockout();
        self.maybe_echo(t, self.cast_s_q, AB_Q);
    }

    fn deal_e(&mut self, e: &mut Engine, is_echo: bool) {
        let src = if is_echo { self.src_e_echo } else { SRC_E };
        e.deal(self.e_dmg, DType::Magic, src, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.grant_note(e);
    }

    fn cast_e(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.e_ready = t + e.basic_cd(self.e_cd);
        self.deal_e(e, false);
        e.lockout();
        self.maybe_echo(t, self.cast_s_e, AB_E);
    }

    fn deal_r_damage(&mut self, e: &mut Engine, is_echo: bool) {
        let src = if is_echo { self.src_r_echo } else { SRC_R };
        e.deal(self.r_dmg, DType::Magic, src, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.ult_hatefog();
    }

    /// Encore recast after the opening one (the opening cast is handled by
    /// `cast_r`, which the engine already primes Spellblade for).
    fn cast_r_recast(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        e.prime_spellblade();
        self.grant_note(e);
        self.s.r_dmg_at = t + self.r_cast_s;
        e.st.next_attack = pymax(e.st.next_attack, t + self.r_total_lock_s);
        self.maybe_echo(t, self.r_total_lock_s, AB_R);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let echo_cap = kit.num("gen.P.echoMaxStacks")? as i64;
        let state = State {
            echo_stacks: echo_cap,
            echo_pending_ability: 0,
            echo_pending_at: INF,
            note_count: 0,
            note_until: 0.0,
            e_ready: 0.0,
            r_ready: 0.0,
            r_dmg_at: INF,
        };
        let r_cast_s = kit.num("gen.R.castTimeS")?;
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            echo_cap,
            echo_delay: kit.num("gen.P.echoDelayS")?,
            note_cap: kit.num("gen.P.noteMaxStacks")? as i64,
            note_duration: kit.num("gen.P.noteDurationS")?,
            note_dmg: kit.at_level("gen.P.noteDamageByLevel", level)?
                + kit.num("gen.P.noteApRatio")? * sheet.ap,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_amp_cap: kit.num("gen.Q.damageAmpCapPct")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            cast_s_q: kit.num("gen.Q.castTimeS")?,
            e_dmg: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            cast_s_e: kit.num("gen.E.castTimeS")?,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s,
            r_total_lock_s: 2.0 * r_cast_s,
            src_p_note: intern("P onhit"),
            src_q_echo: intern("Q echo"),
            src_e_echo: intern("E echo"),
            src_r_echo: intern("R echo"),
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
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        if self.s.note_count > 0 && t <= self.s.note_until {
            let n = self.s.note_count;
            let mut i = 0;
            while i < n {
                e.deal(self.note_dmg, DType::Magic, self.src_p_note, false, false, 1.0);
                i += 1;
            }
            self.s.note_count = 0;
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        GenDriver::cast_q(self, e);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the engine has already primed Spellblade and
        // held the first attack past the 0.25s window; Encore's own damage
        // lands later, at the end of its (longer) cast time
        let t = e.st.t;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        self.grant_note(e);
        self.s.r_dmg_at = t + self.r_cast_s;
        e.st.next_attack = pymax(e.st.next_attack, t + self.r_total_lock_s);
        self.maybe_echo(t, self.r_total_lock_s, AB_R);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E));
            n += 1;
        }
        if self.ranks.r > 0 {
            out[n] = (pymax(self.s.r_ready, e.st.t), Kind::Ev(EV_R_READY));
            n += 1;
        }
        if self.s.r_dmg_at != INF {
            out[n] = (self.s.r_dmg_at, Kind::Ev(EV_R_DMG));
            n += 1;
        }
        if self.s.echo_pending_at != INF {
            out[n] = (self.s.echo_pending_at, Kind::Ev(EV_ECHO));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        match kind {
            Kind::Ev(EV_E) => {
                self.cast_e(e);
            }
            Kind::Ev(EV_R_READY) => {
                self.cast_r_recast(e);
            }
            Kind::Ev(EV_R_DMG) => {
                self.s.r_dmg_at = INF;
                self.deal_r_damage(e, false);
            }
            Kind::Ev(EV_ECHO) => {
                let ab = self.s.echo_pending_ability;
                self.s.echo_pending_ability = 0;
                self.s.echo_pending_at = INF;
                let t = e.st.t;
                if ab == AB_Q {
                    self.deal_q(e, true);
                    e.lockout();
                } else if ab == AB_E {
                    self.deal_e(e, true);
                    e.lockout();
                } else if ab == AB_R {
                    e.prime_spellblade();
                    self.grant_note(e);
                    self.deal_r_damage(e, true);
                    e.st.next_attack = pymax(e.st.next_attack, t + self.r_total_lock_s);
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
