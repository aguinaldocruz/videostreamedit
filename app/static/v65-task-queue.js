(function () {
  const setup=$('#setup'),tabs=setup?.querySelector('.setup-tabs'),panels=setup?.querySelector('.setup-tab-panels');
  if(!setup||!tabs||!panels)return;
  tabs.insertAdjacentHTML('beforeend','<button type="button" role="tab" data-setup-tab="queue">Task queue</button>');
  panels.insertAdjacentHTML('beforeend',`<section data-setup-panel="queue" class="hidden"><article class="task-queue-maintenance"><div class="task-queue-heading"><div><h3>Multi-purpose task queue</h3><p>Persistent background media edits, index updates, and future queued operations.</p></div><div><label class="queue-media-search"><span>Find media</span><input type="search" data-queue-media-search placeholder="Movie, show, episode or path" autocomplete="off"></label><button type="button" data-queue-search-expedite>Run matching sooner</button><button type="button" data-queue-refresh>Refresh</button><button type="button" data-queue-grouped>Workflow groups</button><button type="button" data-queue-delete-all hidden>Delete all</button><button type="button" data-queue-retry-all hidden>Retry all</button><button type="button" data-queue-control>Pause queue</button></div></div><div class="task-queue-summary" data-queue-summary>Loading…</div><div class="task-queue-list" data-queue-list></div></article></section>`);
  const queueTab=tabs.querySelector('[data-setup-tab="queue"]'),mediaSearch=setup.querySelector('[data-queue-media-search]'),searchExpedite=setup.querySelector('[data-queue-search-expedite]'),deleteAll=setup.querySelector('[data-queue-delete-all]'),retryAll=setup.querySelector('[data-queue-retry-all]'),queuePanel=panels.querySelector('[data-setup-panel="queue"]'),list=queuePanel.querySelector('[data-queue-list]'),summary=queuePanel.querySelector('[data-queue-summary]'),control=queuePanel.querySelector('[data-queue-control]');
  let detailDialog=null;
  function taskTypeLabel(value){return String(value||'Task').replaceAll('_',' ').replace(/\b\w/g,letter=>letter.toUpperCase())}
  function statusLabelHuman(value){const labels={pending:'Queued',running:'Running',succeeded:'Completed',failed:'Failed',cancelled:'Cancelled'};return labels[value]||taskTypeLabel(value)}
  function valueLabel(value){return String(value||'').replaceAll('_',' ').replace(/\b\w/g,letter=>letter.toUpperCase())}
  function humanValue(value){
    if(value===null||value===undefined||value==='')return '—';
    if(typeof value==='boolean')return value?'Yes':'No';
    if(Array.isArray(value))return value.length?value.map(item=>humanValue(item)).join(', '):'None';
    if(typeof value==='object')return Object.entries(value).map(([key,item])=>`${valueLabel(key)}: ${humanValue(item)}`).join(' · ');
    return String(value);
  }
  function detailField(label,value,extra=''){return `<div class="task-detail-field"><dt>${esc(label)}</dt><dd${extra?` class="${extra}"`:''}>${esc(humanValue(value))}</dd></div>`}
  function detailSection(title,body,kind=''){return `<section class="task-detail-section${kind?` ${kind}`:''}"><h4>${esc(title)}</h4>${body}</section>`}
  function streamChangeRows(payload){
    const rows=[];
    (payload.tracks||[]).forEach(track=>{const type=taskTypeLabel(track.codec_type||'stream');const number=Number(track.type_index);const fields=[];if(track.language!==undefined)fields.push(`Language → ${humanValue(track.language)}`);if(track.region!==undefined)fields.push(`Region → ${humanValue(track.region)}`);if(track.title!==undefined)fields.push(`Track name → ${humanValue(track.title)}`);rows.push(`<li><strong>${esc(type)} ${Number.isFinite(number)?number+1:''}</strong><span>${esc(fields.join(' · ')||'Properties update requested')}</span></li>`)});
    (payload.external_subtitles||[]).forEach(subtitle=>{const fields=[];if(subtitle.embed)fields.push('Integrate into video');if(subtitle.language!==undefined)fields.push(`Language → ${humanValue(subtitle.language)}`);if(subtitle.region!==undefined)fields.push(`Region → ${humanValue(subtitle.region)}`);if(subtitle.title!==undefined)fields.push(`Track name → ${humanValue(subtitle.title)}`);rows.push(`<li><strong>External subtitle</strong><span>${esc(fields.join(' · ')||'External subtitle operation requested')}</span></li>`)});
    if(payload.remove?.length)rows.push(`<li><strong>Remove streams</strong><span>${esc(payload.remove.length+' stream'+(payload.remove.length===1?'':'s'))}</span></li>`);
    if(payload.order?.length)rows.push(`<li><strong>Stream order</strong><span>${esc(payload.order.map(item=>`${taskTypeLabel(item.codec_type||'stream')} ${Number.isFinite(Number(item.type_index))?Number(item.type_index)+1:''}`).join(' → ')||'Reorder requested')}</span></li>`);
    return rows.length?`<ul class="task-detail-changes">${rows.join('')}</ul>`:'<p class="task-detail-muted">No field-level changes were recorded.</p>';
  }
  function renderTaskDetails(item){
    const payload=item.payload||{},result=item.result||{};
    const progressTotal=Number(item.progress_total||0),progressCurrent=Number(item.progress_current||0),percent=progressTotal?Math.max(0,Math.min(100,Math.round(progressCurrent/progressTotal*100))):null;
    const metadata=detailField('Task type',taskTypeLabel(item.task_type))+detailField('Status',statusLabelHuman(item.status),`status-${item.status}`)+detailField('Created',when(item.created_at))+detailField('Attempts',item.attempts||0);
    const target=payload.path||payload.file||payload.media_path||payload.source_path;
    const plan=detailField('Media',target||item.label||'Not specified')+(payload.filename?detailField('Filename',payload.filename):'')+(payload.mode?detailField('Processing mode',valueLabel(payload.mode)):'');
    const resultEntries=Object.entries(result).filter(([key])=>!['signature','payload','raw'].includes(key));
    const resultHtml=resultEntries.length?`<dl class="task-detail-grid">${resultEntries.map(([key,value])=>detailField(valueLabel(key),value)).join('')}</dl>`:'<p class="task-detail-muted">No completion details recorded yet.</p>';
    const error=item.error?`<div class="task-detail-error"><strong>What needs attention</strong><p>${esc(item.error)}</p></div>`:'';
    const progress=`<div class="task-detail-progress"><div class="task-detail-progress-head"><strong>${esc(item.progress_message||statusLabelHuman(item.status))}</strong><span>${percent===null?'Step in progress':`${percent}% · ${progressCurrent}/${progressTotal}`}</span></div>${percent===null?'':`<div class="task-detail-progress-bar"><span style="width:${percent}%"></span></div>`}</div>`;
    const generalKeys=new Set(['path','file','media_path','source_path','filename','mode','tracks','external_subtitles','remove','order','default_audio','default_subtitle','forced_audio','forced_subtitle']);
    const general=Object.entries(payload).filter(([key])=>!generalKeys.has(key));
    const generalHtml=general.length?`<dl class="task-detail-grid">${general.map(([key,value])=>detailField(valueLabel(key),value)).join('')}</dl>`:'';
    return detailSection('Overview',`<dl class="task-detail-grid">${metadata}</dl>`)+detailSection('Plan',`<dl class="task-detail-grid">${plan}</dl>${streamChangeRows(payload)}${generalHtml}`)+detailSection('Progress',progress)+detailSection('Result',resultHtml)+(error?detailSection('Error',error,'error-section'):'');
  }
  function renderWorkflowDetails(workflow){
    const group=workflow.group||{},stages=workflow.stages||[],artifacts=workflow.artifacts||[];
    const stageHtml=stages.length?`<ol class="task-detail-stages">${stages.map(stage=>`<li class="status-${esc(stage.status)}"><div><strong>Stage ${stage.stage_number}: ${esc(taskTypeLabel(stage.task_type))}</strong><span>${esc(statusLabelHuman(stage.status))} · ${stage.attempts||0} attempt(s)</span></div>${stage.error?`<p>${esc(stage.error)}</p>`:''}</li>`).join('')}</ol>`:'<p class="task-detail-muted">No workflow stages recorded.</p>';
    const artifactHtml=artifacts.length?`<ul class="task-detail-artifacts">${artifacts.map(artifact=>`<li><strong>${esc(statusLabelHuman(artifact.status))}</strong><span>${esc(artifact.original_path||artifact.artifact_path||'Staged artifact')}</span></li>`).join('')}</ul>`:'<p class="task-detail-muted">No staged artifacts.</p>';
    return detailSection('Workflow overview',`<dl class="task-detail-grid">${detailField('Workflow',String(group.group_id||'').slice(0,16)||'—')}${detailField('Type',taskTypeLabel(group.kind))}${detailField('Status',statusLabelHuman(group.status))}${detailField('Current stage',group.current_stage??'—')}</dl>`)+detailSection('Stages',stageHtml)+detailSection('Staged files',artifactHtml);
  }
  async function showTaskDetails(item){
    if(!detailDialog){
      detailDialog=document.createElement('dialog');
      detailDialog.className='task-detail-dialog';
      detailDialog.innerHTML='<div class="dialog-title"><div><h3>Task details</h3><p data-task-detail-summary></p></div><button type="button" class="icon-close" aria-label="Close">×</button></div><div data-task-detail-log class="task-detail-content"></div><div class="dialog-actions"><button type="button" data-task-workflow hidden>Review staged workflow</button><button type="button" data-task-rollback hidden>Rollback staged original</button><button type="button" data-task-detail-close>Close</button></div>';
      document.body.append(detailDialog);
      detailDialog.querySelector('.icon-close').onclick=()=>detailDialog.close();
      detailDialog.querySelector('[data-task-detail-close]').onclick=()=>detailDialog.close();
      detailDialog.querySelector('[data-task-workflow]').onclick=()=>renderWorkflowDialog(detailDialog._workflow);
      detailDialog.querySelector('[data-task-rollback]').onclick=()=>rollbackWorkflow(detailDialog._workflow);
    }
    const payload=item.payload||{},result=item.result||{};
    detailDialog.querySelector('[data-task-detail-summary]').textContent=`#${item.id} · ${taskTypeLabel(item.task_type)} · ${statusLabelHuman(item.status)}`;
    detailDialog.querySelector('[data-task-detail-log]').innerHTML=renderTaskDetails(item);
    const workflowButton=detailDialog.querySelector('[data-task-workflow]'),rollbackButton=detailDialog.querySelector('[data-task-rollback]');
    workflowButton.hidden=true; rollbackButton.hidden=true; detailDialog._workflow=null;
    if(item.group_id){
      try{
        const workflow=await api(`/api/v86/workflows/${encodeURIComponent(item.group_id)}`);
        detailDialog._workflow=workflow; workflowButton.hidden=false;
        rollbackButton.hidden=!['failed','cancelled'].includes(workflow.group?.status)||!(workflow.artifacts||[]).length;
      }catch(error){ console.warn('Workflow detail unavailable',error); }
    }
    detailDialog.showModal();
  }
  function renderWorkflowDialog(workflow){
    if(!workflow||!detailDialog)return;
    const group=workflow.group||{}, stages=workflow.stages||[], artifacts=workflow.artifacts||[];
    detailDialog.querySelector('[data-task-detail-summary]').textContent=`Workflow ${String(group.group_id||'').slice(0,12)} · ${statusLabelHuman(group.status)}`;
    detailDialog.querySelector('[data-task-detail-log]').innerHTML=renderWorkflowDetails(workflow);
    detailDialog.querySelector('[data-task-workflow]').hidden=true;
    detailDialog.querySelector('[data-task-rollback]').hidden=!['failed','cancelled'].includes(group.status)||!artifacts.length;
  }
  async function rollbackWorkflow(workflow){
    const group=workflow?.group?.group_id; if(!group)return;
    if(!confirm('Restore the staged original media for this failed workflow? This is a destructive replacement of the current file.'))return;
    try{
      const result=await api(`/api/v86/workflows/${encodeURIComponent(group)}/rollback`,{method:'POST',body:JSON.stringify({confirm:'ROLLBACK'})});
      toast(`Workflow rolled back · ${result.restored||0} original file(s) restored`); detailDialog.close(); await loadQueue();
    }catch(error){toast(error.message,true)}
  }
  let timer=null,queueVisible=false,activeStatus=null,activeType=null,groupedView=false;
  function when(value){if(!value)return'—';try{return new Date(value).toLocaleString()}catch(_){return value}}
  function statusLabel(item){if(item.status==='running'&&item.progress_total)return`${item.progress_message} · ${item.progress_current}/${item.progress_total}`;return item.progress_message||item.status}
  function renderGroups(groups){
    if(!groups.length){list.innerHTML='<p class="muted">No workflow groups match the current view.</p>';return}
    list.innerHTML='<div class="queue-group-list">'+groups.map(group=>{
      const workflow=group.workflow||{}, stages=workflow.stages||[], artifacts=workflow.artifacts||[], tasks=group.tasks||[];
      const state=workflow.group?.status||((group.running||0)?'running':(group.pending||0)?'pending':(group.failed||0)?'failed':'succeeded');
      const stageHtml=stages.length?stages.map(stage=>'<div class="queue-workflow-stage status-'+esc(stage.status)+'"><b>Stage '+stage.stage_number+' · '+esc(stage.task_type)+'</b><span>'+esc(stage.status)+' · '+(stage.attempts||0)+' attempt(s)</span>'+(stage.error?'<small>'+esc(stage.error)+'</small>':'')+'</div>').join(''):'<p class="muted">No staged records.</p>';
      const taskHtml=tasks.length?'<ul>'+tasks.map(task=>'<li>#'+task.id+' · '+esc(task.task_type.replaceAll('_',' '))+' · '+esc(task.status)+'</li>').join('')+'</ul>':'';
      const artifactHtml=artifacts.length?'<div class="queue-workflow-artifacts"><b>Staged artifacts: '+artifacts.length+'</b></div>':'';
      return '<details class="queue-workflow-group" data-group-id="'+esc(group.group_id)+'"><summary><strong>'+esc(String(group.group_id).slice(0,12))+'…</strong><em class="queue-workflow-state status-'+state+'">'+esc(state)+'</em><span>'+(group.task_count||tasks.length)+' tasks · '+(group.pending||0)+' pending · '+(group.running||0)+' running · '+(group.failed||0)+' failed · '+(group.succeeded||0)+' completed</span><small>'+esc(when(group.updated_at))+'</small></summary><div class="queue-workflow-meta">Created '+esc(when(group.created_at))+' · Updated '+esc(when(group.updated_at))+'</div><div class="queue-workflow-stages">'+stageHtml+'</div>'+taskHtml+artifactHtml+(group.pending?'<button type="button" data-group-expedite>Run pending stages sooner</button>':'')+'</details>';
    }).join('')+'</div>';list.querySelectorAll('[data-group-expedite]').forEach(button=>button.onclick=async()=>{const group=button.closest('[data-group-id]')?.dataset.groupId;if(!group)return;try{const result=await api(`/api/v65/queue/group/${encodeURIComponent(group)}/expedite`,{method:'POST',body:JSON.stringify({minutes:60})});toast(`${result.tasks} pending workflow stage(s) expedited for the next hour`);await loadQueue()}catch(error){toast(error.message,true)}});
  }
  function render(data){
    const counts=data.counts||{};const statusMarkup=`<div class="queue-status-summary"><button type="button" class="queue-status-filter${activeStatus==='running'?' active':''}" data-queue-status="running" aria-pressed="${activeStatus==='running'}">${counts.running||0} running</button><button type="button" class="queue-status-filter${activeStatus==='pending'&&!activeType?' active':''}" data-queue-status="pending" aria-pressed="${activeStatus==='pending'&&!activeType}">${counts.pending||0} pending</button><button type="button" class="queue-status-filter${activeStatus==='failed'?' active':''}" data-queue-status="failed" aria-pressed="${activeStatus==='failed'}">${counts.failed||0} failed</button><button type="button" class="queue-status-filter${activeStatus==='succeeded'?' active':''}" data-queue-status="succeeded" aria-pressed="${activeStatus==='succeeded'}">${counts.succeeded||0} completed</button></div>`;const typeMarkup=`<div class="queue-type-summary"><span class="queue-summary-label">Pending by type</span>${(data.pending_types||[]).map(item=>`<button type="button" class="queue-type-filter${activeType===item.task_type?' active':''}" data-queue-type="${esc(item.task_type)}" aria-pressed="${activeType===item.task_type}">${esc(item.task_type.replace(/_/g,' '))} (${item.count})</button>`).join('')}</div>`;summary.innerHTML=statusMarkup+typeMarkup;const typeGroup=summary.querySelector(".queue-type-summary"),typeLabel=summary.querySelector(".queue-summary-label");if(typeLabel)typeLabel.textContent=data.type_status?`${data.type_status==='succeeded'?'Completed':data.type_status.charAt(0).toUpperCase()+data.type_status.slice(1)} by type`:'';if(typeGroup)typeGroup.hidden=!activeStatus||activeStatus==='running';deleteAll.hidden=!(['pending','failed','succeeded','cancelled'].includes(activeStatus));retryAll.hidden=activeStatus!=='failed';summary.querySelectorAll('[data-queue-status]').forEach(button=>button.onclick=()=>{const status=button.dataset.queueStatus;activeStatus=activeStatus===status?null:status;activeType=null;loadQueue()});summary.querySelectorAll('[data-queue-type]').forEach(button=>button.onclick=()=>{const type=button.dataset.queueType;activeType=activeType===type?null:type;activeStatus=activeType?(activeStatus||'pending'):null;loadQueue()});control.textContent=data.paused?'Resume queue':'Pause queue';control.classList.toggle('primary',data.paused);
    const groupedButton=queuePanel.querySelector("[data-queue-grouped]");if(groupedButton)groupedButton.classList.toggle("primary",groupedView);if(groupedView){renderGroups(data.groups||[]);return}
    const visibleItems=(data.items||[]).filter(item=>(!activeStatus||item.status===activeStatus)&&(!activeType||item.task_type===activeType));
    list.innerHTML=visibleItems.length?visibleItems.map(item=>`<div class="task-queue-item status-${item.status}" data-task-id="${item.id}"><div class="task-queue-state"><strong>#${item.id} · ${esc(item.label)}</strong><button type="button" class="task-status-button" data-task-details>${esc(item.task_type.replaceAll('_',' '))} · ${esc(item.status)}${item.expedited?' · expedited':''}</button></div><div class="task-queue-progress"><strong>${esc(statusLabel(item))}</strong><small>Created ${esc(when(item.created_at))}${item.attempts?` · Attempt ${item.attempts}`:''}</small></div><div class="task-queue-actions">${item.status==='failed'?'<button type="button" data-task-retry>Retry</button>':''}${item.status==='pending'?(item.expedited?'<button type="button" data-task-unexpedite>Cancel expedite</button>':'<button type="button" data-task-expedite>Run sooner</button><button type="button" data-task-cancel>Cancel</button>'):''}${['succeeded','failed','cancelled'].includes(item.status)?'<button type="button" class="danger" data-task-delete>Delete</button>':''}</div></div>`).join(''):'<p class="muted">'+(activeStatus?'No '+activeStatus+' tasks in the current queue view.':'The queue is empty.')+'</p>';
    list.querySelectorAll('[data-task-id]').forEach(row=>{const id=row.dataset.taskId;row.querySelector('[data-task-details]')?.addEventListener('click',()=>{const detail=visibleItems.find(item=>String(item.id)===String(id));if(detail)showTaskDetails(detail)});row.querySelector('[data-task-retry]')?.addEventListener('click',()=>taskAction(id,'retry'));row.querySelector('[data-task-expedite]')?.addEventListener('click',()=>expediteTask(id));row.querySelector('[data-task-unexpedite]')?.addEventListener('click',()=>cancelExpedite(id));row.querySelector('[data-task-cancel]')?.addEventListener('click',()=>taskAction(id,'cancel'));row.querySelector('[data-task-delete]')?.addEventListener('click',()=>deleteTask(id))});
  }
  async function loadQueue(){if(!queueVisible)return;try{const query=[];if(activeStatus)query.push("status="+encodeURIComponent(activeStatus));if(activeType)query.push("task_type="+encodeURIComponent(activeType));if(mediaSearch?.value.trim())query.push("q="+encodeURIComponent(mediaSearch.value.trim()));if(groupedView)query.push("grouped=true");const data=await api("/api/v65/queue"+(query.length?"?"+query.join("&"):""));render(data);clearTimeout(timer);if((data.counts.running||0)||(data.counts.pending||0))timer=setTimeout(loadQueue,2000)}catch(error){summary.textContent=error.message}}
  async function expediteTask(id){try{await api(`/api/v65/queue/${id}/expedite`,{method:'POST',body:JSON.stringify({minutes:60})});toast('Task expedited for the next hour');await loadQueue()}catch(error){toast(error.message,true)}}
  async function cancelExpedite(id){try{await api(`/api/v65/queue/${id}/expedite`,{method:'DELETE'});await loadQueue()}catch(error){toast(error.message,true)}}
  async function taskAction(id,action){try{await api(`/api/v65/queue/${id}/${action}`,{method:'POST',body:'{}'});await loadQueue()}catch(error){toast(error.message,true)}}
  async function deleteTask(id){try{await api(`/api/v65/queue/${id}`,{method:'DELETE'});await loadQueue()}catch(error){toast(error.message,true)}}
  async function bulkAction(action){if(!activeStatus)return;const scope=activeType?` ${activeType.replaceAll("_"," ")}`:"";const label=action==='retry'?`Retry all failed${scope}?`:`Delete all ${activeStatus}${scope}?`;if(!confirm(label))return;try{const result=await api('/api/v65/queue/bulk',{method:'POST',body:JSON.stringify({action,status:activeStatus,task_type:activeType||''})});toast(`${action==='retry'?'Retried':'Deleted'} ${result.count} task(s)${result.skipped?` · ${result.skipped} kept because their workflow group is still in progress`:''}`,Boolean(result.skipped));await loadQueue()}catch(error){toast(error.message,true)}}
  deleteAll.onclick=()=>bulkAction('delete');retryAll.onclick=()=>bulkAction('retry');
  queuePanel.querySelector('[data-queue-refresh]').onclick=loadQueue;
  let searchTimer=null;
  mediaSearch?.addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(loadQueue,250)});
  searchExpedite?.addEventListener('click',async()=>{const query=mediaSearch?.value.trim();if(!query){toast('Enter a movie, show, episode, or path fragment first',true);mediaSearch?.focus();return}if(!confirm(`Run pending tasks matching “${query}” sooner for the next hour?`))return;try{const result=await api('/api/v65/queue/expedite-matching',{method:'POST',body:JSON.stringify({query,minutes:60})});toast(`${result.tasks} matching pending task(s) expedited for the next hour`);await loadQueue()}catch(error){toast(error.message,true)}});
  queuePanel.querySelector("[data-queue-grouped]").onclick=()=>{groupedView=!groupedView;loadQueue()};
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
