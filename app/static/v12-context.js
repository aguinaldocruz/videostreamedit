let currentShowContext='';

function renderShowContext(){
  let line=$('#selected-show-title');
  if(!line){
    line=document.createElement('p');line.id='selected-show-title';line.className='selected-show-title hidden';
    $('#selected-file').parentNode.insertBefore(line,$('#selected-file'));
  }
  line.textContent=currentShowContext;line.classList.toggle('hidden',!currentShowContext);
}

const contextOpenEditor=openEditor;
openEditor=async function(path,label){renderShowContext();return contextOpenEditor(path,label)};

navigateMedia=function(offset){
  const next=mediaNavigationIndex+offset;if(next<0||next>=mediaNavigation.length)return;
  mediaNavigationIndex=next;const item=mediaNavigation[next];currentShowContext=item.showTitle||'';openEditor(item.path,item.label);
};

wireEditors=function(){
  document.querySelectorAll('.edit-file').forEach(button=>button.onclick=()=>{
    const isEpisode=Boolean(button.closest('#episode-list')),container=isEpisode?'#episode-list':'#movie-list';
    mediaNavigationKind=isEpisode?'episode':'movie';currentShowContext=isEpisode?clean(state.currentShow?.name||''):'';
    mediaNavigation=[...document.querySelectorAll(`${container} .edit-file`)].map(item=>({path:item.dataset.path,label:item.dataset.label,showTitle:currentShowContext}));
    mediaNavigationIndex=mediaNavigation.findIndex(item=>item.path===button.dataset.path);openEditor(button.dataset.path,button.dataset.label);
  });
};
