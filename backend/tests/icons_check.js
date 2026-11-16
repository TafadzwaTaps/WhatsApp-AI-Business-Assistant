// U4: every <use href="#i-x"> must have a matching <symbol id="i-x"> in the page sprite.
const fs = require('fs'), path = require('path');
const dir = path.join(__dirname, '..', '..', 'static');
const rd = f => fs.readFileSync(path.join(dir, f), 'utf8');
const syms = t => new Set([...t.matchAll(/<symbol id="(i-[a-z0-9-]+)"/g)].map(m => m[1]));
const uses = t => [...t.matchAll(/<use href="#(i-[a-z0-9-]+)"/g)].map(m => m[1]);
let fail = 0;
const dash = syms(rd('dashboard.html'));
function check(label, used, have) {
  if (!used.length) { console.error(label + ': no icons found'); fail++; }
  for (const u of used) if (!have.has(u)) { console.error(label + ': missing symbol ' + u); fail++; }
}
check('dashboard.html', uses(rd('dashboard.html')), dash);
check('inbox.html', uses(rd('inbox.html')), syms(rd('inbox.html')));
check('dashboard.js', uses(rd('dashboard.js')), dash);
check('inbox.js', uses(rd('inbox.js')), syms(rd('inbox.html')));
process.exit(fail ? 1 : 0);
