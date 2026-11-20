// Runs the REAL static/csp-actions.js against a minimal fake DOM.
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const eq = (a, b, l) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(l + ': got ' + JSON.stringify(a) + ' expected ' + JSON.stringify(b)); };

function el(attrs, parent, dataset) {
  const e = { nodeType: 1, parentNode: parent || null, dataset: dataset || {}, _a: attrs || {},
    hasAttribute(n) { return n in this._a; }, getAttribute(n) { return this._a[n]; } };
  return e;
}
const listeners = {};
const document = { nodeType: 9, addEventListener(t, f) { (listeners[t] = listeners[t] || []).push(f); } };
const errors = [];
const sandbox = { document, window: {}, console: { error: (...a) => errors.push(a.join(' ')) }, Object, TypeError };
sandbox.window.console = sandbox.console;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
const wz = sandbox.window.wzActions;
const fire = (type, target) => { const ev = { target, prevented: false, preventDefault() { this.prevented = true; } }; (listeners[type] || []).forEach(f => f(ev)); return ev; };

eq(Object.keys(listeners).sort(), ['change', 'click', 'input', 'keydown', 'submit'], 'listeners registered once per event type');

const calls = [];
wz.register('save', (ev, element) => calls.push(['save', element.dataset.id]));
wz.register('boom', () => { throw new Error('x'); });
wz.register('typed', (ev, element) => calls.push(['typed', element.dataset.q]));
wz.register('send', () => calls.push(['send']));

// click on a child finds the closest ancestor carrying data-click; args come from dataset
const btn = el({ 'data-click': 'save' }, document, { id: '12' });
const icon = el({}, btn);
fire('click', icon);
eq(calls, [['save', '12']], 'delegated click uses the closest data-click ancestor');
// the right attribute for the right event
fire('input', btn); eq(calls.length, 1, 'data-click is not triggered by input events');
fire('input', el({ 'data-input': 'typed' }, document, { q: 'hi' })); eq(calls[1], ['typed', 'hi'], 'input action');
// unregistered names do nothing, and window functions are NEVER looked up by name
let called = false;
sandbox.window.evil = () => { called = true; };
sandbox.evil = sandbox.window.evil;
fire('click', el({ 'data-click': 'evil' }, document));
fire('click', el({ 'data-click': 'constructor' }, document));
fire('click', el({ 'data-click': '__proto__' }, document));
fire('click', el({ 'data-click': 'toString' }, document));
eq(called, false, 'window functions are not callable through markup');
eq(calls.length, 2, 'unregistered / prototype names are ignored');
// an action that throws is contained and logged
fire('click', el({ 'data-click': 'boom' }, document));
eq(errors.length, 1, 'errors are caught and logged'); fire('click', btn); eq(calls.length, 3, 'dispatcher keeps working after an error');
// submit prevents the browser default only for a handled form
const form = el({ 'data-submit': 'send' }, document);
eq(fire('submit', form).prevented, true, 'handled submit is prevented');
eq(fire('submit', el({ 'data-submit': 'nope' }, document)).prevented, false, 'unhandled submit is untouched');
// bad registrations are rejected
let threw = false; try { wz.register('x', 'not a function'); } catch (e) { threw = true; } eq(threw, true, 'register validates arguments');
eq(wz.has('save'), true, 'has()'); eq(wz.has('constructor'), false, 'has() ignores prototype names');
// a non-element target (text node) is handled
fire('click', { nodeType: 3, parentNode: btn }); eq(calls.length, 5, 'text node target resolves to its parent');
console.log('csp actions OK');
