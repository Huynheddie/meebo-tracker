#!/bin/sh
# One check, then report to healthchecks.io. Usage: run.sh x|sites
# x runs every minute, sites every 10 (see the timers). They share state.json, so they take turns.
mode=$1
case "$mode" in
  x)     hc=$HEALTHCHECK_URL_X ;;
  sites) hc=$HEALTHCHECK_URL ;;
  *)     echo "usage: run.sh x|sites" >&2; exit 2 ;;
esac
cd "$(dirname "$0")/.." || exit 1

flock -w 50 -E 75 /var/lib/meebo/lock python3 tracker.py --once --only "$mode"
status=$?
# 75: the other check held the lock the whole time. Skip quietly; the next minute will run.
[ "$status" -eq 75 ] && exit 0
[ "$status" -eq 0 ] && path="" || path="/fail"

hc=$(printf '%s' "$hc" | tr -d '[:space:]"<>')
[ -n "$hc" ] && { curl -fsS -m 10 --retry 3 -o /dev/null "${hc%/}$path" || echo "healthchecks.io ping failed" >&2; }
exit "$status"
