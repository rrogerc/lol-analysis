"""Enemy population is independent of adjacency in theoretical scoring."""
import unittest

import tft
import tft_theory
from test_tft import ENGINE
from test_tft_audit_frontline import harvest_spec
from test_tft_theory_pressure import actor


class TestTheoryPopulation(unittest.TestCase):
    def test_both_layouts_declare_the_same_three_enemies_and_pressure(self):
        spread = tft_theory.scenarios("spread")
        clump = tft_theory.scenarios("clump")
        self.assertEqual(len(spread), 48)
        self.assertEqual(spread, clump)
        self.assertTrue(all(scenario["targetCount"] == 3 for scenario in spread))
        self.assertEqual({s["incomingDps"] for s in spread}, {1000, 2000})
        self.assertEqual({s["targeting"] for s in spread}, {"main-first", "secondary-first"})

    def score(self, geometry, carry):
        scenario = dict(tft_theory.scenarios(geometry)[0], controlDuration=0,
                        physicalShare=1, wound=0, armor=0, mr=0)
        scorer = ENGINE.TheoryScorer([scenario])
        front = actor("population-front", hp=3000, targets=3)
        tft.set_geometry(front, geometry)
        front["pool"] = []
        tft.set_geometry(carry, geometry)
        carry["pool"] = []
        tank_id = scorer.register(front)
        carry_id = scorer.register(carry)
        return scorer.evaluate_many([[carry_id, tank_id]], 0, 1, details=True)[0]

    def test_native_scorer_preserves_nearest_three_damage_in_spread(self):
        scores = []
        for geometry in ("spread", "clump"):
            spec = harvest_spec(geometry=geometry)
            spec["unit"]["objective"] = "carry"  # isolate outgoing spell shape
            result = self.score(geometry, spec)
            scores.append(result["metrics"]["damageDps"])
            # The complete two-second channel deals 105 to each of three
            # zero-MR immortal targets. The frontline buys exactly 3 seconds.
            self.assertAlmostEqual(scores[-1], 315 / 3)
        self.assertEqual(*scores)

    def test_extra_population_does_not_multiply_a_single_attack(self):
        scores = []
        for geometry in ("spread", "clump"):
            spec = actor("population-single", damage=100, attack_speed=0.01, targets=3)
            spec["unit"]["objective"] = "carry"
            result = self.score(geometry, spec)
            scores.append(result["metrics"]["damageDps"])
            self.assertAlmostEqual(scores[-1], 100 / 3)
        self.assertEqual(*scores)


if __name__ == "__main__":
    unittest.main()
