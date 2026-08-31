(function () {
  const dismissed = new Set();

  function suggestionKey(item) {
    return `${item.stream_type}\n${item.old_value}\n${item.new_value}`;
  }

  function renderTrackNameSuggestions(items) {
    document.querySelectorAll('.track-name-suggestion').forEach(panel => panel.remove());
    const suggestions = items.filter(item => !dismissed.has(suggestionKey(item)));
    if (!suggestions.length) return;
    const descriptions = suggestions.map(item => {
      const from = item.old_value || '(empty)';
      return `<li><strong>${esc(item.stream_type)}</strong>: “${esc(from)}” → “${esc(item.new_value)}” <small>${item.use_count} previous changes</small></li>`;
    }).join('');
    const panel = document.createElement('div');
    panel.className = 'track-name-suggestion';
    panel.innerHTML = `<div><strong>Common track-name correction available</strong><p>This only fills matching fields. Review them and use Apply changes when ready.</p><ul>${descriptions}</ul></div><div><button type="button" data-dismiss-suggestion>Not now</button><button type="button" class="primary" data-use-suggestion>Use suggestions</button></div>`;
    $('#stream-content').prepend(panel);
    panel.querySelector('[data-dismiss-suggestion]').onclick = () => {
      suggestions.forEach(item => dismissed.add(suggestionKey(item)));
      panel.remove();
      closeSavedValueMenu();
      $('#stream-dialog')?.focus({preventScroll: true});
    };
    panel.querySelector('[data-use-suggestion]').onclick = () => {
      const active = document.activeElement;
      document.querySelectorAll('#stream-content .stream-row').forEach(row => {
        const input = row.querySelector('[name=title]');
        const suggestion = suggestions.find(item => item.stream_type === row.dataset.codecType && item.old_value === input.value.trim());
        if (!suggestion || row.querySelector('[name=remove]')?.checked) return;
        input.value = suggestion.new_value;
        input.dataset.dirty = 'true';
        input.classList.add('learned-track-name-suggestion');
        input.title = `Suggested from ${suggestion.use_count} previous corrections`;
      });
      closeSavedValueMenu();
      if (active?.matches?.('#stream-content input')) active.blur();
      panel.remove();
      updateQueuedChangeLabels();
      $('#stream-dialog')?.focus({preventScroll: true});
      toast('Track-name suggestions queued for review');
    };
  }

  async function loadTrackNameSuggestions() {
    const rows = [...document.querySelectorAll('#stream-content .stream-row')];
    if (!rows.length) return;
    const values = rows.map(row => ({stream_type: row.dataset.codecType, value: row.querySelector('[name=title]').value.trim()}));
    try {
      const result = await api('/api/v40/track-name-suggestions', {method: 'POST', body: JSON.stringify({values})});
      renderTrackNameSuggestions(result.suggestions);
      closeSavedValueMenu();
      $('#stream-dialog')?.focus({preventScroll: true});
    } catch (error) {
      console.warn('Could not load learned track-name suggestions', error);
    }
  }

  const suggestionOpenEditor = openEditor;
  openEditor = async function (...argumentsList) {
    const result = await suggestionOpenEditor(...argumentsList);
    await loadTrackNameSuggestions();
    return result;
  };
})();
