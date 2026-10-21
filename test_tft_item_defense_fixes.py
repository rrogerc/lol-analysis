"""Defensive item callbacks checked against independent native arithmetic.

Current 18.4b item values are applied to a plain 1000-HP unit. Its inert
driver and noncritical pressure isolate shield order, health floors and
application-relative effect clocks from champion abilities and build search.
"""

import unittest

import tft


class TestItemDefenseFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = tft.load_snapshot(18, "18.4b")
        cls.effects = tft.load_item_effects(18)
        cls.engine = tft.engine()

    def spec(self, *, items=(), raw=0.0, period=1.0, duration=1.1,
             fx=(), mana=0.0):
        dummy = {
            "critEv": 1.0, "enemyDebuffs": {}, "targetDebuffs": {},
            "meleeRepositionSeconds": 0.0,
            "slots": [{
                "hp": 10**9, "armor": 0.0, "mr": 0.0, "kind": "tank",
                "ad": raw, "as": 1.0 / period, "ability": 0.0,
                "physicalShare": 1.0, "manaMax": 0.0, "manaStart": 0.0,
                "manaPerAttack": 0.0, "manaFromDamage": False,
                "nearby": True, "streams": 1, "lane": 3, "row": 0,
            }],
        }
        spec = tft.cell_spec(
            self.snap, self.snap.unit("Leona"), 1, "clump", [], dummy,
            duration=duration, pressure=True, item_fx=self.effects, trait_fx={},
            items=list(items), driver="Driver", cast_timing={})
        spec["unit"].update(kind="Fighter", objective="fighter", castTime=0.0,
                            timing=None)
        spec.update(immortal=False, enemyDebuffs={}, targetDebuffs={})
        spec["role"] = {"manaRegen": 0.0, "asPct": 0.0}
        kit = spec["kits"]["base"]
        kit.update(baseAd=0.0, hpStar=1000.0)
        kit["stats"].update(hp=1000.0, ad=0.0, armor=0.0, mr=0.0,
                            mana=mana, initialMana=0.0, critChance=0.0,
                            critMult=1.0)
        kit["stats"]["as"] = 1.0
        for extra in fx:
            spec["items"].append({"api": "test", "name": "test", "stats": [],
                                  "adds": [], **extra})
        return spec

    def run_spec(self, **options):
        return self.engine.simulate(self.spec(**options), True)

    @staticmethod
    def evs(result, kind, source=None):
        return [e for e in result["trace"]
                if e[1] == kind and (source is None or e[4] == source)]

    def test_small_real_hits_keep_exact_damage_without_health_cancellation(self):
        from test_tft_symmetric import ally, match, events as combat_events
        donor = ally(ad=0.3, attack_speed=1.0)
        victim = ally(ad=0.0, hp=1000000000.0)
        result = match([donor], [victim], duration=2.01)
        # Subtracting HP near1e9 loses part of a .3 hit. Resolved damage is
        # already known precisely and must be credited without that subtraction.
        hits = combat_events(result, "damage", name="auto")
        self.assertEqual([hit["amount"] for hit in hits], [0.3] * 3)
        self.assertEqual(result["damage"], sum([0.3] * 3))

    def test_lifelines_absorb_an_otherwise_lethal_first_hit(self):
        # Item armor applies first. Native PreApply then adds the shield
        # before the same hit consumes it; all numbers use max HP.
        cases = (
            ("DA_Bloodthirster", 1200.0, 1000.0, 0.0, 300.0, 100.0),
            ("DA_ProtectorsVow", 1375.0, 1000.0, 25.0, 200.0, 100.0),
            ("DA_SteraksGage", 1500.0, 1300.0, 0.0, 520.0, 320.0),
        )
        for item, raw, max_hp, armor, shield, hp in cases:
            with self.subTest(item=item):
                sheet, result = self.run_spec(items=(item,), raw=raw)
                self.assertEqual(sheet["hp"], max_hp)
                self.assertEqual(sheet["armor"], armor)
                self.assertIsNone(result["diedAt"])
                self.assertAlmostEqual(result["hpLeft"], hp)
                self.assertAlmostEqual(result["shielded"], shield)
                self.assertEqual(result["healed"], 0.0)
                kinds = [e[1] for e in result["trace"]]
                self.assertLess(kinds.index("shield"), kinds.index("take"))

    def test_lifeline_shield_does_not_prevent_unabsorbable_damage(self):
        _, plain = self.run_spec(raw=1200.0)
        _, too_large = self.run_spec(items=("DA_Bloodthirster",), raw=2000.0)
        self.assertEqual(plain["diedAt"], 1.0)
        self.assertEqual(plain["shielded"], 0.0)
        self.assertEqual(too_large["diedAt"], 1.0)
        self.assertEqual(too_large["shielded"], 300.0)
        self.assertEqual(too_large["hpLeft"], 0.0)

    def test_existing_shield_participates_in_lethal_fallback(self):
        _, result = self.run_spec(
            items=("DA_Bloodthirster",), raw=1400.0,
            fx=({"shieldAtStart": [0.20, 10.0]},))
        # 200 opening shield plus300 lifeline: 900 damage reaches HP.
        self.assertAlmostEqual(result["shielded"], 500.0)
        self.assertAlmostEqual(result["hpLeft"], 100.0)
        self.assertIsNone(result["diedAt"])

    def test_duplicate_lifelines_recheck_damage_after_each_fallback(self):
        _, result = self.run_spec(items=("DA_Bloodthirster",) * 2, raw=1250.0)
        # The first300 shield makes this hit nonlethal. The second copy
        # fires after the hit at50 HP, so it cannot absorb this hit too.
        self.assertEqual(result["shielded"], 300.0)
        self.assertEqual(result["hpLeft"], 50.0)
        relevant = [e[1] for e in result["trace"] if e[1] in ("shield", "take")]
        self.assertEqual(relevant, ["shield", "take", "shield"])

    def test_lifeline_fallback_is_spent_once(self):
        _, result = self.run_spec(items=("DA_Bloodthirster",), raw=1200.0,
                                  duration=2.1)
        self.assertEqual(result["diedAt"], 2.0)
        self.assertEqual(result["shielded"], 300.0)
        self.assertEqual(len(self.evs(result, "shield", "low health")), 1)

    def test_vow_fallback_grants_mana_once(self):
        _, result = self.run_spec(items=("DA_ProtectorsVow",), raw=1375.0,
                                  mana=1000.0)
        # Start20, two Fighter attacks10 each, one second regeneration1,
        # and native shield/mana fallback15. No casts spend this pool.
        self.assertEqual(result["attacks"], 2)
        self.assertEqual(result["casts"], 0)
        self.assertAlmostEqual(result["probe"]["mana"], 56.0)

    def test_threshold_equality_triggers_normal_lifeline(self):
        for item, raw, hp in (("DA_Bloodthirster", 500.0, 500.0),
                              ("DA_ProtectorsVow", 750.0, 400.0),
                              ("DA_SteraksGage", 520.0, 780.0)):
            with self.subTest(item=item):
                _, result = self.run_spec(items=(item,), raw=raw)
                self.assertAlmostEqual(result["hpLeft"], hp)
                self.assertEqual(result["shielded"], 0.0)
                self.assertEqual(len(self.evs(result, "shield", "low health")), 1)

    def test_edge_prevents_its_triggering_hit_from_crossing_health_floor(self):
        for raw in (720.0, 1800.0):
            with self.subTest(raw=raw):
                sheet, result = self.run_spec(items=("DA_EdgeOfNight",), raw=raw)
                self.assertEqual(sheet["armor"], 20.0)
                # 600 or1500 postmit damage clamps HP to400. Edge then
                # heals15% of the missing600, rather than overkill HP.
                self.assertEqual(result["hpLeft"], 490.0)
                self.assertEqual(result["healed"], 90.0)
                self.assertIsNone(result["diedAt"])
                self.assertEqual(result["probe"]["untargetableUntil"], 2.0)
                self.assertEqual(self.evs(result, "take")[0][5], 400.0)

    def test_edge_health_floor_is_spent_once(self):
        _, result = self.run_spec(items=("DA_EdgeOfNight",), raw=1800.0,
                                  period=2.0, duration=4.1)
        self.assertEqual(result["diedAt"], 4.0)
        self.assertEqual(len(self.evs(result, "heal", "edge of night")), 1)
        self.assertEqual(result["healed"], 90.0)

    def test_edge_preserves_permanent_standalone_benchmark_debuffs(self):
        spec = self.spec(items=("DA_EdgeOfNight",), raw=2400.0)
        spec["enemyDebuffs"] = {"wound": 0.33, "sunder": 0.30, "shred": 0.30}
        _, result = self.engine.simulate(spec, True)
        # Benchmark reductions describe continuing enemy coverage. Unlike
        # real encounter effects, they have no temporary status to remove.
        self.assertAlmostEqual(result["healed"], 90.0 * 0.67)
        self.assertAlmostEqual(result["hpLeft"], 400.0 + 90.0 * 0.67)
        self.assertIsNone(result["diedAt"])

    def test_sterak_decay_clock_starts_at_shield_application(self):
        spec = self.spec(items=("DA_SteraksGage",), raw=600.0, period=1.1,
                         duration=1.61)
        times = [1.1, 1.25, 1.35, 1.4, 1.6]
        _, samples = self.engine.measure_response(spec, times)
        # Proc at1.1,520 shield, decay520/4*.25=32.5 at1.35 and1.60.
        self.assertEqual([s["shieldHp"] for s in samples],
                         [520.0, 520.0, 487.5, 487.5, 455.0])
        self.assertEqual([s["hp"] for s in samples], [700.0] * 5)
        _, shorter = self.engine.measure_response(spec, times[:3])
        self.assertEqual(shorter, samples[:3])

    def test_sterak_settles_due_decay_before_an_off_tick_hit(self):
        spec = self.spec(items=("DA_SteraksGage",), raw=600.0, period=1.1,
                         duration=1.41)
        extra = dict(spec["dummies"]["slots"][0], ad=500.0)
        extra["as"] = 1.0 / 1.4
        spec["dummies"]["slots"].append(extra)
        _, samples = self.engine.measure_response(spec, [1.1, 1.4])
        # First hit leaves700 HP and520 shield. At1.4, native's1.35 decay
        # has already removed32.5;500 damage consumes487.5 and12.5 HP.
        self.assertEqual(samples[1]["selfShield"], 487.5)
        self.assertEqual(samples[1]["shieldHp"], 0.0)
        self.assertEqual(samples[1]["hp"], 687.5)

    def test_spirit_heals_missing_health_on_one_second_actions(self):
        spec = self.spec(items=("DA_SpiritVisage",), raw=200.0, period=0.5,
                         duration=2.01)
        _, samples = self.engine.measure_response(spec, [0.5, 0.9, 1.0, 1.5, 2.0])
        # Durability10% makes each hit180. Native regeneration precedes
        # exact-time pressure:3.6 at1s, then(540-3.6)*.02=10.728 at2s.
        expected_heals = [0.0, 0.0, 3.6, 3.6, 14.328]
        for sample, healed in zip(samples, expected_heals):
            self.assertAlmostEqual(sample["selfHeal"], healed)
        self.assertAlmostEqual(samples[2]["hp"], 943.6)
        self.assertAlmostEqual(samples[-1]["hp"], 594.328)
        _, result = self.engine.simulate(spec, True)
        self.assertEqual([e[0] for e in self.evs(result, "heal", "regeneration")], [1.0, 2.0])

    def member(self, **options):
        return {"spec": self.spec(**options), "frontline": True, "lane": 3}

    def match(self, target, sources, duration):
        return self.engine.simulate_match({
            "allies": [target], "enemies": sources, "duration": duration,
            "geometry": "clump", "initiative": 1,
        }, True)

    def attacker(self, ad, attack_speed=0.01):
        member = self.member()
        kit = member["spec"]["kits"]["base"]
        kit["baseAd"] = ad
        kit["stats"].update(ad=ad)
        kit["stats"]["as"] = attack_speed
        return member

    def test_real_encounter_lifeline_credit_includes_newly_spent_shield(self):
        for item, raw, hp, damage in (("DA_Bloodthirster", 1200.0, 100.0, 1200.0),
                                      ("DA_ProtectorsVow", 1375.0, 100.0, 1100.0),
                                      ("DA_SteraksGage", 1500.0, 320.0, 1500.0)):
            with self.subTest(item=item):
                result = self.match(self.member(items=(item,)), [self.attacker(raw)], 0.1)
                self.assertEqual(result["allyHpLeft"], hp)
                self.assertAlmostEqual(result["enemyDamage"], damage)
                self.assertTrue(result["allies"][0]["alive"])

    def test_real_encounter_edge_does_not_credit_prevented_overkill(self):
        result = self.match(self.member(items=("DA_EdgeOfNight",)),
                            [self.attacker(1800.0)], 0.1)
        self.assertEqual(result["allyHpLeft"], 490.0)
        self.assertEqual(result["allies"][0]["healing"], 90.0)
        # 1000->400 loss then90 healing: settled damaging hit is600.
        self.assertEqual(result["enemyDamage"], 600.0)

    def test_edge_cleanses_existing_stun_in_real_encounter(self):
        leona = self.member(mana=10**6)
        spec = leona["spec"]
        spec["driver"] = "Leona"
        spec["unit"]["castTime"] = 0.25
        kit = spec["kits"]["base"]
        kit["stats"]["initialMana"] = 10**6
        for calc, amount in (("MagicDamageCalc1", 100.0), ("GenericCalc1", 0.0)):
            kit["calcs"][calc]["terms"] = [{"type": "flat", "value": amount, "op": "add"}]
        kit["rows"]["StunDuration"] = 3.0
        target = self.member(items=("DA_EdgeOfNight",))
        result = self.match(target, [self.attacker(300.0, 1.0), leona], 1.3)
        attacks = [e["time"] for e in result["trace"]
                   if e["kind"] == "attack" and e["side"] == "ally"]
        # Hits at0 and1 cause250 physical each; Leona's100 at.25
        # stuns until3.25. Edge's1s proc removes that existing stun.
        self.assertEqual(attacks, [0.0, 1.0])
        self.assertEqual(result["allies"][0]["healing"], 90.0)
        self.assertEqual(result["allyHpLeft"], 490.0)

    def test_edge_cleanses_existing_burn_and_wound_before_healing(self):
        burn_source = self.attacker(12.0)
        burn_source["spec"]["items"].append({
            "api": "test-burn", "name": "test-burn", "stats": [],
            "burnOnHit": [0.01, 4.0],
        })
        result = self.match(self.member(items=("DA_EdgeOfNight",)),
                            [burn_source, self.attacker(360.0, 1.0)], 1.6)
        target = result["allies"][0]
        # Two300 attacks, an opening10 hit and four2.5 burn ticks cross
        # 40% at1s. The triggering attacker has no burn application.
        # Edge removes the active wound before its90 heal. It also removes
        # the ongoing burn, including ticks during its untargetability.
        self.assertEqual(target["healing"], 90.0)
        self.assertEqual(result["allyHpLeft"], 490.0)
        burns = [e["time"] for e in result["trace"] if e["kind"] == "damage"
                 and e["side"] == "enemy" and e.get("sourceName") == "burn"]
        self.assertEqual(burns, [0.25, 0.5, 0.75, 1.0])


if __name__ == "__main__":
    unittest.main()
