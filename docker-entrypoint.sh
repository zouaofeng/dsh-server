#!/bin/sh
# Prepare the data directories, then drop root and run the service as an unprivileged user.
# Running unprivileged is also what lets bubblewrap (workspace-write sandbox) work without
# granting the container extra capabilities.
set -e

APP_UID="${APP_UID:-1000}"
APP_GID="${APP_GID:-1000}"

if [ "$(id -u)" = "0" ]; then
    for dir in "$DSH_HOME" "$DSH_WORKSPACE"; do
        mkdir -p "$dir"
        # Only fix ownership when needed, so restarts with a large data dir stay fast.
        if [ "$(stat -c %u "$dir")" != "$APP_UID" ]; then
            echo "entrypoint: chown -R $APP_UID:$APP_GID $dir"
            chown -R "$APP_UID:$APP_GID" "$dir"
        fi
    done
    exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --clear-groups -- "$@"
fi

exec "$@"
