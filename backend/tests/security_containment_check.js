// Runs security_center.js + security_containment.js against a fake page (test_security_containment.py).
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const EVIL = '<img src=x onerror=alert(1)>"\'';
const elements = {};
function element(id) {
  if (!elements[id]) {
    elements[id] = {
      id, innerHTML: '', textContent: '', value: id === 'sc-days' ? '7' : '', checked: false, disabled: false,
      options: [{}], handlers: {}, dataset: {}, style: {}, resets: 0,
      addEventListener(type, fn) { this.handlers[type] = fn; },
      add(option) { this.options.push(option); }, reset() { this.resets++; }, focus() {}, scrollIntoView() {},
      classList: { names: new Set(), add(n) { this.names.add(n); }, remove(n) { this.names.delete(n); } },
    };
  }
  return elements[id];
}

const posts = [];
const questions = [];
let answer = false;
let reason = '';
async function apiFetch(url, opts = {}) {
  if (opts.method) {
    posts.push({ url, body: opts.body ? JSON.parse(opts.body) : null });
    return { ok: true, warnings: [] };
  }
  if (url.includes('/restrictions')) {
    return { available: true, your_address: EVIL, ip_blocking_enabled: true, features: { uploads: EVIL },
             restrictions: [
               { id: 3, kind: 'ip', target: EVIL, reason: EVIL, created_at: EVIL, expires_at: '2999-01-01T00:00:00Z' },
               { id: 4, kind: 'feature', feature: 'uploads', target: EVIL, reason: EVIL, created_at: EVIL,
                 expires_at: '2000-01-01T00:00:00Z' }] };
  }
  if (url.includes('/overview')) return { events: { available: false }, monitoring: {}, catalog: [] };
  return { available: true, events: [], logs: [] };
}

const context = {
  URLSearchParams, setTimeout, console, apiFetch, Option: function () {},
  document: { getElementById: element, addEventListener() {} },
  escHtml: s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
  fmtTime: s => String(s), toast() {}, exportDataset() {},
};
context.window = context;
context.confirm = question => { questions.push(question); return answer; };
context.prompt = () => reason;
vm.createContext(context);
for (const name of ['security_center.js', 'security_containment.js']) {
  vm.runInContext(fs.readFileSync(path.join(process.argv[2], name), 'utf8'), context);
}
const settle = () => new Promise(resolve => setTimeout(resolve, 20));
const submit = async id => { element(id).handlers.submit({ preventDefault() {} }); await settle(); };
const set = values => Object.entries(values).forEach(([id, v]) => { element(id).value = v; });

(async () => {
  context.loadSecurityCenter();
  await settle();
  const table = element('sc-restrictions').innerHTML;
  assert(table.includes('&lt;img') && !table.includes('<img'), 'escaping');
  assert.strictEqual((table.match(/data-lift=/g) || []).length, 1, 'only the active restriction can be lifted');

  set({ 'sc-c-ip': '93.184.216.34', 'sc-c-ip-minutes': '60', 'sc-c-ip-reason': 'attack' });
  await submit('sc-ip-form');
  assert(posts.length === 0 && questions[0].includes('93.184.216.34'), 'sent without confirmation');
  answer = true;
  element('sc-ip-form').handlers.submit({ preventDefault() {} });
  element('sc-ip-form').handlers.submit({ preventDefault() {} });
  await settle();
  assert.strictEqual(posts.length, 1, 'double submission');
  assert.deepStrictEqual({ ...posts[0].body }, { kind: 'ip', target: '93.184.216.34', minutes: 60, reason: 'attack',
    incident_id: null, confirm: true });
  assert.strictEqual(element('sc-ip-form').resets, 1);

  set({ 'sc-c-s-business': '7 or 8', 'sc-c-s-reason': 'r' });
  const asked = questions.length;
  await submit('sc-signout-form');
  assert(posts.length === 1 && questions.length === asked, 'invalid business ID must stop before confirming');
  set({ 'sc-c-s-business': '7' });
  await submit('sc-signout-form');
  assert(posts[1].url.endsWith('/businesses/7/sign-out') && posts[1].body.confirm === true);

  set({ 'sc-c-u-business': '7', 'sc-c-u-action': 'activate', 'sc-c-u-reason': 'a&b ?' });
  await submit('sc-suspend-form');
  assert.strictEqual(posts[2].url, '/platform/businesses/7/activate?reason=a%26b+%3F');
  assert(questions[questions.length - 1].includes('pending account deletion'));

  const lift = () => element('sc-restrictions').handlers.click({ target: { closest: () => ({ dataset: { lift: '3' } }) } });
  lift();
  await settle();
  assert.strictEqual(posts.length, 3, 'lifted without a reason');
  reason = 'done';
  lift();
  await settle();
  assert(posts[3].url.endsWith('/restrictions/3/revoke') && posts[3].body.reason === 'done');

  element('sc-detail-block').dataset.ip = '93.184.216.34';
  element('sc-c-ip').value = '';
  element('sc-detail-block').handlers.click();
  assert(element('sc-c-ip').value === '93.184.216.34' && posts.length === 4, 'prefill must not block by itself');
  console.log('security containment OK');
})().catch(error => { console.error(error.message); process.exit(1); });
