(function(){
  const page=document.querySelector('#tv');
  const pane=page?.querySelector('.episode-pane');
  const heading=pane?.querySelector('.episode-heading');
  const table=pane?.querySelector('table');
  const filterRow=page?.querySelector('#season-stream-filter-row');
  const editRow=page?.querySelector('#season-stream-edit-row');
  if(!page||!pane||!heading||!table||!filterRow||!editRow)return;

  page.classList.add('tv-workspace');
  const actionBar=document.createElement('div');
  actionBar.className='tv-show-action-bar';
  actionBar.innerHTML='<span class="tv-action-caption">Show changes</span><span class="tv-draft-help">Edit mode stages changes without touching media until Save & queue.</span>';
  for(const id of ['tv-show-edit-mode','tv-show-edit-discard','tv-edit-session-status']){
    const control=page.querySelector('#'+id);
    if(control)actionBar.append(control);
  }
  heading.after(actionBar);

  const toolbar=document.createElement('div');
  toolbar.className='tv-episode-toolbar';
  toolbar.innerHTML='<div class="tv-toolbar-primary"><span class="tv-toolbar-label">Episodes</span></div><div class="tv-toolbar-actions"></div>';
  actionBar.after(toolbar);
  const primary=toolbar.querySelector('.tv-toolbar-primary');
  const actions=toolbar.querySelector('.tv-toolbar-actions');
  const season=page.querySelector('#season-tools');
  const search=page.querySelector('#episode-search');
  const discrepancy=page.querySelector('#episode-detection-filter');
  const filter=page.querySelector('#season-stream-toggle');
  const refresh=page.querySelector('thead .refresh');
  if(season)primary.append(season);
  if(search){search.placeholder='Find an episode…';search.setAttribute('aria-label','Find an episode by number or title');primary.append(search)}
  if(discrepancy){const wrapper=document.createElement('label');wrapper.className='tv-discrepancy-control';wrapper.append(discrepancy,document.createTextNode('Discrepancies'));primary.append(wrapper)}
  if(filter){filter.textContent='Stream filters';filter.title='Show or hide stream-value filters';filter.setAttribute('aria-label','Show or hide stream-value filters');actions.append(filter);const open=filter.onclick;filter.onclick=event=>{if(!filterRow.classList.contains('hidden'))window.resetTvHeaderFilters?.();else open?.call(filter,event)}}
  if(refresh){refresh.textContent='Refresh';actions.append(refresh)}
  const actionHeading=table.querySelector('thead tr th:last-child');
  if(actionHeading)actionHeading.textContent='Actions';

  const summary=document.createElement('div');
  summary.className='tv-episode-summary';
  summary.innerHTML='<span data-tv-visible-count>Choose a show</span><div class="tv-active-filters" data-tv-active-filters></div><button type="button" data-tv-clear-filters hidden>Clear filters</button>';
  toolbar.after(summary);
  const filterPanel=document.createElement('div');filterPanel.className='tv-stream-filter-panel hidden';summary.after(filterPanel);
  const editPanel=document.createElement('div');editPanel.className='tv-stream-edit-panel hidden';filterPanel.after(editPanel);
  const filterContent=page.querySelector('#season-stream-filter-content');
  const editContent=page.querySelector('#season-stream-edit-content');
  filterPanel.append(filterContent);editPanel.append(editContent);
  const tableScroll=document.createElement('div');
  tableScroll.className='tv-episode-table-scroll';
  table.before(tableScroll);
  tableScroll.append(table);

  const showTitle=page.querySelector('#show-title');
  const showList=page.querySelector('#show-list');
  const showHeading=page.querySelector('.show-heading h3');
  if(showHeading){const count=document.createElement('small');count.className='tv-show-count';count.setAttribute('aria-live','polite');showHeading.after(count)}
  const countLabel=page.querySelector('[data-tv-visible-count]');
  const chips=page.querySelector('[data-tv-active-filters]');
  const clear=page.querySelector('[data-tv-clear-filters]');
  let updateFrame=0;
  function scheduleUpdate(){if(updateFrame)return;updateFrame=requestAnimationFrame(()=>{updateFrame=0;update()})}
  function setText(node,value){if(node&&node.textContent!==value)node.textContent=value}
  function selectedText(control){if(!control)return '';return control.selectedOptions?.[0]?.textContent?.trim()||control.value||''}
  function activeFilters(){
    const result=[];
    const seasonSelect=page.querySelector('#season-filter');
    if(seasonSelect?.value&&seasonSelect.value!=='*')result.push(selectedText(seasonSelect));
    if(search?.value.trim())result.push('Episode: '+search.value.trim());
    if(discrepancy?.classList.contains('active'))result.push('Discrepancies only');
    if(!filterRow.classList.contains('hidden')){
      const presence=filterContent.querySelector('[data-season-field="presence"]');
      if(presence?.checked)result.push('Missing stream');
      const names={stream:'Stream',language:'Language',region:'Region',track_name:'Track',filename_tag:'Filename'};
      const pair=filterContent.querySelector('.language-region-select');
      for(const [name,label] of Object.entries(names)){
        if(pair&&(name==='language'||name==='region'))continue;
        const control=filterContent.querySelector(`[data-season-field="${name}"]`);
        if(control?.value&&!['__all__','__empty__'].includes(control.value))result.push(label+': '+selectedText(control));
        else if(control?.value==='__empty__')result.push(label+': empty');
      }
      if(pair){const values=[...pair.selectedOptions].filter(option=>!option.value.startsWith('__'));if(values.length)result.push('Language: '+values.map(option=>option.textContent.trim()).join(', '))}
    }
    return result;
  }
  function update(){
    filterPanel.classList.toggle('hidden',filterRow.classList.contains('hidden'));
    editPanel.classList.toggle('hidden',editRow.classList.contains('hidden'));
    if(filter){filter.classList.toggle('active',!filterRow.classList.contains('hidden'));filter.setAttribute('aria-expanded',String(!filterRow.classList.contains('hidden')))}
    const filterClose=filterContent.querySelector('.season-stream-filter-close');
    if(filterClose){setText(filterClose,'Close filters');filterClose.title='Close and clear stream filters';filterClose.setAttribute('aria-label',filterClose.title)}
    const editToggle=filterContent.querySelector('[data-season-edit-toggle]');
    if(editToggle){setText(editToggle,'Edit matches');editToggle.setAttribute('aria-label','Edit matching streams in this TV-show draft')}
    const editClose=editContent.querySelector('[data-season-edit-close]');
    if(editClose){setText(editClose,'Close editor');editClose.title='Close bulk editor without staging this change'}
    const bulkApply=editContent.querySelector('[data-season-bulk-apply]');
    setText(bulkApply,'Stage changes');
    const session=window.tvShowEditSession;
    actionBar.classList.toggle('draft-active',Boolean(session));
    actionBar.classList.toggle('draft-committing',session?.status==='committing');
    const projections=window.tvDraftStreamProjections||{};
    let draftCount=0;
    page.querySelectorAll('#episode-list tr').forEach(row=>{
      const path=row.querySelector('.edit-file')?.dataset.path;
      if(!path)return;
      const changed=Boolean(session&&(projections[path]?.length||window.tvDraftNoteChanged?.(path)));
      row.classList.toggle('tv-draft-row',changed);
      row.classList.toggle('tv-selected-row',path===state.selectedPath);
      let marker=row.querySelector('.tv-draft-marker');
      if(changed){draftCount++;if(!marker){marker=document.createElement('span');marker.className='tv-draft-marker';marker.textContent='Draft';row.querySelector('.episode-title')?.after(marker)}}
      else marker?.remove();
    });
    const help=actionBar.querySelector('.tv-draft-help');
    setText(help,session?.status==='committing'?'Saving is queued. This show is read-only until the task finishes.':session?(draftCount?`${draftCount} listed episode${draftCount===1?'':'s'} with staged changes · media files are unchanged.`:window.tvDraftShowNoteChanged?.()?'Show note, review, or Final-version change staged · media files are unchanged.':'Changes remain virtual until Save & queue.'):'Edit mode stages changes without touching media until Save & queue.');
    const showCount=page.querySelector('.tv-show-count');setText(showCount,showList?.children.length?`${showList.children.length} shown`:'');
    const rows=[...page.querySelectorAll('#episode-list tr')];
    const visible=rows.filter(row=>!row.classList.contains('season-stream-filtered-out')&&!row.classList.contains('season-language-preview-out')).length;
    setText(countLabel,state.currentShow?`${visible} episode${visible===1?'':'s'} shown`:'Choose a show');
    const selected=activeFilters();
    chips.replaceChildren(...selected.map(value=>{const chip=document.createElement('span');chip.className='tv-filter-chip';chip.textContent=value;return chip}));
    clear.hidden=!selected.length;
  }
  clear.onclick=()=>{
    if(search?.value){search.value='';search.dispatchEvent(new Event('input',{bubbles:true}))}
    const seasonSelect=page.querySelector('#season-filter');
    if(seasonSelect?.value!=='*'){seasonSelect.value='*';seasonSelect.dispatchEvent(new Event('change',{bubbles:true}))}
    if(discrepancy?.classList.contains('active'))discrepancy.click();
    if(!filterRow.classList.contains('hidden'))window.resetTvHeaderFilters?.();
    scheduleUpdate();
  };
  if(search){let searchFrame=0;search.oninput=()=>{cancelAnimationFrame(searchFrame);searchFrame=requestAnimationFrame(()=>{renderEpisodes();scheduleUpdate()})}}
  page.addEventListener('input',scheduleUpdate,true);
  page.addEventListener('change',scheduleUpdate,true);
  page.addEventListener('click',scheduleUpdate,true);
  const observer=new MutationObserver(scheduleUpdate);
  for(const [node,options] of [[showList,{childList:true}],[page.querySelector('#episode-list'),{childList:true,subtree:true,attributes:true,attributeFilter:['class']}],[filterRow,{attributes:true,attributeFilter:['class']}],[editRow,{attributes:true,attributeFilter:['class']}],[filterContent,{childList:true,subtree:true}],[editContent,{childList:true,subtree:true}],[page.querySelector('#tv-edit-session-status'),{childList:true,attributes:true,attributeFilter:['class']}],[showTitle,{childList:true}]])if(node)observer.observe(node,options);
  scheduleUpdate();
})();
