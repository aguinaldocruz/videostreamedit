/* Shared editor lifecycle for lists, reports and TV-show drafts. */
(function () {
  const dialog=document.querySelector('#stream-dialog'),form=document.querySelector('#stream-form');
  if(!dialog||!form)return;
  let phase='ready',opening=Promise.resolve(),generation=0,submitting=false;
  const busy=()=>phase==='loading'||phase==='saving';
  window.isStreamEditorBusy=busy;
  const draft=()=>Boolean(window.activeTvDraftForPath?.(state.selectedPath)?.session_id);
  const header=dialog.querySelector('.dialog-title>div');
  header?.insertAdjacentHTML('beforeend','<p class="stream-editor-context" role="status"></p>');
  const footer=form.querySelector('.dialog-actions');
  const reset=document.querySelector('#undo-stream-changes')||document.createElement('button');reset.type='button';reset.textContent='Reset edits';reset.className='stream-reset-edits';reset.hidden=true;
  if(!reset.isConnected)footer?.insertBefore(reset,footer.querySelector('[data-close-stream]'));
  window.updateStreamEditorContext=function(count=queuedChangeCount()){
    const context=dialog.querySelector('.stream-editor-context');
    if(context)context.textContent=phase==='loading'?'Loading stream properties…':draft()?'TV-show draft · Apply now updates the draft only. Save the show to change files.':'Direct media editing · Apply now or add changes to the queue.';
    dialog.dataset.editorMode=draft()?'draft':'direct';
    dialog.dataset.editorPhase=phase;
    const content=dialog.querySelector('#stream-content');if(content)content.inert=busy();
    const final=dialog.querySelector('#stream-final-version');
    if(draft()&&final)final.title='Final-version changes are applied to this TV-show draft only';
    reset.classList.remove('hidden');reset.hidden=!count;reset.disabled=busy();
    const submit=form.querySelector('[type=submit]');
    if(submit){submit.disabled=busy()||!count;submit.textContent=phase==='saving'?'Processing…':draft()?'Apply now':count?`Apply ${count} change${count===1?'':'s'}`:'Apply changes';}
    dialog.querySelector('#selected-file')?.setAttribute('title',dialog.querySelector('#selected-file').textContent);
  };
  const previousOpen=openEditor;
  openEditor=function(path,label){
    const request=++generation;
    // Serialize legacy renderers: a late renderer must never overwrite the
    // next media's DOM or capture its values as the previous media baseline.
    phase='loading';window.updateStreamEditorContext(0);
    if(!dialog.open)dialog.showModal();
    opening=opening.catch(()=>{}).then(async()=>{
      if(request!==generation)return;
      delete form.dataset.applyModeConfirmed;
      try{await previousOpen(path,label);}finally{
        if(request===generation){phase=submitting?'saving':'ready';window.updateStreamEditorContext();}
      }
    });
    return opening;
  };
  const previousSubmit=form.onsubmit;
  form.onsubmit=async function(event){
    event.preventDefault();if(busy())return;
    submitting=true;phase='saving';window.updateStreamEditorContext();
    try{return await previousSubmit.call(this,event);}
    finally{submitting=false;phase='ready';window.updateStreamEditorContext();}
  };
  reset.onclick=async()=>{
    if(busy()||!confirm('Discard the unapplied edits on this screen? Previously applied TV-show draft changes are kept.'))return;
    await openEditor(state.selectedPath,dialog.querySelector('#selected-file').textContent);
  };
  // Block navigation/close while an operation owns the editor. Read-only
  // tools and the Apply/Queue choice dialog retain their normal behavior.
  dialog.addEventListener('click',event=>{
    if(busy()&&event.target.closest('.dialog-title button,[data-close-stream]')){
      event.preventDefault();event.stopImmediatePropagation();
    }
  },true);
  dialog.addEventListener('cancel',event=>{
    event.preventDefault();if(!busy())dialog.querySelector('[data-close-stream]')?.click();
  });
  form.addEventListener('input',()=>window.updateStreamEditorContext());
  form.addEventListener('change',()=>window.updateStreamEditorContext());
})();
