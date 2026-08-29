let importDestinationItems = [], destinationBrowsePath = null;

function ensureDestinationBrowser() {
  if ($('#destination-folder-dialog')) return;
  document.body.insertAdjacentHTML('beforeend', '<dialog id="destination-folder-dialog" class="destination-folder-dialog"><div class="dialog-title"><div><h2>Browse Plex destination</h2><code id="destination-folder-current"></code></div><button type="button" class="icon-close" data-close-destination-folder>×</button></div><div id="destination-folder-list" class="import-folder-list"></div><div class="dialog-actions"><button type="button" data-close-destination-folder>Cancel</button><button type="button" id="choose-destination-folder" class="primary">Use this folder</button></div></dialog>');
  document.querySelectorAll('[data-close-destination-folder]').forEach(button => button.onclick = () => $('#destination-folder-dialog').close());
  $('#choose-destination-folder').onclick = chooseBrowsedDestination;
}

const browsableRenderImportDestinations = renderImportDestinations;
renderImportDestinations = function (destinations) {
  importDestinationItems = destinations;
  browsableRenderImportDestinations(destinations);
  $('#import-destinations').insertAdjacentHTML('beforeend', '<button type="button" id="browse-output-folder" class="browse-output-folder">Browse another Plex folder…</button>');
  $('#browse-output-folder').onclick = () => openDestinationBrowser(importDestinationItems[0]?.path);
};

async function openDestinationBrowser(path) {
  if (!path) {toast('No synchronized Plex movie folder is available', true);return}
  ensureDestinationBrowser();
  try {
    const data = await api(`/api/v31/import/destination/browse?path=${encodeURIComponent(path)}`);
    destinationBrowsePath = data.path;
    $('#destination-folder-current').textContent = data.path;
    $('#destination-folder-list').innerHTML = data.directories.length ? data.directories.map(item => `<button type="button" data-destination-folder="${attr(item.path)}">📁 ${esc(item.name)}</button>`).join('') : '<p class="muted">No subfolders.</p>';
    $('#destination-folder-list').querySelectorAll('[data-destination-folder]').forEach(button => button.onclick = () => openDestinationBrowser(button.dataset.destinationFolder));
    if (!$('#destination-folder-dialog').open) $('#destination-folder-dialog').showModal();
  } catch (error) {toast(error.message, true)}
}

async function chooseBrowsedDestination() {
  try {
    const selected = await api('/api/v31/import/destinations/custom', {method: 'POST', body: JSON.stringify({path: destinationBrowsePath})});
    const destinations = await api('/api/v28/import/destinations');
    renderImportDestinations(destinations);
    const radio = [...document.querySelectorAll('[name=import-destination]')].find(input => input.value === selected.path);
    if (radio) {radio.checked = true;radio.dispatchEvent(new Event('change', {bubbles: true}))}
    $('#destination-folder-dialog').close();
    toast('Output folder selected');
  } catch (error) {toast(error.message, true)}
}

ensureDestinationBrowser();
