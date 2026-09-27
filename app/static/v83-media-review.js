(function(){
  // Loaded globally; keep one modal instance and never leak it into Setup.
  if(document.querySelector("#media-review-dialog"))return;
  const actions=$('#stream-form .dialog-actions'),close=actions?.querySelector('[data-close-stream]');
  if(!actions||!close)return;
  const review=document.createElement('button');review.type='button';review.id='review-media';review.textContent='Review media';review.title='Watch this media with the selected audio and subtitle';close.insertAdjacentElement('beforebegin',review);
  const evaluate=document.createElement('button');evaluate.type='button';evaluate.id='evaluate-forced';evaluate.textContent='Evaluate';evaluate.title='Analyze subtitle language, forced likelihood, and SDH';evaluate.hidden=false;close.insertAdjacentElement('beforebegin',evaluate);
  document.body.insertAdjacentHTML('beforeend','<dialog id="forced-evaluation-dialog" class="forced-evaluation-dialog"><div class="dialog-title"><div><h2>Subtitle evaluation</h2><p id="forced-evaluation-summary"></p></div><button type="button" class="icon-close" data-close-forced-evaluation aria-label="Close">×</button></div><div class="forced-evaluation-header"><span>Subtitle</span><span>Force analysis</span><span>SDH certainty</span></div><div id="forced-evaluation-results"></div><div class="dialog-actions"><button type="button" data-close-forced-evaluation>Close</button></div></dialog>');
  document.body.insertAdjacentHTML("beforeend", '<dialog id="media-review-dialog"></dialog>');
  const forcedDialog=$('#forced-evaluation-dialog'),forcedSummary=$('#forced-evaluation-summary'),forcedResults=$('#forced-evaluation-results');
  function updateForcedAvailability(){evaluate.hidden=false;}
  window.updateForcedEvaluateAvailability=updateForcedAvailability;
  new MutationObserver(updateForcedAvailability).observe($('#stream-content'),{childList:true,subtree:true});
  forcedDialog.querySelectorAll('[data-close-forced-evaluation]').forEach(button=>button.onclick=()=>forcedDialog.close());
  forcedDialog.addEventListener('cancel',event=>{event.preventDefault();forcedDialog.close()});
  function arrangeEvaluationRows(){forcedResults.querySelectorAll(".forced-evaluation-item").forEach(article=>{const grid=article.querySelector(".forced-evaluation-columns"),strong=grid?.querySelector("strong"),small=article.querySelector("small"),force=grid?.querySelector(".forced-certainty"),sdh=grid?.querySelector(".forced-sdh-certainty");if(!grid||!strong)return;force?.remove();if(small){const cell=document.createElement("div");cell.className="forced-subtitle-cell";cell.append(strong);cell.append(small);grid.prepend(cell)}if(sdh){const pct=Number((sdh.textContent.match(/([0-9.]+)%/)||[])[1]||0);sdh.classList.toggle("recommendation-likely",pct>=80);sdh.classList.toggle("recommendation-uncertain",pct>=60&&pct<80);sdh.classList.toggle("recommendation-unsupported",pct<60)}});}
  function forcedLabel(item,index){return `${item.source==='external'?'External subtitle':`Subtitle ${Number(item.type_index)+1}`} · ${item.language||'language unset'}${item.title?` · ${item.title}`:''}`}
  function refreshEvaluationDots(items){items.forEach(item=>{const row=[...document.querySelectorAll("#stream-content .stream-row")].find(candidate=>candidate.dataset.codecType==="subtitle"&&(item.source==="external"?candidate.dataset.external==="true"&&candidate.dataset.path===item.path:candidate.dataset.external!=="true"&&Number(candidate.dataset.typeIndex)===Number(item.type_index)));const dot=row?.querySelector("[data-stream-detect]");if(!dot)return;const confidence=Number(item.language_confidence||0),detected=item.detected_language||"",metadataLanguage=dot.dataset.metadataLanguage||"",metadataRegion=dot.dataset.metadataRegion||"";const color=detected?(typeof streamDetectionColor==="function"?streamDetectionColor(detected,metadataLanguage,metadataRegion,confidence):(confidence>=.6?"#d1a84b":"#7d8995")):"#7d8995";dot.style.setProperty("--detection-color",color);dot.classList.toggle("detected",Boolean(detected));dot.classList.add("evaluated");dot.title=detected?"Detected "+detected+" with "+(confidence*100).toFixed(1)+"% confidence"+(item.language_evidence?" — "+item.language_evidence:""):"No confident common language detected"})}
  async function evaluateForced(){
    const subtitles=rows('subtitle');updateForcedAvailability();
    if(typeof beginGlobalBusy==='function')beginGlobalBusy('Evaluating subtitles');
    if(typeof setGlobalBusyProgress==='function')setGlobalBusyProgress(0,3,'Evaluating subtitles','Reading all subtitle streams in this media');
    evaluate.disabled=true;
    try{
      if(typeof setGlobalBusyProgress==='function')setGlobalBusyProgress(1,3,'Analyzing subtitle coverage','Measuring timing, density, and signs or foreign-dialogue cues');
      const result=await api('/api/v19/stream/evaluate-forced',{method:'POST',body:JSON.stringify({path:state.selectedPath})});
      if(typeof setGlobalBusyProgress==='function')setGlobalBusyProgress(3,3,'Subtitle evaluation complete','Recommendations are non-destructive and require user confirmation');
      refreshEvaluationDots(result.subtitles);forcedSummary.textContent=`${result.subtitle_count} subtitle stream${result.subtitle_count===1?'':'s'} analyzed · ${result.analyzed} text stream${result.analyzed===1?'':'s'} available for content analysis`;
      forcedResults.innerHTML=`<p class="muted">${esc(result.message)}</p>${result.subtitles.map((item,index)=>`<article class="forced-evaluation-item"><div class="forced-evaluation-columns"><strong>${esc(forcedLabel(item,index))}</strong><span class="forced-recommendation recommendation-${item.recommendation.startsWith('Likely')?'likely':item.recommendation.startsWith('Cannot')?'unsupported':'uncertain'}">${esc(item.recommendation)}</span><span class="forced-certainty">Force ${Number(item.score)*100}%</span><span class="forced-sdh-certainty">SDH ${item.sdh_label||"Not evaluated"} ${Math.round(Number(item.sdh_confidence||0)*100)}%</span></div><small>${item.text_available?`Confidence ${(Number(item.score)*100).toFixed(0)}% · ${item.cues} cues · ${(Number(item.coverage)*100).toFixed(1)}% timeline coverage · ${Number(item.density).toFixed(1)} cues/min`:'Content could not be read automatically; metadata and filename remain the authority.'}${item.forced?' · Currently Forced':''}</small></article>`).join('')||'<p>No subtitle streams were found.</p>'}`;
      arrangeEvaluationRows();forcedDialog.showModal();
    }catch(error){forcedSummary.textContent='Evaluation failed';forcedResults.innerHTML=`<p class="no-streams error">${esc(error.message)}</p>`;arrangeEvaluationRows();forcedDialog.showModal();if(typeof setGlobalBusyProgress==='function')setGlobalBusyProgress(3,3,'Subtitle evaluation failed',error.message)}
    finally{evaluate.disabled=false;updateForcedAvailability();if(typeof endGlobalBusy==='function')endGlobalBusy()}
  }
  evaluate.onclick=evaluateForced;
  function rows(type){return[...document.querySelectorAll('#stream-content .stream-row')].filter(row=>row.dataset.codecType===type&&!row.querySelector('[name=remove]')?.checked)}
  window.openMediaReviewForStream=row=>window.reviewPlayerOpen?.(row);
  document.addEventListener("click",event=>{const button=event.target.closest?.("[data-review-stream]");if(button){event.preventDefault();event.stopPropagation();window.reviewPlayerOpen?.(button.closest(".stream-row"));}});
})();
