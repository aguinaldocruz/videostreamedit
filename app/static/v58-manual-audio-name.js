(function () {
  function installManualAudioName() {
    const list = $('#saved-property-list');
    const group = [...(list?.querySelectorAll(".saved-property-group") || [])].find(item => item.querySelector("strong")?.textContent === "Audio track names");
    if (!group || group.querySelector('.manual-audio-name')) return;
    group.querySelector('strong').insertAdjacentHTML('afterend', `<form class="manual-audio-name"><input type="text" aria-label="New audio track name" placeholder="Add an audio track name" autocomplete="off"><button type="submit" class="primary">Add</button></form>`);
    const form = group.querySelector('.manual-audio-name');
    form.onsubmit = async event => {
      event.preventDefault();
      const input = form.querySelector('input'), value = input.value.trim();
      if (!value) { toast('Audio track name cannot be empty', true); input.focus(); return; }
      const button = form.querySelector('button');
      button.disabled = true;
      try {
        await api('/api/v8/saved-values', {method: 'POST', body: JSON.stringify({field: 'title_audio', value, save: true})});
        input.value = '';
        v8Saved = await api('/api/v8/saved-values');
        toast(`Audio track name “${value}” saved`);
        await renderSavedPropertyMaintenance();
      } catch (error) {
        toast(error.message, true);
      } finally {
        button.disabled = false;
      }
    };
  }

  const originalRenderSavedPropertyMaintenance = renderSavedPropertyMaintenance;
  renderSavedPropertyMaintenance = async function (...args) {
    const result = await originalRenderSavedPropertyMaintenance(...args);
    installManualAudioName();
    return result;
  };
  installManualAudioName();

  const maintenance = document.querySelector('#learned-suggestion-maintenance');
  if (maintenance) {
    const panel = document.createElement('details');
    panel.className = 'setup-advanced';
    panel.innerHTML = `<summary>Declined track names</summary><p>Names you declined to save will not be offered again. Save a name here to make it reusable. Clearing this list allows questions again after two new uses.</p><button type="button" data-clear-declined>Clear denied list</button><div data-declined-list></div>`;
    maintenance.append(panel);
    async function refreshDeclined() {
      const list = panel.querySelector('[data-declined-list]');
      list.textContent = 'Loading declined names…';
      try {
        const data = await api('/api/v8/declined-track-names');
        list.replaceChildren();
        if (!data.values.length) { list.textContent = 'No declined track names.'; return; }
        for (const item of data.values) {
          const row = document.createElement('div'); row.className = 'saved-property-item';
          const label = document.createElement('span');
          label.textContent = `${item.field === 'title_audio' ? 'Audio' : 'Subtitle'} · ${item.value}`;
          const button = document.createElement('button'); button.type = 'button'; button.textContent = 'Add to saved names';
          button.onclick = async () => {
            button.disabled = true;
            try {
              await api('/api/v8/saved-values', {method:'POST', body:JSON.stringify({field:item.field,value:item.value,save:true})});
              v8Saved = await api('/api/v8/saved-values');
              await refreshDeclined();
              await renderSavedPropertyMaintenance();
              toast('Track name saved');
            } catch(error) { toast(error.message,true); button.disabled = false; }
          };
          row.append(label,button); list.append(row);
        }
      } catch(error) { list.textContent = error.message; }
    }
    panel.addEventListener('toggle', () => { if(panel.open) refreshDeclined(); });
    panel.querySelector('[data-clear-declined]').onclick = async event => {
      if(!confirm('Clear all declined track names? They may be offered again after two new uses. Saved names and correction rules are preserved.')) return;
      const button=event.currentTarget; button.disabled=true;
      try { await api('/api/v8/declined-track-names',{method:'DELETE'}); await refreshDeclined(); }
      catch(error) { toast(error.message,true); }
      finally { button.disabled=false; }
    };
  }
})();
