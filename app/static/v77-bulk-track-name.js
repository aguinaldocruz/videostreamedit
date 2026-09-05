(function () {
  const filters = document.querySelector('.movie-stream-filters');
  if (!filters) return;
  filters.insertAdjacentHTML('afterend', `<div id="movie-bulk-track-name" class="movie-bulk-track-name hidden">
    <label>New track name<input id="movie-bulk-track-name-value" type="text" autocomplete="off" placeholder="Type or select a saved value"></label>
    <button type="button" id="movie-bulk-track-name-queue" class="primary">Queue bulk track-name change</button>
    <div id="movie-bulk-track-name-menu" class="movie-bulk-track-name-menu hidden"></div>
  </div>`);
  const panel=$('#movie-bulk-track-name'),input=$('#movie-bulk-track-name-value'),button=$('#movie-bulk-track-name-queue'),menu=$('#movie-bulk-track-name-menu');
  const type=$('#movie-stream-type'),language=$('#movie-stream-language'),trackName=$('#movie-stream-track-name'),search=$('#movie-search');
  let values={title_audio:[],title_subtitle:[]};

  function matches(){return typeof window.currentFilteredMovies==='function'?window.currentFilteredMovies():[]}
  function eligible(){return ['audio','subtitle'].includes(type.value)&&Boolean(language.value)&&Boolean(trackName.value)&&matches().length>5}
  function update(){const count=matches().length;panel.classList.toggle('hidden',!eligible());button.textContent=`Queue change for ${count} movies`}
  function closeMenu(){menu.classList.add('hidden');menu.innerHTML=''}
  function showMenu(){
    const field=type.value==='audio'?'title_audio':'title_subtitle',query=input.value.trim().toLocaleLowerCase();
    const options=(values[field]||[]).filter(value=>!query||value.toLocaleLowerCase().includes(query));
    menu.innerHTML=options.length?options.map(value=>`<button type="button" data-value="${attr(value)}">${esc(value)}</button>`).join(''):'<span class="muted">No saved match — keep typing to use a new value</span>';
    menu.classList.remove('hidden');
    menu.querySelectorAll('button').forEach(choice=>choice.onclick=()=>{input.value=choice.dataset.value;closeMenu();input.focus()});
  }
  input.onclick=showMenu;input.oninput=showMenu;input.onkeydown=event=>{if(event.key==='Escape')closeMenu()};
  input.onblur=()=>setTimeout(closeMenu,150);
  type.addEventListener('change',()=>setTimeout(update,0));language.addEventListener('change',()=>setTimeout(update,0));trackName.addEventListener('change',()=>setTimeout(update,0));search.addEventListener('input',()=>setTimeout(update,0));

  const previousRender=renderMovies;
  renderMovies=function(){previousRender();update()};
  button.onclick=async()=>{
    const newName=input.value.trim(),files=matches();
    if(!eligible()){update();return}
    if(!newName){toast('Enter or select the new track name',true);input.focus();return}
    if(newName===trackName.value){toast('The new track name is already selected',true);input.focus();return}
    const kind=type.value==='audio'?'audio':'subtitle';
    if(!confirm(`Queue a track-name change for ${files.length} movies?\n\nMatching ${kind} streams with language “${language.value}” and track name “${trackName.value}” will be changed to “${newName}”.`))return;
    button.disabled=true;
    try{
      const result=await api('/api/v77/movies/bulk-track-name',{method:'POST',body:JSON.stringify({paths:files.map(file=>file.path),stream_type:type.value,language:language.value,track_name:trackName.value,new_track_name:newName})});
      const queuedPaths=new Set(files.map(file=>file.path));state.movies.forEach(file=>{if(queuedPaths.has(file.path))file.change_requested=true});renderMovies();
      const field=type.value==='audio'?'title_audio':'title_subtitle';
      try{await offerSavedValues([{field,value:newName}]);v8Saved=await api('/api/v8/saved-values');values=v8Saved}catch(_){}
      toast(`${result.queued} movie changes added to the queue${result.skipped.length?` · ${result.skipped.length} skipped`:''}`);
      panel.classList.add('hidden');
    }catch(error){toast(error.message,true)}finally{button.disabled=false}
  };
  api('/api/v8/saved-values').then(result=>{values=result}).catch(()=>{});
  update();
})();
