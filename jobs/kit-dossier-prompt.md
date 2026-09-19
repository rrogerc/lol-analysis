# Writing a champion kit dossier

You are one stage of a pipeline that models League of Legends champions for a
damage simulator. Your output is a **dossier**: JSON that maps the champion's
numbers between two archived sources, states the mechanics that matter with
verbatim quotes, and lists what the sources leave open. A script checks
everything you write. A human and a stronger model read your dossier
afterwards and make the rulings; your job is to be accurate and to surface
doubt, not to resolve it by guessing.

## How to work: small steps

The dossier is written as six small part files, ONE AT A TIME:

    data/builds/dossiers/<slug>.parts/P.json  Q.json  W.json  E.json  R.json  top.json

A skeleton already exists. For each ability part, in the order P, Q, W, E, R:
read the part file, fill it in, write it back, then run

    python3 jobs/kit_sources.py check <slug> --slot Q      (the slot you just wrote)

and fix what it reports before moving on. Then fill `top.json` and run the
whole check: `python3 jobs/kit_sources.py check <slug>`.

**Keep your reasoning short.** Do not draft JSON in your head and do not plan
the whole dossier before writing: decide one ability, write its file, and let
the checker tell you what is wrong. One part file per step; never write two
parts in one step. Stop when the whole check prints OK, or after 10 check
runs in total (then report what still fails).

## The fight the dossier is for

The simulator runs two kinds of fight, and one dossier serves both. In the
**damage fight** one champion, at some level with some items, fights ONE
stationary target dummy for about 15 seconds: the dummy has health and
resists, never fights back, never moves, and counts as an enemy champion; the
simulator ranks item builds by kill time and damage. In the **survival
fight** a tanky champion is attacked by one enemy champion's full rotation
and is ranked by how long it survives. There are no allies, minions or
terrain in either. So record what changes damage dealt (damage, attack speed,
buffs, debuffs on the target, costs, cooldowns, cast times, attack resets)
AND what changes damage taken (heals, shields, damage reduction, bonus
resists and health, health regeneration). Crowd control, mobility and vision
change neither: ignore them.

## Inputs (read these, and nothing else in the repository)

The sources for champion `<slug>` are in `data/builds/sources/<patch>/<slug>/`
(the newest `<patch>` directory that has the champion):

- `python3 jobs/kit_sources.py sheet <slug>` prints the **numbers sheet**
  (`sheet.json` is the same data as JSON);
- `wiki.json`: the League wiki's template for each ability (`wikitext`, `flags`);
- `python3 jobs/kit_sources.py primitives` prints the closed list of rotation
  primitives (for `top.json`).

Do NOT read `data/builds/<slug>.json` or any other kit file, anything under
`engine/`, `builds.py`, `test_builds.py`, `README.md`, `CLAUDE.md`, or other
champions' dossiers, and do not use the web. This run measures what can be
derived from the archived sources alone; if your context already contains
notes about this champion, do not rely on them. Do not edit any file except
the six part files of your champion.

## Reading the sources

- The **numbers sheet** lists every data value and spell calculation in
  Riot's own game file (the "bin"), by ability slot. `Spell.Name = ...` lines
  are data values (by rank when in brackets; `L[...]` is by champion level
  1-18). `Spell#Name = ...` lines are calculations, written as base + ratio x
  stat. `cost` is the live cost; a `costLegacy` line is a stale array.
- The **wiki template** of an ability carries its leveling, cooldown, cost,
  cast time, description, notes and flags (damage type, spell effects, on-hit
  effects). In wikitext, `{{ap|65 to 185}}` grows linearly over the ability's
  ranks (5 ranks for Q/W/E, 3 for R unless the sheet shows otherwise;
  `{{ap|4*5 to 6*5}}` is arithmetic: 20 to 30), `{{pp|...}}` and
  `{{pplevel|...}}` are values by champion level, `{{fd|1.5}}` is the number
  1.5, `{{as|(+ 80% AP)}}` is a ratio, `{{#var:x}}` reads a `{{#vardefine:x|...}}`
  at the top of the template (a quote may use either form).

## An ability part

```json
{
  "name": "Arc Bolt",
  "cooldown": {"values": [9, 8.5, 8, 7.5, 7], "quote": "{{ap|9 to 7}}"},
  "cost": {"values": [60, 65, 70, 75, 80], "resource": "mana", "quote": "{{ap|60 to 80}}"},
  "castTime": {"value": 0.25, "quote": "{{fd|0.25}}"},
  "values": [
    {
      "id": "q_1",
      "meaning": "magic damage to the target",
      "role": "damage",
      "damageType": "magic",
      "by": "rank",
      "base": [65, 95, 125, 155, 185],
      "terms": [{"stat": "ap", "coef": 0.8}],
      "quote": "{{ap|65 to 185}} {{as|(+ 80% AP)}}",
      "bin": ["ArcBolt#TotalDamage"]
    }
  ],
  "mechanics": [
    {"claim": "The damage lands when the missile arrives, not on cast.",
     "quote": "fires an orb of energy at the target enemy", "affectsDamageFight": true}
  ],
  "binUnused": [
    {"ref": "ArcBolt.CastRange", "reason": "range-or-geometry"}
  ]
}
```

The skeleton gives you the name, the wiki's cooldown / cost / cast time
fields with their quotes (fill in the numbers; set the whole field to `null`
if the ability has none), and one stub value per entry of the wiki's leveling
table with its verbatim quote. For each stub: fill it in; or DELETE it when it
only restates other values (a "maximum" or "total" row), or when it is pure
utility (a slow's strength, a range, movement speed). Then ADD values for the
numbers the leveling table lacks but a fight needs - durations, stack caps,
tick intervals, flat on-hit numbers - which the wiki states in the
description text.

Rules, all enforced by the checker:

- `cooldown`, `cost`, `castTime`: the numbers by rank (or one number). They
  are compared with the bin's; a difference needs a `binNote` in that object.
  When the wiki says there is none (`cast time = none`), leave the number null.
- A **value** is any number that can change either fight: damage, ratios,
  on-hit damage, damage over time, buff and debuff sizes, durations, stack
  caps, tick intervals, resource refunds, cooldown refunds; and heals,
  shields, damage reduction, bonus resists, bonus health and health
  regeneration (role `heal-shield`, or `self-buff` for a stat gain).
  - `role`: damage, dot, onhit, self-buff, target-debuff, resource,
    heal-shield, duration, count, interval, utility, other.
  - `damageType`: physical, magic, true, adaptive, or null when not damage.
  - `by`: `rank` (one number per ability rank), `level` (exactly 18 numbers,
    champion levels 1-18), or `const`.
  - `base`: a number or a list, the flat part (null if there is none).
    `terms`: the scaling parts. `stat` is one of: ap, ad, bonusAd, baseAd,
    maxHp, bonusHp, missingHp, armor, bonusArmor, mr, bonusMr, maxMana,
    bonusMana, missingMana, attackSpeed, bonusAttackSpeed, moveSpeed,
    critChance, critDamage, lethality, level, targetMaxHp, targetCurrentHp,
    targetMissingHp, stacks, other. Write ratios and percentages as
    fractions: 80% AP is `0.8`, 8% of the target's missing health is
    `{"stat": "targetMissingHp", "coef": 0.08}`. A ratio that grows with the
    ability's rank is ONE term whose `coef` is the list
    (`{"stat": "targetMaxHp", "coef": [0.04, 0.055, 0.07, 0.085, 0.1]}`), never
    one term per rank. A ratio that itself scales
    ("+1.5% per 100 AP") goes in
    `"coefScaling": [{"stat": "ap", "per": 100, "coef": 0.015}]` on that term.
  - `quote`: a VERBATIM substring of that ability's `wikitext` that carries the
    numbers. Copy it exactly (whitespace may differ, nothing else may). Keep
    it short. The skeleton's quotes are already verbatim.
  - `bin`: the bin rows that carry the same numbers: `"Spell.DataValue"` or
    `"Spell#Calculation"`, spelled as the sheet prints them. Every number you
    state must equal one of the rows you name (the checker knows 0.8 and 80
    are the same ratio). Name a calculation OR the data values it reads, and
    only rows whose numbers this value states. If the bin has no such row, use
    `"bin": []` and ALWAYS say why in `binNote`. **Never make an error go away by changing a number to
    agree with the other source.** When the wiki and the bin disagree, state
    the wiki's number, name the bin row, and explain the disagreement in
    `binNote`: a disagreement is a finding, the most valuable thing you can
    report.
- `mechanics`: short factual claims a simulator author must know (what
  triggers the effect, whether it applies on-hit effects, whether it resets
  the attack timer, when its cooldown starts, how stacks are gained and lost,
  what is a separate damage instance, when a heal or shield applies). Each
  needs a verbatim `quote` and `affectsDamageFight` (true when it changes the
  damage DEALT; a defensive mechanic is recorded with false). Use the wiki's
  description, notes and flags; do not state anything the wikitext does not
  say.
- `binUnused`: EVERY data value of the slot's first spell in the sheet must
  either be named by a value's `bin` (directly, or through a calculation that
  reads it) or be listed here with a reason. The skeleton lists them all with
  `"reason": null`: delete the entries whose rows your values use, and give
  every other entry its reason: stale-duplicate (a row no
  calculation reads and the wiki contradicts), range-or-geometry,
  tooltip-only, defensive, utility, not-in-a-dummy-fight, unknown. Use
  `unknown` honestly.

## top.json

```json
{
  "champion": "<slug>",
  "primitives": [ {"id": "<primitive id>", "abilities": ["W"], "note": "what it is here"} ],
  "needsBespoke": {"value": false, "why": "..."},
  "unsettled": [ {"question": "...", "options": ["...", "..."], "impact": "high|medium|low",
                  "quotes": ["optional verbatim wikitext"]} ],
  "rotation": {"proposal": ["...ordered plain statements..."], "questions": ["..."]}
}
```

- `primitives`: which entries of the closed list this champion's damage fight
  needs, and for which abilities. `needsBespoke.value` is true when the kit
  needs logic the list cannot express; say what in `why`.
- `unsettled`: questions that would change the simulated damage and that the
  two sources do not settle (or settle differently). At least two concrete
  options each. The kind of thing: do two parts of an ability hit as separate
  damage instances; does a recast re-trigger item effects; which of two bin
  rows is live; does a buff's timer start on cast or on the first hit. Do not
  pad this list; do not leave out a real doubt.
- `rotation.proposal`: how the champion should play the 15 s fight for
  maximum damage, as ordered plain statements (the opening, then what is cast
  when). `rotation.questions`: what you would ask a human expert before
  trusting that rotation.

## Final reply

A short plain-text report, nothing else:

1. the last line the whole check printed, and how many check runs you made;
2. every wiki/bin disagreement you found (one line each);
3. the two or three `unsettled` questions you consider most important;
4. every file you read and every command you ran.
