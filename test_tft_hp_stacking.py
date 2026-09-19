"""Hand-calculated HP budgets for ordinary additive item/trait percentages.

Pinned item and trait packets exercise the production native stat paths.
Expected HP values are specified independently of the engine's aggregation.
Run after rebuilding the engine: python -m unittest test_tft_hp_stacking -v
"""

from copy import deepcopy
import unittest

import tft
from tft_comp_traits import resolve_board_traits
from tft_unit_profiles import generic_targets
from test_tft_unit_profiles import plain_spec


class TestNativeHealthStacking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")
        cls.engine = tft.engine()
        cls.item_fx = tft.load_item_effects(cls.snap.set_no)

    def spec(self, name="Sett", *, star=2, items=(), effects=()):
        spec = tft.cell_spec(
            self.snap, self.snap.unit(name), star, "spread", [], generic_targets(),
            duration=0.01, pressure=False, item_fx=self.item_fx,
            items=[self.snap.item(item)["api"] for item in items], driver="Driver")
        spec["traits"] = deepcopy(list(effects))
        return spec

    def assert_opening_hp(self, spec, expected):
        # The standalone leaderboard and composition opening calculation
        # must both expose the same HP, before any attacks or timed grants.
        sheet, _ = self.engine.simulate(spec, False)
        theory = self.engine.theory_opening(spec, False)
        self.assertAlmostEqual(sheet["hp"], expected, places=7)
        self.assertAlmostEqual(theory["hp"], expected, places=7)

    def test_zero_through_three_warmogs_add_their_percentages(self):
        # Sett: 1200 base HP × 1.8 at two stars. Each Warmog adds 500
        # flat HP and 18 percentage points to the same HP multiplier.
        for count, expected in ((0, 2160.0), (1, 3138.8),
                                (2, 4297.6), (3, 5636.4)):
            with self.subTest(warmogs=count):
                spec = self.spec(items=("Warmog's Armor",) * count)
                self.assert_opening_hp(spec, expected)

    def test_different_health_items_use_the_same_percentage_pool(self):
        cases = (
            # (2160 + 500) × (1 + .18 + .06 + .06).
            (("Warmog's Armor", "Bramble Vest", "Dragon's Claw"), 3458.0),
            # Sunfire adds 150 flat HP and 8%; Bramble adds 6%.
            # (2160 + 500 + 150) × (1 + .18 + .08 + .06).
            (("Warmog's Armor", "Sunfire Cape", "Bramble Vest"), 3709.2),
        )
        for items, expected in cases:
            for ordered in (items, tuple(reversed(items))):
                with self.subTest(items=ordered):
                    self.assert_opening_hp(self.spec(items=ordered), expected)

    def test_sett_brawler_and_blossom_share_the_item_health_pool(self):
        members = [{"api": self.snap.unit(name)["api"], "star": 2}
                   for name in ("Sett", "Kobuko", "Ahri", "Ashe")]
        resolved = resolve_board_traits(self.snap, members)
        effects = resolved["effects"][self.snap.unit("Sett")["api"]]
        self.assertEqual({trait["name"]: trait["breakpoint"]
                          for trait in resolved["traits"] if trait["active"]},
                         {"Brawler": 2, "Blossom": 3})
        # Brawler adds 120 flat HP and 25%; Blossom adds 10%.
        # With three Warmogs: (2160 + 120 + 1500) × 1.89 = 7144.2.
        for count, expected in ((0, 3078.0), (1, 4253.4), (3, 7144.2)):
            with self.subTest(warmogs=count):
                spec = self.spec(items=("Warmog's Armor",) * count, effects=effects)
                self.assert_opening_hp(spec, expected)

    def test_star_scaling_precedes_flat_item_health_and_percentage_health(self):
        # Kobuko is a supported three-star unit with 700 base HP. Only
        # champion base HP receives the 1 / 1.8 / 3.24 star multiplier;
        # Warmog's 500 flat HP is added afterwards, then amplified by 18%.
        for star, bare_hp, item_hp in ((1, 700.0, 1416.0),
                                      (2, 1260.0, 2076.8),
                                      (3, 2268.0, 3266.24)):
            with self.subTest(star=star):
                self.assert_opening_hp(self.spec("Kobuko", star=star), bare_hp)
                self.assert_opening_hp(
                    self.spec("Kobuko", star=star, items=("Warmog's Armor",)), item_hp)

    def test_runtime_flat_health_uses_combined_percentage_in_both_schedulers(self):
        # A minimal timed grant isolates the common stat-grant path from
        # champion abilities. 1000 × (1 + .2 + .3) starts at 1500 HP;
        # +100 flat HP grants 150 HP, then 100 raw damage leaves 1550.
        spec = plain_spec(incoming_dps=100, pressure_interval=0.5,
                          fx=[{"hpMult": 1.2}])
        spec["traits"] = [{
            "api": "test-flat-health", "name": "Test flat health",
            "stats": [["hpMult", 1.3]],
            "timedStats": [{"after": 1.0, "interval": 0.0,
                            "stats": [["hp", 100.0]]}],
        }]
        spec["enemyDebuffs"] = {"wound": 1.0}
        for mode in ("standalone", "shared theory"):
            with self.subTest(mode=mode):
                if mode == "standalone":
                    opening, samples = self.engine.measure_response(spec, [1.0])
                else:
                    opening = self.engine.theory_opening(spec, True)
                    result = self.engine.measure_theory_team(
                        [spec], [True], 1.0, 100.0, 1.0, 8.0, 0.0)
                    samples = result["samples"]
                self.assertAlmostEqual(opening["hp"], 1500.0)
                self.assertAlmostEqual(samples[0]["hp"], 1550.0)
                self.assertEqual(samples[0]["incomingSpent"], 100.0)
                self.assertEqual(samples[0]["selfHeal"], 0.0)


if __name__ == "__main__":
    unittest.main()
