const tpl=document.getElementById('stage-tpl');
document.querySelectorAll('.stage-slot').forEach(s=>s.appendChild(tpl.content.cloneNode(true)));
const fit=()=>document.querySelectorAll('.stage-wrap').forEach(w=>w.firstElementChild.style.zoom=w.clientWidth/800);
fit();addEventListener('resize',fit);
document.querySelectorAll('.seg button').forEach(b=>b.onclick=()=>{document.querySelectorAll('.seg button').forEach(x=>x.classList.toggle('on',x===b));document.querySelector('.mini').dataset.p=b.dataset.p});
// Demo video with minimal custom controls (no audio track, so no volume).
const v=document.getElementById('demo'),play=document.querySelector('.play'),note=document.querySelector('.film-note');
play.onclick=async()=>{if(!vc.hidden){v.play();return}try{const r=await fetch('assets/demo.mp4?v=2',{method:'HEAD'});if(!r.ok)throw 0;v.hidden=false;play.hidden=true;vc.hidden=false;v.play()}catch(e){note.hidden=false}};
const io=new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting){e.target.classList.add('in');io.unobserve(e.target)}}),{threshold:.12});
document.querySelectorAll('section:not(.hero)').forEach(s=>{s.classList.add('reveal');io.observe(s)});
// tall bento card cycles left/above/right
const am=document.getElementById('autoMini');
if(am&&!matchMedia('(prefers-reduced-motion: reduce)').matches){const ps=['left','above','right'];let k=0;setInterval(()=>{am.dataset.p=ps[++k%3]},2200)}

const vc=document.querySelector('.vc'),pp=vc.querySelector('.vc-pp'),seek=vc.querySelector('.vc-seek'),vt=vc.querySelector('.vc-t'),fs=vc.querySelector('.vc-fs'),film=document.querySelector('.film');
const fmt=t=>{t=Math.floor(t||0);return Math.floor(t/60)+':'+String(t%60).padStart(2,'0')};
const toggle=()=>v.paused?v.play():v.pause();
const sync=()=>{if(!vc.hidden)play.hidden=!v.paused;film.classList.toggle('paused',v.paused);pp.setAttribute('aria-label',v.paused?'Play':'Pause')};
v.addEventListener('play',sync);v.addEventListener('pause',sync);v.addEventListener('ended',sync);
v.addEventListener('timeupdate',()=>{if(v.duration){seek.value=v.currentTime/v.duration*1000;seek.style.setProperty('--p',seek.value/10+'%')}vt.textContent=fmt(v.currentTime)+' / '+fmt(v.duration)});
seek.addEventListener('input',()=>{if(v.duration)v.currentTime=seek.value/1000*v.duration});
pp.onclick=toggle;v.onclick=toggle;
fs.onclick=()=>{if(document.fullscreenElement)document.exitFullscreen();else if(film.requestFullscreen)film.requestFullscreen();else if(v.webkitEnterFullscreen)v.webkitEnterFullscreen()};
document.addEventListener('keydown',e=>{if(vc.hidden||e.target.matches('input[type=email],textarea'))return;const r=film.getBoundingClientRect();if(e.code==='Space'&&r.bottom>0&&r.top<innerHeight&&e.target.tagName!=='BUTTON'){e.preventDefault();toggle()}});
