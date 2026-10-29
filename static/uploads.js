'use strict';
function uploadBytes(n){if(!Number.isFinite(Number(n)))return '0 B';const units=['B','KB','MB','GB','TB'];let i=0;while(n>=1024&&i<4){n/=1024;i++;}return n.toFixed(i?1:0)+' '+units[i];}
const uploadStates={waiting:'等待检查',running:'正在检查',idle:'暂无新文件',completed:'上传完成',partial:'有文件需处理',failed:'检查失败',paused:'已暂停',interrupted:'等待续传'};
function uploadSummary(r={},manual=false){return `上传 ${r.uploaded||0} 个 · 已存在 ${r.skipped||0} 个 · 同名冲突 ${r.conflicts||0} 个 · ${manual?'待上传':'等待稳定'} ${r.waiting||0} 个${r.deleted?' · 已删除本地 '+r.deleted+' 个':''}`;}
window.loadUploadWatches=async function(){
  const rows=(await api('/api/uploads')).filter(w=>w.mode!=='manual');$('uploadWatchList').replaceChildren();
  if(!rows.length)$('uploadWatchList').append(textElement('div','还没有上传监控。选择本地目录和 115 目标目录即可添加。','card'));
  for(const w of rows){
    const card=textElement('article','','card history-card');
    const head=textElement('div','','history-head');head.append(textElement('b',w.name),textElement('span',w.enabled?(uploadStates[w.last_status]||w.last_status):'已暂停','history-status'));card.append(head);
    card.append(textElement('p',w.source+' → '+w.target_label));
    card.append(textElement('small',`每 ${w.interval/60} 分钟检查 · 稳定 ${w.stable_seconds} 秒后上传 · 累计上传 ${w.uploaded_total||0} 个 · ${w.delete_local?'上传后删除本地':'保留本地文件'}`));
    if(w.last_status==='running'&&w.enabled){
      card.append(textElement('p',(w.stage||'检查文件')+(w.current?' · '+w.current:'')));
      if(w.file_size!==undefined)card.append(textElement('small',uploadBytes(w.file_done||0)+' / '+uploadBytes(w.file_size)));
    }
    if(w.last_result)card.append(textElement('p',uploadSummary(w.last_result)));
    if(w.last_error)card.append(textElement('div',w.last_error,'errorbox'));
    if(w.last_result?.errors?.length){const details=document.createElement('details');details.append(textElement('summary','查看失败文件'));for(const e of w.last_result.errors)details.append(textElement('p',e.path+'：'+e.message));card.append(details);}
    const buttons=textElement('div','','history-buttons');
    for(const [label,path,data,disabled] of [
      [w.enabled?'暂停':'启用','/api/uploads',{id:w.id,enabled:!w.enabled},false],
      ['立即检查','/api/uploads/check',{id:w.id},!w.enabled||w.last_status==='running'],
      ['重试失败文件','/api/uploads/retry',{id:w.id},!w.enabled||w.last_status==='running'],
      ['移除监控','/api/uploads/delete',{id:w.id},false]]){
      const b=textElement('button',label);b.disabled=disabled;b.addEventListener('click',safe(async()=>{await api(path,data);await window.loadUploadWatches();}));buttons.append(b);
    }
    card.append(buttons);$('uploadWatchList').append(card);
  }
};
$('browseUploadSource').addEventListener('click',safe(()=>openFolder('upload_source',$('uploadSource'))));
$('browseUploadTarget').addEventListener('click',safe(()=>openFolder('upload_target',$('uploadTarget'))));
$('uploadWatchForm').addEventListener('submit',safe(async e=>{
  e.preventDefault();const target=$('uploadTarget').dataset.cloud;if(!target)throw Error('请浏览选择 115 目标目录');
  const submit=e.currentTarget.querySelector('button[type=submit]');submit.disabled=true;$('uploadFormStatus').textContent='正在记录本地目录…';
  try{await api('/api/uploads',{name:$('uploadWatchName').value.trim(),source:$('uploadSource').value.trim(),target,interval:Number($('uploadInterval').value)*60,stable_seconds:Number($('uploadStable').value),include_existing:$('uploadExisting').checked,delete_local:$('uploadDeleteLocal').checked});$('uploadFormStatus').textContent='已添加上传监控';await window.loadUploadWatches();toast('上传监控已启用');}
  finally{submit.disabled=false;}
}));
$('refreshUploads').addEventListener('click',safe(()=>window.loadUploadWatches()));
setInterval(()=>{if(!$('uploads').classList.contains('hidden')&&!$('uploadAutomatic').classList.contains('hidden'))window.loadUploadWatches().catch(()=>{});},5000);
window.renderUploadHistory=function(j){
  const card=textElement('article','','card history-card');const head=textElement('div','','history-head');head.append(textElement('b',(j.upload_mode==='manual'?'手动上传到 115 · ':'上传监控 · ')+new Date(j.created*1000).toLocaleString('zh-CN')),textElement('span',j.status==='running'?'上传中':statuses[j.status]||j.status,'history-status'));card.append(head,textElement('p',j.options.source+' → '+j.options.target),textElement('small',uploadSummary(j.result,j.upload_mode==='manual')));
  if(j.error)card.append(textElement('div',j.error,'errorbox'));for(const e of j.result?.errors||[])card.append(textElement('p',e.path+'：'+e.message));$('historyList').append(card);
};

let manualSources=[], manualFolder={path:'',entries:[],offset:0,total:0}, manualLastFolder='';
let computerFiles=[], browserTransfer=null, sendingComputer=false;
for(const radio of document.querySelectorAll('input[name=manualOrigin]'))radio.addEventListener('change',()=>{
  const computer=document.querySelector('input[name=manualOrigin]:checked').value==='browser';
  show('manualComputerFields',computer);show('manualNasFields',!computer);show('manualDeleteLocalLabel',!computer);
});
function computerSummary(){
  const size=computerFiles.reduce((n,f)=>n+f.size,0);
  $('computerFilesSummary').textContent=computerFiles.length?`${computerFiles.length} 个文件 · ${uploadBytes(size)} · ${computerFiles.slice(0,3).map(f=>f.webkitRelativePath||f.name).join('、')}${computerFiles.length>3?'…':''}`:'尚未选择电脑文件';
}
function addComputerFiles(files){
  const map=new Map(computerFiles.map(f=>[f.webkitRelativePath||f.name,f]));
  for(const f of files){const key=f.webkitRelativePath||f.name;if(map.has(key)&&(map.get(key).size!==f.size||map.get(key).lastModified!==f.lastModified))throw Error('存在同名的不同文件，请分批上传：'+key);map.set(key,f);}
  if(map.size>1000)throw Error('电脑上传每批最多 1000 个文件，请分批选择');
  computerFiles=[...map.values()];browserTransfer=null;computerSummary();
}
$('chooseComputerFiles').addEventListener('click',()=>$('computerUploadFiles').click());
$('chooseComputerFolder').addEventListener('click',()=>$('computerUploadFolder').click());
for(const id of ['computerUploadFiles','computerUploadFolder'])$(id).addEventListener('change',safe(e=>{addComputerFiles([...e.target.files]);e.target.value='';}));
$('clearComputerFiles').addEventListener('click',()=>{computerFiles=[];browserTransfer=null;computerSummary();});
async function sendComputerFiles(target){
  if(sendingComputer)throw Error('电脑文件正在传输');
  if(!computerFiles.length)throw Error('请选择电脑文件或文件夹');
  sendingComputer=true;
  const controls=['chooseComputerFiles','chooseComputerFolder','clearComputerFiles'];
  for(const id of controls)$(id).disabled=true;
  try{
    const files=computerFiles.map(f=>({name:f.webkitRelativePath||f.name,size:f.size}));
    let task;
    if(browserTransfer&&browserTransfer.target===target)task=await api('/api/uploads/staging/'+browserTransfer.id);
    else{task=await api('/api/uploads/staging',{target,files});browserTransfer={id:task.id,target};}
    if(task.last_status!=='receiving')throw Error('这些电脑文件已经进入后台上传队列');
    for(let i=0;i<computerFiles.length;i++){
      const file=computerFiles[i];let offset=task.browser_files[i].received;
      while(offset<file.size){
        const end=Math.min(file.size,offset+8*1024*1024);
        const response=await fetch('/api/uploads/staging/'+task.id+'/chunk',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/octet-stream','X-Upload-Index':String(i),'X-Upload-Offset':String(offset)},body:file.slice(offset,end)});
        const data=await response.json();if(!response.ok)throw Error(data.error||'电脑文件传输失败');offset=data.received;
        $('manualUploadStatus').textContent=`接收电脑文件 ${i+1} / ${computerFiles.length} · ${uploadBytes(data.received_bytes)} / ${uploadBytes(task.total_bytes)}`;
      }
    }
    await api('/api/uploads/staging/'+task.id+'/finish',{});browserTransfer=null;
    $('manualUploadStatus').textContent='电脑文件已传输完整，后台正在上传至 115';
  }catch(e){$('manualUploadStatus').textContent=e.message+'；可点击开始上传继续传输。';throw e;}
  finally{sendingComputer=false;for(const id of controls)$(id).disabled=false;await window.loadManualUploads();}
}
const manualPicked=new Set();
function manualSelectionCount(){
  $('manualSelectionCount').textContent=`已选 ${manualPicked.size} 项`;
  $('chooseManualSources').disabled=!manualPicked.size;
}
async function browseManual(path='',offset=0){
  $('manualSourceMessage').textContent='正在读取目录…';
  try{
    manualFolder=await api('/api/browse?kind=upload_files&path='+encodeURIComponent(path)+'&offset='+offset);
    if(!manualFolder.entries)manualFolder.entries=(manualFolder.directories||[]).map(d=>({...d,folder:true,size:0}));
    $('manualSourcePath').value=manualFolder.path;
    $('manualSourceFilter').value='';
    $('chooseManualDirectory').disabled=!manualFolder.path;
    $('manualSourcePrev').disabled=!(manualFolder.offset>0);
    $('manualSourceNext').disabled=!manualFolder.more;
    $('manualSourcePage').textContent=manualFolder.path?`${manualFolder.total?(manualFolder.offset||0)+1:0}—${Math.min((manualFolder.offset||0)+200,manualFolder.total||0)} / ${manualFolder.total||0}`:'挂载目录';
    $('manualSourceMessage').textContent=manualFolder.path||'选择一个已挂载目录';
    renderManualFiles();
  }catch(e){$('manualSourceMessage').textContent=e.message;throw e;}
}
function renderManualFiles(){
  const filter=$('manualSourceFilter').value.toLocaleLowerCase();
  const list=$('manualSourceList');list.replaceChildren();
  for(const entry of manualFolder.entries.filter(e=>e.name.toLocaleLowerCase().includes(filter))){
    const row=textElement('div','','manual-source-row');
    const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=manualPicked.has(entry.path);
    checkbox.setAttribute('aria-label','选中 '+entry.name);
    checkbox.addEventListener('change',()=>{if(checkbox.checked)manualPicked.add(entry.path);else manualPicked.delete(entry.path);manualSelectionCount();});
    row.append(checkbox);
    if(entry.folder){
      const b=textElement('button','▱ '+entry.name,'manual-entry-name');b.type='button';
      b.addEventListener('click',safe(()=>browseManual(entry.path)));row.append(b,textElement('small','文件夹'));
    }else{
      const label=textElement('label');const name=textElement('span',entry.name,'manual-entry-name');
      label.append(name);label.addEventListener('click',()=>checkbox.click());
      row.append(label,textElement('small',uploadBytes(entry.size)));
    }
    list.append(row);
  }
  if(!list.children.length)list.append(textElement('p','此页没有匹配的文件。'));
  manualSelectionCount();
}
function useManualSources(paths){
  if(!paths.length)return;
  manualSources=paths;
  $('manualUploadSource').value=paths.length===1?paths[0]:`已选择 ${paths.length} 个文件或文件夹`;
  manualLastFolder=manualFolder.path;
  $('manualSourceDialog').close();
}
$('browseManualSource').addEventListener('click',safe(async()=>{
  manualPicked.clear();for(const path of manualSources)manualPicked.add(path);
  $('manualSourceDialog').showModal();manualSelectionCount();await browseManual(manualLastFolder);
}));
$('closeManualSource').addEventListener('click',()=>$('manualSourceDialog').close());
$('manualSourceUp').addEventListener('click',safe(()=>browseManual(manualFolder.parent||'')));
$('manualSourceGo').addEventListener('click',safe(()=>browseManual($('manualSourcePath').value.trim())));
$('manualSourcePath').addEventListener('keydown',safe(async e=>{if(e.key==='Enter'){e.preventDefault();await browseManual(e.currentTarget.value.trim());}}));
$('manualSourceFilter').addEventListener('input',renderManualFiles);
$('manualSelectPage').addEventListener('click',()=>{const filter=$('manualSourceFilter').value.toLocaleLowerCase();for(const e of manualFolder.entries)if(e.name.toLocaleLowerCase().includes(filter))manualPicked.add(e.path);renderManualFiles();});
$('manualClearSelection').addEventListener('click',()=>{manualPicked.clear();renderManualFiles();});
$('manualSourcePrev').addEventListener('click',safe(()=>browseManual(manualFolder.path,Math.max(0,manualFolder.offset-200))));
$('manualSourceNext').addEventListener('click',safe(()=>browseManual(manualFolder.path,manualFolder.offset+200)));
$('chooseManualDirectory').addEventListener('click',()=>useManualSources(manualFolder.path?[manualFolder.path]:[]));
$('chooseManualSources').addEventListener('click',()=>useManualSources([...manualPicked]));
$('browseManualTarget').addEventListener('click',safe(()=>openFolder('upload_target',$('manualUploadTarget'))));
$('manualUploadForm').addEventListener('submit',safe(async e=>{
  e.preventDefault();const fromComputer=document.querySelector('input[name=manualOrigin]:checked').value==='browser';if(!fromComputer&&!manualSources.length)throw Error('请选择本地文件或文件夹');
  const target=$('manualUploadTarget').dataset.cloud;if(!target)throw Error('请选择 115 目标目录');
  const button=e.currentTarget.querySelector('button[type=submit]');button.disabled=true;
  try{
    if(fromComputer)await sendComputerFiles(target);else await api('/api/uploads/manual',{sources:manualSources,target,delete_local:$('manualDeleteLocal').checked});
    $('manualUploadStatus').textContent='任务已加入上传队列';toast('已创建手动上传任务');await window.loadManualUploads();
  }finally{button.disabled=false;}
}));
window.loadManualUploads=async function(){
  const tasks=await api('/api/uploads/manual');const list=$('manualUploadList');list.replaceChildren();
  if(!tasks.length)list.append(textElement('div','选择文件和目标目录，开始手动上传。','card'));
  for(const w of tasks){
    const card=textElement('article','','card history-card');const head=textElement('div','','history-head');
    const state=w.last_status==='receiving'?'接收电脑文件':w.enabled?({queued:'排队等待',running:'上传中',interrupted:'等待续传'}[w.last_status]||uploadStates[w.last_status]||w.last_status):
      (['completed','partial','failed'].includes(w.last_status)?uploadStates[w.last_status]:'已暂停');
    head.append(textElement('b',w.name),textElement('span',state,'history-status'));card.append(head);
    card.append(textElement('p',(w.origin==='browser'?'电脑文件':w.source)+' → '+w.target_label));
    if(['running','receiving'].includes(w.last_status)){
      card.append(textElement('p',(w.stage||'准备上传清单')+(w.current?' · '+w.current:'')));
      card.append(textElement('small',w.last_status==='receiving'?`正在接收 · ${uploadBytes(w.received_bytes||0)} / ${uploadBytes(w.total_bytes||0)}`:`已处理 ${w.completed_count||0} / ${w.file_count||0} 个文件`));
      if(w.file_size!==undefined){
        const progress=textElement('div','','manual-upload-progress');progress.append(textElement('small',uploadBytes(w.file_done||0)+' / '+uploadBytes(w.file_size)));
        const track=textElement('div','','progress-track');const bar=document.createElement('i');bar.style.width=(w.file_size?Math.min(100,(w.file_done||0)/w.file_size*100):0)+'%';track.append(bar);progress.append(track);card.append(progress);
      }
    }
    if(w.last_result)card.append(textElement('p',uploadSummary(w.last_result,true)));
    if(w.last_error)card.append(textElement('div',w.last_error,'errorbox'));
    if(w.last_result?.errors?.length){const details=document.createElement('details');details.append(textElement('summary','查看失败文件'));for(const e of w.last_result.errors)details.append(textElement('p',e.path+'：'+e.message));card.append(details);}
    card.append(textElement('small',w.origin==='browser'?'电脑原文件保留，校验后清理 NAS 临时副本':w.delete_local?'校验成功后删除本地文件':'保留本地文件'));
    const buttons=textElement('div','','history-buttons');
    const controls=[];
    if(w.enabled)controls.push(['暂停','pause']);
    else if(!['completed','partial','failed','receiving'].includes(w.last_status))controls.push(['继续上传','resume']);
    if(w.last_status==='receiving'&&browserTransfer?.id===w.id)controls.push(['继续从电脑传输','browser-resume']);
    if(!w.enabled&&(w.last_status==='failed'||w.last_result?.errors?.length||w.last_result?.conflicts))controls.push(['重试失败文件','retry']);
    if(!w.enabled)controls.push([w.origin==='browser'?'移除任务及临时副本':'移除记录','delete']);
    for(const [label,action] of controls){const b=textElement('button',label);b.addEventListener('click',safe(async()=>{b.disabled=true;try{if(action==='browser-resume'){await sendComputerFiles(w.target);}else await api('/api/uploads/manual/'+action,{id:w.id});await window.loadManualUploads();}finally{b.disabled=false;}}));buttons.append(b);}
    card.append(buttons);list.append(card);
  }
};
$('refreshManualUploads').addEventListener('click',safe(()=>window.loadManualUploads()));
setInterval(()=>{if(!$('uploads').classList.contains('hidden'))window.loadManualUploads().catch(()=>{});},5000);
