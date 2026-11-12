# Golden fixtures — the damage engine's output, pinned

`engine-fights.json` and `enumerate.json` pin the **exact** output of the
engine (item patch 16.18). Kayle's and Vladimir's cases are what the pre-Rust
Python engine (`builds.py` at commit **d2922e6**) computed, and they proved
the Rust port bit-identical; when the fixtures were regenerated on 2026-09-07
to add Twitch (`lol_engine 6979fdd6…`, on top of commit e291d15) every one of
those 2,565 cases and the three original enumeration runs came out byte for
byte the same, and the file's provenance now names the Rust engine.
Regenerated again later on 2026-09-07 for the Fiendhunter Bolts model
(Opening Barrage's crit-weighted empowered crit and its true-damage rider,
`lol_engine fb379dd8…`): 167 of the 3,870 fight cases changed, every one of
them a build holding the item (92 Kayle, 75 Twitch; Vladimir's kit never
auto-attacks), and every other case came out byte for byte the same. No
enumeration run's pool holds the item, so `enumerate.json` changed only in
its provenance header.
Regenerated on 2026-09-18 to add Kassadin (`lol_engine 6f54b786…`, on top of
commit ba1a3a3), which also moved the fixtures to item patch 16.18: the
16.18 snapshot (ba1a3a3) had left `TestGolden.test_fights` failing on the
patch label alone. Before regenerating, every one of the 3,870 old cases was
replayed with the new engine at 16.17 and came out bit-identical (the only
differences were the new data key `costIncreasePct` in the 239 Actualizer
cases' merged effects); regenerated at 16.18 every old case keeps its build,
sheet and result byte for byte (the pool's numbers did not move between the
two patches) and the four old enumeration runs are identical. What changed
in the old cases is shape only: sheets gained `ult_cd_mult` (ultimate
haste, read by a kit that recasts its ult) and Actualizer's `manaActive`
gained `costIncreasePct`.
`engine-fights.json` holds one case per (build, target, flag-variant): its
inputs plus the `sheet`, `fx`, `ranks` and `simulate` result (5,249 cases:
129 Kayle builds, 126 Vladimir, 129 Twitch, 131 Kassadin). `enumerate.json`
holds five `enumerate_builds` runs (a Kayle test fixture and a 12-item pool
per champion) with their ordered result rows. Floats are stored as Python
`repr`, which round-trips exactly; a non-finite value would be stored as the
string `"inf"` / `"-inf"` / `"nan"` (none occur today).

Replayed by `test_builds.TestGolden`, which re-runs every case through the
engine and diffs bit for bit (an int and a float of equal value count as the
same number: the Python engine let Vladimir's integer ult delay leak through
the clock as `4`, the Rust engine says `4.0`). Regenerate with
`python3 -m lol.codegen.golden`. **These files are not regenerated on a whim: any
deliberate model change must regenerate them in the same commit** — a silent
drift means the golden files no longer describe the engine.

Regenerated on 2026-09-22 for two deliberate changes in one commit: Actualizer
left the damage pool and mana stopped being spent by every champion, so
every bar stays at its maximum (Roger's call both times). Before regenerating, the
old fixtures were replayed against the new engine to see how far the change
reached: of `engine-fights.json`'s 5,249 cases, **449 of Kassadin's 1,379
moved and all 3,870 Kayle, Twitch and Vladimir cases came out byte for byte
the same** — he is the only one of the four whose kit ever spent mana. The new
`engine-fights.json` holds 5,220 cases rather than 5,249: four hand-picked
builds (`shred-mana` for Kayle, Twitch and Kassadin, and Kassadin's
`mana-pool`) name Actualizer, which `champion_pool` no longer offers, so the
generator skips them and the item's effect key is no longer exercised here —
`test_builds.TestKassadinEngine.test_actualizer_cost_increase_is_inert` still
pins it. `enumerate.json`'s five runs are regenerated over the 75-item pool.

`survival.json`, which pinned the Survival tier's fights (Dr. Mundo builds
against Kayle's and Kassadin's), was removed with that tier on 2026-11-10.
Neither file above was regenerated for it: both replayed bit for bit against
the engine without the defender.

Regenerated on 2026-11-10, after that removal, for two deliberate model
corrections in one commit, at the fixtures' own item patch (`--patch 16.18`,
an option added for this, so that nothing but the model moved):

- Base critical strike damage is 200%, not 175%. The game went back to 200%
  in V26.01 and Riot's character files carry `critDamageMultiplier` 2.0;
  meraki still publishes 175, which the sheet no longer reads. Fiendhunter
  Bolts' empowered crit became 80% of a whole critical strike (Riot's
  `CritModifier` 0.8) where it was 80% of the crit bonus: the old formula
  only landed on the live 160% because of the old base.
- Eclipse deals 8% (melee) or 5% (ranged) of maximum health, the V26.16
  values, on two hits within 2 s, then has a 6 s cooldown. The old model
  had 6% / 4% on every second hit with no cooldown.

Before regenerating, the old fixtures were replayed against the new engine:
**2,685 of `engine-fights.json`'s 5,220 cases moved** (Kayle 874, Vladimir
186, Twitch 782, Kassadin 843), every one of them a build with crit chance
(2,253), Eclipse (138) or both (294). All 1,686 cases with neither came out
byte for byte the same, and so did 849 cases whose fight never uses what
the build holds: Vladimir never attacks, a fight can end on one Sundered Sky
attack (its guaranteed crit is a fixed 160%), and a stop-after variant can
end before a second hit. Sheets changed in `crit_damage` and nothing else;
merged effects changed in exactly the 470 cases that hold Eclipse. Of the
five enumeration runs, Vladimir's is identical and the other four reordered.
The new `engine-fights.json` holds the same 508 builds in 5,224 cases rather
than 5,220: two Twitch builds now kill the tank dummy inside the fight and
gain their two stop-after variants.

Regenerated on 2026-11-12 for three deliberate corrections to Kayle's model
in one commit, at the fixtures' own item patch (`--patch 16.18`):

- Aflame's wave base is 20 through level 11, then +3 a level to 41 at 18
  (Riot's `PassiveWaveDamage` table; the wiki's "20 + 3 for every level
  starting from level 11"). The kit had a line from level 1, which has the
  same two ends but put 32.35 at level 11 and 38.53 at 16.
- Radiant Blast's shred lands after the hit that inflicts it ("The
  resistance reduction is applied after the damage has been dealt"): the
  Q's own hit, and the Muramana and Eclipse procs it sets off, no longer
  see their own 15% reduction.
- Starfire Spellblade's cooldown starts when the empowered attack lands
  (`cdstart = post-effect`), not at the cast a windup earlier.

Before regenerating, the old fixtures were replayed against the new engine
and kit: **1,253 of Kayle's 1,353 cases moved and all 3,871 Vladimir, Twitch
and Kassadin cases came out byte for byte the same**. Each correction alone
moved 1,119 (the wave, none below level 11), 1,252 (the shred) and 287 (the
cooldown), and together exactly their union. The 100 Kayle cases that did
not move are 99 stop-after variants cut off before the kill either way and
one fight that ends at t = 0 against magic resistance that flat penetration
already takes to zero. Sheets, merged effects and ranks did not move. The
new `engine-fights.json` holds the same 5,224 cases; 20 more Kayle cases
changed only in `stop_after`, which the generator sets from the base
fight's kill time, and still return `None`. Of the five enumeration runs,
Vladimir's, Twitch's and Kassadin's are identical and Kayle's two reordered.

