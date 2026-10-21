"""
generate_pairs.py
-----------------
Writes counterfactual PAIRS of test cases into paired_cases.json: two cases that are the same
but for one detail, the prompt or the fabricated transcript, chosen so that the detail flips
the answer. A model that ignores the detail gets at most one of the two right, so a pair passes
only when both variants are semantic_ok (run_semantic_eval.pair_summary()).

The idea follows the BIRD-style pairs on the Tyler branch (SEM-001..020, RC-001..012), rebuilt
on this pipeline: real Fall 2026 sections from the Section Tally, real degree programs, rules
answer keys, and every candidate cart scored the way a model's answer is scored. Seats are
real, so a pair cannot make a section full; it flips what the student asked or has taken.

  family          what changes between a and b                       the reasoning it needs
  full_fallback   first choice: a course with every section full     notice the first choice is full
                  (a) or one with open seats (b)                     and apply the fallback
  in_progress     the transcript: the named course is in progress    read courses_in_progress; don't
                  (a) or another course is (b)                       register for what you're taking
  transfer        the transcript: a transfer course that counts      add credits across completed and
                  toward the requirement (a) or one that doesn't (b) transfer courses
  busy_block      the student's fixed weekly commitment              interval overlap, not equal times
  nested_groups   the transcript: which courses of a (A and B and C)  evaluate AND-inside-OR groups
                  or (A and B and D) requirement are done
  credit_limit    the credit limit: A + B (a) or A + B - 1 (b)        credit arithmetic
  modality        "fully online" (a) or "in person" (b)              the modality constraint
  days            Mondays/Wednesdays (a) or Tuesdays/Thursdays (b)   the meeting-day constraint
  grade_threshold the transcript: a grade below the minimum (a) or   minimum passing grade
                  at it (b)
  withdrawal      the transcript: a W (a) or a B (b)                 a W earns no credit

Not built: campus (all but 30 of 5,129 sections are in Glassboro, online or off-campus).

Case ids are pair-<family>-<program>-a / -b; ground_truth.pair = {"id": "<family>/<program>",
"variant", "changed"}. Each variant's invalid examples start with its twin's valid carts that it
rejects, so --check proves the detail matters.

HOW TO RUN (from the repo root):
    python generate_pairs.py                    # adds pairs to paired_cases.json until it holds 200
    python generate_pairs.py --dry-run          # prints what it would add
    python generate_pairs.py --total 250        # grow it further; pairs already there are kept
    python generate_pairs.py --fresh --total 40 # start over, ignoring the file
    python run_semantic_eval.py --test-file paired_cases.json --check
"""

import argparse
import copy
import json
import random
import re
import zlib
from functools import lru_cache
from itertools import combinations
from pathlib import Path

import fabricate
import registration as reg
import run_semantic_eval as rse
import section_tally
from harness import parse_didcomm

PAIR_FILE = Path("paired_cases.json")
ONE = {"count": {"min": 1, "max": 1}}
TRANSFER_SCHOOL = "Rowan College of South Jersey"


# ── Building and scoring a case (the generator's toolkit) ──
# A case is built against the whole Fall 2026 Section Tally, and every candidate answer is
# scored the way a model's answer is, so each answer key matches the grader.

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
        self.case = rse.resolve_case(case)
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


def candidate_courses(c, req_id):
    """Offered courses that would move this requirement forward for this student."""
    r = next(x for x in c.data["program_requirements"] if x["req_id"] == req_id)
    canon = reg._canonical(c.data)
    courses = [cid for cid in by_course() if reg._matches(r, canon(cid), reg.catalog_attributes(c.data, cid), canon)]
    return [cid for cid in courses if req_id in c.counts_toward(cid)]


def _fixed_credits(course):
    """The credits every section of a course carries, or None (variable credit, or sections disagree)."""
    values = {json.dumps(s["credit_hours"]) for s in by_course().get(course, [])}
    if len(values) != 1:
        return None
    credits = json.loads(values.pop())
    return None if isinstance(credits, dict) else credits


def difficulty(case, c=None):
    """ground_truth.difficulty for a case: how many answers a model could plausibly give (every
    non-cancelled section of the case's candidate courses, or every pair of sections when the
    answer is two named courses), how many of those are correct, the chance of a correct answer
    by picking one of them at random, the number of rules, and the prompt's length in words.
    c: the case's Case, if already built."""
    c = c or Case(case)
    rules = case["ground_truth"]["rules"]
    courses = case.get("candidate_courses", [])
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


# ── shared pieces ──

def open_secs(course):
    """A course's Fall 2026 sections with a free seat."""
    return [s for s in by_course().get(course, []) if is_open(s)]


def major_pool(prog, rules=("all_of", "any_of", "credits_from"), sections=("major",), most=25):
    """(req_id, course) for each course a major requirement names, with 1 to `most` sections this fall."""
    pool = []
    for r in prog["program_requirements"]:
        if r["rule"] in rules and r["guide_section"] in sections:
            for cid in r["course_options"]:
                if 1 <= len(by_course().get(cid, [])) <= most and (r["req_id"], cid) not in pool:
                    pool.append((r["req_id"], cid))
    return pool


def named(course, prog):
    """'Title (COURSE)' for a prompt."""
    return f"{title_of(course, prog)} ({course})"


def build(key, prog, fab, prompt, sid):
    """(raw case, Case) for a transcript spec and prompt; None when the spec cannot be fabricated."""
    try:
        case = make_case(key, 0, prog, fab, prompt, sid, None)
        return case, Case(case)
    except ValueError:
        return None


def reprompt(case, prompt):
    """The same raw case with another prompt. A prompt-only flip shares one transcript and one
    Case: scoring never reads the prompt."""
    out = copy.deepcopy(case)
    out["input"]["prompt"] = prompt
    return out


def clock(minutes):
    """785 -> '1:05 PM'."""
    hour, minute = divmod(minutes, 60)
    return f"{(hour - 1) % 12 + 1}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def plural_days(days):
    """['Tuesday', 'Thursday'] -> 'Tuesdays and Thursdays'."""
    names = [d + "s" for d in days]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def unique_label(prog, r):
    """True when a prompt naming r's label can only mean r."""
    return same_label(prog, r) == [r["req_id"]]


def pair(family, key, a, b, changed, targets, courses, rng):
    """The two finished cases, or None when the detail does not change the answer.
    a, b: (raw case, Case, rules, candidates, size, why)."""
    scored = []
    for case, c, rules, candidates, size, why in (a, b):
        acc, rej = judge(c, candidates, rules, size)
        if not acc:
            return None
        scored.append((case, c, rules, acc, rej, why, size))
    # cross[i]: the twin's accepted carts that variant i rejects
    cross = [[x[0] for x in twin[3] if here[1].score(x[0], here[2])[0] != "semantic_ok"]
             for here, twin in zip(scored, scored[::-1])]
    if not all(cross):
        return None
    out = []
    for i, ((case, c, rules, acc, rej, why, size), variant) in enumerate(zip(scored, "ab")):
        ex = examples(acc, rej, {tuple(cart) for cart in cross[i]}, rng)
        # check_pairs() reads only the saved valid examples: keep one the twin rejects
        theirs = cross[1 - i]
        if not set(map(tuple, ex["valid"])) & set(map(tuple, theirs)):
            ex["valid"] = sorted(ex["valid"][:3] + [theirs[0]])
        case["case_id"] = f"pair-{family}-{key}-{variant}"
        case["candidate_courses"] = sorted(set(courses))
        case["ground_truth"] = {
            "rules": rules, "examples": ex,
            "implicit_relationship": why + " " + summarize(acc, rej, size)
                                     + f" Its twin's answers {', '.join('+'.join(x) for x in cross[i][:4])} are wrong here.",
            "case_type": family, "targets": targets,
            "pair": {"id": f"{family}/{key}", "variant": variant, "changed": changed}}
        out.append(case)
    return out


# ── families ──

def f_full_fallback(key, prog, rng, sid):
    """'Register me for <Z | X>. If every section of it is full, register me for <Y> instead.'"""
    pool = major_pool(prog)
    full = [(r, c) for r, c in pool if not open_secs(c)]
    opn = [(r, c) for r, c in pool if open_secs(c)]
    rng.shuffle(full)
    rng.shuffle(opn)
    for rz, z in full[:3]:
        for (rx, x), (ry, y) in list(combinations(opn, 2))[:6]:
            if len({x, y, z}) < 3:
                continue
            targets = sorted({rz, rx, ry})
            fab = plan_transcript(prog, targets, [x, y, z], rng.randrange(10 ** 6))
            then = f"If every section of it is full, register me for {named(y, prog)} instead."
            built = build(key, prog, fab, f"Register me for {named(z, prog)}. {then}", sid)
            if not built:
                continue
            case_a, c = built
            case_b = reprompt(case_a, f"Register me for {named(x, prog)}. {then}")
            cands = [s for cid in (x, y, z) for s in by_course()[cid]]
            got = pair("full_fallback", key,
                       (case_a, c, {**ONE, "courses_in": [y]}, cands, 1,
                        f"Every Fall 2026 section of {z} is full, so the fallback {y} applies."),
                       (case_b, c, {**ONE, "courses_in": [x]}, cands, 1,
                        f"{x} has open sections, so the first choice stands and {y} is not wanted."),
                       f"first choice: {z} (every section full) vs {x} (open seats)", targets, [x, y, z], rng)
            if got:
                return got
    return None


def f_in_progress(key, prog, rng, sid):
    """'Register me for <X> this fall. If I'm already taking it this semester, register me for <Y> instead.'"""
    pool = [(r, c) for r, c in major_pool(prog) if open_secs(c)]
    rng.shuffle(pool)
    for (rx, x), (ry, y), (rv, v) in list(combinations(pool, 3))[:8]:
        if len({x, y, v}) < 3:
            continue
        targets = sorted({rx, ry, rv})
        seed = rng.randrange(10 ** 6)
        prompt = (f"Register me for {named(x, prog)} this fall. If I'm already taking it this semester, register me "
                  f"for {named(y, prog)} instead.")
        # b is a's transcript with only the in-progress course swapped, so nothing else can differ
        fab = {**plan_transcript(prog, targets, [x, y, v], seed), "through": "Fall 2025",
               "in_progress": [{"term": "Spring 2026", "course_id": x}]}
        twin = {**fab, "edits": [{"in": "courses_in_progress", "course_id": x,
                                  "set": {"course_id": v, "credit_hours": fabricate._tally().get(v, (None, None))[1] or 3}}]}
        built = [build(key, prog, f, prompt, sid) for f in (fab, twin)]
        if not all(built):
            continue
        (case_a, ca), (case_b, cb) = built
        cands = [s for cid in (x, y) for s in by_course()[cid]]
        got = pair("in_progress", key,
                   (case_a, ca, {**ONE, "courses_in": [y]}, cands, 1,
                    f"{x} is in courses_in_progress (Spring 2026), so the fallback {y} applies."),
                   (case_b, cb, {**ONE, "courses_in": [x]}, cands, 1,
                    f"The course in progress is {v}, not {x}, so {x} is the answer."),
                   f"courses_in_progress: {x} vs {v}", targets, [x, y], rng)
        if got:
            return got
    return None


def f_transfer(key, prog, rng, sid):
    """'One class toward <R1>. If I've already finished it, one toward <R2> instead.' R1 is met only
    when the transfer course counts toward it."""
    reqs = prog["program_requirements"]
    firsts = [r for r in reqs if r["rule"] == "credits_from" and r["credit_hours"] >= 6 and r["guide_section"] == "major"
              and unique_label(prog, r)]
    seconds = [r for r in reqs if r["rule"] in ("any_of", "credits_from") and r["guide_section"] == "major"
               and unique_label(prog, r)]
    rng.shuffle(firsts)
    gened = [cid for cid in by_course() if cid[:-5] in fabricate.GENED_SUBJECTS and cid[-3:].isdigit()
             and int(cid[-3:]) < 300 and _fixed_credits(cid) == 3]
    for r1 in firsts:
        opts = [cid for cid in dict.fromkeys(r1["course_options"]) if _fixed_credits(cid)]
        if len(opts) < 3:
            continue
        rng.shuffle(opts)
        t, rows, earned = opts[0], [], 0
        for cid in opts[1:]:
            if earned >= r1["credit_hours"] - _fixed_credits(t):
                break
            rows.append(cid)
            earned += _fixed_credits(cid)
        if not earned < r1["credit_hours"] <= earned + _fixed_credits(t):
            continue
        canon = lambda cid: cid
        other = [cid for cid in gened if _fixed_credits(cid) == _fixed_credits(t)
                 and not reg._matches(r1, cid, (), canon)]
        if not other:
            continue
        t2 = rng.choice(other)
        for r2 in [r for r in seconds if r["req_id"] != r1["req_id"]][:4]:
            targets = [r1["req_id"], r2["req_id"]]
            seed = rng.randrange(10 ** 6)
            row_specs = [{"term": term, "course_id": cid, "grade": "B"}
                         for cid, term in zip(rows, ["Fall 2025", "Spring 2026"] * len(rows))]
            prompt = (f"Register me for one class that counts toward {phrase(label_of(r1))}. If I've already "
                      f"finished it, register me for one that counts toward {phrase(label_of(r2))} instead.")
            # b is a's transcript with only the transfer course swapped, so nothing else can differ
            fab = {**plan_transcript(prog, targets, opts + [t2], seed, rows=row_specs), "keep_unmet": [r2["req_id"]],
                   "transfer_rows": [{"institution": TRANSFER_SCHOOL, "course_id": t, "grade": "TA"}]}
            twin = {**fab, "edits": [{"in": "transfer_courses", "course_id": t, "set": {"course_id": t2}}]}
            built = [build(key, prog, f, prompt, sid) for f in (fab, twin)]
            if not all(built):
                continue
            (case_a, ca), (case_b, cb) = built
            status_a, status_b = reg.audit(ca.data)["requirements"], reg.audit(cb.data)["requirements"]
            if not status_a[r1["req_id"]]["satisfied"] or status_b[r1["req_id"]]["satisfied"]:
                continue
            courses = sorted(set(candidate_courses(cb, r1["req_id"])) | set(candidate_courses(ca, r2["req_id"])))
            cands = [s for cid in courses for s in by_course().get(cid, [])]
            if not cands or len(cands) > 160:
                continue
            h = r1["credit_hours"]
            got = pair("transfer", key,
                       (case_a, ca, {**ONE, "courses_count_toward": [r2["req_id"]]}, cands, 1,
                        f"{r1['req_id']} needs {h:g} sh: {earned:g} completed plus transfer {t} "
                        f"({_fixed_credits(t):g} sh, one of its options) meets it, so the request moves to {r2['req_id']}."),
                       (case_b, cb, {**ONE, "courses_count_toward": [r1["req_id"]]}, cands, 1,
                        f"{r1['req_id']} needs {h:g} sh: {earned:g} completed, and transfer {t2} is not one of its "
                        f"options, so it is unmet and the answer counts toward it."),
                       f"transfer_courses: {t} (counts toward {r1['req_id']}) vs {t2} (does not)",
                       targets, courses, rng)
            if got:
                return got
    return None


def busy_block(section):
    """A weekly commitment that partly overlaps the section's first meeting: the same days, 30
    minutes later. None when the section has no set times."""
    m = section["meetings"][0] if section["meetings"] else {}
    if "start_time" not in m:
        return None
    start, end = reg.minutes(m["start_time"]) + 30, reg.minutes(m["end_time"]) + 30
    return {"days": m["days"], "start_time": clock(start), "end_time": clock(end)}


def f_busy_block(key, prog, rng, sid):
    """'Register me for <X>. I work <days> from <start> to <end>, so it can't overlap that.'"""
    pool = [(r, c) for r, c in major_pool(prog) if len([s for s in open_secs(c) if busy_block(s)]) >= 2]
    rng.shuffle(pool)
    for rx, x in pool[:4]:
        fab = plan_transcript(prog, [rx], [x], rng.randrange(10 ** 6))
        built = build(key, prog, fab, "", sid)
        if not built or reg.missing_prerequisites(built[1].data, x):
            continue
        case, c = built
        timed = [s for s in open_secs(x) if busy_block(s)]
        for s1, s2 in list(combinations(timed, 2))[:10]:
            b1, b2 = busy_block(s1), busy_block(s2)
            if b1 == b2:
                continue
            text = lambda b: (f"Register me for {named(x, prog)}. I work {plural_days(b['days'])} from {b['start_time']} "
                              f"to {b['end_time']}, so the class can't overlap that.")
            case_a, case_b = reprompt(case, text(b1)), reprompt(case, text(b2))
            cands = by_course()[x]
            rules = lambda b: {**ONE, "must_include_courses": [x], "busy": [b]}
            got = pair("busy_block", key,
                       (case_a, c, rules(b1), cands, 1,
                        f"The commitment starts 30 minutes into section {s1['crn']}'s meeting, so it overlaps without "
                        f"sharing a start or end time."),
                       (case_b, c, rules(b2), cands, 1,
                        f"The commitment starts 30 minutes into section {s2['crn']}'s meeting."),
                       f"commitment: {plural_days(b1['days'])} {b1['start_time']}-{b1['end_time']} vs "
                       f"{plural_days(b2['days'])} {b2['start_time']}-{b2['end_time']}", [rx], [x], rng)
            if got:
                return got
    return None


def completers(held, groups):
    """Courses that would finish a one_of_groups requirement given the courses held."""
    return sorted({c for g in groups for c in g if c not in held and set(g) <= held | {c}})


def f_nested_groups(key, prog, rng, sid):
    """'Register me for the one class I still need to finish my <requirement>.' The requirement is
    (A and B and C) or (A and B and D); which courses are done decides which one finishes it."""
    needs = {}
    for p in prog.get("prerequisites", []):
        needs.setdefault(p["course_id"], set()).update(p["requires"])
    for r in [r for r in prog["program_requirements"] if r["rule"] == "one_of_groups"]:
        groups = [list(dict.fromkeys(g)) for g in r["course_groups"]]
        every = {c for g in groups for c in g}
        states = []
        for g in groups:
            for m in g:
                held = set(g) - {m}
                done = completers(held, groups)
                # each held course's prerequisites inside the requirement are held too, and no held
                # course is a later one in a missing course's subject (II done but not I)
                in_order = not any(h[:-5] == d[:-5] and h > d for h in held for d in done)
                if held and in_order and all(open_secs(c) for c in done) and all(needs.get(h, set()) & every <= held for h in held):
                    if frozenset(held) not in [s for s, _ in states]:
                        states.append((frozenset(held), done))
        options = [(s1, s2) for (s1, d1), (s2, d2) in combinations(states, 2) if not set(d1) & set(d2)]
        if not options:
            continue
        best = max(len(s1 & s2) for s1, s2 in options)
        options = [o for o in options if len(o[0] & o[1]) == best]
        h1, h2 = rng.choice(options)
        seed = rng.randrange(10 ** 6)
        prompt = f"Register me for the one class I still need to finish {phrase(label_of(r))}."
        built = []
        for held in (h1, h2):
            rows = [{"term": term, "course_id": cid, "grade": "B"}
                    for cid, term in zip(sorted(held), ["Fall 2025", "Spring 2026"] * len(held))]
            fab = plan_transcript(prog, [r["req_id"]], sorted(every), seed, rows=rows, skip=[r["req_id"]])
            built.append(build(key, prog, fab, prompt, sid))
        if not all(built):
            continue
        (case_a, ca), (case_b, cb) = built
        cands = [s for cid in sorted(every) for s in by_course().get(cid, [])]
        groups_text = " or ".join("(" + " and ".join(g) + ")" for g in groups)
        got = pair("nested_groups", key,
                   (case_a, ca, {**ONE, "courses_in": completers(set(h1), groups)}, cands, 1,
                    f"{r['req_id']} is {groups_text}. With {', '.join(sorted(h1))} done, only "
                    f"{' or '.join(completers(set(h1), groups))} finishes it."),
                   (case_b, cb, {**ONE, "courses_in": completers(set(h2), groups)}, cands, 1,
                    f"{r['req_id']} is {groups_text}. With {', '.join(sorted(h2))} done, only "
                    f"{' or '.join(completers(set(h2), groups))} finishes it."),
                   f"courses done toward {r['req_id']}: {', '.join(sorted(h1))} vs {', '.join(sorted(h2))}",
                   [r["req_id"]], sorted(every), rng)
        if got:
            return got
    return None


def f_credit_limit(key, prog, rng, sid):
    """'I can take at most <A+B | A+B-1> credits. Register me for <A>, and add <B> too only if both fit.'"""
    pool = [(r, c) for r, c in major_pool(prog, sections=("major", "non_program")) if open_secs(c) and _fixed_credits(c)]
    rng.shuffle(pool)
    for (ra, a), (rb, b) in list(combinations(pool, 2))[:8]:
        if a == b:
            continue
        ca, cb = _fixed_credits(a), _fixed_credits(b)
        targets = sorted({ra, rb})
        fab = plan_transcript(prog, targets, [a, b], rng.randrange(10 ** 6))
        built = build(key, prog, fab, "", sid)
        if not built:
            continue
        case, c = built
        text = lambda n: (f"I can take at most {n:g} credits this fall. Register me for {named(a, prog)}, and add "
                          f"{named(b, prog)} too only if both fit within my limit.")
        case_a, case_b = reprompt(case, text(ca + cb)), reprompt(case, text(ca + cb - 1))
        got = pair("credit_limit", key,
                   (case_a, c,
                    {"count": {"min": 2, "max": 2}, "must_include_courses": [a, b], "credits": {"max": ca + cb}},
                    (by_course()[a], by_course()[b]), 2,
                    f"{a} ({ca:g} sh) + {b} ({cb:g} sh) = {ca + cb:g}, within the limit: one section of each."),
                   (case_b, c,
                    {**ONE, "must_include_courses": [a], "credits": {"max": ca + cb - 1}}, by_course()[a], 1,
                    f"{a} ({ca:g} sh) + {b} ({cb:g} sh) = {ca + cb:g}, over the limit of {ca + cb - 1:g}: {a} alone."),
                   f"credit limit: {ca + cb:g} vs {ca + cb - 1:g}", targets, [a, b], rng)
        if got:
            return got
    return None


def _constraint_pair(family, key, prog, rng, sid, first, second, changed):
    """One named course under two constraints (CONSTRAINTS names) that pick different sections."""
    rules_of = {name: (extra, text) for name, extra, text, _ in CONSTRAINTS}
    pool = [(r, c) for r, c in major_pool(prog) if len(open_secs(c)) >= 2]
    rng.shuffle(pool)
    for rx, x in pool[:6]:
        fab = plan_transcript(prog, [rx], [x], rng.randrange(10 ** 6))
        built = build(key, prog, fab, "", sid)
        if not built:
            continue
        case, c = built
        variants = []
        for name in (first, second):
            extra, text = rules_of[name]
            v = reprompt(case, f"Register me for {named(x, prog)}. {text}")
            variants.append((v, c, {**ONE, "must_include_courses": [x], **extra}, by_course()[x], 1,
                             f"Only sections of {x} that satisfy '{text}' are correct."))
        got = pair(family, key, variants[0], variants[1], changed, [rx], [x], rng)
        if got:
            return got
    return None


def f_modality(key, prog, rng, sid):
    """'Register me for <X>. It has to be fully online.' vs '... I only want in-person classes.'"""
    return _constraint_pair("modality", key, prog, rng, sid, "online", "in_person", "modality: online vs in-person")


def f_days(key, prog, rng, sid):
    """'Register me for <X>.' + Mondays/Wednesdays in person vs Tuesdays/Thursdays in person."""
    return _constraint_pair("days", key, prog, rng, sid, "mon_wed", "tue_thu", "days: Mon/Wed vs Tue/Thu")


def _grade_pair(family, key, prog, rng, sid, grades, prompt_of, changed_of):
    """The transcript holds <X> with grade grades[0] (a: retake X) or grades[1] (b: take Y)."""
    choices = []
    for r in prog["program_requirements"]:
        if r["minimum_grade"] in BELOW and r["rule"] in ("all_of", "any_of") and r["guide_section"] in ("major", "non_program"):
            for cid in r["course_options"]:
                if open_secs(cid) and len(by_course()[cid]) <= 40:
                    choices.append((r, cid))
    rng.shuffle(choices)
    others = [(r, c) for r, c in major_pool(prog) if open_secs(c)]
    for r, x in choices[:6]:
        low, high = grades(r["minimum_grade"])
        for ry, y in [o for o in others if o[1] != x][:3]:
            targets = sorted({r["req_id"], ry})
            seed = rng.randrange(10 ** 6)
            # b is a's transcript with only x's grade edited, so nothing else can differ
            fab = plan_transcript(prog, targets, [x, y], seed, rows=[{"term": THROUGH, "course_id": x, "grade": low}])
            twin = {**fab, "edits": [{"in": "completed_courses", "course_id": x, "set": {"grade": high}}]}
            built = [build(key, prog, f, prompt_of(x, y), sid) for f in (fab, twin)]
            if not all(built):
                continue
            (case_a, ca), (case_b, cb) = built
            cands = [s for cid in (x, y) for s in by_course()[cid]]
            got = pair(family, key,
                       (case_a, ca, {**ONE, "courses_in": [x]}, cands, 1,
                        f"{x} at {low} does not count toward {r['req_id']} (minimum {r['minimum_grade']}), so retake it."),
                       (case_b, cb, {**ONE, "courses_in": [y]}, cands, 1,
                        f"{x} at {high} counts toward {r['req_id']} (minimum {r['minimum_grade']}), so the answer is {y}."),
                       changed_of(x, low, high), targets, [x, y], rng)
            if got:
                return got
    return None


def f_grade_threshold(key, prog, rng, sid):
    """'If my grade in <X> doesn't count toward my major, retake it; otherwise <Y>.' Grade below vs at the minimum."""
    return _grade_pair("grade_threshold", key, prog, rng, sid, lambda m: (BELOW[m], m),
                       lambda x, y: (f"If my grade in {named(x, prog)} last spring doesn't count toward my major, "
                                     f"register me to retake it. Otherwise, register me for {named(y, prog)}."),
                       lambda x, lo, hi: f"grade in {x}: {lo} (below the minimum) vs {hi} (the minimum)")


def f_withdrawal(key, prog, rng, sid):
    """'Register me for <X> if I haven't earned credit for it yet; if I have, <Y> instead.' W vs B."""
    return _grade_pair("withdrawal", key, prog, rng, sid, lambda m: ("W", "B" if reg.meets_minimum("B", m) else "A"),
                       lambda x, y: (f"Register me for {named(x, prog)} if I haven't earned credit for it yet. "
                                     f"If I have, register me for {named(y, prog)} instead."),
                       lambda x, lo, hi: f"grade in {x}: {lo} vs {hi}")


FAMILIES = {f.__name__[2:]: f for f in (f_full_fallback, f_in_progress, f_transfer, f_busy_block, f_nested_groups,
                                         f_credit_limit, f_modality, f_days, f_grade_threshold, f_withdrawal)}


# ── ground_truth.pair.difference: what differs between a pair's two cases, in words ──

def _prompt_part(here, twin):
    """The words of `here` that differ from `twin`, after their common start and end."""
    a, b = here.split(), twin.split()
    start = 0
    while start < min(len(a), len(b)) and a[start] == b[start]:
        start += 1
    end = 0
    while end < min(len(a), len(b)) - start and a[-1 - end] == b[-1 - end]:
        end += 1
    return " ".join(a[start:len(a) - end]).strip(" ,.") or "(nothing)"


def _transcript_rows(transcript):
    """Every transcript row as text, by the list it is in (what the model reads)."""
    def title(r):
        return f" {r['title'].title()}" if r.get("title") else ""
    return {"completed": {f"{r['course_id']}{title(r)} ({r['grade']})" for r in transcript["completed_courses"]},
            "transfer": {f"{r['course_id']}{title(r)} (transfer, {r['grade']})" for r in transcript["transfer_courses"]},
            "in progress": {f"{r['course_id']}{title(r)} ({r['term']}, in progress)" for r in transcript["courses_in_progress"]}}


def _transcript_part(here, twin):
    """The transcript rows `here` has and `twin` does not, by list."""
    rh, rt = _transcript_rows(here), _transcript_rows(twin)
    parts = [f"{name}: {', '.join(sorted(rh[name] - rt[name]))}" for name in rh if rh[name] - rt[name]]
    parts += [f"{name}: no {', '.join(sorted(rt[name] - rh[name]))}" for name in rh
              if rt[name] - rh[name] and not rh[name] - rt[name]]
    return "; ".join(parts) or "(nothing)"


def answer_text(rules, prog):
    """A correct cart under these rules, in words."""
    names = lambda courses: [named(c, prog) for c in courses]
    if "must_include_courses" in rules:
        text = "one section each of " + " and ".join(names(rules["must_include_courses"])) \
            if len(rules["must_include_courses"]) > 1 else "a section of " + names(rules["must_include_courses"])[0]
    elif "courses_in" in rules:
        text = "a section of " + " or ".join(names(rules["courses_in"]))
    else:
        labels = {r["req_id"]: label_of(r) for r in prog["program_requirements"]}
        text = "one class that counts toward " + " or ".join(
            f"{labels.get(r, r)} ({r})" for r in rules.get("courses_count_toward", []))
    if "modality_in" in rules:
        text += f", {' or '.join(rules['modality_in'])}"
    if "no_days" in rules:
        allowed = [d for d in DAYS if d not in rules["no_days"]]
        text += f", meeting only on {plural_days(allowed)}" if len(allowed) <= 3 else f", with nothing on {plural_days(rules['no_days'])}"
    for b in rules.get("busy", []):
        text += f", not overlapping {plural_days(b['days'])} {b['start_time']}-{b['end_time']}"
    if "credits" in rules:
        text += f", {rules['credits']['max']:g} credits at most"
    return text


def describe(case, twin, prog):
    """ground_truth.pair.difference for `case`: where it differs from its twin, how, and how that
    changes the answer. Read from the inputs the model sees, so an unintended difference shows too."""
    here_in, twin_in = fabricate.build_input(case["input"]), fabricate.build_input(twin["input"])
    where = []
    if here_in["prompt"] != twin_in["prompt"]:
        where.append("prompt")
    if here_in["transcript"] != twin_in["transcript"]:
        where.append("transcript")
    this = [f"prompt says '{_prompt_part(here_in['prompt'], twin_in['prompt'])}'" if w == "prompt"
            else _transcript_part(here_in["transcript"], twin_in["transcript"]) for w in where]
    other = [f"prompt says '{_prompt_part(twin_in['prompt'], here_in['prompt'])}'" if w == "prompt"
             else _transcript_part(twin_in["transcript"], here_in["transcript"]) for w in where]
    here_answer = answer_text(case["ground_truth"]["rules"], prog)
    twin_answer = answer_text(twin["ground_truth"]["rules"], prog)
    return {"where": " and ".join(where) or "nothing",
            "this_case": "; ".join(this), "twin_case": "; ".join(other), "twin_id": twin["case_id"],
            "answer_here": here_answer, "answer_in_twin": twin_answer,
            "summary": (f"Only the {' and '.join(where)} differs. Here: {'; '.join(this)}. In the twin ({twin['case_id']}): "
                        f"{'; '.join(other)}. So the answer here is {here_answer}, while in the twin it is {twin_answer}.")}


def shared_answer(a, b, ca, cb):
    """A one-section answer correct in both variants of a pair (its crn), else None. Such a pair
    is weak: a model can pass it without using the changed detail."""
    ra, rb = a["ground_truth"]["rules"], b["ground_truth"]["rules"]
    if ra.get("count", {}).get("max") != 1 or rb.get("count", {}).get("max") != 1:
        return None                         # a one-section answer cannot fit a two-section variant
    courses = sorted(set(a["candidate_courses"]) | set(b["candidate_courses"]))
    for s in (s for cid in courses for s in by_course().get(cid, [])):
        if ca.score([s["crn"]], ra)[0] == "semantic_ok" and cb.score([s["crn"]], rb)[0] == "semantic_ok":
            return s["crn"]
    return None


def annotate(cases, full=False):
    """Sets, on every pair in `cases` (in place): ground_truth.pair.difference; and, where missing
    (or always when `full`), ground_truth.pair.has_shared_answer (+ shared_answer_example) and each
    case's ground_truth.difficulty. Returns the pairs whose cases differ in more than one place."""
    programs, odd = fabricate.load_programs(), []
    for pid, (a, b) in ((pid, sorted(cs, key=lambda c: c["ground_truth"]["pair"]["variant"]))
                        for pid, cs in pairs_by_id(cases).items()):
        prog = programs[a["input"]["program"]]
        for case, twin in ((a, b), (b, a)):
            case["ground_truth"]["pair"]["difference"] = describe(case, twin, prog)
        if a["ground_truth"]["pair"]["difference"]["where"] not in ("prompt", "transcript"):
            odd.append(pid)
        if full or any("has_shared_answer" not in c["ground_truth"]["pair"] or "difficulty" not in c["ground_truth"]
                       for c in (a, b)):
            ca, cb = Case(a), Case(b)
            crn = shared_answer(a, b, ca, cb)
            for case, c in ((a, ca), (b, cb)):
                case["ground_truth"]["pair"]["has_shared_answer"] = crn is not None
                case["ground_truth"]["pair"].pop("shared_answer_example", None)
                if crn:
                    case["ground_truth"]["pair"]["shared_answer_example"] = crn
                case["ground_truth"]["difficulty"] = difficulty(case, c)
    return odd


ROUNDS = 4          # passes over the programs; a later pass reseeds, so it picks other courses


def signature(cases):
    """What makes a pair the same pair: both answer keys' rules and the changed detail."""
    return json.dumps([c["ground_truth"]["rules"] for c in cases] + [cases[0]["ground_truth"]["pair"]["changed"]],
                      sort_keys=True)


def attempts(name):
    """(round, program) in the order a family tries them: every program once (round 1, seeded as
    the first pairs were), then again with a new seed per round."""
    order = sorted(fabricate.load_programs())
    random.Random(zlib.crc32(name.encode())).shuffle(order)
    return [(r, key) for r in range(1, ROUNDS + 1) for key in order]


def generate(total=200, families=None, existing=()):
    """Pairs until there are `total` in all, counting `existing` (kept as they are). Families take
    turns adding one pair each, so they stay even until one runs out of programs and courses; a
    program can give a family more than one pair (round 2 on) when the courses differ.
    Returns (new cases, per-family counts)."""
    programs = fabricate.load_programs()
    names = [n for n in FAMILIES if not families or n in families]
    seen = {}                                   # (family, program) -> signatures of its pairs
    have = {n: 0 for n in names}
    for pid, variants in pairs_by_id(existing).items():
        name, key = pid.split("/")[0], pid.split("/")[1].split("#")[0]
        seen.setdefault((name, key), set()).add(signature(variants))
        have[name] = have.get(name, 0) + 1
    tried = {(c["ground_truth"]["pair"]["id"].split("/")[0], c["ground_truth"]["pair"]["id"].split("/")[1]) for c in existing}
    queues = {n: [a for a in attempts(n) if (n, a[1] if a[0] == 1 else f"{a[1]}#{a[0]}") not in tried] for n in names}
    cases, made, sid = [], sum(have.values()), 8000
    active = [n for n in names]
    while made < total and active:
        for name in list(active):
            if made >= total:
                break
            got = None
            while queues[name] and not got:
                r, key = queues[name].pop(0)
                sid += 1
                seed = f"{name}/{key}" if r == 1 else f"{name}/{key}/{r}"
                got = FAMILIES[name](key, programs[key], random.Random(zlib.crc32(seed.encode())), f"S{sid}")
                if got and signature(got) in seen.get((name, key), set()):
                    got = None                  # the same courses again: not a new pair
                if got and r > 1:
                    for c in got:
                        variant = c["ground_truth"]["pair"]["variant"]
                        c["case_id"] = f"pair-{name}-{key}-{r}-{variant}"
                        c["ground_truth"]["pair"]["id"] = f"{name}/{key}#{r}"
            if not got:
                active.remove(name)
                print(f"{name:16} out of programs and courses at {have[name]} pairs", flush=True)
                continue
            seen.setdefault((name, key), set()).add(signature(got))
            cases += got
            have[name] += 1
            made += 1
            if made % 10 == 0:
                print(f"{made} pairs: " + ", ".join(f"{n} {have[n]}" for n in names), flush=True)
    return cases, have


def pairs_by_id(cases):
    """pair id -> its cases, in file order."""
    out = {}
    for c in cases:
        out.setdefault(c["ground_truth"]["pair"]["id"], []).append(c)
    return out


def main():
    """Command line: adds pairs to paired_cases.json until it holds --total (--dry-run only prints).
    Pairs already in the file are kept as they are."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--total", type=int, default=200, help="pairs the file should hold, existing ones included")
    ap.add_argument("--families", nargs="+", choices=list(FAMILIES))
    ap.add_argument("--fresh", action="store_true", help="ignore the pairs already in the file")
    ap.add_argument("--annotate", action="store_true",
                    help="only (re)write each pair's difference, weak-pair flag and difficulty; build nothing")
    args = ap.parse_args()
    existing = [] if args.fresh or not PAIR_FILE.exists() else json.loads(PAIR_FILE.read_text(encoding="utf-8"))
    if args.annotate:
        cases = []
    else:
        cases, have = generate(args.total, args.families, existing)
        print(f"\n{len(existing) // 2} kept + {len(cases) // 2} new = {(len(existing) + len(cases)) // 2} pairs; "
              + ", ".join(f"{n} {k}" for n, k in have.items()))
    if args.dry_run:
        return
    order = list(FAMILIES)
    out = sorted(existing + cases, key=lambda c: order.index(c["ground_truth"]["case_type"]))   # stable: grouped by family
    odd = annotate(out, full=args.annotate)
    print(f"described {len(out) // 2} pairs" + (f"; differing in more than one place: {odd}" if odd else ""))
    PAIR_FILE.write_bytes((json.dumps(out, indent=2, ensure_ascii=False).replace("\n", "\r\n") + "\r\n").encode("utf-8"))
    print(f"wrote {PAIR_FILE}")


if __name__ == "__main__":
    main()
