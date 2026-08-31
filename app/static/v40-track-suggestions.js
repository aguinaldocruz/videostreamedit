(function () {
  const dismissed = new Set();

  function suggestionKey(item) {
    return `${item.stream_type}\n${item.old_value}\n${item.new_value}`;
  }

  function renderTrackNameSuggestions(items) {
    document.querySelector('#track-name-suggestion')?.remove();
    const suggestions = items.filter(item => !dismissed.has(suggestionKey(item)));
    if (!suggestions.length) return;
    const descriptions = suggestions.map(item => {
      const from = item.old_value || '(empty)';
      return `<li><strong>${esc(item.stream_type)}</strong>: “${esc(from)}” → “${esc(item.new_value)}” <small>${item.use_count} previous changes</small></li>`;
    }).join('');
    const panel = document.createElement('div');
    panel.id = 'track-name-suggestion';
    panel.className = 'track-name-suggestion';
    panel.innerHTML = `<div><strong>Common track-name correction available</strong><p>This only fills the matching fields. Review and use Apply changes when ready.</p><ul>${descriptions}</ul></div><div><button type="button" data-dismiss-suggestion>Not now</button><button type="button" class="primary" data-use-suggestion>Use suggestions</button></div>`;
    $('#stream-content').prepend(panel);
    panel.querySelector('[data-dismiss-suggestion]').onclick = () => {
      suggestions.forEach(item => dismissed.add(suggestionKey(item)));
      panel.remove();
    };
    panel.querySelector('[data-use-suggestion]').onclick = () => {
      document.querySelectorAll('#stream-content .stream-row').forEach(row => {
        const input = row.querySelector('[name=title]');
        const suggestion = suggestions.find(item => item.stream_type === row.dataset.codecType && item.old_value === input.value.trim());
        if (!suggestion || row.querySelector('[name=remove]')?.checked) return;
        input.value = suggestion.new_value;
        input.dataset.dirty = 'true';
        input.classList.add('learned-track-name-suggestion');
        input.title = `Suggested from ${suggestion.use_count} previous corrections`;
        input.dispatchEvent(new Event('input', {bubbles: true}));
      });
      panel.remove();
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
