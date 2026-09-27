#!/bin/sh
set -eu

runtime_uid="${PUID:-1000}"
runtime_gid="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
    current_gid="$(id -g videostreamedit)"
    current_uid="$(id -u videostreamedit)"
    if [ "$current_gid" != "$runtime_gid" ]; then
        groupmod --non-unique --gid "$runtime_gid" videostreamedit
    fi
    if [ "$current_uid" != "$runtime_uid" ]; then
        usermod --non-unique --uid "$runtime_uid" videostreamedit
    fi
    # /config/data contains PostgreSQL files owned by the database container.
    # Never recursively chown that subtree from the application container.
    find /config -mindepth 1 -maxdepth 1 ! -name data -exec chown -R videostreamedit:videostreamedit {} + || true
    mkdir -p /config /data /backup
    # Fresh named volumes start root-owned. Change only the mount roots, never
    # recursively take ownership of a local PostgreSQL cluster under /data.
    chown videostreamedit:videostreamedit /config /data /backup
    exec gosu videostreamedit "$@"
fi

exec "$@"
