const SLOTS=[
  {x:0,y:53,w:7,h:31,label:'مراقبة العمليات'},
  {x:6,y:56,w:11,h:37,label:'تحليل البيانات'},
  {x:14,y:54,w:11,h:39,label:'تطوير الأنظمة'},
  {x:19,y:39,w:9,h:27,label:'إدارة المشاريع'},
  {x:24,y:41,w:8,h:26,label:'تجربة المستخدم'},
  {x:31,y:40,w:10,h:25,label:'تكامل الأنظمة'},
  {x:41,y:40,w:10,h:26,label:'المحاسبة المالية'},
  {x:27,y:57,w:10,h:37,label:'الأمن السيبراني'},
  {x:36,y:58,w:10,h:38,label:'إدارة المحتوى'},
  {x:56,y:39,w:9,h:27,label:'التحليلات المتقدمة'},
  {x:46,y:57,w:11,h:39,label:'الذكاء الاصطناعي'},
  {x:57,y:58,w:10,h:38,label:'إدارة البنية التحتية'},
  {x:71,y:38,w:10,h:29,label:'إدارة الشبكات'},
  {x:66,y:57,w:10,h:39,label:'التسويات البنكية'},
  {x:78,y:55,w:12,h:39,label:'التقارير المالية'},
  {x:93,y:52,w:7,h:35,label:'دعم العملاء'}
];

const stage=document.getElementById('stage');
const drawer=document.getElementById('drawer');
const workspaceBody=document.getElementById('workspaceBody');
const login=document.getElementById('login');
const loginForm=document.getElementById('loginForm');
const loginError=document.getElementById('loginError');
let zoom=1;
let staff=[];

function esc(value){return String(value??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
function fmtTime(value){if(!value)return '—';try{return new Intl.DateTimeFormat('ar-SA',{dateStyle:'short',timeStyle:'short'}).format(new Date(value));}catch{return String(value);}}
async function api(path,options={}){
  const response=await fetch(path,{credentials:'same-origin',headers:{Accept:'application/json',...(options.headers||{})},...options});
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
function renderTasks(items){if(!items?.length)return '<div class="empty">لا توجد مهام مسندة لهذا الموظف حاليًا.</div>';return items.map(t=>`<div class="task"><div class="task-head"><span class="task-title">${esc(t.title)}</span><span class="task-state">${esc(t.state)}</span></div><small>${esc(t.action)} → ${esc(t.resource)} · risk=${esc(t.risk)}<br>${esc(t.task_id)}</small></div>`).join('');}
function renderAudit(items){if(!items?.length)return '<div class="empty">لا يوجد نشاط حديث لهذا الموظف.</div>';return items.slice(0,20).map(a=>`<div class="audit"><strong>${esc(a.event_type)}</strong><small>${esc(a.subject_type)} / ${esc(a.subject_id)} · ${fmtTime(a.occurred_at)}</small></div>`).join('');}

async function selectStaff(member){
  openDrawer(member.display_name,member.role_name);
  workspaceBody.innerHTML='<div class="loading">جاري تحميل مساحة العمل من Control Plane…</div>';
  try{
    const data=await api(`/ui/api/staff/${encodeURIComponent(member.staff_id)}/workspace`);
    const placement=data.placement||{};
    workspaceBody.innerHTML=`
      ${statusBadge(data.status)}
      <div class="card"><h3>هوية الموظف</h3><dl class="meta">
        <dt>Staff ID</dt><dd>${esc(data.staff_id)}</dd>
        <dt>الدور</dt><dd>${esc(data.role_name)}</dd>
        <dt>المسمى</dt><dd>${esc(placement.job_title||'—')}</dd>
        <dt>القسم</dt><dd>${esc(placement.department_name||'—')}</dd>
        <dt>المدير</dt><dd>${esc(placement.manager_id||'—')}</dd>
        <dt>المنظمة</dt><dd>${esc(placement.organization_name||'—')}</dd>
      </dl></div>
      <div class="card"><h3>الصلاحيات المحكومة</h3>${renderPermissions(data.permissions)}</div>
      <div class="card"><h3>المهام</h3>${renderTasks(data.tasks)}</div>
      <div class="card"><h3>النشاط الحديث</h3>${renderAudit(data.audit)}</div>`;
  }catch(error){if(error.message!=='unauthorized')workspaceBody.innerHTML='<div class="error">تعذر تحميل مساحة العمل من Control Plane.</div>';}
}

function buildHotspots(){
  stage.querySelectorAll('.hotspot').forEach(node=>node.remove());
  SLOTS.forEach((slot,index)=>{
    const member=staff[index]||null;
    const button=document.createElement('button');
    button.type='button';button.className='hotspot'+(member?'':' unassigned');
    button.style.cssText=`--x:${slot.x};--y:${slot.y};--w:${slot.w};--h:${slot.h}`;
    const name=member?member.display_name:'مقعد غير معيّن';
    const role=member?member.role_name:slot.label;
    button.setAttribute('aria-label',`${name} — ${role}`);
    button.innerHTML=`<span class="tag">${esc(name)} · ${esc(role)}</span>`;
    if(member)button.onclick=()=>selectStaff(member);
    else button.onclick=()=>{openDrawer('مقعد غير معيّن',slot.label);workspaceBody.innerHTML='<div class="card"><h3>هذا المقعد غير مربوط</h3><p>لا يوجد StaffMember حقيقي مسجل لهذا الموضع بعد. لن تنشئ الواجهة موظفًا وهميًا تلقائيًا.</p></div>';};
    stage.appendChild(button);
  });
}

async function showOverview(){
  openDrawer('لوحة التحكم','Control Plane — Read only');
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
    buildHotspots();
    hideLogin();
  }catch(error){if(error.message!=='unauthorized')showLogin('تعذر الاتصال بواجهة MyNemotron.');}
}

bootstrap();
