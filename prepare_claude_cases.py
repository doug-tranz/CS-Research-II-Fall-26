import json

source_file = "registration_format_cases.jsonl"
output_file = "registration_format_cases_for_claude.jsonl"

count = 0

with open(source_file, "r", encoding="utf-8") as source, \
     open(output_file, "w", encoding="utf-8") as output:

    for line in source:
        if not line.strip():
            continue

        case = json.loads(line)

        # Send only the case ID and test input.
        # Do not reveal expected answers to Claude.
        clean_case = {
            "case_id": case["case_id"],
            "input": case["input"]
        }

        output.write(json.dumps(clean_case) + "\n")
        count += 1

print(f"Prepared {count} test cases.")
print(f"Output file: {output_file}")
