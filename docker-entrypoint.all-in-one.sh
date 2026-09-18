#!/bin/sh
set -eu

runtime_uid="${PUID:-1000}"
runtime_gid="${PGID:-1000}"
pgdata="${PGDATA:-/data/postgres}"

if [ "$(id -u)" -ne 0 ]; then
  echo "single-container mode must start as root so PostgreSQL and the app can be supervised" >&2
  exit 1
fi

current_gid="$(id -g videostreamedit)"
current_uid="$(id -u videostreamedit)"
[ "$current_gid" = "$runtime_gid" ] || groupmod --non-unique --gid "$runtime_gid" videostreamedit
[ "$current_uid" = "$runtime_uid" ] || usermod --non-unique --uid "$runtime_uid" videostreamedit
mkdir -p "$pgdata" /run/postgresql
chown -R postgres:postgres "$pgdata" /run/postgresql
if [ ! -s "$pgdata/PG_VERSION" ]; then
  su -s /bin/sh postgres -c "initdb -D '$pgdata' --auth=trust --encoding=UTF8" >/dev/null
fi
# Bind only to the container loopback; the app connects through localhost.
su -s /bin/sh postgres -c "postgres -D '$pgdata' -c listen_addresses=127.0.0.1 -c unix_socket_directories=/run/postgresql -c timezone=America/Sao_Paulo -c log_timezone=America/Sao_Paulo" &
pg_pid=$!
cleanup() { kill "$pg_pid" 2>/dev/null || true; wait "$pg_pid" 2>/dev/null || true; }
trap cleanup INT TERM EXIT
until su -s /bin/sh postgres -c "pg_isready -h 127.0.0.1 -p 5432" >/dev/null 2>&1; do sleep 1; done
su -s /bin/sh postgres -c "psql -h 127.0.0.1 -d postgres -c \"CREATE ROLE videostreamedit LOGIN PASSWORD 'change-this-local-password'\"" >/dev/null 2>&1 || true
su -s /bin/sh postgres -c "createdb -h 127.0.0.1 -O videostreamedit videostreamedit" 2>/dev/null || true
export DATABASE_BACKEND=postgres
export DATABASE_URL="${DATABASE_URL:-postgresql://videostreamedit:change-this-local-password@127.0.0.1:5432/videostreamedit}"
find /config -mindepth 1 -maxdepth 1 ! -name data -exec chown -R videostreamedit:videostreamedit {} + || true
exec gosu videostreamedit uvicorn app.v86:app --host 0.0.0.0 --port 8080
