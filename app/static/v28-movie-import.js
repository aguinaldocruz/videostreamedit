let movieImportMode = null, importBrowsePath = '/', importConfig = null;

function ensureMovieImportUi() {
  if (!$('#movie-import-page-button')) {
    document.querySelector('header nav').insertAdjacentHTML('beforeend', '<button id="movie-import-page-button" data-page="movie-import" type="button">Import Movies</button>');
    $('#movie-import-page-button').onclick = () => {document.querySelectorAll('.page').forEach(page => page.classList.add('hidden'));document.querySelectorAll('header nav button').forEach(button => button.classList.remove('active'));$('#movie-import').classList.remove('hidden');$('#movie-import-page-button').classList.add('active');resetChangedEpisodeSession();loadMovieImport()};
  }
  if (!$('#movie-import')) document.querySelector('main').insertAdjacentHTML('beforeend', '<section id="movie-import" class="page hidden"><div class="page-title"><div><h2>Import Movies</h2><p>Copy a new movie into a synchronized Plex folder and edit the copied media streams.</p></div><button id="refresh-movie-import">Refresh</button></div><div class="movie-import-layout"><article><h3>1. Choose source movie</h3><p id="import-current-folder" class="import-path"></p><div id="import-browser" class="import-browser"></div></article><article><h3>2. Choose Plex destination</h3><div id="import-destinations" class="import-destinations"></div><div id="import-selection-summary" class="import-selection-summary">Choose a source movie and destination.</div><button type="button" id="import-edit-copy" class="primary" disabled>Edit streams and copy</button></article></div></section>');
  if (!$('#import-folder-dialog')) document.body.insertAdjacentHTML('beforeend', '<dialog id="import-folder-dialog"><div class="dialog-title"><div><h2>Default movie input folder</h2><code id="import-folder-current">/</code></div><button type="button" class="icon-close" data-close-import-folder>×</button></div><div id="import-folder-list" class="import-folder-list"></div><div class="dialog-actions"><button type="button" data-close-import-folder>Cancel</button><button type="button" id="save-import-folder" class="primary">Use this folder</button></div></dialog>');
  $('#refresh-movie-import').onclick = loadMovieImport;
  $('#import-edit-copy').onclick = beginMovieImportEdit;
  document.querySelectorAll('[data-close-import-folder]').forEach(button => button.onclick = () => $('#import-folder-dialog').close());
  $('#save-import-folder').onclick = saveImportInputFolder;
  ensureImportSetupCards();
}

function ensureImportSetupCards() {
  const setup = $('#setup');
  if (!$('#movie-import-settings')) setup.insertAdjacentHTML('beforeend', '<div class="import-setup-grid"><article id="movie-import-settings"><h3>Movie import</h3><p>Choose the folder where new movies arrive inside this container.</p><div id="movie-import-input-path" class="import-path">Not configured</div><button type="button" id="browse-import-input">Choose input folder</button></article><article id="template-maintenance"><h3>Saved change templates</h3><p>Review or delete browser-local stream change templates.</p><div id="template-maintenance-list"></div><button type="button" id="clear-change-templates" class="danger">Delete all templates</button></article><article id="saved-property-maintenance"><h3>Saved stream properties</h3><p>Edit or remove reusable languages, regions, and track names.</p><div id="saved-property-list"></div></article></div>');
  $('#browse-import-input').onclick = () => openImportFolderPicker(importConfig?.input_folder || '/');
  $('#clear-change-templates').onclick = () => {if(window.confirm('Delete all saved change templates?')){localStorage.removeItem(CHANGE_HISTORY_KEY);localStorage.removeItem(LAST_CHANGE_KEY);renderTemplateMaintenance();scheduleBulkCloneInspection()}};
}

function renderTemplateMaintenance() {
  const list = $('#template-maintenance-list'), templates = readChangeHistory();
  list.innerHTML = templates.length ? templates.map((template,index)=>{const summary=templateSummary(template);return`<div class="template-maintenance-item"><div><strong>${esc(summary.time)}</strong><small>${esc(summary.detail)}</small></div><button type="button" class="danger" data-delete-template="${index}">Delete</button></div>`}).join('') : '<p class="muted">No saved templates.</p>';
  list.querySelectorAll('[data-delete-template]').forEach(button => button.onclick = () => {const templates=readChangeHistory(),removed=templates.splice(Number(button.dataset.deleteTemplate),1);if(removed.length&&readLastChange()&&templateFingerprint(removed[0])===templateFingerprint(readLastChange()))localStorage.removeItem(LAST_CHANGE_KEY);localStorage.setItem(CHANGE_HISTORY_KEY,JSON.stringify(templates.slice(0,10)));renderTemplateMaintenance();scheduleBulkCloneInspection()});
}

async function renderSavedPropertyMaintenance(){const list=$("#saved-property-list");if(!list)return;try{const data=await api("/api/v8/saved-values"),labels={language:"Languages",region:"Regions",title_audio:"Audio track names",title_subtitle:"Subtitle track names"};list.innerHTML=Object.entries(labels).map(([field,label])=>`<section class="saved-property-group"><strong>${label}</strong>${(data[field]||[]).length?(data[field]||[]).map(value=>`<div class="saved-property-item"><input type="text" value="${attr(value)}" data-saved-field="${field}" data-saved-original="${attr(value)}"><button type="button" data-update-saved>Save</button><button type="button" class="danger" data-remove-saved>Delete</button></div>`).join(""):`<small class="muted">None saved</small>`}</section>`).join("");list.querySelectorAll("[data-update-saved]").forEach(button=>button.onclick=()=>updateSavedProperty(button));list.querySelectorAll("[data-remove-saved]").forEach(button=>button.onclick=()=>removeSavedProperty(button))}catch(error){list.innerHTML=`<p class="error">${esc(error.message)}</p>`}}
async function updateSavedProperty(button){const input=button.parentElement.querySelector("input"),newValue=input.value.trim();if(!newValue){toast("Saved value cannot be empty",true);return}try{const result=await api("/api/v34/saved-values",{method:"PUT",body:JSON.stringify({field:input.dataset.savedField,value:input.dataset.savedOriginal,new_value:newValue})});if(!result.updated)throw new Error("Saved value was not found");toast("Saved property updated");await renderSavedPropertyMaintenance()}catch(error){toast(error.message,true)}}
async function removeSavedProperty(button){const input=button.parentElement.querySelector("input");if(!window.confirm(`Remove saved value “${input.dataset.savedOriginal}”?`))return;try{await api("/api/v34/saved-values",{method:"DELETE",body:JSON.stringify({field:input.dataset.savedField,value:input.dataset.savedOriginal})});toast("Saved property removed");await renderSavedPropertyMaintenance()}catch(error){toast(error.message,true)}}

const importPage = page;
page = function(name) { importPage(name); if(name==='setup'){loadImportConfig();renderTemplateMaintenance();renderSavedPropertyMaintenance()} };

async function loadImportConfig() {
  try {importConfig=await api('/api/v28/import/config');$('#movie-import-input-path').textContent=importConfig.input_folder||'Not configured'} catch(error){toast(error.message,true)}
}

async function openImportFolderPicker(path) {
  try {const data=await api(`/api/browse?path=${encodeURIComponent(path)}`);importBrowsePath=data.path;$('#import-folder-current').textContent=data.path;$('#import-folder-list').innerHTML=(data.parent?`<button type="button" data-folder="${attr(data.parent)}">↰ ..</button>`:'')+data.directories.map(item=>`<button type="button" data-folder="${attr(item.path)}">📁 ${esc(item.name)}</button>`).join('');$('#import-folder-list').querySelectorAll('[data-folder]').forEach(button=>button.onclick=()=>openImportFolderPicker(button.dataset.folder));if(!$('#import-folder-dialog').open)$('#import-folder-dialog').showModal()}catch(error){toast(error.message,true)}
}

async function saveImportInputFolder() {
  try {importConfig=await api('/api/v28/import/config',{method:'PUT',body:JSON.stringify({input_folder:importBrowsePath})});$('#movie-import-input-path').textContent=importConfig.input_folder;$('#import-folder-dialog').close();toast('Movie input folder saved')}catch(error){toast(error.message,true)}
}

async function loadMovieImport() {
  try {const [config,destinations]=await Promise.all([api('/api/v28/import/config'),api('/api/v28/import/destinations')]);importConfig=config;renderImportDestinations(destinations);if(config.input_folder)await browseImportMovies(config.input_folder);else{$('#import-current-folder').textContent='Configure an input folder in Setup first.';$('#import-browser').innerHTML=''}}catch(error){toast(error.message,true)}
}

function renderImportDestinations(destinations) {
  $('#import-destinations').innerHTML=destinations.length?destinations.map((item,index)=>`<label class="import-destination"><input type="radio" name="import-destination" value="${attr(item.path)}" ${index===0?'checked':''}><span><strong>${esc(item.name)}</strong><small>${item.movie_count} movies · ${esc(item.path)}</small></span></label>`).join(''):'<p>No synchronized Plex movie destinations.</p>';
  document.querySelectorAll('[name=import-destination]').forEach(input=>input.onchange=updateImportSelection);updateImportSelection();
}

async function browseImportMovies(path) {
  try {const data=await api(`/api/v28/import/browse?path=${encodeURIComponent(path)}`);$('#import-current-folder').textContent=data.path;const parent=data.parent?`<button type="button" class="import-browser-row folder" data-import-folder="${attr(data.parent)}">↰ ..</button>`:'';$('#import-browser').innerHTML=parent+data.directories.map(item=>`<button type="button" class="import-browser-row folder" data-import-folder="${attr(item.path)}">📁 ${esc(item.name)}</button>`).join('')+data.files.map(item=>`<button type="button" class="import-browser-row movie" data-import-file="${attr(item.path)}" data-name="${attr(item.name)}"><span>🎬 ${esc(item.name)}</span><small>${bytes(item.size)}</small></button>`).join('');document.querySelectorAll('[data-import-folder]').forEach(button=>button.onclick=()=>browseImportMovies(button.dataset.importFolder));document.querySelectorAll('[data-import-file]').forEach(button=>button.onclick=()=>selectImportMovie(button))}catch(error){toast(error.message,true)}
}

function selectImportMovie(button) {document.querySelectorAll('[data-import-file]').forEach(item=>item.classList.toggle('active',item===button));movieImportMode={source:button.dataset.importFile,sourceName:button.dataset.name,filename:button.dataset.name,editing:false};updateImportSelection()}

function updateImportSelection(){const destination=document.querySelector("[name=import-destination]:checked")?.value;if(movieImportMode)movieImportMode.destination=destination;const valid=Boolean(movieImportMode?.source&&destination);$("#import-edit-copy").disabled=!valid;$("#import-selection-summary").textContent=valid?`${movieImportMode.sourceName} → ${destination}`:"Choose a source movie and destination."}
async function beginMovieImportEdit() {updateImportSelection();if(!movieImportMode?.destination)return;movieImportMode.editing=true;await openEditor(movieImportMode.source,movieImportMode.sourceName);updateQueuedChangeLabels()}

function collectImportEditPayload() {
  const rows=[...document.querySelectorAll('#stream-content .stream-row')],tracks=[],external=[],order=[],remove=[];
  rows.forEach(row=>{if(row.querySelector('[name=remove]').checked)remove.push(row.dataset.key);const language=row.querySelector('[name=language]'),region=row.querySelector('[name=region]'),title=row.querySelector('[name=title]');if(row.dataset.external==='true'){external.push({path:row.dataset.path,embed:row.querySelector('[name=embed]').checked,language:language.value,region:region.value,title:title.value,forced:false});order.push({source:'external',codec_type:'subtitle',path:row.dataset.path})}else{const update={codec_type:row.dataset.codecType,type_index:Number(row.dataset.typeIndex)};if(language.dataset.dirty==='true'||region.dataset.dirty==='true'){update.language=language.value;update.region=region.value}if(title.dataset.dirty==='true')update.title=title.value;tracks.push(update);order.push({source:'embedded',codec_type:row.dataset.codecType,type_index:Number(row.dataset.typeIndex)})}});
  const selected=name=>document.querySelector(`#stream-content [name=${name}]:checked`)?.value||null;
  const defaultSubtitle=selected('default-subtitle'),forcedSubtitle=selected('forced-subtitle');for(const choice of[defaultSubtitle,forcedSubtitle])if(choice?.startsWith('external:')){const item=external.find(value=>`external:${value.path}`===choice);if(item)item.embed=true}
  return{path:movieImportMode.source,tracks,external_subtitles:external,order,default_audio:selected('default-audio'),forced_audio:selected('forced-audio'),default_subtitle:defaultSubtitle,forced_subtitle:forcedSubtitle,remove};
}

function collectImportUsedValues(){const values=[];document.querySelectorAll("#stream-content .stream-row").forEach(row=>{const removed=row.querySelector("[name=remove]").checked,embedded=row.dataset.external!=="true"||row.querySelector("[name=embed]").checked;if(removed||!embedded)return;for(const[field,name]of[["language","language"],["region","region"],[row.dataset.codecType==="audio"?"title_audio":"title_subtitle","title"]]){const input=row.querySelector(`[name=${name}]`);if(input.dataset.dirty==="true"&&input.value.trim())values.push({field,value:input.value.trim()})}});return values}

$('#stream-form').addEventListener('submit',async event=>{if(!movieImportMode?.editing)return;event.preventDefault();event.stopImmediatePropagation();const mode={...movieImportMode},button=event.target.querySelector('[type=submit]'),queued=queuedChangeCount(),usedValues=collectImportUsedValues();button.disabled=true;setApplyProgress(1,4,'Copying movie','Creating the destination media file');try{mode.filename=validateStreamFilename();const result=await api('/api/v28/import/movie',{method:'POST',body:JSON.stringify({source:mode.source,destination:mode.destination,filename:mode.filename,edit:collectImportEditPayload()})});await offerSavedValues(usedValues);setApplyProgress(4,4,'Import complete',result.target);$('#stream-dialog').close();movieImportMode=null;toast('Movie copied and stream changes applied');if(queued)document.dispatchEvent(new CustomEvent('media-properties-applied',{detail:{path:result.target}}));if(window.confirm('Movie imported successfully. Remove the original media and matching external subtitles?')){await api('/api/v28/import/cleanup',{method:'POST',body:JSON.stringify({source:mode.source})});toast('Original media and subtitles removed')}await loadMovieImport()}catch(error){setApplyProgress(4,4,'Import failed',error.message);toast(error.message,true)}finally{button.disabled=false;updateQueuedChangeLabels()}},true);

const importUpdateQueuedChangeLabels=updateQueuedChangeLabels;
updateQueuedChangeLabels=function(){importUpdateQueuedChangeLabels();if(!movieImportMode?.editing)return;const count=queuedChangeCount(),button=$('#stream-form [type=submit]'),close=$('#stream-form .dialog-actions [data-close-stream]');button.disabled=false;button.textContent=count?`Copy movie with ${count} change${count===1?'':'s'}`:'Copy movie';close.textContent='Cancel'};

document.querySelectorAll('[data-close-stream]').forEach(button=>button.addEventListener('click',()=>{if(movieImportMode){movieImportMode=null;updateQueuedChangeLabels()}}));
ensureMovieImportUi();loadImportConfig();renderTemplateMaintenance();
