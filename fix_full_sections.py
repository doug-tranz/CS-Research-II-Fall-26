
import json
import shutil

path = r"hard50_batch.jsonl"
backup_path = path + ".bak"

# Back up the original before making changes.
shutil.copy2(path, backup_path)

with open(path, "r", encoding="utf-8") as file:
    rows = [json.loads(line) for line in file if line.strip()]

changed = 0

for row in rows:
    sections = (
        row.get("input", {})
        .get("schedule", {})
        .get("sections", [])
    )

    for section in sections:
        total = section.get("seats_total")
        taken = section.get("seats_taken")

        if (
            isinstance(total, int)
            and isinstance(taken, int)
            and total > 0
            and taken == total
        ):
            section["seats_taken"] = taken - 1
            changed += 1

with open(path, "w", encoding="utf-8") as file:
    for row in rows:
        file.write(json.dumps(row, ensure_ascii=False) + "\n")

print(f"Made {changed} full sections available.")
print(f"Backup saved as: {backup_path}")
