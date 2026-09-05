(function () {
  const maintenance=document.querySelector('.index-maintenance');
  if(!maintenance)return;
  const heading=maintenance.querySelector('.split-index-heading p');
  if(heading)heading.textContent='Core metadata is scheduled and indexed. Subtitle inspection and media previews run only when requested.';
  const core=maintenance.querySelector('[data-index-job="core"]');
  if(core){core.querySelector('h3').textContent='Core stream metadata index';core.querySelector('p').textContent='Language, region, track name, stream type, flags, and external subtitle tags for movie and TV filters.'}
  document.head.insertAdjacentHTML('beforeend','<style>.index-on-demand [data-index-status],.index-on-demand [data-index-queue-items],.index-on-demand [data-index-retry],.index-on-demand [data-index-pause],.index-on-demand [data-index-stop],.index-on-demand .index-schedule{display:none!important}</style>');
  for(const [job,title,description] of [
    ['subtitles','On-demand subtitle inspection','Subtitle text, formatting tags, and graphical previews are inspected only when you open a subtitle preview.'],
    ['previews','On-demand preview cache','Audio segments stream when requested and only viewed segments are retained in the 512 MB LRU cache.']
  ]){
    const card=maintenance.querySelector(`[data-index-job="${job}"]`);
    if(!card)continue;
    card.classList.add('index-on-demand');
    const tab=maintenance.querySelector(`[data-index-tab="${job}"]`);if(tab)tab.textContent=job==='subtitles'?'Subtitle inspection':'Preview cache';
    card.querySelector('h3').textContent=title;
    card.querySelector('p').textContent=description;
    const check=card.querySelector('[data-index-check]');if(check)check.hidden=true;
    const rebuild=card.querySelector('[data-index-rebuild]');
    if(rebuild){
      rebuild.textContent=job==='subtitles'?'Clear inspection data':'Clear preview cache';
      rebuild.onclick=async()=>{
        if(!confirm(`Clear stored ${job==='subtitles'?'subtitle inspection data':'preview files'}? New information will be generated only when requested.`))return;
        try{const task=await api(`/api/v80/setup/index/${job}/rebuild`,{method:'POST',body:'{}'});toast(`Cleanup queued as job #${task.id}.`)}catch(error){toast(error.message,true)}
      };
    }
    card.querySelector('.index-schedule')?.classList.add('hidden');
  }
  core?.insertAdjacentHTML('beforeend','<section id="performance-health"><h3>Performance health</h3><p data-performance-summary>Load Setup to check current risks.</p><div class="index-maintenance-actions"><button type="button" data-prune-history>Clean all finished queue history</button></div></section>');
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
