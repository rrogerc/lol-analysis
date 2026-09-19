# Installed TFT character findings

**All 65 modeled primary champions were located in both installations.** The five requested alternate-form asset names were not located in either Set18/Common decode. This is static client evidence, not live-game mechanic verification. No production files were changed.

- Live game build: `16.17.8104348`; PBE: `16.18.8147109` (installation manifest).
- **50/65 primary BINs are byte-identical.** The other 15 differ only in character-root values; all **235 decoded SpellObjects** across the 65 records are identical between live and PBE.
- **63/65 main spells are generic DataValue/OtherValue templates** in each installation. Alune and Kobuko have the same other named fields already present in the public export. They are not evidence of the active Set18 formulas.
- All inspected live root stats/resource values and spell timing, flags, main data rows and calculation counts match the saved public latest exports (allowing float32 display precision).

## Useful version differences

PBE changes 14 basic-stat fields, 9 primary resource fields and 3 initial-mana fields across 15 champions. These are build differences; they were not applied to the pinned 18.1d model.

`resource field` below means `primaryAbilityResource.0x726ee5cd.baseValue`. Its values resemble a mana cap, but the unknown hash and omitted defaults are preserved explicitly; this audit does not infer active timing rules from it.

| Champion | Live → PBE root values |
|---|---|
| Akali | resource field 30 → 25 |
| Leona | HP 700 → 750; resource field absent → 90; initial mana 40 → 30 |
| Pebbles | AD 30 → 35; resource field 65 → 70; initial mana 25 → 30 |
| Warwick | AD 40 → 45 |
| Diana | resource field 40 → 30 |
| Kha'Zix | HP 850 → 950; AD 30 → 40 |
| Mama Beak | AD 60 → 55 |
| Master Yi | Armor 60 → 55; MR 60 → 55 |
| Amumu | resource field 140 → 125 |
| Brambleback | AD 110 → 115 |
| Morgana | resource field 60 → 65 |
| Nidalee | resource field 40 → 45 |
| Draven | AS 0.8 → 0.85; resource field 120 → 110 |
| Elder Dragon | AD 110 → 125; Armor 70 → 75; MR 70 → 75 |
| Maokai | HP 1100 → 1150; resource field absent → 90; initial mana 40 → 30 |

The PBE values independently corroborate existing pinned overrides for **Draven AS 0.85, Elder Dragon AD 125 and Master Yi Armor/MR 55**. Other PBE changes should remain separate until a target patch is chosen and its actual gameplay data verified. Gromp AP AD 30 matches its equipped form; the joined root AD 45 is not a conflict. Kayle lacks an explicit AD property in these records, which does not mean zero AD.

## What this does not resolve

- **Yorick scaling:** his main spell remains generic, so installed data cannot settle the AD-tooltip/AP-calculation conflict.
- **Akali recasts, Brambleback Frenzy, Azir mana locks:** generic flags do not establish active chain delays, buff refresh/stacking or mana behavior.
- **AoE and replacement attacks:** visual/radius fields and base attack metadata do not establish active hitboxes, nearest-target selection, crit flags or secondary-hit timing.
- **Alternate forms:** `DA_18_Akali_AP`, `DA_18_MasterYi_AP`, `DA_Gromp18_AD`, `DA_KogMaw18_AP` and `DA_NidaleeCougar18_AD` have no exact-name occurrence in any of the 1,234 decoded Set18/Common files. They may be supplied through runtime data or another name/location; their absence here is not proof that forms are missing in-game.

Existing windup, missile-speed, spell timing and AS-ratio metadata is corroborated as static data; whether every field is actively consumed by TFT remains unresolved. No unused placeholder or legacy-looking field was promoted into a mechanics rule.

Evidence: [installed-character-findings.json](installed-character-findings.json) provides 65-entry coverage, file hashes, decoded paths, exact changes and the five alternate-name search results. The installed source manifests are under `.cache/tft/client-audit/2026-09-08/`; the public comparison manifest is [source-manifest.json](source-manifest.json).
