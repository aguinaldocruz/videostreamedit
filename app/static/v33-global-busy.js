let globalBusyRequests = 0, globalBusyReleaseTimer = null;
const originalFetch = window.fetch.bind(window);

function isApplicationRequest(resource) {
  const value = typeof resource === 'string' ? resource : resource?.url || '';
  try { return new URL(value, window.location.href).pathname.startsWith('/api/'); }
  catch (_) { return value.startsWith('/api/'); }
}

function beginGlobalBusy() {
  clearTimeout(globalBusyReleaseTimer);
  globalBusyRequests++;
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
    document.body.removeAttribute('aria-busy');
  }, 180);
}

window.fetch = async function (resource, options) {
  const tracked = isApplicationRequest(resource);
  if (tracked) beginGlobalBusy();
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
