(function () {
  let pendingNavigation = null;

  function ensurePendingNavigationDialog() {
    if ($('#pending-navigation-dialog')) return;
    document.body.insertAdjacentHTML('beforeend', `<dialog id="pending-navigation-dialog" class="pending-navigation-dialog">
      <div class="dialog-title"><div><h2>Pending stream changes</h2><p>You have unapplied changes on the current media.</p></div><button type="button" class="icon-close" data-cancel-pending-navigation>×</button></div>
      <p>Apply them before leaving, or move without applying them?</p>
      <div class="dialog-actions"><button type="button" data-cancel-pending-navigation>Cancel</button><button type="button" id="move-without-applying">Move without applying</button><button type="button" id="apply-before-navigation" class="primary">Apply and stay</button></div>
    </dialog>`);
    document.querySelectorAll('[data-cancel-pending-navigation]').forEach(button => button.onclick = () => {
      pendingNavigation = null;
      $('#pending-navigation-dialog').close();
      $('#stream-dialog')?.focus({preventScroll: true});
    });
    $('#move-without-applying').onclick = () => {
      const action = pendingNavigation;
      pendingNavigation = null;
      $('#pending-navigation-dialog').close();
      action?.();
    };
    $('#apply-before-navigation').onclick = () => {
      pendingNavigation = null;
      $('#pending-navigation-dialog').close();
      const form = $('#stream-form');
      form.dataset.applyModeConfirmed = 'now';
      form.requestSubmit();
    };
  }

  function guardNavigation(action, mode = 'navigate') {
    if (typeof queuedChangeCount !== 'function' || queuedChangeCount() === 0) {
      action();
      return;
    }
    ensurePendingNavigationDialog();
    const closing = mode === "close";
    document.querySelector("#pending-navigation-dialog > p").textContent = closing ? "Apply them before closing, or close without applying them?" : "Apply them before leaving, or move without applying them?";
    document.querySelector("#move-without-applying").textContent = closing ? "Close without applying" : "Move without applying";
    const queueButton = document.querySelector("#pending-navigation-dialog [data-queue-pending]"); if (queueButton) queueButton.textContent = closing ? "Queue changes and close" : "Queue changes and move";
    pendingNavigation = action;
    $('#pending-navigation-dialog').showModal();
    $('#apply-before-navigation').focus({preventScroll: true});
  }

  window.guardPendingStreamChanges = guardNavigation;

  const unguardedNavigateMedia = navigateMedia;
  navigateMedia = function (offset) {
    guardNavigation(() => unguardedNavigateMedia(offset));
  };

  const unguardedNavigateBoundary = navigateToMediaBoundary;
  navigateToMediaBoundary = function (index) {
    guardNavigation(() => unguardedNavigateBoundary(index));
  };
})();
