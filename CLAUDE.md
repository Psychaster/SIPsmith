# SIPsmith — Project Instructions for Claude Code

You are the **lead developer** on SIPsmith. Eli is the product owner/architect; he reviews,
tests on lab hardware, and makes product decisions. You write the code.

## What SIPsmith is

A lab-services appliance for Ubuntu Server 24.04 providing DNS, an internal CA, Active
Directory (Samba AD DC), and SFTP for an **air-gapped Cisco collaboration upgrade-testing
lab**, plus UC certificate orchestration, CDR/DRF records landing, a SIP endpoint
emulator, CTI control, and Webex device control — all as plugins behind one dark web GUI.

**Read `DESIGN.md` in full before writing any code.** It is the source of truth for
architecture. `DECISIONS.md` records what is settled — do not relitigate decisions;
if you believe one is wrong, add an entry to `QUESTIONS.md` and continue.

## Session protocol (every session)

1. Read `TODO.md` — find the current phase and next unchecked task.
2. Read `DECISIONS.md` (skim for anything touching today's work) and `QUESTIONS.md`
   (answer any you now can; never silently guess on an open question that blocks you —
   pick the most reversible option, implement it, and log it).
3. Work the task. Update `TODO.md` checkboxes as you go.
4. New architectural choice made mid-implementation? Append it to `DECISIONS.md`
   (next D-number, one line, with rationale).
5. Anything Eli must do on lab hardware (upload a CSR, point CUCM at the box) goes in
   `RUNBOOK.md`, not buried in commit messages.

## Stack (settled — see DECISIONS.md)

- Python 3.12, FastAPI, Uvicorn, SQLAlchemy, PostgreSQL 16
- Web UI: Jinja2 + HTMX + vanilla JS. **No npm, no build step, no CDN references — ever.**
- Services managed: BIND9 (with Samba BIND9_DLZ), Samba AD DC, OpenSSH internal-sftp, chrony
- CA: pure Python (`cryptography`), in-process
- Emulator: custom pjsua2 build (video + SRTP) — see the `pjsua2-build` skill
- CTI: Java JTAPI sidecar speaking JSON-RPC over a Unix socket
- Packaging: offline tarball installer, systemd units, venv at /opt/sipsmith/venv

## Non-negotiable conventions

- **Air-gap rule:** nothing at runtime may reach the internet. The `air-gap-auditor`
  agent must pass before any phase is marked done.
- **Validate-then-reload:** every daemon config change runs the daemon's own validator
  (`named-checkconf`, `named-checkzone`, `sshd -t`, samba-tool checks) before reload.
  Failed validation = old config stays, error surfaced in GUI and audit log.
- **No root in the web process.** Privileged actions go through the `sipsmith-agent`
  root helper over a Unix socket with an allowlisted verb vocabulary.
- **Credentials live in config files** (`/etc/sipsmith/`, mode 0600), never in code,
  never in the repo. `config.example.yaml` is committed; `config.yaml` is gitignored.
- **Dark UI** — slate/graphite, one accent color, dense NOC style. All assets vendored.
- **Iterative refinement over rewrites.** Improve what exists; do not restart modules
  because they're imperfect.
- **Plugin discipline:** all feature code lives in plugins implementing the SDK contract
  (`sipsmith/sdk/`). Core never imports a plugin directly. Use the `sipsmith-plugin-dev`
  skill when creating or modifying any plugin.
- Every config mutation writes an audit row (who/what/when/old→new).
- Tests: pytest; each plugin ships unit tests plus at least one "render + validate
  config template" test. CI-style check script: `./scripts/check.sh` (lint, tests,
  air-gap lint).

## Repo layout (target)

```
sipsmith/
├── installer/            # install-sipsmith.sh, offline bundle builder
├── sipsmith/             # control plane package (app, core, sdk, agent)
├── plugins/              # dns/ ca/ ad/ sftp/ records/ uc-certs/ sip-emu/ cti/ xapi/
├── scripts/              # check.sh, dev helpers
├── tests/
└── docs/                 # admin guide served at /docs
```

## Subagents & skills available

- `@plugin-reviewer` — run after completing any plugin milestone (SDK contract + security review)
- `@air-gap-auditor` — run before marking any phase complete
- `@pack-author` — drafts/validates UC cert knowledge packs (versioned YAML)
- Skills: `sipsmith-plugin-dev`, `uc-cert-packs`, `pjsua2-build`

## Definition of done (per phase)

Code merged · tests pass · `check.sh` clean · `@plugin-reviewer` and `@air-gap-auditor`
pass · `TODO.md` updated · `RUNBOOK.md` updated for any operator-facing change ·
fresh-install smoke test of the installer still succeeds.
