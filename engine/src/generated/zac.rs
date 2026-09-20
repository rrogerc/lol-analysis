//! Zac. His damage comes almost entirely from abilities: Let's Bounce! opens
//! the fight with four bounces on the stationary dummy, Elastic Slingshot is
//! pressed and instantly recast (charging only helps range), Stretching
//! Strikes lands its cast and then its tether-empowered next attack on the
//! same target, and Unstable Matter is woven on cooldown throughout,
//! including during Let's Bounce!. Cell Division's chunk pickup is
//! approximated as instantaneous, shaving Unstable Matter's cooldown.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// One of Let's Bounce!'s four impacts.
const EV_R_BOUNCE: u8 = 0;
/// Unstable Matter, woven on cooldown.
const EV_W_CAST: u8 = 1;
/// Elastic Slingshot, pressed and instantly recast.
const EV_E_CAST: u8 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    q_dmg: f64,
    q_cd: f64,
    q_empower_cast_s: f64,
    w_base: f64,
    w_pct: f64,
    w_ap_per100: f64,
    w_cd: f64,
    e_dmg: f64,
    e_cd: f64,
    r_full: f64,
    r_half: f64,
    r_cast_s: f64,
    r_interval_s: f64,
    r_bounces_total: i64,
    chunk_cdr_s: f64,
    src_q_emp: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// Stretching Strikes' tether is active, and whether the very next
    /// attack has already been earmarked as its empowered replacement.
    q_tether_armed: bool,
    q_pending_empowered: bool,
    w_ready: f64,
    e_ready: f64,
    r_bounces_done: i64,
    r_next_bounce_at: f64,
    /// Until this time, Let's Bounce! blocks attacks, Q and E.
    r_lock_until: f64,
}

impl GenDriver {
    /// A chunk from a landed ability hit, assumed picked up at once: shave
    /// Unstable Matter's current cooldown by the kit's per-chunk amount.
    fn shed_chunk(&mut self, e: &Engine, n: i64) {
        let mut wr = self.s.w_ready;
        let mut i = 0;
        while i < n {
            wr -= self.chunk_cdr_s;
            i += 1;
        }
        self.s.w_ready = pymax(e.st.t, wr);
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let q_dmg = kit.hit("gen.Q.damage", ranks.q, sheet)?;
        let q_cd = kit.at_rank("abilities.Q.cooldownS", ranks.q)?;
        let q_empower_cast_s = kit.num("gen.Q.empoweredCastTimeS")?;

        let w_base = kit.at_rank("gen.W.baseDamage", ranks.w)?;
        let w_pct = kit.at_rank("gen.W.percentMaxHp", ranks.w)?;
        let w_ap_per100 = kit.num("gen.W.apPercentPer100")?;
        let w_cd = kit.at_rank("abilities.W.cooldownS", ranks.w)?;

        let e_dmg = kit.hit("gen.E.damage", ranks.e, sheet)?;
        let e_cd = kit.at_rank("abilities.E.cooldownS", ranks.e)?;

        let r_full = kit.hit("gen.R.damage", ranks.r, sheet)?;
        let subsequent_factor = kit.num("gen.R.subsequentFactor")?;
        let r_half = r_full * subsequent_factor;
        let r_cast_s = kit.num("gen.R.castTimeS")?;
        let r_interval_s = kit.num("gen.R.intervalS")?;
        let r_bounces_total = kit.num("gen.R.bounces")? as i64;

        let chunk_cdr_s = kit.num("gen.P.chunkCdrS")?;

        let state = State {
            q_tether_armed: false,
            q_pending_empowered: false,
            w_ready: 0.0,
            e_ready: 0.0,
            r_bounces_done: 0,
            r_next_bounce_at: INF,
            r_lock_until: 0.0,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            q_dmg,
            q_cd,
            q_empower_cast_s,
            w_base,
            w_pct,
            w_ap_per100,
            w_cd,
            e_dmg,
            e_cd,
            r_full,
            r_half,
            r_cast_s,
            r_interval_s,
            r_bounces_total,
            chunk_cdr_s,
            src_q_emp: intern("Q empowered"),
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

    fn bonus_as(&self, _t: f64) -> f64 {
        0.0
    }

    fn attack_damage(&self, e: &Engine) -> f64 {
        // the tether's empowered attack deals no basic-attack damage
        if self.s.q_pending_empowered { 0.0 } else { e.p.ad }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
        shave(&mut self.s.e_ready, t, factor);
    }

    fn after_attack(&mut self, e: &mut Engine) {
        if self.s.q_pending_empowered {
            // the second Stretching Strike, landing on the same (only) target
            e.deal(self.q_dmg, DType::Magic, self.src_q_emp, false, true, 1.0);
            self.s.q_tether_armed = false;
            self.s.q_pending_empowered = false;
            e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        e.st.next_attack = t + e.attack_period(b);
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        if e.st.t < self.s.r_lock_until {
            return INF;
        }
        if self.s.q_tether_armed {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.deal(self.q_dmg, DType::Magic, SRC_Q, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.shed_chunk(e, 1);
        e.lockout();
        // the tether: Zac's next attack is replaced by the empowered strike,
        // and both Stretching Strikes reset the basic attack timer
        self.s.q_tether_armed = true;
        self.s.q_pending_empowered = true;
        e.st.next_attack = pymax(e.st.next_attack, t + self.q_empower_cast_s);
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r == 0 {
            return;
        }
        let t = e.st.t;
        self.s.r_bounces_done = 0;
        self.s.r_next_bounce_at = t + self.r_cast_s;
        self.s.r_lock_until =
            t + self.r_cast_s + (self.r_bounces_total - 1) as f64 * self.r_interval_s;
        // Q, E and basic attacks are locked out until the last bounce lands
        e.st.next_attack = pymax(e.st.next_attack, self.s.r_lock_until);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.s.r_bounces_done < self.r_bounces_total {
            out[n] = (self.s.r_next_bounce_at, Kind::Ev(EV_R_BOUNCE));
            n += 1;
        }
        if self.ranks.w > 0 {
            out[n] = (pymax(self.s.w_ready, e.st.t), Kind::Ev(EV_W_CAST));
            n += 1;
        }
        if self.ranks.e > 0 {
            let ready = pymax(self.s.e_ready, pymax(self.s.r_lock_until, e.st.t));
            out[n] = (ready, Kind::Ev(EV_E_CAST));
            n += 1;
        }
        n
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_R_BOUNCE) => {
                let dmg = if self.s.r_bounces_done == 0 { self.r_full } else { self.r_half };
                e.deal(dmg, DType::Magic, SRC_R, false, true, 1.0);
                e.ult_hatefog();
                self.shed_chunk(e, 1);
                self.s.r_bounces_done += 1;
                if self.s.r_bounces_done < self.r_bounces_total {
                    self.s.r_next_bounce_at = t + self.r_interval_s;
                } else {
                    self.s.r_next_bounce_at = INF;
                }
            }
            Kind::Ev(EV_W_CAST) => {
                let ap = e.p.sheet.ap;
                let dmg = self.w_base
                    + e.target_hp * (self.w_pct + self.w_ap_per100 * (ap / 100.0));
                e.deal(dmg, DType::Magic, SRC_W, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.w_ready = t + e.basic_cd(self.w_cd);
                self.shed_chunk(e, 1);
            }
            Kind::Ev(EV_E_CAST) => {
                e.deal(self.e_dmg, DType::Magic, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.shed_chunk(e, 1);
                self.s.e_ready = t + e.basic_cd(self.e_cd);
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
