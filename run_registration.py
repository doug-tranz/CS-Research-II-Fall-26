# run_registration.py — end-to-end: generate a registration transaction, score it, log it.
import json
from harness import TaskSpec, OllamaBackend, generate, log_result
import registration as reg

MODEL = "llama3.1:8b"
N     = 5   # samples per task — stochasticity (design §10)

# Known system state the transaction runs against
SEED = """
INSERT INTO sections VALUES
 ('CS565-01','CS565',30,29,'MW14'),   -- 1 seat left, no conflict
 ('CS500-01','CS500',30,30,'MW10'),   -- full
 ('CS520-01','CS520',30,5, 'TR13');   -- student already enrolled here
INSERT INTO enrollments VALUES ('S001','CS520-01');
"""

# What the model sees: known state + a status-read + the intent
task = TaskSpec(
    task_id="reg_enroll_01",
    domain="registration",
    inputs=json.dumps({
        "student": {"student_id": "S001",
                    "enrolled": [{"section_id": "CS520-01", "meets": "TR13"}]},
        "available_sections": [
            {"section_id": "CS565-01", "seats_left": 1, "meets": "MW14"},
            {"section_id": "CS500-01", "seats_left": 0, "meets": "MW10"},
        ],
    }),
    intent="Enroll me in CS565.",
    output_contract=(
        'The body MUST have exactly these fields: '
        '{"student_id": <string>, "section_id": <string>}. '
        'Pick section_id from available_sections. '
        # Example uses a DIFFERENT student/section than this task, so a correct
        # answer requires ADAPTING the shape — not copying the example (no answer leak).
        'Example for an unrelated request: {"type": "registration/enroll", '
        '"id": "<uuid>", "body": {"student_id": "S042", "section_id": "MATH200-03"}}'
    ),
)

if __name__ == "__main__":
    for i in range(N):
        res = generate(OllamaBackend(), MODEL, task, run_index=i)
        if not res.ok:
            print(f"run {i}: GENERATION FAILED: {res.error}")
            log_result(res)
            continue
        conn    = reg.make_db(SEED)          # fresh known state per run
        verdict = reg.evaluate(conn, res.raw_output)
        log_result(res, verdict)
        oneline = res.raw_output.replace(chr(10), " ")[:100]
        print(f"run {i}: {verdict['outcome']:26s} | {verdict['detail']}")
        print(f"   raw: {oneline}")
