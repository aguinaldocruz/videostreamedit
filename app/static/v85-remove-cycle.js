(function(){
  function installRemoveCycle(){
    const heading=[...document.querySelectorAll('#stream-content .stream-grid.v7.head>span')].find(item=>item.textContent.trim()==='Remove');
    if(!heading||heading.querySelector('[data-remove-cycle]'))return;
    const button=document.createElement('button');button.type='button';button.className='remove-cycle';button.dataset.removeCycle='';button.textContent='↻';button.setAttribute('aria-label','Mark all streams for removal');heading.append(button);
    let step=0,baseline=[];const labels=['Mark all for removal','Unmark all removals','Invert the original removal selection'];
    function boxes(){return[...document.querySelectorAll('#stream-content .stream-row [name=remove]')]}
    function updateTitle(){button.title=labels[step];button.setAttribute('aria-label',labels[step])}
    button.onclick=()=>{
      const items=boxes();if(!items.length)return;if(step===0)baseline=items.map(item=>item.checked);
      items.forEach((item,index)=>{const checked=step===0?true:step===1?false:!baseline[index];if(item.checked!==checked){item.checked=checked;item.dispatchEvent(new Event('change',{bubbles:true}))}});
      step=(step+1)%3;updateTitle();if(typeof updateQueuedChangeLabels==='function')updateQueuedChangeLabels();
    };
    updateTitle();
  }
  const previousOpenEditor=openEditor;openEditor=async function(...args){const result=await previousOpenEditor(...args);
    // This is the outermost Stream Properties wrapper.  Re-apply the virtual
    // TV-show journal after every renderer/plugin has finished building rows;
    // some integrations replace the row DOM after the normal v9 projection.
    // The projection is idempotent, so this does not duplicate removals.
    if(window.activeTvDraftForPath?.(args[0])?.session_id&&typeof window.applyTvDraftProjection==='function'){
      try{await window.applyTvDraftProjection(args[0]);if(typeof captureEditorBaseline==='function')captureEditorBaseline()}catch(_){/* committed media remains usable */}
    }
    installRemoveCycle();return result};
})();
