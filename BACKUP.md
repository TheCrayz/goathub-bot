# GoatHub Backup-Plan (M-19, 2026-06-12)

## Was wird gesichert
`/var/www/goathub-bot/goathub.db` — die komplette SQLite-DB:
- `users` — Emails, bcrypt-Passwort-Hashes, **verschlüsselte HL-Agent-Keys** (`hl_api_secret_enc`)
- `managed_trades`, `processed_signal` — offene/abgeschlossene Trades + Replay-Dedup
- `activity`, `token_usage` — Audit-Trail + Kosten-Historie

**Nicht** in der DB, aber genauso kritisch: `/var/www/goathub-bot/.env`
(JWT_SECRET, **ENCRYPTION_KEY** — ohne den Key sind die gesicherten Agent-Keys
unbrauchbar!). Die .env ändert sich selten → bei jeder Änderung manuell in den
Passwort-Manager kopieren.

## Wie & wohin
- Skript: [scripts/backup_db.sh](scripts/backup_db.sh) — `sqlite3 ".backup"`
  (Online-Backup-API, konsistent auch bei laufendem Service/WAL — **niemals**
  blankes `cp` auf der Live-DB), danach `gzip`, `chmod 600`.
- Ziel: `/var/backups/goathub/goathub-YYYYMMDD-HHMMSS.db.gz` (Verzeichnis `0700`, Owner `goathub`)
- Zeitplan: täglich via systemd-Timer ([goathub-backup.timer](goathub-backup.timer)
  → [goathub-backup.service](goathub-backup.service)), `OnCalendar=daily` mit
  bis zu 45 Min randomisierter Verzögerung, `Persistent=true` (verpasste Läufe
  werden nachgeholt).
- Zusätzlich legt der Deploy-Workflow vor jedem `git pull` eine Kopie
  `goathub.db.bak-<ts>` ins App-Verzeichnis (Schutz gegen destruktive
  Migrationen in `init_db`).

## Retention
14 Tage (`find -mtime +14 -delete` im Skript; via `GOATHUB_BACKUP_RETENTION_DAYS` übersteuerbar).

## Installation / Status prüfen
Der Deploy-Workflow installiert Timer + Units automatisch, sobald der
`goathub`-User existiert (One-time-Setup: Kommentarblock in
[goathub.service](goathub.service)). Prüfen:

```bash
systemctl list-timers goathub-backup.timer     # nächster/letzter Lauf
ls -lh /var/backups/goathub/                   # Backups da + wachsend?
journalctl -u goathub-backup.service -n 20     # letzter Lauf OK?
```

## RESTORE-Prozedur
```bash
# 1. Bot stoppen (kein Writer auf der DB)
systemctl stop goathub

# 2. Kaputte/alte DB beiseitelegen (inkl. WAL/SHM!)
cd /var/www/goathub-bot
mv goathub.db goathub.db.broken-$(date +%s) || true
rm -f goathub.db-wal goathub.db-shm

# 3. Gewünschtes Backup einspielen
gunzip -c /var/backups/goathub/goathub-YYYYMMDD-HHMMSS.db.gz > goathub.db
chown goathub:goathub goathub.db
sqlite3 goathub.db "PRAGMA integrity_check;"    # muss "ok" ausgeben

# 4. Bot starten + verifizieren
systemctl start goathub
curl -fsS localhost:8000/api/health
# Dashboard: User vorhanden? Offene Positionen decken sich mit Hyperliquid?
# (Der Startup-Reconciler zieht fehlende SL/TP-Schutzorders nach.)
```
Achtung: alles zwischen Backup-Zeitpunkt und Restore (neue Trades,
`processed_signal`-Dedup-Einträge) fehlt danach — offene HL-Positionen
**manuell** gegen das Dashboard abgleichen, bevor der Listener wieder scharf
geschaltet wird.

## TODO
- **TODO: Off-VPS-Kopie** — Backups liegen bisher NUR auf dem VPS; bei
  Disk-/Host-Verlust ist alles weg. Verschlüsselt (z.B. `age`/`gpg` +
  `rclone`) nach extern (Hetzner Storage Box / S3) schieben, Key getrennt
  vom VPS aufbewahren.
