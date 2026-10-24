'use strict';
const palettes=[
  {id:'mint',name:'松石绿',colors:['#268971','#103e38','#b2e8d6']},
  {id:'ocean',name:'海盐蓝',colors:['#3d78c9','#142f52','#b3d4ff']},
  {id:'violet',name:'暮光紫',colors:['#8c5ec5','#352044','#d5c0ff']},
  {id:'rose',name:'蔷薇粉',colors:['#bc557f','#4c2538','#f5c5da']},
  {id:'amber',name:'琥珀金',colors:['#b78530','#49331a','#f8d594']},
  {id:'teal',name:'青瓷色',colors:['#248d96','#143e43','#ace7e8']},
  {id:'indigo',name:'星夜靛',colors:['#6d71cc','#292d58','#c3c9ff']},
  {id:'slate',name:'石墨灰',colors:['#667e93','#27343f','#c9d5df']}
];
const appearanceDefaults={theme:'auto',palette:'mint',transparency:18,background:'bing',custom_url:''};
let appearance={...appearanceDefaults},bingInfo=null,wallpaperSequence=0;
try{appearance={...appearanceDefaults,...JSON.parse(localStorage.getItem('strm-appearance')||'{}')};}catch{}
if(!['dark','light','auto'].includes(appearance.theme))appearance.theme='auto';
if(!palettes.some(p=>p.id===appearance.palette))appearance.palette='mint';
if(!['bing','custom','none'].includes(appearance.background))appearance.background='bing';
appearance.transparency=Math.min(100,Math.max(0,Number(appearance.transparency)||0));
function appearanceSave(){try{localStorage.setItem('strm-appearance',JSON.stringify(appearance));}catch{} }
function appearanceApply(){document.documentElement.dataset.theme=appearance.theme;document.documentElement.dataset.palette=appearance.palette;document.documentElement.style.setProperty('--panel-opacity',String(1-appearance.transparency/100));$('paletteName').textContent=palettes.find(p=>p.id===appearance.palette).name;$('transparency').value=appearance.transparency;$('transparencyValue').value=appearance.transparency+'%';for(const b of document.querySelectorAll('[data-palette-choice]'))b.setAttribute('aria-pressed',String(b.dataset.paletteChoice===appearance.palette));for(const b of document.querySelectorAll('[data-theme-choice]'))b.setAttribute('aria-pressed',String(b.dataset.themeChoice===appearance.theme));for(const b of document.querySelectorAll('[data-background-choice]'))b.setAttribute('aria-pressed',String(b.dataset.backgroundChoice===appearance.background));}
for(const palette of palettes){const button=document.createElement('button');button.type='button';button.className='palette-choice';button.dataset.paletteChoice=palette.id;button.setAttribute('aria-label',palette.name);button.setAttribute('aria-pressed','false');const sample=document.createElement('span');sample.className='palette-sample';sample.setAttribute('aria-hidden','true');palette.colors.forEach((color,i)=>sample.style.setProperty('--sample-'+i,color));sample.append(document.createElement('i'),document.createElement('i'),document.createElement('i'));const name=document.createElement('span');name.textContent=palette.name;button.append(sample,name);button.addEventListener('click',()=>{appearance.palette=palette.id;appearanceApply();appearanceSave();});$('paletteGrid').append(button);}
function wallpaperSet(url){if(url&&!url.startsWith('/api/appearance/image/'))return;document.documentElement.style.setProperty('--wallpaper-image',url?'url('+JSON.stringify(url)+')':'none');}
async function wallpaperLoad(refresh=false){const sequence=++wallpaperSequence;$('wallpaperStatus').textContent='';if(appearance.background==='none'){wallpaperSet('');$('wallpaperCredit').textContent='';return;}if(appearance.background==='custom'){wallpaperSet(appearance.custom_url);$('wallpaperCredit').textContent='自选背景';if(!appearance.custom_url)$('wallpaperStatus').textContent='请上传背景图片。';return;}try{$('wallpaperStatus').textContent='正在获取今日壁纸…';if(!bingInfo||refresh)bingInfo=await api('/api/appearance/bing');if(sequence!==wallpaperSequence)return;wallpaperSet(bingInfo.url);$('wallpaperCredit').textContent=bingInfo.copyright||'Bing 每日壁纸';$('wallpaperStatus').textContent='今日壁纸已更新';}catch(e){if(sequence!==wallpaperSequence)return;wallpaperSet('');$('wallpaperCredit').textContent='';$('wallpaperStatus').textContent='壁纸获取失败，暂用渐变背景。';}}
for(const b of document.querySelectorAll('[data-theme-choice]'))b.addEventListener('click',()=>{appearance.theme=b.dataset.themeChoice;appearanceApply();appearanceSave();});
for(const b of document.querySelectorAll('[data-background-choice]'))b.addEventListener('click',()=>{appearance.background=b.dataset.backgroundChoice;appearanceApply();appearanceSave();wallpaperLoad();});
$('transparency').addEventListener('input',()=>{appearance.transparency=Number($('transparency').value);appearanceApply();appearanceSave();});
$('appearanceShortcut').addEventListener('click',()=>changePage('appearance'));
$('uploadWallpaper').addEventListener('click',()=>$('wallpaperFile').click());
$('wallpaperFile').addEventListener('change',safe(async()=>{const file=$('wallpaperFile').files[0];if(!file)return;if(file.size>10*1024*1024)throw Error('背景图片需要在 10MB 以内');$('uploadWallpaper').disabled=true;try{const bitmap=await createImageBitmap(file);bitmap.close();const r=await fetch('/api/appearance/upload',{method:'POST',credentials:'same-origin',headers:{'Content-Type':file.type||'application/octet-stream'},body:file});const d=await r.json();if(!r.ok)throw Error(d.error||'背景上传失败');appearance.custom_url=d.url;appearance.background='custom';appearanceSave();appearanceApply();await wallpaperLoad();toast('背景图片已保存');}finally{$('uploadWallpaper').disabled=false;$('wallpaperFile').value='';}}));
$('refreshWallpaper').addEventListener('click',()=>wallpaperLoad(true));
$('resetAppearance').addEventListener('click',()=>{const custom=appearance.custom_url;appearance={...appearanceDefaults,custom_url:custom};appearanceSave();appearanceApply();wallpaperLoad();toast('已恢复默认外观');});
document.addEventListener('bridge-ready',async()=>{try{const d=await api('/api/appearance');if(d.custom_url){appearance.custom_url=d.custom_url;appearanceSave();}}catch{}await wallpaperLoad();});
setInterval(()=>{const today=new Date().toLocaleDateString('sv-SE').replaceAll('-','');if(appearance.background==='bing'&&bingInfo?.date!==today)wallpaperLoad(true);},60*60*1000);
appearanceApply();
