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
    chown -R videostreamedit:videostreamedit /config
    exec gosu videostreamedit "$@"
fi

exec "$@"
