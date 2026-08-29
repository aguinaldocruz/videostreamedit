function ensureBoundaryNavigation() {
  ensureMediaNavigation();
  const navigation = $('#media-navigation');
  if (!$('#first-media')) {
    const first = document.createElement('button');
    first.type = 'button';
    first.id = 'first-media';
    first.textContent = '<<';
    navigation.insertBefore(first, $('#previous-media'));
    first.onclick = () => navigateToMediaBoundary(0);
  }
  if (!$('#last-media')) {
    const last = document.createElement('button');
    last.type = 'button';
    last.id = 'last-media';
    last.textContent = '>>';
    navigation.append(last);
    last.onclick = () => navigateToMediaBoundary(mediaNavigation.length - 1);
  }
}

function navigateToMediaBoundary(index) {
  if (index < 0 || index >= mediaNavigation.length || index === mediaNavigationIndex) return;
  mediaNavigationIndex = index;
  const item = mediaNavigation[index];
  currentShowContext = item.showTitle || '';
  openEditor(item.path, item.label);
}

const boundaryUpdateMediaNavigation = updateMediaNavigation;
updateMediaNavigation = function () {
  boundaryUpdateMediaNavigation();
  ensureBoundaryNavigation();
  const active = mediaNavigationIndex >= 0 && mediaNavigation.length > 0;
  const first = $('#first-media'), last = $('#last-media');
  first.disabled = !active || mediaNavigationIndex === 0;
  last.disabled = !active || mediaNavigationIndex === mediaNavigation.length - 1;
  first.title = `First ${mediaNavigationKind}`;
  last.title = `Last ${mediaNavigationKind}`;
  first.setAttribute('aria-label', first.title);
  last.setAttribute('aria-label', last.title);
};

ensureBoundaryNavigation();
updateMediaNavigation();
