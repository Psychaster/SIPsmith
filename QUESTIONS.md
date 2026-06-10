# SIPsmith — Questions

Open implementation questions. Claude Code: if one of these blocks you, choose the most
reversible option, note it here with what you picked and why, and continue. Eli: answer
inline; answered items move to DECISIONS.md if they're decisions, or just get marked ✅.

## Open

1. **Samba DLZ ↔ BIND9 version coupling on Ubuntu 24.04.** The `dlz_bind9_*.so` module
   must match the installed BIND major version, and Ubuntu's AppArmor profile for named
   needs amendments to read Samba's DNS dir. Verify exact package/paths on a 24.04 VM
   during Phase 3 and document in RUNBOOK. (Known sharp edge — budget time.)
2. **JTAPI jar fetch path per CUCM version.** Confirm the plugins-page URL pattern and
   auth needed to pull the version-matched jar from CUCM 12.5 / 14 / 15 (Phase 8).
   Fallback: manual jar upload in GUI must exist regardless.
3. **CMS CDR receiver auth model.** What does CMS support when POSTing CDRs to a
   receiver (none / basic / TLS client cert) across 3.x and 4.x? Drives the receiver
   endpoint design in Phase 5.
4. **Expressway records transport per version.** Syslog vs REST polling capability and
   schemas for X14 vs X15 — pin down when writing expressway.yaml records rules.
5. **SCEP/EST library choices.** Pure-Python options vs implementing the protocol
   surfaces directly on FastAPI. Evaluate at the start of Phase 2; constraint: must be
   pip-vendorable for the offline bundle (no system daemons).
6. **CUCM Certificate Management REST API availability.** Which exact 14/15 SU
   introduces it and what it covers — determines how much of the automated tier the
   cucm.yaml pack can declare (Phase 10). Guided tier does not depend on this.
7. **pjsua2 H.264 licensing route.** openh264 (Cisco-distributed binary) vs VP8-only
   for v1 video. VP8-only avoids the binary-blob question; confirm CMS/endpoints in the
   lab negotiate VP8 acceptably, else vendor openh264.
8. **RisPort70 polling cadence** for the harness baseline — single snapshot vs short
   window average for registration counts?
9. **Emulator RTP port range + ufw strategy** — fixed range in manifest (e.g.,
   16384–17383) acceptable?
10. **GUI cert rotation UX:** when the CA re-issues the appliance's own HTTPS cert,
    how do we sequence the Uvicorn reload so the operator's session survives gracefully?

## Answered

(none yet)
