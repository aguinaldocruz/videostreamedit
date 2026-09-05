(function () {
  let dialogOpenSequence = 0;
  const nativeShowModal = HTMLDialogElement.prototype.showModal;
  HTMLDialogElement.prototype.showModal = function (...args) {
    this.dataset.escapeOpenOrder = String(++dialogOpenSequence);
    return nativeShowModal.apply(this, args);
  };

  function closeButton(dialog) {
    const explicit = dialog.querySelector('.dialog-title .icon-close, .dialog-title [aria-label="Close"]');
    if (explicit) return explicit;
    return [...dialog.querySelectorAll('.dialog-title button')].find(button => button.textContent.trim() === '×') || null;
  }

  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || event.defaultPrevented || document.body.classList.contains('app-busy')) return;
    const dialogs = [...document.querySelectorAll('dialog[open]')].filter(dialog => closeButton(dialog));
    if (!dialogs.length) return;
    const focusedDialog = document.activeElement?.closest?.('dialog[open]');
    const dialog = focusedDialog && dialogs.includes(focusedDialog)
      ? focusedDialog
      : dialogs.sort((left, right) => Number(left.dataset.escapeOpenOrder || 0) - Number(right.dataset.escapeOpenOrder || 0)).at(-1);
    const button = closeButton(dialog);
    if (!button || button.disabled) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    button.click();
  }, true);
})();
