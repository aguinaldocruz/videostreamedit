(function(){
  const root=document.querySelector('#dashboard'),body=document.querySelector('#dashboard-content');
  if(!root||!body)return;
  let data=null,pending=null,last=0,findings=null,findingTime=0,previousWork=null;
  let findingPending=null,findingRevision=0;
  let scope=localStorage.getItem('vse.dashboard.scope')||'all';
  if(!['all','movies','tv'].includes(scope))scope='all';
  root.querySelector('.page-title').innerHTML='<div><h2>Your collection</h2><p>Review progress, findings and background work.</p></div><div class="dashboard-tools"><label>Collection <select id="dashboard-scope"><option value="all">All media</option><option value="movies">Movies</option><option value="tv">TV Shows</option></select></label><button type="button" id="dashboard-refresh">Refresh</button><small data-dashboard-updated role="status"></small></div>';
  const scopeSelect=root.querySelector('#dashboard-scope');scopeSelect.value=scope;
  const number=n=>Number(n||0).toLocaleString();
  function languageChart(values){
    const names=new Intl.DisplayNames(['en'],{type:'language'});
    const max=Math.max(1,...values.map(v=>v.media_count));
    const row=v=>{let label=v.language;try{label=v.language==='und'?'Unknown / unspecified':names.of(v.language)||v.language}catch(_){}return `<div class="dash-language"><span>${esc(label)}</span><progress value="${v.media_count}" max="${max}" aria-label="${esc(label)}"></progress><strong>${number(v.media_count)}</strong></div>`};
    return values.slice(0,8).map(row).join('')+(values.length>8?`<details><summary>Show all ${values.length} languages</summary>${values.slice(8).map(row).join('')}</details>`:'');
  }
  const esc=window.esc||((v)=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])));
  const name={running:'Running',pending:'Waiting',failed:'Failed',succeeded:'Completed'};
  const purpose=t=>({media_edit:'Media changes',subtitle_html_cleanup:'Subtitle HTML cleanup',image_subtitle_convert:'Image subtitle conversion',audio_language_detection:'Voice detection',core:'Core indexing',subtitles:'Subtitle inspection',plex_sync:'Plex synchronization',filtered_stream_edit:'Movie stream changes',tv_filtered_stream_edit:'Episode stream changes',tv_filtered_stream_edit_batch:'TV-show changes'}[t]||t.replaceAll('_',' '));
  function destination(tab){page('setup');window.openSetupDestination?.('tasks',tab)}
  function jobs(source,status,type){destination(source==='indexes'?'indexes':'queue');if(source==='indexes')window.openDashboardIndexFilter?.(type||'all',status||'all');else window.openDashboardTaskFilter?.(status||null,type||null)}
  function review(kind,status){
    let offset=0,items=[];
    const modal=document.createElement('dialog');modal.className='dashboard-drilldown';
    modal.innerHTML=`<div class="dialog-title"><div><h2>${kind==='tv'?'Episodes':'Movies'} · ${esc({final:'Final version',reviewed:'Reviewed, not final',unreviewed:'Not reviewed',changed:'New or changed since review'}[status])}</h2><p>Current committed collection data · 100 results per page</p></div><button type="button" data-close>Close</button></div><div class="dialog-body" data-rows></div><div class="dialog-actions"><button type="button" data-prev>Previous</button><button type="button" data-next>Next</button></div>`;
    document.body.append(modal);modal.showModal();
    modal.querySelector('[data-close]').onclick=()=>modal.close();modal.addEventListener('close',()=>{if(!modal.dataset.editing)modal.remove()});
    const load=async()=>{const rows=modal.querySelector('[data-rows]');rows.textContent='Loading…';try{const result=await api(`/api/dashboard/media?kind=${kind}&status=${status}&offset=${offset}`);items=result.items||[];rows.innerHTML=items.map((m,i)=>`<div class="dash-media-row"><div><strong>${esc(kind==='tv'?m.show_title+' · S'+String(m.season_number||0).padStart(2,'0')+'E'+String(m.episode_number||0).padStart(2,'0'):m.title)}</strong><small>${esc(m.title)} · ${esc(m.library_name)}</small></div><button type="button" data-edit="${i}">Stream properties</button></div>`).join('')||'<p>No matching media.</p>';modal.querySelector('[data-prev]').disabled=!offset;modal.querySelector('[data-next]').disabled=!result.more;rows.querySelectorAll('[data-edit]').forEach(b=>b.onclick=async()=>{const m=items[Number(b.dataset.edit)];modal.dataset.editing='true';modal.close();mediaNavigation=items.map(x=>({path:x.path,label:kind==='tv'?x.show_title+' · '+x.title:x.title}));mediaNavigationIndex=Number(b.dataset.edit);mediaNavigationKind='report';await openEditor(m.path,mediaNavigation[mediaNavigationIndex].label);document.querySelector('#stream-dialog').addEventListener('close',()=>{delete modal.dataset.editing;modal.showModal();load()},{once:true})});}catch(e){rows.textContent=e.message}};
    modal.querySelector('[data-prev]').onclick=()=>{offset=Math.max(0,offset-100);load()};modal.querySelector('[data-next]').onclick=()=>{offset+=100;load()};load();
  }
  function draw(){
    if(!data)return;
    const kinds=scope==='all'?['movies','tv']:[scope],work=data.work;
    const cards=kinds.map(k=>{const c=data.collection[k],missing=c.total-c.indexed;return `<article class="dash-panel"><header><h3>${k==='tv'?'TV Shows':'Movies'}</h3><button type="button" data-browse="${k}">Browse →</button></header><div class="dash-total">${number(k==='tv'?c.shows:c.total)} <small>${k==='tv'?number(c.total)+' episodes':''}</small></div><p>${dashboardBytes(c.bytes)} · ${number(c.indexed)} / ${number(c.total)} indexed</p><progress max="${Math.max(c.total,1)}" value="${c.indexed}" aria-label="Indexed coverage"></progress><p>${missing?number(missing)+' not yet indexed':'All catalog media have an index'} <small>Index existence, not a live file verification.</small></p><h4>Review progress ${k==='tv'?'· episodes':''}</h4><div class="dash-segments">${[['final','Final version',c.final],['reviewed','Reviewed, not final',c.reviewed-c.final],['unreviewed','Not reviewed',c.total-c.reviewed]].map(([s,l,n])=>`<button type="button" data-review="${k}:${s}" ${n?'':'disabled'} title="Open ${number(n)} ${k==='tv'?'episodes':'movies'}: ${l}"><strong>${number(n)}</strong>${l}<span style="--bar:${c.total?Math.round(n/c.total*100):0}%"></span></button>`).join('')}</div><button type="button" class="dash-changed" data-review="${k}:changed" ${c.changed?'':'disabled'}>${number(c.changed)} new or changed since review →</button>${k==='tv'?`<small>${number(c.final_shows)} shows have all episodes finalized</small>`:''}</article>`}).join('');
    const waiting=[...work.tasks,...work.indexes].filter(r=>r.status==='pending').reduce((n,r)=>n+r.count,0);
    const trend=previousWork&&previousWork.at!==data.updated_at?waiting-previousWork.waiting:null;
    const workGroup=(source,label,rows)=>`<section><h4>${label}</h4><div class="dash-status">${['running','pending','failed'].map(s=>{const n=rows.filter(r=>r.status===s).reduce((sum,r)=>sum+r.count,0);return `<button type="button" data-jobs="${source}:${s}:" ${n?'':'disabled'} title="Open ${label.toLowerCase()} filtered by ${name[s].toLowerCase()}"><strong>${number(n)}</strong>${name[s]}</button>`}).join('')}</div><div class="dash-work-types">${[...new Set(rows.filter(r=>['pending','running','failed'].includes(r.status)).map(r=>r.type))].map(t=>`<div><span>${esc(purpose(t))}${(source==='indexes'?work.index_paused[t]:work.paused)?' · Paused':''}</span>${['running','pending','failed'].map(s=>{const n=rows.filter(r=>r.type===t&&r.status===s).reduce((v,r)=>v+r.count,0);return n?`<button type="button" data-jobs="${source}:${s}:${esc(t)}" title="Open ${esc(purpose(t))}: ${name[s]}">${number(n)} ${name[s]}</button>`:''}).join('')}</div>`).join('')||'<p>No active or failed work.</p>'}</div></section>`;
    const expanded=root.querySelector('[data-insights]')?.open;
    body.innerHTML=`<div class="dash-grid">${cards}</div><article class="dash-panel dash-work"><header><div><h3>Background work</h3><p>Whole server · independent of collection scope · ${number(work.completed_hour)} jobs completed in the last hour</p></div><button type="button" data-jobs="tasks::">Task queue →</button></header><p>${work.paused?'Media task queue is paused. ':''}${trend===null?'Backlog trend appears after the next refresh.':trend===0?'Backlog unchanged since the previous refresh.':`Backlog ${trend>0?'grew':'decreased'} by ${number(Math.abs(trend))} since the previous refresh.`} A large queue alone does not mean overload.</p><div class="dash-grid">${workGroup('tasks','Media tasks',work.tasks)}${workGroup('indexes','Indexing and subtitle inspection',work.indexes)}</div><div class="dash-status"><span>Preflight validation</span>${work.preflight.filter(r=>['pending','running','failed'].includes(r.status)).map(r=>`<button type="button" data-preflight="${r.status}">${number(r.count)} ${name[r.status]}</button>`).join('')||'<small>No waiting or failed validations</small>'}</div>${work.running.map(r=>`<p class="dash-running"><strong>${esc(r.label)}</strong> · ${esc(r.progress_message||'Running')}${r.progress_total?' · '+Math.min(100,Math.round(r.progress_current/r.progress_total*100))+'%':''}</p>`).join('')}<small>Remaining time is not estimated across mixed operations. Paused work and scheduled analysis can wait without blocking editing; failed jobs need review.</small></article><article class="dash-panel"><header><h3>Needs attention</h3><button type="button" data-reports>All reports →</button></header><p>Report memberships can overlap; these counts must not be added together.</p><div class="dash-findings" data-findings>Loading current report findings…</div></article>${scope!=='movies'?`<article class="dash-panel"><header><h3>Continue reviewing TV Shows</h3></header><div class="dash-continue">${data.continue.map(s=>`<button type="button" data-show="${esc(s.id)}"><strong>${esc(s.title)}</strong><small>${number(s.final)} / ${number(s.total)} episodes finalized · ${esc(s.library)}</small><progress value="${s.final}" max="${s.total}"></progress></button>`).join('')||'<p>No partially finalized shows.</p>'}</div></article>`:''}<details class="dash-panel" data-insights ${expanded?'open':''}><summary>Collection insights · languages, storage and Plex libraries</summary><div data-insight-body></div></details>`;
    root.querySelector('[data-dashboard-updated]').textContent='Updated '+formatAppTime(data.updated_at)+' · '+appTimezone();
    body.querySelectorAll('[data-browse]').forEach(b=>b.onclick=()=>page(b.dataset.browse==='tv'?'tv':'movies'));
    body.querySelectorAll('[data-review]').forEach(b=>b.onclick=()=>review(...b.dataset.review.split(':')));
    body.querySelectorAll('[data-jobs]').forEach(b=>b.onclick=()=>jobs(...b.dataset.jobs.split(':')));
    body.querySelectorAll('[data-preflight]').forEach(b=>b.onclick=()=>{destination('queue');const panel=document.querySelector('[data-dispatcher-panel]');panel.open=true;const select=panel.querySelector('[data-dispatcher-status]');select.value=b.dataset.preflight;select.dispatchEvent(new Event('change'));panel.scrollIntoView({block:'start'})});
    body.querySelector('[data-reports]').onclick=()=>page('reports');
    body.querySelectorAll('[data-show]').forEach(b=>b.onclick=async()=>{page('tv');await loadTv();const show=state.shows.find(s=>s.id===b.dataset.show);if(show){
      for(const [id,key] of [['reviewed','reviewState'],['notes','notesState'],['plex','plexState'],['index','indexState'],['detection','detectionState'],['final','finalState']]){const input=document.querySelector('#tv-'+id+'-filter');if(input){input.dataset[key]='all';input.dataset.symbol='—';input.checked=false;input.indeterminate=false;input.title='All items'}}
      window.resetTvHeaderFilters?.();state.episodeDetectionFilter=false;state.currentSeason='*';document.querySelector('#episode-search').value='';
      const search=document.querySelector('#show-search');search.value=show.name;state.currentShow=show;window._preserveShowSelection=true;renderShows();await loadSelectedTvShow(show);renderEpisodes()}});
    const details=body.querySelector('[data-insights]');details.ontoggle=()=>{if(details.open)loadInsights()};if(expanded)loadInsights();loadFindings();
  }
  async function loadFindings(){
    try{if(!findings||Date.now()-findingTime>60000){
      findingPending??=(async()=>{
        let result,revision;
        do{revision=findingRevision;result=await api('/api/v19/reports/availability')}while(revision!==findingRevision);
        findings=result;findingTime=Date.now();
      })().finally(()=>{findingPending=null});
      await findingPending;
    }
      const target=body.querySelector('[data-findings]');if(!target)return;
      const labels={image:'Image subtitles',damaged:'Damaged subtitles',html:'HTML subtitles',confidence:'Uncertain subtitles',language:'Language discrepancies',duplicate_audio:'Duplicate audio',duplicate_subtitle:'Duplicate subtitles',uncommon:'Uncommon languages',forced:'Forced streams',audio_only:'No subtitles',english_only:'English-only streams',external_only:'External subtitles',video_titles:'Video titles',matroska_layout:'Matroska headers'};
      target.innerHTML=Object.entries(labels).filter(([key])=>localStorage.getItem('vse.report.'+key)!=='hidden').flatMap(([key,label])=>(scope==='all'?['movies','tv']:[scope]).map(kind=>{const n=findings.counts?.[key]?.[kind]||0;return n?`<button type="button" data-finding="${key}:${kind}" title="Open ${esc(label)} report"><strong>${number(n)}</strong><span>${label}</span><small>${kind==='tv'?'Episodes':'Movies'} →</small></button>`:''})).join('')||'<p>No enabled reports have current findings.</p>';
      target.querySelectorAll('[data-finding]').forEach(b=>b.onclick=()=>{const[key,kind]=b.dataset.finding.split(':');page('reports');const card=document.querySelector(`[data-report-key="${key}"]`);const button=[...card?.querySelectorAll('.reports-group-actions button')||[]].find(x=>Object.values(x.dataset).some(v=>v===kind||v.startsWith(kind+':')));button?.click()});
    }catch(e){const target=body.querySelector('[data-findings]');if(target)target.textContent='Findings unavailable: '+e.message}
  }
  let insightData=null,insightPending=null;
  async function loadInsights(){const target=body.querySelector('[data-insight-body]');if(!target)return;target.textContent='Loading indexed insights…';try{if(!insightData){insightPending??=api('/api/v86/dashboard/stats');insightData=await insightPending}const kinds=scope==='all'?['movies','tv']:[scope];target.innerHTML=kinds.map(k=>`<h4>${k==='tv'?'TV episodes':'Movies'}</h4><div class="dash-grid">${['audio','subtitle'].map(t=>`<div><h4>${t==='audio'?'Audio':'Subtitle'} languages / regions</h4><p>Media can contain several languages. Subtitles include external files.</p>${languageChart(insightData.languages[k][t])}</div>`).join('')}</div>`).join('')+`<h4>Subtitle coverage · indexed media</h4>${(insightData.subtitle_coverage||[]).filter(r=>scope==='all'||(scope==='movies'?r.kind==='movie':r.kind==='episode')).map(r=>`<p>${r.kind==='movie'?'Movies':'Episodes'} · ${r.embedded?(r.external?'Embedded + external':'Embedded only'):(r.external?'External only':'No subtitles')} · ${number(r.media_count)}</p>`).join('')}<h4>Plex libraries</h4>${insightData.libraries.filter(l=>scope==='all'||(scope==='movies'?l.kind==='movie':l.kind==='episode')).map(l=>`<p>${esc(l.library_name)} · ${number(l.media_count)} ${l.kind==='movie'?'movies':'episodes'}</p>`).join('')}`}catch(e){target.textContent=e.message;insightPending=null}}
  window.loadCollectionDashboard=function(force=false){
    if(pending)return pending;if(data&&!force&&Date.now()-last<30000){draw();return Promise.resolve()}
    root.querySelector('[data-dashboard-updated]').textContent='Updating…';root.querySelector('#dashboard-refresh').disabled=true;
    pending=api('/api/dashboard/overview').then(result=>{if(data)previousWork={at:data.updated_at,waiting:[...data.work.tasks,...data.work.indexes].filter(r=>r.status==='pending').reduce((n,r)=>n+r.count,0)};data=result;last=Date.now();draw()}).catch(e=>{root.querySelector('[data-dashboard-updated]').textContent='Could not refresh: '+e.message;if(!data)body.textContent='Dashboard unavailable. Use Refresh to retry; other pages remain available.'}).finally(()=>{pending=null;root.querySelector('#dashboard-refresh').disabled=false});return pending;
  };
  ['media-final-version-changed','media-properties-applied','media-properties-queued'].forEach(name=>{
    document.addEventListener(name,()=>{
      findingRevision++;findings=null;findingTime=0;last=0;
      if(!root.classList.contains('hidden'))loadFindings();
    });
  });
  scopeSelect.onchange=()=>{scope=scopeSelect.value;localStorage.setItem('vse.dashboard.scope',scope);draw()};
  root.querySelector('#dashboard-refresh').onclick=()=>{findingTime=0;insightData=null;insightPending=null;window.loadCollectionDashboard(true)};
  let dashboardScroll=0;
  const previousPage=page;
  page=function(name){if(!root.classList.contains('hidden'))dashboardScroll=window.scrollY;const result=previousPage(name);if(name==='dashboard')requestAnimationFrame(()=>window.scrollTo(0,dashboardScroll));return result};
  setInterval(()=>{if(!document.hidden&&!root.classList.contains('hidden')&&!document.querySelector('dialog[open]'))window.loadCollectionDashboard()},30000);
  if(!root.classList.contains('hidden'))window.loadCollectionDashboard();
})();
