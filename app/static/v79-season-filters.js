(function(){
  const table=$('#episode-list')?.closest('table'),head=table?.querySelector('thead tr');
  if(!head)return;
  const action=head.lastElementChild;
  action.innerHTML='<button type="button" id="season-stream-toggle" class="season-stream-toggle hidden" title="Show stream-value filters" aria-label="Show stream-value filters">&gt;&gt;</button><button type="button" class="refresh" data-kind="tv">Refresh</button>';
  const seasonTools=document.querySelector('#season-tools');
  if(seasonTools)action.insertBefore(seasonTools,action.querySelector('.refresh'));
  head.insertAdjacentHTML('afterend','<tr id="season-stream-filter-row" class="season-stream-filter-row hidden"><th colspan="5"><div id="season-stream-filter-content"></div></th></tr><tr id="season-stream-edit-row" class="season-stream-edit-row hidden"><th colspan="5"><div id="season-stream-edit-content"></div></th></tr>');
  const toggle=$('#season-stream-toggle'),showTitle=document.querySelector('#show-title'),showStatus=document.querySelector('#show-title')?.parentElement,filterRow=$('#season-stream-filter-row'),filterContent=$('#season-stream-filter-content'),editRow=$('#season-stream-edit-row'),editContent=$('#season-stream-edit-content');
  const originalRenderShows=renderShows;
  window.jumpToFirstFilteredShow=function(force=false){const first=document.querySelector('#show-list .show-card');if(first&&(force||!state.currentShow||String(state.currentShow.id)!==String(first.dataset.id))){state.currentShow=state.shows.find(show=>String(show.id)===String(first.dataset.id))||null;state.currentSeason='*';$('#episode-search').value='';originalRenderShows();renderEpisodes()}};
  renderShows=function(){originalRenderShows();if(window._preserveShowSelection){window._preserveShowSelection=false;return}if(window._suppressShowFilterJump)return;window.jumpToFirstFilteredShow(true)};
  $('#show-search').oninput=()=>renderShows();
  const episodeHeading=document.querySelector('.episode-heading');
  if(episodeHeading&&!document.querySelector('#tv-show-navigation-left')){
    episodeHeading.insertAdjacentHTML('afterbegin','<div id="tv-show-navigation-left" class="tv-show-navigation"><button type="button" data-show-nav="first" aria-label="First filtered TV show">&lt;&lt;</button><button type="button" data-show-nav="previous" aria-label="Previous filtered TV show">&lt;</button></div>');
    episodeHeading.insertAdjacentHTML('beforeend','<div id="tv-show-navigation-right" class="tv-show-navigation"><button type="button" id="tv-show-note" class="note-button" title="Edit TV show note" aria-label="Edit TV show note">i</button><button type="button" data-show-nav="next" aria-label="Next filtered TV show">&gt;</button><button type="button" data-show-nav="last" aria-label="Last filtered TV show">&gt;&gt;</button></div>');
  }
  document.querySelector('#tv-show-note')?.addEventListener('click',()=>state.currentShow&&openEntityNote('tv',state.currentShow.id,state.currentShow.name));
  function renderShowTags(){
    const scope=document.querySelector('#episode-scope');
    if(!scope)return;
    const text=scope.dataset.scopeText||scope.textContent||'';
    scope.dataset.scopeText=text.replace(/\s+R:[^\s]+|\s+N:[^\s]+|\s+P:[^\s]+/g,'').trim();
    const show=state.currentShow;
    const tags=[];
    if(show?.reviewed)tags.push('<button type="button" class="reviewed-tag header-state-action" data-show-reset="reviewed" title="Reviewed — click to mark unreviewed">✓</button>');
    if(show?.note)tags.push('<span class="note-tag" title="This TV show has a note">i</span>');
    if(show?.plex_sync_change)tags.push('<button type="button" class="plex-sync-tag header-state-action" data-show-reset="plex_sync_change" title="Plex Sync Change — click to reset">P</button>');
    scope.innerHTML=`<span class="episode-scope-text">${esc(scope.dataset.scopeText)}</span><span class="show-state-tags">${tags.join('')}</span>`;
    scope.querySelectorAll('[data-show-reset]').forEach(button=>button.onclick=()=>resetShowState(button.dataset.showReset));
  }
  async function resetShowState(field){
    if(!state.currentShow)return;
    const item=state.currentShow;
    try{
      const result=await api('/api/v86/note',{method:'PUT',body:JSON.stringify({entity_type:'tv',entity_key:item.id,note:item.note||'',reviewed:field==='reviewed'?false:Boolean(item.reviewed),plex_sync_change:field==='plex_sync_change'?false:Boolean(item.plex_sync_change)})});
      updateNoteState(result);
      renderShowTags();
      toast(field==='reviewed'?'Marked unreviewed':'Plex Sync Change reset');
    }catch(error){toast(error.message,true)}
  }
  function updateShowNavigation(){
    const cards=[...document.querySelectorAll('#show-list .show-card')], ids=cards.map(card=>card.dataset.id), current=state.currentShow?ids.indexOf(String(state.currentShow.id)):-1, active=current>=0;
    const set=(kind,disabled)=>{const button=document.querySelector(`[data-show-nav="${kind}"]`);if(button)button.disabled=disabled||!ids.length;};
    set('first',current===0);set('previous',current===0);set('next',current===ids.length-1);set('last',current===ids.length-1);
  }
  document.querySelectorAll('[data-show-nav]').forEach(button=>button.onclick=()=>{
    const cards=[...document.querySelectorAll('#show-list .show-card')], ids=cards.map(card=>card.dataset.id), current=state.currentShow?ids.indexOf(String(state.currentShow.id)):-1;
    const kind=button.dataset.showNav;
    const target=current<0?(kind==='first'||kind==='previous'?0:ids.length-1):(kind==='first'?0:kind==='last'?ids.length-1:kind==='previous'?current-1:current+1);
    if(target<0||target>=ids.length)return; state.currentShow=state.shows.find(show=>String(show.id)===String(ids[target]))||null; state.currentSeason='*'; $('#episode-search').value=''; window._preserveShowSelection=true; renderShows(); renderEpisodes();
  });
  // Keep keyboard navigation scoped to the TV page.  Do not steal P/N while
  // the user is typing in a field or while a dialog is open.
  document.addEventListener('keydown', event=>{
    if(event.defaultPrevented||document.querySelector('#tv')?.classList.contains('hidden')||document.querySelector('dialog[open]'))return;
    const target=event.target;
    if(target?.matches('input,textarea,select,[contenteditable="true"]'))return;
    const key=String(event.key||'').toLowerCase();
    if(key!=='p'&&key!=='n')return;
    const button=document.querySelector(`[data-show-nav="${key==='p'?'previous':'next'}"]`);
    if(!button||button.disabled)return;
    event.preventDefault();button.click();
  });
  showTitle?.parentElement.classList.add('tv-show-title-block');
  if(showTitle&&!document.querySelector('#tv-show-status'))showTitle.insertAdjacentHTML('afterend','<span id="tv-show-status" class="tv-show-status hidden" role="status"></span>');
  const statusBadge=document.querySelector('#tv-show-status');action.querySelector('.refresh').onclick=async()=>{await loadTv();statusScope='';await updateShowStatus();};
  let scope='',records=[],loading=false;

  let statusScope='',statusPending=false;
  function lockFilterControls(locked){filterContent.querySelectorAll('select,input,[data-season-edit-toggle]').forEach(control=>{control.disabled=locked});}
  async function updateShowStatus(){
    if(!statusBadge||!state.currentShow){statusPending=false;statusBadge?.classList.add('hidden');lockFilterControls(false);return false;}
    const paths=state.currentShow.seasons.flatMap(season=>season.episodes.map(episode=>episode.path));
    try{const result=await api('/api/v79/tv/show-status',{method:'POST',body:JSON.stringify({paths})});statusPending=Boolean(result.active);statusBadge.classList.toggle('hidden',!statusPending);statusBadge.textContent=statusPending?'Needs attention':'';statusBadge.title=statusPending?result.reasons.join(' · '):'';statusBadge.classList.toggle('busy',Boolean(result.changes||result.indexing));lockFilterControls(statusPending);return statusPending}catch(_){return statusPending}
  }

  function scopeEpisodes(){if(!state.currentShow)return[];const seasons=state.currentSeason==='*'?state.currentShow.seasons:state.currentShow.seasons.filter(item=>item.name===state.currentSeason);return seasons.flatMap(item=>item.episodes)}
  function currentScope(){return state.currentShow?`${state.currentShow.id}\n${state.currentSeason}`:''}
  function option(value){return value===''?'<option value="__empty__">&lt;empty&gt;</option>':`<option value="${attr(value)}">${esc(value)}</option>`}
  function unique(field){
    const filters=selectedFilters(),filterField={stream_type:'stream_type',language:'language',region:'region',track_name:'track_name',filename_tag:'filename_tags'},selectedStream=filterContent.querySelector('[data-season-field=stream]')?.value||'__all__';
    const filtered=records.filter(item=>{if(field!=='stream_type'&&selectedStream!=='__all__'&&String(item.stream_type).toLowerCase()!==String(selectedStream).toLowerCase())return false;if(field!=='stream_type'&&filters.stream_type!==null&&String(item.stream_type).toLowerCase()!==String(filters.stream_type).toLowerCase())return false;return Object.entries(filters).every(([name,value])=>{
      if(name==='presence'||value===null||filterField[name]===field)return true;
      if(name==='language_regions')return field==='language'||value.includes(`${item.language||''}|${item.region||''}`);
      return name==='filename_tag'?(item.filename_tags||[]).includes(value):item[filterField[name]]===value;
    });});
    const values=field==='filename_tags'?filtered.flatMap(item=>item.filename_tags||[]):filtered.map(item=>item[field]);
    return[...new Set(values)].sort((a,b)=>a.localeCompare(b));
  }
  function fill(select,field,label){const old=select.value,items=unique(field);select.innerHTML=`<option value="__all__">${esc(label)}</option>`+items.map(option).join('');if([...select.options].some(item=>item.value===old))select.value=old}
  function selectedFilters(){const value=name=>filterContent.querySelector(`[data-season-field=${name}]`)?.value||'__all__',decoded=name=>value(name)==='__all__'?null:value(name)==='__empty__'?'':value(name),picker=filterContent.querySelector('.language-region-select'),languageRegions=picker?[...picker.selectedOptions].map(item=>item.value).filter(item=>!item.startsWith('__')):[];return{presence:filterContent.querySelector('[data-season-field=presence]')?.checked?'not_have':'have',stream_type:decoded('stream'),language:decoded('language'),region:decoded('region'),language_regions:languageRegions.length?languageRegions:null,track_name:decoded('track_name'),filename_tag:decoded('filename_tag')}}
  function matchingPaths(){const filters=selectedFilters(),streamType=filters.stream_type===null?null:String(filters.stream_type).toLowerCase(),matches=new Set(records.filter(item=>(streamType===null||String(item.stream_type).toLowerCase()===streamType)&&(!filters.language_regions||filters.language_regions.includes(`${item.language||''}|${item.region||''}`))&&(filters.track_name===null||item.track_name===filters.track_name)&&(filters.filename_tag===null||(item.filename_tags||[]).includes(filters.filename_tag))).map(item=>item.path));if(filters.presence==='not_have'){const all=new Set(scopeEpisodes().map(item=>item.path));return new Set([...all].filter(path=>!matches.has(path)))}return matches}
  function applyFilter(){const filters=selectedFilters(),active=Object.entries(filters).some(([name,value])=>name!=='presence'&&value!==null),matches=matchingPaths();$('#episode-list').querySelectorAll('tr').forEach(item=>{const path=item.querySelector('.edit-file')?.dataset.path;item.classList.toggle('season-stream-filtered-out',active&&!matches.has(path))});const count=active?matches.size:scopeEpisodes().length;const summary=filterContent.querySelector('[data-filter-summary]');if(summary)summary.textContent=`${count} matches`;const edit=filterContent.querySelector('[data-season-edit-toggle]');if(edit){edit.disabled=filters.presence==='not_have'||!filters.stream_type||!matches.size;edit.title=filters.presence==='not_have'?'Bulk editing requires Have':(!filters.stream_type?'Select Audio or Subtitles before editing':'Edit matching streams')}if(!matches.size)closeEditor()}

  function availableExtraLanguages(){
    const configured=(window.commonDetectionLanguages||['pt','pt-br','en']).map(value=>String(value).trim().toLowerCase()).filter(Boolean);
    const commonBases=new Set(configured.map(value=>value.split(/[-_]/,1)[0]));
    const base=value=>String(value||'').trim().toLowerCase().split(/[-_]/,1)[0];
    const used=new Set(records.map(item=>String(item.language||'').trim().toLowerCase()).filter(value=>value&&value!=='und'));
    const commonUsed=new Set([...used].filter(value=>commonBases.has(base(value))));
    const collect=types=>new Set(records.filter(item=>types.includes(String(item.stream_type).toLowerCase())).map(item=>String(item.language||'').toLowerCase()).filter(value=>value&&!commonBases.has(base(value))&&value!=='und'));
    return {audio:collect(['audio']), subtitle:collect(['subtitle','external']), commonUsed, configured};
  }
  function updateLanguageButton(){
    const button=filterContent.querySelector('[data-season-language-removal]'); if(!button)return;
    const values=availableExtraLanguages(); button.hidden=values.audio.size<=2&&values.subtitle.size<=2;
    button.title=`Remove uncommon languages (common in use: ${[...values.commonUsed].sort().join(', ')||'none'}; audio: ${values.audio.size}, subtitles: ${values.subtitle.size})`;
  }
  async function openLanguageRemoval(){
    const values=availableExtraLanguages(), dialogId='season-language-removal-dialog'; let dialog=document.getElementById(dialogId);
    if(!dialog){document.body.insertAdjacentHTML('beforeend',`<dialog id="${dialogId}"><div class="dialog-title"><div><h2>Remove uncommon languages</h2><p>Select stream type and languages to remove from the listed episodes.</p><p class="language-common-summary" data-lang-common-summary></p></div><button type="button" class="icon-close" data-lang-remove-cancel>×</button></div><div class="dialog-body"><label>Streams<select data-lang-remove-scope><option value="both">Audio + subtitles</option><option value="audio">Audio</option><option value="subtitle">Subtitles</option></select></label><fieldset><legend>Languages</legend><div class="language-selection-actions"><button type="button" data-lang-select-all>Select all</button><button type="button" data-lang-select-none>Unselect all</button><button type="button" data-lang-select-invert>Invert selection</button></div><div data-lang-remove-options></div></fieldset></div><div class="dialog-actions"><button type="button" data-lang-remove-cancel>Cancel</button><button type="button" class="primary" data-lang-remove-apply>Remove selected</button></div></dialog>`);dialog=document.getElementById(dialogId);dialog.querySelectorAll('[data-lang-remove-cancel]').forEach(item=>item.onclick=()=>{dialog.close();document.querySelectorAll('.season-language-preview-out').forEach(row=>row.classList.remove('season-language-preview-out'))});dialog.querySelector('[data-lang-select-all]').onclick=()=>dialog.querySelectorAll('[data-lang-remove-language]').forEach(item=>{item.checked=true});dialog.querySelector('[data-lang-select-none]').onclick=()=>dialog.querySelectorAll('[data-lang-remove-language]').forEach(item=>{item.checked=false});dialog.querySelector('[data-lang-select-invert]').onclick=()=>dialog.querySelectorAll('[data-lang-remove-language]').forEach(item=>{item.checked=!item.checked});dialog.querySelector('[data-lang-remove-apply]').onclick=async()=>{
      const scope=dialog.querySelector('[data-lang-remove-scope]').value,selected=[...dialog.querySelectorAll('[data-lang-remove-language]:checked')].map(item=>item.value);
      if(!selected.length){toast('Select at least one language',true);return}
      const types=scope==='audio'?['audio']:scope==='subtitle'?['subtitle','external']:['audio','subtitle','external'],base=selectedFilters();
      const matches=new Set();
      for(const item of records){
        if(!types.includes(String(item.stream_type).toLowerCase())||!selected.includes(String(item.language||'').toLowerCase()))continue;
        if(base.region!==null&&String(item.region||'')!==String(base.region||''))continue;
        if(base.track_name!==null&&String(item.track_name||'')!==String(base.track_name||''))continue;
        if(base.filename_tag!==null&&!(item.filename_tags||[]).includes(base.filename_tag))continue;
        matches.add(item.path);
      }
      if(!matches.size){toast('No matching episodes for the selected languages',true);return}
      const mode=await requestMode(matches.size,{...base,stream_type:types.length===1?types[0]:null,language:null,region:base.region,track_name:base.track_name});
      if(!mode)return;
      dialog.close();toast(mode==='now'?'Applying selected language removals…':'Submitting selected language removals…');
      let queued=0,taskIds=[];
      for(const type of [null]){
        const result=await api('/api/v79/tv/season-stream-bulk-edit',{method:'POST',body:JSON.stringify({paths:[...matches],filters:{presence:'have',stream_type:null,stream_types:types,language:null,languages:selected,region:base.region,language_regions:null,track_name:base.track_name,filename_tag:base.filename_tag},changed_fields:['remove'],language:'',region:'',track_name:'',default_action:'unchanged',forced_action:'unchanged',integrate:false,remove:true,mode})});
        queued+=result.queued||0;taskIds.push(...(result.task_ids||[]));
      }
      if(mode==='now'&&taskIds.length&&typeof waitForGlobalTasks==='function')await waitForGlobalTasks(taskIds);
      toast(mode==='now'?`${queued||matches.size} matching episodes updated`:`${queued} removal changes added to the queue`);
      collapse();
      await loadTv();
      statusScope='';
      await updateShowStatus();
    };dialog.querySelector('[data-lang-remove-scope]').onchange=()=>{renderLanguageOptions(dialog,values);dialog.querySelectorAll('[data-lang-remove-language]').forEach(item=>item.onchange=()=>previewLanguageRemoval(dialog));previewLanguageRemoval(dialog)}}
    const summary=dialog.querySelector('[data-lang-common-summary]');if(summary)summary.textContent=`Common languages in use: ${values.commonUsed.size?[...values.commonUsed].sort().join(', '):'none'}`;renderLanguageOptions(dialog,values);dialog.querySelectorAll('[data-lang-remove-language]').forEach(item=>item.onchange=()=>previewLanguageRemoval(dialog));previewLanguageRemoval(dialog);dialog.showModal();
  }
  function renderLanguageOptions(dialog,values){const scope=dialog.querySelector('[data-lang-remove-scope]').value, selected=scope==='audio'?values.audio:scope==='subtitle'?values.subtitle:new Set([...values.audio,...values.subtitle]);dialog.querySelector('[data-lang-remove-options]').innerHTML=[...selected].sort().map(value=>`<label><input type="checkbox" data-lang-remove-language value="${attr(value)}"> ${esc(value)}</label>`).join('')||'<small>No uncommon languages detected.</small>'}
  function previewLanguageRemoval(dialog){const scope=dialog.querySelector('[data-lang-remove-scope]').value,selected=new Set([...dialog.querySelectorAll('[data-lang-remove-language]:checked')].map(item=>item.value)),types=scope==='audio'?['audio']:scope==='subtitle'?['subtitle','external']:['audio','subtitle','external'],base=selectedFilters(),matches=new Set();if(selected.size)for(const item of records){if(!types.includes(String(item.stream_type).toLowerCase())||!selected.has(String(item.language||'').toLowerCase()))continue;if(base.region!==null&&String(item.region||'')!==String(base.region||''))continue;if(base.track_name!==null&&String(item.track_name||'')!==String(base.track_name||''))continue;if(base.filename_tag!==null&&!(item.filename_tags||[]).includes(base.filename_tag))continue;matches.add(item.path)}document.querySelectorAll('#episode-list tr').forEach(row=>{const path=row.querySelector('.edit-file')?.dataset.path;row.classList.toggle('season-language-preview-out',selected.size>0&&!matches.has(path))})}
  function filterChanged(){closeEditor();applyFilter()}
  function renderFilters(){
    filterContent.innerHTML='<div class="season-stream-filters"><label class="filter-not-have"><span>&lt;&gt;</span><input type="checkbox" data-season-field="presence" aria-label="Not have"></label><label>Stream<select data-season-field="stream"></select></label><label>Language<select data-season-field="language"></select></label><label>Region<select data-season-field="region"></select></label><label>Track name<select data-season-field="track_name"></select></label><label class="filename-tag-filter hidden">Filename tag<select data-season-field="filename_tag"></select></label><button type="button" data-season-language-removal class="season-language-removal" hidden>langs</button><span data-filter-summary></span><button type="button" data-season-edit-toggle class="season-stream-toggle" aria-label="Edit matching streams">&gt;&gt;</button><button type="button" class="season-stream-filter-close" title="Hide and clear stream filters" aria-label="Hide and clear stream filters">&lt;&lt;</button></div>';
    const stream=filterContent.querySelector('[data-season-field=stream]'),language=filterContent.querySelector('[data-season-field=language]'),region=filterContent.querySelector('[data-season-field=region]'),name=filterContent.querySelector('[data-season-field=track_name]'),filenameTag=filterContent.querySelector('[data-season-field=filename_tag]');
    const refreshOptions=(changed='')=>{const order=['stream','language','region','track_name','filename_tag'],changedIndex=order.indexOf(changed),pairCascade=filterContent.dataset.pairCascade==='true';delete filterContent.dataset.pairCascade;if(changedIndex>=0&&!pairCascade){for(let i=changedIndex+1;i<order.length;i++)([stream,language,region,name,filenameTag][i]).value='__all__'}const selectedCount=[stream,language,region,name,filenameTag].filter(select=>select.value&&select.value!=='__all__').length,start=!changed||selectedCount<=1?0:Math.max(0,changedIndex);if(!changed||start===0)fill(stream,'stream_type','Audio or subtitles');const external=stream.value==='external';filenameTag.closest('label').classList.toggle('hidden',!external);if(!external)filenameTag.value='__all__';if(!changed||start<=1)fill(language,'language','All languages');if(!changed||start<=2)fill(region,'region','All regions');if(!changed||start<=3)fill(name,'track_name','All track names');if(external&&(!changed||start<=4))fill(filenameTag,'filename_tags','All filename tags');if(filterContent.dataset.initialFilterLoad==='true'&&[language,region,name].every(select=>select.options.length===2&&select.options[1].value!=='__all__')){language.value=language.options[1].value;region.value=region.options[1].value;name.value=name.options[1].value;filterContent.dataset.initialSingleton='true';filterContent.dataset.initialFilterLoad='done'}filterChanged()};
    for(const select of[stream,language,region,name,filenameTag])select.onchange=()=>{if(!statusPending)refreshOptions(select.dataset.seasonField)};filterContent.querySelector("[data-season-field=presence]").onchange=()=>{if(!statusPending)filterChanged()};filterContent.querySelector('.season-stream-filter-close').onclick=collapse;filterContent.querySelector('[data-season-edit-toggle]').onclick=openEditor;filterContent.querySelector('[data-season-language-removal]').onclick=openLanguageRemoval;filterContent.dataset.initialFilterLoad='true';const streamTypes=new Set(records.map(item=>String(item.stream_type||'').toLowerCase()));if(streamTypes.has('audio')&&!streamTypes.has('subtitle')&&!streamTypes.has('external'))stream.dataset.initial='audio';refreshOptions();if(stream.dataset.initial){stream.value=stream.dataset.initial;refreshOptions('stream')}updateLanguageButton();
  }
  async function expand(){
    if(await updateShowStatus()){toast('Filters are unavailable while this show has queued, processing, or stale media updates',true);return}
    const episodes=scopeEpisodes();if(!episodes.length||loading)return;loading=true;filterRow.classList.remove('hidden');filterContent.innerHTML='<div class="season-filter-loading">Loading embedded stream values for the listed episodes…</div>';toggle.disabled=true;
    try{const result=await api('/api/v79/tv/season-stream-values',{method:'POST',body:JSON.stringify({paths:episodes.map(item=>item.path)})});window.currentTvStreamRecords=records=result.values;scope=currentScope();renderFilters();if(result.pending)toast(String(result.pending)+' episode'+(result.pending===1?'':'s')+' queued in background; refresh after completion');if(result.errors.length)toast(`${result.errors.length} episodes could not be indexed`,true)}catch(error){filterContent.innerHTML=`<div class="season-filter-loading error">${esc(error.message)}</div>`}finally{loading=false;toggle.disabled=false}
  }
  async function openEditor(){
    const paths=[...matchingPaths()],streamType=selectedFilters().stream_type;if(!streamType||!paths.length)return;
    // Render the editor immediately; saved values are supplemental and must
    // never hold the filtered-content editor open behind a page-level wait.
    if(!(v8Saved.language?.length||v8Saved.region?.length||v8Saved.title_audio?.length||v8Saved.title_subtitle?.length)){try{v8Saved=await api('/api/v8/saved-values')}catch(_){}}
    editRow.classList.remove('hidden');
    const externalActions=(streamType==='external'?'<label class="season-bulk-action"><input name="integrate" type="checkbox"> Integrate</label>':'')+'<label class="season-bulk-action tri-action"><span>Remove</span><input name="remove" type="checkbox"></label>';
    editContent.innerHTML=`<div class="season-stream-editor ${streamType==='external'?'with-external-actions':''}"><strong>change:</strong><div class="bulk-fields"><label>Language<input class="season-bulk-value" data-saved-field="language" name="language" type="text" placeholder="Leave unchanged"></label><label>Region<input class="season-bulk-value" data-saved-field="region" name="region" type="text" placeholder="Leave unchanged"></label><label>Track name<input class="season-bulk-value" data-saved-field="${streamType==='audio'?'title_audio':'title_subtitle'}" name="track_name" type="text" placeholder="Leave unchanged"></label></div><div class="bulk-actions">${externalActions}<label class="season-bulk-action tri-action"><span>Default <em data-tri-state>—</em></span><input name="default" type="checkbox" data-action="default" aria-label="Default"></label><label class="season-bulk-action tri-action"><span>Forced <em data-tri-state>—</em></span><input name="forced" type="checkbox" data-action="forced" aria-label="Forced"></label><button type="button" data-season-bulk-apply disabled>Apply</button><button type="button" class="season-stream-filter-close" data-season-edit-close aria-label="Close editor">&lt;&lt;</button></div></div>`;
    const apply=editContent.querySelector('[data-season-bulk-apply]');apply.hidden=false;apply.style.visibility="hidden";const integrate=editContent.querySelector('[name=integrate]'),remove=editContent.querySelector('[name=remove]');
    editContent.querySelectorAll('input[type=text]').forEach(input=>input.oninput=()=>{apply.hidden=false;apply.style.visibility="visible";input.dataset.dirty='true';if(remove){remove.checked=false;delete remove.dataset.dirty}if(integrate){integrate.checked=true;integrate.dataset.dirty='true'}apply.disabled=false});
    for(const tri of editContent.querySelectorAll('[data-action]')){tri.dataset.state='unchanged';tri.dataset.symbol='—';tri.onclick=event=>{event.preventDefault();const next=tri.dataset.state==='unchanged'?'set':tri.dataset.state==='set'?'clear':'unchanged';tri.dataset.state=next;tri.checked=next==='set';tri.indeterminate=next==='clear';tri.parentElement.querySelector('[data-tri-state]').textContent=next==='set'?'✓':next==='clear'?'×':'—';tri.dataset.symbol=next==='set'?'✓':next==='clear'?'×':'—';tri.dataset.dirty=next==='unchanged'?'':'true';apply.hidden=false;apply.style.visibility="visible";apply.disabled=false}};for(const action of[integrate,remove].filter(Boolean))action.onchange=()=>{if(action.checked&&action===remove&&!confirm(`Are you sure you want to remove every stream matching this filter from ${paths.length} media item${paths.length===1?"":"s"}?`)){action.checked=false;return}if(action.checked){action.dataset.dirty="true";const other=action===integrate?remove:integrate;if(other){other.checked=false;delete other.dataset.dirty}if(action===remove)editContent.querySelectorAll("input[type=text]").forEach(input=>{input.value="";delete input.dataset.dirty})}else delete action.dataset.dirty;const dirty=Boolean(editContent.querySelector("[data-dirty=true]"));apply.hidden=!dirty;apply.style.visibility=dirty?'visible':'hidden';apply.disabled=!dirty};
    installSavedValuePopups(editContent);editContent.querySelector('[data-season-edit-close]').onclick=closeEditor;editContent.querySelector('[data-season-bulk-apply]').onclick=chooseApplyMode;
  }
  function closeEditor(){closeSavedValueMenu();editRow.classList.add('hidden');editContent.innerHTML=''}
  function requestMode(count,filters){
    let dialog=$('#season-stream-apply-dialog');
    if(!dialog){document.body.insertAdjacentHTML('beforeend','<dialog id="season-stream-apply-dialog"><div class="dialog-title"><div><h2>Apply filtered episode changes</h2><p data-season-mode-summary></p></div><button type="button" class="icon-close" data-season-mode="cancel" aria-label="Cancel">×</button></div><div class="dialog-actions"><button type="button" data-season-mode="cancel">Cancel</button><button type="button" data-season-mode="queue">Add changes to queue</button><button type="button" class="primary" data-season-mode="now">Apply changes now</button></div></dialog>');dialog=$('#season-stream-apply-dialog')}
    const languageRegion=filters.language===null?'any language':(filters.language||'<empty>')+(filters.region===null?'':('-'+(filters.region||'<empty>')));dialog.querySelector('[data-season-mode-summary]').textContent=count+' matching episode'+(count===1?'':'s')+'. Filter: '+(filters.stream_type||'any stream')+' · '+languageRegion+' · '+(filters.track_name===null?'any track name':(filters.track_name||'<empty>'))+'. Exact streams will be fixed when queued.';
    return new Promise(resolve=>{let done=false;const finish=value=>{if(done)return;done=true;dialog.close();dialog.removeEventListener('cancel',cancel);resolve(value)};const cancel=event=>{event.preventDefault();finish(null)};dialog.querySelectorAll('[data-season-mode]').forEach(button=>button.onclick=()=>finish(button.dataset.seasonMode==='cancel'?null:button.dataset.seasonMode));dialog.addEventListener('cancel',cancel);dialog.showModal();dialog.querySelector('[data-season-mode=now]').focus({preventScroll:true})})
  }
  async function chooseApplyMode(){
    const paths=[...matchingPaths()],filters=selectedFilters(),inputs=[...editContent.querySelectorAll('input')],changed=inputs.filter(input=>input.dataset.dirty==='true');if(!changed.length)return;
    const mode=await requestMode(paths.length,filters);if(!mode)return;
    const textInputs=inputs.filter(input=>input.type==='text'),values=Object.fromEntries(textInputs.map(input=>[input.name,input.value])),default_action=editContent.querySelector('[data-action=default]')?.dataset.state||'unchanged',forced_action=editContent.querySelector('[data-action=forced]')?.dataset.state||'unchanged',integrate=Boolean(editContent.querySelector('[name=integrate]')?.checked),remove=Boolean(editContent.querySelector('[name=remove]')?.checked);
    try{let result=await api('/api/v79/tv/season-stream-bulk-edit',{method:'POST',body:JSON.stringify({paths,filters,changed_fields:changed.map(input=>input.name),...values,default_action,forced_action,integrate,remove,mode})});if(mode==='now'&&result.task_ids?.length&&typeof waitForGlobalTasks==='function'){let progress=await waitForGlobalTasks(result.task_ids);if(result.preflight){const childIds=[];for(const item of(progress.items||[])){try{const detail=JSON.parse(item.result_json||'{}');childIds.push(...(detail.task_ids||[]))}catch(_){}}if(childIds.length)progress=await waitForGlobalTasks(childIds)}result={...result,applied:progress.succeeded,failed:Array.from({length:progress.failed},()=>({}))};}const used=[];for(const input of changed.filter(input=>input.type==='text')){const field=input.name==='track_name'?(filters.stream_type==='audio'?'title_audio':'title_subtitle'):input.name;if(input.value.trim())used.push({field,value:input.value.trim()},{field,value:input.value.trim()})}if(used.length&&typeof offerSavedValues==='function')await offerSavedValues(used);toast(mode==='queue'?`${result.queued} episode changes added to the queue`:`${result.applied} episodes updated${result.failed.length?` · ${result.failed.length} failed`:''}`,result.failed.length>0);if(mode==='queue'&&typeof markMediaChangeRequested==='function'){markMediaChangeRequested(paths,`${filters.stream_type||'Stream'}: ${changed.map(input=>input.name).join(', ')} change requested`);renderEpisodes()}closeEditor();if(mode==='now'){records=[];scope='';filterRow.classList.add('hidden');await loadTv()}}
    catch(error){toast(error.message,true)}
  }
  function collapse(){records=[];scope='';closeEditor();filterRow.classList.add('hidden');filterContent.innerHTML='';$('#episode-list').querySelectorAll('tr').forEach(item=>item.classList.remove('season-stream-filtered-out'))}
  window.resetTvHeaderFilters=collapse;
  toggle.onclick=expand;
  const oldRender=renderEpisodes;renderEpisodes=function(){const next=currentScope();if(scope&&scope!==next)collapse();oldRender();renderShowTags();updateShowNavigation();toggle.classList.toggle('hidden',!state.currentShow);if(!state.currentShow){filterRow.classList.add('hidden');closeEditor();statusBadge?.classList.add('hidden');statusScope='';}else if(records.length&&scope===next)applyFilter();const nextStatus=state.currentShow?String(state.currentShow.id):'';if(nextStatus!==statusScope){statusScope=nextStatus;updateShowStatus()}};
  renderEpisodes();
})();
