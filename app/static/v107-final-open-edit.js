/* A new editing visit unfreezes approval; refreshing that visit must not. */
(function(){
  function enableFinalEditors(container){
    document.querySelectorAll(`${container} .edit-file[disabled]`).forEach(button=>{
      if(!button.title.startsWith('Final version'))return;
      button.disabled=false;
      button.title='Open Stream Properties and remove Final Revision to edit this media';
    });
  }
  const previousMovies=renderMovies,previousEpisodes=renderEpisodes;
  renderMovies=function(...args){const result=previousMovies(...args);enableFinalEditors('#movie-list');return result};
  renderEpisodes=function(...args){const result=previousEpisodes(...args);enableFinalEditors('#episode-list');return result};
  enableFinalEditors('#movie-list');enableFinalEditors('#episode-list');

  const previousOpen=openEditor;
  let generation=0;
  openEditor=async function(path,label,options={}){
    const dialog=document.querySelector('#stream-dialog');
    const newEditingVisit=!movieImportMode?.editing&&!options.preserveFinal&&(!dialog?.open||state.selectedPath!==path);
    const request=++generation;
    window.streamFinalVersionOpening=newEditingVisit;
    let result,editorLoaded=false;
    let wasFinal=false;
    try{
      result=await previousOpen(path,label);
      editorLoaded=true;
      if(!newEditingVisit||request!==generation||state.selectedPath!==path||!dialog?.open)return result;
      const status=await api('/api/v86/final-version?path='+encodeURIComponent(path));
      if(!status.effective_final_version)return result;
      if(request!==generation||state.selectedPath!==path||!dialog?.open)return result;
      wasFinal=true;
      const draft=window.activeTvDraftForPath?.(path);
      if(draft){
        if(draft.status!=='open')throw new Error('This TV-show draft is being saved. Wait for it to finish before editing.');
        if(window.tvDraftFinalForPath?.(path)!==false){
          if(window.tvDraftShowFinal?.()===true)await window.stageTvDraftFinal('show',draft.show_id,false);
          else await window.stageTvDraftFinal('episode',path,false);
        }
        if(request!==generation||state.selectedPath!==path)return result;
        const show=state.shows.find(item=>String(item.id)===String(draft.show_id));
        if(show){show.final_version=false;renderShows();renderEpisodes()}
        await window.applyTvDraftProjection?.(path);
        window.markEditorCommittedClean?.();
        toast('Final Revision removal staged in the TV-show draft. Save the show to commit it.');
      }else{
        await api('/api/v86/final-version',{method:'PUT',body:JSON.stringify({path,final_version:false})});
        if(request!==generation||state.selectedPath!==path)return result;
        await refreshStreamFinalVersion(path);
        window.markEditorCommittedClean?.();
        const movie=state.movies.find(item=>item.path===path);
        if(movie){movie.final_version=false;renderMovies()}
        else{
          for(const show of state.shows){
            const episode=show.seasons?.flatMap(season=>season.episodes||[]).find(item=>item.path===path);
            if(episode){episode.final_version=false;show.final_version=false;renderShows();renderEpisodes();break}
          }
        }
        toast('Final Revision removed. Stream Properties is ready for editing.');
      }
    }catch(error){
      if(!editorLoaded)throw error;
      if(request===generation&&state.selectedPath===path){if(wasFinal)setStreamFinalLock(true);toast('Could not remove Final Revision: '+error.message,true)}
    }finally{
      if(request===generation){window.streamFinalVersionOpening=false;window.updateStreamEditorContext?.()}
    }
    return result;
  };
})();
