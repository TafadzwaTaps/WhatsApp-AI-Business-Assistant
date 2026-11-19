// Runs the REAL _fmtBillingDate from static/dashboard.js.
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const fn = src.match(/function _fmtBillingDate\([\s\S]*?\n\}\n/)[0];
const _fmtBillingDate = new Function(fn + '; return _fmtBillingDate;')();
let bad = 0;
const check = (c, m) => { if (!c) { bad++; console.error('FAIL', m); } };
const d = _fmtBillingDate('2027-01-15T12:00:00Z');
check(/2027/.test(d) && /15/.test(d), 'formats a real date: ' + d);
check(_fmtBillingDate('') === '', 'empty');
check(_fmtBillingDate(null) === '', 'null');
check(_fmtBillingDate(undefined) === '', 'undefined');
check(_fmtBillingDate('not a date') === '', 'invalid');
if (bad) process.exit(1);
console.log('billing UI OK');
