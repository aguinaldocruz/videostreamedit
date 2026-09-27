# PostgreSQL first-run setup

VideoStreamEdit can now start without a PostgreSQL URL. Use
`docker-compose.first-run.yml` for a new deployment or a migration target:

```sh
docker compose -f docker-compose.first-run.yml up -d --build
```

Open `http://localhost:8383`. The wizard asks for:

- the PostgreSQL server URL and maintenance database;
- an administrator account, used only for this provisioning request;
- the target database name;
- the application role and password; and
- whether this is a new installation or a restore preparation.

Before configuration, only the wizard, its assets, branding and health endpoints
are available. Catalog workers and schema initialization do not run; no temporary
SQLite catalog is created. Saving the connection requires a container restart
before the normal application becomes available.

The wizard creates the role and database if necessary, verifies the
application credentials, and stores only the application connection URL in an
encrypted `/config/database.connection.enc` file. Administrator credentials
are never stored. Restart the container after provisioning. A new installation
creates its schema on startup; a restore preparation can then use Setup →
Tasks → Backup to restore the selected archive.

The backup archive intentionally does not contain any database URL or
password. This prevents a restore from silently reconnecting to the old
server. The destination database must therefore be provisioned first.

The existing `docker-compose.yml` is left unchanged for the currently running
deployment. Use the first-run file when moving to an external PostgreSQL
server, then retire the old Compose stack after the new connection has been
verified.
