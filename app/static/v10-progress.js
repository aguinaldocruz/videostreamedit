let applyProgressBusy=false;

function ensureApplyProgress(){
  let status=$('#apply-progress');
  if(status)return status;
  status=document.createElement('div');status.id='apply-progress';status.className='apply-progress';status.setAttribute('role','status');status.setAttribute('aria-live','polite');
  const close=$('#stream-form .dialog-actions [data-close-stream]');close.parentNode.insertBefore(status,close);
  return status;
}

function setApplyProgress(step,total,message,detail=''){
  const status=ensureApplyProgress(),progress=total?`Step ${step} of ${total}`:'';
  status.innerHTML=`<strong>${esc(message)}</strong>${progress?`<span>${esc(progress)}</span>`:''}${detail?`<small>${esc(detail)}</small>`:''}`;
}

function queuedChangeSummary(){
  if(!editorBaseline)return'';const current=editorSnapshot(),groups=[];let metadata=0,remove=0,embed=0,tags=0,orders=0;
  for(const[key,value]of Object.entries(current.values)){const initial=editorBaseline.values[key];if(!initial)continue;if(value.removed){if(!initial.removed)remove++;continue}if(value.external&&value.embed!==initial.embed)embed++;if(!value.external||value.embed)for(const field of['language','region','title'])if(value[field]!==initial[field])metadata++}
  for(const type of['audio','subtitle'])if(current.order[type].join('\n')!==editorBaseline.order[type].join('\n'))orders++;
  for(const field of['defaultAudio','forcedAudio','defaultSubtitle','forcedSubtitle'])if(current[field]!==editorBaseline[field])tags++;
  if(metadata)groups.push(`${metadata} metadata`);if(tags)groups.push(`${tags} tag`);if(orders)groups.push(`${orders} reorder`);if(embed)groups.push(`${embed} embed`);if(remove)groups.push(`${remove} removal`);return groups.join(' · ');
}

const progressUpdateQueuedChangeLabels=updateQueuedChangeLabels;
updateQueuedChangeLabels=function(){progressUpdateQueuedChangeLabels();if(applyProgressBusy)return;const count=queuedChangeCount();setApplyProgress(0,0,count?`${count} change${count===1?'':'s'} queued`:'Ready',count?queuedChangeSummary():'No pending changes')};
ensureApplyProgress();setApplyProgress(0,0,'Ready','No pending changes');
