const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function grab(name) {
  const m = src.match(new RegExp('function ' + name + String.raw`\([\s\S]*?
\}
`));
  if (!m) throw new Error(name + ' not found');
  return m[0];
}
const code = ['classifyConversation', 'filterConversations', 'countConversations'].map(grab).join('\n');
const { classifyConversation: cl, filterConversations: fl, countConversations: ct } =
  new Function(code + '; return {classifyConversation,filterConversations,countConversations};')();
const eq = (a, b, m) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(m + ': ' + JSON.stringify(a)); };
const L = [
  { customer_id: 1, in_handoff: true },
  { customer_id: 2, handoff_state: 'human_handoff' },
  { customer_id: 3, unread_count: 2, last_direction: 'incoming' },
  { customer_id: 4, unread_count: 2 },
  { customer_id: 5, unread_count: 3, last_direction: 'outgoing' },
  { customer_id: 6, unread_count: 0, last_direction: 'incoming' },
  { customer_id: 7 },
];
eq(cl(L[0]), 'human', 'in_handoff'); eq(cl(L[1]), 'human', 'state');
eq(cl(L[2]), 'human', 'unread incoming'); eq(cl(L[3]), 'human', 'unread no dir');
eq(cl(L[4]), 'ai', 'ai replied last'); eq(cl(L[5]), 'ai', 'read'); eq(cl(L[6]), 'ai', 'bare');
eq(cl(null), 'ai', 'null');
eq(fl(L, 'human').map(c => c.customer_id), [1, 2, 3, 4], 'human filter');
eq(fl(L, 'ai').map(c => c.customer_id), [5, 6, 7], 'ai filter');
eq(fl(L, 'all').length, 7, 'all'); eq(fl(L, 'bogus').length, 7, 'bogus'); eq(fl(undefined, 'ai'), [], 'undef');
eq(ct(L), { all: 7, human: 4, ai: 3 }, 'counts'); eq(ct(null), { all: 0, human: 0, ai: 0 }, 'counts empty');
console.log('inbox filters OK');
