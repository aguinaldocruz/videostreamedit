(function () {
  document.addEventListener('keydown', event => {
    if (event.key !== 'F10' || event.repeat || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const streamDialog = $('#stream-dialog');
    if (!streamDialog?.open || document.body.classList.contains('app-busy')) return;
    if (document.querySelector('dialog[open]:not(#stream-dialog)')) return;
    const button = document.querySelector('.track-name-suggestion [data-use-suggestion]');
    if (!button || button.disabled || button.offsetParent === null) return;
    event.preventDefault();
    event.stopPropagation();
    button.click();
  });
})();
