# SIPsmith Installation Guide

**Applies to:** SIPsmith 0.1.x on Ubuntu Server 24.04 LTS

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Air-Gap Considerations](#2-air-gap-considerations)
3. [Online Installation](#3-online-installation)
4. [Building the Offline Bundle](#4-building-the-offline-bundle)
5. [Air-Gapped Installation](#5-air-gapped-installation)
6. [Post-Installation Checklist](#6-post-installation-checklist)
7. [Plugin Prerequisites](#7-plugin-prerequisites)
8. [Network Port Reference](#8-network-port-reference)
9. [Upgrading](#9-upgrading)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Prerequisites

### 1.1 Hardware

| Resource | Minimum | Recommended |
|---|---|---|
| CPU | 2 vCPUs | 4 vCPUs |
| RAM | 4 GB | 8 GB (required when running AD + SIP emulator together) |
| Disk | 40 GB | 100 GB+ (CDR/DRF archives grow; plan accordingly) |
| NIC | 1 (single NIC, static IP) | 1 (multi-IP support post-v1) |

> **Warning:** Attempting to run the AD plugin or SIP emulator on a host with fewer than 8 GB RAM will result in degraded performance. The installer will warn but will not block installation.

### 1.2 Operating System

SIPsmith requires **Ubuntu Server 24.04 LTS** (Noble Numbat), 64-bit. No other release is supported.

- Ubuntu 22.04 — not supported.
- Ubuntu 24.04 Desktop — not supported (the desktop environment wastes resources and introduces network management conflicts).
- Any release other than 24.04 — the installer will exit with an error.

Verify your OS version before proceeding:

```bash
lsb_release -a
# Expected: Ubuntu 24.04.x LTS
```

### 1.3 Network: Static IP and FQDN

SIPsmith issues TLS certificates, acts as the lab's authoritative DNS and NTP server, and provisions Active Directory — all of which require a stable hostname and IP. A DHCP-assigned address will break these services after any lease renewal.

**Step 1 — Assign a static IP using Netplan.**

Identify your primary interface name:

```bash
ip link show
```

Edit (or create) `/etc/netplan/00-sipsmith.yaml`, replacing values for your environment:

```yaml
network:
  version: 2
  ethernets:
    ens3:                          # replace with your interface name
      addresses: [192.168.10.5/24]
      routes:
        - to: default
          via: 192.168.10.1
      nameservers:
        addresses: [192.168.10.5]  # point to itself once DNS plugin is enabled
        search: [lab.example.com]
```

Apply the configuration:

```bash
sudo netplan apply
```

**Step 2 — Set the FQDN.**

The installer reads the FQDN from `hostname -f`. Set it before running the installer:

```bash
sudo hostnamectl set-hostname sipsmith.lab.example.com
```

Verify the full FQDN resolves:

```bash
hostname -f
# Expected: sipsmith.lab.example.com
```

If `hostname -f` returns only the short hostname (e.g., `sipsmith`) rather than the fully qualified name, edit `/etc/hosts` and ensure an entry exists:

```
192.168.10.5  sipsmith.lab.example.com  sipsmith
```

> **Note:** The installer checks for a valid FQDN and will refuse to proceed if `hostname -f` returns `localhost` or an unqualified name. An unqualified hostname will result in a TLS certificate with an invalid CN, breaking browser trust and SIP endpoint registration.

### 1.4 Other Requirements

- The installer must run as `root` (via `sudo`).
- `bash` 5.x (ships with Ubuntu 24.04).
- PostgreSQL 16 and Python 3.12 are installed by the installer — do not pre-install conflicting versions.
- Open port 8443/TCP inbound from management workstations.

---

## 2. Air-Gap Considerations

SIPsmith is designed from the ground up to operate in an air-gapped environment — a network that has no path to the public internet. This is not an afterthought; it is the primary operating mode.

**What "air-gapped" means for SIPsmith:**

- The installer in `--offline` mode installs all system packages from a locally bundled `debs/` directory and all Python dependencies from a `wheels/` directory. `apt` is never called against an external mirror.
- The web GUI vendors all JavaScript (HTMX), CSS, and font assets locally. There are no CDN references anywhere in the codebase; a CI lint rule enforces this.
- The appliance does not make outbound connections at runtime for any purpose — no telemetry, no update checks, no OCSP calls to public CAs (SIPsmith is the CRL distribution point for its own CA).
- Plugin updates, appliance upgrades, and UC knowledge-pack updates are all delivered as signed `.tar.gz` or `.zip` files uploaded through the GUI — the same path whether online or offline.
- The full admin documentation is served from the appliance itself at `https://<fqdn>:8443/docs`.

**What you need to do before the air gap:**

For an initial install on an air-gapped machine, you must build the offline bundle on a machine that does have internet access and carry the resulting tarball across the air gap. See [Section 4](#4-building-the-offline-bundle).

For subsequent upgrades, the same bundle mechanism applies: download the new release bundle on an internet-connected machine and upload it via the GUI or carry the tarball across and re-run the installer.

---

## 3. Online Installation

Use this method when the target host can reach the Ubuntu package mirrors and PyPI, even temporarily (e.g., during initial lab setup before the air gap is enforced).

**Step 1 — Clone or copy the SIPsmith source to the target host.**

```bash
# Example: from a local mirror or USB
tar xzf sipsmith-src.tar.gz
cd sipsmith
```

**Step 2 — Verify prerequisites** (static IP, FQDN, OS version — see Section 1).

**Step 3 — Run the installer.**

```bash
sudo bash installer/install-sipsmith.sh
```

The installer performs the following steps automatically:

1. Preflight checks: OS version, FQDN, static IP detection, RAM and disk warnings.
2. Installs system packages via `apt` (Python 3.12, PostgreSQL, OpenSSH, chrony, ufw, build tools).
3. Creates the `sipsmith` system user and directory tree under `/opt/sipsmith`, `/etc/sipsmith`, `/var/lib/sipsmith`, and `/var/log/sipsmith`.
4. Creates a Python 3.12 virtual environment at `/opt/sipsmith/venv` and installs all Python dependencies.
5. Initializes the PostgreSQL database (`sipsmith` database and user, with a randomly generated password written to `/etc/sipsmith/config.yaml`).
6. Generates a bootstrap self-signed TLS certificate for the GUI (valid for 10 years; replaced by your own CA once the CA plugin is enabled).
7. Writes `/etc/sipsmith/config.yaml` (mode 0600) with generated secrets.
8. Configures chrony as the lab's authoritative NTP server.
9. Configures `ufw` (allows SSH and 8443/TCP).
10. Runs Alembic database migrations.
11. Installs and starts the `sipsmith` and `sipsmith-agent` systemd services.
12. Generates a one-time bootstrap admin token.

**Step 4 — Record the installer output.**

At the end of a successful install, the console prints:

```
━━━ SIPsmith 0.1.0 installed successfully! ━━━━━━━━━━━━━━━━━━━━

  GUI URL  : https://sipsmith.lab.example.com:8443
  Bootstrap token (one-time, expires after first admin setup):
  a3f7c2e1...

  NOTE: The GUI uses a self-signed certificate. Your browser will warn.
  Once the CA plugin is enabled, the certificate will be re-issued from your own CA.

  Service status: systemctl status sipsmith
  Logs          : journalctl -u sipsmith -f
```

> **Warning:** Copy the bootstrap token immediately. It is printed once and stored in `/etc/sipsmith/.bootstrap_token` (readable only by root). It expires after you complete the first admin account setup in the GUI.

**Step 5 — Open the GUI** in a browser:

```
https://sipsmith.lab.example.com:8443
```

Your browser will present a certificate warning because the bootstrap cert is self-signed. Accept the exception and proceed to first login (see [Section 6](#6-post-installation-checklist)).

---

## 4. Building the Offline Bundle

Run this on an **internet-connected Ubuntu 24.04** machine. The output is a single tarball that contains everything needed to install SIPsmith on an air-gapped host.

**Step 1 — Ensure you are running Ubuntu 24.04** on the build machine. The `.deb` files downloaded are architecture- and release-specific.

**Step 2 — Install build prerequisites** on the build machine:

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv python3-pip rsync curl
```

**Step 3 — Run the bundle builder** from the SIPsmith repo root:

```bash
bash installer/build-bundle.sh
```

The script:
- Downloads all required `.deb` packages (with recursive dependency resolution) into `debs/`.
- Downloads all Python wheels as pre-built binaries for `manylinux_2_35_x86_64` / Python 3.12 into `wheels/`.
- Fetches the HTMX JavaScript asset and stores it under `sipsmith/ui/static/js/` (vendored; no CDN references at runtime).
- Copies the application source.
- Produces `/tmp/sipsmith-<version>-offline.tar.gz`.

**Step 4 — Verify the bundle:**

```bash
ls -lh /tmp/sipsmith-*.tar.gz
tar tzf /tmp/sipsmith-*-offline.tar.gz | head -30
```

Confirm the archive contains `install-sipsmith.sh`, `debs/`, `wheels/`, and `app/` at the top level.

**Step 5 — Transfer the tarball** across the air gap using whatever approved transport is available (USB drive, secured file share, physical media).

---

## 5. Air-Gapped Installation

This procedure assumes the offline bundle tarball has been transferred to the target host.

**Step 1 — Extract the bundle:**

```bash
tar xzf sipsmith-0.1.0-offline.tar.gz
cd sipsmith-0.1.0-offline
```

**Step 2 — Verify prerequisites** (static IP, FQDN, OS version — see Section 1). The target host must already have a static IP and FQDN configured; the installer cannot fetch anything from the network.

**Step 3 — Run the installer in offline mode:**

```bash
sudo bash install-sipsmith.sh --offline
```

The installer behaves identically to the online mode with two differences:
- System packages are installed from `debs/` using `dpkg -i` followed by `apt-get install -f` to resolve any ordering issues.
- Python packages are installed from `wheels/` using `pip install --no-index --find-links`.

No outbound network calls are made.

**Step 4 — Record the bootstrap token** from the installer output (same as Step 4 in Section 3).

**Step 5 — Open the GUI:**

```
https://sipsmith.lab.example.com:8443
```

---

## 6. Post-Installation Checklist

### 6.1 Verify Services

Confirm all expected services are running:

```bash
systemctl status sipsmith sipsmith-agent postgresql named smbd chrony ssh
```

All six services should show `active (running)`. `named` and `smbd` will only be active after the DNS and AD plugins are enabled respectively.

Tail the application log to confirm startup completed without errors:

```bash
journalctl -u sipsmith -f
# Or: tail -f /var/log/sipsmith/sipsmith.log
```

### 6.2 First Login

Navigate to `https://<fqdn>:8443`. The bootstrap login screen accepts the one-time token printed by the installer:

```
┌─ SIPsmith :8443 ─────────────────────────────────────────────┐
│ [Dashboard] [DNS] [CA] [SFTP] [AD] [CTI] [xAPI] [Emulator]  │
│                                                               │
│  ┌─ First Login ──────────────────────────────────────────┐  │
│  │  Username:  admin                                       │  │
│  │  Password:  [from installer output]                     │  │
│  │                          [ Log In ]                     │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### 6.3 Set the Admin Password

After first login you are prompted to set a permanent password for the `admin` account. Choose a strong password; it protects access to CA private key operations, Active Directory provisioning, and all lab infrastructure.

### 6.4 Enable Plugins

Navigate to **System → Plugins**. Plugins are disabled by default. Enable them in dependency order:

1. **DNS** — no dependencies; enable first if you plan to use AD.
2. **CA** — no dependencies; enabling it triggers the GUI to re-issue its own TLS certificate from your new internal CA within a few seconds. Your browser will prompt for a new certificate exception.
3. **SFTP** — no dependencies.
4. **AD** — requires DNS to be enabled first (uses BIND9_DLZ mode).
5. **Records** — requires SFTP.
6. **UC Certs** — requires CA and DNS.
7. **SIP Emulator** — requires the pjsua2 build (see Section 7).
8. **CTI** — requires Java runtime and JTAPI jar (see Section 7).
9. **xAPI** — no OS-level dependencies.

> **Note:** `/etc/sipsmith/config.yaml` is the appliance configuration file (mode 0600). Do not edit it directly; all configuration changes should be made through the GUI. Direct edits may be overwritten by the installer during upgrades, and changes will not be audited.

---

## 7. Plugin Prerequisites

Some plugins require additional OS-level setup that the installer does not handle automatically.

### 7.1 AD Plugin — Samba

The AD plugin provisions Samba 4 as a full Active Directory Domain Controller. Samba is not included in the core installer package list; the AD plugin installs it when you enable it in the GUI (online mode) or from the bundle in offline mode. After enabling:

- The plugin runs the Samba AD provisioning wizard in the GUI.
- DNS must be enabled first; the AD plugin uses BIND9_DLZ to serve the AD zone inside the existing BIND instance — it does not run a separate DNS listener.
- Port 88 (Kerberos), 389 (LDAP), and 636 (LDAPS) are added to `ufw` by the plugin's manifest at enable time.
- LDAPS certificate is automatically issued from the CA plugin and rotated on renewal.

### 7.2 CTI Plugin — Java and JTAPI

The CTI plugin uses a Java sidecar process communicating over a Unix socket. It requires:

1. **Java 11+ runtime** on the SIPsmith host:

   ```bash
   sudo apt-get install -y openjdk-21-jre-headless
   ```

   In offline mode, include `openjdk-21-jre-headless` in the bundle by adding it to `PKGS` in `build-bundle.sh` before building.

2. **JTAPI jar from the CUCM cluster.** The JTAPI client library must version-match the CUCM release. SIPsmith can fetch it directly from the cluster (the cluster serves it at `http://<cucm-pub>/plugins/jtapiwin.exe` / `jtapi.jar`) even in an air-gapped lab, since CUCM itself is on the local network. The CTI plugin setup wizard in the GUI guides you through this step and stores the jar per cluster+version under `/var/lib/sipsmith/`. After a CUCM upgrade, bump the cluster version field in the GUI and the wizard will prompt you to refresh the jar.

### 7.3 SIP Endpoint Emulator — pjsua2 Build

The SIP emulator requires a custom pjsua2 build with video support (VP8, H.264 via OpenH264) and SRTP enabled. The distro package (`python3-pjproject`) does not include these capabilities.

The build process is documented in the `pjsua2-build` skill and results in a wheel that must be included in the offline bundle. Summary:

1. On an internet-connected build machine, run the pjsua2 build script (see `scripts/build-pjsua2.sh` — generated during Phase 6 of the build plan).
2. Place the resulting `.whl` file in the `wheels/` directory before running `build-bundle.sh`, or add it to an existing bundle's `wheels/` directory.
3. When the SIP Emulator plugin is enabled in the GUI, it installs the wheel from `/opt/sipsmith/venv` automatically.

---

## 8. Network Port Reference

### Ports SIPsmith Listens On

| Port | Protocol | Service | Plugin | Notes |
|---|---|---|---|---|
| 8443 | TCP | HTTPS GUI / API | Core | Primary management interface |
| 53 | UDP/TCP | DNS | DNS | BIND9; also serves AD zone via DLZ |
| 123 | UDP | NTP | Core (chrony) | Lab authoritative time server |
| 22 | TCP | SSH / SFTP | SFTP | OpenSSH; SFTP accounts are chroot-isolated |
| 389 | TCP | LDAP | AD | Samba AD DC; enabled with AD plugin |
| 636 | TCP | LDAPS | AD | Samba AD DC with CA-issued cert |
| 88 | TCP/UDP | Kerberos | AD | Samba AD DC; enabled with AD plugin |
| 443 | TCP | HTTPS CDR receiver | Records | CMS CDR HTTP(S) POST endpoint |
| 514 | UDP | Syslog receiver | Records | Expressway event/call records |
| 5060 | UDP/TCP | SIP (non-secure) | SIP Emulator | pjsua2 emulated endpoints |
| 5061 | TCP | SIP TLS | SIP Emulator | Secure SIP registration to CUCM |

### Outbound Connections SIPsmith Makes (LAN Only)

| Destination | Port | Protocol | Purpose |
|---|---|---|---|
| CUCM Publisher | 8443 | HTTPS | AXL API (UC cert orchestration, CTI setup) |
| CUCM Publisher | 2748 | TCP | JTAPI CTI (CTI plugin sidecar) |
| CUCM Publisher | 80 | HTTP | JTAPI jar download during CTI setup |
| CMS | 22 | SSH | Automated cert push (uc-certs automated tier) |
| Expressway | 443 | HTTPS | REST API for cert operations and record pulls |
| RoomOS Devices | 443 / 22 | HTTPS/SSH | xAPI direct (WebSocket or SSH) |

> **Note:** All entries in the outbound table are LAN destinations within the lab network. SIPsmith makes no connections to the public internet at any time.

---

## 9. Upgrading

SIPsmith upgrades are performed by re-running the installer. The installer is idempotent: it will not recreate the database or overwrite an existing `/etc/sipsmith/config.yaml`.

**Online upgrade:**

```bash
# Pull new source or extract new release archive
sudo bash installer/install-sipsmith.sh
```

**Air-gapped upgrade:**

1. Build a new offline bundle from the updated source on an internet-connected machine (Section 4).
2. Transfer the bundle across the air gap.
3. Extract and run:
   ```bash
   tar xzf sipsmith-<new-version>-offline.tar.gz
   cd sipsmith-<new-version>-offline
   sudo bash install-sipsmith.sh --offline
   ```

The installer during an upgrade:
- Stops `sipsmith` and `sipsmith-agent`.
- Syncs updated source to `/opt/sipsmith/src`.
- Updates Python packages in the venv.
- Runs any new Alembic migrations (`alembic upgrade head`).
- Restarts services.

After upgrading, if you have changed a cluster's CUCM version (post-upgrade), update the version field in **System → Clusters** so that UC cert knowledge packs and the CTI plugin apply the correct rules for the new release.

---

## 10. Troubleshooting

### 10.1 Installer Fails: "No FQDN set"

```
[sipsmith] ERROR: No FQDN set. Run: hostnamectl set-hostname <fqdn>
```

Resolution:

```bash
sudo hostnamectl set-hostname sipsmith.lab.example.com
# Also verify /etc/hosts has the full FQDN mapped to the static IP
grep sipsmith /etc/hosts
```

### 10.2 Installer Fails: "Ubuntu 24.04 required"

The installer reads `/etc/os-release`. If you see this error on a fresh Ubuntu 24.04 install, verify:

```bash
cat /etc/os-release | grep VERSION_ID
# Expected: VERSION_ID="24.04"
```

If you are running a daily/interim build, the PRETTY_NAME may differ. Contact the SIPsmith maintainer.

### 10.3 Offline Mode: "debs/ directory not found"

The `--offline` flag expects `debs/` and `wheels/` to be siblings of `install-sipsmith.sh` (i.e., at the root of the extracted bundle). Verify the extraction:

```bash
ls sipsmith-*-offline/
# Expected: install-sipsmith.sh  debs/  wheels/  app/
```

If you extracted into a subdirectory, `cd` to the bundle root before running the installer.

### 10.4 Service Fails to Start

Check the journal for the specific error:

```bash
journalctl -u sipsmith --no-pager -n 50
journalctl -u sipsmith-agent --no-pager -n 50
```

Common causes:
- **Database connection refused** — PostgreSQL not running: `sudo systemctl start postgresql`
- **Config file missing** — `/etc/sipsmith/config.yaml` was deleted; re-run the installer to regenerate it.
- **Port 8443 in use** — another process is bound to 8443: `sudo ss -tlnp | grep 8443`

### 10.5 Browser Certificate Warning Persists After CA Enable

When you enable the CA plugin, SIPsmith re-issues its GUI certificate from the new internal CA. To eliminate the browser warning permanently:

1. Download the CA chain from **CA → Download CA Chain** (PEM format).
2. Install it in your browser's or OS's trusted certificate store.
3. On Windows: double-click the `.pem`, choose "Install Certificate," and place it in "Trusted Root Certification Authorities."
4. On macOS: double-click the `.pem`, open Keychain Access, find the cert, and set it to "Always Trust."
5. Restart the browser.

### 10.6 GUI Unreachable After Install

Verify `ufw` is not blocking port 8443:

```bash
sudo ufw status verbose | grep 8443
```

If the rule is absent:

```bash
sudo ufw allow 8443/tcp
sudo ufw reload
```

Verify the service is listening:

```bash
sudo ss -tlnp | grep 8443
```

If the port is not listed, the service has not started successfully — consult the journal (see 10.4).

### 10.7 Uninstalling SIPsmith

```bash
sudo bash installer/install-sipsmith.sh --uninstall
```

The uninstaller stops and removes the `sipsmith` and `sipsmith-agent` services, removes `/opt/sipsmith`, `/etc/sipsmith`, and `/var/log/sipsmith`, drops the PostgreSQL database and user, and removes the `sipsmith` system user. It prompts before removing `/var/lib/sipsmith` (which contains CA keys, SFTP data, and backups). Answer `y` only if you intend to destroy all appliance data.

> **Warning:** Uninstalling does not remove BIND9, Samba, OpenSSH, or PostgreSQL system packages — only SIPsmith's configuration of them. If you wish to remove those packages as well, do so manually after uninstall.
