#!/bin/sh
# Fix ownership on mounted volumes that may have been created with a
# different UID from a previous build.  Runs as root, then exec's the
# actual worker process as the configured UID.
APP_UID="${APP_UID:-1026}"
APP_GID="${APP_GID:-1026}"
chown -Rf "${APP_UID}:${APP_GID}" /home/worker/.cache /home/worker/.EasyOCR /app/server/cache 2>/dev/null || true

# gosu with numeric UID doesn't set HOME, so EasyOCR/HuggingFace would
# write to /.EasyOCR / /.cache instead of /home/worker/.
export HOME=/home/worker
exec gosu "${APP_UID}:${APP_GID}" python3.12 -m worker "$@"
