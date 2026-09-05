(function () {
  const form=$('#stream-form');
  if(!form)return;
  document.body.insertAdjacentHTML('beforeend',`<dialog id="stream-apply-mode-dialog"><div class="dialog-title"><div><h2>Apply stream changes</h2><p>Choose how the complete media operation will be processed.</p></div><button type="button" class="icon-close" data-stream-apply-mode="cancel" aria-label="Cancel">×</button></div><div class="dialog-actions"><button type="button" data-stream-apply-mode="cancel">Cancel</button><button type="button" data-stream-apply-mode="queue">Add changes to queue</button><button type="button" class="primary" data-stream-apply-mode="now">Apply changes now</button></div></dialog>`);
  const dialog=$('#stream-apply-mode-dialog'),applyNow=form.onsubmit;
  function chooseMode(){return new Promise(resolve=>{let finished=false;const finish=value=>{if(finished)return;finished=true;dialog.close();dialog.removeEventListener('cancel',cancel);resolve(value)};const cancel=event=>{event.preventDefault();finish(null)};dialog.querySelectorAll('[data-stream-apply-mode]').forEach(button=>button.onclick=()=>finish(button.dataset.streamApplyMode==='cancel'?null:button.dataset.streamApplyMode));dialog.addEventListener('cancel',cancel);dialog.showModal();dialog.querySelector('[data-stream-apply-mode=now]').focus({preventScroll:true})})}
  form.onsubmit=async function(event){
    if(movieImportMode?.editing)return;
    event.preventDefault();
    if(typeof queuedChangeCount==='function'&&queuedChangeCount()===0){toast('No changes queued');return}
    if(form.dataset.applyModeConfirmed==="now"){
      delete form.dataset.applyModeConfirmed;
      return applyNow.call(form,event);
    }
    const mode=await chooseMode();
    if(!mode)return;
    if(mode==='now')return applyNow.call(form,event);
    try{
      if(typeof window.collectCompleteQueuedEdit!=='function')throw new Error('Could not prepare the complete edit payload');
      const payload=window.collectCompleteQueuedEdit(),label=$('#selected-file')?.textContent||payload.edit.path;
      const task=await api('/api/v65/queue',{method:'POST',body:JSON.stringify({task_type:'media_edit',payload,label:`Edit ${label}`})});
      toast(`Stream changes added to queue as task #${task.id}`);
      $('#stream-dialog').close();
    }catch(error){toast(error.message,true)}
  };
})();
