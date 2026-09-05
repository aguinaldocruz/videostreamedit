window.templateSavePromptsEnabled = true;
window.trackNameCorrectionPromptsEnabled = true;

(function () {
  const setupGrid = document.querySelector('.import-setup-grid') || $('#setup');
  setupGrid.insertAdjacentHTML('beforeend', `<article id="prompt-settings">
    <h3>Editing prompts</h3>
    <p>Pause or resume optional questions without deleting saved templates or learned correction history.</p>
    <label class="prompt-setting"><input type="checkbox" id="ask-save-templates"><span><strong>Ask to save change templates</strong><small>Offer to save each successfully applied change as a reusable template.</small></span></label>
    <label class="prompt-setting"><input type="checkbox" id="offer-track-name-corrections"><span><strong>Offer track-name corrections</strong><small>Suggest commonly used track-name corrections when stream properties open.</small></span></label>
    <p id="prompt-settings-status" class="plex-status" aria-live="polite"></p>
  </article>`);

  const templateToggle = $('#ask-save-templates');
  const correctionToggle = $('#offer-track-name-corrections');
  const status = $('#prompt-settings-status');

  function applyPromptSettings(settings) {
    window.templateSavePromptsEnabled = settings.ask_save_templates;
    window.trackNameCorrectionPromptsEnabled = settings.offer_track_name_corrections;
    templateToggle.checked = settings.ask_save_templates;
    correctionToggle.checked = settings.offer_track_name_corrections;
    if (!settings.offer_track_name_corrections) document.querySelectorAll('.track-name-suggestion').forEach(panel => panel.remove());
    status.textContent = `Template questions ${settings.ask_save_templates ? 'active' : 'paused'} · Track-name suggestions ${settings.offer_track_name_corrections ? 'active' : 'paused'}`;
  }

  async function loadPromptSettings() {
    try { applyPromptSettings(await api('/api/v44/settings/prompts')); }
    catch (error) { status.textContent = error.message; }
  }

  async function savePromptSetting(key, enabled, toggle) {
    toggle.disabled = true;
    try {
      const settings = await api('/api/v44/settings/prompts', {method: 'PUT', body: JSON.stringify({key, enabled})});
      applyPromptSettings(settings);
      toast(`${enabled ? 'Resumed' : 'Paused'} ${key === 'ask_save_templates' ? 'template questions' : 'track-name suggestions'}`);
    } catch (error) {
      toggle.checked = !enabled;
      toast(error.message, true);
    } finally { toggle.disabled = false; }
  }

  templateToggle.onchange = () => savePromptSetting('ask_save_templates', templateToggle.checked, templateToggle);
  correctionToggle.onchange = () => savePromptSetting('offer_track_name_corrections', correctionToggle.checked, correctionToggle);

  const promptSettingsLoadRoots = loadRoots;
  loadRoots = async function () { await promptSettingsLoadRoots(); await loadPromptSettings(); };
  loadPromptSettings();
})();
