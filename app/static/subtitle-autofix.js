(function(){
  const setup=document.querySelector('#setup');
  const panel=setup?.querySelector('[data-setup-panel="subtitle-autofix"]');
  if(!panel||panel.querySelector('[data-autofix-workspace]'))return;
  const endpoint='/api/settings/subtitle-autofix';
  const safe=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  let languageNames=null,regionNames=null;
  try{languageNames=new Intl.DisplayNames(['en'],{type:'language'});regionNames=new Intl.DisplayNames(['en'],{type:'region'})}catch(_error){}
  const state={rules:[],languages:[],limits:{languages:3,replacements:500,preview_characters:32768},loaded:false,loading:false,busy:false,draft:null,dirty:false};
  panel.innerHTML=`<div class="autofix-workspace" data-autofix-workspace>
    <article class="autofix-intro">
      <div class="autofix-heading"><div><h3>Subtitle autofix rules</h3><p>Build your own language-specific corrections for broken characters, mojibake, or words.</p></div><span class="autofix-badge">Supervised repairs</span></div>
      <p class="autofix-scope">Saving a rule does not modify media or start jobs. Use Autofix beside Stream properties in the Damaged SRT subtitles report to review each corrected stream before choosing Apply now or Queue.</p>
      <div class="autofix-toolbar"><span data-autofix-count>Rules have not been loaded.</span><div><button type="button" class="primary" data-autofix-add disabled>Add rule</button><button type="button" data-autofix-refresh>Refresh</button></div></div>
      <p class="autofix-status" data-autofix-status role="status" aria-live="polite"></p>
    </article>
    <form class="autofix-editor" data-autofix-editor hidden>
      <fieldset data-autofix-fields>
        <div class="autofix-heading"><h3 data-autofix-editor-title>New rule</h3><span class="autofix-badge" data-autofix-draft-state>Unsaved rule</span></div>
        <div class="autofix-identity">
          <label>Rule name<input type="text" data-autofix-name maxlength="120" required autocomplete="off" placeholder="For example: Portuguese character repairs"></label>
          <label>Description <small>(optional)</small><input type="text" data-autofix-description maxlength="1000" autocomplete="off" placeholder="What this rule corrects"></label>
          <label class="autofix-enabled"><input type="checkbox" data-autofix-enabled checked> Enable this rule for future repairs</label>
        </div>
        <section class="autofix-languages"><div class="autofix-section-heading"><h4>Subtitle languages</h4><button type="button" data-autofix-add-language>Add language</button></div>
          <p>Choose 1–3 metadata languages. A language without a region matches all of its regions; a language with a region matches only that variant. No language detection is performed here.</p>
          <div class="autofix-language-list" data-autofix-languages></div>
        </section>
        <section class="autofix-replacements"><div class="autofix-section-heading"><h4>From → To replacements</h4><button type="button" data-autofix-add-replacement>Add replacement</button></div>
          <p>Exact, case-sensitive text — not regular expressions. Empty <strong>To</strong> removes the matched text. Whitespace is preserved. Rows run from top to bottom, so a later row can change an earlier result.</p>
          <div class="autofix-replacement-list" data-autofix-replacements></div>
          <small data-autofix-replacement-count></small>
        </section>
        <section class="autofix-preview" data-autofix-preview><div class="autofix-section-heading"><h4>Try a sample</h4><button type="button" data-autofix-preview-button>Preview replacements</button></div>
          <p>Paste subtitle dialogue, not cue numbers or timestamps. This tests the current draft, including disabled rules, without saving it or changing any subtitle.</p>
          <div class="autofix-preview-grid"><label>Original sample<textarea data-autofix-sample rows="5" maxlength="32768" placeholder="Paste a few lines to check your corrections"></textarea></label><label>Preview result<textarea data-autofix-result rows="5" readonly placeholder="The corrected sample appears here"></textarea></label></div>
          <small data-autofix-preview-status role="status"></small>
        </section>
        <div class="autofix-editor-actions"><small data-autofix-editor-status role="status" aria-live="polite"></small><div><button type="submit" class="primary" data-autofix-save>Save rule</button><button type="button" data-autofix-cancel>Close editor</button></div></div>
      </fieldset>
    </form>
    <div class="autofix-rule-list" data-autofix-rules aria-label="Saved subtitle autofix rules"></div>
  </div>`;
  const find=selector=>panel.querySelector(selector);
  const editor=find('[data-autofix-editor]');
  function languageLabel(value){
    const [language,region]=value.split('-');
    if(language==='und')return 'Undetermined';
    let name=language.toUpperCase(),area=region;
    try{name=languageNames?.of(language)||name}catch(_error){}
    if(region==='BR'&&language==='pt')area='Brazilian';
    else if(region){try{area=regionNames?.of(region)||region}catch(_error){}}
    return region?`${name} — ${area}`:`${name} (all regions)`;
  }
  function message(text,error=false,selector='[data-autofix-status]'){
    const node=find(selector);node.textContent=text;node.classList.toggle('error',error);
  }
  function fields(rule){
    return {name:rule.name,description:rule.description,enabled:rule.enabled,languages:[...rule.languages],replacements:rule.replacements.map(row=>({...row}))};
  }
  function readDraft(){
    if(!state.draft)return null;
    Object.assign(state.draft,{
      name:find('[data-autofix-name]').value,description:find('[data-autofix-description]').value,
      enabled:find('[data-autofix-enabled]').checked,
      languages:[...panel.querySelectorAll('[data-autofix-language]')].map(input=>input.value),
      replacements:[...panel.querySelectorAll('[data-autofix-replacement]')].map(row=>({from:row.querySelector('[data-autofix-from]').value,to:row.querySelector('[data-autofix-to]').value,match:row.querySelector('[data-autofix-match]').value}))
    });
    return state.draft;
  }
  function markDirty(){
    state.dirty=true;
    find('[data-autofix-draft-state]').textContent='Unsaved changes';
    find('[data-autofix-save]').disabled=state.busy;
    find('[data-autofix-result]').value='';
    message('Draft changed. Preview again to check the new result.',false,'[data-autofix-preview-status]');
    message('',false,'[data-autofix-editor-status]');
  }
  function canDiscard(){return !state.dirty||confirm('Discard the unsaved changes to this autofix rule? No media has been changed.');}
  function renderLanguages(){
    const values=state.draft.languages;
    find('[data-autofix-languages]').innerHTML=values.map((selected,index)=>`<div class="autofix-language-row"><label>Language ${index+1}<select data-autofix-language aria-label="Language ${index+1}" required><option value="">Choose a language…</option>${[...new Set([...state.languages,...values.filter(Boolean)])].map(value=>`<option value="${safe(value)}" ${value===selected?'selected':''} ${values.includes(value)&&value!==selected?'disabled':''}>${safe(languageLabel(value))}</option>`).join('')}</select></label><button type="button" data-autofix-remove-language="${index}" aria-label="Remove language ${index+1}" ${values.length===1?'disabled':''}>Remove</button></div>`).join('');
    find('[data-autofix-add-language]').disabled=state.busy||values.length>=state.limits.languages;
  }
  function renderReplacements(){
    const values=state.draft.replacements;
    find('[data-autofix-replacements]').innerHTML=values.map((row,index)=>`<div class="autofix-replacement" data-autofix-replacement><span class="autofix-row-number" aria-label="Replacement ${index+1}">${index+1}</span><label>From<input type="text" data-autofix-from value="${safe(row.from)}" maxlength="1000" required autocomplete="off" spellcheck="false" aria-label="From ${index+1}" placeholder="Exact characters or words"></label><label>To<input type="text" data-autofix-to value="${safe(row.to)}" maxlength="1000" autocomplete="off" spellcheck="false" aria-label="To ${index+1}" placeholder="Empty removes the value"></label><label>Match<select data-autofix-match aria-label="Match ${index+1}"><option value="literal" ${row.match==='literal'?'selected':''}>Anywhere (characters)</option><option value="word" ${row.match==='word'?'selected':''}>Whole word / phrase</option></select></label><div class="autofix-row-actions"><button type="button" data-autofix-move-up="${index}" aria-label="Move replacement ${index+1} up" title="Move up" ${index===0?'disabled':''}>↑</button><button type="button" data-autofix-move-down="${index}" aria-label="Move replacement ${index+1} down" title="Move down" ${index===values.length-1?'disabled':''}>↓</button><button type="button" data-autofix-remove-replacement="${index}" aria-label="Remove replacement ${index+1}" ${values.length===1?'disabled':''}>Remove</button></div></div>`).join('');
    find('[data-autofix-replacement-count]').textContent=`${values.length} replacement${values.length===1?'':'s'} · Applied in the order shown`;
    find('[data-autofix-add-replacement]').disabled=state.busy||values.length>=state.limits.replacements;
  }
  function renderRules(){
    find('[data-autofix-count]').textContent=`${state.rules.length} rule${state.rules.length===1?'':'s'} · ${state.rules.filter(rule=>rule.enabled).length} enabled`;
    find('[data-autofix-rules]').innerHTML=state.rules.length?state.rules.map(rule=>`<article class="autofix-rule ${rule.enabled?'':'autofix-rule-disabled'}"><div class="autofix-rule-information"><div class="autofix-rule-title"><h4>${safe(rule.name)}</h4><span class="autofix-badge ${rule.enabled?'autofix-badge-enabled':''}">${rule.enabled?'Enabled':'Disabled'}</span></div>${rule.description?`<p>${safe(rule.description)}</p>`:''}<div class="autofix-rule-languages">${rule.languages.map(language=>`<span>${safe(languageLabel(language))}</span>`).join('')}</div><small>${rule.replacements.length} replacement${rule.replacements.length===1?'':'s'}${typeof formatAppDate==='function'?` · Updated ${safe(formatAppDate(rule.updated_at))}`:''}</small></div><div class="autofix-rule-actions"><button type="button" data-autofix-edit="${safe(rule.id)}">Edit</button><button type="button" data-autofix-toggle="${safe(rule.id)}" ${state.draft?.id===rule.id?'disabled':''}>${rule.enabled?'Disable':'Enable'}</button><button type="button" class="danger" data-autofix-delete="${safe(rule.id)}">Delete</button></div></article>`).join(''):'<article class="autofix-empty"><h4>No autofix rules yet</h4><p>Add only corrections you have checked. There are no automatic default substitutions, and valid language characters are not treated as errors.</p></article>';
    if(state.busy)panel.querySelectorAll('[data-autofix-rules] button').forEach(button=>button.disabled=true);
  }
  function setBusy(value){
    state.busy=value;panel.setAttribute('aria-busy',String(value));
    find('[data-autofix-fields]').disabled=value;
    find('[data-autofix-add]').disabled=value||!state.loaded;
    find('[data-autofix-refresh]').disabled=value;
    find('[data-autofix-save]').disabled=value||!state.dirty;
    if(state.draft){find('[data-autofix-add-language]').disabled=value||state.draft.languages.length>=state.limits.languages;find('[data-autofix-add-replacement]').disabled=value||state.draft.replacements.length>=state.limits.replacements;}
    renderRules();
  }
  function openEditor(rule=null){
    if(state.busy||!canDiscard())return;
    state.draft=rule?{...fields(rule),id:rule.id,revision:rule.revision}:{name:'',description:'',enabled:true,languages:[''],replacements:[{from:'',to:'',match:'literal'}]};
    state.dirty=!rule;editor.hidden=false;
    find('[data-autofix-editor-title]').textContent=rule?'Edit rule':'New rule';
    find('[data-autofix-name]').value=state.draft.name;find('[data-autofix-description]').value=state.draft.description;find('[data-autofix-enabled]').checked=state.draft.enabled;
    find('[data-autofix-draft-state]').textContent=rule?'Saved · no pending changes':'Unsaved rule';
    find('[data-autofix-sample]').value='';find('[data-autofix-result]').value='';
    message('',false,'[data-autofix-editor-status]');message('',false,'[data-autofix-preview-status]');
    renderLanguages();renderReplacements();setBusy(false);find('[data-autofix-name]').focus();
  }
  function closeEditor(){
    if(state.busy||!canDiscard())return;
    state.draft=null;state.dirty=false;editor.hidden=true;renderRules();
  }
  function validDraft(){
    const rule=fields(readDraft());
    if(!rule.name.trim())throw new Error('Give this rule a name.');
    if(!rule.languages.length||rule.languages.some(language=>!language))throw new Error('Choose 1–3 subtitle languages.');
    if(new Set(rule.languages).size!==rule.languages.length)throw new Error('Select each language/region only once.');
    const seen=new Set();
    rule.replacements.forEach((row,index)=>{
      if(!row.from.length)throw new Error(`Replacement ${index+1}: From cannot be empty.`);
      if(row.from===row.to)throw new Error(`Replacement ${index+1}: From and To must differ.`);
      const key=JSON.stringify([row.from,row.match]);if(seen.has(key))throw new Error(`Replacement ${index+1}: this From value and matching mode are already listed.`);seen.add(key);
    });
    return rule;
  }
  function acceptRule(rule){
    state.rules=state.rules.filter(item=>item.id!==rule.id);state.rules.push(rule);
    state.rules.sort((a,b)=>a.name.localeCompare(b.name)||a.id.localeCompare(b.id));
  }
  async function loadRules(){
    if(state.busy||state.loading)return;
    state.loading=true;setBusy(true);message('Loading saved autofix rules…');
    try{
      const result=await api(endpoint+'/rules');
      state.rules=result.rules||[];state.languages=result.languages||[];Object.assign(state.limits,result.limits||{});state.loaded=true;
      message('Rules are saved configuration. No media is changed until a future repair action is connected.');
      if(state.draft)renderLanguages();
    }catch(error){message(error.message,true)}finally{state.loading=false;setBusy(false)}
  }
  async function saveRule(event){
    event.preventDefault();if(state.busy||!state.dirty)return;
    let payload;try{payload=validDraft()}catch(error){message(error.message,true,'[data-autofix-editor-status]');return}
    const id=state.draft.id;if(id)payload.revision=state.draft.revision;
    setBusy(true);message('Saving rule…',false,'[data-autofix-editor-status]');
    try{
      const result=await api(endpoint+'/rules'+(id?'/'+id:''),{method:id?'PUT':'POST',body:JSON.stringify(payload)});
      acceptRule(result);state.draft={...fields(result),id:result.id,revision:result.revision};state.dirty=false;
      find('[data-autofix-name]').value=result.name;find('[data-autofix-description]').value=result.description;
      find('[data-autofix-editor-title]').textContent='Edit rule';find('[data-autofix-draft-state]').textContent='Saved · no pending changes';
      renderLanguages();
      if(!find('[data-autofix-result]').value)message('Preview a sample to check this saved rule.',false,'[data-autofix-preview-status]');
      message('Rule saved. No media or job queues were changed.',false,'[data-autofix-editor-status]');message('Autofix configuration saved.');
    }catch(error){message(error.message,true,'[data-autofix-editor-status]')}finally{setBusy(false)}
  }
  async function changeSavedRule(id,remove=false){
    if(state.busy)return;const rule=state.rules.find(item=>item.id===id);if(!rule)return;
    if(remove&&!confirm(`Delete the autofix rule “${rule.name}”?\n\nOnly this saved rule will be removed. Media files are not affected.${state.draft?.id===id&&state.dirty?' Unsaved edits to this rule will also be discarded.':''}`))return;
    setBusy(true);message(remove?'Deleting rule…':'Updating rule…');
    try{
      if(remove){await api(`${endpoint}/rules/${id}?revision=${rule.revision}`,{method:'DELETE'});state.rules=state.rules.filter(item=>item.id!==id);if(state.draft?.id===id){state.draft=null;state.dirty=false;editor.hidden=true}message('Rule deleted. Media files were not changed.')}
      else{const result=await api(`${endpoint}/rules/${id}`,{method:'PUT',body:JSON.stringify({...fields(rule),enabled:!rule.enabled,revision:rule.revision})});acceptRule(result);message(`Rule ${result.enabled?'enabled':'disabled'}. No jobs were started.`)}
    }catch(error){message(error.message,true)}finally{setBusy(false)}
  }
  async function preview(){
    if(state.busy||!state.draft)return;
    let rule;try{rule=validDraft()}catch(error){message(error.message,true,'[data-autofix-preview-status]');return}
    setBusy(true);message('Testing replacements on your sample…',false,'[data-autofix-preview-status]');
    try{
      const result=await api(endpoint+'/preview',{method:'POST',body:JSON.stringify({rule,text:find('[data-autofix-sample]').value})});
      find('[data-autofix-result]').value=result.text;
      const rows=(result.counts||[]).map((count,index)=>count?`row ${index+1}: ${count}`:'').filter(Boolean).join(' · ');
      message(`${result.replacement_count} replacement${result.replacement_count===1?'':'s'}${rows?' · '+rows:''}${!result.changed?' · Sample unchanged':''}. Nothing was saved or applied to media.`,false,'[data-autofix-preview-status]');
    }catch(error){find('[data-autofix-result]').value='';message(error.message,true,'[data-autofix-preview-status]')}finally{setBusy(false)}
  }
  panel.addEventListener('click',event=>{
    const button=event.target.closest('button');if(!button||state.busy)return;
    const data=button.dataset;
    if('autofixAdd' in data)openEditor();
    else if('autofixRefresh' in data){if(!canDiscard())return;state.draft=null;state.dirty=false;editor.hidden=true;loadRules()}
    else if('autofixEdit' in data)openEditor(state.rules.find(rule=>rule.id===data.autofixEdit));
    else if('autofixToggle' in data)changeSavedRule(data.autofixToggle);
    else if('autofixDelete' in data)changeSavedRule(data.autofixDelete,true);
    else if('autofixCancel' in data)closeEditor();
    else if('autofixPreviewButton' in data)preview();
    else if(state.draft){
      readDraft();
      if('autofixAddLanguage' in data&&state.draft.languages.length<state.limits.languages){state.draft.languages.push('');renderLanguages();markDirty()}
      else if('autofixRemoveLanguage' in data&&state.draft.languages.length>1){state.draft.languages.splice(Number(data.autofixRemoveLanguage),1);renderLanguages();markDirty()}
      else if('autofixAddReplacement' in data&&state.draft.replacements.length<state.limits.replacements){state.draft.replacements.push({from:'',to:'',match:'literal'});renderReplacements();markDirty();[...panel.querySelectorAll('[data-autofix-from]')].at(-1)?.focus()}
      else if('autofixRemoveReplacement' in data&&state.draft.replacements.length>1){state.draft.replacements.splice(Number(data.autofixRemoveReplacement),1);renderReplacements();markDirty()}
      else if('autofixMoveUp' in data||'autofixMoveDown' in data){const index=Number(data.autofixMoveUp??data.autofixMoveDown),destination=index+('autofixMoveUp' in data?-1:1),rows=state.draft.replacements;if(destination>=0&&destination<rows.length){[rows[index],rows[destination]]=[rows[destination],rows[index]];renderReplacements();markDirty()}}
    }
  });
  editor.addEventListener('submit',saveRule);
  editor.addEventListener('input',event=>{if(event.target.closest('[data-autofix-preview]')||state.busy)return;readDraft();markDirty()});
  editor.addEventListener('change',event=>{if(event.target.closest('[data-autofix-preview]')||state.busy)return;readDraft();if(event.target.matches('[data-autofix-language]'))renderLanguages();markDirty()});
  find('[data-autofix-sample]').addEventListener('input',()=>{find('[data-autofix-result]').value='';message('Sample changed. Preview again to see the result.',false,'[data-autofix-preview-status]')});
  window.addEventListener('beforeunload',event=>{if(state.dirty){event.preventDefault();event.returnValue=''}});
  function loadWhenVisible(){if(!setup.classList.contains('hidden')&&!panel.classList.contains('hidden')&&!state.loaded)loadRules()}
  document.addEventListener('setup-section-opened',event=>{if(event.detail?.section==='subtitle-autofix')loadWhenVisible()});
  // Only watch the page's visibility, not editor/list DOM mutations.
  new MutationObserver(loadWhenVisible).observe(setup,{attributes:true,attributeFilter:['class']});
  loadWhenVisible();
})();
