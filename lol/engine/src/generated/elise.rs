//! Elise. Opens in Human Form with Neurotoxin (Q, current-HP based) and
//! Volatile Spiderling (W), then permanently switches to Spider Form: Q
//! becomes Venomous Bite (missing-HP based), W becomes Skittering Frenzy
//! (an attack-speed buff and attack reset), and every basic attack applies
//! Spider Queen's bonus on-hit magic damage. Casts go one at a time: a
//! single busy_until tracks the cast in progress (Q and Human W have real
//! cast times; Spider W is instant but still routed through the same
//! busy-until bookkeeping so it waits its turn). E is never cast.

use crate::fight::{shave, Driver, Engine, Events, Kind, St};
use crate::fx::*;
use crate::kit::Kit;
use crate::num::*;
use crate::sheet::Sheet;

/// The single W-slot event: casts Human W (and switches form) or Spider W.
const EV_W: u8 = 0;

#[derive(Clone, Debug, PartialEq)]
pub struct GenDriver {
    ranks: Ranks,
    attack_range: f64,
    windup_fraction: f64,

    q_cd: f64,
    q_cast_s: f64,
    q_human_base: f64,
    // Percent (of target current HP) per cast, e.g. 4 + 3%-per-100-AP*AP.
    q_human_ratio_pct: f64,
    q_spider_base: f64,
    q_spider_ratio_pct: f64,

    w_human_dmg: f64,
    w_human_cd: f64,
    w_human_cast_s: f64,
    w_spider_cd: f64,
    w_spider_cast_s: f64,
    w_spider_active_as_pct: f64,
    w_spider_buff_dur: f64,

    p_onhit_dmg: f64,

    src_q_spider: SourceId,
    src_p_onhit: SourceId,

    s: State,
    s0: State,
}

/// The rotation state a fight moves.
#[derive(Clone, Copy, Debug, PartialEq)]
struct State {
    /// false = Human Form (ranged Q/W), true = Spider Form (melee Q/W + on-hit).
    form_spider: bool,
    /// Next time the single W slot (Human or Spider variant) may be cast.
    w_ready: f64,
    /// Skittering Frenzy's active bonus attack speed lasts until this time.
    frenzy_until: f64,
    /// The cast in progress ends here: no other cast, no attack before it.
    busy_until: f64,
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
    fn new(kit: &Kit, sheet: &Sheet, _level: i64, ranks: Ranks, _prestacked: bool)
        -> Result<Self, String> {
        let ap = sheet.ap;
        let human_ratio_base = kit.num("gen.Q.human.hpRatioBasePct")?;
        let human_ratio_ap = kit.num("gen.Q.human.hpRatioApCoefPct")?;
        let spider_ratio_base = kit.num("gen.Q.spider.hpRatioBasePct")?;
        let spider_ratio_ap = kit.num("gen.Q.spider.hpRatioApCoefPct")?;

        let state = State {
            form_spider: false,
            w_ready: 0.0,
            frenzy_until: -1.0,
            busy_until: 0.0,
        };

        Ok(GenDriver {
            ranks,
            attack_range: sheet.base_attack_range,
            windup_fraction: kit.windup_fraction.ok_or("elise kit needs attack.windupFraction")?,

            q_cd: kit.at_rank("abilities.Q.cooldownS", ranks.q)?,
            q_cast_s: kit.num("gen.Q.castTimeS")?,
            q_human_base: kit.at_rank("gen.Q.human.base", ranks.q)?,
            q_human_ratio_pct: human_ratio_base + human_ratio_ap * ap,
            q_spider_base: kit.at_rank("gen.Q.spider.base", ranks.q)?,
            q_spider_ratio_pct: spider_ratio_base + spider_ratio_ap * ap,

            w_human_dmg: kit.hit("gen.W.human.damage", ranks.w, sheet)?,
            w_human_cd: kit.at_rank("abilities.W.cooldownS", ranks.w)?,
            w_human_cast_s: kit.num("gen.W.castTimeS")?,
            w_spider_cd: kit.num("gen.W.spider.cooldownS")?,
            w_spider_cast_s: kit.num("gen.W.spiderCastTimeS")?,
            w_spider_active_as_pct: kit.at_rank("gen.W.spider.activeAsPct", ranks.w)?,
            w_spider_buff_dur: kit.num("gen.W.spider.buffDurationS")?,

            p_onhit_dmg: kit.hit("gen.P.onhit", ranks.r, sheet)?,

            src_q_spider: intern("Q spider"),
            src_p_onhit: intern("P onhit"),

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
        if self.s.form_spider && t < self.s.frenzy_until {
            self.w_spider_active_as_pct
        } else {
            0.0
        }
    }

    fn shave_cooldowns(&mut self, st: &mut St, t: f64, factor: f64) {
        shave(&mut st.q_ready, t, factor);
        shave(&mut self.s.w_ready, t, factor);
    }

    fn attack_riders(&mut self, e: &mut Engine) {
        // Spider Queen's bonus on-hit magic damage, Spider Form only.
        if self.s.form_spider {
            e.deal(self.p_onhit_dmg, DType::Magic, self.src_p_onhit, false, false, 1.0);
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
        e.st.q_ready = t + e.basic_cd(self.q_cd);
        let dmg;
        let src;
        if self.s.form_spider {
            // Venomous Bite: missing health.
            let missing = e.target_hp - pymax(e.st.hp, 0.0);
            dmg = self.q_spider_base + self.q_spider_ratio_pct * missing / 100.0;
            src = self.src_q_spider;
        } else {
            // Neurotoxin: current health.
            let current = pymax(e.st.hp, 0.0);
            dmg = self.q_human_base + self.q_human_ratio_pct * current / 100.0;
            src = SRC_Q;
        }
        e.deal(dmg, DType::Magic, src, false, true, 1.0);
        e.ability_cast_proc();
        e.eclipse_hit();
        e.prime_spellblade();
        self.busy_for(e, self.q_cast_s);
    }

    fn events(&self, e: &Engine, out: &mut Events) -> usize {
        if self.ranks.w > 0 {
            out[0] = (self.castable_at(e, self.s.w_ready), Kind::Ev(EV_W));
            1
        } else {
            0
        }
    }

    fn on_event(&mut self, e: &mut Engine, kind: Kind) {
        let t = e.st.t;
        match kind {
            Kind::Ev(EV_W) => {
                if !self.s.form_spider {
                    // Human Form: Volatile Spiderling (has a cast time),
                    // then switch forms (unless Spider Form is not learned
                    // yet) at no extra cast time or cooldown cost of its own.
                    e.deal(self.w_human_dmg, DType::Magic, SRC_W, false, true, 1.0);
                    e.ability_cast_proc();
                    e.eclipse_hit();
                    e.prime_spellblade();
                    self.busy_for(e, self.w_human_cast_s);
                    if self.ranks.r > 0 {
                        self.s.form_spider = true;
                        // Skittering Frenzy is a fresh, separate cooldown.
                        self.s.w_ready = t;
                    } else {
                        self.s.w_ready = t + e.basic_cd(self.w_human_cd);
                    }
                } else {
                    // Spider Form: Skittering Frenzy. No damage of its own;
                    // grants attack speed and resets Elise's attack timer.
                    self.s.frenzy_until = t + self.w_spider_buff_dur;
                    self.s.w_ready = t + e.basic_cd(self.w_spider_cd);
                    e.prime_spellblade();
                    let b = self.bonus_as(t);
                    e.st.next_attack = t + e.attack_windup(b, self.windup_fraction);
                    // Cast time is 0 (wiki: "cast time = none"), but it still
                    // runs through the same busy-until bookkeeping.
                    self.busy_for(e, self.w_spider_cast_s);
                }
            }
            other => panic!("unhandled event {other:?}"),
        }
    }
}
