(function(){
  async function waitForBulkPreflight(requestId){
    if(!requestId)return {succeeded:0,failed:0,items:[]};
    for(let attempt=0;attempt<600;attempt++){
      const status=await api('/api/v89/preflight/'+encodeURIComponent(requestId));
      if(status.status==='pending'||status.status==='running'){await new Promise(resolve=>setTimeout(resolve,500));continue}
      if(status.status!=='approved')throw new Error(status.error||status.result?.reason||'Bulk validation did not approve the request');
      const ids=(status.result?.execution?.task_ids||[]).map(Number).filter(Boolean);
      if(ids.length&&typeof waitForGlobalTasks==='function'){
        const state=await waitForGlobalTasks(ids),batch=state.items?.[0]?.result;
        if(batch?.batch)return {succeeded:Number(batch.succeeded||0),failed:Number(batch.failed||0),items:batch.items||[]};
        return state;
      }
      return {succeeded:Number(status.result?.approved||0),failed:0,items:[]};
    }
    throw new Error('Bulk validation is taking too long; the request remains queued');
  }
  const table=$('#episode-list')?.closest('table'),head=table?.querySelector('thead tr');
  if(!head)return;
  const action=head.lastElementChild;
  action.innerHTML='<button type="button" id="season-stream-toggle" class="season-stream-toggle hidden" title="Show stream-value filters" aria-label="Show stream-value filters">&gt;&gt;</button><button type="button" class="refresh" data-kind="tv">Refresh</button>';
  const seasonTools=document.querySelector('#season-tools');
  if(seasonTools)action.insertBefore(seasonTools,action.querySelector('.refresh'));
  head.insertAdjacentHTML('afterend','<tr id="season-stream-filter-row" class="season-stream-filter-row hidden"><th colspan="5"><div id="season-stream-filter-content"></div></th></tr><tr id="season-stream-edit-row" class="season-stream-edit-row hidden"><th colspan="5"><div id="season-stream-edit-content"></div></th></tr>');
  const toggle=$('#season-stream-toggle'),showTitle=document.querySelector('#show-title'),showStatus=document.querySelector('#show-title')?.parentElement,filterRow=$('#season-stream-filter-row'),filterContent=$('#season-stream-filter-content'),editRow=$('#season-stream-edit-row'),editContent=$('#season-stream-edit-content');
  const originalRenderShows=renderShows;
  window.jumpToFirstFilteredShow=function(force=false){const cards=[...document.querySelectorAll('#show-list .show-card')],first=cards[0];if(!first)return;const currentVisible=cards.some(card=>String(card.dataset.id)===String(state.currentShow?.id));if(force||!currentVisible){state.currentShow=state.shows.find(show=>String(show.id)===String(first.dataset.id))||null;state.currentSeason='*';$('#episode-search').value='';originalRenderShows();renderEpisodes()}};
  renderShows=function(){originalRenderShows();if(window._preserveShowSelection){window._preserveShowSelection=false;return}if(window._suppressShowFilterJump)return;if(!document.querySelector('#show-list .show-card')){if(state.currentShow){state.currentShow=null;renderEpisodes()}return}window.jumpToFirstFilteredShow()};
  $('#show-search').oninput=()=>{if(tvEditSession&&tvEditSession.status!=='committing'){toast('Exit TV-show edit mode before changing the show list',true);return}renderShows()};
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
      if(field==='reviewed'&&tvEditSession){
        const result=await window.stageTvDraftNote(item.id,{reviewed:false});
        item.reviewed=Boolean(result.reviewed);renderShowTags();toast('Unreviewed change staged in the TV-show draft');return;
      }
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
    if(tvEditSession&&tvEditSession.status!=='committing'){toast('Exit TV-show edit mode before navigating to another TV show',true);return}
    const cards=[...document.querySelectorAll('#show-list .show-card')], ids=cards.map(card=>card.dataset.id), current=state.currentShow?ids.indexOf(String(state.currentShow.id)):-1;
    const kind=button.dataset.showNav;
    const target=current<0?(kind==='first'||kind==='previous'?0:ids.length-1):(kind==='first'?0:kind==='last'?ids.length-1:kind==='previous'?current-1:current+1);
    if(target<0||target>=ids.length)return; state.currentShow=state.shows.find(show=>String(show.id)===String(ids[target]))||null; state.currentSeason='*'; $('#episode-search').value=''; window._preserveShowSelection=true; renderShows(); renderEpisodes();
  });
  document.querySelector('#show-list')?.addEventListener('click',event=>{
    if(!tvEditSession||tvEditSession.status==='committing')return;
    const card=event.target.closest('.show-card');
    if(card&&String(card.dataset.id)!==String(state.currentShow?.id)){event.preventDefault();event.stopImmediatePropagation();toast('Exit TV-show edit mode before changing the selected show',true)}
  },true);
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
  const statusBadge=document.querySelector('#tv-show-status');action.querySelector('.refresh').onclick=async()=>{
    if(tvEditSession){
      // Refresh the episode shell, not the draft stream snapshot.  The
      // journal is the authoritative virtual view until Save or Discard.
      renderEpisodes();
      if(records.length&&scope===currentScope())applyFilter();
      statusScope='';await updateShowStatus();
      toast('Refresh kept the TV-show journal and virtual filter values');
      return;
    }
    await loadTv();statusScope='';await updateShowStatus();
  };
  let scope='',records=[],loading=false,draftBaseline=[];
  let tvEditSession=null,tvEditDirty=false;
  let saveSubmitting=false,sessionShow='',sessionLookup=0;
  const commitMonitors=new Set();
  function applySession(session){
    tvEditSession=session;tvEditDirty=Boolean(session?.dirty)&&session?.status==='open';
    window.tvShowEditSession=session;window.tvEditDirty=tvEditDirty;
    renderEditMode();
    if(session?.status==='committing')monitorTvEditCommit(session,session.task_id);
  }
  async function recoverShowSession(){
    const showId=String(state.currentShow?.id||'');
    if(sessionShow===showId)return;
    sessionShow=showId;const lookup=++sessionLookup;
    if(String(tvEditSession?.show_id||'')!==showId){applySession(null);window.tvDraftStreamProjections={};window.clearTvDraftNotes?.();}
    if(!showId)return;
    try{
      const result=await api('/api/v79/tv/edit-session-active?show_id='+encodeURIComponent(showId));
      if(lookup!==sessionLookup||String(state.currentShow?.id)!==showId)return;
      if(result.session){applySession(result.session);await window.loadTvDraftNotes?.(result.session);}
    }catch(error){toast('Unable to recover TV-show draft status: '+error.message,true)}
  }
  function editModeButton(){return document.querySelector('#tv-show-edit-mode')}
  function renderEditMode(){
    const button=editModeButton(); if(!button)return;
    button.textContent=tvEditSession?(tvEditDirty?'Save & queue':'Exit edit mode'):'Edit TV show';
    button.classList.toggle('primary',Boolean(tvEditSession));
    button.disabled=saveSubmitting||tvEditSession?.status==='committing';
    button.title=tvEditSession?(tvEditDirty?'Save all staged changes':'Leave edit mode'):'Stage bulk and episode changes before one consolidated save';
    const status=document.querySelector('#tv-edit-session-status');
    if(status){
      status.className='tv-edit-session-status '+(tvEditSession?'active':'');
      const task=tvEditSession?.task;
      status.textContent=!tvEditSession?'':tvEditSession.status==='committing'?`Draft saved · Job #${tvEditSession.task_id||'…'} ${task?.status==='running'?'running':'queued'}${task?.progress_total?` · ${task.progress_current}/${task.progress_total}`:''} · Show locked; browsing is available`:tvEditDirty?'Draft changes pending save':'Edit mode active · no staged changes';
      status.onclick=tvEditSession?.status==='committing'?()=>{page('setup');window.openSetupDestination?.('tasks','queue');window.openDashboardTaskFilter?.(null,'tv_edit_session_commit')}:null;
      status.style.cursor=tvEditSession?.status==='committing'?'pointer':'';
      status.title=tvEditSession&&tvEditDirty?'All changes are virtual until you save this TV show.':'Changes are kept in the TV-show draft session.';
    }
    const discard=document.querySelector('#tv-show-edit-discard');
    if(discard){discard.hidden=!tvEditSession||!tvEditDirty;discard.disabled=tvEditSession?.status==='committing';}
    filterContent.querySelectorAll('[data-season-edit-toggle],[data-season-language-removal]').forEach(control=>{
      control.style.display=tvEditSession?'':'none';
      control.disabled=!tvEditSession||tvEditSession.status==='committing';
    });
    const committing=tvEditSession?.status==='committing';
    // A committed TV-show draft owns all episode edits until its consolidated
    // task completes.  Keep navigation and stream-property entry read-only,
    // while still allowing the user to browse the rest of the application.
    document.querySelectorAll('#episode-list .edit-file').forEach(control=>{control.disabled=Boolean(committing);control.setAttribute('aria-disabled',committing?'true':'false')});
  }
  window.renderTvEditMode=renderEditMode;
  window.markTvEditDirty=()=>{tvEditDirty=true;if(tvEditSession)tvEditSession.dirty=true;window.tvEditDirty=true;renderEditMode()};
  async function openEditMode(){
    if(!state.currentShow)return;
    try{
      const result=await api('/api/v79/tv/edit-session/open',{method:'POST',body:JSON.stringify({show_id:String(state.currentShow.id),show_title:String(state.currentShow.name||'')})});
      applySession(result);window.tvDraftStreamProjections=window.tvDraftStreamProjections||{};await window.loadTvDraftNotes?.(result);renderEditMode();toast(result.status==='committing'?`Draft already submitted as job #${result.task_id}`:'TV-show edit mode enabled. Changes are virtual until Save.');
    }catch(error){tvEditSession=null;tvEditDirty=false;window.tvShowEditSession=null;window.tvEditDirty=false;window.clearTvDraftNotes?.();renderEditMode();toast(error.message,true)}
  }
  async function leaveEditMode(){
    if(!tvEditSession)return;
    if(tvEditDirty&&!confirm('This TV show has staged changes. Discard them?'))return;
    try{await api('/api/v79/tv/edit-session/'+encodeURIComponent(tvEditSession.session_id),{method:'DELETE'});tvEditSession=null;tvEditDirty=false;window.tvShowEditSession=null;window.tvEditDirty=false;window.tvDraftStreamProjections={};window.clearTvDraftNotes?.();renderEditMode();toast('TV-show edit mode closed')}catch(error){toast(error.message,true)}
  }
  async function discardEditChanges(){
    if(!tvEditSession||!tvEditDirty)return;
    if(!confirm('Discard all staged TV-show changes?'))return;
    try{
      await api('/api/v79/tv/edit-session/'+encodeURIComponent(tvEditSession.session_id),{method:'DELETE'});
      tvEditSession=null;tvEditDirty=false;window.tvShowEditSession=null;window.tvEditDirty=false;window.tvDraftStreamProjections={};window.clearTvDraftNotes?.();
      records=[];draftBaseline=[];scope='';renderEditMode();closeEditor();toast('All staged TV-show changes discarded');
      await loadTv();
    }catch(error){toast(error.message,true)}
  }
  async function monitorTvEditCommit(sessionSnapshot,taskId){
    const id=sessionSnapshot.session_id;if(commitMonitors.has(id))return;
    commitMonitors.add(id);
    try{
      for(;;){
        const response=await (typeof originalFetch==='function'?originalFetch:fetch)('/api/v79/tv/edit-session/'+encodeURIComponent(id)+'/status');
        if(!response.ok)throw new Error('Unable to read TV-show save progress');
        const current=await response.json();
        const visible=String(state.currentShow?.id)===String(sessionSnapshot.show_id)&&tvEditSession?.session_id===id;
        if(current.status!=='committing'){
          if(visible){
            applySession(current.status==='open'?current:null);
            window.tvDraftStreamProjections={};window.clearTvDraftNotes?.();
            records=[];scope='';window._preserveShowSelection=true;
            await loadTv();await updateShowStatus();
          }
          toast(current.status==='committed'?'TV-show draft processing completed':current.error||'TV-show save needs attention; the remaining draft is retained',current.status!=='committed');
          return;
        }
        if(visible){tvEditSession={...tvEditSession,...current};window.tvShowEditSession=tvEditSession;renderEditMode();}
        await new Promise(resolve=>setTimeout(resolve,3000));
      }
    }catch(error){
      // A failed poll never unlocks a submitted draft. Retry without tying
      // progress to whichever show the user is now browsing.
      if(tvEditSession?.session_id===id){
        const status=document.querySelector('#tv-edit-session-status');
        if(status)status.textContent='Draft submitted · reconnecting to save status…';
      }
      setTimeout(()=>monitorTvEditCommit(sessionSnapshot,taskId),5000);
    }finally{commitMonitors.delete(id)}
  }
  async function saveEditMode(){
    if(saveSubmitting)return;
    if(!tvEditSession||!tvEditDirty)return leaveEditMode();
    if(!confirm('Queue all staged TV-show changes for processing? The show will stay locked while its job runs; you can browse other media.'))return;
    const snapshot={...tvEditSession};saveSubmitting=true;
    const ownsBusy=typeof beginGlobalBusyImmediate==='function';
    if(ownsBusy){beginGlobalBusyImmediate('Submitting TV-show draft');setGlobalBusyProgress(0,2,'Saving the draft to the task queue','Your changes are already journaled. No media processing is performed in this screen.')}
    renderEditMode();
    try{
      const result=await api('/api/v79/tv/edit-session/'+encodeURIComponent(snapshot.session_id)+'/save',{method:'POST'});
      if(ownsBusy)setGlobalBusyProgress(2,2,'Draft safely submitted',result.task_id?'Job #'+result.task_id+' · You may continue browsing':'No changes to process');
      applySession(result.status==='committed'?null:{...snapshot,...result,dirty:false});
      closeEditor();
      toast(result.task_id?'Draft saved to job #'+result.task_id+' · '+result.operation_count+' items · You may continue browsing':'No pending draft changes');
    }catch(error){toast('Unable to confirm submission. Your draft is retained; retrying Save is safe. '+error.message,true)}
    finally{saveSubmitting=false;renderEditMode();if(ownsBusy)endGlobalBusy()}
  }
  if(showTitle&&!document.querySelector('#tv-show-edit-mode')){
    showTitle.insertAdjacentHTML('afterend','<button type="button" id="tv-show-edit-mode" class="tv-show-edit-mode">Edit TV show</button><button type="button" id="tv-show-edit-discard" class="tv-show-edit-discard" hidden>Discard changes</button><span id="tv-edit-session-status" class="tv-edit-session-status" role="status" aria-live="polite"></span>');
    editModeButton().onclick=()=>tvEditSession?(tvEditDirty?saveEditMode():leaveEditMode()):openEditMode();
    document.querySelector('#tv-show-edit-discard').onclick=discardEditChanges;
  }
  renderEditMode();

  let statusScope='',statusPending=false;
  function lockFilterControls(locked){
    // A TV edit session is deliberately a local draft.  Index activity may
    // continue in the background, but it must not freeze the draft controls;
    // signatures are rechecked when the consolidated save starts.
    if(tvEditSession)locked=false;
    filterContent.querySelectorAll('select,input,[data-season-edit-toggle]').forEach(control=>{control.disabled=locked});
  }
  let statusPollTimer=null;
  function scheduleStatusPoll(){
    if(statusPollTimer||!statusPending)return;
    statusPollTimer=setTimeout(async()=>{statusPollTimer=null;const still=await updateShowStatus();if(still)scheduleStatusPoll()},1200);
  }
  async function updateShowStatus(){
    if(!statusBadge||!state.currentShow){statusPending=false;statusBadge?.classList.add('hidden');lockFilterControls(false);return false;}
    const paths=state.currentShow.seasons.flatMap(season=>season.episodes.map(episode=>episode.path));
    try{const result=await api('/api/v79/tv/show-status',{method:'POST',body:JSON.stringify({paths})});statusPending=Boolean(result.active);statusBadge.classList.toggle('hidden',!statusPending);statusBadge.textContent=statusPending?'Needs attention':'';statusBadge.title=statusPending?result.reasons.join(' · '):'';statusBadge.classList.toggle('busy',Boolean(result.changes||result.indexing));lockFilterControls(statusPending);if(statusPending)scheduleStatusPoll();return statusPending}catch(_){scheduleStatusPoll();return statusPending}
  }

  function scopeEpisodes(){if(!state.currentShow)return[];const seasons=state.currentSeason==='*'?state.currentShow.seasons:state.currentShow.seasons.filter(item=>item.name===state.currentSeason);return seasons.flatMap(item=>item.episodes)}
  function currentScope(){return state.currentShow?`${state.currentShow.id}\n${state.currentSeason}`:''}
  // Keep the virtual editor's language/region matching identical to the
  // combined Plex selector and the server (pt + BR is the same as pt|BR).
  function canonicalPair(language,region){let lang=String(language||'').trim().toLowerCase().replace('_','-'),area=String(region||'').trim().toUpperCase();const aliases={'portuguese':'pt','português':'pt','brazilian':'pt','brasileiro':'pt'};if(aliases[lang])lang=aliases[lang];if(lang.includes('-')){const parts=lang.split('-');if(!area&&parts[1]?.length===2){area=parts[1].toUpperCase();lang=parts[0]}}if(area==='BRAZIL'||area==='BRAZILIAN')area='BR';if(area==='PORTUGAL')area='PT';return `${lang}|${area}`}
  function streamMatchesFilters(item,filters){if(filters.stream_type&&String(item.stream_type).toLowerCase()!==String(filters.stream_type).toLowerCase())return false;if(filters.stream_types&&!filters.stream_types.map(String).map(value=>value.toLowerCase()).includes(String(item.stream_type||'').toLowerCase()))return false;if(filters.language_regions?.length&&!filters.language_regions.some(value=>canonicalPair(value.split('|')[0],value.split('|')[1])===canonicalPair(item.language,item.region)))return false;if(filters.language!==null&&filters.language!==undefined&&canonicalPair(item.language,item.region).split('|')[0]!==canonicalPair(filters.language,'').split('|')[0])return false;if(filters.languages?.length&&!filters.languages.some(value=>canonicalPair(value,'').split('|')[0]===canonicalPair(item.language,item.region).split('|')[0]))return false;if(filters.region!==null&&filters.region!==undefined&&canonicalPair(item.language,item.region).split('|')[1]!==canonicalPair('',filters.region).split('|')[1])return false;if(filters.track_name!==null&&filters.track_name!==undefined&&String(item.track_name||'')!==String(filters.track_name||''))return false;if(filters.filename_tag!==null&&filters.filename_tag!==undefined&&!(item.filename_tags||[]).includes(filters.filename_tag))return false;return true}
  // The live file can briefly have a different/stale region representation
  // than the indexed snapshot used by the TV editor (for example an index may
  // still say pt|PT while the file currently has plain pt).  Keep the exact
  // stream keys selected by the user so the commit can apply to that snapshot
  // without re-matching against a changed representation.
  function streamKey(item){return item.source==='external'?`external:${item.external_path||''}`:`embedded:${item.stream_type}:${item.type_index}`}
  function option(value){return value===''?'<option value="__empty__">&lt;empty&gt;</option>':`<option value="${attr(value)}">${esc(value)}</option>`}
  function unique(field){
    const filters=selectedFilters(),filterField={stream_type:'stream_type',language:'language',region:'region',track_name:'track_name',filename_tag:'filename_tags'},selectedStream=filterContent.querySelector('[data-season-field=stream]')?.value||'__all__';
    const filtered=records.filter(item=>!item._draftRemoved&&(()=>{if(field!=='stream_type'&&selectedStream!=='__all__'&&String(item.stream_type).toLowerCase()!==String(selectedStream).toLowerCase())return false;if(field!=='stream_type'&&filters.stream_type!==null&&String(item.stream_type).toLowerCase()!==String(filters.stream_type).toLowerCase())return false;return Object.entries(filters).every(([name,value])=>{
      if(name==='presence'||value===null||filterField[name]===field)return true;
      if(name==='language_regions')return field==='language'||value.includes(`${item.language||''}|${item.region||''}`);
      return name==='filename_tag'?(item.filename_tags||[]).includes(value):item[filterField[name]]===value;
    });})());
    const values=field==='filename_tags'?filtered.flatMap(item=>item.filename_tags||[]):filtered.map(item=>item[field]);
    return[...new Set(values)].sort((a,b)=>a.localeCompare(b));
  }
  function fill(select,field,label){const old=select.value,items=unique(field);select.innerHTML=`<option value="__all__">${esc(label)}</option>`+items.map(option).join('');if([...select.options].some(item=>item.value===old))select.value=old}
  function selectedFilters(){const value=name=>filterContent.querySelector(`[data-season-field=${name}]`)?.value||'__all__',decoded=name=>value(name)==='__all__'?null:value(name)==='__empty__'?'':value(name),picker=filterContent.querySelector('.language-region-select'),languageRegions=picker?[...picker.selectedOptions].map(item=>item.value).filter(item=>!item.startsWith('__')):[];return{presence:filterContent.querySelector('[data-season-field=presence]')?.checked?'not_have':'have',stream_type:decoded('stream'),language:picker?(languageRegions.length===1?languageRegions[0].split('|')[0]:null):decoded('language'),region:picker?(languageRegions.length===1?languageRegions[0].split('|')[1]:null):decoded('region'),language_regions:languageRegions.length?languageRegions:null,track_name:decoded('track_name'),filename_tag:decoded('filename_tag')}}
  function replayDraftOperations(operations){
    const byPath=new Map();
    for(const item of operations||[]){const path=item.path,operation=item.operation||{};if(!path||operation._discard||operation.note_edit)continue;if(!byPath.has(path))byPath.set(path,[]);byPath.get(path).push(operation)}
    window.tvDraftStreamProjections=Object.fromEntries(byPath);
    for(const [path,operations] of byPath){
      const rows=records.filter(item=>item.path===path);
      for(const operation of operations)projectDraftFlags(rows,operation);
    }
    for(const item of records){
      for(const operation of byPath.get(item.path)||[]){
        const key=streamKey(item),direct=operation.direct_edit;
        if(direct){
          const change=item.source==='external'?(direct.external_subtitles||[]).find(value=>String(value.path)===String(item.external_path)):(direct.tracks||[]).find(value=>String(value.codec_type)===String(item.stream_type)&&Number(value.type_index)===Number(item.type_index));
          if(change){if(change.language!==undefined)item.language=change.language;if(change.region!==undefined)item.region=change.region;if(change.title!==undefined)item.track_name=change.title}
          item._draftRemoved=(direct.remove||[]).includes(key);
          continue;
        }
        const targets=Array.isArray(operation.target_keys)?operation.target_keys:null;
        if(!(targets?targets.includes(key):streamMatchesFilters(item,operation.filters||{})))continue;
        if(operation.remove){item._draftRemoved=true;continue}
        if(operation.language!==''&&operation.language!==undefined)item.language=operation.language;
        if(operation.region!==''&&operation.region!==undefined)item.region=operation.region;
        if(operation.changed_fields?.includes('track_name')||(operation.track_name!==''&&operation.track_name!==undefined))item.track_name=operation.track_name||'';
      }
    }
  }
  function matchingPaths(){const filters=selectedFilters(),matches=new Set(records.filter(item=>!item._draftRemoved&&streamMatchesFilters(item,filters)).map(item=>item.path));if(filters.presence==='not_have'){const all=new Set(scopeEpisodes().map(item=>item.path));return new Set([...all].filter(path=>!matches.has(path)))}return matches}
  function applyFilter(){const filters=selectedFilters(),active=Object.entries(filters).some(([name,value])=>name!=='presence'&&value!==null),matches=matchingPaths();$('#episode-list').querySelectorAll('tr').forEach(item=>{const path=item.querySelector('.edit-file')?.dataset.path;item.classList.toggle('season-stream-filtered-out',active&&!matches.has(path))});const count=active?matches.size:scopeEpisodes().length;const summary=filterContent.querySelector('[data-filter-summary]');if(summary)summary.textContent=`${count} matches`;const edit=filterContent.querySelector('[data-season-edit-toggle]');if(edit){edit.disabled=filters.presence==='not_have'||!filters.stream_type||!matches.size;edit.title=filters.presence==='not_have'?'Bulk editing requires Have':(!filters.stream_type?'Select Audio or Subtitles before editing':'Edit matching streams')}if(!matches.size)closeEditor()}
  function renderVirtualStreamCounts(){
    if(!tvEditSession||!records.length)return;
    const counts=new Map();
    for(const item of records){
      if(item._draftRemoved)continue;
      const count=counts.get(item.path)||{audio:0,subtitle:0,external:0};
      const type=item.source==='external'?'external':item.stream_type;
      if(type in count)count[type]++;
      counts.set(item.path,count);
    }
    $('#episode-list').querySelectorAll('tr').forEach(row=>{
      const path=row.querySelector('.edit-file')?.dataset.path,cell=row.querySelector('.episode-stream-count');
      if(!path||!cell||!records.some(item=>item.path===path))return;
      const count=counts.get(path)||{audio:0,subtitle:0,external:0};
      cell.textContent=`${count.audio}/${count.subtitle}${count.external?`/${count.external}`:''}`;
      cell.title='Draft audio / embedded subtitles / external subtitles';
    });
  }
  function refreshVirtualFilters(){
    if(filterContent.classList.contains('season-filter-loading'))return;
    const selectedValues={};
    filterContent.querySelectorAll('[data-season-field]').forEach(control=>{selectedValues[control.dataset.seasonField]=control.type==='checkbox'?Boolean(control.checked):control.value});
    renderFilters(true);
    Object.entries(selectedValues).forEach(([name,value])=>{
      const control=filterContent.querySelector(`[data-season-field="${name}"]`);
      if(!control)return;
      if(control.type==='checkbox'){control.checked=Boolean(value);return}
      if(control.options&&[...control.options].some(option=>option.value===value))control.value=value;
    });
    window.refreshTvLanguageRegionFilter?.();
    applyFilter();
  }
  function projectDraftFlags(rows,operation){
    const direct=operation.direct_edit;
    for(const type of ['audio','subtitle'])for(const flag of ['default','forced']){
      const typed=rows.filter(item=>item.source!=='external'&&item.stream_type===type&&!item._draftRemoved);
      if(direct){
        const choice=direct[flag+'_'+type];
        if(choice!==undefined&&choice!=='__preserve__')typed.forEach(item=>item['is_'+flag]=streamKey(item)===choice);
      }else{
        const action=operation[flag+'_action'];if(!action||action==='unchanged')continue;
        const matched=typed.filter(item=>Array.isArray(operation.target_keys)?operation.target_keys.includes(streamKey(item)):streamMatchesFilters(item,operation.filters||{}));
        if(!matched.length)continue;
        if(action==='set')typed.forEach(item=>item['is_'+flag]=item===matched[matched.length-1]);
        else matched.forEach(item=>item['is_'+flag]=false);
      }
    }
  }
  function projectDraft(paths,filters,values,remove,actions={}){
    const targets=new Set(paths);
    const effective=new Set();
    records.forEach(item=>{
      if(!targets.has(item.path))return;
      if(item._draftRemoved)return;
      if(!streamMatchesFilters(item,filters))return;
      if(remove){if(!item._draftRemoved){item._draftRemoved=true;effective.add(item.path)}return}
      if(values.language!==''&&String(item.language||'')!==String(values.language)){item.language=values.language;effective.add(item.path)}
      if(values.region!==''&&String(item.region||'')!==String(values.region)){item.region=values.region;effective.add(item.path)}
      if((actions.changed_fields?.includes('track_name')||values.track_name!=='')&&String(item.track_name||'')!==String(values.track_name)){item.track_name=values.track_name;effective.add(item.path)}
      for(const flag of ['default','forced']){
        const action=actions[flag+'_action'];
        if(action==='clear'){item['is_'+flag]=false;effective.add(item.path)}
        if(action==='set'){
          records.filter(other=>other.path===item.path&&other.stream_type===item.stream_type).forEach(other=>other['is_'+flag]=false);
          item['is_'+flag]=true;effective.add(item.path);
        }
      }
    });
    // The journal represents the net difference from the moment edit mode
    // began. If a user changes a value and then changes it back, that media is
    // no longer part of the save plan.
    const baseline=new Map(draftBaseline.map(item=>[item.path+':'+item.source+':'+item.stream_type+':'+(item.type_index??item.external_path??''),item]));
    const net=new Set();
    records.filter(item=>targets.has(item.path)).forEach(item=>{
      const key=item.path+':'+item.source+':'+item.stream_type+':'+(item.type_index??item.external_path??'');
      const original=baseline.get(key);
      if(!original||Boolean(original._draftRemoved)!==Boolean(item._draftRemoved)||original.language!==item.language||original.region!==item.region||original.track_name!==item.track_name||Boolean(original.is_default)!==Boolean(item.is_default)||Boolean(original.is_forced)!==Boolean(item.is_forced))net.add(item.path);
    });
    renderEpisodes();
    refreshVirtualFilters();
    // Journal reversals too: returning to the baseline must undo an earlier
    // flag operation when the server consolidates the draft.
    return new Set([...net,...effective]);
  }
  window.projectTvEpisodeEdit=(path,direct)=>{
    if(!tvEditSession||!direct)return;
    const rows=records.filter(item=>item.path===path);
    projectDraftFlags(rows,{direct_edit:direct});
    (direct.tracks||[]).forEach(change=>rows.filter(item=>String(item.stream_type).toLowerCase()===String(change.codec_type).toLowerCase()&&Number(item.type_index)===Number(change.type_index)).forEach(item=>{if(change.language!==undefined)item.language=change.language;if(change.region!==undefined)item.region=change.region;if(change.title!==undefined)item.track_name=change.title}));
    (direct.external_subtitles||[]).forEach(change=>rows.filter(item=>item.source==='external'&&String(item.external_path||'')===String(change.path||'')).forEach(item=>{if(change.language!==undefined)item.language=change.language;if(change.region!==undefined)item.region=change.region;if(change.title!==undefined)item.track_name=change.title}));
    const removedKeys=new Set(direct.remove||[]);
    rows.forEach(item=>{item._draftRemoved=removedKeys.has(streamKey(item))});
    renderVirtualStreamCounts();
    refreshVirtualFilters();
  };

  function availableExtraLanguages(){
    const configured=(window.commonDetectionLanguages||['pt','pt-br','en']).map(value=>String(value).trim().toLowerCase()).filter(Boolean);
    const commonBases=new Set(configured.map(value=>value.split(/[-_]/,1)[0]));
    const base=value=>String(value||'').trim().toLowerCase().split(/[-_]/,1)[0];
    const used=new Set(records.filter(item=>!item._draftRemoved).map(item=>String(item.language||'').trim().toLowerCase()).filter(value=>value&&value!=='und'));
    const commonUsed=new Set([...used].filter(value=>commonBases.has(base(value))));
    const collect=types=>new Set(records.filter(item=>!item._draftRemoved&&types.includes(String(item.stream_type).toLowerCase())).map(item=>String(item.language||'').toLowerCase()).filter(value=>value&&!commonBases.has(base(value))&&value!=='und'));
    return {audio:collect(['audio']), subtitle:collect(['subtitle','external']), commonUsed, configured};
  }
  function updateLanguageButton(){
    const button=filterContent.querySelector('[data-season-language-removal]'); if(!button)return;
    const values=availableExtraLanguages(); button.hidden=values.audio.size<=2&&values.subtitle.size<=2;
    button.title=`Remove uncommon languages (common in use: ${[...values.commonUsed].sort().join(', ')||'none'}; audio: ${values.audio.size}, subtitles: ${values.subtitle.size})`;
  }
  async function openLanguageRemoval(){
    if(!tvEditSession){toast('Enter TV-show edit mode before staging bulk changes',true);return}
    const values=availableExtraLanguages(), dialogId='season-language-removal-dialog'; let dialog=document.getElementById(dialogId);
    if(!dialog){document.body.insertAdjacentHTML('beforeend',`<dialog id="${dialogId}"><div class="dialog-title"><div><h2>Remove uncommon languages</h2><p>Select stream type and languages to remove from the listed episodes.</p><p class="language-common-summary" data-lang-common-summary></p></div><button type="button" class="icon-close" data-lang-remove-cancel>×</button></div><div class="dialog-body"><label>Streams<select data-lang-remove-scope><option value="both">Audio + subtitles</option><option value="audio">Audio</option><option value="subtitle">Subtitles</option></select></label><fieldset><legend>Languages</legend><div class="language-selection-actions"><button type="button" data-lang-select-all>Select all</button><button type="button" data-lang-select-none>Unselect all</button><button type="button" data-lang-select-invert>Invert selection</button></div><div data-lang-remove-options></div></fieldset></div><div class="dialog-actions"><button type="button" data-lang-remove-cancel>Cancel</button><button type="button" class="primary" data-lang-remove-apply>Remove selected</button></div></dialog>`);dialog=document.getElementById(dialogId);dialog.querySelectorAll('[data-lang-remove-cancel]').forEach(item=>item.onclick=()=>{dialog.close();document.querySelectorAll('.season-language-preview-out').forEach(row=>row.classList.remove('season-language-preview-out'))});dialog.querySelector('[data-lang-select-all]').onclick=()=>dialog.querySelectorAll('[data-lang-remove-language]').forEach(item=>{item.checked=true});dialog.querySelector('[data-lang-select-none]').onclick=()=>dialog.querySelectorAll('[data-lang-remove-language]').forEach(item=>{item.checked=false});dialog.querySelector('[data-lang-select-invert]').onclick=()=>dialog.querySelectorAll('[data-lang-remove-language]').forEach(item=>{item.checked=!item.checked});dialog.querySelector('[data-lang-remove-apply]').onclick=async()=>{
      const scope=dialog.querySelector('[data-lang-remove-scope]').value,selected=[...dialog.querySelectorAll('[data-lang-remove-language]:checked')].map(item=>item.value);
      if(!selected.length){toast('Select at least one language',true);return}
      const types=scope==='audio'?['audio']:scope==='subtitle'?['subtitle','external']:['audio','subtitle','external'],base=selectedFilters();
      const matches=new Set();
      for(const item of records){
        if(item._draftRemoved)continue;
        if(!types.includes(String(item.stream_type).toLowerCase())||!selected.includes(String(item.language||'').toLowerCase()))continue;
        if(base.region!==null&&String(item.region||'')!==String(base.region||''))continue;
        if(base.track_name!==null&&String(item.track_name||'')!==String(base.track_name||''))continue;
        if(base.filename_tag!==null&&!(item.filename_tags||[]).includes(base.filename_tag))continue;
        matches.add(item.path);
      }
      if(!matches.size){toast('No matching episodes for the selected languages',true);return}
      if(tvEditSession){
        dialog.close();
        const ownsBusy=typeof beginGlobalBusyImmediate==='function';
        if(ownsBusy){beginGlobalBusyImmediate('Staging virtual subtitle removals');setGlobalBusyProgress(0,matches.size,'Recording removal journal',`${matches.size} matching episodes selected`)}
        try{
          let done=0;
          for(const path of matches){
            await api('/api/v79/tv/edit-session/'+encodeURIComponent(tvEditSession.session_id)+'/operation',{method:'POST',body:JSON.stringify({path,operation:{paths:[path],filters:{presence:'have',stream_type:null,stream_types:types,language:null,languages:selected,region:base.region,language_regions:null,track_name:base.track_name,filename_tag:base.filename_tag},changed_fields:['remove'],language:'',region:'',track_name:'',default_action:'unchanged',forced_action:'unchanged',integrate:false,remove:true,mode:'now'}})});
            records.filter(item=>item.path===path&&!item._draftRemoved&&types.includes(String(item.stream_type).toLowerCase())&&selected.includes(String(item.language||'').toLowerCase())).forEach(item=>item._draftRemoved=true);
            if(ownsBusy)setGlobalBusyProgress(++done,matches.size,'Recording removal journal',`Episode ${done}/${matches.size} staged`);
          }
          renderFilters(true);applyFilter();window.markTvEditDirty();toast(`${matches.size} episode removals staged; save the TV show when ready`);
        }catch(error){toast(error.message,true)}finally{if(ownsBusy){setGlobalBusyProgress(matches.size,matches.size,'Virtual removals staged','The media files are unchanged until Save');endGlobalBusy()}}
        return;
      }
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
  function renderFilters(keepEditor=false){
    filterContent.innerHTML='<div class="season-stream-filters"><label class="filter-not-have"><span>&lt;&gt;</span><input type="checkbox" data-season-field="presence" aria-label="Not have"></label><label>Stream<select data-season-field="stream"></select></label><label>Language<select data-season-field="language"></select></label><label>Region<select data-season-field="region"></select></label><label>Track name<select data-season-field="track_name"></select></label><label class="filename-tag-filter hidden">Filename tag<select data-season-field="filename_tag"></select></label><button type="button" data-season-language-removal class="season-language-removal" hidden>langs</button><span data-filter-summary></span><button type="button" data-season-edit-toggle class="season-stream-toggle" aria-label="Edit matching streams">&gt;&gt;</button><button type="button" class="season-stream-filter-close" title="Hide and clear stream filters" aria-label="Hide and clear stream filters">&lt;&lt;</button></div>';
    const stream=filterContent.querySelector('[data-season-field=stream]'),language=filterContent.querySelector('[data-season-field=language]'),region=filterContent.querySelector('[data-season-field=region]'),name=filterContent.querySelector('[data-season-field=track_name]'),filenameTag=filterContent.querySelector('[data-season-field=filename_tag]');
    const refreshOptions=(changed='')=>{const order=['stream','language','region','track_name','filename_tag'],changedIndex=order.indexOf(changed),pairCascade=filterContent.dataset.pairCascade==='true';delete filterContent.dataset.pairCascade;if(changedIndex>=0&&!pairCascade){for(let i=changedIndex+1;i<order.length;i++)([stream,language,region,name,filenameTag][i]).value='__all__'}const selectedCount=[stream,language,region,name,filenameTag].filter(select=>select.value&&select.value!=='__all__').length,start=!changed||selectedCount<=1?0:Math.max(0,changedIndex);if(!changed||start===0)fill(stream,'stream_type','Audio or subtitles');const external=stream.value==='external';filenameTag.closest('label').classList.toggle('hidden',!external);if(!external)filenameTag.value='__all__';if(!changed||start<=1)fill(language,'language','All languages');if(!changed||start<=2)fill(region,'region','All regions');if(!changed||start<=3)fill(name,'track_name','All track names');if(external&&(!changed||start<=4))fill(filenameTag,'filename_tags','All filename tags');if(filterContent.dataset.initialFilterLoad==='true'&&[language,region,name].every(select=>select.options.length===2&&select.options[1].value!=='__all__')){language.value=language.options[1].value;region.value=region.options[1].value;name.value=name.options[1].value;filterContent.dataset.initialSingleton='true';filterContent.dataset.initialFilterLoad='done'}if(!keepEditor)filterChanged()};
    for(const select of[stream,language,region,name,filenameTag])select.onchange=()=>{if(!statusPending)refreshOptions(select.dataset.seasonField)};filterContent.querySelector("[data-season-field=presence]").onchange=()=>{if(!statusPending)filterChanged()};filterContent.querySelector('.season-stream-filter-close').onclick=collapse;filterContent.querySelector('[data-season-edit-toggle]').onclick=()=>{if(tvEditSession)openEditor();else toast('Enter TV-show edit mode before staging bulk changes',true)};filterContent.querySelector('[data-season-language-removal]').onclick=openLanguageRemoval;filterContent.dataset.initialFilterLoad='true';const streamTypes=new Set(records.map(item=>String(item.stream_type||'').toLowerCase()));if(streamTypes.has('audio')&&!streamTypes.has('subtitle')&&!streamTypes.has('external'))stream.dataset.initial='audio';refreshOptions();if(stream.dataset.initial){stream.value=stream.dataset.initial;refreshOptions('stream')}updateLanguageButton();renderEditMode();
  }
  async function expand(){
    if(await updateShowStatus()&&!tvEditSession){toast('Filters are unavailable while this show has queued, processing, or stale media updates',true);return}
    const episodes=scopeEpisodes();if(!episodes.length||loading)return;loading=true;filterRow.classList.remove('hidden');filterContent.innerHTML='<div class="season-filter-loading">Loading current stream filter values for the listed episodes…</div>';toggle.disabled=true;
    try{const [result,session]=await Promise.all([api('/api/v79/tv/season-stream-values',{method:'POST',body:JSON.stringify({paths:episodes.map(item=>item.path)})}),tvEditSession?api('/api/v79/tv/edit-session/'+encodeURIComponent(tvEditSession.session_id)):Promise.resolve(null)]);draftBaseline=JSON.parse(JSON.stringify(result.values));records=JSON.parse(JSON.stringify(result.values));window.currentTvStreamRecords=records;if(session)replayDraftOperations(session.operations);scope=currentScope();renderFilters();window.refreshTvLanguageRegionFilter?.();renderVirtualStreamCounts();applyFilter();if(result.pending)toast(String(result.pending)+' episode'+(result.pending===1?'':'s')+' queued in background; refresh after completion');if(result.errors.length)toast(`${result.errors.length} episodes could not be indexed`,true)}catch(error){filterContent.innerHTML=`<div class="season-filter-loading error">${esc(error.message)}</div>`}finally{loading=false;toggle.disabled=false}
  }
  async function openEditor(){
    if(!tvEditSession){toast('Enter TV-show edit mode before staging bulk changes',true);return}
    const paths=[...matchingPaths()],streamType=selectedFilters().stream_type;if(!streamType||!paths.length)return;
    // Render the editor immediately; saved values are supplemental and must
    // never hold the filtered-content editor open behind a page-level wait.
    if(!(v8Saved.language?.length||v8Saved.region?.length||v8Saved.title_audio?.length||v8Saved.title_subtitle?.length)){try{v8Saved=await api('/api/v8/saved-values')}catch(_){}}
    editRow.classList.remove('hidden');
    const externalActions=(streamType==='external'?'<label class="season-bulk-action"><input name="integrate" type="checkbox"> Integrate</label>':'')+'<label class="season-bulk-action tri-action"><span>Remove</span><input name="remove" type="checkbox"></label>';
    editContent.innerHTML=`<div class="season-stream-editor ${streamType==='external'?'with-external-actions':''}"><strong>change:</strong><div class="bulk-fields"><label>Language<input class="season-bulk-value" data-saved-field="language" name="language" type="text" placeholder="Leave unchanged"></label><label>Region<input class="season-bulk-value" data-saved-field="region" name="region" type="text" placeholder="Leave unchanged"></label><label>Track name<input class="season-bulk-value" data-saved-field="${streamType==='audio'?'title_audio':'title_subtitle'}" name="track_name" type="text" placeholder="Leave unchanged"></label></div><div class="bulk-actions">${externalActions}<label class="season-bulk-action tri-action"><span>Default <em data-tri-state>—</em></span><input name="default" type="checkbox" data-action="default" aria-label="Default"></label><label class="season-bulk-action tri-action"><span>Forced <em data-tri-state>—</em></span><input name="forced" type="checkbox" data-action="forced" aria-label="Forced"></label><button type="button" data-season-bulk-apply disabled>Apply</button><button type="button" class="season-stream-filter-close" data-season-edit-close aria-label="Close editor">&lt;&lt;</button></div></div>`;
    const apply=editContent.querySelector('[data-season-bulk-apply]');apply.hidden=false;apply.style.visibility="hidden";const integrate=editContent.querySelector('[name=integrate]'),remove=editContent.querySelector('[name=remove]');
    editContent.querySelectorAll('input[type=text]').forEach(input=>input.oninput=()=>{apply.hidden=false;apply.style.visibility="visible";input.dataset.dirty='true';if(remove){remove.checked=false;delete remove.dataset.dirty}if(integrate){integrate.checked=true;integrate.dataset.dirty='true'}apply.disabled=false});
for(const tri of editContent.querySelectorAll('[data-action]')){tri.dataset.state='unchanged';tri.dataset.symbol='—';tri.onclick=event=>{const next=tri.dataset.state==='unchanged'?'set':tri.dataset.state==='set'?'clear':'unchanged';tri.dataset.state=next;tri.checked=next==='set';tri.indeterminate=next==='clear';tri.parentElement.querySelector('[data-tri-state]').textContent=next==='set'?'✓':next==='clear'?'×':'—';tri.dataset.symbol=next==='set'?'✓':next==='clear'?'×':'—';tri.dataset.dirty=next==='unchanged'?'':'true';apply.hidden=false;apply.style.visibility="visible";tri.title=next==='set'?'Set flag':next==='clear'?'Clear flag on matching streams':'Leave unchanged';const dirty=Boolean(editContent.querySelector('[data-dirty=true]'));apply.disabled=!dirty;apply.style.visibility=dirty?'visible':'hidden'}};for(const action of[integrate,remove].filter(Boolean))action.onchange=()=>{if(action.checked&&action===remove&&!confirm(`Are you sure you want to remove every stream matching this filter from ${paths.length} media item${paths.length===1?"":"s"}?`)){action.checked=false;return}if(action.checked){action.dataset.dirty="true";const other=action===integrate?remove:integrate;if(other){other.checked=false;delete other.dataset.dirty}if(action===remove)editContent.querySelectorAll("input[type=text]").forEach(input=>{input.value="";delete input.dataset.dirty})}else delete action.dataset.dirty;const dirty=Boolean(editContent.querySelector("[data-dirty=true]"));apply.hidden=!dirty;apply.style.visibility=dirty?'visible':'hidden';apply.disabled=!dirty};
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
    if(saveSubmitting||tvEditSession?.status==='committing'){toast('This draft has been submitted; wait for its save task before changing the show',true);return}
    const paths=[...matchingPaths()],filters=selectedFilters(),inputs=[...editContent.querySelectorAll('input')],changed=inputs.filter(input=>input.dataset.dirty==='true');if(!changed.length)return;
    if(tvEditSession){
      const textInputs=inputs.filter(input=>input.type==='text'),values=Object.fromEntries(textInputs.map(input=>[input.name,input.value]));
      // The selector normally writes separate `pt`/`BR` values.  Also accept
      // a pasted/saved combined value so a bulk edit cannot journal a display
      // label such as "Portuguese-Brazilian" as the language code.
      if(values.language&&!values.region){const raw=String(values.language).trim().toLowerCase().replace('—','-');const parts=raw.split(/\s*-\s*/);const aliases={'portuguese':'pt','português':'pt','brazilian':'pt','brasileiro':'pt'};if(aliases[parts[0]]){values.language=aliases[parts[0]];if(parts[1])values.region=parts[1].toUpperCase()==='BRAZIL'?'BR':parts[1].toUpperCase()==='PORTUGAL'?'PT':parts[1].toUpperCase()}}
      const default_action=editContent.querySelector('[data-action=default]')?.dataset.state||'unchanged',forced_action=editContent.querySelector('[data-action=forced]')?.dataset.state||'unchanged',integrate=Boolean(editContent.querySelector('[name=integrate]')?.checked),remove=Boolean(editContent.querySelector('[name=remove]')?.checked);
      const selectedKeysByPath=new Map();
      // Select only from the current virtual stream state. A second filter
      // must not match a language or track name that this draft already
      // changed, even when the committed index still contains that value.
      const selectionRows=records;
      const seenSelection=new Set();
      selectionRows.forEach(item=>{if(item._draftRemoved||!paths.includes(item.path)||!streamMatchesFilters(item,filters))return;const key=streamKey(item),identity=`${item.path}\n${key}`;if(seenSelection.has(identity))return;seenSelection.add(identity);if(!selectedKeysByPath.has(item.path))selectedKeysByPath.set(item.path,[]);selectedKeysByPath.get(item.path).push(key)});
      const ownsBusy=typeof beginGlobalBusyImmediate==='function';
      if(ownsBusy){beginGlobalBusyImmediate('Staging virtual TV-show changes');setGlobalBusyProgress(0,3,'Recording journal changes',`${paths.length} matching episode${paths.length===1?'':'s'} selected`)}
      try{
        const request={paths:[],filters,changed_fields:changed.map(input=>input.name),...values,default_action,forced_action,integrate,remove,mode:'now'};
        const effectivePaths=projectDraft(paths,filters,values,remove,{default_action,forced_action,changed_fields:request.changed_fields});
        if(!effectivePaths.size){
          // Do not discard an earlier virtual operation merely because the
          // current projected rows no longer match the old filter.  The
          // journal is the source of truth until Save or Discard is chosen.
          const hasJournal=paths.some(path=>Array.isArray(window.tvDraftStreamProjections?.[path])&&window.tvDraftStreamProjections[path].length);
          if(!hasJournal){tvEditDirty=false;tvEditSession.dirty=false;renderEditMode();toast('No effective changes found; journal is empty');}
          closeEditor();return;
        }
        let done=0;
        for(const path of effectivePaths){const draftOperation={...request,paths:[path],target_keys:selectedKeysByPath.get(path)||[]};await api('/api/v79/tv/edit-session/'+encodeURIComponent(tvEditSession.session_id)+'/operation',{method:'POST',body:JSON.stringify({path,operation:draftOperation})});window.tvDraftStreamProjections[path]=[draftOperation];if(ownsBusy)setGlobalBusyProgress(++done,effectivePaths.size,'Recording journal changes',`Episode ${done}/${effectivePaths.size} staged`)}
        // A new request that changes no row on one episode must not erase an
        // earlier operation on that episode from the same TV-show draft.
        window.markTvEditDirty();toast(`${effectivePaths.size} episode change${effectivePaths.size===1?'':'s'} staged; save the TV show when ready`);closeEditor();return;
      }catch(error){toast(error.message,true);return}
      finally{if(ownsBusy){setGlobalBusyProgress(3,3,'Virtual changes staged','The media files are unchanged until Save');endGlobalBusy()}}
    }
    const mode=await requestMode(paths.length,filters);if(!mode)return;
    // Lock immediately after the user chooses Now/Queue. This prevents the
    // short preflight/API gap from looking like the operation was cancelled.
    const ownsBusy=typeof beginGlobalBusyImmediate==='function';
    if(ownsBusy){beginGlobalBusyImmediate(mode==='now'?'Applying filtered episode changes':'Queueing filtered episode changes');setGlobalBusyProgress(0,3,mode==='now'?'Preparing filtered episode changes':'Submitting filtered episode changes',`${paths.length} matching episode${paths.length===1?'':'s'} selected`)}
    const textInputs=inputs.filter(input=>input.type==='text'),values=Object.fromEntries(textInputs.map(input=>[input.name,input.value])),default_action=editContent.querySelector('[data-action=default]')?.dataset.state||'unchanged',forced_action=editContent.querySelector('[data-action=forced]')?.dataset.state||'unchanged',integrate=Boolean(editContent.querySelector('[name=integrate]')?.checked),remove=Boolean(editContent.querySelector('[name=remove]')?.checked);
    const selectedKeysByPath=new Map();
    records.forEach(item=>{if(item._draftRemoved||!paths.includes(item.path)||!streamMatchesFilters(item,filters))return;const key=streamKey(item);if(!selectedKeysByPath.has(item.path))selectedKeysByPath.set(item.path,[]);selectedKeysByPath.get(item.path).push(key)});
    try{let result=await api('/api/v79/tv/season-stream-bulk-edit',{method:'POST',body:JSON.stringify({paths,filters,changed_fields:changed.map(input=>input.name),...values,target_keys:[...selectedKeysByPath.values()].flat(),default_action,forced_action,integrate,remove,mode})});if(ownsBusy)setGlobalBusyProgress(1,3,mode==='now'?'Validating current episodes':'Changes submitted',mode==='now'?'Checking each media signature before applying':'The request is now protected against duplicate submission');if(mode==='now'&&result.preflight_id){const progress=await waitForBulkPreflight(result.preflight_id);result={...result,applied:progress.succeeded,failed:Array.from({length:progress.failed||0},()=>({}))};}const used=[];for(const input of changed.filter(input=>input.type==='text')){const field=input.name==='track_name'?(filters.stream_type==='audio'?'title_audio':'title_subtitle'):input.name;if(input.value.trim())used.push({field,value:input.value.trim()})}if(used.length&&typeof offerSavedValues==='function')await offerSavedValues(used);const skipped=result.skipped||[];const noOp=Number(result.queued||0)===0&&skipped.length>0;const skipReason=skipped[0]?.reason||'No matching streams required a change';toast(noOp?`${result.requested_media||paths.length} episodes checked · ${skipReason.toLowerCase()}`:(mode==='queue'?`${result.queued} episode changes added to the queue`:`${result.applied} episodes updated${result.failed.length?` · ${result.failed.length} failed`:''}`),!noOp&&result.failed.length>0);if(mode==='queue'&&!noOp&&typeof markMediaChangeRequested==='function'){markMediaChangeRequested(paths,`${filters.stream_type||'Stream'}: ${changed.map(input=>input.name).join(', ')} change requested`);renderEpisodes()}closeEditor();if(mode==='now'){records=[];scope='';filterRow.classList.add('hidden');await loadTv()}if(ownsBusy)setGlobalBusyProgress(3,3,'Filtered episode changes submitted','Refreshing the TV-show view')}
    catch(error){toast(error.message,true)}
    finally{if(ownsBusy)endGlobalBusy()}
  }
  function collapse(){records=[];scope='';closeEditor();filterRow.classList.add('hidden');filterContent.innerHTML='';$('#episode-list').querySelectorAll('tr').forEach(item=>item.classList.remove('season-stream-filtered-out'))}
  window.resetTvHeaderFilters=collapse;
  toggle.onclick=expand;
  const oldRender=renderEpisodes;renderEpisodes=function(){const next=currentScope();if(scope&&scope!==next)collapse();oldRender();recoverShowSession();renderEditMode();renderShowTags();updateShowNavigation();toggle.classList.toggle('hidden',!state.currentShow);if(!state.currentShow){filterRow.classList.add('hidden');closeEditor();statusBadge?.classList.add('hidden');statusScope='';}else if(records.length&&scope===next){renderVirtualStreamCounts();applyFilter()}const nextStatus=state.currentShow?String(state.currentShow.id):'';if(nextStatus!==statusScope){statusScope=nextStatus;updateShowStatus()}};
  renderEpisodes();
})();
