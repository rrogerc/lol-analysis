"""Native trait events and split Solar damage, with hand-computed outcomes."""

from copy import deepcopy
import math
import unittest

from test_tft import ENGINE, events, spec_for
from test_tft_team_engine import ally, enemy, fight, trace
from test_tft_symmetric import match as champion_match, events as match_events
from test_tft_theory_pressure import measure


def spellweaver_member(api, *, points=1.0, tagged=True, fast=False):
    member = ally("Ahri", driver="Ahri", hp=10000, cast=True)
    spec = member["spec"]
    spec["unit"].update(api=api, name=api, objective="carry")
    spec["traits"] = [{"api": "DA_18_Spellweaver" if tagged else "generic-own-cast",
                        "name": "Spellweaver" if tagged else "Own-cast AP",
                        "stats": [], "apPerCast": points}]
    spec["dummies"] = {"slots": [{"hp": 100000, "armor": 0, "mr": 0,
                                   "nearby": True}], "critEv": 1}
    kit = spec["kits"]["base"]
    kit["rows"].update(ChannelTime=0.25, HexPercentDamageFalloffTooltip=0.0)
    kit["calcs"]["MagicDamageCalc1"] = {"dtype": "magic", "terms": [
        {"type": "scaled", "coef": 100.0, "scaling": "AbilityPower", "op": "add"}]}
    if fast:
        spec["role"]["manaRegen"] = 400
        kit["stats"].update(mana=100, initialMana=100)
    return member


class TestSharedSpellweaver(unittest.TestCase):
    def test_actual_casts_grant_self_once_and_every_other_member_once(self):
        for points in (1.0, 2.0):
            for mode in ("standalone", "team", "symmetric", "theory"):
                with self.subTest(points=points, mode=mode):
                    members = [spellweaver_member("TFT18_Ahri", points=points),
                               spellweaver_member("TFT18_Karma", points=points)]
                    if mode == "standalone":
                        spec = members[0]["spec"]
                        spec["duration"] = 0.4
                        _, result = ENGINE.simulate(spec, True)
                        self.assertEqual(result["total"], 100 + points)
                        continue
                    if mode == "team":
                        result = fight(members, [enemy(hp=100000)], duration=0.4)
                        amounts = [row["damage"] for row in result["allies"]]
                    elif mode == "symmetric":
                        result = champion_match(members, [ally(hp=100000)], duration=0.4)
                        amounts = [row["damage"] for row in result["allies"]]
                    else:
                        result = measure([m["spec"] for m in members], [True, False],
                                         pressure=1, window=0.4)
                        amounts = [row["damage"] for row in result["samples"]]
                    self.assertEqual(amounts, [100 + 2 * points] * 2)

    def test_generic_own_cast_ap_is_neither_a_spellweaver_emitter_nor_recipient(self):
        members = [spellweaver_member("TFT18_Ahri"), spellweaver_member("TFT18_Karma"),
                   spellweaver_member("TFT18_Leona", tagged=False)]
        for mode in ("team", "symmetric", "theory"):
            with self.subTest(mode=mode):
                if mode == "team":
                    result = fight(members, [enemy(hp=100000)], duration=0.4)
                    amounts = [row["damage"] for row in result["allies"]]
                elif mode == "symmetric":
                    result = champion_match(members, [ally(hp=100000)], duration=0.4)
                    amounts = [row["damage"] for row in result["allies"]]
                else:
                    result = measure([m["spec"] for m in members], [True, False, False],
                                     pressure=1, window=0.4)
                    amounts = [row["damage"] for row in result["samples"]]
                self.assertEqual(amounts, [102, 102, 101])

    def test_unequal_cast_cadences_share_actual_events_without_a_time_estimate(self):
        members = [spellweaver_member("TFT18_Ahri", fast=True),
                   spellweaver_member("TFT18_Karma")]
        for mode in ("team", "symmetric", "theory"):
            with self.subTest(mode=mode):
                if mode == "team":
                    result = fight(members, [enemy(hp=100000)], duration=1.6)
                    casts = trace(result, "cast", source=0)
                    self.assertEqual([row["time"] for row in casts], [0, 1.25])
                    amounts = [row["damage"] for row in result["allies"]]
                elif mode == "symmetric":
                    result = champion_match(members, [ally(hp=100000)], duration=1.6)
                    casts = match_events(result, "cast", source=0)
                    self.assertEqual([row["time"] for row in casts], [0, 1.25])
                    amounts = [row["damage"] for row in result["allies"]]
                else:
                    result = measure([m["spec"] for m in members], [True, False],
                                     pressure=1, window=1.6)
                    amounts = [row["damage"] for row in result["samples"]]
                    reverse = measure([m["spec"] for m in reversed(members)], [False, True],
                                      pressure=1, window=1.6)
                    self.assertEqual(result, dict(reverse, samples=list(reversed(reverse["samples"])),
                        targetOrder=[1 - index for index in reverse["targetOrder"]],
                        initialSourceTargets=[None if index is None else 1 - index
                                              for index in reverse["initialSourceTargets"]]))
                self.assertEqual(amounts, [102 + 103, 102])

    def test_dead_spellweaver_receives_no_later_cast_bonus(self):
        members = [spellweaver_member("TFT18_Ahri", fast=True),
                   spellweaver_member("TFT18_Karma")]
        recipient = members[1]
        recipient["lane"] = 0
        recipient["spec"]["kits"]["base"]["hpStar"] = 1
        result = fight(members, [enemy(ad=10, lane=0, **{"as": 1}, attackStart=0.1)], duration=1.6)
        self.assertFalse(result["allies"][1]["alive"])
        self.assertEqual([row["time"] for row in trace(result, "traitAP", source=1)], [0])
        self.assertEqual([row["time"] for row in trace(result, "cast", source=0)], [0, 1.25])


def solar_member(*, mr=100, magic=0.2, true=0.2, amp=0.0, hp=100000):
    member = ally(ad=100, attack_speed=0.01)
    spec = member["spec"]
    spec["traits"] = [{"api": "DA_18_Solar", "name": "Solar", "stats": [["amp", amp]],
                        "bonusMagicPct": magic, "bonusTruePct": true}]
    spec["dummies"] = {"slots": [{"hp": hp, "armor": 0, "mr": mr,
                                   "nearby": True}], "critEv": 1}
    spec["duration"] = 0.1
    return member


class TestSolarTrueConversion(unittest.TestCase):
    def test_magic_and_true_bonus_use_the_same_base_and_do_not_recurse(self):
        for mr in (0, 100):
            for amp in (0.0, 0.5):
                expected = 100 * (1 + amp) * (1 + 0.2 * 100 / (100 + mr) + 0.2)
                for mode in ("standalone", "team", "symmetric", "theory"):
                    with self.subTest(mr=mr, amp=amp, mode=mode):
                        member = solar_member(mr=mr, amp=amp)
                        if mode == "standalone":
                            _, result = ENGINE.simulate(member["spec"], True)
                            total = result["total"]
                            self.assertEqual(set(result["breakdown"]), {"auto", "solar", "solar true"})
                        elif mode == "team":
                            result = fight([member], [enemy(mr=mr, hp=100000)], duration=0.1)
                            total = result["damage"]
                        elif mode == "symmetric":
                            result = champion_match([member], [ally(mr=mr, hp=100000)], duration=0.1)
                            total = result["damage"]
                        else:
                            result = measure([member["spec"]], [True], pressure=1, window=0.1)
                            total = result["samples"][0]["damage"]
                        self.assertAlmostEqual(total, expected)

    def test_conversion_preserves_bonus_budget_when_resistance_is_zero(self):
        for magic, true in ((0.4, 0.0), (0.2, 0.2), (0.0, 0.4)):
            with self.subTest(magic=magic, true=true):
                _, result = ENGINE.simulate(solar_member(mr=0, magic=magic, true=true)["spec"], True)
                self.assertEqual(result["total"], 140)

    def test_lethal_bonus_keeps_original_target_and_does_not_spill_to_next_enemy(self):
        for hp, mr, expected_true in ((105, 0, 0), (120, 100, 10)):
            for mode in ("standalone", "symmetric"):
                with self.subTest(hp=hp, mr=mr, mode=mode):
                    member = solar_member(mr=mr, hp=hp)
                    if mode == "standalone":
                        member["spec"]["dummies"]["slots"].append({"hp": 100000, "armor": 0, "mr": 0})
                        _, result = ENGINE.simulate(member["spec"], True)
                        self.assertEqual(result["total"], hp)
                        self.assertFalse(events(result, "damage", target=1))
                        true_total = sum(row[2] for row in events(result, "damage", "solar true"))
                    else:
                        result = champion_match([member], [ally(hp=hp, mr=mr), ally(hp=100000)], duration=0.1)
                        self.assertEqual(result["damage"], hp)
                        self.assertFalse([row for row in match_events(result, "damage") if row["target"] == 1])
                        true_total = sum(row["amount"] for row in match_events(result, "damage", name="solar true"))
                    self.assertEqual(true_total, expected_true)

    def test_absent_and_zero_true_fraction_preserve_existing_standalone_output(self):
        spec = solar_member(true=0)["spec"]
        expected = ENGINE.simulate(spec, True)
        spec["traits"][0].pop("bonusTruePct")
        self.assertEqual(ENGINE.simulate(spec, True), expected)

    def test_invalid_true_fraction_fails_instead_of_poisoning_scores(self):
        for invalid in (-0.01, math.nan, math.inf, -math.inf):
            with self.subTest(value=invalid):
                with self.assertRaisesRegex(ValueError, "bonusTruePct"):
                    ENGINE.simulate(solar_member(true=invalid)["spec"], False)


if __name__ == "__main__":
    unittest.main()
