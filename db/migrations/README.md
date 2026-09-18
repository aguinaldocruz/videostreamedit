# PostgreSQL migration

The application database is being moved from SQLite to PostgreSQL. The
Compose file starts a local PostgreSQL 16 service by default and persists it
under `/data/postgres`. An external server can be selected by setting
`DATABASE_URL` and disabling the internal `postgres` service.

Only durable user configuration is migrated. Historical task queues, derived
indexes, preview metadata, and old migration markers are intentionally not
copied.
