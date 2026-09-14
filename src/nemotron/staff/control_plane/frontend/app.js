const SLOTS=[
  {x:11.5,y:29.2,w:8.9,h:16.8,label:'مراقبة العمليات',staffId:'staff-operations-monitor'},
  {x:35.7,y:28.7,w:9.4,h:17,label:'تحليل البيانات',staffId:'staff-data-analyst'},
  {x:61.4,y:28.1,w:6.3,h:15.3,label:'تطوير الأنظمة',staffId:'staff-systems-developer'},
  {x:22,y:17,w:7.3,h:26.5,label:'إدارة المشاريع',staffId:'staff-project-manager'},
  {x:29.6,y:19.2,w:5.5,h:25.9,label:'تجربة المستخدم',staffId:'staff-ux-specialist'},
  {x:67.9,y:18.8,w:6.9,h:26.3,label:'تكامل الأنظمة',staffId:'staff-integration-engineer'},
  {x:13.6,y:43.6,w:16.6,h:29.3,label:'المحاسبة المالية',staffId:'staff-financial-accountant'},
  {x:1.3,y:43.7,w:11.7,h:27.7,label:'الأمن السيبراني',staffId:'staff-cybersecurity'},
  {x:45.2,y:28.7,w:7,h:15.5,label:'إدارة المحتوى',staffId:'staff-content-manager'},
  {x:52.7,y:29.9,w:8.7,h:16.1,label:'التحليلات المتقدمة',staffId:'staff-advanced-analytics'},
  {x:32.3,y:45.8,w:14.8,h:26.9,label:'الذكاء الاصطناعي',staffId:'staff-ai-specialist'},
  {x:87.7,y:29.9,w:12.3,h:14.5,label:'إدارة البنية التحتية',staffId:'staff-infrastructure-manager'},
  {x:75.2,y:29.9,w:8.4,h:16.4,label:'إدارة الشبكات',staffId:'staff-network-manager'},
  {x:51,y:46.2,w:15,h:25.5,label:'التسويات البنكية',staffId:'staff-bank-reconciliation'},
  {x:70.9,y:46.5,w:13.3,h:24.3,label:'التقارير المالية',staffId:'staff-financial-reporting'},
  {x:85.9,y:44.8,w:14.1,h:28.3,label:'دعم العملاء',staffId:'staff-customer-support'}
];

const stage=document.getElementById('stage');
const drawer=document.getElementById('drawer');
const workspaceBody=document.getElementById('workspaceBody');
const login=document.getElementById('login');
const loginForm=document.getElementById('loginForm');
const loginError=document.getElementById('loginError');
const staffCount=document.getElementById('staffCount');
let zoom=1;
let staff=[];

document.body.classList.add('lite-mode');
drawer.style.background='transparent';

function esc(value){return String(value??'').replace(/[&<>'\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','\"':'&quot;'}[c]));}
function fmtTime(value){if(!value)return '—';try{return new Intl.DateTimeFormat('ar-SA',{dateStyle:'short',timeStyle:'short'}).format(new Date(value));}catch{return String(value);}}
function sleep(ms){return new Promise(resolve=>setTimeout(resolve,ms));}
async function api(path,options={}){
  const {headers={},...rest}=options;
  const response=await fetch(path,{credentials:'same-origin',...rest,headers:{Accept:'application/json',...headers}});
  if(response.status===401){showLogin();throw new Error('unauthorized');}
  if(!response.ok){throw new Error(`HTTP ${response.status}`);}
  if(response.status===204)return null;
  return response.json();
}
function showLogin(message=''){
  login.setAttribute('aria-hidden','false');
  loginError.style.display=message?'block':'none';
  loginError.textContent=message;
  setTimeout(()=>document.getElementById('accessToken').focus(),0);
}
function hideLogin(){login.setAttribute('aria-hidden','true');loginError.style.display='none';document.getElementById('accessToken').value='';}

loginForm.addEventListener('submit',async event=>{
  event.preventDefault();
  loginError.style.display='none';
  const input=document.getElementById('accessToken');
  try{
    const response=await fetch('/ui/login',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','Accept':'application/json'},body:JSON.stringify({token:input.value})});
    input.value='';
    if(!response.ok)throw new Error('invalid');
    hideLogin();
    await bootstrap();
  }catch{
    input.value='';
    showLogin('تعذر تسجيل الدخول. تحقق من مفتاح الوصول ثم أعد المحاولة.');
  }
});

document.getElementById('logout').addEventListener('click',async()=>{
  try{await fetch('/ui/logout',{method:'POST',credentials:'same-origin'});}finally{showLogin();}
});

function setZoom(value){zoom=Math.max(.75,Math.min(1.5,value));stage.style.transform=`scale(${zoom})`;document.getElementById('zoomText').textContent=Math.round(zoom*100)+'%';}
document.getElementById('plus').onclick=()=>setZoom(zoom+.1);
document.getElementById('minus').onclick=()=>setZoom(zoom-.1);
document.getElementById('fit').onclick=()=>setZoom(1);

document.getElementById('close').onclick=closeDrawer;
drawer.onclick=event=>{if(event.target===drawer)closeDrawer();};
document.addEventListener('keydown',event=>{if(event.key==='Escape')closeDrawer();});
document.getElementById('overviewBtn').onclick=showOverview;

function closeDrawer(){drawer.classList.remove('open');drawer.setAttribute('aria-hidden','true');}
function openDrawer(name,role){document.getElementById('employeeName').textContent=name;document.getElementById('employeeRole').textContent=role||'';drawer.classList.add('open');drawer.setAttribute('aria-hidden','false');document.getElementById('close').focus();}
function statusBadge(status){const active=status==='active';return `<span class="status ${active?'':'warn'}"><span class="dot"></span>${active?'نشط':'موقوف'}</span>`;}
function renderPermissions(items){if(!items?.length)return '<div class="empty">لا توجد صلاحيات مسجلة.</div>';return `<div class="chips">${items.map(p=>`<span class="chip">${esc(p.action)} · ${esc(p.resource)} · ${esc(p.max_risk)}</span>`).join('')}</div>`;}
function renderQueuedWork(items){if(!items?.length)return '<div class="empty">لا توجد تعليمات في طابور هذا الموظف.</div>';return items.map(item=>`<div class="task"><div class="task-head"><span class="task-title">${esc(item.title)}</span><span class="task-state">${esc(item.status)}</span></div><small>${esc(item.action)} → ${esc(item.resource)} · risk=${esc(item.risk)}<br>${esc(item.work_item_id)}</small></div>`).join('');}
function renderTasks(items){if(!items?.length)return '<div class="empty">لا توجد مهام محكومة لهذا الموظف حاليًا.</div>';return items.map(t=>`<div class="task"><div class="task-head"><span class="task-title">${esc(t.title)}</span><span class="task-state">${esc(t.state)}</span></div><small>${esc(t.action)} → ${esc(t.resource)} · risk=${esc(t.risk)}<br>${esc(t.task_id)}</small>${t.decision?.rationale?`<div class="instruction-feedback success">النتيجة: ${esc(t.decision.rationale)}</div>`:''}</div>`).join('');}
function renderAudit(items){if(!items?.length)return '<div class="empty">لا يوجد نشاط حديث لهذا الموظف.</div>';return items.slice(0,20).map(a=>`<div class="audit"><strong>${esc(a.event_type)}</strong><small>${esc(a.subject_type)} / ${esc(a.subject_id)} · ${fmtTime(a.occurred_at)}</small></div>`).join('');}

function instructionCard(member){return `
  <div class="card instruction-card">
    <h3>إرسال تعليمات إلى الموظف</h3>
    <p>اكتب المطلوب بوضوح. سيتم إسناد التعليمات فورًا، ويكمل الموظف التنفيذ في الخلفية بينما تتابع الواجهة النتيجة تلقائيًا دون تجاوز الصلاحيات أو بوابات الموافقة والتنفيذ.</p>
    <form class="instruction-form" id="instructionForm">
      <label for="instructionText">التعليمات</label>
      <textarea id="instructionText" name="instruction" maxlength="8000" rows="5" required placeholder="مثال: راجع آخر التسويات البنكية وحدد البنود التي تحتاج تحقيقًا إضافيًا."></textarea>
      <div class="instruction-meta"><span>${esc(member.display_name)}</span><span>حد أقصى 8000 حرف</span></div>
      <div class="actions"><button class="primary" type="submit">إسناد التعليمات</button></div>
      <div class="instruction-feedback" id="instructionFeedback" aria-live="polite"></div>
    </form>
  </div>`;}

function renderWorkspace(member,data){
  const placement=data.placement||{};
  workspaceBody.innerHTML=`
    ${statusBadge(data.status)}
    ${instructionCard(member)}
    <div class="card"><h3>هوية الموظف</h3><dl class="meta">
      <dt>Staff ID</dt><dd>${esc(data.staff_id)}</dd>
      <dt>الدور</dt><dd>${esc(data.role_name)}</dd>
      <dt>المسمى</dt><dd>${esc(placement.job_title||'—')}</dd>
      <dt>القسم</dt><dd>${esc(placement.department_name||'—')}</dd>
      <dt>المدير</dt><dd>${esc(placement.manager_id||'—')}</dd>
      <dt>المنظمة</dt><dd>${esc(placement.organization_name||'—')}</dd>
    </dl></div>
    <div class="card"><h3>العمل في الطابور</h3>${renderQueuedWork(data.queued_work)}</div>
    <div class="card"><h3>الصلاحيات المحكومة</h3>${renderPermissions(data.permissions)}</div>
    <div class="card"><h3>المهام المحكومة</h3>${renderTasks(data.tasks)}</div>
    <div class="card"><h3>النشاط الحديث</h3>${renderAudit(data.audit)}</div>`;
  const form=document.getElementById('instructionForm');
  form?.addEventListener('submit',event=>submitInstruction(member,event));
}

function instructionOutcome(data,workItemId,taskId){
  const task=(data.tasks||[]).find(item=>item.task_id===taskId);
  if(task?.decision?.rationale)return {done:true,ok:true,text:task.decision.rationale};
  const audit=(data.audit||[]).find(item=>item.subject_type==='work_item'&&item.subject_id===workItemId&&[
    'worker.work_blocked','worker.governance_blocked','worker.retry_exhausted','ui.instruction_background_failed'
  ].includes(item.event_type));
  if(audit)return {done:true,ok:false,text:audit.detail||'توقف التنفيذ ويحتاج مراجعة.'};
  return {done:false,ok:false,text:'جاري التنفيذ…'};
}

async function waitForInstructionResult(member,workItemId,taskId){
  const started=Date.now();
  while(Date.now()-started<180000){
    await sleep(2000);
    const data=await api(`/ui/api/staff/${encodeURIComponent(member.staff_id)}/workspace`);
    const outcome=instructionOutcome(data,workItemId,taskId);
    if(outcome.done)return {data,...outcome};
    const feedback=document.getElementById('instructionFeedback');
    if(feedback){feedback.className='instruction-feedback pending';feedback.textContent='تم استلام التعليمات. الموظف يعمل الآن…';}
  }
  const data=await api(`/ui/api/staff/${encodeURIComponent(member.staff_id)}/workspace`);
  return {data,done:false,ok:false,text:'التنفيذ ما زال جاريًا. يمكنك إبقاء الصفحة مفتوحة أو الرجوع لاحقًا إلى المهام المحكومة.'};
}

async function submitInstruction(member,event){
  event.preventDefault();
  const form=event.currentTarget;
  const input=form.querySelector('#instructionText');
  const button=form.querySelector('button[type="submit"]');
  const feedback=form.querySelector('#instructionFeedback');
  const instruction=input.value.trim();
  if(!instruction)return;
  button.disabled=true;
  feedback.className='instruction-feedback pending';
  feedback.textContent='جاري إسناد التعليمات…';
  try{
    const result=await api(`/ui/api/staff/${encodeURIComponent(member.staff_id)}/instructions`,{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({instruction})
    });
    input.value='';
    feedback.className='instruction-feedback pending';
    feedback.textContent='تم استلام التعليمات. الموظف يعمل الآن…';
    const outcome=await waitForInstructionResult(member,result.work_item_id,result.execution?.task_id||`work-task:${result.work_item_id}`);
    renderWorkspace(member,outcome.data);
    const refreshedFeedback=document.getElementById('instructionFeedback');
    if(refreshedFeedback){
      refreshedFeedback.className=`instruction-feedback ${outcome.ok?'success':outcome.done?'error':'pending'}`;
      refreshedFeedback.textContent=outcome.ok?`تم التنفيذ بنجاح · ${outcome.text}`:outcome.text;
    }
  }catch(error){
    if(error.message!=='unauthorized'){
      const current=document.getElementById('instructionFeedback')||feedback;
      current.className='instruction-feedback error';
      current.textContent='تعذر متابعة التنفيذ. تحقق من الاتصال وحالة Control Plane ثم أعد فتح مساحة الموظف.';
    }
  }finally{
    const currentButton=document.querySelector('#instructionForm button[type="submit"]');
    if(currentButton)currentButton.disabled=false;
  }
}

async function selectStaff(member){
  openDrawer(member.display_name,member.role_name);
  workspaceBody.innerHTML='<div class="loading">جاري تحميل مساحة العمل من Control Plane…</div>';
  try{
    const data=await api(`/ui/api/staff/${encodeURIComponent(member.staff_id)}/workspace`);
    renderWorkspace(member,data);
  }catch(error){if(error.message!=='unauthorized')workspaceBody.innerHTML='<div class="error">تعذر تحميل مساحة العمل من Control Plane.</div>';}
}

function memberForSlot(slot,usedIds){
  const exact=staff.find(member=>member.staff_id===slot.staffId);
  if(exact)return exact;
  const roleMatch=staff.find(member=>!usedIds.has(member.staff_id)&&member.role_name===slot.label);
  if(roleMatch)return roleMatch;
  return staff.find(member=>!usedIds.has(member.staff_id))||null;
}

function buildHotspots(){
  stage.querySelectorAll('.hotspot').forEach(node=>node.remove());
  const usedIds=new Set();
  SLOTS.forEach(slot=>{
    const member=memberForSlot(slot,usedIds);
    if(!member)return;
    usedIds.add(member.staff_id);
    const button=document.createElement('button');
    button.type='button';
    button.className='hotspot';
    button.style.cssText=`--x:${slot.x};--y:${slot.y};--w:${slot.w};--h:${slot.h}`;
    button.setAttribute('aria-label',`${member.display_name} — ${member.role_name}`);
    button.title=`${member.display_name} — ${member.role_name}`;
    button.innerHTML=`<span class="tag">${esc(member.display_name)} · ${esc(member.role_name)}</span>`;
    button.onclick=()=>selectStaff(member);
    stage.appendChild(button);
  });
}

async function showOverview(){
  openDrawer('لوحة التحكم','Control Plane');
  workspaceBody.innerHTML='<div class="loading">جاري تحميل الحالة التشغيلية…</div>';
  try{
    const data=await api('/ui/api/overview');
    const approvals=data.approvals||[];
    const executions=data.executions||[];
    const audit=data.audit||[];
    workspaceBody.innerHTML=`
      <span class="status ${data.health?.ready?'':'warn'}"><span class="dot"></span>${data.health?.ready?'النظام جاهز':'النظام متدهور'}</span>
      <div class="card"><h3>ملخص</h3><div class="stats"><div class="stat"><strong>${approvals.length}</strong><span>بانتظار الموافقة</span></div><div class="stat"><strong>${executions.length}</strong><span>في لوحة التنفيذ</span></div><div class="stat"><strong>${staff.length}</strong><span>موظف مسجل</span></div></div></div>
      <div class="card"><h3>Approval Inbox</h3>${approvals.length?approvals.map(a=>`<div class="task"><div class="task-head"><span class="task-title">${esc(a.title)}</span><span class="task-state">${esc(a.risk)}</span></div><small>${esc(a.task_id)} · ${esc(a.action)} → ${esc(a.resource)}</small></div>`).join(''):'<div class="empty">لا توجد موافقات معلقة.</div>'}</div>
      <div class="card"><h3>Execution Dashboard</h3>${executions.length?executions.slice(0,20).map(e=>`<div class="task"><div class="task-head"><span class="task-title">${esc(e.title)}</span><span class="task-state">${esc(e.state)}</span></div><small>${esc(e.tool_id||'—')} / ${esc(e.tool_operation||'—')} · ${esc(e.execution_reference||'لم ينفذ بعد')}</small></div>`).join(''):'<div class="empty">لا توجد عمليات تنفيذ معروضة.</div>'}</div>
      <div class="card"><h3>Audit Timeline</h3>${renderAudit(audit)}</div>`;
  }catch(error){if(error.message!=='unauthorized')workspaceBody.innerHTML='<div class="error">تعذر تحميل لوحة التحكم.</div>';}
}

async function bootstrap(){
  try{
    const response=await api('/ui/api/staff');
    staff=response.items||[];
    if(staffCount)staffCount.textContent=String(staff.length);
    buildHotspots();
    hideLogin();
  }catch(error){if(error.message!=='unauthorized')showLogin('تعذر الاتصال بواجهة MyNemotron.');}
}

bootstrap();

// Full-screen office projection. The image uses object-fit: cover, so the
// visible origin changes with the viewport. Re-project every percentage slot
// from the native 1672x941 daylight image into the covered viewport.
const OFFICE_SOURCE_WIDTH=1672;
const OFFICE_SOURCE_HEIGHT=941;
const teamImage=document.getElementById('teamImage');
const connectionState=document.getElementById('connectionState');
let officeLayoutFrame=0;

function readSlotValue(node,name){return Number.parseFloat(node.style.getPropertyValue(name))||0;}
function layoutOfficeHotspots(){
  cancelAnimationFrame(officeLayoutFrame);
  officeLayoutFrame=requestAnimationFrame(()=>{
    const width=stage.clientWidth;
    const height=stage.clientHeight;
    if(!width||!height)return;
    const scale=Math.max(width/OFFICE_SOURCE_WIDTH,height/OFFICE_SOURCE_HEIGHT);
    const renderedWidth=OFFICE_SOURCE_WIDTH*scale;
    const renderedHeight=OFFICE_SOURCE_HEIGHT*scale;
    const offsetX=(width-renderedWidth)/2;
    const offsetY=(height-renderedHeight)/2;
    stage.querySelectorAll('.hotspot').forEach(button=>{
      const x=readSlotValue(button,'--x')/100*OFFICE_SOURCE_WIDTH;
      const y=readSlotValue(button,'--y')/100*OFFICE_SOURCE_HEIGHT;
      const w=readSlotValue(button,'--w')/100*OFFICE_SOURCE_WIDTH;
      const h=readSlotValue(button,'--h')/100*OFFICE_SOURCE_HEIGHT;
      button.style.left=`${offsetX+x*scale}px`;
      button.style.top=`${offsetY+y*scale}px`;
      button.style.width=`${Math.max(24,w*scale)}px`;
      button.style.height=`${Math.max(24,h*scale)}px`;
    });
  });
}

function setConnectionState(text,connected){
  if(!connectionState)return;
  connectionState.textContent=text;
  connectionState.closest('.system-state')?.classList.toggle('offline',!connected);
}

const stageObserver=new MutationObserver(records=>{
  if(records.some(record=>[...record.addedNodes].some(node=>node.nodeType===1&&node.classList?.contains('hotspot')))){
    setConnectionState('متصل',true);
    layoutOfficeHotspots();
  }
});
stageObserver.observe(stage,{childList:true});

const loginObserver=new MutationObserver(()=>{
  const locked=login.getAttribute('aria-hidden')!=='true';
  if(locked)setConnectionState('يتطلب دخول',false);
});
loginObserver.observe(login,{attributes:true,attributeFilter:['aria-hidden']});

if('ResizeObserver' in window)new ResizeObserver(layoutOfficeHotspots).observe(stage);
window.addEventListener('resize',layoutOfficeHotspots,{passive:true});
window.visualViewport?.addEventListener('resize',layoutOfficeHotspots,{passive:true});
teamImage?.addEventListener('load',layoutOfficeHotspots,{once:false});

layoutOfficeHotspots();