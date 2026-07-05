#!/usr/bin/env bash
# Live-Bot auf macOS starten — mit Schlaf-Schutz und Auto-Neustart.
# Aufruf:  bash scripts/run_live_macbook.sh      (Stoppen: Ctrl-C)
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs

echo "LIVE-Bot startet (Config: config.live.yaml, Kill-Switch -30 USDC/Tag)."
echo "Log: logs/live.log — Stoppen mit Ctrl-C."

stop=0
trap 'stop=1; echo; echo "Stoppe Bot..."' INT TERM

while [ "$stop" -eq 0 ]; do
  # caffeinate verhindert, dass macOS den Bot in den Schlaf schickt
  # (-i: kein Idle-Sleep, -s: kein System-Sleep am Netzteil).
  caffeinate -is ./.venv/bin/python -m polybot.main run --config config.live.yaml \
    2>&1 | tee -a logs/live.log
  code=$?
  [ "$stop" -eq 1 ] && break
  # Kill-Switch (Exit über KillSwitch) soll NICHT neu starten — der Stopp ist
  # gewollt; alles andere (Netzabriss, Crash) startet nach 15s neu.
  if grep -q "Tagesverlust.*Limit.*erreicht" logs/live.log 2>/dev/null && \
     tail -5 logs/live.log | grep -q "Kill"; then
    echo "Kill-Switch hat gestoppt — KEIN Auto-Neustart. Erst Lage prüfen."
    break
  fi
  echo "Bot beendet (Exit $code) — Neustart in 15s (Ctrl-C zum Abbrechen)."
  sleep 15
done
echo "Live-Bot gestoppt."
