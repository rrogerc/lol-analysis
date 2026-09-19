"""Keep patch-note values and source context intact for reviewed transitions."""
import unittest

import tft


def entry(text, *, parent="", section="UNITS: TIER 3", major="LARGE CHANGES", update=""):
    return {"text": text, "parent": parent, "section": section, "major": major, "update": update}


class TestPatchNotesParser(unittest.TestCase):
    def test_real_18_2_nested_form_labels_do_not_absorb_numeric_values(self):
        cases = [
            ("Master Yi AP Form: Ability Damage: 140/210/335 AP ⇒ 125/190/285 AP",
             "Master Yi AP Form: Ability Damage", "140/210/335 AP", "125/190/285 AP"),
            ("Master Yi AD Form: Base AD: 65 ⇒ 60",
             "Master Yi AD Form: Base AD", "65", "60"),
            ("Nidalee AP Form: Nidalee Empowered Attack Damage: 285/425 AP ⇒ 300/450 AP",
             "Nidalee AP Form: Nidalee Empowered Attack Damage", "285/425 AP", "300/450 AP"),
        ]
        for text, what, old, new in cases:
            with self.subTest(what=what):
                parsed = tft.patch_entry_changes(entry(text))
                self.assertEqual(parsed, [{"what": what, "old": old, "new": new,
                    "section": "UNITS: TIER 3", "major": "LARGE CHANGES", "update": ""}])

    def test_html_inline_formatting_preserves_form_label_and_mana_pair(self):
        document = tft.patch_notes_document("""
            <h2>LARGE CHANGES</h2><h4>UNITS: TIER 1</h4><ul>
              <li><strong>Akali AD Form</strong> Mana: 0/30 &rArr; 0/25</li>
              <li>Leona Mana: 40/100 ⇒ 30/90</li>
            </ul><h4>UNITS: TIER 3</h4><ul>
              <li>Master Yi <em>AP Form</em>: Ability Damage:
                  140/210/335 AP ⇒ 125/190/285 AP</li>
            </ul>""", "18.2")
        changes = document["changes"]
        self.assertEqual([(c["what"], c["old"], c["new"]) for c in changes], [
            ("Akali AD Form Mana", "0/30", "0/25"),
            ("Leona Mana", "40/100", "30/90"),
            ("Master Yi AP Form: Ability Damage", "140/210/335 AP", "125/190/285 AP"),
        ])
        self.assertEqual([c["section"] for c in changes], ["UNITS: TIER 1"] * 2 + ["UNITS: TIER 3"])
        self.assertTrue(all(c["major"] == "LARGE CHANGES" and c["update"] == "" for c in changes))

    def test_compound_numeric_statements_keep_each_label_and_shared_parent(self):
        document = tft.patch_notes_document("""
            <h2>LARGE CHANGES</h2><h4>UNITS: TIER 5</h4><ul><li>Taric:
              <ul><li>Passive Shield: 175/350 + 10% max HP ⇒ 100/225 + 15% max HP.
                  Heal: 200/300 AP ⇒ 250/375 AP</li></ul>
            </li></ul>""", "18.2")
        self.assertEqual(document["changes"], [
            {"what": "Taric Passive Shield", "old": "175/350 + 10% max HP",
             "new": "100/225 + 15% max HP", "section": "UNITS: TIER 5",
             "major": "LARGE CHANGES", "update": ""},
            {"what": "Taric Heal", "old": "200/300 AP", "new": "250/375 AP",
             "section": "UNITS: TIER 5", "major": "LARGE CHANGES", "update": ""},
        ])
        compound = next(n for n in document["notes"] if n["text"].startswith("Passive Shield:"))
        self.assertEqual(compound["parent"], "Taric")
        self.assertIn(". Heal:", compound["text"])
        self.assertEqual(tft.patch_entry_changes(compound), document["changes"])

    def test_deep_three_star_headings_and_sibling_scope_are_retained(self):
        document = tft.patch_notes_document("""
            <h2>SMALL CHANGES</h2><h4>UNITS</h4><ul>
              <li>3-star 4-costs:<ul><li>Nidalee:<ul><li>AD Form:
                <ul><li>Ability Damage: 2500% AD ⇒ 3000% AD</li></ul>
              </li></ul></li></ul></li>
              <li>3-star 5-costs:<ul><li>Gnar:<ul>
                <li>Rage Per Attack: 5 ⇒ 20</li>
              </ul></li></ul></li>
            </ul>""", "18.2")
        self.assertEqual([(c["what"], c["old"], c["new"]) for c in document["changes"]], [
            ("3-star 4-costs Nidalee AD Form Ability Damage", "2500% AD", "3000% AD"),
            ("3-star 5-costs Gnar Rage Per Attack", "5", "20"),
        ])
        self.assertTrue(all(c["section"] == "UNITS" and c["major"] == "SMALL CHANGES"
                            for c in document["changes"]))
        leaf = next(n for n in document["notes"] if n["text"].startswith("Ability Damage:"))
        self.assertEqual(leaf["parent"], "3-star 4-costs Nidalee AD Form")

    def test_deep_parents_retain_mid_patch_date_without_leaking_into_main_article(self):
        document = tft.patch_notes_document("""
            <h2>Mid-Patch Updates</h2><h3>SEPTEMBER 10TH</h3><h4>UNITS</h4>
            <ul><li>3-star 4-costs:<ul><li>Nidalee AD Form:
              <ul><li>Ability Damage: 2500% AD ⇒ 3000% AD</li></ul>
            </li></ul></li></ul>
            <h2>LARGE CHANGES</h2><h4>ITEMS</h4>
            <ul><li>Bloodthirster Trigger Health: 40% ⇒ 50%</li></ul>""", "18.2")
        self.assertEqual(document["patch"], "18.2b")
        self.assertEqual(document["updates"], ["SEPTEMBER 10TH"])
        hotfix, main = document["changes"]
        self.assertEqual((hotfix["what"], hotfix["update"], hotfix["major"]),
                         ("3-star 4-costs Nidalee AD Form Ability Damage", "SEPTEMBER 10TH", "Mid-Patch Updates"))
        self.assertEqual((main["what"], main["update"], main["major"]),
                         ("Bloodthirster Trigger Health", "", "LARGE CHANGES"))

    def test_numeric_ratio_colon_is_kept_for_review_instead_of_becoming_a_label(self):
        parsed = tft.patch_entry_changes(entry("Damage Ratio: 1:2 AD/AP ⇒ 2:3 AD/AP", parent="Champion AP Form"))
        self.assertEqual(len(parsed), 1)
        self.assertEqual((parsed[0]["what"], parsed[0]["old"], parsed[0]["new"]),
                         ("Champion AP Form Damage Ratio", "1:2 AD/AP", "2:3 AD/AP"))

    def test_nested_label_stops_before_signed_or_decimal_numeric_expression(self):
        for old, new in (("-1:2 AD/AP", "-2:3 AD/AP"), (".5:1 AD/AP", ".75:1 AD/AP")):
            with self.subTest(old=old):
                parsed = tft.patch_entry_changes(entry(f"Champion AP Form: Ratio: {old} ⇒ {new}"))
                self.assertEqual((parsed[0]["what"], parsed[0]["old"], parsed[0]["new"]),
                                 ("Champion AP Form: Ratio", old, new))

    def test_mechanics_suffix_is_not_silently_removed_from_new_value(self):
        parsed = tft.patch_entry_changes(entry(
            "Champion AP Form: Damage: 140/210 AP ⇒ 125/190 AP and now stuns the target"))
        self.assertEqual(parsed[0]["old"], "140/210 AP")
        self.assertEqual(parsed[0]["new"], "125/190 AP and now stuns the target")

    def test_real_18_2_xp_level_changes_keep_systems_context(self):
        document = tft.patch_notes_document("""
            <h2>SYSTEMS</h2><h4>XP PER LEVEL</h4><ul>
              <li>Level 7 to Level 8: 60 ⇒ 56</li>
              <li>Level 8 to Level 9: 68 ⇒ 64</li>
              <li>Level 9 to Level 10: 68 ⇒ 64</li>
            </ul><h2>LARGE CHANGES</h2><h4>TRAITS</h4>
            <ul><li>Hunter Targeting Duration for Damage Amp: 4 seconds ⇒ 3 seconds</li></ul>""", "18.2")
        xp, hunter = document["changes"][:3], document["changes"][3]
        self.assertEqual([(c["what"], c["old"], c["new"]) for c in xp], [
            ("Level 7 to Level 8", "60", "56"),
            ("Level 8 to Level 9", "68", "64"),
            ("Level 9 to Level 10", "68", "64"),
        ])
        self.assertTrue(all(c["section"] == "XP PER LEVEL" and c["major"] == "SYSTEMS" for c in xp))
        self.assertEqual((hunter["section"], hunter["major"]), ("TRAITS", "LARGE CHANGES"))

    def test_mechanics_only_leaf_remains_available_for_review(self):
        document = tft.patch_notes_document("""
            <h2>LARGE CHANGES</h2><h4>UNITS: TIER 4</h4><ul>
              <li>Sentinel Now targets the largest line of enemies. No longer required to include his current target</li>
              <li>Sentinel Ability Shield: 400/500 AP ⇒ 350/450 AP</li>
            </ul>""", "18.2")
        self.assertEqual(len(document["changes"]), 1)
        mechanics = next(n for n in document["notes"] if n["text"].startswith("Sentinel Now"))
        self.assertEqual(tft.patch_entry_changes(mechanics), [])
        self.assertEqual((mechanics["section"], mechanics["major"]), ("UNITS: TIER 4", "LARGE CHANGES"))


if __name__ == "__main__":
    unittest.main()
