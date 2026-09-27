let editorBaseline=null,editorObserver=null,editorCommitClean=false;
window.tvDraftStreamProjections=window.tvDraftStreamProjections||{};

function editorSnapshot(){
  const rows=[...document.querySelectorAll('#stream-content .stream-row')],values={},order={audio:[],subtitle:[]};
  for(const row of rows){
    const key=row.dataset.key;
    values[key]={
      language:row.querySelector('[name=language]').value,
      region:row.querySelector('[name=region]').value,
      title:row.querySelector('[name=title]').value,
      external:row.dataset.external==='true',
      embed:row.querySelector('[name=embed]')?.checked||false,
      removed:row.querySelector('[name=remove]')?.checked||false
    };
    order[row.dataset.codecType].push(key);
  }
  const selected=name=>document.querySelector(`#stream-content [name=${name}]:checked`)?.value||null;
  const finalButton=$('#stream-final-version');
  return{values,order,defaultAudio:selected('default-audio'),forcedAudio:selected('forced-audio'),defaultSubtitle:selected('default-subtitle'),forcedSubtitle:selected('forced-subtitle'),finalVersion:finalButton?.dataset.finalVersion==='true'};
}

function queuedChangeCount(){
  // The rendered baseline is authoritative. A sticky "clean" flag can hide
  // later removals, drag reorders or Final-version clicks after an apply.
  if(!editorBaseline)return 0;
  const current=editorSnapshot();let count=0;
  for(const[key,value]of Object.entries(current.values)){
    const initial=editorBaseline.values[key];if(!initial)continue;
    if(value.removed!==initial.removed)count++;
    if(value.removed)continue;
    if(value.external){if(value.embed!==initial.embed)count++;if(!value.embed)continue}
    for(const field of['language','region','title'])if(value[field]!==initial[field])count++;
  }
  for(const type of['audio','subtitle'])if(current.order[type].join('\n')!==editorBaseline.order[type].join('\n'))count++;
  for(const field of['defaultAudio','forcedAudio','defaultSubtitle','forcedSubtitle'])if(current[field]!==editorBaseline[field])count++;
  const finalButton=$('#stream-final-version'); if(finalButton?.dataset.finalPending!==undefined && (finalButton.dataset.finalPending==='true')!==Boolean(editorBaseline.finalVersion))count++;
  return count;
}

function updateQueuedChangeLabels(){
  const count=queuedChangeCount(),apply=$('#stream-form .dialog-actions [type=submit]'),close=$('#stream-form .dialog-actions [data-close-stream]');
  if(!apply||!close)return;
  apply.textContent=count?`Apply ${count} change${count===1?'':'s'}`:'Apply changes';
  apply.disabled=count===0;
  close.textContent='Close';
  window.updateStreamEditorContext?.(count);
}

function captureEditorBaseline(){
  const content=$('#stream-content');
  // Loading a fresh media snapshot must never inherit dirty markers from a
  // previous editor instance or a report-originated navigation.
  content.querySelectorAll('[data-dirty]').forEach(input=>{delete input.dataset.dirty});
  editorBaseline=editorSnapshot();
  editorCommitClean=false;
  const editorForm=content.closest('form'); if(editorForm){delete editorForm.dataset.queueCommitted;delete editorForm.dataset.commitClean;}
  if(!content.dataset.dirtyListenerBound){
    content.dataset.dirtyListenerBound='true';
    const markDirty=()=>{editorCommitClean=false;if(editorForm)delete editorForm.dataset.commitClean;updateQueuedChangeLabels()};
    content.addEventListener('input',markDirty);content.addEventListener('change',markDirty);content.addEventListener('click',updateQueuedChangeLabels);
  }
  if(editorObserver)editorObserver.disconnect();editorObserver=new MutationObserver(updateQueuedChangeLabels);editorObserver.observe(content,{childList:true});
  updateQueuedChangeLabels();
}

const sessionOpenEditor=openEditor;
async function applyTvDraftProjection(path){
  const session=window.activeTvDraftForPath?.(path);if(!session?.session_id)return;
  const form=document.querySelector('#stream-form');if(form)delete form.dataset.draftProjectionFailed;
  document.querySelector('#stream-content [data-draft-projection-error]')?.remove();
  try{
  const projection=await api('/api/v79/tv/edit-session/'+encodeURIComponent(session.session_id)+'/projection?path='+encodeURIComponent(path));
  if(state.selectedPath!==path||window.activeTvDraftForPath?.(path)?.session_id!==session.session_id)return;
  const rows=[...document.querySelectorAll('#stream-content .stream-row')];
  const setChoice=(name,value)=>{
    if(value===undefined||value==='__preserve__')return;
    const inputs=[...document.querySelectorAll(`#stream-content [name="${name}"]`)];
    if(value===null||value===''){inputs.forEach(input=>input.checked=false);return}
    inputs.forEach(input=>{input.checked=String(input.value)===String(value)});
  };
  const setBulkChoice=(name,action,candidates)=>{
    if(!action||action==='unchanged')return;
    // A bulk request may target both audio and subtitles.  The backend applies
    // default/forced independently per codec type, so never let the last
    // subtitle row accidentally become the audio selection (or vice versa).
    const requestedType=name.includes('audio')?'audio':'subtitle';
    const typed=candidates.filter(row=>String(row.dataset.codecType)===requestedType);
    if(action==='clear'){
      typed.forEach(row=>{const input=row.querySelector(`[name="${name}"]`);if(input)input.checked=false});
      return;
    }
    const chosen=typed[typed.length-1];
    if(chosen){const key=chosen.dataset.key;setChoice(name,key)}
  };
  const pair=(language,region)=>{let lang=String(language||'').trim().toLowerCase().replace('_','-'),area=String(region||'').trim().toUpperCase();if(lang==='portuguese'||lang==='português'||lang==='brazilian'||lang==='brasileiro')lang='pt';if(lang.includes('-')){const parts=lang.split('-');if(!area&&parts[1]?.length===2){area=parts[1].toUpperCase();lang=parts[0]}}if(area==='BRAZIL'||area==='BRAZILIAN')area='BR';if(area==='PORTUGAL')area='PT';return `${lang}|${area}`};
  const matches=(row,filters)=>{
    // Once a draft operation removes a stream it is no longer part of the
    // virtual stream set.  Later journal operations must not match it again.
    if(row.querySelector('[name="remove"]')?.checked)return false;
    if(filters.stream_type&&filters.stream_type!==String(row.dataset.codecType))return false;
    if(filters.stream_types&&!filters.stream_types.includes(String(row.dataset.codecType)))return false;
    const language=row.querySelector('[name=language]')?.value||'', region=row.querySelector('[name=region]')?.value||'', title=row.querySelector('[name=title]')?.value||'';
    if(filters.language_regions?.length&&!filters.language_regions.some(value=>pair(value.split('|')[0],value.split('|')[1])===pair(language,region)))return false;
    if(filters.language!==null&&filters.language!==undefined&&pair(language,region).split('|')[0]!==pair(filters.language,'').split('|')[0])return false;
    if(filters.languages&&!filters.languages.map(value=>pair(value,'').split('|')[0]).includes(pair(language,region).split('|')[0]))return false;
    if(filters.region!==null&&filters.region!==undefined&&pair(language,region).split('|')[1]!==pair('',filters.region).split('|')[1])return false;
    if(filters.track_name!==null&&filters.track_name!==undefined&&String(filters.track_name)!==title)return false;
    return true;
  };
  const localOperations=window.tvDraftStreamProjections[path];
  const operations=Array.isArray(projection.operations)?projection.operations:(localOperations||[]);
  // Older renderers used `embedded-<index>`/`external-<ordinal>` as the DOM
  // key while the journal uses the durable key
  // `embedded:<codec>:<index>`/`external:<path>`.  Accept both forms here so
  // a staged TV-show edit is visible regardless of which renderer supplied
  // the Stream Properties rows (and so a stale browser bundle cannot hide a
  // perfectly valid virtual change).
  const rowKey=row=>String(row.dataset.key||'');
  const rowMatchesTarget=(row,targetKeys)=>{
    if(!targetKeys?.size)return false;
    const key=rowKey(row), type=String(row.dataset.codecType||''), index=String(row.dataset.typeIndex??'');
    const durable=row.dataset.external==='true'?`external:${row.dataset.path||''}`:`embedded:${type}:${index}`;
    return targetKeys.has(key)||targetKeys.has(durable)||targetKeys.has(`embedded-${index}`);
  };
  for(const operation of operations){
    if(operation.note_edit||operation.audio_compatibility)continue;
    const direct=operation.direct_edit;
    if(direct){
      for(const change of direct.tracks||[])rows.filter(row=>String(row.dataset.codecType)===String(change.codec_type)&&Number(row.dataset.typeIndex)===Number(change.type_index)).forEach(row=>{if(change.language!==undefined)row.querySelector('[name=language]').value=change.language||'';if(change.region!==undefined)row.querySelector('[name=region]').value=change.region||'';if(change.title!==undefined)row.querySelector('[name=title]').value=change.title||''});
      for(const change of direct.external_subtitles||[])rows.filter(row=>row.dataset.external==='true'&&String(row.dataset.path)===String(change.path)).forEach(row=>{if(change.language!==undefined)row.querySelector('[name=language]').value=change.language||'';if(change.region!==undefined)row.querySelector('[name=region]').value=change.region||'';if(change.title!==undefined)row.querySelector('[name=title]').value=change.title||'';const embed=row.querySelector('[name=embed]');if(embed&&change.embed!==undefined)embed.checked=Boolean(change.embed)});
      const removedKeys=new Set((direct.remove||[]).map(String));
      rows.forEach(row=>{const remove=row.querySelector('[name=remove]');if(remove){const shouldRemove=[...removedKeys].some(key=>rowMatchesTarget(row,new Set([key])));if(remove.checked!==shouldRemove)remove.click()}});
      setChoice('default-audio',direct.default_audio);setChoice('forced-audio',direct.forced_audio);
      setChoice('default-subtitle',direct.default_subtitle);setChoice('forced-subtitle',direct.forced_subtitle);
      if(direct.final_version!==undefined&&direct.final_version!==null){
        const button=document.querySelector('#stream-final-version'),final=Boolean(direct.final_version);
        if(button){button.dataset.finalVersion=String(final);delete button.dataset.finalPending;button.textContent=final?'Unfreeze final':'Final version';button.classList.toggle('active',final)}
        if(typeof setStreamFinalLock==='function')setStreamFinalLock(final&&!window.activeTvDraftForPath?.(path));
      }
      if(Array.isArray(direct.order)&&direct.order.length){
        direct.order.map(item=>item.source==='external'?`external:${item.path}`:`embedded:${item.codec_type}:${item.type_index}`).forEach(key=>{const row=rows.find(candidate=>rowMatchesTarget(candidate,new Set([key])));if(row)row.parentNode.appendChild(row)});
      }
      continue;
    }
    const filters=operation.filters||{};
    // TV edit-mode bulk operations capture exact indexed stream keys.  The
    // live media can have a stale/different region representation, so the
    // projection must honor those keys instead of re-matching the old filter
    // against the current file (otherwise the staged change is invisible in
    // the episode stream-properties editor).
    const targetKeys=Array.isArray(operation.target_keys)?new Set(operation.target_keys.map(String)):null;
    let projectedRows=targetKeys?.size?rows.filter(row=>rowMatchesTarget(row,targetKeys)):rows.filter(row=>matches(row,filters));
    // Compatibility with journals created before exact target keys were
    // persisted, and with older rows whose region was normalized differently:
    // retain the stream type/language/title constraints but do not let a stale
    // region-only value hide the staged edit from Stream Properties.
    if(!projectedRows.length){
      const relaxed={...filters,language_regions:null,region:null};
      projectedRows=rows.filter(row=>matches(row,relaxed));
    }
    projectedRows.forEach(row=>{
      const remove=row.querySelector('[name=remove]');
      // Projection can be safely re-applied by late editor wrappers.  Do not
      // toggle an already projected removal back to its original state.
      if(operation.remove&&remove&&!remove.checked)remove.click();
      if(operation.language!=='')row.querySelector('[name=language]').value=operation.language||'';
      if(operation.region!=='')row.querySelector('[name=region]').value=operation.region||'';
      if(operation.changed_fields?.includes('track_name')||(operation.track_name!==''&&operation.track_name!==undefined))row.querySelector('[name=title]').value=operation.track_name||'';
    });
    for(const type of ['audio','subtitle']){
      setBulkChoice('default-'+type,operation.default_action,projectedRows);
      setBulkChoice('forced-'+type,operation.forced_action,projectedRows);
    }
  }
  const draftFinal=window.tvDraftFinalForPath?.(path);
  if(draftFinal!==undefined){
    const button=document.querySelector('#stream-final-version');
    if(button){button.dataset.finalVersion=String(draftFinal);delete button.dataset.finalPending;button.textContent=draftFinal?'Unfreeze final':'Final version';button.classList.toggle('active',draftFinal);button.disabled=window.tvDraftShowFinal?.()!==undefined;button.title=button.disabled?'The show-wide Final-version draft controls this episode':'Final-version change is staged in this TV-show draft'}
    if(typeof setStreamFinalLock==='function')setStreamFinalLock(false);
  }
  window.syncStreamLanguageRegionSelectors?.();
  }catch(error){
    if(form)form.dataset.draftProjectionFailed='true';
    document.querySelector('#stream-content')?.insertAdjacentHTML('afterbegin',`<p class="no-streams error" data-draft-projection-error>Could not load the TV-show draft: ${esc(error.message)}. Episode changes are disabled until it loads.</p>`);
    console.warn('TV edit projection unavailable',error);
  }
}
window.applyTvDraftProjection=applyTvDraftProjection;
window.activeTvDraftForPath=path=>{
  const session=window.tvShowEditSession,show=state.currentShow;
  if(!session?.session_id||session.status==='committed'||!show||String(session.show_id)!==String(show.id))return null;
  return show.seasons?.some(season=>season.episodes?.some(episode=>episode.path===path))?session:null;
};
openEditor=async function(path,label){await sessionOpenEditor(path,label);if(document.querySelector('#stream-content .stream-row')){await applyTvDraftProjection(path);captureEditorBaseline()}};
window.markEditorCommittedClean=()=>{
  // The media was reread after a successful commit. Replace the baseline with
  // that committed snapshot, rather than relying only on a boolean: late
  // openEditor wrappers or DOM mutations must not resurrect the old diff.
  if(document.querySelector('#stream-content .stream-row')) editorBaseline=editorSnapshot();
  editorCommitClean=true;
  const form=document.querySelector('#stream-form');if(form)form.dataset.commitClean='true';
  document.querySelectorAll('#stream-content [data-dirty]').forEach(input=>delete input.dataset.dirty);
  updateQueuedChangeLabels();
};

$('#stream-form').onsubmit=async function(e){
  e.preventDefault();
  if(e.target.dataset.draftProjectionFailed==='true'){toast('Could not load the TV-show draft; reopen this episode before editing',true);return}
  if(window.activeTvDraftForPath?.(state.selectedPath)?.status==='committing'){toast('This TV show is being saved; episode changes are temporarily locked',true);return}
  const queued=queuedChangeCount();if(!queued){toast('No changes queued');return}let applySucceeded=false,applyError='';applyProgressBusy=true;if(typeof beginGlobalBusy==='function')beginGlobalBusy('Applying stream changes');setApplyProgress(1,4,'Preparing changes',queuedChangeSummary())
  const rows=[...document.querySelectorAll('.stream-row')],tracks=[],external=[],order=[],remove=[],usedValues=[];
  rows.forEach(r=>{
    if(r.querySelector('[name=remove]').checked)remove.push(r.dataset.key);
    const language=r.querySelector('[name=language]'),region=r.querySelector('[name=region]'),title=r.querySelector('[name=title]'),isExternal=r.dataset.external==='true',removed=remove.includes(r.dataset.key);
    if(isExternal){
      const embed=r.querySelector('[name=embed]').checked;external.push({path:r.dataset.path,embed,language:language.value,region:region.value,title:title.value,forced:false});order.push({source:'external',codec_type:'subtitle',path:r.dataset.path});
      if(embed&&!removed)for(const[field,input]of[['language',language],['region',region],[r.dataset.codecType==='audio'?'title_audio':'title_subtitle',title]])if(input.dataset.dirty==='true'&&input.value.trim())usedValues.push({field,value:input.value.trim()});
    }else{
      const typeIndex=Number(r.dataset.typeIndex),update={codec_type:r.dataset.codecType,type_index:typeIndex,language:language.value,region:region.value,title:title.value};
      // Capture the complete current row. Selecting a datalist value can emit
      // only `change` in some browsers; relying on a dirty marker alone used
      // to journal an empty update and made the virtual language/region
      // appear to revert on the next open.
      tracks.push(update);order.push({source:'embedded',codec_type:r.dataset.codecType,type_index:typeIndex});
      if(!removed)for(const[field,input]of[['language',language],['region',region],[r.dataset.codecType==='audio'?'title_audio':'title_subtitle',title]])if(input.dataset.dirty==='true'&&input.value.trim())usedValues.push({field,value:input.value.trim()});
    }
  });
  const selected=name=>document.querySelector(`[name=${name}]:checked`)?.value||null,defaults={audio:selected('default-audio'),subtitle:selected('default-subtitle')},forced={audio:selected('forced-audio'),subtitle:selected('forced-subtitle')};
  for(const choice of[defaults.subtitle,forced.subtitle])if(choice?.startsWith('external:')){const item=external.find(x=>`external:${x.path}`===choice);if(item)item.embed=true}
  const button=e.target.querySelector("[type=submit]"),label=$("#selected-file").textContent,path=state.selectedPath;button.disabled=true;button.textContent="Applying…";
  let mediaCommitSucceeded=false;
  try{
    if(window.activeTvDraftForPath?.(path)?.session_id){
      const draftOperation={direct_edit:{path,tracks,external_subtitles:external,order,default_audio:defaults.audio,forced_audio:forced.audio,default_subtitle:defaults.subtitle,forced_subtitle:forced.subtitle,remove,final_version:document.querySelector('#stream-final-version')?.dataset.finalPending!==undefined?(document.querySelector('#stream-final-version').dataset.finalPending==='true'):null}};
      await api('/api/v79/tv/edit-session/'+encodeURIComponent(window.tvShowEditSession.session_id)+'/operation',{method:'POST',body:JSON.stringify({path,operation:draftOperation})});
      window.tvDraftStreamProjections[path]=[draftOperation];
      window.projectTvEpisodeEdit?.(path,{tracks,remove,external_subtitles:external,default_audio:defaults.audio,forced_audio:forced.audio,default_subtitle:defaults.subtitle,forced_subtitle:forced.subtitle});
      window.tvShowEditSession.dirty=true;window.markTvEditDirty?.();
      toast(`${queued} episode change${queued===1?'':'s'} staged; save the TV show when ready`);
      // Re-read through the normal projection path so the dialog itself is
      // authoritative too; this prevents a late editor refresh from showing
      // the committed Portuguese value over the virtual Portuguese-Brazil
      // draft.
      await openEditor(path,label);
      window.markEditorCommittedClean?.();
      applySucceeded=true;return;
    }
    const filename=validateStreamFilename(),renameQueued=filename!==streamFilenameOriginal,streamQueued=queued-(renameQueued?1:0);let result={warnings:[]},finalPath=path;
    if(streamQueued){setApplyProgress(2,4,"Updating media container",queuedChangeSummary());result=await api("/api/v7/media/edit",{method:"POST",body:JSON.stringify({path,tracks,external_subtitles:external,order,default_audio:defaults.audio,forced_audio:forced.audio,default_subtitle:defaults.subtitle,forced_subtitle:forced.subtitle,remove,final_version:document.querySelector('#stream-final-version')?.dataset.finalPending!==undefined?(document.querySelector('#stream-final-version').dataset.finalPending==='true'):null})});mediaCommitSucceeded=true;window.markEditorCommittedClean?.()};setApplyProgress(3,4,"Updating indexes","Queueing refreshed stream and media indexes");await api("/api/v80/index/request",{method:"POST",body:JSON.stringify({path,indexes:result.operation==="single_remux"?["core","subtitles"]:["core"],reason:"Clone last change completed"})})
    if(renameQueued){setApplyProgress(3,4,"Renaming media",filename);const renamed=await api("/api/v37/media/rename",{method:"POST",body:JSON.stringify({path,filename})});finalPath=renamed.path;adoptRenamedMediaPath(path,finalPath)}
    if(usedValues.length){setApplyProgress(3,4,"Saving reusable values","Recording successfully used metadata values");await offerSavedValues(usedValues)}
    setApplyProgress(4,4,"Refreshing properties","Reading updated streams from the media file");toast(result.warnings.length?result.warnings.join(" "):queued+" change"+(queued===1?"":"s")+" applied",result.warnings.length>0);const indexes=result.operation==='single_remux'?['core','subtitles']:result.subtitle_html_cleaned?['core','subtitles']:['core'];
    // A successful apply must render the authoritative stream layout again.
    // This is essential when an external subtitle becomes embedded (or a
    // stream is removed/reordered): keeping the old DOM would show a stale
    // external row even though the transaction succeeded. The v19 details
    // route is called with refresh by openEditor, so this read is from the
    // committed media rather than the old index snapshot.
    (window.streamEditorRefreshPaths??=new Set()).add(finalPath);
    await openEditor(finalPath,$('#selected-file').textContent);
    editorBaseline=null;
    editorCommitClean=true;
    if(typeof window.clearPendingHtmlCleanups==='function')window.clearPendingHtmlCleanups();
    pendingLastChange=null;
    document.dispatchEvent(new CustomEvent("media-properties-applied",{detail:{path:finalPath,indexes}}));
    window.markEditorCommittedClean?.();
    applySucceeded=true;
  }catch(error){applyError=mediaCommitSucceeded?`Media changes were saved, but follow-up processing failed: ${error.message}. Close and reopen to verify; do not repeat the same edit.`:error.message;pendingLastChange=null;if(mediaCommitSucceeded)window.markEditorCommittedClean?.();toast(applyError,true)}finally{if(document.querySelector("#stream-content .stream-row"))updateQueuedChangeLabels();else{button.disabled=false;button.textContent="Apply changes";$("#stream-form .dialog-actions [data-close-stream]").textContent="Close"}applyProgressBusy=false;if(typeof endGlobalBusy==='function')endGlobalBusy();setApplyProgress(4,4,applySucceeded?"Complete":"Could not complete",applySucceeded?queued+" change"+(queued===1?"":"s")+" applied":applyError)}
};
