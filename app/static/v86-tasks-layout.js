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
  const priorityDescriptions={
    media_edit:'Media stream edits. User-ordered changes to audio, subtitles, metadata, ordering, and flags; may remux and should be processed before dependent reindex work.',
    movie_import:'Movie imports and copies. Moves a new file into the Plex-accessible library and applies requested stream edits; long-running and media-mutating.',
    subtitle_html_cleanup:'Subtitle markup cleanup. Removes HTML/formatting tags after user approval; rewrites subtitle content and then refreshes subtitle indexes.',
    image_subtitle_convert:'Image subtitle OCR conversion. Converts graphical subtitles to text; CPU-intensive and staged for rollback before approval.',
    audio_language_detection:'Audio language detection. Samples audio through the language-id service; read-only but can be slow across large media.',
    media_reindex:'Per-media reindex refresh. Updates cached stream/filter metadata after a media change; lightweight and normally high priority.',
    plex_sync:'Plex catalog synchronization. Reads Plex metadata and paths, reconciles additions/removals, and schedules affected index work.',
    index_check_prepare:'Index check preparation. Discovers changed media and submits work to the independent index queues; coordination only.',
    index_rebuild_prepare:'Index rebuild preparation. Clears/repopulates a selected index in staged batches; potentially very large and low priority.',
  };
  function renderPriorities(){const root=shell.querySelector('#queue-priority-list');root.innerHTML=priorityValues.map((value,index)=>{const description=priorityDescriptions[value]||'Background task type. Hover for its purpose; ordering controls when it runs relative to other queued work.';return`<div class="queue-priority-row" draggable="true" data-index="${index}" title="${attr(description)}"><span class="queue-priority-grip">☰</span><strong title="${attr(description)}">${esc(value.replaceAll('_',' '))}</strong><code>${esc(value)}</code></div>`}).join('');let drag=null;root.querySelectorAll('.queue-priority-row').forEach(row=>{row.ondragstart=()=>{drag=Number(row.dataset.index);row.classList.add('dragging')};row.ondragend=()=>row.classList.remove('dragging');row.ondragover=e=>e.preventDefault();row.ondrop=()=>{const target=Number(row.dataset.index);if(drag===null||drag===target)return;const [item]=priorityValues.splice(drag,1);priorityValues.splice(target,0,item);drag=null;renderPriorities();savePriorities()}})}

  shell.querySelectorAll('[data-tasks-tab]').forEach(button=>button.onclick=()=>activate(button.dataset.tasksTab));
  tasksTab.addEventListener('click',()=>activate(localStorage.getItem(key)||'queue'));
  activate(localStorage.getItem(key)||'queue');
})();
