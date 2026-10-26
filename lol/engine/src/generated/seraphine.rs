//! Seraphine. A mixed AA/ability attacker: Stage Presence's Echo doubles
//! whichever basic ability cast lands at 2 stacks (starting there), Harmony
//! grants a Note per ability cast that a subsequent attack fires as bonus
//! magic damage, and High Note / Beat Drop / Encore go out on cooldown.
//! Casts go one at a time: one busy_until in the state blocks any other cast
//! and any attack for a cast's own cast time (Encore's attacks are held a
//! further half second beyond that, for its second, cast-free lockout phase).
//! Surround Sound (W) is never cast: it deals no damage, so its cast time is
//! carried in the kit (gen.W.castTimeS) but never read by this driver.

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
    r_attack_lock_s: f64,
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
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
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

    fn grant_note(&mut self, e: &Engine) {
        self.s.note_count = imin(self.s.note_count + 1, self.note_cap);
        self.s.note_until = e.st.t + self.note_duration;
    }

    /// Registers a basic ability cast against Echo: if it was already at cap
    /// it consumes the stacks and schedules a free extra cast once this one
    /// fully completes, otherwise it just adds a stack.
    fn maybe_echo(&mut self, t: f64, completes_in: f64, ability: i64) {
        if self.s.echo_stacks >= self.echo_cap {
            self.s.echo_stacks = 0;
            self.s.echo_pending_ability = ability;
            self.s.echo_pending_at = t + completes_in + self.echo_delay;
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

    fn cast_q_impl(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        self.deal_q(e, false);
        self.maybe_echo(t, self.cast_s_q, AB_Q);
        self.busy_for(e, self.cast_s_q);
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
        self.maybe_echo(t, self.cast_s_e, AB_E);
        self.busy_for(e, self.cast_s_e);
    }

    fn deal_r_damage(&mut self, e: &mut Engine, is_echo: bool) {
        let src = if is_echo { self.src_r_echo } else { SRC_R };
        e.deal(self.r_dmg, DType::Magic, src, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.ult_hatefog();
    }

    /// One full Encore cast: the busy_until is only the first cast time (the
    /// second phase locks attacks alone), the damage lands as a delayed
    /// event, and the mimicked-ability completion used for Echo purposes is
    /// the full attack-lock window (both phases).
    fn begin_r_cast(&mut self, e: &mut Engine, is_echo_source: bool) {
        let t = e.st.t;
        e.prime_spellblade();
        self.grant_note(e);
        self.s.r_dmg_at = t + self.r_cast_s;
        self.busy_for(e, self.r_cast_s);
        e.st.next_attack = pymax(e.st.next_attack, t + self.r_attack_lock_s);
        if !is_echo_source {
            self.maybe_echo(t, self.r_attack_lock_s, AB_R);
        }
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let echo_cap = kit.num("gen.P.echoMaxStacks")? as i64;
        let state = State {
            busy_until: 0.0,
            echo_stacks: echo_cap,
            echo_pending_ability: 0,
            echo_pending_at: INF,
            note_count: 0,
            note_until: 0.0,
            e_ready: 0.0,
            r_ready: 0.0,
            r_dmg_at: INF,
        };
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
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_attack_lock_s: kit.num("gen.R.attackLockS")?,
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
        self.castable_at(e, e.st.q_ready)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        self.cast_q_impl(e);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        // the opening cast: the engine has already primed Spellblade and
        // held the first attack past the 0.25s window
        let t = e.st.t;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        self.begin_r_cast(e, false);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            out[n] = (self.castable_at(e, self.s.e_ready), Kind::Ev(EV_E));
            n += 1;
        }
        if self.ranks.r > 0 {
            out[n] = (self.castable_at(e, self.s.r_ready), Kind::Ev(EV_R_READY));
            n += 1;
        }
        if self.s.r_dmg_at != INF {
            // a delayed hit, not a cast: reports its own fixed time
            out[n] = (self.s.r_dmg_at, Kind::Ev(EV_R_DMG));
            n += 1;
        }
        if self.s.echo_pending_at != INF {
            out[n] = (self.castable_at(e, self.s.echo_pending_at), Kind::Ev(EV_ECHO));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_E) => {
                self.cast_e(e);
            }
            Kind::Ev(EV_R_READY) => {
                self.s.r_ready = t + e.ult_cd(self.r_cd);
                self.begin_r_cast(e, false);
            }
            Kind::Ev(EV_R_DMG) => {
                self.s.r_dmg_at = INF;
                self.deal_r_damage(e, false);
            }
            Kind::Ev(EV_ECHO) => {
                let ab = self.s.echo_pending_ability;
                self.s.echo_pending_ability = 0;
                self.s.echo_pending_at = INF;
                if ab == AB_Q {
                    self.deal_q(e, true);
                    self.busy_for(e, self.cast_s_q);
                } else if ab == AB_E {
                    self.deal_e(e, true);
                    self.busy_for(e, self.cast_s_e);
                } else if ab == AB_R {
                    self.begin_r_cast(e, true);
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
