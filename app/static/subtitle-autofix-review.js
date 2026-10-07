/* Damage report -> immediate full-text review -> one approved media commit. */
(function(){
  const endpoint='/api/subtitle-autofix';
  const safe=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const dialog=document.createElement('dialog');
  dialog.id='subtitle-autofix-review-dialog';dialog.className='autofix-review';
  dialog.setAttribute('aria-labelledby','autofix-review-title');
  dialog.innerHTML='<header class="dialog-title"><div><h2 id="autofix-review-title">Subtitle autofix</h2><p data-af-media></p></div><span class="autofix-badge" data-af-stage>Choose a rule</span></header><div class="autofix-review-body" data-af-body></div><p class="autofix-review-error error" data-af-error role="alert" hidden></p><footer class="dialog-actions"><div data-af-actions></div><button type="button" data-af-close>Cancel</button></footer>';
  document.body.append(dialog);
  const find=selector=>dialog.querySelector(selector);
  let state={path:'',label:'',review:null,position:0,busy:false,submitted:false};
  const action=()=>state.encoding?'UTF-8 encoding quickfix':'subtitle autofix';
  const error=message=>{find('[data-af-error]').textContent=message;find('[data-af-error]').hidden=!message;};
  const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  async function busy(message,work){
    if(state.busy)return;
    state.busy=true;error('');
    find('[data-af-close]').disabled=true;
    find('[data-af-actions]').querySelectorAll('button').forEach(button=>button.disabled=true);
    window.beginGlobalBusyImmediate?.(message);
    window.setGlobalBusyProgress?.(0,1,message,'Original media is unchanged until approval and execution.');
    try{return await work()}
    catch(exc){error(exc.message||'Subtitle autofix failed');}
    finally{
      state.busy=false;find('[data-af-close]').disabled=false;
      find('[data-af-actions]').querySelectorAll('button').forEach(button=>button.disabled=button.dataset.unavailable==='true');
      window.endGlobalBusyOperation?.();
    }
  }
  async function discard(){
    const id=state.review?.review_id;
    state.review=null;
    if(id)try{await api(`${endpoint}/reviews/${encodeURIComponent(id)}`,{method:'DELETE'})}catch(_error){/* Expiry/restart already discarded it. */}
  }
  dialog.addEventListener('cancel',event=>{event.preventDefault();if(!state.busy)dialog.close()});
  dialog.addEventListener('close',()=>{discard();});
  find('[data-af-close]').onclick=()=>{if(!state.busy)dialog.close();};
  function warningsHtml(){return (state.review?.warnings||[]).length?`<details class="autofix-review-warnings"><summary>Streams left unchanged (${state.review.warnings.length})</summary><ul>${state.review.warnings.map(value=>`<li>${safe(value)}</li>`).join('')}</ul></details>`:'';}
  async function chooseRule(){
    find('[data-af-stage]').textContent='Choose a rule';
    find('[data-af-close]').textContent='Cancel';
    find('[data-af-body]').innerHTML='<p role="status">Reading enabled rules and current subtitle languages…</p>';
    find('[data-af-actions]').replaceChildren();
    const options=await api(`${endpoint}/options`,{method:'POST',body:JSON.stringify({path:state.path})});
    const rules=(options.rules||[]).filter(rule=>rule.matching_streams>0);
    find('[data-af-body]').innerHTML=`<section class="autofix-rule-choice"><h3>Which rule should be applied?</h3><p>Prepare corrected text now, then approve or reject each changed subtitle. No media is modified during review.</p>${rules.length?`<label>Autofix rule<select data-af-rule>${rules.map(rule=>`<option value="${safe(rule.id)}">${safe(rule.name)} · ${rule.matching_streams} matching stream${rule.matching_streams===1?'':'s'}</option>`).join('')}</select></label><p data-af-rule-detail></p>`:'<p class="autofix-review-empty">No enabled rule matches an SRT/SubRip subtitle in this media. Add or enable a matching rule in Setup → Editing → Subtitle autofix.</p>'}<p class="muted">${safe(options.note)}</p></section>`;
    if(!rules.length)return;
    const select=find('[data-af-rule]');
    const describe=()=>{const rule=rules.find(item=>item.id===select.value);find('[data-af-rule-detail]').textContent=`${rule.languages.join(' · ')}${rule.description?' — '+rule.description:''}`;};
    select.onchange=describe;describe();
    find('[data-af-actions]').innerHTML='<button type="button" class="primary" data-af-prepare>Prepare previews now</button>';
    find('[data-af-prepare]').onclick=()=>prepareReview(select.value);
  }
  async function chooseEncoding(){
    find('[data-af-stage]').textContent='UTF-8 quickfix';
    find('[data-af-close]').textContent='Cancel';
    find('[data-af-body]').innerHTML='<section class="autofix-rule-choice"><h3>Normalize legacy subtitle bytes to UTF-8</h3><p>Only SRT/SubRip subtitles safely and reversibly decoded from Windows-1252 are eligible. The words, cue timing and formatting stay unchanged — only the stored encoding changes.</p><p>Review the full text and approve or reject each eligible subtitle. Replacement characters (�), controls, mixed encodings and possible mojibake are refused, not guessed or silently repaired. No media is changed during preview.</p></section>';
    find('[data-af-actions]').innerHTML='<button type="button" class="primary" data-af-prepare>Prepare UTF-8 previews</button>';
    find('[data-af-prepare]').onclick=()=>prepareReview();
  }
  async function prepareReview(rule_id){
    await busy(`Preparing ${action()} previews`,async()=>{
      // getRandomValues also works on the server's ordinary HTTP/LAN URL;
      // randomUUID is restricted to secure contexts in several browsers.
      const operation_id=Array.from(crypto.getRandomValues(new Uint8Array(16)),value=>value.toString(16).padStart(2,'0')).join('');
      let polling=true;
      const poll=(async()=>{
        while(polling){
          try{const progress=await api(`${endpoint}/reviews/${operation_id}/progress`);if(polling)window.setGlobalBusyProgress?.(progress.step,progress.total,`Preparing ${action()} previews`,progress.message)}catch(_error){}
          await sleep(450);
        }
      })();
      try{state.review=await api(`${endpoint}/${state.encoding?'encoding/prepare':'prepare'}`,{method:'POST',body:JSON.stringify({path:state.path,...(state.encoding?{}:{rule_id}),operation_id})});state.position=0;}
      finally{polling=false;await poll;}
      await reviewNext();
    });
  }
  async function reviewNext(){
    const review=state.review;
    if(state.position>=review.streams.length){summary();return;}
    const selected=review.streams[state.position];
    window.setGlobalBusyProgress?.(state.position,review.streams.length,'Loading full corrected subtitle',selected.label);
    const stream=await api(`${endpoint}/reviews/${review.review_id}/streams/${selected.id}`);
    find('[data-af-stage]').textContent=`Review ${state.position+1} of ${review.streams.length}`;
    const change=state.encoding?`${safe(stream.source_encoding)} → UTF-8 · text unchanged`:`${stream.replacements} replacement${stream.replacements===1?'':'s'}`;
    find('[data-af-body]').innerHTML=`<section class="autofix-stream-review"><div class="autofix-stream-heading"><div><h3>${safe(stream.label)}${stream.title?' · '+safe(stream.title):''}</h3><p>${safe(stream.language)} · ${safe(review.rule)} · ${change} · ${stream.cached?'Read from valid cache':'Extracted for this review'}</p></div><label><input type="checkbox" data-af-compare> Show original too</label></div><p class="muted">${state.encoding?'Both panes intentionally show the same decoded text. Approval changes only the source bytes to UTF-8, not its wording.':'Full corrected subtitle below — nothing is truncated.'} Review it before approving. Cue numbers, timing and existing presentation tags are preserved.</p><div class="autofix-review-texts"><section data-af-original-pane hidden><h4>Original${state.encoding?' (decoded legacy text)':''}</h4><pre data-af-original tabindex="0"></pre></section><section><h4>${state.encoding?'UTF-8 subtitle':'Corrected subtitle'}</h4><pre data-af-fixed tabindex="0"></pre></section></div></section>`;
    find('[data-af-original]').textContent=stream.original;
    find('[data-af-fixed]').textContent=stream.text;
    find('[data-af-compare]').onchange=event=>{find('[data-af-original-pane]').hidden=!event.target.checked;dialog.querySelector('.autofix-review-texts').classList.toggle('comparing',event.target.checked);};
    find('[data-af-actions]').innerHTML='<button type="button" class="primary" data-af-approve>Approve this subtitle</button><button type="button" data-af-reject>Reject this subtitle</button>';
    const decide=approve=>busy('Recording subtitle review',async()=>{
      state.review=await api(`${endpoint}/reviews/${review.review_id}/decision`,{method:'POST',body:JSON.stringify({stream_id:selected.id,approve})});
      state.position++;await reviewNext();
    });
    find('[data-af-approve]').onclick=()=>decide(true);
    find('[data-af-reject]').onclick=()=>decide(false);
  }
  function summary(){
    const review=state.review;
    const approved=review.streams.filter(stream=>stream.decision===true),rejected=review.streams.length-approved.length;
    const embedded=approved.filter(stream=>stream.source==='embedded').length;
    find('[data-af-stage]').textContent='Review complete';
    find('[data-af-body]').innerHTML=`<section class="autofix-review-summary"><h3>${approved.length?'How should the approved subtitles be replaced?':'No subtitle replacements approved'}</h3><p>${approved.length} approved · ${rejected} rejected · ${review.unchanged||0} ${state.encoding?'streams already UTF-8 or not eligible':'matching streams without changes'}.</p>${approved.length?`<ul>${approved.map(stream=>`<li>${safe(stream.label)} · ${safe(stream.language)} · ${state.encoding?'Normalize to UTF-8':`${stream.replacements} replacements`}</li>`).join('')}</ul><p>${embedded?'All approved embedded subtitles are replaced together in one verified remux.':'Only the approved external SRT files are replaced; no container remux is needed.'} Rejected streams, languages, titles, flags and stream order stay unchanged.</p><p class="muted">Apply now waits for the current safe media operation if needed, then shows execution progress. Queue returns you to the report immediately. A changed source is refused in either mode.</p>`:`<p>The original media is unchanged. ${state.encoding?'No safe legacy-encoding conversion was approved or available.':'You can choose another rule or close this review.'}</p>`}${warningsHtml()}</section>`;
    find('[data-af-actions]').innerHTML=(approved.length?'<button type="button" class="primary" data-af-apply>Apply now</button><button type="button" data-af-queue>Queue replacement</button>':'')+`<button type="button" data-af-restart>${state.encoding?'Review again':'Choose another rule'}</button>`;
    find('[data-af-restart]').onclick=()=>busy(`Preparing ${action()}`,async()=>{await discard();await (state.encoding?chooseEncoding():chooseRule());});
    find('[data-af-apply]')?.addEventListener('click',()=>submit('now'));
    find('[data-af-queue]')?.addEventListener('click',()=>submit('queue'));
  }
  async function waitTask(task_id){
    while(true){
      const result=await api('/api/v65/queue/status',{method:'POST',body:JSON.stringify({task_ids:[task_id]})});
      const task=(result.items||[]).find(item=>Number(item.id)===Number(task_id));
      if(!task)throw Error(`Task #${task_id} is no longer available. Check Task Queue before submitting another replacement.`);
      window.setGlobalBusyProgress?.(task.progress_current,task.progress_total||8,`Applying approved ${action()}`,task.status==='pending'?`Task #${task_id} is waiting for a safe execution slot; approved changes are saved.`:task.progress_message);
      if(task.status==='succeeded')return task;
      if(['failed','cancelled'].includes(task.status))throw Error(`Task #${task_id} ${task.status}: ${task.error||task.progress_message}. Review it in Setup → Tasks → Task Queue.`);
      await sleep(650);
    }
  }
  async function submit(mode){
    await busy(`${mode==='now'?'Applying':'Queueing'} approved ${action()}`,async()=>{
      const result=await api(`${endpoint}/reviews/${state.review.review_id}/apply`,{method:'POST',body:JSON.stringify({mode})});
      state.submitted=true;
      const eventDetail={path:state.path,task_id:result.task_id};
      document.dispatchEvent(new CustomEvent('media-properties-queued',{detail:eventDetail}));
      // Submission is already durable. Remove commit controls immediately,
      // including when later status polling fails; never offer it twice.
      find('[data-af-actions]').replaceChildren();find('[data-af-close]').textContent='Close';
      find('[data-af-stage]').textContent=`Task #${result.task_id}`;
      find('[data-af-body]').innerHTML=`<section class="autofix-review-summary"><h3>Replacement ${mode==='queue'?'queued':'submitted'}</h3><p data-af-result>Task #${result.task_id} contains only your approved subtitle replacements.</p></section>`;
      if(mode==='now'){
        await waitTask(result.task_id);
        document.dispatchEvent(new CustomEvent('media-properties-applied',{detail:eventDetail}));
        find('[data-af-stage]').textContent='Completed';
        find('[data-af-result]').textContent=`Task #${result.task_id} completed. Media and subtitle cache were updated; fresh inspection will decide whether it still belongs in this report.`;
      }else{
        toast(`Approved ${action()} queued as task #${result.task_id}`);
        dialog.close();
      }
    });
  }
  async function openReview(path,label,encoding){
    if(state.busy)return;
    if(dialog.open)return;
    await discard();
    state={path,label:label||path,review:null,position:0,busy:false,submitted:false,encoding};
    find('#autofix-review-title').textContent=encoding?'Subtitle UTF-8 quickfix':'Subtitle autofix';
    find('[data-af-media]').textContent=state.label;error('');dialog.showModal();
    await busy(encoding?'Preparing UTF-8 quickfix review':'Loading subtitle autofix rules',encoding?chooseEncoding:chooseRule);
  }
  window.openSubtitleAutofixReview=(path,label)=>openReview(path,label,false);
  window.openSubtitleEncodingReview=(path,label)=>openReview(path,label,true);
})();
