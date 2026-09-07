"""Prepared/batched matches preserve all values and own fresh combat state."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import gc
import struct
import unittest

from test_tft import ENGINE, spec_for
from test_tft_symmetric import caster
from test_tft_team_engine import ally


def bits(value):
    """Compare floating-point bits, list order, dictionary order and types."""
    if isinstance(value, float):
        return (float, struct.pack("!d", value))
    if isinstance(value, dict):
        return (dict, [(key, bits(item)) for key, item in value.items()])
    if isinstance(value, list):
        return (list, [bits(item) for item in value])
    return (type(value), value)


def prepared(spec):
    return {**spec, **{side: [ENGINE.prepare_actor(entry) for entry in spec[side]]
                     for side in ("allies", "enemies")}}


class TestPreparedMatches(unittest.TestCase):
    def assertExact(self, expected, actual):
        self.assertEqual(bits(expected), bits(actual))

    def test_raw_prepared_and_mixed_entries_have_identical_full_traces(self):
        spec = {"allies": [caster(), ally(hp=150, ad=50, lane=5)],
                "enemies": [caster("Leona"), ally(ad=75, attack_speed=2, lane=5)],
                "duration": 5.0, "geometry": "spread", "initiative": 1}
        expected = ENGINE.simulate_match(spec, True)
        native = prepared(spec)
        self.assertExact(expected, ENGINE.simulate_match(native, True))
        native["enemies"][0] = spec["enemies"][0]
        self.assertExact(expected, ENGINE.simulate_matches([native], trace=True)[0])
        self.assertExact(expected, ENGINE.simulate_matches([spec], trace=True)[0])

    def test_preparation_is_an_immutable_owned_snapshot(self):
        member = caster(damage=91)
        member["spec"]["dummies"] = {"slots": []}
        before = deepcopy(member)
        handle = ENGINE.prepare_actor(member)
        self.assertEqual(before, member)
        fixture = {"allies": [handle], "enemies": [ally(hp=1000)], "duration": 1.0}
        expected = ENGINE.simulate_match(fixture, True)
        member["spec"]["kits"]["base"]["baseAd"] = 99999.0
        member["spec"]["kits"]["base"]["calcs"].clear()
        member["lane"] = 6
        del member
        gc.collect()
        self.assertExact(expected, ENGINE.simulate_match(fixture, True))
        with self.assertRaises(AttributeError):
            handle.lane = 0
        with self.assertRaises(ValueError):
            ENGINE.simulate(before["spec"], False)

    def test_shared_handle_gets_fresh_driver_effect_and_clock_state(self):
        scuttle = {"spec": spec_for("Scuttlecrab", items=("Protector's Vow", "Warmog's Armor")),
                   "frontline": True, "lane": 1}
        sentinel = {"spec": spec_for("Sentinel", items=("Guinsoo's Rageblade",)),
                    "frontline": False, "lane": 5}
        enemy = {"spec": spec_for("Karma", items=("Hextech Gunblade", "Red Buff")),
                 "frontline": True, "lane": 3}
        spec = {"allies": [scuttle, sentinel], "enemies": [enemy, enemy], "duration": 9.0}
        expected = ENGINE.simulate_match(spec, True)
        native = prepared(spec)
        # Deliberately share the same seed both within a world and across
        # worlds; casts mutate driver state and Sentinel mutates its Fx.
        native["enemies"][1] = native["enemies"][0]
        matches = [native] * 12
        for workers in (1, 4):
            for actual in ENGINE.simulate_matches(matches, trace=True, workers=workers):
                self.assertExact(expected, actual)

    def test_target_count_geometry_and_duration_belong_to_each_match(self):
        actor = caster(damage=73, attack_speed=1.3)
        actor["spec"]["dummies"] = {"slots": []}
        actor["spec"]["duration"] = 0.1
        enemy = caster("Leona", damage=19)
        actor_handle, enemy_handle = ENGINE.prepare_actor(actor), ENGINE.prepare_actor(enemy)
        raw, native = [], []
        for count in range(1, 10):
            for geometry in ("spread", "clump"):
                for duration in (0.125, 2.125):
                    spec = {"allies": [actor] * count, "enemies": [enemy] * (10-count),
                            "geometry": geometry, "duration": duration, "initiative": count % 2}
                    raw.append(spec)
                    native.append({**spec, "allies": [actor_handle] * count,
                                   "enemies": [enemy_handle] * (10-count)})
        expected = [ENGINE.simulate_match(spec, True) for spec in raw]
        self.assertExact(expected, ENGINE.simulate_matches(native, trace=True, workers=4))
        # Run in reverse as well so a preceding larger/longer world cannot
        # leave an extra target, event or stack in a prepared input.
        self.assertExact(expected[::-1], ENGINE.simulate_matches(native[::-1], trace=True))

    def test_compact_values_are_exact_full_values_and_keep_input_order(self):
        holder = ally(hp=60, ad=10, lane=1, fx=[
            {"burnOnHit": [0.01, 1.0], "allyHealPct": 0.2}])
        survivor = ally(hp=10000, ad=90, lane=5, frontline=False)
        matches = []
        for initiative in (0, 1):
            for proc_heal in (True, False):
                for post_death in (True, False):
                    matches.append(prepared({"allies": [holder, survivor],
                        "enemies": [ally(ad=100, attack_speed=1, lane=1), ally(ad=45, lane=5)],
                        "duration": 3.1, "initiative": initiative,
                        "damageHealingFromProcs": proc_heal, "postDeathAllyHealing": post_death}))
        full = ENGINE.simulate_matches(matches)
        compact = ENGINE.simulate_matches(matches, detail="compact", workers=4)
        self.assertEqual(len(full), len(compact))
        for complete, small in zip(full, compact):
            self.assertNotIn("allies", small)
            self.assertNotIn("modelAssumptions", small)
            self.assertNotIn("trace", small)
            self.assertExact({key: complete[key] for key in small}, small)
        self.assertNotEqual(full[0]["damage"], full[-1]["damage"])

    def test_python_threads_can_share_prepared_inputs_without_state_leaks(self):
        spec = prepared({"allies": [caster(attack_speed=1.7), ally(ad=35)],
                         "enemies": [caster("Leona"), ally(ad=70, attack_speed=1.1)],
                         "duration": 5.0})
        expected = ENGINE.simulate_match(spec, True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: ENGINE.simulate_match(spec, True), range(16)))
        for result in results:
            self.assertExact(expected, result)

    def test_batch_validation_and_empty_input(self):
        self.assertEqual(ENGINE.simulate_matches([]), [])
        spec = prepared({"allies": [ally(ad=100)], "enemies": [ally()], "duration": 0.1})
        for options in ({"detail": "unknown"}, {"detail": "compact", "trace": True},
                        {"workers": 0}, {"workers": 65}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                ENGINE.simulate_matches([spec], **options)
        for change in ({"allies": []}, {"enemies": spec["enemies"] * 10},
                       {"geometry": "bad"}, {"duration": 0}, {"initiative": 2}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                ENGINE.simulate_matches([spec, {**spec, **change}])
        result = ENGINE.simulate_matches([spec])[0]
        self.assertEqual(result["damage"], 100.0)


if __name__ == "__main__":
    unittest.main()
