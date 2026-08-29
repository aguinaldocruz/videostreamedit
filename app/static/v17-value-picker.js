function installSavedValuePickers(root = document) {
  root.querySelectorAll('.stream-row input[type="text"][name]').forEach(input => {
    if (input.parentElement?.classList.contains('saved-value-editor')) return;
    const values = v8Saved[input.name] || [];
    if (!values.length) return;
    const wrapper = document.createElement('span');
    wrapper.className = 'saved-value-editor';
    const picker = document.createElement('select');
    picker.className = 'saved-value-picker';
    picker.setAttribute('aria-label', `Choose saved ${input.name}`);
    picker.innerHTML = '<option value="">Saved…</option>' + values.map(value =>
      `<option value="${attr(value)}">${esc(value)}</option>`
    ).join('');
    input.before(wrapper);
    wrapper.append(input, picker);
    picker.onchange = () => {
      if (!picker.value) return;
      input.value = picker.value;
      input.dataset.dirty = 'true';
      input.dispatchEvent(new Event('input', {bubbles: true}));
      picker.selectedIndex = 0;
      input.focus();
    };
  });
}

const savedValuePickerObserver = new MutationObserver(() => installSavedValuePickers($('#stream-content')));
savedValuePickerObserver.observe($('#stream-content'), {childList: true, subtree: true});
installSavedValuePickers($('#stream-content'));
