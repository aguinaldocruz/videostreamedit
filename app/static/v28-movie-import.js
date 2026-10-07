let movieImportMode = null, importBrowsePath = '/', importConfig = null;
let movieImportBusy = false;
window.isMovieImportBusy = () => movieImportBusy;

function ensureMovieImportUi() {
  if (!$('#movie-import-page-button')) {
    document.querySelector('header nav').insertAdjacentHTML('beforeend', '<button id="movie-import-page-button" data-page="movie-import" type="button">Import Media</button>');
    $('#movie-import-page-button').onclick = () => {document.querySelectorAll('.page').forEach(page => page.classList.add('hidden'));document.querySelectorAll('header nav button').forEach(button => button.classList.remove('active'));$('#movie-import').classList.remove('hidden');$('#movie-import-page-button').classList.add('active');resetChangedEpisodeSession();loadMovieImport()};
  }
  if (!$('#movie-import')) document.querySelector('main').insertAdjacentHTML('beforeend', '<section id="movie-import" class="page hidden"><div class="page-title"><div><h2>Import Media</h2><p>Import a movie or TV episode into its destination folder and edit the copied streams.</p></div><button id="refresh-movie-import">Refresh</button></div><div class="movie-import-layout"><article><h3>1. Choose source media</h3><p id="import-current-folder" class="import-path"></p><div id="import-browser" class="import-browser"></div></article><article><h3>2. Choose Plex destination</h3><div id="import-destinations" class="import-destinations"></div><div id="import-selection-summary" class="import-selection-summary">Choose a source media file and destination.</div><button type="button" id="import-edit-copy" class="primary" disabled>Edit streams and copy</button></article></div></section>');
  if (!$('#import-folder-dialog')) document.body.insertAdjacentHTML('beforeend', '<dialog id="import-folder-dialog"><div class="dialog-title"><div><h2>Default media input folder</h2><code id="import-folder-current">/</code></div><button type="button" class="icon-close" data-close-import-folder>×</button></div><div id="import-folder-list" class="import-folder-list"></div><div class="dialog-actions"><button type="button" data-close-import-folder>Cancel</button><button type="button" id="save-import-folder" class="primary">Use this folder</button></div></dialog>');
  if (!$('#import-processing-dialog')) document.body.insertAdjacentHTML('beforeend', '<dialog id="import-processing-dialog"><div class="dialog-title"><div><h2>Start media import</h2><p>Choose how the complete copy and stream-edit operation will be processed.</p></div><button type="button" class="icon-close" data-import-processing="cancel" aria-label="Cancel">×</button></div><div class="dialog-body"><label><input type="checkbox" id="import-remove-original"> Remove the original media and matching external subtitles only after a successful import</label></div><div class="dialog-actions"><button type="button" data-import-processing="cancel">Cancel</button><button type="button" data-import-processing="queue">Add whole import to queue</button><button type="button" class="primary" data-import-processing="now">Process whole import now</button></div></dialog>');
  $('#refresh-movie-import').onclick = loadMovieImport;
  if (!$('#import-media-kind')) $('#movie-import .page-title').insertAdjacentHTML('afterend', '<label class="import-media-kind">Import type <select id="import-media-kind"><option value="movie">Movie</option><option value="episode">TV episode</option></select><small>For episodes, select the show/season destination and keep SxxExx in the filename.</small></label>');
  $('#import-media-kind').onchange=()=>{if(movieImportMode)movieImportMode.mediaKind=$('#import-media-kind').value;updateImportSelection()};
  $('#import-edit-copy').onclick = beginMovieImportEdit;
  document.querySelectorAll('[data-close-import-folder]').forEach(button => button.onclick = () => $('#import-folder-dialog').close());
  $('#save-import-folder').onclick = saveImportInputFolder;
  ensureImportSetupCards();
}

function ensureImportSetupCards() {
  const setup = $('#setup');
  if (!$('#movie-import-settings')) setup.insertAdjacentHTML('beforeend', '<div class="import-setup-grid"><article id="movie-import-settings"><h3>Media import</h3><p>Choose the folder where new movies and episodes arrive inside this container.</p><div id="movie-import-input-path" class="import-path">Not configured</div><button type="button" id="browse-import-input">Choose input folder</button></article><article id="template-maintenance"><h3>Saved change templates</h3><p>Manage durable stream change templates; local history remains a fallback.</p><div id="template-maintenance-list"></div><details class="setup-advanced"><summary>Advanced template maintenance</summary><button type="button" id="clear-change-templates" class="danger">Delete all templates</button></details></article><article id="saved-property-maintenance"><h3>Saved track names</h3><p>Reusable audio and subtitle track names. Language and region values are managed in the Language / region order tab.</p><div id="saved-property-list"></div></article></div>');
  $('#browse-import-input').onclick = () => openImportFolderPicker(importConfig?.input_folder || '/');
  $('#clear-change-templates').onclick = async () => {if(!window.confirm('Delete all saved change templates?')) return; try { await api('/api/v25/templates', {method:'DELETE'}); if(typeof durableTemplateCache !== 'undefined') durableTemplateCache=[]; } catch (error) { console.warn('Could not clear durable templates', error); } localStorage.removeItem(CHANGE_HISTORY_KEY); localStorage.removeItem(LAST_CHANGE_KEY); renderTemplateMaintenance(); scheduleBulkCloneInspection()};
}

function renderTemplateMaintenance() {
  const list = $('#template-maintenance-list'), templates = typeof allTemplateHistory === 'function' ? allTemplateHistory() : readChangeHistory();
  list.innerHTML = templates.length ? templates.map((template,index)=>{
    const summary=templateSummary(template), durable=Boolean(template.id);
    return `<div class="template-maintenance-item" data-template-row="${index}">
      <div class="template-maintenance-fields">
        ${durable ? `<input type="text" value="${attr(template.name || summary.detail)}" data-template-name aria-label="Template name">` : `<strong>${esc(summary.time)}</strong>`}
        ${durable ? `<input type="text" value="${attr(template.description || '')}" placeholder="Description" data-template-description aria-label="Template description">` : `<small>${esc(summary.detail)}</small>`}
        ${durable ? `<small>${template.use_count || 0} uses · ${template.enabled ? 'Enabled' : 'Disabled'}</small>` : ''}
      </div>
      ${durable ? `<label class="template-enabled"><input type="checkbox" data-template-enabled ${template.enabled ? 'checked' : ''}> Enabled</label><button type="button" data-save-template>Save</button>` : ''}
      <button type="button" class="danger" data-delete-template="${index}">Delete</button>
    </div>`;
  }).join('') : '<p class="muted">No saved templates.</p>';
  list.querySelectorAll('[data-save-template]').forEach(button => button.onclick = async () => {
    const row=button.closest('[data-template-row]'), template=templates[Number(row.dataset.templateRow)];
    if(!template?.id) return;
    button.disabled=true;
    try {
      const result=await api(`/api/v25/templates/${encodeURIComponent(template.id)}`, {method:'PUT', body:JSON.stringify({name:row.querySelector('[data-template-name]').value.trim(), description:row.querySelector('[data-template-description]').value.trim(), enabled:row.querySelector('[data-template-enabled]').checked})});
      if(typeof durableTemplateCache !== 'undefined') durableTemplateCache=durableTemplateCache.map(item=>item.id===template.id?result.template:item);
      toast('Template saved'); renderTemplateMaintenance(); scheduleBulkCloneInspection();
    } catch(error) { toast(error.message,true); button.disabled=false; }
  });
  list.querySelectorAll('[data-delete-template]').forEach(button => button.onclick = async () => {
    const current=readChangeHistory(), removed=current.splice(Number(button.dataset.deleteTemplate),1), item=removed[0];
    if(item?.id){ try { await api(`/api/v25/templates/${encodeURIComponent(item.id)}`, {method:'DELETE'}); if(typeof durableTemplateCache !== 'undefined') durableTemplateCache=durableTemplateCache.filter(template=>template.id!==item.id); } catch(error){ toast(error.message,true); return; } }
    if(removed.length&&readLastChange()&&templateFingerprint(removed[0])===templateFingerprint(readLastChange()))localStorage.removeItem(LAST_CHANGE_KEY);
    localStorage.setItem(CHANGE_HISTORY_KEY,JSON.stringify(current.filter(template=>!template.id).slice(0,10)));
    renderTemplateMaintenance(); scheduleBulkCloneInspection();
  });
}


async function renderSavedPropertyMaintenance(){const list=$("#saved-property-list");if(!list)return;try{const data=await api("/api/v8/saved-values"),labels={title_audio:"Audio track names",title_subtitle:"Subtitle track names"};list.innerHTML=Object.entries(labels).map(([field,label])=>`<section class="saved-property-group"><strong>${label}</strong>${(data[field]||[]).length?(data[field]||[]).map(value=>`<div class="saved-property-item"><input type="text" value="${attr(value)}" data-saved-field="${field}" data-saved-original="${attr(value)}"><button type="button" data-update-saved>Save</button><button type="button" class="danger" data-remove-saved>Delete</button></div>`).join(""):`<small class="muted">None saved</small>`}</section>`).join("");list.querySelectorAll("[data-update-saved]").forEach(button=>button.onclick=()=>updateSavedProperty(button));list.querySelectorAll("[data-remove-saved]").forEach(button=>button.onclick=()=>removeSavedProperty(button))}catch(error){list.innerHTML=`<p class="error">${esc(error.message)}</p>`}}
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
  try {importConfig=await api('/api/v28/import/config',{method:'PUT',body:JSON.stringify({input_folder:importBrowsePath})});$('#movie-import-input-path').textContent=importConfig.input_folder;$('#import-folder-dialog').close();toast('Media input folder saved')}catch(error){toast(error.message,true)}
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

function selectImportMovie(button) {document.querySelectorAll('[data-import-file]').forEach(item=>item.classList.toggle('active',item===button));movieImportMode={source:button.dataset.importFile,sourceName:button.dataset.name,filename:button.dataset.name,mediaKind:$('#import-media-kind').value,editing:false};updateImportSelection()}

function updateImportSelection(){const destination=document.querySelector("[name=import-destination]:checked")?.value;if(movieImportMode)movieImportMode.destination=destination;const valid=Boolean(movieImportMode?.source&&destination);$("#import-edit-copy").disabled=!valid;$("#import-selection-summary").textContent=valid?`${movieImportMode.sourceName} → ${destination}`:"Choose a source media file and destination."}
async function beginMovieImportEdit() {updateImportSelection();if(!movieImportMode?.destination)return;movieImportMode.editing=true;await openEditor(movieImportMode.source,movieImportMode.sourceName);updateQueuedChangeLabels()}

function collectImportEditPayload() {
  const rows=[...document.querySelectorAll('#stream-content .stream-row')],tracks=[],external=[],order=[],remove=[];
  rows.forEach(row=>{if(row.querySelector('[name=remove]').checked)remove.push(row.dataset.key);const language=row.querySelector('[name=language]'),region=row.querySelector('[name=region]'),title=row.querySelector('[name=title]');if(row.dataset.external==='true'){external.push({path:row.dataset.path,embed:row.querySelector('[name=embed]').checked,language:language.value,region:region.value,title:title.value,forced:false});order.push({source:'external',codec_type:'subtitle',path:row.dataset.path})}else{const update={codec_type:row.dataset.codecType,type_index:Number(row.dataset.typeIndex)};if(language.dataset.dirty==='true'||region.dataset.dirty==='true'){update.language=language.value;update.region=region.value}if(title.dataset.dirty==='true')update.title=title.value;tracks.push(update);order.push({source:'embedded',codec_type:row.dataset.codecType,type_index:Number(row.dataset.typeIndex)})}});
  const selected=name=>document.querySelector(`#stream-content [name=${name}]:checked`)?.value||null;
  const defaultSubtitle=selected('default-subtitle'),forcedSubtitle=selected('forced-subtitle');for(const choice of[defaultSubtitle,forcedSubtitle])if(choice?.startsWith('external:')){const item=external.find(value=>`external:${value.path}`===choice);if(item)item.embed=true}
  return{path:movieImportMode.source,tracks,external_subtitles:external,order,default_audio:selected('default-audio'),forced_audio:selected('forced-audio'),default_subtitle:defaultSubtitle,forced_subtitle:forcedSubtitle,remove};
}

function collectImportUsedValues(){const values=[];document.querySelectorAll("#stream-content .stream-row").forEach(row=>{const removed=row.querySelector("[name=remove]").checked,embedded=row.dataset.external!=="true"||row.querySelector("[name=embed]").checked;if(removed||!embedded)return;for(const[field,name]of[["language","language"],["region","region"],[row.dataset.codecType==="audio"?"title_audio":"title_subtitle","title"]]){const input=row.querySelector(`[name=${name}]`);if(input.dataset.dirty==="true"&&input.value.trim())values.push({field,value:input.value.trim()})}});return values}

function chooseImportProcessing(){return new Promise(resolve=>{const dialog=$('#import-processing-dialog');$('#import-remove-original').checked=false;let finished=false;const finish=mode=>{if(finished)return;finished=true;dialog.close();dialog.removeEventListener('cancel',cancel);resolve(mode?{mode,removeOriginal:$('#import-remove-original').checked}:null)};const cancel=event=>{event.preventDefault();finish(null)};dialog.querySelectorAll('[data-import-processing]').forEach(button=>button.onclick=()=>finish(button.dataset.importProcessing==='cancel'?null:button.dataset.importProcessing));dialog.addEventListener('cancel',cancel);dialog.showModal();dialog.querySelector('[data-import-processing=now]').focus()})}

function followMovieImportProgress(operationId,total){
  let stopped=false,timer=null;
  async function poll(){
    try{
      const result=await api(`/api/v28/import/progress/${operationId}`);
      if(stopped)return;
      const detail=[result.detail,result.copy_total?`${result.copy_percent}% copied · ${bytes(result.copy_current)} of ${bytes(result.copy_total)}`:''].filter(Boolean).join(' · ');
      setApplyProgress(result.step,total,result.message,detail);
    }catch(_){/* Initial registration can race the first read; do not interrupt the import. */}
    if(!stopped)timer=setTimeout(poll,750);
  }
  timer=setTimeout(poll,250);
  return ()=>{stopped=true;clearTimeout(timer)};
}

$('#stream-form').addEventListener('submit',async event=>{
  if(!movieImportMode?.editing)return;
  event.preventDefault();event.stopImmediatePropagation();if(movieImportBusy)return;
  const choice=await chooseImportProcessing();if(!choice)return;
  const mode={...movieImportMode},button=event.target.querySelector('[type=submit]'),usedValues=collectImportUsedValues();
  const total=choice.mode==='queue'?2:choice.removeOriginal?7:6;
  let stopProgress=()=>{},busyOwned=true,committed=false;
  movieImportBusy=true;applyProgressBusy=true;button.disabled=true;
  window.beginGlobalBusyImmediate?.(choice.mode==='queue'?'Queueing media import':'Importing media');
  setApplyProgress(1,total,choice.mode==='queue'?'Queueing media import':'Validating media import',mode.sourceName);
  const releaseBusy=()=>{if(!busyOwned)return;busyOwned=false;window.endGlobalBusyOperation?.()};
  try{
    // Paint the modal before validation, serialization, copying or remuxing.
    await new Promise(resolve=>requestAnimationFrame(()=>resolve()));
    mode.filename=validateStreamFilename();
    const request={source:mode.source,destination:mode.destination,filename:mode.filename,media_kind:mode.mediaKind||'movie',edit:collectImportEditPayload(),remove_original:choice.removeOriginal};
    if(choice.mode==='queue'){
      const task=await api('/api/v65/queue',{method:'POST',body:JSON.stringify({task_type:'movie_import',payload:request,label:`Import ${mode.filename}`})});
      if(!task?.id)throw new Error('The queue did not return an import task id');
      committed=true;setApplyProgress(2,2,'Import queued',`Task #${task.id}`);
      $('#stream-dialog').close();movieImportMode=null;releaseBusy();
      toast(`Whole media import added to queue as task #${task.id}`);
    }else{
      request.operation_id=Array.from(crypto.getRandomValues(new Uint8Array(16)),value=>value.toString(16).padStart(2,'0')).join('');
      stopProgress=followMovieImportProgress(request.operation_id,total);
      const result=await api('/api/v28/import/movie',{method:'POST',body:JSON.stringify(request)});
      stopProgress();committed=true;
      const removed=result.source_cleanup_status==='removed';
      if(choice.removeOriginal&&!removed&&!result.warnings?.length)result.warnings=['Import completed, but source removal was not confirmed. Check the source folder.'];
      setApplyProgress(total,total,'Import complete',result.target);
      $('#stream-dialog').close();movieImportMode=null;releaseBusy();
      toast(result.warnings?.length?result.warnings.join(' '):removed?'Media imported and originals removed':'Media copied and stream changes applied',Boolean(result.warnings?.length));
      document.dispatchEvent(new CustomEvent('media-properties-applied',{detail:{path:result.target}}));
    }
    // Learning prompts are user decisions, not part of a locked busy phase.
    await offerSavedValues(usedValues);
    await loadMovieImport();
  }catch(error){
    setApplyProgress(0,0,committed?'Import completed; display refresh failed':'Import failed',error.message);
    toast(committed?`Import already completed. ${error.message}`:error.message,true);
  }finally{
    stopProgress();releaseBusy();movieImportBusy=false;applyProgressBusy=false;
    button.disabled=false;updateQueuedChangeLabels();window.updateStreamEditorContext?.();
  }
},true);

const importUpdateQueuedChangeLabels=updateQueuedChangeLabels;
updateQueuedChangeLabels=function(){importUpdateQueuedChangeLabels();if(!movieImportMode?.editing)return;const count=queuedChangeCount(),button=$('#stream-form [type=submit]'),close=$('#stream-form .dialog-actions [data-close-stream]');button.disabled=movieImportBusy;button.textContent=movieImportBusy?'Importing…':count?`Copy media with ${count} change${count===1?'':'s'}`:'Copy media';close.textContent='Cancel'};

document.querySelectorAll('[data-close-stream]').forEach(button=>button.addEventListener('click',()=>{if(movieImportMode){movieImportMode=null;updateQueuedChangeLabels()}}));
ensureMovieImportUi();loadImportConfig();renderTemplateMaintenance();
