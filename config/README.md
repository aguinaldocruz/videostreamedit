# Runtime configuration

Docker Compose mounts this directory at `/config` inside the container.

It contains private runtime state, including:

- `videostreamedit.db`: application settings, cached Plex catalog, saved stream values, and navigation state.
- `plex-token.key`: the encryption key for the Plex token stored in the database.

The directory contents are excluded from Git. Never commit either runtime file. Back up and restore the database and key together; the encrypted Plex token cannot be recovered from the database without its matching key.
