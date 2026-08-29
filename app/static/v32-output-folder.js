let configuredOutputRoot = '', currentImportOutput = '', outputSetupBrowsePath = '/';

function ensureOutputSetup() {
  if (!$('#movie-output-settings')) {
    $('#movie-import-settings').insertAdjacentHTML('beforeend', '<hr><h3 id="movie-output-settings">Movie output</h3><p>Choose the default destination root. Import browsing stays inside this folder.</p><div id="movie-import-output-path" class="import-path">Not configured</div><button type="button" id="browse-import-output">Choose output folder</button>');
    $('#browse-import-output').onclick = () => openOutputSetupBrowser('/');
  }
  if (!$('#output-setup-dialog')) document.body.insertAdjacentHTML('beforeend', '<dialog id="output-setup-dialog"><div class="dialog-title"><div><h2>Default movie output folder</h2><code id="output-setup-current">/</code></div><button type="button" class="icon-close" data-close-output-setup>×</button></div><div id="output-setup-list" class="import-folder-list"></div><div class="dialog-actions"><button type="button" data-close-output-setup>Cancel</button><button type="button" id="save-output-setup" class="primary">Use this folder</button></div></dialog>');
  document.querySelectorAll('[data-close-output-setup]').forEach(button => button.onclick = () => $('#output-setup-dialog').close());
  $('#save-output-setup').onclick = saveOutputSetupFolder;
}

async function loadOutputConfig() {
  ensureOutputSetup();
  try {const config=await api('/api/v32/import/output/config');configuredOutputRoot=config.output_folder||'';$('#movie-import-output-path').textContent=configuredOutputRoot||'Not configured'}catch(error){toast(error.message,true)}
}

async function openOutputSetupBrowser(path) {
  try {const data=await api(`/api/browse?path=${encodeURIComponent(path)}`);outputSetupBrowsePath=data.path;$('#output-setup-current').textContent=data.path;$('#output-setup-list').innerHTML=data.directories.map(item=>`<button type="button" data-output-setup-folder="${attr(item.path)}">📁 ${esc(item.name)}</button>`).join('')||'<p class="muted">No subfolders.</p>';$('#output-setup-list').querySelectorAll('[data-output-setup-folder]').forEach(button=>button.onclick=()=>openOutputSetupBrowser(button.dataset.outputSetupFolder));if(!$('#output-setup-dialog').open)$('#output-setup-dialog').showModal()}catch(error){toast(error.message,true)}
}

async function saveOutputSetupFolder() {
  try {const config=await api('/api/v32/import/output/config',{method:'PUT',body:JSON.stringify({path:outputSetupBrowsePath})});configuredOutputRoot=config.output_folder;currentImportOutput=configuredOutputRoot;$('#movie-import-output-path').textContent=configuredOutputRoot;$('#output-setup-dialog').close();renderFilesystemOutputFolder();toast('Default movie output folder saved')}catch(error){toast(error.message,true)}
}

async function loadMovieImport() {
  try {const [input,output]=await Promise.all([api('/api/v28/import/config'),api('/api/v32/import/output/config')]);importConfig=input;configuredOutputRoot=output.output_folder||'';currentImportOutput=output.last_output_folder||configuredOutputRoot;renderFilesystemOutputFolder();if(input.input_folder)await browseImportMovies(input.last_input_folder||input.input_folder);else{$('#import-current-folder').textContent='Configure an input folder in Setup first.';$('#import-browser').innerHTML=''}}catch(error){toast(error.message,true)}
}

function renderFilesystemOutputFolder() {
  const container=$('#import-destinations');
  if(currentImportOutput)container.innerHTML=`<label class="import-destination filesystem-output"><input type="radio" name="import-destination" value="${attr(currentImportOutput)}" checked><span><strong>${currentImportOutput===configuredOutputRoot?'Default output folder':'Selected output folder'}</strong><small>${esc(currentImportOutput)}</small></span></label><button type="button" id="browse-output-folder" class="browse-output-folder">Browse another output folder…</button>`;
  else container.innerHTML='<p class="muted">Configure the default output folder in Setup first.</p>';
  container.querySelector('[name=import-destination]')?.addEventListener('change',updateImportSelection);
  if($('#browse-output-folder'))$('#browse-output-folder').onclick=()=>openConfiguredOutputBrowser(configuredOutputRoot);
  updateImportSelection();
}

async function openConfiguredOutputBrowser(path) {
  if(!path){toast('Configure the output folder in Setup first',true);return}ensureDestinationBrowser();try{const data=await api(`/api/v32/import/output/browse?path=${encodeURIComponent(path)}`);destinationBrowsePath=data.path;$('#destination-folder-dialog h2').textContent='Browse output folder';$('#destination-folder-current').textContent=data.path;$('#destination-folder-list').innerHTML=data.directories.map(item=>`<button type="button" data-destination-folder="${attr(item.path)}">📁 ${esc(item.name)}</button>`).join('')||'<p class="muted">No subfolders.</p>';$('#destination-folder-list').querySelectorAll('[data-destination-folder]').forEach(button=>button.onclick=()=>openConfiguredOutputBrowser(button.dataset.destinationFolder));if(!$('#destination-folder-dialog').open)$('#destination-folder-dialog').showModal()}catch(error){toast(error.message,true)}
}

chooseBrowsedDestination=async function(){try{const selected=await api('/api/v32/import/output/select',{method:'POST',body:JSON.stringify({path:destinationBrowsePath})});currentImportOutput=selected.path;renderFilesystemOutputFolder();$('#destination-folder-dialog').close();toast('Output folder selected for this import')}catch(error){toast(error.message,true)}};
setTimeout(() => { if ($('#choose-destination-folder')) $('#choose-destination-folder').onclick = chooseBrowsedDestination; }, 0);

const outputPage=page;
page=function(name){outputPage(name);if(name==='setup')loadOutputConfig()};
ensureOutputSetup();loadOutputConfig();
