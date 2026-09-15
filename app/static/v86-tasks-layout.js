(function(){
  const setup=$('#setup'),topTabs=setup?.querySelector('.setup-tabs'),panels=setup?.querySelector('.setup-tab-panels');
  const tasksTab=topTabs?.querySelector('[data-setup-tab="queue"]'),tasksPanel=panels?.querySelector('[data-setup-panel="queue"]');
  const taskQueue=tasksPanel?.querySelector('.task-queue-maintenance'),indexes=setup?.querySelector('.index-maintenance');
  if(!tasksTab||!tasksPanel||!taskQueue||!indexes)return;
  tasksTab.textContent='Tasks';
  tasksPanel.classList.add('tasks-panel');
  const shell=document.createElement('div');shell.className='tasks-shell';
  shell.innerHTML='<div class="tasks-tabs" role="tablist" aria-label="Task sections"><button type="button" role="tab" data-tasks-tab="queue">Task Queue</button><button type="button" role="tab" data-tasks-tab="indexes">Indexes</button><button type="button" role="tab" data-tasks-tab="priorities">Priorities</button></div><section data-tasks-panel="queue"></section><section data-tasks-panel="indexes"></section><section data-tasks-panel="priorities" class="queue-priorities-panel"><h3>Queue priorities</h3><p>Drag task types from highest priority at the top to lowest priority at the bottom.</p><div id="queue-priority-list"></div><small id="queue-priority-status"></small></section>';
  tasksPanel.append(shell);shell.querySelector('[data-tasks-panel="queue"]').append(taskQueue);shell.querySelector('[data-tasks-panel="indexes"]').append(indexes);
  const heading=indexes.querySelector('.split-index-heading p');
  if(heading)heading.textContent='Pending index work starts immediately. Queue Check schedules discovery of file changes made outside VideoStreamEdit; optional schedules automate that discovery.';
  const key='videostreamedit.tasks-tab.v1';
  function activate(name){
    if(!shell.querySelector(`[data-tasks-panel="${name}"]`))name='queue';
    shell.querySelectorAll('[data-tasks-tab]').forEach(button=>{const active=button.dataset.tasksTab===name;button.classList.toggle('active',active);button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1});
    shell.querySelectorAll('[data-tasks-panel]').forEach(panel=>panel.classList.toggle('hidden',panel.dataset.tasksPanel!==name));
    localStorage.setItem(key,name);
    if(name==='queue')taskQueue.querySelector('[data-queue-refresh]')?.click();
    else if(name==='indexes') indexes.querySelector('[data-index-tab].active')?.click();else if(name==='priorities') loadPriorities();
  }
  let priorityValues=[];
  async function loadPriorities(){const root=shell.querySelector('#queue-priority-list');if(!root)return;root.innerHTML='<p class="muted">Loading priorities…</p>';try{const result=await api('/api/v65/queue/priorities');priorityValues=result.priorities||[];renderPriorities()}catch(error){root.innerHTML=`<p class="error">${esc(error.message)}</p>`}}
  async function savePriorities(){try{const result=await api('/api/v65/queue/priorities',{method:'PUT',body:JSON.stringify({priorities:priorityValues})});priorityValues=result.priorities||priorityValues;$('#queue-priority-status').textContent='Saved';toast('Queue priorities saved')}catch(error){$('#queue-priority-status').textContent=error.message;toast(error.message,true)}}
  function renderPriorities(){const root=shell.querySelector('#queue-priority-list');root.innerHTML=priorityValues.map((value,index)=>`<div class="queue-priority-row" draggable="true" data-index="${index}"><span class="queue-priority-grip">☰</span><strong>${esc(value.replaceAll('_',' '))}</strong><code>${esc(value)}</code></div>`).join('');let drag=null;root.querySelectorAll('.queue-priority-row').forEach(row=>{row.ondragstart=()=>{drag=Number(row.dataset.index);row.classList.add('dragging')};row.ondragend=()=>row.classList.remove('dragging');row.ondragover=e=>e.preventDefault();row.ondrop=()=>{const target=Number(row.dataset.index);if(drag===null||drag===target)return;const [item]=priorityValues.splice(drag,1);priorityValues.splice(target,0,item);drag=null;renderPriorities();savePriorities()}})}

  shell.querySelectorAll('[data-tasks-tab]').forEach(button=>button.onclick=()=>activate(button.dataset.tasksTab));
  tasksTab.addEventListener('click',()=>activate(localStorage.getItem(key)||'queue'));
  activate(localStorage.getItem(key)||'queue');
})();
