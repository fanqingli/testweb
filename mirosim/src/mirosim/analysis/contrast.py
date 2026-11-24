"""同一批人物在两个版本之间的配对差（骨架）。未曝光或没有数值答案的人物不成对。

给了权重就同时报加权与不加权两个数（docs/05 §2）。两种区间用同一组自助抽样（对人物整体重抽，
没有按分群分层），反映的是人物抽样与模型采样的波动，不是对真实人群的推断区间。
"""
from __future__ import annotations

from mirosim.core.rng import u01
from mirosim.experiment.sampling import n_eff


def _person_means(responses: list[dict], variant: str, measure: str) -> dict[str, float]:
    """每个人物在该版本下、各次重复的平均分。"""
    scores: dict[str, list[int]] = {}
    for r in responses:
        value = r.get("payload", {}).get("answers", {}).get(measure) if r["variant_id"] == variant and r["valid"] else None
        if isinstance(value, int) and not isinstance(value, bool):
            scores.setdefault(r["persona_id"], []).append(value)
    return {pid: sum(v) / len(v) for pid, v in scores.items()}


def paired_contrast(responses: list[dict], measure: str, variant: str, baseline: str, *,
                    seed: int, n_boot: int = 1000, weights: dict[str, float] | None = None) -> dict:
    """variant − baseline 的人均配对差，及对人物做自助抽样得到的 95% 区间。

    weights 是人物编号到代表性权重的映射；给了就多报一组加权结果，没有权重的人物不参与加权。
    """
    a, b = _person_means(responses, variant, measure), _person_means(responses, baseline, measure)
    pids = sorted(set(a) & set(b))
    out = {"variant": variant, "baseline": baseline, "measure": measure, "n_pairs": len(pids), "mean_diff": None, "ci95": None}
    if not pids:
        return out
    diffs = [a[p] - b[p] for p in pids]
    n = len(diffs)
    picks = [[int(u01(seed, "boot", measure, variant, baseline, i, j) * n) for j in range(n)] for i in range(n_boot)]

    def estimate(w: list[float]) -> tuple[float, list[float]]:
        def mean(idx):
            total = sum(w[k] for k in idx)
            return sum(w[k] * diffs[k] for k in idx) / total if total else None
        boots = sorted(m for m in map(mean, picks) if m is not None)
        lo, hi = boots[int(0.025 * len(boots))], boots[min(len(boots) - 1, int(0.975 * len(boots)))]
        return round(mean(range(n)), 4) + 0.0, [round(lo, 4) + 0.0, round(hi, 4) + 0.0]  # + 0.0：不输出 -0.0

    out["mean_diff"], out["ci95"] = estimate([1.0] * n)
    if weights is not None:
        w = [weights.get(p, 0.0) for p in pids]
        out["weighted"] = {"n_pairs": sum(x > 0 for x in w), "mean_diff": None, "ci95": None,
                           "n_eff": n_eff([x for x in w if x > 0])}
        if any(w):
            out["weighted"]["mean_diff"], out["weighted"]["ci95"] = estimate(w)
    return out


def contrast_table(responses: list[dict], stimulus: dict, measure: str, *, seed: int, n_boot: int = 1000,
                   weights: dict[str, float] | None = None) -> dict:
    """各版本对基线（现状版本；没有则取第一个版本）的配对差，以及安慰剂对其对应版本的差（噪声底）。"""
    present = {r["variant_id"] for r in responses}
    variants = [v for v in stimulus["variants"] if v["variant_id"] in present]
    baseline = next((v["variant_id"] for v in variants if v["role"] == "status_quo"), variants[0]["variant_id"])
    effects = [
        paired_contrast(responses, measure, v["variant_id"], baseline, seed=seed, n_boot=n_boot, weights=weights)
        for v in variants if v["variant_id"] != baseline and v["role"] != "placebo"
    ]
    noise = [
        paired_contrast(responses, measure, v["variant_id"], v["placebo_of"], seed=seed, n_boot=n_boot, weights=weights)
        for v in variants if v["role"] == "placebo" and v.get("placebo_of") in present
    ]
    return {"measure": measure, "baseline": baseline, "weighted": weights is not None, "effects": effects, "noise_floor": noise}
