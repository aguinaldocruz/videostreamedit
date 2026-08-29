# Contributing

Thank you for helping improve VideoStreamEdit.

## Development setup

1. Fork or clone the repository.
2. Review `docker-compose.yml` and use disposable test media mounts.
3. Start the development image with `docker compose up --build -d`.
4. Confirm `http://localhost:8383/api/health` returns a successful response.

Do not use irreplaceable media when testing. Stream edits rewrite the container file in place after creating a temporary replacement.

## Changes

- Keep changes focused and preserve backward compatibility for the SQLite database.
- Add a migration for any persistent schema change.
- Never commit `config/`, Plex tokens, encryption keys, databases, media, or logs.
- Keep file operations restricted to configured roots.
- Log completed mutations as one concise `change=...` line without secrets.
- Disable interactive controls while long-running file operations are active.
- Update documentation when configuration or behavior changes.

## Before opening a pull request

```console
python -m compileall -q app
docker compose config -q
docker compose build
```

Manually verify relevant workflows using disposable media. Describe the test media/container type without attaching copyrighted files.

## Pull requests

Explain the problem, the behavior change, migration impact, media safety considerations, and verification performed. Screenshots are welcome for UI changes, but remove private paths and Plex details.
