const fs=require('fs');
const src=fs.readFileSync(process.argv[2],'utf8');
const a=src.indexOf('// ── U2b accessibility helpers'), b=src.indexOf('// ── U2b end ──');
if(a<0||b<0) throw new Error('helper block not found');
const block=src.slice(a,b);
const listeners=[];
function mkEl(o){ return Object.assign({clicks:0,focused:0,tagName:'DIV',attrs:{},
  matches(sel){return sel.split(',').some(s=>{const m=s.match(/\[(role|data-kbd-click)(?:="(.*)")?\]/);return m&&(m[1]==='role'?this.role===m[2]:this.kbd);});},
  click(){this.clicks++;}, focus(){this.focused++;document.activeElement=this;},
  contains(x){return x===this||(this.kids||[]).includes(x);},
  getClientRects(){return [1];}, querySelectorAll(){return this.kids||[];},
  hasAttribute(k){return k in this.attrs;}, getAttribute(k){return this.attrs[k];}, setAttribute(k,v){this.attrs[k]=v;}},o);}
const document={activeElement:null,body:{},addEventListener:(t,f)=>listeners.push(f),contains:()=>true};
const window={};
new Function('document','window',block+';window.api={_a11yKbdClick,_a11yTrapTab,_a11yDialogOpened,_a11yDialogClosed};')(document,window);
const api=window.api, ev=(key,t,x)=>Object.assign({key,target:t,prevented:false,preventDefault(){this.prevented=true;}},x);
const assert=(c,m)=>{if(!c) throw new Error(m);};
// keyboard
let d=mkEl({role:'button'});
let e=ev('Enter',d); assert(api._a11yKbdClick(e)&&d.clicks===1&&e.prevented,'Enter must click role=button');
e=ev(' ',d); api._a11yKbdClick(e); assert(d.clicks===2&&e.prevented,'Space must click and preventDefault');
e=ev('a',d); assert(!api._a11yKbdClick(e)&&d.clicks===2,'other keys ignored');
e=ev('Tab',d); assert(!api._a11yKbdClick(e),'Tab ignored');
let btn=mkEl({role:'button',tagName:'BUTTON'}); e=ev('Enter',btn); assert(!api._a11yKbdClick(e)&&btn.clicks===0&&!e.prevented,'native button untouched');
let row=mkEl({kbd:true,tagName:'TR'}); api._a11yKbdClick(ev('Enter',row)); assert(row.clicks===1,'data-kbd-click works');
let plain=mkEl({}); assert(!api._a11yKbdClick(ev('Enter',plain))&&plain.clicks===0,'plain div untouched');
assert(!api._a11yKbdClick(ev('Enter',d,{ctrlKey:true})),'modified keys ignored');
// focus trap
const f1=mkEl({tagName:'BUTTON'}), f2=mkEl({tagName:'BUTTON'});
const dlg=mkEl({kids:[f1,f2]});
document.activeElement=f2; e=ev('Tab',null); assert(api._a11yTrapTab(e,dlg)&&f1.focused===1&&e.prevented,'Tab on last wraps to first');
document.activeElement=f1; e=ev('Tab',null,{shiftKey:true}); assert(api._a11yTrapTab(e,dlg)&&f2.focused===1,'Shift+Tab on first wraps to last');
document.activeElement=f1; e=ev('Tab',null); assert(!api._a11yTrapTab(e,dlg)&&!e.prevented,'middle Tab untouched');
// focus restore
const opener=mkEl({tagName:'BUTTON'}); document.activeElement=opener;
api._a11yDialogOpened(dlg); assert(f1.focused===2,'focus moves into dialog');
api._a11yDialogClosed(dlg); assert(opener.focused===1,'focus restored to opener');
console.log('a11y markup/keyboard helpers OK');
