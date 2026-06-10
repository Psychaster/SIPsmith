#!/usr/bin/env bash
# Fetch vendored frontend assets for development.
# In production/offline mode these come from the offline bundle.
# This script requires internet access — do NOT run on the appliance.
set -euo pipefail

STATIC="sipsmith/ui/static"

echo "Fetching htmx..."
curl -fsSL "https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js" \
  -o "${STATIC}/js/htmx.min.js"

echo "Done. Assets are vendored in ${STATIC}/"
