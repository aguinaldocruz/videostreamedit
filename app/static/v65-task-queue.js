(function () {
  const setup=$('#setup'),tabs=setup?.querySelector('.setup-tabs'),panels=setup?.querySelector('.setup-tab-panels');
  if(!setup||!tabs||!panels)return;
  tabs.insertAdjacentHTML('beforeend','<button type="button" role="tab" data-setup-tab="queue">Task queue</button>');
  panels.insertAdjacentHTML('beforeend',`<section data-setup-panel="queue" class="hidden"><article class="task-queue-maintenance"><div class="task-queue-heading"><div><h3>Multi-purpose task queue</h3><p>Persistent background media edits, index updates, and future queued operations.</p></div><div><button type="button" data-queue-refresh>Refresh</button><button type="button" data-queue-delete-all hidden>Delete all</button><button type="button" data-queue-retry-all hidden>Retry all</button><button type="button" data-queue-control>Pause queue</button></div></div><div class="task-queue-summary" data-queue-summary>Loading…</div><div class="task-queue-list" data-queue-list></div></article></section>`);
  const queueTab=tabs.querySelector('[data-setup-tab="queue"]'),deleteAll=setup.querySelector('[data-queue-delete-all]'),retryAll=setup.querySelector('[data-queue-retry-all]'),queuePanel=panels.querySelector('[data-setup-panel="queue"]'),list=queuePanel.querySelector('[data-queue-list]'),summary=queuePanel.querySelector('[data-queue-summary]'),control=queuePanel.querySelector('[data-queue-control]');
  let timer=null,queueVisible=false,activeStatus=null,activeType=null;
  function when(value){if(!value)return'—';try{return new Date(value).toLocaleString()}catch(_){return value}}
  function statusLabel(item){if(item.status==='running'&&item.progress_total)return`${item.progress_message} · ${item.progress_current}/${item.progress_total}`;return item.progress_message||item.status}
  function render(data){
    const counts=data.counts||{};const statusMarkup=`<div class="queue-status-summary"><button type="button" class="queue-status-filter${activeStatus==='running'?' active':''}" data-queue-status="running" aria-pressed="${activeStatus==='running'}">${counts.running||0} running</button><button type="button" class="queue-status-filter${activeStatus==='pending'&&!activeType?' active':''}" data-queue-status="pending" aria-pressed="${activeStatus==='pending'&&!activeType}">${counts.pending||0} pending</button><button type="button" class="queue-status-filter${activeStatus==='failed'?' active':''}" data-queue-status="failed" aria-pressed="${activeStatus==='failed'}">${counts.failed||0} failed</button><button type="button" class="queue-status-filter${activeStatus==='succeeded'?' active':''}" data-queue-status="succeeded" aria-pressed="${activeStatus==='succeeded'}">${counts.succeeded||0} completed</button></div>`;const typeMarkup=`<div class="queue-type-summary"><span class="queue-summary-label">Pending by type</span>${(data.pending_types||[]).map(item=>`<button type="button" class="queue-type-filter${activeType===item.task_type?' active':''}" data-queue-type="${esc(item.task_type)}" aria-pressed="${activeType===item.task_type}">${esc(item.task_type.replace(/_/g,' '))} (${item.count})</button>`).join('')}</div>`;summary.innerHTML=statusMarkup+typeMarkup;const typeGroup=summary.querySelector(".queue-type-summary"),typeLabel=summary.querySelector(".queue-summary-label");if(typeLabel)typeLabel.textContent=data.type_status?`${data.type_status==='succeeded'?'Completed':data.type_status.charAt(0).toUpperCase()+data.type_status.slice(1)} by type`:'';if(typeGroup)typeGroup.hidden=!activeStatus||activeStatus==='running';deleteAll.hidden=!(['pending','failed','succeeded','cancelled'].includes(activeStatus));retryAll.hidden=activeStatus!=='failed';summary.querySelectorAll('[data-queue-status]').forEach(button=>button.onclick=()=>{const status=button.dataset.queueStatus;activeStatus=activeStatus===status?null:status;activeType=null;loadQueue()});summary.querySelectorAll('[data-queue-type]').forEach(button=>button.onclick=()=>{const type=button.dataset.queueType;activeType=activeType===type?null:type;activeStatus=activeType?(activeStatus||'pending'):null;loadQueue()});control.textContent=data.paused?'Resume queue':'Pause queue';control.classList.toggle('primary',data.paused);
    const visibleItems=(data.items||[]).filter(item=>(!activeStatus||item.status===activeStatus)&&(!activeType||item.task_type===activeType));
    list.innerHTML=visibleItems.length?visibleItems.map(item=>`<div class="task-queue-item status-${item.status}" data-task-id="${item.id}"><div class="task-queue-state"><strong>#${item.id} · ${esc(item.label)}</strong><span>${esc(item.task_type.replaceAll('_',' '))} · ${esc(item.status)}</span></div><div class="task-queue-progress"><strong>${esc(statusLabel(item))}</strong><small>Created ${esc(when(item.created_at))}${item.attempts?` · Attempt ${item.attempts}`:''}</small>${item.error?`<details><summary>Error details</summary><pre>${esc(item.error)}</pre></details>`:''}</div><div class="task-queue-actions">${item.status==='failed'?'<button type="button" data-task-retry>Retry</button>':''}${item.status==='pending'?'<button type="button" data-task-cancel>Cancel</button>':''}${['succeeded','failed','cancelled'].includes(item.status)?'<button type="button" class="danger" data-task-delete>Delete</button>':''}</div></div>`).join(''):'<p class="muted">'+(activeStatus?'No '+activeStatus+' tasks in the current queue view.':'The queue is empty.')+'</p>';
    list.querySelectorAll('[data-task-id]').forEach(row=>{const id=row.dataset.taskId;row.querySelector('[data-task-retry]')?.addEventListener('click',()=>taskAction(id,'retry'));row.querySelector('[data-task-cancel]')?.addEventListener('click',()=>taskAction(id,'cancel'));row.querySelector('[data-task-delete]')?.addEventListener('click',()=>deleteTask(id))});
  }
  async function loadQueue(){if(!queueVisible)return;try{const data=await api('/api/v65/queue'+(activeStatus||activeType?'?'+(activeStatus?'status='+encodeURIComponent(activeStatus):'')+(activeStatus&&activeType?'&':'')+(activeType?'task_type='+encodeURIComponent(activeType):''):''));render(data);clearTimeout(timer);if((data.counts.running||0)||(data.counts.pending||0))timer=setTimeout(loadQueue,2000)}catch(error){summary.textContent=error.message}}
  async function taskAction(id,action){try{await api(`/api/v65/queue/${id}/${action}`,{method:'POST',body:'{}'});await loadQueue()}catch(error){toast(error.message,true)}}
  async function deleteTask(id){try{await api(`/api/v65/queue/${id}`,{method:'DELETE'});await loadQueue()}catch(error){toast(error.message,true)}}
  async function bulkAction(action){if(!activeStatus)return;const scope=activeType?` ${activeType.replaceAll("_"," ")}`:"";const label=action==='retry'?`Retry all failed${scope}?`:`Delete all ${activeStatus}${scope}?`;if(!confirm(label))return;try{const result=await api('/api/v65/queue/bulk',{method:'POST',body:JSON.stringify({action,status:activeStatus,task_type:activeType||''})});toast(`${action==='retry'?'Retried':'Deleted'} ${result.count} task(s)`);await loadQueue()}catch(error){toast(error.message,true)}}
  deleteAll.onclick=()=>bulkAction('delete');retryAll.onclick=()=>bulkAction('retry');
  queuePanel.querySelector('[data-queue-refresh]').onclick=loadQueue;
  control.onclick=async()=>{try{const action=control.textContent.startsWith('Resume')?'resume':'pause';await api('/api/v65/queue/control',{method:'PUT',body:JSON.stringify({action})});await loadQueue()}catch(error){toast(error.message,true)}};
  queueTab.onclick=()=>{queueVisible=true;tabs.querySelectorAll('[data-setup-tab]').forEach(button=>{const active=button===queueTab;button.classList.toggle('active',active);button.setAttribute('aria-selected',String(active))});panels.querySelectorAll('[data-setup-panel]').forEach(panel=>panel.classList.toggle('hidden',panel!==queuePanel));localStorage.setItem('videostreamedit.setup-tab.v1','queue');localStorage.setItem('videostreamedit.queue-tab-active','1');loadQueue()};
  tabs.querySelectorAll('[data-setup-tab]:not([data-setup-tab="queue"])').forEach(button=>button.addEventListener('click',()=>{queueVisible=false;clearTimeout(timer);localStorage.removeItem('videostreamedit.queue-tab-active')}));
  if(localStorage.getItem('videostreamedit.queue-tab-active')==='1')queueTab.click();

  function collectQueuedEdit(){
    const tracks=[],external_subtitles=[],order=[],remove=[];
    document.querySelectorAll('#stream-content .stream-row').forEach(row=>{
      if(row.querySelector('[name=remove]')?.checked)remove.push(row.dataset.key);
      const language=row.querySelector('[name=language]'),region=row.querySelector('[name=region]'),title=row.querySelector('[name=title]');
      if(row.dataset.external==='true'){
        external_subtitles.push({path:row.dataset.path,embed:row.querySelector('[name=embed]').checked,language:language.value,region:region.value,title:title.value,forced:false});
        order.push({source:'external',codec_type:'subtitle',path:row.dataset.path});
      }else{
        const update={codec_type:row.dataset.codecType,type_index:Number(row.dataset.typeIndex)};
        if(language.dataset.dirty==='true'||region.dataset.dirty==='true'){update.language=language.value;update.region=region.value}if(title.dataset.dirty==='true')update.title=title.value;
        tracks.push(update);order.push({source:'embedded',codec_type:row.dataset.codecType,type_index:Number(row.dataset.typeIndex)});
      }
    });
    const selected=name=>document.querySelector(`#stream-content [name=${name}]:checked`)?.value||null,default_subtitle=selected('default-subtitle'),forced_subtitle=selected('forced-subtitle');
    for(const choice of[default_subtitle,forced_subtitle])if(choice?.startsWith('external:')){const item=external_subtitles.find(value=>`external:${value.path}`===choice);if(item)item.embed=true}
    const edit={path:state.selectedPath,tracks,external_subtitles,order,default_audio:selected('default-audio'),forced_audio:selected('forced-audio'),default_subtitle,forced_subtitle,remove};
    const filename=typeof validateStreamFilename==='function'?validateStreamFilename():'';
    return{edit,filename};
  }
  window.collectCompleteQueuedEdit=collectQueuedEdit;
  async function queuePendingChanges(){
    try{const payload=collectQueuedEdit(),label=$('#selected-file')?.textContent||payload.edit.path;await api('/api/v65/queue',{method:'POST',body:JSON.stringify({task_type:'media_edit',payload,label:`Edit ${label}`})});toast('Media changes added to the task queue');$('#move-without-applying').click()}catch(error){toast(error.message,true)}
  }
  const dialogObserver=new MutationObserver(()=>{const dialog=$('#pending-navigation-dialog');if(!dialog||dialog.querySelector('[data-queue-pending]'))return;const move=$('#move-without-applying');move.insertAdjacentHTML('beforebegin','<button type="button" data-queue-pending>Queue changes and move</button>');dialog.querySelector('[data-queue-pending]').onclick=queuePendingChanges});
  dialogObserver.observe(document.body,{childList:true,subtree:true});

  const queuedApi=api;
  api=async function(resource,options){
    if(resource==='/api/v39/movies/stream-filter-refresh'||resource==='/api/v38/movies/stream-filter-invalidate'||resource==='/api/v57/index/media-refresh'||resource==='/api/v54/index/invalidate'){
      const payload=JSON.parse(options?.body||'{}');
      if(payload.path)await queuedApi('/api/v80/index/request',{method:'POST',body:JSON.stringify({path:payload.path,indexes:payload.indexes||['core','subtitles','previews'],reason:'Media changed outside generic queue'})});
      return{indexed:true,queued:true,path:payload.path};
    }
    return queuedApi(resource,options);
  };
})();
