(function(){
  async function waitForBulkPreflight(requestId){
    if(!requestId)return {succeeded:0,failed:0,items:[]};
    for(let attempt=0;attempt<600;attempt++){
      const status=await api('/api/v89/preflight/'+encodeURIComponent(requestId));
      if(status.status==='pending'||status.status==='running'){await new Promise(resolve=>setTimeout(resolve,500));continue}
      if(status.status==='skipped')return {succeeded:0,failed:0,items:[]};
      if(status.status!=='approved')throw new Error(status.error||status.result?.reason||'Bulk validation did not approve the request');
      const ids=(status.result?.execution?.task_ids||[]).map(Number).filter(Boolean);
      if(ids.length&&typeof waitForGlobalTasks==='function')return await waitForGlobalTasks(ids);
      return {succeeded:Number(status.result?.approved||0),failed:0,items:[]};
    }
    throw new Error('Bulk validation is taking too long; the request remains queued');
  }
  const table=$('#movie-list')?.closest('table'),head=table?.querySelector('thead tr');
  if(!head)return;
  const action=head.lastElementChild;
  action.textContent='Actions';
  const movieTools=document.querySelector('#movies .movie-content-header');
  movieTools?.insertAdjacentHTML('afterbegin','<button type="button" id="movie-header-stream-toggle" class="movie-header-stream-toggle hidden" title="Show stream-value filters" aria-label="Show stream-value filters">&gt;&gt;</button>');
  head.insertAdjacentHTML('afterend','<tr id="movie-header-stream-filter-row" class="movie-header-stream-filter-row hidden"><th colspan="4"><div id="movie-header-stream-filter-content"></div></th></tr><tr id="movie-header-stream-edit-row" class="movie-header-stream-edit-row hidden"><th colspan="4"><div id="movie-header-stream-edit-content"></div></th></tr>');
  const toggle=$('#movie-header-stream-toggle'),filterRow=$('#movie-header-stream-filter-row'),filterContent=$('#movie-header-stream-filter-content'),editRow=$('#movie-header-stream-edit-row'),editContent=$('#movie-header-stream-edit-content');
  let records=[],loading=false,refreshTimer=null;

  // The status/title scope must not include this panel's own stream filter.
  // Otherwise "Does not have" is calculated against an already narrowed list.
  function scopeMovies(){return typeof movieBaseFilteredMovies==='function'?movieBaseFilteredMovies():state.movies}
  function scopePaths(){return new Set(scopeMovies().map(item=>item.path))}
  function option(value){return value===''?'<option value="__empty__">&lt;empty&gt;</option>':`<option value="${attr(value)}">${esc(value)}</option>`}
  function unique(field){
    const filters=selectedFilters(),filterField={stream_type:'stream_type',language:'language',region:'region',track_name:'track_name',filename_tag:'filename_tags'},selectedStream=filterContent.querySelector('[data-season-field=stream]')?.value||'__all__';
    const inScope=scopePaths();
    const filtered=records.filter(item=>{if(!inScope.has(item.path))return false;if(field!=='stream_type'&&selectedStream!=='__all__'&&String(item.stream_type).toLowerCase()!==String(selectedStream).toLowerCase())return false;if(field!=='stream_type'&&filters.stream_type!==null&&String(item.stream_type).toLowerCase()!==String(filters.stream_type).toLowerCase())return false;return Object.entries(filters).every(([name,value])=>{
      if(name==='presence'||value===null||filterField[name]===field)return true;
      if(name==='language_regions')return field==='language'||value.includes(`${item.language||''}|${item.region||''}`);
      return name==='filename_tag'?(item.filename_tags||[]).includes(value):item[filterField[name]]===value;
    });});
    const values=field==='filename_tags'?filtered.flatMap(item=>item.filename_tags||[]):filtered.map(item=>item[field]);
    return[...new Set(values)].sort((a,b)=>a.localeCompare(b));
  }
  function fill(select,field,label){const old=select.value,items=unique(field);select.innerHTML=`<option value="__all__">${esc(label)}</option>`+items.map(option).join('');if([...select.options].some(item=>item.value===old))select.value=old}
  function selectedFilters(){const value=name=>filterContent.querySelector(`[data-season-field=${name}]`)?.value||'__all__',decoded=name=>value(name)==='__all__'?null:value(name)==='__empty__'?'':value(name),picker=filterContent.querySelector('.language-region-select'),languageRegions=picker?[...picker.selectedOptions].map(item=>item.value).filter(item=>!item.startsWith('__')):[];return{presence:filterContent.querySelector('[data-season-field=presence]')?.checked?'not_have':'have',stream_type:decoded('stream'),language:decoded('language'),region:decoded('region'),language_regions:languageRegions.length?languageRegions:null,track_name:decoded('track_name'),filename_tag:decoded('filename_tag')}}
  function matchingStreamRecords(){const filters=selectedFilters(),inScope=scopePaths(),streamType=filters.stream_type===null?null:String(filters.stream_type).toLowerCase();return records.filter(item=>inScope.has(item.path)&&(streamType===null||String(item.stream_type).toLowerCase()===streamType)&&(!filters.language_regions||filters.language_regions.includes(`${item.language||''}|${item.region||''}`))&&(filters.track_name===null||item.track_name===filters.track_name)&&(filters.filename_tag===null||(item.filename_tags||[]).includes(filters.filename_tag)))}
  function matchingPaths(){const filters=selectedFilters(),inScope=scopePaths(),matches=new Set(matchingStreamRecords().map(item=>item.path));if(filters.presence==='not_have')return new Set([...inScope].filter(path=>!matches.has(path)));return matches}
  function editableMatchingPaths(){const matches=matchingPaths(),finalPaths=new Set(state.movies.filter(item=>item.final_version).map(item=>item.path));return [...matches].filter(path=>!finalPaths.has(path))}
  function applyFilter(){const filters=selectedFilters(),active=Object.entries(filters).some(([name,value])=>name!=='presence'&&value!==null),matches=matchingPaths(),count=active?matches.size:scopeMovies().length,editable=editableMatchingPaths().length;window.movieHeaderAllowedPaths=active?matches:null;oldRender();const summary=filterContent.querySelector("[data-filter-summary]");if(summary)summary.textContent=`${count} matching · ${editable} editable`;const edit=filterContent.querySelector("[data-season-edit-toggle]");if(edit){edit.disabled=filters.presence==='not_have'||!filters.stream_type||!editable;edit.title=filters.presence==='not_have'?'Bulk editing requires Have':(!filters.stream_type?"Select Audio, Subtitles, or External before editing":!editable?'All matching movies are Final version':'Edit matching streams')}if(!count)closeEditor()}
  function filterChanged(){closeEditor();applyFilter()}
  function renderFilters(){
    filterContent.innerHTML='<div class="movie-header-stream-filters"><label class="filter-not-have" title="Show movies without the selected kind of stream"><span>Without</span><input type="checkbox" data-season-field="presence" aria-label="Show movies without matching streams"></label><label>Stream<select data-season-field="stream"></select></label><label>Language<select data-season-field="language"></select></label><label>Region<select data-season-field="region"></select></label><label>Track name<select data-season-field="track_name"></select></label><label class="filename-tag-filter hidden">Filename tag<select data-season-field="filename_tag"></select></label><span data-filter-summary></span><button type="button" data-season-edit-toggle class="movie-header-stream-toggle" aria-label="Edit matching streams">&gt;&gt;</button><button type="button" class="movie-header-stream-filter-close" title="Hide and clear stream filters" aria-label="Hide and clear stream filters">&lt;&lt;</button></div>';
    const stream=filterContent.querySelector('[data-season-field=stream]'),language=filterContent.querySelector('[data-season-field=language]'),region=filterContent.querySelector('[data-season-field=region]'),name=filterContent.querySelector('[data-season-field=track_name]'),filenameTag=filterContent.querySelector('[data-season-field=filename_tag]');
    const refreshOptions=(changed='')=>{const order=['stream','language','region','track_name','filename_tag'],selectedCount=[stream,language,region,name,filenameTag].filter(select=>select.value&&select.value!=='__all__').length,start=!changed||selectedCount<=1?0:Math.max(0,order.indexOf(changed));if(!changed||start===0)fill(stream,'stream_type','Audio or subtitles');const external=stream.value==='external';filenameTag.closest('label').classList.toggle('hidden',!external);if(!external)filenameTag.value='__all__';if(!changed||start<=1)fill(language,'language','All languages');if(!changed||start<=2)fill(region,'region','All regions');if(!changed||start<=3)fill(name,'track_name','All track names');if(external&&(!changed||start<=4))fill(filenameTag,'filename_tags','All filename tags');filterChanged()};
    for(const select of[stream,language,region,name,filenameTag])select.onchange=()=>refreshOptions(select.dataset.seasonField);filterContent.querySelector("[data-season-field=presence]").onchange=filterChanged;filterContent.querySelector('.movie-header-stream-filter-close').onclick=collapse;filterContent.querySelector('[data-season-edit-toggle]').onclick=openEditor;refreshOptions();
  }
  async function expand(){
    if(!state.movies.length||loading)return;loading=true;filterRow.classList.remove('hidden');filterContent.innerHTML='<div class="movie-header-filter-loading">Loading indexed stream values…</div>';toggle.disabled=true;
    try{const result=await api('/api/v82/movies/stream-values');window.currentMovieStreamRecords=records=result.values;renderFilters();if(result.errors.length)toast(`${result.errors.length} movies could not be indexed`,true)}catch(error){filterContent.innerHTML=`<div class="movie-header-filter-loading error">${esc(error.message)}</div>`}finally{loading=false;toggle.disabled=false}
  }
  async function openEditor(){
    const paths=editableMatchingPaths(),streamType=selectedFilters().stream_type;if(!streamType||!paths.length)return;
    if(!(v8Saved.language?.length||v8Saved.region?.length||v8Saved.title_audio?.length||v8Saved.title_subtitle?.length)){try{v8Saved=await api('/api/v8/saved-values')}catch(_){}}
    editRow.classList.remove('hidden');
    const externalActions=(streamType==='external'?'<label class="movie-header-bulk-action"><input name="integrate" type="checkbox"> Integrate</label>':'')+'<label class="movie-header-bulk-action tri-action"><span>Remove</span><input name="remove" type="checkbox"></label>';
    editContent.innerHTML=`<div class="movie-header-stream-editor ${streamType==='external'?'with-external-actions':''}"><strong>change:</strong><span class="bulk-count">${paths.length} movie${paths.length===1?'':'s'}</span><label>Language<input class="movie-header-bulk-value" data-saved-field="language" name="language" type="text" placeholder="Leave unchanged"></label><label>Region<input class="movie-header-bulk-value" data-saved-field="region" name="region" type="text" placeholder="Leave unchanged"></label><label>Track name<input class="movie-header-bulk-value" data-saved-field="${streamType==='audio'?'title_audio':'title_subtitle'}" name="track_name" type="text" placeholder="Leave unchanged"></label></div><div class="bulk-actions">${externalActions}<label class="movie-header-bulk-action tri-action"><span>Default <em data-tri-state>—</em></span><input name="default" type="checkbox" data-action="default" aria-label="Default"></label><label class="movie-header-bulk-action tri-action"><span>Forced <em data-tri-state>—</em></span><input name="forced" type="checkbox" data-action="forced" aria-label="Forced"></label><button type="button" data-movie-header-bulk-apply disabled>Apply</button><button type="button" class="movie-header-stream-filter-close" data-season-edit-close aria-label="Close editor">&lt;&lt;</button></div></div>`;
    const apply=editContent.querySelector('[data-movie-header-bulk-apply]');apply.hidden=false;apply.style.visibility="hidden";const integrate=editContent.querySelector('[name=integrate]'),remove=editContent.querySelector('[name=remove]');
    editContent.querySelectorAll('input[type=text]').forEach(input=>input.oninput=()=>{apply.hidden=false;apply.style.visibility="visible";input.dataset.dirty='true';if(remove){remove.checked=false;delete remove.dataset.dirty}if(integrate){integrate.checked=true;integrate.dataset.dirty='true'}apply.disabled=false});
for(const tri of editContent.querySelectorAll('[data-action]')){tri.dataset.state='unchanged';tri.dataset.symbol='—';tri.onclick=event=>{const next=tri.dataset.state==='unchanged'?'set':tri.dataset.state==='set'?'clear':'unchanged';tri.dataset.state=next;tri.checked=next==='set';tri.indeterminate=next==='clear';tri.parentElement.querySelector('[data-tri-state]').textContent=next==='set'?'✓':next==='clear'?'×':'—';tri.dataset.symbol=next==='set'?'✓':next==='clear'?'×':'—';tri.dataset.dirty=next==='unchanged'?'':'true';apply.hidden=false;apply.style.visibility="visible";tri.title=next==='set'?'Set flag':next==='clear'?'Clear flag on matching streams':'Leave unchanged';const dirty=Boolean(editContent.querySelector('[data-dirty=true]'));apply.disabled=!dirty;apply.style.visibility=dirty?'visible':'hidden'}};for(const action of[integrate,remove].filter(Boolean))action.onchange=()=>{if(action.checked&&action===remove&&!confirm(`Are you sure you want to remove every stream matching this filter from ${paths.length} media item${paths.length===1?"":"s"}?`)){action.checked=false;return}if(action.checked){action.dataset.dirty="true";const other=action===integrate?remove:integrate;if(other){other.checked=false;delete other.dataset.dirty}if(action===remove)editContent.querySelectorAll("input[type=text]").forEach(input=>{input.value="";delete input.dataset.dirty})}else delete action.dataset.dirty;const dirty=Boolean(editContent.querySelector("[data-dirty=true]"));apply.hidden=!dirty;apply.style.visibility=dirty?'visible':'hidden';apply.disabled=!dirty};
    installSavedValuePopups(editContent);editContent.querySelector('[data-season-edit-close]').onclick=closeEditor;editContent.querySelector('[data-movie-header-bulk-apply]').onclick=chooseApplyMode;
  }
  function closeEditor(){closeSavedValueMenu();editRow.classList.add('hidden');editContent.innerHTML=''}
  function requestMode(count,streamCount,filters,changes,skippedFinal){
    let dialog=$('#movie-header-stream-apply-dialog');
    if(!dialog){document.body.insertAdjacentHTML('beforeend','<dialog id="movie-header-stream-apply-dialog"><div class="dialog-title"><div><h2>Apply filtered  movie changes</h2><p data-movie-mode-summary></p></div><button type="button" class="icon-close" data-movie-mode="cancel" aria-label="Cancel">×</button></div><div class="dialog-actions"><button type="button" data-movie-mode="cancel">Cancel</button><button type="button" data-movie-mode="queue">Add changes to queue</button><button type="button" class="primary" data-movie-mode="now">Apply changes now</button></div></dialog>');dialog=$('#movie-header-stream-apply-dialog')}
    const languageRegion=filters.language===null?'any language':(filters.language||'<empty>')+(filters.region===null?'':('-'+(filters.region||'<empty>')));dialog.querySelector('[data-movie-mode-summary]').textContent=`${count} editable movie${count===1?'':'s'} · ${streamCount} matching stream${streamCount===1?'':'s'}${skippedFinal?` · ${skippedFinal} Final version skipped`:''}. Change: ${changes.join(', ')}. Filter: ${filters.stream_type||'any stream'} · ${languageRegion} · ${filters.track_name===null?'any track name':filters.track_name||'<empty>'}. Current streams will be checked again before execution.`;
    return new Promise(resolve=>{let done=false;const finish=value=>{if(done)return;done=true;dialog.close();dialog.removeEventListener('cancel',cancel);resolve(value)};const cancel=event=>{event.preventDefault();finish(null)};dialog.querySelectorAll('[data-movie-mode]').forEach(button=>button.onclick=()=>finish(button.dataset.movieMode==='cancel'?null:button.dataset.movieMode));dialog.addEventListener('cancel',cancel);dialog.showModal();dialog.querySelector('[data-movie-mode=now]').focus({preventScroll:true})})
  }
  async function chooseApplyMode(){
    const paths=editableMatchingPaths(),filters=selectedFilters(),inputs=[...editContent.querySelectorAll('input')],changed=inputs.filter(input=>input.dataset.dirty==='true');if(!changed.length||!paths.length)return;
    const skippedFinal=matchingPaths().size-paths.length,editable=new Set(paths),streamCount=matchingStreamRecords().filter(item=>editable.has(item.path)).length;
    const mode=await requestMode(paths.length,streamCount,filters,changed.map(input=>input.name),skippedFinal);if(!mode)return;
    const textInputs=inputs.filter(input=>input.type==='text'),values=Object.fromEntries(textInputs.map(input=>[input.name,input.value])),default_action=editContent.querySelector('[data-action=default]')?.dataset.state||'unchanged',forced_action=editContent.querySelector('[data-action=forced]')?.dataset.state||'unchanged',integrate=Boolean(editContent.querySelector('[name=integrate]')?.checked),remove=Boolean(editContent.querySelector('[name=remove]')?.checked);
    if(mode==='now'){window.beginGlobalBusyImmediate?.('Applying movie changes');window.setGlobalBusyProgress?.(0,2,'Validating movies','Checking selected media and stream signatures')}
    try{let result=await api('/api/v82/movies/stream-bulk-edit',{method:'POST',body:JSON.stringify({paths,filters,changed_fields:changed.map(input=>input.name),...values,default_action,forced_action,integrate,remove,mode})});if(mode==='now'&&result.preflight_id){window.setGlobalBusyProgress?.(1,2,'Applying movie changes','Validation complete; processing approved media');const progress=await waitForBulkPreflight(result.preflight_id);result={...result,applied:progress.succeeded,failed:Array.from({length:progress.failed||0},()=>({}))};}const used=[];for(const input of changed.filter(input=>input.type==='text')){const field=input.name==='track_name'?(filters.stream_type==='audio'?'title_audio':'title_subtitle'):input.name;if(input.value.trim())used.push({field,value:input.value.trim()})}if(used.length&&typeof offerSavedValues==='function')await offerSavedValues(used);toast(mode==='queue'?`Validation requested for ${paths.length} movies; unchanged media will be skipped`:`${result.applied} movies updated${result.failed.length?` · ${result.failed.length} failed`:''}`,result.failed.length>0);closeEditor();if(mode==='queue'&&(result.preflight_id||result.task_ids?.length)){if(typeof markMediaChangeRequested==='function')markMediaChangeRequested(paths,`${filters.stream_type||'Stream'}: ${changed.map(input=>input.name).join(', ')} change requested`);renderMovies();if(result.preflight_id)monitorQueuedPreflight(result.preflight_id,paths);else if(result.task_ids?.length)monitorBulkCompletion(result.task_ids,paths)}if(mode==='now'){window.setGlobalBusyProgress?.(2,2,'Movie changes complete','Refreshing the movie list');collapse();await loadMovies();refreshIndexedRecords(paths)}}
    catch(error){toast(error.message,true)}finally{if(mode==='now')window.endGlobalBusyOperation?.()}
  }
  async function waitForMovieIndex(paths){
    if(!paths.length)return true;
    for(let attempt=0;attempt<120;attempt++){
      const status=await api('/api/v82/movies/index-work',{method:'POST',body:JSON.stringify({paths})});
      if(status.ready)return true;
      await new Promise(resolve=>setTimeout(resolve,3000));
    }
    return false;
  }
  async function refreshIndexedRecords(paths){
    try{
      if(!await waitForMovieIndex(paths)){toast('Movie indexing is still pending; refresh the filters after it finishes',true);return}
      if(!filterRow.classList.contains('hidden')&&records.length){
        const result=await api('/api/v82/movies/stream-values',{method:'POST',body:JSON.stringify({paths})});
        const changed=new Set(paths);records=records.filter(item=>!changed.has(item.path)).concat(result.values);window.currentMovieStreamRecords=records;renderMovies();
      }
    }catch(error){console.warn('Could not refresh edited movie stream values',error)}
  }
  async function refreshAfterQueuedChange(paths,failed=0){
    await refreshIndexedRecords(paths);
    await loadMovies();
    toast(failed?`Movie changes finished with ${failed} failure${failed===1?'':'s'}`:'Movie changes completed; listing refreshed',failed>0);
  }
  async function monitorQueuedPreflight(requestId,paths){
    try{const status=await api('/api/v89/preflight/'+encodeURIComponent(requestId));if(status.status==='pending'||status.status==='running'){refreshTimer=setTimeout(()=>monitorQueuedPreflight(requestId,paths),3000);return}if(status.status==='skipped'){await refreshAfterQueuedChange(paths,0);toast('No movie needed this change');return}if(status.status!=='approved'){toast(status.error||status.result?.reason||'Movie bulk validation did not complete',true);await refreshAfterQueuedChange(paths,1);return}const ids=(status.result?.execution?.task_ids||[]).map(Number).filter(Boolean);if(ids.length)monitorBulkCompletion(ids,paths);else await refreshAfterQueuedChange(paths,0)}catch(error){console.warn('Could not monitor movie bulk validation',error)}
  }
  async function monitorBulkCompletion(taskIds,paths){
    try{const status=await api('/api/v82/movies/bulk-task-status',{method:'POST',body:JSON.stringify({task_ids:taskIds})});if(!status.finished){refreshTimer=setTimeout(()=>monitorBulkCompletion(taskIds,paths),3000);return}await refreshAfterQueuedChange(paths,status.counts.failed||0)}catch(error){console.warn('Could not refresh completed movie bulk filters',error)}
  }
  document.addEventListener('media-properties-applied',event=>{const path=event.detail?.path;if(!path||!state.movies.some(item=>item.path===path))return;loadMovies();refreshIndexedRecords([path])});
  function collapse(){records=[];window.movieHeaderAllowedPaths=null;closeEditor();filterRow.classList.add("hidden");filterContent.innerHTML="";oldRender()}
  window.resetMovieHeaderFilters=collapse;
  toggle.onclick=expand;
  const oldRender=renderMovies;renderMovies=function(){if(records.length)applyFilter();else oldRender();toggle.classList.toggle("hidden",!state.movies.length)};
  renderMovies();
})();
