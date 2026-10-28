'use strict';
(() => {
  function copySource(input){const value=sourceValue();if(!value.startsWith('cloud://'))throw Error('请先在生成任务选择 115 源目录');input.dataset.cloud=value;input.value=$('source').value;}
  $('browseReconcileSource').addEventListener('click',safe(()=>openFolder('cloud_source',$('reconcileSource'))));
  $('browseReconcileTarget').addEventListener('click',safe(()=>openFolder('target',$('reconcileTarget'))));
  $('reuseReconcile').addEventListener('click',safe(()=>{copySource($('reconcileSource'));$('reconcileTarget').value=$('target').value;$('reconcileKind').value=document.querySelector('input[name=kind]:checked').value;}));
  function reconcileOptions(){const source=$('reconcileSource').dataset.cloud,target=$('reconcileTarget').value.trim();if(!source||!target)throw Error('请选择 115 源目录和 STRM 目标目录');return {source,target,kind:$('reconcileKind').value,reconcile:true,mode:'redirect',cleanup:false,subtitle_mode:'copy'};}
  $('reconcileForm').addEventListener('submit',safe(async e=>{e.preventDefault();const options=reconcileOptions();const job=await api('/api/reconcile',options);currentJob=job.id;changePage('generate');await poll();$('preview').scrollIntoView({behavior:'smooth'});}));
  $('enableReconcile').addEventListener('click',safe(async()=>{const options=reconcileOptions(),watches=await api('/api/watches');const existing=watches.find(w=>w.options.source===options.source&&w.options.target===options.target);await api('/api/watches',existing?{id:existing.id,enabled:true,reconcile:true,interval:Number($('reconcileInterval').value)*60}:{options,interval:Number($('reconcileInterval').value)*60});$('reconcileStatus').textContent='已启用目录同步，云端改名、移动和删除会同步到 STRM 目录';changePage('watch');}));

  function ruleHint(){const h=Number($('videoThreshold').value),w=Math.round(h*16/9);$('videoRuleHint').textContent=$('videoRule').value==='height'?`按原始画面高度 < ${h} 判断。`:`按短边 < ${h} 且长边 < ${w} 判断，保留达到长边标准的裁切影片。`;}
  $('videoThreshold').addEventListener('change',ruleHint);$('videoRule').addEventListener('change',ruleHint);ruleHint();
  let scans=[],current=null,batch=null,offset=0,total=0,rows=[],request=0;
  const selected=new Set();
  const states={scanning:'检测中',running:'操作中',completed:'完成',partial:'部分完成',cancelled:'已暂停',interrupted:'等待继续',failed:'失败'};
  function selection(){const running=current?.status==='scanning'||batch?.status==='running';$('videoSelectedCount').textContent=`已选 ${selected.size} 个低清影片`;$('moveSelectedVideos').disabled=!selected.size||running;$('deleteSelectedVideos').disabled=!selected.size||running;}
  async function refresh(){
    const ticket=++request;const all=await api('/api/videos/scans');if(ticket!==request)return;
    scans=all.filter(s=>!s.operation);batch=all.find(s=>s.operation)||null;
    const wanted=$('videoScanHistory').value||current?.id;
    $('videoScanHistory').replaceChildren();
    for(const s of scans){const option=textElement('option',new Date(s.created*1000).toLocaleString('zh-CN')+' · '+(states[s.status]||s.status));option.value=s.id;$('videoScanHistory').append(option);}
    current=scans.find(s=>s.id===wanted)||scans[0]||null;
    if(current)$('videoScanHistory').value=current.id;
    if(current){
      const data=await api('/api/videos/rows/'+current.id+'?offset='+offset+'&filter='+$('videoResultFilter').value);if(ticket!==request)return;
      current=data.scan;rows=data.rows;total=data.total;
      const listing=current.listing;
      $('videoScanProgress').textContent=`${states[current.status]||current.status} · ${current.done} / ${current.total||'…'} 个 · 低于阈值 ${current.matched} 个 · 已知 ${current.known} 个 · 未知 ${current.unknown} 个 · 缓存 ${current.cached} 个`+(current.total===0&&listing?` · 已读取 ${listing.files||0} 个文件`:'')+(current.current?' · '+current.current:'')+(current.error?' · '+current.error:'');
    }else{rows=[];total=0;$('videoScanProgress').textContent='选择目录开始检测；结果自动保存，后续检测复用视频参数。';}
    $('videoRows').replaceChildren();
    for(const row of rows){
      const tr=document.createElement('tr'),checkCell=document.createElement('td'),check=document.createElement('input');check.type='checkbox';check.disabled=!row.low||!!row.operation;check.checked=selected.has(row.id);check.setAttribute('aria-label','选择 '+row.name);check.addEventListener('change',()=>{check.checked?selected.add(row.id):selected.delete(row.id);selection();});checkCell.append(check);
      const file=document.createElement('td');file.append(textElement('b',row.name),textElement('small',row.path));if(row.error)file.append(textElement('small',row.error));
      const dimensions=textElement('td',row.width&&row.height?`${row.width} × ${row.height}`:'未知');const size=textElement('td',uploadBytes(row.size));if(row.operation)size.append(textElement('small',row.operation==='move'?'已移动':'已移入回收站'));
      tr.append(checkCell,file,dimensions,size);$('videoRows').append(tr);
    }
    if(!rows.length){const tr=document.createElement('tr'),td=textElement('td','暂无符合条件的影片；检测过程中可刷新查看结果。');td.colSpan=4;tr.append(td);$('videoRows').append(tr);}
    $('videoPage').textContent=`${Math.floor(offset/100)+1} / ${Math.max(1,Math.ceil(total/100))} · ${total} 个`;$('videoPrev').disabled=offset===0;$('videoNext').disabled=offset+100>=total;
    const active=all.find(s=>['scanning','running'].includes(s.status));$('cancelVideoTask').disabled=!active;
    if(batch){const r=batch.result||{};$('videoTaskResult').textContent=`最近批量操作：${states[batch.status]||batch.status} · ${batch.done} / ${batch.total} · 移动 ${r.moved||0} 个 · 删除 ${r.deleted||0} 个`+(r.errors?.length?' · '+r.errors.slice(0,3).map(x=>x.path+'：'+x.message).join('；'):'')+(batch.error?' · '+batch.error:'');}
    selection();
  }
  window.loadVideoTools=refresh;
  $('browseVideoSource').addEventListener('click',safe(()=>openFolder('cloud_source',$('videoSource'))));
  $('browseVideoMoveTarget').addEventListener('click',safe(()=>openFolder('upload_target',$('videoMoveTarget'))));
  $('reuseVideoSource').addEventListener('click',safe(()=>copySource($('videoSource'))));
  async function start(options){const s=await api('/api/videos/scan',options);current=s;offset=0;selected.clear();$('videoScanHistory').replaceChildren();$('videoFormStatus').textContent='正在读取原视频参数，关闭页面后后台继续检测。';await refresh();}
  $('videoScanForm').addEventListener('submit',safe(async e=>{e.preventDefault();const source=$('videoSource').dataset.cloud;if(!source)throw Error('请选择 115 源目录');await start({source,height:Number($('videoThreshold').value),rule:$('videoRule').value,refresh:$('refreshVideoMetadata').checked});}));
  $('resumeVideoScan').addEventListener('click',safe(async()=>{if(!current)throw Error('还没有检测记录');await start(current.options);}));
  $('cancelVideoTask').addEventListener('click',safe(async()=>{const all=await api('/api/videos/scans'),active=all.find(s=>['scanning','running'].includes(s.status));if(active){await api('/api/videos/cancel',{id:active.id});toast('已请求暂停，当前文件处理结束后停止');}await refresh();}));
  $('videoScanHistory').addEventListener('change',safe(async()=>{current=scans.find(s=>s.id===$('videoScanHistory').value);offset=0;selected.clear();await refresh();}));
  $('videoResultFilter').addEventListener('change',safe(async()=>{offset=0;await refresh();}));
  $('videoPrev').addEventListener('click',safe(async()=>{offset=Math.max(0,offset-100);await refresh();}));
  $('videoNext').addEventListener('click',safe(async()=>{offset+=100;await refresh();}));
  $('selectVideoPage').addEventListener('click',safe(async()=>{for(const r of rows)if(r.low&&!r.operation)selected.add(r.id);await refresh();}));
  $('selectAllLowVideos').addEventListener('click',safe(async()=>{if(!current)throw Error('请先检测视频');const data=await api('/api/videos/ids/'+current.id);if(data.ids.length>5000)throw Error('每次最多选择 5000 个，请分批操作');for(const id of data.ids)selected.add(id);await refresh();}));
  $('clearVideoSelection').addEventListener('click',safe(async()=>{selected.clear();await refresh();}));
  async function operate(operation){
    if(!current||!selected.size)throw Error('请选择已检测为低于阈值的影片');
    const target=$('videoMoveTarget').dataset.cloud;
    if(operation==='move'&&!target)throw Error('请选择同一 115 账号的移动目标');
    if(operation==='delete'&&!confirm(`将选中的 ${selected.size} 个网盘视频移入 115 回收站。已启用的目录同步会随后回收对应 STRM。确认删除？`))return;
    batch=await api('/api/videos/batch',{scan_id:current.id,ids:[...selected],operation,target});selected.clear();await refresh();
  }
  $('moveSelectedVideos').addEventListener('click',safe(()=>operate('move')));$('deleteSelectedVideos').addEventListener('click',safe(()=>operate('delete')));
  setInterval(()=>{if(!$('videos').classList.contains('hidden'))refresh().catch(e=>toast(e.message));},3000);
  window.renderCloudToolsHistory=function(job){const card=textElement('article','','card history-card');card.append(textElement('b',job.type==='video_scan'?'视频分辨率检测':'网盘批量操作'),textElement('p',(states[job.status]||job.status)+' · '+(job.done||0)+' / '+(job.total||0)),textElement('small',job.options?.source||''));const button=textElement('button','查看检测结果');button.addEventListener('click',()=>changePage('videos'));card.append(button);$('historyList').append(card);};
})();
