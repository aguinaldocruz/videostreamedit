(function () {
  const dialog=$('#stream-dialog');
  if(!dialog||typeof window.guardPendingStreamChanges!=='function')return;
  dialog.querySelectorAll('[data-close-stream]').forEach(button=>{
    const closeWithoutGuard=button.onclick||(()=>dialog.close());
    button.onclick=function(event){
      if(typeof queuedChangeCount==='function'&&queuedChangeCount()>0){
        event?.preventDefault();
        window.guardPendingStreamChanges(()=>closeWithoutGuard.call(button,event),'close');
        return;
      }
      return closeWithoutGuard.call(button,event);
    };
  });
})();
