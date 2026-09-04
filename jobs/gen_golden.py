#!/usr/bin/env python3
"""Regenerate the engine's golden fixtures (data/builds/golden).

The fixtures pin the engine's output bit for bit — test_builds.TestGolden
replays them — so they are regenerated ONLY after a deliberate model change,
in the same commit, from the live engine:

    python3 jobs/gen_golden.py [--random N] [--only fights|enumerate]

Every build is drawn with fixed seeds, so an unchanged model reproduces the
files byte for byte. Writes engine-fights.json (a few hundred builds — every
pool item at least twice, every effect key, hand-picked interactions, five
targets, the flag variants) and enumerate.json (three enumeration passes).
"""
import argparse
import json
import math
import os
import random
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import builds as B  # noqa: E402

GOLDEN_DIR = os.path.join(B.BUILDS_DATA_DIR, "golden")
NONFINITE = {}


def enc(o):
    """An engine value made JSON-safe without losing bits: floats pass
    through (json writes repr, which round-trips) except inf/nan, which
    become strings; tuples become lists."""
    if isinstance(o, bool) or o is None or isinstance(o, (int, str)):
        return o
    if isinstance(o, float):
        if math.isnan(o):
            NONFINITE["nan"] = NONFINITE.get("nan", 0) + 1
            return "nan"
        if math.isinf(o):
            key = "inf" if o > 0 else "-inf"
            NONFINITE[key] = NONFINITE.get(key, 0) + 1
            return key
        return o
    if isinstance(o, (list, tuple)):
        return [enc(x) for x in o]
    if isinstance(o, dict):
        return {(k if isinstance(k, str) else str(k)): enc(v)
                for k, v in o.items()}
    raise TypeError("cannot encode %r (%s)" % (o, type(o).__name__))


def provenance():
    """Which code produced the fixture: the engine's source hash and the
    checkout's commit."""
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, check=True,
                                cwd=B.BASE_DIR).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
    engine = getattr(B, "lol_engine", None)
    return dict(engine=("lol_engine " + engine.SOURCE_HASH[:16]) if engine
                else "builds.py", commit=commit)


# ---------------------------------------------------------------- seeds ----
SEED_RANDOM = 20260904      # the 150 random legal builds
SEED_COVERAGE = 20260905    # >=2 builds per pool item
SEED_LOWLEVEL = 20260906    # level 9 / 11 / 13 builds
SEED_ENUM_POOL = 20260907   # the 12-item pools for enumerate runs B and C

CHAMPIONS = ("kayle", "vladimir")
LOW_LEVELS = (9, 11, 13)
LOW_PER_LEVEL = 10
VARIANT_EVERY = 5           # every 5th build also gets the flag variants

# Extra targets beyond the three SCENARIOS full-build ones.
EXTRA_TARGETS = {
    "tiny": dict(targetHp=600, armor=30, mr=30, duration=3, targetBonusHp=0),
    "wall": dict(targetHp=20000, armor=300, mr=300, duration=20,
                 targetBonusHp=3000),
}

# Hand-picked interaction builds (5 items, no boots).  Ids resolved by name
# through item_index/resolve_item at run time so a rename is caught loudly.
HANDPICKED = [
    ("collector-execute", ("kayle", "vladimir"),
     ["The Collector", "Infinity Edge", "Yun Tal Wildarrows",
      "Lord Dominik's Regards", "Navori Flickerblade"]),
    ("ap-burst", ("kayle", "vladimir"),
     ["Stormsurge", "Shadowflame", "Rabadon's Deathcap", "Luden's Echo",
      "Horizon Focus"]),
    ("on-hit", ("kayle", "vladimir"),
     ["Guinsoo's Rageblade", "Terminus", "Kraken Slayer", "Wit's End",
      "Nashor's Tooth"]),
    ("burn-amp", ("kayle", "vladimir"),
     ["Liandry's Torment", "Riftmaker", "Blackfire Torch", "Malignance",
      "Cosmic Drive"]),
    # Muramana + Essence Reaver + Trinity Force is illegal (two Spellblade):
    ("mana-crit", ("kayle",),
     ["Muramana", "Essence Reaver", "Yun Tal Wildarrows",
      "Navori Flickerblade", "Infinity Edge"]),
    ("actives", ("kayle", "vladimir"),
     ["Experimental Hexplate", "Fiendhunter Bolts", "Sundered Sky", "Eclipse",
      "Hullbreaker"]),
    ("energized", ("kayle", "vladimir"),
     ["Rapid Firecannon", "Statikk Shiv", "Stormrazor", "Voltaic Cyclosword",
      "Runaan's Hurricane"]),
    ("shred-mana", ("kayle",),
     ["Black Cleaver", "Bloodletter's Curse", "Spear of Shojin",
      "Abyssal Mask", "Actualizer"]),
    # Titanic + Rocketbelt + Gunblade + Stridebreaker is illegal (hydra group):
    ("hydra-hextech", ("kayle", "vladimir"),
     ["Titanic Hydra", "Hextech Rocketbelt", "Hextech Gunblade",
      "Umbral Glaive", "Dusk and Dawn"]),
    ("hp-ap", ("kayle",),
     ["Overlord's Bloodmail", "Endless Hunger", "Rod of Ages",
      "Seraph's Embrace", "Rabadon's Deathcap"]),
    ("hp-ap", ("vladimir",),
     ["Overlord's Bloodmail", "Endless Hunger", "Rod of Ages",
      "Zhonya's Hourglass", "Rabadon's Deathcap"]),
    # Lich Bane + Nashor's + Rabadon's + Void Staff + Cryptbloom is illegal
    # (two VoidPen), so Horizon Focus replaces Cryptbloom:
    ("lich-bane-ap", ("kayle", "vladimir"),
     ["Lich Bane", "Nashor's Tooth", "Rabadon's Deathcap", "Void Staff",
      "Horizon Focus"]),
    ("hexoptics-as", ("kayle", "vladimir"),
     ["Hexoptics C44", "Guinsoo's Rageblade", "Kraken Slayer",
      "Phantom Dancer", "Statikk Shiv"]),
]

# Pools for enumerate runs B and C: forced strong items + a seeded draw.
ENUM_FORCED = {
    "kayle": ["Infinity Edge", "Yun Tal Wildarrows", "Lord Dominik's Regards",
              "Kraken Slayer", "Blade of the Ruined King",
              "Navori Flickerblade"],
    "vladimir": ["Rabadon's Deathcap", "Void Staff", "Shadowflame",
                 "Blackfire Torch", "Horizon Focus", "Liandry's Torment"],
}
ENUM_POOL_SIZE = 12

SKIPPED = []    # (champion, name, reason) for the report


# ------------------------------------------------------------ engine ctx ---
class Ctx:
    """Everything the engine needs for one champion, loaded once."""

    def __init__(self, slug, pool, effects, groups, caps):
        self.slug = slug
        self.pool, self.effects = pool, effects
        self.groups, self.caps = groups, caps
        self.kit = B.load_kit(slug)
        self.champ = B.load_champion(slug)
        self.items = B.champion_pool(self.kit, effects)
        self.max_order = B.kit_max_order(self.kit)
        self.idx = B.item_index(pool)
        self._ranks = {}

    def ranks(self, level):
        if level not in self._ranks:
            self._ranks[level] = B.skill_ranks(level, self.max_order)
        return self._ranks[level]

    def rid(self, name):
        return B.resolve_item(self.pool, self.idx, name)

    def legal(self, ids):
        return B.build_is_legal(list(ids), self.groups, self.caps)

    def full_legal(self, boots, items):
        """The engine's own rule: the 5 items legal on their own *and* the
        finished 6-item build legal with boots."""
        return self.legal(items) and self.legal([boots] + list(items))


def contexts():
    patch, pool = B.load_items()
    effects = B.load_item_effects()
    groups, caps = B.load_exclusive_groups()
    return patch, pool, effects, {s: Ctx(s, pool, effects, groups, caps)
                                  for s in CHAMPIONS}


# --------------------------------------------------------- build pickers ---
def boots_bag(n, rng):
    """n boots ids with every one of the 7 used at least once, shuffled."""
    bag = (B.BOOTS * ((n // len(B.BOOTS)) + 1))[:max(n, len(B.BOOTS))]
    rng.shuffle(bag)
    return bag[:n]


def random_builds(ctx, n, rng):
    bag = boots_bag(n, rng)
    out = []
    while len(out) < n:
        items = rng.sample(ctx.items, 5)
        boots = bag[len(out)]
        if ctx.full_legal(boots, items):
            out.append([boots] + items)
    return out


def coverage_builds(ctx, rng, times=2):
    """Greedy: every item in champion_pool lands in at least `times` builds,
    so every effect key in item-effects.json gets exercised."""
    order = list(ctx.items)
    rank = {i: n for n, i in enumerate(order)}
    need = {i: times for i in order}
    out, guard = [], 0
    while any(v > 0 for v in need.values()) and guard < 10000:
        guard += 1
        pri = [i for i in order if need[i] > 0]
        rng.shuffle(pri)                       # vary inside a need tier
        pri.sort(key=lambda i: -need[i])       # stable: most-needed first
        picked = []
        for i in pri:
            if len(picked) == 5:
                break
            if ctx.legal(picked + [i]):
                picked.append(i)
        if len(picked) < 5:                    # top up with anything legal
            pad = [i for i in order if i not in picked]
            rng.shuffle(pad)
            for i in pad:
                if len(picked) == 5:
                    break
                if ctx.legal(picked + [i]):
                    picked.append(i)
        if len(picked) < 5:
            continue
        boots = next((b for b in B.BOOTS[len(out) % 7:] + B.BOOTS
                      if ctx.legal([b] + picked)), None)
        if boots is None:
            continue
        for i in picked:
            need[i] = max(0, need[i] - 1)
        out.append([boots] + picked)
    left = sorted(i for i, v in need.items() if v > 0)
    if left:
        raise RuntimeError("coverage failed for %r" % left)
    return out


def handpicked_builds(ctx):
    out = []
    for name, champs, names in HANDPICKED:
        if ctx.slug not in champs:
            continue
        try:
            items = [ctx.rid(n) for n in names]
        except SystemExit:
            SKIPPED.append((ctx.slug, name, "unknown item name"))
            continue
        missing = [n for n, i in zip(names, items) if i not in ctx.items]
        if missing:
            SKIPPED.append((ctx.slug, name,
                            "not in champion_pool: " + ", ".join(missing)))
            continue
        if not ctx.legal(items):
            SKIPPED.append((ctx.slug, name, "5 items are not a legal build"))
            continue
        boots = next((b for b in B.BOOTS if ctx.legal([b] + items)), None)
        if boots is None:
            SKIPPED.append((ctx.slug, name, "no boots make it legal"))
            continue
        out.append(([boots] + items, name))
    return out


def build_plan(ctx, n_random):
    """The ordered build list for one champion: (level, ids, kind)."""
    plan = []
    rng = random.Random(SEED_RANDOM + sum(map(ord, ctx.slug)))
    for ids in random_builds(ctx, n_random, rng):
        plan.append((16, ids, "random"))
    rng = random.Random(SEED_COVERAGE + sum(map(ord, ctx.slug)))
    for ids in coverage_builds(ctx, rng):
        plan.append((16, ids, "coverage"))
    for ids, name in handpicked_builds(ctx):
        plan.append((16, ids, "handpicked:" + name))
    rng = random.Random(SEED_LOWLEVEL + sum(map(ord, ctx.slug)))
    for level in LOW_LEVELS:
        for ids in random_builds(ctx, LOW_PER_LEVEL, rng):
            plan.append((level, ids, "level%d" % level))
    return plan


# ------------------------------------------------------------- fights -----
def targets():
    out = {k: B.SCENARIOS[k] for k in B.tier_targets("full")}
    tg = {k: {f: v[f] for f in ("targetHp", "armor", "mr", "duration",
                                "targetBonusHp")} for k, v in out.items()}
    tg.update(EXTRA_TARGETS)
    return tg


def run_fight(ctx, sheet, fx, level, tgt, **kw):
    return B.simulate(sheet, ctx.kit, fx, level, ctx.ranks(level),
                      tgt["targetHp"], tgt["armor"], tgt["mr"],
                      tgt["duration"], target_bonus_hp=tgt["targetBonusHp"],
                      **kw)


def gen_fights(ctxs, patch, n_random):
    tgs = targets()
    cases, nid = [], 0
    per_kind, per_champ = {}, {}
    for slug in CHAMPIONS:
        ctx = ctxs[slug]
        plan = build_plan(ctx, n_random)
        per_champ[slug] = len(plan)
        for bno, (level, ids, kind) in enumerate(plan):
            per_kind[kind.split(":")[0]] = per_kind.get(
                kind.split(":")[0], 0) + 1
            sheet = B.resolve_stats(ctx.champ, level, ids, ctx.pool,
                                    ctx.effects, kit=ctx.kit)
            names = list(sheet["items"])
            slim = {k: v for k, v in sheet.items() if k != "uncovered"}
            fx = B.merge_effects(ids, ctx.effects)
            eslim, efx, eranks = enc(slim), enc(fx), enc(ctx.ranks(level))
            variants = (bno % VARIANT_EVERY == 0)
            for tname, tgt in tgs.items():
                base = run_fight(ctx, sheet, fx, level, tgt)
                runs = [(dict(use_ult=True, prestacked=False, breakdown=True,
                              stop_after=None, blend=True), base)]
                if variants:
                    runs.append((dict(use_ult=False, prestacked=False,
                                      breakdown=True, stop_after=None,
                                      blend=True),
                                 run_fight(ctx, sheet, fx, level, tgt,
                                           use_ult=False)))
                    runs.append((dict(use_ult=True, prestacked=True,
                                      breakdown=True, stop_after=None,
                                      blend=True),
                                 run_fight(ctx, sheet, fx, level, tgt,
                                           prestacked=True)))
                    runs.append((dict(use_ult=True, prestacked=False,
                                      breakdown=False, stop_after=None,
                                      blend=True),
                                 run_fight(ctx, sheet, fx, level, tgt,
                                           breakdown=False)))
                    runs.append((dict(use_ult=True, prestacked=False,
                                      breakdown=True, stop_after=None,
                                      blend=False),
                                 run_fight(ctx, sheet, fx, level, tgt,
                                           _blend=False)))
                    ttk = base["ttk"] if base else None
                    if ttk:
                        half = 0.5 * ttk
                        runs.append((dict(use_ult=True, prestacked=False,
                                          breakdown=True, stop_after=half,
                                          blend=True),
                                     run_fight(ctx, sheet, fx, level, tgt,
                                               stop_after=half)))
                        runs.append((dict(use_ult=True, prestacked=False,
                                          breakdown=True, stop_after=ttk,
                                          blend=True),
                                     run_fight(ctx, sheet, fx, level, tgt,
                                               stop_after=ttk)))
                for flags, res in runs:
                    nid += 1
                    cases.append(dict(
                        id=nid, champion=slug, kind=kind, build=bno,
                        level=level, ids=list(ids), items=names,
                        target=tname, targetHp=tgt["targetHp"],
                        armor=tgt["armor"], mr=tgt["mr"],
                        duration=tgt["duration"],
                        targetBonusHp=tgt["targetBonusHp"],
                        use_ult=flags["use_ult"],
                        prestacked=flags["prestacked"],
                        breakdown=flags["breakdown"],
                        stop_after=flags["stop_after"], blend=flags["blend"],
                        sheet=eslim, fx=efx, ranks=eranks,
                        result=enc(res) if res is not None else None))
    return dict(kind="engine-fights", patch=patch, **provenance(),
                generated_by="jobs/gen_golden.py",
                seeds=dict(random=SEED_RANDOM, coverage=SEED_COVERAGE,
                           lowlevel=SEED_LOWLEVEL),
                n_random=n_random, targets=tgs, cases=cases), per_kind, per_champ


# ----------------------------------------------------------- enumerate ----
ENUM_SHEET_KEYS = ("ap", "ad", "attack_speed", "crit_chance")


def enum_pool(ctx, rng):
    forced = [ctx.rid(n) for n in ENUM_FORCED[ctx.slug]]
    rest = [i for i in ctx.items if i not in forced]
    return forced + rng.sample(rest, ENUM_POOL_SIZE - len(forced))


def enum_rows(lists):
    out = {}
    for key, rows in lists.items():
        out[key] = [dict(ids=list(ids),
                         sheet={k: enc(sheet[k]) for k in ENUM_SHEET_KEYS},
                         fights={t: enc(f) for t, f in fights.items()})
                    for ids, sheet, fights in rows]
    return out


def gen_enumerate(ctxs, patch):
    tg = {k: B.SCENARIOS[k] for k in B.tier_targets("full")}
    runs = []

    ctx = ctxs["kayle"]
    full = ctx.items
    charged = [i for i in full if "energized" in ctx.effects.get(i, {})]
    tiny = [i for i in full if i not in charged][:8] + [charged[0]]
    plan = [("A-test-fixture", "kayle", tiny, 500),
            ("B-kayle-12", "kayle",
             enum_pool(ctxs["kayle"], random.Random(SEED_ENUM_POOL)), 200),
            ("C-vladimir-12", "vladimir",
             enum_pool(ctxs["vladimir"], random.Random(SEED_ENUM_POOL)), 200)]

    for name, slug, cand, keep in plan:
        c = ctxs[slug]
        t0 = time.time()
        lists, count = B.enumerate_builds(
            c.champ, c.pool, c.effects, c.kit, 16, c.ranks(16), tg,
            candidates=list(cand), overall="full-overall", keep=keep,
            workers=1)
        secs = time.time() - t0
        sys.stderr.write("  %-16s %d candidates, count=%d, %.1fs\n"
                         % (name, len(cand), count, secs))
        runs.append(dict(name=name, champion=slug, level=16,
                         pool=list(cand), keep=keep, count=count,
                         overall="full-overall", workers=1,
                         sheet_keys=list(ENUM_SHEET_KEYS),
                         lists=enum_rows(lists)))
    return dict(kind="enumerate", patch=patch, **provenance(),
                generated_by="jobs/gen_golden.py",
                seed_pool=SEED_ENUM_POOL, targets=tg, runs=runs)


# ---------------------------------------------------------------- main ----
def dump(path, obj):
    with open(path, "w") as fh:
        json.dump(obj, fh, allow_nan=False, separators=(",", ":"))
        fh.write("\n")
    return os.path.getsize(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--random", type=int, default=56,
                    help="random builds per champion; 56 keeps the "
                         "fixture just under 6 MB")
    ap.add_argument("--out", default=GOLDEN_DIR)
    ap.add_argument("--only", choices=("fights", "enumerate"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    patch, pool, effects, ctxs = contexts()
    if args.only != "enumerate":
        t0 = time.time()
        obj, per_kind, per_champ = gen_fights(ctxs, patch, args.random)
        size = dump(os.path.join(args.out, "engine-fights.json"), obj)
        sys.stderr.write(
            "engine-fights.json: %d cases, %.2f MB, %.1fs\n  builds/champ=%r\n"
            "  builds by kind=%r\n"
            % (len(obj["cases"]), size / 1e6, time.time() - t0, per_champ,
               per_kind))
        covered = set()
        for c in obj["cases"]:
            covered.update(c["ids"])
        miss = sorted(set(effects) - covered)
        sys.stderr.write("  effect keys exercised: %d/%d  missing=%r\n"
                         % (len(set(effects) & covered), len(effects), miss))
        sys.stderr.write("  boots seen: %r\n"
                         % sorted(covered & set(B.BOOTS)))
    if args.only != "fights":
        t0 = time.time()
        obj = gen_enumerate(ctxs, patch)
        size = dump(os.path.join(args.out, "enumerate.json"), obj)
        sys.stderr.write("enumerate.json: %.2f MB, %.1fs total\n"
                         % (size / 1e6, time.time() - t0))
    if SKIPPED:
        sys.stderr.write("SKIPPED hand-picked builds:\n")
        for s in SKIPPED:
            sys.stderr.write("  %s / %s: %s\n" % s)
    sys.stderr.write("non-finite values encoded: %r\n" % (NONFINITE or "none"))


if __name__ == "__main__":
    main()
