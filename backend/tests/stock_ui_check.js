// Runs the REAL _stockNote / _orderStockStatus functions extracted from static/dashboard.js.
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const note = src.match(/function _stockNote\([\s\S]*?\n\}\n/)[0];
const stat = src.match(/function _orderStockStatus\([\s\S]*?\n\}\n/)[0];
const { _stockNote, _orderStockStatus } = new Function(note + stat + '; return {_stockNote, _orderStockStatus};')();
let bad = 0;
const eq = (a, b, m) => { if (a !== b) { bad++; console.error('FAIL', m, JSON.stringify(a), '!=', JSON.stringify(b)); } };

eq(_stockNote(null), '', 'no response');
eq(_stockNote({}), '', 'no stock key (non-cancel update)');
eq(_stockNote({ stock: { restored: [{ name: 'Tea', qty: 2 }], skipped: [], failed: [] } }),
   ' · stock restored (Tea ×2)', 'restored');
eq(_stockNote({ stock: { restored: [], skipped: [{ name: 'Ebook', reason: 'not_deducted' }], failed: [] } }),
   '', 'nothing deducted says nothing');
eq(_stockNote({ stock: { restored: [], skipped: [], failed: [{ name: 'Tea' }] } }).includes('could not be restored'), true, 'failure is loud');

const item = (o) => JSON.stringify([Object.assign({ name: 'Tea', qty: 2 }, o)]);
eq(_orderStockStatus({ status: 'cancelled', items: item({ stock_deducted: true, stock_restored_qty: 2 }) }),
   'Stock restored to inventory', 'restored order');
eq(_orderStockStatus({ status: 'cancelled', items: item({ stock_deducted: true }) }),
   'Stock not returned to inventory', 'cancelled but not restored');
eq(_orderStockStatus({ status: 'pending', items: item({ stock_deducted: true }) }),
   'Stock reserved for this order', 'open order');
eq(_orderStockStatus({ status: 'cancelled', items: item({ stock_deducted: false }) }), '', 'unlimited/service');
eq(_orderStockStatus({ status: 'cancelled', items: item({}) }), '', 'legacy order without record');
eq(_orderStockStatus({ status: 'cancelled', items: 'not json' }), '', 'garbage items');
eq(_orderStockStatus({ status: 'cancelled', items: [{ stock_deducted: true, stock_restored_qty: 1 }] }),
   'Stock restored to inventory', 'items already an array');
eq(_orderStockStatus(null), '', 'null order');
if (bad) process.exit(1);
console.log('stock UI OK');
