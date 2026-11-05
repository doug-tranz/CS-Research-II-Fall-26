#!/usr/bin/env bash
# The main study runs, one model per command, started on your call:
#
#   bash run_main.sh list          the models, in run order, with their estimates
#   bash run_main.sh credits       what is left on the OpenRouter key (free)
#   bash run_main.sh luna          one model: its main run, then its stability check
#   bash run_main.sh free          the three free Nemotron models at once (logs in results/logs/);
#                                  run it again each day after 00:00 UTC until all three finish
#
# Free models share OpenRouter's daily allowance (~1,000 requests). When it runs out the run stops
# at once (no failed rows) and exits 3; the same command after the reset continues the stopped
# session where it left off (--resume), so no request is spent twice.
#
# Main run: 200 cases (--pairs 100, every check 4 or 6 cases) at low, medium and high effort,
# one session each, into results/main_<name>.json. Stability check: 10 pairs (all seven
# groups) sampled 3 times at medium effort, reusing the main run's answers as the first sample,
# into results/check_<name>.json. Every run is live (no Batch API: its calls skip the cache).
#
# --max-cost is about 1.5x each measured estimate: a run whose estimate is above it does not
# start, and one that reaches it stops. Estimates come from the model's test run
# (--measured-from). If a run stops, run the same command again: --resume skips the efforts
# already finished and continues a cut-off one from its next call. Keep the computer awake and online.
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python}"   # the interpreter (a test can stand one in)

#      name       model                                    provider               main$  check$  estimate (3 efforts)
MODELS="
luna       openai/gpt-6-luna                        openai/flex            1.50   0.50   0.90
terra      openai/gpt-6.1-terra                     openai/flex            8.00   2.00   5.42
sol        openai/gpt-6.1-sol                       openai/flex            14.00  3.00   9.44
flashlite  google/gemini-3.5-flash-lite             google-ai-studio/flex  6.00   1.00   4.07
flash      google/gemini-3.8-flash                  google-ai-studio/flex  14.00  2.00   9.37
argon      google/gemini-4-argon                    google-ai-studio/flex  24.00  4.00   16.14
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

run_model() {  # one model: its main run, then (once the main run is whole) its stability check
  local row name model provider cap check_cap _ status
  row=$(echo "$MODELS" | awk -v n="$1" '$1 == n')
  [ -n "$row" ] || { echo "unknown model '$1'; one of:" $(echo "$MODELS" | awk 'NF {print $1}'); return 1; }
  read -r name model provider cap check_cap _ <<< "$row"
  local prov=(); [ "$provider" != - ] && prov=(--provider "$provider")
  local common=(--model "$model" "${prov[@]}" --tally compact --max-tokens 32000 --resume
                --measured-from "results/${TESTED[$name]}.json")

  echo "=== $name: main run, 200 cases x low/medium/high -> results/main_$name.json"
  status=0
  "$PYTHON" run_semantic_eval.py "${common[@]}" --pairs 100 --effort low medium high \
    --output "results/main_$name.json" --max-cost "$cap" || status=$?
  if [ "$status" = "$DAILY_LIMIT_EXIT" ]; then
    echo "=== $name: stopped at the daily free-model limit; run this again after 00:00 UTC to continue"
    return "$status"
  fi
  [ "$status" = 0 ] || return "$status"
  echo "=== $name: stability check, 10 pairs x 3 samples at medium -> results/check_$name.json"
  "$PYTHON" run_semantic_eval.py "${common[@]}" --pairs 10 -n 3 --effort medium \
    --reuse-from "results/main_$name.json" --output "results/check_$name.json" --max-cost "$check_cap" || status=$?
  [ "$status" = "$DAILY_LIMIT_EXIT" ] && echo "=== $name: check stopped at the daily limit; run this again after 00:00 UTC"
  return "$status"
}

case "${1:-}" in
  ""|-h|--help) sed -n '2,22p' "$0"; exit 0 ;;
  list) echo "run order (name, model, endpoint, --max-cost main / check, estimate):"; echo "$MODELS" | sed '/^$/d'; exit 0 ;;
  credits) credits; exit 0 ;;
  free)    # the three free Nemotron models at once, each with its own log; run again daily until all finish
    mkdir -p results/logs
    for n in lightning super ultra; do
      run_model "$n" > "results/logs/$n.log" 2>&1 &
      echo "started $n (log: results/logs/$n.log)"
    done
    failed=0
    for n in lightning super ultra; do
      wait -n || failed=$((failed + 1))
    done
    for n in lightning super ultra; do echo "--- $n: $(grep -E '^=== ' "results/logs/$n.log" | tail -1)"; done
    [ "$failed" = 0 ] && echo "all three finished" || echo "$failed not finished: run 'bash run_main.sh free' again after 00:00 UTC"
    exit 0 ;;
esac

credits
status=0
run_model "$1" || status=$?
credits
exit "$status"
