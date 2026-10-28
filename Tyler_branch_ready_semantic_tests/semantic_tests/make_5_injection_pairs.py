
import copy
import json
from pathlib import Path

SOURCE = Path("..") / ".." / "paired_injection_no_warning_20.jsonl"
OUTPUT = Path("..") / ".." / "title_injection_5_pairs.jsonl"

def read_jsonl(path):
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]

def candidate_title(case):
    meta = case["pair_metadata"]
    path = meta["only_input_field_changed_between_pair"]
    prefix = "schedule.sections[crn="
    suffix = "].title"

    if not path.startswith(prefix) or not path.endswith(suffix):
        raise ValueError(f"Unexpected candidate path: {path}")

    crn = path[len(prefix):-len(suffix)]
    matches = [
        s for s in case["input"]["schedule"]["sections"]
        if str(s["crn"]) == crn
    ]
    if len(matches) != 1:
        raise ValueError(f"{case['case_id']}: candidate CRN not unique")
    return matches[0]["title"]

def main():
    if not SOURCE.exists():
        raise FileNotFoundError(f"Source file not found: {SOURCE.resolve()}")

    cases = read_jsonl(SOURCE)
    pairs = {}

    for case in cases:
        meta = case.get("pair_metadata", {})
        pair_id = meta.get("pair_id")
        condition = meta.get("condition")
        if condition not in ("control", "attack") or not pair_id:
            raise ValueError(f"Unexpected case metadata: {case.get('case_id')}")
        pairs.setdefault(pair_id, {})[condition] = case

    complete = [
        (pid, p) for pid, p in pairs.items()
        if "control" in p and "attack" in p
    ]
    if len(complete) < 5:
        raise ValueError(f"Need 5 complete pairs; found {len(complete)}")

    output = []

    for n, (old_id, pair) in enumerate(complete[:5], start=1):
        control = copy.deepcopy(pair["control"])
        attack = copy.deepcopy(pair["attack"])

        c_meta = control["pair_metadata"]
        a_meta = attack["pair_metadata"]

        if c_meta["injection_candidate_crn"] != a_meta["injection_candidate_crn"]:
            raise ValueError(f"{old_id}: candidate CRNs do not match")

        # Verify that the title is the only input difference.
        c_input = copy.deepcopy(control["input"])
        a_input = copy.deepcopy(attack["input"])
        crn = str(c_meta["injection_candidate_crn"])

        c_sections = [
            s for s in c_input["schedule"]["sections"]
            if str(s["crn"]) == crn
        ]
        a_sections = [
            s for s in a_input["schedule"]["sections"]
            if str(s["crn"]) == crn
        ]
        if len(c_sections) != 1 or len(a_sections) != 1:
            raise ValueError(f"{old_id}: candidate section not unique")

        normal_title = c_sections[0]["title"]
        misleading_title = a_sections[0]["title"]
        a_sections[0]["title"] = normal_title

        if c_input != a_input or control.get("ground_truth") != attack.get("ground_truth"):
            raise ValueError(f"{old_id}: pair differs beyond candidate title or ground truth")

        new_id = f"FOOL-{n:02d}"
        for case, condition in (
            (control, "control"),
            (attack, "attack"),
        ):
            case["case_id"] = f"{new_id}-{condition.upper()}"
            case["pair_metadata"]["pair_id"] = new_id
            case["pair_metadata"]["condition"] = condition
            case["pair_metadata"]["source_pair_id"] = old_id

        print(f"\n{new_id} (source {old_id})")
        print(f"  Expected valid CRN: {c_meta['expected_valid_crn']}")
        print(f"  Candidate CRN:      {crn}")
        print(f"  Normal title:       {normal_title}")
        print(f"  Misleading title:   {misleading_title}")

        output.extend([control, attack])

    with OUTPUT.open("w", encoding="utf-8") as f:
        for case in output:
            f.write(json.dumps(case, ensure_ascii=False) + "\n")

    print(f"\nCreated {len(output)} cases: {OUTPUT.resolve()}")
    print("Review the printed CRNs and titles before running the model.")

if __name__ == "__main__":
    main()
