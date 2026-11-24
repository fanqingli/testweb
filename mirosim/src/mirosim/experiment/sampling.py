"""按人群框架从人物库里取人，并给出代表性权重（docs/05 §2）。

未做：库内人数不足时由人物工厂补人（缺口只如实报告）、分群内按 spread_fields 拉开差异、
过度使用的人物降权、reuse_min_ratio、按关系成组抽取。
"""
from __future__ import annotations

from mirosim.core.rng import u01


def _field(doc: dict, path: str):
    for key in path.split("."):
        if not isinstance(doc, dict) or key not in doc:
            return None
        doc = doc[key]
    return doc


def matches(persona: dict, flt: dict) -> bool:
    """各条件同时满足；字段是列表时，与允许取值有交集即满足。"""
    for path, allowed in flt.items():
        value = _field(persona, path)
        values = value if isinstance(value, list) else [value]
        if not any(v in allowed for v in values):
            return False
    return True


def segment_of(persona: dict, frame: dict) -> str | None:
    """分群条件有重叠时，归入框架里排在最前的那个分群。"""
    return next((s["segment_id"] for s in frame["segments"] if matches(persona, s["filter"])), None)


def draw_sample(frame: dict, personas: list[dict], seed: int) -> dict:
    """每个分群取 n 人。抽签的键只有种子、框架和人物，所以各版本、各次重复用的是同一批人。

    权重 w_s = share_s / (n_s / N)，n_s 与 N 取实际抽到的人数。库里没有人的分群不进入加权，
    covered_share 是抽到了人的分群占比之和：它小于 1 时，加权数字只代表这一部分人群。
    """
    pool: dict[str, list[dict]] = {s["segment_id"]: [] for s in frame["segments"]}
    unmatched = []
    for p in sorted(personas, key=lambda p: p["persona_id"]):
        sid = segment_of(p, frame)
        (pool[sid] if sid else unmatched).append(p)
    chosen = {
        s["segment_id"]: sorted(
            sorted(pool[s["segment_id"]], key=lambda p: u01(seed, "sample", frame["frame_id"], p["persona_id"]))[: s["n"]],
            key=lambda p: p["persona_id"])
        for s in frame["segments"]
    }
    total = sum(len(v) for v in chosen.values())
    segments, members = [], []
    for s in frame["segments"]:
        got = chosen[s["segment_id"]]
        weight = round(s["share"] / (len(got) / total), 6) if got else None
        segments.append({
            "segment_id": s["segment_id"], "share": s["share"], "n_target": s["n"],
            "n_pool": len(pool[s["segment_id"]]), "n_sampled": len(got), "weight": weight,
        })
        members += [{"persona_id": p["persona_id"], "segment_id": s["segment_id"], "weight": weight} for p in got]
    members.sort(key=lambda m: m["persona_id"])
    return {
        "frame_id": frame["frame_id"],
        "segments": segments,
        "members": members,
        "unmatched": [p["persona_id"] for p in unmatched],
        "n_sampled": total,
        "shortfall": sum(s["n_target"] - s["n_sampled"] for s in segments),
        "covered_share": round(sum(s["share"] for s in segments if s["n_sampled"]), 6),
        "n_eff": n_eff([m["weight"] for m in members]),
    }


def n_eff(weights: list[float]) -> float | None:
    """有效样本量 (Σw)² / Σw²。没有正权重（没有人，或只取到占比为 0 的分群）时为 None。"""
    squares = sum(w * w for w in weights)
    return round(sum(weights) ** 2 / squares, 2) if squares else None
