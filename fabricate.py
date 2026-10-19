"""
fabricate.py
------------
Builds a test case's input object from its degree program (degree_requirements.json) and
a fabricated transcript. A case no longer writes out its requirements or transcript:

    "input": {
      "program": "marketing-bs",                       # a key in degree_requirements.json
      "student": {"student_id": "S301", "catalog_year": "Fall 2024", "entry": {"type": "first_year"}},
      "transcript": {"fabricate": {
          "seed": 7,                                   # same seed, same transcript
          "through": "Spring 2026",                    # last finished term (default)
          "complete": ["CORE-*", "EXP-*", "NONPROG-*", "BUS-FOUNDATION"],   # requirements already met
          "rows": [{"term": "Spring 2026", "course_id": "MKT09379", "grade": "D+"}],  # fixed rows (traps)
          "transfer_rows": [{"institution": "Rowan College of South Jersey",   # fixed transfer_courses
                             "course_id": "MKT09370", "grade": "TA"}],
          "in_progress": [{"term": "Spring 2026", "course_id": "MKT09382"}],  # fixed courses_in_progress
          "avoid": ["MKT09384"],                       # courses the fabricator must not use
          "keep_unmet": ["MKT-REQUIRED"],              # checked after fabrication
          "edits": [{"in": "completed_courses", "course_id": "MKT09379", "set": {"grade": "C"}}]}},
                                                       # applied last, after the checks
      "prompt": "..."}

Everything else in the input (requirements, guide, attributes, graduation rules, prerequisites,
course equivalents) comes from the program.

The fabricator picks real courses that satisfy each requirement in `complete`, gives each a
grade at or above every minimum it must meet (the requirement's, and any prerequisite that
needs it), orders them so prerequisites come first, fills the terms from the catalog year to
`through`, and then runs registration.audit() to confirm that every requirement in
`complete` is met and every one in `keep_unmet` is not. Rowan Core / Rowan Experience
attribute requirements are met with courses whose designations a program guide states
(transcript rows carry the attribute). Deterministic for a given seed. Last, `edits` change
single rows of the finished transcript without fabricating again (a counterfactual pair's twin).
"""

import fnmatch
import json
import random
from functools import lru_cache
from pathlib import Path

import registration as reg
import section_tally

PROGRAMS_FILE = Path(__file__).with_name("degree_requirements.json")
GRADES = ["A", "A-", "B+", "B", "B-", "C+"]
GRADE_WEIGHTS = [2, 3, 3, 4, 3, 2]
GENED_SUBJECTS = {"ART", "ARHS", "MUSG", "HIST", "PHIL", "PSY", "SOC", "ENGL", "GEOG", "ANTH", "POSC", "ECON",
                  "THD", "RTF", "WA", "INTR", "AMST", "SPAN", "FREN", "ITAL", "CHIN", "ASTR", "GEOL", "BIOL", "PHYS"}
INPUT_ORDER = ["student", "transcript", "program_requirements", "prerequisites", "course_equivalents",
               "graduation_rules", "program_guide", "course_attributes", "prompt"]


@lru_cache(maxsize=None)
def load_programs() -> dict:
    """Every program in degree_requirements.json, by program key (read once)."""
    return json.loads(PROGRAMS_FILE.read_text(encoding="utf-8"))["programs"]


@lru_cache(maxsize=None)
def _tally():
    """course id -> (title, fixed credits or None) for every course in the Section Tally. The
    title is the one most of the course's sections carry (not an honors or topics variant)."""
    titles, credits_of = {}, {}
    for s in section_tally.full_schedule()["sections"]:
        titles.setdefault(s["course_id"], []).append(s["title"])
        credits = s["credit_hours"]
        if isinstance(credits, (int, float)) and s["course_id"] not in credits_of:
            credits_of[s["course_id"]] = credits
    return {c: (max(set(ts), key=lambda t: (ts.count(t), -len(t))), credits_of.get(c)) for c, ts in titles.items()}


@lru_cache(maxsize=None)
def _offered() -> tuple:
    """Every course with at least one non-cancelled Fall 2026 section."""
    return tuple(sorted({s["course_id"] for s in section_tally.full_schedule()["sections"] if not s.get("cancelled")}))


@lru_cache(maxsize=None)
def attribute_pool() -> dict:
    """Rowan Core / Experience code -> courses some program guide designates with it, offered ones first."""
    offered, pool = set(_offered()), {}
    for prog in load_programs().values():
        for course, codes in prog["course_attributes"].items():
            for code in codes:
                pool.setdefault(code, set()).add(course)
    return {code: sorted(cs, key=lambda c: (c not in offered, c)) for code, cs in pool.items()}


def _next_term(term: str) -> str:
    """Fall Y -> Spring Y+1 -> Fall Y+1 (summer and winter terms are skipped)."""
    season, year = term.split()
    return f"Spring {int(year) + 1}" if season == "Fall" else f"Fall {year}"


def term_range(first: str, last: str) -> list:
    """Fall and Spring terms from `first` to `last` inclusive (a Spring or Winter start rounds to its Fall)."""
    season, year = first.split()
    term = first if season in ("Fall", "Spring") else f"Fall {year}"
    terms = []
    while True:
        terms.append(term)
        if term == last or len(terms) > 20:
            break
        term = _next_term(term)
    return terms


def _named(requirements: list) -> set:
    """Every course a requirement names outright (its options and groups, not its ranges)."""
    return {c for r in requirements for c in r["course_options"] + [c for g in r.get("course_groups", []) for c in g]}


def build_input(spec: dict) -> dict:
    """The full input object for a case that names a program (see the module docstring)."""
    prog = load_programs()[spec["program"]]
    data = {"student": {**prog["student"], **spec["student"]},
            "program_requirements": prog["program_requirements"],
            "graduation_rules": dict(prog["graduation_rules"]),
            "program_guide": prog["program_guide"],
            "course_attributes": prog["course_attributes"],
            "prompt": spec["prompt"]}
    for key in ("prerequisites", "course_equivalents"):
        if prog.get(key):
            data[key] = prog[key]
    data["transcript"] = fabricate(data, spec["transcript"]["fabricate"], prog)
    return {k: data[k] for k in INPUT_ORDER if k in data}


def _expand(patterns: list, requirements: list) -> list:
    """The requirement ids matching the patterns ('CORE-*' style), in program order."""
    ids = [r["req_id"] for r in requirements]
    out = []
    for p in patterns:
        matched = [i for i in ids if fnmatch.fnmatchcase(i, p)]
        if not matched:
            raise ValueError(f"fabricate: no requirement matches {p!r}")
        out += [i for i in matched if i not in out]
    return [i for i in ids if i in out]          # program order


class _Builder:
    """Builds one fabricated transcript: the spec's fixed rows, plus courses picked one
    requirement at a time, then grades and terms."""
    def __init__(self, data, fab, prog):
        self.data, self.fab, self.prog = data, fab, prog
        self.rng = random.Random(fab.get("seed", 0))
        self.reqs = {r["req_id"]: r for r in data["program_requirements"]}
        self.canon = reg._canonical(data)
        self.avoid = {self.canon(c) for c in fab.get("avoid", [])}
        self.fixed = [dict(r) for r in fab.get("rows", [])]
        self.transfers = [{**r, "credit_hours": r.get("credit_hours", self.credits(r["course_id"]))}
                          for r in fab.get("transfer_rows", [])]
        self.in_progress = [{**r, "credit_hours": r.get("credit_hours", self.credits(r["course_id"]))}
                            for r in fab.get("in_progress", [])]
        self.picks = []                           # {"course_id", "credit_hours", "attributes", "min_grade"}
        self.named = _named(data["program_requirements"])

    # ── the transcript as the audit sees it (terms and grades do not matter yet) ──
    def transcript(self, grades=None):
        """The transcript so far, in the input format. Picks are graded A until grades are
        assigned, so choosing courses never trips a minimum grade."""
        rows = [{"term": r.get("term", "Fall 2000"), "course_id": r["course_id"], "grade": r["grade"],
                 "credit_hours": r.get("credit_hours", self.credits(r["course_id"])),
                 **({"attributes": r["attributes"]} if r.get("attributes") else {})} for r in self.fixed]
        rows += [{"term": "Fall 2000", "course_id": p["course_id"], "grade": (grades or {}).get(i, "A"),
                  "credit_hours": p["credit_hours"], **({"attributes": p["attributes"]} if p["attributes"] else {})}
                 for i, p in enumerate(self.picks)]
        return {"completed_courses": rows, "transfer_courses": self.transfers, "courses_in_progress": self.in_progress}

    def audit(self):
        """registration.audit() of the transcript so far."""
        return reg.audit({**self.data, "transcript": self.transcript()})["requirements"]

    def used(self):
        """Every course already on the transcript (in progress included), as canonical ids."""
        fixed = self.fixed + self.transfers + self.in_progress
        return {self.canon(r["course_id"]) for r in fixed} | {self.canon(p["course_id"]) for p in self.picks}

    def credits(self, course, requirement=None):
        """A course's credits: the tally's fixed credits, else the requirement's, else 3."""
        fixed = _tally().get(course, (None, None))[1]
        if fixed:
            return fixed
        if requirement and requirement["rule"] == "any_of":
            return requirement["credit_hours"]
        return 3

    def add(self, course, requirement, attributes=()):
        """Puts a chosen course on the transcript for a requirement."""
        self.picks.append({"course_id": course, "credit_hours": self.credits(course, requirement),
                           "attributes": list(attributes), "req": requirement["req_id"]})

    # ── choosing the next course for a requirement ──
    def _free(self, courses, allow_used=()):
        """The courses that are neither avoided nor already used (unless listed in allow_used)."""
        used = self.used()
        return [c for c in courses if c not in self.avoid and (c not in used or c in allow_used)]

    def _range_courses(self, requirement):
        """Offered courses that match the requirement's course_ranges."""
        ranges = requirement.get("course_ranges", [])
        return [c for c in _offered() if any(reg.in_range(c, cr, self.canon) for cr in ranges)]

    def next_course(self, requirement, status):
        """The next course to add toward a requirement and the attributes its row carries,
        or None when no course fits."""
        rule = requirement["rule"]
        if rule in ("all_of", "one_of_groups"):
            missing = self._free(status.get("missing", []))
            if missing:
                return missing[0], ()
            if rule == "one_of_groups":
                for group in requirement["course_groups"]:
                    group = [self.canon(c) for c in group]
                    if not set(group) & self.avoid:
                        return self._free(group)[0], ()
            return None
        if rule == "credits_with_attribute":
            code = requirement["attribute"]
            pool = [c for c in attribute_pool().get(code, []) if c not in self.named]
            choices = self._free(pool) or self._free(attribute_pool().get(code, []))
            # a general-education course: lower level and a common subject first
            choices.sort(key=lambda c: (not c[-3:].isdigit() or int(c[-3:]) >= 300, c[:-5] not in GENED_SUBJECTS,
                                        self.rng.random()))
            return (self.rng.choice(choices[:4]), (code,)) if choices else None
        if rule == "credits_any":
            pool = [c for c in _offered() if c[:-5] in GENED_SUBJECTS and c[-3:].isdigit()
                    and int(c[-3:]) < 300 and c not in self.named and _tally()[c][1] == 3]
            choices = self._free(pool)
            return (self.rng.choice(choices), ()) if choices else None
        options = [self.canon(c) for c in requirement["course_options"]]
        offered = set(_offered())
        options = sorted(dict.fromkeys(options), key=lambda c: (c not in offered, self.rng.random()))
        repeatable = {self.canon(c) for c in requirement.get("repeatable", [])}
        capped = set()
        for cap, left in zip(requirement.get("credit_caps", []), status.get("cap_remaining", [])):
            if left <= 0:
                capped |= {self.canon(c) for c in cap["course_options"]}
        choices = [c for c in self._free(options, allow_used=repeatable) if c not in capped]
        if not choices:
            ranged = self._range_courses(requirement)
            self.rng.shuffle(ranged)
            choices = [c for c in self._free(ranged) if c not in capped]
        return (choices[0], ()) if choices else None

    def satisfy(self, req_id):
        """Adds courses until the requirement is met."""
        requirement = self.reqs[req_id]
        for _ in range(40):
            status = self.audit()[req_id]
            if status["satisfied"]:
                return
            choice = self.next_course(requirement, status)
            if choice is None:
                raise ValueError(f"fabricate: cannot find a course for {req_id} (avoid={sorted(self.avoid)})")
            course, attrs = choice
            self.add(course, requirement, attrs)
        raise ValueError(f"fabricate: {req_id} is still unmet after 40 courses")

    # ── grades and terms ──
    def min_grade(self, pick):
        """The strictest grade a pick must reach: its requirement's minimum, or that of any
        prerequisite entry that needs the course."""
        floors = [self.reqs[pick["req"]]["minimum_grade"]]
        course = self.canon(pick["course_id"])
        floors += [p["minimum_grade"] for p in self.data.get("prerequisites", [])
                   if course in {self.canon(c) for c in p["requires"]}]
        return max(floors, key=lambda g: reg.GRADE_ORDER.index(g) * -1)

    def grades(self):
        """A seeded grade for each pick, at or above its minimum."""
        out = {}
        for i, p in enumerate(self.picks):
            floor = self.min_grade(p)
            allowed = [g for g in GRADES if reg.meets_minimum(g, floor)] or ["A"]
            weights = [GRADE_WEIGHTS[GRADES.index(g)] for g in allowed]
            out[i] = self.rng.choices(allowed, weights)[0]
        return out

    def terms(self):
        """Assign each pick a term: prerequisites first, lower levels first, about 16 sh a term."""
        terms = term_range(self.data["student"]["catalog_year"], self.fab.get("through", "Spring 2026"))
        load = {t: 0.0 for t in terms}
        for r in self.fixed:
            if r.get("term") in load:
                load[r["term"]] += r.get("credit_hours", self.credits(r["course_id"]))
        prereq = {}
        for p in self.data.get("prerequisites", []):
            prereq.setdefault(self.canon(p["course_id"]), set()).update(self.canon(c) for c in p["requires"])
        placed = {self.canon(r["course_id"]): terms.index(r["term"]) for r in self.fixed if r.get("term") in terms}
        # placement order: fewest prerequisite steps first, then course level (first digit of the number)
        order = sorted(range(len(self.picks)), key=lambda i: (
            self._depth(self.canon(self.picks[i]["course_id"]), prereq),
            int(self.picks[i]["course_id"][-3]) if self.picks[i]["course_id"][-3].isdigit() else 9, i))
        total = sum(p["credit_hours"] for p in self.picks) + sum(load.values())
        cap = max(12, -(-total // len(terms)) + 1)   # spread the credits evenly over the terms
        out = {}
        # Each pick goes in the first term after all its prerequisites that has room under the cap;
        # anything that does not fit lands in the last term.
        for i in order:
            course = self.canon(self.picks[i]["course_id"])
            earliest = max([placed[c] + 1 for c in prereq.get(course, ()) if c in placed] or [0])
            k = earliest
            while k < len(terms) - 1 and load[terms[k]] + self.picks[i]["credit_hours"] > cap:
                k += 1
            k = min(k, len(terms) - 1)
            out[i], placed[course] = terms[k], k
            load[terms[k]] += self.picks[i]["credit_hours"]
        return out

    @staticmethod
    def _depth(course, prereq, seen=()):
        """How many prerequisite steps lead to a course (0 when it needs none)."""
        if course in seen:
            return 0
        return 1 + max([_Builder._depth(c, prereq, seen + (course,)) for c in prereq.get(course, ())] or [-1])


def _title(course, prog):
    """The Section Tally's title, else the guide's (when it reads as one title)."""
    tally = _tally().get(course, (None,))[0]
    if tally:
        return tally
    guide = prog.get("course_titles", {}).get(course, "")
    return guide if guide and " OR " not in f" {guide} " and not guide.endswith(" OR") else None


def fabricate(data: dict, fab: dict, prog: dict) -> dict:
    """A transcript for `data` (see the module docstring); raises if the spec cannot be met."""
    b = _Builder(data, fab, prog)
    requirements = data["program_requirements"]
    for req_id in _expand(fab.get("complete", []), requirements):
        if not reg.waived(data, b.reqs[req_id]):
            b.satisfy(req_id)
    grades, terms = b.grades(), b.terms()

    rows = []
    for r in b.fixed:
        row = {"term": r["term"], "course_id": r["course_id"], "campus": r.get("campus", "Glassboro"),
               "level": r.get("level", "UG")}
        title = r.get("title") or _title(r["course_id"], prog)
        if title:
            row["title"] = title
        row.update(grade=r["grade"], credit_hours=r.get("credit_hours", b.credits(r["course_id"])))
        if r.get("attributes"):
            row["attributes"] = r["attributes"]
        rows.append(row)
    for i, p in enumerate(b.picks):
        row = {"term": terms[i], "course_id": p["course_id"], "campus": "Glassboro", "level": "UG"}
        title = _title(p["course_id"], prog)
        if title:
            row["title"] = title
        row.update(grade=grades[i], credit_hours=p["credit_hours"])
        if p["attributes"]:
            row["attributes"] = p["attributes"]
        rows.append(row)
    term_index = {t: k for k, t in enumerate(term_range("Fall 1990", "Fall 2030"))}
    rows.sort(key=lambda r: (term_index.get(r["term"], 0), r["course_id"]))

    def titled(r, keys):
        """A fixed transfer or in-progress row in the input format, with its title when known."""
        row = {k: r[k] for k in keys if k in r}
        title = r.get("title") or _title(r["course_id"], prog)
        return {**row, **({"title": title} if title else {}), "credit_hours": r["credit_hours"]}
    # fabricated students are first-year entrants; transfer and in-progress rows come only from the spec
    transcript = {"completed_courses": rows,
                  "transfer_courses": [titled(r, ("institution", "course_id", "grade")) for r in b.transfers],
                  "courses_in_progress": [titled(r, ("term", "course_id", "campus", "level")) for r in b.in_progress]}

    status = reg.audit({**data, "transcript": transcript})["requirements"]
    unmet = [i for i in _expand(fab.get("complete", []), requirements) if not status[i]["satisfied"]]
    met = [i for i in _expand(fab.get("keep_unmet", []), requirements) if status[i]["satisfied"]]
    if unmet or met:
        raise ValueError(f"fabricate: requirements {unmet} should be met and {met} should not be")
    return _edit(transcript, fab.get("edits", []), prog)


def _edit(transcript: dict, edits: list, prog: dict) -> dict:
    """Applies the spec's `edits` to the finished transcript, after the checks above: each sets
    fields of the one row of `in` (a transcript list) whose course_id matches. Nothing is
    re-fabricated, so a pair's twin can be its base transcript with exactly one row changed.
    A new course_id brings that course's title."""
    for e in edits:
        rows = [r for r in transcript[e["in"]] if r["course_id"] == e["course_id"]]
        if len(rows) != 1:
            raise ValueError(f"fabricate: edit needs one {e['course_id']} row in {e['in']}, found {len(rows)}")
        rows[0].update(e["set"])
        if "course_id" in e["set"]:
            rows[0].pop("title", None)
            title = e["set"].get("title") or _title(e["set"]["course_id"], prog)
            if title:
                rows[0]["title"] = title
    return transcript
