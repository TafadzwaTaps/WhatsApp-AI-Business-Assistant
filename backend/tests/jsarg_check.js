const fs=require('fs');
let bad=0;
for (const f of process.argv.slice(2)) {
  const src=fs.readFileSync(f,'utf8');
  const esc=src.match(/function escHtml\([\s\S]*?\n\}\n/)[0];
  const ja=src.match(/function jsArg\([\s\S]*?\n\}\n/)[0];
  const safeText=s=>String(s==null?'':s);   // inbox.js helper used by its escHtml
  const jsArg=new Function('safeText',esc+ja+'; return jsArg;')(safeText);
  const dec=s=>s.replace(/&quot;/g,'"').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
  const vals=["+263771234567","O'Brien","a\b","');alert(1);//","\"><img src=x onerror=alert(1)>","</script>","line\nbreak","\u2028x\u2029",""];
  for (const v of vals) {
    const attr=jsArg(v);
    if (/["<>]/.test(attr)) {bad++;console.error(f,'raw char in attribute for',JSON.stringify(v));}
    let got; const fn=new Function('capture','capture('+dec(attr)+')');
    fn(x=>got=x);
    if (got!==v){bad++;console.error(f,'round trip failed',JSON.stringify(v),JSON.stringify(got));}
  }
  if (/onclick="[A-Za-z_]+\('\$\{/.test(src)) {bad++;console.error(f,'quoted ${} arg remains in inline handler');}
}
if(bad) process.exit(1); console.log('jsArg OK');
