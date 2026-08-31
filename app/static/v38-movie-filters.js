(function () {
  const tools = document.querySelector('#movies .page-tools');
  const search = document.querySelector('#movie-search');
  if (!tools || !search) return;

  const filters = document.createElement('div');
  filters.className = 'movie-stream-filters';
  filters.innerHTML = `
    <label>Stream<select id="movie-stream-type"><option value="all">Audio or subtitles</option><option value="audio">Audio</option><option value="subtitle">Subtitles</option></select></label>
    <label>Language<select id="movie-stream-language"><option value="">All languages</option></select></label>
    <label>Track name<select id="movie-stream-track-name"><option value="">All track names</option></select></label>
    <small id="movie-stream-index-status" aria-live="polite">Loading stream filters…</small>`;
  search.insertAdjacentElement('afterend', filters);

  const type = document.querySelector('#movie-stream-type');
  const language = document.querySelector('#movie-stream-language');
  const trackName = document.querySelector('#movie-stream-track-name');
  const status = document.querySelector('#movie-stream-index-status');
  let values = null;
  let allowedPaths = null;
  let requestNumber = 0;
  let pollTimer = null;

  function options(select, items, emptyLabel) {
    const signature = JSON.stringify(items);
    if (select.dataset.optionSignature === signature) return false;
    const current = select.value;
    select.innerHTML = `<option value="">${esc(emptyLabel)}</option>` + items.map(value => `<option value="${attr(value)}">${esc(value)}</option>`).join('');
    if (items.includes(current)) select.value = current;
    select.dataset.optionSignature = signature;
    return true;
  }

  function refreshOptions() {
    if (!values) return;
    const group = type.value;
    options(language, values.languages[group] || [], 'All languages');
    options(trackName, values.track_names[group] || [], 'All track names');
  }

  function filterRows() {
    const active = Boolean(type.value !== 'all' || language.value || trackName.value);
    document.querySelectorAll('#movie-list tr').forEach(row => {
      const path = row.querySelector('.edit-file')?.dataset.path;
      row.classList.toggle('movie-stream-filter-hidden', active && allowedPaths !== null && !allowedPaths.has(path));
    });
    if (active && allowedPaths !== null) {
      const shown = document.querySelectorAll('#movie-list tr:not(.movie-stream-filter-hidden)').length;
      document.querySelector('#movies-empty').style.display = shown ? 'none' : 'block';
      if (!shown) document.querySelector('#movies-empty').textContent = 'No movies match the selected stream properties.';
    }
  }

  async function applyFilters() {
    const active = Boolean(type.value !== 'all' || language.value || trackName.value);
    if (!active) {
      allowedPaths = null;
      renderMovies();
      return;
    }
    const ownRequest = ++requestNumber;
    const query = new URLSearchParams({stream_type: type.value, language: language.value, track_name: trackName.value});
    const result = await api(`/api/v38/movies/stream-filter-matches?${query}`);
    if (ownRequest !== requestNumber) return;
    const nextPaths = new Set(result.paths);
    const changed = allowedPaths === null || nextPaths.size !== allowedPaths.size || [...nextPaths].some(path => !allowedPaths.has(path));
    allowedPaths = nextPaths;
    if (changed) filterRows();
  }

  async function loadValues() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    try {
      const result = await api('/api/v38/movies/stream-filter-values');
      const s = result.status;
      if (!values || !s.running) { values = result; refreshOptions(); }
      status.textContent = s.running ? `Indexing movie streams ${s.completed}/${s.total} · ${s.indexed}/${s.movies} cached` : `${s.indexed}/${s.movies} movies indexed${s.errors ? ` · ${s.errors} unavailable` : ''}`;
      if (s.running) pollTimer = setTimeout(loadValues, 3000);
      if (!s.running && (type.value !== 'all' || language.value || trackName.value)) await applyFilters();
    } catch (error) {
      status.textContent = `Stream filters unavailable: ${error.message}`;
    }
  }

  type.onchange = () => { refreshOptions(); applyFilters(); };
  language.onchange = applyFilters;
  trackName.onchange = applyFilters;

  const originalRenderMovies = renderMovies;
  renderMovies = function () { originalRenderMovies(); filterRows(); };
  search.oninput = renderMovies;
  document.addEventListener('media-properties-applied', async event => {
    try {
      await api('/api/v38/movies/stream-filter-invalidate', {method: 'POST', body: JSON.stringify({path: event.detail.path})});
      await loadValues();
    } catch (error) { console.warn('Could not refresh movie stream filter cache', error); }
  });
  loadValues();
})();
