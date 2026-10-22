"""
registration.py: compatibility shim. The code that lived here is now in three modules:

    checker.py        Tiers 1-3 and the checker database (evaluate, seed_db, consistency_checks)
    input_format.py   validate_input(), minutes()
    audit.py          the degree audit (audit, counts_toward, missing_prerequisites, ...)

New code imports those directly. This file keeps `import registration as reg` working for
older scripts.
"""
from audit import (GRADE_ORDER, GRADE_POINTS, audit, canonical, catalog_attributes,  # noqa: F401
                   counts_toward, in_range, matches, meets_minimum, missing_prerequisites, waived)
from checker import CONSISTENT, consistency_checks, evaluate, make_db, seed_db       # noqa: F401
from input_format import CREDIT_RULES, INPUT_SCHEMA, minutes, validate_input         # noqa: F401

_canonical, _matches = canonical, matches          # the old private names
