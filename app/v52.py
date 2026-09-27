"""Subtitle-aware movie index routes backed by the canonical queue."""
from app.v39 import RefreshMovieIndex, refresh_one_movie_filter
from app.v51 import app


@app.post('/api/v52/setup/movie-index/rebuild')
def rebuild_all_movie_indexes() -> dict:
    from app.v80 import rebuild_index_queue
    return rebuild_index_queue('core')


@app.post('/api/v52/movies/stream-filter-refresh')
def refresh_movie_indexes(payload: RefreshMovieIndex) -> dict:
    return refresh_one_movie_filter(payload)
