function alternativeTitleHint(item) {
  const values = item?.alternative_titles || [];
  return values.length ? `Alternative titles:\n${values.join('\n')}` : '';
}

let selectedTvShowRequest=0;
let selectedTvShowAbort=null;
async function loadSelectedTvShow(show){
  if(!show)return;
  const request=++selectedTvShowRequest;
  selectedTvShowAbort?.abort();
  const controller=new AbortController();
  selectedTvShowAbort=controller;
  const selectedId=String(show.id);
  try{
    const details=await api("/api/v19/tv?show_id="+encodeURIComponent(selectedId),{signal:controller.signal});
    // A slower response for a previously clicked show must never overwrite the
    // show the user selected most recently.
    if(request!==selectedTvShowRequest||String(state.currentShow?.id)!==selectedId)return;
    const fresh=details.find(item=>String(item.id)===selectedId)||details[0];
    if(fresh){Object.assign(show,fresh);window._preserveShowSelection=true;renderShows();renderEpisodes()}
  }catch(error){
    if(error?.name!=='AbortError'&&request===selectedTvShowRequest)toast(error.message,true);
  }finally{
    if(request===selectedTvShowRequest)selectedTvShowAbort=null;
  }
}

window.movieMatchesStatusFilters = function (file) {
  const reviewState = $('#movie-reviewed-filter')?.dataset.reviewState || 'all';
  const notesState = $('#movie-notes-filter')?.dataset.notesState || 'all';
  const plexState = $('#movie-plex-filter')?.dataset.plexState || 'all';
  const detectionState = $('#movie-detection-filter')?.dataset.detectionState || 'all';
  const finalState = $('#movie-final-filter')?.dataset.finalState || 'all';
  return (reviewState === 'all' || (reviewState === 'reviewed' && file.reviewed) || (reviewState === 'not_reviewed' && !file.reviewed))
    && (notesState === 'all' || (notesState === 'has_notes' && Boolean(file.note)) || (notesState === 'no_notes' && !file.note))
    && (plexState === 'all' || (plexState === 'changed' && file.plex_sync_change) || (plexState === 'not_changed' && !file.plex_sync_change))
    && (detectionState === 'all' || (detectionState === 'has_detection' && hasDetectionDiscrepancy(file)) || (detectionState === 'no_detection' && !hasDetectionDiscrepancy(file)))
    && (finalState === 'all' || (finalState === 'final' && file.final_version) || (finalState === 'not_final' && !file.final_version));
};
window.movieRowHTML = function (file) {
  const badges = `${file.note ? `<span class="note-tag" title="${attr(file.note)}">i</span>` : ''}${file.reviewed ? `<span class="reviewed-tag" title="Reviewed">✓</span>` : ''}${file.plex_sync_change ? `<span class="plex-sync-tag" title="Plex Sync Change">P</span>` : ''}${file.final_version ? `<button type="button" class="final-version-tag" data-final-toggle="movie" data-final-path="${attr(file.path)}" title="Final version · click to unfreeze">F</button>` : ''}${typeof reportConfidenceDot === 'function' ? reportConfidenceDot(file.portuguese_detection_confidence,file.portuguese_detection_metadata,file.portuguese_detection_language,'subtitle',file.portuguese_detection_no_confidence,file.portuguese_detection_region) : ''}${file.audio_detection_confidence >= 0.6 && typeof reportConfidenceDot === 'function' ? reportConfidenceDot(file.audio_detection_confidence,file.audio_detection_metadata,file.audio_detection_language,'audio') : ''}`;
  return `<tr><td><strong class="movie-title title-with-alternatives" title="${attr(alternativeTitleHint(file))}"><span class="movie-primary-title">${esc(movieTitle(file))}</span><span class="movie-title-badges">${badges}</span></strong></td><td>${Number(file.year) > 0 ? esc(file.year) : '—'}</td><td>${bytes(file.size)}</td><td><button class="edit-file" ${file.final_version ? 'disabled title="Final version · click F to unfreeze"' : ''} data-path="${attr(file.path)}" data-label="${attr(movieTitle(file))}">Stream properties</button></td></tr>`;
};

renderMovies = function () {
  const files = state.movies.filter(file => matches([movieTitle(file), ...(file.alternative_titles || [])].join(' '), $('#movie-search').value) && movieMatchesStatusFilters(file));
  $('#movies-empty').style.display = files.length ? 'none' : 'block';
  $('#movies-empty').textContent = state.movies.length ? 'No matching movies.' : 'No movies found.';
  $('#movie-list').innerHTML = files.map(movieRowHTML).join('');
  wireEditors();
};

renderShows = function () {
  const reviewState = $('#tv-reviewed-filter')?.dataset.reviewState || 'all';
  const notesState = $('#tv-notes-filter')?.dataset.notesState || 'all';
  const plexState = $('#tv-plex-filter')?.dataset.plexState || 'all';
  const indexState = $('#tv-index-filter')?.dataset.indexState || 'all';
  const detectionState = $('#tv-detection-filter')?.dataset.detectionState || 'all';
  const finalState = $('#tv-final-filter')?.dataset.finalState || 'all';
  const shows = state.shows.filter(show => matches([show.name, ...(show.alternative_titles || [])].join(' '), $('#show-search').value)
    && (reviewState === 'all' || (reviewState === 'reviewed' && show.reviewed) || (reviewState === 'not_reviewed' && !show.reviewed))
    && (notesState === 'all' || (notesState === 'has_notes' && Boolean(show.note)) || (notesState === 'no_notes' && !show.note))
    && (plexState === 'all' || (plexState === 'changed' && show.plex_sync_change) || (plexState === 'not_changed' && !show.plex_sync_change))
    && (indexState === 'all' || (indexState === 'busy' && show.index_busy) || (indexState === 'not_busy' && !show.index_busy))
    && (detectionState === 'all' || (detectionState === 'has_detection' && hasDetectionDiscrepancy(show)) || (detectionState === 'no_detection' && !hasDetectionDiscrepancy(show)))
    && (finalState === 'all' || (finalState === 'final' && show.final_version) || (finalState === 'not_final' && !show.final_version)));
  $('#tv-empty').style.display = shows.length ? 'none' : 'block';
  $('#show-list').innerHTML = shows.map(show => `<button class="show-card ${state.currentShow?.id === show.id ? 'active' : ''}" data-id="${attr(show.id)}"><strong class="title-with-alternatives" title="${attr(alternativeTitleHint(show))}">${esc(clean(show.name))}${show.note ? ` <span class="note-tag" title="${attr(show.note)}">i</span>` : ''}${show.reviewed ? ` <span class="reviewed-tag" title="Reviewed">✓</span>` : ''}${show.plex_sync_change ? ` <span class="plex-sync-tag" role="button" tabindex="0" data-plex-reset="${attr(show.id)}" title="Plex Sync Change · click to clear">P</span>` : ''}${show.final_version ? ` <span class="final-version-tag" role="button" tabindex="0" data-final-toggle="show" data-final-key="${attr(show.id)}" title="Final version · click to unfreeze all episodes">F</span>` : ''}${typeof reportConfidenceDot === 'function' ? reportConfidenceDot(show.portuguese_detection_confidence,show.portuguese_detection_metadata,show.portuguese_detection_language,'subtitle',show.portuguese_detection_no_confidence,show.portuguese_detection_region) : ''}</strong><small>${show.episode_count} episodes · ${esc(show.root_name)}</small></button>`).join('');
  document.querySelectorAll('.show-card').forEach(button => button.onclick = () => {state.currentShow = state.shows.find(show => show.id === button.dataset.id);state.currentSeason = '*';$('#episode-search').value = '';window._preserveShowSelection = true;renderShows();loadSelectedTvShow(state.currentShow)});
};

document.addEventListener('click', async event => {
  const badge = event.target.closest('[data-plex-reset]');
  if (!badge) return;
  event.preventDefault();
  event.stopPropagation();
  if (badge.dataset.busy === 'true') return;
  const show = state.shows.find(item => String(item.id) === badge.dataset.plexReset);
  if (!show) return;
  badge.dataset.busy = 'true';
  try {
    const result = await api('/api/v86/note', {method:'PUT', body:JSON.stringify({entity_type:'tv',entity_key:show.id,note:show.note||'',plex_sync_change:false})});
    show.plex_sync_change = Boolean(result.plex_sync_change);
    window._preserveShowSelection = true;
    renderShows();
    renderEpisodes();
    toast('Plex Sync Change cleared');
  } catch (error) {
    toast(error.message, true);
  } finally {
    delete badge.dataset.busy;
  }
}, true);

document.addEventListener('keydown', event => {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  const badge = event.target.closest?.('[data-final-toggle="show"],[data-plex-reset]');
  if (!badge) return;
  event.preventDefault();
  badge.click();
});

const titleRenderEpisodes = renderEpisodes;
renderEpisodes = function () {
  titleRenderEpisodes();
  const heading = $('#show-title');
  heading.title = state.currentShow ? alternativeTitleHint(state.currentShow) : '';
  heading.classList.toggle('title-with-alternatives', Boolean(heading.title));
};

/* Final-version badges are deliberate controls, not passive status labels. */
document.addEventListener('click', async event => {
  const button = event.target.closest('[data-final-toggle]');
  if (!button) return;
  event.preventDefault();
  event.stopPropagation();
  if (button.disabled || button.dataset.busy === 'true') return;
  const kind = button.dataset.finalToggle;
  const next = false; // the badge is rendered only while the target is final
  button.dataset.busy = 'true';
  try {
    if (window.tvShowEditSession && (kind === 'show' || kind === 'episode')) {
      await window.stageTvDraftFinal(kind, kind === 'show' ? button.dataset.finalKey : button.dataset.finalPath, next);
      toast('Final-version change staged in the TV-show draft');
      return;
    }
    let result;
    if (kind === 'show') {
      result = await api('/api/v86/final-version/show', {method:'PUT', body:JSON.stringify({entity_key:button.dataset.finalKey, final_version:next})});
      await loadTv();
      toast(`Final version removed from ${result.episodes} episode${result.episodes === 1 ? '' : 's'}`);
    } else if (kind === 'episode') {
      result = await api('/api/v86/final-version', {method:'PUT', body:JSON.stringify({path:button.dataset.finalPath, final_version:next})});
      await loadTv();
      toast('Episode Final version removed');
    } else {
      result = await api('/api/v86/final-version', {method:'PUT', body:JSON.stringify({path:button.dataset.finalPath, final_version:next})});
      await loadMovies();
      toast('Movie Final version removed');
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    delete button.dataset.busy;
  }
}, true);

/* Video-stream title metadata shown beside movie and episode titles. */
(function () {
  const videoTitles = Object.create(null);
  let activePath = '';
  let activeRender = null;
  function ensureDialog() {
    let dialog = document.querySelector('#video-title-dialog');
    if (dialog) return dialog;
    document.body.insertAdjacentHTML('beforeend', '<dialog id="video-title-dialog"><form method="dialog" id="video-title-form"><div class="dialog-title"><div><h2>Video title metadata</h2><p>Edit or clear the title on the video stream.</p></div><button type="button" class="icon-close" data-video-title-close aria-label="Close">×</button></div><div style="padding:18px"><label>Video title<input id="video-title-input" type="text" maxlength="1000" autocomplete="off" style="width:100%"></label></div><div class="dialog-actions"><button type="button" data-video-title-clear>Clear</button><button type="button" data-video-title-cancel>Cancel</button><button type="submit" class="primary">Save</button></div></form></dialog>');
    dialog = document.querySelector('#video-title-dialog');
    const close = () => dialog.close();
    dialog.querySelector('[data-video-title-close]').onclick = close;
    dialog.querySelector('[data-video-title-cancel]').onclick = close;
    dialog.querySelector('[data-video-title-clear]').onclick = () => saveVideoTitle('');
    dialog.querySelector('form').onsubmit = event => { event.preventDefault(); saveVideoTitle(dialog.querySelector('#video-title-input').value); };
    return dialog;
  }
  function openVideoTitle(path, render) {
    const dialog = ensureDialog();
    activePath = path; activeRender = render;
    dialog.querySelector('#video-title-input').value = videoTitles[path]?.title || '';
    dialog.showModal();
    dialog.querySelector('#video-title-input').focus({preventScroll:true});
  }
  async function saveVideoTitle(title) {
    const dialog = document.querySelector('#video-title-dialog'), button = dialog?.querySelector('button[type=submit]');
    if (!activePath || !dialog) return;
    button.disabled = true;
    try {
      const result = await api('/api/v19/video-title/edit', {method:'POST', body:JSON.stringify({path:activePath, title})});
      videoTitles[activePath] = {...(videoTitles[activePath] || {}), title: result.title};
      dialog.close(); activeRender?.();
      toast(result.title ? 'Video title metadata updated' : 'Video title metadata cleared');
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  }
  function decorate(container, render) {
    container?.querySelectorAll('.edit-file').forEach(button => {
      const path = button.dataset.path, item = videoTitles[path];
      const title = button.closest('tr')?.querySelector('.movie-title,.episode-title');
      if (!title || !item?.title || title.querySelector('.video-title-metadata')) return;
      title.insertAdjacentHTML('beforeend', ` <button type="button" class="video-title-metadata" title="Edit video stream title metadata" data-video-title-path="${attr(path)}">/ ${esc(item.title)}</button>`);
      title.querySelector('[data-video-title-path]').onclick = event => { event.preventDefault(); event.stopPropagation(); openVideoTitle(path, render); };
    });
  }
  async function hydrate(paths, render) {
    const missing = [...new Set(paths)].filter(path => !(path in videoTitles));
    if (!missing.length) { decorate(document, render); return; }
    try {
      const result = await api('/api/v19/video-titles', {method:'POST', body:JSON.stringify({paths:missing})});
      for (const path of missing) videoTitles[path] = result.items?.[path] || {title:'', index:0};
      render();
    } catch (_) { /* Listing remains usable if metadata inspection fails. */ }
  }
  const oldMovieRender = renderMovies;
  renderMovies = function () {
    oldMovieRender();
    const paths = [...document.querySelectorAll('#movie-list .edit-file')].map(button => button.dataset.path);
    decorate(document.querySelector('#movie-list'), renderMovies);
    hydrate(paths, renderMovies);
  };
  const oldEpisodeRender = renderEpisodes;
  renderEpisodes = function () {
    oldEpisodeRender();
    const paths = [...document.querySelectorAll('#episode-list .edit-file')].map(button => button.dataset.path);
    decorate(document.querySelector('#episode-list'), renderEpisodes);
    hydrate(paths, renderEpisodes);
  };
})();
