"""
batch.py
--------
Many small sessions in one command (--session-per-pair), and their grand totals.

A local model cannot hold the whole Section Tally, and even the subjects of 200 cases (~250k
tokens) are too many. One pair at a time fits: its opening holds only the subjects that pair
needs (--subjects auto, per pair), a median of ~22k tokens per call. So a batch runs each pair
as its own session, appends them all to one results file, and tags every session's summary
with the same `batch` block:

    "batch": {"id": "batch-20261023T040000Z", "sessions": 100, "price_as": "...",
              "whole_tally_estimate_usd": 29.6}

batch_report() adds those sessions up (calls, accuracy, paired accuracy, money billed,
electricity, projected OpenRouter cost) at the end of the run, or later with --batch-report.

    pair_groups()    the cases split into sessions: both halves of a pair together
    spread_pairs()   --pairs N: N pairs spread evenly over the file (it is grouped by family)
"""
from .cases import read_cases
from .pairs import pair_stats, pairs_of
from .results import load_results


def pair_groups(cases: list) -> list:
    """The cases as sessions, in file order: a pair's two cases together, any case without a
    pair on its own."""
    groups, index = [], {}
    for case in cases:
        pair = (case.get("ground_truth") or {}).get("pair")
        if not pair:
            groups.append([case])
        elif pair["id"] in index:
            groups[index[pair["id"]]].append(case)
        else:
            index[pair["id"]] = len(groups)
            groups.append([case])
    return groups


def spread_pairs(raw_cases: list, n: int) -> list:
    """The case ids of n pairs spread over the families. The file is grouped by family, so the
    first n pairs would cover only a few; instead the families take turns (the first pair of
    each family, then the second of each, ...), so any n of 10 or more covers all ten."""
    pairs = pairs_of(raw_cases)
    if not 0 < n <= len(pairs):
        raise ValueError(f"--pairs must be between 1 and {len(pairs)} (the pairs in the file)")
    families = {}
    for pair_id, variants in pairs.items():                 # pair ids are "<family>/<program>"
        families.setdefault(pair_id.split("/")[0], []).append(variants)
    turns = [fam[k] for k in range(max(map(len, families.values()))) for fam in families.values() if k < len(fam)]
    return [cid for variants in turns[:n] for cid in variants.values()]


def batch_totals(case_file: str, sessions: list, results: list, batch_id: str) -> dict:
    """The grand totals of one batch, from its sessions' rows in a results file."""
    mine = [s for s in sessions if (s.get("batch") or {}).get("id") == batch_id]
    ids = {s["session_id"] for s in mine}
    rows = [r for r in results if r.get("session_id") in ids]
    ok = [r for r in rows if r.get("error") is None]
    outcomes = {(r["task_id"], r["run_index"]): r["verdict"]["outcome"] for r in rows}
    sum_of = lambda key: sum(r[key] for r in rows if r.get(key) is not None)
    info = mine[0]["batch"] if mine else {}
    return {
        "batch_id": batch_id, "model": mine[0].get("model") if mine else None,
        "backend": mine[0].get("backend") if mine else None,
        "sessions": len(mine), "sessions_planned": info.get("sessions"),
        "calls": len(rows), "failed_calls": len(rows) - len(ok),
        "semantic_ok": sum(r["verdict"]["outcome"] == "semantic_ok" for r in rows),
        "paired_accuracy": pair_stats(pairs_of(read_cases(case_file)), outcomes),
        "billed_usd": round(sum_of("usd_cost"), 6),
        "energy_wh": round(sum_of("energy_wh"), 4) if any("energy_wh" in r for r in rows) else None,
        "energy_usd": round(sum_of("energy_usd"), 6) if any("energy_usd" in r for r in rows) else None,
        "projected_usd": (round(sum_of("projected_openrouter_usd"), 6)
                          if any("projected_openrouter_usd" in r for r in rows) else None),
        "price_as": info.get("price_as"), "whole_tally_estimate_usd": info.get("whole_tally_estimate_usd"),
        "kwh_price_usd": mine[0].get("kwh_price_usd") if mine else None,
    }


def report_lines(t: dict) -> list:
    """A batch's totals as printed lines."""
    done = f"{t['sessions']}" + (f" of {t['sessions_planned']}" if t["sessions_planned"] != t["sessions"] else "")
    lines = [f"{t['batch_id']} ({t['model']} via {t['backend']}): {done} sessions, {t['calls']} calls"
             + (f" ({t['failed_calls']} failed)" if t["failed_calls"] else ""),
             f"  semantic_ok: {t['semantic_ok']}/{t['calls']}"]
    pa = t["paired_accuracy"]
    if pa:
        lines.append(f"  paired accuracy: {pa['passed']}/{pa['scored']} pairs"
                     + (f" ({100 * pa['rate']:.0f}%, 95% CI {pa['ci95'][0]:.2f}-{pa['ci95'][1]:.2f})" if pa["scored"] else "")
                     + (f"; {pa['incomplete']} incomplete" if pa["incomplete"] else ""))
    lines.append(f"  billed by OpenRouter: ${t['billed_usd']:.4f}")
    if t["energy_wh"] is not None:
        lines.append(f"  electricity: {t['energy_wh']:.3f} Wh = {t['energy_wh'] / 1000:.4f} kWh, "
                     f"${t['energy_usd']:.4f} at ${t['kwh_price_usd']:g}/kWh")
    if t["projected_usd"] is not None:
        lines.append(f"  projected on OpenRouter as {t['price_as']}: ${t['projected_usd']:.4f} as sent "
                     f"(each pair with its own small opening, written to the cache once)")
    if t["whole_tally_estimate_usd"] is not None:
        lines.append(f"  the same cases in one whole-tally session on OpenRouter: about "
                     f"${t['whole_tally_estimate_usd']:.2f} (estimate; the real experiment)")
    return lines


def batch_report(case_file: str, results_file: str) -> None:
    """--batch-report: the grand totals of every batch in a results file."""
    sessions, results = load_results(results_file)
    ids = list(dict.fromkeys((s.get("batch") or {}).get("id") for s in sessions if s.get("batch")))
    if not ids:
        print(f"no batches in {results_file} (a batch is a run with --session-per-pair)")
    for batch_id in ids:
        print("\n".join(report_lines(batch_totals(case_file, sessions, results, batch_id))))
