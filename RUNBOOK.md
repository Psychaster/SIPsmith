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

### Configure Records Landing (Phase 5)

#### CUCM CDR via SFTP
1. GUI → SFTP → Presets → "CDR billing server" — creates the SFTP account and directory.
2. GUI → Records → "Add Source" → type `cucm_cdr`, point to the SFTP account/path created above.
3. CUCM: OS Admin → CDR Management → Billing Application Server Settings — set the SIPsmith IP,
   directory matching the SFTP path, user/password from the preset.
4. CDR files land automatically after each call. Click **Ingest** on the source row to parse
   new files on demand (schedule via cron coming in a later phase).

#### CMS CDR via HTTP receiver
1. GUI → Records → "Add Source" → type `cms_cdr`.
2. Click the **Token** button — copy the full receiver URL shown.
3. CMS: MMP or API → CDR Settings → CDR receiver URL — paste the URL.
4. CMS will POST XML CDRs to SIPsmith after each call; records appear in search immediately.
5. If the CMS server IP/config changes, click **Rotate Token** and update CMS with the new URL.

#### Search and journey view
- GUI → Records → Search — filter by calling/called DN, source, Q.850 cause code.
- Click a journey ID to see the full cross-product call flow (CUCM legs + CMS legs correlated).

#### Analytics endpoint
- `GET /api/v1/plugins/records/analytics/cause-codes` — Q.850 breakdown (JSON).
- `GET /api/v1/plugins/records/analytics/volume` — total records + average duration.

### Active Directory (Phase 7)

#### Prerequisites
- DNS plugin must be enabled and running (BIND9_DLZ requires BIND9)
- Install Samba packages (handled by `install-sipsmith.sh` when AD plugin is enabled):
  `apt install samba winbind libpam-winbind libnss-winbind krb5-user ldb-tools`

#### Provision the domain

1. GUI → Active Directory → "Provision Domain Wizard"
2. Enter:
   - **Realm**: fully-qualified AD domain (e.g. `LAB.LOCAL`)
   - **NetBIOS Name**: short name (e.g. `LAB`, ≤15 chars)
   - **Administrator Password**: AD admin password (min 8 chars)
   - **DSRM Password**: Directory Services Restore Mode password — store this safely
3. Click **Provision Domain** — takes ~2 minutes (samba-tool domain provision runs in background)
4. After completion:
   - Samba AD DC starts automatically (`samba-ad-dc.service`)
   - BIND9 DLZ module wired automatically (AD zone served by existing BIND instance)
   - Service dots on dashboard turn green

#### Create users for CUCM LDAP sync

1. GUI → AD → Users → **Bulk Create** → Pattern Mode
2. Configure: prefix (`user`), count (`100`), phone start (`+1-555-1000`), OU (`TestUsers`)
3. Click **Create Users** — creates 100 users (user001–user100) with telephoneNumber and ipPhone attrs
4. Each user's `telephoneNumber` = `+1-555-100N`, `ipPhone` = `+1-555-100N`

Or CSV mode:
```csv
sam_account,first_name,last_name,password,telephone_number,ip_phone,ou_dn
jsmith,John,Smith,Sipsmith1!,+1-555-1001,2001,OU=TestUsers,DC=lab,DC=local
```

#### Create CUCM sync service account

1. GUI → AD → Sync Helper → **Create Sync Service Account**
2. Enter username (e.g. `cucm-sync`) and a password
3. The account is created with password non-expiry set
4. The page shows the exact LDAP URL and DN to enter in CUCM

#### Configure CUCM LDAP sync

1. GUI → AD → CUCM Sync Helper — copy all connection values
2. CUCM Admin → System → LDAP → LDAP System: enable synchronisation, type = Microsoft Active Directory
3. CUCM → System → LDAP → LDAP Directory → Add New: paste values from sync helper
4. Map User ID = `sAMAccountName`, phone fields per the attribute table on the page
5. Click **Perform Full Sync Now**

#### Enable LDAPS (port 636)

1. GUI → AD → index → **Issue LDAPS Certificate from CA** (CA plugin must be initialized)
   - CA plugin auto-issues a server cert for the domain realm and installs it into Samba's TLS directory
2. For CUCM secure LDAP sync, also upload the SIPsmith CA chain to CUCM:
   - CUCM OS Admin → Security → Certificate Management → Upload Certificate
   - Upload to both `tomcat-trust` and `directory-trust`
   - Restart Tomcat on CUCM (Cisco CallManager serviceability)
3. Update CUCM LDAP Directory: change port to 636, enable TLS

#### DLZ zone and BIND integration notes

After provisioning, Samba writes `/var/lib/samba/private/named.conf` (the DLZ database declaration).
The AD plugin writes `/etc/bind/named.conf.ad-dlz` as a wrapper include and adds it to `named.conf.local`.
BIND reloads automatically. If the AD zone doesn't appear in DNS:
```bash
rndc status               # confirm named is running
rndc reload               # force reload
samba-tool dns query 127.0.0.1 LAB.LOCAL @ ALL -U Administrator
```

#### Common failure modes (Phase 7)

| Symptom | Check |
|---|---|
| Provision fails: "binary not found" | Install samba package: `apt install samba ldb-tools` |
| Provision fails: "realm invalid" | Realm must be FQDN with at least two labels (e.g. LAB.LOCAL, not just LAB) |
| samba-ad-dc fails to start | Check `/var/log/samba/log.samba`; common cause: BIND not running (DLZ load fails) |
| CUCM sync "LDAP connection failed" | Verify port 389 reachable from CUCM; check ufw allows 389/tcp from CUCM IP |
| CUCM sync returns 0 users | Check Base DN matches; try `ldapsearch -H ldap://localhost -x -b "DC=lab,DC=local"` |
| LDAPS cert error | CA plugin must be initialized before issuing LDAPS cert; check `/var/lib/samba/private/tls/` |

### SIP Endpoint Emulator (Phase 6)

#### Build pjsua2 (first-time or after OS update)

```bash
# Online (pulls packages via apt)
sudo bash scripts/build-pjsua2.sh

# Air-gapped (put pjproject-*.tar.bz2 in installer/vendor/ first)
sudo bash scripts/build-pjsua2.sh
```

The script is idempotent — a version stamp prevents rebuilds unless you delete
`/opt/sipsmith/venv/pjsua2-version`. Build time: ~4 min on a 4-core VM.

#### Enable the SIP Emulator plugin

1. GUI → Plugins → SIP Endpoint Emulator → Enable.
   - If pjsua2 was not built the worker starts with status `pjsua2_unavailable`;
     the dashboard banner links to the build instructions above.
2. Check Plugins dashboard — the tile should show "Worker ready · 0 endpoint(s) · 0 active call(s)".

#### Configure endpoints

1. GUI → SIP Emulator → Endpoints → "Add Endpoint".
2. Fill in SIP user, domain (e.g. `cucm.lab`), password, transport (`udp`/`tcp`), codec.
3. Click **Register** on the row — the endpoint registers with CUCM. Status dot turns green.
4. **Register All** bulk-registers all configured endpoints.

Ports used by the worker (must be open in any host firewall):
- UDP/TCP **5080** — SIP signaling
- UDP **10000–20000** — RTP media (pjsua2 default range)

#### Watch a call on the live dashboard

1. GUI → SIP Emulator → Dashboard.
2. Click an endpoint in the Fleet panel (left) — it becomes the active endpoint.
3. Type a destination URI (e.g. `sip:2000@cucm.lab`) and click **Dial**.
4. The Ladder panel (center) builds the SIP signaling diagram in real time via SSE.
5. The Stats panel (right) shows MOS gauge, TX/RX packet sparklines, jitter/RTT/loss figures.
6. Call controls: **Mute Audio**, **Mute Video**, **Hold**, **Hang Up**, **DTMF pad**.

#### Run a scenario

1. GUI → SIP Emulator → Scenarios → "Add Scenario".
2. Paste YAML (reference on the page), click **Validate** to check syntax offline.
3. Click **Run** — a scenario-run row appears with live pass/fail step counters.
4. Expand a run row to see the full execution log.

Example scenario (30-second test call):

```yaml
name: Basic call test
description: emu-001 calls 2000, 30 s, normal release
steps:
  - action: call
    endpoint: emu-001
    to: "sip:2000@cucm.lab"
    wait_for: connected
    timeout: 15
  - action: wait
    duration: 30
  - action: hangup
    endpoint: emu-001
  - action: assert_cdr
    endpoint: emu-001
    cause_code: 16
    wait_timeout: 30
```

#### Common failure modes (Phase 6)

| Symptom | Check |
|---|---|
| Worker status `pjsua2_unavailable` | Run `sudo bash scripts/build-pjsua2.sh`; check `/var/log/sipsmith/` for build errors |
| Endpoint stuck in `registering` | Confirm CUCM has a SIP trunk or device pointing at this appliance's IP; UDP 5080 reachable |
| No RTP stats / MOS always 4.5 | Verify RTP ports 10000–20000 are not firewalled; check CUCM media resource config |
| Scenario fails on `assert_cdr` | CDR pipeline (Phase 5) must be configured; CUCM billing server → SIPsmith SFTP |
| Ladder diagram shows no events | SSE connection blocked by proxy/WAF — check browser console for EventSource errors |

## Uninstall

```bash
sudo bash installer/install-sipsmith.sh --uninstall   # prompts; offers to preserve /var/lib/sipsmith
```
