//! Illaoi. Tentacle slams (a shared formula amplified by Tentacle Smash's
//! rank and reduced by a shared 0.66 s same-target stacking cap) are the
//! backbone: Leap of Faith opens for its own damage, an extra Tentacle and a
//! halved Harsh Lesson cooldown; Tentacle Smash goes out on cooldown; Harsh
//! Lesson is woven in via an attack reset and commands the available
//! Tentacle(s) to slam; Test of Spirit is modeled as a single echo instance
//! plus one autonomous Vessel-phase slam per cast, on cooldown.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// Test of Spirit resolves (echo damage), then its own cooldown.
const EV_E_CAST: u8 = 0;
/// The autonomous Vessel-phase Tentacle slam that follows an E cast.
const EV_E_VESSEL: u8 = 1;
/// Leap of Faith is (re)cast.
const EV_R_CAST: u8 = 2;
/// Leap of Faith's damage lands, after its cast time.
const EV_R_SWING: u8 = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,
    /// A tentacle slam's total damage: the passive formula amplified by
    /// Tentacle Smash's rank bonus.
    p_slam_dmg: f64,
    p_reduction_per_hit: f64,
    p_reduction_cap: f64,
    p_reduction_window: f64,
    q_cd: f64,
    w_pct_frac: f64,
    w_min: f64,
    w_cd: f64,
    w_cd_r: f64,
    e_cd: f64,
    e_echo_dmg: f64,
    e_vessel_interval: f64,
    e_vessel_fires: bool,
    r_dmg: f64,
    r_cd: f64,
    r_cast_s: f64,
    r_active_dur: f64,
    src_w_slam: SourceId,
    src_e_vessel: SourceId,
    s: State,
    s0: State,
}

/// Everything a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    w_ready: f64,
    w_armed: bool,
    e_ready: f64,
    /// When the pending Vessel-phase slam lands (INF: none pending).
    e_vessel_at: f64,
    r_ready: f64,
    /// When Leap of Faith's damage lands (INF: none pending).
    r_swing_at: f64,
    /// Until when Leap of Faith's Tentacle/Harsh Lesson buff is active.
    r_active_until: f64,
    /// A ring buffer of recent slam timestamps against the dummy.
    slam_hist: [f64; 4],
    slam_idx: i64,
}

impl GenDriver {
    /// A tentacle slam against the dummy: applies the shared 0.66 s
    /// stacking damage reduction, then records the hit.
    fn fire_slam(&mut self, e: &mut Engine, src: SourceId) {
        let t = e.st.t;
        let mut count = 0.0;
        let mut i = 0usize;
        while i < 4 {
            let ts = self.s.slam_hist[i];
            if t - ts < self.p_reduction_window {
                count += 1.0;
            }
            i += 1;
        }
        let reduction = pymin(self.p_reduction_cap, self.p_reduction_per_hit * count);
        let idx = (self.s.slam_idx % 4) as usize;
        self.s.slam_hist[idx] = t;
        self.s.slam_idx += 1;
        let amt = self.p_slam_dmg * (1.0 - reduction);
        e.deal(amt, DType::Physical, src, false, true, 1.0);
    }

    /// Harsh Lesson's empowered attack lands: its own bonus damage plus the
    /// slam(s) it commands (two while Leap of Faith's Tentacle is up).
    fn spend_empower(&mut self, e: &mut Engine) {
        let t = e.st.t;
        self.s.w_armed = false;
        let base = if t < self.s.r_active_until { self.w_cd_r } else { self.w_cd };
        self.s.w_ready = t + e.basic_cd(base);
        let amt = pymax(self.w_pct_frac * e.target_hp, self.w_min);
        e.deal(amt, DType::Physical, SRC_W, false, true, 1.0);
        let n = if t < self.s.r_active_until { 2 } else { 1 };
        let mut i = 0;
        while i < n {
            self.fire_slam(e, self.src_w_slam);
            i += 1;
        }
    }

    /// Casts (or recasts) Leap of Faith: schedules its swing and cooldown.
    fn do_r_cast(&mut self, e: &mut Engine) {
        let t = e.st.t;
        e.lockout();
        self.s.r_swing_at = t + self.r_cast_s;
        self.s.r_ready = t + e.ult_cd(self.r_cd);
        e.prime_spellblade();
    }
}

impl Driver for GenDriver {
    fn new(kit: &Kit, sheet: &Sheet, level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let p_base = kit.at_level("gen.P.slamBaseByLevel", level)?;
        let p_ad_ratio = kit.num("gen.P.slamAdRatio")?;
        let p_ap_ratio = kit.num("gen.P.slamApRatio")?;
        let q_amp = kit.at_rank("gen.Q.tentacleDamageAmp", ranks.q)?;
        let p_slam_dmg = (p_base + p_ad_ratio * sheet.ad + p_ap_ratio * sheet.ap) * (1.0 + q_amp);

        let w_base_pct = kit.at_rank("gen.W.healthPercentDamage", ranks.w)?;
        let w_ad_ratio = kit.num("gen.W.healthDamageAdRatio")?;
        let w_divisor = kit.num("gen.W.percentToFractionDivisor")?;
        let w_pct_frac = (w_base_pct + w_ad_ratio * sheet.ad) / w_divisor;

        let e_base_pct = kit.at_rank("gen.E.damageTransferPercent", ranks.e)?;
        let e_tad = kit.num("gen.E.echoTadScalar")?;
        let echo_frac = e_base_pct + e_tad * sheet.ad;
        let e_echo_dmg = echo_frac * p_slam_dmg;
        let e_vessel_interval = kit.at_level("gen.E.vesselSlamIntervalByLevel", level)?;
        let e_vessel_duration = kit.num("gen.E.vesselDuration")?;

        let state = State {
            w_ready: 0.0,
            w_armed: false,
            e_ready: 0.0,
            e_vessel_at: INF,
            r_ready: 0.0,
            r_swing_at: INF,
            r_active_until: 0.0,
            slam_hist: [-INF; 4],
            slam_idx: 0,
        };
        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("illaoi kit needs attack.windupFraction")?,
            p_slam_dmg,
            p_reduction_per_hit: kit.num("gen.P.slamReductionPerHit")?,
            p_reduction_cap: kit.num("gen.P.slamReductionCapPct")?,
            p_reduction_window: kit.num("gen.P.slamReductionWindowS")?,
            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            w_pct_frac,
            w_min: kit.at_rank("gen.W.minDamage", ranks.w)?,
            w_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_cd_r: kit.num("gen.W.cooldownDuringR")?,
            e_cd: kit.at_rank("abilities.E.cooldownS", ranks.e)?,
            e_echo_dmg,
            e_vessel_interval,
            e_vessel_fires: e_vessel_interval <= e_vessel_duration,
            r_dmg: kit.hit("gen.R.damage", ranks.r, sheet)?,
            r_cd: kit.at_rank("abilities.R.cooldownS", ranks.r)?,
            r_cast_s: kit.num("gen.R.castTimeS")?,
            r_active_dur: kit.num("gen.R.activeDurationS")?,
            src_w_slam: intern("W slam"),
            src_e_vessel: intern("E vessel"),
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
        if self.s.w_armed {
            self.spend_empower(e);
        }
    }

    fn schedule_attack(&mut self, e: &mut Engine) {
        let t = e.st.t;
        let b = self.bonus_as(t);
        if self.ranks.w > 0 && !self.s.w_armed && t >= self.s.w_ready {
            // Harsh Lesson, woven in right after an attack: its reset
            // brings the next attack one windup away.
            self.s.w_armed = true;
            e.prime_spellblade();
            e.ability_cast_proc();
            e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
        } else {
            e.st.next_attack = t + e.attack_period(b);
        }
    }

    fn q_at(&self, e: &Engine) -> f64 {
        if self.ranks.q == 0 {
            return INF;
        }
        pymax(e.st.q_ready, e.st.t)
    }

    fn cast_q(&mut self, e: &mut Engine) {
        e.st.q_ready = e.st.t + e.basic_cd(self.q_cd);
        self.fire_slam(e, SRC_Q);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        e.lockout();
    }

    fn cast_r(&mut self, e: &mut Engine) {
        if self.ranks.r > 0 {
            self.do_r_cast(e);
        }
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        let mut n = 0;
        if self.ranks.e > 0 {
            out[n] = (pymax(self.s.e_ready, e.st.t), Kind::Ev(EV_E_CAST));
            n += 1;
        }
        if self.s.e_vessel_at != INF {
            out[n] = (self.s.e_vessel_at, Kind::Ev(EV_E_VESSEL));
            n += 1;
        }
        if self.ranks.r > 0 {
            if self.s.r_swing_at != INF {
                out[n] = (self.s.r_swing_at, Kind::Ev(EV_R_SWING));
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
                // Test of Spirit: the Spirit is assumed to die almost
                // instantly to Illaoi's own hit, so it resolves as one
                // echo instance right away.
                e.lockout();
                e.deal(self.e_echo_dmg, DType::Physical, SRC_E, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.prime_spellblade();
                self.s.e_ready = t + e.basic_cd(self.e_cd);
                self.s.e_vessel_at = if self.e_vessel_fires { t + self.e_vessel_interval } else { INF };
            }
            Kind::Ev(EV_E_VESSEL) => {
                // The one autonomous Vessel-phase Tentacle attack.
                self.s.e_vessel_at = INF;
                let src = self.src_e_vessel;
                self.fire_slam(e, src);
            }
            Kind::Ev(EV_R_CAST) => {
                self.do_r_cast(e);
            }
            Kind::Ev(EV_R_SWING) => {
                self.s.r_swing_at = INF;
                e.deal(self.r_dmg, DType::Physical, SRC_R, false, true, 1.0);
                e.ability_cast_proc();
                e.eclipse_hit();
                e.ult_hatefog();
                self.s.r_active_until = t + self.r_active_dur;
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
