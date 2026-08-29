let streamFilenameOriginal = '';

function mediaBasename(path) {
  return String(path || '').split('/').pop() || '';
}

function ensureStreamFilenameEditor() {
  let editor = $('#stream-filename-editor');
  if (editor) return editor;
  $('#selected-file').insertAdjacentHTML('afterend', '<label id="stream-filename-editor" class="stream-filename-editor"><span>Original filename</span><small id="stream-filename-original" class="stream-filename-original"></small><span>Filename</span><input id="stream-filename" type="text" autocomplete="off" spellcheck="false"></label>');
  editor = $('#stream-filename-editor');
  $('#stream-filename').addEventListener('input', updateQueuedChangeLabels);
  return editor;
}

function setStreamFilename(path) {
  ensureStreamFilenameEditor();
  streamFilenameOriginal = mediaBasename(path);
  $('#stream-filename').value = movieImportMode?.editing ? (movieImportMode.filename || streamFilenameOriginal) : streamFilenameOriginal;
  $('#stream-filename-original').textContent = streamFilenameOriginal;
}

function requestedStreamFilename() {
  return $('#stream-filename')?.value.trim() || '';
}

function streamFilenameChanged() {
  return Boolean(streamFilenameOriginal && requestedStreamFilename() && requestedStreamFilename() !== streamFilenameOriginal);
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
