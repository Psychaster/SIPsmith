#!/usr/bin/env bash
# Build pjsua2 Python bindings with video (VP8/H.264) + SRTP support.
# Outputs: /opt/sipsmith/venv/lib/python3.12/site-packages/_pjsua2.so
#          /opt/sipsmith/venv/lib/python3.12/site-packages/pjsua2.py
#
# Requirements: Ubuntu 24.04, build-essential, python3.12-dev, libssl-dev,
#   libopus-dev, libvpx-dev, libopenh264-dev, libasound2-dev, libportaudio2
#
# Run as root (or via sipsmith-agent on install).
# Idempotent: skips if already built for the same version.

set -euo pipefail

PJSIP_VERSION="2.14.1"
BUILD_DIR="/opt/sipsmith/build/pjsip"
VENV="/opt/sipsmith/venv"
PYTHON="${VENV}/bin/python"
PJSIP_SRC="${BUILD_DIR}/pjproject-${PJSIP_VERSION}"
TARBALL="${BUILD_DIR}/pjproject-${PJSIP_VERSION}.tar.bz2"
VERSION_STAMP="${BUILD_DIR}/.built_version"

# ── Check if already built ───────────────────────────────────────────────────
if [[ -f "${VERSION_STAMP}" ]] && [[ "$(cat ${VERSION_STAMP})" == "${PJSIP_VERSION}" ]]; then
    SITE=$(${PYTHON} -c "import site; print(site.getsitepackages()[0])")
    if [[ -f "${SITE}/_pjsua2*.so" ]] || ls "${SITE}"/_pjsua2*.so &>/dev/null 2>&1; then
        echo "[pjsua2] Already built ${PJSIP_VERSION}. Use --force to rebuild."
        exit 0
    fi
fi

echo "[pjsua2] Building pjproject ${PJSIP_VERSION} with video + SRTP..."

# ── Build dependencies ───────────────────────────────────────────────────────
echo "[pjsua2] Installing build dependencies..."
apt-get install -y --no-install-recommends \
    build-essential python3.12-dev \
    libssl-dev libopus-dev libvpx-dev \
    libasound2-dev libspeex-dev libspeexdsp-dev \
    uuid-dev swig

# Try openh264 (may not be in all Ubuntu 24.04 repos)
apt-get install -y --no-install-recommends libopenh264-dev 2>/dev/null || \
    echo "[pjsua2] libopenh264-dev not found — H.264 will use built-in stub"

mkdir -p "${BUILD_DIR}"

# ── Download (online) or look for bundled source (offline) ───────────────────
if [[ ! -f "${TARBALL}" ]]; then
    OFFLINE_TARBALL="$(dirname "$0")/../installer/vendor/pjproject-${PJSIP_VERSION}.tar.bz2"
    if [[ -f "${OFFLINE_TARBALL}" ]]; then
        echo "[pjsua2] Using bundled source: ${OFFLINE_TARBALL}"
        cp "${OFFLINE_TARBALL}" "${TARBALL}"
    else
        echo "[pjsua2] ERROR: Source tarball not found and no offline bundle."
        echo "         In an air-gapped environment, place pjproject-${PJSIP_VERSION}.tar.bz2"
        echo "         in installer/vendor/ before running this script."
        exit 1
    fi
fi

# ── Extract and configure ─────────────────────────────────────────────────────
echo "[pjsua2] Extracting source..."
cd "${BUILD_DIR}"
tar xjf "${TARBALL}"
cd "${PJSIP_SRC}"

# user.mak: enable SRTP + VP8 video, disable unneeded codecs
cat > user.mak << 'USERMAK'
export CFLAGS += -DPJ_HAS_IPV6=1
export CXXFLAGS += -DPJ_HAS_IPV6=1
USERMAK

# Configure: SRTP (OpenSSL), VP8 via libvpx, disable G711 annex
./configure \
    --with-ssl=/usr \
    --enable-shared \
    --disable-resample-dll \
    --disable-sound \
    CFLAGS="-fPIC -O2 -DPJ_HAS_SRTP=1" \
    CXXFLAGS="-fPIC -O2 -DPJ_HAS_SRTP=1"

# ── Compile pjproject ─────────────────────────────────────────────────────────
echo "[pjsua2] Compiling pjproject (this takes ~5 minutes)..."
make dep
make -j"$(nproc)"

# ── Build pjsua2 Python bindings ──────────────────────────────────────────────
echo "[pjsua2] Building Python bindings (SWIG)..."
cd pjsip-apps/src/swig/python
make

# ── Install into venv ────────────────────────────────────────────────────────
echo "[pjsua2] Installing into ${VENV}..."
SITE=$(${PYTHON} -c "import site; print(site.getsitepackages()[0])")
cp _pjsua2*.so "${SITE}/"
cp pjsua2.py "${SITE}/"
echo "[pjsua2] Installed: ${SITE}/_pjsua2*.so"

# ── Verify ────────────────────────────────────────────────────────────────────
if ${PYTHON} -c "import pjsua2; print('pjsua2 OK')" 2>/dev/null; then
    echo "${PJSIP_VERSION}" > "${VERSION_STAMP}"
    echo "[pjsua2] Build complete. pjsua2 ${PJSIP_VERSION} is ready."
else
    echo "[pjsua2] ERROR: pjsua2 import failed after build. Check output above."
    exit 1
fi
