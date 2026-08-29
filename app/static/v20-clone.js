const LAST_CHANGE_KEY = 'videostreamedit.last-stream-change.v1';
let pendingLastChange = null;
let cloneEditorBaseline = null;

function cloneStreamState() {
  const counters = {}, rows = [], keyToId = new Map();
  document.querySelectorAll('#stream-content .stream-row').forEach(row => {
    const source = row.dataset.external === 'true' ? 'external' : 'embedded';
    const type = row.dataset.codecType;
    const counterKey = `${source}:${type}`;
    const position = counters[counterKey] || 0;
    counters[counterKey] = position + 1;
    const id = row.dataset.cloneId || `${counterKey}:${position}`;
    row.dataset.cloneId = id;
    keyToId.set(row.dataset.key, id);
    rows.push({
      id,
      source,
      type,
      codec: row.querySelector('.stream-kind small')?.textContent.trim() || '',
      language: row.querySelector('[name=language]').value,
      region: row.querySelector('[name=region]').value,
      title: row.querySelector('[name=title]').value,
      embed: row.querySelector('[name=embed]')?.checked || false,
      removed: row.querySelector('[name=remove]').checked
    });
  });
  const selected = name => {
    const key = document.querySelector(`#stream-content [name=${name}]:checked`)?.value;
    return key ? keyToId.get(key) || null : null;
  };
  return {
    rows,
    order: {
      audio: rows.filter(row => row.type === 'audio').map(row => row.id),
      subtitle: rows.filter(row => row.type === 'subtitle').map(row => row.id)
    },
    defaultAudio: selected('default-audio'),
    forcedAudio: selected('forced-audio'),
    defaultSubtitle: selected('default-subtitle'),
    forcedSubtitle: selected('forced-subtitle')
  };
}

function describeLastChange(before, after) {
  const descriptions = [], beforeById = new Map(before.rows.map(row => [row.id, row])), labels = {language: 'language', region: 'region', title: 'track name', embed: 'move inside', removed: 'remove'};
  after.rows.forEach((row, index) => {
    const old = beforeById.get(row.id), stream = `${row.type} ${indexForType(after.rows, index) + 1}`;
    for (const field of ['language', 'region', 'title', 'embed', 'removed']) {
      if (old[field] !== row[field]) descriptions.push(`${stream} ${labels[field]}: ${displayCloneValue(old[field])} → ${displayCloneValue(row[field])}`);
    }
  });
  for (const type of ['audio', 'subtitle']) if (before.order[type].join('|') !== after.order[type].join('|')) descriptions.push(`${type} stream order changed`);
  for (const field of ['defaultAudio', 'forcedAudio', 'defaultSubtitle', 'forcedSubtitle']) {
    if (before[field] !== after[field]) descriptions.push(`${field.replace(/([A-Z])/g, ' $1').toLowerCase()}: ${displayCloneValue(before[field])} → ${displayCloneValue(after[field])}`);
  }
  return descriptions;
}

function indexForType(rows, index) {
  return rows.slice(0, index).filter(row => row.type === rows[index].type).length;
}

function displayCloneValue(value) {
  if (value === '' || value === null || value === false) return 'untagged';
  if (value === true) return 'enabled';
  return String(value);
}

function compatibleCloneState(expected, current) {
  return JSON.stringify(expected) === JSON.stringify(current);
}

function readLastChange() {
  try { return JSON.parse(localStorage.getItem(LAST_CHANGE_KEY) || 'null'); }
  catch (_) { return null; }
}

function ensureCloneButton() {
  let button = $('#clone-last-change');
  if (button) return button;
  button = document.createElement('button');
  button.type = 'button';
  button.id = 'clone-last-change';
  button.className = 'clone-last-change hidden';
  button.textContent = 'Clone last change';
  const close = $('#stream-form .dialog-actions [data-close-stream]');
  close.insertAdjacentElement('beforebegin', button);
  return button;
}

function updateCloneButton() {
  const button = ensureCloneButton(), saved = readLastChange();
  const compatible = Boolean(saved && editorBaseline && compatibleCloneState(saved.before, cloneStreamState()));
  button.classList.toggle('hidden', !compatible);
  button.title = compatible ? `Repeat ${saved.changes.length} changes:\n${saved.changes.join('\n')}` : '';
  button.onclick = compatible ? () => applyLastChange(saved) : null;
}

function applyLastChange(saved) {
  if (!compatibleCloneState(saved.before, cloneStreamState())) { updateCloneButton(); return; }
  const currentRows = [...document.querySelectorAll('#stream-content .stream-row')];
  const rowById = new Map(cloneStreamState().rows.map((item, index) => [item.id, currentRows[index]]));
  saved.after.rows.forEach(item => {
    const row = rowById.get(item.id);
    for (const field of ['language', 'region', 'title']) {
      const input = row.querySelector(`[name=${field}]`);
      input.value = item[field];
      input.dataset.dirty = 'true';
      input.dispatchEvent(new Event('input', {bubbles: true}));
    }
    const embed = row.querySelector('[name=embed]');
    if (embed) { embed.checked = item.embed; embed.dispatchEvent(new Event('change', {bubbles: true})); }
    const remove = row.querySelector('[name=remove]');
    remove.checked = item.removed;
    remove.dispatchEvent(new Event('change', {bubbles: true}));
  });
  for (const type of ['audio', 'subtitle']) {
    const desired = saved.after.order[type].map(id => rowById.get(id));
    const slots = [...document.querySelectorAll(`#stream-content .stream-row[data-codec-type="${type}"]`)];
    if (slots.length) {
      const marker = document.createComment(`clone-${type}-order`);
      slots[0].parentNode.insertBefore(marker, slots[0]);
      desired.forEach(row => marker.parentNode.insertBefore(row, marker));
      marker.remove();
    }
  }
  const setTag = (name, id) => {
    document.querySelectorAll(`#stream-content [name=${name}]`).forEach(radio => radio.checked = false);
    if (id) {
      const row = rowById.get(id);
      const radio = row?.querySelector(`[name=${name}]`);
      if (radio) radio.checked = true;
    }
  };
  setTag('default-audio', saved.after.defaultAudio);
  setTag('forced-audio', saved.after.forcedAudio);
  setTag('default-subtitle', saved.after.defaultSubtitle);
  setTag('forced-subtitle', saved.after.forcedSubtitle);
  updateQueuedChangeLabels();
  toast(`${saved.changes.length} cloned change${saved.changes.length === 1 ? '' : 's'} queued for review`);
}

$('#stream-form').addEventListener('submit', () => {
  if (!editorBaseline || !queuedChangeCount()) { pendingLastChange = null; return; }
  const after = cloneStreamState(), before = structuredClone(cloneEditorBaseline);
  const changes = describeLastChange(before, after);
  pendingLastChange = changes.length ? {before, after, changes, savedAt: new Date().toISOString()} : null;
}, true);

document.addEventListener('media-properties-applied', () => {
  if (pendingLastChange) localStorage.setItem(LAST_CHANGE_KEY, JSON.stringify(pendingLastChange));
  pendingLastChange = null;
  updateCloneButton();
});

const cloneCaptureEditorBaseline = captureEditorBaseline;
captureEditorBaseline = function () {
  cloneCaptureEditorBaseline();
  cloneEditorBaseline = cloneStreamState();
  updateCloneButton();
};

ensureCloneButton();
