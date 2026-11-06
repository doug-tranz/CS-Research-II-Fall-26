#!/usr/bin/env bash
# Finishes the free Nemotron runs on any computer (written to hand the study off to a home PC).
# Super and Ultra are done; Lightning was paused at 583 of 600 main-run calls, before its check.
#
#   export OPENROUTER_API_KEY=sk-or-...      (the same key the study used)
#   bash resume_nemotron.sh
#
# It pulls the latest results, sets up venv/ if missing, continues Lightning's session from its
# next call (--resume: nothing is redone), then runs its 60-call stability check. Stopped by the
# daily free-model limit (exit 3), it waits until 00:05 UTC and continues. When Lightning is
# finished it commits and pushes results/FINALIZED_RESULTS. Free models: it costs nothing.
# Safe to rerun at any point: finished work is skipped.
set -euo pipefail
cd "$(dirname "$0")"
BRANCH=Khalil-context-path
DAILY_LIMIT_EXIT=3

[ -n "${OPENROUTER_API_KEY:-}" ] || { echo "set OPENROUTER_API_KEY first (export OPENROUTER_API_KEY=sk-or-...)"; exit 1; }

git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

if [ ! -d venv ]; then
  python -m venv venv 2>/dev/null || python3 -m venv venv
fi
PY=venv/Scripts/python.exe; [ -x "$PY" ] || PY=venv/bin/python
"$PY" -m pip install -q -r requirements.txt
export PYTHON="$PWD/$PY" PYTHONUNBUFFERED=1
mkdir -p results/FINALIZED_RESULTS/logs
LOG=results/FINALIZED_RESULTS/logs/lightning.resume.log

while true; do
  status=0
  bash run_main.sh lightning 2>&1 | tee -a "$LOG" || status=${PIPESTATUS[0]}
  [ "$status" = 0 ] && break
  if [ "$status" != "$DAILY_LIMIT_EXIT" ]; then
    echo "=== Lightning stopped with exit $status (see $LOG); fix it and run this script again"; exit "$status"
  fi
  now=$(date -u +%s); next=$(date -u -d "tomorrow 00:05" +%s)
  echo "=== daily free limit reached; continuing at $(date -u -d "@$next" '+%Y-%m-%d %H:%M UTC')"
  sleep $(( next - now ))
done

echo "=== Lightning finished; committing and pushing the results"
git add results/FINALIZED_RESULTS
git commit -m "FINALIZED_RESULTS: Nemotron Lightning finished (main run + stability check)" || echo "nothing new to commit"
git push origin "$BRANCH"
echo "=== all 11 models done"
