(function(){
  const setup=document.querySelector('#setup');
  if(!setup)return;
  function install(){
    const shell=setup.querySelector('.tasks-shell'),tabs=shell?.querySelector('.tasks-tabs'),panels=shell?.querySelectorAll('[data-tasks-panel]');
    if(!shell||!tabs||!panels||tabs.querySelector('[data-tasks-tab="schedules"]'))return Boolean(shell);
    const indexTab=tabs.querySelector('[data-tasks-tab="indexes"]'),priorityTab=tabs.querySelector('[data-tasks-tab="priorities"]'),indexPanel=shell.querySelector('[data-tasks-panel="indexes"]'),priorityPanel=shell.querySelector('[data-tasks-panel="priorities"]');
    if(!indexTab||!priorityTab||!indexPanel||!priorityPanel)return Boolean(shell);
    const tab=document.createElement('button');tab.type='button';tab.role='tab';tab.dataset.tasksTab='schedules';tab.textContent='Scheduled Tasks';tabs.insertBefore(tab,priorityTab);
    const panel=document.createElement('section');panel.dataset.tasksPanel='schedules';panel.innerHTML='<article class="scheduled-tasks-panel"><div class="detector-section-heading"><div><h3>Scheduled Tasks</h3><p>Configure recurring discovery and advisory detection. User-ordered edits and required index dependencies still run immediately.</p></div><span class="detector-badge">Background only</span></div><div id="scheduled-task-cards"></div><small id="scheduled-task-status" class="muted"></small></article>';
    panel.classList.add('hidden');
    shell.insertBefore(panel,priorityPanel);
    const cards=panel.querySelector('#scheduled-task-cards'),status=panel.querySelector('#scheduled-task-status');
    const indexCopy={core:['Core stream metadata','Discover added or externally changed media for stream metadata indexing.'],subtitles:['Subtitle inspection','Discover media whose subtitle analysis needs refreshing.']};
    setup.querySelectorAll('.index-schedule,.plex-sync-schedule').forEach(node=>{
      const card=document.createElement('section');card.className='scheduled-task-card schedule-discovery-card';
      if(node.classList.contains('plex-sync-schedule')){
        card.innerHTML='<div class="schedule-card-heading"><div><strong>Plex library sync</strong><small>Check Plex for new or changed movies and episodes.</small></div></div>';
      }else{
        const job=node.closest('[data-index-job]')?.dataset.indexJob;
        const [title,description]=indexCopy[job]||['Media index check','Discover new or changed media for indexing.'];
        card.innerHTML=`<div class="schedule-card-heading"><div><strong>${title}</strong><small>${description}</small></div></div>`;
      }
      card.append(node);cards.append(card);
    });
    const jobs=[['subtitle_detection','Subtitle language detection','Check subtitle-language findings in the background.'],['voice_detection','Voice language detection','Check audio-language findings in the background.']];
    const labels={disabled:'Disabled',daily:'Daily',every_other_day:'Every other day',weekly:'Weekly'};
    function describe(v){return v.frequency==='disabled'?'Disabled':`${labels[v.frequency]||v.frequency} · next ${v.next_run?formatAppDate(v.next_run)+' · '+appTimezone():'scheduled'}`}
    cards.insertAdjacentHTML('beforeend',jobs.map(([job,label,description])=>`<section class="scheduled-task-card schedule-detection-card" data-scheduled-job="${job}"><div class="schedule-card-heading"><div><strong>${label}</strong><small>${description}</small></div></div><div class="schedule-card-controls"><label>Frequency<select data-frequency><option value="disabled">Disabled</option><option value="daily">Daily</option><option value="every_other_day">Every other day</option><option value="weekly">Weekly</option></select></label><label>Time<input type="time" data-time value="03:00"></label><button type="button" data-save>Save schedule</button></div><small class="schedule-card-status" data-scheduled-status role="status">Loading…</small></section>`).join(''));
    cards.insertAdjacentHTML('beforeend',`<div class="scheduled-task-card subtitle-cache-card" data-subtitle-cache>
      <div class="subtitle-cache-heading"><strong>Cache subtitles</strong><small>Complete text subtitles only · Final Revision first, then newest media</small></div>
      <div class="subtitle-cache-controls">
        <label>Frequency<select data-cache-frequency><option value="disabled">Disabled</option><option value="daily">Daily</option><option value="every_other_day">Every other day</option><option value="weekly">Weekly</option></select></label>
        <label>Start time<input type="time" data-cache-time value="03:00"></label>
        <label>Run limit · hours<input type="number" data-cache-hours min="0" max="168" value="1"></label>
        <label>Minutes<input type="number" data-cache-minutes min="0" max="59" value="0"></label>
        <button type="button" data-cache-save>Save</button><button type="button" data-cache-run>Run now</button><button type="button" data-cache-stop disabled>Stop gracefully</button>
      </div><p data-cache-status role="status">Loading cache schedule…</p>
      <div class="subtitle-cache-progress-row"><small data-cache-progress></small><button type="button" data-cache-failures>Failed media</button></div>
      <small data-cache-current hidden></small>
    </div>`);
    cards.querySelectorAll('[data-scheduled-job]').forEach(card=>{const job=card.dataset.scheduledJob,frequency=card.querySelector('[data-frequency]'),time=card.querySelector('[data-time]'),message=card.querySelector('[data-scheduled-status]');
      async function load(){try{const v=await api(`/api/v67/setup/index/${job}/schedule`);frequency.value=v.frequency;time.value=v.time;time.disabled=v.frequency==='disabled';message.textContent=describe(v)}catch(e){message.textContent=e.message}}
      frequency.onchange=()=>time.disabled=frequency.value==='disabled';card.querySelector('[data-save]').onclick=async()=>{try{const v=await api(`/api/v67/setup/index/${job}/schedule`,{method:'PUT',body:JSON.stringify({frequency:frequency.value,time:time.value||'03:00'})});message.textContent=describe(v);toast(`${job.replaceAll('_',' ')} schedule saved`)}catch(e){status.textContent=e.message;toast(e.message,true)}};load();
    });
    const cacheCard=cards.querySelector('[data-subtitle-cache]');
    const cacheFrequency=cacheCard.querySelector('[data-cache-frequency]'),cacheTime=cacheCard.querySelector('[data-cache-time]'),cacheHours=cacheCard.querySelector('[data-cache-hours]'),cacheMinutes=cacheCard.querySelector('[data-cache-minutes]');
    const cacheStatus=cacheCard.querySelector('[data-cache-status]'),cacheProgress=cacheCard.querySelector('[data-cache-progress]'),cacheCurrent=cacheCard.querySelector('[data-cache-current]'),cacheFailures=cacheCard.querySelector('[data-cache-failures]'),cacheRun=cacheCard.querySelector('[data-cache-run]'),cacheStop=cacheCard.querySelector('[data-cache-stop]');
    const failureDialog=document.createElement('dialog');failureDialog.className='subtitle-cache-failure-dialog';failureDialog.innerHTML='<header><h3>Failed subtitle caches</h3><div><button type="button" data-retry-all>Retry all</button> <button type="button" data-close aria-label="Close">Close</button></div></header><p>Failures stay here until the media changes or you retry them. A detected change clears the failure and queues a fresh attempt.</p><div data-failure-list>Loading…</div>';
    document.body.append(failureDialog);
    failureDialog.querySelector('[data-close]').onclick=()=>failureDialog.close();
    failureDialog.querySelector('[data-retry-all]').onclick=async()=>{if(!confirm('Requeue all recorded subtitle-cache failures for the next run?'))return;const button=failureDialog.querySelector('[data-retry-all]');button.disabled=true;try{const result=await api('/api/subtitle-cache/failures/retry',{method:'POST'});toast(`${result.queued} failed media queued for the next cache run`);failureDialog.close();await loadCacheSchedule()}catch(error){toast(error.message,true)}finally{button.disabled=false}};
    cacheFailures.onclick=async()=>{failureDialog.showModal();const list=failureDialog.querySelector('[data-failure-list]');list.textContent='Loading…';try{const data=await api('/api/subtitle-cache/failures');list.innerHTML=data.items.length?data.items.map(item=>{const code=item.kind==='episode'?`S${String(item.season_number??0).padStart(2,'0')}E${String(item.episode_number??0).padStart(2,'0')}`:'';const label=[item.show_title,code,item.title].filter(Boolean).join(' · ')||item.path.split('/').pop();return `<article class="subtitle-cache-failure"><button type="button" data-path="${attr(item.path)}" data-label="${attr(label)}">${esc(label)}</button><small>${esc(item.path)}</small><p>${esc(item.error)}</p><button type="button" data-retry-path="${attr(item.path)}">Retry this media next run</button></article>`}).join(''):'<p>No individually recorded failures remain. Older run totals may predate per-media tracking.</p>';list.querySelectorAll('[data-path]').forEach(button=>button.onclick=async()=>{failureDialog.close();await openEditor(button.dataset.path,button.dataset.label)});list.querySelectorAll('[data-retry-path]').forEach(button=>button.onclick=async()=>{button.disabled=true;try{await api('/api/subtitle-cache/failures/retry-one?path='+encodeURIComponent(button.dataset.retryPath),{method:'POST'});button.closest('.subtitle-cache-failure').remove();toast('This media will be retried on the next cache run');await loadCacheSchedule()}catch(error){button.disabled=false;toast(error.message,true)}})}catch(error){list.textContent=error.message}};
    let cacheLoaded=false;
    async function loadCacheSchedule(){
      try{const data=await api('/api/subtitle-cache/schedule');
        if(!cacheLoaded){cacheFrequency.value=data.frequency;cacheTime.value=data.time;cacheHours.value=data.hours;cacheMinutes.value=data.minutes;cacheLoaded=true}
        const run=data.latest_run,active=run&&['pending','running'].includes(run.status);
        cacheRun.disabled=Boolean(active);cacheStop.disabled=!active;
        const next=data.next_run?formatAppDate(data.next_run):'not scheduled';
        cacheStatus.textContent=`${data.frequency==='disabled'?'Schedule disabled':'Next run: '+next} · ${appTimezone()} · ${data.hours}h ${data.minutes}m maximum`;
        const attempted=run?Number(run.processed||0)+Number(run.failed||0):0;
        const remaining=Number(data.remaining_work||0),workTotal=attempted+remaining;
        const percent=workTotal?100*attempted/workTotal:100;
        const workProgress=active?` · cache work this run ${percent<1?percent.toFixed(2):percent.toFixed(1)}% (${attempted}/${workTotal} attempted)`:'';
        const scanProgress=run&&data.total_media?` · catalog scan ${run.cursor_offset}/${data.total_media}`:'';
        cacheProgress.textContent=run?`${run.status} · ${run.processed} media cached · ${run.skipped} skipped (cached, no subtitles, or quarantined) · ${run.failed} failed · ${data.priority_pending||0} priority pending · ${remaining} actionable remaining${workProgress}${scanProgress}`:`${data.priority_pending||0} priority pending · ${remaining} actionable remaining · no cache run yet.`;
        cacheCurrent.hidden=!active||!run.current_path;
        cacheCurrent.textContent=cacheCurrent.hidden?'':`Current media: ${run.current_path}`;
        cacheFailures.textContent=`Failed media (${data.failed_media_count||0} unresolved)`;
      }catch(e){cacheStatus.textContent=e.message}
    }
    cacheCard.querySelector('[data-cache-save]').onclick=async()=>{try{cacheLoaded=false;await api('/api/subtitle-cache/schedule',{method:'PUT',body:JSON.stringify({frequency:cacheFrequency.value,time:cacheTime.value||'03:00',hours:Number(cacheHours.value),minutes:Number(cacheMinutes.value)})});await loadCacheSchedule();toast('Subtitle cache schedule saved')}catch(e){cacheLoaded=true;cacheStatus.textContent=e.message;toast(e.message,true)}};
    cacheRun.onclick=async()=>{try{cacheRun.disabled=true;await api('/api/subtitle-cache/run-now',{method:'POST'});await loadCacheSchedule()}catch(e){cacheRun.disabled=false;cacheStatus.textContent=e.message;toast(e.message,true)}};
    cacheStop.onclick=async()=>{try{cacheStop.disabled=true;await api('/api/subtitle-cache/stop',{method:'POST'});cacheProgress.textContent='Stopping after the current subtitle is safely finished or discarded…';await loadCacheSchedule()}catch(e){cacheStatus.textContent=e.message;toast(e.message,true)}};
    loadCacheSchedule();setInterval(()=>{if(!panel.classList.contains('hidden'))loadCacheSchedule()},5000);
    const key='videostreamedit.tasks-tab.v1';
    function activate(name){const target=shell.querySelector(`[data-tasks-panel="${name}"]`);if(!target)return;tabs.querySelectorAll('[data-tasks-tab]').forEach(b=>{const active=b.dataset.tasksTab===name;b.classList.toggle('active',active);b.setAttribute('aria-selected',String(active))});shell.querySelectorAll('[data-tasks-panel]').forEach(p=>p.classList.toggle('hidden',p!==target));localStorage.setItem(key,name)}
    tab.onclick=()=>activate('schedules');
    if(localStorage.getItem(key)==='schedules')activate('schedules');
    return true;
  }
  if(!install()){const observer=new MutationObserver(()=>{if(install())observer.disconnect()});observer.observe(setup,{childList:true,subtree:true});setTimeout(()=>observer.disconnect(),10000)}
})();
