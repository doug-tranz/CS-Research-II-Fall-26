"""
degree_audit.py
---------------
The degree audit over an input object: which program requirements and graduation rules the
student's transcript already satisfies, which requirements an offered course would move
forward, and which prerequisites the student has not met.

Used in three places:
  - checker.seed_db():          unmet prerequisites become a Tier 3 consistency rule
  - scoring.check_rules():      the courses_count_toward rule (Tier 4) and its explanations
  - fabricate.py:               a fabricated transcript is checked against the audit

Spec open items filled in here are marked PROVISIONAL (README.md, "How the spec's open items
were filled in").
"""

GRADE_ORDER = ["A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-"]   # highest to lowest
GRADE_POINTS = dict(zip(GRADE_ORDER, [4.0, 3.7, 3.3, 3.0, 2.7, 2.3, 2.0, 1.7, 1.3, 1.0, 0.7]), F=0.0)


# ── Grades ──

def meets_minimum(grade: str, minimum: str) -> bool:
    """Is `grade` at or above `minimum`? PROVISIONAL (spec open item 5): accepted
    transfer credit (`TA`) meets every minimum. `F` and `W` meet none."""
    if grade == "TA":
        return True
    return grade in GRADE_ORDER and GRADE_ORDER.index(grade) <= GRADE_ORDER.index(minimum)


def _rank(grade: str) -> int:
    """Sort key for grades: lower is better; TA ranks with the best, anything unknown last."""
    return -1 if grade == "TA" else GRADE_ORDER.index(grade) if grade in GRADE_ORDER else 99


# ── Courses: equivalents, attributes, ranges ──

def canonical(data: dict):
    """A function mapping a course id to one id per course_equivalents group (spec open item 6),
    so 'CS04103' and its renamed twin count as the same course everywhere."""
    same = {c: group[0] for group in data.get("course_equivalents", []) for c in group}
    return lambda course_id: same.get(course_id, course_id)


def catalog_attributes(data: dict, course_id: str) -> set:
    """A course's Rowan Core / Rowan Experience designations from `course_attributes`,
    looked up under its own id and under any equivalent id."""
    canon, table = canonical(data), data.get("course_attributes", {})
    return {a for listed, attrs in table.items() if canon(listed) == canon(course_id) for a in attrs}


def in_range(course: str, course_range: dict, canon) -> bool:
    """Is the course in a family like 'any 300+ level PHYS course'? (See course_range in the schema.)
    A course id is subject + 2-digit discipline + 3-digit number, e.g. PHYS 00 310."""
    subject, discipline, number = course[:-5], course[-5:-3], course[-3:]
    if subject != course_range["subject"] or not number.isdigit():
        return False
    if course_range.get("discipline", discipline) != discipline:
        return False
    if not course_range.get("min_number", 0) <= int(number) <= course_range.get("max_number", 999):
        return False
    return course not in {canon(c) for c in course_range.get("exclude", [])}


def matches(requirement: dict, course: str, attributes, canon) -> bool:
    """Does this course (with these attributes) fit the requirement's rule? Grades, credits and
    exclusivity are handled by the caller."""
    rule = requirement["rule"]
    if rule == "credits_with_attribute":
        return requirement["attribute"] in attributes
    if rule == "credits_any":
        return True
    if rule == "one_of_groups":
        return course in {canon(c) for group in requirement["course_groups"] for c in group}
    return (course in {canon(c) for c in requirement["course_options"]}
            or any(in_range(course, cr, canon) for cr in requirement.get("course_ranges", [])))


def waived(data: dict, requirement: dict) -> bool:
    """The guide's waivers: the requirement is met with no course when the student's entry
    matches any condition in waived_if."""
    entry, condition = data["student"].get("entry", {}), requirement.get("waived_if", {})
    if condition.get("nj_associate_degree") and entry.get("nj_associate_degree"):
        return True
    needed = condition.get("transfer_credits_at_entry")
    return needed is not None and entry.get("transfer_credits_at_entry", 0) >= needed


# ── The transcript as graded attempts ──

def _attempts(data: dict) -> list:
    """Every graded attempt on the transcript: Rowan courses (institutional) and transfer
    courses. In-progress courses have no grade, so they are not attempts yet."""
    canon, transcript = canonical(data), data["transcript"]
    rows = [(c, True) for c in transcript["completed_courses"]] + \
           [(c, False) for c in transcript["transfer_courses"]]
    return [{"id": i, "course": canon(c["course_id"]), "grade": c["grade"],
             "credits": c["credit_hours"], "institutional": institutional,
             "attributes": set(c.get("attributes", [])) | catalog_attributes(data, c["course_id"])}
            for i, (c, institutional) in enumerate(rows)]


def _counted(pool: list, repeatable: set) -> list:
    """The best attempt of each course, plus every attempt of a repeatable course."""
    best, every = {}, []
    for a in pool:
        if a["course"] in repeatable:
            every.append(a)
        elif a["course"] not in best or _rank(a["grade"]) < _rank(best[a["course"]]["grade"]):
            best[a["course"]] = a
    return sorted(every + list(best.values()), key=lambda a: a["id"])


# ── The audit ──

def audit(data: dict) -> dict:
    """Degree audit: which requirements and graduation rules the transcript satisfies.

    Requirements are read in order. One course may count toward several requirements,
    unless both are marked `exclusive`; then the earlier requirement takes it.
    Returns {"requirements": {req_id: status}, "graduation": {rule: status}}.
    """
    canon, attempts = canonical(data), _attempts(data)
    taken_exclusively, requirements = set(), {}
    for r in data["program_requirements"]:
        if waived(data, r):
            requirements[r["req_id"]] = {"rule": r["rule"], "satisfied": True, "waived": True}
            continue
        # The attempts that may count here: good enough grade, the right course, and not
        # already taken by an earlier exclusive requirement.
        repeatable = {canon(c) for c in r.get("repeatable", [])}
        pool = _counted([a for a in attempts
                         if meets_minimum(a["grade"], r["minimum_grade"])
                         and matches(r, a["course"], a["attributes"], canon)
                         and not (r.get("exclusive") and a["id"] in taken_exclusively)], repeatable)
        result, used = _requirement_status(r, pool, canon)
        if r.get("exclusive"):
            taken_exclusively |= {a["id"] for a in used}
        requirements[r["req_id"]] = result
    return {"requirements": requirements, "graduation": _graduation(data, attempts)}


def _requirement_status(r: dict, pool: list, canon) -> tuple:
    """(status, attempts used) for one requirement, given the attempts that may count toward it."""
    result = {"rule": r["rule"]}
    if r["rule"] == "all_of":
        have = {a["course"] for a in pool}
        result["missing"] = sorted({canon(c) for c in r["course_options"]} - have)
        result["satisfied"] = not result["missing"]
        return result, pool
    if r["rule"] == "any_of":
        result["satisfied"] = bool(pool)
        return result, sorted(pool, key=lambda a: _rank(a["grade"]))[:1]
    if r["rule"] == "one_of_groups":
        have = {a["course"] for a in pool}
        groups = [sorted({canon(c) for c in g}) for g in r["course_groups"]]
        closest = min(groups, key=lambda g: len(set(g) - have))      # first group on a tie
        result["missing"] = sorted(set(closest) - have)
        result["satisfied"] = not result["missing"]
        return result, [a for a in pool if a["course"] in closest]
    # The credit rules (credits_from, credits_with_attribute, credits_any): add up credits,
    # each credit_caps entry limiting how much a set of courses may contribute.
    caps = [({canon(c) for c in cap["course_options"]}, cap["max_credits"])
            for cap in r.get("credit_caps", [])]
    cap_used, earned, used = [0] * len(caps), 0, []
    for a in pool:
        credits = a["credits"]
        for i, (courses, limit) in enumerate(caps):
            if a["course"] in courses:
                credits = min(credits, limit - cap_used[i])
        for i, (courses, _) in enumerate(caps):
            if a["course"] in courses:
                cap_used[i] += credits
        if credits > 0:
            if earned < r["credit_hours"]:
                used.append(a)        # an exclusive requirement takes only what it needs
            earned += credits
    result.update(earned_credits=earned,
                  remaining_credits=max(0, r["credit_hours"] - earned),
                  satisfied=earned >= r["credit_hours"])
    if caps:
        result["cap_remaining"] = [max(0, limit - u) for (_, limit), u in zip(caps, cap_used)]
    return result, used


def _graduation(data: dict, attempts: list) -> dict:
    """Graduation-wide rules (spec open item 7): total and residency credits, GPA, major GPA.
    Quality points and GPA are derived here, never stored. PROVISIONAL: a repeated course
    counts once, by its best attempt."""
    canon = canonical(data)
    repeatable = {canon(c) for r in data["program_requirements"] for c in r.get("repeatable", [])}
    earned = _counted([a for a in attempts if meets_minimum(a["grade"], "D-")], repeatable)
    graded = _counted([a for a in attempts if a["institutional"] and a["grade"] in GRADE_POINTS], repeatable)
    gpa_credits = sum(a["credits"] for a in graded)
    totals = {
        "total_credits": sum(a["credits"] for a in earned),
        "residency_credits": sum(a["credits"] for a in earned if a["institutional"]),
        "minimum_gpa": round(sum(GRADE_POINTS[a["grade"]] * a["credits"] for a in graded) / gpa_credits, 3)
                       if gpa_credits else None,
        "major_gpa": _major_gpa(data, attempts, repeatable),
    }
    rules = data.get("graduation_rules", {})
    return {name: {"value": value, "required": rules.get(name),
                   "satisfied": None if name not in rules else value is not None and value >= rules[name]}
            for name, value in totals.items()}


def _major_gpa(data: dict, attempts: list, repeatable: set):
    """GPA over every Rowan-graded attempt (best attempt per course, F included) at a course
    that a guide_section 'major' requirement names, the rule behind 'an overall GPA of 2.5 or
    better in all courses that satisfy major requirements'. None when there is none."""
    canon = canonical(data)
    major = [r for r in data["program_requirements"]
             if r.get("guide_section") == "major" and r["rule"] != "credits_any"]
    graded = _counted([a for a in attempts if a["institutional"] and a["grade"] in GRADE_POINTS
                       and any(matches(r, a["course"], a["attributes"], canon) for r in major)], repeatable)
    credits = sum(a["credits"] for a in graded)
    return round(sum(GRADE_POINTS[a["grade"]] * a["credits"] for a in graded) / credits, 3) if credits else None


# ── Questions about one offered course ──

def counts_toward(data: dict, course_id: str, attributes=()) -> list:
    """The req_ids of UNMET requirements that taking this course would move forward."""
    canon = canonical(data)
    course = canon(course_id)
    attributes = set(attributes) | catalog_attributes(data, course_id)
    status = audit(data)["requirements"]
    progressing = []
    for r in data["program_requirements"]:
        if status[r["req_id"]]["satisfied"] or not matches(r, course, attributes, canon):
            continue
        capped = [remaining for cap, remaining in zip(r.get("credit_caps", []),
                                                      status[r["req_id"]].get("cap_remaining", []))
                  if course in {canon(c) for c in cap["course_options"]}]
        if capped and min(capped) <= 0:
            continue                      # its credits would not count: the cap is used up
        # Already earned with a good enough grade: taking it again helps only if repeatable.
        earned = any(a["course"] == course and meets_minimum(a["grade"], r["minimum_grade"])
                     for a in _attempts(data))
        if not earned or course in {canon(c) for c in r.get("repeatable", [])}:
            progressing.append(r["req_id"])
    return progressing


def missing_prerequisites(data: dict, course_id: str) -> list:
    """(required course, minimum grade) pairs the transcript does not satisfy (spec open
    item 4). PROVISIONAL: a prerequisite is a consistency rule, and a course still in
    progress does not satisfy one, because it has no grade yet."""
    canon, attempts = canonical(data), _attempts(data)
    missing = []
    for p in data.get("prerequisites", []):
        if canon(p["course_id"]) != canon(course_id):
            continue
        for needed in p["requires"]:
            if not any(a["course"] == canon(needed) and meets_minimum(a["grade"], p["minimum_grade"])
                       for a in attempts):
                missing.append((needed, p["minimum_grade"]))
    return missing
