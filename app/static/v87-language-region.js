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

  function syncHiddenValue(input) {
    // The hidden language/region inputs still need their normal input event
    // for dirty-state handling, but must not open the inline saved-value menu.
    const wasSuppressed = typeof suppressSavedComboboxInput !== 'undefined' && suppressSavedComboboxInput;
    if (typeof suppressSavedComboboxInput !== 'undefined') suppressSavedComboboxInput = true;
    input.dispatchEvent(new Event('input', {bubbles: true}));
    if (typeof suppressSavedComboboxInput !== 'undefined') suppressSavedComboboxInput = wasSuppressed;
  }

  function makeSelector(values, selected, unchanged, multiple = false, compact = false) {
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
    const isSentinel = value => {
      const key = String(value || '').trim().toLowerCase();
      return key === '__all__' || key === '__unchanged__' || key === '__load_more__' || key.startsWith('__unchanged__|');
    };
    const selectedValues = Array.isArray(selected)
      ? selected.filter(value => !isSentinel(value) && !values.includes(value))
      : (selected && !isSentinel(selected) && !values.includes(selected) ? [selected] : []);
    const ordered = [...new Set(values.filter(value => !isSentinel(value)).concat(selectedValues))]
      .sort((left, right) => usageFor(right) - usageFor(left) || labelFor(left).localeCompare(labelFor(right)));
    const visible = compact && ordered.length > 4 ? ordered.slice(0, 4) : ordered;
    if (compact && ordered.length > 4 && !Array.isArray(selected) && selected && !visible.includes(selected)) {
      visible[visible.length - 1] = selected;
    }
    visible.forEach(value => option(select, value, selected));
    if (compact && ordered.length > 4) {
      const more = document.createElement('option');
      more.value = '__load_more__';
      more.textContent = 'Load more…';
      select.append(more);
    }
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
      const values = [...common, ...savedPairs];
      let expanded = false;
      let selectedValue = current;
      let select;
      const rebuild = () => {
        const previous = selectedValue;
        select?.remove();
        select = makeSelector(values, previous, false, false, !expanded);
        select.setAttribute('aria-label', 'Language and region');
        language.before(select);
        select.onchange = () => {
          if (select.value === '__load_more__') { expanded = true; rebuild(); return; }
          selectedValue = select.value;
          recordSelection(select.value);
          const [nextLanguage, nextRegion] = select.value.split('|');
          language.value = nextLanguage; region.value = nextRegion;
          language.dataset.dirty = 'true'; region.dataset.dirty = 'true';
          syncHiddenValue(language);
          syncHiddenValue(region);
        };
      };
      language.classList.add('language-region-internal');
      region.classList.add('language-region-internal');
      rebuild();
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
    let expanded = false;
    let selectedValue = '__unchanged__';
    let select;
    const rebuild = () => {
      const previous = selectedValue;
      select?.remove();
      select = makeSelector(values, previous, true, false, !expanded);
      wrapper.append(select);
      select.onchange = () => {
        if (select.value === '__load_more__') { expanded = true; rebuild(); return; }
        if (select.value === '__unchanged__') return;
        selectedValue = select.value;
        recordSelection(select.value);
        const [nextLanguage, nextRegion] = select.value.split('|');
        language.value = nextLanguage; region.value = nextRegion;
        language.dataset.dirty = 'true'; region.dataset.dirty = 'true';
        syncHiddenValue(language);
        syncHiddenValue(region);
      };
    };
    languageLabel.before(wrapper);
    rebuild();
  }

  function enhanceFilter(container, records) {
    const filters = container?.querySelector('.season-stream-filters,.movie-header-stream-filters');
    if (!filters || filters.dataset.languageRegionReady) return;
    const language = filters.querySelector('[data-season-field=language]');
    const region = filters.querySelector('[data-season-field=region]');
    const streamControl = filters.querySelector('[data-season-field=stream]');
    const trackControl = filters.querySelector('[data-season-field=track_name]');
    if (!language || !region) return;
    filters.dataset.languageRegionReady = 'true';
    language.closest('label').classList.add('language-region-internal');
    region.closest('label').classList.add('language-region-internal');
    const wrapper = document.createElement('label');
    wrapper.textContent = 'Language / region';
    let select;
    function rebuildPairs() {
      const stream = streamControl?.value || '__all__';
      const track = trackControl?.value || '__all__';
      const previous = select?.value || '__all__';
      const pairs = [...new Set((records || [])
        .filter(item => stream === '__all__' || String(item.stream_type).toLowerCase() === String(stream).toLowerCase())
        .filter(item => track === '__all__' || (track === '__empty__' ? !item.track_name : item.track_name === track))
        .map(item => normalized(item.language, item.region)))];
      const initial = filters.parentElement?.dataset.initialSingleton === 'true' && pairs.length === 1 ? pairs[0] : (pairs.includes(previous) ? previous : '__all__');
      const next = makeSelector(['__all__', ...pairs], initial, false);
      select?.replaceWith(next);
      select = next;
      if (filters.parentElement?.dataset.initialSingleton === 'true') delete filters.parentElement.dataset.initialSingleton;
      if (initial !== '__all__') {
        const [initialLanguage, initialRegion] = initial.split('|');
        language.value = initialLanguage || '__empty__';
        region.value = initialRegion || '__empty__';
      }
      select.onchange = () => {
        let selected = [...select.selectedOptions].map(item => item.value);
        if (!selected.length || selected.includes('__all__')) { selected = []; language.value = '__all__'; region.value = '__all__'; }
        else {
          const [nextLanguage, nextRegion] = selected[0].split('|');
          language.value = nextLanguage || '__empty__'; region.value = nextRegion || '__empty__';
        }
        select.dataset.selectedValues = selected.join('\u001f');
        selected.filter(value => !value.startsWith('__')).forEach(recordSelection);
        streamControl?.dispatchEvent(new Event('change', {bubbles: true}));
      };
    }
    wrapper.append(document.createElement('select'));
    select = wrapper.lastElementChild;
    language.closest('label').before(wrapper);
    rebuildPairs();
    streamControl?.addEventListener('change', rebuildPairs);
    trackControl?.addEventListener('change', rebuildPairs);
  }

  function relabelLegacyMovieFilter() {
    const select = document.querySelector('#movie-stream-language');
    if (!select) return;
    [...select.options].forEach(item => {
      if (item.value) { const next = labelFor(normalized(item.value, '')); if (item.textContent !== next) item.textContent = next; }
    });
  }

  // Older selector instances could expose the internal sentinel as a normal
  // option.  Normalize any already-rendered selectors as well as new ones.
  function sanitizeSelectors(root = document) {
    root.querySelectorAll('.language-region-select option').forEach(item => {
      const key = String(item.value || '').trim().toLowerCase();
      if (key === '__unchanged__') {
        const select = item.parentElement;
        const prior = select?.querySelector('option[data-unchanged-normalized="true"]');
        if (prior && prior !== item) { item.remove(); return; }
        if (item.dataset.unchangedNormalized !== 'true') item.dataset.unchangedNormalized = 'true';
        if (item.textContent !== 'Leave unchanged') item.textContent = 'Leave unchanged';
      }
      else if (key === 'unchanged' || key === 'unchanged|' || key.startsWith('__unchanged__|')) item.remove();
    });
  }

  const observer = new MutationObserver(() => {
    enhanceStreamRows(document.querySelector('#stream-content') || document);
    enhanceBulkEditor(document.querySelector('.season-stream-editor'));
    enhanceBulkEditor(document.querySelector('.movie-header-stream-editor'));
    enhanceFilter(document.querySelector('#season-stream-filter-content'), window.currentTvStreamRecords || []);
    enhanceFilter(document.querySelector('#movie-header-stream-filter-content'), window.currentMovieStreamRecords || []);
    sanitizeSelectors();
    relabelLegacyMovieFilter();
  });
  observer.observe(document.body, {childList: true, subtree: true});
})();
