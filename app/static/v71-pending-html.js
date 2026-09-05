(function () {
  const pendingHtmlCleanups=new Map();
  const cleanupKey=body=>body.external_path?`external:${body.external_path}`:`embedded:${body.type_index}`;
  let selectedSubtitleRow=null;
  document.addEventListener('click',event=>{const trigger=event.target.closest('[data-preview-stream]');if(!trigger)return;selectedSubtitleRow=trigger.closest('.stream-row');setTimeout(updateHtmlButton,0)},true);
  function selectedCleanup(){if(!selectedSubtitleRow)return null;const external=selectedSubtitleRow.dataset.external==='true';return{path:state.selectedPath,type_index:external?null:Number(selectedSubtitleRow.dataset.typeIndex),external_path:external?selectedSubtitleRow.dataset.path:null}}
  function updateHtmlButton(){const button=$('#subtitle-cleanup-apply'),body=selectedCleanup();if(!button||!body)return;const pending=pendingHtmlCleanups.has(cleanupKey(body));button.textContent=pending?'Undo HTML removal':'Remove HTML tags';button.classList.toggle('danger',pending);button.classList.toggle('primary',!pending)}
  const htmlButton=$('#subtitle-cleanup-apply');
  if(htmlButton)htmlButton.onclick=()=>{const body=selectedCleanup();if(!body)return;const key=cleanupKey(body);if(pendingHtmlCleanups.has(key)){pendingHtmlCleanups.delete(key);toast('Pending HTML-tag removal undone');updateHtmlButton()}else{pendingHtmlCleanups.set(key,body);toast('HTML-tag removal added to pending changes');$('#stream-preview-dialog').close()}updateQueuedChangeLabels()};

  const countWithoutHtml=queuedChangeCount;
  queuedChangeCount=function(){return countWithoutHtml()+pendingHtmlCleanups.size};
  const summaryWithoutHtml=queuedChangeSummary;
  queuedChangeSummary=function(){const value=summaryWithoutHtml(),html=pendingHtmlCleanups.size?`${pendingHtmlCleanups.size} HTML cleanup${pendingHtmlCleanups.size===1?'':'s'}`:'';return[value,html].filter(Boolean).join(' · ')};

  const apiWithoutPendingHtml=api;
  api=async function(resource,options){
    const cleanups=[...pendingHtmlCleanups.values()];
    if(cleanups.length&&resource==='/api/v7/media/edit'){
      for(const cleanup of cleanups)await apiWithoutPendingHtml('/api/v51/subtitle-cleanup',{method:'POST',body:JSON.stringify(cleanup)});
      const result=await apiWithoutPendingHtml(resource,options);result.subtitle_html_cleaned=true;pendingHtmlCleanups.clear();return result;
    }
    if(cleanups.length&&resource==='/api/v28/import/movie'&&options?.method==='POST'){
      const request=JSON.parse(options.body||'{}');request.html_cleanups=cleanups;
      const result=await apiWithoutPendingHtml(resource,{...options,body:JSON.stringify(request)});pendingHtmlCleanups.clear();return result;
    }
    if(cleanups.length&&resource==='/api/v65/queue'&&options?.method==='POST'){
      const request=JSON.parse(options.body||'{}');
      if(request.task_type==='media_edit'||request.task_type==='movie_import'){
        request.payload.html_cleanups=cleanups;
        const result=await apiWithoutPendingHtml(resource,{...options,body:JSON.stringify(request)});pendingHtmlCleanups.clear();return result;
      }
    }
    return apiWithoutPendingHtml(resource,options);
  };

  const openWithoutPendingHtml=openEditor;
  openEditor=async function(...args){pendingHtmlCleanups.clear();return openWithoutPendingHtml(...args)};
  updateQueuedChangeLabels();
})();
