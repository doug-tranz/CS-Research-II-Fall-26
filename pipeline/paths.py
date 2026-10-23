"""
paths.py
--------
Where the pipeline's files live, all anchored on the repository root, so a command works from
any directory:

    data/      the case file, the degree requirements, the Section Tally export
    schemas/   the input format and the register message, as JSON Schema
    results/   results files (new runs go to results/semantic_eval_results.json)
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA = ROOT / "data"
CASES = DATA / "paired_cases.json"                    # the test cases (generate_pairs.py writes it)
PROGRAMS = DATA / "degree_requirements.json"          # 50 Rowan degree programs
TALLY_CSV = DATA / "section_tally_fall2026.csv"       # the registrar's Section Tally, as downloaded

SCHEMAS = ROOT / "schemas"
INPUT_SCHEMA = SCHEMAS / "registration_input.schema.json"
MESSAGE_SCHEMA = SCHEMAS / "registration_message.schema.json"

RESULTS = ROOT / "results"
DEFAULT_OUTPUT = RESULTS / "semantic_eval_results.json"


def shown(path) -> str:
    """A path as recorded in a results file: relative to the repository when it is inside it
    (so a summary does not carry anyone's home directory), else as given."""
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)
