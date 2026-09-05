let bulkAudioInspection=null,bulkInspectionGeneration=0,bulkInspectionTimer=null;

function ensureBulkAudioUi(){
  if(!$('#bulk-audio-button')){$('.episode-heading').insertAdjacentHTML('beforeend','<button type="button" id="bulk-audio-button" class="bulk-audio-button hidden">Edit common audio</button>')}
  if(!$('#bulk-audio-dialog'))document.body.insertAdjacentHTML('beforeend','<dialog id="bulk-audio-dialog" class="bulk-audio-dialog"><form id="bulk-audio-form"><div class="dialog-title"><div><h2>Common audio properties</h2><p id="bulk-audio-scope"></p></div><button type="button" class="icon-close" data-close-bulk>×</button></div><div id="bulk-audio-content" class="bulk-audio-content"></div><div class="dialog-actions"><div id="bulk-audio-progress" class="apply-progress"><strong>Ready</strong><small>No pending changes</small></div><button type="button" data-close-bulk>Close</button><button type="submit" class="primary" disabled>Apply changes</button></div></form></dialog>');
  document.querySelectorAll('[data-close-bulk]').forEach(button=>button.onclick=()=>$('#bulk-audio-dialog').close());
  $('#bulk-audio-button').onclick=openBulkAudioEditor;$('#bulk-audio-form').onsubmit=applyBulkAudio;
}

function listedEpisodePaths(){return[...document.querySelectorAll('#episode-list .edit-file')].map(button=>button.dataset.path)}
async function inspectListedEpisodes(){
  ensureBulkAudioUi();const button=$('#bulk-audio-button'),paths=listedEpisodePaths(),generation=++bulkInspectionGeneration;bulkAudioInspection=null;button.classList.add('hidden');
  if(!state.currentShow||!paths.length)return;
  button.textContent='Checking common audio…';button.disabled=true;button.classList.remove('hidden');
  try{const result=await api('/api/v16/tv/bulk-audio/inspect',{method:'POST',body:JSON.stringify({paths})});if(generation!==bulkInspectionGeneration)return;bulkAudioInspection=result;if(result.eligible){button.textContent=`Edit common audio · ${result.count} episodes`;button.disabled=false}else button.classList.add('hidden')}
  catch(error){if(generation===bulkInspectionGeneration){button.classList.add('hidden');console.error('Bulk audio inspection failed',error)}}
}

const bulkRenderEpisodes=renderEpisodes;
renderEpisodes=function(){bulkRenderEpisodes()};

function bulkField(name,label,value,list=''){return`<label class="bulk-field"><span>${label}</span><input type="text" name="${name}" value="${attr(value||'')}" ${list?`list="${list}"`:''}></label>`}
async function openBulkAudioEditor(){
  if(!bulkAudioInspection?.eligible)return;v8Saved=await api('/api/v8/saved-values');const common=bulkAudioInspection.common,fields=[];
  if('language'in common)fields.push(bulkField('language','Language',common.language,'bulk-saved-language'));
  if('region'in common)fields.push(bulkField('region','Region',common.region,'bulk-saved-region'));
  if('title'in common)fields.push(bulkField('title','Track name',common.title,'bulk-saved-title'));
  if('default'in common)fields.push(`<label class="bulk-flag"><input type="checkbox" name="default" ${common.default?'checked':''}> Default</label>`);
  if('forced'in common)fields.push(`<label class="bulk-flag"><input type="checkbox" name="forced" ${common.forced?'checked':''}> Forced</label>`);
  $('#bulk-audio-scope').textContent=`${clean(state.currentShow?.name||'TV show')} · changes will be applied separately to all ${bulkAudioInspection.count} listed episodes`;
  $('#bulk-audio-content').innerHTML=fields.length?`<p>Only properties with the same current value across every listed episode are available.</p><div class="bulk-fields">${fields.join('')}</div><datalist id="bulk-saved-language">${savedOptions('language')}</datalist><datalist id="bulk-saved-region">${savedOptions('region')}</datalist><datalist id="bulk-saved-title">${savedOptions('title_audio')}</datalist>`:'<p>No properties have the same value across all listed episodes.</p>';
  $('#bulk-audio-content').querySelectorAll('input').forEach(input=>{input.dataset.initial=input.type==='checkbox'?String(input.checked):input.value;input.oninput=updateBulkApply;input.onchange=updateBulkApply});
  $('#bulk-audio-progress').innerHTML='<strong>Ready</strong><small>No pending changes</small>';updateBulkApply();$('#bulk-audio-dialog').showModal();
}

function changedBulkFields(){return[...$('#bulk-audio-content').querySelectorAll('input[name]')].filter(input=>(input.type==='checkbox'?String(input.checked):input.value)!==input.dataset.initial)}
function updateBulkApply(){const fields=changedBulkFields(),total=fields.length*(bulkAudioInspection?.count||0),button=$('#bulk-audio-form [type=submit]'),close=$('#bulk-audio-form [data-close-bulk]:not(.icon-close)');button.disabled=!total;button.textContent=total?`Apply ${total} changes`:'Apply changes';close.textContent=total?'Cancel':'Close';if(total)$('#bulk-audio-progress').innerHTML=`<strong>${total} changes queued</strong><small>${fields.map(input=>input.name).join(' · ')}</small>`}

async function applyBulkAudio(event){
  event.preventDefault();const fields=changedBulkFields();if(!fields.length)return;const form=$('#bulk-audio-form'),button=form.querySelector('[type=submit]'),close=form.querySelector('[data-close-bulk]:not(.icon-close)'),changed=new Set(fields.map(input=>input.name)),usedValues=[];button.disabled=true;close.disabled=true;
  let completed=0;
  try{for(const item of bulkAudioInspection.items){const audio=item.audio,language=form.elements.language,region=form.elements.region,title=form.elements.title,track={codec_type:'audio',type_index:0};if(changed.has('language')||changed.has('region')){track.language=changed.has('language')?language.value:audio.language;track.region=changed.has('region')?region.value:audio.region}if(changed.has('title'))track.title=title.value;const tracks=Object.keys(track).length>2?[track]:[],defaultValue=changed.has('default')?form.elements.default.checked:audio.default,forcedValue=changed.has('forced')?form.elements.forced.checked:audio.forced;$('#bulk-audio-progress').innerHTML=`<strong>Editing episode ${completed+1} of ${bulkAudioInspection.count}</strong><small>${esc(item.path)}</small>`;await api('/api/v7/media/edit',{method:'POST',body:JSON.stringify({path:item.path,tracks,external_subtitles:[],order:[{source:'embedded',codec_type:'audio',type_index:0}],default_audio:defaultValue?'embedded:audio:0':null,forced_audio:forcedValue?'embedded:audio:0':null,default_subtitle:null,forced_subtitle:null,remove:[]})});completed++}
    for(const input of fields)if(['language','region','title'].includes(input.name)&&input.value.trim())for(let index=0;index<bulkAudioInspection.count;index++)usedValues.push({field:input.name==='title'?'title_audio':input.name,value:input.value.trim()});await offerSavedValues(usedValues);$('#bulk-audio-progress').innerHTML=`<strong>Complete</strong><small>${completed} episodes updated successfully</small>`;toast(`${completed} episodes updated`);await inspectListedEpisodes();
    $('#bulk-audio-content').querySelectorAll('input[name]').forEach(input=>input.dataset.initial=input.type==='checkbox'?String(input.checked):input.value);updateBulkApply();$('#bulk-audio-progress').innerHTML='<strong>Complete</strong><small>'+completed+' episodes updated successfully</small>';
  }catch(error){$('#bulk-audio-progress').innerHTML=`<strong class="error">Stopped after ${completed} episodes</strong><small>${esc(error.message)}</small>`;toast(error.message,true)}finally{close.disabled=false;updateBulkApply()}
}

// The stream-value filter/editor supersedes the old automatic common-audio
// inspection. Keep listedEpisodePaths() for clone workflows, but do not create
// the legacy header control, dialog, timers, or network checks.
$('#bulk-audio-button')?.remove();
$('#bulk-audio-dialog')?.remove();
