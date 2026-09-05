(function () {
  const movieTools = document.querySelector('#movies .page-tools');
  const search = document.querySelector('#movie-search');
  const movieCard = document.querySelector('#movies .list-card');
  if (!movieTools || !search || !movieCard) return;

  const filters = document.createElement('div');
  filters.className = 'movie-stream-filters';
  filters.innerHTML = `
    <label>Stream<select id="movie-stream-type"><option value="all">Audio or subtitles</option><option value="audio">Audio</option><option value="subtitle">Subtitles</option></select></label>
    <label>Language<select id="movie-stream-language"><option value="">All languages</option></select></label>
    <label>Track name<select id="movie-stream-track-name"><option value="">All track names</option></select></label>`;
  search.insertAdjacentElement('afterend', filters);

  movieCard.insertAdjacentHTML('beforeend', `<div class="movie-pagination">
    <label>Movies per page<select id="movie-page-size"><option>10</option><option selected>50</option><option>100</option><option>150</option><option>200</option><option>500</option></select></label>
    <div><button type="button" id="movie-page-first" title="First page">«</button><button type="button" id="movie-page-previous" title="Previous page">‹</button><span id="movie-page-status"></span><button type="button" id="movie-page-next" title="Next page">›</button><button type="button" id="movie-page-last" title="Last page">»</button></div>
  </div>`);

  const type = $('#movie-stream-type'), language = $('#movie-stream-language'), trackName = $('#movie-stream-track-name');
  const pageSize = $('#movie-page-size'), pageStatus = $('#movie-page-status');
  let savedValues = null, allowedPaths = null, currentPage = 1, requestNumber = 0;

  function setOptions(select, items, label) {
    const current = select.value;
    select.innerHTML = `<option value="">${esc(label)}</option>` + items.map(value => `<option value="${attr(value)}">${esc(value)}</option>`).join('');
    if (items.includes(current)) select.value = current;
  }

  function refreshFilterOptions() {
    if (!savedValues) return;
    const group = type.value;
    setOptions(language, savedValues.languages[group] || [], 'All languages');
    setOptions(trackName, savedValues.track_names[group] || [], 'All track names');
  }

  async function loadSavedFilterValues() {
    savedValues = await api('/api/v39/movies/stream-filter-values');
    refreshFilterOptions();
  }

  function filteredMovies() {
    const query = search.value;
    return state.movies.filter(file => {
      if (!matches([movieTitle(file), ...(file.alternative_titles || [])].join(' '), query)) return false;
      const headerPaths = window.movieHeaderAllowedPaths;
      return (allowedPaths === null || allowedPaths.has(file.path)) &&
        (!(headerPaths instanceof Set) || headerPaths.has(file.path));
    });
  }
  window.currentFilteredMovies = filteredMovies;

  renderMovies = function () {
    const files = filteredMovies();
    const size = Number(pageSize.value) || 50;
    const pages = Math.max(1, Math.ceil(files.length / size));
    currentPage = Math.min(Math.max(1, currentPage), pages);
    const visible = files.slice((currentPage - 1) * size, currentPage * size);
    $('#movies-empty').style.display = files.length ? 'none' : 'block';
    $('#movies-empty').textContent = state.movies.length ? 'No movies match the current search and stream filters.' : 'No movies found.';
    $('#movie-list').innerHTML = visible.map(file => `<tr><td><strong class="movie-title title-with-alternatives" title="${attr(alternativeTitleHint(file))}">${esc(movieTitle(file))}</strong></td><td>${esc(file.root_name)}</td><td>${bytes(file.size)}</td><td><button class="edit-file" data-path="${attr(file.path)}" data-label="${attr(movieTitle(file))}">Stream properties</button></td></tr>`).join('');
    pageStatus.textContent = files.length ? `${(currentPage - 1) * size + 1}–${Math.min(currentPage * size, files.length)} of ${files.length}` : '0 movies';
    $('#movie-page-first').disabled = $('#movie-page-previous').disabled = currentPage <= 1;
    $('#movie-page-next').disabled = $('#movie-page-last').disabled = currentPage >= pages;
    wireEditors();
  };

  async function applyStreamFilters() {
    const active = type.value !== 'all' || language.value || trackName.value;
    currentPage = 1;
    if (!active) { allowedPaths = null; renderMovies(); return; }
    const ownRequest = ++requestNumber;
    const query = new URLSearchParams({stream_type: type.value, language: language.value, track_name: trackName.value});
    const result = await api(`/api/v38/movies/stream-filter-matches?${query}`);
    if (ownRequest !== requestNumber) return;
    allowedPaths = new Set(result.paths);
    renderMovies();
  }

  search.oninput = () => { currentPage = 1; renderMovies(); };
  type.onchange = () => { refreshFilterOptions(); applyStreamFilters(); };
  language.onchange = applyStreamFilters;
  trackName.onchange = applyStreamFilters;
  pageSize.onchange = () => { currentPage = 1; renderMovies(); };
  $('#movie-page-first').onclick = () => { currentPage = 1; renderMovies(); };
  $('#movie-page-previous').onclick = () => { currentPage--; renderMovies(); };
  $('#movie-page-next').onclick = () => { currentPage++; renderMovies(); };
  $('#movie-page-last').onclick = () => { currentPage = Math.ceil(filteredMovies().length / (Number(pageSize.value) || 50)); renderMovies(); };

  const setup = $('#setup');
  setup.insertAdjacentHTML('beforeend', `<div class="index-maintenance"><article><div><h3>Movie stream filter index</h3><p>Build the saved audio and subtitle language and track-name index used by the Movies filters.</p></div><div class="index-maintenance-actions"><button type="button" id="movie-index-check">Check index</button><button type="button" id="movie-index-rebuild" class="danger">Rebuild index</button></div><p id="movie-index-progress" class="plex-status" aria-live="polite">Index status has not been checked.</p></article></div>`);
  let indexTimer = null;

  function showIndexStatus(status) {
    const progress = $('#movie-index-progress');
    if (status.running) {
      progress.textContent = `Indexing ${status.completed} of ${status.total} movies · ${status.errors} unavailable`;
      indexTimer = setTimeout(loadIndexStatus, 2000);
    } else if (status.pending) {
      progress.textContent = `${status.indexed} of ${status.movies} movies indexed · ${status.pending} need indexing`;
    } else {
      progress.textContent = `${status.indexed} of ${status.movies} movies indexed · Index is current`;
    }
    $('#movie-index-check').disabled = $('#movie-index-rebuild').disabled = status.running;
  }

  async function loadIndexStatus() {
    if (document.querySelector("#setup").classList.contains("hidden")) return;
    if (indexTimer) { clearTimeout(indexTimer); indexTimer = null; }
    try { showIndexStatus(await api('/api/v39/setup/movie-index/status')); }
    catch (error) { $('#movie-index-progress').textContent = error.message; }
  }

  $('#movie-index-check').onclick = async () => {
    try { showIndexStatus(await api('/api/v39/setup/movie-index/check', {method: 'POST', body: '{}'})); }
    catch (error) { toast(error.message, true); }
  };
  $('#movie-index-rebuild').onclick = async () => {
    if (!confirm('Clear and rebuild the complete movie stream filter index?')) return;
    try { showIndexStatus(await api('/api/v39/setup/movie-index/rebuild', {method: 'POST', body: '{}'})); }
    catch (error) { toast(error.message, true); }
  };

  const previousLoadRoots = loadRoots;
  loadRoots = async function () { await previousLoadRoots(); await loadIndexStatus(); };

  document.addEventListener('media-properties-applied', async event => {
    try {
      const result = await api('/api/v39/movies/stream-filter-refresh', {method: 'POST', body: JSON.stringify({path: event.detail.path,indexes:event.detail.indexes})});
      if (result.indexed) { await loadSavedFilterValues(); await applyStreamFilters(); }
    } catch (error) { console.warn('Could not refresh the edited movie in the stream filter index', error); }
  });

  loadSavedFilterValues().then(renderMovies).catch(error => toast(error.message, true));
})();
