(function () {
  const maintenance=document.querySelector('.index-maintenance');
  if(!maintenance)return;
  maintenance.querySelectorAll('[data-index-job]').forEach(card=>{
    const job=card.dataset.indexJob,actions=card.querySelector('.index-maintenance-actions');
    actions.insertAdjacentHTML('beforeend','<button type="button" data-index-retry disabled>Retry failed</button><button type="button" data-index-pause disabled>Pause</button><button type="button" class="danger" data-index-stop disabled>Stop</button>');
    const retry=card.querySelector('[data-index-retry]'),pause=card.querySelector('[data-index-pause]'),stop=card.querySelector('[data-index-stop]');
    const synchronizeControls=()=>{const status=card.querySelector('[data-index-status]').textContent,active=!status.startsWith('0 running · 0 queued');pause.disabled=!active;stop.disabled=!active;if(!active)pause.textContent='Pause'};
    new MutationObserver(synchronizeControls).observe(card.querySelector('[data-index-status]'),{childList:true,characterData:true,subtree:true});
    pause.onclick=async()=>{const action=pause.textContent==='Resume'?'resume':'pause';try{const status=await api(`/api/v80/setup/index/${job}/${action}`,{method:'POST',body:'{}'});pause.textContent=status.paused?'Resume':'Pause';toast(status.paused?'Index queue paused.':'Index queue resumed.')}catch(error){toast(error.message,true)}};
    stop.onclick=async()=>{try{await api(`/api/v80/setup/index/${job}/stop`,{method:'POST',body:'{}'});card.querySelector('[data-index-status]').textContent='Stopping after the current item and cancelling pending index requests…';stop.disabled=true}catch(error){toast(error.message,true)}};
    retry.onclick=async()=>{try{await api(`/api/v80/setup/index/${job}/retry`,{method:'POST',body:'{}'});toast(`Failed ${job} index items queued again`)}catch(error){toast(error.message,true)}};
  });
  const controlsApi=api;
  api=async function(resource,options){const result=await controlsApi(resource,options);const match=resource.match(/\/api\/v80\/setup\/index\/(core|subtitles|previews)\/status/);if(match)setTimeout(()=>{const card=maintenance.querySelector('[data-index-job="'+match[1]+'"]'),pause=card?.querySelector("[data-index-pause]");if(pause)pause.textContent=result.paused?"Resume":"Pause"},0);return result};
  const preview=maintenance.querySelector('[data-index-job="previews"]');
  if(preview){
    preview.querySelector('h3').textContent='3. Efficient preview cache';
    preview.querySelector('p').textContent='Pre-caches the first 25-second audio sample and internal text subtitles. Later audio samples are created only when requested.';
    preview.querySelector('.index-maintenance-actions').insertAdjacentHTML('afterend','<label class="preview-cache-limit">Maximum cache <input type="number" min="1" max="100" step="1" data-cache-limit> GB</label>');
    const input=preview.querySelector('[data-cache-limit]');
    api('/api/v63/setup/preview-cache').then(value=>input.value=value.gigabytes).catch(()=>{});
    input.onchange=async()=>{try{const value=await api('/api/v63/setup/preview-cache',{method:'PUT',body:JSON.stringify({gigabytes:Number(input.value)})});input.value=value.gigabytes;toast(`Preview cache limited to ${value.gigabytes} GB.`)}catch(error){toast(error.message,true)}};
  }
})();
