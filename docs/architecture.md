# SIPsmith Architecture Reference

**Version:** 0.3 | **Platform:** Ubuntu Server 24.04 LTS

---

## 1. Overview

SIPsmith is a single-box lab-services appliance that provides DNS, an internal CA, Active Directory (Samba AD DC), and SFTP for an air-gapped Cisco collaboration upgrade-testing lab, plus a UC certificate orchestration engine, CDR/DRF records landing, a SIP endpoint emulator, CTI control, and Webex/RoomOS device control — all managed through a dark-themed web GUI and unified REST API.

**Design goals:**

| Goal | Implementation |
|---|---|
| **Air-gap first** | Zero runtime internet access; all assets, packages, and wheels vendored in the offline tarball. CI lint fails on any external URL in templates. |
| **No CDN, no build step** | Jinja2 + HTMX + vanilla JS. All JS/CSS assets served from `/static/` off the appliance. |
| **Validate-then-reload** | Every daemon config change runs the daemon's own validator (`named-checkconf`, `named-checkzone`, `sshd -t`, `samba-tool`) before reload. A failed validation leaves the old config in place and surfaces the error in the GUI and audit log. |
| **Privilege separation** | The FastAPI web process runs as the unprivileged `sipsmith` user. All privileged operations go through the `sipsmith-agent` root helper over a Unix socket. |
| **Plugin discipline** | All feature code lives in plugins that implement the SDK contract. Core never imports a plugin directly. Plugins are discovered from manifests and loaded at runtime. |

---

## 2. System Architecture

![System Architecture](/static/docs/system-architecture.svg)

The control plane (FastAPI + Uvicorn, port 8443 HTTPS) hosts the REST API, the Jinja2/HTMX web GUI, the plugin registry, and a Postgres-backed job runner for long-running tasks (installs, reloads, TLS probes). Plugins mount their own `APIRouter` instances and Jinja2 routes at load time. Underneath each plugin sits a battle-tested daemon managed via `pystemd` + templated config files: BIND9 for DNS, OpenSSH `internal-sftp` for SFTP, Samba AD DC for Active Directory. The CA plugin is the exception — it runs entirely in-process using the Python `cryptography` library with no external daemon dependency. PostgreSQL 16 stores all plugin config, audit records, issued cert inventory, CDR records, and job state.

---

## 3. Network Topology

![Network Topology](/static/docs/network-topology.svg)

SIPsmith occupies a single IP on the lab management VLAN (D1: single NIC, single IP for v1). All lab UC nodes (CUCM, CUC, IM&P, Expressway, CMS, RoomOS endpoints) point to the appliance for DNS (port 53), LDAP/LDAPS (389/636), SFTP (22), NTP (123), and the web GUI/API (8443). Inbound CDR feeds arrive via SFTP (CUCM CDR repository) and HTTPS POST (CMS CDR receiver). The appliance has no default route to the internet in production; in offline-install mode the installer never contacts external package mirrors.

---

## 4. Security Model

![Security Model](/static/docs/security-model.svg)

### sipsmith-agent Unix Socket

Privileged operations (systemd service control, writes to `/etc/`, config file deployment) are performed by `sipsmith-agent`, a root daemon that listens on a Unix domain socket at `/run/sipsmith/agent.sock`. The web process connects to this socket and sends JSON verb messages. The agent authenticates callers using `SO_PEERCRED` — only processes running as `sipsmith` are accepted.

### Allowlisted Verb Vocabulary

The agent maintains an explicit allowlist of permitted verbs. No arbitrary command execution is possible. Representative verbs:

```
systemd.start   systemd.stop   systemd.reload   systemd.status
file.write      file.chown     file.chmod
named.checkconf named.reload
sshd.check      sshd.reload
samba.tool      samba.reload
```

Any verb not in the allowlist is rejected and the attempt is logged to the audit table.

### ALLOWED_WRITE_PREFIXES

`file.write` verbs are further restricted to a declared set of path prefixes (`/etc/bind/`, `/etc/ssh/sshd_config.d/`, `/etc/samba/`, `/var/lib/sipsmith/`, `/etc/sipsmith/`). Attempts to write outside these prefixes are rejected even if the verb itself is allowed.

### Additional Security Controls

- CA private keys are stored encrypted at rest under `/var/lib/sipsmith/ca/` (mode 0600), never exposed via API, and never leave the appliance.
- ufw rules are derived exclusively from plugin manifests — only the ports a plugin declares are opened when it is enabled.
- All secrets live in `/etc/sipsmith/` (mode 0600); nothing secret is committed to the code tree. `config.example.yaml` is the committed template; `config.yaml` is gitignored.
- Every config mutation writes an audit row (actor, timestamp, resource, old value, new value) to the `audit_log` table.

---

## 5. Plugin SDK Contract

### plugin.yaml Manifest Fields

```yaml
id: dns                          # machine identifier, must be unique
name: DNS Server                 # human label shown in GUI
version: 1.0.0                   # semver
api_version: 1                   # SDK compatibility level
requires_packages: [bind9, bind9-utils]
requires_plugins: []             # dependency ordering (e.g., ad requires dns)
ports:
  - {port: 53, proto: udp}
  - {port: 53, proto: tcp}
entry_point: sipsmith_dns:DnsPlugin
```

The core plugin loader reads all `plugin.yaml` files, resolves the dependency graph, opens ufw rules from the `ports` list on enable, and instantiates the entry-point class.

### SipsmithPlugin ABC

```python
class SipsmithPlugin(ABC):
    meta: PluginMeta                         # parsed from plugin.yaml

    # Lifecycle
    async def install(self, ctx) -> None     # package install, dir creation, initial config
    async def enable(self, ctx) -> None
    async def disable(self, ctx) -> None
    async def status(self, ctx) -> ServiceStatus   # systemd state + functional probe
    async def apply_config(self, ctx) -> None      # render templates, validate, reload

    # FastAPI integration
    def api_router(self) -> APIRouter        # mounted at /api/v1/plugins/{id}
    def ui_pages(self) -> list[UiPage]       # nav entries + Jinja2 template routes

    # Backup
    def backup(self, ctx) -> Path            # plugin's contribution to appliance backup
    def restore(self, ctx, path: Path) -> None
```

The `ctx` object provides controlled, namespaced access to: the async DB session (scoped to the plugin's schema), `pystemd` systemd bindings, the Jinja2 template renderer, the audit logger, the `sipsmith-agent` client (for privileged file writes and daemon reloads), and **other plugins' public APIs** via `ctx.plugin('ca')`, `ctx.plugin('dns')` etc. Inter-plugin calls never go through core business logic — they call the public Python interface of the target plugin directly.

### Writing Audit Records

Every config mutation must call `ctx.audit.write(actor, resource, old_value, new_value)`. The audit logger inserts a row into the shared `audit_log` table, which is displayed in the GUI at System > Audit.

---

## 6. Data Model

### Per-Plugin Declarative Bases

Each plugin owns its SQLAlchemy declarative base and a dedicated schema in the `sipsmith` PostgreSQL database:

```
sipsmith (database)
├── public.audit_log          # shared — all plugins write here
├── public.jobs               # shared — background task queue
├── dns.*                     # zones, records, forwarders
├── ca.*                      # certs, keys, crl_entries, profiles
├── ad.*                      # domains, users, groups
├── sftp.*                    # accounts, quotas, retention_policies
├── uc_certs.*                # clusters, csr_submissions, probe_results
├── records.*                 # cdr_records, drf_sets, sources
├── sip_emu.*                 # endpoints, scenarios, run_results
├── cti.*                     # clusters, device_associations
└── xapi.*                    # devices, config_snapshots
```

### Async Sessions

All database access uses SQLAlchemy's async session factory (`AsyncSession` via `asyncpg`). Plugin routers receive a session via FastAPI dependency injection. Long-running background tasks (install, bulk user creation, TLS probes) run via the Postgres-backed job table — the web process enqueues a job row and a FastAPI background task picks it up, posting progress events that the GUI polls via HTMX.

### Alembic Migrations

Each plugin ships an `alembic/` directory scoped to its schema prefix. On plugin enable, the core runs the plugin's pending Alembic revisions in a transaction. The shared `public` schema migrations are managed by the core package.

### audit_log Table

```sql
CREATE TABLE public.audit_log (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor       TEXT NOT NULL,           -- username or 'system'
    plugin      TEXT,                    -- originating plugin id
    resource    TEXT NOT NULL,           -- e.g. 'dns.zone.lab.example.com'
    action      TEXT NOT NULL,           -- 'create' | 'update' | 'delete'
    old_value   JSONB,
    new_value   JSONB
);
```

---

## 7. Key Architectural Decisions

| Ref | Topic | Decision | Rationale |
|---|---|---|---|
| D3 | Off-box key generation | Supported as opt-in, clearly labelled "lab only" in the UI; guided CSR flow (private key never leaves the device) is the default and recommended path. | CSR flow is best practice; key issuance is a legitimate lab shortcut when a device cannot generate its own CSR. |
| D4 | CDR landing | Full parse-to-Postgres in v1: Q.850 analytics and cross-product call association ("call journey") across CUCM, CMS, and Expressway records. Landing is via SFTP (CUCM CDR), HTTP(S) receiver (CMS CDR POST), and syslog/REST pull (Expressway). No streaming ingestion pipeline. | Matches each product's native transport; Postgres is sufficient for lab-scale record volumes. |
| D5 | SIP emulator scope | Functional verification, not load. Real RTP audio and synthetic video (VP8/H.264); TLS/SRTP and non-secure modes; scenarios interoperating with CTI-controlled real phones. Custom pjsua2 build with video + SRTP support, vendored in the offline bundle. | The distro pjsua2 package lacks video; vendoring ensures reproducible air-gap builds. |
| D6 | CA hierarchy | Two-tier CA: offline-style Root CA + Issuing CA. Root key stays pristine; CUCM trust stores receive both certificates. Full feature set from v1: CSR signing, profiles, key+cert issuance, CRL, OCSP responder, SCEP, EST, expiry tracking, and renewal campaigns. | CUCM and Expressway expect a chain; pure Python `cryptography` gives exact EKU/SAN control without a step-ca dependency. |
| D9 | AD DNS mode | BIND9_DLZ: one BIND instance owns port 53; Samba's DLZ module serves the AD zone inside BIND. Samba never runs its internal DNS on port 53. Delegation mode is retained in documentation as a fallback. | Single port-53 owner prevents conflicts; DLZ is Samba's recommended production DNS integration. |
| A8 | JTAPI sidecar | The CTI plugin uses a Java sidecar process (JSON-RPC over Unix socket) rather than a Python JTAPI binding. The sidecar uses Java reflection to load the JTAPI jar at runtime — no compile-time dependency on the proprietary Cisco jar. The correct version-matched jar is fetched from the CUCM cluster itself over the LAN. | JTAPI is Java-only; jar must version-match CUCM; runtime reflection avoids proprietary build-time deps and works fully air-gapped since CUCM serves its own jar. |

---

## 8. Port Reference

| Port | Protocol | Direction | Service | Plugin |
|---|---|---|---|---|
| 22 | TCP | Inbound | SFTP (OpenSSH internal-sftp) | SFTP |
| 53 | UDP/TCP | Inbound | DNS (BIND9 + Samba DLZ) | DNS / AD |
| 88 | UDP/TCP | Inbound | Kerberos | AD |
| 123 | UDP | Inbound | NTP (chrony) | Core |
| 389 | TCP | Inbound | LDAP (Samba AD DC) | AD |
| 443 | TCP | Inbound | HTTPS CDR receiver (CMS) | Records |
| 636 | TCP | Inbound | LDAPS (Samba AD DC) | AD |
| 2748 | TCP | Outbound | JTAPI to CUCM CTI manager | CTI |
| 7443 | TCP | Outbound | AXL (CUCM SOAP API) | UC Certs / CTI / xAPI |
| 8443 | TCP | Inbound | SIPsmith web GUI / REST API (Uvicorn TLS) | Core |
| 5060 | UDP/TCP | Inbound/Out | SIP (non-secure emulator registration) | SIP Emu |
| 5061 | TCP | Inbound/Out | SIP TLS (secure emulator) | SIP Emu |
| 10000–20000 | UDP | Inbound/Out | RTP/SRTP media (emulator) | SIP Emu |
| /run/sipsmith/agent.sock | Unix | Internal | sipsmith-agent privileged helper | Core |

---

## 9. Extending SIPsmith — Writing a New Plugin

### Step 1: Create the directory layout

```
plugins/myplugin/
├── plugin.yaml
├── __init__.py        # exposes MyPlugin class
├── api.py             # FastAPI APIRouter
├── ui/
│   ├── templates/     # Jinja2 templates
│   └── static/        # CSS, JS, icons (all vendored — no CDN links)
├── templates/         # daemon config templates (.j2)
├── migrations/        # Alembic revisions for myplugin.* schema
└── tests/
    ├── test_api.py
    └── test_config_template.py   # required: render + validate config template
```

### Step 2: Write plugin.yaml

Declare `id`, `name`, `version`, `api_version: 1`, any `requires_packages`, any `requires_plugins` (dependency ordering), `ports`, and `entry_point`.

### Step 3: Implement SipsmithPlugin

Subclass `sipsmith.sdk.SipsmithPlugin` and implement all abstract methods. Return an `APIRouter` from `api_router()` — it will be mounted at `/api/v1/plugins/myplugin`. Return a list of `UiPage` objects from `ui_pages()` for nav entries and Jinja2 template routes.

Use `ctx.audit.write(...)` on every config mutation. Use `ctx.agent.file_write(path, content)` for files under agent-managed prefixes. Use `ctx.plugin('ca')` to call another plugin's public API.

### Step 4: Write tests

Every plugin must ship unit tests and at least one config-template test that renders the Jinja2 template with representative input and runs the daemon validator against the output (e.g., `named-checkzone` in a subprocess).

### Step 5: Install and test

```bash
# Drop the directory into plugins/
cp -r plugins/myplugin /opt/sipsmith/plugins/

# The plugin loader discovers it on next restart; or hot-load via API:
curl -X POST https://localhost:8443/api/v1/system/plugins/myplugin/install \
     -H "Authorization: Bearer <pat>"

# Run the check script
./scripts/check.sh
```

The `check.sh` script runs pylint, pytest, the air-gap lint (no external URL references), and the `@air-gap-auditor` agent. All must pass before a phase is marked done.

---

*SIPsmith architecture reference — served from the appliance at `/docs/architecture`.*
