# Triage: which rotation primitives does each champion need?

You are classifying League of Legends champions for a damage simulator. The
simulator has a hand-written "driver" per champion today; the goal is a
generic driver built from a small vocabulary of **rotation primitives**, so
most champions become data instead of code. Your classification decides
which primitives get built first, so be accurate rather than generous.

## How to work: one champion per step

Your batch is a list of champion slugs in
`data/builds/sources/<patch>/_triage/in/batch-N.txt`. For EACH slug, in order:

1. read `data/builds/sources/<patch>/_triage/in/<slug>.json` (small);
2. decide, and write `data/builds/sources/<patch>/_triage/out/<slug>.json`.

One champion per step. Keep your reasoning short: do not plan the whole batch
and do not deliberate at length over one champion - classify it from its
descriptions and move on. When every slug has a result, run

    python3 jobs/kit_triage.py check --batch N

and fix what it reports.

## The fight

One champion, at level 16 with a full build and full mana, fights ONE
stationary target dummy for about 15 seconds. The dummy has health and
resists, never fights back, never moves, and counts as an enemy champion. No
allies, minions or terrain. The champion plays whatever rotation deals the
most damage. Only what changes the damage dealt matters: healing, shields,
crowd control, mobility and vision are ignored.

## Input

`in/<slug>.json`: the wiki's description, leveling, cooldown, cost and flags
of every ability (`P` is the innate passive). Wikitext markup:
`{{ap|65 to 185}}` is a value by rank, `{{pp|...}}` a value by level,
`{{as|(+ 80% AP)}}` a ratio, `{{tip|...}}` and `{{sti|...}}` are tooltips.

`python3 jobs/kit_sources.py primitives` prints the closed vocabulary, one
line per primitive. Read only your batch list, the `in/<slug>.json` files of
your batch and that output: no other files in the repository, no web.
Classify from the descriptions; use your own knowledge of the game only to
understand them, never to override them.

## Output: `out/<slug>.json`

```json
{
  "<slug>": {
    "primitives": [ {"id": "<primitive id>", "abilities": ["Q", "E"]} ],
    "needsBespoke": false,
    "why": "one or two sentences: what decides needsBespoke, and the hardest part of this kit",
    "confidence": "high|medium|low",
    "damageFrom": "attacks|abilities|mixed"
  }
}
```

- `primitives`: every primitive the champion's damage rotation needs in this
  fight, each with the ability slots (P, Q, W, E, R) that need it. Judge by
  what a simulator must implement. An ability usually needs more than one
  primitive (an empowered attack that also resets the attack timer is
  `empowered-attack` and `attack-reset`). Do not list a primitive for an
  effect that cannot change the damage dealt to one stationary dummy.
  - `plain-cast`: a damaging spell cast on cooldown. Skillshots always hit the
    stationary dummy.
  - `defensive-only`: an ability (or part) that only heals, shields, moves or
    controls. It never blocks a generic driver.
  - `resource-budget`: ONLY when the costs would actually stop a full-mana
    (or full-energy) level 16 champion from casting on cooldown inside 15
    seconds - energy users, very expensive spam, health costs that matter.
    Ordinary mana costs do not count.
  - `conditional-damage`: the damage reads the TARGET's state (its missing,
    current or maximum health, or a mark, poison or control effect on it).
  - `positional-assumption`: the damage depends on geometry the simulator has
    to assume (a returning projectile hitting twice, a wall, how many of
    several projectiles hit one target, distance travelled or kept).
  - `infinite-scaling`: permanent stacks earned before the fight (from kills,
    minions, time); the simulator needs an assumed stack count.
- `needsBespoke`: true when the kit's damage cannot be expressed as a
  combination of the listed primitives, so it needs champion-specific logic:
  a weapon rotation, form swapping with separate cooldowns, a pet with its
  own attack timer, ability order that changes what the next ability does,
  and so on. Listing `form-or-stance`, `weapon-or-ammo-system` or
  `pet-or-summon` does not by itself force true: decide whether a generic
  version of that primitive would really be enough for THIS champion.
- `confidence`: how sure you are that the primitive list is complete.
- `damageFrom`: where most of the damage in this fight comes from.

Do not edit any file other than your batch's `out/<slug>.json` files.

## Final reply

A short plain-text report: the checker's last line, the champions you marked
`needsBespoke` with a few words each, and the champions where your confidence
is low.
