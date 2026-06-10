#!/usr/bin/env bash
# Build an offline install bundle: debs/ + wheels/ + app source.
# Run this on an Ubuntu 24.04 machine WITH internet access.
# The resulting tarball is the air-gap-safe artifact.
set -euo pipefail

SIPSMITH_VERSION="$(python3 -c "from sipsmith import __version__; print(__version__)" 2>/dev/null || echo "0.1.0")"
BUNDLE_NAME="sipsmith-${SIPSMITH_VERSION}-offline"
BUILD_DIR="/tmp/${BUNDLE_NAME}"

PKGS=(
  python3.12 python3.12-venv python3.12-dev
  postgresql postgresql-client libpq-dev
  openssh-server chrony ufw openssl
  build-essential libffi-dev libssl-dev curl
)

info() { echo "[build-bundle] $*"; }

info "Building bundle: ${BUNDLE_NAME}"
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"/{debs,wheels,app}

# ── Fetch .deb packages ───────────────────────────────────────────────────

info "Downloading apt packages..."
cd "${BUILD_DIR}/debs"
apt-get download "${PKGS[@]}"
for pkg in "${PKGS[@]}"; do
  apt-cache depends --recurse --no-recommends --no-suggests \
    --no-conflicts --no-breaks --no-replaces --no-enhances \
    --no-pre-depends "${pkg}" 2>/dev/null | grep "^\w" | sort -u
done | xargs apt-get download 2>/dev/null || true
info "apt packages downloaded: $(ls "${BUILD_DIR}/debs" | wc -l) .deb files"

# ── Fetch Python wheels ───────────────────────────────────────────────────

info "Downloading Python wheels..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
pip3 download -d "${BUILD_DIR}/wheels" \
  --platform manylinux_2_35_x86_64 \
  --python-version 312 \
  --only-binary=:all: \
  -r "${REPO_ROOT}/requirements.txt" \
  "${REPO_ROOT}"
info "Wheels downloaded: $(ls "${BUILD_DIR}/wheels" | wc -l) files"

# ── Fetch frontend assets ─────────────────────────────────────────────────

info "Fetching frontend assets..."
STATIC="${REPO_ROOT}/sipsmith/ui/static"
curl -fsSL "https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js" \
  -o "${STATIC}/js/htmx.min.js"

# ── Copy application source ───────────────────────────────────────────────

info "Copying application source..."
rsync -a --exclude='.git' --exclude='*.pyc' --exclude='__pycache__' \
  --exclude='.venv' --exclude='venv' --exclude='*.egg-info' \
  --exclude='debs' --exclude='wheels' \
  "${REPO_ROOT}/" "${BUILD_DIR}/app/"

# ── Symlink installer to bundle root ─────────────────────────────────────

cp "${REPO_ROOT}/installer/install-sipsmith.sh" "${BUILD_DIR}/"

# ── Create tarball ────────────────────────────────────────────────────────

cd /tmp
tar czf "${BUNDLE_NAME}.tar.gz" "${BUNDLE_NAME}/"
info "Bundle created: /tmp/${BUNDLE_NAME}.tar.gz"
info "  Size: $(du -sh "/tmp/${BUNDLE_NAME}.tar.gz" | cut -f1)"
info ""
info "Transfer this file across the air gap, then:"
info "  tar xzf ${BUNDLE_NAME}.tar.gz && cd ${BUNDLE_NAME}"
info "  sudo bash install-sipsmith.sh --offline"
