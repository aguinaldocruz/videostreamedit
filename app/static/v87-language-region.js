(function () {
  const languageNames = typeof Intl.DisplayNames === 'function' ? new Intl.DisplayNames(['en'], {type: 'language'}) : null;
  const regionNames = typeof Intl.DisplayNames === 'function' ? new Intl.DisplayNames(['en'], {type: 'region'}) : null;
  const common = ['und|','pt|PT','pt|BR','en|','en|US','en|GB','es|','es|ES','es|MX','fr|','fr|FR','fr|CA','de|','it|','ja|','ko|','zh|CN','zh|TW','ar|','nl|','pl|','ru|','tr|','sv|','no|','da|','fi|','el|','he|','hi|'];

  function normalized(language, region) {
    return `${String(language || '').trim().toLowerCase()}|${String(region || '').trim().toUpperCase()}`;
  }

  function labelFor(value) {
    const [language, region] = value.split('|');
    if (!language && !region) return '<empty>';
    if (language === 'und') return region ? `Undetermined — ${region}` : 'Undetermined';
    if (language === 'pt' && region === 'BR') return 'Portuguese — Brazilian';
    if (language === 'pt' && (region === 'PT' || !region)) return 'Portuguese — Portugal';
    let languageName;
    try { languageName = languageNames?.of(language); } catch (_) {}
    languageName = languageName && languageName !== language ? languageName : language.toUpperCase();
    let regionName = '';
    if (region) {
      try { regionName = regionNames?.of(region); } catch (_) {}
      regionName = regionName && regionName !== region ? regionName : region;
    } else if (language === 'pt') regionName = 'Portugal';
    return regionName ? `${languageName} — ${regionName}` : languageName;
  }

  function option(select, value, selected) {
    const item = document.createElement('option');
    item.value = value;
    item.textContent = labelFor(value);
    item.selected = Array.isArray(selected) ? selected.includes(value) : value === selected;
    select.append(item);
  }

  function usageFor(value) {
    return Number((typeof v8Saved !== 'undefined' && v8Saved.language_region_usage?.[value]) || 0);
  }

  function recordSelection(value) {
    if (!value || value.startsWith('__')) return;
    if (typeof v8Saved !== 'undefined') {
      v8Saved.language_region_usage ||= {};
      v8Saved.language_region_usage[value] = usageFor(value) + 1;
    }
    api('/api/v86/language-region-use', {
      method: 'POST', body: JSON.stringify({value})
    }).then(result => {
      if (typeof v8Saved !== 'undefined') v8Saved.language_region_usage[result.value] = result.use_count;
    }).catch(error => console.warn('Could not record language selection usage', error));
  }

  function makeSelector(values, selected, unchanged, multiple = false) {
    const select = document.createElement('select');
    select.className = 'language-region-select';
    if (multiple) { select.multiple = true; select.setAttribute('aria-label', 'Language and region filters (multiple selection)'); select.title = 'Hold Ctrl or Command to select multiple values'; }
    if (unchanged) {
      const item = document.createElement('option');
      item.value = '__unchanged__'; item.textContent = 'Leave unchanged'; select.append(item);
    } else if (values.includes('__all__')) {
      const item = document.createElement('option');
      item.value = '__all__'; item.textContent = 'All languages and regions'; select.append(item);
    }
    [...new Set(values.filter(value => value !== '__all__').concat(Array.isArray(selected) ? selected.filter(value => !values.includes(value)) : (selected && !values.includes(selected) ? [selected] : [])))]
      .sort((left, right) => usageFor(right) - usageFor(left) || labelFor(left).localeCompare(labelFor(right)))
      .forEach(value => option(select, value, selected));
    return select;
  }

  function enhanceStreamRows(root) {
    root.querySelectorAll('.stream-row:not([data-language-region-ready])').forEach(row => {
      const language = row.querySelector('input[name=language]');
      const region = row.querySelector('input[name=region]');
      if (!language || !region) return;
      row.dataset.languageRegionReady = 'true';
      const current = normalized(language.value, region.value);
      const savedLanguages = (((typeof v8Saved !== "undefined" && v8Saved.language) || []) || []).map(value => normalized(value, ''));
      const savedRegions = ((typeof v8Saved !== "undefined" && v8Saved.region) || []) || [];
      const savedPairs = savedLanguages.flatMap(value => {
        const languageCode = value.split('|')[0];
        return [value, ...savedRegions.map(regionCode => normalized(languageCode, regionCode))];
      });
      const select = makeSelector([...common, ...savedPairs], current, false);
      select.setAttribute('aria-label', 'Language and region');
      language.classList.add('language-region-internal');
      region.classList.add('language-region-internal');
      language.before(select);
      select.onchange = () => {
        recordSelection(select.value);
        const [nextLanguage, nextRegion] = select.value.split('|');
        language.value = nextLanguage; region.value = nextRegion;
        language.dataset.dirty = 'true'; region.dataset.dirty = 'true';
        language.dispatchEvent(new Event('input', {bubbles: true}));
        region.dispatchEvent(new Event('input', {bubbles: true}));
      };
    });
    const head = root.querySelector('.stream-grid.v7.head');
    if (head && !head.dataset.languageRegionReady) {
      head.dataset.languageRegionReady = 'true';
      const cells = head.children;
      if (cells[2]) cells[2].textContent = 'Language / region';
      if (cells[3]) cells[3].classList.add('language-region-internal');
    }
  }

  function enhanceBulkEditor(editor) {
    if (!editor || editor.dataset.languageRegionReady) return;
    const language = editor.querySelector('input[name=language]');
    const region = editor.querySelector('input[name=region]');
    if (!language || !region) return;
    editor.dataset.languageRegionReady = 'true';
    const languageLabel = language.closest('label');
    const regionLabel = region.closest('label');
    languageLabel.classList.add('language-region-internal');
    regionLabel.classList.add('language-region-internal');
    const wrapper = document.createElement('label');
    wrapper.textContent = 'Language / region';
    const values = [...common];
    for (const value of ((typeof v8Saved !== "undefined" && v8Saved.language) || [])) values.push(normalized(value, ''));
    const select = makeSelector(values, '', true);
    wrapper.append(select);
    languageLabel.before(wrapper);
    select.onchange = () => {
      if (select.value === '__unchanged__') return;
      recordSelection(select.value);
      const [nextLanguage, nextRegion] = select.value.split('|');
      language.value = nextLanguage; region.value = nextRegion;
      language.dataset.dirty = 'true'; region.dataset.dirty = 'true';
      language.dispatchEvent(new Event('input', {bubbles: true}));
      region.dispatchEvent(new Event('input', {bubbles: true}));
    };
  }

  function enhanceFilter(container, records) {
    const filters = container?.querySelector('.season-stream-filters,.movie-header-stream-filters');
    if (!filters || filters.dataset.languageRegionReady) return;
    const language = filters.querySelector('[data-season-field=language]');
    const region = filters.querySelector('[data-season-field=region]');
    if (!language || !region) return;
    filters.dataset.languageRegionReady = 'true';
    language.closest('label').classList.add('language-region-internal');
    region.closest('label').classList.add('language-region-internal');
    const wrapper = document.createElement('label');
    wrapper.textContent = 'Language / region';
    const pairs = [...new Set((records || []).map(item => normalized(item.language, item.region)))];
    const select = makeSelector(['__all__', ...pairs], '__all__', false);
    wrapper.append(select);
    language.closest('label').before(wrapper);
    select.onchange = () => {
      let selected = [...select.selectedOptions].map(item => item.value);
      if (selected.includes('__all__') && selected.length > 1) {
        selected = selected.filter(value => value !== '__all__');
        [...select.options].forEach(item => { item.selected = selected.includes(item.value); });
      }
      if (!selected.length || selected.includes('__all__')) { language.value = '__all__'; region.value = '__all__'; }
      else {
        const [nextLanguage, nextRegion] = selected[0].split('|');
        language.value = nextLanguage || '__empty__';
        region.value = nextRegion || '__empty__';
      }
      select.dataset.selectedValues = selected.join('\u001f');
      selected.filter(value => !value.startsWith('__')).forEach(recordSelection);
      region.dispatchEvent(new Event('change', {bubbles: true}));
      const stream = filters.querySelector('[data-season-field=stream]')?.value || '__all__';
      const trackName = filters.querySelector('[data-season-field=track_name]');
      if (!trackName) return;
      const selectedPairs = selected.filter(value => !value.startsWith('__'));
      const names = [...new Set((records || [])
        .filter(item => (stream === '__all__' || item.stream_type === stream)
          && (!selectedPairs.length || selectedPairs.includes(normalized(item.language, item.region))))
        .map(item => item.track_name))].sort((left, right) => left.localeCompare(right));
      const previous = trackName.value;
      trackName.innerHTML = '<option value="__all__">All track names</option>';
      names.forEach(name => {
        const item = document.createElement('option');
        item.value = name === '' ? '__empty__' : name;
        item.textContent = name === '' ? '<empty>' : name;
        trackName.append(item);
      });
      trackName.value = [...trackName.options].some(item => item.value === previous) ? previous : '__all__';
      if (trackName.value !== previous) trackName.dispatchEvent(new Event('change', {bubbles: true}));
    };
  }

  function relabelLegacyMovieFilter() {
    const select = document.querySelector('#movie-stream-language');
    if (!select) return;
    [...select.options].forEach(item => {
      if (item.value) { const next = labelFor(normalized(item.value, '')); if (item.textContent !== next) item.textContent = next; }
    });
  }

  const observer = new MutationObserver(() => {
    enhanceStreamRows(document.querySelector('#stream-content') || document);
    enhanceBulkEditor(document.querySelector('.season-stream-editor'));
    enhanceBulkEditor(document.querySelector('.movie-header-stream-editor'));
    enhanceFilter(document.querySelector('#season-stream-filter-content'), window.currentTvStreamRecords || []);
    enhanceFilter(document.querySelector('#movie-header-stream-filter-content'), window.currentMovieStreamRecords || []);
    relabelLegacyMovieFilter();
  });
  observer.observe(document.body, {childList: true, subtree: true});
})();
