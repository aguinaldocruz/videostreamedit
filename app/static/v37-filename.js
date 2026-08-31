let streamFilenameOriginal = '';
let streamFilenameEditEnabled = false;

function mediaBasename(path) {
  return String(path || '').split('/').pop() || '';
}

function ensureStreamFilenameEditor() {
  let editor = $('#stream-filename-editor');
  if (editor) return editor;
  const indicator = ensureMediaPathIndicator();
  indicator.insertAdjacentHTML('afterend', '<button id="stream-filename-edit" class="filename-edit-toggle" type="button" aria-label="Edit filename" title="Edit filename" aria-pressed="false">e</button>');
  $('#stream-filename-edit').insertAdjacentHTML('afterend', '<div id="stream-filename-editor" class="stream-filename-editor filename-edit-disabled"><label class="stream-filename-label" for="stream-filename">New filename</label><input id="stream-filename" type="text" autocomplete="off" spellcheck="false"></div>');
  editor = $('#stream-filename-editor');
  $('#stream-filename').addEventListener('input', updateQueuedChangeLabels);
  $('#stream-filename-edit').addEventListener('click', toggleStreamFilenameEditing);
  return editor;
}

function toggleStreamFilenameEditing() {
  streamFilenameEditEnabled = !streamFilenameEditEnabled;
  const editor = $('#stream-filename-editor'), button = $('#stream-filename-edit'), input = $('#stream-filename');
  editor.classList.toggle('filename-edit-disabled', !streamFilenameEditEnabled);
  button.classList.toggle('active', streamFilenameEditEnabled);
  button.setAttribute('aria-pressed', String(streamFilenameEditEnabled));
  if (!streamFilenameEditEnabled) {
    input.value = streamFilenameOriginal;
  } else {
    input.focus({preventScroll: true});
    const dot = input.value.lastIndexOf('.');
    input.setSelectionRange(0, dot > 0 ? dot : input.value.length);
  }
  updateQueuedChangeLabels();
}

function setStreamFilename(path) {
  ensureStreamFilenameEditor();
  streamFilenameOriginal = mediaBasename(path);
  streamFilenameEditEnabled = false;
  $('#stream-filename-editor').classList.add('filename-edit-disabled');
  $('#stream-filename-edit').classList.remove('active');
  $('#stream-filename-edit').setAttribute('aria-pressed', 'false');
  $('#stream-filename').value = movieImportMode?.editing ? (movieImportMode.filename || streamFilenameOriginal) : streamFilenameOriginal;
}

function requestedStreamFilename() {
  return streamFilenameEditEnabled ? ($('#stream-filename')?.value.trim() || '') : streamFilenameOriginal;
}

function streamFilenameChanged() {
  return Boolean(streamFilenameEditEnabled && streamFilenameOriginal && requestedStreamFilename() && requestedStreamFilename() !== streamFilenameOriginal);
}

function validateStreamFilename() {
  const filename = requestedStreamFilename();
  if (!filename || filename.includes('/') || filename.includes('\\') || filename === '.' || filename === '..') throw new Error('Filename cannot be empty or contain folders');
  const originalExtension = streamFilenameOriginal.includes('.') ? streamFilenameOriginal.slice(streamFilenameOriginal.lastIndexOf('.')).toLocaleLowerCase() : '';
  const extension = filename.includes('.') ? filename.slice(filename.lastIndexOf('.')).toLocaleLowerCase() : '';
  if (extension !== originalExtension) throw new Error(`Filename must keep the ${originalExtension || 'original'} extension`);
  return filename;
}

function adoptRenamedMediaPath(oldPath, newPath) {
  state.selectedPath = newPath;
  if (typeof mediaNavigation !== 'undefined') mediaNavigation.forEach(item => {if (item.path === oldPath) item.path = newPath});
  document.querySelectorAll('[data-path]').forEach(item => {if (item.dataset.path === oldPath) item.dataset.path = newPath});
  setMediaPathIndicator(newPath);
}

const filenameQueuedChangeCount = queuedChangeCount;
queuedChangeCount = function () {
  return filenameQueuedChangeCount() + (streamFilenameChanged() ? 1 : 0);
};

const filenameOpenEditor = openEditor;
openEditor = async function (path, label) {
  const result = await filenameOpenEditor(path, label);
  setStreamFilename(path);
  updateQueuedChangeLabels();
  const dialog = $('#stream-dialog');
  closeSavedValueMenu();
  if (dialog?.open) dialog.focus({preventScroll: true});
  return result;
};

ensureStreamFilenameEditor();
