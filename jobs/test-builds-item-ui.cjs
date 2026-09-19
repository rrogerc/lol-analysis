// Run with node jobs/test-builds-item-ui.cjs. No browser or npm packages required.
// The Builds tab's item icons: catalog lookup by row name, the initials an
// icon shows without its image, the model notes in the tooltip, and the
// buy order the top rows number their items in.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../web/index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script); // Check the complete dashboard script.
const functions = (first, next) => script.slice(script.indexOf(`function ${first}(`), script.indexOf(`function ${next}(`));
const context = vm.createContext({});
vm.runInContext(functions('buildItemLookup', 'ddRuns'), context);
vm.runInContext(functions('buyOrderItems', 'buildCell'), context);
const { buildItemLookup, itemAbbrev, itemModelText, buyOrderItems } = context;

const cap = { id: 3089, name: "Rabadon's Deathcap", icon: 'x.png' };
const lookup = buildItemLookup({ items: [cap, { id: 1001, name: 'Boots' }] });
assert.equal(lookup.get("Rabadon's Deathcap"), cap, 'Rows name items exactly as the catalog does');
assert.equal(lookup.get('Rabadons Deathcap'), undefined);
assert.equal(buildItemLookup(null).size, 0, 'An older serve without the catalog gives an empty lookup');
assert.equal(buildItemLookup({}).size, 0);

assert.equal(itemAbbrev("Rabadon's Deathcap"), 'RD');
assert.equal(itemAbbrev('Blade of the Ruined King'), 'BR', 'Articles and prepositions are skipped');
assert.equal(itemAbbrev("Lord Dominik's Regards"), 'LD');
assert.equal(itemAbbrev('Ionian Boots of Lucidity'), 'IB');
assert.equal(itemAbbrev('Yun Tal Wildarrows'), 'YT');
assert.equal(itemAbbrev('Boots'), 'BO', 'A single word shows two letters');
assert.equal(itemAbbrev('Void Staff'), 'VS');

// Arrays cross the vm boundary as another realm's Array: compare by value.
const notes = modeled => JSON.parse(JSON.stringify(itemModelText(modeled)));
assert.deepEqual(notes({ covers: ['Magical Opus'], unmodeled: [], note: null }), ['Accounts for: Magical Opus.']);
assert.deepEqual(notes({ covers: ["Mist's Edge", 'Clawing Shadows'], unmodeled: [], note: 'Clawing Shadows is a slow — damage-irrelevant' }),
  ["Accounts for: Mist's Edge, Clawing Shadows.", 'Clawing Shadows is a slow — damage-irrelevant.']);
assert.deepEqual(notes({ covers: [], unmodeled: [], note: 'Stasis is defensive — stats only.' }),
  ['Stasis is defensive — stats only.'], 'A note ending in a period is not doubled');
assert.deepEqual(notes({ covers: [], unmodeled: ['Fervor'], note: null }), ['Text-only passives outside the numbers: Fervor.']);
assert.deepEqual(notes({ covers: [], unmodeled: [], note: null }), ['Stats only.'], 'Boots');
assert.deepEqual(notes(null), ['No model notes for this item.']);

// A top row's items after the boots, in buy order; other rows keep theirs.
const row = { items: ["Berserker's Greaves", 'Infinity Edge', 'Yun Tal Wildarrows', 'Umbral Glaive'],
  buyOrder: ['Yun Tal Wildarrows', 'Umbral Glaive', 'Infinity Edge'] };
assert.deepEqual(JSON.parse(JSON.stringify(buyOrderItems(row))), row.buyOrder);
assert.equal(buyOrderItems({ items: row.items }), null,
  'Rows past the top 10, and cells computed before orders existed, have none');
assert.equal(buyOrderItems({ ...row, buyOrder: ['Yun Tal Wildarrows', 'Umbral Glaive', 'Kraken Slayer'] }), null,
  'An order naming other items is ignored');
assert.equal(buyOrderItems({ ...row, buyOrder: row.buyOrder.slice(1) }), null, 'as is a partial one');
assert.equal(buyOrderItems({ ...row, buyOrder: [row.items[0], ...row.buyOrder.slice(1)] }), null,
  'The boots are not part of the order');
console.log('Builds item icon checks passed: catalog lookup, initials, model notes and buy order.');
