#!/bin/sh
# Fix ownership on mounted volumes that may have been created with a
# different UID from a previous build.  Runs as root, then exec's the
# actual worker process as UID 65532 (matches distroless:nonroot).
chown -Rf 65532:65532 /home/worker/.cache /home/worker/.EasyOCR /app/cache 2>/dev/null || true
exec gosu 65532:65532 python3.12 -m worker "$@"
