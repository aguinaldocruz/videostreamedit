(function () {
  const form=$('#stream-form');
  if(!form)return;
  document.body.insertAdjacentHTML('beforeend',`<dialog id="stream-apply-mode-dialog"><div class="dialog-title"><div><h2>Apply stream changes</h2><p>Choose how the complete media operation will be processed.</p></div><button type="button" class="icon-close" data-stream-apply-mode="cancel" aria-label="Cancel">×</button></div><div class="dialog-actions"><button type="button" data-stream-apply-mode="cancel">Cancel</button><button type="button" data-stream-apply-mode="queue">Add changes to queue</button><button type="button" class="primary" data-stream-apply-mode="now">Apply changes now</button></div></dialog>`);
  const dialog=$("#stream-apply-mode-dialog"),applyNow=form.onsubmit;
  const resetEditorAfterCommit=()=>{if(typeof editorBaseline!=='undefined')editorBaseline=null;if(typeof pendingLastChange!=='undefined')pendingLastChange=null;if(typeof window.clearPendingHtmlCleanups==='function')window.clearPendingHtmlCleanups();form.dataset.queueCommitted='true';if(typeof updateQueuedChangeLabels==='function')updateQueuedChangeLabels()};
  function chooseMode(){return new Promise(resolve=>{let finished=false;const finish=value=>{if(finished)return;finished=true;dialog.close();dialog.removeEventListener('cancel',cancel);resolve(value)};const cancel=event=>{event.preventDefault();finish(null)};dialog.querySelectorAll('[data-stream-apply-mode]').forEach(button=>button.onclick=()=>finish(button.dataset.streamApplyMode==='cancel'?null:button.dataset.streamApplyMode));dialog.addEventListener('cancel',cancel);dialog.showModal();dialog.querySelector('[data-stream-apply-mode=now]').focus({preventScroll:true})})}
  form.onsubmit=async function(event){
    if(movieImportMode?.editing)return;
    event.preventDefault();
    if(typeof queuedChangeCount==='function'&&queuedChangeCount()===0){toast('No changes queued');return}
    // A stream editor opened from a TV-show draft is itself part of the
    // virtual journal.  Never open the normal Apply/Queue chooser here: the
    // episode operation is recorded by v9-session.js and the TV-show Save
    // action is the only commit point.
    if(window.activeTvDraftForPath?.(state.selectedPath)?.session_id){
      delete form.dataset.applyModeConfirmed;
      window.beginGlobalBusyImmediate?.('Applying stream changes to the TV-show draft');
      try{return await applyNow.call(form,event)}finally{window.endGlobalBusy?.()}
    }
    if(form.dataset.applyModeConfirmed==="now"){
      delete form.dataset.applyModeConfirmed;
      window.beginGlobalBusyImmediate?.('Applying stream changes');
      try{const result=await applyNow.call(form,event);if(!$('#stream-dialog')?.open)resetEditorAfterCommit();return result}finally{window.endGlobalBusy?.()}
    }
    const mode=await chooseMode();
    if(!mode)return;
    // Hold the overlay across request preparation, acknowledgement and the
    // refreshed editor, not just the HTTP call. A slow signature check must
    // never look like a missed click. The editor lifecycle also blocks submit.
    window.beginGlobalBusyImmediate?.(mode==='queue'?'Adding stream changes to the queue':'Applying stream changes');
    try{
      if(mode==='now'){
        const result=await applyNow.call(form,event);
        if(!$('#stream-dialog')?.open)resetEditorAfterCommit();
        return result;
      }
      window.setGlobalBusyProgress?.(0,3,'Preparing the queue request','Keeping this edit locked until the server acknowledges it');
      if(typeof window.collectCompleteQueuedEdit!=='function')throw new Error('Could not prepare the complete edit payload');
      const payload=window.collectCompleteQueuedEdit(),label=$('#selected-file')?.textContent||payload.edit.path;
      window.setGlobalBusyProgress?.(1,3,'Recording stream changes','Checking the source identity and accepting one job for this request');
      const task=await api('/api/v65/queue',{method:'POST',body:JSON.stringify({task_type:'media_edit',payload,label:`Edit ${label}`})});
      if(!task?.id)throw new Error('The queue did not return a task id');
      resetEditorAfterCommit();
      const queuedPath=payload.edit?.path||payload.path;
      document.dispatchEvent(new CustomEvent('media-properties-queued',{detail:{path:queuedPath,task_id:task.id}}));
      // Queueing does not change the file immediately. Reload it so the
      // proposed values disappear and Close/Next can be used without a stale
      // pending-change prompt.
      try{
        window.setGlobalBusyProgress?.(2,3,`Task #${task.id} queued`,'Refreshing stream properties; the queued proposal has been cleared');
        await openEditor(queuedPath,label);
      }finally{
        // Queueing is also a commit of the editor proposal.  Reloading the
        // media is informational and must never restore the old dirty state.
        window.markEditorCommittedClean?.();
      }
      window.setGlobalBusyProgress?.(3,3,`Task #${task.id} queued`,'You can close this media, navigate, or make another edit');
      toast(task.deduplicated?`These changes are already queued as task #${task.id}; no duplicate job created`:`Stream changes added to queue as task #${task.id}`);
    }catch(error){toast(error.message,true)}finally{window.endGlobalBusy?.()}
  };
})();
