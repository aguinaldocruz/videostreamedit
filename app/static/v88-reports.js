async function reportRequest(url){
  const epoch=window.reportEpoch||0;
  try{
    const result=await api(url);
    if(epoch!==(window.reportEpoch||0))throw Object.assign(new Error('Report changed'),{reportStale:true});
    return result;
  }catch(error){
    if(epoch!==(window.reportEpoch||0))error.reportStale=true;
    throw error;
  }
}
function openReportMediaEditor(path,label,navigationItems,reportDialog,onReturn){
  if(!path||typeof openEditor!=='function')return;
  const previous=window.captureMediaNavigationContext?.();
  const items=uniqueReportNavigation((navigationItems?.length?navigationItems:[{path,label:label||path}]).map(item=>({...item,label:item.label||item.episode||item.title||item.path})));
  const index=items.findIndex(item=>item.path===path);
  if(items.length&&window.setMediaNavigationContext)window.setMediaNavigationContext(items,index<0?0:index,'report');
  const editorDialog=document.querySelector('#stream-dialog');
  const reportView=window.captureReportView?.();
  const restore=()=>{
    window.restoreMediaNavigationContext?.(previous);
    if(reportDialog&&!reportDialog.open){
      try{reportDialog.showModal()}catch(_error){}
      if(window.resumeReportView)window.resumeReportView(reportView);
      else if(typeof onReturn==='function')onReturn();
    }
  };
  editorDialog?.addEventListener('close',restore,{once:true});
  if(reportDialog?.open)reportDialog.close();
  openEditor(path,label||path);
}
function uniqueReportNavigation(items){
  const seen=new Set();
  return (items||[]).filter(item=>item?.path&&!seen.has(item.path)&&seen.add(item.path));
}
function reportConfidenceColor(value){const n=Math.max(60,Math.min(100,Number(value)*100));return `hsl(${Math.round(48-(n-60)*0.9)} 90% 55%)`}
function reportConfidenceDot(value,metadata,detected,kind='subtitle',noConfidence=false,metadataRegion=''){if(kind==='audio'&&window.voiceDetectionEnabled===false)return '';const uncertain=kind!=='audio'&&noConfidence?'<span class="language-detection-no-confidence" title="Subtitle language could not be determined confidently; text may be empty, damaged, advertising, or unsupported">×</span>':'';if(value==null||Number(value)<.6)return uncertain;const matches=typeof streamDetectionMatches==='function'&&streamDetectionMatches(detected,metadata,metadataRegion);if(matches)return uncertain;const pct=(Number(value)*100).toFixed(1);const color=typeof streamDetectionColor==='function'?streamDetectionColor(detected,metadata,metadataRegion,value):reportConfidenceColor(value);const label=kind==='audio'?'Voice language mismatch detected':'Subtitle language metadata mismatch detected';return uncertain+'<span class="language-detection-dot" style="--detection-color:'+color+'" title="'+label+' with '+pct+'% confidence"></span>'}
window.commonDetectionLanguages=['pt','pt-BR','en'];
window.showReportPreflight=async function(dialog,ids){
  const values=(ids||[]).map(Number).filter(Boolean);
  const panel=dialog?.querySelector('[data-report-preflight]'),content=dialog?.querySelector('[data-report-preflight-content]');
  if(!panel||!content||!values.length)return;
  panel.hidden=false; content.textContent='Loading validation status…';
  const render=items=>{content.innerHTML=items.map(item=>{const result=item.result||{};const execution=result.execution||{};const rows=(result.items||[]).slice(0,12).map(row=>`<li><strong>${esc(row.decision||'pending')}</strong> · ${esc(row.path||'')} ${row.reason?`· ${esc(row.reason)}`:''}</li>`).join('');return `<div class="report-preflight-request"><strong>Request #${item.id}</strong> · ${esc(item.operation_type)} · ${esc(item.status)}${result.approved!=null?` · ${result.approved} approved, ${result.skipped||0} skipped, ${result.invalid||0} invalid`:''}${execution.queued!=null?` · ${execution.queued} child task${execution.queued===1?'':'s'} created`:''}${rows?`<ul>${rows}</ul>`:''}</div>`}).join('')};
  for(let attempt=0;attempt<8;attempt++){try{const statuses=await Promise.all(values.map(id=>api('/api/v89/preflight/'+encodeURIComponent(id))));render(statuses);if(statuses.some(item=>item.status==='pending'||item.status==='running'))await new Promise(resolve=>setTimeout(resolve,750));else return}catch(error){if(error?.reportStale)return;content.textContent=error.message;return}}
};
async function waitReportPreflight(requestId){
  for(let attempt=0;attempt<600;attempt++){
    const status=await api('/api/v89/preflight/'+encodeURIComponent(requestId));
    if(status.status==='pending'||status.status==='running'){await new Promise(resolve=>setTimeout(resolve,500));continue}
    if(status.status!=='approved')throw new Error(status.error||status.result?.reason||'Validation did not approve the requested report action');
    const ids=(status.result?.execution?.task_ids||[]).map(Number).filter(Boolean);
    if(ids.length&&typeof waitForGlobalTasks==='function')return await waitForGlobalTasks(ids);
    return {succeeded:Number(status.result?.approved||0),failed:0};
  }
  throw new Error('Report validation is taking too long; the request remains queued');
}
async function loadLanguageDetectionSettings(){try{const result=await api('/api/v79/language-detection/settings');window.commonDetectionLanguages=result.common_languages||window.commonDetectionLanguages;window.renderCommonLanguageEditor?.(window.commonDetectionLanguages)}catch(error){if(error?.reportStale)return;const status=document.querySelector('#language-detection-status');if(status)status.textContent=error.message}}
function wireLanguageDetectionSettings(){const input=document.querySelector('#language-detection-common'),button=document.querySelector('#language-detection-save'),status=document.querySelector('#language-detection-status');if(!input||!button||button.dataset.ready)return;button.dataset.ready='true';let values=[];const list=document.createElement('div');list.className='common-language-list';input.parentElement.before(list);input.placeholder='Add a language, e.g. pt-BR';input.setAttribute('aria-label','Add a common detection language');const add=document.createElement('button');add.type='button';add.textContent='Add language';input.after(add);
  function render(next){values=[...new Map((next||[]).map(value=>[String(value).toLowerCase(),String(value).trim()]).filter(([key,value])=>key&&value)).values()];list.innerHTML=values.length?values.map((value,index)=>`<span class="common-language-chip">${esc(value)} <button type="button" data-language-remove="${index}" aria-label="Remove ${attr(value)}">×</button></span>`).join(''):'<p class="muted">No common languages selected.</p>';list.querySelectorAll('[data-language-remove]').forEach(control=>control.onclick=()=>{values.splice(Number(control.dataset.languageRemove),1);render(values);status.textContent='Unsaved changes'})}
  window.renderCommonLanguageEditor=render;
  function addValue(){const value=input.value.trim();if(!value)return;render([...values,value]);input.value='';status.textContent='Unsaved changes'}
  add.onclick=addValue;input.onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();addValue()}};
  button.onclick=async()=>{if(input.value.trim())addValue();button.disabled=true;try{const result=await api('/api/v79/language-detection/settings',{method:'PUT',body:JSON.stringify({common_languages:values})});window.commonDetectionLanguages=result.common_languages;render(result.common_languages);status.textContent='Saved. New subtitle inspection jobs will use this list.';toast('Common detection languages saved')}catch(error){if(error?.reportStale)return;status.textContent=error.message;toast(error.message,true)}finally{button.disabled=false}};
  loadLanguageDetectionSettings()}
wireLanguageDetectionSettings();
(function(){
  const panel=document.querySelector('#subtitle-detection-health');
  if(!panel)return;
  async function load(){
    try{
      const result=await api('/api/v80/setup/index/subtitles/status');
      const stream=result.stream_status||{complete:result.indexed||0},queue=result.queue||{pending:result.queued||0,running:result.running?1:0};
      const labels=[`ready ${stream.complete||0}`,`mismatch ${stream.mismatch||0}`,`no confidence ${stream.no_confidence||0}`,`unreadable ${stream.unreadable||0}`,`skipped ${stream.skipped_non_common||0}`];
      const pending=(queue.pending||0)+(queue.running||0);
      const stale=result.stale_results||{};
      const staleLabel=Number(stale.media||0)>0?` · ${Number(stale.media)} media stale after detector update`:'';
      panel.innerHTML=`<strong>Subtitle inspection health</strong><span>Detector v${esc(String(result.detector_version||''))} · ${labels.join(' · ')} · queue ${pending} pending/running${result.eta_seconds?` · ETA ${Math.ceil(Number(result.eta_seconds)/60)} min`:''}${staleLabel}${result.last_checked_at?` · last checked ${esc(String(result.last_checked_at))}`:''}</span>`;
    }catch(error){if(error?.reportStale)return;panel.innerHTML=`<strong>Subtitle inspection health</strong><span class="error">${esc(error.message)}</span>`}
  }
  load();
  window.setInterval(load,30000);
})();
(function(){
  const full=document.querySelector('#language-detection-queue-full'), incremental=document.querySelector('#language-detection-queue-incremental'), enabled=document.querySelector('#language-detection-incremental'), status=document.querySelector('#language-detection-queue-status');
  if(!full||!incremental)return;
  async function load(){try{const result=await api('/api/v79/language-detection/queue-settings');if(enabled)enabled.checked=result.incremental_enabled;status.textContent=result.incremental_enabled?'Changed-media detection is enabled.':'Changed-media detection is disabled.'}catch(error){if(error?.reportStale)return;status.textContent=error.message}}
  async function run(mode){if(mode==='full'&&!confirm('Clear existing language/voice detection results and queue every movie and episode?'))return;full.disabled=true;incremental.disabled=true;try{const result=await api('/api/v79/language-detection/queue',{method:'POST',body:JSON.stringify({mode})});if(enabled)enabled.checked=result.incremental_enabled;status.textContent=mode==='full'?`Queued ${result.media} media: ${result.subtitle_queued} subtitle and ${result.voice_queued} voice checks.`:'Changed-media detection enabled for future media changes.';toast(status.textContent)}catch(error){if(error?.reportStale)return;status.textContent=error.message;toast(error.message,true)}finally{full.disabled=false;incremental.disabled=false}}
  full.onclick=()=>run('full'); incremental.onclick=()=>run('incremental'); enabled?.addEventListener('change',()=>{if(enabled.checked)run('incremental');else api('/api/v79/language-detection/queue',{method:'POST',body:JSON.stringify({mode:'disable'})}).then(()=>{status.textContent='Changed-media detection disabled for future media changes.'}).catch(error=>{if(error?.reportStale)return;enabled.checked=true;status.textContent=error.message;toast(error.message,true)})}); load();
})();
(function(){
  const list=document.querySelector('#forced-report-exclusion-list'),input=document.querySelector('#forced-report-exclusion-new'),addButton=document.querySelector('#forced-report-exclusion-add'),status=document.querySelector('#forced-report-exclusions-status');
  if(!list||!input||!addButton)return;
  let names=[];
  function render(){list.innerHTML=names.length?names.map((name,index)=>`<div class="forced-exclusion-row" data-exclusion-index="${index}"><input type="text" value="${attr(name)}" aria-label="Excluded track name"><button type="button" data-exclusion-save>Save</button><button type="button" class="danger" data-exclusion-delete>Remove</button></div>`).join(''):'<p class="muted">No excluded track names.</p>';list.querySelectorAll('.forced-exclusion-row').forEach(row=>{const index=Number(row.dataset.exclusionIndex),field=row.querySelector('input');row.querySelector('[data-exclusion-save]').onclick=()=>{const next=names.slice();next[index]=field.value.trim();persist(next,'Track name updated')};row.querySelector('[data-exclusion-delete]').onclick=()=>persist(names.filter((_,i)=>i!==index),'Track name removed')})}
  async function persist(next,message){const clean=[...new Map(next.map(value=>[String(value).trim().toLocaleLowerCase(),String(value).trim()]).filter(([key])=>key)).values()];try{const result=await api('/api/v79/language-detection/forced-exclusions',{method:'PUT',body:JSON.stringify({track_names:clean})});names=result.track_names||[];render();status.textContent=message;toast(message)}catch(error){if(error?.reportStale)return;status.textContent=error.message;toast(error.message,true)}}
  addButton.onclick=()=>{const value=input.value.trim();if(!value)return;persist([...names,value],'Track name added').then(()=>{input.value=''})};input.onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();addButton.click()}};
  api('/api/v79/language-detection/forced-exclusions').then(result=>{names=result.track_names||[];render()}).catch(error=>{if(error?.reportStale)return;status.textContent=error.message});
})();
(function(){
  const dialog=document.querySelector('#image-subtitle-report-dialog'); if(!dialog)return;
  const title=dialog.querySelector('[data-report-title]'),summary=dialog.querySelector('[data-report-summary]'),items=dialog.querySelector('[data-report-items]'),languageFix=dialog.querySelector('[data-report-language-fix]'),htmlFix=dialog.querySelector('[data-report-html-fix]');
  let currentItems=[],currentKind='',currentType='';
  const close=()=>{dialog.close();const panel=dialog.querySelector('[data-report-preflight]');if(panel){panel.hidden=true;const content=panel.querySelector('[data-report-preflight-content]');if(content)content.textContent=''}};
  async function queueLanguageFix(kind,button){button.disabled=true;button.textContent='Queueing…';if(typeof beginGlobalBusy==='function')beginGlobalBusy('Queueing language corrections');if(typeof setGlobalBusyProgress==='function')setGlobalBusyProgress(0,2,'Queueing language corrections','Preparing eligible media');try{const result=await api('/api/v19/reports/portuguese-language/fix',{method:'POST',body:JSON.stringify({kind})});if(typeof setGlobalBusyProgress==='function')setGlobalBusyProgress(2,2,'Language corrections queued',`${result.queued_media||0} media queued`);button.textContent=result.queued_media?`Queued ${result.queued_streams} stream${result.queued_streams===1?'':'s'}`:'No eligible streams';if(result.preflight_id)window.showReportPreflight(dialog,[result.preflight_id]);toast(result.queued_media?`${result.queued_media} media queued for language correction`:'No eligible streams above 80%',!result.queued_media);if(result.queued_media)window.refreshActiveReportAfterQueue?.('language')}catch(error){if(error?.reportStale)return;button.disabled=false;button.textContent='Queue fixes above 80%';toast(error.message,true)}finally{if(typeof endGlobalBusy==='function')endGlobalBusy()}} dialog.querySelectorAll('[data-report-close]').forEach(button=>button.onclick=close);
  function reportItemPeak(item){const values=item.episodes?item.episodes.flatMap(ep=>(ep.mismatches||[]).map(m=>Number(m.confidence||0)/100)):(item.mismatches||[]).map(m=>Number(m.confidence||0)/100);return Math.max(...values,0)}
  function openListedMedia(item){const destination=currentKind==='movies'?'movies':'tv';page(destination);if(destination==='movies'){$('#movie-search').value=item.title;loadMovies()}else{$('#show-search').value=item.title;loadTv()}close()}
  async function queueAction(item,action,button){button.disabled=true;button.textContent='Queueing…';if(action==='html_cleanup'){item._queueState='validating';renderSubtitleItems('HTML','html_cleanup')}if(typeof beginGlobalBusy==='function')beginGlobalBusy(action==='html_cleanup'?'Queueing HTML cleanup':'Queueing subtitle conversion');if(typeof setGlobalBusyProgress==='function')setGlobalBusyProgress(0,2,'Queueing subtitle work','Preparing the selected report entries');try{const result=await api('/api/v19/reports/subtitle-action',{method:'POST',body:JSON.stringify({action,streams:item.streams||[]})});if(typeof setGlobalBusyProgress==='function')setGlobalBusyProgress(2,2,'Subtitle work queued',`${result.queued||0} operation(s) queued`);if(result.preflight_ids?.length)window.showReportPreflight(dialog,result.preflight_ids);else if(result.preflight_id)window.showReportPreflight(dialog,[result.preflight_id]);if(action==='html_cleanup'&&result.preflight_id)waitHtmlPreflight(result.preflight_id).catch(error=>{if(error?.reportStale)return;item._queueState='skipped';item._queueReason=error.message;renderSubtitleItems('HTML','html_cleanup')});const errors=result.errors||[];toast(`${result.queued||0} operation${result.queued===1?'':'s'} submitted${errors.length?` · ${errors.length} skipped: ${errors[0]}`:''}`,errors.length>0);if(result.queued){button.textContent='Queued';window.refreshActiveReportAfterQueue?.(action==='html_cleanup'?'html':'image')}else button.textContent='No eligible streams'}catch(error){if(error?.reportStale)return;if(action==='html_cleanup'){item._queueState='failed';item._queueReason=error.message;renderSubtitleItems('HTML','html_cleanup')}button.disabled=false;button.textContent=action==='image_convert'?'Queue OCR → SRT':'Queue HTML cleanup';toast(error.message,true)}finally{if(typeof endGlobalBusy==='function')endGlobalBusy()}}
  function renderSubtitleItems(label,action){
    const entries=[];
    const navigation=uniqueReportNavigation(currentItems.flatMap(item=>(item.episodes||[]).map(ep=>({path:ep.path,label:item.title+' · '+ep.episode}))));
    function actionButton(item,index){
      const state=item._queueState||'';
      let text=action==='image_convert'?'Queue OCR → SRT':'Queue HTML cleanup';
      if(state==='validating')text='Validating…';else if(state==='queued')text='Queued';else if(state==='skipped')text='Skipped · review';else if(state==='failed')text='Queue failed · retry';
      return '<button type="button" class="report-item-action vse-btn" data-report-action="'+index+'" '+(['queued','validating'].includes(state)?'disabled':'')+'>'+text+'</button>';
    }
    function row(item,showTitle=''){
      const index=entries.push(item)-1;
      const count=item[currentType==='html'?'html_subtitle_count':'image_subtitle_count']||0;
      const name=showTitle?item.episode:item.title;
      return '<div class="report-item vse-panel"><button type="button" class="report-title-link vse-link" data-report-open="'+index+'">'+esc(name)+'</button><span>'+count+' '+label+' subtitle'+(count===1?'':'s')+(item._queueReason?' · '+esc(item._queueReason):'')+'</span>'+actionButton(item,index)+'</div>';
    }
    items.innerHTML=currentItems.length?currentItems.map(item=>{
      if(currentKind!=='tv'||!item.episodes)return row(item);
      const index=entries.push(item)-1;
      return '<details class="report-show-group"><summary><strong>'+esc(item.title)+'</strong><span>'+item.episodes.length+' episodes</span><button type="button" class="report-item-action vse-btn" data-report-open="'+index+'">Open show</button>'+actionButton(item,index)+'</summary><div class="report-show-episodes">'+item.episodes.map(ep=>{ep.showTitle=item.title;return row(ep,item.title)}).join('')+'</div></details>';
    }).join(''):'<p class="reports-empty vse-status" data-status="current">No indexed '+label+' subtitles found.</p>';
    items.querySelectorAll('[data-report-open]').forEach(button=>button.onclick=event=>{
      event.preventDefault();event.stopPropagation();
      const item=entries[Number(button.dataset.reportOpen)];
      if(item.showTitle)openReportMediaEditor(item.path,item.showTitle+' · '+item.episode,navigation,dialog);
      else openListedMedia(item);
    });
    items.querySelectorAll('[data-report-action]').forEach(button=>button.onclick=event=>{
      event.preventDefault();event.stopPropagation();
      queueAction(entries[Number(button.dataset.reportAction)],action,button);
    });
  }
  async function waitHtmlPreflight(requestId){const requestItems=currentItems;for(let attempt=0;attempt<1200;attempt++){const status=await api('/api/v89/preflight/'+encodeURIComponent(requestId));if(status.status==='pending'||status.status==='running'){await new Promise(resolve=>setTimeout(resolve,500));continue}const outcomes=status.result?.items||[];const byPath=new Map(outcomes.map(row=>[String(row.path||''),row]));requestItems.forEach(item=>{const rows=(item.streams||[]).map(stream=>byPath.get(String(stream.path||''))).filter(Boolean);if(!rows.length)return;const approved=rows.filter(row=>row.decision==='approved'),failed=rows.filter(row=>row.decision!=='approved');if(approved.length){item._queueState='queued';item._queueReason='validated; child task queued'}else if(failed.length){item._queueState='skipped';item._queueReason=failed[0].reason||'not eligible'}else item._queueState='skipped';});if(currentItems===requestItems&&dialog.dataset.reportKey==='html')renderSubtitleItems('HTML','html_cleanup');return status}throw new Error('HTML cleanup validation is taking too long; the request remains queued')}
  async function queueAllHtml(button){button.disabled=true;button.textContent='Queueing…';if(typeof beginGlobalBusy==='function')beginGlobalBusy('Queueing HTML cleanup');if(typeof setGlobalBusyProgress==='function')setGlobalBusyProgress(0,3,'Queueing HTML cleanup','Preparing all eligible report entries');const streams=currentItems.flatMap(item=>item.streams||[]);if(!streams.length){button.textContent='No eligible streams';if(typeof endGlobalBusy==='function')endGlobalBusy();return}if(!confirm(`Queue HTML tag removal for ${streams.length} subtitle${streams.length===1?'':'s'}?`)){button.disabled=false;button.textContent=`Queue HTML cleanup (${streams.length})`;if(typeof endGlobalBusy==='function')endGlobalBusy();return}try{currentItems.forEach(item=>{if((item.streams||[]).length)item._queueState='validating'});renderSubtitleItems('HTML','html_cleanup');if(typeof setGlobalBusyProgress==='function')setGlobalBusyProgress(1,3,'Queueing HTML cleanup','Submitting subtitle cleanup requests');const result=await api('/api/v19/reports/subtitle-action',{method:'POST',body:JSON.stringify({action:'html_cleanup',streams})});if(typeof setGlobalBusyProgress==='function')setGlobalBusyProgress(2,3,'Validating HTML cleanup','Checking each TV episode and subtitle stream');if(result.preflight_ids?.length)window.showReportPreflight(dialog,result.preflight_ids);if(result.preflight_id)waitHtmlPreflight(result.preflight_id).catch(error=>{if(error?.reportStale)return;currentItems.forEach(item=>{if(item._queueState==='validating'){item._queueState='failed';item._queueReason=error.message}});renderSubtitleItems('HTML','html_cleanup')});if(typeof setGlobalBusyProgress==='function')setGlobalBusyProgress(3,3,'HTML cleanup queued','Eligible subtitle streams are now queued');button.textContent=`Queued ${result.queued||0}`;toast(`${result.queued||0} HTML subtitle cleanup operation${result.queued===1?'':'s'} submitted`);if(result.queued)window.refreshActiveReportAfterQueue?.('html')}catch(error){if(error?.reportStale)return;currentItems.forEach(item=>{if(item._queueState==='validating'){item._queueState='failed';item._queueReason=error.message}});renderSubtitleItems('HTML','html_cleanup');button.disabled=false;button.textContent=`Queue HTML cleanup (${streams.length})`;toast(error.message,true)}finally{if(typeof endGlobalBusy==='function')endGlobalBusy()}}

  async function openSubtitleReport(button,type,kind){languageFix.hidden=true;if(htmlFix){htmlFix.hidden=true;htmlFix.disabled=false}currentKind=kind;currentType=type;const isHtml=type==='html',label=isHtml?'HTML':'image',action=isHtml?'html_cleanup':'image_convert';title.textContent=isHtml?(kind==='tv'?'TV Shows with HTML subtitles':'Movies with HTML subtitles'):(kind==='tv'?'TV Shows with Image subtitles':'Movies with Image subtitles');summary.textContent='Loading indexed report…';items.innerHTML='<p class="reports-loading vse-status" data-status="pending">Loading…</p>';dialog.showModal();button.disabled=true;try{const result=await reportRequest(`/api/v19/reports/${isHtml?'html-subtitles':'image-subtitles'}?kind=${encodeURIComponent(kind)}`);currentItems=result.items||[];summary.textContent=`${result.title_count} titles · ${result.media_count} media with ${label} subtitles`;if(isHtml&&htmlFix){const count=(result.items||[]).reduce((total,item)=>total+(item.streams||[]).length,0);htmlFix.hidden=count===0;htmlFix.textContent=`Queue HTML cleanup (${count})`;htmlFix.onclick=()=>queueAllHtml(htmlFix)}renderSubtitleItems(label,action)}catch(error){if(error?.reportStale)return;summary.textContent='';items.innerHTML=`<p class="reports-error vse-status" data-status="failed">${esc(error.message)}</p>`}finally{button.disabled=false}}
  document.querySelectorAll('[data-image-subtitle-report]').forEach(button=>button.onclick=()=>openSubtitleReport(button,'image',button.dataset.imageSubtitleReport));
  document.querySelectorAll('[data-html-subtitle-report]').forEach(button=>button.onclick=()=>openSubtitleReport(button,'html',button.dataset.htmlSubtitleReport));
  function makeTvReportPanelsExpandable(){
    if(currentKind!=='tv')return;
    [...items.querySelectorAll(':scope > .report-item')].forEach(panel=>{
      if(panel.parentElement?.classList.contains('report-show-group'))return;
      const titleNode=panel.querySelector('[data-forced-title], [data-report-detected]');
      if(!titleNode)return;
      const details=document.createElement('details'); details.className='report-show-group';
      const summaryNode=document.createElement('summary'); summaryNode.innerHTML=`<strong>${esc(titleNode.textContent.trim())}</strong><span>Expand episodes</span>`;
      panel.parentNode.insertBefore(details,panel); details.append(summaryNode,panel);
    });
  }
  function forcedReportNavigationItems(items,kind){return items.flatMap(item=>item.episodes?item.episodes.map(ep=>({path:ep.path,label:`${item.title} · ${ep.episode}`,showTitle:item.title})):item.path?[{path:item.path,label:item.title,showTitle:''}]:[])}
  function openForcedEditor(path,label,navigationItems,onReturn){openReportMediaEditor(path,label,navigationItems,dialog,onReturn)}
  function openForcedReport(button,kind){languageFix.hidden=true;if(htmlFix)htmlFix.hidden=true;currentKind=kind;currentType='forced';title.textContent=kind==='tv'?'TV Shows with Forced streams':'Movies with Forced streams';summary.textContent='Loading indexed report…';items.innerHTML='<p class="reports-loading vse-status" data-status="pending">Loading…</p>';dialog.showModal();button.disabled=true;reportRequest(`/api/v19/reports/forced-streams?kind=${encodeURIComponent(kind)}`).then(result=>{currentItems=result.items||[];const forcedNavigation=forcedReportNavigationItems(currentItems,kind);summary.textContent=`${result.title_count} titles · ${result.media_count} media with Forced streams`;items.innerHTML=currentItems.length?currentItems.map((item,index)=>{const details=item.episodes?item.episodes.map((ep,ei)=>`<div class="report-subitem"><button type="button" class="report-episode-link vse-link" data-forced-episode="${index}:${ei}">${esc(ep.episode)}</button><span>${ep.forced_count} forced stream${ep.forced_count===1?'':'s'}</span></div>`).join(''):`<span>${item.forced_count} forced stream${item.forced_count===1?'':'s'}</span>`;return `<div class="report-item vse-panel"><button type="button" class="report-title-link vse-link" data-forced-title="${index}">${esc(item.title)}</button><div class="report-details">${details}${item.root_name?`<small>${esc(item.root_name)}</small>`:''}</div>${!item.episodes&&item.path?`<button type="button" class="report-item-action vse-btn" data-forced-edit="${index}">Stream properties</button>`:''}</div>`}).join(''):'<p class="reports-empty vse-status" data-status="current">No forced streams found.</p>';if(kind==='tv')makeTvReportPanelsExpandable();items.querySelectorAll('[data-forced-title]').forEach(b=>b.onclick=()=>openListedMedia(currentItems[Number(b.dataset.forcedTitle)]));items.querySelectorAll('[data-forced-edit]').forEach(b=>b.onclick=()=>{const item=currentItems[Number(b.dataset.forcedEdit)];openForcedEditor(item.path,item.title,forcedNavigation,()=>openForcedReport(button,kind))});items.querySelectorAll('[data-forced-episode]').forEach(b=>b.onclick=()=>{const [i,e]=b.dataset.forcedEpisode.split(':').map(Number),ep=currentItems[i]?.episodes?.[e];if(ep){openForcedEditor(ep.path,`${currentItems[i].title} · ${ep.episode}`,forcedNavigation,()=>openForcedReport(button,kind))}})}).catch(error=>{if(error?.reportStale)return;items.innerHTML=`<p class="reports-error vse-status" data-status="failed">${esc(error.message)}</p>`}).finally(()=>button.disabled=false)}
  document.querySelectorAll('[data-forced-report]').forEach(button=>button.onclick=()=>openForcedReport(button,button.dataset.forcedReport));
  document.querySelectorAll('[data-portuguese-report]').forEach(button=>button.onclick=async()=>{const kind=button.dataset.portugueseReport;currentKind=kind;currentType='language';if(htmlFix)htmlFix.hidden=true;title.textContent=kind==='tv'?'TV Shows with detected subtitle language differences':'Movies with detected subtitle language differences';summary.textContent='Loading detection report…';items.innerHTML='<p class="reports-loading vse-status" data-status="pending">Loading…</p>';dialog.showModal();button.disabled=true;languageFix.hidden=true;try{const result=await reportRequest(`/api/v19/reports/portuguese-language?kind=${encodeURIComponent(kind)}`);summary.textContent=`${result.title_count} titles · ${result.media_count} media with confidence above 60%`;languageFix.hidden=!(result.fixable_above_80>0);languageFix.textContent=result.fixable_above_80>0?`Queue fixes above 80% (${result.fixable_above_80})`:'Queue fixes above 80%';languageFix.disabled=false;languageFix.onclick=()=>queueLanguageFix(kind,languageFix);items.innerHTML=result.items.length?result.items.map(item=>{const detail=item.episodes?item.episodes.map((ep,index)=>`<div class="report-subitem"><button type="button" class="report-episode-link vse-link" data-report-episode="${result.items.indexOf(item)}:${index}">${reportConfidenceDot(Number(ep.confidence||0)/100,'',ep.detected_language)}${esc(ep.episode)}</button>${ep.mismatches.map(m=>`<span>${esc(m.detected_language)} · ${m.confidence}% confidence${m.evidence?` · ${esc(m.evidence)}`:''}${m.sdh_label?` · SDH: ${esc(m.sdh_label)} (${Number(m.sdh_confidence||0).toFixed(1)}%)${m.sdh_evidence?` · ${esc(m.sdh_evidence)}`:''}`:''}</span>`).join('')}</div>`).join(''):item.mismatches.map(m=>`<span>${esc(m.detected_language)} · ${m.confidence}% confidence${m.evidence?` · ${esc(m.evidence)}`:''}${m.sdh_label?` · SDH: ${esc(m.sdh_label)} (${Number(m.sdh_confidence||0).toFixed(1)}%)${m.sdh_evidence?` · ${esc(m.sdh_evidence)}`:''}`:''}</span>`).join('');return`<div class="report-item vse-panel"><button type="button" class="report-title-link vse-link" data-report-detected="${result.items.indexOf(item)}">${reportItemPeak(item)>=.6?reportConfidenceDot(reportItemPeak(item),'',item.detected_language||item.mismatches?.[0]?.detected_language):''}${esc(item.title)}</button><div class="report-details">${detail}${item.root_name?`<small>${esc(item.root_name)}</small>`:''}</div>${!item.episodes&&item.path?`<button type="button" class="report-item-action vse-btn" data-report-movie-edit="${result.items.indexOf(item)}">Stream properties</button>`:''}</div>`}).join(''):'<p class="reports-empty vse-status" data-status="current">No high-confidence Portuguese language mismatches found.</p>';if(kind==='tv')makeTvReportPanelsExpandable();items.querySelectorAll('[data-report-detected]').forEach(link=>link.onclick=()=>openListedMedia(result.items[Number(link.dataset.reportDetected)]));items.querySelectorAll('[data-report-movie-edit]').forEach(link=>link.onclick=()=>{const item=result.items[Number(link.dataset.reportMovieEdit)];if(item?.path){const nav=uniqueReportNavigation(result.items.flatMap(group=>group.episodes||[]).length?result.items.flatMap(group=>group.episodes||[]):result.items.map(group=>({path:group.path,label:group.title})));openReportMediaEditor(item.path,item.title,nav,dialog)}});items.querySelectorAll('[data-report-episode]').forEach(link=>link.onclick=()=>{const [itemIndex,episodeIndex]=link.dataset.reportEpisode.split(':').map(Number),episode=result.items[itemIndex]?.episodes?.[episodeIndex];if(!episode?.path)return;const nav=uniqueReportNavigation(result.items.flatMap(group=>group.episodes||[]));openReportMediaEditor(episode.path,`${result.items[itemIndex].title} · ${episode.episode}`,nav,dialog)})}catch(error){if(error?.reportStale)return;summary.textContent='';items.innerHTML=`<p class="reports-error vse-status" data-status="failed">${esc(error.message)}</p>`}finally{button.disabled=false}});
})();

(function(){
  async function queueAudioDetection(path, button){
    button.disabled=true; button.textContent='Queued…';
    try { const result=await api('/api/v79/audio-language-detection/queue',{method:'POST',body:JSON.stringify({path})}); button.textContent=result.status==='running'?'Running':'Queued'; toast('Voice language detection queued'); }
    catch(error){if(error?.reportStale)return;button.disabled=false;button.textContent='Detect voice';toast(error.message,true)}
  }
  document.addEventListener('click', event=>{
    const button=event.target.closest?.('[data-audio-detect]');
    if(button) queueAudioDetection(button.dataset.audioDetect,button);
  });
  async function loadVoiceReport(){
    const root=document.querySelector('#audio-language-report-list'),status=document.querySelector('#audio-language-report-status');
    if(!root)return;
    root.innerHTML='<p class="muted">Loading voice report…</p>';
    try{
      const result=await api('/api/v79/reports/audio-language');
      status.textContent=`${result.media_count||0} media · ${result.stream_count||0} audio streams`;
      root.innerHTML=result.items?.length?result.items.map(item=>`<div class="audio-language-report-row"><strong>${esc(item.title||item.path)}</strong><small>${esc(item.kind||'media')} · Audio ${Number(item.type_index)+1} · ${Number(item.sample_agree||0)}/${Number(item.sample_total||0)} samples agree · metadata: ${esc(item.metadata_language||'unset')} → detected: ${esc(item.detected_language||'unknown')} · ${Number(item.confidence||0).toFixed(1)}%</small><code>${esc(item.path)}</code></div>`).join(''):'<p class="muted">No voice metadata mismatches found.</p>';
    }catch(error){if(error?.reportStale)return;root.innerHTML=`<p class="error">${esc(error.message)}</p>`;status.textContent=''}
  }
  document.querySelector('#audio-language-report-open')?.addEventListener('click',loadVoiceReport);
})();


(function(){
  const audio=document.querySelector('#duplicate-audio-languages'), subtitle=document.querySelector('#duplicate-subtitle-languages'), save=document.querySelector('#duplicate-language-save'), status=document.querySelector('#duplicate-language-status');
  if(!audio||!subtitle||!save)return;
  api('/api/v19/settings/duplicate-languages').then(result=>{audio.value=(result.audio||[]).join(', ');subtitle.value=(result.subtitle||[]).join(', ')}).catch(error=>{if(error?.reportStale)return;status.textContent=error.message});
  save.onclick=async()=>{save.disabled=true;try{const result=await api('/api/v19/settings/duplicate-languages',{method:'PUT',body:JSON.stringify({audio:audio.value.split(',').map(x=>x.trim()).filter(Boolean),subtitle:subtitle.value.split(',').map(x=>x.trim()).filter(Boolean)})});audio.value=(result.audio||[]).join(', ');subtitle.value=(result.subtitle||[]).join(', ');status.textContent='Saved.';toast('Duplicate-language report settings saved')}catch(error){if(error?.reportStale)return;status.textContent=error.message;toast(error.message,true)}finally{save.disabled=false}};
})();

(function(){
  window.voiceDetectionEnabled=true;
  const enabled=document.querySelector('#voice-detection-enabled'), url=document.querySelector('#voice-detection-url'), seconds=document.querySelector('#voice-detection-seconds'), count=document.querySelector('#voice-detection-count'), positions=document.querySelector('#voice-detection-positions'), save=document.querySelector('#voice-detection-save'), status=document.querySelector('#voice-detection-status'), config=document.querySelector('#voice-detection-config');
  function apply(){if(config)config.hidden=!window.voiceDetectionEnabled;document.querySelectorAll('[data-audio-detect]').forEach(button=>button.hidden=!window.voiceDetectionEnabled);const reportButton=document.querySelector('#audio-language-report-open');if(reportButton)reportButton.hidden=!window.voiceDetectionEnabled}
  async function load(){try{const result=await api('/api/v79/audio-language-detection/settings');window.voiceDetectionEnabled=result.enabled!==false;if(enabled)enabled.checked=window.voiceDetectionEnabled;if(url)url.value=result.service_url||'http://language-id:9000';if(seconds)seconds.value=result.sample_seconds||30;if(count)count.value=result.sample_count||((result.sample_positions||[.1,.5,.9]).length);if(positions)positions.value=(result.sample_positions||[.1,.5,.9]).join(', ');apply();if(window.loadMovies)loadMovies();if(window.loadTv)loadTv()}catch(error){if(error?.reportStale)return;if(status)status.textContent=error.message}}
  enabled?.addEventListener('change',()=>{window.voiceDetectionEnabled=enabled.checked;apply()});
  save?.addEventListener('click',async()=>{save.disabled=true;try{const result=await api('/api/v79/audio-language-detection/settings',{method:'PUT',body:JSON.stringify({enabled:enabled.checked,service_url:url.value,sample_seconds:Number(seconds.value||30),sample_count:Number(count?.value||3),sample_positions:positions.value.split(',').map(Number).filter(Number.isFinite)})});window.voiceDetectionEnabled=result.enabled;status.textContent='Voice detection settings saved.';apply();if(window.loadMovies)loadMovies();if(window.loadTv)loadTv();toast('Voice detection settings saved')}catch(error){if(error?.reportStale)return;status.textContent=error.message;toast(error.message,true)}finally{save.disabled=false}});
  document.querySelector('#voice-detection-help')?.addEventListener('click',()=>alert('SpeechBrain Docker setup:\n\n1. Stop and remove the old Whisper container:\n   docker stop whisper && docker rm whisper\n\n2. Build and start the replacement from this project:\n   docker compose build language-id\n   docker compose up -d language-id\n\n3. Check readiness:\n   curl http://127.0.0.1:9000/healthz\n\nThe model is downloaded once and stored in /home/docker/videostreamedit/language-id-models. The service performs spoken-language identification only; it does not transcribe audio.'));
  load();
})();

(function(){document.addEventListener('click',async event=>{const dot=event.target.closest?.('[data-stream-detect]');if(!dot||dot.disabled)return;dot.disabled=true;dot.classList.add('checking');dot.title='Detecting language…';if(typeof setGlobalBusyProgress==='function')setGlobalBusyProgress(0,2,'Detecting stream language','Reading the selected audio or subtitle stream');try{const result=await api('/api/v79/language-detection/stream',{method:'POST',body:JSON.stringify({path:dot.dataset.path,codec_type:dot.dataset.codecType,type_index:Number(dot.dataset.typeIndex),external:dot.dataset.external==='true'})});if(typeof setGlobalBusyProgress==='function')setGlobalBusyProgress(2,2,'Language detection complete','Comparing detected language with stream metadata');const confidence=Number(result.confidence||0);dot.classList.toggle('detected',Boolean(result.detected_language));const metadataLanguage=dot.dataset.metadataLanguage||'';const metadataRegion=dot.dataset.metadataRegion||'';const color=typeof streamDetectionColor==='function'?streamDetectionColor(result.detected_language,metadataLanguage,metadataRegion,confidence):(confidence>=.6?'#d1a84b':'#7d8995');dot.style.setProperty('--detection-color',color);dot.title=result.detected_language?`Detected ${result.detected_language} with ${(confidence*100).toFixed(1)}% confidence${result.evidence?` — ${result.evidence}`:''}`:'No confident common language detected';toast(dot.title);}catch(error){if(error?.reportStale)return;if(typeof setGlobalBusyProgress==='function')setGlobalBusyProgress(2,2,'Language detection failed',error.message);dot.title=error.message;toast(error.message,true)}finally{dot.disabled=false;dot.classList.remove('checking')}});})();


(function(){
  const dialog=document.querySelector('#image-subtitle-report-dialog');
  if(!dialog)return;
  const title=dialog.querySelector('[data-report-title]');
  const summary=dialog.querySelector('[data-report-summary]');
  const items=dialog.querySelector('[data-report-items]');
  function close(){if(dialog.open)dialog.close()}
  function edit(path,label,navigationItems){
    openReportMediaEditor(path,label,navigationItems,dialog);
  }
  function render(result,kind){
    const groups=result.items||[], rows=[];
    items.replaceChildren();
    if(kind==='tv'){
      items.innerHTML=groups.length?groups.map(item=>{
        const episodes=item.episodes||[];
        const body=episodes.map(ep=>{const index=rows.push({path:ep.path,label:`${item.title||''} · ${ep.episode||''}`})-1;return `<div class="report-episode-row"><span class="report-episode-name">${esc(ep.episode||'Episode')}</span><span>${(ep.streams||[]).length} damaged subtitle stream${(ep.streams||[]).length===1?'':'s'}</span><button type="button" class="report-item-action vse-btn" data-damage-edit="${index}">Stream properties</button></div>`}).join('');
        return `<details class="report-show-group"><summary><strong>${esc(item.title||'Unknown show')}</strong><span>${episodes.length} episode${episodes.length===1?'':'s'}</span></summary><div class="report-show-episodes">${body||'<p class="muted">No episode details.</p>'}</div></details>`;
      }).join(''):'<p class="reports-empty vse-status" data-status="current">No damaged SRT subtitles found.</p>';
    } else {
      groups.forEach(item=>{const index=rows.push({path:(item.paths||[])[0],label:item.title})-1;items.insertAdjacentHTML('beforeend',`<div class="report-item vse-panel"><span class="report-title-link vse-link">${esc(item.title||'')}</span><span>${Number(item.damaged_subtitle_count||0)} damaged subtitle stream${Number(item.damaged_subtitle_count||0)===1?'':'s'}</span><button type="button" class="report-item-action vse-btn" data-damage-edit="${index}">Stream properties</button></div>`)})
      if(!groups.length)items.innerHTML='<p class="reports-empty vse-status" data-status="current">No damaged SRT subtitles found.</p>';
    }
    items.querySelectorAll('[data-damage-edit]').forEach(button=>{const row=rows[Number(button.dataset.damageEdit)];if(row?.path)button.onclick=()=>edit(row.path,row.label,uniqueReportNavigation(rows))});
  }

  document.querySelectorAll('[data-damaged-subtitle-report]').forEach(button=>button.onclick=async()=>{
    const kind=button.dataset.damagedSubtitleReport||'movies';
    title.textContent=kind==='tv'?'TV Shows with damaged SRT subtitles':'Movies with damaged SRT subtitles';
    summary.textContent='Loading…'; items.innerHTML='<p class="reports-loading vse-status" data-status="pending">Loading…</p>'; dialog.showModal(); button.disabled=true;
    try{const result=await reportRequest('/api/v19/reports/damaged-subtitles?kind='+encodeURIComponent(kind));summary.textContent=`${result.title_count||0} titles · ${result.media_count||0} media`;render(result,kind)}
    catch(error){if(error?.reportStale)return;summary.textContent='';items.innerHTML=`<p class="reports-errors vse-status" data-status="failed">${esc(error.message||'Unable to load report')}</p>`}
    finally{button.disabled=false}
  });
})();

(function(){
  const dialog=document.querySelector('#image-subtitle-report-dialog'); if(!dialog)return;
  const title=dialog.querySelector('[data-report-title]'), summary=dialog.querySelector('[data-report-summary]'), items=dialog.querySelector('[data-report-items]');
  function openEditorFor(path,label,navigationItems){openReportMediaEditor(path,label,navigationItems,dialog)}
  document.querySelectorAll('[data-duplicate-language-report]').forEach(button=>button.onclick=async()=>{
    const [kind,type]=button.dataset.duplicateLanguageReport.split(':'); title.textContent=`${kind==='tv'?'TV Shows':'Movies'} with duplicate ${type} languages`; summary.textContent='Loading…'; items.innerHTML='<p class="reports-loading vse-status" data-status="pending">Loading…</p>'; dialog.showModal(); button.disabled=true;
    try{
      const result=await reportRequest(`/api/v19/reports/duplicate-languages?kind=${encodeURIComponent(kind)}&stream_type=${encodeURIComponent(type)}`);
      summary.textContent=`${result.title_count||0} titles · ${result.media_count||0} media · languages: ${(result.languages||[]).join(', ')||'none configured'}`;
      const entries=[];
      (result.items||[]).forEach(item=>{const media=item.episodes||[{path:item.path,label:item.title,duplicates:item.duplicates||[]}];media.forEach(ep=>(ep.duplicates||[]).forEach(duplicate=>entries.push({path:ep.path,label:ep.episode?`${item.title} · ${ep.episode}`:item.title,show:item.title,episode:ep.episode,duplicate}))) });
      if(kind==='tv'){
        const grouped=new Map(); entries.forEach((entry,index)=>{const key=entry.show||'Unknown show';if(!grouped.has(key))grouped.set(key,[]);grouped.get(key).push({...entry,index})});
        items.innerHTML=grouped.size?[...grouped.entries()].map(([show,group])=>`<details class="report-show-group"><summary><strong>${esc(show)}</strong><span>${group.length} finding${group.length===1?'':'s'}</span></summary><div class="report-show-episodes">${group.map(entry=>`<div class="report-item vse-panel" data-dup-row="${entry.index}"><span class="report-title-link vse-link">${esc(entry.episode||show)}</span><span>${esc(entry.duplicate.language)} ×${entry.duplicate.count}</span><button type="button" class="report-item-action vse-btn" data-dup-edit="${entry.index}">Stream properties</button><button type="button" data-dup-ignore="${entry.index}" title="Remove from report until the relevant streams change">Dismiss</button></div>`).join('')}</div></details>`).join(''):'<p class="reports-empty vse-status" data-status="current">No duplicate-language media found.</p>';
      } else items.innerHTML=entries.map((entry,index)=>`<div class="report-item vse-panel" data-dup-row="${index}"><span class="report-title-link vse-link">${esc(entry.label)}</span><span>${esc(entry.duplicate.language)} ×${entry.duplicate.count}</span><button type="button" class="report-item-action vse-btn" data-dup-edit="${index}">Stream properties</button><button type="button" data-dup-ignore="${index}" title="Remove from report until the relevant streams change">Dismiss</button></div>`).join('')||'<p class="reports-empty vse-status" data-status="current">No duplicate-language media found.</p>';
      items.querySelectorAll('[data-dup-edit]').forEach(edit=>{const entry=entries[Number(edit.dataset.dupEdit)];edit.onclick=()=>openEditorFor(entry.path,entry.label,uniqueReportNavigation(entries.map(item=>({path:item.path,label:item.label}))))});
      items.querySelectorAll('[data-dup-ignore]').forEach(ignore=>ignore.onclick=async()=>{const entry=entries[Number(ignore.dataset.dupIgnore)];if(!entry||!confirm(`Remove ${entry.label} · ${entry.duplicate.language} from this report until its streams change?`))return;ignore.disabled=true;try{await api('/api/v19/reports/duplicate-languages/suppress',{method:'POST',body:JSON.stringify({path:entry.path,stream_type:type,language:entry.duplicate.language})});items.querySelector(`[data-dup-row="${Number(ignore.dataset.dupIgnore)}"]`)?.remove();toast('Removed from this report until the relevant streams change')}catch(error){if(error?.reportStale)return;ignore.disabled=false;toast(error.message,true)}});
    }catch(error){if(error?.reportStale)return;items.innerHTML=`<p class="reports-error vse-status" data-status="failed">${esc(error.message)}</p>`}finally{button.disabled=false}
  });
})();

(function(){
  const dialog=document.querySelector('#image-subtitle-report-dialog');
  if(!dialog)return;
  const title=dialog.querySelector('[data-report-title]'),summary=dialog.querySelector('[data-report-summary]'),items=dialog.querySelector('[data-report-items]');
  document.querySelectorAll('[data-no-confidence-report]').forEach(button=>button.onclick=async()=>{
    const kind=button.dataset.noConfidenceReport; title.textContent=kind==='tv'?'TV Shows with subtitle analysis requiring review':'Movies with subtitle analysis requiring review'; summary.textContent='Loading subtitle evidence…'; items.innerHTML='<p class="reports-loading vse-status" data-status="pending">Loading…</p>'; dialog.showModal(); button.disabled=true;
    try{
      const result=await reportRequest(`/api/v19/reports/subtitle-no-confidence?kind=${encodeURIComponent(kind)}`);
      summary.textContent=`${result.media_count||0} media · ${result.stream_count||0} subtitle streams`; const rows=result.items||[];
      items.innerHTML=rows.length?rows.map((item,index)=>`<div class="report-item vse-panel"><div><strong>${esc(item.title)}</strong>${item.show_title?`<small>${esc(item.show_title)}</small>`:''}<span>${esc(item.source)} subtitle ${Number(item.type_index)>=0?Number(item.type_index)+1:''} · ${esc(item.status)} · ${esc(item.reason||'No reason recorded')}</span><small>${item.cue_count||0} cues · ${item.text_chars||0} text chars · ${Number(item.text_coverage||0).toFixed(1)}% density coverage${item.damage?` · ${esc(item.damage)}`:''}</small></div><div class="report-item-actions"><button type="button" class="report-item-action vse-btn" data-no-confidence-edit="${index}">Stream properties</button><button type="button" class="report-item-action vse-btn" data-no-confidence-revalidate="${index}">Revalidate</button></div></div>`).join(''):'<p class="reports-empty vse-status" data-status="current">No subtitle streams currently require review.</p>';
      items.querySelectorAll('[data-no-confidence-edit]').forEach(node=>node.onclick=()=>{const item=rows[Number(node.dataset.noConfidenceEdit)];openReportMediaEditor(item.path,item.show_title?`${item.show_title} · ${item.title}`:item.title,rows.map(row=>({path:row.path,label:row.show_title?`${row.show_title} · ${row.title}`:row.title})),dialog)});
      items.querySelectorAll('[data-no-confidence-revalidate]').forEach(node=>node.onclick=async()=>{const item=rows[Number(node.dataset.noConfidenceRevalidate)];node.disabled=true;node.textContent='Queued…';try{const result=await api('/api/v19/reports/subtitle-revalidate',{method:'POST',body:JSON.stringify({paths:[item.path],subtitle_indices:item.type_index>=0?[item.type_index]:null})});node.textContent=result.queued?'Queued':'Already queued';toast(result.queued?'Subtitle revalidation queued':'Subtitle inspection already queued')}catch(error){if(error?.reportStale)return;node.disabled=false;node.textContent='Revalidate';toast(error.message,true)}});
    }catch(error){if(error?.reportStale)return;summary.textContent='';items.innerHTML=`<p class="reports-error vse-status" data-status="failed">${esc(error.message)}</p>`}finally{button.disabled=false}
  });
})();


/* Subtitle-confidence report refinement: actionable filters and expandable evidence. */
(function(){
  const dialog=document.querySelector('#image-subtitle-report-dialog');
  if(!dialog||typeof api!=='function')return;
  const title=dialog.querySelector('[data-report-title]'),summary=dialog.querySelector('[data-report-summary]'),items=dialog.querySelector('[data-report-items]');
  let currentKind='movies', rows=[];
  function ensureControls(){
    let bar=dialog.querySelector('[data-no-confidence-controls]');
    if(bar)return bar;
    bar=document.createElement('div'); bar.dataset.noConfidenceControls='true'; bar.className='report-filter-bar';
    bar.innerHTML='<label>Status <select data-no-confidence-status><option value="">All review findings</option><option value="no_confidence">No confidence</option><option value="unreadable">Unreadable</option></select></label><label>Reason <input data-no-confidence-reason type="search" placeholder="Filter reason…" autocomplete="off"></label><button type="button" class="vse-btn" data-no-confidence-refresh>Refresh</button>';
    const anchor=dialog.querySelector('.image-subtitle-report-body'); anchor?.parentNode?.insertBefore(bar,anchor);
    bar.querySelector('[data-no-confidence-status]').onchange=()=>load();
    bar.querySelector('[data-no-confidence-reason]').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();load()}};
    bar.querySelector('[data-no-confidence-refresh]').onclick=()=>load();
    return bar;
  }
  function render(){
    const card=(item,index)=>`<div class="report-item vse-panel"><div><strong>${esc(item.title)}</strong><span>${esc(item.source)} subtitle ${Number(item.type_index)>=0?Number(item.type_index)+1:''} · ${esc(item.status)} · ${esc(item.reason||'No reason recorded')}</span><details><summary>Evidence</summary><small>${item.cue_count||0} cues · ${item.text_chars||0} text chars · ${Number(item.text_coverage||0).toFixed(1)}% coverage · ${item.markup_count||0} markup tags${item.damage?` · ${esc(item.damage)}`:''}<br>Metadata: ${esc(item.metadata_language||'empty')}${item.metadata_region?` / ${esc(item.metadata_region)}`:''}${item.external_path?`<br>External: ${esc(item.external_path)}`:''}</small></details></div><div class="report-item-actions"><button type="button" class="report-item-action vse-btn" data-no-confidence-edit="${index}">Stream properties</button><button type="button" class="report-item-action vse-btn" data-no-confidence-revalidate="${index}">Revalidate</button></div></div>`;
    if(currentKind==='tv'){
      const groups=new Map();rows.forEach((item,index)=>{const key=item.show_title||'Unknown show';if(!groups.has(key))groups.set(key,[]);groups.get(key).push({item,index})});
      items.innerHTML=groups.size?[...groups.entries()].map(([show,group])=>`<details class="report-show-group"><summary><strong>${esc(show)}</strong><span>${group.length} episode finding${group.length===1?'':'s'}</span></summary><div class="report-show-episodes">${group.map(entry=>card(entry.item,entry.index)).join('')}</div></details>`).join(''):'<p class="reports-empty vse-status" data-status="current">No subtitle streams match the selected review filters.</p>';
    } else items.innerHTML=rows.length?rows.map(card).join(''):'<p class="reports-empty vse-status" data-status="current">No subtitle streams match the selected review filters.</p>';
    items.querySelectorAll('[data-no-confidence-edit]').forEach(node=>node.onclick=()=>{const item=rows[Number(node.dataset.noConfidenceEdit)];openReportMediaEditor(item.path,item.show_title?`${item.show_title} · ${item.title}`:item.title,rows.map(row=>({path:row.path,label:row.show_title?`${row.show_title} · ${row.title}`:row.title})),dialog)});
    items.querySelectorAll('[data-no-confidence-revalidate]').forEach(node=>node.onclick=async()=>{const item=rows[Number(node.dataset.noConfidenceRevalidate)];node.disabled=true;node.textContent='Queued…';try{const result=await api('/api/v19/reports/subtitle-revalidate',{method:'POST',body:JSON.stringify({paths:[item.path],subtitle_indices:item.type_index>=0?[item.type_index]:null})});node.textContent=result.queued?'Queued':'Already queued';toast(result.queued?'Subtitle revalidation queued':'Subtitle inspection already queued')}catch(error){if(error?.reportStale)return;node.disabled=false;node.textContent='Revalidate';toast(error.message,true)}});
  }
  async function load(){
    const bar=ensureControls(),status=bar.querySelector('[data-no-confidence-status]').value,reason=bar.querySelector('[data-no-confidence-reason]').value.trim();
    items.innerHTML='<p class="reports-loading vse-status" data-status="pending">Loading…</p>';
    try{const query=new URLSearchParams({kind:currentKind});if(status)query.set('status',status);if(reason)query.set('reason',reason);const result=await reportRequest('/api/v19/reports/subtitle-no-confidence?'+query.toString());rows=result.items||[];summary.textContent=`${result.media_count||0} media · ${result.stream_count||0} subtitle streams`;render()}catch(error){if(error?.reportStale)return;summary.textContent='';items.innerHTML=`<p class="reports-error vse-status" data-status="failed">${esc(error.message)}</p>`}
  }
  document.querySelectorAll('[data-no-confidence-report]').forEach(button=>button.onclick=()=>{currentKind=button.dataset.noConfidenceReport||'movies';title.textContent=currentKind==='tv'?'TV Shows with subtitle analysis requiring review':'Movies with subtitle analysis requiring review';ensureControls();const bar=dialog.querySelector('[data-no-confidence-controls]');bar.querySelector('[data-no-confidence-status]').value='';bar.querySelector('[data-no-confidence-reason]').value='';dialog.showModal();load()});
})();


/* Reports for media that match the season-filter "langs" rule. */
(function(){
  const reportDialog=document.querySelector('#image-subtitle-report-dialog');
  if(!reportDialog||typeof api!=='function')return;
  const title=reportDialog.querySelector('[data-report-title]'), summary=reportDialog.querySelector('[data-report-summary]'), items=reportDialog.querySelector('[data-report-items]');
  function escText(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
  function openRemoval(kind, paths, rows){
    const existing=document.querySelector('#uncommon-language-action-dialog'); if(existing)existing.remove();
    const byType={audio:new Set(),subtitle:new Set()}; (rows||[]).forEach(row=>{const type=row.stream_type==='external'?'subtitle':row.stream_type;if(byType[type])byType[type].add(String(row.language||''))});
    const languages=[...new Set([...byType.audio,...byType.subtitle])].filter(Boolean).sort((a,b)=>a.localeCompare(b));
    if(!languages.length){toast('No uncommon languages remain for this media',true);return}
    const dialog=document.createElement('dialog');dialog.id='uncommon-language-action-dialog';dialog.innerHTML=`<div class="dialog-title"><div><h2>Remove uncommon languages</h2><p>${kind==='tv'?'Selected episodes':'Selected movies'} · choose stream type and languages.</p></div><button type="button" class="icon-close" data-action-cancel aria-label="Close">×</button></div><div class="dialog-body"><label>Streams<select data-action-scope><option value="both">Audio + subtitles</option><option value="audio">Audio</option><option value="subtitle">Subtitles</option></select></label><fieldset><legend>Languages</legend><div class="language-selection-actions"><button type="button" data-action-all>Select all</button><button type="button" data-action-none>Unselect all</button><button type="button" data-action-invert>Invert selection</button></div><div data-action-options></div></fieldset></div><div class="dialog-actions"><button type="button" data-action-cancel>Cancel</button><button type="button" data-action-queue>Add to queue</button><button type="button" class="primary" data-action-now>Apply now</button></div>`;document.body.append(dialog);
    const scope=dialog.querySelector('[data-action-scope]'), options=dialog.querySelector('[data-action-options]');
    const render=()=>{const selected=scope.value==='audio'?byType.audio:scope.value==='subtitle'?byType.subtitle:new Set([...byType.audio,...byType.subtitle]);options.innerHTML=[...selected].sort((a,b)=>a.localeCompare(b)).map(lang=>`<label><input type="checkbox" data-action-language value="${escText(lang)}"> ${escText(lang)}</label>`).join('')||'<small>No matching languages.</small>'};
    render();scope.onchange=render;dialog.querySelector('[data-action-all]').onclick=()=>options.querySelectorAll('input').forEach(node=>node.checked=true);dialog.querySelector('[data-action-none]').onclick=()=>options.querySelectorAll('input').forEach(node=>node.checked=false);dialog.querySelector('[data-action-invert]').onclick=()=>options.querySelectorAll('input').forEach(node=>node.checked=!node.checked);
    const close=()=>dialog.close();dialog.querySelectorAll('[data-action-cancel]').forEach(node=>node.onclick=close);
    const submit=async mode=>{const selected=[...options.querySelectorAll('[data-action-language]:checked')].map(node=>node.value);if(!selected.length){toast('Select at least one language',true);return}const stream_types=scope.value==='audio'?['audio']:scope.value==='subtitle'?['subtitle','external']:['audio','subtitle','external'];const buttons=dialog.querySelectorAll('button');buttons.forEach(node=>node.disabled=true);try{const result=await api('/api/v19/reports/uncommon-languages/remove',{method:'POST',body:JSON.stringify({kind,paths,languages:selected,stream_types,mode})});if(mode==='now'&&result.preflight_id){if(typeof beginGlobalBusy==='function')beginGlobalBusy('Applying uncommon-language removal');window.showReportPreflight(reportDialog,[result.preflight_id]);const progress=await waitReportPreflight(result.preflight_id);if(typeof endGlobalBusy==='function')endGlobalBusy();close();toast(`${progress.succeeded||0} media updated${progress.failed?` · ${progress.failed} failed`:''}`,Boolean(progress.failed));}else{close();toast(`Removal validation added to queue (request #${result.preflight_id||'pending'})`)} }catch(error){if(error?.reportStale)return;if(typeof endGlobalBusy==='function')endGlobalBusy();buttons.forEach(node=>node.disabled=false);toast(error.message,true)}};
    dialog.querySelector('[data-action-queue]').onclick=()=>submit('queue');dialog.querySelector('[data-action-now]').onclick=()=>submit('now');dialog.addEventListener('cancel',event=>{event.preventDefault();close()});dialog.showModal();
  }
  function renderReport(result,kind){
    const groups=result.items||[], allMedia=kind==='tv'?groups.flatMap(group=>group.episodes||[]):groups.map(item=>({path:item.path,label:item.title,streams:item.streams||[]}));
    const allPaths=allMedia.map(item=>item.path).filter(Boolean), allRows=allMedia.flatMap(item=>item.streams||[]);
    const summaryText=`${result.title_count||0} titles · ${result.media_count||0} media · configured common: ${(result.configured_languages||[]).join(', ')||'none'}`;summary.textContent=summaryText;
    const mediaCard=(media,label)=>`<div class="report-item vse-panel"><div><strong>${escText(label)}</strong><span>${(media.streams||[]).length} uncommon stream${(media.streams||[]).length===1?'':'s'} · ${[...new Set((media.streams||[]).map(row=>row.language))].map(escText).join(', ')}</span></div><button type="button" class="report-item-action vse-btn" data-uncommon-remove="${escText(media.path)}">Remove languages</button></div>`;
    if(kind==='tv')items.innerHTML=groups.length?groups.map(group=>{const episodes=group.episodes||[], paths=episodes.map(ep=>ep.path), rows=episodes.flatMap(ep=>ep.streams||[]);return `<details class="report-show-group"><summary><strong>${escText(group.title)}</strong><span>${episodes.length} episode${episodes.length===1?'':'s'}</span><button type="button" class="report-item-action vse-btn" data-uncommon-show="${escText(group.title)}">Remove from show</button></summary><div class="report-show-episodes">${episodes.map(ep=>mediaCard(ep,ep.episode)).join('')}</div></details>`}).join(''):'<p class="reports-empty vse-status" data-status="current">No uncommon-language media found.</p>';
    else items.innerHTML=groups.length?groups.map(item=>mediaCard(item,item.title)).join(''):'<p class="reports-empty vse-status" data-status="current">No uncommon-language media found.</p>';
    const bulk=document.createElement('button');bulk.type='button';bulk.className='primary report-bulk-action';bulk.textContent=kind==='tv'?'Remove from all listed episodes':'Remove from all listed movies';bulk.onclick=()=>openRemoval(kind,allPaths,allRows);items.prepend(bulk);
    items.querySelectorAll('[data-uncommon-remove]').forEach(node=>node.onclick=()=>{const media=allMedia.find(item=>item.path===node.dataset.uncommonRemove);if(media)openRemoval(kind,[media.path],media.streams||[])});
    items.querySelectorAll('[data-uncommon-show]').forEach(node=>node.onclick=()=>{const group=groups.find(item=>item.title===node.dataset.uncommonShow);if(group)openRemoval(kind,(group.episodes||[]).map(ep=>ep.path),(group.episodes||[]).flatMap(ep=>ep.streams||[]))});
  }
  document.querySelectorAll('[data-uncommon-language-report]').forEach(button=>button.onclick=async()=>{const kind=button.dataset.uncommonLanguageReport;title.textContent=kind==='tv'?'TV Shows with uncommon languages':'Movies with uncommon languages';summary.textContent='Loading indexed report…';items.innerHTML='<p class="reports-loading vse-status" data-status="pending">Loading…</p>';reportDialog.showModal();button.disabled=true;try{const result=await reportRequest(`/api/v19/reports/uncommon-languages?kind=${encodeURIComponent(kind)}`);renderReport(result,kind)}catch(error){if(error?.reportStale)return;items.innerHTML=`<p class="reports-error vse-status" data-status="failed">${escText(error.message)}</p>`}finally{button.disabled=false}});
})();

/* Audio-only report and report landing-page availability. */
(function(){
  var dialog=document.querySelector('#image-subtitle-report-dialog');
  if(!dialog||typeof api!=='function') return;
  var title=dialog.querySelector('[data-report-title]'), summary=dialog.querySelector('[data-report-summary]'), items=dialog.querySelector('[data-report-items]');
  function render(result,kind){
    var groups=result.items||[];
    if(kind==='tv'){
      items.innerHTML=groups.length?groups.map(function(group,gi){
        var episodes=(group.episodes||[]).map(function(ep,ei){return '<div class="report-item vse-panel"><button type="button" class="report-title-link vse-link" data-audio-only-episode="'+gi+':'+ei+'">'+esc(ep.episode)+'</button><span>'+ep.audio_count+' audio · no subtitles</span><button type="button" class="report-item-action vse-btn" data-audio-only-edit="'+gi+':'+ei+'">Stream properties</button></div>';}).join('');
        return '<details class="report-show-group"><summary><strong>'+esc(group.title)+'</strong><span>'+group.media_count+' episode'+(group.media_count===1?'':'s')+' · '+group.audio_count+' audio</span></summary><div class="report-show-episodes">'+episodes+'</div></details>';
      }).join(''):'<p class="reports-empty vse-status" data-status="current">No audio-only media found.</p>';
    }else{
      items.innerHTML=groups.length?groups.map(function(item,i){return '<div class="report-item vse-panel"><button type="button" class="report-title-link vse-link" data-audio-only-movie="'+i+'">'+esc(item.title)+'</button><span>'+item.audio_count+' audio · no subtitles</span><button type="button" class="report-item-action vse-btn" data-audio-only-movie-edit="'+i+'">Stream properties</button></div>';}).join(''):'<p class="reports-empty vse-status" data-status="current">No audio-only media found.</p>';
    }
    if(kind==='tv') { items.querySelectorAll('details.report-show-group').forEach(function(panel){panel.open=false;}); }
    var nav=uniqueReportNavigation(kind==='tv'?groups.flatMap(function(g){return (g.episodes||[]).map(function(ep){return {path:ep.path,label:g.title+' · '+ep.episode};});}):groups.map(function(g){return {path:g.path,label:g.title};}));
    items.querySelectorAll('[data-audio-only-episode]').forEach(function(node){node.onclick=function(){var parts=node.dataset.audioOnlyEpisode.split(':').map(Number),ep=groups[parts[0]]&&groups[parts[0]].episodes[parts[1]];if(ep)openReportMediaEditor(ep.path,groups[parts[0]].title+' · '+ep.episode,nav,dialog);};});items.querySelectorAll('[data-audio-only-movie-edit]').forEach(function(node){node.onclick=function(){var item=groups[Number(node.dataset.audioOnlyMovieEdit)];if(item)openReportMediaEditor(item.path,item.title,nav,dialog);};});
    items.querySelectorAll('[data-audio-only-movie]').forEach(function(node){node.onclick=function(){var item=groups[Number(node.dataset.audioOnlyMovie)];if(item)openReportMediaEditor(item.path,item.title,nav,dialog);};});items.querySelectorAll('[data-audio-only-edit]').forEach(function(node){node.onclick=function(){var parts=node.dataset.audioOnlyEdit.split(':').map(Number),ep=groups[parts[0]]&&groups[parts[0]].episodes[parts[1]];if(ep)openReportMediaEditor(ep.path,groups[parts[0]].title+' · '+ep.episode,nav,dialog);};});
  }
  document.querySelectorAll('[data-audio-only-report]').forEach(function(button){button.onclick=async function(){
    var kind=button.dataset.audioOnlyReport;title.textContent=kind==='tv'?'TV Shows with audio only · no subtitles':'Movies with audio only · no subtitles';summary.textContent='Loading indexed report…';items.innerHTML='<p class="reports-loading vse-status" data-status="pending">Loading…</p>';dialog.showModal();button.disabled=true;
    try{var result=await reportRequest('/api/v19/reports/audio-only?kind='+encodeURIComponent(kind));summary.textContent=(result.title_count||0)+' titles · '+(result.media_count||0)+' media · '+(result.audio_count||0)+' audio streams';render(result,kind);if(!result.items||!result.items.length) hideEmptyReportButton(button);}catch(error){if(error?.reportStale)return;items.innerHTML='<p class="reports-error vse-status" data-status="failed">'+esc(error.message)+'</p>';}finally{button.disabled=false;}
  };});
  function hideEmptyReportButton(button){button.hidden=true;var group=button.closest('.reports-group');if(group&&![].slice.call(group.querySelectorAll('button')).some(function(node){return !node.hidden;}))group.hidden=true;}

})();


/* External-subtitles-only report. */
(function(){
  var dialog=document.querySelector('#image-subtitle-report-dialog');
  if(!dialog||typeof api!=='function') return;
  var title=dialog.querySelector('[data-report-title]'), summary=dialog.querySelector('[data-report-summary]'), items=dialog.querySelector('[data-report-items]');
  function render(result,kind){
    var groups=result.items||[];
    if(kind==='tv'){
      items.innerHTML=groups.length?groups.map(function(group,gi){
        var episodes=(group.episodes||[]).map(function(ep,ei){return '<div class="report-item vse-panel"><button type="button" class="report-title-link vse-link" data-external-only-episode="'+gi+':'+ei+'">'+esc(ep.episode)+'</button><span>'+ep.external_count+' external subtitle'+(ep.external_count===1?'':'s')+'</span><button type="button" class="report-item-action vse-btn" data-external-only-edit="'+gi+':'+ei+'">Stream properties</button></div>';}).join('');
        return '<details class="report-show-group"><summary><strong>'+esc(group.title)+'</strong><span>'+group.media_count+' episode'+(group.media_count===1?'':'s')+' · '+group.external_count+' external</span></summary><div class="report-show-episodes">'+episodes+'</div></details>';
      }).join(''):'<p class="reports-empty vse-status" data-status="current">No media with external subtitles found.</p>';
    }else{
      items.innerHTML=groups.length?groups.map(function(item,i){return '<div class="report-item vse-panel"><button type="button" class="report-title-link vse-link" data-external-only-movie="'+i+'">'+esc(item.title)+'</button><span>'+item.external_count+' external subtitle'+(item.external_count===1?'':'s')+'</span><button type="button" class="report-item-action vse-btn" data-external-only-movie-edit="'+i+'">Stream properties</button></div>';}).join(''):'<p class="reports-empty vse-status" data-status="current">No media with external subtitles found.</p>';
    }
    if(kind==='tv') items.querySelectorAll('details.report-show-group').forEach(function(panel){panel.open=false;});
    var nav=uniqueReportNavigation(kind==='tv'?groups.flatMap(function(g){return (g.episodes||[]).map(function(ep){return {path:ep.path,label:g.title+' · '+ep.episode};});}):groups.map(function(g){return {path:g.path,label:g.title};}));
    items.querySelectorAll('[data-external-only-episode],[data-external-only-edit]').forEach(function(node){node.onclick=function(){var parts=(node.dataset.externalOnlyEpisode||node.dataset.externalOnlyEdit).split(':').map(Number),ep=groups[parts[0]]&&groups[parts[0]].episodes[parts[1]];if(ep)openReportMediaEditor(ep.path,groups[parts[0]].title+' · '+ep.episode,nav,dialog);};});
    items.querySelectorAll('[data-external-only-movie],[data-external-only-movie-edit]').forEach(function(node){node.onclick=function(){var item=groups[Number(node.dataset.externalOnlyMovie||node.dataset.externalOnlyMovieEdit)];if(item)openReportMediaEditor(item.path,item.title,nav,dialog);};});
  }
  document.querySelectorAll('[data-external-only-report]').forEach(function(button){button.onclick=async function(){var kind=button.dataset.externalOnlyReport;title.textContent=kind==='tv'?'TV Shows with external subtitles':'Movies with external subtitles';summary.textContent='Loading indexed report…';items.innerHTML='<p class="reports-loading vse-status" data-status="pending">Loading…</p>';dialog.showModal();button.disabled=true;try{var result=await reportRequest('/api/v19/reports/external-only?kind='+encodeURIComponent(kind));summary.textContent=(result.title_count||0)+' titles · '+(result.media_count||0)+' media · '+(result.external_count||0)+' external subtitle streams';render(result,kind);if(!result.items||!result.items.length)button.hidden=true;}catch(error){if(error?.reportStale)return;items.innerHTML='<p class="reports-error vse-status" data-status="failed">'+esc(error.message)+'</p>';}finally{button.disabled=false;}};});
})();

/* OpenSubtitles setup and stream-editor search/download dialog. */
(function(){
  function updateButton(){
    var button=document.querySelector('#opensubtitles-search');
    var cfg=window.openSubtitlesSettings||{};
    if(button) button.hidden=!(cfg.enabled&&cfg.configured);
  }
  window.updateOpenSubtitlesButton=updateButton;
  function wireSetup(){
    var save=document.querySelector('#opensubtitles-save'); if(!save||save.dataset.ready)return; save.dataset.ready='1';
    var enabled=document.querySelector('#opensubtitles-enabled'), key=document.querySelector('#opensubtitles-api-key'), user=document.querySelector('#opensubtitles-username'), pass=document.querySelector('#opensubtitles-password'), langs=document.querySelector('#opensubtitles-languages'), status=document.querySelector('#opensubtitles-status');
    function fields(){[key,user,pass,langs].forEach(function(el){if(el)el.disabled=!enabled.checked;}); save.disabled=!enabled.checked;}
    api('/api/v19/opensubtitles/settings').then(function(result){window.openSubtitlesSettings=result; enabled.checked=!!result.enabled; if(result.api_key)key.placeholder='Configured (leave blank to keep)'; user.value=result.username||''; langs.value=(result.languages||['pt','en']).join(', '); fields(); updateButton();}).catch(function(error){status.textContent=error.message;});
    enabled.onchange=fields;
    save.onclick=async function(){save.disabled=true;try{var result=await api('/api/v19/opensubtitles/settings',{method:'PUT',body:JSON.stringify({enabled:enabled.checked,api_key:key.value,username:user.value,password:pass.value,languages:langs.value.split(',').map(function(x){return x.trim();}).filter(Boolean)})});window.openSubtitlesSettings=result;key.value='';pass.value='';key.placeholder=result.api_key?'Configured (leave blank to keep)':'';langs.value=(result.languages||[]).join(', ');status.textContent=result.enabled?(result.configured?'Enabled and configured.':'Enabled; add API credentials to use searches.'):'Disabled.';toast('OpenSubtitles settings saved');updateButton();}catch(error){if(error?.reportStale)return;status.textContent=error.message;toast(error.message,true);}finally{fields();}};
  }
  wireSetup(); updateButton();
  function closeDialog(dialog){if(dialog&&dialog.open)dialog.close();dialog&&dialog.remove();}
  async function openSearch(){
    var cfg=window.openSubtitlesSettings||{}; if(!(cfg.enabled&&cfg.configured)){toast('Enable and configure OpenSubtitles in Setup first',true);updateButton();return;}
    var path=(typeof state!=='undefined'&&state.selectedPath)||window.selectedPath||''; if(!path){toast('Open a media item before searching',true);return;}
    var dialog=document.createElement('dialog'); dialog.className='opensubtitles-dialog'; dialog.innerHTML='<div class="dialog-title"><div><h2>Find subtitles</h2><p>Search OpenSubtitles.com for this media. Downloads are staged for review.</p></div><button type="button" class="icon-close" data-os-close>×</button></div><div class="dialog-body"><label>Query<input type="search" data-os-query value="" readonly></label><div data-os-context class="muted"></div><div data-os-languages class="muted"></div><div data-os-quota class="muted">Checking OpenSubtitles download quota…</div><button type="button" class="primary" data-os-search>Search</button><div data-os-status class="muted"></div><div data-os-results class="report-show-episodes"></div></div><div class="dialog-actions"><button type="button" data-os-close>Close</button></div>'; document.body.append(dialog);
    var query=dialog.querySelector('[data-os-query]'), context=dialog.querySelector('[data-os-context]'), languageInfo=dialog.querySelector('[data-os-languages]'), quota=dialog.querySelector('[data-os-quota]'), searchButton=dialog.querySelector('[data-os-search]'), status=dialog.querySelector('[data-os-status]'), results=dialog.querySelector('[data-os-results]');
    var quotaState={available:true,remaining:null};
    async function refreshQuota(){try{var q=await api('/api/v19/opensubtitles/quota');quotaState=q; if(q.reason==='disabled')quota.textContent='OpenSubtitles search is disabled in Setup.';else if(q.reason==='not_configured')quota.textContent='Configure the OpenSubtitles account in Setup to see download limits.';else if(q.remaining===0){quota.textContent='No OpenSubtitles downloads remain for this account.'+(q.reset_at?' Limit resets at '+q.reset_at+'.':'');searchButton.disabled=true;}else if(q.remaining!=null){quota.textContent='Downloads remaining: '+q.remaining+(q.limit!=null?' of '+q.limit:'')+(q.reset_at?' · resets at '+q.reset_at:'');}else quota.textContent='OpenSubtitles download limit is managed by the account; availability was checked.';}catch(_e){quota.textContent='OpenSubtitles quota could not be read; search is still available.';}}
    function close(){closeDialog(dialog);} dialog.querySelectorAll('[data-os-close]').forEach(function(node){node.onclick=close;});
    try{var ctx=await api('/api/v19/opensubtitles/context?path='+encodeURIComponent(path)); query.value=ctx.query||''; context.textContent=(ctx.kind==='episode'?(ctx.show_title+' · S'+String(ctx.season_number||0).padStart(2,'0')+'E'+String(ctx.episode_number||0).padStart(2,'0')):'Movie: '+(ctx.title||ctx.query||''))+' · matched by '+String((ctx.match_source||'title')).toUpperCase()+(ctx.imdb_id?' · IMDb '+ctx.imdb_id:(ctx.tmdb_id?' · TMDB '+ctx.tmdb_id:'')); languageInfo.textContent='Search languages from Setup: '+((cfg.languages||[]).join(', ')||'none');}catch(error){if(error?.reportStale)return;query.value=path.split('/').pop().replace(/\\.[^.]*$/,'');context.textContent='Using media filename';languageInfo.textContent='Search languages from Setup: '+((cfg.languages||[]).join(', '));}
    async function search(){await refreshQuota();if(quotaState.remaining===0){status.textContent='Search stopped because the OpenSubtitles download limit has been reached.';return;}status.textContent='Searching…';results.innerHTML='';try{var params=new URLSearchParams({path:path,query:query.value});var result=await api('/api/v19/opensubtitles/search?'+params.toString());status.textContent=(result.total_count||0)+' result(s) · '+((result.languages||cfg.languages||[]).join(', '))+' · matched by '+String(result.match_source||'title').toUpperCase();results.innerHTML=(result.items||[]).map(function(item){var files=(item.files||[]).map(function(file){return '<button type="button" class="vse-btn" data-os-download="'+file.file_id+'" data-os-name="'+attr(file.file_name||'subtitle.srt')+'" data-os-language="'+attr(item.language||'und')+'">Download '+esc(file.file_name||'file')+'</button>';}).join('');return '<div class="report-item vse-panel"><strong>'+esc(item.language||'und')+'</strong><span>'+esc(item.release||'')+(item.hearing_impaired?' · SDH':'')+'</span><div class="report-item-actions">'+files+'</div></div>';}).join('')||'<p class="reports-empty vse-status">No matching subtitles found.</p>';results.querySelectorAll('[data-os-download]').forEach(function(node){node.onclick=async function(){node.disabled=true;node.textContent='Downloading…';try{var out=await api('/api/v19/opensubtitles/download',{method:'POST',body:JSON.stringify({path:path,file_id:Number(node.dataset.osDownload),language:node.dataset.osLanguage,file_name:node.dataset.osName})});node.textContent='Staged';toast('Subtitle staged for review: '+out.staged_path);refreshQuota();}catch(error){if(error?.reportStale)return;node.disabled=false;node.textContent='Download failed';toast(error.message,true);}}});}catch(error){if(error?.reportStale)return;status.textContent=error.message;toast(error.message,true);}}
    dialog.querySelector('[data-os-search]').onclick=search; dialog.addEventListener('cancel',function(event){event.preventDefault();close();}); dialog.showModal(); refreshQuota(); search();
  }
  document.addEventListener('click',function(event){if(event.target.closest&&event.target.closest('#opensubtitles-search'))openSearch();});
})();
