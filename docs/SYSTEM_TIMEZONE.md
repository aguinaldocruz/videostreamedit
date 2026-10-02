# System timezone

Setup → Tasks → Info offers a searchable standard Linux/IANA
timezone list. New installations default to `America/Sao_Paulo`. The selected
zone is saved at `/config/system-timezone.json` and reapplied before background
workers start. Changing it takes effect immediately in the application;
the Setup screen refreshes so every visible timestamp uses the new zone.

Scheduled wall-clock times (subtitle cache, indexing/detection, Plex sync, and
backups), application log timestamps, and UI dates use this setting. UTC
instants already stored in PostgreSQL are not rewritten. A schedule such as
01:00 means 01:00 in the selected zone, including that zone's daylight-saving
rules. Docker/Compose startup defaults to São Paulo until the saved setting is
loaded.

The application does **not** alter the host clock, Docker daemon timestamps,
external PostgreSQL server logs, or logs from other containers. Configure
those systems separately if their own timestamps must use the same zone.
