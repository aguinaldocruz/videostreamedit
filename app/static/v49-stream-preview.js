(function () {
  document.body.insertAdjacentHTML('beforeend', `<dialog id="stream-preview-dialog" class="stream-preview-dialog">
    <div class="dialog-title"><div><h2 id="stream-preview-title">Stream preview</h2><p id="stream-preview-description"></p></div><button type="button" class="icon-close" data-close-preview aria-label="Close">×</button></div>
    <div id="stream-preview-content" class="stream-preview-content"></div>
    <div class="dialog-actions"><button type="button" data-close-preview>Close</button></div>
  </dialog>`);

  const dialog = $('#stream-preview-dialog');
  const content = $('#stream-preview-content');
  let audioUrl = '';

  function closePreview() {
    const audio = content.querySelector('audio');
    if (audio) { audio.pause(); audio.removeAttribute('src'); audio.load(); }
    if (audioUrl) URL.revokeObjectURL(audioUrl);
    audioUrl = '';
    dialog.close();
  }

  dialog.querySelectorAll('[data-close-preview]').forEach(button => button.onclick = closePreview);
  dialog.addEventListener('cancel', event => { event.preventDefault(); closePreview(); });

  function previewLabel(row) {
    if (row.dataset.external === 'true') return 'External subtitle';
    const type = row.dataset.codecType === 'audio' ? 'Audio' : 'Subtitle';
    return `${type} ${Number(row.dataset.typeIndex) + 1}`;
  }

  async function openPreview(row) {
    const label = previewLabel(row);
    $('#stream-preview-title').textContent = `${label} preview`;
    $('#stream-preview-description').textContent = row.querySelector('.stream-kind small')?.textContent || '';
    content.innerHTML = '<p class="preview-loading">Preparing preview…</p>';
    dialog.showModal();
    const query = new URLSearchParams({path: state.selectedPath});
    if (row.dataset.external === 'true') query.set('external_path', row.dataset.path);
    else query.set('type_index', row.dataset.typeIndex);
    try {
      if (row.dataset.codecType === 'audio') {
        beginGlobalBusy();
        const response = await originalFetch(`/api/v49/stream-preview/audio?${query}`);
        if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(error.detail || `Preview failed (${response.status})`); }
        audioUrl = URL.createObjectURL(await response.blob());
        content.innerHTML = `<p>25-second sample from the selected audio track.</p><audio controls autoplay preload="auto"></audio>`;
        content.querySelector('audio').src = audioUrl;
      } else {
        const result = await api(`/api/v49/stream-preview/subtitle?${query}`);
        content.innerHTML = `<pre class="subtitle-preview-text">${esc(result.text)}</pre>${result.clipped ? '<p class="muted">Preview shortened for display.</p>' : ''}`;
        $('#stream-preview-description').textContent = result.description;
      }
    } catch (error) {
      content.innerHTML = `<p class="no-streams error">${esc(error.message)}</p>`;
    } finally {
      if (row.dataset.codecType === 'audio') endGlobalBusy();
    }
  }

  function installStreamPreviewLinks() {
    document.querySelectorAll('#stream-content .stream-row').forEach(row => {
      const strong = row.querySelector('.stream-kind strong');
      if (!strong || strong.querySelector('[data-preview-stream]')) return;
      strong.textContent = '';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'stream-preview-link';
      button.dataset.previewStream = '';
      button.textContent = previewLabel(row);
      button.title = row.dataset.codecType === 'audio' ? 'Listen to an audio sample' : 'Read a subtitle sample';
      button.onclick = event => { event.stopPropagation(); openPreview(row); };
      strong.append(button);
    });
  }

  const previewOpenEditor = openEditor;
  openEditor = async function (...args) {
    const result = await previewOpenEditor(...args);
    installStreamPreviewLinks();
    return result;
  };
})();
