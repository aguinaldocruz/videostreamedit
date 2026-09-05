let savedComboboxInput = null;
let savedComboboxIndex = -1;
let suppressSavedComboboxInput = false;

closeSavedValueMenu = function () {
  if (openSavedValueMenu) openSavedValueMenu.remove();
  openSavedValueMenu = null;
  savedComboboxInput = null;
  savedComboboxIndex = -1;
};

function savedComboboxValues(input, filter) {
  const values = [...(v8Saved[input.dataset.savedField || input.name] || [])];
  if (!filter) return values;
  const query = input.value.trim().toLocaleLowerCase();
  if (!query) return values;
  return values
    .filter(value => value.toLocaleLowerCase().includes(query))
    .sort((left, right) => {
      const leftStarts = left.toLocaleLowerCase().startsWith(query);
      const rightStarts = right.toLocaleLowerCase().startsWith(query);
      return Number(rightStarts) - Number(leftStarts) || left.localeCompare(right);
    });
}

function positionSavedCombobox(menu, input) {
  const rect = input.getBoundingClientRect();
  const spaceBelow = window.innerHeight - rect.bottom - 12;
  const spaceAbove = rect.top - 12;
  const openAbove = spaceBelow < 150 && spaceAbove > spaceBelow;
  const available = Math.max(92, Math.min(280, openAbove ? spaceAbove : spaceBelow));
  menu.style.left = `${rect.left}px`;
  menu.style.width = `${Math.max(rect.width, 260)}px`;
  menu.style.maxWidth = `${Math.max(180, window.innerWidth - rect.left - 12)}px`;
  menu.style.maxHeight = `${available}px`;
  menu.style.top = openAbove ? `${Math.max(8, rect.top - available - 4)}px` : `${rect.bottom + 4}px`;
  if (openAbove) menu.style.top = `${Math.max(8, rect.top - menu.offsetHeight - 4)}px`;
}

function activateSavedComboboxOption(index) {
  if (!openSavedValueMenu) return;
  const options = [...openSavedValueMenu.querySelectorAll('.saved-value-option')];
  if (!options.length) return;
  savedComboboxIndex = (index + options.length) % options.length;
  options.forEach((option, optionIndex) => option.classList.toggle('active', optionIndex === savedComboboxIndex));
  options[savedComboboxIndex].scrollIntoView({block: 'nearest'});
}

function chooseSavedComboboxValue(input, value) {
  suppressSavedComboboxInput = true;
  input.value = value;
  delete input.dataset.fastDefaultFrom;
  input.classList.remove('fast-default-suggestion');
  input.removeAttribute('title');
  input.dataset.dirty = 'true';
  input.dispatchEvent(new Event('input', {bubbles: true}));
  suppressSavedComboboxInput = false;
  closeSavedValueMenu();
  input.focus();
}

showSavedValueMenu = function (input, filter = false) {
  closeSavedValueMenu();
  const values = savedComboboxValues(input, filter);
  const menu = document.createElement('div');
  menu.className = 'saved-value-menu saved-combobox-menu';
  menu.setAttribute('role', 'listbox');
  menu.setAttribute('aria-label', 'Saved values');
  if (!values.length) {
    const empty = document.createElement('div');
    empty.className = 'saved-combobox-empty';
    empty.textContent = 'No saved match — keep typing to use a new value';
    menu.append(empty);
  }
  values.forEach(value => {
    const option = document.createElement('button');
    option.type = 'button';
    option.className = 'saved-value-option';
    option.textContent = value;
    option.title = value;
    option.setAttribute('role', 'option');
    option.classList.toggle('current', value === input.value);
    option.onpointerdown = event => event.preventDefault();
    option.onclick = () => chooseSavedComboboxValue(input, value);
    menu.append(option);
  });
  (input.closest('dialog') || document.body).append(menu);
  openSavedValueMenu = menu;
  savedComboboxInput = input;
  positionSavedCombobox(menu, input);
};

installSavedValuePopups = function (root = document) {
  root.querySelectorAll('.stream-row input[type="text"][name],input.season-bulk-value[data-saved-field],input.movie-header-bulk-value[data-saved-field]').forEach(input => {
    if (input.dataset.inlineComboboxReady === 'true') return;
    if (!(v8Saved[input.dataset.savedField || input.name] || []).length) return;
    input.dataset.inlineComboboxReady = 'true';
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.removeAttribute('list');
    input.onclick = () => showSavedValueMenu(input, false);
    input.onfocus = null;
    input.onblur = () => setTimeout(() => {
      if (savedComboboxInput === input) closeSavedValueMenu();
    }, 120);
    input.addEventListener('input', () => {
      if (!suppressSavedComboboxInput) showSavedValueMenu(input, true);
    });
    input.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        closeSavedValueMenu();
      } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (savedComboboxInput !== input) showSavedValueMenu(input, true);
        activateSavedComboboxOption(savedComboboxIndex + (event.key === 'ArrowDown' ? 1 : -1));
      } else if (event.key === 'Enter' && openSavedValueMenu && savedComboboxIndex >= 0) {
        event.preventDefault();
        openSavedValueMenu.querySelectorAll('.saved-value-option')[savedComboboxIndex]?.click();
      }
    });
  });
};

const inlineComboboxObserver = new MutationObserver(() => installSavedValuePopups($('#stream-content')));
inlineComboboxObserver.observe($('#stream-content'), {childList: true, subtree: true});
installSavedValuePopups($('#stream-content'));

const inlineComboboxOpenEditor = openEditor;
openEditor = async function (...argumentsList) {
  const result = await inlineComboboxOpenEditor(...argumentsList);
  const dialog = $("#stream-dialog");
  closeSavedValueMenu();
  if (dialog?.open) {
    dialog.tabIndex = -1;
    dialog.focus({preventScroll: true});
  }
  return result;
};
