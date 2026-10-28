'use strict';
(() => {
  let offset=0, total=0, rows=[], search='';
  const selected=new Set();
  function selection(){
    $('deleteGenerated').disabled=!selected.size;
    $('deleteGenerated').textContent=selected.size?`删除选中记录（${selected.size}）`:'删除选中记录';
    const count=rows.filter(r=>selected.has(r.id)).length;
    $('selectGeneratedPage').checked=!!rows.length&&count===rows.length;
    $('selectGeneratedPage').indeterminate=count>0&&count<rows.length;
  }
  async function load(){
    const data=await api('/api/generated?offset='+offset+'&search='+encodeURIComponent(search));
    rows=data.records; total=data.total;
    if(!rows.length&&offset>0){offset=Math.max(0,offset-100);return load();}
    $('generatedRows').replaceChildren();
    for(const record of rows){
      const tr=document.createElement('tr'),checkCell=document.createElement('td'),check=document.createElement('input');
      check.type='checkbox';check.checked=selected.has(record.id);check.setAttribute('aria-label','选择 '+record.source.split(/[\\/]/).pop());
      check.addEventListener('change',()=>{check.checked?selected.add(record.id):selected.delete(record.id);selection();});
      checkCell.append(check);
      const file=document.createElement('td');file.append(textElement('b',record.source.split(/[\\/]/).pop()),textElement('small',record.source),textElement('small',record.target));
      const time=textElement('td',new Date(record.created*1000).toLocaleString('zh-CN'));
      const buttons=document.createElement('td'),regen=textElement('button','重新生成'),remove=textElement('button','删除记录','text-button');
      regen.addEventListener('click',safe(async()=>{
        regen.disabled=true;
        try{const job=await api('/api/generated/regenerate',{id:record.id});currentJob=job.id;changePage('generate');await poll();toast('已提交此文件重新生成');}
        finally{regen.disabled=false;}
      }));
      remove.addEventListener('click',safe(async()=>{await api('/api/generated/delete',{ids:[record.id]});selected.delete(record.id);await load();toast('记录已删除，下次扫描会重新生成此文件');}));
      buttons.append(regen,remove);tr.append(checkCell,file,time,buttons);$('generatedRows').append(tr);
    }
    if(!rows.length){const tr=document.createElement('tr'),cell=textElement('td','暂无匹配的成功记录');cell.colSpan=4;tr.append(cell);$('generatedRows').append(tr);}
    $('generatedCount').textContent=`共 ${total} 条`;
    $('generatedPage').textContent=`${Math.floor(offset/100)+1} / ${Math.max(1,Math.ceil(total/100))}`;
    $('generatedPrev').disabled=offset===0;$('generatedNext').disabled=offset+100>=total;selection();
  }
  window.loadGenerated=load;
  $('refreshGenerated').addEventListener('click',safe(load));
  $('searchGenerated').addEventListener('click',safe(async()=>{search=$('generatedSearch').value.trim();offset=0;selected.clear();await load();}));
  $('generatedSearch').addEventListener('keydown',e=>{if(e.key==='Enter'){$('searchGenerated').click();}});
  $('generatedPrev').addEventListener('click',safe(async()=>{offset=Math.max(0,offset-100);await load();}));
  $('generatedNext').addEventListener('click',safe(async()=>{offset+=100;await load();}));
  $('selectGeneratedPage').addEventListener('change',()=>{for(const row of rows){$('selectGeneratedPage').checked?selected.add(row.id):selected.delete(row.id);}for(const input of $('generatedRows').querySelectorAll('input'))input.checked=$('selectGeneratedPage').checked;selection();});
  $('deleteGenerated').addEventListener('click',safe(async()=>{const r=await api('/api/generated/delete',{ids:[...selected]});selected.clear();await load();toast(`已删除 ${r.deleted} 条记录，下次扫描会重新生成这些文件`);}));
})();
