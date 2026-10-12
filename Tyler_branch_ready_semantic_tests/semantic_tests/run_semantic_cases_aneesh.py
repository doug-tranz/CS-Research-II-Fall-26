"""BIRD-inspired semantic benchmark adapter for the Aneesh-Anand branch.

Requires that this script sits beside that branch's `harness.py` and
`registration.py`. Reads JSONL cases using Daquan-1's registration input schema.
Does not modify either teammate's existing modules.

Usage:
    python run_semantic_cases_anesh.py --dry-run
    python run_semantic_cases_anesh.py --dry-run --schema registration_input.schema.json
    python run_semantic_cases_anesh.py --model llama3.1:8b --samples 3

The current Aneesh harness has Ollama implemented; OpenRouter is a stub there.
"""
import argparse
import json
from pathlib import Path
import sqlite3

from harness import TaskSpec, OllamaBackend, generate, log_result, parse_didcomm
import registration as reg

HERE = Path(__file__).resolve().parent


def load_cases(path):
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def validate_input_against_schema(case, schema):
    # The official JSON schema file is maintained by Daquan-1.
    if schema is not None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError as exc:
            raise SystemExit('Install jsonschema: python -m pip install jsonschema') from exc
        Draft202012Validator(schema).validate(case['input'])


def legacy_slot(section):
    """Bridge v0.3 structured meetings into provisional legacy slot code.

    This is intentionally NOT a time-overlap algorithm. It lets the existing
    exact-slot checker seed a DB while preserving seat/existence tests.
    """
    meetings = section['meetings']
    if not meetings:
        return 'ASYNC:' + section['crn']
    pieces=[]
    for m in meetings:
        pieces.append(','.join(m['days']) + '@' + m['start_time'] + '-' + m['end_time'])
    return '|'.join(sorted(pieces))


def make_db_from_v03(inp):
    conn=reg.make_db()
    for sec in inp['schedule']['sections']:
        conn.execute(
            'INSERT INTO sections (section_id, course_id, seats_total, seats_taken, meets) VALUES (?, ?, ?, ?, ?)',
            (sec['crn'], sec['course_id'], sec['seats_total'], sec['seats_taken'], legacy_slot(sec))
        )
    conn.commit()
    return conn


def didcomm_response(student,crn):
    return json.dumps({'type':'registration/enroll','id':'fixture-check-only',
                       'body':{'student_id':student,'section_id':crn}})


def score_against_ground_truth(conn, raw, case):
    """Aneesh's existing consistency checker + goal-based semantic tier.

    We use a set of acceptable CRNs, not exact JSON byte equality or IDs.
    """
    first=reg.evaluate(conn,raw)
    if first['outcome'] != 'consistent__semantic_TBD':
        return first

    body,error=parse_didcomm(raw)
    expected_student=case['input']['student']['student_id']
    acceptable=case['ground_truth']['acceptable_crns']
    if body['student_id'] != expected_student:
        return {'outcome':'semantic_error',
                'detail':f"wrong student: {body['student_id']!r}; expected {expected_student!r}"}
    if body['section_id'] not in acceptable:
        return {'outcome':'semantic_error',
                'detail':f"CRN {body['section_id']!r} does not satisfy intent; accepted CRNs: {acceptable}"}
    return {'outcome':'success','detail':'consistent; selected CRN meets hidden semantic goal'}


def smoke_check(cases, schema=None):
    """Validates each fixture: expected CRNs succeed; known wrong CRNs are
    DB-consistent but fail the semantic condition. No LLM/API call made.
    """
    found_ids=set()
    for case in cases:
        cid=case['case_id']
        assert cid not in found_ids, f'duplicate case_id {cid}'
        found_ids.add(cid)
        validate_input_against_schema(case,schema)
        inp=case['input']
        crns=[s['crn'] for s in inp['schedule']['sections']]
        assert len(set(crns))==len(crns),f'{cid}: duplicate CRN'
        assert all(0<=s['seats_taken']<=s['seats_total'] for s in inp['schedule']['sections'])
        acceptable=case['ground_truth']['acceptable_crns']
        wrongs=case['ground_truth']['wrong_but_consistent_crns']
        assert acceptable and wrongs and not(set(acceptable)&set(wrongs)), f'{cid}: gold sets invalid'
        for crn, outcome in [(c,'success') for c in acceptable] + [(c,'semantic_error') for c in wrongs]:
            assert crn in crns, f'{cid}: unknown CRN {crn}'
            conn=make_db_from_v03(inp)
            try:
                verdict=score_against_ground_truth(conn,didcomm_response(inp['student']['student_id'],crn),case)
            finally:
                conn.close()
            assert verdict['outcome']==outcome, f'{cid}: {crn} expected {outcome}, got {verdict}'
    print(f'PASS: {len(cases)} cases; expected CRNs succeed and wrong, DB-consistent CRNs fail semantics.')
    if schema is None:
        print('NOTE: JSON Schema validation not run. Pass --schema Daquan-1/registration_input.schema.json.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test-file',default=str(HERE/'semantic_cases_daquan_aneesh.jsonl'))
    parser.add_argument('--schema',default=None,help='path to Daquan-1 registration_input.schema.json')
    parser.add_argument('--dry-run',action='store_true',help='evaluate known outputs only; no model calls')
    parser.add_argument('--model',default='llama3.1:8b')
    parser.add_argument('--samples',type=int,default=1)
    parser.add_argument('--output',default=str(HERE/'semantic_benchmark_results.jsonl'))
    args=parser.parse_args()
    cases=load_cases(args.test_file)
    schema=json.loads(Path(args.schema).read_text(encoding='utf-8')) if args.schema else None
    smoke_check(cases,schema)
    if args.dry_run:
        return
    if args.samples<1:
        parser.error('--samples must be >= 1')
    backend=OllamaBackend()
    for case in cases:
        inp=case['input']
        task=TaskSpec(task_id=case['case_id'],domain='registration',
                      inputs=json.dumps(inp),intent=inp['prompt'],
                      output_contract=(
                        'Return one DIDComm registration/enroll message. '
                        'body must contain student_id and section_id. '
                        'For section_id, use the CRN string from schedule.sections[].crn, '
                        'not the display section number. Do not add an explanation.'
                      ))
        for i in range(args.samples):
            result=generate(backend,args.model,task,run_index=i)
            if result.ok:
                conn=make_db_from_v03(inp)
                try:
                    verdict=score_against_ground_truth(conn,result.raw_output,case)
                finally:
                    conn.close()
            else:
                verdict={'outcome':'generation_failed','detail':result.error}
            verdict['scenario_id']=case['scenario_id']
            verdict['semantic_dimension']=case['semantic_dimension']
            log_result(result,verdict=verdict,path=args.output)
            print(f"{case['case_id']} sample {i}: {verdict['outcome']}")
    print(f'Results appended to {args.output}')


if __name__=='__main__':
    main()
