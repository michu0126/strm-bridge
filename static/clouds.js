'use strict';
(() => {
  let catalog=[], accounts=[], editing=null, qrGeneration=0;
  const names={access_token:'Access Token',refresh_token:'Refresh Token',client_id:'Client ID / AppKey',client_secret:'Client Secret / SecretKey',cookie:'登录 Cookie',drive_id:'Drive ID（可选）',username:'手机号 / 邮箱',password:'登录密码',web_token:'网页登录令牌'};
  const secret=new Set(['access_token','refresh_token','client_secret','cookie','password','web_token']);
  const badges={'115':'115',baidu:'百',aliyun:'阿',123:'123',189:'天'};
  function stopQR(){qrGeneration++;$('cloudQR').hidden=true;$('cloudQRImage').removeAttribute('src');}
  function methods(){
    const spec=catalog.find(p=>p.id===$('cloudProvider').value);
    $('cloudMethod').replaceChildren(...spec.methods.map(m=>{const o=textElement('option',m.name);o.value=m.id;return o;}));
    $('cloudMethod').value=editing?.credentials?.auth_mode|| (editing ? (spec.id==='189'?'cookie':'open') : spec.default_method);
    $('cloudMethodLabel').hidden=spec.methods.length===1;
    $('cloudRoot').value=editing?.root||spec.root;
    fields();
  }
  function fields(){
    stopQR();const spec=catalog.find(p=>p.id===$('cloudProvider').value);
    const method=spec.methods.find(m=>m.id===$('cloudMethod').value);
    $('cloudFields').replaceChildren();$('cloudAuthActions').replaceChildren();$('cloudAuthHint').textContent=method.hint;
    const sameMethod=editing && (editing.credentials?.auth_mode||(spec.id==='189'?'cookie':'open'))===method.id;
    if(method.qr){const button=textElement('button','扫码登录 / 刷新二维码');button.type='button';button.addEventListener('click',safe(startQR));$('cloudAuthActions').append(button);}
    for(const [url,title] of [[method.auth_url,'打开授权页面 ↗'],[method.docs,'获取方式 ↗']]){if(url){const a=textElement('a',title);a.href=url;a.target='_blank';a.rel='noopener noreferrer';$('cloudAuthActions').append(a);}}
    for(const key of method.fields){
      const label=textElement('label',names[key]);const input=document.createElement('input');
      input.name=key;input.type=secret.has(key)?'password':'text';input.autocomplete='off';
      input.value=sameMethod?(editing.credentials?.[key]||''):'';
      input.placeholder=sameMethod&&editing.configured?.includes(key)?'已保存，留空保留原值':'';
      label.append(input);$('cloudFields').append(label);
    }
  }
  async function startQR(){
    stopQR();const generation=qrGeneration;$('cloudQR').hidden=false;$('cloudQRStatus').textContent='正在获取二维码…';
    try{const qr=await api('/api/clouds/login/start',{provider:'115'});if(generation!==qrGeneration)return;
      $('cloudQRImage').src=qr.image;$('cloudQRStatus').textContent=qr.message;
      while(generation===qrGeneration&&$('cloudDialog').open){
        await new Promise(resolve=>setTimeout(resolve,3000));if(generation!==qrGeneration)return;
        const result=await api('/api/clouds/login/status',{id:qr.id});if(generation!==qrGeneration)return;
        $('cloudQRStatus').textContent=result.message;
        if(result.status==='confirmed'){$('cloudFields').querySelector('[name="cookie"]').value=result.cookie;$('cloudQRImage').removeAttribute('src');return;}
        if(result.status==='expired')return;
      }
    }catch(err){if(generation===qrGeneration)$('cloudQRStatus').textContent=err.message;}
  }
  async function open(account=null){
    if(!catalog.length)await load();editing=account;
    $('cloudProvider').replaceChildren(...catalog.map(p=>{const o=textElement('option',p.name);o.value=p.id;return o;}));
    $('cloudProvider').value=account?.provider||'115';$('cloudProvider').disabled=!!account;
    $('cloudName').value=account?.name||'';$('cloudDialogTitle').textContent=account?'编辑网盘':'添加网盘';
    $('cloudFormStatus').textContent='';methods();$('cloudDialog').showModal();
  }
  async function load(){
    const d=await api('/api/clouds');catalog=d.providers;accounts=d.accounts;
    $('cloudList').replaceChildren();
    if(!accounts.length){const card=textElement('div','','card cloud-empty');card.append(textElement('h3','把你的网盘连到片库'),textElement('p','115 · 百度网盘 · 阿里云盘 · 123 云盘 · 天翼云盘'));const b=textElement('button','添加第一个网盘','primary');b.addEventListener('click',safe(()=>open()));card.append(b);$('cloudList').append(card);}
    for(const a of accounts){
      const card=textElement('article','','card cloud-card');const head=textElement('div','','cloud-card-head');
      head.append(textElement('span',badges[a.provider],'cloud-badge provider-'+a.provider));const title=textElement('div');title.append(textElement('h3',a.name),textElement('small',catalog.find(p=>p.id===a.provider)?.name));
      head.append(title,textElement('span',{connected:'已连接',error:'连接异常',untested:'待连接'}[a.status]||'待连接','cloud-status '+a.status));card.append(head);
      if(a.error)card.append(textElement('p',a.error,'cloud-error'));
      const buttons=textElement('div','','cloud-actions');
      const choose=textElement('button','选择目录','primary');choose.addEventListener('click',safe(async()=>{changePage('generate');$('source').dataset.cloud=a.source;$('source').value=a.name+'/';updateSourceMode();await openFolder('source',$('source'));}));
      const edit=textElement('button','编辑');edit.addEventListener('click',safe(()=>open(a)));
      const test=textElement('button','测试连接');test.addEventListener('click',safe(async()=>{test.disabled=true;test.textContent='连接中…';try{const r=await api('/api/clouds/test',{id:a.id});toast(r.message);}finally{await load();}}));
      const remove=textElement('button','移除','text-button');remove.addEventListener('click',safe(async()=>{if(!confirm('移除“'+a.name+'”的连接？其监控会暂停，已有 STRM 将无法通过此账号播放；云端文件保留。'))return;await api('/api/clouds/delete',{id:a.id});await load();}));
      buttons.append(choose,edit,test,remove);card.append(buttons);$('cloudList').append(card);
    }
  }
  window.loadClouds=load;
  $('addCloud').addEventListener('click',safe(()=>open()));
  $('cloudProvider').addEventListener('change',methods);
  $('cloudMethod').addEventListener('change',fields);
  $('cloudDialog').addEventListener('close',stopQR);
  $('closeCloud').addEventListener('click',()=>$('cloudDialog').close());
  $('cloudForm').addEventListener('submit',safe(async e=>{
    e.preventDefault();$('saveCloud').disabled=true;$('cloudFormStatus').textContent='正在保存并连接…';
    try{const credentials={auth_mode:$('cloudMethod').value,...Object.fromEntries([...$('cloudFields').querySelectorAll('input')].map(i=>[i.name,i.value.trim()]))};
      const a=await api('/api/clouds/save',{id:editing?.id,provider:$('cloudProvider').value,name:$('cloudName').value.trim(),root:$('cloudRoot').value.trim(),credentials});
      editing=a;const r=await api('/api/clouds/test',{id:a.id});$('cloudDialog').close();toast(r.message);await load();
    }catch(err){$('cloudFormStatus').textContent=err.message;await load();}finally{$('saveCloud').disabled=false;}
  }));
})();
