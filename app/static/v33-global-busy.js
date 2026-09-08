let globalBusyRequests = 0, globalBusyReleaseTimer = null;
const originalFetch = window.fetch.bind(window);

function isApplicationRequest(resource) {
  const value = typeof resource === 'string' ? resource : resource?.url || '';
  try { return new URL(value, window.location.href).pathname.startsWith('/api/'); }
  catch (_) { return value.startsWith('/api/'); }
}

function busyContext(resource, options) {
  let path = '';
  try {
    const payload = options?.body && typeof options.body === 'string' ? JSON.parse(options.body) : null;
    path = payload?.path || payload?.edit?.path || payload?.request?.path || payload?.paths?.[0] || payload?.request?.paths?.[0] || '';
  } catch (_) { /* Non-JSON request bodies have no media context. */ }
  const match = String(path).match(/(?:^|[^a-z])((?:S\d{1,2}E\d{1,2}))(?:[^a-z]|$)/i);
  return match ? `Processing episode ${match[1].toUpperCase()}` : '';
}

function beginGlobalBusy(context = '') {
  clearTimeout(globalBusyReleaseTimer);
  globalBusyRequests++;
  if (globalBusyRequests === 1 && !document.getElementById('global-busy-overlay')) {
    const overlay = document.createElement('div');
    overlay.id = 'global-busy-overlay';
    overlay.setAttribute('role', 'status');
    overlay.setAttribute('aria-live', 'polite');
    overlay.innerHTML = '<div class="busy-card"><div class="busy-ring indeterminate" aria-hidden="true"></div><strong>Processing…</strong><p data-busy-context>Controls temporarily locked</p></div>';
    document.body.appendChild(overlay);
    document.body.classList.add('app-busy-overlay');
  }
  if (context) document.querySelector('[data-busy-context]')?.replaceChildren(context);
  document.body.classList.add('app-busy');
  document.body.setAttribute('aria-busy', 'true');
}

function endGlobalBusy() {
  globalBusyRequests = Math.max(0, globalBusyRequests - 1);
  if (globalBusyRequests) return;
  clearTimeout(globalBusyReleaseTimer);
  globalBusyReleaseTimer = setTimeout(() => {
    if (globalBusyRequests) return;
    document.body.classList.remove('app-busy');
    document.body.classList.remove('app-busy-overlay');
    document.getElementById('global-busy-overlay')?.remove();
    document.body.removeAttribute('aria-busy');
  }, 180);
}

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
  const ring = document.querySelector('#global-busy-overlay .busy-ring');
  if (ring) ring.style.setProperty('--busy-progress', `${percent}%`);
  const strong = document.querySelector('#global-busy-overlay .busy-card strong');
  if (strong) strong.textContent = `${percent}% · ${completed} of ${totalTasks} complete`;
  const detail = document.querySelector('[data-busy-context]');
  if (detail) detail.textContent = current?.progress_message || (completed === totalTasks ? 'Finishing…' : 'Preparing next media…');
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
      const state = updateBusyTaskProgress((await response.json()).items || [], ids.length);
      if (state.finished) return state;
      await new Promise(resolve => setTimeout(resolve, 350));
    }
  } finally { endGlobalBusy(); }
};

window.fetch = async function (resource, options) {
  const tracked = isApplicationRequest(resource);
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
