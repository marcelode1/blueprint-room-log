
(()=>{
const root=document.getElementById('full-drop-shades'),q=s=>root.querySelector(s),canvas=q('canvas'),ctx=canvas.getContext('2d');
function defaults(){const out={b:[],s:[]};for(const k of ['b','s'])for(let i=0;i<4;i++){let left,right;if(i<2){left=i===0?140:(k==='b'?727:776);right=i===0?(k==='b'?713:762):1488;}else{left=140+i*337;right=left+323;}out[k].push({on:i<2,drop:((k==='b'?580:449)-32)/(951-32),c:[{x:left/1653,y:32/951},{x:right/1653,y:32/951},{x:right/1653,y:1},{x:left/1653,y:1}]});}return out;}
const photos=[{name:'Sample window',src:root.dataset.example,panels:defaults(),lines:[],history:[]}];let current=0,img=new Image(),version=0,drag=null,draft=null;
const photo=()=>photos[current],selected=()=>photo().panels[q('#layer').value][+q('#panel').value];
const mix=(a,b,t)=>({x:a.x+(b.x-a.x)*t,y:a.y+(b.y-a.y)*t});
function polygon(p){return [p.c[0],p.c[1],mix(p.c[1],p.c[2],p.drop),mix(p.c[0],p.c[3],p.drop)];}
function sync(){q("#gap-position").value=gapAnchor()*100;q("#gap-position").disabled=!gapPanels().every(p=>p.on);for(const k of ['b','s'])for(let i=0;i<4;i++)q('#'+k+i).checked=photo().panels[k][i].on;const p=selected();q('#drop').value=Math.round(p.drop*100);q('#drop-value').textContent=Math.round(p.drop*100)+'%';q('#position').value=[0,.5,1].includes(p.drop)?String(p.drop):'custom';q('#fabric-controls').hidden=q('#hide').checked;draw();}
function load(index){current=index;drag=null;draft=null;const token=++version,next=new Image();next.onload=()=>{if(token!==version)return;img=next;q('.scene').style.aspectRatio=img.naturalWidth+'/'+img.naturalHeight;sync();};next.onerror=()=>{q('#status').textContent='Could not open picture. Use JPG, PNG, or WebP.';};next.src=photo().src;}
for(const k of ['b','s'])for(let i=0;i<4;i++){const label=document.createElement('label');label.className='form-check';const input=document.createElement('input');input.type='checkbox';input.className='form-check-input';input.id=k+i;input.checked=i<2;const span=document.createElement('span');span.className='form-check-label';span.textContent=String(i+1);label.append(input,span);q(k==='b'?'#blackout-panels':'#sheer-panels').append(label);input.addEventListener('change',()=>{photo().panels[k][i].on=input.checked;q('#layer').value=k;q('#panel').value=String(i);sync();});}
function path(points,w,h){ctx.beginPath();points.forEach((p,i)=>i?ctx.lineTo(p.x*w,p.y*h):ctx.moveTo(p.x*w,p.y*h));ctx.closePath();}
function gapPanels(){const panels=photo().panels[q('#layer').value],i=+q('#gap-pair').value;return [panels[i],panels[i+1]];}
function gapCenter(){const [a,b]=gapPanels();const pa=polygon(a),pb=polygon(b);return mix(mix(pa[1],pa[2],.55),mix(pb[0],pb[3],.55),.5);}
function gapAnchor(){const [a,b]=gapPanels(),mode=q('#gap-mode').value;const left=(a.c[1].x+a.c[2].x)/2,right=(b.c[0].x+b.c[3].x)/2;return mode==='left'?left:mode==='right'?right:(left+right)/2;}
function shiftGap(delta){
const [a,b]=gapPanels(),mode=q('#gap-mode').value;if(!a.on||!b.on)return;
const moveLeft=mode!=='right',moveRight=mode!=='left',minWidth=.005,minGap=.001;
let lo=-Infinity,hi=Infinity;
if(moveLeft){lo=Math.max(lo,a.c[0].x+minWidth-a.c[1].x,a.c[3].x+minWidth-a.c[2].x,-a.c[1].x,-a.c[2].x);hi=Math.min(hi,1-a.c[1].x,1-a.c[2].x);}
if(moveRight){hi=Math.min(hi,b.c[1].x-minWidth-b.c[0].x,b.c[2].x-minWidth-b.c[3].x,1-b.c[0].x,1-b.c[3].x);lo=Math.max(lo,-b.c[0].x,-b.c[3].x);}
const clearance=Math.min(b.c[0].x-a.c[1].x,b.c[3].x-a.c[2].x);
if(mode==='left')hi=Math.min(hi,Math.max(0,clearance-minGap));
if(mode==='right')lo=Math.max(lo,-Math.max(0,clearance-minGap));
if(lo>hi)return;delta=Math.max(lo,Math.min(hi,delta));
if(moveLeft){a.c[1].x+=delta;a.c[2].x+=delta;}
if(moveRight){b.c[0].x+=delta;b.c[3].x+=delta;}
sync();
}
q('#gap-position').addEventListener('input',()=>shiftGap(+q('#gap-position').value/100-gapAnchor()));
for(const id of ['#gap-pair','#gap-mode'])q(id).addEventListener('change',sync);

function handles(){const p=selected();if(!p.on||q('#hide').checked||q('#tool').value!=='fabric')return [];const corners=polygon(p);return [...corners.map((pos,i)=>({kind:'corner',i,pos})),{kind:'pull',pos:mix(corners[2],corners[3],.5)},...(gapPanels().every(p=>p.on)?[{kind:'gap',pos:gapCenter()}]:[])];}
function draw(){const r=canvas.getBoundingClientRect(),w=r.width,h=r.height;if(!w||!h)return;const dpr=devicePixelRatio||1;canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);if(img.naturalWidth)ctx.drawImage(img,0,0,w,h);
for(const k of ['b','s'])for(const p of photo().panels[k]){if(!p.on||p.drop<.001)continue;const poly=polygon(p);path(poly,w,h);ctx.fillStyle=k==='b'?'#f0f0ed':'rgba(220,198,161,.38)';ctx.fill();ctx.beginPath();ctx.moveTo(poly[2].x*w,poly[2].y*h);ctx.lineTo(poly[3].x*w,poly[3].y*h);ctx.strokeStyle=k==='b'?'#c9c9c5':'rgba(181,158,120,.65)';ctx.lineWidth=2;ctx.stroke();}
ctx.strokeStyle='#e22222';ctx.lineWidth=3;ctx.lineCap='round';for(const line of [...photo().lines,...(draft?[draft]:[])]){ctx.beginPath();ctx.moveTo(line.a.x*w,line.a.y*h);ctx.lineTo(line.b.x*w,line.b.y*h);ctx.stroke();}
const hs=handles();if(hs.length){path(polygon(selected()),w,h);ctx.strokeStyle='#1976d2';ctx.lineWidth=1;ctx.setLineDash([5,4]);ctx.stroke();ctx.setLineDash([]);for(const handle of hs){const x=handle.pos.x*w,y=handle.pos.y*h;ctx.fillStyle='#fff';ctx.strokeStyle='#1976d2';ctx.lineWidth=2;if(handle.kind==='gap'){ctx.fillStyle='#1976d2';ctx.fillRect(x-26,y-13,52,26);ctx.fillStyle='#fff';ctx.font='14px sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText('↔',x,y);}else if(handle.kind==='pull'){ctx.fillRect(x-18,y-7,36,14);ctx.strokeRect(x-18,y-7,36,14);}else{ctx.beginPath();ctx.arc(x,y,7,0,Math.PI*2);ctx.fill();ctx.stroke();}}}
q('#undo').disabled=!photo().history.length;q('#clear').disabled=!photo().lines.length;canvas.style.cursor=q('#tool').value==='fabric'?'default':'crosshair';}
const point=e=>{const r=canvas.getBoundingClientRect();return{x:Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),y:Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))};};
function checkpoint(){photo().history.push(structuredClone(photo().lines));if(photo().history.length>100)photo().history.shift();}
function endpoint(e){const p=point(e),r=canvas.getBoundingClientRect();if(draft&&(e.shiftKey||q('#snap').checked)){if(Math.abs(p.x-draft.a.x)*r.width>Math.abs(p.y-draft.a.y)*r.height)p.y=draft.a.y;else p.x=draft.a.x;}return p;}
canvas.addEventListener('pointerdown',e=>{if(e.button!==0)return;const pos=point(e),r=canvas.getBoundingClientRect(),mode=q('#tool').value;e.preventDefault();
if(mode==='fabric'){let best=24,hit=null;for(const h of handles()){const dist=Math.hypot((pos.x-h.pos.x)*r.width,(pos.y-h.pos.y)*r.height);if(dist<best){best=dist;hit=h;}}if(!hit)return;drag={...hit,id:e.pointerId,lastX:pos.x};if(hit.kind==='corner'&&hit.i>1&&selected().drop<.05)selected().drop=.05;}
else if(mode==='draw'){draft={a:pos,b:pos};drag={kind:'line',id:e.pointerId};}
else{let best=15,index=-1;photo().lines.forEach((l,i)=>{const dx=(l.b.x-l.a.x)*r.width,dy=(l.b.y-l.a.y)*r.height,px=(pos.x-l.a.x)*r.width,py=(pos.y-l.a.y)*r.height,t=Math.max(0,Math.min(1,(px*dx+py*dy)/(dx*dx+dy*dy||1))),d=Math.hypot(px-t*dx,py-t*dy);if(d<best){best=d;index=i;}});if(index>=0){checkpoint();photo().lines.splice(index,1);draw();}return;}canvas.setPointerCapture(e.pointerId);sync();});
function pointerMove(e){if(!drag||drag.id!==e.pointerId)return;const pos=point(e),p=selected();if(drag.kind==='gap'){const delta=pos.x-drag.lastX;drag.lastX=pos.x;shiftGap(delta);return;}if(drag.kind==='line')draft.b=endpoint(e);else if(drag.kind==='corner'){if(drag.i<2)p.c[drag.i]=pos;else{const top=p.c[drag.i===2?1:0],f=Math.max(.05,p.drop);p.c[drag.i]={x:Math.max(0,Math.min(1,top.x+(pos.x-top.x)/f)),y:Math.max(top.y+.01,Math.min(1,top.y+(pos.y-top.y)/f))};}}else{const top=mix(p.c[0],p.c[1],.5),bottom=mix(p.c[2],p.c[3],.5),dx=bottom.x-top.x,dy=bottom.y-top.y;p.drop=Math.max(0,Math.min(1,((pos.x-top.x)*dx+(pos.y-top.y)*dy)/(dx*dx+dy*dy||1)));}sync();}
canvas.addEventListener('pointermove',pointerMove);canvas.addEventListener('pointerup',e=>{if(!drag||drag.id!==e.pointerId)return;pointerMove(e);if(draft){const r=canvas.getBoundingClientRect();if(Math.hypot((draft.a.x-draft.b.x)*r.width,(draft.a.y-draft.b.y)*r.height)>3){checkpoint();photo().lines.push(draft);}}drag=null;draft=null;draw();});canvas.addEventListener('pointercancel',()=>{drag=null;draft=null;draw();});
for(const id of ['#layer','#panel','#tool','#hide'])q(id).addEventListener('change',()=>{drag=null;draft=null;sync();});q('#drop').addEventListener('input',()=>{selected().drop=+q('#drop').value/100;sync();});q('#position').addEventListener('change',()=>{if(q('#position').value!=='custom')selected().drop=+q('#position').value;sync();});
q('#undo').addEventListener('click',()=>{if(photo().history.length)photo().lines=photo().history.pop();draw();});q('#clear').addEventListener('click',()=>{if(photo().lines.length){checkpoint();photo().lines=[];}draw();});q('#picture').addEventListener('change',()=>load(+q('#picture').value));
q('#photos').addEventListener('change',async()=>{const files=[...q('#photos').files];q('#photos').disabled=true;let added=0,failed=0;for(const file of files){try{const src=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=reject;reader.readAsDataURL(file);});await new Promise((resolve,reject)=>{const test=new Image();test.onload=resolve;test.onerror=reject;test.src=src;});photos.push({name:file.name,src,panels:defaults(),lines:[],history:[]});const option=document.createElement('option');option.value=photos.length-1;option.textContent=file.name;q('#picture').append(option);added++;}catch(e){failed++;}}q('#photos').disabled=false;q('#photos').value='';if(added){q('#picture').value=photos.length-1;load(photos.length-1);}q('#status').textContent=failed?'Some pictures could not open. Use JPG, PNG, or WebP.':'Photos, fabric layouts, and lines stay here while this view is open.';});
new ResizeObserver(draw).observe(q('.scene'));load(0);
let revision=0,dirty=false,loading=true,saving=false;
const saveButton=document.getElementById('save-layout'),saveStatus=document.getElementById('save-status');
root.addEventListener('input',()=>{if(!loading)dirty=true;});root.addEventListener('change',()=>{if(!loading)dirty=true;});canvas.addEventListener('pointerup',()=>{if(!loading)dirty=true;});
for(const id of ['#clear','#undo'])q(id).addEventListener('click',()=>{dirty=true;});
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
saveButton.addEventListener('click',async()=>{
 if(loading||saving)return;saving=true;saveButton.disabled=true;saveStatus.textContent='Saving…';
 try{const saveable=()=>photos.filter(p=>p.src!==root.dataset.example).map(({name,src,panels,lines})=>({name,src,panels,lines}));
  const documentData={schema_version:1,photos:saveable()};
  if(!documentData.photos.length){saveStatus.textContent='Add a picture of the window first';saving=false;saveButton.disabled=false;return;}
 const response=await fetch(root.dataset.api,{method:'PUT',headers:{'Content-Type':'application/json','X-CSRF-Token':root.dataset.csrf},body:JSON.stringify({revision,document:documentData})});
 const data=await response.json();if(!response.ok)throw Error(data.error||'Save failed');revision=data.revision;dirty=JSON.stringify(documentData)!==JSON.stringify({schema_version:1,photos:saveable()});saveStatus.textContent=dirty?'Saved previous changes; save again for latest edits':'Saved';
 }catch(e){saveStatus.textContent=e.message;}finally{saving=false;saveButton.disabled=false;}
});
(async()=>{saveButton.disabled=true;saveStatus.textContent='Loading saved layout…';try{
const response=await fetch(root.dataset.api,{cache:'no-store'});if(!response.ok)throw Error('Could not load saved layout. Saving is disabled to protect existing work.');const data=await response.json();revision=data.revision;
if(data.document&&data.document.photos&&data.document.photos.length){photos.splice(0,photos.length,...data.document.photos.map(p=>({...p,history:[]})));q('#picture').replaceChildren();photos.forEach((p,i)=>{const option=document.createElement('option');option.value=i;option.textContent=p.name;q('#picture').append(option);});load(0);}
loading=false;saveButton.disabled=false;saveStatus.textContent=data.document?'Saved layout loaded':'New layout';
}catch(e){saveStatus.textContent=e.message;}})();

})();
