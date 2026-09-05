(function () {
  const maintenance=document.querySelector('.index-maintenance');
  if(!maintenance)return;
  const labels={disabled:'Disabled',daily:'Daily',every_other_day:'Every other day',weekly:'Weekly'};
  function describe(value){if(value.frequency==='disabled')return'Optional discovery disabled · pending index items still start immediately';const next=value.next_run?new Date(value.next_run).toLocaleString():'after the selected time';return`${labels[value.frequency]} · next check ${next}`}
  maintenance.querySelectorAll('[data-index-job]').forEach(card=>{
    const job=card.dataset.indexJob;
    card.insertAdjacentHTML('beforeend',`<div class="index-schedule"><label>Scheduled external-change discovery<select data-index-frequency><option value="disabled">Disabled</option><option value="daily">Daily</option><option value="every_other_day">Every other day</option><option value="weekly">Weekly</option></select></label><label>Time<input type="time" data-index-time value="03:00"></label><button type="button" data-index-schedule-save>Save schedule</button><small data-index-schedule-status>Loading schedule…</small></div>`);
    const frequency=card.querySelector('[data-index-frequency]'),time=card.querySelector('[data-index-time]'),status=card.querySelector('[data-index-schedule-status]');
    async function load(){try{const value=await api(`/api/v67/setup/index/${job}/schedule`);frequency.value=value.frequency;time.value=value.time;time.disabled=value.frequency==='disabled';status.textContent=describe(value)}catch(error){status.textContent=error.message}}
    frequency.onchange=()=>time.disabled=frequency.value==='disabled';
    card.querySelector('[data-index-schedule-save]').onclick=async()=>{try{const value=await api(`/api/v67/setup/index/${job}/schedule`,{method:'PUT',body:JSON.stringify({frequency:frequency.value,time:time.value||'03:00'})});status.textContent=describe(value);toast(`${job} index schedule saved`)}catch(error){toast(error.message,true)}};
    load();
  });
})();
