const fs=require('fs');
const src=fs.readFileSync(process.argv[2],'utf8');
const m=src.match(/async function loadNeedsAttention\(\) \{[\s\S]*?\n\}\n/);
if(!m) throw new Error('function not found');
function run(orders){
  const panel={style:{}}, body={innerHTML:''};
  const ROUTES={orders:'/o',products:'/p',conversations:'/c'};
  const document={getElementById:id=>id==='needs-attention-panel'?panel:body};
  const apiFetch=async p=>p==='/o'?orders:(p==='/analytics/handoff-stats'?{active_count:0}:[]);
  const escHtml=s=>s, _isProdOos=()=>false,_isProdLowStock=()=>false;
  const f=new Function('document','apiFetch','ROUTES','escHtml','_isProdOos','_isProdLowStock', m[0]+'; return loadNeedsAttention;')(document,apiFetch,ROUTES,escHtml,_isProdOos,_isProdLowStock);
  return f().then(()=>({panel,body:body.innerHTML}));
}
(async()=>{
  // user's case: 3 confirmed-cash orders, nothing actually awaiting payment
  let r=await run([{status:'pending_cash'},{status:'pending_cash'},{status:'pending_cash'}]);
  if(/awaiting payment/.test(r.body)) throw new Error('cash orders still counted as awaiting payment');
  if(!/3 orders need fulfillment/.test(r.body)) throw new Error('cash orders should count as need fulfillment: '+r.body);
  // real awaiting + review, one already paid
  r=await run([{status:'awaiting_payment',payment_status:'pending'},{status:'payment_review'},{status:'awaiting_payment',payment_status:'paid'}]);
  if(!/2 orders awaiting payment/.test(r.body)) throw new Error('expected 2: '+r.body);
  if(!/attentionOpenOrders\('awaiting_payment'\)/.test(r.body)) throw new Error('filter not wired');
  // nothing
  r=await run([{status:'delivered'}]);
  if(r.panel.style.display!=='none') throw new Error('panel should hide');
  console.log('needs-attention logic OK');
})().catch(e=>{console.error(e.message);process.exit(1)});
