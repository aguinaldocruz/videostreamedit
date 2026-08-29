function ensureMediaPathIndicator(){
  let indicator=$('#media-path-indicator');
  if(indicator)return indicator;
  indicator=document.createElement('button');indicator.type='button';indicator.id='media-path-indicator';indicator.className='media-path-indicator';indicator.textContent='i';indicator.setAttribute('aria-label','Show complete media file path');
  $('#selected-file').insertAdjacentElement('afterend',indicator);return indicator;
}

function setMediaPathIndicator(path){
  const indicator=ensureMediaPathIndicator();indicator.dataset.path=path;indicator.setAttribute('aria-description',path);
}

const pathOpenEditor=openEditor;
openEditor=async function(path,label){setMediaPathIndicator(path);return pathOpenEditor(path,label)};

function sizeShowListToEpisodes(){const episodeCount=$('#episode-list')?.children.length||0,rows=Math.max(6,episodeCount),list=$('#show-list');if(list)list.style.setProperty('--show-list-rows',rows)}
const episodeListObserver=new MutationObserver(sizeShowListToEpisodes);if($('#episode-list'))episodeListObserver.observe($('#episode-list'),{childList:true});sizeShowListToEpisodes();
