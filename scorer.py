import math

def score_from_scorer_py(issues):
    counts = {"Critical": 0, "Medium": 0, "Low": 0}
    for it in issues or []:
        sev = (it.get("severity") or "Low").strip()
        if sev not in counts:
            sev = "Low"
        counts[sev] += 1

    C, M, L = counts["Critical"], counts["Medium"], counts["Low"]

    # Dashboard-friendly score (won’t drop to 0 instantly)
    raw = 100 * math.exp(-(0.08*C + 0.03*M + 0.01*L))
    score = int(round(max(0, min(100, raw))))

    breakdown = {
        "counts": counts,
        "formula": "score = 100 * exp(-(0.08*C + 0.03*M + 0.01*L))",
        "raw": raw,
    }
    return score, breakdown