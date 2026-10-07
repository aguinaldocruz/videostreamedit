/* Cached damage evidence and explicit, guarded repair actions. */
window.renderDamageSummary = function(container, report, kind, dialog) {
  const summary=report.damage_summary;
  if(!summary?.reasons?.length)return;
  const section=document.createElement('details');
  section.className='damage-analysis';section.open=true;
  section.innerHTML=`<summary><strong>Reasons & examples</strong><span>${summary.reason_count} reasons · ${summary.stream_count} subtitle streams</span></summary>
    <p>Top 30 reasons, ranked by affected subtitle streams in this entire report. A stream may have several reasons. Expand a reason for its top 30 recurring examples from cached text. These findings are clues for review, not proof of damage.</p>
    <div class="damage-reasons">${summary.reasons.map((reason,index)=>`<details class="damage-reason" data-damage-reason="${index}"><summary><span class="damage-rank">${reason.rank}</span><strong>${esc(reason.reason)}</strong><span>${reason.stream_count} streams · ${reason.media_count} media</span></summary><p>${esc(reason.note)}</p><div class="damage-evidence" aria-live="polite"></div></details>`).join('')}</div>`;
  container.prepend(section);
  const nav=uniqueReportNavigation((report.items||[]).flatMap(item=>item.episodes?.length
    ?item.episodes.map(ep=>({path:ep.path,label:`${item.title} · ${ep.episode}`}))
    :(item.paths||[]).map(path=>({path,label:item.title}))));
  const streamLabel=row=>row.source==='external'?'External subtitle':`Subtitle ${Number(row.type_index)+1}`;
  let bulkQueueing=false;
  function wireEncodingBulk(root){
    root.querySelectorAll('[data-damage-encoding-bulk]').forEach(button=>button.onclick=async()=>{
      if(bulkQueueing)return;
      bulkQueueing=true;button.disabled=true;
      const originalLabel=button.textContent,status=button.parentElement.querySelector('[data-encoding-bulk-status]');
      button.textContent='Queueing…';
      window.beginGlobalBusyImmediate?.('Queueing Windows-1252 UTF-8 quickfixes');
      try{
        window.setGlobalBusyProgress?.(0,2,'Preparing the encoding quickfix queue','Selecting the entire Windows-1252 group; no individual approvals');
        const result=await api('/api/subtitle-autofix/encoding/queue-report',{method:'POST',body:JSON.stringify({kind})});
        if(result.preflight_id){
          window.setGlobalBusyProgress?.(2,2,'UTF-8 quickfix requests accepted','Validation and one replacement per media continue in the task queue');
          const message=`Request #${result.preflight_id} · ${result.media_count} media · ${result.stream_count} subtitles · validation and quickfix queued without individual approvals`;
          button.textContent='Queued';if(status)status.textContent=message;
          toast(message);
          window.showReportPreflight?.(dialog,[result.preflight_id]);
          window.refreshActiveReportAfterQueue?.('damaged');
        }else{
          button.textContent='No eligible entries';if(status)status.textContent='No available Windows-1252 SRT/SubRip entries remain in the current report.';
        }
      }catch(error){
        button.disabled=false;button.textContent=originalLabel;
        if(status)status.textContent=error.message;toast(error.message,true);
      }finally{bulkQueueing=false;window.endGlobalBusyOperation?.()}
    });
  }
  function wireLocations(root,rows){
    root.querySelectorAll('[data-damage-example-edit]').forEach(button=>button.onclick=()=>{
      const row=rows[Number(button.dataset.damageExampleEdit)];
      if(row?.path)openReportMediaEditor(row.path,row.label,nav,dialog);
    });
    root.querySelectorAll('[data-damage-example-autofix]').forEach(button=>button.onclick=()=>{
      const row=rows[Number(button.dataset.damageExampleAutofix)];
      if(row?.path)window.openSubtitleAutofixReview?.(row.path,row.label);
    });
    root.querySelectorAll('[data-damage-example-encoding]').forEach(button=>button.onclick=()=>{
      const row=rows[Number(button.dataset.damageExampleEncoding)];
      if(row?.path)window.openSubtitleEncodingReview?.(row.path,row.label);
    });
  }
  section.querySelectorAll('[data-damage-reason]').forEach(details=>{
    let loaded=false,loading=false;
    details.addEventListener('toggle',async()=>{
      if(!details.open||loaded||loading)return;
      loading=true;
      const reason=summary.reasons[Number(details.dataset.damageReason)].reason;
      const output=details.querySelector('.damage-evidence');
      output.innerHTML='<p role="status">Reading cached subtitle evidence… No media extraction is needed.</p>';
      try{
        const data=await api(`/api/v19/reports/damaged-subtitles/examples?kind=${encodeURIComponent(kind)}&reason=${encodeURIComponent(reason)}`);
        if(!section.isConnected)return;
        const rows=[];
        const bulkButton=example=>reason==='Non-UTF-8 source bytes'&&example.example==='Windows-1252 (inferred)'&&Number(example.quickfix_media_count)>0
          ?`<div class="damage-encoding-bulk"><button type="button" class="vse-btn report-item-action" data-damage-encoding-bulk>Queue all UTF-8 quickfixes (${Number(example.quickfix_media_count)} media)</button><small>No individual approvals. All eligible entries in this encoding group, not only the examples below. Unsafe inputs and changed media are refused.</small><p role="status" data-encoding-bulk-status></p></div>`:'';
        const location=row=>{
          const index=rows.push(row)-1;
          return `<div class="damage-location"><div><strong>${esc(row.label)}</strong><small>${esc(streamLabel(row))}${row.line?` · line ${row.line}`:''}${row.cue?` · cue ${esc(row.cue)}`:''}${row.timing?` · ${esc(row.timing)}`:''}</small>${row.text!==undefined?`<pre>${esc(row.text)}</pre>`:''}</div><span class="damage-media-actions"><button type="button" class="vse-btn report-item-action" data-damage-example-edit="${index}">Stream properties</button><button type="button" class="vse-btn report-item-action" data-damage-example-autofix="${index}">Autofix</button>${reason==='Non-UTF-8 source bytes'?`<button type="button" class="vse-btn report-item-action" data-damage-example-encoding="${index}">UTF-8 quickfix</button>`:''}</span></div>`;
        };
        output.innerHTML=`<p class="damage-coverage">${data.cached_streams} of ${data.reported_streams} reported streams checked from cache · ${data.unavailable_streams} without a usable cache · ${data.unreproduced_count} stored findings not reproduced</p>
          <p>Top ${Math.min(30,data.distinct_examples)} of ${data.distinct_examples} distinct examples, ranked by occurrences (encoding and empty-track reasons count streams). Up to three stream locations per example. Line numbers refer to cached text.</p>
          ${(data.examples||[]).map(example=>`<details class="damage-example"><summary><span class="damage-rank">${example.rank}</span><code>${esc(example.example)}</code><span>${example.occurrences} occurrences · ${example.stream_count} streams · ${example.media_count} media</span></summary>${bulkButton(example)}${example.locations.map(location).join('')}</details>`).join('')||'<p>No matching examples are available in the current cache.</p>'}
          ${data.unreproduced_count?`<details class="damage-unreproduced"><summary>Review stored findings not reproduced (${data.unreproduced_count})</summary><p>The current cached text does not trigger this recorded reason. This may indicate stale inspection data; it does not automatically prove the subtitle is correct. Showing up to 30 streams.</p>${(data.unreproduced||[]).map(location).join('')}</details>`:''}`;
        wireLocations(output,rows);wireEncodingBulk(output);loaded=true;
      }catch(error){if(section.isConnected){output.replaceChildren();const message=document.createElement('p');message.className='error';message.textContent=error.message||'Unable to load cached examples.';const retry=document.createElement('button');retry.type='button';retry.textContent='Retry';retry.onclick=()=>{details.open=false;requestAnimationFrame(()=>{details.open=true})};output.append(message,retry)}}
      finally{loading=false}
    });
  });
};
