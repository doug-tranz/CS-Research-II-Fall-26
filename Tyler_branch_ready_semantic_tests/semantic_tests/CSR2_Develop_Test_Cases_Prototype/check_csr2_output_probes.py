"""Check output-level negative tests against the CURRENT Tyler evaluator.

Place in semantic_tests beside run_semantic_cases_aneesh.py, harness.py,
registration.py, csr2_reasoning_cases_12.jsonl and
csr2_output_validation_probes.jsonl. No LLM or API required.
"""
from pathlib import Path
import json
from run_semantic_cases_aneesh import load_cases, make_db_from_v03, score_against_ground_truth

HERE=Path(__file__).resolve().parent
cases={r['case_id']:r for r in load_cases(HERE/'csr2_reasoning_cases_12.jsonl')}
probes=load_cases(HERE/'csr2_output_validation_probes.jsonl')
for p in probes:
    case=cases[p['case_id']]
    conn=make_db_from_v03(case['input'])
    try:
        outcome=score_against_ground_truth(conn,p['raw_output'],case)['outcome']
    finally:
        conn.close()
    expected=p['expected_current_outcome']
    print(f"{p['probe_id']}: {outcome} (expected current: {expected})")
    assert outcome==expected, f"Unexpected result for {p['probe_id']}: {outcome}"
    if p.get('desired_future_outcome'):
        print(f"  KNOWN GAP: stricter protocol check should return {p['desired_future_outcome']}")
print(f"PASS: {len(probes)} output validation probes match current evaluator behavior")
