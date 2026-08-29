let changedEpisodePaths = new Set();
let changedEpisodeShowId = null;

function resetChangedEpisodeSession() {
  changedEpisodePaths.clear();
  changedEpisodeShowId = null;
  decorateChangedEpisodes();
}

function decorateChangedEpisodes() {
  document.querySelectorAll('#episode-list .edit-file').forEach(button => {
    const row = button.closest('tr'), changed = changedEpisodePaths.has(button.dataset.path);
    row.classList.toggle('episode-session-changed', changed);
    let badge = row.querySelector('.episode-changed-badge');
    if (changed && !badge) {
      badge = document.createElement('span');
      badge.className = 'episode-changed-badge';
      badge.textContent = 'Changed';
      row.querySelector('.episode-title')?.insertAdjacentElement('afterend', badge);
    } else if (!changed) badge?.remove();
  });
}

function markEpisodeChanged(path) {
  if (!state.currentShow) return;
  const belongsToShow = state.currentShow.seasons.some(season => season.episodes.some(episode => episode.path === path));
  if (!belongsToShow) return;
  if (changedEpisodeShowId !== state.currentShow.id) changedEpisodePaths.clear();
  changedEpisodeShowId = state.currentShow.id;
  changedEpisodePaths.add(path);
  decorateChangedEpisodes();
}

document.addEventListener('media-properties-applied', event => markEpisodeChanged(event.detail.path));
document.addEventListener('episode-session-changed', event => markEpisodeChanged(event.detail.path));

const sessionChangedRenderEpisodes = renderEpisodes;
renderEpisodes = function () {
  const nextShowId = state.currentShow?.id || null;
  if (changedEpisodeShowId && nextShowId !== changedEpisodeShowId) resetChangedEpisodeSession();
  sessionChangedRenderEpisodes();
  decorateChangedEpisodes();
};

document.querySelectorAll('[data-page]').forEach(button => button.addEventListener('click', () => {
  if (button.dataset.page !== 'tv') resetChangedEpisodeSession();
}));

function ensureCloneChoiceDialog() {
  if ($('#clone-choice-dialog')) return;
  document.body.insertAdjacentHTML('beforeend', '<dialog id="clone-choice-dialog" class="clone-choice-dialog"><div class="dialog-title"><div><h2>Clone last change</h2><p id="clone-choice-scope"></p></div><button type="button" class="icon-close" data-close-clone-choice>×</button></div><div class="clone-choice-content"><p>The same previous values were found in multiple listed episodes. Choose how to continue.</p></div><div class="dialog-actions"><button type="button" data-close-clone-choice>Cancel</button><button type="button" id="clone-one-media">Clone this episode only</button><button type="button" id="clone-bulk-review" class="primary">Review bulk clone</button></div></dialog>');
  document.querySelectorAll('[data-close-clone-choice]').forEach(button => button.onclick = () => $('#clone-choice-dialog').close());
}

async function chooseCloneMode(saved) {
  const button = $('#clone-last-change'), paths = state.currentShow ? listedEpisodePaths() : [];
  if (!state.currentShow || paths.length < 2) { applyLastChange(saved); return; }
  button.disabled = true;
  button.textContent = 'Checking bulk matches…';
  try {
    const result = await api('/api/v22/tv/clone/inspect', {method: 'POST', body: JSON.stringify({paths, expected: saved.before})});
    if (result.count < 2) { applyLastChange(saved); return; }
    ensureCloneChoiceDialog();
    bulkCloneInspection = {...result, saved};
    $('#clone-choice-scope').textContent = `${result.count} compatible listed episodes are available`;
    $('#clone-one-media').onclick = () => { $('#clone-choice-dialog').close(); applyLastChange(saved); };
    $('#clone-bulk-review').onclick = () => { $('#clone-choice-dialog').close(); $('#stream-dialog').close(); openBulkCloneReview(); };
    $('#clone-choice-dialog').showModal();
  } catch (error) {
    toast(`Could not inspect bulk matches: ${error.message}`, true);
    applyLastChange(saved);
  } finally {
    button.disabled = false;
    button.textContent = 'Clone last change';
  }
}

const choiceUpdateCloneButton = updateCloneButton;
updateCloneButton = function () {
  choiceUpdateCloneButton();
  const button = $('#clone-last-change'), saved = readLastChange();
  if (saved && !button.classList.contains('hidden')) button.onclick = () => chooseCloneMode(saved);
};
