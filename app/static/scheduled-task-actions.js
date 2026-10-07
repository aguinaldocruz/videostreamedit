(function(){
  const setup=document.querySelector('#setup');if(!setup)return;
  const labels={pending:'Queued',running:'Running',succeeded:'Completed',completed:'Completed',failed:'Failed',cancelled:'Cancelled',interrupted:'Interrupted',time_limit:'Time limit reached'};
  const popup=document.createElement('dialog');popup.className='scheduled-log-dialog';
  popup.innerHTML='<header><div><h3 data-log-title>Latest task log</h3><small>Newest entries first · application timezone</small></div><div><button type="button" data-log-refresh>Refresh</button><button type="button" data-log-close>Close</button></div></header><p data-log-summary role="status"></p><div data-log-entries></div><footer><small data-log-limit></small><button type="button" data-log-more hidden>Load earlier entries</button></footer>';
  document.body.append(popup);
  const summary=popup.querySelector('[data-log-summary]'),list=popup.querySelector('[data-log-entries]'),more=popup.querySelector('[data-log-more]');
  let selected='',runId=null,cursor=null,generation=0;
  popup.querySelector('[data-log-close]').onclick=()=>popup.close();
  popup.onclose=()=>generation++;
  async function loadLog(append=false){
    const own=++generation,query=new URLSearchParams();
    if(append&&cursor){query.set('before',cursor);query.set('run_id',runId)}
    const refresh=popup.querySelector('[data-log-refresh]');refresh.disabled=true;more.disabled=true;
    if(!append){summary.textContent='Loading latest job log…';list.replaceChildren();more.hidden=true}
    try{
      const data=await api(`/api/scheduled-tasks/${selected}/latest-log?${query}`);
      if(own!==generation||!popup.open)return;
      popup.querySelector('[data-log-title]').textContent=data.title+' · latest log';
      const run=data.run;
      if(!run){summary.textContent='No recorded run yet. Run now or wait for the next scheduled execution to create a log.';return}
      runId=run.run_id;cursor=data.before;
      if(!append)summary.textContent=[labels[run.status]||run.status,run.source==='schedule'?'Scheduled':'User requested',formatAppDate(run.started_at),run.finished_at?'Finished '+formatAppDate(run.finished_at):'',run.current_message,run.summary,run.error].filter(Boolean).join(' · ');
      for(const entry of data.entries){
        const article=document.createElement('article');article.className='scheduled-log-entry';
        article.classList.toggle('error',entry.level==='error'||entry.item_status==='failed');
        const heading=document.createElement('small');heading.textContent=[formatAppDate(entry.created_at),entry.level==='error'?'Error':'Info',entry.task_id?'Job #'+entry.task_id:'',entry.item_status?(labels[entry.item_status]||entry.item_status):''].filter(Boolean).join(' · ');article.append(heading);
        const message=document.createElement('p');message.textContent=entry.message;article.append(message);
        if(entry.path){const path=document.createElement('small');path.className='scheduled-log-path';path.textContent=entry.path;article.append(path)}
        if(entry.error){const error=document.createElement('p');error.textContent=entry.error;article.append(error)}
        list.append(article);
      }
      more.hidden=!data.more;
      popup.querySelector('[data-log-limit]').textContent=data.truncated?`Latest ${data.entry_limit} entries retained; the summary includes the full run. Older finished runs are rotated automatically.`:'Logs retain the latest 10 runs for each task. Processing jobs show their current status when this view is opened or refreshed.';
    }catch(error){if(own===generation)summary.textContent=error.message}
    finally{if(own===generation){refresh.disabled=false;more.disabled=false}}
  }
  popup.querySelector('[data-log-refresh]').onclick=()=>loadLog();more.onclick=()=>loadLog(true);
  function install(){
    setup.querySelectorAll('[data-schedule-actions-job]').forEach(card=>{
      if(card.querySelector('[data-schedule-actions]'))return;
      const job=card.dataset.scheduleActionsJob,actions=document.createElement('div');actions.className='scheduled-task-actions';actions.dataset.scheduleActions='';
      const run=card.querySelector('[data-cache-run]')||document.createElement('button');
      run.type='button';run.textContent='Run now';run.dataset.scheduleRun=job;actions.append(run);
      run.title='Run using saved settings, even if the recurring schedule is disabled';
      const stop=card.querySelector('[data-cache-stop]');if(stop)actions.append(stop);
      const log=document.createElement('button');log.type='button';log.textContent='Latest log';log.dataset.scheduleLog=job;
      log.onclick=()=>{selected=job;runId=null;cursor=null;popup.showModal();loadLog()};actions.append(log);
      const message=document.createElement('small');message.role='status';message.dataset.runNowStatus='';actions.append(message);card.append(actions);
      if(job!=='subtitle_cache')run.onclick=async()=>{
        run.disabled=true;message.textContent='Submitting run request…';
        try{
          const data=await api(`/api/scheduled-tasks/${job}/run-now`,{method:'POST',body:'{}'});
          message.textContent=data.task_id||data.id?`Queued · job #${data.task_id||data.id} · view Latest log for progress`:'Started · view Latest log for progress';
          toast('Run requested');
        }catch(error){message.textContent=error.message;toast(error.message,true)}finally{run.disabled=false}
      };
    });
  }
  install();new MutationObserver(install).observe(setup,{childList:true,subtree:true});
})();
