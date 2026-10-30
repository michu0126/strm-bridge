'use strict';
// An ordinary page section stays visible even when the browser suppresses dialogs.
let pendingConfirmation;
function finishConfirmation(accepted){
  if(!pendingConfirmation)return;
  const resolve=pendingConfirmation;
  pendingConfirmation=null;
  document.getElementById('confirmationPanel').classList.add('hidden');
  resolve(accepted);
}
function confirmAction(message,{title='确认操作',accept='确认执行',details=[]}={}){
  finishConfirmation(false);
  document.getElementById('confirmationTitle').textContent=title;
  document.getElementById('confirmationMessage').textContent=message;
  const list=document.getElementById('confirmationDetails');
  list.replaceChildren();
  for(const detail of details){const row=document.createElement('li');row.textContent=detail;list.append(row);}
  document.getElementById('acceptConfirmation').textContent=accept;
  const panel=document.getElementById('confirmationPanel');
  const result=new Promise(resolve=>{pendingConfirmation=resolve;});
  panel.classList.remove('hidden');
  panel.scrollIntoView({behavior:'smooth',block:'center'});
  document.getElementById('cancelConfirmation').focus({preventScroll:true});
  return result;
}
document.getElementById('acceptConfirmation').addEventListener('click',()=>finishConfirmation(true));
document.getElementById('cancelConfirmation').addEventListener('click',()=>finishConfirmation(false));
document.getElementById('confirmationPanel').addEventListener('keydown',event=>{
  if(event.key==='Escape'){event.preventDefault();finishConfirmation(false);}
});
