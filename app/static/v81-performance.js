(function () {
  const maintenance=document.querySelector('.index-maintenance');
  if(!maintenance)return;
  const heading=maintenance.querySelector('.split-index-heading p');
  if(heading)heading.textContent='Core metadata and subtitle inspection can run incrementally on demand or on a schedule. Preview media remains on demand.';
  const core=maintenance.querySelector('[data-index-job="core"]');
  if(core){core.querySelector('h3').textContent='Core stream metadata index';core.querySelector('p').textContent='Language, region, track name, stream type, flags, and external subtitle tags for movie and TV filters.'}
  document.head.insertAdjacentHTML('beforeend','<style>.index-on-demand [data-index-check],.index-on-demand .index-schedule{display:none!important}</style>');
  for(const [job,title,description] of [
    ['subtitles','Subtitle inspection','Incremental subtitle inspection can run now or on a schedule. Start from scratch only when necessary.'],
    ['previews','On-demand preview cache','Audio segments stream when requested and only viewed segments are retained in the 512 MB LRU cache.']
  ]){
    const card=maintenance.querySelector(`[data-index-job="${job}"]`);
    if(!card)continue;
    if(job==='previews')card.classList.add('index-on-demand');
    const tab=maintenance.querySelector(`[data-index-tab="${job}"]`);if(tab)tab.textContent=job==='subtitles'?'Subtitle inspection':'Preview cache';
    card.querySelector('h3').textContent=title;
    card.querySelector('p').textContent=description;
    const check=card.querySelector('[data-index-check]');if(check&&job==='previews')check.hidden=true;
    if(job==='subtitles'){if(check){check.textContent='Run incremental';check.title='Inspect only new or changed media and affected subtitle streams.'}const actions=card.querySelector('.index-maintenance-actions');actions.insertAdjacentHTML('beforeend','<button type="button" data-index-clear title="Delete stored subtitle analysis and cancel pending subtitle inspections; do not queue new work.">Clear stored results</button>');const clear=card.querySelector('[data-index-clear]');clear.onclick=async()=>{if(!confirm('Delete stored subtitle inspection results and cancel pending subtitle inspections? No new inspection work will be queued.'))return;clear.disabled=true;try{await api('/api/v80/setup/index/subtitles/clear',{method:'POST',body:'{}'});toast('Stored subtitle results cleared; no rebuild was queued.')}catch(error){toast(error.message,true)}finally{clear.disabled=false}};const rebuildButton=card.querySelector('[data-index-rebuild]');if(rebuildButton){rebuildButton.textContent='Rebuild full index';rebuildButton.title='Clear the subtitle inspection index and queue every media for a complete rebuild.'}}
    const rebuild=card.querySelector('[data-index-rebuild]');
    if(rebuild){
      rebuild.textContent=job==='subtitles'?'Rebuild full index':'Clear preview cache';
      rebuild.onclick=async()=>{
        const message=job==='subtitles'?'Queue a complete subtitle inspection rebuild? Existing subtitle results will be cleared when the rebuild starts, then every catalog media will be queued.':'Clear preview files? New preview segments will be generated on demand.';
        if(!confirm(message))return;
        try{const task=await api(`/api/v80/setup/index/${job}/rebuild`,{method:'POST',body:'{}'});toast(job==='subtitles'?`Full subtitle rebuild queued as job #${task.id}.`:`Preview cleanup queued as job #${task.id}.`)}catch(error){toast(error.message,true)}
      };
    }
    if(job==='previews')card.querySelector('.index-schedule')?.classList.add('hidden');
  }
  core?.insertAdjacentHTML('beforeend','<section id="performance-health"><h3>Performance health</h3><p data-performance-summary>Load Setup to check current risks.</p><div class="index-maintenance-actions"><div class="index-action-group" data-action-group="history"><span class="index-action-label">History</span><button type="button" data-prune-history>Clean all finished queue history</button></div></div></section>');
  const cacheInput=maintenance.querySelector('[data-cache-limit]');if(cacheInput){cacheInput.min='0.25';cacheInput.step='0.25'}
  const prune=maintenance.querySelector("[data-prune-history]");if(prune)prune.onclick=async()=>{try{const result=await api("/api/v82/setup/queues/prune",{method:"POST",body:"{}"});toast(`Removed ${result.generic+result.index} finished queue entries`);refresh();document.querySelector('[data-index-tab][aria-selected="true"]')?.click()}catch(error){toast(error.message,true)}};
  const summary=document.querySelector('[data-performance-summary]');
  const bytes=value=>`${(Number(value||0)/1024/1024).toFixed(0)} MB`;
  async function refresh(){
    try{
      const value=await api('/api/v81/setup/performance');
      summary.classList.toggle('error',value.risks.length>0);
      summary.textContent=value.risks.length?value.risks.join(" · "):`Healthy · WAL active · ${value.unified_indexed} of ${value.catalog} media indexed · ${value.core_pending} core updates queued · preview cache ${bytes(value.cache_bytes)} / ${bytes(value.cache_limit)}`;
    }catch(error){summary.textContent=error.message;summary.classList.add('error')}
  }
  document.querySelector('[data-page="setup"]')?.addEventListener('click',()=>setTimeout(refresh,0));
  if(!document.querySelector('#setup')?.classList.contains('hidden'))refresh();
})();
