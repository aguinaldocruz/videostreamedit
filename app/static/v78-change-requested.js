(function(){
  function media(path){return state.movies.find(item=>item.path===path)||state.shows.flatMap(show=>show.seasons).flatMap(season=>season.episodes).find(item=>item.path===path)}
  function requests(path){return media(path)?.change_requests||[]}
  function requested(path){return Boolean(media(path)?.change_requested||requests(path).length)}
  function statusLabel(items){return items.some(item=>item.status==='running')?'Changing':items.some(item=>item.status==='failed')?'Change Failed':'Change Requested'}
  function clue(items){return items.length?items.map(item=>`${item.status==='running'?'Processing':item.status==='failed'?'Failed':'Queued'} #${item.id}: ${item.summary||'Media stream change'}`).join('\n'):'A queued or in-progress media change exists'}
  window.markMediaChangeRequested=function(paths,summary='Media stream change'){
    const wanted=new Set(paths);const update=item=>{if(!wanted.has(item.path))return;item.change_requested=true;item.change_requests=[...(item.change_requests||[]),{id:'new',status:'pending',summary}]};state.movies.forEach(update);state.shows.forEach(show=>show.seasons.forEach(season=>season.episodes.forEach(update)));
  };
  function decorate(container){
    container.querySelectorAll('.edit-file').forEach(button=>{
      if(!requested(button.dataset.path))return;const items=requests(button.dataset.path),row=button.closest('tr');row?.classList.add('change-requested-row');const title=row?.querySelector('.movie-title,.episode-title');
      if(title&&!row.querySelector('.change-requested-badge'))title.insertAdjacentHTML('afterend',`<span class="change-requested-badge ${items.some(item=>item.status==='running')?'running':''} ${items.some(item=>item.status==='failed')?'failed':''}" title="${attr(clue(items))}">${esc(statusLabel(items))}</span>`);
    });
  }
  const oldMovies=renderMovies;renderMovies=function(){oldMovies();decorate($('#movie-list'))};
  const oldEpisodes=renderEpisodes;renderEpisodes=function(){oldEpisodes();decorate($('#episode-list'))};
  const oldOpen=openEditor;openEditor=async function(path,label){
    $('#stream-dialog .dialog-title .change-requested-badge')?.remove();
    let status={change_requested:requested(path),requests:requests(path)};try{status=await api(`/api/v78/change-requested?path=${encodeURIComponent(path)}`)}catch(_){}
    if(status.change_requested)toast(`${statusLabel(status.requests)}: hover the notice for queued change details`,status.requests.some(item=>item.status==='failed'));
    const result=await oldOpen(path,label);
    if(status.change_requested&&$('#stream-dialog')?.open&&!$('#stream-content .change-requested-notice')){
      const details=clue(status.requests);$('#stream-content').insertAdjacentHTML('afterbegin',`<p class="change-requested-notice" title="${attr(details)}"><strong>${esc(statusLabel(status.requests))}</strong> — this media has ${status.requests.length} queued, running, or failed request${status.requests.length===1?'':'s'}. Hover for details and review the task queue before applying conflicting edits.</p>`);
      const selected=$('#selected-file');if(selected&&!selected.parentElement.querySelector('.change-requested-badge'))selected.insertAdjacentHTML('afterend',`<span class="change-requested-badge" title="${attr(details)}">${esc(statusLabel(status.requests))}</span>`);
    }
    return result;
  };
  const oldApi=api;api=async function(resource,options){
    const result=await oldApi(resource,options);
    if(resource==='/api/v65/queue'&&options?.method==='POST')try{const body=JSON.parse(options.body||'{}'),path=(body.payload?.edit||body.payload||{}).path;if(path)markMediaChangeRequested([path],body.label||'Media stream change')}catch(_){}
    return result;
  };
})();
