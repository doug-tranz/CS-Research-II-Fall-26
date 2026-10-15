# run_registration.py — end-to-end: generate a registration transaction, score it, log it.
# CART version (README.md).
import argparse, json
from harness import TaskSpec, OllamaBackend, OpenRouterBackend, generate, log_result
import registration as reg
from didcomm_schema import OUTPUT_CONTRACT

MODEL = "llama3.1:8b"
N     = 5   # samples per task — stochasticity (design §10)

# Known system state the transaction runs against
SEED = """
INSERT INTO sections VALUES
 ('CS565-01','CS00565',30,29,'MW14'),   -- 1 seat left, no conflict
 ('CS500-01','CS00500',30,30,'MW10'),   -- full
 ('CS520-01','CS00520',30,5, 'TR13');   -- student already enrolled here
INSERT INTO section_details VALUES
 ('CS565-01','1','Glassboro','in-person','[{"days": ["Monday", "Wednesday"], "start_time": "2:00 PM", "end_time": "3:15 PM"}]'),
 ('CS500-01','1','Glassboro','in-person','[{"days": ["Monday", "Wednesday"], "start_time": "10:00 AM", "end_time": "11:15 AM"}]'),
 ('CS520-01','1','Glassboro','in-person','[{"days": ["Tuesday", "Thursday"], "start_time": "1:00 PM", "end_time": "2:15 PM"}]');
INSERT INTO enrollments VALUES ('S001','CS520-01');
"""

# What the model sees: known state + a status-read + the intent
task = TaskSpec(
    task_id="reg_enroll_01",
    domain="registration",
    inputs=json.dumps({
        "term_code": "202640",
        "student": {"student_id": "S001",
                    "enrolled": [{"crn": "CS520-01", "course_id": "CS00520", "meets": "TR13"}]},
        "available_sections": [
            {"crn": "CS565-01", "course_id": "CS00565", "section": "1", "credit_hours": 3,
             "seats_total": 30, "seats_taken": 29, "campus": "Glassboro", "modality": "in-person",
             "meetings": [{"days": ["Monday", "Wednesday"], "start_time": "2:00 PM", "end_time": "3:15 PM"}]},
            {"crn": "CS500-01", "course_id": "CS00500", "section": "1", "credit_hours": 3,
             "seats_total": 30, "seats_taken": 30, "campus": "Glassboro", "modality": "in-person",
             "meetings": [{"days": ["Monday", "Wednesday"], "start_time": "10:00 AM", "end_time": "11:15 AM"}]},
        ],
    }),
    intent="Enroll me in CS00565.",
    # The contract's example uses a DIFFERENT student/sections than this task, so a
    # correct answer requires ADAPTING the shape — not copying the example (no answer leak).
    output_contract=OUTPUT_CONTRACT + " Pick sections from available_sections.",
)

def run(backend, model: str, n: int = N) -> None:
    print(f"== {backend.name} / {model}")
    for i in range(n):
        res = generate(backend, model, task, run_index=i)
        if not res.ok:
            print(f"run {i}: GENERATION FAILED: {res.error}")
            log_result(res)
            continue
        conn    = reg.make_db(SEED)          # fresh known state per run
        verdict = reg.evaluate(conn, res.raw_output)
        log_result(res, verdict)
        oneline = res.raw_output.replace(chr(10), " ")[:100]
        cost    = f" | ${res.usd_cost:.6f}" if res.usd_cost else ""
        print(f"run {i}: {verdict['outcome']:26s} | {verdict['detail']}{cost}")
        print(f"   raw: {oneline}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["ollama", "openrouter"], default="ollama")
    ap.add_argument("--models", nargs="+", default=[MODEL],
                    help="e.g. meta-llama/llama-3.1-8b-instruct openai/gpt-4o-mini")
    ap.add_argument("-n", type=int, default=N, help="samples per model")
    args = ap.parse_args()
    backend = OpenRouterBackend() if args.backend == "openrouter" else OllamaBackend()
    for model in args.models:
        run(backend, model, args.n)
