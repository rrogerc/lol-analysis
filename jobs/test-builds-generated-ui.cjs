// Run with node jobs/test-builds-generated-ui.cjs. No browser or npm packages
// required. The Builds tab with machine-written champions: the reviewed ones
// are buttons, the unreviewed ones are found by name and tagged, and the
// banner says what such a kit is and what it assumed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script); // the complete dashboard script parses
const section = (from, to) => script.slice(script.indexOf(from), script.indexOf(to));

const create = tag => ({ tag, className: '', dataset: {}, children: [], ownText: '', hidden: false,
  value: '', placeholder: '', listeners: {},
  classList: { set: new Set(), toggle(c, on) { on ? this.set.add(c) : this.set.delete(c); },
               contains(c) { return this.set.has(c); } },
  append(...children) {
    for (const child of children) this.children.push(typeof child === 'string' ? { textContent: child } : child);
  },
  replaceChildren(...children) { this.children = []; this.ownText = ''; this.append(...children); },
  addEventListener(type, fn) { this.listeners[type] = fn; },
  blur() {},
  get textContent() { return this.ownText + this.children.map(child => child.textContent || '').join(''); },
  set textContent(value) { this.ownText = String(value); this.children = []; } });
const elements = new Map();
const element = id => { if (!elements.has(id)) elements.set(id, create('div')); return elements.get(id); };
for (const id of ['champion-seg', 'champion-more', 'champion-search', 'champion-list', 'builds-generated'])
  assert.ok(html.includes(`id="${id}"`), `the page has #${id}`);

const picked = [];
const context = vm.createContext({
  document: { createElement: create, getElementById: element },
  bstate: { champion: 'kayle', meta: { champions: [
    { slug: 'kayle', name: 'Kayle' },
    { slug: 'drmundo', name: 'Dr. Mundo', objective: 'survival' },
    { slug: 'ahri', name: 'Ahri', generated: true, reviewed: false,
      assumed: ['gen.W.delayS: the wiki gives no per-flame timing'], unused: [] },
    { slug: 'kaisa', name: "Kai'Sa", generated: true, reviewed: false, assumed: [],
      unused: ['E: only grants attack speed'] },
  ] } },
  syncScenarioSeg() {}, renderPool() {}, loadScenario() { picked.push(context.bstate.champion); },
});
vm.runInContext(section('function pickChampion(', '// Show the selected champion'), context);
const { renderChampionPicker, renderGeneratedBanner } = context;
const labels = () => element('champion-seg').children.map(b => b.textContent);

// reviewed champions are buttons; the machine-written ones are not, until picked
renderChampionPicker();
assert.deepEqual(labels(), ['Kayle', 'Dr. Mundo']);
assert.equal(element('champion-more').hidden, false);
assert.match(element('champion-search').placeholder, /^2 more champions, machine-written and unreviewed/);
assert.deepEqual(element('champion-list').children.map(o => o.value), ['Ahri', "Kai'Sa"]);

// typing a machine-written champion's name (or its slug) selects it
const input = element('champion-search');
input.value = 'ahr';
input.listeners.input();
assert.deepEqual(picked, [], 'a partial name selects nothing');
input.value = "kai'sa";
input.listeners.input();
assert.deepEqual(picked, ['kaisa']);
assert.equal(input.value, '');
assert.deepEqual(labels(), ['Kayle', 'Dr. Mundo', "Kai'Saunreviewed"], 'it joins the buttons, tagged');
const kaisa = element('champion-seg').children[2];
assert.ok(kaisa.classList.contains('active'));
assert.equal(kaisa.children[0].className, 'tag');
kaisa.listeners.click();
assert.deepEqual(picked, ['kaisa', 'kaisa']);

// the banner: only for a machine-written champion, with what it assumed and left out
renderGeneratedBanner();
const banner = element('builds-generated');
assert.equal(banner.hidden, false);
assert.match(banner.textContent, /NOT reviewed by a person/);
assert.match(banner.textContent, /Abilities it does not use: E: only grants attack speed\./);
assert.doesNotMatch(banner.textContent, /Assumed numbers/);
context.bstate.champion = 'ahri';
renderGeneratedBanner();
assert.match(banner.textContent, /Assumed numbers: gen\.W\.delayS: the wiki gives no per-flame timing\./);
context.bstate.champion = 'kayle';
renderGeneratedBanner();
assert.equal(banner.hidden, true, 'a hand-written champion has no banner');
renderChampionPicker();
assert.deepEqual(labels(), ['Kayle', 'Dr. Mundo'], 'and the machine-written button goes with the selection');

// a serve from before the field: everything is a button, no search box
context.bstate.meta = { champions: [{ slug: 'kayle', name: 'Kayle' }, { slug: 'twitch', name: 'Twitch' }] };
renderChampionPicker();
assert.deepEqual(labels(), ['Kayle', 'Twitch']);
assert.equal(element('champion-more').hidden, true);

// a cell that records a failed driver is reported, not rendered
assert.ok(script.includes("if (d.error) {"), 'loadScenario handles an error cell');

console.log('Builds generated-champion UI checks passed: picker, search, tag, banner and legacy meta.');
