# Kit sources and dossiers (pilot, 2026-09-19)

A trial of modeling champions for the Builds tab in stages, so that a cheaper
model (Claude Sonnet here) does the per-champion reading and a script checks
everything it writes. Nothing in this directory feeds `builds.py` or the
engine; the hand-encoded kits in `data/builds/<slug>.json` are untouched.

## Stages

1. `python3 jobs/kit_sources.py fetch <slug>...` archives, per patch and
   champion: Riot's character bin as CommunityDragon publishes it (`bin.json`,
   raw), the numbers sheet built from it (`sheet.json`: data values by rank,
   spell calculations resolved to base + ratio x stat, by-level parts expanded
   to 18 levels, ddragon's cooldowns and costs), and the wiki's ability
   templates (`wiki.json`, with revision ids). `--offline` rebuilds the sheet
   from the archive after a script change. `sheet <slug>` prints it.
2. `init <slug>` writes a dossier skeleton under
   `data/builds/dossiers/<slug>.parts/`: one stub per wiki leveling entry with
   its verbatim quote, and every data value of the slot's spell pre-listed
   under `binUnused` for the model to use or explain. The model stage is
   `python3 jobs/kit_dossier.py <slug>... [--workers 4]`: one small call per
   part (P, Q, W, E, R, top) through `claude -p --model sonnet --tools ""` with
   its own system prompt, the checker's errors sent back on a retry (three
   calls a part at most), parts that already pass kept, so a run resumes.
   It uses the CLI's login (no API key), runs from an empty directory (the
   project's CLAUDE.md never reaches the model) and logs every call's tokens
   and cost-equivalent to `<slug>.parts/run.json`. The rules it sends are
   sections of `jobs/kit-dossier-prompt.md`, which also carries the protocol
   for doing the same job with a Claude Code subagent.
3. `check <slug> [--slot Q]` assembles the parts and validates them: schema;
   every quote verbatim in the archived wikitext; every number equal to a bin
   row the value names (0.8 and 80 are the same ratio) unless a `binNote`
   explains the disagreement; every named row carrying one of the value's
   numbers; every data value of the five slot spells either used or listed in
   `binUnused` with a reason. Explained disagreements stay as warnings for
   the reviewer.
4. `score <slug>` compares a dossier with the hand-encoded kit (pilot only).
5. Triage: `kit_sources.py wiki-all` archives every champion's ability fields,
   `kit_triage.py batches` deals the champions without a result into batches,
   a model classifies one champion per step (`jobs/kit-triage-prompt.md`)
   against the closed primitive list (`kit_sources.py primitives`), and
   `kit_triage.py merge` writes `_triage/triage.json` and the coverage table.

Tests: `python3 -m unittest test_kit_sources` (hermetic, reads this archive;
the model stage itself is not called).

## What the pilot showed

- Nine dossiers (Kassadin, Kayle, Vladimir, Twitch, Dr. Mundo with hand kits;
  Jax, Ashe, Cassiopeia, Aphelios without) all reached 0 checker errors in
  7-10 check runs, about 200k tokens and 17-25 minutes each. Against the hand
  kits Sonnet found every damage-relevant number; what `score` lists as
  missing is what the prompt told it to skip (slows, shields, ranges). Dr.
  Mundo's kit is a tank kit, so its misses are the defensive numbers: the
  prompt describes a damage fight and needs a role switch for tank kits.
- By script (`kit_dossier.py`, effort low for the parts and medium for
  top.json) a champion takes 6-8 calls, 1.5-4 minutes and $0.35-0.65
  cost-equivalent (Annie 6 calls / 86 s, Ezreal 7 / 128 s, Master Yi 8 / 229
  s, Vayne 8 / 121 s), against ~200k tokens and 17-25 minutes for a subagent,
  with the same checker and the same quality on inspection. Eight champions
  were done this way (Veigar, Garen, Lux, Vayne, Darius, Annie, Master Yi,
  Ezreal). The first runs needed two calls for nearly every part; reading the
  logged checker messages showed why, and fixing the harness rather than the
  model brought most parts to one call: the wiki's `cast time = none` is now
  accepted, a quote may drop wiki markup (every word still has to be in the
  source, in order), the rows are pre-listed, and the prompt says that a
  by-rank ratio is one term with a list.
- The whole roster (2026-09-19, `kit_dossier.py --all --workers 6`, on the
  Claude plan's login, no API): 173 dossiers, all passing the checker after
  one resume pass; 1,487 calls, 22.2M tokens in and 4.4M out, $116
  cost-equivalent, about two hours of wall clock, and the plan's session limit
  was not reached. They hold 3,677 values, 671 `unsettled` questions and 70
  `needsBespoke` flags. Every champion the runner marked FAILED on the way
  (13 of them) had one error left, and all but two were the checker being too
  literal, fixed without calling the model again: a full stop glued to a
  quote's last word, a lone "+" dropped from "(+ 20% AP)", a calculation
  named with a dot or with the sheet's "(percent)" tag, a `Cooldown` row the
  ability's own cooldown already states, a passive's by-level cooldown, sheet
  lines cited as evidence. The two real catches were misquotes (Mordekaiser's
  E cooldown cited as [18, 14, ...] where the sheet says [16, 14, ...]).
  What a reviewer still has to read: 2,876 warnings, of which 1,157 are
  "number not readable in the quote" (wiki arithmetic, see Limits), 509 are
  wiki-only numbers, and about 1,200 are explained mismatches between a value
  and its bin rows. Most of those are derived numbers (a doubled hit, a
  per-tick share, an interpolation) whose ingredients the bin carries, not
  conflicts: letting a value state its derivation for the checker to evaluate
  would remove most of that noise.
- The dossiers from before that run (Kassadin, Kayle, Vladimir, Twitch, Ashe,
  Jax, Cassiopeia, Aphelios, Veigar, Garen, Lux, Vayne, Darius, Annie, Master
  Yi, Ezreal) were written under the damage-only prompt; `--force` rewrites
  one with the defensive numbers included.
- The first subagent attempt asked for the whole dossier in one go: both agents spent
  the full 64,000-token output budget inside one thinking block, four times,
  and wrote nothing. Skeleton + one ability per step fixed it.
- The model's flags found two bugs in this script rather than in the game:
  the bin has two cost arrays and `mana` is the stale one (`manaValues` is
  live: Jax's Q 50 not 65, Cassiopeia's E 45 not 40), and two stat labels
  were wrong (8 is crit chance, 9 crit damage, per Ashe's Frost Shot).
- Open finding in a hand kit: `kayle.json` encodes the Aflame wave's base
  as 20 -> 41 linear over levels 1-18. Riot's bin (16.16 and 16.18) and the
  wiki both say 20 until level 11, then +3 a level (35 at 16, 41 at 18): the
  kit is 10% high on the base at level 16 and 17-62% high at the buy-order
  stage levels 11-15. Not changed here: it is a model change that
  regenerates the goldens.
- Triage of all 174 entries: 6.8 primitives a champion on average; 71
  flagged as needing champion-specific logic. The 20 primitives the four
  bespoke drivers already exercise cover 30 champions in full, 42 with a
  default "every hit lands" assumption, 83 with a generic recast/multi-part
  primitive, 95 with executes, assumed permanent stacks and attack-speed
  conversions. The bespoke flag is noisy: batches flagged between 4/17 and
  10/18, and for three of the nine pilot champions the dossier pass (full
  sources) said bespoke where the triage (descriptions only) did not, each
  time over one small wrinkle (Riftwalk's doubling cost, Crimson Pact's
  coupled conversion, Twin Fang waiting for a pending Noxious Blast). Read the
  coverage as a range, and expect nearly every kit to need a small escape
  hatch beside any generic vocabulary.

## Limits

- The stat enum of calculation parts is verified only for 0 (AP), 2 (AD),
  8, 9 and 12 (max health); the rest print with a `?`.
- The script does not expand the wiki's `{{pp}}` / `{{pplevel}}` / arithmetic
  ranges, so "number not readable in the quote" is a warning, not an error.
- Subagents run with the project's CLAUDE.md in context, which describes
  Twitch and Kassadin in detail; the prompts forbid reading the kits, but for
  those two champions "what Sonnet noticed" is not a clean measurement. Jax,
  Ashe, Cassiopeia and Aphelios are.
