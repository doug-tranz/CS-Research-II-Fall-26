"""
generate_cases.py
-----------------
Writes test cases built from the degree requirements: five per program in
degree_requirements.json. Each case names its program, fabricates its transcript
(fabricate.py), and gets a rules answer key whose accepted carts are found by scoring every
candidate cart against the whole Fall 2026 Section Tally, the way the session run scores a
model's answer.

The five cases per program come from these templates, in order, each falling back to the
next idea when the program's requirements or this term's sections cannot support it:

  1. requirement   "Register me for one class that counts toward my <requirement>." + a
                   constraint (days, times, online / in person) that rules out some sections.
  2. named_pair    "Enroll me in <course A> and <course B>." Two required courses the student
                   has not taken; time conflicts and the constraint are the traps.
  3. retake        The transcript has a course below the requirement's minimum grade: "If that
                   doesn't count toward my major, register me to retake it."
     passed        (when no requirement sets a minimum grade) "One class toward <requirement>;
                   nothing I've already passed." The transcript has passed electives.
  4. attribute     "I still need a class for my <Rowan Core literacy / Rowan Experience>
                   requirement." Designations come from the program's course_attributes.
     fallback      "Sign me up for <a course with no Fall 2026 section>. If it isn't offered,
                   register me for another class toward <requirement>."
  5. credit_limit  "I can take at most N credits this fall. Register me for <course A>, and add
                   <course B> too only if both fit within my limit." N is set so that both fit
                   (about half the programs) or only A does.

HOW TO RUN (from the repo root):
    python generate_cases.py            # writes the generated cases into input_format_cases.json
    python generate_cases.py --dry-run  # prints what it would write

Re-running replaces the generated cases (case ids "<program>-1" ... "-5") and keeps every
other case. The same programs and Section Tally give the same cases.
"""

import argparse
import json
import random
import re
import zlib
from functools import lru_cache
from pathlib import Path

import fabricate
import registration as reg
import run_semantic_eval as rse
import section_tally
from harness import parse_didcomm

CASE_FILE = Path("input_format_cases.json")
THROUGH = "Spring 2026"
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
# (name, rules, text for one class, text for two)
CONSTRAINTS = [
    ("no_friday", {"no_days": ["Friday"]}, "No Friday classes.", "No Friday classes."),
    ("tue_thu", {"no_days": ["Monday", "Wednesday", "Friday", "Saturday", "Sunday"], "modality_in": ["in-person"]},
     "I can only be on campus Tuesdays and Thursdays, and I want it in person.",
     "I can only be on campus Tuesdays and Thursdays, and I want both in person."),
    ("mon_wed", {"no_days": ["Tuesday", "Thursday", "Friday", "Saturday", "Sunday"], "modality_in": ["in-person"]},
     "I can only be on campus Mondays and Wednesdays, and I want it in person.",
     "I can only be on campus Mondays and Wednesdays, and I want both in person."),
    ("online", {"modality_in": ["online"]}, "It has to be fully online.", "They both have to be fully online."),
    ("in_person", {"modality_in": ["in-person"]}, "I only want in-person classes.", "I only want in-person classes."),
    ("ends_by_5", {"ends_by": "5:00 PM"}, "I work evenings, so nothing that ends after 5 PM.",
     "I work evenings, so nothing that ends after 5 PM."),
    ("before_5", {"starts_before": "5:00 PM"}, "Nothing that starts at 5 PM or later.", "Nothing that starts at 5 PM or later."),
]
LIT_NAMES = {"ARTL": "Artistic Literacy", "GLBL": "Global Literacy", "HUML": "Humanistic Literacy",
             "QNTL": "Quantitative Literacy", "SCIL": "Scientific Literacy", "LIT": "Broad-Based Literature",
             "WI": "Writing Intensive"}
BELOW = {"C-": "D+", "C": "C-", "C+": "C", "B-": "C+", "B": "B-"}


@lru_cache(maxsize=None)
def tally():
    """The whole Fall 2026 Section Tally (read once)."""
    return section_tally.full_schedule()


@lru_cache(maxsize=None)
def by_course():
    """course id -> its non-cancelled sections."""
    out = {}
    for s in tally()["sections"]:
        if not s.get("cancelled"):
            out.setdefault(s["course_id"], []).append(s)
    return out


def is_open(s):
    """Has a free seat."""
    return s["seats_taken"] < s["seats_total"]


def title_of(course, prog):
    """A course's name for a prompt: the guide's title when it reads as one, else the Section
    Tally's, without honors / attribute decorations, in title case."""
    guide = re.sub(r"\s+OR$", "", prog.get("course_titles", {}).get(course, ""))
    clean = guide and len(guide) <= 60 and not re.search(r"\b(OR|SATISFIES|MINIMUM|PREREQ)\b", guide)
    raw = guide if clean else (fabricate._title(course, prog) or course)
    raw = re.sub(r"^HONORS\s+", "", raw)
    raw = re.sub(r"\s*-\s*(WI|RS|W|R)$", "", raw).strip(" -")
    small = {"and", "of", "the", "for", "in", "to", "a", "an", "or", "with", "on", "at"}

    def word(w, first):
        if any(ch.isdigit() for ch in w) or w.strip(",.:()") in ACRONYMS:
            return w
        return "/".join(p.capitalize() if first or p.lower() not in small else p.lower() for p in w.split("/"))
    return " ".join(word(w, i == 0) for i, w in enumerate(raw.replace("&", " & ").split()))


ACRONYMS = {"US", "U.S.", "GIS", "IOS", "AI", "TV", "PR", "CS", "ECE", "BME", "HRM", "MIS", "SQL", "STEM", "ABA", "IS&A",
            "I", "II", "III", "IV", "V", "VI", "DNA", "RTF", "SCL", "MCB", "TBS", "HES", "EKG", "PHW", "UX", "CAD"}


def _titled(r):
    """Has a short title of its own that a prompt can name."""
    title = r.get("title")
    return bool(title and len(title) <= 60 and " or " not in title.lower())


def label_of(r):
    """What a prompt calls a requirement: its title, else its category."""
    if _titled(r):
        return r["title"]
    name = r["category"].split(": ", 1)[-1]
    return name.split(" (")[0]


def phrase(label):
    """'my <label> requirement', without saying 'requirement' twice."""
    ends = ("requirement", "requirements", "courses", "electives", "elective", "sequence", "bank", "studies")
    return f"my {label}" + ("" if label.lower().endswith(ends) else " requirement")


def same_label(prog, r):
    """Every requirement a prompt naming r's label could mean: those with the same title, or,
    when the label is a category ("Non-Program Courses"), every requirement in that category."""
    if _titled(r):
        return [x["req_id"] for x in prog["program_requirements"] if _titled(x) and x["title"] == r["title"]]
    base = r["category"].split(" (")[0]
    return [x["req_id"] for x in prog["program_requirements"] if x["category"].split(" (")[0] == base]


COUNTS_TOWARD = reg.counts_toward


class Case:
    """One generated case: build it, then score carts against the whole tally."""

    def __init__(self, case):
        """Builds the case against the whole tally and seeds its checker database once."""
        self.raw = case
        self.case = rse.resolve_case({**case, "schedule_from_tally": {"whole": True}})
        self.data = self.case["input"]
        self.conn = reg.seed_db(self.data)
        self._counts = {}

    def counts_toward(self, course):
        """registration.counts_toward() for this case, cached per course. Calls the original
        function, since score() swaps reg.counts_toward for this one while it runs."""
        if course not in self._counts:
            self._counts[course] = COUNTS_TOWARD(self.data, course)
        return self._counts[course]

    def score(self, cart, rules):
        """Tiers 1-4 for a cart under `rules`, as (outcome, detail)."""
        raw = rse.expected_message(self.case, [s["crn"] if isinstance(s, dict) else s for s in cart])
        base = reg.evaluate(self.conn, raw)
        if base["outcome"] != "consistent__semantic_TBD":
            return base["outcome"], base["detail"]
        original = COUNTS_TOWARD
        rse.reg.counts_toward = lambda data, course, attrs=(): self.counts_toward(course)
        try:
            verdict = rse.check_rules(parse_didcomm(raw)[0], {**self.case, "ground_truth": {"rules": rules}})
        finally:
            rse.reg.counts_toward = original
        return verdict["outcome"], verdict["detail"]


# ── transcripts ──

def plan_transcript(prog, targets, avoid, seed, rows=(), extra_complete=(), skip=()):
    """A fabricate spec: every requirement before the earliest target is complete (except
    those that need an avoided course), the targets stay unmet."""
    reqs = prog["program_requirements"]
    ids = [r["req_id"] for r in reqs]
    first = min(ids.index(t) for t in targets)
    complete = []
    for i, r in enumerate(reqs):
        if r["req_id"] in targets or r["req_id"] in skip or r["rule"] == "credits_any":
            continue
        if i < first or r["req_id"] in extra_complete or r["guide_section"] in ("rowan_core", "rowan_experience"):
            needs = set(r["course_options"]) if r["rule"] == "all_of" else set()
            if needs & set(avoid):
                continue
            complete.append(r["req_id"])
    return {"seed": seed, "through": THROUGH, "complete": complete, "rows": list(rows), "avoid": sorted(avoid),
            "keep_unmet": sorted(targets)}


def make_case(key, n, prog, fab, prompt, student_id, rng):
    """A case dict with its transcript fabricated, catalog year set from its credits."""
    probe = fabricate.build_input({"program": key, "student": {"student_id": student_id, "catalog_year": "Fall 2022",
                                                               "entry": {"type": "first_year"}},
                                   "transcript": {"fabricate": fab}, "prompt": prompt})
    # About 15 credits a term: that many terms back from Spring 2026 gives the entry Fall. A fixed
    # row from an earlier term pushes the catalog year back to that row's academic year.
    credits = sum(r["credit_hours"] for r in probe["transcript"]["completed_courses"])
    terms = max(2, min(8, round(credits / 15)))
    year = 2026 - (terms + 1) // 2
    catalog = f"Fall {year}"
    for r in fab["rows"]:
        if int(r["term"].split()[1]) < year or (r["term"].startswith("Spring") and int(r["term"].split()[1]) == year):
            catalog = "Fall " + str(int(r["term"].split()[1]) - (1 if r["term"].startswith("Spring") else 0))
    return {"case_id": f"{key}-{n}",
            "input": {"program": key,
                      "student": {"student_id": student_id, "catalog_year": catalog, "entry": {"type": "first_year"}},
                      "transcript": {"fabricate": fab}, "prompt": prompt}}


# ── answer keys ──

def judge(c, candidates, rules, size):
    """Accepted carts of `size` sections drawn from `candidates`, and the rejected singles."""
    from itertools import combinations, product
    accepted, rejected = [], []
    if size == 1:
        for s in candidates:
            outcome, detail = c.score([s], rules)
            (accepted if outcome == "semantic_ok" else rejected).append(([s["crn"]], outcome, detail))
    else:
        a_secs, b_secs = candidates
        for sa, sb in product(a_secs, b_secs):
            outcome, detail = c.score([sa, sb], rules)
            (accepted if outcome == "semantic_ok" else rejected).append(([sa["crn"], sb["crn"]], outcome, detail))
    return accepted, rejected


def pick_constraint(c, candidates, base_rules, size, rng):
    """The first constraint (in a seeded order) that leaves at least one correct cart and rules
    out at least one cart that is otherwise correct. Falls back to no constraint when full
    sections or other traps already make the key interesting."""
    plain, plain_rej = judge(c, candidates, base_rules, size)
    if not plain:
        return None
    order = CONSTRAINTS[:]
    rng.shuffle(order)
    for name, extra, one, two in order:
        rules = {**base_rules, **extra}
        acc, rej = judge(c, candidates, rules, size)
        # carts that are correct without the constraint but not with it: the constraint's own traps
        knocked = {tuple(x[0]) for x in plain} - {tuple(x[0]) for x in acc}
        if acc and knocked:
            return name, rules, (one if size == 1 else two), acc, rej, knocked
    if plain_rej:
        return "none", base_rules, "", plain, plain_rej, set()
    return None


def examples(acc, rej, knocked, rng, extra_invalid=()):
    """The answer key's example carts: up to four accepted carts as valid; as invalid, the carts
    the constraint knocked out, then near misses (full sections, other rule breaks), then
    the template's own traps (extra_invalid)."""
    valid = [x[0] for x in acc]
    rng.shuffle(valid)
    invalid, seen = [], set()
    for cart in sorted(knocked)[:3]:
        invalid.append(list(cart))
        seen.add(tuple(cart))
    for cart, outcome, detail in rej:
        if len(invalid) >= 6:
            break
        key = tuple(cart)
        if key not in seen and (outcome != "semantic_error" or "full" in detail or len(invalid) < 4):
            invalid.append(cart)
            seen.add(key)
    for cart in extra_invalid:
        if tuple(cart) not in seen and len(invalid) < 8:
            invalid.append(list(cart))
    return {"valid": sorted(valid[:4]), "invalid": invalid}


def summarize(acc, rej, size):
    """The implicit_relationship text: how many carts are correct and why the others fail."""
    full = sorted({crn for cart, outcome, d in rej if "is full" in d for crn in cart})
    prereq = sorted({crn for cart, o, d in rej if "prerequisite" in d for crn in cart})
    conflict = [cart for cart, o, d in rej if "time conflict" in d]
    broke = [cart for cart, o, d in rej if o == "semantic_error"]
    parts = [f"{len(acc)} correct {'section' if size == 1 else 'cart'}{'s' if len(acc) != 1 else ''}: "
             + ", ".join("+".join(x[0]) for x in acc[:10]) + (" ..." if len(acc) > 10 else "")]
    if full:
        parts.append(f"full sections (no waitlist allowed): {', '.join(full[:8])}{' ...' if len(full) > 8 else ''}")
    if prereq:
        parts.append(f"missing a prerequisite: {', '.join(prereq[:6])}")
    if conflict:
        parts.append(f"{len(conflict)} pair(s) overlap in time, e.g. {'+'.join(conflict[0])}")
    if broke:
        parts.append(f"{len(broke)} {'section' if size == 1 else 'cart'}(s) break a rule of the request")
    return "; ".join(parts) + "."


# ── templates ──

def unmet_targets(c, prog, r):
    """r and every other unmet requirement a prompt naming r's label could mean."""
    status = reg.audit(c.data)["requirements"]
    return [t for t in same_label(prog, r) if not status[t]["satisfied"]] or [r["req_id"]]


def candidate_courses(c, req_id):
    """Offered courses that would move this requirement forward for this student."""
    r = next(x for x in c.data["program_requirements"] if x["req_id"] == req_id)
    canon = reg._canonical(c.data)
    courses = [cid for cid in by_course() if reg._matches(r, canon(cid), reg.catalog_attributes(c.data, cid), canon)]
    return [cid for cid in courses if req_id in c.counts_toward(cid)]


def t_requirement(key, n, prog, rng, used, sid):
    """Template 1: 'Register me for one class that counts toward my <requirement>.'"""
    reqs = [r for r in prog["program_requirements"]
            if r["guide_section"] in ("major", "non_program") and r["rule"] in ("any_of", "credits_from", "all_of")
            and r["req_id"] not in used]
    rng.shuffle(reqs)
    reqs.sort(key=lambda r: (len(same_label(prog, r)) > 1, r["rule"] == "all_of", r["guide_section"] != "major"))
    for r in reqs[:6]:
        fab = plan_transcript(prog, [r["req_id"]], [], rng.randrange(10 ** 6))
        prompt = f"Register me for one class that counts toward {phrase(label_of(r))}."
        try:
            case = make_case(key, n, prog, fab, prompt, sid, rng)
            c = Case(case)
        except ValueError:
            continue
        targets = unmet_targets(c, prog, r)
        courses = sorted({cid for t in targets for cid in candidate_courses(c, t)})
        sections = [s for cid in courses for s in by_course().get(cid, [])]
        if not sections or len(sections) > 160:
            continue
        rules = {"count": {"min": 1, "max": 1}, "courses_count_toward": targets}
        found = pick_constraint(c, sections, rules, 1, rng)
        if not found:
            continue
        name, rules, text, acc, rej, knocked = found
        if text:
            case["input"]["prompt"] = f"{prompt} {text}"
        earned = [x for x in c.data["transcript"]["completed_courses"] if x["course_id"] in by_course()]
        extra = [[s["crn"]] for x in earned[:2] for s in by_course()[x["course_id"]][:1]]
        two = [[acc[0][0][0], acc[1][0][0]]] if len(acc) > 1 else []
        ex = examples(acc, rej, knocked, rng, extra + two)
        why = (f"{label_of(r)} ({', '.join(targets)}) is not yet met on the fabricated transcript. Every Fall 2026 course "
               f"that would move it forward was considered ({len(courses)} courses, {len(sections)} sections)"
               + (f"; the request also says: '{text}'" if text else "") + ". " + summarize(acc, rej, 1))
        return _finish(case, rules, ex, why, "requirement", targets, courses)
    return None


def t_named_pair(key, n, prog, rng, used, sid, sections=("major",)):
    """Template 2: 'Enroll me in <course A> and <course B>.' (two untaken required courses)"""
    pool = []
    for r in prog["program_requirements"]:
        if r["rule"] == "all_of" and r["guide_section"] in sections:
            for cid in r["course_options"]:
                secs = by_course().get(cid, [])
                if 1 <= len(secs) <= 25 and any(is_open(s) for s in secs):
                    pool.append((r["req_id"], cid))
    rng.shuffle(pool)
    for i in range(len(pool)):
        for j in range(i + 1, min(len(pool), i + 6)):
            (ra, a), (rb, b) = pool[i], pool[j]
            if a == b:
                continue
            targets = sorted({ra, rb})
            fab = plan_transcript(prog, targets, [a, b], rng.randrange(10 ** 6))
            prompt = f"Enroll me in {title_of(a, prog)} ({a}) and {title_of(b, prog)} ({b})."
            try:
                case = make_case(key, n, prog, fab, prompt, sid, rng)
                c = Case(case)
            except ValueError:
                continue
            if reg.missing_prerequisites(c.data, a) or reg.missing_prerequisites(c.data, b):
                continue
            rules = {"count": {"min": 2, "max": 2}, "must_include_courses": [a, b]}
            found = pick_constraint(c, (by_course()[a], by_course()[b]), rules, 2, rng)
            if not found:
                continue
            name, rules, text, acc, rej, knocked = found
            if text:
                case["input"]["prompt"] = f"{prompt} {text}"
            single = [[acc[0][0][0]]]
            ex = examples(acc, rej, knocked, rng, single)
            why = (f"{a} and {b} are required major courses the student has not taken. Every pair of their Fall 2026 "
                   f"sections was scored" + (f"; the request also says: '{text}'" if text else "") + ". "
                   + summarize(acc, rej, 2))
            return _finish(case, rules, ex, why, "named_pair", targets, [a, b])
    return None


def t_retake(key, n, prog, rng, used, sid):
    """Template 3: a course earned below its requirement's minimum grade, to be retaken. Falls
    back to t_passed when no requirement sets a minimum grade."""
    choices = []
    for r in prog["program_requirements"]:
        if r["minimum_grade"] in BELOW and r["rule"] in ("all_of", "any_of") and r["guide_section"] in ("major", "non_program"):
            for cid in r["course_options"]:
                if any(is_open(s) for s in by_course().get(cid, [])) and len(by_course()[cid]) <= 40:
                    choices.append((r, cid))
    rng.shuffle(choices)
    for r, x in choices[:8]:
        low = BELOW[r["minimum_grade"]]
        row = {"term": THROUGH, "course_id": x, "grade": low}
        fab = plan_transcript(prog, [r["req_id"]], [x], rng.randrange(10 ** 6), rows=[row])
        name_x = title_of(x, prog)
        prompt = (f"I got a {low} in {name_x} ({x}) last spring. If that doesn't count toward my major, register me "
                  f"to retake it.")
        try:
            case = make_case(key, n, prog, fab, prompt, sid, rng)
            c = Case(case)
        except ValueError:
            continue
        if reg.missing_prerequisites(c.data, x):
            continue
        rules = {"count": {"min": 1, "max": 1}, "must_include_courses": [x]}
        found = pick_constraint(c, by_course()[x], rules, 1, rng)
        if not found:
            continue
        cname, rules, text, acc, rej, knocked = found
        if text:
            case["input"]["prompt"] = f"{prompt} {text}"
        ex = examples(acc, rej, knocked, rng)
        why = (f"{r['req_id']} needs a {r['minimum_grade']} or better, so {x} at {low} does not count and must be "
               f"retaken" + (f"; the request also says: '{text}'" if text else "") + ". " + summarize(acc, rej, 1))
        return _finish(case, rules, ex, why, "retake", [r["req_id"]], [x])
    return t_passed(key, n, prog, rng, used, sid)


def t_passed(key, n, prog, rng, used, sid):
    """Template 3, fallback: one class toward an elective requirement, nothing already passed."""
    reqs = [r for r in prog["program_requirements"] if r["rule"] == "credits_from" and r["credit_hours"] >= 9
            and r["guide_section"] == "major" and r["req_id"] not in used]
    rng.shuffle(reqs)
    for r in reqs:
        repeat = set(r.get("repeatable", []))
        offered = [cid for cid in r["course_options"] if cid not in repeat
                   and any(is_open(s) for s in by_course().get(cid, []))]
        if len(offered) < 3:
            continue
        passed = rng.sample(offered, 2)
        rows = [{"term": t, "course_id": cid, "grade": g}
                for cid, t, g in zip(passed, ["Fall 2025", THROUGH], ["B+", "B"])]
        fab = plan_transcript(prog, [r["req_id"]], passed, rng.randrange(10 ** 6), rows=rows)
        prompt = (f"Register me for one class that counts toward {phrase(label_of(r))}. I don't want to retake "
                  f"anything I've already passed.")
        try:
            case = make_case(key, n, prog, fab, prompt, sid, rng)
            c = Case(case)
        except ValueError:
            continue
        if c.data["program_requirements"] and reg.audit(c.data)["requirements"][r["req_id"]]["satisfied"]:
            continue
        targets = unmet_targets(c, prog, r)
        courses = sorted({cid for t in targets for cid in candidate_courses(c, t)}) + passed
        sections = [s for cid in dict.fromkeys(courses) for s in by_course().get(cid, [])]
        if len(sections) > 160:
            continue
        rules = {"count": {"min": 1, "max": 1}, "courses_count_toward": targets}
        found = pick_constraint(c, sections, rules, 1, rng)
        if not found:
            continue
        cname, rules, text, acc, rej, knocked = found
        if text:
            case["input"]["prompt"] = f"{prompt} {text}"
        traps = [[s["crn"]] for cid in passed for s in by_course()[cid] if is_open(s)][:2]
        ex = examples(acc, rej, knocked, rng, traps)
        why = (f"The student passed {passed[0]} and {passed[1]}, which count toward {r['req_id']} but are not "
               f"repeatable, so retaking them moves nothing forward" + (f"; the request also says: '{text}'" if text else "")
               + ". " + summarize(acc, rej, 1))
        return _finish(case, rules, ex, why, "passed", targets, list(dict.fromkeys(courses)))
    return None


def t_attribute(key, n, prog, rng, used, sid):
    """Template 4: a Rowan Core literacy or Rowan Experience attribute. Falls back to t_fallback
    when the program designates no offered course for any of them."""
    codes = list(LIT_NAMES)
    rng.shuffle(codes)
    for code in codes:
        req_id = ("CORE-" if code in ("ARTL", "GLBL", "HUML", "QNTL", "SCIL") else "EXP-") + code
        designated = [cid for cid, attrs in prog["course_attributes"].items() if code in attrs]
        offered = [cid for cid in designated if any(is_open(s) for s in by_course().get(cid, []))]
        if not offered:
            continue
        fab = plan_transcript(prog, [req_id], designated, rng.randrange(10 ** 6), skip=[req_id])
        prompt = f"I still need a class for my {LIT_NAMES[code]} requirement. Register me for one that satisfies it."
        try:
            case = make_case(key, n, prog, fab, prompt, sid, rng)
            c = Case(case)
        except ValueError:
            continue
        sections = [s for cid in designated for s in by_course().get(cid, []) if req_id in c.counts_toward(cid)]
        if not sections or len(sections) > 160:
            continue
        rules = {"count": {"min": 1, "max": 1}, "courses_count_toward": [req_id]}
        found = pick_constraint(c, sections, rules, 1, rng)
        if not found:
            continue
        cname, rules, text, acc, rej, knocked = found
        if text:
            case["input"]["prompt"] = f"{prompt} {text}"
        ex = examples(acc, rej, knocked, rng)
        why = (f"{req_id} is unmet. The courses this input designates {code} (course_attributes) are "
               f"{', '.join(sorted(designated))}" + (f"; the request also says: '{text}'" if text else "") + ". "
               + summarize(acc, rej, 1))
        return _finish(case, rules, ex, why, "attribute", [req_id], designated)
    return t_fallback(key, n, prog, rng, used, sid)


def t_fallback(key, n, prog, rng, used, sid):
    """Template 4, fallback: a course with no Fall 2026 section, then another toward its requirement."""
    reqs = [r for r in prog["program_requirements"] if r["rule"] in ("any_of", "credits_from")
            and r["guide_section"] == "major" and r["req_id"] not in used]
    rng.shuffle(reqs)
    for r in reqs:
        missing = [cid for cid in r["course_options"] if cid not in by_course() and
                   fabricate._title(cid, prog)]
        if not missing:
            continue
        x = rng.choice(missing)
        fab = plan_transcript(prog, [r["req_id"]], [x], rng.randrange(10 ** 6))
        prompt = (f"Sign me up for {title_of(x, prog)} ({x}) this fall. If it isn't offered, register me for another "
                  f"class that counts toward {phrase(label_of(r))} instead.")
        try:
            case = make_case(key, n, prog, fab, prompt, sid, rng)
            c = Case(case)
        except ValueError:
            continue
        targets = unmet_targets(c, prog, r)
        courses = sorted({cid for t in targets for cid in candidate_courses(c, t)})
        sections = [s for cid in courses for s in by_course().get(cid, [])]
        if not sections or len(sections) > 160:
            continue
        rules = {"count": {"min": 1, "max": 1}, "courses_count_toward": targets}
        found = pick_constraint(c, sections, rules, 1, rng)
        if not found:
            continue
        cname, rules, text, acc, rej, knocked = found
        if text:
            case["input"]["prompt"] = f"{prompt} {text}"
        ex = examples(acc, rej, knocked, rng)
        why = (f"{x} has no Fall 2026 section, so the fallback applies: another course toward {r['req_id']}"
               + (f"; the request also says: '{text}'" if text else "") + ". " + summarize(acc, rej, 1))
        return _finish(case, rules, ex, why, "fallback", targets, courses + [x])
    return None


def _fixed_credits(course):
    """The credits every section of a course carries, or None (variable credit, or sections disagree)."""
    values = {json.dumps(s["credit_hours"]) for s in by_course().get(course, [])}
    if len(values) != 1:
        return None
    credits = json.loads(values.pop())
    return None if isinstance(credits, dict) else credits


def t_credit_limit(key, n, prog, rng, used, sid):
    """Template 5: 'I can take at most N credits this fall. Register me for <course A>, and add
    <course B> too only if both fit within my limit.' The limit is chosen so that both fit (the
    answer is one section of each, not overlapping) or so that they do not (the answer is A
    alone); which one is drawn per program, so the set has both."""
    fits = rng.random() < 0.5
    # required courses first (major, then non-program); then courses from an elective list,
    # for programs with no required pair that has fixed credits and Fall 2026 sections
    pool = []
    for rule_kinds, sections in ((("all_of",), ("major",)), (("all_of",), ("non_program",)),
                                 (("any_of", "credits_from"), ("major",))):
        for r in prog["program_requirements"]:
            if r["rule"] in rule_kinds and r["guide_section"] in sections:
                for cid in r["course_options"]:
                    secs = by_course().get(cid, [])
                    if 1 <= len(secs) <= 25 and any(is_open(s) for s in secs) and _fixed_credits(cid):
                        pool.append((r["req_id"], cid))
    rng.shuffle(pool)
    for i in range(len(pool)):
        for j in range(i + 1, min(len(pool), i + 6)):
            (ra, a), (rb, b) = pool[i], pool[j]
            if a == b:
                continue
            ca, cb = _fixed_credits(a), _fixed_credits(b)
            limit = ca + cb if fits else ca + cb - 1        # never below A's own credits
            targets = sorted({ra, rb})
            fab = plan_transcript(prog, targets, [a, b], rng.randrange(10 ** 6))
            prompt = (f"I can take at most {limit:g} credits this fall. Register me for {title_of(a, prog)} ({a}), "
                      f"and add {title_of(b, prog)} ({b}) too only if both fit within my limit.")
            try:
                case = make_case(key, n, prog, fab, prompt, sid, rng)
                c = Case(case)
            except ValueError:
                continue
            if reg.missing_prerequisites(c.data, a) or reg.missing_prerequisites(c.data, b):
                continue
            if fits:
                rules = {"count": {"min": 2, "max": 2}, "must_include_courses": [a, b], "credits": {"max": limit}}
                acc, rej = judge(c, (by_course()[a], by_course()[b]), rules, 2)
                if not acc:
                    continue
                traps = [[acc[0][0][0]]]                      # A alone: both fit, so B was wanted
            else:
                rules = {"count": {"min": 1, "max": 1}, "must_include_courses": [a], "credits": {"max": limit}}
                acc, rej = judge(c, by_course()[a], rules, 1)
                if not acc:
                    continue
                open_b = [s["crn"] for s in by_course()[b] if is_open(s)]
                traps = [[acc[0][0][0], crn] for crn in open_b[:2]]   # adding B breaks the limit
            ex = examples(acc, rej, set(), rng, traps)
            why = (f"{a} is {ca:g} sh and {b} is {cb:g} sh, {ca + cb:g} together, against a limit of {limit:g}: "
                   + ("both fit, so the answer is one section of each that do not overlap. " if fits else
                      f"they do not fit, so the answer is {a} alone; adding {b} breaks the limit. ")
                   + summarize(acc, rej, 2 if fits else 1))
            return _finish(case, rules, ex, why, "credit_limit", targets, [a, b])
    return None


def difficulty(case, c=None):
    """ground_truth.difficulty for a case: how many answers a model could plausibly give (every
    non-cancelled section of the case's candidate courses, or every pair of sections when the
    answer is two named courses), how many of those are correct, the chance of a correct answer
    by picking one of them at random, the number of rules, and the prompt's length in words.
    c: the case's Case, if already built."""
    c = c or Case(case)
    rules = case["ground_truth"]["rules"]
    courses = (case.get("schedule_from_tally") or {}).get("courses", [])
    size = rules.get("count", {}).get("max", 1)
    if size == 2 and len(rules.get("must_include_courses", [])) == 2:
        a, b = rules["must_include_courses"]
        candidates = (by_course().get(a, []), by_course().get(b, []))
        total = len(candidates[0]) * len(candidates[1])
    else:
        candidates = [s for cid in courses for s in by_course().get(cid, [])]
        total, size = len(candidates), 1
    accepted, _ = judge(c, candidates, rules, size) if total else ([], [])
    return {"candidate_courses": len(courses), "candidate_answers": total, "correct_answers": len(accepted),
            "chance": round(len(accepted) / total, 4) if total else None, "rules": len(rules),
            "prompt_words": len(case["input"]["prompt"].split())}


def add_difficulty(path):
    """Writes ground_truth.difficulty into every case of a case file (no model call)."""
    raw = Path(path).read_bytes().decode("utf-8")
    cases = json.loads(raw)
    for i, case in enumerate(cases, 1):
        case["ground_truth"]["difficulty"] = difficulty(case)
        if i % 50 == 0:
            print(f"{path}: {i}/{len(cases)}", flush=True)
    text = json.dumps(cases, indent=2, ensure_ascii=False)
    Path(path).write_bytes(((text.replace("\n", "\r\n") + "\r\n") if "\r\n" in raw else text + "\n").encode("utf-8"))
    print(f"wrote difficulty for {len(cases)} cases in {path}")


def _finish(case, rules, ex, why, kind, targets, courses):
    """Adds the named-courses schedule and the answer key to a built case."""
    case["schedule_from_tally"] = {"named_courses": True, "courses": sorted(set(courses))}
    case["ground_truth"] = {"rules": rules, "examples": ex, "implicit_relationship": why,
                            "case_type": kind, "targets": targets}
    return case


TEMPLATES = [t_requirement, t_named_pair, t_retake, t_attribute]


def generate(only=None):
    """Five cases for every program (or only those in `only`); returns (cases, failures)."""
    programs = fabricate.load_programs()
    cases, failures, sid = [], [], 1000
    for key in sorted(programs):
        if only and key not in only:
            continue
        prog = programs[key]
        rng = random.Random(zlib.crc32(key.encode()))
        used = set()
        made = []
        for n, template in enumerate(TEMPLATES, start=1):
            sid += 1
            case = template(key, n, prog, rng, used, f"S{sid}")
            if case is None:          # fall back to another requirement case, then a non-program pair
                case = t_requirement(key, n, prog, rng, used, f"S{sid}")
            if case is None:
                case = t_named_pair(key, n, prog, rng, used, f"S{sid}", sections=("major", "non_program"))
            if case is None:
                failures.append(f"{key}-{n} ({template.__name__})")
                continue
            used.update(case["ground_truth"]["targets"])
            made.append(case)
        # Template 5 has its own seed and student ids (S5001...), so adding it leaves cases 1-4 as they were.
        rng5 = random.Random(zlib.crc32(f"{key}-5".encode()))
        case = t_credit_limit(key, 5, prog, rng5, used, f"S{5001 + sorted(programs).index(key)}")
        if case is None:
            failures.append(f"{key}-5 (t_credit_limit)")
        else:
            made.append(case)
        cases += made
        print(f"{key:40} {len(made)} cases: " + ", ".join(c["ground_truth"]["case_type"] for c in made), flush=True)
    return cases, failures


def main():
    """Command line: writes the generated cases into the case file (--dry-run only prints)."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--programs", nargs="+")
    ap.add_argument("--difficulty", nargs="+", metavar="FILE",
                    help="only (re)write ground_truth.difficulty for every case in these case files")
    args = ap.parse_args()
    if args.difficulty:
        for path in args.difficulty:
            add_difficulty(path)
        return
    cases, failures = generate(args.programs)
    for case in cases:
        case["ground_truth"]["difficulty"] = difficulty(case)
    print(f"\n{len(cases)} cases generated; could not build: {failures or 'none'}")
    if args.dry_run:
        return
    raw = CASE_FILE.read_bytes()
    existing = json.loads(raw.decode("utf-8"))
    keys = set(fabricate.load_programs())
    keep = [c for c in existing if c["case_id"].rsplit("-", 1)[0] not in keys or
            not c["case_id"].rsplit("-", 1)[-1].isdigit()]
    replaced = {c["case_id"] for c in cases}
    keep = [c for c in keep if c["case_id"] not in replaced]
    out = keep + cases
    CASE_FILE.write_bytes((json.dumps(out, indent=2, ensure_ascii=False).replace("\n", "\r\n") + "\r\n").encode("utf-8"))
    print(f"wrote {CASE_FILE}: {len(keep)} kept + {len(cases)} generated = {len(out)} cases")


if __name__ == "__main__":
    main()
