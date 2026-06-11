# SIPsmith Documentation

SIPsmith is a lab-services appliance for **air-gapped Cisco collaboration upgrade testing**.
It runs on Ubuntu Server 24.04 and provides everything a lab needs in one dark web GUI —
no internet connectivity required at runtime.

---

## Documentation

| Guide | Description |
|---|---|
| [Installation Guide](/docs/install) | Hardware sizing, Ubuntu prep, installer walkthrough, post-install checklist |
| [Administrator Guide](/docs/admin) | Plugin-by-plugin configuration reference, backup/restore, security |
| [User Guide](/docs/user) | Day-to-day workflows: cert signing, DNS records, CDR search, CTI dial, scenarios |
| [Architecture Reference](/docs/architecture) | System diagrams, plugin SDK, security model, port reference |

---

## What's Included

| Plugin | Purpose | Key Protocols |
|---|---|---|
| **DNS** | BIND9 authoritative DNS + BIND9_DLZ for AD | UDP/TCP 53 |
| **CA** | Root + Issuing CA, SCEP, EST, OCSP, CRL | HTTPS 8443 |
| **SFTP** | DRS backup, firmware staging, CDR landing | TCP 22 |
| **UC Certs** | Cert pack-driven CUCM/Expressway cert orchestration | HTTPS |
| **Records** | CDR/DRF/CMS landing, journey correlation, analytics | SFTP, HTTP |
| **SIP Emulator** | pjsua2-based endpoint fleet, scenarios, video+SRTP | SIP 5080, RTP |
| **Active Directory** | Samba AD DC, LDAP/LDAPS, Kerberos, bulk test users | 88, 389, 636, 445 |
| **CTI Control** | JTAPI Java sidecar, click-to-dial, live device grid | TCP 2748 |
| **xAPI** | RoomOS device control via Cisco xAPI REST | HTTPS 443 |

---

## Quick Status Check

```bash
# All services at a glance
systemctl status sipsmith sipsmith-agent postgresql named smbd chrony ssh

# Live logs
journalctl -u sipsmith -f

# Check configuration
cat /etc/sipsmith/config.yaml
```

---

## Architecture Overview

![System Architecture](/static/docs/system-architecture.svg)

---

> **Air-gap note:** Nothing in SIPsmith reaches the internet at runtime. All dependencies
> are vendored. The offline bundle (`installer/build-bundle.sh`) captures all Python wheels
> and Debian packages needed for a fully disconnected install.
