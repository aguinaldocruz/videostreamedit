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
})();
