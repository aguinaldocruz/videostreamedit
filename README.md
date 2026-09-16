# VideoStreamEdit

VideoStreamEdit is a self-hosted intranet web application for browsing Plex movie and TV libraries and editing audio and subtitle stream metadata directly in the underlying media files.

The project is intended for a trusted home network. Plex remains the source of catalog metadata while VideoStreamEdit keeps a local SQLite catalog and indexes so normal browsing does not repeatedly scan the filesystem or query every Plex item.

It provides an MP3Tag-style workflow for language, region, track name, default/forced flags, stream order, external subtitle integration, stream removal, reusable values, and compatible change templates. A separate Movie Import workflow copies new movies into a chosen library folder and applies stream edits during import.

> [!CAUTION]
> VideoStreamEdit modifies media files in place. Test with disposable files first and keep verified backups. The container must have write access to every media path it edits.

## Features

- Plex-backed movie and TV catalog with local synchronization.
- Movie browsing and TV show → season → episode navigation.
- Audio and subtitle metadata editing without re-encoding streams.
- Drag-and-drop stream ordering.
- Default and forced disposition controls, including an untagged state.
- External subtitle discovery, editing, integration, ordering, and removal.
- Filename editing in Stream Properties: destination naming for imports and in-place renaming for synchronized movies and episodes.
- Previous/next and first/last media navigation.
- Session change markers, reusable property values, and compatible change templates.
- Bulk cloning for compatible TV episodes.
- Movie Import with source/destination browsing and optional source cleanup.
- Encrypted Plex token storage.
- Dashboard with collection counts and language distributions.
- Reports for image subtitles, HTML subtitles, forced streams, and language or voice-detection findings.
- Multi-purpose task queue with priorities, retries, progress, signatures, and separate incremental index queues.

## Requirements

- Docker Engine with the Compose plugin.
- A Plex Media Server reachable from the container.
- The same media paths Plex reports mounted inside VideoStreamEdit.
- Read/write permissions for the configured `PUID` and `PGID`.

## Quick start

1. Clone the repository and enter it.
2. Edit `docker-compose.yml`:
   - Change `/media/:/media` to match the paths exposed to Plex.
   - Set `PUID`, `PGID`, `TZ`, and the published port if needed.
   - Keep `./config:/config` to retain application state locally.
3. Start the application:

   ```console
   docker compose up --build -d
   ```

4. Open `http://localhost:8383`.
5. In **Setup**, enter the Plex server URL and token, select libraries, and synchronize the catalog.

The first sync can take time. Subsequent syncs are incremental. The application must be able to access media using the exact paths returned by Plex.

No `.env` file is required or used by the supplied Compose configuration.

## Volume layout

```yaml
volumes:
  - /media/:/media
  - ./config:/config
```

Plex paths and VideoStreamEdit paths must match. For example, if Plex reports `/media/Movies/Film.mkv`, that exact path must exist inside this container.

The `config/` directory is deliberately excluded from Git. It contains the SQLite database and Plex-token encryption key. Back up both files together.

## Movie Import

Configure input and output roots in **Setup**, then use **Import Movies** to:

1. Browse to a source movie.
2. Browse to a destination directory.
3. Edit the target filename and stream properties.
4. Copy and remux the movie into the destination.
5. Optionally remove the original movie and matching external subtitles after success.

## How media changes work

VideoStreamEdit uses FFmpeg stream copying, so audio and video payloads are not transcoded. It writes a temporary file beside the source and replaces the original only after FFmpeg succeeds. Matroska metadata inspection also uses MKVToolNix.

Some operations can still take a long time because the complete container must be rewritten. Free space is required on the same filesystem during processing.

Queued media changes capture a file signature before execution. If a file changes or moves while waiting, the task is rejected safely instead of applying an operation to the wrong media. Bulk requests use a lightweight preflight that creates child jobs only when a real change is required.

Subtitle HTML cleanup validates the current codec, complete FFmpeg extraction, and actual HTML tags before creating a cleanup job. Graphical subtitles are excluded from the HTML report and handled by the image-subtitle workflow.

## Background work

Setup exposes the generic **Tasks** queue and incremental **Indexes** queues:

- **Core** — stream metadata and fast filter values.
- **Subtitles** — extended subtitle properties and HTML inspection.
- **Previews** — audio preview cache work.

Index checks are incremental by media fingerprint. A rebuild is only needed after an intentional catalog/index reset.

## Useful commands

```console
# Start or rebuild
docker compose up --build -d

# Follow application and one-line change logs
docker compose logs -f videostreamedit

# Stop the application
docker compose down

# Check container status
docker compose ps
```

## Security

VideoStreamEdit is intended for trusted networks. It currently has no user authentication. Do not expose it directly to the public internet. See [SECURITY.md](SECURITY.md) for reporting and deployment guidance.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

For the optional standalone spoken-language checker, see [docs/AUDIO_LANGUAGE_AUDIT.md](docs/AUDIO_LANGUAGE_AUDIT.md).

For the standalone damaged-text subtitle scanner, see [docs/SUBTITLE_CORRUPTION_AUDIT.md](docs/SUBTITLE_CORRUPTION_AUDIT.md).

For the maintenance checklist and runtime module map, see [docs/MAINTENANCE.md](docs/MAINTENANCE.md).

## License

The repository currently uses the all-rights-reserved notice in [LICENSE](LICENSE). Choose an open-source license before accepting outside contributions or publishing the project for general redistribution.
