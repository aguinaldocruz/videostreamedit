function markFastDefault(input, value, reason) {
  if (input.value === value) return false;
  if (!Object.hasOwn(input.dataset, 'fastDefaultFrom')) input.dataset.fastDefaultFrom = input.value;
  input.value = value;
  input.dataset.dirty = 'true';
  input.classList.add('fast-default-suggestion');
  input.title = reason;
  input.dispatchEvent(new Event('input', {bubbles: true}));
  return true;
}

function applyFastStreamDefaults() {
  let changed = 0;
  document.querySelectorAll('#stream-content .stream-row').forEach(row => {
    const language = row.querySelector('[name=language]'), region = row.querySelector('[name=region]');
    const code = language.value.trim().toLowerCase();
    if (code === 'por') {
      if (markFastDefault(language, 'pt', 'Suggested default: por → pt')) changed++;
      if (markFastDefault(region, 'BR', 'Suggested default region for Portuguese: BR')) changed++;
    } else if (code === 'pt') {
      if (markFastDefault(region, 'BR', 'Suggested default region for Portuguese: BR')) changed++;
    } else if (code === 'eng') {
      if (markFastDefault(language, 'en', 'Suggested default: eng → en')) changed++;
    }
    for (const input of [language, region]) {
      if (input.dataset.fastDefaultListener === 'true') continue;
      input.dataset.fastDefaultListener = 'true';
      input.addEventListener('input', event => {
        if (!event.isTrusted || !Object.hasOwn(input.dataset, 'fastDefaultFrom')) return;
        delete input.dataset.fastDefaultFrom;
        input.classList.remove('fast-default-suggestion');
        input.removeAttribute('title');
      });
    }
  });
  if (changed) updateQueuedChangeLabels();
}

function projectedStateWithoutFastDefaults() {
  const projected = cloneStreamState();
  const rows = [...document.querySelectorAll('#stream-content .stream-row')];
  projected.rows.forEach((row, index) => {
    for (const field of ['language', 'region']) {
      const input = rows[index].querySelector(`[name=${field}]`);
      if (Object.hasOwn(input.dataset, 'fastDefaultFrom')) row[field] = input.dataset.fastDefaultFrom;
    }
  });
  return projected;
}

function restoreFastDefaults() {
  document.querySelectorAll('#stream-content [data-fast-default-from]').forEach(input => {
    input.value = input.dataset.fastDefaultFrom;
    delete input.dataset.fastDefaultFrom;
    input.dataset.dirty = 'false';
    input.classList.remove('fast-default-suggestion');
    input.removeAttribute('title');
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dataset.dirty = 'false';
  });
}

const fastDefaultsApplyLastChange = applyLastChange;
applyLastChange = function (saved) {
  const current = cloneStreamState();
  if (!compatibleCloneState(saved.before, current)) {
    const projected = projectedStateWithoutFastDefaults();
    if (compatibleCloneState(saved.before, projected) && removalTemplateMatches(saved, projected)) restoreFastDefaults();
  }
  return fastDefaultsApplyLastChange(saved);
};

const fastDefaultsOpenEditor = openEditor;
openEditor = async function (path, label) {
  await fastDefaultsOpenEditor(path, label);
  if (document.querySelector('#stream-content .stream-row')) applyFastStreamDefaults();
};
