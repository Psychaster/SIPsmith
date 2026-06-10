#!/usr/bin/env bash
# SIPsmith installer for Ubuntu Server 24.04
# Usage:
#   sudo bash install-sipsmith.sh            # online
#   sudo bash install-sipsmith.sh --offline  # from bundled debs/ and wheels/
#   sudo bash install-sipsmith.sh --uninstall
#
# Requirements: Ubuntu 24.04, static IP, FQDN set via hostnamectl, >=4 GB RAM.

set -euo pipefail

SIPSMITH_VERSION="0.1.0"
SIPSMITH_USER="sipsmith"
SIPSMITH_GROUP="sipsmith"
INSTALL_DIR="/opt/sipsmith"
SRC_DIR="${INSTALL_DIR}/src"
VENV_DIR="${INSTALL_DIR}/venv"
CONFIG_DIR="/etc/sipsmith"
DATA_DIR="/var/lib/sipsmith"
LOG_DIR="/var/log/sipsmith"
RUN_DIR="/run/sipsmith-agent"
TLS_DIR="${DATA_DIR}/tls"
DB_NAME="sipsmith"
DB_USER="sipsmith"

# Script directory (location of the installer or the unpacked bundle root)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_DEBS="${SCRIPT_DIR}/../debs"
BUNDLE_WHEELS="${SCRIPT_DIR}/../wheels"

OFFLINE=0
UNINSTALL=0

# ── Colour helpers ────────────────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; RESET='\033[0m'
info()    { echo -e "${GREEN}[sipsmith]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[sipsmith] WARN:${RESET} $*"; }
error()   { echo -e "${RED}[sipsmith] ERROR:${RESET} $*" >&2; exit 1; }
section() { echo -e "\n${BOLD}━━━ $* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"; }

# ── Argument parsing ──────────────────────────────────────────────────────

for arg in "$@"; do
  case "$arg" in
    --offline)   OFFLINE=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *) error "Unknown argument: $arg" ;;
  esac
done

# ── Must run as root ──────────────────────────────────────────────────────

[[ "${EUID}" -eq 0 ]] || error "This installer must run as root (use sudo)"

# ── Uninstall path ────────────────────────────────────────────────────────

if [[ "${UNINSTALL}" -eq 1 ]]; then
  section "SIPsmith Uninstall"
  read -rp "Remove /var/lib/sipsmith (CA keys, SFTP data, backups)? [y/N] " confirm
  systemctl disable --now sipsmith sipsmith-agent 2>/dev/null || true
  rm -f /etc/systemd/system/sipsmith.service /etc/systemd/system/sipsmith-agent.service
  systemctl daemon-reload
  rm -rf "${INSTALL_DIR}" "${CONFIG_DIR}" "${LOG_DIR}"
  if [[ "${confirm,,}" == "y" ]]; then
    rm -rf "${DATA_DIR}"
    info "Data directory removed."
  else
    info "Data directory preserved at ${DATA_DIR}."
  fi
  # Remove DB and user
  sudo -u postgres psql -c "DROP DATABASE IF EXISTS ${DB_NAME};" 2>/dev/null || true
  sudo -u postgres psql -c "DROP USER IF EXISTS ${DB_USER};" 2>/dev/null || true
  id "${SIPSMITH_USER}" &>/dev/null && userdel "${SIPSMITH_USER}" || true
  info "Uninstall complete."
  exit 0
fi

# ── Preflight ─────────────────────────────────────────────────────────────

section "Preflight Checks"

# Ubuntu 24.04
if ! grep -qE 'Ubuntu 24\.04' /etc/os-release 2>/dev/null; then
  error "SIPsmith requires Ubuntu Server 24.04 LTS. Detected: $(grep PRETTY_NAME /etc/os-release | cut -d= -f2)"
fi
info "OS: Ubuntu 24.04 ✓"

# FQDN
FQDN="$(hostname -f 2>/dev/null || true)"
if [[ -z "${FQDN}" || "${FQDN}" == "localhost" || "${FQDN}" == "$(hostname -s)" ]]; then
  error "No FQDN set. Run: hostnamectl set-hostname <fqdn>  (e.g. sipsmith.lab.example.com)"
fi
info "FQDN: ${FQDN} ✓"

# Static IP (warn only — DHCP is possible in dev)
DEFAULT_IF="$(ip route show default 2>/dev/null | awk '/^default/{print $5; exit}')"
if [[ -n "${DEFAULT_IF}" ]]; then
  if ip addr show "${DEFAULT_IF}" | grep -q "dynamic"; then
    warn "Interface ${DEFAULT_IF} appears to use DHCP. A static IP is strongly recommended."
  else
    info "Network: static IP on ${DEFAULT_IF} ✓"
  fi
fi

# RAM
TOTAL_KB="$(awk '/MemTotal/{print $2}' /proc/meminfo)"
TOTAL_GB=$(( TOTAL_KB / 1024 / 1024 ))
if [[ "${TOTAL_GB}" -lt 3 ]]; then
  warn "Only ~${TOTAL_GB} GB RAM detected. SIPsmith recommends ≥4 GB (8 GB with AD + emulator)."
else
  info "RAM: ~${TOTAL_GB} GB ✓"
fi

# Disk
AVAIL_KB="$(df --output=avail / | tail -1)"
AVAIL_GB=$(( AVAIL_KB / 1024 / 1024 ))
if [[ "${AVAIL_GB}" -lt 20 ]]; then
  warn "Only ~${AVAIL_GB} GB available on /. Recommend ≥40 GB."
else
  info "Disk: ~${AVAIL_GB} GB available ✓"
fi

# ── System packages ───────────────────────────────────────────────────────

section "System Packages"

PKGS=(
  python3.12 python3.12-venv python3.12-dev
  postgresql postgresql-client
  openssh-server
  chrony
  ufw
  openssl
  build-essential libffi-dev libssl-dev
  libpq-dev
  curl
)

if [[ "${OFFLINE}" -eq 1 ]]; then
  info "Offline mode: installing from ${BUNDLE_DEBS}/"
  [[ -d "${BUNDLE_DEBS}" ]] || error "debs/ directory not found next to installer. Is the bundle complete?"
  dpkg -i "${BUNDLE_DEBS}"/*.deb 2>/dev/null || apt-get install -f -y
else
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${PKGS[@]}"
fi
info "System packages installed ✓"

# ── System user & directories ─────────────────────────────────────────────

section "System User & Directories"

if ! id "${SIPSMITH_USER}" &>/dev/null; then
  useradd --system --no-create-home --shell /usr/sbin/nologin \
    --home-dir "${INSTALL_DIR}" "${SIPSMITH_USER}"
  info "Created system user: ${SIPSMITH_USER}"
else
  info "System user ${SIPSMITH_USER} already exists ✓"
fi

install -d -m 0755 -o root       -g root            "${INSTALL_DIR}"
install -d -m 0755 -o root       -g "${SIPSMITH_GROUP}" "${SRC_DIR}"
install -d -m 0700 -o "${SIPSMITH_USER}" -g "${SIPSMITH_GROUP}" "${CONFIG_DIR}"
install -d -m 0750 -o "${SIPSMITH_USER}" -g "${SIPSMITH_GROUP}" "${DATA_DIR}"
install -d -m 0750 -o "${SIPSMITH_USER}" -g "${SIPSMITH_GROUP}" "${DATA_DIR}/tls"
install -d -m 0750 -o "${SIPSMITH_USER}" -g "${SIPSMITH_GROUP}" "${DATA_DIR}/ca"
install -d -m 0750 -o "${SIPSMITH_USER}" -g "${SIPSMITH_GROUP}" "${DATA_DIR}/sftp"
install -d -m 0750 -o "${SIPSMITH_USER}" -g "${SIPSMITH_GROUP}" "${DATA_DIR}/backups"
install -d -m 0750 -o "${SIPSMITH_USER}" -g "${SIPSMITH_GROUP}" "${LOG_DIR}"
install -d -m 0750 -o root       -g "${SIPSMITH_GROUP}" "${RUN_DIR}"
info "Directories created ✓"

# ── Python venv ───────────────────────────────────────────────────────────

section "Python Environment"

if [[ ! -d "${VENV_DIR}" ]]; then
  python3.12 -m venv "${VENV_DIR}"
  info "Created venv at ${VENV_DIR}"
fi

PIP="${VENV_DIR}/bin/pip"

if [[ "${OFFLINE}" -eq 1 ]]; then
  [[ -d "${BUNDLE_WHEELS}" ]] || error "wheels/ directory not found."
  "${PIP}" install --no-index --find-links="${BUNDLE_WHEELS}" sipsmith
else
  "${PIP}" install --upgrade pip -q
  "${PIP}" install -e "${SRC_DIR}[dev]" -q
fi
info "Python packages installed ✓"

# ── Copy source ───────────────────────────────────────────────────────────

section "Application Source"

REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
rsync -a --exclude='.git' --exclude='*.pyc' --exclude='__pycache__' \
  --exclude='.venv' --exclude='venv' --exclude='*.egg-info' \
  "${REPO_ROOT}/" "${SRC_DIR}/"
chown -R "${SIPSMITH_USER}:${SIPSMITH_GROUP}" "${SRC_DIR}"
info "Source copied to ${SRC_DIR} ✓"

# ── PostgreSQL ────────────────────────────────────────────────────────────

section "PostgreSQL"

systemctl enable --now postgresql

# Generate a random DB password
DB_PASS="$(openssl rand -base64 24 | tr -d '/+=\n')"

sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" \
  | grep -q 1 || sudo -u postgres psql -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';"

sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" \
  | grep -q 1 || sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"

info "PostgreSQL: database '${DB_NAME}' ready ✓"

# ── Config file ───────────────────────────────────────────────────────────

section "Configuration"

SECRET_KEY="$(openssl rand -hex 32)"

if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
  cat > "${CONFIG_DIR}/config.yaml" <<EOF
database:
  url: "postgresql+asyncpg://${DB_USER}:${DB_PASS}@localhost/${DB_NAME}"

server:
  host: "0.0.0.0"
  port: 8443
  fqdn: "${FQDN}"
  tls_cert: "${TLS_DIR}/gui.crt"
  tls_key: "${TLS_DIR}/gui.key"

security:
  secret_key: "${SECRET_KEY}"
  session_max_age: 86400
  token_expiry_days: 365

logging:
  level: "INFO"
  log_dir: "${LOG_DIR}"

agent:
  socket: "${RUN_DIR}/agent.sock"

chrony:
  enabled: true
  stratum: 3
  allow_networks: ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
EOF
  chmod 600 "${CONFIG_DIR}/config.yaml"
  chown "${SIPSMITH_USER}:${SIPSMITH_GROUP}" "${CONFIG_DIR}/config.yaml"
  info "Config written to ${CONFIG_DIR}/config.yaml ✓"
else
  info "Config already exists — skipping (delete to regenerate) ✓"
fi

# ── Self-signed TLS cert for GUI ──────────────────────────────────────────

section "TLS Bootstrap Certificate"

if [[ ! -f "${TLS_DIR}/gui.crt" ]]; then
  openssl req -x509 -newkey rsa:4096 -days 3650 -nodes \
    -keyout "${TLS_DIR}/gui.key" \
    -out    "${TLS_DIR}/gui.crt" \
    -subj   "/CN=${FQDN}/O=SIPsmith/C=XX" \
    -addext "subjectAltName=DNS:${FQDN},DNS:localhost" \
    -addext "basicConstraints=critical,CA:FALSE" \
    2>/dev/null
  chmod 600 "${TLS_DIR}/gui.key" "${TLS_DIR}/gui.crt"
  chown "${SIPSMITH_USER}:${SIPSMITH_GROUP}" "${TLS_DIR}/gui.key" "${TLS_DIR}/gui.crt"
  info "Bootstrap TLS cert generated for ${FQDN} ✓"
else
  info "TLS cert already exists ✓"
fi

# ── Chrony config ─────────────────────────────────────────────────────────

section "Chrony / NTP"

cat > /etc/chrony/chrony.conf <<'CHRONY'
# Managed by SIPsmith — do not edit manually.
# SIPsmith acts as the lab's authoritative NTP server (air-gapped).

local stratum 3

allow 10.0.0.0/8
allow 172.16.0.0/12
allow 192.168.0.0/16

driftfile /var/lib/chrony/chrony.drift
rtcsync
makestep 1.0 3
logdir /var/log/chrony
CHRONY

systemctl enable --now chrony
info "Chrony configured and running ✓"

# ── Firewall ──────────────────────────────────────────────────────────────

section "Firewall (ufw)"

ufw --force enable 2>/dev/null || true
ufw allow OpenSSH
ufw allow 8443/tcp    # SIPsmith GUI
ufw allow 123/udp     # NTP
info "ufw rules set ✓"

# ── Run DB migrations ─────────────────────────────────────────────────────

section "Database Migrations"

SIPSMITH_CONFIG="${CONFIG_DIR}/config.yaml" \
  "${VENV_DIR}/bin/alembic" -c "${SRC_DIR}/alembic.ini" upgrade head
info "Migrations applied ✓"

# ── Systemd units ─────────────────────────────────────────────────────────

section "Systemd Services"

cp "${SRC_DIR}/systemd/sipsmith.service"       /etc/systemd/system/
cp "${SRC_DIR}/systemd/sipsmith-agent.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sipsmith-agent sipsmith
info "Services enabled and started ✓"

# ── Bootstrap admin token ─────────────────────────────────────────────────

section "One-Time Admin Bootstrap"

BOOTSTRAP_TOKEN="$(openssl rand -hex 24)"
BOOTSTRAP_FILE="${CONFIG_DIR}/.bootstrap_token"
echo "${BOOTSTRAP_TOKEN}" > "${BOOTSTRAP_FILE}"
chmod 600 "${BOOTSTRAP_FILE}"
chown "${SIPSMITH_USER}:${SIPSMITH_GROUP}" "${BOOTSTRAP_FILE}"

# ── Done ──────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}${GREEN}━━━ SIPsmith ${SIPSMITH_VERSION} installed successfully! ━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
echo -e "  GUI URL  : ${BOLD}https://${FQDN}:8443${RESET}"
echo -e "  Bootstrap token (one-time, expires after first admin setup):"
echo -e "  ${BOLD}${BOOTSTRAP_TOKEN}${RESET}"
echo ""
echo -e "  ${YELLOW}NOTE:${RESET} The GUI uses a self-signed certificate. Your browser will warn."
echo -e "  Once the CA plugin is enabled, the certificate will be re-issued from your own CA."
echo ""
echo -e "  Service status: ${BOLD}systemctl status sipsmith${RESET}"
echo -e "  Logs          : ${BOLD}journalctl -u sipsmith -f${RESET}"
echo ""
