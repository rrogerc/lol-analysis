//! Bel'Veth. Q is cast on an averaged (4-way-cycled) direction cooldown that
//! also resets Bel'Veth's attack timer; W is a short-cast tail slam that
//! also refreshes the averaged Q cooldown (approximating its champion-hit
//! direction reset); E is a channeled multi-slash that blocks Q, W and
//! normal attacks for its duration, with a slash count set by bonus attack
//! speed at cast; Endless Banquet's ever-growing passive bonus true damage
//! rides every qualifying on-hit application (full on attacks and Q, reduced
//! during E's slashes).

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Above and Below: the cast begins, then its damage lands after the cast time.
const EV_W_CAST: u8 = 0;
const EV_W_LAND: u8 = 1;
/// Royal Maelstrom: the channel begins, then each slash fires in turn.
const EV_E_CAST: u8 = 2;
const EV_E_SLASH: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// Death in Lavender: permanent stacks' bonus AS% at this level (the
    /// dossier's per-level table is already in percent), the assumed
    /// permanent stack count, and the post-cast ghost buff.
    p_as_per_stack_pct: f64,
    p_stacks_assumed: i64,
    p_sheen_as_pct: f64,
    p_sheen_dur_s: f64,
    q_dmg: f64,
    /// The averaged (per-side / 4) Q cooldown, already reduced by the
    /// bonus-AS-to-haste conversion (excluding the temporary ghost buff).
    q_cd_avg: f64,
    w_dmg: f64,
    w_cd: f64,
    w_cast_s: f64,
    e_slash_base: f64,
    e_missing_mult: f64,
    e_duration_s: f64,
    e_base_slashes: i64,
    e_as_per_slash: f64,
    e_onhit_ratio: f64,
    e_cd: f64,
    r_onhit_val: f64,
    src_r_onhit: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    ghost_until: f64,
    w_ready: f64,
    /// When a cast Above and Below lands (INF: none pending).
    w_land_at: f64,
    e_ready: f64,
    /// When the next Royal Maelstrom slash fires (INF: not channeling).
    e_next_slash_at: f64,
    e_slash_interval: f64,
    e_slashes_left: i64,
    /// Endless Banquet's ever-growing on-hit true-damage stacks.
    r_stacks: i64,
}

impl GenDriver {
    /// One qualifying on-hit application of Endless Banquet's passive: the
    /// stack always advances by one, the damage dealt scales by `effectiveness`
    /// (1.0 on attacks and Void Surge, reduced during Royal Maelstrom).
    fn apply_r_onhit(&mut self, e: &mut Engine, effectiveness: f64) {
        self.s.r_stacks += 1;
        let dmg = self.r_onhit_val * (self.s.r_stacks as f64) * effectiveness;
        e.deal(dmg, DType::True, self.src_r_onhit, false, false, 1.0);
    }

    fn channeling(&self) -> bool {
        self.s.e_next_slash_at != INF
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        // the dossier's per-level table is already in percent (its own
        // quote spells out "0.1%; then +0.05*x..."), so it is used directly
        let p_as_per_stack_pct = kit.at_level("gen.P.asPerStackByLevel", level)?;
        let p_stacks_assumed = kit.num("gen.P.assumedStacks")? as i64;

        let q_perside_base = kit.at_rank("gen.Q.perSideCooldownS", ranks.q)?;
        let q_cd_as_mult = kit.num("gen.Q.perSideCdAsMult")?;
        let as_for_cdr = sheet.bonus_as_pct + (p_stacks_assumed as f64) * p_as_per_stack_pct;
        let eff_haste = q_cd_as_mult * as_for_cdr;
        let reduced_cd = q_perside_base / (1.0 + eff_haste / 100.0);
        let q_cd_avg = reduced_cd / 4.0;

        let state = State {
            ghost_until: 0.0,
            w_ready: 0.0,
            w_land_at: INF,
            e_ready: 0.0,
            e_next_slash_at: INF,
            e_slash_interval: 0.0,
            e_slashes_left: 0,
            r_stacks: 0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("belveth kit needs attack.windupFraction")?,
            p_as_per_stack_pct,
            p_stacks_assumed,
            p_sheen_as_pct: kit.num("gen.P.sheenAsPct")? * 100.0,
            p_sheen_dur_s: kit.num("gen.P.sheenDurationS")?,
            q_dmg: kit.hit("gen.Q.damage", ranks.q, sheet)?,
            q_cd_avg,
            w_dmg: kit.hit("gen.W.damage", ranks.w, sheet)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cast_s: kit.num("gen.W.castTimeS")?,
            e_slash_base: kit.hit("gen.E.damage", ranks.e, sheet)?,
            e_missing_mult: kit.num("gen.E.missingHealthMult")?,
            e_duration_s: kit.num("gen.E.durationS")?,
            e_base_slashes: kit.num("gen.E.baseSlashes")? as i64,
            e_as_per_slash: kit.num("gen.E.bonusAsPerSlash")?,
            e_onhit_ratio: kit.num("gen.E.onHitRatio")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            r_onhit_val: kit.hit("gen.R.onhit", ranks.r, sheet)?,
            src_r_onhit: intern("R onhit"),
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
        let mut v = self.p_as_per_stack_pct * (self.p_stacks_assumed as f64);
        if t < self.s.ghost_until {
            v += self.p_sheen_as_pct;
        }
        v
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        // Endless Banquet: every basic-attack on-hit application at full
        // effectiveness
        if self.ranks.r > 0 {
            self.apply_r_onhit(e, 1.0);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if self.channeling() {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.st.q_ready = t + self.q_cd_avg;
        e.deal(self.q_dmg, DType::Physical, SRC_Q, true, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        if self.ranks.r > 0 {
            // Void Surge applies on-hit to the first target, at full effect
            self.apply_r_onhit(e, 1.0);
        }
        self.s.ghost_until = t + self.p_sheen_dur_s;
        // Void Surge resets Bel'Veth's basic attack timer
        e.st.next_attack = t + e.attack_windup(self.bonus_as(t), self.windup_fraction);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        let channeling = self.channeling();
        if self.ranks.w > 0 && !channeling {
            if self.s.w_land_at != INF {
                out[n] = (self.s.w_land_at, Kind::Ev(EV_W_LAND));
            } else {
                out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            }
            n += 1;
        }
        if self.ranks.e > 0 {
            if self.s.e_next_slash_at != INF {
                out[n] = (self.s.e_next_slash_at, Kind::Ev(EV_E_SLASH));
            } else {
                out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            }
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W_CAST) => {
                self.s.w_land_at = t + self.w_cast_s;
                e.lockout();
                e.prime_spellblade();
            }
            Kind::Ev(EV_W_LAND) => {
                self.s.w_land_at = INF;
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                e.deal(self.w_dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.ghost_until = t + self.p_sheen_dur_s;
                // Above and Below hits the (champion-flagged) dummy: reset
                // the averaged Void Surge cooldown, approximating its
                // direction-cooldown refund
                e.st.q_ready = t;
            }
            Kind::Ev(EV_E_CAST) => {
                let total_bonus_as = e.p.sheet.bonus_as_pct + self.bonus_as(t);
                let extra = (total_bonus_as / self.e_as_per_slash) as i64;
                let slashes = self.e_base_slashes + extra;
                self.s.e_slashes_left = slashes;
                self.s.e_slash_interval = self.e_duration_s / (slashes as f64);
                self.s.e_next_slash_at = t + self.s.e_slash_interval;
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                // the channel locks out movement/attacks and other casts for
                // its full duration
                e.st.next_attack = pymax(e.st.next_attack, t + self.e_duration_s);
                e.prime_spellblade();
                e.ability_cast_proc();
                e.eclipse_hit();
                self.s.ghost_until = t + self.p_sheen_dur_s;
            }
            Kind::Ev(EV_E_SLASH) => {
                let missing_frac = (e.target_hp - pymax(e.st.hp, 0.0)) / e.target_hp;
                let dmg = self.e_slash_base * (1.0 + self.e_missing_mult * missing_frac);
                e.deal(dmg, DType::Physical, SRC_E, true, true, 1.0);
                if self.ranks.r > 0 {
                    self.apply_r_onhit(e, self.e_onhit_ratio * missing_frac);
                }
                self.s.e_slashes_left -= 1;
                if self.s.e_slashes_left > 0 {
                    self.s.e_next_slash_at = t + self.s.e_slash_interval;
                } else {
                    self.s.e_next_slash_at = INF;
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
