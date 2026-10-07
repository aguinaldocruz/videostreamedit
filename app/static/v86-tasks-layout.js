(function(){
  const setup=$('#setup'),topTabs=setup?.querySelector('.setup-tabs'),panels=setup?.querySelector('.setup-tab-panels');
  const tasksTab=topTabs?.querySelector('[data-setup-tab="queue"]'),tasksPanel=panels?.querySelector('[data-setup-panel="queue"]');
  const taskQueue=tasksPanel?.querySelector('.task-queue-maintenance'),indexes=setup?.querySelector('.index-maintenance');
  if(!tasksTab||!tasksPanel||!taskQueue||!indexes)return;
  tasksTab.textContent='Tasks';
  tasksPanel.classList.add('tasks-panel');
  const shell=document.createElement('div');shell.className='tasks-shell';
  shell.innerHTML='<div class="tasks-tabs" role="tablist" aria-label="Task sections"><button type="button" role="tab" data-tasks-tab="queue">Task Queue</button><button type="button" role="tab" data-tasks-tab="indexes">Indexes</button><button type="button" role="tab" data-tasks-tab="priorities">Priorities</button></div><section data-tasks-panel="queue"></section><section data-tasks-panel="indexes"></section><section data-tasks-panel="priorities" class="queue-priorities-panel"><div class="priority-hero"><div><h3>How should waiting work compete?</h3><p>Choose what the server should prefer when several safe tasks are waiting. Running work and workflow dependencies are never interrupted.</p></div><label class="priority-preset"><span>Starting point</span><select id="queue-priority-preset"><option value="balanced">Balanced (recommended)</option><option value="responsive">Keep browsing responsive</option><option value="background">Analysis in background</option><option value="custom">Custom arrangement</option></select></label></div><div class="priority-explanation"><span>Drag the friendly groups into the order you prefer.</span><span>Top = preferred first · Bottom = when the system is quiet</span></div><div id="queue-priority-list"></div><div class="priority-actions"><button type="button" class="primary" id="queue-priority-save">Save priorities</button><button type="button" id="queue-priority-reset">Restore Balanced</button><small id="queue-priority-status"></small></div><details class="priority-advanced"><summary>What is included in each group?</summary><div id="queue-priority-advanced-list"></div></details></section>';
  tasksPanel.append(shell);shell.querySelector('[data-tasks-panel="queue"]').append(taskQueue);shell.querySelector('[data-tasks-panel="indexes"]').append(indexes);
  // v98 moves the existing schedule controls into Scheduled Tasks after this
  // shell is created. Never delete them before that installer runs.
  function moveIndexSchedules(){
    const target=shell.querySelector('[data-tasks-panel="schedules"] #scheduled-task-cards');
    if(target)indexes.querySelectorAll('.index-schedule,.plex-sync-schedule').forEach(node=>target.append(node));
  }
  indexes.insertAdjacentHTML('beforeend','<section class="index-job-history" data-index-job-history><div class="detector-section-heading"><div><h3>Index jobs</h3><p>Filter the latest jobs by index type and status. Each status keeps its own recent history window.</p></div><div class="index-history-actions"><button type="button" data-index-retry-failed>Retry failed</button><button type="button" class="danger" data-index-delete-failed>Delete failed</button><button type="button" data-index-history-refresh>Refresh</button></div></div><div class="index-history-filters" data-index-history-filters><div class="index-history-filter-group" data-index-type-filters></div><div class="index-history-filter-group" data-index-status-filters></div></div><div data-index-history-list><p class="muted">Loading index jobs…</p></div></section>');
  let indexHistoryTimer=null;
  let indexHistoryResults=[];
  let indexHistoryType='all';
  let indexHistoryStatus='running';
  window.openDashboardIndexFilter=(type='all',status='all')=>{indexHistoryType=type;indexHistoryStatus=status;renderIndexHistory()};
  const indexHistoryList=indexes.querySelector('[data-index-history-list]');
  const indexHistoryTypeFilters=indexes.querySelector('[data-index-type-filters]');
  const indexHistoryStatusFilters=indexes.querySelector('[data-index-status-filters]');
  const indexHistoryRetry=indexes.querySelector('[data-index-retry-failed]');
  const indexHistoryDelete=indexes.querySelector('[data-index-delete-failed]');
  const statusOrder=['running','pending','waiting_cache','blocked_cache','failed','succeeded','cancelled'];
  const statusLabel={running:'Running',pending:'Queued',blocked_cache:'Blocked: cache failed',waiting_cache:'Waiting for cache',failed:'Failed',succeeded:'Completed',cancelled:'Cancelled'};
  const jobLabel={core:'Core stream metadata',subtitles:'Subtitle inspection'};
  const statusCountKey=status=>status==='succeeded'?'completed':status==='pending'?'queued':status;
  function totalStatus(results,status){return results.reduce((total,result)=>total+(status==='waiting_cache'?Math.max(0,Number(result.waiting_cache||0)-Number(result.blocked_cache||0)):Number(result[statusCountKey(status)]||0)),0)}
  function renderIndexHistory(){
    const selected= indexHistoryResults.filter(result=>indexHistoryType==='all'||result.job===indexHistoryType);
    const rows=selected.flatMap(result=>(result.items||[]).map(item=>({...item,job:result.job}))).filter(item=>indexHistoryStatus==='all'||item.status===indexHistoryStatus||item.display_status===indexHistoryStatus).sort((a,b)=>Number(b.id||0)-Number(a.id||0));
    indexHistoryTypeFilters.innerHTML=[['all','All indexes'],['core','Core stream metadata'],['subtitles','Subtitle inspection']].map(([value,label])=>`<button type="button" class="queue-type-filter${indexHistoryType===value?' active':''}" data-index-type="${value}" aria-pressed="${indexHistoryType===value}">${label}</button>`).join('');
    indexHistoryStatusFilters.innerHTML=[['all','All states'],...statusOrder.map(value=>[value,statusLabel[value]])].map(([value,label])=>{const count=value==='all'?selected.reduce((sum,result)=>sum+Number(result.running||0)+Number(result.queued||0)+Number(result.failed||0)+Number(result.completed||0)+Number(result.cancelled||0),0):totalStatus(selected,value);return `<button type="button" class="queue-status-filter${indexHistoryStatus===value?' active':''}" data-index-status="${value}" aria-pressed="${indexHistoryStatus===value}">${label} <span>${count}</span></button>`}).join('');
    indexHistoryTypeFilters.querySelectorAll('[data-index-type]').forEach(button=>button.onclick=()=>{indexHistoryType=indexHistoryType===button.dataset.indexType?'all':button.dataset.indexType;indexHistoryStatus='all';renderIndexHistory()});
    indexHistoryStatusFilters.querySelectorAll('[data-index-status]').forEach(button=>button.onclick=()=>{indexHistoryStatus=indexHistoryStatus===button.dataset.indexStatus?'all':button.dataset.indexStatus;renderIndexHistory()});
    const failedSelected=selected.reduce((sum,result)=>sum+Number(result.failed||0),0);
    indexHistoryRetry.disabled=failedSelected===0; indexHistoryDelete.disabled=failedSelected===0;
    indexHistoryList.innerHTML=rows.length?`<div class="index-history-list-head"><span>Type</span><span>Status</span><span>ID</span><span>Media</span><span>When</span><span></span></div>${rows.map(item=>`<div class="index-history-row"><span class="index-history-kind">${esc(jobLabel[item.job]||item.job)}</span><span class="index-history-state state-${esc(item.display_status==='blocked_cache'?'failed':item.status)}">${esc(item.permanent_failure?'Permanent failure':statusLabel[item.display_status||item.status]||item.status)}</span><span class="index-history-id">#${esc(String(item.id))}</span><span class="index-history-path" title="${attr(item.path||'')}">${esc((item.path||'').split('/').pop()||item.path||'')}</span><span class="index-history-date">${esc(formatAppDate(item.finished_at||item.started_at||item.created_at))}</span>${item.review_path?`<button type="button" data-index-review="${attr(item.review_path)}">Review media</button>`:item.status==='failed'&&!item.permanent_failure?`<button type="button" class="index-history-retry" data-index-retry="${esc(item.job)}" data-index-task-id="${esc(String(item.id))}" title="Retry only this media">Retry</button>`:'<span></span>'}${item.error?`<small class="error">${esc(item.error)}</small>`:''}</div>`).join('')}`:'<p class="muted">No index jobs match the selected filters.</p>';
    indexHistoryList.querySelectorAll('[data-index-review]').forEach(button=>button.onclick=()=>openEditor(button.dataset.indexReview,button.dataset.indexReview.split('/').pop()));
    indexHistoryList.querySelectorAll('[data-index-retry]').forEach(button=>button.onclick=async()=>{button.disabled=true;try{await api(`/api/v80/setup/index/${encodeURIComponent(button.dataset.indexRetry)}/retry/${encodeURIComponent(button.dataset.indexTaskId)}`,{method:'POST',body:'{}'});toast('This failed index item was queued for retry');await loadIndexHistory()}catch(error){button.disabled=false;toast(error.message,true)}});
  }
  async function loadIndexHistory(){try{indexHistoryResults=await Promise.all(['core','subtitles'].map(async job=>({...await api(`/api/v80/setup/index/${job}/status`),job})));renderIndexHistory();clearTimeout(indexHistoryTimer);if(indexHistoryResults.some(result=>Number(result.running||0)>0||Number(result.queued||0)>0))indexHistoryTimer=setTimeout(loadIndexHistory,4000)}catch(error){indexHistoryList.innerHTML=`<p class="error">${esc(error.message)}</p>`}}
  async function indexBulkAction(action){
    const jobs=indexHistoryType==='all'?['core','subtitles']:[indexHistoryType];
    if(!jobs.length)return;
    const label=action==='retry'?'Retry all failed index jobs?':'Delete all failed index jobs?';
    if(!confirm(label))return;
    try{for(const job of jobs)await api(`/api/v80/setup/index/${encodeURIComponent(job)}/${action==='retry'?'retry':'delete-failed'}`,{method:'POST',body:'{}'});toast(action==='retry'?'Failed index jobs queued for retry':'Failed index jobs deleted');await loadIndexHistory()}catch(error){toast(error.message,true)}
  }
  indexHistoryRetry.onclick=()=>indexBulkAction('retry');
  indexHistoryDelete.onclick=()=>indexBulkAction('delete');
  indexes.querySelector('[data-index-history-refresh]').onclick=loadIndexHistory;
  window.refreshIndexJobHistory=loadIndexHistory;
  const heading=indexes.querySelector('.split-index-heading p');
  if(heading)heading.textContent='Ready index work starts immediately. Subtitle inspection waits for cached subtitles; cache failures need review. Queue Check discovers external file changes.';
  const key='videostreamedit.tasks-tab.v1';
  function activate(name){
    if(!shell.querySelector(`[data-tasks-panel="${name}"]`))name='queue';
    shell.querySelectorAll('[data-tasks-tab]').forEach(button=>{const active=button.dataset.tasksTab===name;button.classList.toggle('active',active);button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1});
    shell.querySelectorAll('[data-tasks-panel]').forEach(panel=>panel.classList.toggle('hidden',panel.dataset.tasksPanel!==name));
    localStorage.setItem(key,name);
    window.setTaskQueueVisibility?.(name==='queue'&&!tasksPanel.classList.contains('hidden'),name==='queue');
    if(name==='indexes'){moveIndexSchedules();indexes.querySelector('[data-index-tab].active')?.click();loadIndexHistory()}else if(name==='priorities') loadPriorities();
  }
  const priorityPreset=shell.querySelector('#queue-priority-preset');
  const prioritySave=shell.querySelector('#queue-priority-save');
  const priorityReset=shell.querySelector('#queue-priority-reset');
  const priorityStatus=shell.querySelector('#queue-priority-status');
  const priorityGroups=[
    {id:'user_changes',label:'Changes I requested',short:'Your edits and requested media work',description:'Stream edits, bulk changes, imports, Matroska layout repair, approved subtitle cleanup and reviewed autofix. These are the actions you deliberately asked the application to perform.',tasks:['media_edit','matroska_layout_remux','tv_filtered_stream_edit_batch','tv_filtered_stream_edit_now','filtered_stream_edit_now','tv_filtered_stream_edit','filtered_stream_edit','movie_import','subtitle_html_cleanup','subtitle_autofix']},
    {id:'responsive',label:'Keep browsing responsive',short:'Lightweight organization work',description:'Small index refreshes and coordination work that keeps filters and listings useful without competing aggressively with your requested changes.',tasks:['media_reindex','index_check_prepare']},
    {id:'analysis',label:'Subtitle and audio analysis',short:'Read-only detection and reports',description:'Language, voice, subtitle quality, and report analysis. Useful, but normally safe to let run quietly in the background.',tasks:['audio_language_detection']},
    {id:'heavy',label:'Heavy conversion work',short:'CPU- and storage-intensive work',description:'OCR, graphical subtitle conversion, AAC audio preparation, and large rebuild preparation. These are deliberately placed lower in the balanced order.',tasks:['image_subtitle_convert','review_audio_prepare','index_rebuild_prepare']},
    {id:'sync',label:'External library synchronization',short:'Plex and external catalog work',description:'Plex synchronization and catalog reconciliation. It is protected by fairness rules and never bypasses media safety checks.',tasks:['plex_sync','plex_import_refresh']},
  ];
  const balancedGroupOrder=priorityGroups.map(group=>group.id);
  const priorityByTask=new Map(priorityGroups.flatMap(group=>group.tasks.map(task=>[task,group.id])));
  let priorityValues=[];
  let priorityLoaded=false;
  function groupForTask(task){return priorityByTask.get(task)||'other'}
  function groupedPriorityOrder(){const groups=priorityGroups.map(group=>({...group,tasks:priorityValues.filter(task=>group.tasks.includes(task))}));const other=priorityValues.filter(task=>!priorityByTask.has(task));if(other.length)groups.push({id:'other',label:'Other background work',short:'Additional task types',description:'Additional work introduced by an extension or newer feature.',tasks:other});const position=new Map(priorityValues.map((task,index)=>[task,index]));return groups.sort((a,b)=>{const first=group=>group.tasks.length?Math.min(...group.tasks.map(task=>position.get(task)??999999)):999999;return first(a)-first(b)})}
  function renderPriorityAdvanced(){const root=shell.querySelector('#queue-priority-advanced-list');root.innerHTML=groupedPriorityOrder().map(group=>`<section><strong>${esc(group.label)}</strong><small>${esc(group.tasks.length?group.tasks.join(' · '):'No queued task types currently assigned')}</small></section>`).join('')}
  function renderPriorities(){const root=shell.querySelector('#queue-priority-list');const groups=groupedPriorityOrder();root.innerHTML=groups.map((group,index)=>`<div class="priority-group-card" draggable="true" data-group-id="${esc(group.id)}" data-group-index="${index}"><span class="queue-priority-grip">☰</span><div class="priority-group-copy"><strong>${esc(group.label)}</strong><span>${esc(group.short)}</span></div><span class="priority-group-count" data-priority-count="${esc(group.id)}">${group.tasks.length} task type${group.tasks.length===1?'':'s'}</span><span class="priority-group-help" title="${attr(group.description)}">?</span></div>`).join('');renderPriorityAdvanced();let drag=null;root.querySelectorAll('.priority-group-card').forEach(row=>{row.ondragstart=()=>{drag=row.dataset.groupId;row.classList.add('dragging')};row.ondragend=()=>{drag=null;row.classList.remove('dragging')};row.ondragover=e=>e.preventDefault();row.ondrop=()=>{const target=row.dataset.groupId;if(!drag||drag===target)return;const order=groups.map(group=>group.id),from=order.indexOf(drag),to=order.indexOf(target);if(from<0||to<0)return;const [moved]=order.splice(from,1);order.splice(to,0,moved);const byId=new Map(groups.map(group=>[group.id,group]));priorityValues=order.flatMap(id=>byId.get(id)?.tasks||[]);priorityPreset.value='custom';renderPriorities()}})}
  function flattenedForGroupOrder(order){const groups=groupedPriorityOrder(),byId=new Map(groups.map(group=>[group.id,group]));const used=new Set(order);return order.flatMap(id=>byId.get(id)?.tasks||[]).concat(groups.filter(group=>!used.has(group.id)).flatMap(group=>group.tasks||[]))}
  async function loadPriorities(){const root=shell.querySelector('#queue-priority-list');if(!root)return;root.innerHTML='<p class="muted">Loading priorities…</p>';try{const result=await api('/api/v65/queue/priorities');priorityValues=result.priorities||[];priorityLoaded=true;renderPriorities();const order=groupedPriorityOrder().map(group=>group.id);priorityPreset.value=order.join('|')===balancedGroupOrder.join('|')?'balanced':'custom'}catch(error){root.innerHTML=`<p class="error">${esc(error.message)}</p>`}}
  async function savePriorities(){try{const result=await api('/api/v65/queue/priorities',{method:'PUT',body:JSON.stringify({priorities:priorityValues})});priorityValues=result.priorities||priorityValues;priorityStatus.textContent='Saved';priorityPreset.value='custom';toast('Queue priorities saved')}catch(error){priorityStatus.textContent=error.message;toast(error.message,true)}}
  priorityPreset.onchange=()=>{if(!priorityLoaded)return;const value=priorityPreset.value;const orders={balanced:balancedGroupOrder,responsive:['responsive','user_changes','analysis','sync','heavy'],background:['user_changes','responsive','sync','analysis','heavy']};if(orders[value]){priorityValues=flattenedForGroupOrder(orders[value]);renderPriorities();priorityStatus.textContent='Preview only — press Save priorities to apply'}else priorityStatus.textContent='Custom arrangement — drag the groups below'};
  prioritySave.onclick=savePriorities;
  priorityReset.onclick=()=>{priorityValues=flattenedForGroupOrder(balancedGroupOrder);priorityPreset.value='balanced';priorityStatus.textContent='Balanced order restored locally — press Save priorities to apply';renderPriorities()};

  shell.querySelectorAll('[data-tasks-tab]').forEach(button=>button.onclick=()=>activate(button.dataset.tasksTab));
  shell.querySelector('.tasks-tabs').addEventListener('click',event=>{const button=event.target.closest('[data-tasks-tab]');if(button&&button.dataset.tasksTab!=='queue')window.setTaskQueueVisibility?.(false)},true);
  tasksTab.addEventListener('click',()=>activate(localStorage.getItem(key)||'queue'));
  activate(localStorage.getItem(key)||'queue');
})();
