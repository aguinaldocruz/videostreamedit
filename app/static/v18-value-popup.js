let openSavedValueMenu = null;

function closeSavedValueMenu() {
  if (openSavedValueMenu) openSavedValueMenu.remove();
  openSavedValueMenu = null;
}

function showSavedValueMenu(input) {
  closeSavedValueMenu();
  const values = v8Saved[input.dataset.savedField || input.name] || [];
  if (!values.length) return;
  const menu = document.createElement('div');
  menu.className = 'saved-value-menu';
  menu.setAttribute('role', 'listbox');
  values.forEach(value => {
    const option = document.createElement('button');
    option.type = 'button';
    option.className = 'saved-value-option';
    option.textContent = value;
    option.classList.toggle('current', value === input.value);
    option.onpointerdown = event => event.preventDefault();
    option.onclick = () => {
      input.value = value;
      delete input.dataset.fastDefaultFrom;
      input.classList.remove('fast-default-suggestion');
      input.removeAttribute('title');
      input.dataset.dirty = 'true';
      input.dispatchEvent(new Event('input', {bubbles: true}));
      closeSavedValueMenu();
      input.focus();
    };
    menu.append(option);
  });
  const rect = input.getBoundingClientRect();
  const spaceBelow = window.innerHeight - rect.bottom - 12;
  const spaceAbove = rect.top - 12;
  const openAbove = spaceBelow < 120 && spaceAbove > spaceBelow;
  const available = Math.max(72, Math.min(190, openAbove ? spaceAbove : spaceBelow));
  menu.style.left = `${rect.left}px`;
  menu.style.width = `${rect.width}px`;
  menu.style.maxHeight = `${available}px`;
  menu.style.top = openAbove ? `${Math.max(8, rect.top - available - 4)}px` : `${rect.bottom + 4}px`;
  $('#stream-dialog').append(menu);
  if (openAbove) menu.style.top = `${Math.max(8, rect.top - menu.offsetHeight - 4)}px`;
  openSavedValueMenu = menu;
}

function installSavedValuePopups(root = document) {
  root.querySelectorAll('.stream-row input[type="text"][name]').forEach(input => {
    const oldWrapper = input.parentElement?.classList.contains('saved-value-editor') ? input.parentElement : null;
    if (oldWrapper?.dataset.popupReady === 'true') return;
    if (!(v8Saved[input.dataset.savedField || input.name] || []).length) return;
    const wrapper = oldWrapper || document.createElement('span');
    wrapper.className = 'saved-value-editor saved-value-popup-editor';
    wrapper.dataset.popupReady = 'true';
    if (!oldWrapper) {
      input.before(wrapper);
      wrapper.append(input);
    } else {
      oldWrapper.querySelectorAll('.saved-value-picker').forEach(item => item.remove());
    }
    input.setAttribute('autocomplete', 'off');
    input.removeAttribute('list');
    input.onclick = () => showSavedValueMenu(input);
    input.onfocus = () => showSavedValueMenu(input);
    input.onblur = () => setTimeout(closeSavedValueMenu, 100);
  });
}

const savedValuePopupObserver = new MutationObserver(() => installSavedValuePopups($('#stream-content')));
savedValuePopupObserver.observe($('#stream-content'), {childList: true, subtree: true});
installSavedValuePopups($('#stream-content'));
$('#stream-content').addEventListener('scroll', closeSavedValueMenu, {passive: true});
window.addEventListener('resize', closeSavedValueMenu, {passive: true});
