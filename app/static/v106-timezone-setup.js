(function(){
  const info=document.querySelector('#setup [data-tasks-panel="info"]');
  if(!info)return;
  const card=document.createElement('article');
  card.className='backup-card system-timezone-card';
  card.innerHTML='<div class="detector-section-heading"><div><h3>System timezone</h3><p>Used for scheduled start times, application log timestamps, and all displayed dates. Stored database instants are not rewritten.</p></div></div><div class="system-timezone-controls"><label>Linux / IANA timezone<input type="text" list="system-time-zones" data-timezone-value autocomplete="off" spellcheck="false" placeholder="America/Sao_Paulo"><datalist id="system-time-zones"></datalist></label><button type="button" data-timezone-save>Save timezone</button></div><small data-timezone-status>Loading timezone…</small>';
  info.prepend(card);
  const input=card.querySelector('[data-timezone-value]'),list=card.querySelector('#system-time-zones'),status=card.querySelector('[data-timezone-status]'),save=card.querySelector('[data-timezone-save]');
  async function load(){
    try{const [current,zones]=await Promise.all([api('/api/system/timezone'),api('/api/system/timezone/zones')]);
      input.value=current.timezone;list.replaceChildren(...zones.zones.map(name=>{const option=document.createElement('option');option.value=name;return option}));
      status.textContent=`Current: ${current.timezone} · ${formatAppDate(current.now)}`;
    }catch(error){status.textContent=error.message}
  }
  save.onclick=async()=>{const selected=input.value.trim();if(!selected)return;save.disabled=true;try{
    const data=await api('/api/system/timezone',{method:'PUT',body:JSON.stringify({timezone:selected})});
    setAppTimezone(data.timezone);status.textContent=`Saved ${data.timezone}. Refreshing displayed times…`;
    location.reload();
  }catch(error){status.textContent=error.message;save.disabled=false}};
  load();
})();
