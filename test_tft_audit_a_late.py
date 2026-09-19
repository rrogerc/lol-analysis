"""Source-backed target selection and attack identity repairs.

These checks use flat damage and explicit target populations, so the expected
recipient counts and damage can be derived without replaying a golden trace.
Run after rebuilding the TFT engine: python3 -m unittest test_tft_audit_a_late -v
"""

from collections import Counter
import unittest

from test_tft import ENGINE, events, spec_for
from test_tft_team_engine import enemy, fight, trace


def flat(dtype, amount):
    return {"dtype": dtype,
            "terms": [{"type": "flat", "value": float(amount), "op": "add"}]}


def opening_cast(name, *, form=None, geometry="spread", targets=4,
                 duration=0.4, immortal=False):
    fx = [{"stats": [["ap", 1.0]]}] if form == "AP" else []
    spec = spec_for(name, fx=fx, geometry=geometry, duration=duration,
                    pressure=False)
    spec["role"] = {"manaRegen": 0.0, "asPct": 0.0}
    spec["unit"]["castTime"] = 0.25
    spec["immortal"] = immortal
    spec["dummies"]["slots"] = [
        dict(spec["dummies"]["slots"][0], hp=10000.0, armor=0.0, mr=0.0,
             nearby=i == 0, ad=0.0, ability=0.0)
        for i in range(targets)
    ]
    for kit in spec["kits"].values():
        kit["baseAd"] = 0.0
        kit["stats"].update(ad=0.0, initialMana=1000000.0, mana=1000000.0,
                            critChance=0.0, critMult=1.0)
        kit["stats"]["as"] = 0.01
    return spec


def independent_cast(name, **kwargs):
    spec = opening_cast(name, **kwargs)
    for kit in spec["kits"].values():
        if name == "Alune":
            kit["calcs"]["MagicDamageCalc1"] = flat("magic", 75.0)
        elif name == "Teemo":
            kit["calcs"]["MagicDamageCalc1"] = flat("magic", 90.0)
            kit["calcs"]["MagicDamageCalc2"] = flat("magic", 200.0)
        else:
            kit["calcs"]["PhysicalDamageCalc1"] = flat("physical", 250.0)
            kit["calcs"]["PhysicalDamageCalc2"] = flat("physical", 350.0)
            kit["calcs"]["MagicDamageCalc1"] = flat("magic", 240.0)
            kit["calcs"]["MagicDamageCalc2"] = flat("magic", 72.0)
    return spec


class TestIndependentTargeting(unittest.TestCase):
    def test_alune_distributes_all_nine_shards_among_three_spread_enemies(self):
        for geometry in ("spread", "clump"):
            for immortal in (False, True):
                with self.subTest(geometry=geometry, immortal=immortal):
                    _, result = ENGINE.simulate(independent_cast(
                        "Alune", geometry=geometry, immortal=immortal), True)
                    hits = events(result, "damage", "moonshards")
                    self.assertEqual([event[3] for event in hits], [0, 1, 2] * 3)
                    self.assertEqual([event[2] for event in hits], [75.0] * 9)
                    self.assertEqual(result["total"], 675.0)
                    self.assertEqual(result["casts"], 1)

    def test_teemo_clusters_reach_three_spread_enemies_and_giant_hits_primary(self):
        for geometry in ("spread", "clump"):
            with self.subTest(geometry=geometry):
                _, result = ENGINE.simulate(independent_cast("Teemo", geometry=geometry), True)
                clusters = events(result, "damage", "mushrooms")
                giant = events(result, "damage", "giant mushroom")
                self.assertEqual([event[3] for event in clusters], [0, 1, 2] * 2)
                self.assertEqual([event[2] for event in clusters], [90.0] * 6)
                self.assertEqual([(event[3], event[2]) for event in giant], [(0, 200.0)])
                self.assertEqual(result["total"], 2 * 3 * 90.0 + 200.0)

    def test_both_kogmaw_forms_reach_a_second_independent_enemy(self):
        for form, damage in (("AD", 250.0), ("AP", 240.0)):
            for geometry in ("spread", "clump"):
                with self.subTest(form=form, geometry=geometry):
                    sheet, result = ENGINE.simulate(independent_cast(
                        "Kog'Maw", form=form, geometry=geometry), True)
                    hits = events(result, "damage", "ability")
                    self.assertEqual(sheet["form"], form)
                    self.assertEqual([(event[3], event[2]) for event in hits],
                                     [(0, damage), (1, damage)])
                    self.assertEqual(result["total"], 2 * damage)

    def test_ap_kogmaw_dot_pays_its_full_total_to_both_recipients(self):
        _, result = ENGINE.simulate(independent_cast(
            "Kog'Maw", form="AP", duration=3.25), True)
        ticks = events(result, "damage", "acid")
        self.assertEqual(Counter(event[3] for event in ticks), {0: 12, 1: 12})
        self.assertTrue(all(event[2] == 6.0 for event in ticks))
        self.assertEqual(result["total"], 2 * (240.0 + 72.0))
        self.assertEqual(result["casts"], 1)

    def test_independent_counts_are_limited_by_living_population(self):
        for count in (1, 2, 5):
            for name in ("Alune", "Teemo", "Kog'Maw"):
                with self.subTest(champion=name, population=count):
                    _, result = ENGINE.simulate(independent_cast(name, targets=count), True)
                    hits = events(result, "damage")
                    if name == "Alune":
                        expected = 9 * 75.0
                        recipients = min(count, 3)
                    elif name == "Teemo":
                        recipients = min(count, 3)
                        expected = 2 * recipients * 90.0 + 200.0
                    else:
                        recipients = min(count, 2)
                        expected = recipients * 250.0
                    self.assertEqual(result["total"], expected)
                    self.assertEqual({event[3] for event in hits}, set(range(recipients)))

    def test_independent_selection_skips_a_target_killed_before_cast_lands(self):
        for name, source, recipients in (("Alune", "moonshards", [1, 2, 3] * 3),
                                         ("Teemo", "mushrooms", [1, 2, 3] * 2),
                                         ("Kog'Maw", "ability", [1, 2])):
            with self.subTest(champion=name):
                spec = independent_cast(name)
                spec["dummies"]["slots"][0]["hp"] = 50.0
                for kit in spec["kits"].values():
                    kit["baseAd"] = 100.0
                _, result = ENGINE.simulate(spec, True)
                self.assertEqual(events(result, "damage", "auto")[0][2], 50.0)
                self.assertEqual([event[3] for event in events(result, "damage", source)], recipients)

    def test_shared_kogmaw_keeps_current_target_even_when_it_has_higher_index(self):
        for form in ("AD", "AP"):
            with self.subTest(form=form):
                caster = {"spec": independent_cast("Kog'Maw", form=form),
                          "frontline": False, "lane": 6, "priority": 0}
                result = fight([caster], [enemy(lane=0), enemy(lane=2), enemy(lane=6)],
                               duration=0.4, geometry="spread")
                hits = trace(result, "damage", source=0, name="ability")
                self.assertEqual(len(hits), 2)
                self.assertEqual(hits[0]["target"], 2)
                self.assertEqual(len({hit["target"] for hit in hits}), 2)


class TestKayleAttackIdentity(unittest.TestCase):
    def kayle(self, *, auto, ascension, star=3, geometry="clump"):
        spec = opening_cast("Kayle", geometry=geometry, duration=0.1)
        spec["star"] = star
        kit = spec["kits"]["base"]
        kit["baseAd"] = auto
        kit["calcs"]["MagicDamageCalc1"] = flat("magic", ascension)
        kit["calcs"]["MagicDamageCalc2"] = flat("magic", 40.0)
        for index, target in enumerate(spec["dummies"]["slots"]):
            target.update(hp=50.0 if index == 0 else 10000.0, nearby=index < 3)
        return spec

    def test_waves_keep_both_secondaries_when_either_primary_damage_component_kills(self):
        for auto, ascension in ((100.0, 0.0), (0.0, 100.0)):
            with self.subTest(auto=auto, ascension=ascension):
                _, result = ENGINE.simulate(self.kayle(auto=auto, ascension=ascension), True)
                waves = events(result, "damage", "waves")
                self.assertEqual([(event[3], event[2]) for event in waves],
                                 [(1, 40.0), (2, 40.0)])
                self.assertEqual(result["total"], 50.0 + 2 * 40.0)
                self.assertEqual(result["attacks"], 1)

    def test_primary_death_does_not_add_waves_to_spread_or_lower_star_kayle(self):
        for star, geometry in ((3, "spread"), (2, "clump"), (1, "clump")):
            with self.subTest(star=star, geometry=geometry):
                _, result = ENGINE.simulate(self.kayle(
                    auto=100.0, ascension=100.0, star=star, geometry=geometry), True)
                self.assertFalse(events(result, "damage", "waves"))
                self.assertEqual(result["total"], 50.0)


if __name__ == "__main__":
    unittest.main()
