# registration.py — registration domain testbed (PROVISIONAL).
# Digests a DIDComm message; scores it on: schema (does the body match our
# required shape?) → consistency (invariants) → semantic (intent, stubbed §13.1).
import sqlite3
from harness import parse_didcomm     # DIDComm envelope parsing is shared

REQUIRED_BODY = {"student_id", "section_id"}   # the enroll message body contract

SCHEMA = """
CREATE TABLE sections (
    section_id  TEXT PRIMARY KEY,
    course_id   TEXT NOT NULL,
    seats_total INTEGER NOT NULL,
    seats_taken INTEGER NOT NULL,
    meets       TEXT NOT NULL          -- provisional slot code, e.g. "MW14", "TR13"
);
CREATE TABLE enrollments (
    student_id TEXT NOT NULL,
    section_id TEXT NOT NULL,
    PRIMARY KEY (student_id, section_id),
    FOREIGN KEY (section_id) REFERENCES sections(section_id)
);
"""

def make_db(seed_sql: str = "") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    if seed_sql:
        conn.executescript(seed_sql)
    conn.commit()
    return conn

def check_consistency(conn, body: dict):
    """Returns (ok, reason). Assumes body already passed schema validation."""
    student, section = body["student_id"], body["section_id"]
    row = conn.execute(
        "SELECT seats_total, seats_taken, meets FROM sections WHERE section_id=?",
        (section,)).fetchone()
    if row is None:
        return False, f"section {section} does not exist"
    seats_total, seats_taken, meets = row
    if conn.execute("SELECT 1 FROM enrollments WHERE student_id=? AND section_id=?",
                    (student, section)).fetchone():
        return False, f"already enrolled in {section}"
    if seats_taken >= seats_total:
        return False, f"section {section} is full ({seats_taken}/{seats_total})"
    conflict = conn.execute("""
        SELECT e.section_id FROM enrollments e
        JOIN sections s ON s.section_id = e.section_id
        WHERE e.student_id=? AND s.meets=?
    """, (student, meets)).fetchone()
    if conflict:
        return False, f"time conflict with {conflict[0]} at {meets}"
    return True, "consistent"

def evaluate(conn, raw_output: str) -> dict:
    # Tier 1: syntactic — is it valid JSON with a DIDComm body?
    body, syn_err = parse_didcomm(raw_output)
    if body is None:
        return {"outcome": "syntactic_error", "detail": syn_err}
    # Tier 2: schema — does the body match OUR required message shape?
    missing = REQUIRED_BODY - set(body.keys())
    if missing:
        return {"outcome": "schema_error",
                "detail": f"body missing required fields: {sorted(missing)}"}
    # Tier 3: consistency — invariants
    ok, reason = check_consistency(conn, body)
    if not ok:
        return {"outcome": "consistency_error", "detail": reason}
    # Tier 4: semantic — STUB, blocked on §13.1
    return {"outcome": "consistent__semantic_TBD",
            "detail": "invariants hold; semantic/intent check not yet defined (§13.1)"}
