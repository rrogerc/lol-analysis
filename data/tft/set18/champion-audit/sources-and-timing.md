# Better behavior sources and action timing

Research and design review, September 8, 2026. No model implementation,
enumeration or publication was performed.

Follow-up: the [Android extraction pilot](android-pilot.md) subsequently
downloaded and extracted 124 real gameplay assets, resolved matching Android
global metadata and decoded standard Unreal curves. Custom gameplay property
decoding remains incomplete. The source discovery below describes the state
before that pilot; see its report for acquisition and verification results.
The pilot also successfully fetched public TFTraits HTML with an ordinary
browser User-Agent; the earlier default-client 403 responses do not establish
that its public pages are unavailable. No complete supported behavior API was
verified.

## Source findings

TFTraits was previously used for Azir's adopted six-command mana lock
(`data/tft/README.md`, Azir section; `test_tft_azir.py`). It is not currently
an ingested source of per-champion attack, cast or movement specifications.

A key distinction changes the next extraction step: Riot documents that
Set 18 moved to Unreal while the launcher temporarily remained on Hextech.
The completed Mac inspection covered legacy League WAD/BIN collections,
not a verified extraction of the active Unreal packages. Its placeholder
findings must not be generalized to all available TFT game data.
[Riot migration FAQ](https://teamfighttactics.leagueoflegends.com/en-us/news/game-updates/faq-tft-unreal-migration/).

The TFTraits creator says their scraper reads Unreal game files and
specifically recommends the Android build for casting and spell-shape
information. They provide an official Riot CDN release manifest in the
[original discussion](https://www.reddit.com/r/TeamfightTactics/comments/1vyyk3w/tft_whiteboard_your_best_friend_for_exploring_tft/).
This is a useful first-party explanation of their pipeline, not independent
verification that every extracted field is active gameplay behavior.

The linked [Android manifest](https://teamfighttactics.secure.dyn.riotcdn.net/channels/public/releases/AB4C45E0FD7F4250.manifest)
was downloaded successfully: 248,073 bytes, RMAN magic, SHA256
`7ddbc1e57761067b6388c7abdc7a8b2a28c7c2b419be44eb27b47b3f29f15046`.
HTTP Last-Modified is August 25, 2026, 07:14:55 GMT. It predates the saved
18.1d hotfix corrections, so it is a starting point for behavioral schema,
not automatic replacement balance data. Only the manifest was downloaded;
its packages have not been extracted or validated.

TFTraits publishes separate cast windows, effect offsets, attack hit/recovery
times and conditional mana locks. For example, its
[Akali page](https://tftraits.com/champions/akali/) reports a 0.80-second cast
window and a 0.27-second effect offset. Those are different events; neither
should blindly replace the single generic 0.25-second value in our engine.
These remain candidate source values pending patch and runtime checks.

The site's [timing methodology](https://tftraits.com/cast-timelines/) says its
tools combine public game data and gameplay observation. Its
[targeting methodology](https://tftraits.com/targeting/) describes hundreds
of in-game tests of competing movement into a hex and explicitly leaves
some ties unresolved. That is useful behavioral evidence. Its rankings and
generated timelines remain another model, not a correctness oracle; some
champion-page prose contradicts its own stated mana-lock endpoint.

No documented complete public behavior API was verified. Direct HTTP reads
of several TFTraits pages returned 403 here, although the browsing tool
could read their public content. Do not promise a stable automated scraper
without verifying a suitable export or access method.

## Recommended evidence policy

- Prefer active, versioned Unreal assets for configuration and formulas.
  Follow referenced ability/form assets rather than collecting unrelated
  animation names or dormant fields.
- Use TFTraits to identify useful fields, candidate timings and observed
  targeting rules. Record whether each fact came from extraction, an
  experiment or a simulator assumption.
- Apply dated Riot patch notes for changes that postdate the package.
  Preserve source conflicts rather than silently taking whichever number
  produces a preferred item ranking.
- Use controlled game observations for unresolved event ordering, action
  cancellation, proc flags, shield/buff overlap and target changes. Tests
  should compare observable timestamps and recipients, not only DPS totals.

Maintained readers worth evaluating are
[CUE4Parse](https://github.com/FabianFG/CUE4Parse), used by
[FModel](https://github.com/4sval/FModel), and
[UAssetAPI](https://github.com/atenfyr/UAssetAPI). They can inspect Unreal
asset data; TFT-specific compatibility, mappings and required package
dependencies have not been established. They are extraction candidates,
not already verified TFT solutions. Production numerical work stays in Rust.

## Modeling recommendation

The current engine already has a Rust event clock, explicit long channels
for some drivers and a separate mana-lock state. However, ordinary attacks
deal damage and grant attack mana at attack start, the first attack is due
at time zero, and actors do not walk between positions. The spec does not
carry movement speed, attack windup or projectile speed. Fixed-lane target
ordering is not a movement simulation (`fight.rs`, `symmetric.rs`, `tft.py`).

Give each actor independent movement and action state, plus pending impacts.
An attack has a start, windup, release and impact. A spell has effect offsets,
an action-unlock point, any movement it performs, and its own mana-lock rule.
Already released projectiles need a lifetime distinct from an interruptible
cast, since the existing owner-gated pending-event path is not sufficient.

Animation is not all additional downtime. Attack windup is part of the
attack period; projectile flight can overlap the next action. A jump can
overlap a cast or replace walking. Gates that overlap combine by their
latest endpoint, while genuinely sequential phases add. DoTs and released
projectiles may continue during movement when the ability permits.

Movement requires declared positions and matching distance/speed units.
Use neutral target layouts to measure approach and range without assigning
named enemy compositions or pretending those layouts are lobby frequencies.
An unobstructed approach is an initial test; realistic melee congestion also
requires occupied hexes, movement contests and waits. Do not claim a simple
distance calculation resolves body blocking.

Keep EHP and damage measurement over elapsed combat time, including time
spent approaching and recovering. Ordinary walking remains exposed to
pressure. Only a sourced invulnerable or untargetable phase changes that
exposure. Retain the existing level-eight/level-nine melee-carry constraints
until a separate change is authorized.

The finite-target damage test can model travel after kills. The composition
scorer's immortal probes cannot: they never produce those target deaths.
If compositions must receive kill/chase/reset value, define shared neutral
target lifetimes and an explicit endpoint or replacement policy. An action
scheduler alone cannot infer that missing workload. A periodic forced
retarget or a universal melee uptime multiplier would merely hide it.

## First useful implementation slice

Start with a small extraction pilot for Brambleback, Azir, Akali and both
Nidalee forms, with one ordinary ranged unit as a control. Establish the
active assets and timing meanings before applying a roster-wide rewrite.
Check cast start, effect time, action recovery, mana unlock and the next hit
after changing targets. Preserve incomplete records as explicit gaps.

Then implement shared action phases with independent checks: delayed first
impact while preserving attack cadence; overlapping versus sequential
cast/jump timing; interrupted windup versus autonomous projectile; finite
target transfer; and conserved incoming pressure. Add occupied-hex tests
before treating melee positioning as resolved. Benchmark representative
item searches before another full rebuild.
