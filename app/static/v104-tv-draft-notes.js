(function(){
  const changes=new Map();
  const committed=new Map();
  const episodeFinalBaseline=new Map();
  let sessionId='';
  const active=()=>Boolean(window.tvShowEditSession?.session_id&&window.tvShowEditSession.session_id===sessionId);
  const showId=()=>String(window.tvShowEditSession?.show_id||'');
  const has=(object,key)=>Object.prototype.hasOwnProperty.call(object||{},key);
  const noteKey=(entityKey)=>String(entityKey).startsWith('episode:')?String(entityKey):String(entityKey);

  window.clearTvDraftNotes=()=>{changes.clear();committed.clear();episodeFinalBaseline.clear();sessionId=''};
  window.loadTvDraftNotes=async session=>{
    window.clearTvDraftNotes();
    sessionId=String(session.session_id);
    const data=await api('/api/v79/tv/edit-session/'+encodeURIComponent(sessionId));
    if(sessionId!==String(window.tvShowEditSession?.session_id))return;
    for(const row of data.operations||[]){
      const edit=row.operation?.note_edit;
      if(!edit)continue;
      const key=String(row.path).startsWith('@show:')?String(row.path).slice(6):'episode:'+String(row.path);
      changes.set(key,{...(changes.get(key)||{}),...edit});
    }
    const saved=await api('/api/v86/note?entity_type=tv&entity_key='+encodeURIComponent(showId()));
    if(active()){
      committed.set(showId(),saved);
      renderShows();renderEpisodes();
    }
  };
  window.tvDraftNoteChanged=path=>active()&&changes.has('episode:'+String(path));
  window.tvDraftShowNoteChanged=()=>active()&&changes.has(showId());
  window.tvDraftShowFinal=()=>active()&&has(changes.get(showId()),'final_version')?Boolean(changes.get(showId()).final_version):undefined;
  window.tvDraftFinalForPath=path=>{
    if(!active())return undefined;
    const showFinal=window.tvDraftShowFinal();
    if(showFinal!==undefined)return showFinal;
    const episode=changes.get('episode:'+String(path));
    return has(episode,'final_version')?Boolean(episode.final_version):undefined;
  };
  window.tvDraftNoteView=saved=>active()&&saved?.entity_type==='tv'?{...saved,...(changes.get(noteKey(saved.entity_key))||{})}:saved;

  async function baseline(entityKey){
    if(committed.has(entityKey))return committed.get(entityKey);
    const saved=await api('/api/v86/note?entity_type=tv&entity_key='+encodeURIComponent(entityKey));
    committed.set(entityKey,saved);
    return saved;
  }
  window.stageTvDraftNote=async(entityKey,fields)=>{
    if(!active()||window.tvShowEditSession.status!=='open')throw new Error('TV-show draft is not open');
    const key=noteKey(entityKey);
    const path=key===showId()?'@show:'+key:key.startsWith('episode:')?key.slice(8):'';
    if(!path)throw new Error('This note is outside the active TV-show draft');
    const allowed={};
    for(const name of ['note','reviewed','final_version'])if(has(fields,name))allowed[name]=fields[name];
    if(!Object.keys(allowed).length)throw new Error('No note change to stage');
    const saved=await baseline(key);
    if(allowed.reviewed===false&&Boolean(has(allowed,'final_version')?allowed.final_version:has(changes.get(key),'final_version')?changes.get(key).final_version:saved.final_version))throw new Error('Unfreeze Final version before marking this item unreviewed');
    await api('/api/v79/tv/edit-session/'+encodeURIComponent(sessionId)+'/operation',{method:'POST',body:JSON.stringify({path,operation:{note_edit:allowed}})});
    changes.set(key,{...(changes.get(key)||{}),...allowed});
    window.markTvEditDirty?.();
    const projected={...saved,...changes.get(key)};
    if(projected.final_version)projected.reviewed=true;
    if(key===showId()){
      const show=state.shows.find(item=>String(item.id)===key);
      if(show){show.note=projected.note;show.reviewed=Boolean(projected.reviewed);show.final_version=Boolean(projected.final_version)}
    }
    window._preserveShowSelection=true;
    renderShows();renderEpisodes();
    return projected;
  };
  window.stageTvDraftFinal=async(kind,value,final)=>{
    if(kind==='episode'&&has(changes.get(showId()),'final_version'))throw new Error('The show-wide Final-version draft controls every episode. Change the show-wide setting first.');
    const key=kind==='show'?String(value):'episode:'+String(value);
    return window.stageTvDraftNote(key,{final_version:Boolean(final),...(final?{reviewed:true}:{})});
  };

  const oldRenderShows=renderShows;
  renderShows=function(){
    if(active()){
      const show=state.shows.find(item=>String(item.id)===showId());
      const saved=committed.get(showId());
      const edit=changes.get(showId());
      if(show&&saved&&edit){
        for(const field of ['note','reviewed','final_version'])if(has(edit,field))show[field]=edit[field];
        if(show.final_version)show.reviewed=true;
      }
      window._preserveShowSelection=true;
    }
    oldRenderShows();
  };
  const oldRenderEpisodes=renderEpisodes;
  renderEpisodes=function(){
    if(active()&&state.currentShow&&String(state.currentShow.id)===showId()){
      const showFinal=changes.get(showId());
      for(const season of state.currentShow.seasons||[])for(const episode of season.episodes||[]){
        const path=String(episode.path);
        if(!episodeFinalBaseline.has(path))episodeFinalBaseline.set(path,Boolean(episode.final_version));
        const episodeEdit=changes.get('episode:'+path);
        episode.final_version=has(showFinal,'final_version')?Boolean(showFinal.final_version):has(episodeEdit,'final_version')?Boolean(episodeEdit.final_version):episodeFinalBaseline.get(path);
      }
    }
    oldRenderEpisodes();
    // Final version becomes a real edit lock only after the queued save. A
    // staged F badge is a preview, so the user may still refine the draft.
    if(active()&&window.tvShowEditSession.status==='open')document.querySelectorAll('#episode-list .edit-file').forEach(button=>{button.disabled=false;button.removeAttribute('aria-disabled')});
  };
})();
