"""Task 2.9: statistics on the FINALIZED results (effort, latency, case type vs accuracy).

Reads Khalil's CSV export (results/FINALIZED_RESULTS/analysis/) and writes tables plus a
summary to an output folder. Follows the rules in Reading_the_Results_Files.docx:
  - an answer is correct only when semantic_ok == 1
  - calls with outcome == generation_failed are dropped before computing accuracy
  - compare the 7 groups before single checks (each check has only 4 to 6 cases)

Builds on the team's code: uses pipeline/stats.py (Wilson intervals and the exact McNemar test,
the same functions behind --compare and the session summaries). Run it on the Khalil-context-path
branch, which has pipeline/ and the same FINALIZED_RESULTS files.

Usage (from the repository root):
    py stats_2_9.py
    py stats_2_9.py --analysis-dir results/FINALIZED_RESULTS/analysis --out results/stats_2_9

Needs pandas and scipy:  py -m pip install pandas scipy
"""

import argparse
import os

import numpy as np
import pandas as pd
import warnings

from scipy import stats

warnings.filterwarnings("ignore", category=stats.ConstantInputWarning)

from pipeline.stats import mcnemar, wilson  # the team's own statistics

EFFORTS = ["low", "medium", "high"]


def holm(pvals):
    """Holm-Bonferroni adjusted p-values (same order as input). NaN stays NaN."""
    p = np.asarray(pvals, dtype=float)
    out = np.full_like(p, np.nan)
    ok = ~np.isnan(p)
    idx = np.where(ok)[0]
    order = idx[np.argsort(p[idx])]
    m = len(order)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * p[i]))
        out[i] = running
    return out


def cochran_q(matrix):
    """Cochran's Q test. matrix: rows = cases, columns = conditions, values 0/1."""
    x = np.asarray(matrix, dtype=float)
    k = x.shape[1]
    col = x.sum(axis=0)
    row = x.sum(axis=1)
    n = x.sum()
    denom = k * n - (row ** 2).sum()
    if denom == 0:  # every case scored the same at every effort
        return 0.0, 1.0
    q = (k - 1) * (k * (col ** 2).sum() - n ** 2) / denom
    return q, stats.chi2.sf(q, k - 1)


def load(analysis_dir):
    calls = pd.read_csv(os.path.join(analysis_dir, "main.csv"))
    sessions = pd.read_csv(os.path.join(analysis_dir, "main_sessions.csv"))
    n_all = len(calls)
    calls = calls[calls["outcome"] != "generation_failed"].copy()
    return calls, sessions, n_all


def short(model):
    return model.split("/")[-1].replace(":free", "")


def table_models(calls, sessions):
    """Table 1: headline numbers per model x effort, from Khalil's session export."""
    cols = ["model", "effort", "calls", "generation_failures", "semantic_ok_rate",
            "semantic_ok_ci_low", "semantic_ok_ci_high", "paired_rate", "strong_pairs_rate",
            "mean_latency_ms", "reasoning_tokens", "cost_usd", "per_correct_answer_usd"]
    t = sessions[cols].copy()
    # Cross-check: our accuracy from main.csv must match the session export.
    mine = calls.groupby(["model", "effort"])["semantic_ok"].mean().rename("acc_from_main_csv")
    t = t.merge(mine.reset_index(), on=["model", "effort"], how="left")
    t["matches_export"] = (t["acc_from_main_csv"] - t["semantic_ok_rate"]).abs() < 0.0005
    t["effort"] = pd.Categorical(t["effort"], EFFORTS, ordered=True)
    return t.sort_values(["model", "effort"]).reset_index(drop=True)


def table_effort(calls):
    """Table 2: effort vs accuracy per model, Cochran's Q on the same cases."""
    rows = []
    for model, g in calls.groupby("model"):
        wide = g.pivot_table(index="task_id", columns="effort", values="semantic_ok", aggfunc="first")
        wide = wide.reindex(columns=EFFORTS).dropna()  # only cases answered at all 3 efforts
        q, p = cochran_q(wide.values)
        acc = wide.mean()
        def mc(a, b):  # team's exact McNemar on the same cases
            return mcnemar(int(((wide[a] == 1) & (wide[b] == 0)).sum()),
                           int(((wide[b] == 1) & (wide[a] == 0)).sum()))
        rows.append({
            "model": model, "cases_compared": len(wide),
            "acc_low": acc["low"], "acc_medium": acc["medium"], "acc_high": acc["high"],
            "best_effort": acc.idxmax(), "high_minus_low": acc["high"] - acc["low"],
            "cochran_q": q, "p_value": p,
            "mcnemar_p_low_vs_medium": mc("low", "medium"),
            "mcnemar_p_medium_vs_high": mc("medium", "high"),
        })
    t = pd.DataFrame(rows)
    t["p_holm"] = holm(t["p_value"])
    t["significant_holm_0.05"] = t["p_holm"] < 0.05
    return t.sort_values("acc_medium", ascending=False).reset_index(drop=True)


def table_numeric_vs_correct(calls, column, label):
    """Tables 3/4: does a numeric column (latency, reasoning tokens) relate to correctness?
    Computed within each model x effort session, so slow models are not compared with fast ones."""
    rows = []
    for (model, effort), g in calls.groupby(["model", "effort"]):
        g = g.dropna(subset=[column])
        right = g.loc[g["semantic_ok"] == 1, column]
        wrong = g.loc[g["semantic_ok"] == 0, column]
        if len(right) < 5 or len(wrong) < 5:
            rho, p = np.nan, np.nan
        else:
            rho, p = stats.spearmanr(g[column], g["semantic_ok"])
        rows.append({
            "model": model, "effort": effort, "n": len(g),
            f"median_{label}_right": right.median(), f"median_{label}_wrong": wrong.median(),
            "spearman_rho": rho, "p_value": p,
        })
    t = pd.DataFrame(rows)
    t["p_holm"] = holm(t["p_value"])
    t["significant_holm_0.05"] = t["p_holm"] < 0.05
    t["effort"] = pd.Categorical(t["effort"], EFFORTS, ordered=True)
    return t.sort_values(["model", "effort"]).reset_index(drop=True)


def table_groups(calls):
    """Table 5: accuracy by check group, pooled and per model, with chi-square tests."""
    pooled = (calls.groupby("group")
              .agg(calls=("semantic_ok", "size"), accuracy=("semantic_ok", "mean"),
                   mean_chance=("chance", "mean"))
              .sort_values("accuracy"))
    pooled["lift_over_chance"] = pooled["accuracy"] - pooled["mean_chance"]
    correct = calls.groupby("group")["semantic_ok"].sum().loc[pooled.index]
    ci = [wilson(int(k), int(n)) for k, n in zip(correct, pooled["calls"])]
    pooled["ci95_low"] = [c[0] for c in ci]
    pooled["ci95_high"] = [c[1] for c in ci]
    per_model = calls.pivot_table(index="group", columns="model", values="semantic_ok", aggfunc="mean")
    per_model.columns = [short(c) for c in per_model.columns]
    per_model = per_model.loc[pooled.index]

    tests = []
    ct = pd.crosstab(calls["group"], calls["semantic_ok"])
    chi2, p, dof, _ = stats.chi2_contingency(ct)
    tests.append({"model": "ALL (pooled)", "chi2": chi2, "dof": dof, "p_value": p})
    for model, g in calls.groupby("model"):
        ct = pd.crosstab(g["group"], g["semantic_ok"])
        if ct.shape[1] < 2:
            continue
        chi2, p, dof, _ = stats.chi2_contingency(ct)
        tests.append({"model": model, "chi2": chi2, "dof": dof, "p_value": p})
    tests = pd.DataFrame(tests)
    tests["p_holm"] = holm(tests["p_value"])
    return pooled.reset_index(), per_model.reset_index(), tests


def table_checks(calls):
    """Table 6: accuracy by single check (follow-up only: 4 to 6 cases per check)."""
    t = (calls.groupby(["check", "check_name", "group"])
         .agg(cases=("task_id", "nunique"), calls=("semantic_ok", "size"),
              accuracy=("semantic_ok", "mean"), mean_chance=("chance", "mean"))
         .reset_index())
    t["lift_over_chance"] = t["accuracy"] - t["mean_chance"]
    return t.sort_values("accuracy").reset_index(drop=True)


def fmt_p(p):
    return "< 0.001" if p < 0.001 else f"{p:.3f}"


CASE_FEATURES = {
    "n_rules": "rules in the request",
    "prompt_words": "words in the request",
    "transcript_courses": "courses on the transcript",
    "chance": "random-guess odds",
}


def table_case_features(calls):
    """Table 7: are harder-looking cases answered worse? The unit is the case (200 cases), so
    each case counts once: its accuracy over all model x effort sessions, against each feature."""
    per_case = (calls.groupby("task_id")
                .agg(accuracy=("semantic_ok", "mean"), sessions=("semantic_ok", "size"),
                     **{f: (f, "first") for f in CASE_FEATURES}))
    rows = []
    for f, label in CASE_FEATURES.items():
        rho, p = stats.spearmanr(per_case[f], per_case["accuracy"])
        rows.append({"feature": f, "meaning": label, "cases": len(per_case),
                     "spearman_rho": rho, "p_value": p})
    t = pd.DataFrame(rows)
    t["p_holm"] = holm(t["p_value"])
    by_rules = (per_case.groupby("n_rules")
                .agg(cases=("accuracy", "size"), mean_case_accuracy=("accuracy", "mean"))
                .reset_index())
    return t, by_rules, per_case.reset_index()


def pct(x):
    return f"{100 * x:.1f}%"


def write_summary(path, n_all, calls, models, effort, latency, think, groups, group_tests, checks,
                  features, by_rules):
    n_models = calls["model"].nunique()
    n_cases = calls["task_id"].nunique()
    by_effort = calls.groupby("effort")["semantic_ok"].mean().reindex(EFFORTS)
    lat_neg = latency[(latency["spearman_rho"] < 0) & latency["significant_holm_0.05"]]
    lat_pos = latency[(latency["spearman_rho"] > 0) & latency["significant_holm_0.05"]]
    think_neg = think[(think["spearman_rho"] < 0) & think["significant_holm_0.05"]]
    think_pos = think[(think["spearman_rho"] > 0) & think["significant_holm_0.05"]]
    sig_eff = effort[effort["significant_holm_0.05"]]
    pooled_test = group_tests.iloc[0]
    sig_group_models = group_tests.iloc[1:][group_tests.iloc[1:]["p_holm"] < 0.05]

    L = []
    L.append("# Task 2.9: statistics on the finalized results\n")
    L.append(f"Data: `main.csv` from `results/FINALIZED_RESULTS/analysis/` ({n_all} calls; "
             f"{n_all - len(calls)} `generation_failed` calls dropped, leaving {len(calls)}). "
             f"{n_models} models, {n_cases} cases, efforts low / medium / high. "
             "Correct means `semantic_ok` = 1. All tables are in this folder as CSV.\n")
    L.append(f"Cross-check: accuracy recomputed from `main.csv` matches `main_sessions.csv` "
             f"for {int(models['matches_export'].sum())} of {len(models)} sessions.\n")

    L.append("## 1. Effort vs accuracy (table_2_effort.csv)\n")
    L.append("Pooled over all models: " + ", ".join(f"{e} {pct(by_effort[e])}" for e in EFFORTS)
             + ". Pooling mixes models, so the test is run within each model: Cochran's Q on the "
             "same cases at the three efforts, Holm-adjusted across the 11 models. table_2_effort.csv also "
             "has the team's exact McNemar p for low vs medium and medium vs high (pipeline/stats.py).\n")
    L.append("| Model | Low | Medium | High | Best | p (Holm) |\n|---|---|---|---|---|---|")
    for _, r in effort.iterrows():
        L.append(f"| {short(r['model'])} | {pct(r['acc_low'])} | {pct(r['acc_medium'])} | "
                 f"{pct(r['acc_high'])} | {r['best_effort']} | {fmt_p(r['p_holm'])} |")
    L.append(f"\nSignificant effort effect (Holm p < 0.05) for {len(sig_eff)} of {len(effort)} models"
             + (": " + ", ".join(short(m) for m in sig_eff['model']) if len(sig_eff) else "") + ".\n")

    L.append("## 2. Latency vs accuracy (table_3_latency.csv)\n")
    L.append("Spearman correlation between `latency_ms` and correctness within each model x effort "
             f"session ({len(latency)} sessions, Holm-adjusted). Negative means slower answers were "
             f"more often wrong. Significant negative: {len(lat_neg)}; significant positive: "
             f"{len(lat_pos)}; not significant: {len(latency) - len(lat_neg) - len(lat_pos)}.\n")
    med_r = latency["median_latency_right"].median()
    med_w = latency["median_latency_wrong"].median()
    L.append(f"Across sessions, the typical median latency was {med_r / 1000:.1f} s for right answers "
             f"and {med_w / 1000:.1f} s for wrong answers.\n")

    L.append("## 3. Thinking (reasoning tokens) vs accuracy (table_4_reasoning_tokens.csv)\n")
    L.append("Same method with `reasoning_tokens` (the guide says to compare thinking by tokens, "
             f"not characters). Significant negative: {len(think_neg)}; significant positive: "
             f"{len(think_pos)}; not significant or no data: "
             f"{len(think) - len(think_neg) - len(think_pos)} of {len(think)} sessions.\n")

    L.append("## 4. Case type vs accuracy (table_5_groups*.csv, table_6_checks.csv)\n")
    L.append("| Group | Calls | Accuracy (95% Wilson CI) | Chance | Lift over chance |\n|---|---|---|---|---|")
    for _, r in groups.iterrows():
        L.append(f"| {r['group']} | {r['calls']} | {pct(r['accuracy'])} ({pct(r['ci95_low'])}-{pct(r['ci95_high'])}) | {pct(r['mean_chance'])} | "
                 f"{100 * r['lift_over_chance']:+.1f} pts |")
    L.append(f"\nChi-square test of group vs correctness, pooled: chi2 = {pooled_test['chi2']:.1f}, "
             f"dof = {int(pooled_test['dof'])}, p = {pooled_test['p_value']:.2g}. Within single models, "
             f"group matters significantly (Holm p < 0.05) for {len(sig_group_models)} of "
             f"{len(group_tests) - 1} models.\n")
    L.append("Hardest single checks (follow-up only: each check has just 4 to 6 cases, so these are uncertain):\n")
    for _, r in checks.head(5).iterrows():
        L.append(f"- {r['check']} {r['check_name']}: {pct(r['accuracy'])} "
                 f"(chance {pct(r['mean_chance'])}, {r['cases']} cases)")
    L.append("")

    L.append("## 5. Case difficulty vs accuracy (table_7_case_features.csv, table_7b_by_rules.csv)\n")
    L.append("Each case counted once: its accuracy over all sessions that answered it, against "
             "features of the case (Spearman over the cases, Holm-adjusted).\n")
    L.append("| Feature | rho | p (Holm) |\n|---|---|---|")
    for _, r in features.iterrows():
        L.append(f"| {r['meaning']} (`{r['feature']}`) | {r['spearman_rho']:+.2f} | {fmt_p(r['p_holm'])} |")
    L.append("\nMean case accuracy by number of rules in the request:\n")
    L.append("| Rules | Cases | Mean accuracy |\n|---|---|---|")
    for _, r in by_rules.iterrows():
        L.append(f"| {int(r['n_rules'])} | {int(r['cases'])} | {pct(r['mean_case_accuracy'])} |")
    L.append("")

    L.append("## Limits\n")
    L.append("- One sample per case per effort; the 3-sample stability run is in `check.csv` and is not used here.")
    L.append("- Tyler revised the D-category cases after this run (PR #7); these results use the earlier D cases.")
    L.append("- Latency includes provider queueing. The free Nemotron models are rate-limited and "
             "Lightning's run was resumed, so their latencies are not comparable with paid models.")
    L.append("- OpenAI and Gemini return a reasoning summary; token counts are used for comparison.")
    L.append("- Correlation is not causation: harder cases can make a model both slower and more often wrong.")
    L.append("- Spearman on a 0/1 outcome is a rank correlation; it says whether wrong answers tend to "
             "be slower, not by how much.")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--analysis-dir", default=os.path.join("results", "FINALIZED_RESULTS", "analysis"))
    ap.add_argument("--out", default=os.path.join("results", "stats_2_9"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    calls, sessions, n_all = load(args.analysis_dir)
    models = table_models(calls, sessions)
    effort = table_effort(calls)
    latency = table_numeric_vs_correct(calls, "latency_ms", "latency")
    think = table_numeric_vs_correct(calls, "reasoning_tokens", "reasoning_tokens")
    groups, groups_by_model, group_tests = table_groups(calls)
    checks = table_checks(calls)
    features, by_rules, per_case = table_case_features(calls)

    out = lambda name: os.path.join(args.out, name)
    models.to_csv(out("table_1_models.csv"), index=False)
    effort.to_csv(out("table_2_effort.csv"), index=False)
    latency.to_csv(out("table_3_latency.csv"), index=False)
    think.to_csv(out("table_4_reasoning_tokens.csv"), index=False)
    groups.to_csv(out("table_5_groups.csv"), index=False)
    groups_by_model.to_csv(out("table_5b_groups_by_model.csv"), index=False)
    group_tests.to_csv(out("table_5c_group_tests.csv"), index=False)
    checks.to_csv(out("table_6_checks.csv"), index=False)
    features.to_csv(out("table_7_case_features.csv"), index=False)
    by_rules.to_csv(out("table_7b_by_rules.csv"), index=False)
    per_case.to_csv(out("table_7c_per_case.csv"), index=False)
    write_summary(out("summary.md"), n_all, calls, models, effort, latency, think,
                  groups, group_tests, checks, features, by_rules)
    print(f"Wrote 12 files to {args.out}")


if __name__ == "__main__":
    main()
