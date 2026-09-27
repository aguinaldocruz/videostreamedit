(function () {
  const page = document.querySelector('#reports');
  const grid = page?.querySelector('.reports-groups');
  const dialog = document.querySelector('#image-subtitle-report-dialog');
  if (!page || !grid || !dialog || typeof api !== 'function') return;
  grid.insertAdjacentHTML('beforeend','<section class="reports-group" data-report-group="video-titles"><h3>Video stream titles</h3><p>Media with a title stored on a video stream. Remove these titles without changing audio or subtitles.</p><div class="reports-group-actions"><button type="button" data-video-title-report="movies">Movies</button><button type="button" data-video-title-report="tv">TV Shows</button></div></section>');

  const categories = [
    ['Subtitle quality', 'Find subtitles that need inspection or cleanup.', ['image', 'damaged', 'html', 'confidence']],
    ['Language', 'Review language metadata and language-based findings.', ['language', 'duplicate_audio', 'duplicate_subtitle', 'uncommon']],
    ['Stream configuration', 'Find missing, external, or forced streams.', ['forced', 'english_only', 'audio_only', 'external_only']],
    ['Video metadata', 'Review and clean video-stream metadata.', ['video_titles']]
  ];
  const selectors = {
    image: '[data-image-subtitle-report]', damaged: '[data-damaged-subtitle-report]',
    html: '[data-html-subtitle-report]', confidence: '[data-no-confidence-report]',
    language: '[data-portuguese-report]', duplicate_audio: '[data-duplicate-language-report$=":audio"]',
    duplicate_subtitle: '[data-duplicate-language-report$=":subtitle"]',
    uncommon: '[data-uncommon-language-report]', forced: '[data-forced-report]',
    english_only: '[data-english-only-report]', audio_only: '[data-audio-only-report]',
    external_only: '[data-external-only-report]', video_titles: '[data-video-title-report]'
  };
  const cards = {};
  Object.entries(selectors).forEach(([key, selector]) => {
    const card = grid.querySelector(selector)?.closest('.reports-group');
    if (!card) return;
    card.dataset.reportKey = key;
    const status = document.createElement('span');
    status.className = 'report-card-status';
    status.textContent = 'Checking availability…';
    status.setAttribute('role', 'status');
    card.querySelector('.reports-group-actions')?.append(status);
    cards[key] = card;
  });
  grid.replaceChildren();
  categories.forEach(([heading, description, keys]) => {
    const section = document.createElement('section');
    section.className = 'reports-category';
    section.innerHTML = `<div class="reports-category-heading"><div><h3>${heading}</h3><p>${description}</p></div></div><div class="reports-category-grid"></div>`;
    keys.forEach(key => { if (cards[key]) section.querySelector('.reports-category-grid').append(cards[key]); });
    grid.append(section);
  });
  page.querySelector('[data-report-group="external-only"] h3').textContent = 'External subtitles';
  const heading = page.querySelector('.page-title p');
  if (heading) heading.textContent = 'Review indexed media findings and open the affected streams directly.';
  const note = page.querySelector('.reports-note');
  if (note) note.textContent = 'Reports reflect indexed data. Pending edits and media not indexed yet may be absent.';

  let availabilityPromise = null;
  let lastAvailability = 0;
  let availability = null;
  const updateCategories = () => {
    grid.querySelectorAll('.reports-category').forEach(section => {
      section.hidden = !section.querySelector('.reports-group:not([hidden])');
    });
  };
  window.applyReportVisibility = function () {
    Object.entries(cards).forEach(([key, card]) => {
      card.hidden = localStorage.getItem('vse.report.' + key) === 'hidden' ||
        !card.querySelector('.reports-group-actions button:not([hidden])');
    });
    updateCategories();
    emptyLanding.hidden = !!grid.querySelector('.reports-category:not([hidden])');
  };
  const emptyLanding = document.createElement('p');
  emptyLanding.className = 'reports-empty';
  emptyLanding.hidden = true;
  emptyLanding.textContent = 'No enabled reports have current findings. Report visibility can be changed in Setup → Appearance.';
  grid.after(emptyLanding);
  window.addEventListener('vse-report-visibility-changed', window.applyReportVisibility);
  const reportKind = button => {
    const value = button.dataset.duplicateLanguageReport || button.dataset.imageSubtitleReport ||
      button.dataset.damagedSubtitleReport || button.dataset.htmlSubtitleReport ||
      button.dataset.noConfidenceReport || button.dataset.portugueseReport ||
      button.dataset.uncommonLanguageReport || button.dataset.forcedReport ||
      button.dataset.englishOnlyReport || button.dataset.audioOnlyReport ||
      button.dataset.externalOnlyReport || button.dataset.videoTitleReport || '';
    return value.split(':')[0];
  };
  window.refreshReportAvailability = function (force = false) {
    if (availabilityPromise) return availabilityPromise;
    if (!force && availability && Date.now() - lastAvailability < 15000) return Promise.resolve(availability);
    page.classList.add('reports-checking');
    Object.values(cards).forEach(card => {
      const status = card.querySelector('.report-card-status');
      if (status) status.textContent = 'Checking availability…';
    });
    availabilityPromise = api('/api/v19/reports/availability').then(result => {
      availability = result.reports || {};
      lastAvailability = Date.now();
      Object.entries(cards).forEach(([key, card]) => {
        const values = availability[key] || {};
        const available = [];
        card.querySelectorAll('.reports-group-actions button').forEach(button => {
          const kind = reportKind(button);
          button.hidden = values[kind] === false;
          const total = result.counts?.[key]?.[kind];
          if (Number.isFinite(total)) button.textContent = `${kind === 'tv' ? 'TV Shows' : 'Movies'} · ${total.toLocaleString()}${kind === 'tv' ? (total === 1 ? ' episode' : ' episodes') : ''}`;
          if (!button.hidden) available.push(kind === 'tv' ? 'TV Shows' : 'Movies');
        });
        card.querySelector('.report-card-status').textContent = available.length ? 'Indexed' : 'No current findings';
      });
      page.classList.remove('reports-checking', 'reports-availability-error');
      window.applyReportVisibility?.();
      return availability;
    }).catch(error => {
      page.classList.remove('reports-checking');
      page.classList.add('reports-availability-error');
      Object.values(cards).forEach(card => {
        card.querySelector('.report-card-status').textContent = 'Availability unavailable · open to check';
      });
      return null;
    }).finally(() => { availabilityPromise = null; });
    return availabilityPromise;
  };
  const title = dialog.querySelector('[data-report-title]');
  function scopeControls() {
    if (dialog.dataset.reportKey !== 'html') dialog.querySelector('[data-report-html-fix]').hidden = true;
    if (dialog.dataset.reportKey !== 'language') dialog.querySelector('[data-report-language-fix]').hidden = true;
    const confidence = dialog.querySelector('[data-no-confidence-controls]');
    if (confidence) confidence.hidden = dialog.dataset.reportKey !== 'confidence';
  }
  new MutationObserver(scopeControls).observe(title, {childList: true, characterData: true, subtree: true});

  // A single toolbar works for all report renderers, including large TV trees.
  const body = dialog.querySelector('[data-report-items]');
  const actionbar = document.createElement('div');
  actionbar.className = 'report-result-actions';
  actionbar.append(dialog.querySelector('[data-report-html-fix]'), dialog.querySelector('[data-report-language-fix]'));
  actionbar.hidden = true;
  body.before(actionbar);
  const syncActions = () => {
    const hidden = [...actionbar.children].every(button => button.hidden);
    if (actionbar.hidden !== hidden) actionbar.hidden = hidden;
  };
  const actionObserver = new MutationObserver(syncActions);
  [...actionbar.children].forEach(button => actionObserver.observe(button, {attributes: true, attributeFilter: ['hidden']}));
  const toolbar = document.createElement('div');
  toolbar.className = 'report-result-toolbar';
  toolbar.innerHTML = '<label>Find in report<input type="search" autocomplete="off" placeholder="Title, episode, language, or reason" aria-label="Find in report"></label><span data-report-visible-count role="status"></span><button type="button" data-report-refresh>Refresh</button><small>Search filters the view. Bulk actions apply to the entire report.</small>';
  actionbar.before(toolbar);
  const search = toolbar.querySelector('input');
  const count = toolbar.querySelector('[data-report-visible-count]');
  const more = document.createElement('button');
  more.type = 'button';
  more.textContent = 'Show 100 more';
  more.hidden = true;
  more.className = 'report-load-more';
  dialog.querySelector('.dialog-actions').prepend(more);
  let limit = 100;
  let activeButton = null;
  let restoreView = null;
  let restoring = false;
  window.captureReportView = () => ({query: search.value, scroll: body.scrollTop, limit,
    expanded: [...body.querySelectorAll('.report-show-group[open]')].map(row => row.querySelector('summary strong')?.textContent)});
  window.resumeReportView = view => {
    if (!activeButton) return;
    restoreView = view;
    restoring = true;
    activeButton.onclick();
    restoring = false;
  };
  window.refreshActiveReportAfterQueue = key => {
    window.refreshReportAvailability(true);
    if (dialog.open && dialog.dataset.reportKey === key) window.resumeReportView(window.captureReportView());
  };
  toolbar.querySelector('[data-report-refresh]').onclick = () => window.resumeReportView(window.captureReportView());
  function filterRows() {
    scopeControls();
    if (restoreView && !body.querySelector('.reports-loading')) {
      search.value = restoreView.query || '';
      limit = restoreView.limit || 100;
      body.querySelectorAll('.report-show-group').forEach(row => { row.open = restoreView.expanded?.includes(row.querySelector('summary strong')?.textContent); });
    }
    const query = search.value.trim().toLocaleLowerCase();
    const rows = [...body.querySelectorAll(':scope > .report-item, :scope > .report-show-group, :scope > .report-episode-row')];
    let shown = 0;
    rows.forEach(row => {
      const children = row.matches('.report-show-group') ? [...row.querySelectorAll('.report-show-episodes > .report-item, .report-show-episodes > .report-episode-row, .report-subitem')] : [];
      const titleText = row.querySelector(':scope > summary strong')?.textContent?.toLocaleLowerCase() || '';
      const titleMatches = query && titleText.includes(query);
      let childMatches = 0;
      children.forEach(child => {
        const match = !query || titleMatches || child.textContent.toLocaleLowerCase().includes(query);
        child.hidden = !match;
        if (match) childMatches++;
      });
      const match = !query || titleMatches || (!children.length && row.textContent.toLocaleLowerCase().includes(query)) || childMatches > 0;
      if (match) shown++;
      row.hidden = !match || shown > limit;
      if (row.matches('.report-show-group')) {
        if (query && row.dataset.beforeSearchOpen === undefined) row.dataset.beforeSearchOpen = String(row.open);
        if (query && children.length && match) row.open = true;
        else if (!query && row.dataset.beforeSearchOpen !== undefined) {
          row.open = row.dataset.beforeSearchOpen === 'true';
          delete row.dataset.beforeSearchOpen;
        }
      }
    });
    toolbar.hidden = !!body.querySelector('.reports-loading');
    search.disabled = !rows.length;
    more.hidden = shown <= limit;
    count.textContent = `${Math.min(limit, shown)} of ${shown} ${query ? 'matching ' : ''}results`;
    if (restoreView && !body.querySelector('.reports-loading')) {
      body.scrollTop = restoreView.scroll || 0;
      restoreView = null;
    }
  }
  search.addEventListener('input', () => { limit = 100; filterRows(); body.scrollTop = 0; });
  more.onclick = () => { limit += 100; filterRows(); };
  new MutationObserver(filterRows).observe(body, {childList: true, subtree: true});

  // English-only uses the same report-bound navigation and safe indexed actions as other reports.
  page.querySelectorAll('[data-english-only-report]').forEach(button => {
    button.onclick = async () => {
      const kind = button.dataset.englishOnlyReport;
      title.textContent = kind === 'tv' ? 'TV Shows with English-only streams' : 'Movies with English-only streams';
      dialog.querySelector('[data-report-summary]').textContent = 'Loading indexed report…';
      body.innerHTML = '<p class="reports-loading vse-status" data-status="pending">Loading…</p>';
      dialog.showModal();
      button.disabled = true;
      try {
        const result = await reportRequest('/api/v19/reports/english-only?kind=' + encodeURIComponent(kind));
        const groups = result.items || [];
        const media = kind === 'tv' ? groups.flatMap(group => (group.episodes || []).map(episode => ({...episode, label: `${group.title} · ${episode.episode}`}))) : groups.map(movie => ({...movie, label: movie.title}));
        const nav = uniqueReportNavigation(media.map(item => ({path: item.path, label: item.label})));
        dialog.querySelector('[data-report-summary]').textContent = `${result.title_count || 0} titles · ${result.media_count || 0} media`;
        const row = (item, index, label) => `<div class="report-item vse-panel"><button type="button" class="report-title-link vse-link" data-english-index="${index}">${esc(label)}</button><span>${item.mode === 'audio_only_english' ? 'English audio · no subtitles' : 'English subtitles · no audio'}</span><button type="button" class="report-item-action vse-btn" data-english-index="${index}">Stream properties</button></div>`;
        body.innerHTML = kind === 'tv' ? groups.map(group => {
          const episodes = group.episodes || [];
          return `<details class="report-show-group"><summary><strong>${esc(group.title)}</strong><span>${episodes.length} episode${episodes.length === 1 ? '' : 's'}</span></summary><div class="report-show-episodes">${episodes.map(episode => row(episode, media.findIndex(item => item.path === episode.path), episode.episode)).join('')}</div></details>`;
        }).join('') : groups.map((movie, index) => row(movie, index, movie.title)).join('');
        if (!groups.length) body.innerHTML = '<p class="reports-empty vse-status" data-status="current">No English-only media found.</p>';
        body.querySelectorAll('[data-english-index]').forEach(node => {
          node.onclick = () => {
            const item = media[Number(node.dataset.englishIndex)];
            if (item) openReportMediaEditor(item.path, item.label, nav, dialog);
          };
        });
      } catch (error) {
        if (error?.reportStale) return;
        body.innerHTML = `<p class="reports-error vse-status" data-status="failed">${esc(error.message)}</p>`;
      } finally { button.disabled = false; }
    };
  });

  page.querySelectorAll('[data-video-title-report]').forEach(button => {
    button.onclick=async()=>{
      const kind=button.dataset.videoTitleReport;
      title.textContent=(kind==='tv'?'TV Shows':'Movies')+' with video stream titles';
      body.innerHTML='<p class="reports-loading">Loading indexed report…</p>';
      if(!dialog.open)dialog.showModal();
      button.disabled=true;
      try{
        const result=await reportRequest('/api/v19/reports/video-titles?kind='+kind);
        const groups=result.items||[], media=kind==='tv'?groups.flatMap(g=>g.episodes.map(e=>({...e,label:g.title+' · '+e.episode}))):groups.map(m=>({...m,label:m.title}));
        const nav=uniqueReportNavigation(media.map(m=>({path:m.path,label:m.label})));
        dialog.querySelector('[data-report-summary]').textContent=`${result.title_count} titles · ${result.media_count} media · queued Matroska metadata cleanup`;
        const row=(m,label)=>{const i=media.findIndex(e=>e.path===m.path);return `<div class="report-item vse-panel"><div><button type="button" class="report-title-link" data-video-edit="${i}">${esc(label)}</button><span>Video title: ${esc(m.video_title)}</span></div><button type="button" class="report-item-action" data-video-remove="${i}">Queue removal</button><button type="button" class="report-item-action" data-video-edit="${i}">Stream properties</button></div>`};
        body.innerHTML=media.length?'<div class="report-result-actions"><button type="button" data-video-all>Queue removal for all listed</button></div>'+ (kind==='tv'?groups.map((g,i)=>`<details class="report-show-group"><summary><strong>${esc(g.title)}</strong><span>${g.episodes.length} episodes</span><button type="button" class="report-item-action" data-video-show="${i}">Queue show removal</button></summary><div class="report-show-episodes">${g.episodes.map(e=>row(e,e.episode)).join('')}</div></details>`).join(''):groups.map(m=>row(m,m.title)).join('')):'<p class="reports-empty">No media with video stream titles found.</p>';
        const queue=async(paths)=>{
          if(!confirm(`Queue video-stream title removal for ${paths.length} media? Audio, subtitles and container titles are preserved.`))return;
          const controls=[...body.querySelectorAll('button')];controls.forEach(b=>b.disabled=true);
          beginGlobalBusy('Queueing video-stream title removal',true);
          try{
            const queued=await api('/api/v19/reports/video-titles/remove',{method:'POST',body:JSON.stringify({kind,paths})});
            toast(queued.queued?`${queued.queued} media submitted · preflight #${queued.preflight_id}`:'No eligible media remain');
            await button.onclick();window.refreshReportAvailability?.(true);
          }catch(error){toast(error.message,true);controls.forEach(b=>b.disabled=false)}finally{endGlobalBusy()}
        };
        body.querySelector('[data-video-all]')?.addEventListener('click',()=>queue(media.map(m=>m.path)));
        body.querySelectorAll('[data-video-show]').forEach(b=>b.onclick=e=>{e.preventDefault();e.stopPropagation();queue(groups[Number(b.dataset.videoShow)].episodes.map(m=>m.path))});
        body.querySelectorAll('[data-video-remove]').forEach(b=>b.onclick=()=>queue([media[Number(b.dataset.videoRemove)].path]));
        body.querySelectorAll('[data-video-edit]').forEach(b=>b.onclick=()=>{const m=media[Number(b.dataset.videoEdit)];openReportMediaEditor(m.path,m.label,nav,dialog)});
      }catch(error){if(!error?.reportStale)body.innerHTML=`<p class="reports-error">${esc(error.message)}</p>`}finally{button.disabled=false}
    };
  });

  // Reset only when opening a new report, not when returning from its editor.
  Object.entries(cards).forEach(([key, card]) => card.querySelectorAll('button').forEach(button => {
    const open = button.onclick;
    if (typeof open !== 'function') return;
    button.onclick = (...args) => {
      window.reportEpoch = (window.reportEpoch || 0) + 1;
      if (!restoring) { search.value = ''; limit = 100; restoreView = null; }
      activeButton = button;
      dialog.dataset.reportKey = key;
      dialog.querySelector('[data-report-html-fix]').hidden = true;
      dialog.querySelector('[data-report-language-fix]').hidden = true;
      const preflight = dialog.querySelector('[data-report-preflight]');
      if (preflight) { preflight.hidden = true; preflight.querySelector('[data-report-preflight-content]').replaceChildren(); }
      scopeControls();
      return open.apply(button, args);
    };
  }));
  document.querySelector('[data-page="reports"]')?.addEventListener('click', () => window.refreshReportAvailability());
  dialog.addEventListener('close', () => {
    window.reportEpoch = (window.reportEpoch || 0) + 1;
    window.refreshReportAvailability(true);
  });
  window.refreshReportAvailability();
})();
