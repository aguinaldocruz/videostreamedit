# Security policy

## Intended deployment

VideoStreamEdit is designed for a trusted intranet. It has no built-in user authentication and can modify or remove media files available to the container. Do not publish its port directly to the internet.

Use firewall rules or an authenticated reverse proxy if access extends beyond a trusted LAN. Grant the container access only to required media roots and run it with an unprivileged `PUID`/`PGID`.

## Secrets

The Plex token is encrypted at rest, but the encryption key is stored beside the database in `/config`. This protects against accidental database-only disclosure, not against an attacker who can read the complete config directory.

Never commit or share:

- `config/videostreamedit.db`
- `config/plex-token.key`
- Plex tokens, private server URLs, media paths, or diagnostic logs containing them

Back up the database and encryption key together.

## Reporting a vulnerability

Do not open a public issue for an unpatched vulnerability or exposed secret. Contact the repository owner privately through the security-reporting method configured on the GitHub repository. Include affected versions, reproduction steps, impact, and any proposed mitigation.

If a Plex token may have been exposed, revoke it in Plex and replace it immediately.
