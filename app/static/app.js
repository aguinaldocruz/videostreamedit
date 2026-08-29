const $ = (selector) => document.querySelector(selector);
let browseKind = 'movies';
let browsePath = '/';
let selectedPath = null;

async function api(url, options = {}) {
  const response = await fetch(url, {headers: {'Content-Type': 'application/json'}, ...options});
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { message = (await response.json()).detail || message; } catch (_) {}
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

function toast(message, error = false) {
  const element = $('#toast'); element.textContent = message; element.className = error ? 'error' : '';
  element.style.display = 'block'; setTimeout(() => element.style.display = 'none', 3500);
}

document.querySelectorAll('[data-view]').forEach(button => button.onclick = () => {
  document.querySelectorAll('.view').forEach(view => view.classList.add('hidden'));
  $(`#${button.dataset.view}`).classList.remove('hidden');
  if (button.dataset.view === 'setup') loadRoots();
});

async function loadRoots() {
  try {
    const roots = await api('/api/roots');
    renderRoots('#movie-roots', roots.filter(root => root.kind === 'movies'));
    renderRoots('#tv-roots', roots.filter(root => root.kind === 'tv'));
  } catch (error) { toast(error.message, true); }
}

function renderRoots(selector, roots) {
  $(selector).innerHTML = roots.length ? roots.map(root => `<div class="root"><div><strong>${escapeHtml(root.name)}</strong><small>${escapeHtml(root.path)}</small></div><button class="danger" onclick="removeRoot(${root.id})">Remove</button></div>`).join('') : '<p>No folders added.</p>';
}

window.removeRoot = async id => {
  try { await api(`/api/roots/${id}`, {method: 'DELETE'}); await loadRoots(); await loadLibrary(); }
  catch (error) { toast(error.message, true); }
};

document.querySelectorAll('.add-root').forEach(button => button.onclick = async () => {
  browseKind = button.dataset.kind; await openDirectory('/'); $('#browser').showModal();
});

async function openDirectory(path) {
  try {
    const data = await api(`/api/browse?path=${encodeURIComponent(path)}`); browsePath = data.path;
    $('#current-path').textContent = data.path;
    const parent = data.parent ? `<button type="button" class="directory" data-path="${escapeAttr(data.parent)}">↰ ..</button>` : '';
    $('#directories').innerHTML = parent + data.directories.map(item => `<button type="button" class="directory" data-path="${escapeAttr(item.path)}">📁 ${escapeHtml(item.name)}</button>`).join('');
    document.querySelectorAll('.directory').forEach(item => item.onclick = () => openDirectory(item.dataset.path));
  } catch (error) { toast(error.message, true); }
}

$('#choose-folder').onclick = async () => {
  try {
    await api('/api/roots', {method: 'POST', body: JSON.stringify({kind: browseKind, path: browsePath})});
    $('#browser').close(); await loadRoots(); await loadLibrary(); toast('Media folder added');
  } catch (error) { toast(error.message, true); }
};

async function loadLibrary() {
  const kind = $('#kind-filter').value;
  $('#files').innerHTML = '';
  try {
    const files = await api(`/api/library${kind ? `?kind=${kind}` : ''}`);
    $('#empty').style.display = files.length ? 'none' : 'block';
    $('#files').innerHTML = files.map(file => `<tr data-path="${escapeAttr(file.path)}"><td><input type="checkbox" aria-label="Select"></td><td><strong>${escapeHtml(file.name)}</strong><br><span class="muted">${escapeHtml(file.relative_path)}</span></td><td>${escapeHtml(file.root_name)}</td><td>${formatBytes(file.size)}</td></tr>`).join('');
    document.querySelectorAll('#files tr').forEach(row => row.onclick = event => { if (event.target.type !== 'checkbox') inspect(row); });
  } catch (error) { toast(error.message, true); }
}

async function inspect(row) {
  document.querySelectorAll('#files tr').forEach(item => item.classList.remove('selected')); row.classList.add('selected');
  selectedPath = row.dataset.path; $('#inspector').innerHTML = '<h3>Stream editor</h3><p class="muted">Inspecting media…</p>';
  try {
    const media = await api(`/api/media/probe?path=${encodeURIComponent(selectedPath)}`);
    const streams = media.streams || [];
    $('#inspector').innerHTML = `<h3>${escapeHtml(selectedPath.split('/').pop())}</h3><p class="muted">${streams.length} streams · changes remux this file in place</p><form id="stream-form">${streams.map(streamEditor).join('')}<button class="primary" type="submit">Apply changes</button></form>`;
    $('#stream-form').onsubmit = saveStreams;
  } catch (error) { $('#inspector').innerHTML = `<h3>Stream editor</h3><p class="error">${escapeHtml(error.message)}</p>`; }
}

function streamEditor(stream) {
  const tags = stream.tags || {}, disposition = stream.disposition || {}, language = tags.language || '';
  const split = language.match(/^([A-Za-z]{2,3})-([A-Za-z]{2})$/);
  return `<div class="stream" data-index="${stream.index}"><h4>${stream.codec_type} ${stream.index} · ${escapeHtml(stream.codec_name || 'unknown')}</h4><div class="form-row"><label>Language<input name="language" value="${escapeAttr(split ? split[1] : language)}" placeholder="eng"></label><label>Region<input name="region" value="${escapeAttr(split ? split[2] : '')}" placeholder="US"></label></div><div class="form-row"><label>Title<input name="title" value="${escapeAttr(tags.title || '')}"></label></div><div class="checks"><label><input type="checkbox" name="default" ${disposition.default ? 'checked' : ''}>Default</label><label><input type="checkbox" name="forced" ${disposition.forced ? 'checked' : ''}>Forced</label></div></div>`;
}

async function saveStreams(event) {
  event.preventDefault();
  const button = event.target.querySelector('button[type=submit]'); button.disabled = true; button.textContent = 'Applying…';
  const streams = [...document.querySelectorAll('.stream')].map(element => ({stream_index: Number(element.dataset.index), language: element.querySelector('[name=language]').value, region: element.querySelector('[name=region]').value, title: element.querySelector('[name=title]').value, default: element.querySelector('[name=default]').checked, forced: element.querySelector('[name=forced]').checked}));
  const checked = [...document.querySelectorAll('#files tr')].filter(row => row.querySelector('input[type=checkbox]').checked).map(row => row.dataset.path);  const paths = checked.length ? checked : [selectedPath];  try { await api('/api/media/edit', {method: 'POST', body: JSON.stringify({paths, streams})}); toast('Metadata updated on ' + paths.length + (paths.length === 1 ? ' file' : ' files')); }
  catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = 'Apply changes'; }
}

function escapeHtml(value) { const div = document.createElement('div'); div.textContent = String(value); return div.innerHTML; }
function escapeAttr(value) { return escapeHtml(value).replaceAll('"', '&quot;'); }
function formatBytes(bytes) { if (!bytes) return '0 B'; const unit = Math.floor(Math.log(bytes) / Math.log(1024)); return `${(bytes / 1024 ** unit).toFixed(unit ? 1 : 0)} ${['B','KB','MB','GB','TB'][unit]}`; }
$('#refresh').onclick = loadLibrary; $('#kind-filter').onchange = loadLibrary;
loadRoots(); loadLibrary();
