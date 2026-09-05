(function () {
  const maintenance=document.querySelector('.index-maintenance');
  if(!maintenance)return;
  const definitions=[
    ['core','Common filters','Common stream filters queue','Fast language, stream type, region, and track-name updates.'],
    ['subtitles','Subtitles','Extended subtitle properties queue','Markup/style tags, text/graphical classification, and hover details.'],
    ['previews','Previews','Efficient preview cache queue','On-demand audio samples and internal subtitle previews.']
  ];
  maintenance.innerHTML=`<div class="split-index-shell"><div class="split-index-heading"><h3>Incremental media indexes</h3><p>Each index has an independent queue. Check adds only new or changed movies; rebuild preparation runs in the generic job queue.</p></div><div class="index-queue-tabs" role="tablist" aria-label="Index queues">${definitions.map(([job,label])=>`<button type="button" role="tab" data-index-tab="${job}" aria-controls="index-panel-${job}">${label}</button>`).join('')}</div><div class="split-index-grid">${definitions.map(([job,,title,description],index)=>`<article id="index-panel-${job}" role="tabpanel" data-index-job="${job}"${index?' hidden':''}><div><h3>${title}</h3><p>${description}</p></div><div class="index-maintenance-actions"><button type="button" ${job==='core'?'id="movie-index-check" ':''}data-index-check>Queue check</button><button type="button" ${job==='core'?'id="movie-index-rebuild" ':''}class="danger" data-index-rebuild>Queue rebuild</button></div><p ${job==='core'?'id="movie-index-progress" ':''}class="plex-status" data-index-status>Checking status…</p></article>`).join('')}</div></div>`;
  const previousApi=api;
  api=async function(resource,options){let value=resource,legacyStatus=value==='/api/v39/setup/movie-index/status';if(legacyStatus||value==='/api/v54/setup/index/core/status')value='/api/v80/setup/index/core/status';else if(value==='/api/v39/setup/movie-index/check'||value==='/api/v54/setup/index/core/check')value='/api/v80/setup/index/core/check';else if(value==='/api/v39/setup/movie-index/rebuild'||value==='/api/v52/setup/movie-index/rebuild'||value==='/api/v54/setup/index/core/rebuild')value='/api/v80/setup/index/core/rebuild';else if(value==='/api/v39/movies/stream-filter-refresh'||value==='/api/v52/movies/stream-filter-refresh')value='/api/v57/index/media-refresh';else if(value==='/api/v38/movies/stream-filter-invalidate')value='/api/v54/index/invalidate';const result=await previousApi(value,options);return legacyStatus?{...result,running:false}:result};
  const timers={};
  let activeJob=localStorage.getItem('vse-index-queue-tab')||'core';
  if(!definitions.some(item=>item[0]===activeJob))activeJob='core';
  function bytes(value){if(!value)return'0 B';const units=['B','KB','MB','GB','TB'];const power=Math.min(units.length-1,Math.floor(Math.log(value)/Math.log(1024)));return`${(value/1024**power).toFixed(power?1:0)} ${units[power]}`}
  function stableText(node,value){if(node.textContent!==value)node.textContent=value}
  function show(card,status){
    const job=card.dataset.indexJob,text=card.querySelector('[data-index-status]');
    let summary=`${status.running?1:0} running · ${status.queued||0} queued · ${status.failed||0} failed · ${status.indexed||0} indexed${status.paused?' · Paused':''}`;
    if(job==='previews')summary+=` · ${status.audio_files||0} audio segments · ${status.subtitle_files||0} subtitles · ${bytes(status.cache_bytes||0)}`;
    stableText(text,summary);
    let details=card.querySelector('[data-index-queue-items]');
    if(!details){text.insertAdjacentHTML('afterend','<details data-index-queue-items><summary>Queued work and errors</summary><div></div></details>');details=card.querySelector('[data-index-queue-items]')}
    const items=status.items||[],signature=JSON.stringify(items.map(item=>[item.id,item.status,item.attempts,item.error]));
    if(details.dataset.signature!==signature){
      const wasOpen=details.open;
      details.querySelector('div').innerHTML=items.length?items.map(item=>`<p><strong>#${item.id} · ${esc(item.status)}</strong> · ${esc(item.path.split('/').pop())}${item.error?`<br><span class="error">${esc(item.error)}</span>`:''}</p>`).join(''):'<p class="muted">No active or failed items.</p>';
      details.dataset.signature=signature;
      details.open=wasOpen;
    }
    const retry=card.querySelector('[data-index-retry]');if(retry)retry.disabled=!(status.failed>0);
    clearTimeout(timers[job]);
    if(job===activeJob&&(status.running||status.queued))timers[job]=setTimeout(()=>load(card),2000);
  }
  async function load(card){if(document.querySelector('#setup').classList.contains('hidden')||card.dataset.indexJob!==activeJob)return;const job=card.dataset.indexJob;try{show(card,await api(`/api/v80/setup/index/${job}/status`))}catch(error){stableText(card.querySelector('[data-index-status]'),error.message)}}
  function activate(job){
    activeJob=job;localStorage.setItem('vse-index-queue-tab',job);
    Object.values(timers).forEach(clearTimeout);
    maintenance.querySelectorAll('[data-index-tab]').forEach(tab=>{const selected=tab.dataset.indexTab===job;tab.setAttribute('aria-selected',selected);tab.tabIndex=selected?0:-1;tab.classList.toggle('active',selected)});
    maintenance.querySelectorAll('[data-index-job]').forEach(card=>card.hidden=card.dataset.indexJob!==job);
    const active=maintenance.querySelector(`[data-index-job="${job}"]`);if(active)load(active);
  }
  maintenance.querySelectorAll('[data-index-tab]').forEach(tab=>tab.onclick=()=>activate(tab.dataset.indexTab));
  maintenance.querySelectorAll('[data-index-job]').forEach(card=>{
    const job=card.dataset.indexJob;
    card.querySelector('[data-index-check]').onclick=async()=>{const button=card.querySelector('[data-index-check]');button.disabled=true;try{const task=await api(`/api/v80/setup/index/${job}/check`,{method:'POST',body:'{}'});toast(`${job} index check queued as job #${task.id}.`);load(card)}catch(error){toast(error.message,true)}finally{button.disabled=false}};
    card.querySelector('[data-index-rebuild]').onclick=async()=>{if(!confirm(`Add preparation of a complete ${job} rebuild to the generic job queue? The ${job} index will be cleared only when that job starts.`))return;try{const task=await api(`/api/v80/setup/index/${job}/rebuild`,{method:'POST',body:'{}'});toast(`Rebuild preparation added to the generic queue as job #${task.id}.`);load(card)}catch(error){toast(error.message,true)}};
  });
  activate(activeJob);
  document.querySelector('[data-page=setup]')?.addEventListener('click',()=>setTimeout(()=>activate(activeJob),0));
})();
