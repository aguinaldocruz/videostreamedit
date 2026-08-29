const CHANGE_HISTORY_KEY = 'videostreamedit.stream-change-history.v1';
let bulkHistoryMatches = [];

function templateFingerprint(template) {
  return JSON.stringify({before: template.before, after: template.after});
}

function readChangeHistory() {
  let history = [];
  try { history = JSON.parse(localStorage.getItem(CHANGE_HISTORY_KEY) || '[]'); }
  catch (_) { history = []; }
  const legacy = readLastChange();
  if (legacy && !history.some(template => templateFingerprint(template) === templateFingerprint(legacy))) history.unshift(legacy);
  return history.filter(template => template?.before && template?.after).slice(0, 10);
}

function saveChangeHistory(template) {
  const fingerprint = templateFingerprint(template);
  const history = readChangeHistory().filter(item => templateFingerprint(item) !== fingerprint);
  history.unshift(template);
  localStorage.setItem(CHANGE_HISTORY_KEY, JSON.stringify(history.slice(0, 10)));
}

function compatibleHistoryTemplates(current) {
  return readChangeHistory().filter(template => compatibleCloneState(template.before, current) && removalTemplateMatches(template, current));
}

function templateSummary(template) {
  const time = template.savedAt ? new Date(template.savedAt).toLocaleString() : 'Saved change';
  return {time, detail: (template.changes || []).join(' · ') || 'Stream property changes'};
}

function ensureTemplateChoiceDialog() {
  if ($('#template-choice-dialog')) return;
  document.body.insertAdjacentHTML('beforeend', '<dialog id="template-choice-dialog" class="template-choice-dialog"><div class="dialog-title"><div><h2>Choose a change template</h2><p id="template-choice-scope"></p></div><button type="button" class="icon-close" data-close-template-choice>×</button></div><div id="template-choice-list" class="template-choice-list"></div><div class="dialog-actions"><button type="button" data-close-template-choice>Cancel</button></div></dialog>');
  document.querySelectorAll('[data-close-template-choice]').forEach(button => button.onclick = () => $('#template-choice-dialog').close());
}

function chooseIndividualTemplate(templates) {
  if (templates.length === 1) { chooseCloneMode(templates[0]); return; }
  ensureTemplateChoiceDialog();
  $('#template-choice-scope').textContent = `${templates.length} saved templates match this media`;
  $('#template-choice-list').innerHTML = templates.map((template, index) => {const summary = templateSummary(template);return `<button type="button" data-template-index="${index}"><strong>${esc(summary.time)}</strong><small>${esc(summary.detail)}</small></button>`;}).join('');
  $('#template-choice-list').querySelectorAll('button').forEach(button => button.onclick = () => {const template = templates[Number(button.dataset.templateIndex)];$('#template-choice-dialog').close();chooseCloneMode(template)});
  $('#template-choice-dialog').showModal();
}

const historyUpdateCloneButton = updateCloneButton;
updateCloneButton = function () {
  historyUpdateCloneButton();
  const button = $('#clone-last-change');
  if (!editorBaseline) { button.classList.add('hidden'); return; }
  const templates = compatibleHistoryTemplates(cloneStreamState());
  button.classList.toggle('hidden', !templates.length);
  if (!templates.length) { button.onclick = null; return; }
  button.textContent = templates.length === 1 ? 'Clone saved change' : `Clone saved change · ${templates.length} templates`;
  button.title = templates.length === 1 ? `Repeat:\n${(templates[0].changes || []).join('\n')}` : `${templates.length} compatible saved change templates`;
  button.onclick = () => chooseIndividualTemplate(templates);
};

document.addEventListener('media-properties-applied', event => {
  const template = readLastChange();
  if (!template) return;
  const summary = templateSummary(template);
  const saveTemplate = window.confirm(`Save this change template to history?

${summary.detail}`);
  if (!saveTemplate) { updateCloneButton(); scheduleBulkCloneInspection(); return; }
  saveChangeHistory({...template, sourcePath: event.detail.path, sourceLabel: $('#selected-file').textContent});
  updateCloneButton();
  scheduleBulkCloneInspection();
});

async function inspectBulkCloneHistory() {
  ensureBulkCloneUi();
  const button = $('#bulk-clone-button'), templates = readChangeHistory(), paths = listedEpisodePaths(), generation = ++bulkCloneGeneration;
  bulkCloneInspection = null; bulkHistoryMatches = [];
  button.classList.add('hidden');
  if (!state.currentShow || !templates.length || !paths.length) return;
  button.disabled = true; button.textContent = 'Checking saved changes…'; button.classList.remove('hidden');
  try {
    const result = await api('/api/v25/tv/clone/history/inspect', {method: 'POST', body: JSON.stringify({paths, templates})});
    if (generation !== bulkCloneGeneration) return;
    bulkHistoryMatches = result.matches.map(match => ({...match, saved: templates[match.template_index]}));
    if (!bulkHistoryMatches.length) { button.classList.add('hidden'); return; }
    const episodePaths = new Set(bulkHistoryMatches.flatMap(match => match.candidates));
    button.textContent = `Clone saved change · ${episodePaths.size} episode${episodePaths.size === 1 ? '' : 's'}`;
    button.disabled = false;
    button.onclick = chooseBulkHistoryTemplate;
  } catch (error) {
    if (generation === bulkCloneGeneration) { button.classList.add('hidden'); console.error('Change history inspection failed', error); }
  }
}

inspectBulkCloneCandidates = inspectBulkCloneHistory;

function chooseBulkHistoryTemplate() {
  if (bulkHistoryMatches.length === 1) {
    const match = bulkHistoryMatches[0];
    bulkCloneInspection = {count: match.count, candidates: match.candidates, saved: match.saved};
    openBulkCloneReview();
    return;
  }
  ensureTemplateChoiceDialog();
  $('#template-choice-scope').textContent = `${bulkHistoryMatches.length} saved templates match listed episodes`;
  $('#template-choice-list').innerHTML = bulkHistoryMatches.map((match, index) => {const summary = templateSummary(match.saved);return `<button type="button" data-template-index="${index}"><strong>${match.count} episode${match.count === 1 ? '' : 's'} · ${esc(summary.time)}</strong><small>${esc(summary.detail)}</small></button>`;}).join('');
  $('#template-choice-list').querySelectorAll('button').forEach(button => button.onclick = () => {const match = bulkHistoryMatches[Number(button.dataset.templateIndex)];bulkCloneInspection = {count: match.count, candidates: match.candidates, saved: match.saved};$('#template-choice-dialog').close();openBulkCloneReview()});
  $('#template-choice-dialog').showModal();
}

scheduleBulkCloneInspection();
