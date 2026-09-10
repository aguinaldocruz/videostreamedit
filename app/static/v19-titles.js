function alternativeTitleHint(item) {
  const values = item?.alternative_titles || [];
  return values.length ? `Alternative titles:\n${values.join('\n')}` : '';
}

renderMovies = function () {
  const reviewState = $('#movie-reviewed-filter')?.dataset.reviewState || 'all';
  const notesState = $('#movie-notes-filter')?.dataset.notesState || 'all';
  const files = state.movies.filter(file => matches([movieTitle(file), ...(file.alternative_titles || [])].join(' '), $('#movie-search').value)
    && (reviewState === 'all' || (reviewState === 'reviewed' && file.reviewed) || (reviewState === 'not_reviewed' && !file.reviewed))
    && (notesState === 'all' || (notesState === 'has_notes' && Boolean(file.note)) || (notesState === 'no_notes' && !file.note)));
  $('#movies-empty').style.display = files.length ? 'none' : 'block';
  $('#movies-empty').textContent = state.movies.length ? 'No matching movies.' : 'No movies found.';
  $('#movie-list').innerHTML = files.map(file => `<tr><td><strong class="movie-title title-with-alternatives" title="${attr(alternativeTitleHint(file))}">${esc(movieTitle(file))}${file.note ? ` <span class="note-tag" title="${attr(file.note)}">i</span>` : ''}${file.reviewed ? ` <span class="reviewed-tag" title="Reviewed">✓</span>` : ''}</strong></td><td>${esc(file.root_name)}</td><td>${bytes(file.size)}</td><td><button class="edit-file" data-path="${attr(file.path)}" data-label="${attr(movieTitle(file))}">Stream properties</button></td></tr>`).join('');
  wireEditors();
};

renderShows = function () {
  const reviewState = $('#tv-reviewed-filter')?.dataset.reviewState || 'all';
  const notesState = $('#tv-notes-filter')?.dataset.notesState || 'all';
  const shows = state.shows.filter(show => matches([show.name, ...(show.alternative_titles || [])].join(' '), $('#show-search').value)
    && (reviewState === 'all' || (reviewState === 'reviewed' && show.reviewed) || (reviewState === 'not_reviewed' && !show.reviewed))
    && (notesState === 'all' || (notesState === 'has_notes' && Boolean(show.note)) || (notesState === 'no_notes' && !show.note)));
  $('#tv-empty').style.display = shows.length ? 'none' : 'block';
  $('#show-list').innerHTML = shows.map(show => `<button class="show-card ${state.currentShow?.id === show.id ? 'active' : ''}" data-id="${attr(show.id)}"><strong class="title-with-alternatives" title="${attr(alternativeTitleHint(show))}">${esc(clean(show.name))}${show.note ? ` <span class="note-tag" title="${attr(show.note)}">i</span>` : ''}${show.reviewed ? ` <span class="reviewed-tag" title="Reviewed">✓</span>` : ''}</strong><small>${show.episode_count} episodes · ${esc(show.root_name)}</small></button>`).join('');
  document.querySelectorAll('.show-card').forEach(button => button.onclick = () => {state.currentShow = state.shows.find(show => show.id === button.dataset.id);state.currentSeason = '*';$('#episode-search').value = '';renderShows();renderEpisodes()});
};

const titleRenderEpisodes = renderEpisodes;
renderEpisodes = function () {
  titleRenderEpisodes();
  const heading = $('#show-title');
  heading.title = state.currentShow ? alternativeTitleHint(state.currentShow) : '';
  heading.classList.toggle('title-with-alternatives', Boolean(heading.title));
};

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
