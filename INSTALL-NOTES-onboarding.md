# SIPsmith — UI onboarding patch (2026-06-12)

This drop layers the first-run / forced-password-reset / dashboard-to-config
flow on top of the §11–§16 corrected-drop fixes from the review doc. All
changes are in this repo; the zip is the same set of files in path-preserving
form for in-place deployment on the appliance.

## What's in this patch

**New behavior**
- `/login` detects an empty users table and renders a **"Create initial admin"**
  card (default `admin` / `admin@localhost`, password is operator-chosen).
- If `/etc/sipsmith/.bootstrap_token` exists, the setup card requires the
  matching token and deletes the file on success.
- The newly created admin has `must_change_password=True`; the auto-set session
  cookie sends the browser straight to `/account/change-password`.
- A new HTTP middleware redirects every browser GET (outside `/login`, the auth
  endpoints, `/account/change-password`, `/static`, and `/api/*`) to
  `/account/change-password` while the flag is set.
- After the reset, the user lands on `/dashboard`. Each plugin status card is a
  link: **enabled** → `/plugins/<id>` (config page); **disabled** →
  `/system/plugins` (enable + configure).
- Admins satisfy every `require_role(...)` endpoint (hierarchical RBAC).
- `sipsmith-admin create-admin` provides the same flow headlessly.

**Also bundled (replays of the corrected drop)**
- Agent security: `_resolve_allowed`, `_safe_zone_name`, recv timeout + 8 MiB cap
- Installer order, `rsync` dep, alembic `cd` to SRC_DIR
- `sqlalchemy[asyncio]`, `aiosqlite`
- Systemd `KillMode=mixed` so restarts don't orphan workers on :8443
- `base.html` single `content` block via `{{ self.content() }}`
- All 32 `TemplateResponse` sites switched to `(request, name, ctx)`
- Real htmx 1.9.12 (48,101 bytes)
- `/api/v1/plugins/status` returns `HTMLResponse`
- `PluginLoader.install_all()` runs every plugin's `install()` and upserts a
  `PluginRecord` row on startup; `enable`/`disable` endpoints also upsert
- Per-plugin enable fixes: sip-emu (`settings.database.url`, sync init_manager),
  sftp (`sftp.ensure_group` agent verb), sshd validate (wrapper with in-file
  `Include`)
- BIND tool path resolved via `shutil.which` + `/usr/sbin`/`/usr/bin` fallback;
  zone-dir traverse-bit fixup; `rndc reconfig + reload` on declaration changes
- All 14 `func.count(X).where(...)` → `select(func.count(X)).where(...)`
- `/system/plugins`, `/system/users` server-rendered pages
- New `sipsmith-admin` console script (consumes the bootstrap token)
- Browser `401` on HTML routes redirects to `/login` instead of dumping JSON

## Files

| Group | Path |
|---|---|
| New migration | `alembic/versions/0002_user_must_change_password.py` |
| New CLI | `sipsmith/cli/__init__.py`, `sipsmith/cli/admin.py` |
| New templates | `sipsmith/ui/templates/change_password.html`, `plugins.html`, `users.html` |
| Replaced | `sipsmith/ui/templates/login.html`, `base.html` |
| Updated | `sipsmith/app.py`, `sipsmith/auth/router.py`, `sipsmith/auth/deps.py`, `sipsmith/api/plugins.py`, `sipsmith/sdk/loader.py`, `sipsmith/sdk/plugin.py`, `sipsmith/models/user.py`, `sipsmith/ui/router.py`, `sipsmith/ui/docs_router.py`, `sipsmith/agent/{server,client,protocol}.py`, every `plugins/sipsmith_*/ui_router.py`, several `plugins/sipsmith_*/api.py`, `plugins/sipsmith_sftp/plugin.py`, `plugins/sipsmith_sip_emu/plugin.py`, `plugins/sipsmith_sip_emu/plugin.yaml`, `plugins/sipsmith_records/plugin.yaml`, `sipsmith/ui/static/css/sipsmith.css`, `sipsmith/ui/static/js/htmx.min.js`, `pyproject.toml`, `installer/install-sipsmith.sh`, `systemd/sipsmith.service` |

## Deploy on the appliance (the box at 10.35.30.215)

```bash
# 0. As root on the appliance:
sudo -i

# 1. Stop services and free the port
systemctl stop sipsmith
fuser -k 8443/tcp 2>/dev/null || true
systemctl reset-failed sipsmith
systemctl stop sipsmith-agent

# 2. Backup the current tree (you've done this every round — keep doing it)
TS=$(date +%Y%m%d-%H%M%S)
cp -a /opt/sipsmith/src /opt/sipsmith/src.bak.${TS}

# 3. Unzip the drop on top of /opt/sipsmith/src (preserves paths)
cd /opt/sipsmith/src
unzip -o /path/to/sipsmith-ui-onboarding.zip
chown -R sipsmith:sipsmith /opt/sipsmith/src

# 4. Pick up the new console script + pyproject deps. On this air-gapped box,
#    skip build isolation (setuptools is already in the venv).
sudo -u sipsmith /opt/sipsmith/venv/bin/pip install \
  --no-build-isolation -e '/opt/sipsmith/src[dev]'

# 5. Run the new migration (must be from SRC_DIR — alembic.ini uses a relative
#    script_location).
cd /opt/sipsmith/src
sudo -u sipsmith \
  SIPSMITH_CONFIG=/etc/sipsmith/config.yaml \
  /opt/sipsmith/venv/bin/alembic -c /opt/sipsmith/src/alembic.ini upgrade head

# 6. Refresh the systemd unit (KillMode change) and restart both services.
cp /opt/sipsmith/src/systemd/sipsmith.service /etc/systemd/system/sipsmith.service
systemctl daemon-reload
systemctl start sipsmith-agent
systemctl start sipsmith
systemctl status sipsmith --no-pager | head -20
```

## Verifying

The existing admin row (`admin` from the prior live deploy) will now have
`must_change_password=True` (the migration's `server_default=true` applies it
to all existing rows). Expected flow on first login after this deploy:

1. `https://10.35.30.215:8443/login` → standard sign-in card (users exist now,
   so it's NOT the setup card).
2. Sign in as `admin` with your existing password.
3. Auto-redirect to `/account/change-password`.
4. Submit current + new password (≥ 8 chars).
5. Land on `/dashboard`.
6. Each plugin status card is clickable. Enabled plugins go to their config
   page (`/plugins/<id>`); disabled plugins go to `/system/plugins`.

If you want to demo the **fresh-install** flow instead, on a scratch DB:

```sql
DELETE FROM users;
```

Then reload `/login` — it'll switch to the setup card. The new admin will be
created with `must_change_password=True` and the flow proceeds as above.

## Test-the-flow grep

```bash
# Verify no old-style TemplateResponse remains
grep -rE 'TemplateResponse\(\s*"' /opt/sipsmith/src/sipsmith /opt/sipsmith/src/plugins
# Verify no old-style func.count(X).where remains
grep -rE 'func\.count\([^)]+\)\.where' /opt/sipsmith/src/sipsmith /opt/sipsmith/src/plugins
# Verify htmx is the real thing (~48KB), not the 366-byte stub
wc -c /opt/sipsmith/src/sipsmith/ui/static/js/htmx.min.js
# Verify migration 0002 is present
ls /opt/sipsmith/src/alembic/versions/
```

## Known open items (NOT in this drop)

These are still outstanding from §16 and §10 of the review doc and were
explicitly *not* implemented this round:

- §16.7 DNS plugin still swallows agent apply failures and returns 201
- §16.8 Zone create still emits no glue A record for in-zone NS
- §16.9 `delete_zone` still leaves orphan `.zone` files
- §16.10 `expand_auto_ptr` still dead code (auto-PTR not wired into `create_record`)

Pick one to tackle next when ready.
