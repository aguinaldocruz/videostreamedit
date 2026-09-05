let editorBaseline=null,editorObserver=null;

function editorSnapshot(){
  const rows=[...document.querySelectorAll('#stream-content .stream-row')],values={},order={audio:[],subtitle:[]};
  for(const row of rows){
    const key=row.dataset.key;
    values[key]={
      language:row.querySelector('[name=language]').value,
      region:row.querySelector('[name=region]').value,
      title:row.querySelector('[name=title]').value,
      external:row.dataset.external==='true',
      embed:row.querySelector('[name=embed]')?.checked||false,
      removed:row.querySelector('[name=remove]')?.checked||false
    };
    order[row.dataset.codecType].push(key);
  }
  const selected=name=>document.querySelector(`#stream-content [name=${name}]:checked`)?.value||null;
  return{values,order,defaultAudio:selected('default-audio'),forcedAudio:selected('forced-audio'),defaultSubtitle:selected('default-subtitle'),forcedSubtitle:selected('forced-subtitle')};
}

function queuedChangeCount(){
  if(!editorBaseline)return 0;
  const current=editorSnapshot();let count=0;
  for(const[key,value]of Object.entries(current.values)){
    const initial=editorBaseline.values[key];if(!initial)continue;
    if(value.removed){if(!initial.removed)count++;continue}
    if(value.external){if(value.embed!==initial.embed)count++;if(!value.embed)continue}
    for(const field of['language','region','title'])if(value[field]!==initial[field])count++;
  }
  for(const type of['audio','subtitle'])if(current.order[type].join('\n')!==editorBaseline.order[type].join('\n'))count++;
  for(const field of['defaultAudio','forcedAudio','defaultSubtitle','forcedSubtitle'])if(current[field]!==editorBaseline[field])count++;
  return count;
}

function updateQueuedChangeLabels(){
  const count=queuedChangeCount(),apply=$('#stream-form .dialog-actions [type=submit]'),close=$('#stream-form .dialog-actions [data-close-stream]');
  if(!apply||!close)return;
  apply.textContent=count?`Apply ${count} change${count===1?'':'s'}`:'Apply changes';
  apply.disabled=count===0;
  close.textContent=count?'Cancel':'Close';
}

function captureEditorBaseline(){
  editorBaseline=editorSnapshot();const content=$('#stream-content');
  content.addEventListener('input',updateQueuedChangeLabels);content.addEventListener('change',updateQueuedChangeLabels);content.addEventListener('click',updateQueuedChangeLabels);
  if(editorObserver)editorObserver.disconnect();editorObserver=new MutationObserver(updateQueuedChangeLabels);editorObserver.observe(content,{childList:true});
  updateQueuedChangeLabels();
}

const sessionOpenEditor=openEditor;
openEditor=async function(path,label){await sessionOpenEditor(path,label);if(document.querySelector('#stream-content .stream-row'))captureEditorBaseline()};

$('#stream-form').onsubmit=async function(e){
  e.preventDefault();const queued=queuedChangeCount();if(!queued){toast('No changes queued');return}let applySucceeded=false,applyError='';applyProgressBusy=true;setApplyProgress(1,4,'Preparing changes',queuedChangeSummary())
  const rows=[...document.querySelectorAll('.stream-row')],tracks=[],external=[],order=[],remove=[],usedValues=[];
  rows.forEach(r=>{
    if(r.querySelector('[name=remove]').checked)remove.push(r.dataset.key);
    const language=r.querySelector('[name=language]'),region=r.querySelector('[name=region]'),title=r.querySelector('[name=title]'),isExternal=r.dataset.external==='true',removed=remove.includes(r.dataset.key);
    if(isExternal){
      const embed=r.querySelector('[name=embed]').checked;external.push({path:r.dataset.path,embed,language:language.value,region:region.value,title:title.value,forced:false});order.push({source:'external',codec_type:'subtitle',path:r.dataset.path});
      if(embed&&!removed)for(const[field,input]of[['language',language],['region',region],[r.dataset.codecType==='audio'?'title_audio':'title_subtitle',title]])if(input.dataset.dirty==='true'&&input.value.trim())usedValues.push({field,value:input.value.trim()});
    }else{
      const typeIndex=Number(r.dataset.typeIndex),update={codec_type:r.dataset.codecType,type_index:typeIndex};if(language.dataset.dirty==='true'||region.dataset.dirty==='true'){update.language=language.value;update.region=region.value}if(title.dataset.dirty==='true')update.title=title.value;tracks.push(update);order.push({source:'embedded',codec_type:r.dataset.codecType,type_index:typeIndex});
      if(!removed)for(const[field,input]of[['language',language],['region',region],[r.dataset.codecType==='audio'?'title_audio':'title_subtitle',title]])if(input.dataset.dirty==='true'&&input.value.trim())usedValues.push({field,value:input.value.trim()});
    }
  });
  const selected=name=>document.querySelector(`[name=${name}]:checked`)?.value||null,defaults={audio:selected('default-audio'),subtitle:selected('default-subtitle')},forced={audio:selected('forced-audio'),subtitle:selected('forced-subtitle')};
  for(const choice of[defaults.subtitle,forced.subtitle])if(choice?.startsWith('external:')){const item=external.find(x=>`external:${x.path}`===choice);if(item)item.embed=true}
  const button=e.target.querySelector("[type=submit]"),label=$("#selected-file").textContent,path=state.selectedPath;button.disabled=true;button.textContent="Applying…";
  try{
    const filename=validateStreamFilename(),renameQueued=filename!==streamFilenameOriginal,streamQueued=queued-(renameQueued?1:0);let result={warnings:[]},finalPath=path;
    if(streamQueued){setApplyProgress(2,4,"Updating media container",queuedChangeSummary());result=await api("/api/v7/media/edit",{method:"POST",body:JSON.stringify({path,tracks,external_subtitles:external,order,default_audio:defaults.audio,forced_audio:forced.audio,default_subtitle:defaults.subtitle,forced_subtitle:forced.subtitle,remove})})}
    if(renameQueued){setApplyProgress(3,4,"Renaming media",filename);const renamed=await api("/api/v37/media/rename",{method:"POST",body:JSON.stringify({path,filename})});finalPath=renamed.path;adoptRenamedMediaPath(path,finalPath)}
    if(usedValues.length){setApplyProgress(3,4,"Saving reusable values","Recording successfully used metadata values");await offerSavedValues(usedValues)}
    setApplyProgress(4,4,"Refreshing properties","Reading updated streams from the media file");toast(result.warnings.length?result.warnings.join(" "):queued+" change"+(queued===1?"":"s")+" applied",result.warnings.length>0);const indexes=result.operation==='single_remux'?['core','subtitles','previews']:result.subtitle_html_cleaned?['core','subtitles','previews']:['core'];await openEditor(finalPath,label);document.dispatchEvent(new CustomEvent("media-properties-applied",{detail:{path:finalPath,indexes}}));applySucceeded=true;
  }catch(error){applyError=error.message;toast(error.message,true)}finally{if(document.querySelector("#stream-content .stream-row"))updateQueuedChangeLabels();else{button.disabled=false;button.textContent="Apply changes";$("#stream-form .dialog-actions [data-close-stream]").textContent="Close"}applyProgressBusy=false;setApplyProgress(4,4,applySucceeded?"Complete":"Could not complete",applySucceeded?queued+" change"+(queued===1?"":"s")+" applied":applyError)}
};
