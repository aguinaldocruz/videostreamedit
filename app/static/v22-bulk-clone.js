let bulkCloneInspection = null, bulkCloneTimer = null, bulkCloneGeneration = 0;

function ensureBulkCloneUi() {
  if (!$('#bulk-clone-button')) {
    $('.episode-heading').insertAdjacentHTML('beforeend', '<button type="button" id="bulk-clone-button" class="bulk-clone-button hidden">Clone last change</button>');
  }
  if (!$('#bulk-clone-dialog')) {
    document.body.insertAdjacentHTML('beforeend', '<dialog id="bulk-clone-dialog" class="bulk-clone-dialog"><form id="bulk-clone-form"><div class="dialog-title"><div><h2>Clone last change in bulk</h2><p id="bulk-clone-scope"></p></div><button type="button" class="icon-close" data-close-bulk-clone>×</button></div><div class="bulk-clone-content"><section><h3>Changes</h3><ul id="bulk-clone-changes"></ul></section><section><h3 id="bulk-clone-list-title">Episodes</h3><ol id="bulk-clone-list"></ol></section></div><div class="dialog-actions"><div id="bulk-clone-progress" class="apply-progress"><strong>Ready</strong><small>Review affected episodes before proceeding</small></div><button type="button" data-close-bulk-clone>Cancel</button><button type="submit" class="primary">Apply bulk clone</button></div></form></dialog>');
  }
  document.querySelectorAll('[data-close-bulk-clone]').forEach(button => button.onclick = () => $('#bulk-clone-dialog').close());
  $('#bulk-clone-button').onclick = openBulkCloneReview;
  $('#bulk-clone-form').onsubmit = applyBulkClone;
}

function listedEpisodeLabels() {
  return new Map([...document.querySelectorAll('#episode-list .edit-file')].map(button => [button.dataset.path, button.dataset.label]));
}

async function inspectBulkCloneCandidates() {
  ensureBulkCloneUi();
  const button = $('#bulk-clone-button'), saved = readLastChange(), paths = listedEpisodePaths(), generation = ++bulkCloneGeneration;
  bulkCloneInspection = null;
  button.classList.add('hidden');
  if (!state.currentShow || !saved?.before || !paths.length) return;
  button.disabled = true;
  button.textContent = 'Checking clone matches…';
  button.classList.remove('hidden');
  try {
    const result = await api('/api/v22/tv/clone/inspect', {method: 'POST', body: JSON.stringify({paths, expected: saved.before})});
    if (generation !== bulkCloneGeneration) return;
    bulkCloneInspection = {...result, saved};
    if (result.count) {
      button.textContent = `Clone last change · ${result.count} episode${result.count === 1 ? '' : 's'}`;
      button.disabled = false;
    } else button.classList.add('hidden');
  } catch (error) {
    if (generation === bulkCloneGeneration) { button.classList.add('hidden'); console.error('Bulk clone inspection failed', error); }
  }
}

function scheduleBulkCloneInspection() {
  clearTimeout(bulkCloneTimer);
  bulkCloneTimer = setTimeout(inspectBulkCloneCandidates, 400);
}

const bulkCloneRenderEpisodes = renderEpisodes;
renderEpisodes = function () {
  bulkCloneRenderEpisodes();
  scheduleBulkCloneInspection();
};

function openBulkCloneReview() {
  if (!bulkCloneInspection?.count) return;
  const labels = listedEpisodeLabels(), saved = bulkCloneInspection.saved;
  $('#bulk-clone-scope').textContent = `${clean(state.currentShow?.name || 'TV show')} · ${bulkCloneInspection.count} compatible listed episodes`;
  $('#bulk-clone-changes').innerHTML = saved.changes.map(change => `<li>${esc(change)}</li>`).join('');
  $('#bulk-clone-list-title').textContent = `${bulkCloneInspection.count} episodes will be changed`;
  $('#bulk-clone-list').innerHTML = bulkCloneInspection.candidates.map(path => `<li><strong>${esc(labels.get(path) || path.split('/').pop())}</strong><small>${esc(path)}</small></li>`).join('');
  $('#bulk-clone-progress').innerHTML = '<strong>Ready</strong><small>Review affected episodes before proceeding</small>';
  $('#bulk-clone-form [type=submit]').disabled = false;
  $('#bulk-clone-form [type=submit]').textContent = 'Apply bulk clone';
  $('#bulk-clone-form [data-close-bulk-clone]:not(.icon-close)').textContent = 'Cancel';
  document.querySelectorAll('[data-close-bulk-clone]').forEach(button => button.disabled = false);
  $('#bulk-clone-dialog').showModal();
}

function cloneDetailsState(details) {
  const counters = {}, rows = [], entries = new Map();
  [...details.streams, ...details.external_subtitles].forEach(stream => {
    const source = stream.external ? 'external' : 'embedded', type = stream.codec_type, counterKey = `${source}:${type}`, position = counters[counterKey] || 0;
    counters[counterKey] = position + 1;
    const id = `${counterKey}:${position}`, key = source === 'external' ? `external:${stream.path}` : `embedded:${type}:${stream.type_index}`;
    rows.push({id, source, type, codec: stream.codec || 'unknown', language: stream.language || '', region: stream.region || '', title: stream.title || '', embed: false, removed: false});
    entries.set(id, {...stream, id, key, source, type});
  });
  const selected = (type, flag) => rows.find(row => {const entry = entries.get(row.id);return row.type === type && entry[flag];})?.id || null;
  return {state: {rows, order: {audio: rows.filter(row => row.type === 'audio').map(row => row.id), subtitle: rows.filter(row => row.type === 'subtitle').map(row => row.id)}, defaultAudio: selected('audio', 'default'), forcedAudio: selected('audio', 'forced'), defaultSubtitle: selected('subtitle', 'default'), forcedSubtitle: selected('subtitle', 'forced')}, entries};
}

function bulkClonePayload(path, details, saved) {
  const current = cloneDetailsState(details);
  if (!compatibleCloneState(saved.before, current.state)) throw new Error('Stream properties changed after preview');
  const beforeById = new Map(saved.before.rows.map(row => [row.id, row])), afterById = new Map(saved.after.rows.map(row => [row.id, row]));
  const tracks = [], external = [], remove = [];
  for (const [id, entry] of current.entries) {
    const before = beforeById.get(id), after = afterById.get(id);
    if (after.removed) remove.push(entry.key);
    if (entry.source === 'external') external.push({path: entry.path, embed: after.embed, language: after.language, region: after.region, title: after.title, forced: false});
    else {
      const update = {codec_type: entry.type, type_index: entry.type_index};
      if (before.language !== after.language || before.region !== after.region) { update.language = after.language; update.region = after.region; }
      if (before.title !== after.title) update.title = after.title;
      if (Object.keys(update).length > 2) tracks.push(update);
    }
  }
  const order = [...saved.after.order.audio, ...saved.after.order.subtitle].map(id => {const entry = current.entries.get(id);return entry.source === 'external' ? {source: 'external', codec_type: entry.type, path: entry.path} : {source: 'embedded', codec_type: entry.type, type_index: entry.type_index};});
  const actualKey = id => id ? current.entries.get(id)?.key || null : null;
  return {path, tracks, external_subtitles: external, order, default_audio: actualKey(saved.after.defaultAudio), forced_audio: actualKey(saved.after.forcedAudio), default_subtitle: actualKey(saved.after.defaultSubtitle), forced_subtitle: actualKey(saved.after.forcedSubtitle), remove};
}

async function applyBulkClone(event) {
  event.preventDefault();
  if (!bulkCloneInspection?.count) return;
  const inspection = bulkCloneInspection, button = event.target.querySelector('[type=submit]'), closeButtons = [...document.querySelectorAll('[data-close-bulk-clone]')];
  button.disabled = true; closeButtons.forEach(item => item.disabled = true);
  let completed = 0;
  try {
    for (const path of inspection.candidates) {
      const label = listedEpisodeLabels().get(path) || path.split('/').pop();
      $('#bulk-clone-progress').innerHTML = `<strong>Editing episode ${completed + 1} of ${inspection.count}</strong><small>${esc(label)}</small>`;
      const details = await api(`/api/v13/media/details?path=${encodeURIComponent(path)}`);
      const payload = bulkClonePayload(path, details, inspection.saved);
      await api('/api/v7/media/edit', {method: 'POST', body: JSON.stringify(payload)});
      document.dispatchEvent(new CustomEvent('episode-session-changed', {detail: {path}}));
      completed++;
    }
    const beforeMetadata = new Map(inspection.saved.before.rows.map(row => [row.id, row]));
    const metadata = inspection.saved.after.rows.flatMap(row => ['language', 'region', 'title'].filter(field => beforeMetadata.get(row.id)[field] !== row[field] && row[field].trim()).map(field => ({field, value: row[field].trim()})));
    await offerSavedValues(Array.from({length: completed}, () => metadata).flat());
    $('#bulk-clone-progress').innerHTML = `<strong>Complete</strong><small>${completed} episodes updated successfully</small>`;
    toast(`${completed} episodes updated from the last change`);
    button.disabled = true;
    $('#bulk-clone-form [data-close-bulk-clone]:not(.icon-close)').textContent = 'Close';
    scheduleBulkCloneInspection();
  } catch (error) {
    $('#bulk-clone-progress').innerHTML = `<strong class="error">Stopped after ${completed} episodes</strong><small>${esc(error.message)}</small>`;
    toast(error.message, true);
  } finally {
    closeButtons.forEach(item => item.disabled = false);
    button.disabled = completed === inspection.count;
  }
}

document.addEventListener('media-properties-applied', scheduleBulkCloneInspection);
window.addEventListener('storage', event => {if (event.key === LAST_CHANGE_KEY) scheduleBulkCloneInspection()});
ensureBulkCloneUi();
scheduleBulkCloneInspection();
