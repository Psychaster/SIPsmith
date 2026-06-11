# SIPsmith — TODO

Work top to bottom. A phase is done only when the Definition of Done in CLAUDE.md is met
(including @plugin-reviewer and @air-gap-auditor passes).

## Phase 0 — Skeleton & core platform
- [x] Repo bootstrap: package layout, ruff + pytest config, `scripts/check.sh` (lint, tests, air-gap lint)
- [x] `installer/install-sipsmith.sh`: OS/FQDN/static-IP preflight, system user, dir tree, venv, Postgres init, systemd unit, bootstrap self-signed GUI cert, one-time admin token
- [x] `--offline` mode: install from bundled `debs/` and `wheels/`; `installer/build-bundle.sh` to produce the tarball
- [x] Core app: FastAPI factory, settings loader (/etc/sipsmith/config.yaml), DB session, Alembic migrations, structured logging → journald + /var/log/sipsmith
- [x] Auth: local users (argon2), roles admin/operator/read-only, sessions, optional TOTP; personal access tokens for the API
- [x] Audit log: middleware + table + GUI view
- [x] Plugin SDK: `plugin.yaml` parsing, lifecycle ABC, ctx object (db schema, systemd via agent, template renderer, audit, firewall, plugin registry), dependency-ordered load
- [x] `sipsmith-agent` root helper: Unix socket, allowlisted verbs (systemctl, write-config-path-allowlist, ufw), peer-cred auth
- [x] Dark UI shell: base layout, nav, dashboard page with plugin status cards, HTMX polling
- [x] chrony core service: serve NTP to the lab, stratum config page, time-sanity check API used by wizards
- [ ] Smoke test: fresh Ubuntu 24.04 VM → installer → GUI login → dashboard green

## Phase 1 — SFTP plugin (SDK proof)
- [x] Manifest + lifecycle; sshd_config.d snippet template + `sshd -t` validate-then-reload
- [x] Accounts: `sipsmith-sftp` group, no-shell users, chroot dirs, password/key auth
- [x] Presets: DRS backup target, firmware/MoH staging, log drop (show exact CUCM-side values)
- [x] GUI: account CRUD, per-share file browser (download/delete, usage), retention policy job
- [ ] @plugin-reviewer + @air-gap-auditor pass

## Phase 2 — CA plugin (full scope, D6)
- [x] Root + Issuing CA generation; encrypted-at-rest key storage in /var/lib/sipsmith/ca
- [x] Profiles: server, client, server+client, custom (validity, key usage, EKU)
- [x] CSR signing workflow: upload/paste → parse → SAN editor → sign → cert+chain download (PEM/DER)
- [x] Key+cert bundle issuance (opt-in "lab only" flag, D3)
- [x] Chain download endpoints; GUI re-issues its own HTTPS cert from the CA
- [x] CRL: revocation UI + HTTP distribution point
- [x] OCSP responder
- [x] SCEP endpoint
- [x] EST endpoint
- [x] Cert inventory + expiry dashboard (30/60/90) + renewal campaign job
- [ ] @plugin-reviewer + @air-gap-auditor pass

## Phase 3 — DNS plugin
- [ ] BIND9 ownership: named.conf templates, zone templates, named-checkconf/checkzone gate, rndc reload
- [ ] Zone + record CRUD (A/AAAA/PTR/CNAME/MX/TXT/SRV); auto-PTR option
- [ ] SRV presets: _cisco-uds, _cuplogin, _collab-edge, _sip/_sips
- [ ] Forwarders, recursion ACLs; CSV bulk import of A records
- [ ] BIND9_DLZ readiness (D9): config slots for Samba's DLZ module, version-matched module path detection
- [ ] @plugin-reviewer + @air-gap-auditor pass

## Phase 4 — UC Cert Orchestrator (guided tier)
- [ ] Cluster objects: product, version (editable), nodes, domains, deployment toggles
- [ ] Pack engine: version-keyed YAML schema, loader, validation (use uc-cert-packs skill)
- [ ] Packs: cucm.yaml + expressway.yaml first
- [ ] DNS pre-flight: create/verify A/PTR + MRA SRVs via DNS plugin API; coherence report gate
- [ ] CSR intake: bulk upload, parse, validate against plan (SANs, key size, EKU expectations)
- [ ] Signing via CA plugin API with pack profiles; per-node delivery bundles + ordered upload checklist
- [ ] Post-install TLS probes (8443/5061/5222/5269/443...) + results to dashboard
- [ ] Packs: cuc.yaml, imp.yaml, cms.yaml (guided tier)
- [ ] @plugin-reviewer + @air-gap-auditor pass

## Phase 5 — Records Landing (full depth, D4)
- [ ] Source registry per cluster; SFTP repos for CUCM DRF + CDR (builds on Phase 1)
- [ ] CMS HTTP(S) CDR receiver endpoint (per-cluster URI, auth)
- [ ] Expressway: syslog receiver + scheduled REST pull (pack-declared per version)
- [ ] Retention/quota policies, DRF set integrity check, freshness monitors → dashboard
- [ ] Parsers: CUCM CDR/CMR flat files, CMS XML, Expressway records → Postgres
- [ ] Analytics: search, volume charts, Q.850 cause breakdowns
- [ ] Call-journey correlation engine (globalCallID, SIP Call-ID, CMS correlation IDs, Expressway serials; time-window matching) + journey GUI
- [ ] @plugin-reviewer + @air-gap-auditor pass

## Phase 6 — SIP Endpoint Emulator (D5)
- [ ] pjsua2 custom build (video+SRTP) per pjsua2-build skill; vendor into offline bundle
- [ ] Worker pool architecture; endpoint farm config model
- [ ] Non-secure registration + real RTP audio (tone/WAV) + per-leg stats
- [ ] AXL companion wizard: device + line + digest user creation on CUCM
- [ ] Secure mode: TLS + SRTP using CA plugin certs
- [ ] Scenario engine (YAML): call, answer, hold/resume, transfer, DTMF, duration, teardown; on-demand + scheduled
- [ ] Video: test-pattern/looped-file source; independent audio/video mute/unmute verbs
- [ ] @plugin-reviewer + @air-gap-auditor pass

## Phase 7 — AD plugin
- [ ] Provision wizard: samba-tool domain provision with BIND9_DLZ (D9); DSRM handling
- [ ] DLZ integration with DNS plugin's BIND instance (module version matching, AppArmor)
- [ ] LDAP 389 + LDAPS 636 both on (A12); LDAPS cert from CA plugin, rotation hook
- [ ] Users/groups/OUs; bulk test-user generation (pattern/CSV, telephoneNumber/ipPhone attrs)
- [ ] CUCM LDAP sync helper page + sync service-account button + directory-trust callout
- [ ] @plugin-reviewer + @air-gap-auditor pass

## Phase 8 — CTI + xAPI
- [ ] Java JTAPI sidecar: JSON-RPC over Unix socket; verbs (makeCall, answer, hold, resume, transfer, conference, sendDTMF, observe)
- [ ] Per-cluster jar management: fetch version-matched JTAPI jar from the CUCM cluster; refresh prompt on version bump
- [ ] Setup wizard: AXL-create app user, device association, CTI-enable, port 2748 connectivity check
- [ ] GUI: live device grid, click-to-dial panel
- [ ] xAPI plugin: device inventory + credentials vault, WebSocket/SSH transports, status polling, dial/hangup/DTMF/volume/standby, config get/set, macro push; optional cloud mode per device
- [ ] @plugin-reviewer + @air-gap-auditor pass

## Phase 9 — Upgrade Test Harness
- [ ] Suite definition model; baseline run: versions, RisPort registration counts, xAPI firmware capture, cert probes, CTI test calls, emulator scenarios, CDR-arrival confirmation (journey-stitched)
- [ ] Verification run + diff report (registrations, scenarios, certs, CDR pipeline, firmware)
- [ ] Exportable evidence artifact
- [ ] @plugin-reviewer + @air-gap-auditor pass

## Phase 10 — Hardening & ship
- [ ] Orchestrator automated tier: CMS MMP/SFTP transport; product APIs where pack-declared
- [ ] Plugin upload/install via GUI (signed zips); pack update upload
- [ ] Appliance backup/restore (Postgres dump + plugin backup() contributions; samba-tool domain backup)
- [ ] Offline bundle builder finalization; full air-gap install rehearsal on clean VM
- [ ] Admin guide in /docs (served locally)
