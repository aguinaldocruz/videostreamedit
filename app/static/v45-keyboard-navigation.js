(function () {
  function isInteractiveTarget(target) {
    return Boolean(target?.closest?.('input, textarea, select, button, a, [contenteditable="true"], [role="combobox"], [role="listbox"], [role="option"]'));
  }

  document.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    if (event.repeat || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const dialog = $('#stream-dialog');
    if (!dialog?.open || document.body.classList.contains('app-busy')) return;
    if (document.querySelector('dialog[open]:not(#stream-dialog)')) return;
    if (isInteractiveTarget(event.target) || isInteractiveTarget(document.activeElement)) return;
    const button = event.key === 'ArrowLeft' ? $('#previous-media') : $('#next-media');
    if (!button || button.disabled || button.closest('#media-navigation')?.classList.contains('hidden')) return;
    event.preventDefault();
    event.stopPropagation();
    button.click();
  });
})();
