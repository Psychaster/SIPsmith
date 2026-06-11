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

### UC Cert Orchestrator — first cert campaign (Phase 4)

1. GUI → UC Certs → "Add Cluster" — product (cucm/expressway/…), version (e.g. `14.0.1`), enterprise domain(s).
2. Add nodes: publisher first (sort_order 0), then subscribers. Include IPs so PTR checks work.
3. Enable toggles (MRA, XMPP federation) if applicable.
4. Click "Generate Plan" — the pack engine produces per-service cert items with computed SANs.
5. Click "DNS Preflight" — verifies A/PTR records exist for every SAN; SRV checks for enabled toggles.
   Fix any red/amber DNS items (GUI → DNS → Zones) before signing.
6. For each plan item, generate the CSR on the product side (OS Admin → Security → Certificate Management
   → "Generate CSR") and upload here. The orchestrator validates SANs, key size against the pack rules.
7. Click "Sign All Ready CSRs" — CA plugin signs with the correct profile (server/server_client).
8. Download the bundle ZIP — contains `certs/*.pem`, `chain/ca-chain.pem`, and `CHECKLIST.txt`.
9. Follow CHECKLIST.txt: upload CA chain to trust stores first, then identity certs, in restart-priority order.
10. GUI → plan → "TLS Probes" — confirms each service port presents a valid cert chaining to SIPsmith CA.

#### Adding more product packs (cuc, imp, cms)
Drop `plugins/sipsmith_uc_certs/packs/<product>.yaml` following the cucm.yaml schema.
The GUI "Add Cluster" product dropdown auto-populates from available packs.

### Configure DNS zones for a Cisco UC lab (Phase 3)

1. GUI → DNS → Zones → "New Zone" — create your lab forward zone (e.g. `lab.local`).
   - Primary NS: FQDN of this appliance (e.g. `sipsmith.lab.local`)
   - Admin Email: e.g. `admin@lab.local`
2. Open the zone → "Apply Preset" to add standard Cisco UC SRV records:
   - **CUCM**: adds `_cisco-uds._tcp`, `_cuplogin._tcp`, `_sip._tcp`, `_sips._tcp`, `_sip._udp`
   - **Expressway Edge**: adds `_collab-edge._tls`, `_xmpp-server._tcp`
   - **IM&P**: adds secondary `_cisco-uds._tcp` (priority 20)
3. Add A records pointing service FQDNs to their IPs; or use "Import CSV"
   (columns: `name,rdata[,ttl]`) for bulk.
4. GUI → DNS → Settings — set forwarders if clients need external DNS (e.g. 8.8.8.8).
   For full air-gap leave empty. Set `allow-recursion` to `localnets` for tighter control.
5. Click "Reload named" (or it happens automatically after every change).

Zone files: `/var/lib/sipsmith/dns/zones/<zone-name>.zone`
Master config: `/etc/bind/sipsmith.conf` (included from `named.conf.local`)

#### Point CUCM at SIPsmith for DNS
CUCM OS Admin → Platform → Network IP Settings → Preferred DNS: `<sipsmith-ip>`

#### DLZ (Active Directory integration, Phase 7)
After Samba AD DC is provisioned, GUI → DNS → Settings → enable DLZ and enter the
module path (e.g. `/usr/lib/x86_64-linux-gnu/samba/bind9/dlz_bind9_11.so`).
A `dlz "AD DNS" { … };` block is added to `sipsmith.conf` on next apply.

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
