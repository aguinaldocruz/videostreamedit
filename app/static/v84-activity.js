(function(){
  const header=document.querySelector('body>header'),nav=header?.querySelector('nav');if(!header||!nav)return;
  const indicator=document.createElement('div');indicator.id='background-activity';indicator.className='background-activity';indicator.setAttribute('role','status');indicator.setAttribute('aria-live','polite');indicator.innerHTML='<span aria-hidden="true"></span>';nav.insertAdjacentElement('afterend',indicator);
  let signature='',timer=null;
  function title(data){const summary=[];if(data.running)summary.push(`${data.running} processing`);if(data.pending)summary.push(`${data.pending} queued`);for(const item of data.items||[]){const progress=item.progress_total?` (${item.progress_current}/${item.progress_total})`:'';summary.push(`#${item.id} ${item.label}: ${item.progress_message||'Processing'}${progress}`)}return summary.join('\n')||'No media operations queued'}
  async function refreshActivity(){
    try{const response=await originalFetch('/api/v84/activity',{headers:{Accept:'application/json'}});if(!response.ok)return;const data=await response.json(),next=JSON.stringify([data.active,data.running,data.pending,(data.items||[]).map(item=>[item.id,item.progress_message,item.progress_current])]);if(next!==signature){signature=next;indicator.classList.toggle('active',data.active);indicator.classList.toggle('running',data.running>0);indicator.title=title(data);indicator.setAttribute('aria-label',data.active?`${data.running} processing, ${data.pending} queued`:'No media operations queued')}clearTimeout(timer);timer=setTimeout(refreshActivity,data.active?2500:8000)}catch(_){clearTimeout(timer);timer=setTimeout(refreshActivity,10000)}
  }
  document.querySelectorAll('.refresh').forEach(button=>button.addEventListener('click',()=>{if(button.dataset.kind==='movies')window.resetMovieHeaderFilters?.();else window.resetTvHeaderFilters?.();refreshActivity()},true));
  refreshActivity();
})();
