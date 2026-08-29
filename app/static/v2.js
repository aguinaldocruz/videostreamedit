const $ = selector => document.querySelector(selector);
const state = {movies: [], shows: [], movieSelection: new Set(), episodeSelection: new Set(), currentShow: null, currentSeason: '*', browseKind: 'movies', browsePath: '/'};

async function api(url, options = {}) {
  const response = await fetch(url, {headers: {'Content-Type': 'application/json'}, ...options});
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { message = (await response.json()).detail || message; } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}

function toast(message, error = false) {
  const element = $('#toast');
  element.textContent = message; element.className = error ? 'error' : ''; element.style.display = 'block';
  clearTimeout(toast.timer); toast.timer = setTimeout(() => element.style.display = 'none', 4000);
}

function escapeHtml(value) { const div = document.createElement('div'); div.textContent = String(value ?? ''); return div.innerHTML; }
function escapeAttr(value) { return escapeHtml(value).replaceAll('"', '&quot;'); }
function formatBytes(bytes) { if (!bytes) return '0 B'; const unit = Math.min(4, Math.floor(Math.log(bytes) / Math.log(1024))); return `${(bytes / 1024 ** unit).toFixed(unit ? 1 : 0)} ${['B','KB','MB','GB','TB'][unit]}`; }

document.querySelectorAll('[data-page]').forEach(button => button.onclick = () => showPage(button.dataset.page));
function showPage(page) {
  document.querySelectorAll('.page').forEach(section => section.classList.toggle('hidden', section.id !== page));
  document.querySelectorAll('[data-page]').forEach(button => button.classList.toggle('active', button.dataset.page === page));
  if (page === 'movies') loadMovies();
  if (page === 'tv') loadTv();
  if (page === 'setup') loadRoots();
}

async function loadMovies() {
  try { state.movies = await api('/api/movies'); renderMovies(); }
  catch (error) { toast(error.message, true); }
}

function renderMovies() {
  state.movieSelection = new Set([...state.movieSelection].filter(path => state.movies.some(movie => movie.path === path)));
  $('#movies-empty').style.display = state.movies.length ? 'none' : 'block';
  $('#movie-list').innerHTML = state.movies.map(movie => `<tr><td><input type="checkbox" data-path="${escapeAttr(movie.path)}" ${state.movieSelection.has(movie.path) ? 'checked' : ''}></td><td><strong>${escapeHtml(movie.name)}</strong><br><span class="muted">${escapeHtml(movie.relative_path)}</span></td><td>${escapeHtml(movie.root_name)}</td><td>${formatBytes(movie.size)}</td></tr>`).join('');
  document.querySelectorAll('#movie-list input').forEach(input => input.onchange = () => toggleSelection('movies', input.dataset.path, input.checked));
  updateSelectionHeader('movies');
}

async function loadTv() {
  try {
    state.shows = await api('/api/tv');
    if (state.currentShow) state.currentShow = state.shows.find(show => show.id === state.currentShow.id) || null;
    renderShows(); renderEpisodes();
  } catch (error) { toast(error.message, true); }
}

function renderShows() {
  $('#tv-empty').style.display = state.shows.length ? 'none' : 'block';
  $('#show-list').innerHTML = state.shows.map(show => `<button type="button" class="show-card ${state.currentShow?.id === show.id ? 'active' : ''}" data-id="${escapeAttr(show.id)}"><strong>${escapeHtml(show.name)}</strong><small>${show.episode_count} episodes · ${escapeHtml(show.root_name)}</small></button>`).join('');
  document.querySelectorAll('.show-card').forEach(button => button.onclick = () => selectShow(button.dataset.id));
}

function selectShow(id) {
  state.currentShow = state.shows.find(show => show.id === id); state.currentSeason = '*'; state.episodeSelection.clear();
  renderShows(); renderSeasonFilter(); renderEpisodes(); clearEditor('tv');
}

function renderSeasonFilter() {
  const select = $('#season-filter');
  if (!state.currentShow) { select.disabled = true; select.innerHTML = '<option>Choose a show</option>'; return; }
  select.disabled = false;
  select.innerHTML = `<option value="*">All episodes (${state.currentShow.episode_count})</option>` + state.currentShow.seasons.map(season => `<option value="${escapeAttr(season.name)}">${escapeHtml(season.name)} (${season.episodes.length})</option>`).join('');
  select.value = state.currentSeason;
}

function visibleEpisodes() {
  if (!state.currentShow) return [];
  const seasons = state.currentSeason === '*' ? state.currentShow.seasons : state.currentShow.seasons.filter(season => season.name === state.currentSeason);
  return seasons.flatMap(season => season.episodes.map(episode => ({...episode, season: season.name})));
}

function renderEpisodes() {
  renderSeasonFilter();
  $('#show-title').textContent = state.currentShow?.name || 'Choose a show';
  const episodes = visibleEpisodes();
  $('#episode-scope').textContent = state.currentShow ? (state.currentSeason === '*' ? `All ${episodes.length} episodes` : `${state.currentSeason} · ${episodes.length} episodes`) : '';
  $('#episode-list').innerHTML = episodes.map(episode => `<tr><td><input type="checkbox" data-path="${escapeAttr(episode.path)}" ${state.episodeSelection.has(episode.path) ? 'checked' : ''}></td><td><strong>${escapeHtml(episode.name)}</strong></td><td>${escapeHtml(episode.season)}</td><td>${formatBytes(episode.size)}</td></tr>`).join('');
  document.querySelectorAll('#episode-list input').forEach(input => input.onchange = () => toggleSelection('tv', input.dataset.path, input.checked));
  updateSelectionHeader('tv');
}

$('#season-filter').onchange = event => { state.currentSeason = event.target.value; renderEpisodes(); };

async function toggleSelection(kind, path, selected) {
  const selection = kind === 'movies' ? state.movieSelection : state.episodeSelection;
  selected ? selection.add(path) : selection.delete(path);
  updateSelectionHeader(kind);
  await updateEditor(kind);
}

$('#movies-all').onchange = async event => {
  state.movieSelection = event.target.checked ? new Set(state.movies.map(movie => movie.path)) : new Set();
  renderMovies(); await updateEditor('movies');
};

$('#episodes-all').onchange = async event => {
  const visible = visibleEpisodes();
  visible.forEach(episode => event.target.checked ? state.episodeSelection.add(episode.path) : state.episodeSelection.delete(episode.path));
  renderEpisodes(); await updateEditor('tv');
};

function updateSelectionHeader(kind) {
  const selection = kind === 'movies' ? state.movieSelection : state.episodeSelection;
  const visible = kind === 'movies' ? state.movies : visibleEpisodes();
  const selectedVisible = visible.filter(item => selection.has(item.path)).length;
  $(`#${kind === 'movies' ? 'movies' : 'episodes'}-count`).textContent = `${selection.size} selected`;
  const all = $(`#${kind === 'movies' ? 'movies' : 'episodes'}-all`);
  all.checked = visible.length > 0 && selectedVisible === visible.length;
  all.indeterminate = selectedVisible > 0 && selectedVisible < visible.length;
}

function clearEditor(kind) {
  $(`#${kind === 'movies' ? 'movies' : 'tv'}-editor`).innerHTML = '<h3>Stream properties</h3><p class="muted">Select one or more files to edit language, region, and track name.</p>';
}

async function updateEditor(kind) {
  const paths = [...(kind === 'movies' ? state.movieSelection : state.episodeSelection)];
  const editor = $(`#${kind === 'movies' ? 'movies' : 'tv'}-editor`);
  if (!paths.length) { clearEditor(kind); return; }
  editor.innerHTML = `<h3>Stream properties</h3><p class="muted">Inspecting ${paths.length} selected files…</p>`;
  try {
    const data = await api('/api/media/batch-probe', {method: 'POST', body: JSON.stringify({paths})});
    editor.innerHTML = `<div class="properties-header"><h3>Stream properties</h3><p>${data.file_count} selected file${data.file_count === 1 ? '' : 's'}${data.failures.length ? ` · <span class="error">${data.failures.length} unreadable</span>` : ''}</p></div><form class="property-form" data-kind="${kind}">${data.streams.map((stream, index) => streamFields(stream, index, data.file_count)).join('')}<button type="submit" class="primary apply">Apply changed fields</button></form>`;
    editor.querySelectorAll('input').forEach(input => input.oninput = () => input.dataset.dirty = 'true');
    editor.querySelector('form').onsubmit = applyChanges;
  } catch (error) { editor.innerHTML = `<h3>Stream properties</h3><p class="error">${escapeHtml(error.message)}</p>`; }
}

function streamFields(stream, index, fileCount) {
  const label = `${stream.codec_type} ${stream.type_index + 1}`;
  const availability = stream.present_count === fileCount ? `in all files` : `in ${stream.present_count} of ${fileCount} files`;
  return `<section class="stream" data-codec-type="${stream.codec_type}" data-type-index="${stream.type_index}"><h4>${escapeHtml(label)} <span class="muted">· ${escapeHtml(stream.codecs.join(', '))} · ${availability}</span></h4>${field('Language', 'language', stream.languages, `languages-${index}`)}${field('Region tag', 'region', stream.regions, `regions-${index}`)}${field('Track name', 'title', stream.titles, `titles-${index}`)}</section>`;
}

function field(label, name, values, listId) {
  const unique = [...new Set(values)];
  const mixed = unique.length > 1;
  const value = mixed ? '' : (unique[0] || '');
  const options = unique.filter(Boolean).map(option => `<option value="${escapeAttr(option)}"></option>`).join('');
  return `<div class="field"><label>${label}</label><div><input name="${name}" value="${escapeAttr(value)}" placeholder="${mixed ? 'Multiple values' : ''}" list="${listId}"><datalist id="${listId}">${options}</datalist>${mixed ? `<small class="mixed">Values: ${escapeHtml(unique.map(item => item || '(empty)').join(', '))}</small>` : ''}</div></div>`;
}

async function applyChanges(event) {
  event.preventDefault();
  const form = event.target, kind = form.dataset.kind;
  const paths = [...(kind === 'movies' ? state.movieSelection : state.episodeSelection)];
  const updates = [...form.querySelectorAll('.stream')].map(stream => {
    const update = {codec_type: stream.dataset.codecType, type_index: Number(stream.dataset.typeIndex)};
    stream.querySelectorAll('input').forEach(input => { if (input.dataset.dirty === 'true') update[input.name] = input.value; });
    const language = stream.querySelector('[name=language]'), region = stream.querySelector('[name=region]');    if (language.dataset.dirty === 'true' || region.dataset.dirty === 'true') { update.language = language.value; update.region = region.value; }
    return update;
  }).filter(update => Object.keys(update).length > 2);
  if (!updates.length) { toast('Change at least one field'); return; }
  const button = form.querySelector('button[type=submit]'); button.disabled = true; button.textContent = 'Applying…';
  try {
    const result = await api('/api/media/edit', {method: 'POST', body: JSON.stringify({paths, streams: updates})});
    if (result.failures.length) toast(`Updated ${result.edited.length}; ${result.failures.length} failed`, true);
    else toast(`Updated ${result.edited.length} file${result.edited.length === 1 ? '' : 's'}`);
    await updateEditor(kind);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = 'Apply changed fields'; }
}

document.querySelectorAll('.scan').forEach(button => button.onclick = () => button.dataset.kind === 'movies' ? loadMovies() : loadTv());

async function loadRoots() {
  try {
    const all = await api('/api/roots');
    renderRoots('#movie-roots', all.filter(root => root.kind === 'movies'));
    renderRoots('#tv-roots', all.filter(root => root.kind === 'tv'));
  } catch (error) { toast(error.message, true); }
}

function renderRoots(selector, roots) {
  $(selector).innerHTML = roots.length ? roots.map(root => `<div class="root"><div><strong>${escapeHtml(root.name)}</strong><small>${escapeHtml(root.path)}</small></div><button class="danger" data-root-id="${root.id}">Remove</button></div>`).join('') : '<p>No folders added.</p>';
  document.querySelectorAll(`${selector} [data-root-id]`).forEach(button => button.onclick = async () => { try { await api(`/api/roots/${button.dataset.rootId}`, {method: 'DELETE'}); await loadRoots(); } catch (error) { toast(error.message, true); } });
}

document.querySelectorAll('.add-root').forEach(button => button.onclick = async () => { state.browseKind = button.dataset.kind; await openDirectory('/'); $('#folder-browser').showModal(); });
async function openDirectory(path) {
  try {
    const data = await api(`/api/browse?path=${encodeURIComponent(path)}`); state.browsePath = data.path; $('#current-path').textContent = data.path;
    const parent = data.parent ? `<button type="button" class="directory" data-path="${escapeAttr(data.parent)}">↰ ..</button>` : '';
    $('#directories').innerHTML = parent + data.directories.map(item => `<button type="button" class="directory" data-path="${escapeAttr(item.path)}">📁 ${escapeHtml(item.name)}</button>`).join('');
    document.querySelectorAll('.directory').forEach(item => item.onclick = () => openDirectory(item.dataset.path));
  } catch (error) { toast(error.message, true); }
}

$('#choose-folder').onclick = async () => {
  try { await api('/api/roots', {method: 'POST', body: JSON.stringify({kind: state.browseKind, path: state.browsePath})}); $('#folder-browser').close(); await loadRoots(); toast('Media folder added'); }
  catch (error) { toast(error.message, true); }
};

clearEditor('movies'); clearEditor('tv'); showPage('movies');
