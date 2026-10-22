"""
stats.py
---------
The statistics a results summary uses: Wilson score intervals and McNemar's exact test.
"""



def wilson(k: int, n: int, z: float = 1.96) -> list | None:
    """The 95% Wilson score interval for k successes in n trials, as [low, high] rounded to 3."""
    if not n:
        return None
    p, d = k / n, 1 + z * z / n
    centre, half = (p + z * z / (2 * n)) / d, z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return [round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)]


def rate(k: int, n: int) -> dict:
    """{"k/n", rate, 95% interval} for a summary."""
    return {"count": f"{k}/{n}", "rate": round(k / n, 3) if n else None, "ci95": wilson(k, n)}


def mcnemar(only_a: int, only_b: int) -> float:
    """Two-sided exact McNemar p-value: of the items where A and B disagree, how surprising the
    split is if neither is really better (a binomial test at p = 0.5)."""
    from math import comb
    n = only_a + only_b
    if not n:
        return 1.0
    tail = sum(comb(n, i) for i in range(min(only_a, only_b) + 1)) / 2 ** n
    return min(1.0, 2 * tail)
