const $ = selector => document.querySelector(selector);
const state = {movies: [], shows: [], currentShow: null, currentSeason: '*', selectedPath: null, browseKind: 'movies', browsePath: '/'};

async function api(url, options = {}) {
  const response = await fetch(url, {headers: {'Content-Type': 'application/json'}, ...options});
  if (!response.ok) { let message = `${response.status} ${response.statusText}`; try { message = (await response.json()).detail || message; } catch (_) {} throw new Error(message); }
  return response.json();
}
function toast(message, error = false) { const element = $('#toast'); element.textContent = message; element.className = error ? 'error' : ''; element.style.display = 'block'; clearTimeout(toast.timer); toast.timer = setTimeout(() => element.style.display = 'none', 4000); }
function escapeHtml(value) { const div = document.createElement('div'); div.textContent = String(value ?? ''); return div.innerHTML; }
function escapeAttr(value) { return escapeHtml(value).replaceAll('"', '&quot;'); }
function formatBytes(bytes) { if (!bytes) return '0 B'; const unit = Math.min(4, Math.floor(Math.log(bytes) / Math.log(1024))); return `${(bytes / 1024 ** unit).toFixed(unit ? 1 : 0)} ${['B','KB','MB','GB','TB'][unit]}`; }

document.querySelectorAll('[data-page]').forEach(button => button.onclick = () => showPage(button.dataset.page));
function showPage(page) {
  document.querySelectorAll('.page').forEach(section => section.classList.toggle('hidden', section.id !== page));
  document.querySelectorAll('[data-page]').forEach(button => button.classList.toggle('active', button.dataset.page === page));
  if (page === 'movies') loadMovies(); if (page === 'tv') loadTv(); if (page === 'setup') loadRoots();
}

async function loadMovies() {
  try { state.movies = await api('/api/movies'); $('#movies-empty').style.display = state.movies.length ? 'none' : 'block'; $('#movie-list').innerHTML = state.movies.map(fileRow).join(''); wireEditButtons(); }
  catch (error) { toast(error.message, true); }
}
function fileRow(file, season = '') { return `<tr><td><strong>${escapeHtml(file.name)}</strong><br><span class="muted">${escapeHtml(file.relative_path || '')}</span></td>${season ? `<td>${escapeHtml(season)}</td>` : `<td>${escapeHtml(file.root_name)}</td>`}<td>${formatBytes(file.size)}</td><td><button class="edit-file" data-path="${escapeAttr(file.path)}">Stream properties</button></td></tr>`; }
function wireEditButtons() { document.querySelectorAll('.edit-file').forEach(button => button.onclick = () => openStreamEditor(button.dataset.path)); }

async function loadTv() {
  try { state.shows = await api('/api/tv'); if (state.currentShow) state.currentShow = state.shows.find(show => show.id === state.currentShow.id) || null; renderShows(); renderEpisodes(); }
  catch (error) { toast(error.message, true); }
}
function renderShows() {
  $('#tv-empty').style.display = state.shows.length ? 'none' : 'block';
  $('#show-list').innerHTML = state.shows.map(show => `<button type="button" class="show-card ${state.currentShow?.id === show.id ? 'active' : ''}" data-id="${escapeAttr(show.id)}"><strong>${escapeHtml(show.name)}</strong><small>${show.episode_count} episodes · ${escapeHtml(show.root_name)}</small></button>`).join('');
  document.querySelectorAll('.show-card').forEach(button => button.onclick = () => { state.currentShow = state.shows.find(show => show.id === button.dataset.id); state.currentSeason = '*'; renderShows(); renderEpisodes(); });
}
function visibleEpisodes() { if (!state.currentShow) return []; const seasons = state.currentSeason === '*' ? state.currentShow.seasons : state.currentShow.seasons.filter(season => season.name === state.currentSeason); return seasons.flatMap(season => season.episodes.map(episode => ({...episode, season: season.name}))); }
function renderEpisodes() {
  const select = $('#season-filter'); $('#show-title').textContent = state.currentShow?.name || 'Choose a show';
  if (!state.currentShow) { select.disabled = true; select.innerHTML = '<option>Choose a show</option>'; $('#episode-scope').textContent = ''; $('#episode-list').innerHTML = ''; return; }
  select.disabled = false; select.innerHTML = `<option value="*">All episodes (${state.currentShow.episode_count})</option>` + state.currentShow.seasons.map(season => `<option value="${escapeAttr(season.name)}">${escapeHtml(season.name)} (${season.episodes.length})</option>`).join(''); select.value = state.currentSeason;
  const episodes = visibleEpisodes(); $('#episode-scope').textContent = state.currentSeason === '*' ? `All ${episodes.length} episodes` : `${state.currentSeason} · ${episodes.length} episodes`;
  $('#episode-list').innerHTML = episodes.map(episode => fileRow(episode, episode.season)).join(''); wireEditButtons();
}
$('#season-filter').onchange = event => { state.currentSeason = event.target.value; renderEpisodes(); };

async function openStreamEditor(path) {
  state.selectedPath = path; $('#selected-file').textContent = path.split('/').pop(); $('#stream-content').innerHTML = '<p class="no-streams">Inspecting file…</p>'; $('#stream-dialog').showModal();
  try {
    const data = await api('/api/media/batch-probe', {method: 'POST', body: JSON.stringify({paths: [path]})});
    const streams = data.streams.filter(stream => stream.codec_type === 'audio' || stream.codec_type === 'subtitle');
    $('#stream-content').innerHTML = streams.length ? `<div class="stream-grid head"><span>Stream</span><span>Language</span><span>Region tag</span><span>Track name</span></div>${streams.map(streamRow).join('')}` : '<p class="no-streams">This file has no audio or subtitle streams.</p>';
    $('#stream-content').querySelectorAll('input').forEach(input => input.oninput = () => input.dataset.dirty = 'true');
  } catch (error) { $('#stream-content').innerHTML = `<p class="no-streams error">${escapeHtml(error.message)}</p>`; }
}
function streamRow(stream) {
  const language = stream.languages[0] || '', region = stream.regions[0] || '', title = stream.titles[0] || '';
  return `<div class="stream-grid stream-row" data-codec-type="${stream.codec_type}" data-type-index="${stream.type_index}"><div class="stream-kind"><strong>${escapeHtml(stream.codec_type)} ${stream.type_index + 1}</strong><small>${escapeHtml(stream.codecs.join(', '))}</small></div><input name="language" value="${escapeAttr(language)}" placeholder="eng"><input name="region" value="${escapeAttr(region)}" placeholder="US"><input name="title" value="${escapeAttr(title)}" placeholder="Track name"></div>`;
}
document.querySelectorAll('[data-close-stream]').forEach(button => button.onclick = () => $('#stream-dialog').close());
$('#stream-form').onsubmit = async event => {
  event.preventDefault();
  const updates = [...document.querySelectorAll('.stream-row')].map(row => { const update = {codec_type: row.dataset.codecType, type_index: Number(row.dataset.typeIndex)}; const language = row.querySelector('[name=language]'), region = row.querySelector('[name=region]'), title = row.querySelector('[name=title]'); if (language.dataset.dirty === 'true' || region.dataset.dirty === 'true') { update.language = language.value; update.region = region.value; } if (title.dataset.dirty === 'true') update.title = title.value; return update; }).filter(update => Object.keys(update).length > 2);
  if (!updates.length) { toast('Change at least one field'); return; }
  const button = event.target.querySelector('[type=submit]'); button.disabled = true; button.textContent = 'Applying…';
  try { const result = await api('/api/media/edit', {method: 'POST', body: JSON.stringify({paths: [state.selectedPath], streams: updates})}); if (result.failures.length) toast(result.failures[0].error, true); else { toast('Stream properties updated'); $('#stream-dialog').close(); } }
  catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = 'Apply changes'; }
};

document.querySelectorAll('.refresh').forEach(button => button.onclick = () => button.dataset.kind === 'movies' ? loadMovies() : loadTv());
async function loadRoots() { try { const all = await api('/api/roots'); renderRoots('#movie-roots', all.filter(root => root.kind === 'movies')); renderRoots('#tv-roots', all.filter(root => root.kind === 'tv')); } catch (error) { toast(error.message, true); } }
function renderRoots(selector, roots) { $(selector).innerHTML = roots.length ? roots.map(root => `<div class="root"><div><strong>${escapeHtml(root.name)}</strong><small>${escapeHtml(root.path)}</small></div><button class="danger" data-root-id="${root.id}">Remove</button></div>`).join('') : '<p>No folders added.</p>'; document.querySelectorAll(`${selector} [data-root-id]`).forEach(button => button.onclick = async () => { try { await api(`/api/roots/${button.dataset.rootId}`, {method: 'DELETE'}); loadRoots(); } catch (error) { toast(error.message, true); } }); }
document.querySelectorAll('.add-root').forEach(button => button.onclick = async () => { state.browseKind = button.dataset.kind; await openDirectory('/'); $('#folder-browser').showModal(); });
async function openDirectory(path) { try { const data = await api(`/api/browse?path=${encodeURIComponent(path)}`); state.browsePath = data.path; $('#current-path').textContent = data.path; const parent = data.parent ? `<button type="button" class="directory" data-path="${escapeAttr(data.parent)}">↰ ..</button>` : ''; $('#directories').innerHTML = parent + data.directories.map(item => `<button type="button" class="directory" data-path="${escapeAttr(item.path)}">📁 ${escapeHtml(item.name)}</button>`).join(''); document.querySelectorAll('.directory').forEach(item => item.onclick = () => openDirectory(item.dataset.path)); } catch (error) { toast(error.message, true); } }
$('#choose-folder').onclick = async () => { try { await api('/api/roots', {method: 'POST', body: JSON.stringify({kind: state.browseKind, path: state.browsePath})}); $('#folder-browser').close(); loadRoots(); toast('Media folder added'); } catch (error) { toast(error.message, true); } };

showPage('movies');
