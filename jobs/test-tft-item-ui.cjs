// Run with node jobs/test-tft-item-ui.cjs. No browser or npm packages required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../web/index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script); // Check the complete dashboard script, including navigation.
const functions = (first, next) => script.slice(script.indexOf(`function ${first}(`), script.indexOf(`function ${next}(`));
const recipe = (api, components) => ({ api, components: components.map(api => ({ api, name: api })) });
const context = vm.createContext({ tstate: { meta: { items: [
  recipe('Blue', ['Tear', 'Tear']), recipe('Cap', ['Rod', 'Rod']),
  recipe('Shojin', ['Sword', 'Tear']), recipe('Nashor', ['Bow', 'Belt']),
  recipe('JG', ['Rod', 'Glove']), recipe('ShojinCopy', ['Sword', 'Tear']),
] } }, tcore: { variedComponents: true } });
vm.runInContext(functions('tftComponentUse', 'tftCoreLoss') + functions('tftCoreNear', 'tftCoreRequirement'), context);
const use = context.tftComponentUse;
const order = context.tftCoreCompletionOrder;
const completion = (third, lossPct, status = 'comparable') => ({
  itemApis: ['Blue', 'Cap', third], performance: { status, lossPct },
});
const best = completion('Blue', 0), duplicate = completion('Cap', 1),
  shojin = completion('Shojin', 2), sameRecipe = completion('ShojinCopy', 2.1),
  nashor = completion('Nashor', 4.9), outside = completion('JG', 5.00001);
const rows = [best, duplicate, shojin, sameRecipe, nashor, outside];
const core = { completions: rows };
const snapshot = JSON.stringify(core);
const result = order(core, 5);
assert.equal(result[0], best, 'Always show the actual best completion first');
assert.equal(result[1], nashor, 'A balanced recipe can precede a stronger repeated-component alternative within tolerance');
assert.equal(result[2], shojin, 'Preview includes another distinct component footprint');
assert.equal(result.indexOf(outside), 5, 'Do not promote a component-diverse build outside tolerance');
assert.equal(result.length, rows.length, 'Expansion retains every completion');
assert.equal(new Set(result).size, rows.length, 'No preview/expansion duplicates');
assert.equal(JSON.stringify(core), snapshot, 'Presentation leaves scores and original rankings unchanged');
assert.equal(JSON.stringify(order(core, 5, false)), JSON.stringify(rows), 'Disabling the preference restores performance order');
assert.equal(use(['Blue', 'Blue', 'Blue']).components[0].count, 6, 'Repeated items and duplicate ingredients both count');
assert.equal(use(['Blue', 'Cap', 'Nashor']).maxCopies, 2);
assert.equal(use(['Blue', 'Cap', 'Shojin']).maxCopies, 3);
assert.equal(use(['Blue', 'Cap', 'Shojin']).signature, use(['Shojin', 'Cap', 'Blue']).signature, 'Component identity is independent of item ordering');
assert.equal(use(['missing']), null, 'Unknown recipes do not invent a component preference');
assert.equal(order({ completions: [] }, 5).length, 0);
assert.equal(order({ completions: [best] }, 5)[0], best);
const edge = completion('Nashor', 5);
assert.equal(order({ completions: [best, duplicate, shojin, edge] }, 5)[1], edge, 'The 5% boundary is inclusive');
const censored = completion('Nashor', null, 'shortSurvival');
assert.equal(order({ completions: [best, duplicate, shojin, censored] }, 5)[1], shojin, 'Unknown survival loss cannot qualify as a near-optimal alternative');
const missing = { itemApis: ['missing'], performance: { status: 'comparable', lossPct: 0 } };
assert.equal(order({ completions: [missing, best] }, 5)[0], missing, 'Older metadata preserves the existing order');
console.log('TFT item preview checks passed: optimal anchor, component counts, diversity, tolerance, censored outcomes, fallback, and unchanged data.');
