#!/usr/bin/env bash
# Air-gap lint: fail if any template/CSS/JS references an external URL.
# External URLs in Python source are OK (e.g., comments, error messages).
# Only UI-layer files are checked because those are what the browser loads.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && cd .. && pwd)"
cd "${ROOT}"

PATTERN='(https?://(?!sipsmith\.local)[a-zA-Z0-9._/-]+\.(com|org|net|io|dev|cdn|unpkg|jsdelivr|cloudflare))'

FILES_TO_CHECK=(
  sipsmith/ui/templates/
  sipsmith/ui/static/css/
  sipsmith/ui/static/js/
  plugins/
)

FOUND=0
for dir in "${FILES_TO_CHECK[@]}"; do
  if [[ -d "${ROOT}/${dir}" ]]; then
    while IFS= read -r -d '' file; do
      # Skip the htmx stub that documents where to get it
      [[ "${file}" == *"htmx.min.js" ]] && continue
      # Skip fetch-assets.sh (that's the developer tool, not a runtime file)
      [[ "${file}" == *"fetch-assets.sh" ]] && continue

      if grep -qP "${PATTERN}" "${file}" 2>/dev/null; then
        echo "  AIR-GAP VIOLATION: ${file}"
        grep -nP "${PATTERN}" "${file}" | head -5
        FOUND=$(( FOUND + 1 ))
      fi
    done < <(find "${ROOT}/${dir}" -type f \( -name "*.html" -o -name "*.css" -o -name "*.js" \) -print0)
  fi
done

if [[ "${FOUND}" -gt 0 ]]; then
  echo ""
  echo "Air-gap lint FAILED: ${FOUND} file(s) contain external URL references."
  echo "All UI assets must be vendored locally. See scripts/fetch-assets.sh."
  exit 1
fi

echo "Air-gap lint: no external URLs in UI files ✓"
