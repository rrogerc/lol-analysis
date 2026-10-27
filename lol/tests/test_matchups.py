"""Matchups & draft identity tests: archive invariants, synthetic model + parquet
pipeline, DB snapshot/fallback, and CLI output."""

import argparse
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from lol import matchups
from web import db


def make_synthetic_quant(root, champions):
    """Build a minimal self-contained lol-quant directory with meta.json,
    role_priors.npy, lane-2026-10-01.npz, and participants parquet telemetry."""
    nc = len(champions)
    nr = 5
    npairs = nc * (nc - 1) // 2
    n_main = nc * nr

    models_dir = os.path.join(root, "data", "models")
    os.makedirs(models_dir, exist_ok=True)

    meta = {
        "vocab": champions,
        "cutoffs": ["2026-10-01"],
        "trained_at": "2026-10-15T00:00:00Z",
        "halflife_days": 60.0,
        "report": [
            {"cutoff": "2026-10-01", "model": "lane", "n_train": 123456},
        ],
    }
    with open(os.path.join(models_dir, "meta.json"), "w") as f:
        json.dump(meta, f)

    priors = np.full((nc, nr), 20, dtype=np.int64)
    for i in range(nc):
        priors[i, i % nr] = 1500
    np.save(os.path.join(models_dir, "role_priors.npy"), priors)

    rng = np.random.default_rng(42)
    # Assign alternating AD (i even) and AP (i odd) identities so SV1 / damage
    # tilt has clean structure.
    is_ap = np.array([1.0 if i % 2 == 1 else -1.0 for i in range(nc)])

    w_main = rng.normal(0.0, 0.04, size=n_main)
    v_syn = np.zeros(npairs, dtype=np.float64)
    v_ctr = np.zeros(npairs, dtype=np.float64)
    v_lane = np.zeros(npairs, dtype=np.float64)
    v_duo = np.zeros(npairs, dtype=np.float64)
    v_jgl = np.zeros(npairs, dtype=np.float64)

    k = 0
    for i in range(nc):
        for j in range(i + 1, nc):
            # Mixed AD+AP pairs get positive synergy; stacked AD+AD or AP+AP get negative.
            v_syn[k] = -0.05 * is_ap[i] * is_ap[j] + rng.normal(0.0, 0.01)
            v_ctr[k] = 0.03 * ((i % 4) - (j % 4)) + rng.normal(0.0, 0.01)
            v_lane[k] = rng.normal(0.0, 0.015)
            v_duo[k] = rng.normal(0.0, 0.01)
            v_jgl[k] = rng.normal(0.0, 0.01)
            k += 1

    coef = np.concatenate([w_main, v_syn, v_ctr, v_lane, v_duo, v_jgl, np.zeros(24)])
    np.savez(os.path.join(models_dir, "lane-2026-10-01.npz"), coef=coef, intercept=np.array(0.0))

    # Write participants telemetry shard matching lol-quant schema.
    rows = {
        "champion": [], "role": [], "game_duration": [],
        "damage_champs": [], "physical_to_champs": [], "magic_to_champs": [], "true_to_champs": [],
        "damage_taken": [], "self_mitigated": [], "total_heal": [],
        "heals_on_teammates": [], "shielded_on_teammates": [],
        "cc_time": [], "damage_to_turrets": [], "solo_kills": [], "early_lane_goldxp_adv": [],
    }
    for i, c_name in enumerate(champions):
        role = matchups.ROLES[i % nr]
        ap = i % 2 == 1
        for _ in range(12):
            rows["champion"].append(c_name)
            rows["role"].append(role)
            rows["game_duration"].append(1800)
            rows["damage_champs"].append(26500)
            rows["physical_to_champs"].append(3000 if ap else 22000)
            rows["magic_to_champs"].append(22000 if ap else 3000)
            rows["true_to_champs"].append(1500)
            rows["damage_taken"].append(24000 + (i % 3) * 6000)
            rows["self_mitigated"].append(18000 + (i % 3) * 8000)
            rows["total_heal"].append(5000)
            rows["heals_on_teammates"].append(4000 if role == "UTILITY" else 200)
            rows["shielded_on_teammates"].append(5000 if role == "UTILITY" else 0)
            rows["cc_time"].append(25 + (i % 4) * 8)
            rows["damage_to_turrets"].append(4500 if role == "TOP" else 1800)
            rows["solo_kills"].append(1.4 if role in ("TOP", "MIDDLE") else 0.3)
            rows["early_lane_goldxp_adv"].append(0.05 * ((i % 3) - 1))

    part_dir = os.path.join(root, "data", "parquet", "participants", "kr")
    os.makedirs(part_dir, exist_ok=True)
    pq.write_table(pa.table(rows), os.path.join(part_dir, "participants-0001.parquet"))


class TestMatchupsArchive(unittest.TestCase):
    def test_committed_archive_invariants(self):
        self.assertTrue(os.path.exists(matchups.MATCHUPS_ARCHIVE))
        with open(matchups.MATCHUPS_ARCHIVE) as f:
            d = json.load(f)

        nc = len(d["champions"])
        self.assertGreaterEqual(nc, 160)
        self.assertEqual(len(d["archetypes"]), 14)
        self.assertEqual(d["lanes"], list(matchups.LANES))
        self.assertEqual(len(d["traits"]), len(matchups.TRAITS))
        self.assertGreater(d["games"], 1_000_000)

        # Archetypes form an exact partition of [0, nc) with no empty cluster.
        seen = []
        for a in d["archetypes"]:
            self.assertGreater(len(a["members"]), 0, f"Empty archetype {a['name']}")
            for m in a["members"]:
                self.assertEqual(d["champions"][m]["arch"], a["id"])
            seen.extend(a["members"])
            self.assertEqual(len(a["ctr"]), 14)
            self.assertEqual(len(a["syn"]), 14)
            self.assertEqual(len(a["kitSyn"]), 14)
        self.assertEqual(sorted(seen), list(range(nc)))

        # Counter rollup is antisymmetric; synergy rollups are symmetric.
        for i in range(14):
            self.assertEqual(d["archetypes"][i]["ctr"][i], 0.0)
            for j in range(14):
                self.assertAlmostEqual(
                    d["archetypes"][i]["ctr"][j],
                    -d["archetypes"][j]["ctr"][i],
                    places=2,
                )
                self.assertAlmostEqual(
                    d["archetypes"][i]["syn"][j],
                    d["archetypes"][j]["syn"][i],
                    places=2,
                )
                self.assertAlmostEqual(
                    d["archetypes"][i]["kitSyn"][j],
                    d["archetypes"][j]["kitSyn"][i],
                    places=2,
                )

        # Pair vectors match upper-triangle count and unpack cleanly.
        expected_pairs = nc * (nc - 1) // 2
        for key in ("ctr", "lane", "syn", "duo", "jgl"):
            self.assertEqual(len(d["pairs"][key]), expected_pairs, key)

        # Wukong alias and finite numeric fields on every champion.
        slugs = {c["slug"]: c for c in d["champions"]}
        self.assertIn("wukong", slugs)
        self.assertNotIn("monkeyking", slugs)
        self.assertEqual(slugs["wukong"]["name"], "Wukong")
        for c in d["champions"]:
            self.assertEqual(len(c["roles"]), 5)
            self.assertEqual(len(c["main"]), 5)
            self.assertEqual(len(c["traits"]), len(matchups.TRAITS))
            self.assertTrue(all(np.isfinite(x) for x in c["traits"]))


class TestSyntheticPipelineAndDb(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        # 20 real champion names (including MonkeyKing -> wukong) so 14 clusters fit.
        self.champs = [
            "Aatrox", "Ahri", "Darius", "Lux", "Yasuo", "Syndra", "MonkeyKing",
            "Evelynn", "Caitlyn", "Lulu", "Malphite", "Orianna", "Zed", "Viktor",
            "Jinx", "Nami", "Renekton", "Taliyah", "Lucian", "Braum",
        ]
        make_synthetic_quant(self.root, self.champs)
        self.con = sqlite3.connect(":memory:")
        self.con.executescript(db.SCHEMA)

    def test_build_payload_and_damage_balance_slopes(self):
        payload = matchups.build_matchups_payload(
            self.root, self.con, shards_per_region=10, platforms=["kr"],
        )
        self.assertEqual(payload["cutoff"], "2026-10-01")
        self.assertEqual(payload["games"], 123456)
        self.assertEqual(len(payload["champions"]), 20)
        self.assertEqual(len(payload["archetypes"]), 14)

        by_slug = {c["slug"]: c for c in payload["champions"]}
        self.assertIn("wukong", by_slug)
        # Even-indexed champions were physical (dmgTilt < 0) and gained positive
        # synergy with AP teammates (apSlope > 0); odd-indexed were AP (dmgTilt > 0)
        # and gained positive synergy with AD teammates (apSlope < 0).
        self.assertLess(by_slug["aatrox"]["dmgTilt"], 0.0)
        self.assertGreater(by_slug["aatrox"]["apSlope"], 0.2)
        self.assertGreater(by_slug["ahri"]["dmgTilt"], 0.0)
        self.assertLess(by_slug["ahri"]["apSlope"], -0.2)

        # Antisymmetric vs symmetric matrix unpacking.
        nc = len(payload["champions"])
        ctr = matchups._unpack_pair(payload["pairs"]["ctr"], nc, antisym=True)
        syn = matchups._unpack_pair(payload["pairs"]["syn"], nc, antisym=False)
        self.assertAlmostEqual(ctr[0][1], -ctr[1][0], places=5)
        self.assertAlmostEqual(syn[0][1], syn[1][0], places=5)

    def test_db_snapshot_fallback_and_cli(self):
        # Empty DB with missing archive returns empty structure.
        missing = os.path.join(self.root, "does-not-exist.json")
        with patch.object(matchups, "MATCHUPS_ARCHIVE", missing):
            empty = matchups.api_matchups(self.con)
            self.assertIsNone(empty["synced"])
            self.assertEqual(empty["champions"], [])

        payload = matchups.build_matchups_payload(
            self.root, self.con, shards_per_region=10, platforms=["kr"],
        )
        matchups.store_snapshot(self.con, payload)
        loaded = matchups.api_matchups(self.con)
        self.assertEqual(loaded["games"], 123456)
        self.assertEqual(len(loaded["champions"]), 20)

        # Archive import into fresh DB.
        archive_dir = os.path.join(self.root, "export_data")
        os.makedirs(os.path.join(archive_dir, "matchups"), exist_ok=True)
        with open(os.path.join(archive_dir, "matchups", "matchups.json"), "w") as f:
            json.dump(payload, f)
        con2 = sqlite3.connect(":memory:")
        con2.executescript(db.SCHEMA)
        self.assertTrue(matchups.import_archive(con2, archive_dir))
        self.assertEqual(matchups.api_matchups(con2)["cutoff"], "2026-10-01")

        # CLI commands render cleanly against DB snapshot.
        with patch.object(matchups, "db_connect", return_value=self.con):
            with redirect_stdout(io.StringIO()) as out_arch:
                matchups.cmd_archetypes(argparse.Namespace())
            self.assertIn("[ 0]", out_arch.getvalue())

            with redirect_stdout(io.StringIO()) as out_champ:
                matchups.cmd_champion(argparse.Namespace(name="MonkeyKing", lane=None))
            self.assertIn("Wukong", out_champ.getvalue())
            self.assertIn("Top kit synergies", out_champ.getvalue())


if __name__ == "__main__":
    unittest.main()
