(function () {
  const content=$('#stream-preview-content'),maintenance=$('#subtitle-maintenance'),note=$('#subtitle-cleanup-note');
  if(!content||!maintenance)return;
  let selectedRow=null;
  const containsHtml=text=>/<\s*\/?\s*[A-Za-z][^>]*>/.test(text||'');
  function refresh(){
    if(!selectedRow||selectedRow.dataset.codecType!=='subtitle')return;
    const preview=content.querySelector('.subtitle-preview-text');
    if(!preview)return;
    const hasHtml=containsHtml(preview.textContent);
    maintenance.classList.toggle('hidden',!hasHtml);
    if(hasHtml&&note)note.textContent=selectedRow.dataset.external==='true'
      ?'The external subtitle file will be updated when changes are applied.'
      :'The embedded subtitle will be replaced with cleaned text when changes are applied; this requires one remux.';
  }
  document.addEventListener('click',event=>{
    const trigger=event.target.closest('[data-preview-stream]');
    if(!trigger)return;
    selectedRow=trigger.closest('.stream-row');
    // Never carry the previous stream's HTML result while the new preview is
    // loading. The observer will reveal maintenance after new text arrives.
    maintenance.classList.add('hidden');
  },true);
  new MutationObserver(refresh).observe(content,{childList:true,subtree:true,characterData:true});
})();
