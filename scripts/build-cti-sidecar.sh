#!/usr/bin/env bash
# Build the CTI JTAPI sidecar uber-jar and install it into /opt/sipsmith/lib/
# Usage:
#   ./scripts/build-cti-sidecar.sh            # online Maven build
#   ./scripts/build-cti-sidecar.sh --offline  # use Maven local repo cache (air-gapped)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SIDECAR_DIR="${REPO_ROOT}/sipsmith/cti-sidecar"
DEST_DIR="/opt/sipsmith/lib"
JAR_NAME="sipsmith-cti-sidecar.jar"

OFFLINE=false
for arg in "$@"; do
  [[ "$arg" == "--offline" ]] && OFFLINE=true
done

echo "=== SIPsmith CTI Sidecar — Maven build ==="
echo "Source: ${SIDECAR_DIR}"
echo "Output: ${DEST_DIR}/${JAR_NAME}"
echo ""

# Verify Java and Maven are available
if ! command -v java &>/dev/null; then
  echo "ERROR: java not found. Install default-jre-headless." >&2
  exit 1
fi
if ! command -v mvn &>/dev/null; then
  echo "ERROR: mvn not found. Install maven." >&2
  exit 1
fi

java_ver=$(java -version 2>&1 | head -1)
echo "Java: ${java_ver}"
mvn_ver=$(mvn --version 2>&1 | head -1)
echo "Maven: ${mvn_ver}"
echo ""

cd "${SIDECAR_DIR}"

MVN_OPTS=("-q" "--no-transfer-progress" "package" "-DskipTests")
if [[ "$OFFLINE" == "true" ]]; then
  MVN_OPTS+=("--offline")
  echo "Running in offline mode (using local Maven repo cache)"
fi

echo "Building..."
mvn "${MVN_OPTS[@]}"

BUILT_JAR="${SIDECAR_DIR}/target/sipsmith-cti-sidecar-0.1.jar"
if [[ ! -f "${BUILT_JAR}" ]]; then
  echo "ERROR: expected jar not found at ${BUILT_JAR}" >&2
  exit 1
fi

mkdir -p "${DEST_DIR}"
install -m 644 "${BUILT_JAR}" "${DEST_DIR}/${JAR_NAME}"

echo ""
echo "=== Build complete ==="
echo "Installed: ${DEST_DIR}/${JAR_NAME}"
echo ""
echo "Runtime usage:"
echo "  java -cp /path/to/jtapiXXX.jar:${DEST_DIR}/${JAR_NAME} com.sipsmith.cti.Main"
echo ""
echo "The CTI plugin will look for the JTAPI jar at the path configured per cluster."
echo "Obtain jtapi.jar from CUCM Administration > Application > Plugins > Cisco JTAPI."
