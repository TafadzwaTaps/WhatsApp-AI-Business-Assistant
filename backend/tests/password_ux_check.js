const fs=require('fs');
const src=fs.readFileSync(process.argv[2],'utf8');
function grab(re,name){const m=src.match(re);if(!m)throw new Error(name+' not found');return m[0];}
const fmtSrc=grab(/function formatApiDetail\(detail, fallback\) \{[\s\S]*?\n\}\n/,'formatApiDetail');
const valSrc=grab(/function validateNewPasswordInput\([\s\S]*?\n\}\n/,'validateNewPasswordInput');
const fmt=new Function(fmtSrc+'; return formatApiDetail;')();
const val=new Function(valSrc+'; return validateNewPasswordInput;')();
const eq=(a,b,l)=>{if(a!==b)throw new Error(l+': got '+JSON.stringify(a)+' expected '+JSON.stringify(b));};
eq(fmt('Wrong password','x'),'Wrong password','string');
eq(fmt([{msg:'Value error, too long'},{msg:'other'}],'x'),'too long; other','array');
eq(fmt({message:'nope'},'fb'),'nope','object message');
eq(fmt({a:1},'fb'),'fb','object no msg');
eq(fmt([],'fb'),'fb','empty array');
eq(fmt('', 'fb'),'fb','empty string');
eq(fmt(undefined,'fb'),'fb','undefined');
if(/object Object/.test(fmt([{x:1}],'fb')+fmt({a:1},'fb')))throw new Error('[object Object] leaked');
eq(val('Old1pass','Newpass1','Newpass1'),'','valid');
eq(val('','Newpass1','Newpass1')!=='',true,'no current');
eq(/at least 8/.test(val('a','Ab1','Ab1')),true,'short');
eq(/uppercase/.test(val('a','newpass12','newpass12')),true,'upper');
eq(/number/.test(val('a','Newpassss','Newpassss')),true,'digit');
eq(/at most 128/.test(val('a','A1'+'x'.repeat(127),'A1'+'x'.repeat(127))),true,'128');
const b72='A1'+'x'.repeat(70);   // exactly 72 bytes
eq(val('a',b72,b72),'','72 ascii ok');
const multi='A1'+'é'.repeat(36);  // 38 chars, 74 bytes
eq(/72 bytes/.test(val('a',multi,multi)),true,'multibyte >72');
eq(/do not match/.test(val('a','Newpass1','Newpass2')),true,'mismatch');
eq(/different/.test(val('Newpass1','Newpass1','Newpass1')),true,'same');
console.log('password ux OK');
