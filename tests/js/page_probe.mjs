/* Loads a rendered dashboard in a real DOM, drives it, and reports what
   happened as JSON on stdout.

   This exists because 659 string assertions were green on a page whose tabs
   did nothing and whose ticker script died on its first line with a
   ReferenceError. Asserting on generated HTML tells you what the page *says*;
   nothing in the suite ran what the page *does*. So: jsdom, the actual file
   the user opens, real click events, and assertions about what changed.

   The driver only reports. Every judgement lives in test_dashboard_js.py --
   this file must not decide what counts as passing, or the check and the
   thing it checks drift apart in the usual way.

   Usage: node page_probe.mjs <html-file> <url>
*/
import { readFileSync } from 'node:fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [htmlPath, url] = process.argv.slice(2);
const html = readFileSync(htmlPath, 'utf8');

const errors = [];
const notImplemented = [];

const virtualConsole = new VirtualConsole();
/* An uncaught exception in an inline script arrives here. So does anything
   jsdom refuses to do -- scrollTo, scrollIntoView, layout -- which is not an
   error in the page and is reported separately rather than filtered away,
   because a silently dropped category is how this went wrong last time. */
virtualConsole.on('jsdomError', (err) => {
  const message = String((err && err.message) || err);
  if (/^Not implemented/.test(message)) {
    notImplemented.push(message);
    return;
  }
  errors.push({ where: 'uncaught', message, stack: String((err && err.stack) || '') });
});
/* The panel runtime catches a throwing binder and logs it. That is the right
   behaviour in a browser -- one broken panel must not blank the other
   thirteen -- and the wrong thing to let pass in a test. */
virtualConsole.on('error', (...args) => {
  errors.push({ where: 'console.error', message: args.map(String).join(' ') });
});

const dom = new JSDOM(html, { url, runScripts: 'dangerously', virtualConsole });
const { window } = dom;
const doc = window.document;

const $ = (sel, root = doc) => root.querySelector(sel);
const $$ = (sel, root = doc) => Array.from(root.querySelectorAll(sel));
const tick = () => new Promise((r) => window.setTimeout(r, 0));

function click(el) {
  if (!el) { return false; }
  el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
  return true;
}

function openPanel() {
  const a = $('#mr-main article.panel');
  return a ? a.getAttribute('data-panel') : null;
}

function navCurrent() {
  const a = $('#mr-nav a[aria-current]');
  return a ? a.getAttribute('data-panel') : null;
}

function visibleLists() {
  return $$('#mr-main div.list')
    .filter((d) => !d.hidden)
    .map((d) => {
      const t = $('.ltitle', d);
      return t ? t.textContent.trim() : '(untitled)';
    });
}

function tabs(axis) {
  return $$(`#mr-main .tab[data-axis="${axis}"]`).map((b) => {
    const c = $('.count', b);
    return {
      value: b.getAttribute('data-v'),
      current: b.hasAttribute('aria-current'),
      count: c ? c.textContent.trim() : null,
    };
  });
}

function currentTab(axis) {
  const t = tabs(axis).find((x) => x.current);
  return t ? t.value : null;
}

function stored() {
  try { return window.localStorage.getItem('mr.panel'); } catch (e) { return '<threw>'; }
}

function snapshot(name, extra = {}) {
  return {
    name,
    open: openPanel(),
    navCurrent: navCurrent(),
    panelsInDom: $$('#mr-main article.panel').length,
    stored: stored(),
    ...extra,
  };
}

const steps = [];
const renderedPanelCount = (html.match(/<article class="panel"/g) || []).length;

/* --- what the runtime made of the page on load ------------------------ */
steps.push(snapshot('load', {
  binders: (window.__MR_BINDERS__ || []).length,
  panelIds: window.__MR_PANEL_IDS__ || null,
  navLinks: $$('#mr-nav a[data-panel]').length,
  hasShowPanel: typeof window.__MR_SHOW_PANEL__ === 'function',
  hasOpenTicker: typeof window.__MR_OPEN_TICKER__ === 'function',
  hasBack: typeof window.__MR_BACK__ === 'function',
  hasTkDraw: typeof window.__MR_TK_DRAW__ === 'function',
}));

/* --- the screens panel opens on a combination that has lists in it ---- */
click($('#mr-nav a[data-panel="screens"]'));
const openSec = currentTab('sec');
const openBand = currentTab('band');
steps.push(snapshot('nav:screens', {
  visibleLists: visibleLists(),
  secTabs: tabs('sec'),
  bandTabs: tabs('band'),
  listsInDom: $$('#mr-main div.list').length,
  hiddenListsInDom: $$('#mr-main div.list').filter((d) => d.hidden).length,
}));

/* --- pressing a tab changes which lists are on screen ----------------- */
for (const axis of ['band', 'sec']) {
  const other = tabs(axis).find((t) => !t.current);
  if (!other) { continue; }
  click($(`#mr-main .tab[data-axis="${axis}"][data-v="${other.value}"]`));
  steps.push(snapshot(`tab:${axis}=${other.value}`, {
    visibleLists: visibleLists(),
    tabs: tabs(axis),
    otherAxisUnchanged: axis === 'band' ? currentTab('sec') : currentTab('band'),
  }));
}

/* Back to where the panel opened, so the rows below are the visible ones. */
click($(`#mr-main .tab[data-axis="sec"][data-v="${openSec}"]`));
click($(`#mr-main .tab[data-axis="band"][data-v="${openBand}"]`));

/* --- the gate swaps the ungated list for its gated twin, in place ----- */
const gate = $('#mr-main #gate');
if (gate) {
  for (const on of [true, false]) {
    gate.checked = on;
    gate.dispatchEvent(new window.Event('change', { bubbles: true }));
    steps.push(snapshot(`gate:${on ? 'on' : 'off'}`, { visibleLists: visibleLists() }));
  }
}

/* --- a ticker row routes through the shell into the ticker panel ------ */
const known = new Set(Object.keys(window.__TK__ || {}));
const rowsHere = () => $$('#mr-main div.list:not([hidden]) tr[data-ticker]');

/* The payload covers a subset of the symbols on screen, and which tab opens
   is a policy decision that is allowed to change. So rather than assuming the
   row is on the tab the panel opened on, walk the combinations until one has
   a row the chart has history for. Reported, so a fixture that drifts until
   no tab has one fails loudly instead of skipping the ticker steps. */
let hit = rowsHere().find((r) => known.has(r.getAttribute('data-ticker')));
let searched = 0;
if (!hit) {
  outer:
  for (const s of tabs('sec')) {
    click($(`#mr-main .tab[data-axis="sec"][data-v="${s.value}"]`));
    for (const b of tabs('band')) {
      click($(`#mr-main .tab[data-axis="band"][data-v="${b.value}"]`));
      searched++;
      hit = rowsHere().find((r) => known.has(r.getAttribute('data-ticker')));
      if (hit) { break outer; }
    }
  }
}
const miss = rowsHere().find((r) => !known.has(r.getAttribute('data-ticker')));

/* The negative first: a row the payload has no history for must do nothing
   at all, rather than switching to an empty chart. */
if (miss) {
  click(miss);
  steps.push(snapshot('click:row-without-history', {
    clickedSymbol: miss.getAttribute('data-ticker'),
  }));
}

steps.push(snapshot('find:row-with-history', {
  found: hit ? hit.getAttribute('data-ticker') : null,
  tabsSearched: searched,
  onOpeningTab: searched === 0,
}));

if (hit) {
  click(hit);
  steps.push(snapshot('click:ticker-row', {
    clickedSymbol: hit.getAttribute('data-ticker'),
    chartNodes: $('#tk-chart') ? $('#tk-chart').childNodes.length : null,
    drawerHidden: $('#tk') ? $('#tk').hidden : null,
    drawerSymbol: $('#tk-sym') ? $('#tk-sym').textContent.trim() : null,
    barRows: $$('#tk-bars tbody tr').length,
  }));

  /* --- and close comes back to the panel it was opened from ---------- */
  click($('#tk-x'));
  steps.push(snapshot('click:close'));
}

/* --- every other panel's binder must find nothing and not throw ------- */
for (const id of window.__MR_PANEL_IDS__ || []) {
  if (!click($(`#mr-nav a[data-panel="${id}"]`))) { continue; }
  steps.push(snapshot(`nav:${id}`));
}

/* --- a hash names the panel. Async in jsdom, as in a browser. --------- */
window.location.hash = '#panel-outcomes';
await tick();
steps.push(snapshot('hash:outcomes'));

process.stdout.write(JSON.stringify({
  url,
  renderedPanelCount,
  errors,
  notImplemented,
  steps,
}, null, 1));
