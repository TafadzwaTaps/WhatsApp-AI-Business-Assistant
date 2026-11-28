// Runs static/security_center.js against a fake page and hostile data (used by test_security_center.py).
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const EVIL = '<img src=x onerror=alert(1)>"\'';
const elements = {};
function element(id) {
  if (!elements[id]) {
    elements[id] = {
      id, innerHTML: '', textContent: '', value: id === 'sc-days' ? '7' : '', disabled: false, options: [{}],
      handlers: {}, dataset: {}, style: {},
      addEventListener(type, fn) { this.handlers[type] = fn; },
      add(option) { this.options.push(option); }, reset() {}, focus() {}, scrollIntoView() {},
      classList: { names: new Set(), add(n) { this.names.add(n); }, remove(n) { this.names.delete(n); } },
    };
  }
  return elements[id];
}

const calls = [];
let mode = 'ok';
const event = {
  id: 1, name: EVIL, event_type: EVIL, severity: EVIL, outcome: EVIL, first_seen: EVIL, last_seen: EVIL,
  occurrences: EVIL, ip: EVIL, username: EVIL, business_id: EVIL, endpoint: EVIL, rule: EVIL, user_agent: EVIL,
  incident_id: EVIL, why: EVIL, action_taken: EVIL, follow_up: EVIL, details: { a: EVIL },
};
async function apiFetch(path) {
  calls.push(path);
  if (path.includes('/overview')) {
    if (mode === 'down') {
      return { events: { available: false }, open_incidents: null, active_ip_restrictions: null,
               open_vulnerabilities: null, accounts_now: null, monitoring: {}, catalog: [] };
    }
    return { events: { available: true, counts: { failed_logins: 3 }, by_severity: { high: 1 }, blocked: 4,
                       to_review: [{ event_type: EVIL, name: EVIL, severity: EVIL, occurrences: EVIL }],
                       last_event_at: EVIL },
             open_incidents: { critical: 0, high: 0 }, active_ip_restrictions: 0, open_vulnerabilities: 0,
             accounts_now: { locked_accounts: 0, flagged_ips: 0, open_risk_flags: 0 },
             monitoring: { writer_running: true, events_waiting: 0, retention_days: 180 },
             catalog: [{ event_type: 'login_failed', name: 'Failed sign-in' }] };
  }
  if (path.includes('/security/events')) return { available: true, has_more: true, events: [event] };
  return { has_more: false, logs: [{ created_at: EVIL, actor_username: EVIL, action: EVIL, outcome: EVIL,
    target_type: EVIL, target_id: EVIL, business_id: EVIL, reason: EVIL, ip_address: EVIL }] };
}

const context = {
  window: {}, URLSearchParams, setTimeout, console, apiFetch,
  Option: function (name, value) { this.name = name; this.value = value; },
  document: { getElementById: element, addEventListener() {} },
  escHtml: s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
  fmtTime: s => String(s), toast() {}, exportDataset() {},
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const settle = () => new Promise(resolve => setTimeout(resolve, 20));

(async () => {
  context.window.loadSecurityCenter();
  await settle();
  element('sc-events').handlers.click({ target: { closest: () => ({ dataset: { event: '0' } }) } });
  for (const id of Object.keys(elements)) {
    assert(!elements[id].innerHTML.includes('<img'), `unescaped data in ${id}`);
  }
  assert(element('sc-events').innerHTML.includes('&lt;img'), 'events were not drawn');
  assert(element('sc-audit').innerHTML.includes('&lt;img'), 'audit rows were not drawn');
  assert(element('sc-detail-modal').classList.names.has('open'), 'detail did not open');
  assert.strictEqual(element('sc-events-next').disabled, false);
  assert.strictEqual(element('sc-events-prev').disabled, true);

  element('sc-events-next').handlers.click();
  await settle();
  assert(calls.some(p => p.includes('page=2')), 'next page not requested');

  element('sc-f-severity').value = 'high';
  element('sc-event-filters').handlers.submit({ preventDefault() {} });
  await settle();
  assert(calls[calls.length - 1].includes('severity=high') && calls[calls.length - 1].includes('page=1'));

  mode = 'down';
  element('sc-refresh').handlers.click();
  await settle();
  assert(element('sc-headline').textContent.includes('could not be read'));
  const values = [...element('sc-cards').innerHTML.matchAll(/stat-value">([^<]*)</g)].map(m => m[1]);
  assert(values.length === 12 && values.every(v => v === 'n/a'), 'unreadable figures must not show as numbers: ' + values);
  console.log('security center OK');
})().catch(error => { console.error(error.message); process.exit(1); });
