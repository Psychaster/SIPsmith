# SIPsmith Administrator Guide

**Platform:** Ubuntu Server 24.04 LTS  
**Served locally at:** `https://<fqdn>:8443/docs`  
**Version:** 1.0 (covers Phases 0–8)

---

## Contents

1. [Service Management](#1-service-management)
2. [Plugin Lifecycle](#2-plugin-lifecycle)
3. [DNS Plugin](#3-dns-plugin)
4. [CA Plugin](#4-ca-plugin)
5. [SFTP Plugin](#5-sftp-plugin)
6. [UC Cert Orchestrator](#6-uc-cert-orchestrator)
7. [Records Landing](#7-records-landing)
8. [SIP Endpoint Emulator](#8-sip-endpoint-emulator)
9. [Active Directory](#9-active-directory)
10. [CTI Control](#10-cti-control)
11. [xAPI Device Control](#11-xapi-device-control)
12. [User Management and Roles](#12-user-management-and-roles)
13. [Backup and Restore](#13-backup-and-restore)
14. [Security Hardening](#14-security-hardening)

---

## 1. Service Management

### 1.1 Systemctl Commands

SIPsmith is composed of the control plane and its managed daemons. Use these commands
for day-to-day service operations:

```bash
# Control plane and root helper
systemctl status sipsmith            # FastAPI app (port 8443)
systemctl status sipsmith-agent      # root helper daemon (privileged operations)

# Managed daemons (started by plugins when enabled)
systemctl status named               # DNS (BIND9)
systemctl status samba-ad-dc         # Active Directory (Samba)
systemctl status ssh                 # SFTP (OpenSSH)
systemctl status chrony              # NTP (always active — core service)
systemctl status postgresql          # Database

# Restart the control plane after a config change
systemctl restart sipsmith

# Reload a daemon without restarting (preferred — plugins do this automatically)
rndc reload                          # BIND9 zone reload
systemctl reload ssh                 # sshd reload
```

> **Note:** Do not manually edit config files under `/etc/bind/` or
> `/etc/ssh/sshd_config.d/sipsmith.conf`. SIPsmith owns those files — manual edits
> will be overwritten on the next plugin config apply and will not pass the
> validate-then-reload gate.

### 1.2 Log Locations

All structured logs go to both journald and `/var/log/sipsmith/`.

| Log | How to view |
|---|---|
| Control plane (live) | `journalctl -u sipsmith -f` |
| Control plane (file) | `/var/log/sipsmith/app.log` |
| Audit log (who changed what) | GUI → System → Audit Log, or `/var/log/sipsmith/audit.log` |
| BIND9 | `journalctl -u named` |
| Samba AD DC | `/var/log/samba/log.samba` |
| OpenSSH | `journalctl -u ssh` |
| chrony | `journalctl -u chrony` |

Set the log level in `/etc/sipsmith/config.yaml` under `logging.level`
(`DEBUG`, `INFO`, `WARNING`, `ERROR`). `INFO` is appropriate for production labs.

### 1.3 The Configuration File

`/etc/sipsmith/config.yaml` is the appliance-level config. It holds database
credentials, TLS paths, and the agent socket path. It is **mode 0600, owned root:sipsmith**.

```yaml
database:
  url: "postgresql+asyncpg://sipsmith:CHANGE_ME@localhost/sipsmith"

server:
  host: "0.0.0.0"
  port: 8443
  fqdn: "sipsmith.lab.example.com"
  tls_cert: "/var/lib/sipsmith/tls/gui.crt"
  tls_key:  "/var/lib/sipsmith/tls/gui.key"

security:
  secret_key: "CHANGE_ME_32_BYTE_HEX"
  session_max_age: 86400
  token_expiry_days: 365

logging:
  level: "INFO"
  log_dir: "/var/log/sipsmith"

agent:
  socket: "/run/sipsmith-agent/agent.sock"

chrony:
  enabled: true
  stratum: 3
  allow_networks: ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
```

> **Warning:** Never commit `config.yaml` to source control. Only `config.example.yaml`
> belongs in the repo. The actual file is gitignored.

### 1.4 Key Directory Paths

| Path | Contents |
|---|---|
| `/opt/sipsmith/` | Code, venv, plugins (read-mostly) |
| `/etc/sipsmith/config.yaml` | Appliance config (0600) |
| `/var/lib/sipsmith/ca/` | CA key material — **back this up; protect it** |
| `/var/lib/sipsmith/sftp/<user>/` | Chrooted SFTP shares |
| `/var/lib/sipsmith/backups/` | Appliance backup tarballs |
| `/var/lib/sipsmith/dns/zones/` | BIND9 zone files (owned by SIPsmith) |
| `/var/lib/sipsmith/cti/<cluster_id>/` | Per-cluster JTAPI jars |
| `/var/log/sipsmith/` | App and audit log files |

---

## 2. Plugin Lifecycle

### 2.1 Enabling and Disabling Plugins

Navigate to **System → Plugins** to see all available plugins with their current status.

```
┌─ System → Plugins ──────────────────────────────────────────────────────────┐
│                                                                              │
│  dns         DNS Server          [ok]       [Disable] [Configure]           │
│  ca          Certificate Auth.   [ok]       [Disable] [Configure]           │
│  sftp        SFTP Server         [ok]       [Disable] [Configure]           │
│  ad          Active Directory    [ok]       [Disable] [Configure]           │
│  uc-certs    UC Cert Orchestr.   [ok]       [Disable] [Configure]           │
│  records     Records Landing     [ok]       [Disable] [Configure]           │
│  sip-emu     SIP Emulator        [warn]     [Disable] [Configure]           │
│  cti         CTI Control         [ok]       [Disable] [Configure]           │
│  xapi        xAPI Device Ctrl    [ok]       [Disable] [Configure]           │
│                                                                              │
│  Upload plugin (.zip): [Choose file] [Install]                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

Click **Enable** to install required system packages, create directories, write initial
config, and start the underlying daemon. Click **Disable** to stop the daemon and remove
firewall rules (plugin config and data are preserved).

### 2.2 Plugin Dependency Ordering

The plugin manager resolves dependencies from each plugin's `plugin.yaml` manifest
before enabling. Attempting to enable a plugin before its dependencies are satisfied
will produce a clear error.

| Plugin | Requires |
|---|---|
| `dns` | None |
| `ca` | None |
| `sftp` | None |
| `ad` | `dns` (BIND9_DLZ — AD zone served inside the BIND instance) |
| `uc-certs` | `ca`, `dns` |
| `records` | `sftp` (for DRF/CDR landing) |
| `sip-emu` | `ca` (for TLS/SRTP cert issuance) |
| `cti` | None (but requires CUCM reachability and a built sidecar JAR) |
| `xapi` | None |

### 2.3 Plugin Status States

Each plugin card on the dashboard shows a functional probe result, not just a
systemd state:

| State | Meaning |
|---|---|
| `ok` | Daemon running and functional probe passed (e.g., DNS resolves a test record) |
| `warn` | Running but something needs attention (e.g., cert expiring soon, pjsua2 not built) |
| `error` | Daemon down or functional probe failed — action required |
| `disabled` | Plugin is not enabled; daemon not running |

Click any status card to see the full probe output.

---

## 3. DNS Plugin

### 3.1 Zone and Record Management

```
┌─ DNS Plugin ────────────────────────────────────────────────────────────────┐
│  Zones: [+ New Zone]   [Import CSV]                                         │
│  ┌──────────────────────────┬───────┬────────┬────────────────────────────┐ │
│  │ Zone                     │ Type  │Records │                            │ │
│  ├──────────────────────────┼───────┼────────┼────────────────────────────┤ │
│  │ lab.cisco.com            │ fwd   │ 42     │ [Records] [Preset] [Del]   │ │
│  │ 10.in-addr.arpa          │ rev   │ 38     │ [Records] [Preset] [Del]   │ │
│  │ lab.local (AD zone)      │ fwd   │ DLZ    │ [Managed by AD plugin]     │ │
│  └──────────────────────────┴───────┴────────┴────────────────────────────┘ │
│                                                                              │
│  Settings: Forwarders [8.8.8.8, 8.8.4.4]   Recursion ACL [localnets]       │
│  [Apply Settings]   Last reload: 2026-06-11 14:32:01 UTC — OK              │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Creating a zone:** DNS → Zones → New Zone. Provide the zone name, primary NS FQDN,
and admin email. The plugin renders `named.conf` and zone file templates, runs
`named-checkconf` and `named-checkzone`, then calls `rndc reload` only if validation
passes. If validation fails, the old config is kept and the error is displayed.

**Record types supported:** A, AAAA, PTR, CNAME, MX, TXT, SRV. Enable "Auto-PTR" on
an A record to create the corresponding reverse record automatically.

**Bulk import:** DNS → Zones → (open zone) → Import CSV. Columns: `name,rdata[,ttl]`.
Useful for standing up a full CUCM cluster's A records in one operation.

**Zone files** are stored at `/var/lib/sipsmith/dns/zones/<zone-name>.zone`.
The master BIND config include is `/etc/bind/sipsmith.conf`.

### 3.2 BIND9_DLZ Mode for AD Integration

When the AD plugin provisions a domain, it wires Samba's DLZ module into the existing
BIND instance. A single BIND9 process owns port 53, serving all SIPsmith-managed zones
normally, with the AD zone served via the DLZ database backend.

To enable DLZ after AD provisioning (happens automatically, but can be checked):
DNS → Settings → DLZ section shows the module path and loaded status. If BIND fails
to load the DLZ module, the error is shown here along with the detected module path.

Manual check:
```bash
rndc status            # confirm named is running
rndc reload            # force reload
samba-tool dns query 127.0.0.1 LAB.LOCAL @ ALL -U Administrator
```

> **Warning:** Never run Samba's internal DNS server on port 53 alongside BIND9. The
> BIND9_DLZ mode is the only supported configuration. Delegation mode is documented
> in `DESIGN.md` as a fallback only.

### 3.3 Forwarders and Recursion ACL

DNS → Settings:

- **Forwarders:** IP addresses of upstream resolvers. Leave empty for a fully
  air-gapped lab where all names are local.
- **Recursion ACL:** defaults to `localnets`. Tighten to specific subnets in a
  larger network. Recursion is never served to `any` in the default config.

Point CUCM at SIPsmith for DNS:
CUCM OS Admin → Platform → Network IP Settings → Preferred DNS: `<sipsmith-ip>`

### 3.4 Validate-Then-Reload Guarantee

Every DNS config change follows this sequence:

1. Jinja2 templates rendered to staging files.
2. `named-checkconf` run against staged `named.conf`.
3. `named-checkzone <zone>` run for each modified zone.
4. If all checks pass: staged files moved to live paths, `rndc reload` called.
5. If any check fails: staged files discarded, old config preserved, error returned
   to the GUI and written to the audit log.

No partial state is ever applied.

### 3.5 SRV Preset Table

Apply SRV presets per zone via DNS → Zones → (open zone) → Apply Preset.

| Preset | SRV Records Created |
|---|---|
| CUCM (standard) | `_cisco-uds._tcp`, `_cuplogin._tcp`, `_sip._tcp`, `_sips._tcp`, `_sip._udp` |
| Expressway Edge (MRA) | `_collab-edge._tls`, `_xmpp-server._tcp` |
| IM&P (secondary) | `_cisco-uds._tcp` priority 20 |
| SIP trunk | `_sip._tcp`, `_sips._tcp` |

SRV records have first-class GUI support: priority, weight, port, and target are
individually editable. The UC Cert Orchestrator also creates required SRV records
automatically during its DNS pre-flight step.

---

## 4. CA Plugin

### 4.1 Root CA and Issuing CA Hierarchy

SIPsmith implements an offline-style two-tier CA in pure Python (`cryptography` lib):

```
  SIPsmith Root CA  (RSA 4096 or EC P-384, self-signed, offline-style)
        |
  SIPsmith Issuing CA  (signs all end-entity certs)
        |
  ┌─────┴──────┬──────────┬──────────────┐
  │            │          │              │
 server      client    server+client   custom
 certs       certs     certs           certs
```

Both CA certificates belong in every product's trust store. CUCM needs both in
`tomcat-trust` and `callmanager-trust`. The CA plugin's Overview page has one-click
download buttons for `root.pem`, `issuing.pem`, and `ca-chain.pem` (root + issuing
concatenated, suitable for direct upload to CUCM).

CA keys are stored encrypted at rest in `/var/lib/sipsmith/ca/` (mode 0600). Keys
never leave the appliance and are never exposed via any API endpoint.

### 4.2 Certificate Profiles

| Profile | Key Usage | Extended Key Usage | Notes |
|---|---|---|---|
| `server` | digitalSignature, keyEncipherment | TLS Server Auth | Web servers, CUCM tomcat |
| `client` | digitalSignature | TLS Client Auth | Service accounts |
| `server_client` | digitalSignature, keyEncipherment | TLS Server + Client Auth | CUCM callmanager, Expressway, IM&P cup-xmpp |
| `custom` | Configurable | Configurable | Validity period, key usage, EKU fully editable |

> **Cisco UC note:** Most CUCM and IM&P services require the `server_client` profile.
> Using `server`-only for CallManager or cup-xmpp certificates is a common mistake that
> causes inter-cluster TLS failures and XMPP federation errors.

### 4.3 CSR Signing Workflow

```
┌─ CA Plugin → Sign CSR ──────────────────────────────────────────────────────┐
│                                                                              │
│  Upload CSR:  [Choose .pem file]  — or —  [Paste PEM]                      │
│                                                                              │
│  Parsed:  CN=cucm-pub.lab.local  Key: RSA 2048  SANs: 3 detected           │
│                                                                              │
│  Profile:  [server_client ▼]    Validity: [365] days                       │
│                                                                              │
│  SANs (editable):                                                            │
│    DNS: cucm-pub.lab.local                                                   │
│    DNS: cucm-sub1.lab.local                                                  │
│    IP:  10.0.1.10                                                            │
│    [+ Add SAN]                                                               │
│                                                                              │
│  [Sign Certificate]                                                          │
│                                                                              │
│  Download: [cert.pem]  [chain.pem]  [bundle.der]                            │
└──────────────────────────────────────────────────────────────────────────────┘
```

1. Paste or upload the CSR PEM from the product's Certificate Management page.
2. The parser shows the detected CN, key algorithm/size, and existing SANs.
3. Adjust SANs as needed (the UC Cert Orchestrator pre-populates these from the
   knowledge pack rules, removing the guesswork).
4. Choose a profile and click Sign. The signed cert and full chain download immediately.

### 4.4 SCEP, EST, and OCSP Endpoints

Once the CA plugin is enabled, the following enrollment URLs are shown on the
CA Overview page:

| Protocol | URL | Use |
|---|---|---|
| CRL | `http://<fqdn>/api/v1/plugins/ca/crl` | Certificate revocation list |
| OCSP | `http://<fqdn>/api/v1/plugins/ca/ocsp` | Online cert status (stapling) |
| SCEP | `http://<fqdn>/api/v1/plugins/ca/scep` | Auto-enrollment (Expressway, phones) |
| EST | `https://<fqdn>/.well-known/est` | RFC 7030 enrollment |

These endpoints are served only on the LAN. No internet connectivity is required or
used; SIPsmith is its own CRL distribution point and OCSP responder.

### 4.5 Certificate Expiry Dashboard

CA → Inventory shows all issued certificates with color-coded expiry bands:

- Red: expiring within 30 days
- Amber: expiring within 60 days
- Yellow: expiring within 90 days
- Green: healthy

The dashboard widget on the home page shows the count in each band. A **renewal
campaign** job can be triggered from the Inventory page to re-sign all certificates
for a cluster matching a filter (by subject or SAN pattern).

### 4.6 Re-issuing the GUI's Own HTTPS Certificate

After the CA is initialized, the GUI's bootstrap self-signed certificate should be
replaced with a CA-issued certificate:

1. CA → Overview → **Re-issue GUI cert**.
2. SIPsmith generates a new keypair and CSR internally, signs it with the Issuing CA,
   and replaces the files at the paths in `config.yaml` `tls_cert` / `tls_key`.
3. The control plane reloads. The browser will show a certificate warning on the next
   page load because the new cert chains to SIPsmith's CA, not a public root.
4. Add the SIPsmith CA chain (`ca-chain.pem`) to your browser's trusted CAs (or your
   OS trust store) to permanently clear the warning.

---

## 5. SFTP Plugin

### 5.1 Account Management

SFTP accounts are virtual system users in the `sipsmith-sftp` group with no login
shell. Each account has a chrooted home directory under
`/var/lib/sipsmith/sftp/<username>/`. The SFTP plugin writes its configuration as a
dedicated `Match Group sipsmith-sftp` block into `/etc/ssh/sshd_config.d/sipsmith.conf`
and never modifies the main `sshd_config` file, ensuring management access to the
appliance itself cannot be disrupted.

```
┌─ SFTP Plugin → Accounts ────────────────────────────────────────────────────┐
│  [+ Add Account]  [Apply Preset ▼]                                          │
│                                                                              │
│  ┌──────────────┬──────────────────────────┬───────┬───────────────────────┐│
│  │ Account      │ Chroot Path              │ Quota │                       ││
│  ├──────────────┼──────────────────────────┼───────┼───────────────────────┤│
│  │ cucm-drs     │ /sftp/cucm-drs           │ 20GB  │ [Browse] [Edit] [Del] ││
│  │ cucm-cdr     │ /sftp/cucm-cdr           │ 5GB   │ [Browse] [Edit] [Del] ││
│  │ firmware     │ /sftp/firmware           │ 50GB  │ [Browse] [Edit] [Del] ││
│  └──────────────┴──────────────────────────┴───────┴───────────────────────┘│
└──────────────────────────────────────────────────────────────────────────────┘
```

Authentication supports password, SSH public key, or both per account. To add a
public key, paste the `authorized_keys` format string in the account edit form.

### 5.2 Presets

| Preset | What It Creates | Purpose |
|---|---|---|
| DRS Backup Target | Account `cucm-drs`, directory `/sftp/cucm-drs` | CUCM DRF scheduled backups |
| CDR Billing Server | Account `cucm-cdr`, directory `/sftp/cucm-cdr` | CUCM CDR/CMR flat files |
| Firmware/MoH Staging | Account `firmware`, directory `/sftp/firmware` | Phone firmware, MoH audio |
| Log Drop | Account `logdrop`, directory `/sftp/logdrop` | General log collection |

### 5.3 CUCM-Side Configuration Values

After applying the **DRS Backup Target** preset, the GUI displays the exact values
to enter in CUCM. Example output:

```
DRS Backup Device Configuration (copy into CUCM)
─────────────────────────────────────────────────
Device Name:    sipsmith-drs
Host:           10.0.0.1
Path:           /cucm-drs
User:           cucm-drs
Password:       (shown once at account creation)
Number of Backups to keep: 3 (recommended)
```

For the **CDR Billing Server** preset, the output is:
```
Billing Application Server Configuration (CUCM OS Admin → CDR Management)
──────────────────────────────────────────────────────────────────────────
Host (Primary):   10.0.0.1
User:             cucm-cdr
Password:         (shown once)
Directory:        /cucm-cdr
Resend on Failure: Enabled (recommended)
```

### 5.4 File Browser and Retention Policy

SFTP → Accounts → (account row) → **Browse** opens an in-GUI file browser showing
file names, sizes, and modification dates. Files can be downloaded or deleted from
the GUI without requiring an SFTP client.

Set a retention policy per account under **Edit** → Retention: choose "Keep last N
sets" (for DRF backups) or "Delete files older than N days" (for CDR landing). The
retention job runs nightly and records its actions in the audit log.

---

## 6. UC Cert Orchestrator

### 6.1 Cluster Objects and Cert Packs

A **cluster object** captures everything the orchestrator needs to plan a cert campaign:
product type, version (editable — bump after an upgrade), node FQDNs/IPs, enterprise
domains, and deployment toggles (MRA, XMPP federation, multi-server SAN, secure trunks).

**Knowledge packs** (`plugins/sipsmith_uc_certs/packs/*.yaml`) declare per-product,
per-version rules: which services need certificates, how SANs are composed, which EKU
profile applies, and the trust-store and service-restart order. When Cisco changes
requirements in a new release, the pack is updated and uploaded as a ZIP — no code
changes required.

**Supported products and versions:**

| Product | Supported Version Floor | Primary Target |
|---|---|---|
| CUCM | 12.5 | 14.x, 15.x |
| CUC | 12.5 | 14.x, 15.x |
| IM&P | 12.5 | 14.x, 15.x |
| Expressway | X14 | X14, X15 |
| CMS | 3.x | 3.x, 4.x (same pack, version bump) |

### 6.2 DNS Pre-Flight Check

After generating a cert plan, **DNS Preflight** (required before signing) verifies:

- A record exists for every node FQDN that will appear in any SAN.
- PTR record exists for every node IP.
- For MRA deployments: `_collab-edge._tls.<domain>` and `_cisco-uds._tcp.<domain>` SRV
  records exist and point to a valid target.
- Every SRV target FQDN is also covered by a SAN in the cert plan.

The output is a **coherence report** listing each check as pass/fail. Red items block
signing until resolved. Missing records can be created directly from the report page
(the orchestrator calls the DNS plugin API on your behalf).

### 6.3 CSR Bulk Upload and Signing Workflow

1. UC Certs → (cluster) → **Generate Plan** — shows per-service cert items with
   computed SANs, profile, and which node to generate the CSR on.
2. Generate CSRs on each product node (OS Admin → Security → Certificate Management →
   Generate CSR). Use the options shown in the plan item.
3. UC Certs → (cluster) → **Upload CSRs** — drag-and-drop or upload individually.
   The orchestrator parses each CSR, validates key size, SANs, and EKU against the
   pack rules, and flags mismatches before signing.
4. **Sign All Ready CSRs** — the CA plugin signs each with the correct profile.
5. **Download Bundle ZIP** — contains `certs/*.pem`, `chain/ca-chain.pem`, and
   `CHECKLIST.txt` with the ordered upload and restart sequence.

> **Warning:** Always upload CA chain certificates to trust stores _before_ uploading
> identity certificates. Uploading in the wrong order causes service restart failures.
> Follow `CHECKLIST.txt` exactly — the restart order is product- and service-specific.

### 6.4 Post-Install TLS Probe Results

After uploading certificates to a cluster, UC Certs → (cluster) → **Run TLS Probes**
connects from the appliance to each service port and verifies:

- The presented certificate chains to SIPsmith's CA.
- The certificate SANs match the service FQDN.
- The full chain (including the issuing CA) is served.

| Service | Port | Protocol |
|---|---|---|
| CUCM Tomcat | 8443 | HTTPS |
| CallManager SIP TLS | 5061 | SIP/TLS |
| IM&P XMPP | 5222, 5269 | XMPP/TLS |
| CMS Web Bridge | 443 | HTTPS |
| Expressway TLS | 5061, 8443 | SIP/TLS, HTTPS |

Results appear on the cluster dashboard and are stored in the cert inventory for
historical comparison (useful for post-upgrade verification).

---

## 7. Records Landing

### 7.1 CDR Source Registration

Records → **Add Source** to register a CDR/DRF feed. Each source has a type, a
linked cluster, and transport-specific settings.

| Source Type | Transport | Configuration |
|---|---|---|
| `cucm_cdr` | SFTP (links to SFTP plugin account) | CUCM billing server IP, path, credentials |
| `cucm_drf` | SFTP (links to SFTP plugin account) | CUCM DRS backup schedule |
| `cms_cdr` | HTTP(S) receiver | Receiver URL shown; paste into CMS CDR settings |
| `expressway` | Syslog + REST pull | Syslog listener port; REST credentials per version |

### 7.2 CDR/DRF SFTP Landing

CUCM CDR and DRF sources use the SFTP plugin accounts created by the CDR Billing
Server and DRS Backup Target presets (see Section 5.2). After the source is registered
in Records, landed files are ingested automatically.

Click **Ingest** on a source row to parse newly landed files on demand. Parsed records
are stored in Postgres and immediately searchable.

### 7.3 Call Journey Correlation Engine

The correlation engine stitches multi-leg calls across product lines into a single
**call journey** using:

- CUCM `globalCallID` and leg-pair matching
- SIP `Call-ID` headers (where present in CDRs)
- CMS call and correlation IDs
- Expressway call serial numbers
- Time-window matching (configurable tolerance)

Records → Search → click a **Journey ID** to see the full call flow:

```
Call Journey: j-20260611-001842
──────────────────────────────────────────────────────────────────
  10:18:42  emu-001 (SIP Emulator)  → CUCM  →  DN 2001       ok
  10:18:43  CUCM                    → Expressway  [MRA leg]   ok
  10:18:44  Expressway              → CMS space   [conf]      ok
  10:19:14  Hangup  cause: 16 (Normal clearing)  on all legs
──────────────────────────────────────────────────────────────────
  Duration: 30s   Media: RTP (audio+video)   MOS: 4.2
```

### 7.4 Analytics and Q.850 Cause Breakdown

Records → Analytics provides:

- Call volume charts (hourly, daily) per cluster or source.
- Q.850 cause code breakdown — histogram of disconnect causes with their human-readable
  labels (e.g., cause 16 = Normal, cause 38 = Network out of order, cause 21 = Call rejected).
- Average call duration per source.

API access:
```
GET /api/v1/plugins/records/analytics/cause-codes   # Q.850 breakdown (JSON)
GET /api/v1/plugins/records/analytics/volume        # total records + avg duration
```

**Freshness monitors** alert when no records have landed from a source within a
configurable window (default: 2 hours for CDR, 25 hours for DRF). Stale sources
show an amber indicator on the Records dashboard card.

---

## 8. SIP Endpoint Emulator

### 8.1 pjsua2 Build Prerequisites and Build Script

The SIP Emulator plugin uses a custom pjsua2 build with VP8/H.264 video and SRTP
support. The distro package is not used.

**Prerequisites:**
```bash
sudo apt install build-essential libssl-dev libasound2-dev libv4l-dev \
     libvpx-dev libopencore-amrnb-dev libopencore-amrwb-dev pkg-config
```

**Build:**
```bash
# Online (downloads pjproject source)
sudo bash /opt/sipsmith/scripts/build-pjsua2.sh

# Air-gapped (place pjproject-*.tar.bz2 in installer/vendor/ first)
sudo bash /opt/sipsmith/scripts/build-pjsua2.sh
```

The script is idempotent — a version stamp at `/opt/sipsmith/venv/pjsua2-version`
prevents redundant rebuilds. Build time is approximately 4 minutes on a 4-core VM.
If the stamp is present but you need to force a rebuild, delete it before running
the script.

### 8.2 Endpoint Farm Configuration

```
┌─ SIP Emulator → Endpoints ──────────────────────────────────────────────────┐
│  [+ Add Endpoint]  [Register All]  [Unregister All]                         │
│  ┌──────────┬────────────────┬──────────┬────────┬──────────────────────┐   │
│  │ ID       │ SIP User       │ Domain   │ Status │                      │   │
│  ├──────────┼────────────────┼──────────┼────────┼──────────────────────┤   │
│  │ emu-001  │ 5001           │ cucm.lab │  [reg] │ [Dial] [Edit] [Del]  │   │
│  │ emu-002  │ 5002           │ cucm.lab │  [reg] │ [Dial] [Edit] [Del]  │   │
│  │ emu-003  │ 5003           │ cucm.lab │ [unreg]│ [Dial] [Edit] [Del]  │   │
│  └──────────┴────────────────┴──────────┴────────┴──────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────┘
```

Endpoints can be non-secure (UDP/TCP + RTP) or secure (TLS + SRTP). Secure endpoints
automatically request a client certificate from the CA plugin. Ports used:
- UDP/TCP **5080** — SIP signaling (must be reachable from CUCM)
- UDP **10000–20000** — RTP media (must not be blocked by a host firewall)

### 8.3 Registration and RTP Statistics

The live dashboard (SIP Emulator → Dashboard) shows per-endpoint statistics during
active calls:

- SIP signaling ladder diagram (real-time via SSE)
- MOS gauge (E-model estimate)
- RTP TX/RX packet sparklines
- Jitter, RTT, and packet loss figures per leg

### 8.4 Scenario YAML Reference

Scenarios are YAML files that define multi-step call tests. They can be run on
demand or scheduled.

```yaml
name: "Transfer test"
description: "emu-001 calls emu-002, transfers to emu-003, verify CDR cause 16"
steps:
  - action: call
    endpoint: emu-001
    to: "sip:5002@cucm.lab"
    wait_for: connected
    timeout: 15

  - action: wait
    duration: 10

  - action: hold
    endpoint: emu-001

  - action: call
    endpoint: emu-001
    to: "sip:5003@cucm.lab"
    wait_for: connected
    timeout: 15

  - action: transfer
    endpoint: emu-001

  - action: wait
    duration: 10

  - action: hangup
    endpoint: emu-002

  - action: assert_cdr
    endpoint: emu-001
    cause_code: 16
    wait_timeout: 30
```

**Available actions:**

| Action | Required Parameters | Description |
|---|---|---|
| `call` | `endpoint`, `to` | Place a call; `wait_for: connected` blocks until answered |
| `answer` | `endpoint` | Answer an incoming call |
| `wait` | `duration` (seconds) | Pause scenario execution |
| `hold` | `endpoint` | Place active call on hold |
| `resume` | `endpoint` | Resume held call |
| `transfer` | `endpoint`, `to` | Blind transfer to `to` URI |
| `hangup` | `endpoint` | Terminate active call |
| `dtmf` | `endpoint`, `digits` | Send DTMF digits |
| `mute_audio` | `endpoint` | Mute audio stream |
| `mute_video` | `endpoint` | Mute video stream |
| `assert_cdr` | `endpoint`, `cause_code` | Wait for a CDR confirming the expected Q.850 cause |

### 8.5 Live Dashboard Usage

SIP Emulator → Dashboard:
1. Click an endpoint in the **Fleet** panel (left) to make it active.
2. Type a destination URI and click **Dial**, or use **Answer** when an incoming
   call arrives.
3. The **Ladder** panel (center) shows the SIP signaling diagram in real time.
4. The **Stats** panel (right) shows live RTP metrics.
5. Use the call controls: Mute Audio, Mute Video, Hold, Transfer, DTMF pad, Hang Up.

---

## 9. Active Directory

### 9.1 Domain Provisioning Wizard

> **Prerequisite:** The DNS plugin must be enabled and running before provisioning AD,
> as BIND9_DLZ integration starts immediately after provisioning completes.

AD → **Provision Domain Wizard**:

| Field | Example | Notes |
|---|---|---|
| Realm | `LAB.LOCAL` | Must be a valid FQDN with at least two labels |
| NetBIOS Name | `LAB` | 15 characters maximum |
| Administrator Password | (set at provision) | AD Administrator account |
| DSRM Password | (set at provision) | Store this safely — needed for DC recovery |

Provisioning takes approximately 2 minutes. Samba AD DC starts automatically on
completion, and BIND9 is reloaded with the DLZ module wired in.

### 9.2 BIND9_DLZ Integration with DNS Plugin

After provisioning, Samba writes its DLZ database declaration to
`/var/lib/samba/private/named.conf`. The AD plugin wraps this in
`/etc/bind/named.conf.ad-dlz` and adds an include to `named.conf.local`. BIND
reloads automatically. The AD DNS zone (e.g., `lab.local`) appears as a DLZ-managed
entry in DNS → Zones with status "Managed by AD plugin."

### 9.3 LDAPS Certificate Issuance

AD → Overview → **Issue LDAPS Certificate from CA** (CA plugin must be initialized):

1. The CA plugin issues a server certificate for the AD realm FQDN.
2. The certificate is installed into Samba's TLS directory
   (`/var/lib/samba/private/tls/`).
3. Samba is reloaded; LDAPS on port 636 becomes available.

For CUCM secure LDAP sync, upload the SIPsmith CA chain to CUCM:
- CUCM OS Admin → Security → Certificate Management → Upload Certificate
- Upload `ca-chain.pem` to both `tomcat-trust` and `directory-trust`
- Restart Tomcat (CUCM Serviceability → Tools → Control Center)

### 9.4 Bulk User Generation

**Pattern mode** (AD → Users → Bulk Create → Pattern):

| Field | Example |
|---|---|
| Prefix | `user` |
| Count | `100` |
| Phone start | `+1-555-1000` |
| OU | `OU=TestUsers,DC=lab,DC=local` |

Creates `user001` through `user100` with `telephoneNumber` and `ipPhone` attributes
incremented sequentially. Suitable for CUCM LDAP sync testing with a realistic
directory population.

**CSV mode** — upload a file with columns:
`sam_account, first_name, last_name, password, telephone_number, ip_phone, ou_dn`

### 9.5 CUCM LDAP Sync Configuration Values

AD → **CUCM Sync Helper** shows the exact values for CUCM LDAP Directory configuration:

```
CUCM LDAP Directory Configuration
──────────────────────────────────────────────────────────────
LDAP Server IP:       10.0.0.1
LDAP Port (clear):    389
LDAP Port (TLS):      636
LDAP User DN:         CN=cucm-sync,CN=Users,DC=lab,DC=local
Password:             (from sync account creation)
User Search Base:     DC=lab,DC=local
User ID Attribute:    sAMAccountName
Phone Attribute:      telephoneNumber
LDAP Authentication:  Simple
Sync Enabled:         Yes
──────────────────────────────────────────────────────────────
```

Click **Create Sync Service Account** to create a dedicated read-only account with
password non-expiry set. The page refreshes with the account's DN populated.

---

## 10. CTI Control

### 10.1 Java Sidecar Build

The CTI plugin communicates with CUCM via JTAPI. Because JTAPI is Java-only, a small
Java sidecar process bridges JTAPI to the Python control plane over a Unix socket
using a JSON-line protocol.

```bash
# Online build (downloads Maven dependencies)
sudo bash /opt/sipsmith/scripts/build-cti-sidecar.sh

# Offline build (pre-populate ~/.m2 from an online machine, then sync to appliance)
sudo bash /opt/sipsmith/scripts/build-cti-sidecar.sh --offline
```

The sidecar JAR is installed to `/opt/sipsmith/lib/sipsmith-cti-sidecar.jar`.

**Prerequisite:** `sudo apt install default-jre-headless`

### 10.2 JTAPI Jar Acquisition from CUCM

The JTAPI client library must version-match the target CUCM. SIPsmith stores a
separate jar per cluster and version.

Automatic acquisition (recommended):

1. CTI Control → (cluster) → **Fetch JTAPI Jar**.
2. SIPsmith downloads the jar from `http://<cucm>:8080/plugins/jtapi.jar` and stores
   it at `/var/lib/sipsmith/cti/<cluster_id>/jtapi-<version>.jar`.

Manual acquisition (if the cluster is not yet reachable from the appliance):

```
CUCM Admin → Application → Plugins → Cisco JTAPI Windows → Download
Extract jtapi.jar from the installer and place at the path shown in the cluster config.
```

> **Note:** After upgrading CUCM, bump the cluster's version field in CTI Control
> and click **Fetch JTAPI Jar** again. A JTAPI mismatch between the client jar and
> the CUCM version is a common post-upgrade failure mode. SIPsmith makes this a
> managed, visible step rather than a silent failure.

### 10.3 AXL App User Setup

CTI Control → (cluster) → **Run AXL Setup** creates or updates a `sipsmith-cti`
application user in CUCM with the following roles:

- Standard CTI Enabled
- Standard CTI Allow Control of All Devices

If you prefer to configure CUCM manually:
1. CUCM Admin → User Management → Application User → Add.
2. Name: `sipsmith-cti`. Assign both Standard CTI roles.
3. Associate the devices you want SIPsmith to control under "Controlled Devices."

CTI port 2748 must be open from the appliance to the CUCM publisher (bidirectional).

### 10.4 Connecting Clusters and Observing Devices

```
┌─ CTI Control → Live Dashboard ──────────────────────────────────────────────┐
│  Cluster: cucm-prod  [connected]  Devices: 12  Active Calls: 2              │
│                                                                              │
│  ┌──────────┬──────────────┬────────────────────────────────────────────┐   │
│  │ Device   │ DN           │ Status                                     │   │
│  ├──────────┼──────────────┼────────────────────────────────────────────┤   │
│  │ SEP001   │ 2001         │  [registered]  [active call to 2002]       │   │
│  │ SEP002   │ 2002         │  [registered]  [active call from 2001]     │   │
│  │ SEP003   │ 2003         │  [registered]  [idle]  [Dial from here]    │   │
│  └──────────┴──────────────┴────────────────────────────────────────────┘   │
│                                                                              │
│  Click-to-Dial:  From [SEP003 ▼]  To [    2001    ]  [Dial]                │
└──────────────────────────────────────────────────────────────────────────────┘
```

Device state events are streamed via SSE — the grid updates without polling.
Click **Dial from this device** on an idle device to initiate a call via JTAPI.
Active call controls: Hold, Resume, Transfer, Hangup, DTMF.

---

## 11. xAPI Device Control

### 11.1 Adding and Polling RoomOS Devices

xAPI → **+ Add Device**. Fill in: Display Name, IP address, Username, Password,
Transport (HTTPS recommended). Enable **Periodic Polling** to keep registration
state and system info current.

> **Note on TLS:** RoomOS devices use self-signed certificates by default. SIPsmith
> connects with certificate verification disabled for lab use. For a stricter
> configuration, upload a custom certificate to the RoomOS device signed by SIPsmith's CA.

**Prerequisites on the device:** xAPI must be enabled.
Device UI → Settings → Configuration → NetworkServices → HTTPS → Enable.

### 11.2 Call and Device Controls

xAPI → (device) → **Control**:

- **Dial:** enter a DN or SIP URI and click Dial.
- **Hangup / Hold / Resume / DTMF / Volume:** standard call controls.
- **Standby:** put the device into standby mode.

### 11.3 Macro and Configuration Management

**Macros:** enter a macro name and click **Activate** or **Deactivate**. The macro
must already exist in the device's macro runtime (deploy it from the device's
local web interface or via `xapi.macros.js`).

**Configuration:** read or write any xAPI configuration path. Examples:

```
Read:   Audio.DefaultVolume
Write:  Audio.DefaultVolume = 60

Read:   Conference.AutoAnswer.Mode
Write:  Conference.AutoAnswer.Mode = On
```

**Recent Commands** on the device page shows the last 10 xAPI operations with
timestamps, request, response, and HTTP status.

---

## 12. User Management and Roles

### 12.1 Admin, Operator, and Read-Only Roles

| Role | Capabilities |
|---|---|
| `admin` | Full access — all plugins, system settings, user management, backup/restore |
| `operator` | Read + write to all plugins; cannot manage users or perform backup/restore |
| `read-only` | View all pages and dashboards; no mutations permitted |

Manage users at System → Users. Only `admin` role users can create or modify accounts.

### 12.2 TOTP Setup

Users can enable TOTP (RFC 6238 time-based OTP) from their profile page. A QR code
is shown for enrollment with any standard authenticator app. Once enabled, TOTP is
required on every login. Admins can reset a user's TOTP from System → Users if a
device is lost.

### 12.3 Personal Access Tokens for API

Profile → **Personal Access Tokens** → Create. Set a name and optional expiry.
The token is shown once at creation; store it securely.

Use the token in API calls:
```bash
curl -H "Authorization: Bearer <token>" https://<fqdn>:8443/api/v1/system/health
```

The full OpenAPI specification is served locally at `https://<fqdn>:8443/api/docs`.

---

## 13. Backup and Restore

### 13.1 What Is Backed Up

System → Backup → **Create Backup** produces a single encrypted tarball containing:

| Component | Method |
|---|---|
| PostgreSQL database | `pg_dump` — all tables, plugin config, audit log, users |
| CA key store | `/var/lib/sipsmith/ca/` (encrypted at rest) |
| DNS zone files | Plugin `backup()` contribution |
| Samba AD state | `samba-tool domain backup online` |
| SFTP account metadata | Plugin `backup()` contribution (user list, keys, quotas) |
| Appliance config | `/etc/sipsmith/config.yaml` |

SFTP share contents (DRF backups, firmware files, CDR records) are **not** included
by default due to size. Enable "Include SFTP data" at the cost of a larger archive.

Backups can be downloaded directly or auto-deposited to an SFTP account (including
one on the appliance itself, using a dedicated account).

### 13.2 Restore Procedure

1. Install SIPsmith on a clean Ubuntu 24.04 host using the standard installer.
2. Complete the initial setup wizard (creates the admin account and runs migrations).
3. System → Backup → **Restore from File** — upload the backup tarball.
4. SIPsmith stops services, restores the Postgres dump, extracts plugin data, and
   restarts. The appliance is returned to the state at backup time.

> **Warning:** Restore will overwrite all current data on the target appliance.
> Use only on a fresh install or when data loss on the target is acceptable.

---

## 14. Security Hardening

### 14.1 UFW Firewall Rules

SIPsmith manages its own UFW rules. Each plugin's `plugin.yaml` declares the ports
it requires; the `sipsmith-agent` root helper applies `ufw allow` and `ufw delete`
rules when plugins are enabled and disabled. No port is opened without a plugin
explicitly declaring it.

Default ports open after a standard install with all plugins enabled:

| Port | Protocol | Plugin |
|---|---|---|
| 8443 | TCP | Core (GUI/API) |
| 53 | UDP, TCP | DNS |
| 389 | TCP | AD (LDAP) |
| 636 | TCP | AD (LDAPS) |
| 88 | UDP, TCP | AD (Kerberos) |
| 22 | TCP | SFTP (shared with SSH management) |
| 123 | UDP | chrony (NTP) |
| 5080 | UDP, TCP | SIP Emulator (SIP signaling) |
| 10000–20000 | UDP | SIP Emulator (RTP media) |
| 2748 | TCP | CTI (JTAPI — outbound only, appliance to CUCM) |

Management SSH (the system's own sshd on port 22) is always permitted and is
never restricted by SIPsmith's firewall management.

### 14.2 sipsmith-agent Security Model

The web process runs as the unprivileged `sipsmith` user. All operations requiring
root privileges (writing daemon config files, calling systemctl, modifying UFW rules)
are delegated to `sipsmith-agent` via a Unix domain socket at
`/run/sipsmith-agent/agent.sock`.

The agent enforces a strict **allowlisted verb vocabulary**. Only pre-approved
commands can be executed; the agent refuses any request that does not match the
allowlist exactly. Peer credentials are checked — only the `sipsmith` user may
connect to the socket. This means a compromised web process cannot escalate to root
beyond the defined verb set.

### 14.3 TLS Configuration

- The GUI runs TLS on port 8443 at all times. There is no plain-HTTP fallback.
- After initializing the CA plugin, re-issue the GUI certificate from the CA
  (see Section 4.6) to replace the bootstrap self-signed cert.
- The `cryptography` library is used for all TLS certificate operations. No
  external CA or certificate service is contacted.
- CA private keys are encrypted at rest with the appliance master key. The master
  key is derived at startup from the `security.secret_key` value in `config.yaml`.

### 14.4 Credential Security

- All secrets live in `/etc/sipsmith/config.yaml` (mode 0600, root:sipsmith) or
  `/var/lib/sipsmith/` with equivalently tight modes.
- No credentials appear in source code or in the repo.
- `config.yaml` is gitignored; only `config.example.yaml` is committed.
- Database passwords and API credentials stored by plugins (SFTP account passwords,
  xAPI device credentials, CTI AXL credentials) are stored in PostgreSQL, never in
  flat files accessible to the web process directly.
- Every configuration mutation — including credential changes — writes an audit row
  recording the actor, timestamp, old value, and new value. The audit log is
  append-only from the web process perspective; only `sipsmith-agent` can rotate it.

> **Air-gap reminder:** SIPsmith is designed for and assumes an air-gapped environment.
> The application makes no outbound connections at runtime. All GUI assets, fonts, and
> JavaScript are vendored locally. There are no telemetry calls, update checks, or
> external OCSP/CRL queries. A CI lint rule (`scripts/airgap-lint.sh`) fails the build
> if any template references an external URL — run it with `./scripts/check.sh` before
> every commit.
