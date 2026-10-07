let v8Saved={language:[],region:[],title_audio:[],title_subtitle:[]};
state.deferredDetectionPath='';
async function flushDeferredStreamDetection(){const path=state.deferredDetectionPath;if(!path)return;state.deferredDetectionPath='';try{await api('/api/v79/language-detection/flush?path='+encodeURIComponent(path),{method:'POST'});}catch(error){toast('Could not queue language detection: '+error.message,true)}}
let mediaNavigation=[],mediaNavigationIndex=-1,mediaNavigationKind='item';
function ensureMediaNavigation(){if($('#media-navigation'))return;const close=$('#stream-dialog .icon-close');close.insertAdjacentHTML('beforebegin','<div id="media-navigation" class="media-navigation hidden"><button type="button" id="previous-media" aria-label="Previous">&lt;</button><button type="button" id="next-media" aria-label="Next">&gt;</button></div><button type="button" id="movie-note-button" class="note-button" title="Edit movie note" aria-label="Edit movie note">i</button><button type="button" id="stream-final-version" class="final-version-action" title="Mark this media as Final version">Final version</button>');$('#previous-media').onclick=()=>navigateMedia(-1);$('#next-media').onclick=()=>navigateMedia(1);$('#movie-note-button').onclick=()=>openEntityNote('movie',state.selectedPath,$('#selected-file').textContent)}
function setStreamFinalVersionState(final,{draft=false,disabled=false,title=''}={}){
  const button=$('#stream-final-version');if(!button)return;
  button.dataset.finalVersion=String(final);delete button.dataset.finalPending;
  button.textContent=final?(draft?'Final version · draft':'Final version · set'):'Final version';
  button.title=title||(final?'Final version is set. Click to stage unfreezing, then Apply or Queue.':'Final version is not set. Click to stage approval, then Apply or Queue.');
  button.classList.toggle('active',final);button.setAttribute('aria-pressed',String(final));button.disabled=disabled;
}
async function refreshStreamFinalVersion(path){
  const button=$('#stream-final-version');if(!button)return;button.disabled=true;
  // Import editing targets a copy, not the source's catalog approval. A new
  // source may not be indexed at all; do not lock its copy-edit controls.
  if(movieImportMode?.editing){
    setStreamFinalVersionState(false,{disabled:true,title:'Final Version can be set after the imported media is in the catalog.'});
    setStreamFinalLock(false);return;
  }
  try{
    const result=await api('/api/v86/final-version?path='+encodeURIComponent(path));
    if(state.selectedPath!==path||!$('#stream-dialog')?.open)return;
    const final=Boolean(result.effective_final_version??result.final_version);
    setStreamFinalVersionState(final);setStreamFinalLock(final);
  }catch(error){
    if(state.selectedPath!==path||!$('#stream-dialog')?.open)return;
    delete button.dataset.finalVersion;delete button.dataset.finalPending;
    button.textContent='Final version · unknown';button.title='Could not read approval status: '+error.message;
    button.classList.remove('active');button.setAttribute('aria-pressed','mixed');button.disabled=true;setStreamFinalLock(true);
    const message=$('#stream-content .final-version-lock-message');
    if(message)message.textContent='Approval status is unavailable · editing locked. Reopen this media to retry.';
  }
}
function setStreamFinalLock(locked){const content=$('#stream-content'),submit=$('#stream-form [type=submit]');if(content)content.querySelectorAll('input,select,textarea,button').forEach(input=>{input.disabled=locked});if(submit)submit.disabled=locked;const message=content?.querySelector('.final-version-lock-message');if(locked&&!message&&content){content.insertAdjacentHTML('afterbegin','<p class="final-version-lock-message">Final version · view-only. Open the button above to unfreeze this media.</p>')}else if(!locked&&message)message.remove()}
function wireStreamFinalVersion(){
  const button=$('#stream-final-version');if(!button||button.dataset.ready)return;button.dataset.ready='1';
  button.onclick=()=>{
    const current=button.dataset.finalVersion==='true';
    const next=button.dataset.finalPending!==undefined?button.dataset.finalPending!=='true':!current;
    const draft=Boolean(window.activeTvDraftForPath?.(state.selectedPath));
    if(next===current){
      setStreamFinalVersionState(current,{draft});setStreamFinalLock(current&&!draft);
      updateQueuedChangeLabels();toast('Pending Final Version change undone');return;
    }
    button.dataset.finalPending=String(next);button.textContent=next?'Final version (pending)':'Unfreeze final (pending)';
    button.title=next?`Final Version will be set when you ${draft?'stage this episode':'Apply or Queue changes'}`:`Final Version will be removed when you ${draft?'stage this episode':'Apply or Queue changes'}`;
    button.classList.toggle('active',next);button.setAttribute('aria-pressed',String(next));
    if(!next)setStreamFinalLock(false);updateQueuedChangeLabels();
    toast(next?`Final Version staged; ${draft?'save the episode to the TV-show draft':'choose Apply or Queue to commit'}`:`Final Version removal staged; ${draft?'save the episode to the TV-show draft':'choose Apply or Queue to commit'}`);
  };
}
function updateMediaNavigation(){ensureMediaNavigation();wireStreamFinalVersion();const active=mediaNavigationIndex>=0;$('#media-navigation').classList.toggle('hidden',!active);$('#movie-note-button').classList.toggle('hidden',mediaNavigationKind!=='movie');const previous=$('#previous-media'),next=$('#next-media');previous.disabled=!active||mediaNavigationIndex===0;next.disabled=!active||mediaNavigationIndex===mediaNavigation.length-1;previous.title=`Previous ${mediaNavigationKind}`;next.title=`Next ${mediaNavigationKind}`;previous.setAttribute('aria-label',previous.title);next.setAttribute('aria-label',next.title)}
window.captureMediaNavigationContext=()=>({items:mediaNavigation.map(item=>({...item})),index:mediaNavigationIndex,kind:mediaNavigationKind});
window.setMediaNavigationContext=(items,index=0,kind='report')=>{mediaNavigation=items.map(item=>({...item}));mediaNavigationIndex=Math.max(0,Math.min(index,mediaNavigation.length-1));mediaNavigationKind=kind;currentShowContext=mediaNavigation[mediaNavigationIndex]?.showTitle||'';updateMediaNavigation()};
window.restoreMediaNavigationContext=context=>{if(!context)return;mediaNavigation=context.items||[];mediaNavigationIndex=context.index??-1;mediaNavigationKind=context.kind||'item';currentShowContext=mediaNavigation[mediaNavigationIndex]?.showTitle||'';updateMediaNavigation()};
async function navigateMedia(offset){const next=mediaNavigationIndex+offset;if(next<0||next>=mediaNavigation.length)return;await flushDeferredStreamDetection();mediaNavigationIndex=next;const item=mediaNavigation[next];openEditor(item.path,item.label)}
wireEditors=function(){document.querySelectorAll('.edit-file').forEach(button=>button.onclick=()=>{const container=button.closest('#episode-list')?'#episode-list':'#movie-list';mediaNavigationKind=container==='#episode-list'?'episode':'movie';mediaNavigation=[...document.querySelectorAll(`${container} .edit-file`)].filter(item=>!item.closest('tr')?.classList.contains('season-stream-filtered-out')).map(item=>({path:item.dataset.path,label:item.dataset.label}));mediaNavigationIndex=mediaNavigation.findIndex(item=>item.path===button.dataset.path);openEditor(button.dataset.path,button.dataset.label)})}
function savedOptions(field){return(v8Saved[field]||[]).map(value=>`<option value="${attr(value)}"></option>`).join('')}
let streamEditorOpenToken=0;
function savedLists(){return`<div class="saved-values"><datalist id="saved-language">${savedOptions('language')}</datalist><datalist id="saved-region">${savedOptions('region')}</datalist><datalist id="saved-title-audio">${savedOptions('title_audio')}</datalist><datalist id="saved-title-subtitle">${savedOptions('title_subtitle')}</datalist></div>`}
openEditor=async function(path,label){
  const token=++streamEditorOpenToken;
  const dialog=$('#stream-dialog');if(!dialog.dataset.detectionFlushBound){dialog.dataset.detectionFlushBound='true';dialog.addEventListener('close',()=>{streamEditorOpenToken++;flushDeferredStreamDetection()})}
  state.selectedPath=path;$('#selected-file').textContent=label;updateMediaNavigation();$('#stream-content').innerHTML='<p class="no-streams">Inspecting file and external subtitles…</p>';if(!$('#stream-dialog').open)$('#stream-dialog').showModal();
  try{
    // Always read the file after a commit. The v19 asset rewrites this route
    // to the fast indexed endpoint; without an explicit refresh it can return
    // the pre-commit stream snapshot and make the editor ask for the same
    // change again. The endpoint still uses its bounded probe, so this only
    // invalidates the stale metadata decision for this media.
    const refresh=window.streamEditorRefreshPaths?.has(path)?`&refresh=${Date.now()}`:'';
    const[d,saved]=await Promise.all([api(`/api/media/details?path=${encodeURIComponent(path)}${refresh}`),api('/api/v8/saved-values')]);if(token!==streamEditorOpenToken)return;window.streamEditorRefreshPaths?.delete(path);v8Saved=saved;window.currentPortugueseDetection=d.portuguese_detection||[];const rows=[...d.streams,...d.external_subtitles];
    if(token!==streamEditorOpenToken)return;
    if(!rows.length){$('#stream-content').innerHTML='<p class="no-streams">No audio, subtitle, or matching external subtitle streams.</p>';await refreshStreamFinalVersion(path);return}
    $('#stream-content').innerHTML='<div class="stream-view-modes" data-stream-view-modes role="toolbar" aria-label="Stream view"><span>Show:</span><button type="button" data-stream-view="all" class="active" aria-pressed="true">All</button><button type="button" data-stream-view="audio" aria-pressed="false">Audio only</button><button type="button" data-stream-view="subtitle" aria-pressed="false">Subtitles only</button></div><p class="stream-note stream-order-note"><span>Drag rows to reorder audio or subtitle tracks.</span><span class="clearable-tag-note">Click a selected Default or Forced tag again to clear it.</span></p><div class="stream-grid v7 head"><span></span><span>Stream</span><span>Language</span><span>Region</span><span title="Detect language"> </span><span>Track name</span><span>Default</span><span>Forced</span><span>Integrate</span><span>Remove</span></div>'+rows.map((s,i)=>v8Row(s,s.external?d.external_subtitles.indexOf(s):i)).join('')+savedLists();
    window.updateForcedEvaluateAvailability?.();
    $('#stream-content').querySelectorAll('input[type=text]').forEach(i=>i.oninput=()=>i.dataset.dirty='true');wireDragging();wireRemoval();wireClearableTags();wireStreamViewModes();
    // Apply the server-side Final Version state after the editor DOM exists.
    if(token!==streamEditorOpenToken)return;
    await refreshStreamFinalVersion(path);
    if(token!==streamEditorOpenToken)return;
  }catch(e){if(token!==streamEditorOpenToken)return;$('#stream-content').innerHTML=`<p class="no-streams error">${esc(e.message)}</p>`}
};
function wireStreamViewModes(){
  const controls=document.querySelector('[data-stream-view-modes]');
  if(!controls)return;
  const rows=[...document.querySelectorAll('#stream-content .stream-row')];
  controls.querySelectorAll('button[data-stream-view]').forEach(button=>button.onclick=()=>{
    const mode=button.dataset.streamView;
    controls.querySelectorAll('button[data-stream-view]').forEach(item=>{const active=item===button;item.classList.toggle('active',active);item.setAttribute('aria-pressed',active?'true':'false')});
    rows.forEach(row=>{row.hidden=mode!=='all'&&row.dataset.codecType!==mode});
  });
}
function v8Key(s,i){return s.external?`external:${s.path}`:`embedded:${s.codec_type}:${s.type_index}`}
function externalBadges(s){const detected=(s.filename_tags||[]).map(value=>`<span class="external-badge external-filename-tag" title="Detected from subtitle filename">${esc(value)}</span>`).join('');return`<span class="external-tags"><span class="external-badge" title="${attr(s.name||'External subtitle')}">External</span>${detected}</span>`}
function v8DetectionFor(s){if(s.codec_type!=='subtitle')return null;const list=window.currentPortugueseDetection||[];return list.find(item=>s.external?item.source==='external'&&item.external_path===s.path:item.source==='embedded'&&Number(item.type_index)===Number(s.type_index))}
function streamDetectionKey(language,region){let lang=String(language||'').trim().toLowerCase().replace('_','-'),area=String(region||'').trim().toUpperCase();if(lang.includes('-')){const parts=lang.split('-');lang=parts[0];if(!area&&parts[1])area=parts[1].toUpperCase()}const aliases={eng:'en',por:'pt',pob:'pt',ita:'it',spa:'es',fre:'fr',fra:'fr',ger:'de',deu:'de',jpn:'ja',kor:'ko',zho:'zh',chi:'zh',rus:'ru'};lang=aliases[lang]||lang;return `${lang}|${area}`}
function streamDetectionMatches(detected,language,region){const value=streamDetectionKey(detected,'');const metadata=streamDetectionKey(language,region);if(!detected||!language||['und','unknown'].includes(String(language).toLowerCase()))return false;return value===metadata||(value.split('|')[0]===metadata.split('|')[0]&&(!value.split('|')[1]||!metadata.split('|')[1]))}
function streamDetectionColor(detected,language,region,confidence){const value=Math.max(60,Math.min(100,Number(confidence||0)*100));return streamDetectionMatches(detected,language,region)?'#62d38a':`hsl(${Math.round(48-(value-60)*0.9)} 90% 55%)`}
function v8ConfidenceDot(s){const item=v8DetectionFor(s);if(!item)return '';if(s.codec_type==='subtitle'&&(['no_confidence','unreadable'].includes(item.analysis_status)||(!item.detected_language&&Number(item.confidence||0)<.6)))return '<span class="language-detection-no-confidence" title="'+attr(item.analysis_reason||'Insufficient readable subtitle evidence')+'">×</span>';if(streamDetectionMatches(item.detected_language,s.language||item.metadata_language,s.region||item.metadata_region))return '';const pct=(Number(item.confidence)*100).toFixed(1),color=streamDetectionColor(item.detected_language,s.language||item.metadata_language,s.region||item.metadata_region,item.confidence);return '<span class="language-detection-dot" style="--detection-color:'+color+'" title="Detected '+attr(item.detected_language)+' with '+pct+'% confidence'+(item.checked_at?' · Checked '+attr(formatAppDate(item.checked_at)):'')+(item.evidence?' · '+attr(item.evidence):'')+'"></span>'}
function v8Row(s,i){const key=v8Key(s,i);return`<div class="stream-grid v7 stream-row ${s.external?'external-row':''}" draggable="true" data-key="${attr(key)}" data-external="${s.external}" data-path="${attr(s.path||'')}" data-codec-type="${s.codec_type}" data-type-index="${s.type_index??-1}"><span class="drag-handle" title="Drag to reorder">⠿</span><div class="stream-kind"><strong><button type="button" class="stream-review-name" data-review-stream="${key}" title="Review this stream">${s.external?'External subtitle':`${esc(s.codec_type)} ${(s.type_index??0)+1}`}</button>${v8ConfidenceDot(s)}</strong><small>${esc(s.codec||'unknown')}</small>${s.external?externalBadges(s):''}</div><input type="text" name="language" list="saved-language" value="${attr(s.language||'')}" placeholder="eng"><input type="text" name="region" list="saved-region" value="${attr(s.region||'')}" placeholder="US"><button type="button" class="stream-language-detect-dot" data-stream-detect="1" data-path="${attr(state.selectedPath||'')}" data-codec-type="${attr(s.codec_type)}" data-type-index="${attr(s.type_index??0)}" data-external="${s.external?'true':'false'}" data-metadata-language="${attr(s.language||'')}" data-metadata-region="${attr(s.region||'')}" title="Click to detect this stream’s language now. This checks the selected subtitle/audio stream using the configured common-language detector and does not change its metadata." aria-label="Detect stream language"></button><input type="text" name="title" data-saved-field="${s.codec_type==='audio'?'title_audio':'title_subtitle'}" list="saved-title-${s.codec_type}" value="${attr(s.title||'')}" placeholder="Track name"><label class="tag-cell" title="Default"><input type="radio" aria-label="Default" name="default-${s.codec_type}" value="${attr(key)}" ${s.default?'checked':''}></label><label class="tag-cell" title="Forced"><input type="radio" aria-label="Forced" name="forced-${s.codec_type}" value="${attr(key)}" ${s.forced?'checked':''}></label><label class="move-cell" title="Integrate into video">${s.external?`<input type="checkbox" name="embed" aria-label="Integrate into video">`:'—'}</label><label class="remove-cell" title="Remove stream"><input type="checkbox" name="remove" aria-label="Remove stream"></label></div>`}
function wireClearableTags(){document.querySelectorAll('.tag-cell input[type=radio]').forEach(radio=>{radio.onpointerdown=()=>radio.dataset.wasChecked=radio.checked?'true':'false';radio.onclick=()=>{if(radio.dataset.wasChecked==='true'){radio.checked=false;radio.dataset.wasChecked='false'}}})}
function wireRemoval(){document.querySelectorAll('.stream-row [name=remove]').forEach(box=>box.onchange=()=>{const row=box.closest('.stream-row'),disabled=box.checked;row.classList.toggle('marked-remove',disabled);row.querySelectorAll('input:not([name=remove])').forEach(input=>{if(disabled&&input.type!=='text')input.checked=false;input.disabled=disabled})})}
function wireDragging(){let dragged=null;document.querySelectorAll('.stream-row').forEach(row=>{row.ondragstart=e=>{dragged=row;row.classList.add('dragging');e.dataTransfer.effectAllowed='move'};row.ondragend=()=>{row.classList.remove('dragging');document.querySelectorAll('.drag-over').forEach(x=>x.classList.remove('drag-over'));dragged=null};row.ondragover=e=>{if(dragged&&dragged!==row&&dragged.dataset.codecType===row.dataset.codecType){e.preventDefault();row.classList.add('drag-over')}};row.ondragleave=()=>row.classList.remove('drag-over');row.ondrop=e=>{e.preventDefault();row.classList.remove('drag-over');if(!dragged||dragged.dataset.codecType!==row.dataset.codecType)return;const box=row.getBoundingClientRect();row.parentNode.insertBefore(dragged,e.clientY<box.top+box.height/2?row:row.nextSibling);if(dragged.dataset.external==='true'){const embed=dragged.querySelector('[name=embed]');embed.checked=true;embed.dispatchEvent(new Event('change',{bubbles:true}))}}})}
let savedValuePromptChain=Promise.resolve();
function offerSavedValues(usedValues){
  const values=usedValues.filter(item=>item.field==='title_audio'||item.field==='title_subtitle');
  if(!values.length)return Promise.resolve();
  const run=async()=>{
    const result=await api('/api/v8/value-uses',{method:'POST',body:JSON.stringify({values})});
    for(const item of result.prompts){
      const save=window.confirm(`Save ${item.field==='title_audio'?'audio':'subtitle'} track name “${item.value}” for future selection? Cancel keeps it in Setup → Learned suggestions → Declined track names and prevents future questions.`);
      await api('/api/v8/saved-values',{method:'POST',body:JSON.stringify({...item,save})});
    }
    v8Saved=await api('/api/v8/saved-values');
  };
  const pending=savedValuePromptChain.then(run,run);
  savedValuePromptChain=pending.catch(()=>{});
  return pending;
}
$('#stream-form').onsubmit=async function(e){e.preventDefault();const rows=[...document.querySelectorAll('.stream-row')],tracks=[],external=[],order=[],remove=[],usedValues=[];rows.forEach(r=>{if(r.querySelector('[name=remove]').checked)remove.push(r.dataset.key);const language=r.querySelector('[name=language]'),region=r.querySelector('[name=region]'),title=r.querySelector('[name=title]'),isExternal=r.dataset.external==='true',removed=remove.includes(r.dataset.key);if(isExternal){const embed=r.querySelector('[name=embed]').checked;external.push({path:r.dataset.path,embed,language:language.value,region:region.value,title:title.value,forced:false});order.push({source:'external',codec_type:'subtitle',path:r.dataset.path});if(embed&&!removed){for(const[field,input]of[['language',language],['region',region],[r.dataset.codecType==='audio'?'title_audio':'title_subtitle',title]])if(input.dataset.dirty==='true'&&input.value.trim())usedValues.push({field,value:input.value.trim()})}}else{const typeIndex=Number(r.dataset.typeIndex),update={codec_type:r.dataset.codecType,type_index:typeIndex};if(language.dataset.dirty==='true'||region.dataset.dirty==='true'){update.language=language.value;update.region=region.value}if(title.dataset.dirty==='true')update.title=title.value;tracks.push(update);order.push({source:'embedded',codec_type:r.dataset.codecType,type_index:typeIndex});if(!removed){for(const[field,input]of[['language',language],['region',region],[r.dataset.codecType==='audio'?'title_audio':'title_subtitle',title]])if(input.dataset.dirty==='true'&&input.value.trim())usedValues.push({field,value:input.value.trim()})}}});const selected=name=>document.querySelector(`[name=${name}]:checked`)?.value||null;const defaults={audio:selected('default-audio'),subtitle:selected('default-subtitle')},forced={audio:selected('forced-audio'),subtitle:selected('forced-subtitle')};for(const choice of[defaults.subtitle,forced.subtitle])if(choice?.startsWith('external:')){const item=external.find(x=>`external:${x.path}`===choice);if(item)item.embed=true}const button=e.target.querySelector('[type=submit]');button.disabled=true;button.textContent='Applying…';try{const result=await api('/api/v43/media/edit',{method:'POST',body:JSON.stringify({path:state.selectedPath,tracks,external_subtitles:external,order,default_audio:defaults.audio,forced_audio:forced.audio,default_subtitle:defaults.subtitle,forced_subtitle:forced.subtitle,remove,defer_language_detection:true})});await offerSavedValues(usedValues);state.deferredDetectionPath=state.selectedPath;toast(result.warnings.length?result.warnings.join(' '):'Stream order and properties updated',result.warnings.length>0);$('#stream-dialog').close()}catch(error){toast(error.message,true)}finally{button.disabled=false;button.textContent='Apply changes'}};
