'use strict';
function uploadBytes(n){if(!Number.isFinite(Number(n)))return '0 B';const units=['B','KB','MB','GB','TB'];let i=0;while(n>=1024&&i<4){n/=1024;i++;}return n.toFixed(i?1:0)+' '+units[i];}
const uploadStates={waiting:'等待检查',running:'正在检查',idle:'暂无新文件',completed:'上传完成',partial:'有文件需处理',failed:'检查失败',paused:'已暂停',interrupted:'等待续传'};
function uploadSummary(r={}){return `上传 ${r.uploaded||0} 个 · 已存在 ${r.skipped||0} 个 · 同名冲突 ${r.conflicts||0} 个 · 等待稳定 ${r.waiting||0} 个${r.deleted?' · 已删除本地 '+r.deleted+' 个':''}`;}
window.loadUploadWatches=async function(){
  const rows=await api('/api/uploads');$('uploadWatchList').replaceChildren();
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
setInterval(()=>{if(!$('watch').classList.contains('hidden'))window.loadUploadWatches().catch(()=>{});},5000);
window.renderUploadHistory=function(j){
  const card=textElement('article','','card history-card');const head=textElement('div','','history-head');head.append(textElement('b','上传到 115 · '+new Date(j.created*1000).toLocaleString('zh-CN')),textElement('span',statuses[j.status]||j.status,'history-status'));card.append(head,textElement('p',j.options.source+' → '+j.options.target),textElement('small',uploadSummary(j.result)));
  if(j.error)card.append(textElement('div',j.error,'errorbox'));for(const e of j.result?.errors||[])card.append(textElement('p',e.path+'：'+e.message));$('historyList').append(card);
};
