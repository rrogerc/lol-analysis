"""Next-cast mana costs, independently of item/composition ranking.

The archived Sentinel tooltip in data/tft/set18/18.1d/communitydragon.json
defines Mana Reave as increasing the cost of the next Ability cast. Riot's
keyword definition agrees: https://teamfighttactics.leagueoflegends.com/en-us/
news/game-updates/teamfight-tactics-patch-12-23-notes/
"""

from copy import deepcopy
import unittest

from test_tft import ENGINE, SNAP, events as solo_events
from test_tft_symmetric import caster, events, match
from test_tft_team_engine import ally, enemy, fight, flat_calc, trace


def sentinel(amount=10, *, land=0.25, repeat=False, spark=False):
    member = ally("Sentinel", driver="Sentinel", hp=10000, cast=True,
                  attack_speed=1 if repeat else 0.01,
                  fx=[{"ionicSpark": 1.0}] if spark else [])
    flat_calc(member, "MagicDamageCalc1", 0)
    flat_calc(member, "ShieldCalc1", 0)
    member["spec"]["kits"]["base"]["rows"].update(
        KnockupDuration=0.0, ManaReaveFlat=float(amount))
    member["spec"]["unit"]["castTime"] = land
    if repeat:
        member["spec"]["kits"]["base"]["stats"].update(mana=5, initialMana=0)
    return member


def target(initial=0):
    member = caster(damage=0, hp=10000, attack_speed=1)
    member["spec"]["kits"]["base"]["stats"].update(mana=20, initialMana=initial)
    return member


def dummy_fight(reaver, *, initial=0, first_attack=0, duration=7.1, periodic=False):
    spec = deepcopy(reaver["spec"])
    spec.update(duration=duration, dummies={"critEv": 1.0, "slots": [{
        "hp": 10000, "armor": 0, "mr": 0, "ad": 1, "as": 1,
        "ability": 100, "physicalShare": 0, "manaMax": 20,
        "manaStart": initial, "manaPerAttack": 7, "manaFromDamage": False,
        "attackStart": first_attack,
        **({"castInterval": 2, "castStart": 0.5} if periodic else {}),
    }]})
    return ENGINE.simulate(spec, True)[1]


class TestChampionManaReave(unittest.TestCase):
    def test_archived_tooltip_defines_next_cast_cost_increase(self):
        self.assertIn("Increase the Mana cost of the next Ability cast",
                      SNAP.unit("Sentinel")["ability"]["desc"])

    def test_low_mana_keeps_the_entire_cost_increase_until_one_cast(self):
        for initiative in (0, 1):
            with self.subTest(initiative=initiative):
                result = match([sentinel()], [target()], duration=7.1,
                               initiative=initiative)
                casts = events(result, "cast", "enemy")
                # At .25s, the target has only 7 mana. Adding 10 to the
                # next cast's cost requires five attacks for 35 mana at 4s.
                # The following cast has the normal 20 cost and 5 overflow.
                self.assertEqual([hit["time"] for hit in casts], [4.0, 7.0])
                self.assertEqual([hit["amount"] for hit in casts], [35.0, 26.0])

    def test_empty_bar_reave_delays_cast_from_mana_regeneration(self):
        for initiative in (0, 1):
            with self.subTest(initiative=initiative):
                receiver = target()
                receiver["spec"]["unit"]["kind"] = "Specialist"
                receiver["spec"]["role"]["manaRegen"] = 10
                result = match([sentinel()], [receiver], duration=3.1,
                               initiative=initiative)
                self.assertEqual([(hit["time"], hit["amount"]) for hit in
                                  events(result, "cast", "enemy")], [(3.0, 30.0)])

    def test_full_bar_is_preserved_and_ionic_uses_the_cost_of_each_cast(self):
        result = match([sentinel(land=0, spark=True)], [target(initial=20)],
                       duration=4.1)
        casts = events(result, "cast", "enemy")
        self.assertEqual([hit["time"] for hit in casts], [1.0, 4.0])
        self.assertEqual([hit["amount"] for hit in casts], [34.0, 25.0])
        self.assertEqual([hit["amount"] for hit in events(
            result, "damage", name="ionic spark")], [30.0, 20.0])

    def test_reave_during_a_cast_is_reserved_for_the_next_cast(self):
        result = match([sentinel()], [target(initial=20)], duration=4.3)
        self.assertEqual([(hit["time"], hit["amount"]) for hit in
                          events(result, "cast", "enemy")], [(0.0, 27.0), (4.0, 35.0)])
        self.assertEqual([hit["time"] for hit in events(result, "land", "enemy")],
                         [0.25, 4.25])

    def test_repeated_reaves_keep_the_strongest_pending_debuff(self):
        for first, second, cast_time, mana in ((10, 10, 4.0, 35.0),
                                              (10, 20, 5.0, 42.0),
                                              (20, 10, 5.0, 42.0)):
            with self.subTest(first=first, second=second):
                result = match([sentinel(first), sentinel(second, land=1.25)],
                               [target()], duration=5.1)
                self.assertEqual([(hit["time"], hit["amount"]) for hit in
                                  events(result, "cast", "enemy")], [(cast_time, mana)])

    def test_new_reave_after_a_cast_applies_to_the_following_cast(self):
        result = match([sentinel(), sentinel(land=4.25)], [target()], duration=8.1)
        self.assertEqual([(hit["time"], hit["amount"]) for hit in
                          events(result, "cast", "enemy")], [(4.0, 35.0), (8.0, 33.0)])

    def test_manaless_champion_does_not_gain_a_mana_bar(self):
        receiver = ally(ad=10, attack_speed=1)
        result = match([sentinel()], [receiver], duration=2.1)
        self.assertEqual(result["enemies"][0]["attacks"], 3)
        self.assertEqual(result["enemies"][0]["casts"], 0)


class TestDummyManaReave(unittest.TestCase):
    def test_low_mana_dummy_pays_the_full_increase_then_returns_to_base_cost(self):
        result = dummy_fight(sentinel())
        self.assertEqual([hit[0] for hit in solo_events(result, "take", "magic")],
                         [4.0, 7.0])

    def test_empty_dummy_bar_keeps_reave(self):
        result = dummy_fight(sentinel(), first_attack=1, duration=5.1)
        self.assertEqual([hit[0] for hit in solo_events(result, "take", "magic")], [5.0])

    def test_full_dummy_bar_and_ionic_use_next_cost_once(self):
        result = dummy_fight(sentinel(land=0, spark=True), initial=20,
                             first_attack=0.5, duration=4.6)
        self.assertEqual([hit[0] for hit in solo_events(result, "take", "magic")],
                         [1.5, 4.5])
        self.assertEqual([(hit[0], hit[2]) for hit in
                          solo_events(result, "damage", "ionic spark")],
                         [(1.5, 30.0), (4.5, 20.0)])

    def test_repeated_dummy_reaves_do_not_accumulate_cost(self):
        result = dummy_fight(sentinel(repeat=True), duration=4.1)
        self.assertEqual([hit[0] for hit in solo_events(result, "take", "magic")], [4.0])
        self.assertEqual([hit[0] for hit in solo_events(result, "cast")],
                         [0.0, 1.0, 2.0, 3.0, 4.0])

    def test_fixed_interval_fixtures_keep_their_declared_spell_times(self):
        result = dummy_fight(sentinel(repeat=True), periodic=True, duration=4.6)
        self.assertEqual([hit[0] for hit in solo_events(result, "take", "magic")],
                         [0.5, 2.5, 4.5])
        shared = fight([sentinel(repeat=True)], [
            enemy(ability=100, physicalShare=0, manaMax=20,
                  castInterval=2, castStart=0.5)], duration=4.6)
        self.assertEqual([hit["time"] for hit in trace(shared, "enemySpell")],
                         [0.5, 2.5, 4.5])


if __name__ == "__main__":
    unittest.main()
