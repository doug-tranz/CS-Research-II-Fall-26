#!/usr/bin/env bash
# The main study runs, one model per command, started on your call:
#
#   bash run_main.sh list          the models, in run order, with their estimates
#   bash run_main.sh credits       what is left on the OpenRouter key (free)
#   bash run_main.sh luna          one model: its main run, then its stability check
#
# Main run: 200 cases (--pairs 100, every check 4 or 6 cases) at low, medium and high effort,
# one session each, into results/main_<name>.json. Stability check: 10 pairs (all seven
# groups) sampled 3 times at medium effort, reusing the main run's answers as the first sample,
# into results/check_<name>.json. Every run is live (no Batch API: its calls skip the cache).
#
# --max-cost is about 1.5x each measured estimate: a run whose estimate is above it does not
# start, and one that reaches it stops. Estimates come from the model's test run
# (--measured-from). If a run stops, run the same command again: --resume skips the efforts
# already finished (an effort cut off midway starts over). Keep the computer awake and online.
set -euo pipefail
cd "$(dirname "$0")"

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
  python -X utf8 -c "
import json, os, subprocess, urllib.request
k = os.environ.get('OPENROUTER_API_KEY') or subprocess.run(['powershell', '-NoProfile', '-Command',
    '[Environment]::GetEnvironmentVariable(\"OPENROUTER_API_KEY\",\"User\")'], capture_output=True, text=True).stdout.strip()
d = json.load(urllib.request.urlopen(urllib.request.Request('https://openrouter.ai/api/v1/key',
    headers={'Authorization': 'Bearer ' + k}), timeout=20))['data']
print(f\"OpenRouter key: \${d['limit_remaining']:.2f} left of \${d['limit']:g} (used \${d['usage']:.2f})\")"
}

case "${1:-}" in
  ""|-h|--help) sed -n '2,17p' "$0"; exit 0 ;;
  list) echo "run order (name, model, endpoint, --max-cost main / check, estimate):"; echo "$MODELS" | sed '/^$/d'; exit 0 ;;
  credits) credits; exit 0 ;;
esac

row=$(echo "$MODELS" | awk -v n="$1" '$1 == n')
[ -n "$row" ] || { echo "unknown model '$1'; one of:" $(echo "$MODELS" | awk 'NF {print $1}'); exit 1; }
read -r name model provider cap check_cap _ <<< "$row"
prov=(); [ "$provider" != - ] && prov=(--provider "$provider")
common=(--model "$model" "${prov[@]}" --tally compact --max-tokens 32000)

credits
echo "=== $name: main run, 200 cases x low/medium/high -> results/main_$name.json"
python run_semantic_eval.py "${common[@]}" --pairs 100 --effort low medium high --resume \
  --measured-from "results/${TESTED[$name]}.json" --output "results/main_$name.json" --max-cost "$cap"
echo "=== $name: stability check, 10 pairs x 3 samples at medium -> results/check_$name.json"
python run_semantic_eval.py "${common[@]}" --pairs 10 -n 3 --effort medium \
  --reuse-from "results/main_$name.json" --measured-from "results/${TESTED[$name]}.json" \
  --output "results/check_$name.json" --max-cost "$check_cap"
credits
