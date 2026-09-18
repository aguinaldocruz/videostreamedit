let globalBusyRequests = 0, globalBusyReleaseTimer = null, globalBusyRevealTimer = null, globalBusyContext = '', globalBusyProgress = null;
const originalFetch = window.fetch.bind(window);

function isApplicationRequest(resource, options = {}) {
  const value = typeof resource === 'string' ? resource : resource?.url || '';
  // Read-only GETs (page loads, filters, queue/status polling) must not
  // trigger the global processing overlay. Only mutating/explicit operations
  // should lock the interface.
  const method = String(options?.method || 'GET').toUpperCase();
  if (method === 'GET' || method === 'HEAD') return false;
  try {
    const path = new URL(value, window.location.href).pathname;
    // These endpoints only load/filter cached values. Their screens already
    // show a local loading state; they must not block the whole page with the
    // processing overlay while a large season is being expanded.
    if (path === '/api/v79/tv/show-status' || path === '/api/v79/tv/season-stream-values' || path === '/api/v82/movies/stream-values'
        || path === '/api/v8/saved-values' || path === '/api/v8/value-uses' || path === '/api/v86/language-region-use') return false;
    return path.startsWith('/api/');
  } catch (_) { return value.startsWith('/api/'); }
}

function busyContext(resource, options) {
  let endpoint = '', mediaPath = '';
  try {
    endpoint = new URL(typeof resource === 'string' ? resource : resource?.url || '', window.location.href).pathname;
    const payload = options?.body && typeof options.body === 'string' ? JSON.parse(options.body) : null;
    mediaPath = payload?.path || payload?.edit?.path || payload?.request?.path || payload?.paths?.[0] || payload?.request?.paths?.[0] || '';
  } catch (_) { /* Non-JSON request bodies have no media context. */ }
  const operation = endpoint.includes('language-detection/stream') ? 'Detecting stream language'
    : endpoint.includes('evaluate-forced') ? 'Evaluating subtitles'
    : endpoint.includes('/media/edit') ? 'Applying stream changes'
    : endpoint.includes('/stream-preview/') ? 'Preparing stream preview'
    : endpoint.includes('/index/request') ? 'Updating media indexes'
    : endpoint.includes('/media/rename') ? 'Renaming media'
    : endpoint.includes('/plex/sync') ? 'Synchronizing Plex catalog'
    : '';
  const match = String(mediaPath).match(/(?:^|[^a-z])((?:S\d{1,2}E\d{1,2}))(?:[^a-z]|$)/i);
  return operation ? `${operation}${match ? ` · episode ${match[1].toUpperCase()}` : ''}` : (match ? `Processing episode ${match[1].toUpperCase()}` : '');
}

function revealGlobalBusy() {
  if (!globalBusyRequests || document.getElementById('global-busy-overlay')) return;
  const overlay = document.createElement('dialog');
  overlay.id = 'global-busy-overlay';
  overlay.setAttribute('role', 'status');
  overlay.setAttribute('aria-live', 'polite');
  overlay.innerHTML = `<div class="busy-card"><div class="busy-ring indeterminate" aria-hidden="true"></div><strong>${globalBusyContext || 'Processing…'}</strong><p data-busy-context>${globalBusyContext || 'Processing…'}</p></div>`;
  document.body.appendChild(overlay);
  try { overlay.showModal(); } catch (_) { overlay.setAttribute('open', ''); }
  document.body.classList.add('app-busy-overlay', 'app-busy');
  document.body.setAttribute('aria-busy', 'true');
  if (globalBusyContext) document.querySelector('[data-busy-context]')?.replaceChildren(globalBusyContext);
  applyGlobalBusyProgress();
}

function beginGlobalBusy(context = '') {
  clearTimeout(globalBusyReleaseTimer);
  globalBusyRequests++;
  if (context) globalBusyContext = context;
  // Fast requests remain invisible; operations lasting beyond this threshold
  // use the same progress overlay and animated ring as bulk processing.
  if (globalBusyRequests === 1 && !document.getElementById('global-busy-overlay')) {
    clearTimeout(globalBusyRevealTimer);
    globalBusyRevealTimer = setTimeout(() => { globalBusyRevealTimer = null; revealGlobalBusy(); }, 350);
  }
  if (document.getElementById('global-busy-overlay')) {
    document.body.classList.add('app-busy');
    const context = globalBusyContext || 'Processing…';
    document.querySelector('#global-busy-overlay .busy-card strong')?.replaceChildren(context);
    document.querySelector('[data-busy-context]')?.replaceChildren(context);
  }
}

function endGlobalBusy() {
  globalBusyRequests = Math.max(0, globalBusyRequests - 1);
  if (globalBusyRequests) return;
  clearTimeout(globalBusyRevealTimer);
  globalBusyRevealTimer = null;
  globalBusyContext = '';
  clearTimeout(globalBusyReleaseTimer);
  globalBusyReleaseTimer = setTimeout(() => {
    if (globalBusyRequests) return;
    document.body.classList.remove('app-busy');
    document.body.classList.remove('app-busy-overlay');
    const overlay = document.getElementById('global-busy-overlay');
    if (overlay?.open) overlay.close();
    overlay?.remove();
    document.body.removeAttribute('aria-busy');
    globalBusyProgress = null;
  }, 180);
}

function applyGlobalBusyProgress() {
  const progress = globalBusyProgress;
  if (!progress) return;
  const ring = document.querySelector('#global-busy-overlay .busy-ring');
  if (ring) {
    const percent = progress.total > 0 ? Math.max(0, Math.min(100, Math.round((progress.step / progress.total) * 100))) : null;
    if (percent !== null) ring.style.setProperty('--busy-progress', `${percent}%`);
  }
  const strong = document.querySelector('#global-busy-overlay .busy-card strong');
  if (strong) strong.textContent = progress.total > 0 ? `${progress.message} · Step ${Math.min(progress.step, progress.total)} of ${progress.total}` : progress.message;
  const detail = document.querySelector('#global-busy-overlay [data-busy-context]');
  if (detail) detail.textContent = progress.detail || progress.message || 'Processing…';
}

window.setGlobalBusyProgress = function(step, total, message, detail = '') {
  globalBusyProgress = {step: Number(step) || 0, total: Number(total) || 0, message: String(message || 'Processing…'), detail: String(detail || '')};
  applyGlobalBusyProgress();
};

function updateBusyTaskProgress(items, totalTasks) {
  const terminal = new Set(['succeeded', 'failed', 'cancelled']);
  let completed = 0, units = 0, total = 0, current = null;
  for (const item of items) {
    if (terminal.has(item.status)) { completed++; units += 1; total += 1; continue; }
    if (item.status === 'running' && !current) current = item;
    const itemTotal = Number(item.progress_total) > 0 ? Number(item.progress_total) : 1;
    const itemDone = Number(item.progress_current) > 0 ? Math.min(Number(item.progress_current), itemTotal) : 0;
    units += itemDone / itemTotal; total += 1;
  }
  total = Math.max(total, totalTasks || items.length || 1);
  const percent = Math.max(0, Math.min(100, Math.round((units / total) * 100)));
  const detailed = items.some(item => Number(item.progress_total) > 0 || Number(item.progress_current) > 0);
  const ring = document.querySelector('#global-busy-overlay .busy-ring');
  if (ring) ring.style.setProperty('--busy-progress', `${percent}%`);
  const strong = document.querySelector('#global-busy-overlay .busy-card strong');
  if (strong) strong.textContent = detailed || completed === totalTasks
    ? `${percent}% · ${completed} of ${totalTasks} complete`
    : `Step ${Math.min(completed + 1, totalTasks)} of ${totalTasks}`;
  const detail = document.querySelector('[data-busy-context]');
  if (detail) detail.textContent = current?.progress_message || (completed === totalTasks ? 'Finishing…' : 'Preparing next media…');
  globalBusyProgress = null;
  return {finished: completed === totalTasks && items.length >= totalTasks, failed: items.filter(item => item.status === 'failed').length, succeeded: items.filter(item => item.status === 'succeeded').length};
}

window.waitForGlobalTasks = async function (taskIds) {
  const ids = [...new Set((taskIds || []).map(Number).filter(Boolean))];
  if (!ids.length) return {finished: true, failed: 0, succeeded: 0};
  beginGlobalBusy();
  try {
    while (true) {
      const response = await originalFetch('/api/v65/queue/status', {method: 'POST', headers: {'Content-Type': 'application/json', Accept: 'application/json'}, body: JSON.stringify({task_ids: ids})});
      if (!response.ok) throw new Error('Unable to read bulk operation progress');
      const body = await response.json();
      const items = body.items || [];
      const state = updateBusyTaskProgress(items, ids.length);
      if (state.finished) return {...state, items};
      await new Promise(resolve => setTimeout(resolve, 350));
    }
  } finally { endGlobalBusy(); }
};

window.fetch = async function (resource, options) {
  const tracked = isApplicationRequest(resource, options);
  if (tracked) beginGlobalBusy(busyContext(resource, options));
  try { return await originalFetch(resource, options); }
  finally { if (tracked) endGlobalBusy(); }
};

function blockBusyInteraction(event) {
  if (!document.body.classList.contains('app-busy')) return;
  if (!event.target.closest('button,input,select,textarea,a')) return;
  event.preventDefault();
  event.stopImmediatePropagation();
}

document.addEventListener('click', blockBusyInteraction, true);
document.addEventListener('pointerdown', blockBusyInteraction, true);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && document.body.classList.contains('app-busy')) {
    event.preventDefault();
    event.stopImmediatePropagation();
    return;
  }
  if (event.key === 'Enter' || event.key === ' ') blockBusyInteraction(event);
}, true);
document.addEventListener('cancel', event => {
  if (document.body.classList.contains('app-busy')) event.preventDefault();
}, true);
