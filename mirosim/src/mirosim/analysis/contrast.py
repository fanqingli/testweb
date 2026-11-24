"""同一批人物在两个版本之间的配对差（骨架）。未加权；未曝光或没有数值答案的人物不成对。"""
from __future__ import annotations

from mirosim.core.rng import u01


def _person_means(responses: list[dict], variant: str, measure: str) -> dict[str, float]:
    """每个人物在该版本下、各次重复的平均分。"""
    scores: dict[str, list[int]] = {}
    for r in responses:
        value = r.get("payload", {}).get("answers", {}).get(measure) if r["variant_id"] == variant and r["valid"] else None
        if isinstance(value, int) and not isinstance(value, bool):
            scores.setdefault(r["persona_id"], []).append(value)
    return {pid: sum(v) / len(v) for pid, v in scores.items()}


def paired_contrast(responses: list[dict], measure: str, variant: str, baseline: str, *,
                    seed: int, n_boot: int = 1000) -> dict:
    """variant − baseline 的人均配对差，及对人物做自助抽样得到的 95% 区间。"""
    a, b = _person_means(responses, variant, measure), _person_means(responses, baseline, measure)
    pids = sorted(set(a) & set(b))
    out = {"variant": variant, "baseline": baseline, "measure": measure, "n_pairs": len(pids), "mean_diff": None, "ci95": None}
    if not pids:
        return out
    diffs = [a[p] - b[p] for p in pids]
    n = len(diffs)
    boots = sorted(
        sum(diffs[int(u01(seed, "boot", measure, variant, baseline, i, j) * n)] for j in range(n)) / n
        for i in range(n_boot)
    )
    out["mean_diff"] = round(sum(diffs) / n, 4)
    out["ci95"] = [round(boots[int(0.025 * n_boot)], 4), round(boots[min(n_boot - 1, int(0.975 * n_boot))], 4)]
    return out


def contrast_table(responses: list[dict], stimulus: dict, measure: str, *, seed: int, n_boot: int = 1000) -> dict:
    """各版本对基线（现状版本；没有则取第一个版本）的配对差，以及安慰剂对其对应版本的差（噪声底）。"""
    present = {r["variant_id"] for r in responses}
    variants = [v for v in stimulus["variants"] if v["variant_id"] in present]
    baseline = next((v["variant_id"] for v in variants if v["role"] == "status_quo"), variants[0]["variant_id"])
    effects = [
        paired_contrast(responses, measure, v["variant_id"], baseline, seed=seed, n_boot=n_boot)
        for v in variants if v["variant_id"] != baseline and v["role"] != "placebo"
    ]
    noise = [
        paired_contrast(responses, measure, v["variant_id"], v["placebo_of"], seed=seed, n_boot=n_boot)
        for v in variants if v["role"] == "placebo" and v.get("placebo_of") in present
    ]
    return {"measure": measure, "baseline": baseline, "weighted": False, "effects": effects, "noise_floor": noise}
