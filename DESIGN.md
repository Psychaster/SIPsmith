# SIPsmith — Design Document

**Version:** 0.3 (Draft — adds version-aware packs, Records Landing platform, and the Test & Verification layer)
**Target platform:** Ubuntu Server 24.04 LTS (latest LTS)
**Purpose:** A single-box "lab services appliance" providing the supporting infrastructure a Cisco UC / general lab environment needs — DNS, an internal Certificate Authority, Active Directory (Samba AD DC), and an SFTP server — managed through one web GUI, with a plugin architecture for adding future features.

---

## 1. Product Concept

SIPsmith is the anvil your lab is forged on. CUCM, CMS, Expressway, and endpoints all depend on services that are annoying to stand up individually:

| Plugin | What the lab uses it for |
|---|---|
| **DNS** | A/PTR records for UC nodes, SRV records (`_cisco-uds`, `_collab-edge`, `_sip._tcp`), forwarders |
| **Certificate Authority** | Sign CUCM/CMS/Expressway CSRs, issue server/client certs, download CA chain for trust stores |
| **Active Directory** | LDAP sync source for CUCM end users, Kerberos/auth testing, DNS-integrated domain |
| **SFTP** | DRS backup target, MoH/firmware/TFTP file staging, log collection drop point |

Everything installs with one command, runs air-gapped, and is configured from a dark-themed web GUI.

---

## 2. High-Level Architecture

```
┌──────────────────────────────────────────────────────────┐
│                     SIPsmith Appliance                    │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │   Control Plane (FastAPI, port 8443 HTTPS)         │  │
│  │   - Web GUI (Jinja2 + HTMX, dark theme)            │  │
│  │   - REST API (/api/v1/...)                         │  │
│  │   - Plugin Manager / Registry                      │  │
│  │   - Job Runner (async tasks: installs, reloads)    │  │
│  └───────────────┬────────────────────────────────────┘  │
│                  │ Plugin SDK (Python interface)          │
│   ┌──────────┬───┴──────┬───────────┬───────────┐        │
│   │ DNS      │ CA       │ AD        │ SFTP      │  ...   │
│   │ plugin   │ plugin   │ plugin    │ plugin    │        │
│   └────┬─────┴────┬─────┴─────┬─────┴─────┬─────┘        │
│        │          │           │           │              │
│   BIND9      Python CA    Samba AD DC  OpenSSH           │
│  (systemd)  (in-process)   (systemd)   internal-sftp     │
│                                                          │
│  PostgreSQL (state, users, audit log, plugin config)     │
└──────────────────────────────────────────────────────────┘
```

**Key principle:** the control plane never *is* the service — it *manages* battle-tested daemons (BIND9, Samba, OpenSSH) via templated config files + systemd, except where a pure-Python implementation is genuinely better (the CA).

### Stack

| Layer | Choice | Rationale |
|---|---|---|
| Control plane | Python 3.12 + FastAPI + Uvicorn | Familiar, async, easy plugin routers |
| Database | PostgreSQL 16 | Config state, audit, plugin tables |
| Web UI | Jinja2 + HTMX + vanilla JS, dark theme | Zero node/npm build step → trivially air-gappable |
| Service mgmt | systemd via D-Bus (`pystemd`) + config templating | Standard, observable, survives reboots |
| Task queue | FastAPI background tasks + a small Postgres-backed job table | No Redis dependency for v1; keep footprint small |
| Packaging | Offline tarball installer + optional `.deb` | One-command install, air-gap first |

---

## 3. Installation Design

### 3.1 One-command install

```bash
sudo bash install-sipsmith.sh            # online
sudo bash install-sipsmith.sh --offline  # uses bundled apt debs + wheels
```

The installer:
1. Verifies Ubuntu 24.04, hostname/FQDN, static IP (warns if DHCP).
2. Installs system packages (online: apt; offline: bundled `.deb` cache via `dpkg -i` from `./debs/`).
3. Creates `sipsmith` system user, directory tree under `/opt/sipsmith` and `/var/lib/sipsmith`.
4. Creates a Python venv at `/opt/sipsmith/venv` from vendored wheels (`./wheels/`).
5. Initializes PostgreSQL (dedicated cluster or local db `sipsmith`).
6. Generates a bootstrap self-signed cert for the GUI (later re-issued by the CA plugin — the appliance trusts itself).
7. Installs `sipsmith.service` (systemd) and starts it.
8. Prints the GUI URL + one-time admin setup token.

### 3.2 Offline bundle layout

```
sipsmith-1.0.0-offline.tar.gz
├── install-sipsmith.sh
├── debs/            # bind9, samba, postgresql-16, openssh-server, deps
├── wheels/          # fastapi, uvicorn, jinja2, sqlalchemy, cryptography, pystemd...
├── app/             # control plane source
└── plugins/         # core plugin packages (dns, ca, ad, sftp)
```

### 3.3 Directory layout (FHS-friendly)

```
/opt/sipsmith/              # code, venv, plugins (read-mostly)
/etc/sipsmith/config.yaml   # appliance-level config (DB creds, ports)
/var/lib/sipsmith/          # CA keys, plugin state, sftp roots, backups
/var/log/sipsmith/          # app + audit logs (also journald)
```

---

## 4. Plugin Architecture

### 4.1 Plugin anatomy

A plugin is a directory (or uploaded `.zip` for air-gap installs via the GUI):

```
plugins/dns/
├── plugin.yaml        # manifest
├── __init__.py        # entry point exposing Plugin class
├── api.py             # FastAPI APIRouter
├── ui/                # Jinja2 templates + static assets
├── templates/         # service config templates (named.conf.j2, zone.j2)
└── migrations/        # SQL migrations for plugin-owned tables
```

**`plugin.yaml` manifest:**

```yaml
id: dns
name: DNS Server
version: 1.0.0
api_version: 1               # plugin SDK compatibility
requires_packages: [bind9, bind9-utils]
requires_plugins: []          # e.g. AD plugin requires dns
ports: [{port: 53, proto: udp}, {port: 53, proto: tcp}]
entry_point: sipsmith_dns:DnsPlugin
```

### 4.2 Plugin SDK interface

```python
class SipsmithPlugin(ABC):
    meta: PluginMeta                          # parsed from plugin.yaml

    # Lifecycle
    async def install(self, ctx) -> None      # apt pkgs, dirs, initial config
    async def enable(self, ctx) -> None
    async def disable(self, ctx) -> None
    async def status(self, ctx) -> ServiceStatus   # health: systemd + functional probe
    async def apply_config(self, ctx) -> None # render templates, validate, reload

    # Integration
    def api_router(self) -> APIRouter          # mounted at /api/v1/plugins/{id}
    def ui_pages(self) -> list[UiPage]         # nav entries + template routes
    def backup(self, ctx) -> Path              # contribute to appliance backup
    def restore(self, ctx, path) -> None
```

The `ctx` object gives plugins controlled access to: the DB (namespaced schema), systemd, the template renderer, the audit logger, the firewall (ufw rules from the manifest's `ports`), and **other plugins' public APIs** (e.g., SFTP plugin requesting a cert from the CA plugin).

### 4.3 Why this matters

- New features (TFTP, NTP, syslog collector, RADIUS, a SIP test endpoint…) are drop-in zips — no core changes.
- Dependency declaration (`requires_plugins`) handles ordering (AD needs DNS).
- Versioned `api_version` lets the core evolve without breaking old plugins.

---

## 5. Core Plugin Designs

### 5.1 DNS (BIND9)

- **Engine:** BIND9, config fully owned by SIPsmith via Jinja2 templates → `named-checkconf`/`named-checkzone` validation → `rndc reload`.
- **GUI features:** zone management (forward + reverse), record CRUD with first-class **SRV record support** (presets for `_cisco-uds._tcp`, `_cuplogin._tcp`, `_collab-edge._tls`, `_sip._tcp/_sips._tcp`), conditional forwarders, recursion ACLs.
- **Bulk import:** paste/CSV import of A records (handy when standing up a cluster).
- **Critical design note — AD coexistence (decided, D9):** Samba AD DC requires authoritative control of its domain's DNS, so SIPsmith runs **BIND9_DLZ mode**: one BIND instance owns port 53, with Samba's DLZ module loaded to serve the AD zone while all other zones are normal BIND zones. Delegation mode (Samba internal DNS on a separate listener with BIND delegating the AD subdomain) is retained in documentation as a fallback only.

### 5.2 Certificate Authority (pure Python, `cryptography` lib)

- **Engine:** in-process — no step-ca/EJBCA dependency. Root key (RSA 4096 or EC P-384) generated at plugin enable, stored encrypted at rest in `/var/lib/sipsmith/ca/` (mode 0600, key encrypted with appliance master key).
- **Hierarchy:** offline-style Root CA + an Issuing CA (so the root can be kept pristine; CUCM trust stores get both).
- **Core workflows (built around the CUCM cert dance):**
  - **Sign an uploaded CSR** (the #1 use case): paste or upload CSR → choose profile → adjust SANs → download signed cert + chain in PEM/DER.
  - **Certificate profiles:** Server (TLS server auth), Client, Server+Client (CUCM tomcat/callmanager want both EKUs in many deployments), custom validity/key usage.
  - **Issue full keypairs** (key + cert + chain bundle) for devices that can't generate CSRs.
  - **Download CA chain** as PEM/DER one-click (for tomcat-trust/callmanager-trust uploads).
  - **CRL publishing** at an HTTP endpoint + manual revocation, plus an **OCSP responder** — both from v1 (SIPsmith is the revocation authority for the lab; nothing checks externally).
  - **Enrollment protocols from v1:** **SCEP** and **EST** endpoints for products/endpoints that can auto-enroll (Expressway, phones via CAPF-adjacent workflows, future devices) — full CA feature set ships at the start rather than phased.
- **Inventory:** every issued cert tracked in Postgres with expiry; dashboard widget shows certs expiring in <30/60/90 days.
- **Dogfooding:** the SIPsmith GUI re-issues its own HTTPS cert from this CA once enabled.

### 5.3 Active Directory (Samba AD DC)

- **Engine:** Samba 4 provisioned as a domain controller (`samba-tool domain provision`), managed via `samba-tool` subprocess calls wrapped in the plugin API.
- **GUI features:**
  - Domain provisioning wizard (realm, NetBIOS name, DSRM password, DNS mode per §5.1).
  - **User management with bulk create** — generate N test users from a CSV or a pattern (`user001`–`user250`, with phone numbers in `telephoneNumber`/`ipPhone` attributes) — purpose-built for CUCM LDAP sync testing.
  - Group and OU management.
  - **LDAP sync helper page:** shows exactly the values CUCM's LDAP Directory config needs (LDAP server, port 389/636, search base, sync agreement DN), plus a "create CUCM sync service account" button.
  - **LDAP on 389 and LDAPS on 636 both enabled by default** (Samba AD DC serves both natively). The LDAPS certificate is auto-issued from the CA plugin and rotated with it; the sync helper page shows connection values for both, with a callout that secure sync (636) requires SIPsmith's CA chain uploaded to CUCM's directory-trust first.
- **Constraints surfaced in UI:** Samba AD DC owns Kerberos (88), LDAP (389/636), and its DNS zone; the plugin manifest declares these so the core can detect conflicts before enable.

### 5.4 SFTP Server (OpenSSH internal-sftp)

- **Engine:** OpenSSH with a dedicated `Match Group sipsmith-sftp` block → `ForceCommand internal-sftp`, `ChrootDirectory /var/lib/sipsmith/sftp/%u`. SIPsmith drops a config snippet into `/etc/ssh/sshd_config.d/` — it never rewrites the main sshd_config (so it can't lock you out of the box).
- **GUI features:**
  - Virtual SFTP accounts (system users in the `sipsmith-sftp` group, no shell) with password and/or key auth.
  - Per-account chrooted directories with quota display.
  - **Presets:** "CUCM DRS backup target" (creates account + directory and shows the exact values to enter in DRS Backup Device config), "Firmware/MoH staging," "Log drop."
  - In-GUI file browser per share (download/delete, disk usage), retention policy (e.g., keep last N DRS backups).

### 5.5 UC Certificate Orchestration (`uc-certs` plugin family)

The marquee feature: turn the multi-hour, error-prone collaboration certificate dance into a wizard that drives the **CA** and **DNS** plugins together, with product-specific knowledge for **CUCM, CUC, IM&P, Expressway, and CMS**.

#### Architecture: one orchestrator + knowledge packs

```
plugins/uc-certs/              # orchestrator framework (workflow engine, wizards)
plugins/uc-certs/packs/
├── cucm.yaml                  # services, SAN rules, EKU rules, restart order
├── cuc.yaml
├── imp.yaml
├── expressway.yaml
└── cms.yaml
```

Product requirements are **data, not code** — YAML knowledge packs declaring each product's certificate services, SAN composition rules, EKU requirements, trust-store mapping, upload method, and service-restart order. Packs are **version-aware**: every rule is keyed to a version range, and each cluster object carries its product version, so the orchestrator applies the right rules and transports for *that* release. **Initial version floors (decided):** CUCM/CUC/IM&P **12.5** as the supported floor with 14/15 as primary targets; Expressway **X14+**; CMS **3.x with 4.x rules shipping in the same pack** so a 3.x→4.x upgrade is just a version-field bump. Because this is an **upgrade-testing lab**, the cluster's version field is editable — bump it after an upgrade and the orchestrator re-evaluates the cert plan against the new release's rules and flags anything that changed. When Cisco changes requirements in a new release, you drop in an updated pack zip (air-gap friendly) without touching the orchestrator.

#### The workflow (per cluster)

1. **Cluster definition** — product + version, node FQDNs/IPs, enterprise domain(s), deployment toggles (MRA? multi-server SAN certs? secure SIP trunks? XMPP federation?). Saved as a reusable cluster object.
2. **DNS pre-flight** — the orchestrator calls the DNS plugin to create/verify everything the cert plan depends on: A/PTR for every node, and for MRA the `_collab-edge._tls.<domain>` and `_cisco-uds._tcp.<domain>` SRV records. Output: a **coherence report** — every FQDN that will appear in a SAN must resolve, and every SRV target must be covered by a SAN. Mismatches block signing until acknowledged.
3. **CSR intake — two tiers:**
   - **Guided (default, best practice):** private keys never leave the product. The wizard tells you exactly which CSRs to generate on each node and with which options (e.g., "enable multi-server SAN for tomcat on the publisher," "include both domains on Expressway-E"). You bulk-upload the CSR files; the orchestrator **parses and validates each CSR against the expected plan** — wrong key size, missing chat-node alias, absent MRA domain → flagged *before* anything is signed.
   - **Automated (where the product allows):** CMS via SSH/MMP (`pki csr`) + its own SFTP; Expressway and newer CUCM/CUC/IM&P releases via their management APIs where available. The pack declares which transport it supports; the orchestrator falls back to guided mode otherwise.
4. **Signing with product-correct profiles** — examples of the rules the packs encode:

   | Product / service | SAN composition | EKU |
   |---|---|---|
   | CUCM tomcat / CallManager (multi-SAN) | All cluster node FQDNs | Server **+ Client** (inter-cluster / secure trunk TLS) |
   | IM&P cup-xmpp / cup-xmpp-s2s | Node FQDNs + IM domain + chat node aliases | Server + Client |
   | Expressway-C | Cluster FQDN, peer FQDNs, IM&P chat node aliases, secure phone profile names (if used) | Server + Client (traversal zone is mutual TLS) |
   | Expressway-E | Cluster/peer FQDNs, **every MRA / collab-edge domain**, XMPP federation domains | Server + Client |
   | CMS callbridge / webbridge / webadmin | Service FQDNs + join/Web Bridge URLs | Per-service (database certs have their own strict CN rules) |
   | CUC tomcat | Node FQDNs (multi-SAN) | Server (+ Client if intercluster) |

5. **Delivery** — per-node bundle zip: signed certs + issuing/root chain in the format each product wants, plus an **ordered checklist** (trust certs before identity certs, exactly which services restart, in what sequence, cluster-wide notes). In automated tier, the orchestrator pushes and assigns directly (e.g., SFTP files to CMS, then MMP `callbridge certs` / `webbridge3` assignment and service restart).
6. **Post-install validation** — TLS probes from the appliance against each service port (tomcat 8443, SIP TLS 5061, XMPP 5222/5269, Web Bridge 443, etc.): does the presented cert chain to SIPsmith's CA, do SANs match DNS, is the full chain served? Results land on the dashboard, and every issued cert is already in the CA expiry tracker — so the same plugin later runs **renewal campaigns** ("everything on cluster X expires in 45 days → regenerate plan").

#### Why this is the right shape

- The orchestrator is a *consumer* of the CA and DNS plugin APIs — it proves the plugin-to-plugin contract in §4.2.
- Guided tier works on day one for every product and version, fully air-gapped, with zero product-side API dependencies; automated tier is progressive enhancement per pack.
- Cert/DNS coherence checking is the thing humans always get wrong (SAN issued, SRV forgotten — MRA dead). Making DNS a first-class step in the cert workflow, not a separate chore, is the differentiator.

### 5.6 Records Landing Platform (`records` plugin — DRF + CDR)

Builds on the SFTP plugin to make SIPsmith the structured landing zone for backups and call records across the lab. Key design point: **each product ships records over a different native transport**, so the landing platform speaks all of them rather than pretending everything is SFTP:

| Source | What | Transport SIPsmith provides |
|---|---|---|
| CUCM DRS/DRF | Scheduled cluster backups | SFTP account + chrooted repo (existing preset, now managed) |
| CUCM CDR Repository | CDR/CMR flat files to "billing servers" | SFTP (or FTP if you must) receiver |
| CMS | CDRs | **HTTP(S) receiver endpoint** — CMS POSTs XML CDRs to a configured receiver URI; SIPsmith exposes one per cluster |
| Expressway | Call/event history | **Syslog receiver** and/or scheduled REST pulls from the Expressway API (pack-declared per version) |

On top of the raw landing directories:

- **Repository policies per source:** retention (keep last N DRF sets / M days of CDRs), disk quota with dashboard alerts, integrity checks (DRF set completeness — all expected tar components present).
- **CDR ingestion & analytics (full depth in v1):** landed CDR/CMR files are parsed into Postgres — searchable call records, per-cluster volume charts, and **Q.850 cause-code breakdowns** (same analysis as your NOC dashboard, now built in). CMS XML and CUCM flat-file schemas handled per knowledge pack.
- **Cross-product call association ("call journey"):** a correlation engine stitches the legs of one call across product lines — CUCM `globalCallID`/leg pairs, SIP Call-ID where present, CMS call/correlation IDs, and Expressway call serial numbers, matched within time windows. The GUI shows a single call as its journey (e.g., emulator endpoint → CUCM → Expressway → CMS space), with per-leg cause codes and media stats where available. This is also what makes harness verification rich: one test call, one stitched record proving every hop.
- **"Did records arrive?" probes:** each configured source gets a freshness monitor (e.g., "no CDR files from CUCM-PUB in 2h" → amber). This becomes a verification signal for the upgrade harness (§5.7).
- **DRF awareness:** the DRS preset graduates into tracked backup sets — the dashboard shows last successful backup per cluster, sized and timestamped, which is exactly what you check before pulling the trigger on an upgrade.

### 5.7 Test & Verification Layer (the upgrade-testing toolkit)

Three control plugins plus a harness that orchestrates them. Together they answer the only question that matters after an upgrade: *does everything still work?*

#### 5.7.1 CTI Control plugin (`cti`)

- **Engine:** CUCM CTI means **JTAPI**, and JTAPI is Java-only — so this plugin uses a small **Java sidecar** process speaking JSON-RPC over a local socket to the Python control plane (the same sidecar pattern as Sippy Cup, different language). The sidecar wraps JTAPI provider/terminal/call observers into simple verbs.
- **Version handling (and why it belongs in this lab):** the JTAPI client library must match the CUCM version, and CUCM serves its own version-matched jar from the cluster's plugins page — so even air-gapped, the plugin can **fetch the correct JTAPI jar from the cluster itself**, store it per cluster+version, and prompt to refresh it when you bump a cluster's version after an upgrade. JTAPI mismatch is a classic post-upgrade gotcha; SIPsmith makes it a managed step.
- **Capabilities:** associate an application user with lab devices, then per device/line: make call, answer, hold/resume, transfer, conference, send DTMF, monitor device/line state events. GUI shows a live device grid (registered state, active calls) and a manual "click-to-dial between any two lab phones" panel.
- **Setup wizard:** creates/validates the CUCM application user, device association, and CTI-enable flags (via AXL), and verifies CTI connectivity (port 2748) before declaring ready.

#### 5.7.2 Webex / RoomOS Device Control plugin (`xapi`)

- **Engine:** **xAPI direct to the device over the LAN** (WebSocket or SSH, local device account) — which works for on-prem/CUCM-registered RoomOS devices **with zero internet**, keeping the air-gap rule. If the lab does have cloud connectivity, a second transport mode uses the Webex cloud APIs for cloud-registered devices; the mode is per-device.
- **Capabilities:** device inventory with credentials vault, status polling (SIP registration, software version, peripherals), command execution (dial, hangup, DTMF, volume, standby), configuration get/set, and **macro/UI extension push**. Software version capture feeds the upgrade harness ("did the endpoints take the new firmware?").

#### 5.7.3 SIP Endpoint Emulator plugin (`sip-emu`)

- **Engine:** a **pjsua2-based endpoint farm** — spin up N third-party SIP devices that register to CUCM (digest auth, Third-party SIP Device Basic/Advanced) or point at CMS/Expressway. Runs as its own worker process pool managed by the plugin. **Build note:** pjsua2 is compiled with video support (VP8 + H.264 via openh264) and SRTP, vendored in the offline bundle — this is a custom build, not the distro package.
- **Registration modes:** non-secure (UDP/TCP, RTP) and **secure** (TLS + SRTP) per endpoint — secure endpoints consume certs from the CA plugin, so the emulator doubles as a live test of the cert chain end-to-end.
- **Real media:** endpoints send **actual RTP audio** (tone/WAV sources, with loss/jitter/MOS-style stats per leg) and **synthetic video** (color-bar/test-pattern or looped file source) — so CMS spaces, video endpoints, and bandwidth/region configs see genuine media, not just signaling.
- **Mid-call media control:** mute/unmute audio and video streams independently per endpoint, hold/resume, re-INVITE exercising — scriptable within scenarios.
- **CTI interop scenarios:** emulator endpoints can call (and be called by) the **real, CTI-controlled lab phones** from §5.7.1 — e.g., "emu-007 dials DN 2001, CTI answers on the 8851, both sides verify two-way media, CTI transfers to 2002, CDR confirms cause 16 on both legs." This mixed real/synthetic testing is the heart of upgrade verification.
- **Provisioning:** bulk endpoint creation with the matching CUCM-side config generated via AXL (device + line + digest user in one wizard). Scenario definitions are YAML, runnable on demand or on schedule. This remains the functional-verification sibling of your standalone load generator — emulation and correctness, not load.

#### 5.7.4 Upgrade Test Harness (feature of the core, consuming all of the above)

The unifying workflow for "this lab tests product upgrades":

1. **Baseline run (pre-upgrade):** capture cluster versions, device registration counts (RisPort), endpoint firmware versions (xAPI), cert validation probes (§5.5 step 6), CTI-driven real-phone test calls, emulator scenario suite results, and CDR-arrival confirmation for the test calls (§5.6 freshness probes tie the loop closed — call made → CDR landed → cause code 16).
2. Perform the upgrade on the products.
3. **Verification run (post-upgrade):** bump the cluster version field, refresh JTAPI jar, re-run the identical suite.
4. **Diff report:** side-by-side baseline vs. post — registrations lost, scenarios newly failing, certs no longer presented/valid, CDR pipeline broken, firmware versions moved. Exportable as the upgrade's evidence artifact.

---

## 6. Web GUI

- **Look:** dark theme (slate/graphite palette, single accent color), dense-but-readable NOC style. Responsive enough for a laptop, not chasing mobile.
- **Layout:** left nav = Dashboard, then one entry per enabled plugin, then System (Plugins, Backup, Users, Audit, Updates).
- **Dashboard:** appliance health (CPU/mem/disk), per-plugin status cards with green/amber/red functional probes (e.g., DNS card actually resolves a test record; CA card shows next cert expiry; SFTP shows last DRS backup received).
- **Tech:** server-rendered Jinja2 + HTMX for interactivity (inline edits, modals, polling status) — no SPA build chain, all assets vendored locally (air-gap rule: zero CDN references).
- **Auth:** local accounts (argon2), roles `admin` / `operator` / `read-only`, session cookies, optional TOTP. Future: authenticate against the AD plugin itself.
- **Audit log:** every config mutation recorded (who/what/when/old→new) — Postgres table + GUI view.

---

## 7. API

Everything the GUI does goes through `/api/v1/` (the GUI is just an API client). Token auth (PATs) for automation — so your existing Python/PowerShell tooling can drive it:

```
POST /api/v1/plugins/dns/zones/{zone}/records
POST /api/v1/plugins/ca/sign            # body: CSR PEM + profile
GET  /api/v1/plugins/ca/chain.pem
POST /api/v1/plugins/ad/users/bulk
POST /api/v1/plugins/sftp/accounts
GET  /api/v1/system/health
```

OpenAPI docs served locally at `/api/docs`.

---

## 8. Security Model

- Control plane runs as `sipsmith` user; privileged operations (systemd, file writes to /etc) go through a small root helper daemon (`sipsmith-agent`) over a local Unix socket with a strict, allowlisted command vocabulary — the web process never runs as root.
- ufw managed from plugin manifests; only declared ports open.
- CA private keys: encrypted at rest, never leave the box, never exposed via API.
- Config templates validated before apply (named-checkconf, `sshd -t`, samba-tool checks); failed validation = no reload, old config kept.
- All secrets in `/etc/sipsmith/` and `/var/lib/sipsmith/` with tight modes; nothing secret in the code tree.

---

## 9. Backup / Restore

- One-click appliance backup: Postgres dump + each plugin's `backup()` contribution (zone files, CA store, Samba state via `samba-tool domain backup`, SFTP account metadata) → single encrypted tarball, downloadable or auto-dropped to... its own SFTP server, or another host.
- Restore wizard on a fresh install (matches your CUCM restore-tool philosophy).

---

## 10. Air-Gap Operations (first-class requirement)

SIPsmith assumes it may **never** touch the internet after the tarball crosses the air gap:

- **Install:** the offline bundle (§3.2) carries every `.deb` and Python wheel; the installer never calls `apt update` against external mirrors in `--offline` mode.
- **Zero external references at runtime:** all GUI assets (fonts, JS, CSS) vendored locally; no CDN links, no telemetry, no update checks, no OCSP/CRL calls to the outside (SIPsmith *is* the CRL distribution point). A CI lint rule fails the build if any template references an external URL.
- **Updates & plugins as files:** appliance updates, new plugins, and UC cert knowledge-pack updates are all signed `.zip`/`.tar.gz` files uploaded through the GUI — same path online or offline.
- **Documentation bundled:** full admin guide served from the appliance itself at `/docs`.
- **Time is a core service:** an air-gapped lab has no NTP, and clock skew silently breaks *everything* this box exists for — cert validity windows, Kerberos (AD), CUCM cluster health. SIPsmith ships a **chrony plugin in core**: the appliance acts as the lab's authoritative NTP server (serving its own clock, stratum-configurable), and every wizard that issues certs or provisions AD checks time sanity first. The GUI surfaces "lab time vs. configured offset" prominently.
- **FOSS-only underneath:** BIND9, Samba, OpenSSH, chrony, PostgreSQL — nothing license-activated, nothing that phones home.

---

## 11. Suggested Build Plan (Claude Code phases)

1. **Phase 0 — Skeleton:** installer (online + offline), systemd unit, FastAPI app, auth, dark UI shell, Postgres schema, plugin loader + SDK, dashboard, chrony/NTP core service.
2. **Phase 1 — SFTP plugin** (smallest surface, proves the SDK end-to-end).
3. **Phase 2 — CA plugin, full scope** (signing, profiles, CRL, OCSP, SCEP, EST — pure Python but now the second-largest plugin; budget accordingly).
4. **Phase 3 — DNS plugin** (BIND templating + validation pipeline).
5. **Phase 4 — UC Cert Orchestrator, guided tier** (CUCM + Expressway packs first; consumes CA + DNS, proves plugin-to-plugin APIs).
6. **Phase 5 — Records Landing** (DRF/CDR receivers + full parsing, Q.850 analytics, and the cross-product call-association engine).
7. **Phase 6 — SIP Endpoint Emulator** (custom pjsua2 build with video/SRTP; audio + secure registration first, video and media-control verbs second within the phase).
8. **Phase 7 — AD plugin** (hardest daemon; depends on DNS integration mode).
9. **Phase 8 — CTI plugin** (Java/JTAPI sidecar) **+ xAPI device control.**
10. **Phase 9 — Upgrade Test Harness** (composes phases 4–8 into baseline/verify/diff).
11. **Phase 10 — Orchestrator automated tier, remaining packs, renewal campaigns, plugin upload GUI, backup/restore, offline bundle builder, docs.**

Each phase ships something usable on its own.

---

## 12. Decisions Log

| # | Topic | Decision |
|---|---|---|
| D1 | Network | Single NIC / single IP for v1; multi-IP later if needed |
| D2 | Version floors | CUCM/CUC/IM&P: 12.5 floor, 14/15 primary. Expressway: X14+. CMS: 3.x with 4.x rules in the same pack (upgrade path is a version bump) |
| D3 | Off-box keygen | Supported as opt-in, clearly flagged "lab only"; guided CSR flow remains the default |
| D4 | CDR depth | **Full in v1**: parse-to-Postgres, Q.850 analytics, and cross-product call association/journey stitching across CUCM, CMS, and Expressway records |
| D5 | Emulator scope | Functional verification, not load. Real RTP audio **and synthetic video**, secure (TLS/SRTP) and non-secure registration, independent audio/video mute/unmute, scenarios that interoperate with CTI-controlled real phones. Custom pjsua2 build with video + SRTP, vendored offline |
| D6 | CA scope | **Full feature set from v1**: CSR signing, profiles, key+cert issuance, CRL, OCSP responder, SCEP, EST, expiry tracking, renewal campaigns |
| D7 | TFTP | Nice-to-have, post-v1. SFTP is the priority and stays core |
| D8 | Packaging | Offline tarball installer only for v1; .deb deferred |
| D9 | AD DNS mode | **BIND9_DLZ** — one BIND instance owns port 53; Samba's DLZ module serves the AD zone inside it. Delegation mode kept in the codebase as a documented fallback only |

All architectural decisions are now closed. Design status: **ready for build scaffolding.**
