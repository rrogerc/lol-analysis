"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const STATIC_DIR = path.resolve(__dirname, "../static");
const SCRIPT_FILES = ["tft-flex-worker.js", "app.js", "lol.js", "tft.js"];
const VOID_TAGS = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"]);

function readStatic(name) {
  return fs.readFileSync(path.join(STATIC_DIR, name), "utf8");
}

const INDEX_HTML = readStatic("index.html");
const STYLES_CSS = readStatic("styles.css");
const WORKER_SOURCE = readStatic("tft-flex-worker.js");
const SCRIPTS = SCRIPT_FILES.map(name => ({
  name,
  source: readStatic(name),
  compiled: new vm.Script(readStatic(name), { filename: name }),
}));

const CSS_VARS = (() => {
  const vars = {};
  const rootMatch = /:root\s*\{([\s\S]*?)\}/.exec(STYLES_CSS);
  if (rootMatch) {
    for (const m of rootMatch[1].matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
      vars[m[1]] = m[2].trim();
    }
  }
  return vars;
})();

function decodeHtml(str) {
  return String(str)
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, "&");
}

function toCamelDataKey(attrName) {
  return attrName.slice(5).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

class TextNode {
  constructor(text = "") {
    this.nodeType = 3;
    this.parent = null;
    this._text = String(text);
  }
  get parentNode() { return this.parent; }
  get parentElement() { return this.parent; }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); }
  remove() {
    if (this.parent) {
      this.parent.childNodes = this.parent.childNodes.filter(n => n !== this);
      this.parent = null;
    }
  }
}

function matchSimpleSelector(el, token) {
  if (!el || el.nodeType !== 1 || !token) return false;
  let rest = token;

  const pseudos = [];
  rest = rest.replace(/:(hover|focus-visible)\b/g, (_, p) => {
    pseudos.push(p);
    return "";
  });
  for (const p of pseudos) {
    if (p === "hover" && !el.hovered) return false;
    if (p === "focus-visible" && !el.focusVisible) return false;
  }

  const attrs = [];
  rest = rest.replace(/\[([\w-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\]]*)))?\]/g, (_, k, v1, v2, v3) => {
    const val = v1 !== undefined ? v1 : v2 !== undefined ? v2 : v3;
    attrs.push([k, val]);
    return "";
  });
  for (const [k, expected] of attrs) {
    if (k === "hidden") {
      if (!el.hidden) return false;
      continue;
    }
    if (k === "disabled") {
      if (!el.disabled) return false;
      continue;
    }
    const actual = el.getAttribute(k);
    if (expected === undefined) {
      if (actual === null) return false;
    } else if (actual !== expected) {
      return false;
    }
  }

  let idReq = null;
  rest = rest.replace(/#([\w-]+)/g, (_, id) => {
    idReq = id;
    return "";
  });
  if (idReq !== null && el.id !== idReq) return false;

  const classes = [];
  rest = rest.replace(/\.([\w-]+)/g, (_, cls) => {
    classes.push(cls);
    return "";
  });
  if (classes.length) {
    const elClasses = el.className.split(/\s+/).filter(Boolean);
    for (const c of classes) {
      if (!elClasses.includes(c)) return false;
    }
  }

  const tag = rest.trim().toLowerCase();
  if (tag && tag !== "*" && el.tagName !== tag) return false;
  return true;
}

function matchCompoundChain(el, chain) {
  if (!matchSimpleSelector(el, chain[chain.length - 1])) return false;
  let cur = el.parent;
  for (let i = chain.length - 2; i >= 0; --i) {
    while (cur && !matchSimpleSelector(cur, chain[i])) {
      cur = cur.parent;
    }
    if (!cur) return false;
    cur = cur.parent;
  }
  return true;
}

function parseSelectorGroups(selector) {
  return String(selector)
    .split(",")
    .map(part => part.trim())
    .filter(Boolean)
    .map(part => {
      const tokens = [];
      let cur = "";
      let inBracket = 0;
      let quote = null;
      for (let i = 0; i < part.length; ++i) {
        const ch = part[i];
        if (quote) {
          cur += ch;
          if (ch === quote) quote = null;
        } else if (ch === '"' || ch === "'") {
          quote = ch;
          cur += ch;
        } else if (ch === "[") {
          ++inBracket;
          cur += ch;
        } else if (ch === "]") {
          --inBracket;
          cur += ch;
        } else if (/\s/.test(ch) && inBracket === 0) {
          if (cur) { tokens.push(cur); cur = ""; }
        } else {
          cur += ch;
        }
      }
      if (cur) tokens.push(cur);
      return tokens;
    });
}

class Element {
  constructor(tag = "div", ownerDocument = null) {
    const lower = String(tag).toLowerCase();
    this.nodeType = 1;
    this.tag = lower;
    this.tagName = lower;
    this.ownerDocument = ownerDocument;
    this.parent = null;
    this.childNodes = [];
    this.dataset = {};
    this.attributes = {};
    this.events = {};
    this.listeners = {};
    this._id = "";
    this._className = "";
    this._value = "";
    this._hasExplicitValue = false;
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.selected = false;
    this.readOnly = false;
    this.open = false;
    this.title = "";
    this.src = "";
    this.href = "";
    this.alt = "";
    this.type = "";
    this.min = "";
    this.max = "";
    this.step = "";
    this.placeholder = "";
    this.htmlFor = "";
    this.scrollTop = 0;
    this.clientWidth = 800;
    this.offsetWidth = 220;
    this.offsetHeight = 80;
    this.hovered = false;
    this.focusVisible = false;
    this.focused = false;
    this._rect = { left: 20, top: 20, right: 120, bottom: 52, width: 760, height: 310 };

    const styleObj = {
      setProperty(k, v) { styleObj[k] = String(v); },
      getPropertyValue(k) { return styleObj[k] || ""; },
    };
    this.style = styleObj;

    const self = this;
    this.classList = {
      add(...names) {
        const set = new Set(self._className.split(/\s+/).filter(Boolean));
        for (const n of names) if (n) set.add(n);
        self.className = [...set].join(" ");
      },
      remove(...names) {
        const set = new Set(self._className.split(/\s+/).filter(Boolean));
        for (const n of names) set.delete(n);
        self.className = [...set].join(" ");
      },
      toggle(name, force) {
        const set = new Set(self._className.split(/\s+/).filter(Boolean));
        const shouldHave = force === undefined ? !set.has(name) : Boolean(force);
        if (shouldHave) set.add(name);
        else set.delete(name);
        self.className = [...set].join(" ");
        return shouldHave;
      },
      contains(name) {
        return self._className.split(/\s+/).filter(Boolean).includes(name);
      },
    };
  }

  get id() { return this._id; }
  set id(v) {
    this._id = String(v);
    if (this._id) this.attributes.id = this._id;
    else delete this.attributes.id;
  }

  get className() { return this._className; }
  set className(v) {
    this._className = String(v || "");
    if (this._className) this.attributes.class = this._className;
    else delete this.attributes.class;
  }

  get value() {
    if (this.tagName === "select" && !this._hasExplicitValue) {
      const opts = this.children.filter(c => c.tagName === "option");
      const chosen = opts.find(o => o.selected) || opts[0];
      return chosen ? chosen.value : this._value;
    }
    return this._value;
  }
  set value(v) {
    this._value = String(v ?? "");
    this._hasExplicitValue = true;
  }

  get parentNode() { return this.parent; }
  get parentElement() { return this.parent; }
  get children() { return this.childNodes.filter(n => n.nodeType === 1); }
  get firstChild() { return this.children[0] || this.childNodes[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || this.childNodes[this.childNodes.length - 1] || null; }

  get textContent() {
    return this.childNodes.map(n => n.textContent).join("");
  }
  set textContent(v) {
    for (const n of this.childNodes) n.parent = null;
    const s = String(v ?? "");
    this.childNodes = s ? [this._wrapChild(s)] : [];
  }

  _wrapChild(child) {
    if (child instanceof Element || child instanceof TextNode) return child;
    const t = new TextNode(child == null ? "" : String(child));
    return t;
  }

  append(...nodes) {
    for (const raw of nodes) {
      if (raw == null) continue;
      const node = this._wrapChild(raw);
      if (node.parent) node.remove();
      node.parent = this;
      if (node.nodeType === 1 && !node.ownerDocument) node.ownerDocument = this.ownerDocument;
      this.childNodes.push(node);
    }
    if (this.tagName === "select") {
      const opts = this.children.filter(c => c.tagName === "option");
      if (opts.length && !opts.some(o => o.value === this._value)) {
        this._hasExplicitValue = false;
      }
    }
  }

  appendChild(node) {
    this.append(node);
    return node;
  }

  replaceChildren(...nodes) {
    for (const n of this.childNodes) n.parent = null;
    this.childNodes = [];
    if (this.tagName === "select") this._hasExplicitValue = false;
    this.append(...nodes);
  }

  remove() {
    if (this.parent) {
      this.parent.childNodes = this.parent.childNodes.filter(n => n !== this);
      this.parent = null;
    }
  }

  setAttribute(name, value) {
    const str = String(value);
    this.attributes[name] = str;
    if (name === "id") this._id = str;
    else if (name === "class") this._className = str;
    else if (name === "title") this.title = str;
    else if (name === "type") this.type = str;
    else if (name === "value") { this._value = str; this._hasExplicitValue = true; }
    else if (name === "for") this.htmlFor = str;
    else if (name === "hidden") this.hidden = true;
    else if (name === "disabled") this.disabled = true;
    else if (name === "checked") this.checked = true;
    else if (name === "selected") this.selected = true;
    else if (name === "open") this.open = true;
    else if (name.startsWith("data-")) {
      this.dataset[toCamelDataKey(name)] = str;
    }
  }

  getAttribute(name) {
    if (name === "id") return this._id || (Object.hasOwn(this.attributes, "id") ? this.attributes.id : null);
    if (name === "class") return this._className || (Object.hasOwn(this.attributes, "class") ? this.attributes.class : null);
    if (name.startsWith("data-")) {
      const k = toCamelDataKey(name);
      if (Object.hasOwn(this.dataset, k)) return String(this.dataset[k]);
    }
    return Object.hasOwn(this.attributes, name) ? this.attributes[name] : null;
  }

  hasAttribute(name) {
    return this.getAttribute(name) !== null;
  }

  removeAttribute(name) {
    delete this.attributes[name];
    if (name === "id") this._id = "";
    else if (name === "class") this._className = "";
    else if (name === "hidden") this.hidden = false;
    else if (name === "disabled") this.disabled = false;
    else if (name.startsWith("data-")) delete this.dataset[toCamelDataKey(name)];
  }

  addEventListener(type, fn) {
    (this.events[type] ||= []).push(fn);
    this.listeners[type] = fn;
  }

  removeEventListener(type, fn) {
    if (this.events[type]) {
      this.events[type] = this.events[type].filter(cb => cb !== fn);
    }
  }

  async fire(type, extra = {}) {
    const ev = {
      type,
      target: this,
      currentTarget: this,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() {},
      ...extra,
    };
    for (const cb of [...(this.events[type] || [])]) {
      await cb(ev);
    }
    return ev;
  }

  dispatch(type, extra = {}) {
    const ev = {
      type,
      target: this,
      currentTarget: this,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() {},
      ...extra,
    };
    let last;
    for (const cb of [...(this.events[type] || [])]) {
      last = cb(ev);
    }
    return last;
  }

  click(extra = {}) {
    return this.fire("click", extra);
  }

  focus() {
    this.focused = true;
    if (this.ownerDocument) {
      this.ownerDocument.activeElement = this;
      this.ownerDocument.focusHistory.push(this);
    }
  }

  blur() {
    this.focused = false;
    if (this.ownerDocument && this.ownerDocument.activeElement === this) {
      this.ownerDocument.activeElement = null;
    }
  }

  select() { this.selected = true; }
  setSelectionRange() {}
  scrollIntoView() {}
  getBoundingClientRect() { return { ...this._rect }; }

  contains(other) {
    let cur = other;
    while (cur) {
      if (cur === this) return true;
      cur = cur.parent;
    }
    return false;
  }

  matches(selector) {
    const groups = parseSelectorGroups(selector);
    return groups.some(chain => matchCompoundChain(this, chain));
  }

  closest(selector) {
    let cur = this;
    while (cur && cur.nodeType === 1) {
      if (cur.matches(selector)) return cur;
      cur = cur.parent;
    }
    return null;
  }

  querySelectorAll(selector) {
    const groups = parseSelectorGroups(selector);
    const out = [];
    const visit = node => {
      for (const child of node.childNodes) {
        if (child.nodeType === 1) {
          if (groups.some(chain => matchCompoundChain(child, chain))) out.push(child);
          visit(child);
        }
      }
    };
    visit(this);
    return out;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

function parseAttributes(attrText, el) {
  const rx = /([\w:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>/=`]+)))?/g;
  for (const m of attrText.matchAll(rx)) {
    const name = m[1];
    const val = m[2] !== undefined ? decodeHtml(m[2])
      : m[3] !== undefined ? decodeHtml(m[3])
      : m[4] !== undefined ? decodeHtml(m[4]) : "";
    el.setAttribute(name, val);
  }
}

function parseHtmlInto(htmlSource, doc) {
  const cleaned = htmlSource
    .replace(/<!doctype[^>]*>/gi, "")
    .replace(/<!--[\s\S]*?-->/g, "");
  const root = doc.documentElement;
  const stack = [root];
  const tokenRx = /<\/([a-zA-Z][\w-]*)>|<(script\b[^>]*>[\s\S]*?<\/script>)|<([a-zA-Z][\w-]*)([^>]*?)(\/?)>|([^<]+)/g;

  for (const m of cleaned.matchAll(tokenRx)) {
    if (m[2]) continue; // skip external <script> tags during HTML structure parse
    if (m[1]) {
      const closeTag = m[1].toLowerCase();
      if (closeTag === "html") continue;
      for (let i = stack.length - 1; i >= 1; --i) {
        if (stack[i].tagName === closeTag) {
          stack.length = i;
          break;
        }
      }
    } else if (m[3]) {
      const openTag = m[3].toLowerCase();
      const attrStr = m[4] || "";
      const selfClose = Boolean(m[5]) || VOID_TAGS.has(openTag);
      if (openTag === "html") {
        parseAttributes(attrStr, root);
        continue;
      }
      let el;
      if (openTag === "head") el = doc.head;
      else if (openTag === "body") el = doc.body;
      else el = new Element(openTag, doc);
      parseAttributes(attrStr, el);
      if (el !== doc.head && el !== doc.body) {
        stack[stack.length - 1].append(el);
      }
      if (!selfClose) stack.push(el);
    } else if (m[6]) {
      const rawText = m[6];
      if (!rawText.trim() && stack[stack.length - 1].tagName !== "code" && stack[stack.length - 1].tagName !== "p" && stack[stack.length - 1].tagName !== "span" && stack[stack.length - 1].tagName !== "div" && stack[stack.length - 1].tagName !== "label") {
        continue;
      }
      if (rawText.trim() || rawText.includes(" ")) {
        const parentTag = stack[stack.length - 1].tagName;
        if (!["html", "head", "body", "nav", "section", "table", "thead", "tbody", "tr", "select", "details", "header"].includes(parentTag)) {
          stack[stack.length - 1].append(decodeHtml(rawText));
        }
      }
    }
  }
}

function createDom(htmlSource = INDEX_HTML) {
  const docEvents = {};
  const document = {
    hidden: false,
    activeElement: null,
    focusHistory: [],
    documentElement: null,
    head: null,
    body: null,
    execCommand: null,
    createElement(tag) { return new Element(tag, document); },
    createElementNS(_ns, tag) { return new Element(tag, document); },
    createTextNode(text) { return new TextNode(text); },
    getElementById(id) {
      const target = String(id);
      const find = node => {
        if (node.nodeType === 1 && node.id === target) return node;
        for (const child of node.childNodes || []) {
          if (child.nodeType === 1) {
            const hit = find(child);
            if (hit) return hit;
          }
        }
        return null;
      };
      return find(document.documentElement);
    },
    querySelectorAll(selector) {
      return document.documentElement.querySelectorAll(selector);
    },
    querySelector(selector) {
      return document.documentElement.querySelector(selector);
    },
    addEventListener(type, fn) {
      (docEvents[type] ||= []).push(fn);
    },
    removeEventListener(type, fn) {
      if (docEvents[type]) docEvents[type] = docEvents[type].filter(cb => cb !== fn);
    },
    async fire(type, extra = {}) {
      const ev = {
        type,
        target: extra.target || document.body,
        defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; },
        ...extra,
      };
      for (const cb of [...(docEvents[type] || [])]) {
        await cb(ev);
      }
      return ev;
    },
  };
  document.documentElement = new Element("html", document);
  document.head = new Element("head", document);
  document.body = new Element("body", document);
  document.documentElement.append(document.head, document.body);
  parseHtmlInto(htmlSource, document);
  return document;
}

function createStorage() {
  const store = {};
  return new Proxy(store, {
    get(target, prop) {
      if (prop === "getItem") return k => (Object.hasOwn(target, k) ? target[k] : null);
      if (prop === "setItem") return (k, v) => { target[k] = String(v); };
      if (prop === "removeItem") return k => { delete target[k]; };
      if (prop === "clear") return () => { for (const k of Object.keys(target)) delete target[k]; };
      return target[prop];
    },
  });
}

function createWorkerClass() {
  const workers = [];
  const compiledWorker = new vm.Script(WORKER_SOURCE, { filename: "tft-flex-worker.js" });
  class MockWorker {
    constructor(url) {
      assert.equal(url, "tft-flex-worker.js", "Worker loads web/static/tft-flex-worker.js");
      this.url = url;
      this.terminated = false;
      this.messages = [];
      this.onmessage = null;
      this.onerror = null;
      workers.push(this);
      const self = {
        postMessage: data => {
          if (!this.terminated) this.onmessage?.({ data });
        },
      };
      this.self = self;
      compiledWorker.runInNewContext({ self });
    }
    postMessage(data) {
      this.messages.push(data);
      setTimeout(() => {
        if (!this.terminated && this.self.onmessage) {
          this.self.onmessage({ data });
        }
      }, 0);
    }
    terminate() {
      this.terminated = true;
    }
  }
  return { Worker: MockWorker, workers };
}

const BRIDGE_SCRIPT = new vm.Script(`
Object.defineProperties(globalThis, {
  SLOTS: { get: () => SLOTS, configurable: true },
  SRC_COLOR: { get: () => SRC_COLOR, configurable: true },
  META: { get: () => META, set: v => { META = v; }, configurable: true },
  DATA: { get: () => DATA, set: v => { DATA = v; }, configurable: true },
  OT: { get: () => OT, set: v => { OT = v; }, configurable: true },
  OT_LANES: { get: () => OT_LANES, configurable: true },
  state: { get: () => state, configurable: true },
  sortKey: { get: () => sortKey, set: v => { sortKey = v; }, configurable: true },
  sortDir: { get: () => sortDir, set: v => { sortDir = v; }, configurable: true },
  otSortKey: { get: () => otSortKey, set: v => { otSortKey = v; }, configurable: true },
  otSortDir: { get: () => otSortDir, set: v => { otSortDir = v; }, configurable: true },
  tftTip: { get: () => tftTip, configurable: true },
  TIER_DESC: { get: () => TIER_DESC, configurable: true },
  restoringHash: { get: () => restoringHash, set: v => { restoringHash = v; }, configurable: true },
  restoredHash: { get: () => restoredHash, set: v => { restoredHash = v; }, configurable: true },
  bstate: { get: () => bstate, configurable: true },
  bboard: { get: () => bboard, configurable: true },
  DEFENSE_LABELS: { get: () => DEFENSE_LABELS, configurable: true },
  WARMER_WHY: { get: () => WARMER_WHY, configurable: true },
  EXPECTED_TITLE: { get: () => EXPECTED_TITLE, configurable: true },
  MEAN_TITLE: { get: () => MEAN_TITLE, configurable: true },
  DD_KIND_LABEL: { get: () => DD_KIND_LABEL, configurable: true },
  TTD_TITLE: { get: () => TTD_TITLE, configurable: true },
  SURV_MEAN_TITLE: { get: () => SURV_MEAN_TITLE, configurable: true },
  BOARD_TIME_TITLE: { get: () => BOARD_TIME_TITLE, configurable: true },
  BOARD_MEAN_TITLE: { get: () => BOARD_MEAN_TITLE, configurable: true },
  INSTANT_TITLE: { get: () => INSTANT_TITLE, configurable: true },
  UNREVIEWED_TITLE: { get: () => UNREVIEWED_TITLE, configurable: true },
  TSRC_COLOR: { get: () => TSRC_COLOR, configurable: true },
  tstate: { get: () => tstate, configurable: true },
  tcore: { get: () => tcore, configurable: true },
  tboard: { get: () => tboard, configurable: true },
  tearly: { get: () => tearly, configurable: true },
  tflex: { get: () => tflex, configurable: true },
  TFT_CLASS: { get: () => TFT_CLASS, configurable: true },
  TFT_FLEX_SORTS: { get: () => TFT_FLEX_SORTS, configurable: true },
  TFT_EARLY_SORTS: { get: () => TFT_EARLY_SORTS, configurable: true },
  TFT_EARLY_PAGE_SIZE: { get: () => TFT_EARLY_PAGE_SIZE, configurable: true },
  tftLeaderboardKey: { get: () => tftLeaderboardKey, configurable: true },
  tftFlexGoldCache: { get: () => tftFlexGoldCache, configurable: true },
  tftFlexGoldPending: { get: () => tftFlexGoldPending, configurable: true },
  tftFlexLobbyCache: { get: () => tftFlexLobbyCache, configurable: true },
  tftFlexLobbyPending: { get: () => tftFlexLobbyPending, configurable: true },
  css: { get: () => css, configurable: true },
  fmtInt: { get: () => fmtInt, configurable: true },
  ci95: { get: () => ci95, configurable: true },
  fmtCompact: { get: () => fmtCompact, configurable: true },
  fmtDur: { get: () => fmtDur, configurable: true },
});
`);

function loadDashboard(options = {}) {
  const document = options.document || createDom();
  const winEvents = {};
  const location = { hash: options.initialHash || "" };
  const historyEntries = [location.hash];
  const historyWrites = [];
  let historyIndex = 0;

  const history = {
    pushState(_state, _title, hash) {
      location.hash = String(hash);
      historyEntries.splice(historyIndex + 1);
      historyEntries.push(location.hash);
      ++historyIndex;
      historyWrites.push({ type: "push", hash: location.hash });
    },
    replaceState(_state, _title, hash) {
      location.hash = String(hash);
      historyEntries[historyIndex] = location.hash;
      historyWrites.push({ type: "replace", hash: location.hash });
    },
  };

  const emitWindow = async (type, extra = {}) => {
    const ev = { type, ...extra };
    for (const cb of [...(winEvents[type] || [])]) {
      await cb(ev);
    }
    return ev;
  };

  const window = {
    location,
    history,
    document,
    innerWidth: options.innerWidth || 1280,
    innerHeight: options.innerHeight || 900,
    addEventListener(type, fn) { (winEvents[type] ||= []).push(fn); },
    removeEventListener(type, fn) {
      if (winEvents[type]) winEvents[type] = winEvents[type].filter(cb => cb !== fn);
    },
  };

  const copied = [];
  const navigator = options.navigator !== undefined ? options.navigator : {
    clipboard: {
      writeText: async text => { copied.push(String(text)); },
    },
  };

  const timers = [];
  const clearedTimers = [];
  const useManualTimers = Boolean(options.manualTimers);
  const setTimeoutImpl = useManualTimers
    ? (fn, ms) => {
        const entry = { id: timers.length + 1, fn, ms, cleared: false };
        timers.push(entry);
        return entry.id;
      }
    : (fn, ms, ...args) => {
        const handle = setTimeout(fn, ms, ...args);
        if (Number(ms) >= 50 && typeof handle?.unref === "function") handle.unref();
        return handle;
      };
  const clearTimeoutImpl = useManualTimers
    ? id => {
        clearedTimers.push(id);
        const hit = timers.find(t => t.id === id);
        if (hit) hit.cleared = true;
      }
    : clearTimeout;

  const apiCalls = [];
  let responder = options.api || (async url => {
    throw new Error(`Unmocked api request: ${url}`);
  });

  const workerSupport = options.workers ? createWorkerClass() : null;
  const localStorage = createStorage();

  const sandbox = {
    __LOL_NO_AUTO_INIT__: options.autoInit ? false : true,
    document,
    window,
    location,
    history,
    navigator,
    localStorage,
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
    getComputedStyle: () => ({
      getPropertyValue: prop => CSS_VARS[prop] || "",
    }),
    setTimeout: setTimeoutImpl,
    clearTimeout: clearTimeoutImpl,
    AbortController,
    URLSearchParams,
    console,
    fetch: options.fetch || (async (url, opts) => ({
      ok: true,
      status: 200,
      json: async () => sandbox.api(url, opts),
    })),
    ...(workerSupport ? { Worker: workerSupport.Worker } : {}),
    ...(options.globals || {}),
  };

  const context = vm.createContext(sandbox);
  for (const script of SCRIPTS) {
    script.compiled.runInContext(context);
  }
  BRIDGE_SCRIPT.runInContext(context);

  if (!options.useFetchApi) {
    context.api = async (url, opts) => {
      apiCalls.push({ url, options: opts });
      return responder(url, opts);
    };
  }

  return {
    context,
    document,
    window,
    windowEvents: winEvents,
    location,
    history,
    historyEntries,
    historyWrites,
    get historyIndex() { return historyIndex; },
    copied,
    timers,
    clearedTimers,
    apiCalls,
    workers: workerSupport ? workerSupport.workers : [],
    installWorkers() {
      const ws = createWorkerClass();
      context.Worker = ws.Worker;
      return ws.workers;
    },
    setResponder(fn) { responder = fn; },
    element(id) { return document.getElementById(id); },
    query(sel) { return document.querySelector(sel); },
    queryAll(sel) { return document.querySelectorAll(sel); },
    emitWindow,
    setHash(hash) {
      const next = hash.startsWith("#") ? hash : "#" + hash;
      location.hash = next;
      historyEntries.splice(historyIndex + 1);
      historyEntries.push(next);
      ++historyIndex;
      return emitWindow("hashchange");
    },
    back() {
      assert(historyIndex > 0, "Cannot navigate back past initial history entry");
      --historyIndex;
      location.hash = historyEntries[historyIndex];
      emitWindow("popstate");
      return emitWindow("hashchange");
    },
    forward() {
      assert(historyIndex < historyEntries.length - 1, "Cannot navigate forward past latest history entry");
      ++historyIndex;
      location.hash = historyEntries[historyIndex];
      emitWindow("popstate");
      return emitWindow("hashchange");
    },
  };
}

async function until(condition, message = "Condition timed out", maxAttempts = 1000) {
  for (let i = 0; i < maxAttempts && !condition(); ++i) {
    await new Promise(resolve => setTimeout(resolve, 1));
  }
  assert(condition(), message);
}

module.exports = {
  STATIC_DIR,
  SCRIPT_FILES,
  INDEX_HTML,
  STYLES_CSS,
  WORKER_SOURCE,
  Element,
  TextNode,
  plain,
  readStatic,
  createDom,
  createWorkerClass,
  loadDashboard,
  until,
};
