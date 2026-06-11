# SIPsmith — Runbook

Operator and developer procedures. Claude Code: keep this current — every
operator-facing change lands here in the same commit.

## Install

```bash
# Online
sudo bash installer/install-sipsmith.sh

# Air-gapped (after carrying sipsmith-<ver>-offline.tar.gz across the gap)
tar xzf sipsmith-<ver>-offline.tar.gz && cd sipsmith-<ver>
sudo bash install-sipsmith.sh --offline
```

Preflight requirements: Ubuntu Server 24.04, static IP, FQDN set (`hostnamectl`),
≥4 GB RAM (8 GB with AD + emulator), ≥40 GB disk (CDR/DRF landing grows — plan more).

After install: browse to `https://<fqdn>:8443`, complete admin setup with the
one-time token printed by the installer.

## Service management

```bash
systemctl status sipsmith            # control plane
systemctl status sipsmith-agent      # root helper
systemctl status named smbd chrony ssh postgresql
journalctl -u sipsmith -f            # live logs
```

App + audit logs also at `/var/log/sipsmith/`.

## Key paths

| Path | What |
|---|---|
| /opt/sipsmith | code, venv, plugins |
| /etc/sipsmith/config.yaml | appliance config (0600) |
| /var/lib/sipsmith/ca | CA store — **back this up; protect it** |
| /var/lib/sipsmith/sftp/<user> | chrooted SFTP shares (DRF/CDR landing) |
| /var/lib/sipsmith/backups | appliance backup sets |

## Developer loop

```bash
cd /opt/sipsmith/src            # or your checkout
./scripts/check.sh              # lint + tests + air-gap lint — must pass before commit
./scripts/dev.sh                # run control plane in reload mode against dev DB
installer/build-bundle.sh       # produce offline tarball (vendors wheels, debs, pjsua2)
```

Plugin development: see `.claude/skills/sipsmith-plugin-dev/SKILL.md`.

## Operator procedures (grow as phases land)

### Point CUCM DRS at SIPsmith (Phase 1)
1. GUI → SFTP → Presets → "DRS backup target" → creates account + directory.
2. The preset page shows the exact values for CUCM: OS Admin → Disaster Recovery
   System → Backup Device (host, path, user). Schedule the backup in CUCM.
3. Dashboard shows last received backup set per cluster once Phase 5 lands.

### Bootstrap the CA (Phase 2)
1. GUI → CA → Overview → "Set up CA" if not yet initialized.
2. Accept defaults (Root CN, Issuing CN, RSA 4096) or customize.
3. Click "Generate CA" — takes 5-10 s for RSA 4096.
4. Download root.pem and install into browser / CUCM trust stores.
5. Enrollment URLs shown on the Overview page:
   - CRL:  `http://<fqdn>/api/v1/plugins/ca/crl`
   - OCSP: `http://<fqdn>/api/v1/plugins/ca/ocsp`
   - SCEP: `http://<fqdn>/api/v1/plugins/ca/scep`
   - EST:  `https://<fqdn>/.well-known/est`

### Sign a CUCM CSR (Phase 2/4)
1. Generate CSR on CUCM (OS Admin → Security → Certificate Management) —
   the orchestrator wizard tells you exactly which service/SAN options.
2. GUI → CA → Sign CSR → paste PEM → select profile → sign.
3. Upload trust chain (root + issuing) to CUCM *-trust stores first,
   then the identity cert; follow the restart order in the delivery checklist.
4. GUI → CA → Inventory shows all issued/revoked certs and expiry dates.

### Re-issue GUI HTTPS cert from the CA (Phase 2)
1. After CA is initialized, GUI → CA → Overview → "Re-issue GUI cert".
2. Browser will show a cert warning once on next load — add exception / trust chain.
3. The old self-signed cert is replaced with a CA-issued cert.

### Common failure modes
| Symptom | Check |
|---|---|
| GUI cert warnings after CA enable | Expected once — appliance re-issued its own cert; re-trust the chain |
| named won't start after AD provision | DLZ module/BIND version mismatch or AppArmor — see QUESTIONS #1 notes |
| Secure LDAP sync fails from CUCM | CA chain present in CUCM directory-trust? Tomcat restarted? |
| No CDRs landing | Freshness monitor → check CUCM billing-server config / CMS receiver URI |
| Everything TLS broke at once | Check chrony — clock skew. SIPsmith dashboard shows lab time prominently for a reason |

## Uninstall

```bash
sudo bash installer/install-sipsmith.sh --uninstall   # prompts; offers to preserve /var/lib/sipsmith
```
