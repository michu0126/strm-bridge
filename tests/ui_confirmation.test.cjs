const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
class Element{
 constructor(){this.events={};this.dataset={};this.children=[];this.disabled=false;this.value='';this.textContent='';this.classes=new Set(['hidden']);this.classList={add:x=>this.classes.add(x),remove:x=>this.classes.delete(x),contains:x=>this.classes.has(x),toggle:(x,on)=>on?this.classes.add(x):this.classes.delete(x)};this.style={};}
 addEventListener(name,fn){this.events[name]=fn;}
 replaceChildren(...c){this.children=c;}
 append(...c){this.children.push(...c);}
 focus(){} scrollIntoView(){} setAttribute(){}
}
const elements=new Map();
const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const requests=[];let rejectRun=false;
const job={id:'fixture',status:'preview',counts:{strm:1,delete:2,move:1},options:{source:'/test-source',target:'/test-library'},actions:[],action_total:0};
const context=vm.createContext({document:{getElementById:get,querySelectorAll:()=>[],addEventListener(){},createElement:()=>new Element()},setTimeout:()=>1,clearTimeout(){},setInterval(){},window:{},localStorage:{getItem:()=>null},CustomEvent:class{},fetch:async(path,options)=>{
 requests.push({path,options});
 if(path==='/api/config')return {ok:false,json:async()=>({error:'test initialization skipped'})};
 if(path==='/api/run'&&rejectRun)return {ok:false,json:async()=>({error:'fixture execution failed'})};
 return {ok:true,json:async()=>path.startsWith('/api/jobs/')?job:{id:'fixture'}};
}});
for(const file of ['confirmation.js','app.js'])vm.runInContext(fs.readFileSync('static/'+file,'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 await tick();requests.length=0;
 vm.runInContext("currentJob='fixture'",context);
 let run=get('run').events.click();await tick();
 assert.equal(requests.filter(r=>r.path==='/api/run').length,0,'confirmation must wait before submitting');
 assert.equal(get('confirmationPanel').classes.has('hidden'),false);
 assert.match(get('confirmationMessage').textContent,/2 个/);
 assert.match(get('confirmationMessage').textContent,/1 份字幕/);
 await get('run').events.click();
 assert.equal(requests.filter(r=>r.path.includes('/api/jobs/')).length,1,'repeated clicks cannot duplicate execution');
 get('cancelConfirmation').events.click();await run;
 assert.equal(requests.filter(r=>r.path==='/api/run').length,0);
 assert.equal(get('run').disabled,false);
 run=get('run').events.click();await tick();get('acceptConfirmation').events.click();await run;
 assert.equal(requests.filter(r=>r.path==='/api/run').length,1,'confirm submits exactly once');
 assert.deepEqual(JSON.parse(requests.find(r=>r.path==='/api/run').options.body),{id:'fixture'});
 rejectRun=true;run=get('run').events.click();await tick();get('acceptConfirmation').events.click();await run;
 assert.equal(get('runStatus').textContent,'fixture execution failed','errors remain next to preview');
 assert.equal(get('run').disabled,false);
 requests.length=0;rejectRun=false;
 vm.runInContext("pendingWatch={job:'fixture',data:{id:'watch-fixture',cleanup:true}}",context);
 run=get('run').events.click();await tick();get('acceptConfirmation').events.click();await run;
 assert.equal(requests.filter(r=>r.path==='/api/run').length,0);
 const watch=requests.find(r=>r.path==='/api/watches'&&r.options.method==='POST');
 assert.equal(JSON.parse(watch.options.body).cleanup_preview,'fixture');
 const pending=vm.runInContext("confirmAction('stay visible')",context);
 get('confirmationPanel').events.keydown({key:'Escape',preventDefault(){}});
 assert.equal(await pending,false);
 console.log('Preview confirmation tests passed: cancel, accept, duplicate click, execution error, watch policy, Escape.');
})().catch(error=>{console.error(error);process.exitCode=1;});
