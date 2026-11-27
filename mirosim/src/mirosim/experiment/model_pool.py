"""为每个 persona 按分群稳定、均衡地分配一个思考模型。"""
from __future__ import annotations

from collections import Counter, defaultdict

from mirosim.core.rng import u01
from mirosim.experiment.sampling import segment_of


def assign_models(personas: list[dict], frame: dict, members: list[str], existing: dict[str, str],
                  seed: int) -> dict[str, str]:
    if len(set(members)) != len(members):
        raise ValueError("persona_pool 的 member 编号不能重复")
    if not members and personas:
        raise ValueError("persona_pool 至少需要一个 member")

    member_set = set(members)
    groups: dict[str, list[dict]] = defaultdict(list)
    counts: dict[str, Counter] = defaultdict(Counter)
    assignments = {}
    seen = set()
    for persona in personas:
        persona_id = persona["persona_id"]
        if persona_id in seen:
            raise ValueError(f"人物编号重复：{persona_id}")
        seen.add(persona_id)
        segment_id = segment_of(persona, frame)
        if segment_id is None:
            segment_id = "_unassigned"
        member = existing.get(persona_id)
        if member in member_set:
            assignments[persona_id] = member
            counts[segment_id][member] += 1
        else:
            groups[segment_id].append(persona)

    for segment_id, group in groups.items():
        for persona in sorted(group, key=lambda item: item["persona_id"]):
            persona_id = persona["persona_id"]
            member = min(
                members,
                key=lambda candidate: (
                    counts[segment_id][candidate],
                    u01(seed, "model_pool", persona_id, candidate),
                    candidate,
                ),
            )
            assignments[persona_id] = member
            counts[segment_id][member] += 1

    return {persona_id: assignments[persona_id] for persona_id in sorted(assignments)}
