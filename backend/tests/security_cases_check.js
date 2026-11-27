// Runs security_center.js + security_cases.js against a fake page (used by test_security_cases.py).
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const EVIL = '<img src=x onerror=alert(1)>"\'';
const elements = {};
function element(id) {
  if (!elements[id]) {
    elements[id] = {
      id, innerHTML: '', textContent: '', value: id === 'sc-days' ? '7' : '', disabled: false, required: false,
      options: [{}], handlers: {}, dataset: {}, style: {},
      addEventListener(type, fn) { this.handlers[type] = fn; },
      add(option) { this.options.push(option); }, reset() {}, focus() {}, scrollIntoView() {},
      classList: { names: new Set(), add(n) { this.names.add(n); }, remove(n) { this.names.delete(n); } },
    };
  }
  return elements[id];
}

const sent = [];
const toasts = [];
let answer = true;
const incident = { id: 4, title: EVIL, severity: EVIL, classification: EVIL, status: 'awaiting_verification',
  assigned_to: EVIL, created_by: EVIL, detected_at: EVIL, summary: EVIL, affected_business_ids: [1],
  affected_accounts: [EVIL], data_categories: [], verification_evidence: EVIL };
const finding = { id: 9, title: EVIL, severity: EVIL, component: EVIL, status: 'open', owner: EVIL, source: EVIL,
  evidence: EVIL, detected_at: EVIL };
const event = { id: 7, name: EVIL, severity: EVIL, occurrences: 1, last_seen: EVIL, username: EVIL, incident_id: null };

async function apiFetch(url, opts = {}) {
  if (opts.method) {
    sent.push({ url, method: opts.method, body: JSON.parse(opts.body) });
    return { incident, vulnerability: finding, linked: 1, requested: 1, warnings: [] };
  }
  if (url.includes('/overview')) return { events: { available: false }, monitoring: {}, catalog: [] };
  if (url.includes('/security/events')) return { available: true, events: [event] };
  if (/\/incidents\/\d+$/.test(url)) {
    return { incident, notes: [{ created_at: EVIL, author: EVIL, kind: EVIL, body: EVIL }],
             events: [event], allowed_statuses: ['resolved', EVIL] };
  }
  if (url.includes('/incidents')) return { available: true, incidents: [incident] };
  if (url.includes('/vulnerabilities')) return { available: true, vulnerabilities: [finding] };
  return { logs: [] };
}

const context = {
  URLSearchParams, setTimeout, console, apiFetch,
  Option: function () {},
  document: { getElementById: element, addEventListener() {} },
  escHtml: s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
  fmtTime: s => String(s), toast: (message, bad) => toasts.push([message, bad]), exportDataset() {},
};
context.window = context;
context.confirm = () => answer;
context.prompt = () => '';
vm.createContext(context);
for (const name of ['security_center.js', 'security_cases.js']) {
  vm.runInContext(fs.readFileSync(path.join(process.argv[2], name), 'utf8'), context);
}
const settle = () => new Promise(resolve => setTimeout(resolve, 20));
const submit = async id => { element(id).handlers.submit({ preventDefault() {} }); await settle(); };
const click = async (id, dataset) => {
  element(id).handlers.click({ target: { closest: () => (dataset ? { dataset } : null) } });
  await settle();
};

(async () => {
  context.loadSecurityCenter();
  await settle();
  await click('sc-incidents', { incident: '4' });
  await click('sc-vulns', { vuln: '0' });
  for (const id of Object.keys(elements)) assert(!elements[id].innerHTML.includes('<img'), `unescaped data in ${id}`);
  for (const id of ['sc-incidents', 'sc-vulns', 'sc-inc-notes', 'sc-inc-events', 'sc-v-record', 'sc-inc-facts', 'sc-link-target']) {
    assert(element(id).innerHTML.includes('&lt;img'), `${id} was not drawn`);
  }

  // Finding: untouched evidence box sends no evidence, so what is on record stays.
  element('sc-v-status').value = 'open';
  element('sc-v-reason').value = 'r';
  await submit('sc-vuln-form');
  let last = sent[sent.length - 1];
  assert(last.method === 'PATCH' && last.url.endsWith('/vulnerabilities/9') && !('evidence' in last.body) && !('source' in last.body));

  // Closing an incident asks first; "no" sends nothing.
  element('sc-s-status').value = 'resolved';
  element('sc-s-status').handlers.change();
  assert(element('sc-s-evidence').required === true);
  element('sc-s-note').value = 'done';
  element('sc-s-evidence').value = 'retested';
  const before = sent.length;
  answer = false;
  await submit('sc-status-form');
  assert.strictEqual(sent.length, before, 'sent although not confirmed');
  answer = true;
  element('sc-status-form').handlers.submit({ preventDefault() {} });
  element('sc-status-form').handlers.submit({ preventDefault() {} });   // double click
  await settle();
  assert.strictEqual(sent.length, before + 1, 'double submission');
  assert.strictEqual(sent[before].body.verification_evidence, 'retested');

  // Selecting events, then a new incident from them.
  const box = { dataset: { select: '7' }, checked: true };
  element('sc-events').handlers.change({ target: { closest: () => box } });
  assert.strictEqual(element('sc-selected').textContent, '1 selected');
  await click('sc-link-new');
  element('sc-n-title').value = 't';
  element('sc-n-businesses').value = '3, x';
  const count = sent.length;
  await submit('sc-new-form');
  assert.strictEqual(sent.length, count, 'bad business IDs were sent');
  assert(toasts.some(([m, bad]) => bad && m.includes('Business IDs')));
  element('sc-n-businesses').value = '3, 5';
  await submit('sc-new-form');
  last = sent[sent.length - 1];
  assert.deepStrictEqual([...last.body.event_ids], [7]);
  assert.deepStrictEqual([...last.body.affected_business_ids], [3, 5]);
  assert.strictEqual(element('sc-selected').textContent, '0 selected');

  // Unlinking without a reason sends nothing.
  const sofar = sent.length;
  await click('sc-inc-events', { unlink: '7' });
  assert.strictEqual(sent.length, sofar);
  console.log('security cases OK');
})().catch(error => { console.error(error.message); process.exit(1); });
