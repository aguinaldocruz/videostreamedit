function alternativeTitleHint(item) {
  const values = item?.alternative_titles || [];
  return values.length ? `Alternative titles:\n${values.join('\n')}` : '';
}

renderMovies = function () {
  const files = state.movies.filter(file => matches([movieTitle(file), ...(file.alternative_titles || [])].join(' '), $('#movie-search').value));
  $('#movies-empty').style.display = files.length ? 'none' : 'block';
  $('#movies-empty').textContent = state.movies.length ? 'No matching movies.' : 'No movies found.';
  $('#movie-list').innerHTML = files.map(file => `<tr><td><strong class="movie-title title-with-alternatives" title="${attr(alternativeTitleHint(file))}">${esc(movieTitle(file))}</strong></td><td>${esc(file.root_name)}</td><td>${bytes(file.size)}</td><td><button class="edit-file" data-path="${attr(file.path)}" data-label="${attr(movieTitle(file))}">Stream properties</button></td></tr>`).join('');
  wireEditors();
};

renderShows = function () {
  const shows = state.shows.filter(show => matches([show.name, ...(show.alternative_titles || [])].join(' '), $('#show-search').value));
  $('#tv-empty').style.display = shows.length ? 'none' : 'block';
  $('#show-list').innerHTML = shows.map(show => `<button class="show-card ${state.currentShow?.id === show.id ? 'active' : ''}" data-id="${attr(show.id)}"><strong class="title-with-alternatives" title="${attr(alternativeTitleHint(show))}">${esc(clean(show.name))}</strong><small>${show.episode_count} episodes · ${esc(show.root_name)}</small></button>`).join('');
  document.querySelectorAll('.show-card').forEach(button => button.onclick = () => {state.currentShow = state.shows.find(show => show.id === button.dataset.id);state.currentSeason = '*';$('#episode-search').value = '';renderShows();renderEpisodes()});
};

const titleRenderEpisodes = renderEpisodes;
renderEpisodes = function () {
  titleRenderEpisodes();
  const heading = $('#show-title');
  heading.title = state.currentShow ? alternativeTitleHint(state.currentShow) : '';
  heading.classList.toggle('title-with-alternatives', Boolean(heading.title));
};
