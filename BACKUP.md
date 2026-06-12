# GoatHub Backup & Verification Notes

## Current verified state
- Dashboard UI pass: polished trading sections, mobile-friendly metadata, and trading desk navigation.
- Test verification: `python3 -m pytest -q` → `17 passed in 1.80s`.
- Deployment path: systemd service via [goathub.service](goathub.service), not Docker.

## Deployment commands
```bash
cd /var/www/goathub-bot
git pull origin main
source venv/bin/activate
pip install -q -r requirements.txt
systemctl restart goathub
systemctl status goathub --no-pager -l
```

## Important note
Use the server-side virtual environment for runtime validation, because the repo pins newer dependency versions than the local macOS system Python available in this workspace.
