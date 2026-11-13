"""
Replaces the pairs of data/FINALIZED_TEST_CASES.json whose answer key is broken, in place: each
case keeps its case id, checklist code, position and credit, and gets a newly generated pair of
the same family on the real Fall 2026 tally (generate_pairs, which now names a topics course
without its topic and skips sections the student is barred from). A pair is broken when
  - its prompt named one topic of a multi-topic course while the key accepted any (TOPIC_CASES), or
  - a case's key has no answer the student may register for (ground_truth.restriction:
    no_allowed_answer or unclear; see restrictions.py).
Ben's hand-written O1 pairs (o1_shared_rule, o2_stacked_rules) have no generator: a generated
family that also tests O1 stands in. The same ids keep `--pairs 100` choosing the same 200 cases,
so `--reuse-from` re-asks the models only the replaced ones.

    python -m pipeline.replace_pairs [--dry-run]
"""
import argparse
import json
import random
import zlib
from pathlib import Path

from . import fabricate, restrictions
from . import generate_pairs as g
from .finalize import restricted_courses

PATH = Path("data/FINALIZED_TEST_CASES.json")
TOPIC_CASES = {"D3-001", "D3-002", "I5-003", "I5-004", "K4-003", "K4-004", "K8-003", "K8-004", "O3-001",
               "O3-002", "R2-005", "R2-006", "T9-003", "T9-004", "C3-007", "C3-008", "R10-005", "R10-006"}
STAND_IN = {"o1_shared_rule": ["complete_cart", "credit_limit", "quantity"],
            "o2_stacked_rules": ["complete_cart", "credit_limit", "quantity"]}


def broken_pairs(cases, topics=TOPIC_CASES):
    """pair id -> its cases (file order), for every broken pair. topics: the case ids that still
    hold a topic prompt (none once a replacement has run: the ids then hold new pairs)."""
    out = {}
    for c in cases:
        status = (c["ground_truth"].get("restriction") or {}).get("status")
        if c["case_id"] in topics or status in ("no_allowed_answer", "unclear"):
            out.setdefault(c["ground_truth"]["pair"]["id"], [])
    for c in cases:
        if c["ground_truth"]["pair"]["id"] in out:
            out[c["ground_truth"]["pair"]["id"]].append(c)
    return out


def new_pair(family, used_ids, used_sigs, programs, sid):
    """A fresh pair of `family` (two generated cases, variant a then b), or None when the family has
    no pair left that is new, allowed, and free of guide-only conditions."""
    for r, key in g.attempts(family):
        seed = f"{family}/{key}" if r == 1 else f"{family}/{key}/{r}"
        pid = f"{family}/{key}" if r == 1 else f"{family}/{key}#{r}"
        if pid in used_ids:
            continue
        got = g.FAMILIES[family](key, programs[key], random.Random(zlib.crc32(seed.encode())), f"S{next(sid)}")
        if not got or g.signature(got) in used_sigs or not g.allowed(got):
            continue
        if any(set(c.get("candidate_courses") or []) & restricted_courses(programs[key]) for c in got):
            continue
        for c in got:
            c["ground_truth"]["pair"]["id"] = pid
            c["case_id"] = f"pair-{family}-{key}" + (f"-{r}" if r > 1 else "") + f"-{c['ground_truth']['pair']['variant']}"
        g.annotate(got, full=True)
        return sorted(got, key=lambda c: c["ground_truth"]["pair"]["variant"])
    return None


def as_finalized(old, new):
    """`new` (a generated case) in `old`'s place: old's id, check, key order and credit."""
    gt = new["ground_truth"]
    pair = {k: gt["pair"].get(k) for k in old["ground_truth"]["pair"]}
    credit = {**old["ground_truth"]["credit"], "rebuilt_by": "Khalil", "rebuilt_as": new["case_id"]}
    truth = {k: (pair if k == "pair" else credit if k == "credit" else gt.get(k, old["ground_truth"][k]))
             for k in old["ground_truth"] if k != "restriction"}
    return {"case_id": old["case_id"], "semantic_check": old["semantic_check"], "input": new["input"],
            "candidate_courses": new.get("candidate_courses") or [], "ground_truth": truth}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rounds", type=int, default=g.ROUNDS,
                    help="passes over the programs per family (more find pairs a family ran out of at the default)")
    args = ap.parse_args()
    g.ROUNDS = args.rounds
    raw = PATH.read_text(encoding="utf-8")
    data = json.loads(raw)
    cases, programs = data["cases"], fabricate.load_programs()
    broken = broken_pairs(cases, set() if "replaced" in data["about"] else TOPIC_CASES)
    used_ids = {c["ground_truth"]["pair"]["id"] for c in cases}
    used_sigs = {g.signature(twins) for twins in g.pairs_by_id(cases).values()}
    sid, done, short = iter(range(9001, 99999)), {}, []
    for pid, twins in broken.items():
        family = twins[0]["ground_truth"]["case_type"]
        new = None
        for fam in STAND_IN.get(family, [family]):
            new = new_pair(fam, used_ids, used_sigs, programs, sid)
            if new:
                break
        if not new:
            short.append(pid)
            print(f"{pid}: no replacement left in {STAND_IN.get(family, [family])}")
            continue
        used_ids.add(new[0]["ground_truth"]["pair"]["id"])
        used_sigs.add(g.signature(new))
        done[pid] = new
        print(f"{pid} ({', '.join(c['case_id'] for c in twins)}) -> {new[0]['ground_truth']['pair']['id']}: "
              f"{new[0]['input']['prompt'][:90]}")
    if args.dry_run:
        return
    by_id = {}
    for pid, twins in broken.items():
        for old, new in zip(twins, done.get(pid, [])):
            by_id[old["case_id"]] = as_finalized(old, new)
    data["cases"] = [by_id.get(c["case_id"], c) for c in cases]
    data["about"].pop("rekeyed", None)
    earlier = data["about"].get("replaced")
    data["about"]["replaced"] = (earlier + " Then " if earlier else "") + (
        "2026-11-13: " + str(len(done)) + " pairs with a broken answer key were replaced in place by a newly generated "
        "pair of the same family (same case ids and checks; O1's hand-written pairs by a generated family that also "
        "tests O1): prompts that named one topic of a multi-topic course while the key accepted any, and keys whose "
        "every answer needs a section the student is barred from (restrictions.py). Replaced: "
        + "; ".join(f"{', '.join(c['case_id'] for c in broken[p])} ({p} -> {done[p][0]['ground_truth']['pair']['id']})"
                    for p in done) + ("." if not short else f". Not replaced (no pair left): {short}."))
    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + ("\n" if raw.endswith("\n") else ""), encoding="utf-8")
    restrictions.flag_cases(str(PATH))
    left = broken_pairs(json.loads(PATH.read_text(encoding="utf-8"))["cases"], set())
    print(f"replaced {len(done)} pairs; broken pairs left: {sorted(left) or 'none'}")


if __name__ == "__main__":
    main()
