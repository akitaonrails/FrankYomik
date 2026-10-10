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
PYTHON="$(command -v python3.12 || command -v python3)"

# Docker grants ROCm access through supplemental video/render groups.  gosu
# rebuilds the group list while dropping privileges, which silently removes
# those device grants.  Preserve Docker's non-root supplemental groups while
# still removing the root group itself.
SUPPLEMENTAL_GROUPS=""
for GROUP_ID in $(id -G); do
    case "$GROUP_ID" in
        0|"$APP_GID") continue ;;
        *[!0-9]*|"") echo "invalid supplemental group: $GROUP_ID" >&2; exit 1 ;;
    esac
    if [ -z "$SUPPLEMENTAL_GROUPS" ]; then
        SUPPLEMENTAL_GROUPS="$GROUP_ID"
    else
        SUPPLEMENTAL_GROUPS="$SUPPLEMENTAL_GROUPS,$GROUP_ID"
    fi
done

if [ -n "$SUPPLEMENTAL_GROUPS" ]; then
    exec setpriv --reuid "$APP_UID" --regid "$APP_GID" \
        --groups "$SUPPLEMENTAL_GROUPS" "$PYTHON" -m worker "$@"
fi
exec setpriv --reuid "$APP_UID" --regid "$APP_GID" \
    --clear-groups "$PYTHON" -m worker "$@"
