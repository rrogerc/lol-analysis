// Run with node jobs/test-onetricks-ui.cjs. No browser or npm packages required.
// The One-tricks table's order: champions ranked by one-trick count, equal
// counts sharing a rank, whichever column the table is sorted by.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../web/index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script); // Check the complete dashboard script.
const functions = (first, next) => script.slice(script.indexOf(`function ${first}(`), script.indexOf(`function ${next}(`));
const context = vm.createContext({});
vm.runInContext(functions('onetrickRows', 'renderOnetricks'), context);
const { onetrickRows } = context;

const rows = [
  { c: 'zed', n: 3, top: 0 },
  { c: 'yasuo', n: 5, top: 2 },
  { c: 'garen', n: 0, top: 0 },
  { c: 'ahri', n: 5, top: 0 },
];
// Objects cross the vm boundary from another realm: compare by value.
const order = (key, dir) => JSON.parse(JSON.stringify(onetrickRows(rows, key, dir))).map(r => [r.rank, r.c]);
assert.deepEqual(order('rank', 1), [[1, 'ahri'], [1, 'yasuo'], [3, 'zed'], [4, 'garen']],
  'Equal counts share a rank, alphabetically, and the next rank skips past them');
assert.deepEqual(order('rank', -1), [[4, 'garen'], [3, 'zed'], [1, 'ahri'], [1, 'yasuo']]);
assert.deepEqual(order('n', -1), order('rank', 1), 'Sorting by the count is the ranking');
assert.deepEqual(order('c', 1), [[1, 'ahri'], [4, 'garen'], [1, 'yasuo'], [3, 'zed']],
  'Ranks stay attached when sorted by name');
assert.deepEqual(order('top', -1), [[1, 'yasuo'], [1, 'ahri'], [3, 'zed'], [4, 'garen']],
  'Ties in a role column fall back to rank order');
assert.equal(rows[0].rank, undefined, 'The payload rows are not modified');
console.log('One-tricks table checks passed: shared ranks and column sorting.');
