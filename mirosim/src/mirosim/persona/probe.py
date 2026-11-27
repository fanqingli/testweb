"""特质探针：检验卡上的性格是否出现在人物的选择里。设计见 prompts/trait_probe.md。

每题独立调用，问的是「你会怎么做」，不让人物自报性格。A/B 先后按种子打乱，抵消位置偏好。
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import yaml

from mirosim import ROOT
from mirosim.core.rng import u01
from mirosim.engines.panel import _first_json_object, in_order
from mirosim.persona.behavior import AXES, normalized_inputs
from mirosim.persona.render import load_template, system_prompt

TRAITS = ("emotional_stability", "risk_tolerance", "institutional_trust", "conformity", "price_sensitivity")
DIMS = AXES + TRAITS
PER_DIM = 6
MAX_ATTEMPTS = 2
THRESHOLDS = {"trait_recovery": 0.70, "retest_consistency": 0.80, "distinctiveness": 0.25, "separability": 0.40}


def load_probes(path: Path) -> list[dict]:
    probes = yaml.safe_load(Path(path).read_text(encoding="utf-8"))["probes"]
    ids = [p["id"] for p in probes]
    if len(set(ids)) != len(ids):
        raise ValueError("探针题 id 重复")
    counts = Counter(p["dim"] for p in probes)
    if set(counts) != set(DIMS) or any(n != PER_DIM for n in counts.values()):
        raise ValueError(f"每个维度须恰好 {PER_DIM} 题，维度须为 {DIMS}；现为 {dict(counts)}")
    for p in probes:
        if sorted(o["sign"] for o in (p["options"]["a"], p["options"]["b"])) != [-1, 1]:
            raise ValueError(f"{p['id']} 的两个选项须一正一负")
    return probes


def card_sign(persona: dict, dim: str) -> int:
    """卡上分值的方向。边界轴（|分值|<0.2）与中间特质（0.35–0.65）返回 0，不参与还原度。"""
    value = normalized_inputs(persona)[dim]
    if dim in AXES:
        middle = abs(value) < 0.2
    else:  # 与 soft_text 一致：0.35 与 0.65 本身也算中间档，提示词里不写
        middle = abs(value) <= 0.3 + 1e-9
    return 0 if middle else (1 if value > 0 else -1)


def _parse(text: str) -> tuple[str | None, str | None]:
    text = re.sub(r"^\s*<think>.*?</think>", "", text, count=1, flags=re.S)  # 与 panel 一致：开头的推理段不算答案
    try:
        choice = _first_json_object(text).get("choice")
    except ValueError as error:
        return None, f"不是合法的 JSON：{error}"
    if choice not in ("A", "B"):
        return None, "choice 只能是 A 或 B"
    return choice, None


def run_probes(personas: list[dict], probes: list[dict], gateway, *, seed: int, as_of_date: str, sessions: int = 1,
               assignments: dict | None = None, root: Path = ROOT, concurrency: int = 1) -> list[dict]:
    """返回每人每题每遍一行：sign 为所选做法的方向，两次都答不合格时为 None。"""
    behavior_cfg, text_cfg = (yaml.safe_load((root / "config" / f"{name}.yaml").read_text(encoding="utf-8"))
                              for name in ("mbti_behavior_map", "persona_text"))
    templates = {name: load_template(root, name) for name in ("persona_system", "trait_probe")}

    def ask(job: tuple) -> dict:
        session, persona, probe = job
        pid = persona["persona_id"]
        a_first = u01(seed, pid, "probe_order", probe["id"]) < 0.5  # 两遍用同一顺序，重测只差调用种子
        shown = dict(zip("AB", ("a", "b") if a_first else ("b", "a")))
        system = system_prompt(templates["persona_system"], persona, behavior_cfg, text_cfg, as_of_date)
        user = templates["trait_probe"].render(
            situation=probe["situation"], options={k: probe["options"][shown[k.upper()]] for k in ("a", "b")})
        run_id = f"probe_s{session}"
        row = {"persona_id": pid, "probe_id": probe["id"], "dim": probe["dim"], "session": session, "sign": None}
        member = assignments[pid] if assignments is not None else None
        if member is not None:
            row["model_member"] = member
        reason = None
        for attempt in range(MAX_ATTEMPTS):
            retry = f"\n\n你上一次的输出不合格：{reason}。请重新只输出一个 JSON 对象。" if attempt else ""
            kwargs = dict(system=system, user=user + retry, purpose="probe", run_id=run_id, ordinal=attempt,
                          seed=int(u01(seed, "probe_call", session, pid, probe["id"], attempt) * 2**32), spec={"probe": True})
            if member is not None:
                kwargs["member"] = member
            choice, reason = _parse(gateway.call(**kwargs).text)
            if choice is not None:
                row["sign"] = probe["options"][shown[choice]]["sign"]
                break
        return row

    jobs = [(session, persona, probe) for session in range(1, sessions + 1)
            for persona in sorted(personas, key=lambda p: p["persona_id"]) for probe in probes]
    return in_order(ask, jobs, concurrency, on_fail=gateway.stop)


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks, i = [0.0] * len(values), 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sx, sy = sum((v - mx) ** 2 for v in rx), sum((v - my) ** 2 for v in ry)
    if sx == 0 or sy == 0:
        return None
    return round(sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / (sx * sy) ** 0.5, 3)


def score(rows: list[dict], personas: list[dict], groups: dict[str, str] | None = None) -> dict:
    """groups：persona_id → 分群，distinctiveness 在同一分群内比较；不给则全库一起比。
    各指标对照 THRESHOLDS 只作提示，是否重生成人物由人决定。"""
    by_id = {p["persona_id"]: p for p in personas}
    answers: dict = {}
    for row in rows:
        answers.setdefault(row["persona_id"], {}).setdefault(row["session"], {})[row["probe_id"]] = row
    people = {}
    for pid, by_session in sorted(answers.items()):
        first = by_session[1]
        dims = {}
        for dim in DIMS:
            signs = [r["sign"] for r in first.values() if r["dim"] == dim and r["sign"] is not None]
            dims[dim] = round(sum(signs) / len(signs), 3) if signs else None
        judged = [(card_sign(by_id[pid], dim), dims[dim]) for dim in DIMS]
        judged = [(card, probe) for card, probe in judged if card and probe is not None]
        entry = {
            "scores": dims,
            "invalid": sum(r["sign"] is None for r in first.values()),
            "trait_recovery": round(sum(card * probe > 0 for card, probe in judged) / len(judged), 3) if judged else None,
        }
        if 2 in by_session:
            pairs = [(first[q]["sign"], by_session[2][q]["sign"]) for q in first if q in by_session[2]]
            pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
            entry["retest_consistency"] = round(sum(a == b for a, b in pairs) / len(pairs), 3) if pairs else None
        if "model_member" in next(iter(first.values())):
            entry["model_member"] = next(iter(first.values()))["model_member"]
        people[pid] = entry
    for pid, entry in people.items():
        mine = answers[pid][1]
        distances = []
        for other in people:
            if other == pid or (groups is not None and groups.get(other) != groups.get(pid)):
                continue
            shared = [q for q in mine if mine[q]["sign"] is not None and answers[other][1].get(q, {}).get("sign") is not None]
            if shared:
                distances.append(sum(mine[q]["sign"] != answers[other][1][q]["sign"] for q in shared) / len(shared))
        entry["distinctiveness"] = round(sum(distances) / len(distances), 3) if distances else None

    def separability(ids: list[str]) -> dict:
        out = {}
        for dim in DIMS:
            pairs = [(normalized_inputs(by_id[pid])[dim], people[pid]["scores"][dim]) for pid in ids
                     if people[pid]["scores"][dim] is not None]
            out[dim] = spearman([a for a, _ in pairs], [b for _, b in pairs])
        return out

    result = {"thresholds": THRESHOLDS, "personas": people, "separability": separability(list(people))}
    members = sorted({e["model_member"] for e in people.values() if "model_member" in e})
    if members:
        result["by_model"] = {}
        for member in members:
            ids = [pid for pid, e in people.items() if e.get("model_member") == member]
            recoveries = [people[pid]["trait_recovery"] for pid in ids if people[pid]["trait_recovery"] is not None]
            result["by_model"][member] = {
                "personas": len(ids),
                "mean_trait_recovery": round(sum(recoveries) / len(recoveries), 3) if recoveries else None,
                "separability": separability(ids),
            }
    return result
