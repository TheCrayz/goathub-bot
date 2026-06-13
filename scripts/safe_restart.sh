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
# 2026-06-13 L-11:
#   1) EMERGENCY_HALT-Flag VOR dem Warte-Loop setzen und danach wieder
#      clearen. Vorher konnten während des Wartens NEUE Signale reinkommen
#      und das Fill-Fenster immer wieder neu öffnen → der Loop lief bis zum
#      Hard-Timeout und restartete trotzdem mitten in einem frischen Fill.
#      Mit gesetztem Halt nimmt die Engine keine neuen Entries mehr an, das
#      in-flight-Fenster läuft monoton leer.
#   2) sqlite-FEHLER ("DB locked", "no such table", sqlite3 fehlt) wird NICHT
#      mehr als "0 in-flight → sofort restart" fehlinterpretiert. Ein echter
#      Fehler → konservativ weiter warten (bis zum Hard-Timeout), nicht
#      fail-open sofort-restarten.
#
# Nutzung:  bash scripts/safe_restart.sh [pfad-zur-goathub.db]
set -euo pipefail

DB_PATH="${1:-/var/www/goathub-bot/goathub.db}"
WINDOW_S=300       # = ENTRY_FILL_TIMEOUT_S (config.py)
MAX_WAIT_S=300     # nach 5 Min wird trotzdem restartet (Deploy darf nicht ewig hängen)
POLL_S=10

# L-11: Halt-Flag-Pfad — Env-Override (EMERGENCY_HALT_FLAG_PATH) gewinnt, sonst
# der config.py-Default. Muss mit app/config.py übereinstimmen.
HALT_FLAG="${EMERGENCY_HALT_FLAG_PATH:-/var/lib/goathub/emergency-halt}"
HALT_SET_BY_US=0

clear_halt_if_ours() {
    # Nur clearen, wenn WIR den Halt gesetzt haben — einen vom Admin manuell
    # gesetzten Halt NIE versehentlich löschen.
    if [ "$HALT_SET_BY_US" -eq 1 ]; then
        rm -f "$HALT_FLAG" 2>/dev/null || true
        echo "safe_restart: EMERGENCY_HALT ($HALT_FLAG) wieder freigegeben."
        HALT_SET_BY_US=0
    fi
}
# Auch bei Skript-Abbruch (Ctrl-C / Deploy-Kill) den selbst gesetzten Halt
# wieder freigeben — sonst bliebe der Bot nach einem abgebrochenen Deploy
# dauerhaft gehaltet.
trap clear_halt_if_ours EXIT

# in_flight_count: gibt eine Zahl ODER das Sentinel "ERR" aus (sqlite-Fehler).
# Der Caller unterscheidet die beiden Fälle (L-11: Fehler != echtes 0-Ergebnis).
in_flight_count() {
    local out
    if out="$(sqlite3 -readonly "$DB_PATH" "
        SELECT
          (SELECT COUNT(*) FROM managed_trades
            WHERE status='resting'
              AND updated_at >= datetime('now', '-${WINDOW_S} seconds'))
        + (SELECT COUNT(*) FROM activity
            WHERE kind='order'
              AND ts >= datetime('now', '-${WINDOW_S} seconds'));
    " 2>/dev/null)"; then
        # sqlite3 erfolgreich — out sollte eine Zahl sein. Defensiv prüfen.
        if printf '%s' "$out" | grep -Eq '^[0-9]+$'; then
            printf '%s' "$out"
        else
            printf 'ERR'
        fi
    else
        printf 'ERR'
    fi
}

if ! command -v sqlite3 >/dev/null 2>&1 || [ ! -f "$DB_PATH" ]; then
    # L-11: hier KÖNNEN wir nicht messen (kein sqlite / keine DB). Wie bisher
    # direkt restarten — aber bewusst, nicht als Seiteneffekt eines verschluckten
    # Query-Fehlers. (Eine fehlende DB heißt: es lief noch nie ein Trade.)
    echo "safe_restart: sqlite3 oder DB ($DB_PATH) nicht gefunden — restarte direkt."
else
    # L-11: Bevor wir warten, neue Entries blocken. Existiert der Halt schon
    # (Admin hat manuell gehaltet), NICHT überschreiben/clearen — dann bleibt
    # HALT_SET_BY_US=0 und wir lassen ihn nach dem Restart in Ruhe.
    if [ -e "$HALT_FLAG" ]; then
        echo "safe_restart: EMERGENCY_HALT ($HALT_FLAG) ist bereits gesetzt — unberührt lassen."
    elif mkdir -p "$(dirname "$HALT_FLAG")" 2>/dev/null && \
         printf 'safe_restart deploy %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$HALT_FLAG" 2>/dev/null; then
        HALT_SET_BY_US=1
        echo "safe_restart: EMERGENCY_HALT gesetzt ($HALT_FLAG) — keine neuen Entries während des Wartens."
    else
        echo "⚠️ safe_restart: EMERGENCY_HALT ($HALT_FLAG) konnte NICHT gesetzt werden —"
        echo "⚠️ neue Signale können während des Wartens weiter Entries öffnen."
    fi

    waited=0
    while [ "$waited" -lt "$MAX_WAIT_S" ]; do
        n="$(in_flight_count)"
        if [ "$n" = "ERR" ]; then
            # L-11: sqlite-Fehler ist NICHT "0 in-flight". Konservativ
            # weiterwarten (mit gesetztem Halt läuft das Fenster ohnehin leer)
            # statt fail-open sofort zu restarten.
            echo "safe_restart: in-flight-Query fehlgeschlagen (sqlite-Fehler) — warte konservativ ${POLL_S}s (${waited}/${MAX_WAIT_S}s)…"
        elif [ "$n" -eq 0 ]; then
            echo "safe_restart: keine in-flight Entries — Restart ist safe."
            break
        else
            echo "safe_restart: $n in-flight Entry/Entries — warte ${POLL_S}s (${waited}/${MAX_WAIT_S}s)…"
        fi
        sleep "$POLL_S"
        waited=$((waited + POLL_S))
    done
    if [ "$waited" -ge "$MAX_WAIT_S" ]; then
        echo "⚠️⚠️⚠️ safe_restart: TIMEOUT nach ${MAX_WAIT_S}s — es sind noch Entries"
        echo "⚠️⚠️⚠️ in-flight (oder die DB-Query schlug durchgehend fehl), restarte"
        echo "⚠️⚠️⚠️ TROTZDEM. Der Startup-Reconciler (STARTUP_PROTECTION_RECONCILE)"
        echo "⚠️⚠️⚠️ sollte fehlende SL/TP nachziehen — Positionen nach dem Deploy"
        echo "⚠️⚠️⚠️ MANUELL im Dashboard prüfen!"
    fi
fi

# L-11: Halt VOR dem Restart wieder freigeben, damit der frisch gestartete
# Prozess sofort wieder Signale annimmt. (Der EXIT-trap würde es ohnehin tun;
# explizit hier, damit das Clearen sicher VOR dem restart passiert.)
clear_halt_if_ours

systemctl restart goathub
