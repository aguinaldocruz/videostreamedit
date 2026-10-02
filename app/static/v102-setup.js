(function(){
  const setup=document.querySelector('#setup');
  const legacyTabs=setup?.querySelector('.setup-tabs');
  const shell=setup?.querySelector('.tasks-shell');
  const taskTabs=shell?.querySelector('.tasks-tabs');
  if(!setup||!legacyTabs||!shell||!taskTabs)return;

  // Existing feature modules keep ownership of their forms and API calls. One
  // controller owns the visible Setup navigation instead of each module adding
  // another top-level tab or moving an entire screen into a sibling screen.
  const groups=[
    {id:'connections',label:'Connections',items:[['Plex','plex'],['Media folders','folders'],['OpenSubtitles','integrations']]},
    {id:'editing',label:'Editing',items:[['Movie import','import'],['Saved properties','properties'],['Templates','templates'],['Editing preferences','automation'],['Learned suggestions','suggestions']]},
    {id:'analysis',label:'Detection & reports',items:[['Language detection','language-detection']]},
    {id:'tasks',label:'Tasks',items:[['Task queue','queue'],['Indexes','indexes'],['Scheduled tasks','schedules'],['Priorities','priorities'],['Staged work','staged']]},
    {id:'data',label:'Data & safety',items:[['Backup','backup'],['Migrate','migrate'],['System info','info']]},
    {id:'appearance',label:'Appearance',items:[['Report visibility','visual']]}
  ];
  const taskNames=new Set(['queue','indexes','schedules','priorities','staged','backup','migrate','info']);
  const dataNames=new Set(['backup','migrate','info']);
  const sectionDescriptions={
    plex:'Connect the library that supplies movies, shows, and episode updates.',
    folders:'Manage the movie and TV folders available inside the application container.',
    integrations:'Configure optional subtitle searches and download credentials.',
    import:'Choose the locations used when importing movies.',
    properties:'Maintain reusable language, region, and track-name values.',
    templates:'Review and maintain saved stream-change templates.',
    automation:'Control the optional prompts shown while editing media.',
    suggestions:'Review track-name corrections learned from your edits.',
    'language-detection':'Configure subtitle inspection, audio detection, and report rules.',
    queue:'Review and control user-requested and background work.',
    indexes:'Check catalog index coverage and recent indexing work.',
    schedules:'Set when recurring discovery, detection, caching, and backups run.',
    priorities:'Choose how waiting tasks compete for server time.',
    staged:'Review work awaiting approval or rollback.',
    backup:'Create, restore, and rotate protected system backups.',
    migrate:'Move the database safely to another PostgreSQL server.',
    info:'See installation, storage, and database information.',
    visual:'Choose which report cards appear in this browser.'
  };
  const pageTitle=setup.querySelector('.page-title');
  pageTitle.querySelector('p').textContent='Connections, editing, analysis, background work, and data safety.';
  setup.classList.add('setup-redesigned');
  const navigation=document.createElement('div');navigation.className='setup-navigation';
  navigation.innerHTML='<div class="setup-nav-heading"><strong>Settings</strong><small>Choose an area, then a section.</small></div><div class="setup-group-tabs" role="tablist" aria-label="Setup areas"></div><div class="setup-section-tabs" role="tablist" aria-label="Sections in the selected area"></div><div class="setup-overview" aria-live="polite"><span data-setup-health="plex">Plex: checking…</span><span data-setup-health="index">Indexes: checking…</span><span data-setup-health="backup">Backup: checking…</span><span data-setup-health="staged">Staged work: checking…</span></div>';
  const workspace=document.createElement('div');workspace.className='setup-workspace';
  const content=document.createElement('div');content.className='setup-workspace-content';
  content.innerHTML='<div class="setup-current-section"><small data-setup-current-area></small><h3 data-setup-current-title></h3><p data-setup-current-description></p></div>';
  pageTitle.insertAdjacentElement('afterend',workspace);
  workspace.append(navigation,content);
  const legacyPanels=setup.querySelector('.setup-tab-panels');
  if(legacyPanels)content.append(legacyPanels);
  const groupTabs=navigation.querySelector('.setup-group-tabs');
  const sectionTabs=navigation.querySelector('.setup-section-tabs');
  groups.forEach(group=>{const button=document.createElement('button');button.type='button';button.dataset.setupGroup=group.id;button.setAttribute('role','tab');button.textContent=group.label;button.onclick=()=>activate(group.id,localStorage.getItem(`videostreamedit.setup-item.${group.id}.v2`)||group.items[0][1]);groupTabs.append(button)});

  const queueToolbar=setup.querySelector('.task-queue-heading>div:last-child');
  if(queueToolbar&&!queueToolbar.classList.contains('setup-queue-toolbar')){
    queueToolbar.classList.add('setup-queue-toolbar');
    const actionGroups=[
      ['Find work',['[data-queue-media-search]','[data-queue-expedite-duration]','[data-queue-search-expedite]']],
      ['Explore',['[data-queue-refresh]','[data-queue-grouped]','[data-queue-staged]']],
      ['Manage queue',['[data-queue-control]','[data-queue-retry-all]','[data-queue-delete-all]','[data-queue-clean-completed]']]
    ];
    actionGroups.forEach(([title,selectors])=>{
      const group=document.createElement('div');group.className='setup-queue-action-group';
      const heading=document.createElement('small');heading.textContent=title;group.append(heading);
      const controls=document.createElement('div');controls.className='setup-queue-action-controls';
      selectors.forEach(selector=>{const node=queueToolbar.querySelector(selector);if(node)controls.append(node)});
      group.append(controls);queueToolbar.append(group);
    });
  }

  const subtitleSettings=setup.querySelector('.opensubtitles-settings');
  if(subtitleSettings){
    const fields=document.createElement('div');fields.className='setup-form-grid';
    ['#opensubtitles-api-key','#opensubtitles-username','#opensubtitles-password','#opensubtitles-languages'].forEach(selector=>{
      const label=subtitleSettings.querySelector(selector)?.closest('label');if(label)fields.append(label);
    });
    const toggle=subtitleSettings.querySelector('#opensubtitles-enabled')?.closest('label');
    if(toggle)toggle.after(fields);
  }

  // Move only the schedule forms: their original event handlers, saved values,
  // and loading logic remain attached to the same elements.
  const scheduleCards=shell.querySelector('#scheduled-task-cards');
  const backupSchedule=shell.querySelector('.backup-schedule');
  if(scheduleCards&&backupSchedule){
    const card=document.createElement('section');card.className='scheduled-task-card setup-schedule-extra';
    card.innerHTML='<div><strong>System backup</strong><small>Copies the database and protected configuration; media files are not included.</small></div>';
    card.append(backupSchedule);scheduleCards.append(card);
  }
  const dispatcherControls=setup.querySelector('.dispatcher-request-controls');
  if(scheduleCards&&dispatcherControls){
    const card=document.createElement('section');card.className='scheduled-task-card setup-schedule-extra';
    card.innerHTML='<div><strong>Preflight history cleanup</strong><small>Deletes old finished validation records only. Active requests and resulting media tasks are retained.</small></div>';
    const controls=document.createElement('div');controls.className='schedule-card-controls';
    ['[data-dispatcher-enabled]','[data-dispatcher-retention]','[data-dispatcher-save]'].forEach(selector=>{const input=dispatcherControls.querySelector(selector);const control=input?.closest('label')||input;if(control)controls.append(control)});
    card.append(controls);
    scheduleCards.append(card);
  }

  // Staged OCR originals are rollback data, not language-detection settings.
  const stagedTab=document.createElement('button');stagedTab.type='button';stagedTab.dataset.tasksTab='staged';stagedTab.setAttribute('role','tab');stagedTab.textContent='Staged work';taskTabs.append(stagedTab);
  const stagedPanel=document.createElement('section');stagedPanel.dataset.tasksPanel='staged';stagedPanel.className='hidden setup-staged-panel';
  stagedPanel.innerHTML='<article class="detector-section"><div class="detector-section-heading"><div><h3>OCR conversions awaiting review</h3><p>Approve a conversion to remove rollback data, or restore its staged original. Media changes are not discarded by opening this screen.</p></div><button type="button" data-staged-refresh>Refresh</button></div></article>';
  shell.append(stagedPanel);
  const stagedCard=stagedPanel.querySelector('article');
  const secondary=setup.querySelector('.secondary-detector-section');
  const ocrHeading=[...(secondary?.querySelectorAll('h3')||[])].find(node=>node.textContent.trim()==='OCR conversion staging');
  const voiceHeading=[...(secondary?.querySelectorAll('h3')||[])].find(node=>node.textContent.trim()==='Voice metadata report');
  if(ocrHeading){let node=ocrHeading;while(node&&node!==voiceHeading){const next=node.nextSibling;if(node.nodeType===1&&node.tagName!=='HR')stagedCard.append(node);node=next}}
  stagedPanel.querySelector('[data-staged-refresh]').onclick=()=>window.loadOcrStaged?.();
  stagedTab.onclick=()=>{showTaskPanel('staged');window.loadOcrStaged?.()};
  if(secondary){
    const heading=secondary.querySelector('h3');if(heading)heading.textContent='Report settings and voice findings';
    let reportCard=null;
    [...secondary.children].forEach(node=>{
      if(node.tagName==='HR'){node.remove();return}
      const title=node.textContent.trim();
      if((node.tagName==='H4'&&title==='Forced streams report exclusions')||(node.tagName==='H3'&&['Duplicate-language reports','Voice metadata report'].includes(title))){
        reportCard=document.createElement('section');reportCard.className='setup-report-card';secondary.insertBefore(reportCard,node);
      }
      if(reportCard)reportCard.append(node);
    });
  }

  const voiceConfig=setup.querySelector('#voice-detection-config');
  if(voiceConfig){const advanced=document.createElement('details');advanced.className='setup-advanced';advanced.innerHTML='<summary>Advanced sampling controls</summary><p class="muted">The recommended values work for most media; change these only when detection is too slow or uncertain.</p>';[...voiceConfig.querySelectorAll('label')].slice(1).forEach(label=>advanced.append(label));voiceConfig.append(advanced);const positions=setup.querySelector('#voice-detection-positions');if(positions){positions.readOnly=true;positions.closest('label')?.setAttribute('title','Sample positions are calculated automatically from the number of samples.')}}
  const voiceStatus=setup.querySelector('#voice-detection-status');
  if(voiceStatus){const test=document.createElement('button');test.type='button';test.textContent='Test saved service';test.title='Save the service URL and enable voice detection before testing';voiceStatus.before(test);const health=document.createElement('span');health.className='voice-service-health';voiceStatus.after(health);test.onclick=async()=>{test.disabled=true;health.textContent='Checking saved service…';try{const result=await api('/api/v79/audio-language-detection/health');health.textContent=result.message||result.status;health.classList.toggle('error',result.status==='unavailable')}catch(error){health.textContent=error.message;health.classList.add('error')}finally{test.disabled=false}}}
  const fullDetection=setup.querySelector('#language-detection-queue-full');
  if(fullDetection){const advanced=document.createElement('details');advanced.className='setup-advanced';advanced.innerHTML='<summary>Advanced: full-catalog analysis</summary><p class="muted">Clears existing language and voice findings, then queues every eligible movie and episode again. This may run for many hours.</p>';fullDetection.after(advanced);advanced.append(fullDetection)}

  function showTaskPanel(name){
    shell.querySelectorAll('[data-tasks-tab]').forEach(button=>{const active=button.dataset.tasksTab===name;button.classList.toggle('active',active);button.setAttribute('aria-selected',String(active))});
    shell.querySelectorAll('[data-tasks-panel]').forEach(panel=>panel.classList.toggle('hidden',panel.dataset.tasksPanel!==name));
    localStorage.setItem('videostreamedit.tasks-tab.v1',name);
  }
  function activate(groupId,itemId){
    const group=groups.find(value=>value.id===groupId)||groups[0];
    const item=group.items.find(value=>value[1]===itemId)||group.items[0];
    groupTabs.querySelectorAll('button').forEach(button=>{const active=button.dataset.setupGroup===group.id;button.classList.toggle('active',active);button.setAttribute('aria-selected',String(active))});
    sectionTabs.replaceChildren();
    group.items.forEach(([label,id])=>{const button=document.createElement('button');button.type='button';button.setAttribute('role','tab');button.textContent=label;button.classList.toggle('active',id===item[1]);button.setAttribute('aria-selected',String(id===item[1]));button.onclick=()=>activate(group.id,id);sectionTabs.append(button)});
    sectionTabs.hidden=group.items.length<2;
    content.querySelector('[data-setup-current-area]').textContent=group.label;
    content.querySelector('[data-setup-current-title]').textContent=item[0];
    content.querySelector('[data-setup-current-description]').textContent=sectionDescriptions[item[1]]||'';
    if(taskNames.has(item[1])){
      // The Tasks parent activates its remembered child. Set the destination
      // first so opening Scheduled Tasks never briefly opens the queue (and
      // its modal loading layer) before the child tab can receive the click.
      localStorage.setItem('videostreamedit.tasks-tab.v1',item[1]);
      legacyTabs.querySelector('[data-setup-tab="queue"]')?.click();
      taskTabs.querySelectorAll('[data-tasks-tab]').forEach(button=>button.hidden=dataNames.has(button.dataset.tasksTab)!==(group.id==='data'));
      const taskButton=taskTabs.querySelector(`[data-tasks-tab="${item[1]}"]`);
      if(item[1]!=='queue'){
        if(taskButton)taskButton.click();else showTaskPanel(item[1]);
      }
    }else{
      legacyTabs.querySelector(`[data-setup-tab="${item[1]}"]`)?.click();
    }
    localStorage.setItem('videostreamedit.setup-group.v2',group.id);
    localStorage.setItem(`videostreamedit.setup-item.${group.id}.v2`,item[1]);
    if(item[1]==='indexes')window.refreshSetupIndexCards?.();
    if(item[1]==='schedules')loadPreflightSchedule();
    if(item[1]==='staged')window.loadOcrStaged?.();
  }
  window.openSetupDestination=(group,item)=>activate(group,item);
  async function loadPreflightSchedule(){try{const data=await api('/api/v89/preflight/settings');const enabled=setup.querySelector('[data-dispatcher-enabled]'),retention=setup.querySelector('[data-dispatcher-retention]');if(enabled)enabled.checked=Boolean(data.cleanup_enabled);if(retention)retention.value=data.retention_days||30}catch(_error){}}
  async function refreshOverview(){
    const value=(name,message)=>{const target=navigation.querySelector(`[data-setup-health="${name}"]`);if(target)target.textContent=message};
    const checks=[
      api('/api/v11/plex/config').then(result=>value('plex',result.has_token?'Plex: configured':'Plex: not configured')).catch(()=>value('plex','Plex: unavailable')),
      api('/api/v86/operational-summary').then(result=>{const counts=Object.values(result.indexes||{}).reduce((total,item)=>({failed:total.failed+Number(item.failed||0),queued:total.queued+Number(item.pending||0),running:total.running+Number(item.running||0)}),{failed:0,queued:0,running:0});value('index',counts.failed?`Indexes: ${counts.failed} failed`:counts.running?`Indexes: ${counts.running} running`:counts.queued?`Indexes: ${counts.queued} queued`:'Indexes: idle')}).catch(()=>value('index','Indexes: unavailable')),
      api('/api/v99/backup/status').then(result=>{const last=result.backups?.[0],when=last?.modified?formatAppDate(last.modified):last?.name;value('backup',last?`Last backup: ${when}`:'Backup: none yet')}).catch(()=>value('backup','Backup: unavailable')),
      api('/api/v68/ocr/staged').then(result=>value('staged',`Staged work: ${(result.items||[]).length} awaiting review`)).catch(()=>value('staged','Staged work: unavailable'))
    ];
    await Promise.allSettled(checks);
  }
  const previous=localStorage.getItem('videostreamedit.setup-tab.v1')||'plex';
  const fallback=taskNames.has(previous)?(dataNames.has(localStorage.getItem('videostreamedit.tasks-tab.v1'))?'data':'tasks'):({plex:'connections',folders:'connections',integrations:'connections',import:'editing',properties:'editing',templates:'editing',automation:'editing',suggestions:'editing','language-detection':'analysis',visual:'appearance'}[previous]||'connections');
  const initialGroup=localStorage.getItem('videostreamedit.setup-group.v2')||fallback;
  const initialItem=localStorage.getItem(`videostreamedit.setup-item.${initialGroup}.v2`)||({connections:previous,editing:previous,analysis:'language-detection',tasks:'queue',data:'backup',appearance:'visual'}[initialGroup]);
  activate(initialGroup,initialItem);
  document.querySelector('[data-page="setup"]')?.addEventListener('click',()=>{refreshOverview();const group=localStorage.getItem('videostreamedit.setup-group.v2')||'connections';activate(group,localStorage.getItem(`videostreamedit.setup-item.${group}.v2`))});
  if(!setup.classList.contains('hidden'))refreshOverview();
})();
