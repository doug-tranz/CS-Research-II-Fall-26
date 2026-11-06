#!/usr/bin/env bash
# The main study runs, one model per command, started on your call:
#
#   bash run_main.sh list          the models, in run order, with their estimates
#   bash run_main.sh credits       what is left on the OpenRouter key (free)
#   bash run_main.sh luna          one model: its main run, then its stability check
#   bash run_main.sh paid          the eight paid models one after another (logs in results/FINALIZED_RESULTS/logs/)
#   bash run_main.sh parallel flash argon ...   the named models at once, logs <name>.parallel.log (a model already
#                                  running elsewhere is skipped: its results file is locked)
#   bash run_main.sh free          the three free Nemotron models at once (logs in results/FINALIZED_RESULTS/logs/);
#                                  run it again each day after 00:00 UTC until all three finish
#
# Free models share OpenRouter's daily allowance (~1,000 requests). When it runs out the run stops
# at once (no failed rows) and exits 3; the same command after the reset continues the stopped
# session where it left off (--resume), so no request is spent twice.
#
# Main run: 200 cases (--pairs 100, every check 4 or 6 cases) at low, medium and high effort,
# one session each, into results/FINALIZED_RESULTS/main_<name>.json. Stability check: 10 pairs (all seven
# groups) sampled 3 times at medium effort, reusing the main run's answers as the first sample,
# into results/FINALIZED_RESULTS/check_<name>.json. Every run is live (no Batch API: its calls skip the cache).
#
# --max-cost is about 1.5x each measured estimate: a run whose estimate is above it does not
# start, and one that reaches it stops. Estimates come from the model's test run
# (--measured-from). If a run stops, run the same command again: --resume skips the efforts
# already finished and continues a cut-off one from its next call. Keep the computer awake and online.
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python}"   # the interpreter (a test can stand one in)
OUT=results/FINALIZED_RESULTS   # every main run, check and log of the study

#      name       model                                    provider               main$  check$  estimate (3 efforts)
MODELS="
luna       openai/gpt-6-luna                        openai/flex            1.50   0.50   0.90
terra      openai/gpt-6.1-terra                     openai/flex            8.00   2.00   5.42
sol        openai/gpt-6.1-sol                       openai/flex            14.00  3.00   9.44
flashlite  google/gemini-3.5-flash-lite             google-ai-studio/flex  6.00   1.00   4.07
flash      google/gemini-3.8-flash                  google-ai-studio/flex  14.00  2.00   9.37
argon      google/gemini-4-argon                    google-ai-studio/flex  30.00  6.00   16.14
qwenflash  qwen/qwen3.8-flash                       alibaba                6.50   1.00   4.31
qwen27b    qwen/qwen3.8-27b                         phala                  17.00  2.50   11.48
lightning  nvidia/nemotron-3.5-lightning:free       -                      0.05   0.05   0.00
super      nvidia/nemotron-3.5-super-vl:free        -                      0.05   0.05   0.00
ultra      nvidia/nemotron-3-ultra-550b-a55b:free   -                      0.05   0.05   0.00
"
# the test run each estimate is measured from
declare -A TESTED=([luna]=test_luna [terra]=test_terra [sol]=test_sol [flashlite]=test_flash_lite
                   [flash]=test_gemini_flash [argon]=test_argon [qwenflash]=test_qwen38_flash
                   [qwen27b]=test_qwen38_27b [lightning]=test_nemotron_lightning
                   [super]=test_nemotron_super [ultra]=test_nemotron_ultra)

credits() {
  "$PYTHON" -X utf8 -c "
import json, os, subprocess, urllib.request
k = os.environ.get('OPENROUTER_API_KEY') or subprocess.run(['powershell', '-NoProfile', '-Command',
    '[Environment]::GetEnvironmentVariable(\"OPENROUTER_API_KEY\",\"User\")'], capture_output=True, text=True).stdout.strip()
d = json.load(urllib.request.urlopen(urllib.request.Request('https://openrouter.ai/api/v1/key',
    headers={'Authorization': 'Bearer ' + k}), timeout=20))['data']
print(f\"OpenRouter key: \${d['limit_remaining']:.2f} left of \${d['limit']:g} (used \${d['usage']:.2f})\")"
}

DAILY_LIMIT_EXIT=3     # a run stopped by OpenRouter's daily free-model limit exits with this (pipeline/cli.py)
LOCKED_EXIT=4          # another run is writing that model's results file: this one did not start

run_model() {  # one model: its main run, then (once the main run is whole) its stability check
  local row name model provider cap check_cap _ status
  row=$(echo "$MODELS" | awk -v n="$1" '$1 == n')
  [ -n "$row" ] || { echo "unknown model '$1'; one of:" $(echo "$MODELS" | awk 'NF {print $1}'); return 1; }
  read -r name model provider cap check_cap _ <<< "$row"
  local prov=(); [ "$provider" != - ] && prov=(--provider "$provider")
  local common=(--model "$model" "${prov[@]}" --tally compact --max-tokens 32000 --resume
                --measured-from "results/${TESTED[$name]}.json")

  echo "=== $name: main run, 200 cases x low/medium/high -> $OUT/main_$name.json"
  status=0
  "$PYTHON" run_semantic_eval.py "${common[@]}" --pairs 100 --effort low medium high \
    --output "$OUT/main_$name.json" --max-cost "$cap" || status=$?
  if [ "$status" = "$DAILY_LIMIT_EXIT" ]; then
    echo "=== $name: stopped at the daily free-model limit; run this again after 00:00 UTC to continue"
    return "$status"
  fi
  if [ "$status" = "$LOCKED_EXIT" ]; then
    echo "=== $name: already running in another process (its own log has it); skipped here"
    return 0
  fi
  [ "$status" = 0 ] || return "$status"
  echo "=== $name: stability check, 10 pairs x 3 samples at medium -> $OUT/check_$name.json"
  "$PYTHON" run_semantic_eval.py "${common[@]}" --pairs 10 -n 3 --effort medium \
    --reuse-from "$OUT/main_$name.json" --output "$OUT/check_$name.json" --max-cost "$check_cap" || status=$?
  [ "$status" = "$DAILY_LIMIT_EXIT" ] && echo "=== $name: check stopped at the daily limit; run this again after 00:00 UTC"
  [ "$status" = "$LOCKED_EXIT" ] && { echo "=== $name: check already running in another process; skipped here"; status=0; }
  [ "$status" = 0 ] && echo "=== $name: finished"
  return "$status"
}

case "${1:-}" in
  ""|-h|--help) sed -n '2,25p' "$0"; exit 0 ;;
  list) echo "run order (name, model, endpoint, --max-cost main / check, estimate):"; echo "$MODELS" | sed '/^$/d'; exit 0 ;;
  credits) credits; exit 0 ;;
  free)    # the three free Nemotron models at once, each with its own log; run again daily until all finish
    mkdir -p "$OUT/logs"
    for n in lightning super ultra; do
      run_model "$n" >> "$OUT/logs/$n.log" 2>&1 &
      echo "started $n (log: $OUT/logs/$n.log)"
    done
    failed=0
    for n in lightning super ultra; do
      wait -n || failed=$((failed + 1))
    done
    for n in lightning super ultra; do echo "--- $n: $(grep -E '^=== ' "$OUT/logs/$n.log" | tail -1)"; done
    [ "$failed" = 0 ] && echo "all three finished" || echo "$failed not finished: run 'bash run_main.sh free' again after 00:00 UTC"
    exit 0 ;;
  parallel)  # the models named after it at once, each with its own log (e.g. parallel flash argon qwenflash)
    shift
    [ "$#" -gt 0 ] || { echo "name the models: bash run_main.sh parallel flashlite flash ..."; exit 1; }
    mkdir -p "$OUT/logs"
    credits
    for n in "$@"; do
      PYTHONUNBUFFERED=1 run_model "$n" >> "$OUT/logs/$n.parallel.log" 2>&1 &
      echo "started $n (log: $OUT/logs/$n.parallel.log)"
    done
    failed=""
    for n in "$@"; do wait -n || failed="$failed ?"; done
    for n in "$@"; do echo "--- $n: $(grep -E '^=== ' "$OUT/logs/$n.parallel.log" | tail -1)"; done
    credits
    exit 0 ;;
  paid)    # the eight paid models one after another, in run order, the credits printed before each
    mkdir -p "$OUT/logs"
    failed=""
    for n in luna terra sol flashlite flash argon qwenflash qwen27b; do
      credits
      run_model "$n" 2>&1 | tee "$OUT/logs/$n.log" || failed="$failed $n"
    done
    credits
    [ -z "$failed" ] && echo "all eight paid models finished" || echo "not finished:$failed (see $OUT/logs; run 'bash run_main.sh paid' again to continue)"
    exit 0 ;;
esac

mkdir -p "$OUT/logs"
credits
status=0
run_model "$1" || status=$?
credits
exit "$status"
