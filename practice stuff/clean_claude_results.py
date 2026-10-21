import json
from pathlib import Path

raw = Path("claude_results_raw.txt").read_text(encoding="utf-8")

# Replace curly double quotes with standard JSON quotes
raw = raw.replace("\u201c", '"').replace("\u201d", '"')

rows = []
for number, line in enumerate(raw.splitlines(), start=1):
    if not line.strip():
        continue
    try:
        rows.append(json.loads(line))
    except json.JSONDecodeError as error:
        print(f"Problem on line {number}: {error}")
        raise SystemExit(1)

expected_ids = (
    [f"REG-{i:03}" for i in range(1, 21)]
    + [f"VAL-{i:03}" for i in range(21, 26)]
)
actual_ids = [row.get("case_id") for row in rows]

if actual_ids != expected_ids:
    print("The case count, order, or IDs do not match the expected 25.")
    print("Found:", actual_ids)
    raise SystemExit(1)

Path("claude_results.jsonl").write_text(
    "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
    encoding="utf-8"
)

print("Success! All 25 records are valid JSONL.")
print("Saved as claude_results.jsonl")
