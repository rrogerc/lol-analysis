"""Equipped-form aura reach must agree in provider credit and actual output."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import tft
import tft_theory as theory
from jobs.tft_theory_verify import assert_close
from tft_unit_profiles import UnitProfiles


class TestFormAuraProviders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.1d")
        cls.nidalee = cls.snap.unit("Nidalee")["api"]
        cls.ashe = cls.snap.unit("Ashe")["api"]
        cls.leona = cls.snap.unit("Leona")["api"]

    def board(self, form):
        members = [{"api": api, "star": 2} for api in (self.nidalee, self.ashe, self.leona)]
        effects = {member["api"]: [] for member in members}
        selected = {member["api"]: {"items": [], "alpha": False} for member in members}
        selected[self.nidalee]["items"] = ["DA_SunfireCape", "DA_Evenshroud",
            "DA_Deathblade" if form == "AD" else "DA_RabadonsDeathcap"]
        return members, effects, selected

    def test_reachable_auras_supply_team_utility_and_out_of_range_auras_do_not(self):
        for form in ("AD", "AP"):
            with self.subTest(form=form):
                members, effects, selected = self.board(form)
                before = deepcopy(selected)
                reference = theory.ReferenceEvaluator(self.snap, "clump")
                expected = reference.evaluate(members, effects, selected, self.ashe, self.leona)
                actual = theory.Evaluator(self.snap, "clump").evaluate(
                    members, effects, selected, self.ashe, self.leona)
                assert_close(expected, actual)
                shared = actual["sharedUtility"]
                if form == "AD":
                    self.assertEqual(shared["itemBurnHolder"], self.nidalee)
                    self.assertAlmostEqual(shared["sunder"], 0.3)
                else:
                    self.assertIsNone(shared["itemBurnHolder"])
                    self.assertEqual(shared["sunder"], 0)
                self.assertEqual(selected, before)

    def test_suppressing_a_losing_burn_provider_removes_the_conditional_aura(self):
        profiles = UnitProfiles(self.snap, "clump")
        items = ["DA_SunfireCape", "DA_Deathblade"]
        enabled = profiles.spec(self.nidalee, 2, [], items)
        disabled = profiles.spec(self.nidalee, 2, [], items, item_burn=False)
        before = deepcopy(enabled)
        self.assertTrue(any(item.get("burnAuraByRange") for item in enabled["items"]))
        self.assertFalse(any(item.get("burnAuraByRange") for item in disabled["items"]))
        self.assertIsNotNone(tft.engine().compose_fx(enabled)["burnAura"])
        self.assertIsNone(tft.engine().compose_fx(disabled)["burnAura"])
        self.assertEqual(enabled, before)

    def test_gnar_removal_cannot_inflate_board_capacity_by_changing_probe_health(self):
        gnar = self.snap.unit("Gnar")["api"]
        members = [{"api": api, "star": 2} for api in (gnar, self.leona)]
        effects = {member["api"]: [] for member in members}
        selected = {member["api"]: {"items": [], "alpha": False} for member in members}
        profile = deepcopy(theory.scenarios("spread")[0])
        # A long protected window exercises repeated Gnar casts. These kits
        # have no target-max-health damage, so a larger immortal probe must
        # not make this otherwise identical board stronger.
        profile["incomingDps"] = 100
        outputs = []
        for target_hp in (3000, 30000):
            with patch.object(theory, "scenarios", return_value=[dict(profile, targetHp=target_hp)]):
                reference = theory.ReferenceEvaluator(self.snap, "spread")
                native = theory.Evaluator(self.snap, "spread")
                expected = reference.evaluate(members, effects, selected, gnar, self.leona)
                actual = native.evaluate(members, effects, selected, gnar, self.leona)
                assert_close(expected, actual)
                self.assertGreater(actual["units"][gnar]["casts"], 1)
                outputs.append(actual)
        self.assertEqual(outputs[0]["metrics"], outputs[1]["metrics"])
        self.assertEqual(outputs[0]["units"][gnar]["damage"], outputs[1]["units"][gnar]["damage"])


if __name__ == "__main__":
    unittest.main()
