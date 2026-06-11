# SIPsmith User Guide

**Audience:** UC engineers and lab operators performing day-to-day tasks in an air-gapped Cisco collaboration upgrade-testing lab.

---

## 1. Getting Around

SIPsmith runs at `https://<appliance-ip>:8443`. After login you land on the Dashboard.

```
┌─────────────────────────────────────────────────────────────────┐
│ ◈ SIPsmith                                  admin ▾  [Logout]  │
├──────────────┬──────────────────────────────────────────────────┤
│ Dashboard    │  APPLIANCE HEALTH                                │
│ ─────────    │  CPU 12%  MEM 3.1 GB / 16 GB  DISK 42 GB free   │
│ DNS          │                                                  │
│ CA           │  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐   │
│ AD           │  │ DNS  ● │ │ CA   ● │ │ AD   ● │ │ SFTP ● │   │
│ SFTP         │  │ 4 zones│ │ 38 cert│ │ 250 usr│ │ 6 acct │   │
│ UC Certs     │  └────────┘ └────────┘ └────────┘ └────────┘   │
│ Records      │                                                  │
│ SIP Emu      │  ┌──────────────┐ ┌──────────────┐              │
│ CTI          │  │ UC Certs   ● │ │ Records    ● │              │
│ xAPI         │  │ 2 clusters   │ │ CDR 14d lag  │              │
│ ─────────    │  └──────────────┘ └──────────────┘              │
│ System       │                                                  │
└──────────────┴──────────────────────────────────────────────────┘
```

**Status indicators:** green (●) = healthy, amber (◑) = degraded/warning, red (○) = fault. Each card runs a live functional probe — the DNS card resolves a test record, the CA card checks next expiry, SFTP confirms last DRS receipt.

**Navigation:** click any plugin in the left nav. The System section contains Plugins, Backup, Users, Audit log, and Updates. All configuration changes appear in the Audit log (Admin > Audit).

**Roles:** `admin` — full control. `operator` — read/write on plugins, cannot manage users. `read-only` — view dashboards and certs only.

---

## 2. Certificate Workflows (CA Plugin)

### 2.1 Generating a CSR and Getting It Signed

1. On the UC device (CUCM, Expressway, CMS), generate a CSR for the relevant service (e.g., tomcat, callmanager). Download or copy the PEM text.
2. In SIPsmith, go to **CA > Sign CSR**.
3. Paste or upload the CSR file.
4. Select a **profile**: Server, Client, Server+Client (most CUCM services need Server+Client), or Custom.
5. Review the auto-populated SANs parsed from the CSR. Add any missing FQDNs.
6. Set validity (default: 825 days).
7. Click **Sign**. SIPsmith validates key size and CSR signature before issuing.
8. Download the result — see §2.2.

### 2.2 Downloading Cert Bundles

After signing, the download panel offers:

| Format | Contents | Use case |
|---|---|---|
| **PEM (cert only)** | Signed certificate | CUCM OS Admin cert upload |
| **PEM chain** | Cert + Issuing CA + Root CA | Most services needing a full chain |
| **DER** | Binary cert | Some legacy upload UIs |
| **CA chain only (PEM)** | Issuing + Root | CUCM tomcat-trust / callmanager-trust uploads |

Go to **CA > Inventory**, find any previously issued cert, and click **Download** to re-fetch bundles at any time.

### 2.3 Checking Certificate Expiry

1. Go to **CA > Inventory**.
2. Use the **Expiry** column header to sort ascending.
3. The expiry filter bar at the top accepts `<30d`, `<60d`, `<90d` presets.
4. Red rows = expired. Amber = expiring within 30 days.
5. Click any row > **Renew** to start a new signing flow pre-populated with the original CSR details.

The Dashboard CA card always shows the count of certs expiring within 30 days.

---

## 3. DNS Management (DNS Plugin)

### 3.1 Adding a Zone

1. Go to **DNS > Zones > + New Zone**.
2. Enter the zone name (e.g., `lab.example.com`), type (Forward or Reverse), and primary NS.
3. Click **Create**. SIPsmith writes the zone file and runs `named-checkzone` before reloading BIND. If validation fails, the old config is kept and the error is shown inline.

### 3.2 Adding Records

1. Open a zone > **Records > + Add Record**.
2. Select type: **A**, **PTR**, **CNAME**, or **SRV**.
3. Fill in the fields. Click **Save**. Each save triggers `named-checkzone` + `rndc reload`.

**SRV record fields:**

```
Name:     _cisco-uds._tcp.lab.example.com
Priority: 10   Weight: 10   Port: 8443
Target:   cucm-pub.lab.example.com
```

### 3.3 Cisco UC SRV Presets

1. Open a zone > **Records > + Add Record > Use Preset**.
2. Select a preset from the list:

| Preset | Record created |
|---|---|
| Cisco UDS (on-prem directory) | `_cisco-uds._tcp` SRV → CUCM pub, port 8443 |
| Collab Edge (MRA) | `_collab-edge._tls` SRV → Expressway-E FQDN, port 8443 |
| SIP TCP | `_sip._tcp` SRV |
| SIP TLS | `_sips._tcp` SRV |
| CUP Login | `_cuplogin._tcp` SRV |

3. Fill in the target FQDN and click **Insert**. The record is added and validated.

### 3.4 Bulk Importing A Records via CSV

1. Go to **DNS > Zones > (zone) > Records > Import CSV**.
2. Download the template. Format: `name,ip,ttl` (ttl optional, defaults to zone SOA).
3. Upload your populated CSV. SIPsmith previews the records before committing.
4. Click **Apply**. Failed rows are listed with the reason; successful rows are inserted.

---

## 4. CUCM LDAP Sync Setup (AD Plugin)

### 4.1 Domain Provisioning Wizard

1. Go to **AD > Provision Domain**.
2. Enter: **Realm** (e.g., `LAB.EXAMPLE.COM`), **NetBIOS name** (e.g., `LAB`), **DSRM password**.
3. SIPsmith checks that the DNS plugin has a forward zone matching the realm. If missing, it offers to create one.
4. Click **Provision**. Samba AD DC is initialised; this takes 30–90 seconds. Progress is shown in a live log panel.
5. On completion, the AD status card shows the domain and a Kerberos/LDAP health probe.

### 4.2 Bulk Generating Test Users

1. Go to **AD > Users > Bulk Create**.
2. Choose **Pattern mode**.
3. Set parameters:

```
Prefix:        testuser
Start index:   1
Count:         250
DN pattern:    testuser{n:03d}@lab.example.com
Phone pattern: +1415555{n:04d}
OU:            OU=UCUsers,DC=lab,DC=example,DC=com
```

4. Click **Preview** to see the first 5 entries.
5. Click **Create**. Users are created via `samba-tool` in batches; progress shown live.

### 4.3 CUCM Sync Helper Page

Go to **AD > CUCM Sync Helper**. This page shows ready-to-copy values for CUCM's **LDAP Directory** configuration screen:

```
LDAP Server:         192.168.10.5
LDAP Port (plain):   389
LDAP Port (TLS):     636
User Search Base:    OU=UCUsers,DC=lab,DC=example,DC=com
Bind DN:             CN=cucm-sync,OU=ServiceAccounts,DC=lab,DC=example,DC=com
```

The **Create Sync Account** button creates the service account and shows its password once. The page also shows whether the CA chain has been uploaded to CUCM's directory-trust store (required for secure LDAP on port 636).

---

## 5. UC Certificate Orchestration (UC Certs Plugin)

### 5.1 Adding a CUCM Cluster

1. Go to **UC Certs > Clusters > + Add Cluster**.
2. Select product: CUCM. Enter version (e.g., `15.0`), publisher FQDN/IP, AXL credentials.
3. Add subscriber FQDNs. Toggle deployment options: MRA enabled, multi-server SAN, secure SIP trunks.
4. Click **Save**. SIPsmith validates AXL connectivity and saves the cluster object.

### 5.2 Running DNS Pre-flight

1. Open the cluster > **Actions > DNS Pre-flight**.
2. SIPsmith calls the DNS plugin to verify: A and PTR records for every node, SRV records required by the deployment options (e.g., `_collab-edge._tls` for MRA).
3. The **Coherence Report** lists each expected record: present (green) or missing (red).
4. Click **Fix Missing** to auto-create missing records in the DNS plugin, then re-run pre-flight to confirm.
5. Pre-flight must be clean before signing is enabled.

### 5.3 Bulk Uploading and Signing CSRs

1. Open the cluster > **CSRs > Upload CSRs**.
2. Drag and drop CSR files (or a ZIP). SIPsmith parses each file and matches it to the expected certificate plan for this cluster and version.
3. Validation results appear per file: key size, missing SANs, wrong CN. Fix issues on the source device and re-upload.
4. Once all CSRs validate, click **Sign All**. Certificates are issued using product-correct profiles from the knowledge pack.
5. Download the **per-node bundle ZIPs** — each contains the signed cert, issuing CA cert, and root CA cert, with an ordered checklist for upload sequence and service restarts.

### 5.4 Reading the Post-Install Probe Report

After uploading certs to the devices and restarting services, return to the cluster in SIPsmith.

1. Click **Actions > Run TLS Probes**.
2. SIPsmith connects to each service port (tomcat 8443, SIP TLS 5061, XMPP 5222/5269) and checks: chain validates to SIPsmith CA, SANs match DNS, full chain is served.
3. Results appear in a table. Red rows indicate a problem — hover for detail (e.g., "SAN mismatch: expected cucm-sub.lab.example.com, got old-hostname").

---

## 6. SFTP Account Management (SFTP Plugin)

### 6.1 Creating an Account with a Preset

1. Go to **SFTP > Accounts > + New Account**.
2. Click **Use Preset** and select **CUCM DRS Backup Target**.
3. SIPsmith fills in: account name (`cucm-drs`), chroot path (`/var/lib/sipsmith/sftp/cucm-drs`), and applies read-write permissions for the DRS backup file pattern.
4. Set authentication: password, SSH public key, or both.
5. Click **Create**. The CUCM-side config values appear immediately — see §6.2.

### 6.2 CUCM-Side SFTP Config Values

After account creation, the **CUCM DRS Config** panel shows:

```
Device Name:   sipsmith-drs
Host/IP:       192.168.10.5
Port:          22
User Name:     cucm-drs
Password:      (shown once — copy now)
Directory:     /
```

Copy these values directly into CUCM's **Disaster Recovery System > Backup Device** page.

### 6.3 Using the File Browser

1. Go to **SFTP > Accounts > (account name) > Files**.
2. The browser shows the chroot directory tree with file sizes and timestamps.
3. Click a file to download it. Click the trash icon to delete.
4. The **Retention** tab shows current disk usage and lets you configure a policy (e.g., keep last 5 DRS backup sets, delete CDR files older than 30 days).

---

## 7. CDR Search and Call Journey (Records Plugin)

### 7.1 Searching CDR Records

1. Go to **Records > CDR Search**.
2. Filter by date range, calling party, called party, cluster, or Q.850 cause code.
3. Results show one row per CDR leg: timestamps, parties, duration, codec, cause code.
4. Click **Export CSV** to download the filtered set.

### 7.2 Viewing a Call Journey

1. From any CDR result row, click **Call Journey**.
2. SIPsmith correlates the call across all products using `globalCallID`, SIP Call-ID, and CMS correlation IDs.
3. The journey view shows each leg as a hop:

```
[SIP Emu emu-007] → [CUCM pub] → [Expressway-C] → [Expressway-E] → [CMS Space]
  cause: 16 (normal)              traversal OK                         caller left
  media: RTP 80ms delay                                                G.711 OK
```

4. Per-leg cause codes and media stats (where available from CDR/CMR records) are shown inline.

---

## 8. SIP Emulator — Running a Test Call Scenario (SIP Emu Plugin)

### 8.1 Configuring an Endpoint

1. Go to **SIP Emu > Endpoints > + New Endpoint**.
2. Enter: display name, SIP username, SIP password, registrar (CUCM IP or domain), transport (UDP/TCP/TLS).
3. Select codec preference (G.711, G.722, Opus).
4. For TLS/SRTP: toggle **Secure**. SIPsmith auto-requests a client cert from the CA plugin.
5. Click **Save**.

### 8.2 Registering a Fleet

1. Go to **SIP Emu > Endpoints**. Select multiple endpoints using the checkboxes.
2. Click **Actions > Register Selected**.
3. The status column updates live: Registering → Registered (green) / Failed (red).
4. The Dashboard SIP Emu card shows total registered / total configured.

### 8.3 Writing and Running a Scenario YAML

Create a scenario file. Example:

```yaml
name: basic-call-verify
steps:
  - action: dial
    from: emu-001
    to: "2001"
    wait_answer: 10s
  - action: assert_media
    endpoint: emu-001
    direction: both
    min_mos: 3.5
  - action: dtmf
    endpoint: emu-001
    digits: "1234"
  - action: hangup
    endpoint: emu-001
    expect_cause: 16
```

1. Go to **SIP Emu > Scenarios > + Upload** and upload the YAML file.
2. Click **Run**. A live log panel shows each step as it executes.

### 8.4 Reading MOS and RTP Stats

During an active scenario, go to **SIP Emu > Live Dashboard**:

```
Endpoint     State      MOS    Pkts Sent  Lost  Jitter
emu-001      In-Call    4.2    4,210      0     3ms
emu-002      In-Call    4.1    4,195      2     5ms
emu-003      Registered  —      —          —     —
```

After the scenario completes, per-leg stats are saved to the Records plugin and available in CDR Search.

---

## 9. CTI Click-to-Dial (CTI Plugin)

### 9.1 Connecting to a Cluster

1. Go to **CTI > Clusters > + Add Cluster**.
2. Enter CUCM publisher IP, AXL credentials, and CTI application user credentials.
3. Click **Connect**. SIPsmith fetches the version-matched JTAPI jar from the cluster (port 8443), stores it, and starts the Java sidecar.
4. The setup wizard verifies CTI port 2748 connectivity and application user device associations. Failures show a specific remediation step.

### 9.2 Selecting a Device on the Live Grid

1. Go to **CTI > Device Grid**. All CTI-associated devices appear with registration state and current call state.

```
Device           DN      Registered  State
SEP001122334455  2001    Yes         Idle
SEP556677889900  2002    Yes         In-Call
SEP001122334456  2003    No          —
```

2. Click a device row to select it as the controller.

### 9.3 Placing and Controlling a Call

1. With a device selected, enter a destination DN in the **Dial** field and click **Dial**.
2. Use the control bar: **Answer**, **Hold**, **Resume**, **Transfer** (enter target DN), **Conference**, **DTMF**, **Hangup**.
3. All events appear in the live event log below the grid (call state changes, line events).

---

## 10. xAPI Device Control (xAPI Plugin)

### 10.1 Adding a RoomOS Device

1. Go to **xAPI > Devices > + Add Device**.
2. Enter: device FQDN or IP, local admin username and password, transport (WebSocket preferred; SSH fallback).
3. Click **Save and Connect**. SIPsmith polls the device's xAPI status endpoint.
4. The device card shows: SIP registration state, software version, connected peripherals.

### 10.2 Dialling and Controlling a Call

1. Open a device > **Call Control**.
2. Enter a SIP URI or DN and click **Dial**.
3. Use the controls: **Mute**, **Volume**, **Hold**, **Transfer**, **Hangup**, **Send DTMF**.
4. The current call state and far-end participant are shown live.

### 10.3 Pushing a Configuration Change

1. Open a device > **Configuration**.
2. Browse the xAPI configuration tree or search by key name (e.g., `Audio.DefaultVolume`).
3. Edit the value and click **Apply**. SIPsmith sends the `xConfiguration` command over the active WebSocket session.
4. The change is logged in the Audit log with the old and new values.

---

*SIPsmith admin guide — served from the appliance at `/docs/user`.*
