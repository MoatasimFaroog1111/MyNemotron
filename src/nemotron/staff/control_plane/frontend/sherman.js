const SHERMAN_ID='staff-sherman-trainer';
const SHERMAN_ACCEPT='.zip,.rar,.tar,.tar.gz,.tgz';
const SHERMAN_STATES=['Uploading','Scanning','Validated','Awaiting Approval','Active','Rejected','Unsupported','Malformed','Unsafe','Duplicate'];
const shermanRecommendations=new Map();

SLOTS.push({x:42.2,y:12.4,w:10.2,h:13.5,label:'مدرب المهارات',staffId:SHERMAN_ID});

const baseRenderWorkspace=renderWorkspace;
renderWorkspace=function(member,data){
  if(member.staff_id!==SHERMAN_ID){baseRenderWorkspace(member,data);return;}
  renderShermanWorkspace(member,data);
};

const baseBuildHotspots=buildHotspots;
buildHotspots=function(){
  baseBuildHotspots();
  decorateShermanHotspot();
};

function decorateShermanHotspot(){
  stage.querySelectorAll('.hotspot').forEach(button=>{
    if((button.getAttribute('aria-label')||'').includes('الشيرمان'))button.classList.add('sherman-hotspot');
  });
}

function trainingState(state,detail=''){
  const node=document.getElementById('shermanTrainingState');
  if(!node)return;
  node.dataset.state=state;
  node.innerHTML=`<strong>${esc(state)}</strong>${detail?`<span>${esc(detail)}</span>`:''}`;
}

function renderShermanWorkspace(member,data){
  workspaceBody.innerHTML=`
    ${statusBadge(data.status)}
    <div class="card sherman-hero">
      <span class="drawer-kicker">SKILL TRAINING CENTER</span>
      <h3>مركز تدريب الموظفين · الشيرمان</h3>
      <p>ارفع حزمة مهارات من GitHub. تُفحص كبيانات خاملة، وتُفصل كل مهارة مستقلة، ثم يقترح الشيرمان الموظفين المناسبين. لا يبدأ التدريب إلا بعد اعتمادك.</p>
      <div class="sherman-state" id="shermanTrainingState" data-state="Awaiting Approval"><strong>Awaiting Approval</strong><span>جاهز لاستقبال حزمة مهارات.</span></div>
      <label class="sherman-drop-zone" id="sherman-drop-zone" for="skillUpload" tabindex="0">
        <strong>اسحب الملف هنا أو اضغط للاختيار</strong>
        <span>ZIP · RAR · TAR · TAR.GZ · TGZ</span>
        <small>7z غير مدعوم حاليًا ويُرفض بأمان دون تشغيل محتواه.</small>
        <input id="skillUpload" type="file" accept="${SHERMAN_ACCEPT}">
      </label>
      <div class="sherman-security">لا يتم تشغيل أي Python / Shell / EXE أو install hook أثناء الاستيراد أو التدريب.</div>
    </div>
    <div class="card"><div class="sherman-list-head"><h3>المهارات المسجلة</h3><button class="tool" id="refreshShermanSkills" type="button">تحديث</button></div><div id="shermanSkillList"><div class="loading">جاري تحميل سجل المهارات…</div></div></div>
    <div class="card"><h3>صلاحيات الشيرمان</h3>${renderPermissions(data.permissions)}</div>`;

  const input=document.getElementById('skillUpload');
  const drop=document.getElementById('sherman-drop-zone');
  input.addEventListener('change',()=>{if(input.files?.[0])uploadSkillFile(input.files[0]);});
  drop.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();input.click();}});
  for(const eventName of ['dragenter','dragover'])drop.addEventListener(event=>{event.preventDefault();drop.classList.add('dragging');});
  for(const eventName of ['dragleave','drop'])drop.addEventListener(event=>{event.preventDefault();drop.classList.remove('dragging');});
  drop.addEventListener('drop',event=>{const file=event.dataTransfer?.files?.[0];if(file)uploadSkillFile(file);});
  document.getElementById('refreshShermanSkills').onclick=refreshShermanSkills;
  refreshShermanSkills();
}

async function uploadSkillFile(file){
  trainingState('Uploading',file.name);
  try{
    trainingState('Scanning','فحص النوع والمحتوى والمسارات والحدود الأمنية…');
    const result=await api(`/ui/api/skills/import?filename=${encodeURIComponent(file.name)}`,{
      method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file
    });
    trainingState('Validated',`تم اكتشاف ${result.skills?.length||0} مهارة مستقلة.`);
    await refreshShermanSkills();
    trainingState('Awaiting Approval','راجع المقترحين ثم اعتمد التدريب.');
  }catch(error){
    if(error.message==='unauthorized')return;
    const state=error.status===400?'Unsafe':'Rejected';
    trainingState(state,'تم رفض الحزمة بأمان ولم يتم تفعيل أي مهارة.');
  }
}

async function refreshShermanSkills(){
  const list=document.getElementById('shermanSkillList');
  if(!list)return;
  try{
    const result=await api('/ui/api/skills');
    const items=result.items||[];
    if(!items.length){list.innerHTML='<div class="empty">لم تُرفع أي مهارة بعد.</div>';return;}
    list.innerHTML=items.map(skill=>skillCard(skill)).join('');
    bindSkillActions();
    await Promise.all(items.map(skill=>loadRecommendation(skill.version_id)));
    renderRecommendationLabels();
  }catch(error){if(error.message!=='unauthorized')list.innerHTML='<div class="error">تعذر تحميل سجل المهارات.</div>';}
}

function skillCard(skill){
  const isActive=skill.status==='active';
  const candidates=staff.filter(member=>member.staff_id!==SHERMAN_ID&&member.status==='active');
  return `<article class="sherman-skill-card" data-version-id="${esc(skill.version_id)}">
    <div class="skill-card-head"><div><strong>${esc(skill.name)}</strong><small>${esc(skill.skill_id)} · ${esc(skill.source_path)}</small></div><span class="skill-state ${isActive?'active':''}">${isActive?'Active':'Awaiting Approval'}</span></div>
    <p>${esc(skill.description)}</p>
    <div class="skill-digest">SHA-256 · ${esc(skill.content_sha256)}</div>
    <div class="recommendation" data-recommendation="${esc(skill.version_id)}">جاري تحليل الموظفين المناسبين…</div>
    <details><summary>اختيار يدوي للموظفين</summary><div class="staff-choice-grid">${candidates.map(member=>`<label><input type="checkbox" value="${esc(member.staff_id)}"> <span>${esc(member.display_name)}</span><small>${esc(member.role_name)}</small></label>`).join('')}</div></details>
    <div class="actions sherman-actions">
      <button class="primary" type="button" data-train="suggested">تدريب المقترحين</button>
      <button class="tool" type="button" data-train="all">تدريب الجميع</button>
      <button class="tool" type="button" data-train="selected">تدريب المحددين</button>
    </div>
  </article>`;
}

async function loadRecommendation(versionId){
  try{
    const result=await api(`/ui/api/skills/${encodeURIComponent(versionId)}/recommendations`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:'{}'
    });
    shermanRecommendations.set(versionId,result);
  }catch(error){if(error.message!=='unauthorized')shermanRecommendations.set(versionId,{staff_ids:[],confidence:0,rationale:'manual'});}
}

function renderRecommendationLabels(){
  document.querySelectorAll('[data-recommendation]').forEach(node=>{
    const versionId=node.dataset.recommendation;
    const recommendation=shermanRecommendations.get(versionId);
    if(!recommendation)return;
    const names=(recommendation.staff_ids||[]).map(id=>staff.find(member=>member.staff_id===id)?.display_name||id);
    node.innerHTML=names.length
      ?`<strong>المقترحون:</strong> ${names.map(esc).join('، ')} <small>confidence ${Math.round((recommendation.confidence||0)*100)}%</small>`
      :'<strong>لا توجد توصية واثقة.</strong> اختر الموظفين يدويًا.';
  });
}

function bindSkillActions(){
  document.querySelectorAll('.sherman-skill-card').forEach(card=>{
    card.querySelectorAll('[data-train]').forEach(button=>button.addEventListener('click',()=>trainSkill(card,button.dataset.train)));
  });
}

async function trainSkill(card,mode){
  const versionId=card.dataset.versionId;
  const selected=[...card.querySelectorAll('input[type="checkbox"]:checked')].map(input=>input.value);
  if(mode==='selected'&&!selected.length){trainingState('Awaiting Approval','حدد موظفًا واحدًا على الأقل.');return;}
  trainingState('Awaiting Approval','جاري تنفيذ اعتماد التدريب المحكوم…');
  card.querySelectorAll('button').forEach(button=>button.disabled=true);
  try{
    const result=await api(`/ui/api/skills/${encodeURIComponent(versionId)}/assignments`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode,staff_ids:selected})
    });
    trainingState('Active',`تم تدريب ${result.staff_ids.length} موظف/موظفين والتحقق من التعيين.`);
    await refreshShermanSkills();
  }catch(error){
    if(error.message!=='unauthorized')trainingState('Rejected','لم يُحفظ التدريب؛ راجع الاختيارات أو توصية الشيرمان.');
    card.querySelectorAll('button').forEach(button=>button.disabled=false);
  }
}

// Contract vocabulary shown in validation/rejection flows: Unsupported · Malformed · Duplicate.
void SHERMAN_STATES;
decorateShermanHotspot();
