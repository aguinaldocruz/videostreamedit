(function(){
  const setup=document.querySelector('#setup');
  const automation=setup?.querySelector('[data-setup-panel="subtitle-color"]');
  if(automation&&!automation.querySelector('#subtitle-color-settings')){
    automation.insertAdjacentHTML('beforeend',`<article id="subtitle-color-settings" class="subtitle-color-card">
      <div><h3>Subtitle text color</h3><p>Choose the color used by the Stream properties action. This applies to retained embedded SRT/SubRip subtitles; other subtitle formats are left unchanged.</p></div>
      <div class="subtitle-color-control"><label for="subtitle-color-value">Default color</label><input id="subtitle-color-value" type="color" value="#ffff00"><span data-subtitle-color-preview>Example subtitle text</span><button type="button" data-subtitle-color-save>Save color</button></div>
      <small data-subtitle-color-status role="status">Loading saved color…</small>
    </article>`);
    const input=automation.querySelector('#subtitle-color-value'),preview=automation.querySelector('[data-subtitle-color-preview]'),status=automation.querySelector('[data-subtitle-color-status]'),save=automation.querySelector('[data-subtitle-color-save]');
    const updatePreview=()=>{preview.style.color=input.value};input.addEventListener('input',updatePreview);updatePreview();
    api('/api/settings/subtitle-color').then(data=>{input.value=data.color||'#FFFF00';updatePreview();status.textContent=`Current color: ${input.value.toUpperCase()}`}).catch(error=>status.textContent=error.message);
    save.onclick=async()=>{save.disabled=true;try{const result=await api('/api/settings/subtitle-color',{method:'PUT',body:JSON.stringify({color:input.value})});input.value=result.color;updatePreview();status.textContent=`Saved ${result.color}. New color actions will use it.`;toast('Subtitle color saved')}catch(error){status.textContent=error.message;toast(error.message,true)}finally{save.disabled=false}};
  }

  const dialog=document.querySelector('#stream-dialog'),form=document.querySelector('#stream-form');
  const actions=form?.querySelector('.dialog-actions');
  if(!dialog||!actions||!form||dialog.querySelector('[data-apply-subtitle-color]'))return;
  const button=document.createElement('button');button.type='button';button.className='subtitle-color-action';button.dataset.applySubtitleColor='';
  button.textContent='Set subtitle color';button.title='Stage the color from Setup → Editing → Subtitle color for retained SRT subtitles, then Apply or Queue';
  actions.insertBefore(button,actions.querySelector('[data-close-stream]'));
  function refresh(){
    const visible=Boolean(dialog.open)&&Boolean(document.querySelector('#stream-content .stream-row[data-codec-type="subtitle"]'))&&!(typeof movieImportMode!=='undefined'&&movieImportMode?.editing);
    if(button.hidden!==!visible)button.hidden=!visible;
    const pending=form.dataset.subtitleColorPending;
    const label=pending?`Subtitle color · ${pending}`:'Set subtitle color';
    if(button.textContent!==label)button.textContent=label;
    const disabled=Boolean(window.isStreamEditorBusy?.());
    if(button.disabled!==disabled)button.disabled=disabled;
  }
  button.onclick=async()=>{
    if(button.disabled)return;
    const subtitles=[...document.querySelectorAll('#stream-content .stream-row[data-codec-type="subtitle"]')].filter(row=>!row.querySelector('[name="remove"]')?.checked);
    if(!subtitles.length){toast('No retained subtitle streams are available',true);return}
    try{
      const setting=await api('/api/settings/subtitle-color'),color=String(setting.color||'#FFFF00').toUpperCase();
      if(!/^#[0-9A-F]{6}$/.test(color))throw new Error('The saved subtitle color is invalid; update it in Setup → Editing → Subtitle color.');
      if(form.dataset.subtitleColorPending===color){delete form.dataset.subtitleColorPending;window.updateQueuedChangeLabels?.();refresh();toast('Subtitle color action removed from pending edits');return}
      if(!confirm(`Add the color ${color} to every retained embedded SRT/SubRip subtitle in this media?\n\nASS/SSA and image subtitles will be left unchanged. External subtitles are changed only if selected for integration. You can Apply now, Queue, or keep this as a TV-show draft.`))return;
      form.dataset.subtitleColorPending=color;window.updateQueuedChangeLabels?.();refresh();toast(`Subtitle color ${color} added to pending changes`);
    }catch(error){toast(error.message,true)}
  };
  // Observe the inputs to the action, not its own header label. Updating
  // textContent emits childList mutations even when the text is unchanged.
  const observer=new MutationObserver(refresh);
  observer.observe(dialog,{attributes:true,attributeFilter:['open','data-editor-phase']});
  observer.observe(form,{attributes:true,attributeFilter:['data-subtitle-color-pending']});
  const content=document.querySelector('#stream-content');
  if(content)observer.observe(content,{childList:true,subtree:true});
  document.addEventListener('media-properties-applied',refresh);document.addEventListener('media-properties-queued',refresh);
  form.addEventListener('change',refresh);refresh();
})();
