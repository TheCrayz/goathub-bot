#!/usr/bin/env bash
# M-16 (2026-06-12): Sanfter Service-Restart für Deploys.
#
# Problem: `systemctl restart goathub` mitten im Entry-Fill-Fenster
# (handle_signal pollt bis zu ENTRY_FILL_TIMEOUT_S=300s auf den Fill) killt
# den Watcher — die resting Order lebt auf HL weiter, SL/TP werden nie
# gesetzt (der Startup-Reconciler fängt das zwar ab, aber besser gar nicht
# erst riskieren). Dieses Skript wartet, bis keine Entries mehr in-flight
# sind (max. ~5 Min), und restartet erst dann.
#
# Heuristik "in-flight" (read-only Query auf die SQLite-DB):
#   a) managed_trades mit status='resting', updated_at in den letzten 300s
#   b) activity-Zeilen kind='order' in den letzten 300s (frisch platzierte
#      Entries, deren Trade-Row evtl. schon auf 'open' gekippt ist)
# Timestamps sind UTC (datetime.utcnow in models.py) — sqlite datetime('now')
# ist ebenfalls UTC, passt also direkt.
#
# Nutzung:  bash scripts/safe_restart.sh [pfad-zur-goathub.db]
set -euo pipefail

DB_PATH="${1:-/var/www/goathub-bot/goathub.db}"
WINDOW_S=300       # = ENTRY_FILL_TIMEOUT_S (config.py)
MAX_WAIT_S=300     # nach 5 Min wird trotzdem restartet (Deploy darf nicht ewig hängen)
POLL_S=10

in_flight_count() {
    # Fehler (DB fehlt, Tabelle fehlt, sqlite3 nicht installiert) → "0",
    # dann wird einfach sofort restartet wie bisher.
    sqlite3 -readonly "$DB_PATH" "
        SELECT
          (SELECT COUNT(*) FROM managed_trades
            WHERE status='resting'
              AND updated_at >= datetime('now', '-${WINDOW_S} seconds'))
        + (SELECT COUNT(*) FROM activity
            WHERE kind='order'
              AND ts >= datetime('now', '-${WINDOW_S} seconds'));
    " 2>/dev/null || echo 0
}

if ! command -v sqlite3 >/dev/null 2>&1 || [ ! -f "$DB_PATH" ]; then
    echo "safe_restart: sqlite3 oder DB ($DB_PATH) nicht gefunden — restarte direkt."
else
    waited=0
    while [ "$waited" -lt "$MAX_WAIT_S" ]; do
        n="$(in_flight_count)"
        if [ "${n:-0}" -eq 0 ] 2>/dev/null; then
            echo "safe_restart: keine in-flight Entries — Restart ist safe."
            break
        fi
        echo "safe_restart: $n in-flight Entry/Entries — warte ${POLL_S}s (${waited}/${MAX_WAIT_S}s)…"
        sleep "$POLL_S"
        waited=$((waited + POLL_S))
    done
    if [ "$waited" -ge "$MAX_WAIT_S" ]; then
        echo "⚠️⚠️⚠️ safe_restart: TIMEOUT nach ${MAX_WAIT_S}s — es sind noch Entries"
        echo "⚠️⚠️⚠️ in-flight, restarte TROTZDEM. Der Startup-Reconciler"
        echo "⚠️⚠️⚠️ (STARTUP_PROTECTION_RECONCILE) sollte fehlende SL/TP nachziehen —"
        echo "⚠️⚠️⚠️ Positionen nach dem Deploy MANUELL im Dashboard prüfen!"
    fi
fi

systemctl restart goathub
