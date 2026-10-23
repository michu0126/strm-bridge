'use strict';
const appearanceDefaults={theme:'auto',transparency:18,background:'bing',custom_url:''};
let appearance={...appearanceDefaults},bingInfo=null,wallpaperSequence=0;
try{appearance={...appearanceDefaults,...JSON.parse(localStorage.getItem('strm-appearance')||'{}')};}catch{}
if(!['dark','light','auto'].includes(appearance.theme))appearance.theme='auto';
if(!['bing','custom','none'].includes(appearance.background))appearance.background='bing';
appearance.transparency=Math.min(100,Math.max(0,Number(appearance.transparency)||0));
function appearanceSave(){try{localStorage.setItem('strm-appearance',JSON.stringify(appearance));}catch{} }
function appearanceApply(){document.documentElement.dataset.theme=appearance.theme;document.documentElement.style.setProperty('--panel-opacity',String(1-appearance.transparency/100));$('transparency').value=appearance.transparency;$('transparencyValue').value=appearance.transparency+'%';for(const b of document.querySelectorAll('[data-theme-choice]'))b.setAttribute('aria-pressed',String(b.dataset.themeChoice===appearance.theme));for(const b of document.querySelectorAll('[data-background-choice]'))b.setAttribute('aria-pressed',String(b.dataset.backgroundChoice===appearance.background));}
function wallpaperSet(url){if(url&&!url.startsWith('/api/appearance/image/'))return;document.documentElement.style.setProperty('--wallpaper-image',url?'url('+JSON.stringify(url)+')':'none');}
async function wallpaperLoad(refresh=false){const sequence=++wallpaperSequence;$('wallpaperStatus').textContent='';if(appearance.background==='none'){wallpaperSet('');$('wallpaperCredit').textContent='';return;}if(appearance.background==='custom'){wallpaperSet(appearance.custom_url);$('wallpaperCredit').textContent='你的自选背景 · 图片保存在 NAS';if(!appearance.custom_url)$('wallpaperStatus').textContent='选择一张图片，打造你的媒体工作台。';return;}try{$('wallpaperStatus').textContent='正在获取今日壁纸…';if(!bingInfo||refresh)bingInfo=await api('/api/appearance/bing');if(sequence!==wallpaperSequence)return;wallpaperSet(bingInfo.url);$('wallpaperCredit').textContent=bingInfo.copyright||'Bing 每日壁纸';$('wallpaperStatus').textContent='今日壁纸已就绪 · 每日自动更新';}catch(e){if(sequence!==wallpaperSequence)return;wallpaperSet('');$('wallpaperCredit').textContent='';$('wallpaperStatus').textContent='Bing 壁纸暂时不可用，当前显示渐变背景。可稍后重试或上传图片。';}}
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
