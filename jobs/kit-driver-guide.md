# Writing a champion's kit and rotation driver

You write two files for one League of Legends champion: a **kit** (JSON: the
numbers) and a **driver** (Rust: the rotation — what the champion does with
its attacks and abilities). A damage simulator compiles the driver into its
engine and fights item builds against a stat dummy to rank them. A script
compiles what you write, runs fights with it and sends you what fails.

Your sources are the champion's **dossier** (numbers cross-checked between
Riot's game file and the wiki, mechanics with quotes, open questions, a
proposed rotation), the **numbers sheet** (Riot's file) and the wiki
templates. Use their numbers exactly. Where the sources leave a question
open, pick the most common in-game reading, and say so in the kit's `notes`.

## The fight

One champion at a level (usually 16) with six items fights ONE stationary
dummy for 8-15 seconds. The dummy has health, armor and magic resistance,
never fights back, never moves, and counts as an enemy champion. The driver
plays for maximum damage. There are no allies, minions or terrain: every
skillshot hits, anything that needs another unit does nothing, and anything
the dummy would have to do (attack, move, cast) never happens. Healing,
shields, crowd control and mobility do not matter, except that a cast time
or a channel still costs the time it takes.

The engine is an event loop. It owns the basic attack (damage, crit as an
expected value, attack speed, every item effect and on-hit), the clock, and
damage mitigation. The driver decides when abilities are cast, deals their
damage through the engine, and keeps the kit's own state (stacks, buffs,
cooldowns other than Q's).

## The driver

A driver is `pub struct GenDriver` implementing `crate::fight::Driver`. The
engine calls these hooks:

| hook | when | default |
|---|---|---|
| `new(kit, sheet, level, ranks, prestacked) -> Result<Self, String>` | once per item build; work out every constant here | required |
| `reset(&mut self)` | before each fight | required: `self.s = self.s0;` |
| `ranged(&self) -> bool`, `attack_range(&self) -> f64` | setup | required |
| `bonus_as(&self, t) -> f64` | whenever attack speed is needed | 0 — kit attack speed in PERCENT (30.0 = +30%) at time `t` |
| `attack_damage(&self, e) -> f64` | each attack | `e.p.ad` — override to add a kit AD steroid while it runs |
| `shave_cooldowns(&mut self, st, t, factor)` | an item shortens basic cooldowns on-attack | shaves Q; override to `shave` every basic ability's ready time (not R) |
| `before_attack(&mut self, e)` | an attack starts, before its damage | nothing — on-attack stacks go here |
| `attack_riders(&mut self, e)` | with every application of on-hit effects (an item can apply them twice per attack) | nothing — the kit's on-hit damage goes here |
| `after_attack(&mut self, e)` | after the attack's damage and on-hits | nothing — an empowered attack's bonus goes here |
| `schedule_attack(&mut self, e)` | last thing of an attack: sets `e.st.next_attack` | `t + e.attack_period(bonus_as)` — override for an attack reset |
| `q_at(&self, e) -> f64` | every loop: the earliest time Q can be cast, `INF` for never | `max(e.st.q_ready, now)` when Q is learned |
| `cast_q(&mut self, e)` | the clock reached `q_at` | required (may be empty if `q_at` is `INF`) |
| `cast_r(&mut self, e)` | t = 0, if the ult is learned and allowed: the opening cast | nothing |
| `events(&self, e, out) -> usize` | every loop: the driver's own pending timed events | none |
| `on_event(&mut self, e, kind)` | one of them came due | panics |

Q is wired into the engine (`q_at` / `cast_q`, its ready time is
`e.st.q_ready`). W, E, a recast R, ticks, delayed hits and buff expiries are
**events**: `events` writes `(time, Kind::Ev(i))` pairs into `out` (at most 8)
and returns how many; when the clock reaches the earliest one the engine
calls `on_event(e, Kind::Ev(i))`. At one instant a lower `i` fires first.
Give each `i` a `const EV_...: u8`.

**An event must not come due for ever.** `events` is asked again after every
event. If it reports a time at or before now and `on_event` does not change
the state that made it due, the fight never ends (the engine aborts it). Do
what the reference driver does: report `pymax(ready, e.st.t)` for a cast,
and in `on_event` always move that ready time forward (or set a flag) before
returning.

The opening: the engine calls `cast_r` at t = 0 (it has already primed
Spellblade and delayed the first attack by the 0.25 s cast). An ult that
deals damage after its cast time schedules its own event, as the reference
driver does. An ult that is recast during the fight reports a later event
for it and uses `e.ult_cd(base)` for its cooldown. NEVER put a `damage` key
directly under `abilities.R` in the kit: the engine would cast it itself.

### The engine, as a driver sees it (`e: &mut Engine`)

- `e.st.t` the clock; `e.st.next_attack` when the next attack lands (write
  it only in `schedule_attack`, or to hold attacks during a channel:
  `e.st.next_attack = pymax(e.st.next_attack, until)`); `e.st.q_ready`.
- `e.st.hp` the dummy's current health, `e.target_hp` its maximum: damage
  based on the target's health is `ratio * e.target_hp` (maximum),
  `ratio * pymax(e.st.hp, 0.0)` (current),
  `ratio * (e.target_hp - pymax(e.st.hp, 0.0))` (missing).
- `e.p.ad`, and the sheet `e.p.sheet` (fields below), for numbers that must
  be read at the moment of a hit. Everything that does not change inside a
  fight belongs in `new`.
- `e.deal(amount, dtype, source, crit_mod, ability, 1.0)` deals PRE-mitigation
  damage: `dtype` is `DType::Physical | Magic | True`; `source` a label id
  (below); `crit_mod: true` only for damage the wiki says can critically
  strike (it is multiplied by the build's expected crit); `ability: true` for
  ability damage — it triggers item effects that key on ability damage (burns,
  Shojin, Horizon Focus) — and `false` for on-hit and proc damage. The last
  argument is always `1.0`.
- After an ability that deals damage on cast: `e.ability_cast_proc();` (Muramana)
  and `e.eclipse_hit();` once per cast, and `e.prime_spellblade();` for every
  ability cast, damaging or not (Sheen items).
- `e.lockout()` a cast with a cast time delays the next attack by 0.25 s. An
  instant ability (no cast time, or an attack modifier) does not call it.
- `e.basic_cd(base_cd)` a basic ability's cooldown after ability haste;
  `e.ult_cd(base_cd)` the ult's. A cooldown that starts when the effect ends
  ("post-effect") is set at that moment, not at the cast.
- `e.attack_period(bonus_as_pct)` seconds between attacks;
  `e.attack_windup(bonus_as_pct, windup_fraction)` the windup. An attack
  reset is `e.st.next_attack = t + e.attack_windup(b, self.windup_fraction)`
  inside `schedule_attack` (see the reference driver).
- `e.ult_hatefog()` after an ult's damage lands (Malignance).
- Debuffs on the target: `e.st.kit_amp_pct = x; e.st.kit_amp_mult = 1.0 + x / 100.0;
  e.st.kit_amp_until = t + dur;` makes the dummy take x% more non-true damage
  until then. A percent armor / magic resistance shred: declare it in the kit
  as `"abilities": {"Q": {"shred": {"pct": 15, "appliesTo": ["armor", "mr"], "durationS": 4}}}`
  (the one slot the engine reads, whichever ability applies it) and switch it
  on with `e.st.shred_until = t + dur`.
- Mana and energy are NOT modeled by the engine. Ignore costs unless they
  would really stop casts inside 15 s at level 16 (energy, a cost that
  doubles); then keep a pool in your state, as any other counter.

`Sheet` fields: `ad`, `ad_base`, `ad_bonus`, `ap`, `hp`, `hp_bonus`, `mana`,
`mana_bonus`, `armor`, `mr`, `attack_speed`, `bonus_as_pct`, `crit_chance`
(0-100), `crit_damage` (percent, 175 = 1.75x), `haste`, `lethality`,
`move_speed`, `base_attack_range`.

Source labels for the damage breakdown: `SRC_Q`, `SRC_W`, `SRC_E`, `SRC_R`
exist; make others in `new` with `intern("W onhit")` and keep the `SourceId`
in the struct. Every label starts with its slot letter (`P`, `Q`, `W`, `E`,
`R`): "P", "Q empowered", "E tick".

### The kit, as a driver sees it (`kit: &Kit`, only inside `new`)

Values by dotted path; every error names the path, so use `?`:

- `kit.num("gen.P.maxStacks")? -> f64`, `kit.num_or(path, default)`,
  `kit.has(path)`, `kit.flag(path)`
- `kit.at_rank("abilities.Q.cooldownS", ranks.q)?` a by-rank list at a rank
  (a plain number is the same at every rank; rank 0 reads 0)
- `kit.at_level("gen.P.byLevel", level)?` an 18-entry list at the champion's level
- `kit.hit("gen.Q.damage", ranks.q, sheet)?` a damage block evaluated on the
  sheet: `{"base": [..by rank..], "apRatio", "adRatio", "bonusAdRatio"}` are
  FRACTIONS of the caster's AP / total AD / bonus AD; `"maxHpRatio",
  "bonusHpRatio", "maxManaRatio"` are PERCENT of the caster's own health /
  mana (2 = 2%). 0 for an ability not learned. Scaling on the TARGET's health
  is not part of a block: keep the fraction as its own number and apply it at
  the hit.
- `kit.windup_fraction` (an `Option<f64>`, the script fills `attack.windupFraction`).

### Rules the script enforces

1. The file defines `pub struct GenDriver` with `#[derive(Clone, Debug, PartialEq)]`,
   keeps everything a fight changes in one `#[derive(Clone, Copy, Debug, PartialEq)]`
   state struct held twice (`s`, and the pristine `s0`), and `reset` is
   `self.s = self.s0;`. Constants worked out in `new` live beside them.
2. No game number in the Rust: every number comes from the kit by path. The
   only literals allowed are 0, 1, 2, 100, small counts and indices, `INF`,
   and `ABILITY_LOCKOUT_S` (0.25, the cast lockout).
3. Only these imports: `use crate::fight::{shave, Driver, Engine, Events, Kind, St};
   use crate::fx::*; use crate::kit::Kit; use crate::num::*; use crate::sheet::Sheet;`
   (drop what you do not use). No `unsafe`, no `std::` paths, no macros of
   your own, no I/O, no threads, no allocation in the hooks a fight calls
   (no `Vec`, `String`, `Box`, `format!` outside `new`).
4. Floats: use `pymax(a, b)`, `pymin(a, b)` (and `imin`, `imax` for `i64`)
   instead of `.max()` / `.min()`; no `mul_add`, no `powi` / `powf` (multiply
   in a loop).
5. Abilities with rank 0 are never cast (`ranks.q == 0` etc.; at level 16 with
   the usual max order all are learned, but the checks also run level 1).
6. Never `panic!` / `unwrap()` on kit data: `new` returns `Err(String)`.
   `on_event` ends with `other => panic!("unhandled event {other:?}")`.
7. Damage never decreases when the fight gets longer, and the breakdown has
   a source for every damaging ability you cast. The checks run fights at
   several lengths, levels and item builds and compare.

## The kit

```json
{
  "champion": "<name given to you>",
  "name": "Display Name",
  "patch": "16.18",
  "generated": true,
  "reviewed": false,
  "maxOrder": ["Q", "E", "W"],
  "notes": ["one plain sentence per rotation rule or assumption: a reader decides from these whether to trust the numbers"],
  "assumed": [{"path": "gen.R.assumedStacks", "why": "permanent stacks a level 16 champion plausibly has"}],
  "abilities": {
    "Q": {"name": "...", "cooldownS": [8, 7.5, 7, 6.5, 6]},
    "W": {"name": "...", "cooldownS": [..]}, "E": {...}, "R": {...}
  },
  "gen": { "P": {...}, "Q": {...}, "W": {...}, "E": {...}, "R": {...} }
}
```

- `abilities.<slot>` holds ONLY `name` and `cooldownS` (and the optional
  `shred` block under `Q`). Every other number goes under `gen.<slot>`, in
  whatever shape the driver reads.
- `maxOrder`: the order basic abilities are maxed (most played; the dossier's
  rotation and the ability that scales best with rank decide).
- Every number in the kit must be one the dossier, the numbers sheet or the
  wiki text states. A number that is an assumption of yours (an assumed stack
  count, a travel time) must be listed in `assumed` with its path and reason.
  Keep assumptions few.
- `notes` are shown to the user: the opening, what is cast when, what is
  ignored and why, and every open question you had to settle.
- `attack`: the script fills `attack.windupFraction` and `attack.ranged` from
  Riot's base stats. Write `"attack": {"never": true}` (nothing else) only for
  a pure caster: a champion whose damage is its abilities and who, played
  well, spends the fight casting rather than attacking (a mage kiting at
  range, a channel that fills the fight). The engine then makes no basic
  attacks at all, so items that key on attacks get no value; say so in
  `notes`. Anyone whose kit touches attacks (an on-hit, an empowered attack,
  attack speed, a reset) attacks.

## Your reply

Exactly two fenced blocks and nothing else: first the kit as ```json, then
the driver as ```rust. When the script reports problems, reply with both
complete files again.
