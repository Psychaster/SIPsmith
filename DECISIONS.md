# SIPsmith — Decisions Log

Settled decisions. Do not relitigate; if one seems wrong during implementation,
log a question in QUESTIONS.md with evidence and keep building.

## Product decisions (from design review with Eli)

| # | Topic | Decision |
|---|---|---|
| D1 | Network | Single NIC / single IP for v1; multi-IP later if needed |
| D2 | Version floors | CUCM/CUC/IM&P: 12.5 floor, 14/15 primary targets. Expressway: X14+. CMS: 3.x with 4.x rules in the same pack — a 3.x→4.x upgrade is a version-field bump |
| D3 | Off-box keygen | Supported as opt-in, clearly flagged "lab only"; guided CSR flow is the default |
| D4 | CDR depth | Full in v1: parse-to-Postgres, Q.850 analytics, cross-product call association ("call journey") across CUCM, CMS, Expressway |
| D5 | Emulator scope | Functional verification, not load. Real RTP audio AND synthetic video; secure (TLS/SRTP) and non-secure registration; independent audio/video mute/unmute; scenarios interoperating with CTI-controlled real phones. Custom pjsua2 build, vendored offline |
| D6 | CA scope | Full feature set from v1: CSR signing, profiles, key+cert issuance, CRL, OCSP responder, SCEP, EST, expiry tracking, renewal campaigns |
| D7 | TFTP | Post-v1 nice-to-have. SFTP is core and prioritized |
| D8 | Packaging | Offline tarball installer only for v1; .deb deferred |
| D9 | AD DNS mode | BIND9_DLZ — one BIND owns port 53; Samba's DLZ module serves the AD zone inside it. Delegation documented as fallback only |

## Architectural decisions (from DESIGN.md)

| # | Topic | Decision | Rationale |
|---|---|---|---|
| A1 | Control plane | Python 3.12 + FastAPI + Postgres 16 | Async, team familiarity, plugin routers |
| A2 | Web UI | Jinja2 + HTMX, no build step | Air-gap: zero npm/CDN; trivially vendored |
| A3 | Service model | Manage real daemons (BIND9/Samba/OpenSSH/chrony) via templated config + validate-then-reload + systemd | Battle-tested daemons; appliance is the brain, not the engine |
| A4 | CA engine | Pure Python `cryptography`, in-process | Exact control of EKU/SAN for UC certs; no step-ca dependency |
| A5 | Plugin model | Manifest (`plugin.yaml`) + SDK ABC + plugin-to-plugin API via ctx | Drop-in zips, air-gap installable, dependency ordering |
| A6 | Privilege split | Web process unprivileged; `sipsmith-agent` root helper, allowlisted verbs over Unix socket | Web process never runs as root |
| A7 | UC cert knowledge | Version-keyed YAML packs, data not code | Cisco changes per release; packs update without code |
| A8 | CTI | Java JTAPI sidecar, JSON-RPC over Unix socket; per-cluster jar fetched from the CUCM cluster itself | JTAPI is Java-only; jar must version-match CUCM (works air-gapped — CUCM serves it on the LAN) |
| A9 | Webex devices | xAPI direct over LAN (WebSocket/SSH) as primary transport; cloud API as optional per-device mode | Air-gap compatible for on-prem registered RoomOS |
| A10 | SFTP | OpenSSH internal-sftp, chroot, config snippet in sshd_config.d only | Never rewrite main sshd_config; can't lock out the box |
| A11 | Time | chrony in core; appliance is the lab's authoritative NTP | Air-gapped labs have no NTP; skew breaks certs/Kerberos/CUCM |
| A12 | LDAP | 389 and 636 both on by default; LDAPS cert auto-issued/rotated by CA plugin | CUCM sync needs both options; secure sync requires chain in directory-trust |
| A13 | Records transports | SFTP (CUCM DRF/CDR), HTTP(S) receiver (CMS CDR), syslog/REST pull (Expressway) | Match each product's native transport |
| A14 | Task queue | Postgres-backed job table + FastAPI background tasks (no Redis in v1) | Smaller footprint |
| A15 | First-run onboarding | Empty users table → /login renders a setup card; `POST /auth/setup` is gated by the installer's one-time bootstrap token (file deleted on success) and creates the admin with `must_change_password=true`; an HTTP middleware forces browser sessions to /account/change-password until the flag clears. `sipsmith-admin create-admin` is the headless equivalent | No console required on the appliance; token gate blocks a same-LAN attacker racing the setup endpoint; forced reset ensures the bootstrap password never persists |
| A16 | RBAC semantics | `require_role(X)` enforces a minimum rank on the admin > operator > readonly hierarchy, not set membership | Admins must satisfy operator endpoints; per-endpoint role lists invited drift (review §16.1) |
| A17 | Plugin schema provisioning | Plugin tables are created by each plugin's idempotent `install()`, invoked by `PluginLoader.install_all()` in the app lifespan (which also upserts the `plugins` registry row); core schema stays in Alembic, run by the installer/CLI only — never in-process | Lifespan `alembic upgrade` breaks inside a running event loop (review §2.5); plugins own their schemas (review §8/§14) |

## How to add a decision

Append the next D-number (product) or A-number (architecture), one row,
with a short rationale. Date in the commit is sufficient.
