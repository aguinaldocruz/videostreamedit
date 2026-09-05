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
  const previousOpenEditor=openEditor;openEditor=async function(...args){const result=await previousOpenEditor(...args);installRemoveCycle();return result};
})();
