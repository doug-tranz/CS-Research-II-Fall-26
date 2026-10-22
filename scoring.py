"""
scoring.py
-----------
All four tiers for one model answer to one test case, in one place:

  Tiers 1-2  syntactic + schema   didcomm_schema.validate_message()  } via
  Tier 3     consistency          checker.consistency_checks()       } checker.evaluate()
  Tier 4     semantic             check_rules(), against the case's answer key

evaluate_case() returns the verdict: the outcome, every check made per tier, why a failure
failed in words, and an error_type for counting failures by kind. self_check() scores a
case's own answer key with no model.
"""
import re

import audit
from cases import expected_message, validate_case
from checker import CONSISTENT, seed_db
from checker import evaluate as checker_evaluate
from harness import parse_didcomm
from input_format import minutes


# ── Tier 4: semantic check ──
# Tier 3 only checks invariants: does each section exist, is there a seat, no time
# conflict, prerequisites met. It never checks whether the model chose the RIGHT sections
# for what the intent asked. A cart can pass every consistency check and still be WRONG;
# check_rules() (below) catches that against the case's answer key.

def _normalize(value):
    """Case- and whitespace-insensitive."""
    return str(value).strip().lower()


def _bounds_text(bounds: dict, unit: str) -> str:
    """{"min": 1, "max": 1} -> 'exactly 1 section'; an open end reads 'at least' / 'at most'."""
    low, high = bounds.get("min"), bounds.get("max")
    plural = lambda n: f"{n:g} {unit}{'' if n == 1 else 's'}"
    if low is not None and low == high:
        return f"exactly {plural(low)}"
    if low is not None and high is not None:
        return f"{low:g} to {plural(high)}"
    return f"at least {plural(low)}" if low is not None else f"at most {plural(high)}"


def _rule_meeting(crn: str, m: dict) -> str:
    """'43945 Mon/Wed 9:30 AM-10:45 AM', or '43945 no set times'."""
    days = "/".join(d[:3] for d in m["days"])
    return f"{crn} {days} {m['start_time']}-{m['end_time']}" if "start_time" in m else f"{crn} no set times"


# ── Why a Tier 4 check failed, in words ──

def _section_name(s: dict) -> str:
    """'40961 (PHIL09120 Ethics)': a section's crn, course and title."""
    title = (s.get("title") or "").strip().title()
    return f"{s['crn']} ({s['course_id']}{' ' + title if title else ''})"


def _req_name(data: dict, req_id: str) -> str:
    """'Logic Requirement (PHIL-LOGIC)': a requirement's title, else its category."""
    r = next((r for r in data["program_requirements"] if r["req_id"] == req_id), None)
    label = (r.get("title") or r["category"].split(": ", 1)[-1]) if r else req_id
    return f"{label} ({req_id})"


def _names(items) -> str:
    """'A', 'A and B', 'A, B and C'."""
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else "none"


def _not_counting_why(data: dict, s: dict, wanted: set, status: dict, counts_toward=audit.counts_toward) -> str:
    """Why a section's course does not move any wanted requirement forward."""
    canon = audit.canonical(data)
    course = canon(s["course_id"])
    toward = counts_toward(data, s["course_id"], s.get("attributes", []))
    taken = [c for c in data["transcript"]["completed_courses"] + data["transcript"]["transfer_courses"]
             if canon(c["course_id"]) == course]
    parts = [f"{_section_name(s)} counts toward "
             + (_names(_req_name(data, r) for r in toward) if toward else "no unfinished requirement")
             + f", not {_names(_req_name(data, r) for r in sorted(wanted))}."]
    reqs = [r for r in data["program_requirements"] if r["req_id"] in wanted]
    attrs = audit.catalog_attributes(data, s["course_id"]) | set(s.get("attributes", []))
    done = [r["req_id"] for r in data["program_requirements"]
            if r["req_id"] not in wanted and r["req_id"] not in toward and status.get(r["req_id"], {}).get("satisfied")
            and r["rule"] != "credits_any" and audit.matches(r, course, attrs, canon)]
    if done:
        parts.append(f"It would have counted toward {_names(_req_name(data, r) for r in done)}, but "
                     f"{'that requirement is' if len(done) == 1 else 'those requirements are'} already complete "
                     f"on the transcript, so it no longer helps.")
    if not any(audit.matches(r, course, attrs, canon) for r in reqs):
        parts.append(f"{s['course_id']} is not one of the courses {_names(sorted(wanted))} "
                     f"{'accepts' if len(wanted) == 1 else 'accept'}.")
    elif taken:
        grades = ", ".join(c["grade"] for c in taken)
        parts.append(f"It would fit, but the transcript already has {s['course_id']} ({grades}) and it is not repeatable, "
                     f"so taking it again earns nothing more.")
    met = [r for r in sorted(wanted) if status.get(r, {}).get("satisfied")]
    if met:
        parts.append(f"{_names(_req_name(data, r) for r in met)} is already complete on the transcript.")
    return " ".join(parts)


def _would_count(data: dict, wanted: set, offered: set, limit: int = 5, counts_toward=audit.counts_toward) -> list:
    """Up to `limit` offered courses the wanted requirements name that would count toward them."""
    canon = audit.canonical(data)
    named = [c for r in data["program_requirements"] if r["req_id"] in wanted for c in r["course_options"]]
    out = []
    for c in dict.fromkeys(named):
        if canon(c) in offered and wanted & set(counts_toward(data, c)):
            out.append(c)
        if len(out) >= limit:
            break
    return out


def _busy_overlap(m: dict, b: dict) -> int:
    """Minutes a meeting overlaps a commitment on one shared day (0 when they do not overlap)."""
    if "start_time" not in m or not set(m["days"]) & set(b["days"]):
        return 0
    start = max(minutes(m["start_time"]), minutes(b["start_time"]))
    end = min(minutes(m["end_time"]), minutes(b["end_time"]))
    return max(0, end - start)


def intended(case: dict) -> str:
    """The case's intended reasoning: the start of its implicit_relationship, before the list of
    correct sections the generator appends."""
    text = case["ground_truth"].get("implicit_relationship", "")
    return re.split(r" \d+ correct (?:section|cart)", text)[0].strip()


def check_rules(body: dict, case: dict, counts_toward=audit.counts_toward) -> dict:
    """Tier 4 for a rules answer key. Assumes Tiers 1-3 passed, so every crn exists. The
    verdict's tier4_checks lists every check made, in order: the rule, what it requires in
    words, what the cart has, and whether it passed; a failed check also says `why`, in words:
    what the model chose, why that breaks the rule, and what would have satisfied it.
    counts_toward: audit.counts_toward, or a cached stand-in with the same signature (the case
    generator scores many carts against one case)."""
    data, rules = case["input"], case["ground_truth"]["rules"]
    by_crn = {s["crn"]: s for s in data["schedule"]["sections"]}
    chosen = [(item, by_crn[item["crn"]]) for item in body["sections"]]
    meetings = [(item["crn"], m) for item, s in chosen for m in s["meetings"]]
    times = "; ".join(_rule_meeting(crn, m) for crn, m in meetings) or "no meetings"
    violated, checks = {}, []

    def check(rule, requires, found, broken, why=None):
        """Records one check; `broken` is the failure detail, or None when it passed; `why`
        explains a failure in words."""
        entry = {"rule": rule, "requires": requires, "found": found, "passed": broken is None}
        if broken is not None:
            entry["why"] = why or broken
            violated[rule] = broken
        checks.append(entry)

    sid, term = data["student"]["student_id"], data["schedule"]["term_code"]
    check("student_id", f"the message is for student {sid}", f"student_id {body.get('student_id')!r}",
          None if _normalize(body.get("student_id")) == _normalize(sid) else f"expected {sid}",
          f"The message registers {body.get('student_id')!r}, but the request is from student {sid}, so it would "
          f"enroll the wrong person.")
    check("term_code", f"the message is for term {term}", f"term_code {body.get('term_code')!r}",
          None if body.get("term_code") == term else f"expected {term}",
          f"The message is for term {body.get('term_code')!r}, but registration is for term {term}.")
    names = _names(_section_name(s) for _, s in chosen)
    if "count" in rules:
        low, high = rules["count"].get("min", 0), rules["count"].get("max", 10 ** 6)
        many = len(chosen) > high
        check("count", f"{_bounds_text(rules['count'], 'section')} in the cart",
              f"{len(chosen)}: {', '.join(item['crn'] for item, _ in chosen) or 'none'}",
              None if low <= len(chosen) <= high else f"{len(chosen)} sections, wanted {rules['count']}",
              f"The request calls for {_bounds_text(rules['count'], 'section')}, but the cart has {len(chosen)} "
              f"({names}). " + ("Registering for more than was asked adds classes the student did not request."
                                if many else "The cart leaves out a class the request asked for."))
    if "credits" in rules:
        # an entry's own credit_hours (Tier 3 has checked it is allowed), else the section's
        # credits (the low end of a variable-credit range)
        each = [(item["crn"], item.get("credit_hours", s["credit_hours"]["min"] if isinstance(s["credit_hours"], dict)
                                       else s["credit_hours"])) for item, s in chosen]
        total = sum(c for _, c in each)
        low, high = rules["credits"].get("min", 0), rules["credits"].get("max", 10 ** 6)
        over = total - high if total > high else 0
        check("credits", f"{_bounds_text(rules['credits'], 'credit')} in all",
              f"{total:g} credits ({' + '.join(f'{crn}: {c:g}' for crn, c in each)})",
              None if low <= total <= high else f"{total:g} credits, wanted {rules['credits']}",
              f"The cart adds up to {total:g} credits ({' + '.join(f'{crn} {c:g}' for crn, c in each)}), "
              + (f"{over:g} over the student's limit of {high:g}; a section that only fit if both did should "
                 f"have been left out." if over else f"short of the {low:g} the request needs."))
    if "courses_count_toward" in rules:
        wanted = set(rules["courses_count_toward"])
        helps = [(s, counts_toward(data, s["course_id"], s.get("attributes", []))) for _, s in chosen]
        no = [s for s, toward in helps if not wanted & set(toward)]
        why = None
        if no:
            status = audit.audit(data)["requirements"]
            offered = {s["course_id"] for s in data["schedule"]["sections"]}
            good = _would_count(data, wanted, offered, counts_toward=counts_toward)
            why = (" ".join(_not_counting_why(data, s, wanted, status, counts_toward) for s in no)
                   + (f" Offered courses that would count: {', '.join(good)}." if good else ""))
        check("courses_count_toward",
              f"every course moves one of these unfinished requirements forward: {', '.join(sorted(wanted))}",
              "; ".join(f"{s['crn']} ({s['course_id']}) counts toward {', '.join(toward) or 'no unfinished requirement'}"
                        for s, toward in helps),
              f"{[s['crn'] for s in no]} do not move {sorted(wanted)} forward" if no else None, why)
    if "no_days" in rules:
        week = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        in_week = lambda days: [d for d in week if d in days]
        bad = sorted({crn for crn, m in meetings if set(m["days"]) & set(rules["no_days"])})
        hits = [f"{_rule_meeting(crn, m)} meets on {_names(in_week(set(m['days']) & set(rules['no_days'])))}"
                for crn, m in meetings if set(m["days"]) & set(rules["no_days"])]
        allowed = [d for d in week if d not in rules["no_days"]]
        rule_text = (f"the student can only come {_names(d + 's' for d in allowed)}" if len(allowed) <= 3
                     else f"the request rules out {_names(d + 's' for d in in_week(rules['no_days']))}")
        check("no_days", f"no meeting on {', '.join(rules['no_days'])}", times,
              f"{bad} meet on {rules['no_days']}" if bad else None,
              f"{'; '.join(hits)}, but {rule_text}. A section meeting only on allowed days was needed.")
    if "starts_before" in rules:
        limit = minutes(rules["starts_before"])
        late = [(crn, m) for crn, m in meetings if "start_time" in m and minutes(m["start_time"]) >= limit]
        check("starts_before", f"every meeting starts before {rules['starts_before']}", times,
              f"{sorted({c for c, _ in late})} start at or after {rules['starts_before']}" if late else None,
              "; ".join(f"{_rule_meeting(crn, m)} starts at {m['start_time']}" for crn, m in late)
              + f", but the request wants nothing starting at {rules['starts_before']} or later.")
    if "ends_by" in rules:
        limit = minutes(rules["ends_by"])
        late = [(crn, m) for crn, m in meetings if "end_time" in m and minutes(m["end_time"]) > limit]
        check("ends_by", f"every meeting ends by {rules['ends_by']}", times,
              f"{sorted({c for c, _ in late})} end after {rules['ends_by']}" if late else None,
              "; ".join(f"{_rule_meeting(crn, m)} ends at {m['end_time']}, "
                        f"{minutes(m['end_time']) - limit} minutes past {rules['ends_by']}" for crn, m in late)
              + ". The request rules out anything ending later than that.")
    if "modality_in" in rules:
        wrong = [s for _, s in chosen if s["modality"] not in rules["modality_in"]]
        check("modality_in", f"every section is {' or '.join(rules['modality_in'])}",
              "; ".join(f"{s['crn']} {s['modality']}" for _, s in chosen),
              f"{[s['crn'] for s in wrong]} are not {rules['modality_in']}" if wrong else None,
              "; ".join(f"{_section_name(s)} is {s['modality']}" for s in wrong)
              + f", but the request asks for {' or '.join(rules['modality_in'])} only. "
              f"A different section of the same course with that modality was needed.")
    bad = [item["crn"] for item, _ in chosen if item.get("waitlist")]
    check("waitlist", "no section is waitlisted (no request asks for a waitlist)",
          f"waitlisted: {', '.join(bad)}" if bad else "nothing waitlisted",
          f"{bad} waitlisted, but the student did not ask to be waitlisted" if bad else None,
          f"The message joins the waitlist of {_names(bad)}. The request never asked for a waitlist, so a "
          f"section with an open seat (or the request's fallback) was wanted instead.")
    courses = {s["course_id"] for _, s in chosen}
    if "must_include_courses" in rules:
        missing = sorted(set(rules["must_include_courses"]) - courses)
        offered = {}
        for s in data["schedule"]["sections"]:
            offered.setdefault(s["course_id"], []).append(s)
        check("must_include_courses", f"the cart contains {', '.join(rules['must_include_courses'])}",
              f"courses: {', '.join(sorted(courses)) or 'none'}", f"missing {missing}" if missing else None,
              f"The request names {_names(missing)}, but the cart has no section of "
              f"{'it' if len(missing) == 1 else 'them'} (it has {_names(sorted(courses))}). "
              + " ".join(f"{c} has {sum(1 for s in offered.get(c, []) if s['seats_taken'] < s['seats_total'] and not s.get('cancelled'))} "
                         f"open section(s) this term." for c in missing))
    if "courses_in" in rules:
        extra = sorted(courses - set(rules["courses_in"]))
        check("courses_in", f"every course is one of {', '.join(rules['courses_in'])}",
              f"courses: {', '.join(sorted(courses)) or 'none'}",
              f"{extra} are not among {rules['courses_in']}" if extra else None,
              f"The cart takes {_names(_section_name(s) for _, s in chosen if s['course_id'] in extra)}, but for "
              f"this student only {' or '.join(rules['courses_in'])} is right (see why_wrong for the reason). "
              f"The model chose a course the request mentions or allows in general, without applying the "
              f"condition that rules it out here.")
    if "busy" in rules:
        # an overlap is a shared day with intersecting time ranges; touching end to start is not one
        hits = [(crn, m, b, _busy_overlap(m, b)) for crn, m in meetings for b in rules["busy"] if _busy_overlap(m, b)]
        blocks = "; ".join(f"{'/'.join(d[:3] for d in b['days'])} {b['start_time']}-{b['end_time']}"
                           for b in rules["busy"])
        check("busy", f"no meeting overlaps the student's fixed commitments ({blocks})", times,
              f"{sorted({h[0] for h in hits})} overlap the student's fixed commitments" if hits else None,
              "; ".join(f"{_rule_meeting(crn, m)} overlaps the commitment "
                        f"{'/'.join(d[:3] for d in b['days'])} {b['start_time']}-{b['end_time']} by {mins} minutes on "
                        f"{_names(sorted(set(m['days']) & set(b['days'])))}" for crn, m, b, mins in hits)
              + ". The times do not have to match exactly: any shared minute is a clash.")

    # deviation_score: the share of the checks this cart broke (0 = every rule held). The checks are
    # the key's rules plus student_id, term_code and waitlist.
    checked = len(rules) + 3
    if violated:
        failed = [c for c in checks if not c["passed"]]
        why_wrong = (f"{len(failed)} of {len(checks)} Tier 4 checks failed ({_names(c['rule'] for c in failed)}). "
                     f"What the request needed: {intended(case)}")
        return {"outcome": "semantic_error", "detail": f"rules broken: {violated}",
                "deviation_score": len(violated) / checked, "tier4_checks": checks, "why_wrong": why_wrong}
    return {"outcome": "semantic_ok", "detail": "satisfies every rule", "deviation_score": 0.0,
            "tier4_checks": checks}


def evaluate_case(case: dict, raw_output: str, conn=None) -> dict:
    """Tiers 1-4 for a test case: Tiers 1-3 (checker.evaluate()), then, only when they
    pass, the answer key's rules. `conn` may be the case's database from an earlier call:
    scoring only reads it."""
    conn = conn or seed_db(case["input"])
    base = checker_evaluate(conn, raw_output)
    if base["outcome"] != CONSISTENT:
        verdict = dict(base)         # failed at Tier 1-3: keep that verdict as it is
    else:
        body, _ = parse_didcomm(raw_output)
        verdict = {**check_rules(body, case), **{k: base[k] for k in ("tier1_checks", "tier2_checks", "tier3_checks")}}
    tag = error_type(verdict, case)
    if tag:
        verdict["error_type"] = tag
    return _with_tiers(verdict)


# What a semantic error in a pair family means: the family names the reasoning the case tests.
FAMILY_ERRORS = {"full_fallback": "fallback_not_applied", "in_progress": "in_progress_course_ignored",
                 "transfer": "transfer_credit_ignored", "busy_block": "time_overlap_missed",
                 "nested_groups": "and_or_requirement_misread", "credit_limit": "credit_arithmetic",
                 "modality": "modality_ignored", "days": "meeting_days_ignored",
                 "grade_threshold": "minimum_grade_misread", "withdrawal": "withdrawal_counted_as_credit"}
# Otherwise a semantic error is named by its first failed rule, a consistency error by its first
# failed Tier 3 check, and a format error by its first failed Tier 1-2 check.
RULE_ERRORS = {"student_id": "wrong_student", "term_code": "wrong_term", "count": "wrong_number_of_sections",
               "credits": "credit_arithmetic", "courses_count_toward": "requirement_mismatch",
               "no_days": "meeting_days_ignored", "starts_before": "time_limit_ignored",
               "ends_by": "time_limit_ignored", "modality_in": "modality_ignored", "waitlist": "unrequested_waitlist",
               "must_include_courses": "named_course_missing", "courses_in": "wrong_course", "busy": "time_overlap_missed"}
CHECK_ERRORS = {"term": "wrong_term", "exists": "nonexistent_section", "copied_fields": "section_details_miscopied",
                "not_cancelled": "cancelled_section", "seat": "full_section", "waitlist": "invalid_waitlist",
                "credits": "invalid_credit_hours", "prerequisites": "prerequisite_missing",
                "one_section_per_course": "two_sections_of_one_course", "no_time_conflict": "time_conflict_in_cart",
                "json": "not_json", "envelope": "not_didcomm", "type": "wrong_message_type",
                "body": "malformed_body", "sections": "malformed_section_entry", "unique_crns": "duplicate_crn",
                "other": "schema_violation"}


def error_type(verdict: dict, case: dict) -> str | None:
    """A short tag for what went wrong, for counting failures by kind; None when correct. A
    semantic error in a case whose type tests one kind of reasoning is named for it (a failed
    transfer case is transfer_credit_ignored, unless only the student id or term was wrong)."""
    outcome = verdict["outcome"]
    if outcome == "semantic_ok":
        return None
    if outcome == "generation_failed":
        return "call_failed"
    if outcome == "semantic_error":
        failed = [c["rule"] for c in verdict.get("tier4_checks", []) if not c["passed"]]
        kind = (case.get("ground_truth") or {}).get("case_type")
        if kind in FAMILY_ERRORS and set(failed) - {"student_id", "term_code"}:
            return FAMILY_ERRORS[kind]
        return RULE_ERRORS.get(failed[0], failed[0]) if failed else "semantic_error"
    tier = {"syntactic_error": 1, "schema_error": 2, "consistency_error": 3}.get(outcome)
    failed = [c["check"] for c in verdict.get(f"tier{tier}_checks", []) if not c["passed"]] if tier else []
    return CHECK_ERRORS.get(failed[0], failed[0]) if failed else outcome


def call_error_type(verdict: dict, finish_reason: str | None, native_finish_reason: str | None) -> str | None:
    """What the call's stop reason says about a failure, when it explains it better than the
    verdict: an answer cut off at max_tokens, or a refusal."""
    if verdict["outcome"] == "semantic_ok":
        return None
    if native_finish_reason == "refusal" or finish_reason == "content_filter":
        return "refused"
    if finish_reason == "length" and verdict["outcome"] in ("syntactic_error", "schema_error"):
        return "truncated_at_max_tokens"
    return None


# The tier each failing outcome stops at; semantic_ok passes all four.
FAILED_AT = {"syntactic_error": 1, "schema_error": 2, "consistency_error": 3, "semantic_error": 4}
TIER_NAMES = {1: "syntactic (JSON, DIDComm envelope)", 2: "schema (our register message)",
              3: "consistency (invariants against the schedule and transcript)", 4: "semantic (the request's rules)"}


# A verdict's keys in the order grading happens: the result, the per-tier overview, then each
# tier's own fields from Tier 1 to Tier 4, and Tier 4's score last.
VERDICT_ORDER = ("outcome", "detail", "error_type", "why_wrong", "tiers", "tier1_checks", "tier2_checks", "tier3_checks",
                 "tier4_checks", "deviation_score")


def _with_tiers(verdict: dict) -> dict:
    """The verdict with a `tiers` overview (what each tier checks, and passed / failed / not
    reached with its check count), its keys in VERDICT_ORDER (any other key after them)."""
    stop = FAILED_AT.get(verdict["outcome"], 5)
    tiers = {}
    for n, name in TIER_NAMES.items():
        checks = verdict.get(f"tier{n}_checks", [])
        state = "failed" if n == stop else "passed" if n < stop else "not reached"
        counts = f" ({sum(c['passed'] for c in checks)}/{len(checks)} checks passed)" if checks else ""
        tiers[f"tier{n}"] = f"{name}: {state}{counts}"
    full = {**verdict, "tiers": tiers, **{f"tier{n}_checks": verdict.get(f"tier{n}_checks", []) for n in TIER_NAMES}}
    ordered = {k: full[k] for k in VERDICT_ORDER if k in full}
    return {**ordered, **{k: v for k, v in full.items() if k not in ordered}}

def self_check(case: dict) -> list:
    """Scores a case's own answer key, with no model. Returns problems; empty means it behaves."""
    problems = validate_case(case)
    if problems:
        return problems
    truth = case["ground_truth"]
    conn = seed_db(case["input"])                  # one database for every example
    for cart in truth.get("examples", {}).get("valid", []):
        verdict = evaluate_case(case, expected_message(case, cart), conn)
        if verdict["outcome"] != "semantic_ok":
            problems.append(f"valid example {cart} scores {verdict['outcome']}: {verdict['detail']}")
    for cart in truth.get("examples", {}).get("invalid", []):
        if evaluate_case(case, expected_message(case, cart), conn)["outcome"] == "semantic_ok":
            problems.append(f"invalid example {cart} scores semantic_ok")
    return problems
