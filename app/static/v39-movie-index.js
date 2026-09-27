(function () {
  const movieTools = document.querySelector('#movies .page-tools');
  const search = document.querySelector('#movie-search');
  const movieCard = document.querySelector('#movies .list-card');
  if (!movieTools || !search || !movieCard) return;

  movieTools.insertAdjacentHTML('beforeend', '<div class="movie-list-toolbar-meta"><span id="movie-list-summary" role="status" aria-live="polite">Loading movies…</span><button type="button" id="movie-clear-filters" hidden>Clear filters</button></div>');

  movieCard.insertAdjacentHTML('beforeend', `<div class="movie-pagination">
    <label>Movies per page<select id="movie-page-size"><option>10</option><option selected>50</option><option>100</option><option>150</option><option>200</option><option>500</option></select></label>
    <div><button type="button" id="movie-page-first" title="First page">«</button><button type="button" id="movie-page-previous" title="Previous page">‹</button><span id="movie-page-status"></span><button type="button" id="movie-page-next" title="Next page">›</button><button type="button" id="movie-page-last" title="Last page">»</button></div>
  </div>`);

  const pageSize = $('#movie-page-size'), pageStatus = $('#movie-page-status');
  let currentPage = 1;

  function baseFilteredMovies() {
    const query = search.value;
    return state.movies.filter(file => matches([movieTitle(file), ...(file.alternative_titles || [])].join(' '), query)
      && movieMatchesStatusFilters(file));
  }
  function filteredMovies() {
    const headerPaths = window.movieHeaderAllowedPaths;
    return baseFilteredMovies().filter(file => !(headerPaths instanceof Set) || headerPaths.has(file.path));
  }
  window.movieBaseFilteredMovies = baseFilteredMovies;
  window.currentFilteredMovies = filteredMovies;

  // The status controls were wired before pagination replaced renderMovies.
  // Re-render with the current implementation after their state transitions.
  for (const name of ['reviewed','notes','plex','detection','final']) {
    document.querySelector(`#movie-${name}-filter`)?.addEventListener('click', () => queueMicrotask(() => renderMovies()));
  }

  renderMovies = function () {
    const files = filteredMovies();
    const size = Number(pageSize.value) || 50;
    const pages = Math.max(1, Math.ceil(files.length / size));
    currentPage = Math.min(Math.max(1, currentPage), pages);
    const visible = files.slice((currentPage - 1) * size, currentPage * size);
    $('#movies-empty').style.display = files.length ? 'none' : 'block';
    $('#movies-empty').textContent = state.movies.length ? 'No movies match the current search and stream filters.' : 'No movies found.';
    $('#movie-list').innerHTML = visible.map(movieRowHTML).join('');
    $('#movie-list-summary').textContent = `${files.length} of ${state.movies.length} movies`;
    $('#movie-clear-filters').hidden = !(search.value || window.movieHeaderAllowedPaths instanceof Set || ['review','notes','plex','detection','final'].some(name => {
      const input=$(`#movie-${name==='review'?'reviewed':name}-filter`);
      const stateName={review:'reviewState',notes:'notesState',plex:'plexState',detection:'detectionState',final:'finalState'}[name];
      return input?.dataset[stateName] && input.dataset[stateName]!=='all';
    }));
    pageStatus.textContent = files.length ? `${(currentPage - 1) * size + 1}–${Math.min(currentPage * size, files.length)} of ${files.length}` : '0 movies';
    $('#movie-page-first').disabled = $('#movie-page-previous').disabled = currentPage <= 1;
    $('#movie-page-next').disabled = $('#movie-page-last').disabled = currentPage >= pages;
    wireEditors();
  };

  search.oninput = () => { currentPage = 1; renderMovies(); };
  pageSize.onchange = () => { currentPage = 1; renderMovies(); };
  $('#movie-page-first').onclick = () => { currentPage = 1; renderMovies(); };
  $('#movie-page-previous').onclick = () => { currentPage--; renderMovies(); };
  $('#movie-page-next').onclick = () => { currentPage++; renderMovies(); };
  $('#movie-page-last').onclick = () => { currentPage = Math.ceil(filteredMovies().length / (Number(pageSize.value) || 50)); renderMovies(); };
  $('#movie-clear-filters').onclick = () => {
    search.value='';currentPage=1;
    for(const [name,stateName] of Object.entries({reviewed:'reviewState',notes:'notesState',plex:'plexState',detection:'detectionState',final:'finalState'})){
      const input=$(`#movie-${name}-filter`);if(!input)continue;
      input.dataset[stateName]='all';input.dataset.symbol='—';input.checked=false;input.indeterminate=false;input.title='All items';input.parentElement.title='All items';
      if(name==='reviewed')input.parentElement.querySelector('span').innerHTML='R: <em data-review-state>—</em>';
    }
    window.resetMovieHeaderFilters?.();renderMovies();
  };

  const setup = $('#setup');
  setup.insertAdjacentHTML('beforeend', `<div class="index-maintenance"><div class="setup-index-intro"><h3>Media indexes</h3><p>Core metadata keeps movie and episode filters current. Subtitle inspection adds language and subtitle-quality findings. New and changed media are queued automatically.</p><p class="setup-catalog-completeness" data-catalog-completeness aria-live="polite">Loading catalog coverage…</p></div><div class="setup-index-cards">${[
    ['core','Core stream metadata','Language, region, track names, stream flags, and external subtitles for movies and episodes.'],
    ['subtitles','Subtitle inspection','Text, language, markup, and damage findings for subtitle streams.']
  ].map(([job,title,description])=>`<article data-index-job="${job}"><h3>${title}</h3><p>${description}</p><p class="setup-index-status" data-index-status aria-live="polite">Loading status…</p><button type="button" data-index-check>Check new or changed media</button><details class="setup-index-advanced"><summary>Advanced maintenance</summary><div class="index-maintenance-actions"><button type="button" data-index-rebuild class="danger">Rebuild this index</button></div></details></article>`).join('')}</div></div>`);
  let indexTimer = null;
  let catalogCheckedAt = 0;

  async function loadCatalogCompleteness() {
    if (Date.now() - catalogCheckedAt < 30000) return;
    catalogCheckedAt = Date.now();
    const line=setup.querySelector('[data-catalog-completeness]');
    try {
      const status=await api('/api/v39/setup/movie-index/status');
      line.textContent=`Movies ${status.indexed}/${status.movies} · TV shows ${status.tv_shows_indexed}/${status.tv_shows} · Episodes ${status.episodes_indexed}/${status.episodes} indexed`;
    } catch(error) { line.textContent=`Catalog coverage unavailable: ${error.message}`; }
  }

  async function loadIndexStatus() {
    if (indexTimer) { clearTimeout(indexTimer); indexTimer = null; }
    if (document.querySelector('#setup').classList.contains('hidden')) return;
    loadCatalogCompleteness();
    const states = await Promise.all(['core','subtitles'].map(async job=>{
      const card=setup.querySelector(`[data-index-job="${job}"]`);
      try {
        const status=await api(`/api/v80/setup/index/${job}/status`);
        card.querySelector('[data-index-status]').textContent=`${status.indexed} indexed · ${status.running} running · ${status.queued} queued · ${status.failed} failed${status.paused?' · Paused':''}`;
        return status;
      } catch(error) { card.querySelector('[data-index-status]').textContent=error.message; return null; }
    }));
    if(states.some(status=>status&&(status.running||status.queued))) indexTimer=setTimeout(loadIndexStatus,4000);
  }

  setup.querySelectorAll('[data-index-job]').forEach(card=>{
    const job=card.dataset.indexJob,label=job==='core'?'core stream metadata':'subtitle inspection';
    card.querySelector('[data-index-check]').onclick=async()=>{try{await api(`/api/v80/setup/index/${job}/check`,{method:'POST',body:'{}'});toast(`Incremental ${label} check queued`);loadIndexStatus()}catch(error){toast(error.message,true)}};
    card.querySelector('[data-index-rebuild]').onclick=async()=>{if(!confirm(`Rebuild ${label} for the catalog? This queues a full recheck and can take a long time.`))return;try{await api(`/api/v80/setup/index/${job}/rebuild`,{method:'POST',body:'{}'});toast(`${label} rebuild queued`);loadIndexStatus()}catch(error){toast(error.message,true)}};
  });
  window.refreshSetupIndexCards=loadIndexStatus;

  const previousLoadRoots = loadRoots;
  loadRoots = async function () { await previousLoadRoots(); await loadIndexStatus(); };

  renderMovies();
})();
