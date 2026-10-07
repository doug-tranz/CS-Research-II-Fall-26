# registration.py — registration domain testbed for the CART message (PROVISIONAL).
# Digests a DIDComm message; scores it on: syntactic + schema (the rules in
# didcomm_schema.py) → consistency (invariants) → semantic (intent, stubbed here;
# semantic_check.py fills it in). The single-section version is old_schema/registration.py.
import sqlite3
from didcomm_schema import validate_message   # Tiers 1-2: registration_message.schema.json

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
    student = body["student_id"]
    cart_course, cart_slot = {}, {}       # course / slot -> the cart section that holds it
    for section in body["sections"]:
        row = conn.execute(
            "SELECT course_id, seats_total, seats_taken, meets FROM sections WHERE section_id=?",
            (section,)).fetchone()
        if row is None:
            return False, f"section {section} does not exist"
        course, seats_total, seats_taken, meets = row
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
        # Invariants that only exist for a cart: its sections against each other.
        if course in cart_course:
            return False, f"two sections of {course} in the cart: {cart_course[course]} and {section}"
        if meets in cart_slot:
            return False, f"time conflict inside the cart: {cart_slot[meets]} and {section} at {meets}"
        cart_course[course], cart_slot[meets] = section, section
    return True, "consistent"

def evaluate(conn, raw_output: str) -> dict:
    # Tier 1: syntactic — is it a DIDComm plaintext message?
    # Tier 2: schema — is it OUR register message, with a well-formed cart?
    checked = validate_message(raw_output)
    if checked["outcome"] != "schema_ok":
        return {"outcome": checked["outcome"], "detail": checked["detail"]}
    # Tier 3: consistency — invariants
    ok, reason = check_consistency(conn, checked["body"])
    if not ok:
        return {"outcome": "consistency_error", "detail": reason}
    # Tier 4: semantic — STUB here; evaluate_with_semantics.py adds it on top
    return {"outcome": "consistent__semantic_TBD",
            "detail": "invariants hold; semantic/intent check not yet defined (§13.1)"}
