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
    setup.querySelectorAll('.index-schedule,.plex-sync-schedule').forEach(node=>cards.append(node));
    const jobs=[['subtitle_detection','Subtitle language detection'],['voice_detection','Voice language detection']];
    const labels={disabled:'Disabled',daily:'Daily',every_other_day:'Every other day',weekly:'Weekly'};
    function describe(v){return v.frequency==='disabled'?'Disabled':`${labels[v.frequency]||v.frequency} · next ${v.next_run?new Date(v.next_run).toLocaleString():'scheduled'}`}
    cards.insertAdjacentHTML('beforeend',jobs.map(([job,label])=>`<div class="scheduled-task-card" data-scheduled-job="${job}"><div><strong>${label}</strong><small data-scheduled-status>Loading…</small></div><label>Frequency<select data-frequency><option value="disabled">Disabled</option><option value="daily">Daily</option><option value="every_other_day">Every other day</option><option value="weekly">Weekly</option></select></label><label>Time<input type="time" data-time value="03:00"></label><button type="button" data-save>Save</button></div>`).join(''));
    cards.querySelectorAll('[data-scheduled-job]').forEach(card=>{const job=card.dataset.scheduledJob,frequency=card.querySelector('[data-frequency]'),time=card.querySelector('[data-time]'),message=card.querySelector('[data-scheduled-status]');
      async function load(){try{const v=await api(`/api/v67/setup/index/${job}/schedule`);frequency.value=v.frequency;time.value=v.time;time.disabled=v.frequency==='disabled';message.textContent=describe(v)}catch(e){message.textContent=e.message}}
      frequency.onchange=()=>time.disabled=frequency.value==='disabled';card.querySelector('[data-save]').onclick=async()=>{try{const v=await api(`/api/v67/setup/index/${job}/schedule`,{method:'PUT',body:JSON.stringify({frequency:frequency.value,time:time.value||'03:00'})});message.textContent=describe(v);toast(`${job.replaceAll('_',' ')} schedule saved`)}catch(e){status.textContent=e.message;toast(e.message,true)}};load();
    });
    const key='videostreamedit.tasks-tab.v1';
    function activate(name){const target=shell.querySelector(`[data-tasks-panel="${name}"]`);if(!target)return;tabs.querySelectorAll('[data-tasks-tab]').forEach(b=>{const active=b.dataset.tasksTab===name;b.classList.toggle('active',active);b.setAttribute('aria-selected',String(active))});shell.querySelectorAll('[data-tasks-panel]').forEach(p=>p.classList.toggle('hidden',p!==target));localStorage.setItem(key,name)}
    tab.onclick=()=>activate('schedules');
    if(localStorage.getItem(key)==='schedules')activate('schedules');
    return true;
  }
  if(!install()){const observer=new MutationObserver(()=>{if(install())observer.disconnect()});observer.observe(setup,{childList:true,subtree:true});setTimeout(()=>observer.disconnect(),10000)}
})();
