(function () {
  const setup = $('#setup');
  const pageTitle = setup.querySelector('.page-title');
  const plex = setup.querySelector('.plex-setup');
  const movieImport = $('#movie-import-settings');
  const savedProperties = $('#saved-property-maintenance');
  const templates = $('#template-maintenance');
  const prompts = $('#prompt-settings');
  const indexMaintenance = setup.querySelector('.index-maintenance');
  if (!pageTitle || !plex) return;

  pageTitle.querySelector('h2').textContent = 'Setup';
  pageTitle.querySelector('p').textContent = 'Configure Plex, movie import, reusable metadata, templates, and editing automation.';
  pageTitle.insertAdjacentHTML('afterend', `<div class="setup-tabs" role="tablist" aria-label="Setup sections">
    <button type="button" role="tab" data-setup-tab="plex">Plex</button><button type="button" role="tab" data-setup-tab="import">Movie import</button><button type="button" role="tab" data-setup-tab="properties">Saved properties</button><button type="button" role="tab" data-setup-tab="templates">Templates</button><button type="button" role="tab" data-setup-tab="automation">Automation</button><button type="button" role="tab" data-setup-tab="suggestions">Learned suggestions</button>
  </div><div class="setup-tab-panels">
    <section data-setup-panel="plex"></section><section data-setup-panel="import"></section><section data-setup-panel="properties"></section><section data-setup-panel="templates"></section><section data-setup-panel="automation"></section><section data-setup-panel="suggestions"></section>
  </div>`);

  const panel = name => setup.querySelector(`[data-setup-panel="${name}"]`);
  panel('plex').append(plex);
  if (movieImport) panel('import').append(movieImport);
  if (savedProperties) panel('properties').append(savedProperties);
  if (templates) panel('templates').append(templates);
  if (prompts) panel('automation').append(prompts);
  if (indexMaintenance) panel('automation').append(indexMaintenance);
  setup.querySelectorAll('.import-setup-grid').forEach(grid => {if (!grid.children.length) grid.remove()});
  panel('suggestions').innerHTML = `<article id="learned-suggestion-maintenance"><div class="learned-suggestion-heading"><div><h3>Learned track-name suggestions</h3><p>Edit, pause, resume, reset, or remove individual audio and subtitle correction rules.</p></div><button type="button" id="clear-learned-suggestions" class="danger">Delete all</button></div><input type="search" id="learned-suggestion-search" class="search" placeholder="Filter old or replacement names…"><div id="learned-suggestion-list"></div></article>`;

  const TAB_KEY = 'videostreamedit.setup-tab.v1';
  function activateSetupTab(name) {
    if (!panel(name)) name = 'plex';
    setup.querySelectorAll('[data-setup-tab]').forEach(button => {const active=button.dataset.setupTab===name;button.classList.toggle('active',active);button.setAttribute('aria-selected',String(active))});
    setup.querySelectorAll('[data-setup-panel]').forEach(item => item.classList.toggle('hidden',item.dataset.setupPanel!==name));
    localStorage.setItem(TAB_KEY,name);
    if (name === 'suggestions') loadLearnedSuggestions();
  }
  setup.querySelectorAll('[data-setup-tab]').forEach(button => button.onclick=()=>activateSetupTab(button.dataset.setupTab));
  activateSetupTab(localStorage.getItem(TAB_KEY)||'plex');

  let learnedSuggestions=[];
  function suggestionPayload(item, action, replacement) {return{stream_type:item.stream_type,track_language:item.track_language||"",old_value:item.old_value,new_value:item.new_value,action,replacement}}
  function renderLearnedSuggestions() {
    const query=$('#learned-suggestion-search').value.trim().toLowerCase(),items=learnedSuggestions.filter(item=>!query||`${item.old_value} ${item.new_value} ${item.stream_type}`.toLowerCase().includes(query));
    $('#learned-suggestion-list').innerHTML=items.length?items.map((item,index)=>`<div class="learned-suggestion-item ${item.enabled?'':'paused'}" data-suggestion-index="${learnedSuggestions.indexOf(item)}"><span class="stream-type-badge">${esc(item.stream_type)}<small>${esc(item.track_language || "und")}</small></span><div><small>Current value</small><strong>${esc(item.old_value||'(empty)')}</strong></div><span class="suggestion-arrow">→</span><label><small>Suggested replacement</small><input type="text" value="${attr(item.new_value)}"></label><span class="suggestion-count">${item.use_count} uses${item.enabled?'':' · paused'}</span><div class="suggestion-actions"><button type="button" data-save-suggestion>Save</button><button type="button" data-toggle-suggestion>${item.enabled?'Pause':'Resume'}</button><button type="button" data-reset-suggestion>Reset</button><button type="button" class="danger" data-delete-suggestion>Delete</button></div></div>`).join(''):'<p class="muted">No learned suggestions match.</p>';
    $('#learned-suggestion-list').querySelectorAll('.learned-suggestion-item').forEach(row=>wireSuggestionRow(row));
  }
  async function updateSuggestion(item,action,replacement){const result=await api('/api/v59/settings/track-name-suggestions',{method:'PUT',body:JSON.stringify(suggestionPayload(item,action,replacement))});learnedSuggestions=result.suggestions;renderLearnedSuggestions()}
  function wireSuggestionRow(row){const item=learnedSuggestions[Number(row.dataset.suggestionIndex)],input=row.querySelector('input');row.querySelector('[data-save-suggestion]').onclick=()=>updateSuggestion(item,'rename',input.value).then(()=>toast('Learned replacement updated')).catch(error=>toast(error.message,true));row.querySelector('[data-toggle-suggestion]').onclick=()=>updateSuggestion(item,item.enabled?'pause':'resume').then(()=>toast(item.enabled?'Suggestion paused':'Suggestion resumed')).catch(error=>toast(error.message,true));row.querySelector('[data-reset-suggestion]').onclick=()=>updateSuggestion(item,'reset').then(()=>toast('Suggestion usage reset')).catch(error=>toast(error.message,true));row.querySelector('[data-delete-suggestion]').onclick=async()=>{if(!confirm(`Delete the learned correction “${item.old_value||'(empty)'}” → “${item.new_value}”?`))return;try{const result=await api('/api/v59/settings/track-name-suggestions',{method:'DELETE',body:JSON.stringify(item)});learnedSuggestions=result.suggestions;renderLearnedSuggestions();toast('Learned suggestion deleted')}catch(error){toast(error.message,true)}}}
  async function loadLearnedSuggestions(){try{const result=await api('/api/v59/settings/track-name-suggestions');learnedSuggestions=result.suggestions;renderLearnedSuggestions()}catch(error){$('#learned-suggestion-list').innerHTML=`<p class="error">${esc(error.message)}</p>`}}
  $('#learned-suggestion-search').oninput=renderLearnedSuggestions;
  $('#clear-learned-suggestions').onclick=async()=>{if(!confirm('Delete all learned track-name suggestions? This cannot be undone.'))return;try{await api('/api/v59/settings/track-name-suggestions/all',{method:'DELETE'});learnedSuggestions=[];renderLearnedSuggestions();toast('All learned suggestions deleted')}catch(error){toast(error.message,true)}};

  const tabbedLoadRoots=loadRoots;
  loadRoots=async function(){await tabbedLoadRoots();if((localStorage.getItem(TAB_KEY)||'plex')==='suggestions')await loadLearnedSuggestions()};
})();
