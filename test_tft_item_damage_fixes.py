"""Item listeners: native source tags, accepted hits and periodic events.

Synthetic zero-resist targets and fixed damage provide arithmetic oracles.
Critical hits use the engine's declared expected-value model; these tests
do not establish live projectile timing, crit correlation or missing native
first-tick defaults. Item source/tag rules are bound to the 18.3 assets in
data/tft/set18/flail-mechanics.json, with current 18.4b passive values.
"""

import unittest

from test_tft import ENGINE, events, spec_for
from test_tft_audit_carries import fixture, flat, result, rows
from test_tft_symmetric import match, events as combat_events
from test_tft_team_engine import ally, flat_calc


NASHOR = {"api": "DA_NashorsTooth", "manaPerAttack": 2.0, "manaPerCrit": 2.0}
TITAN = {"api": "DA_TitansResolve", "adapPerAttack": [0.02, 25.0, 0.1],
         "adapPerHit": True}


def basic(name="Ashe", *, hp=(1000000,), duration=2.01, fx=(), ad=100.0,
          chance=0.0, attack_speed=1.0):
    spec = fixture(name, hp=hp, duration=duration, fx=fx)
    spec["driver"] = "Driver"
    spec["unit"]["timing"] = None
    for kit in spec["kits"].values():
        kit.update(baseAd=ad)
        kit["stats"].update(ad=ad, critChance=chance, critMult=1.4,
                            mana=1000000000, initialMana=0.0,
                            **{"as": attack_speed})
    return spec


def spell(name="Lux", *, hp=(1000000,), duration=1.1, fx=(), regen=0.0,
          precision=False):
    spec = fixture(name, hp=hp, duration=duration,
                   fx=[*fx, *([{"precision": 1}] if precision else [])])
    spec["unit"]["timing"] = {"base": {
        "castAnimation": 0.25, "effectAt": 0.25, "attackDelay": 100.0}}
    spec["role"] = {"manaRegen": regen, "asPct": 0.0}
    for kit in spec["kits"].values():
        kit["stats"].update(critChance=1.0, critMult=1.4, **{"as": 1.0})
    flat(spec, "MagicDamageCalc1", 100.0)
    return spec


def retaliation(*, fx=(), damage=30.0, duration=0.51):
    spec = basic("Warwick", hp=(10000,), ad=0.0, attack_speed=0.001,
                 duration=duration, fx=[{"thorns": [damage, 2.0]}, *fx])
    spec["pressure"] = True
    spec["dummies"]["slots"][0].update(ad=10.0, attackStart=0.0, **{"as": 0.001})
    return spec


class TestAcceptedCriticalMana(unittest.TestCase):
    def test_each_critical_attack_target_grants_nashor_mana_after_damage(self):
        for copies, expected in ((1, 18.0), (2, 26.0)):
            with self.subTest(copies=copies):
                spec = basic("Elder Dragon", hp=(1000000,) * 3, duration=0.01,
                             chance=1.0, fx=[NASHOR] * copies)
                spec["driver"] = "ElderDragon"
                rows(spec, AttackAoERatio=0.5, AttackAoEHexRadius=5.0)
                res = result(spec)
                self.assertEqual(res["attacks"], 1)
                self.assertEqual(len(events(res, "damage")), 3)
                # Fighter10 + base2/copy + three eligible crits2/copy.
                self.assertAlmostEqual(res["probe"]["mana"], expected)

    def test_nashor_plain_attack_expected_mana_and_copies(self):
        for chance in (0.0, 0.5, 1.0):
            for copies in (1, 2):
                with self.subTest(chance=chance, copies=copies):
                    res = result(basic(chance=chance, fx=[NASHOR] * copies))
                    self.assertEqual(res["attacks"], 3)
                    self.assertAlmostEqual(res["probe"]["mana"],
                                           3 * (7 + copies * (2 + 2 * chance)))

    def test_positive_lethal_hit_still_grants_its_critical_mana(self):
        res = result(basic(hp=(1,), duration=0.01, chance=1.0, fx=[NASHOR]))
        self.assertEqual(res["left"], [0.0])
        self.assertEqual(len(events(res, "damage")), 1)
        self.assertAlmostEqual(res["probe"]["mana"], 7 + 2 + 2)

    def test_rejected_zero_damage_does_not_grant_critical_mana(self):
        res = result(basic(ad=0.0, duration=0.01, chance=1.0, fx=[NASHOR]))
        self.assertFalse(events(res, "damage"))
        self.assertAlmostEqual(res["probe"]["mana"], 7 + 2)

    def test_spell_crit_without_attack_applicability_does_not_grant_mana(self):
        for precision in (False, True):
            with self.subTest(precision=precision):
                res = result(spell(duration=0.6, fx=[NASHOR], precision=precision))
                self.assertEqual(res["attacks"], 0)
                self.assertEqual(len(events(res, "damage", "ability")), 1)
                self.assertAlmostEqual(res["probe"]["mana"], 0.0)

    def test_scuttle_attack_applicability_includes_each_precision_crit(self):
        spec = basic("Scuttlecrab", hp=(1000000,) * 3, duration=0.01,
                     chance=1.0, fx=[NASHOR, {"precision": 1}])
        spec["driver"] = "Scuttlecrab"
        flat(spec, "PhysicalDamageCalc1", 100.0)
        rows(spec, DanceHexRadius=5.0)
        res = result(spec)
        self.assertEqual(res["attacks"], 1)
        self.assertEqual([h[2] for h in events(res, "damage", "dance")], [140.0] * 3)
        self.assertAlmostEqual(res["probe"]["mana"], 5 + 2 + 3 * 2)
        # Native PassInCritResult copies launch tags. Its producer is still
        # unresolved, so this does not impose a new no-Precision crit rule.


class TestDamageSourceGates(unittest.TestCase):
    def test_item_spark_is_excluded_from_giant_slayer_and_gunblade(self):
        for extra in ([], [{"ampVsTank": 0.3}, {"allyHealPct": 0.2}]):
            with self.subTest(extra=extra):
                spec = basic(ad=0.0, duration=0.3, fx=[{"ionicSpark": 0.5}, *extra])
                spec["pressure"] = True
                spec["dummies"]["slots"][0].update(manaMax=100.0, manaStart=100.0)
                res = result(spec)
                self.assertEqual([h[2] for h in events(res, "damage", "ionic spark")], [50.0])
                self.assertEqual(res["allyHeal"], 0.0)

    def test_native_unit_sourced_bramble_keeps_damage_amp_and_healing(self):
        res = result(retaliation(fx=[{"ampVsTank": 0.3}, {"allyHealPct": 0.2}]))
        self.assertEqual(len(events(res, "damage", "thorns")), 1)
        self.assertAlmostEqual(events(res, "damage", "thorns")[0][2], 30 * 1.3)
        self.assertAlmostEqual(res["allyHeal"], 30 * 1.3 * 0.2)

    def test_bramble_triggers_morello_but_red_requires_attack_or_spell_tag(self):
        for requires_tag, expected in ((False, 50.0), (True, 0.0)):
            with self.subTest(requires_tag=requires_tag):
                effect = {"burnOnHit": [0.01, 1.0],
                          "burnRequiresAttackOrSpell": requires_tag}
                res = result(retaliation(fx=[effect]))
                self.assertEqual(len(events(res, "damage", "thorns")), 1)
                self.assertAlmostEqual(res["breakdown"].get("burn", 0.0), expected)

    def test_untagged_thorns_do_not_grant_titan_incoming_stacks(self):
        holder = ally("Warwick", ad=100.0, attack_speed=1.0, hp=10000.0, fx=[TITAN])
        thorns = ally(ad=0.0, attack_speed=0.01, hp=10000.0, fx=[{"thorns": [30.0, 2.0]}])
        res = match([holder], [thorns], duration=2.01)
        self.assertEqual([e["amount"] for e in combat_events(res, "damage", name="auto")],
                         [102.0, 104.0, 106.0])

    def test_tagged_unit_attack_and_spell_damage_grant_titan_stacks(self):
        for spell_hit in (False, True):
            with self.subTest(spell=spell_hit):
                holder = ally("Warwick", ad=100.0, attack_speed=1.0, hp=10000.0, fx=[TITAN])
                donor = ally("Lux", driver="Lux" if spell_hit else "Driver", ad=0.0 if spell_hit else 10.0,
                             attack_speed=0.01, hp=10000.0, cast=spell_hit)
                if spell_hit:
                    flat_calc(donor, "MagicDamageCalc1", 100.0)
                res = match([holder], [donor], duration=1.01)
                hits = combat_events(res, "damage", name="auto")
                self.assertEqual([e["amount"] for e in hits], [102.0, 106.0])


class TestPeriodicAndAreaListeners(unittest.TestCase):
    def poison(self, *, fx=(), duration=3.01, attack_delay=100.0):
        spec = spell("Cassiopeia", duration=duration, fx=fx)
        rows(spec, Duration=2.0)
        flat(spec, "MagicDamageCalc1", 200.0)
        spec["unit"]["timing"]["base"]["attackDelay"] = attack_delay
        for kit in spec["kits"].values():
            kit["stats"].update(critChance=0.0)
            kit["dotCritPeriods"] = {"poison": 0.5}
        return spec

    def test_area_damage_applies_last_whisper_to_every_accepted_target(self):
        spec = basic("Scuttlecrab", hp=(1000000,) * 3, duration=1.01,
                     fx=[{"sunderOnHit": [0.3, 2.0]}])
        spec["driver"] = "Scuttlecrab"
        flat(spec, "PhysicalDamageCalc1", 100.0)
        rows(spec, DanceHexRadius=5.0)
        for target in spec["dummies"]["slots"]:
            target["armor"] = 100.0
        hits = events(result(spec), "damage", "dance")
        self.assertEqual([hit[3] for hit in hits], [0, 1, 2, 0, 1, 2])
        for hit, expected in zip(hits, [50.0] * 3 + [100 / 1.7] * 3):
            self.assertAlmostEqual(hit[2], expected)

    def test_periodic_morello_refreshes_without_flail_or_attacks(self):
        res = result(self.poison(fx=[{"burnOnHit": [0.01, 0.6]}]))
        self.assertEqual(res["attacks"], 0)
        # Poison applies at.5; logical hits at1/1.5/2/2.5 refresh a .6s
        # burn. Eight .25s integrations at1.25..3 deal 20,000 damage to
        # this million-HP target (10,000 per second).
        self.assertAlmostEqual(res["breakdown"]["burn"], 20000.0)

    def test_periodic_void_refreshes_at_native_opportunities_only(self):
        spec = self.poison(fx=[{"shredOnHit": [0.3, 0.3]}], duration=2.6)
        for target in spec["dummies"]["slots"]:
            target["mr"] = 100.0
        hits = events(result(spec), "damage", "poison")
        self.assertEqual([h[0] for h in hits], [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5])
        # Each .5s proc expires before the next proc; the intervening
        # quarter benefits. Integration alone cannot refresh the shred.
        for hit, expected in zip(hits, [12.5, 12.5, 25 / 1.7, 12.5,
                                        25 / 1.7, 12.5, 25 / 1.7, 12.5]):
            self.assertAlmostEqual(hit[2], expected)

    def test_periodic_magic_damage_also_refreshes_last_whisper(self):
        spec = self.poison(fx=[{"sunderOnHit": [0.3, 0.4]}],
                           duration=2.8, attack_delay=2.25)
        for kit in spec["kits"].values():
            kit.update(baseAd=100.0)
            kit["stats"]["ad"] = 100.0
        for target in spec["dummies"]["slots"]:
            target["armor"] = 100.0
        hits = events(result(spec), "damage", "auto")
        self.assertEqual([h[0] for h in hits], [2.75])
        self.assertAlmostEqual(hits[0][2], 100 / 1.7)

    def test_titan_receives_one_stack_per_native_poison_hit(self):
        for known, expected in ((True, [102.0, 106.0, 112.0, 116.0]),
                                (False, [102.0, 104.0, 106.0, 108.0])):
            with self.subTest(known_period=known):
                holder = ally("Warwick", ad=100.0, attack_speed=1.0, hp=10000.0, fx=[TITAN])
                donor = ally("Cassiopeia", driver="Cassiopeia", ad=0.0,
                             attack_speed=0.01, hp=10000.0, cast=True)
                flat_calc(donor, "MagicDamageCalc1", 200.0)
                for kit in donor["spec"]["kits"].values():
                    kit["rows"]["Duration"] = 2.0
                    kit["dotCritPeriods"] = {"poison": 0.5} if known else {}
                res = match([holder], [donor], duration=3.01)
                hits = combat_events(res, "damage", name="auto")
                self.assertEqual(len(hits), len(expected))
                for hit, amount in zip(hits, expected):
                    self.assertAlmostEqual(hit["amount"], amount)

    def test_compound_trail_terms_do_not_double_titan_periodic_stacks(self):
        holder = ally("Warwick", ad=100.0, attack_speed=1.0, hp=10000.0, fx=[TITAN])
        donor = ally("Ashe", driver="Ashe", ad=0.0, attack_speed=0.01, hp=10000.0, cast=True)
        flat_calc(donor, "PhysicalDamageCalc1", 0.0)
        flat_calc(donor, "PhysicalDamageCalc2", 100.0)
        for kit in donor["spec"]["kits"].values():
            kit["rows"].update(RiftDuration=2.0, MaxHealthDamagePerSecond=0.01)
            kit["dotCritPeriods"] = {"trail": 0.5}
        res = match([holder], [donor], duration=3.01)
        hits = combat_events(res, "damage", name="auto")
        self.assertEqual(len(hits), 4)
        for hit, amount in zip(hits, [102.0, 106.0, 112.0, 116.0]):
            self.assertAlmostEqual(hit["amount"], amount)

    def test_elder_finite_ignite_has_bound_half_second_period(self):
        for kit in spec_for("Elder Dragon")["kits"].values():
            self.assertEqual(kit["dotCritPeriods"]["ignite"], 0.5)

    def test_elder_ignite_uses_spell_crit_eligibility(self):
        for precision, expected in ((False, 25.0), (True, 35.0)):
            with self.subTest(precision=precision):
                spec = spell("Elder Dragon", hp=(10000,), duration=1.26,
                             precision=precision)
                rows(spec, IgniteDuration=2.0, IgniteMaxHealthDamage=0.01,
                     StunDuration=0.0, Omnivamp=0.0)
                hits = events(result(spec), "damage", "ignite")
                self.assertEqual([h[0] for h in hits], [0.75, 1.0, 1.25])
                for hit in hits:
                    # 1% of10,000 HP/sec gives25 per integration slice;
                    # the native Unit spell damage crits with Precision.
                    self.assertAlmostEqual(hit[2], expected)


class TestSunfireSelection(unittest.TestCase):
    def test_start_and_two_second_pulses_prefer_unburned_nearby_targets(self):
        spec = basic("Warwick", hp=(1000,) * 4, ad=0.0, attack_speed=0.001,
                     duration=4.01, fx=[{"sunfireAura": [0.01, 5.0, 2.0, 2.0]}])
        spec["dummies"]["slots"][0]["nearby"] = False
        res = result(spec)
        hits = events(res, "damage", "burn")
        first = {target: min(h[0] for h in hits if h[3] == target)
                 for target in {h[3] for h in hits}}
        self.assertEqual(first, {1: 0.25, 2: 2.25})
        self.assertEqual(res["left"], [1000.0, 960.0, 980.0, 1000.0])
        # The pulse at4 applies target3, which takes no damage until4.25.

    def test_sunfire_copies_each_apply_to_an_unburned_target_at_start(self):
        effect = {"sunfireAura": [0.01, 5.0, 2.0, 2.0]}
        res = result(basic("Warwick", hp=(1000,) * 3, ad=0.0, attack_speed=0.001,
                           duration=0.51, fx=[effect, effect]))
        self.assertEqual(res["left"], [995.0, 995.0, 1000.0])


if __name__ == "__main__":
    unittest.main()
