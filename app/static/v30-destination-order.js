let draggedImportDestination = null;

function wireImportDestinationDragging() {
  document.querySelectorAll('#import-destinations .import-destination').forEach(item => {
    item.draggable = true;
    if (!item.querySelector('.destination-drag-handle')) item.insertAdjacentHTML('afterbegin', '<span class="destination-drag-handle" title="Drag to reorder">⠿</span>');
    item.ondragstart = event => {draggedImportDestination = item;item.classList.add('dragging');event.dataTransfer.effectAllowed = 'move'};
    item.ondragend = () => {item.classList.remove('dragging');document.querySelectorAll('.import-destination.drag-over').forEach(value => value.classList.remove('drag-over'));draggedImportDestination = null};
    item.ondragover = event => {if(draggedImportDestination && draggedImportDestination !== item){event.preventDefault();item.classList.add('drag-over')}};
    item.ondragleave = () => item.classList.remove('drag-over');
    item.ondrop = async event => {event.preventDefault();item.classList.remove('drag-over');if(!draggedImportDestination || draggedImportDestination === item)return;const box=item.getBoundingClientRect();item.parentNode.insertBefore(draggedImportDestination,event.clientY<box.top+box.height/2?item:item.nextSibling);await saveImportDestinationOrder()};
  });
}

async function saveImportDestinationOrder() {
  const paths = [...document.querySelectorAll('#import-destinations [name=import-destination]')].map(input => input.value);
  try {await api('/api/v30/import/destinations/order', {method: 'PUT', body: JSON.stringify({paths})});toast('Plex destination order saved')}
  catch (error) {toast(error.message, true)}
}

const orderedRenderImportDestinations = renderImportDestinations;
renderImportDestinations = function (destinations) {
  orderedRenderImportDestinations(destinations);
  wireImportDestinationDragging();
};
