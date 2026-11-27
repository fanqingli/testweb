"""按人群框架缺口确定性生成骨架；未被过滤器指定的职业字段留给后续写作步骤。"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import random
from collections import Counter

from mirosim import ROOT
from mirosim.core.rng import u01
from mirosim.experiment.sampling import segment_of

_PERSONA_SCHEMA = json.loads((ROOT / "schemas" / "persona.schema.json").read_text(encoding="utf-8"))
_IDENTITY_PROPERTIES = _PERSONA_SCHEMA["properties"]["identity"]["properties"]
_PSYCHE_PROPERTIES = _PERSONA_SCHEMA["properties"]["psyche"]["properties"]
_GENDERS = ["male", "female"]
_EDUCATION = _IDENTITY_PROPERTIES["education"]["enum"]
_REGIONS = _IDENTITY_PROPERTIES["region_tier"]["enum"]
_INCOMES = _IDENTITY_PROPERTIES["income_band"]["enum"]
_MOTIVES = _PSYCHE_PROPERTIES["motive_type"]["enum"]
_LEANINGS = ["support", "oppose", "conditional", "indifferent"]
_VALUES = _PSYCHE_PROPERTIES["values_top3"]["items"]["enum"]
_AXES = {"ei": (0, "E"), "sn": (1, "N"), "tf": (2, "T"), "jp": (3, "J")}
_TRAITS = ("emotional_stability", "risk_tolerance", "institutional_trust", "conformity", "price_sensitivity")
_MBTI_TYPES = ["".join(letters) for letters in itertools.product("EI", "SN", "TF", "JP")]
_ENUM_FIELDS = {
    "identity.gender": _GENDERS,
    "identity.education": _EDUCATION,
    "identity.region_tier": _REGIONS,
    "identity.income_band": _INCOMES,
}


def _get_path(document: dict, path: str):
    value = document
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _set_path(document: dict, path: str, value) -> None:
    node = document
    parts = path.split(".")
    for part in parts[:-1]:
        child = node.setdefault(part, {})
        if not isinstance(child, dict):
            raise ValueError(f"过滤路径冲突：{path}")
        node = child
    node[parts[-1]] = value


def _is_array_path(path: str) -> bool:
    node = _PERSONA_SCHEMA
    for part in path.split("."):
        node = node.get("properties", {}).get(part, {})
    return node.get("type") == "array"


def _permutation(values, seed: int, frame_id: str, segment_id: str, field: str) -> list:
    return sorted(values, key=lambda value: (
        u01(seed, "skeleton", frame_id, segment_id, 0, f"{field}:{value}"), str(value)))


def _beta_sample(config: dict, seed: int, frame_id: str, segment_id: str, k: int, field: str) -> float:
    if config.get("dist") != "beta":
        raise ValueError(f"{field} 目前只支持 beta 分布")
    rng_seed = int(u01(seed, "skeleton", frame_id, segment_id, k, field) * 2**53)
    return random.Random(rng_seed).betavariate(float(config["a"]), float(config["b"]))


def _sample_table(table: dict, seed: int, frame_id: str, segment_id: str, k: int, field: str):
    entries = sorted((key, float(weight)) for key, weight in table.items())
    if not entries or any(not math.isfinite(weight) or weight < 0 for _, weight in entries):
        raise ValueError("mbti_prior.table 必须含非负的有限概率")
    total = sum(weight for _, weight in entries)
    if total <= 0:
        raise ValueError("mbti_prior.table 概率总和必须大于 0")
    draw = u01(seed, "skeleton", frame_id, segment_id, k, field) * total
    cumulative = 0.0
    for value, weight in entries:
        cumulative += weight
        if draw < cumulative:
            return value
    return entries[-1][0]


def _new_persona_id(seed: int, frame_id: str, segment_id: str, k: int, used_ids: set[str]) -> str:
    digest = hashlib.sha256(f"{seed}|{frame_id}|{segment_id}|{k}".encode()).hexdigest()
    for length in range(8, len(digest) + 1):
        persona_id = "p_" + digest[:length]
        if persona_id not in used_ids:
            used_ids.add(persona_id)
            return persona_id
    raise ValueError("无法为人物骨架生成不重复的 persona_id")


def _set_filter_fields(skeleton: dict, segment: dict, seed: int, frame_id: str, k: int, priors: dict) -> set[str]:
    paths = set(segment.get("filter", {}))
    for path, allowed in segment.get("filter", {}).items():
        if not allowed:
            raise ValueError(f"{segment['segment_id']} 的过滤字段 {path} 没有允许值")
        index = min(int(u01(seed, "skeleton", frame_id, segment["segment_id"], k, path) * len(allowed)),
                    len(allowed) - 1)
        value = allowed[index]
        if _is_array_path(path):
            value = [value]
        _set_path(skeleton, path, value)
        priors[path] = "filter"
    return paths


def _sample_axes(skeleton: dict, frame_id: str, segment_id: str, k: int, seed: int, sampling: dict,
                 filter_paths: set[str], priors: dict) -> None:
    mbti_type = _get_path(skeleton, "psyche.mbti.type")
    if not isinstance(mbti_type, str) or len(mbti_type) != 4:
        raise ValueError(f"{segment_id} 的 MBTI 类型无效：{mbti_type!r}")
    axes = _get_path(skeleton, "psyche.mbti.axes")
    if "psyche.mbti.axes" not in filter_paths:
        axes = axes if isinstance(axes, dict) else {}
        borderline_axes = []
        magnitude_cfg = sampling["magnitude"]
        scale_lo, scale_hi = map(float, magnitude_cfg["scale"])
        borderline_prob = float(sampling["borderline_prob_per_axis"])
        for axis, (letter_index, positive_letter) in _AXES.items():
            path = f"psyche.mbti.axes.{axis}"
            if path in filter_paths:
                value = _get_path(skeleton, path)
                if abs(value) < 0.2:
                    borderline_axes.append(axis)
                continue
            positive = mbti_type[letter_index] == positive_letter
            sign = 1.0 if positive else -1.0
            borderline = u01(seed, "skeleton", frame_id, segment_id, k, f"{path}.borderline") < borderline_prob
            if borderline:
                raw = u01(seed, "skeleton", frame_id, segment_id, k, f"{path}.borderline_magnitude") * 0.2
                magnitude = min(0.19, max(0.01, round(raw, 2)))
                borderline_axes.append(axis)
            else:
                beta = _beta_sample(magnitude_cfg, seed, frame_id, segment_id, k, f"{path}.magnitude")
                magnitude = round(scale_lo + beta * (scale_hi - scale_lo), 2)
                magnitude = min(scale_hi, max(0.2, magnitude))
            axes[axis] = sign * magnitude
        _set_path(skeleton, "psyche.mbti.axes", axes)
        if "psyche.mbti.borderline_axes" not in filter_paths:
            _set_path(skeleton, "psyche.mbti.borderline_axes", borderline_axes)
    elif "psyche.mbti.borderline_axes" not in filter_paths:
        _set_path(skeleton, "psyche.mbti.borderline_axes",
                  [axis for axis in _AXES if abs(axes[axis]) < 0.2])


def _fill_skeleton(skeleton: dict, segment: dict, frame: dict, behavior_map: dict, seed: int, k: int,
                   shortfall: int, enum_permutations: dict, mbti_permutation: list[str],
                   motive_permutation: list[str], birth_bins: list[int], combo_counts: Counter,
                   used_ids: set[str]) -> dict:
    frame_id, segment_id = frame["frame_id"], segment["segment_id"]
    filters = segment.get("filter", {})
    filter_paths = _set_filter_fields(skeleton, segment, seed, frame_id, k, skeleton["priors"])

    for path, values in _ENUM_FIELDS.items():
        if path not in filter_paths:
            _set_path(skeleton, path, enum_permutations[path][k % len(values)])
            skeleton["priors"][path] = "stratified_uniform"

    if "identity.birth_year" not in filter_paths:
        bin_index = birth_bins[k]
        bin_width = 45 / shortfall
        low = 1960 + bin_index * bin_width
        draw = u01(seed, "skeleton", frame_id, segment_id, k, "identity.birth_year")
        _set_path(skeleton, "identity.birth_year", min(2004, int(low + draw * bin_width)))
        skeleton["priors"]["identity.birth_year"] = "stratified_uniform"

    mbti_path = "psyche.mbti.type"
    if mbti_path not in filter_paths:
        prior = frame["mbti_prior"]
        mode = prior["mode"]
        if mode == "uniform":
            mbti_type = mbti_permutation[k % len(mbti_permutation)]
            skeleton["priors"][mbti_path] = "stratified_uniform"
        elif mode == "table":
            table = prior.get("table") or {}
            mbti_type = _sample_table(table, seed, frame_id, segment_id, k, mbti_path)
            if mbti_type not in _MBTI_TYPES:
                raise ValueError(f"mbti_prior.table 含无效 MBTI 类型：{mbti_type}")
            skeleton["priors"][mbti_path] = "table"
        else:
            raise ValueError(f"不支持的 mbti_prior.mode：{mode}")
        _set_path(skeleton, mbti_path, mbti_type)

    _sample_axes(skeleton, frame_id, segment_id, k, seed, behavior_map["sampling"], filter_paths, skeleton["priors"])

    trait_cfg = behavior_map["sampling"]["traits"]
    trait_path = "psyche.traits"
    if trait_path not in filter_paths:
        for trait in _TRAITS:
            path = f"{trait_path}.{trait}"
            if path not in filter_paths:
                value = round(_beta_sample(trait_cfg, seed, frame_id, segment_id, k, path), 2)
                _set_path(skeleton, path, value)

    motive_path = "psyche.motive_type"
    if motive_path not in filter_paths:
        mbti_type = _get_path(skeleton, mbti_path)
        cap = int(behavior_map["sampling"]["max_same_combo_per_segment"])
        candidates = [motive_permutation[(k + offset) % len(motive_permutation)]
                      for offset in range(len(motive_permutation))]
        motive = candidates[-1]
        for candidate in candidates:
            if combo_counts[(mbti_type, candidate)] < cap:
                motive = candidate
                break
        _set_path(skeleton, motive_path, motive)
        skeleton["priors"][motive_path] = "stratified_uniform"

    if "psyche.values_top3" not in filter_paths:
        values_top3 = sorted(
            _VALUES,
            key=lambda value: u01(seed, "skeleton", frame_id, segment_id, k, f"psyche.values_top3:{value}"),
        )[:3]
        _set_path(skeleton, "psyche.values_top3", values_top3)
        skeleton["priors"]["psyche.values_top3"] = "uniform"

    if "initial_lean" not in filter_paths:
        _set_path(skeleton, "initial_lean", enum_permutations["initial_lean"][k % len(_LEANINGS)])
        skeleton["priors"]["initial_lean"] = "stratified_uniform"

    _set_path(skeleton, "persona_id", _new_persona_id(seed, frame_id, segment_id, k, used_ids))
    combo = (_get_path(skeleton, mbti_path), _get_path(skeleton, motive_path))
    combo_counts[combo] += 1
    return skeleton


def draw_skeletons(frame: dict, personas: list[dict], behavior_map: dict, seed: int) -> list[dict]:
    """按分群目标数补出缺口；分群和过滤字段沿用 sampling.segment_of 的首匹配规则。"""
    frame_id = frame["frame_id"]
    segments = frame["segments"]
    segment_ids = [segment["segment_id"] for segment in segments]
    if len(set(segment_ids)) != len(segment_ids):
        raise ValueError(f"人群框架 {frame_id} 的分群编号有重复")
    existing_by_segment = {segment_id: [] for segment_id in segment_ids}
    for persona in personas:
        segment_id = segment_of(persona, frame)
        if segment_id in existing_by_segment:
            existing_by_segment[segment_id].append(persona)

    used_ids = {persona.get("persona_id") for persona in personas}
    results = []
    enum_fields = {**_ENUM_FIELDS, "initial_lean": _LEANINGS}
    for segment in segments:
        segment_id = segment["segment_id"]
        existing = existing_by_segment[segment_id]
        shortfall = max(0, int(segment["n"]) - len(existing))
        if not shortfall:
            continue
        enum_permutations = {
            field: _permutation(values, seed, frame_id, segment_id, field)
            for field, values in enum_fields.items()
        }
        mbti_permutation = _permutation(_MBTI_TYPES, seed, frame_id, segment_id, "psyche.mbti.type")
        motive_permutation = _permutation(_MOTIVES, seed, frame_id, segment_id, "psyche.motive_type")
        birth_bins = _permutation(range(shortfall), seed, frame_id, segment_id, "identity.birth_year.bins")
        combos = Counter()
        for persona in existing:
            mbti_type = _get_path(persona, "psyche.mbti.type")
            motive = _get_path(persona, "psyche.motive_type")
            if mbti_type is not None and motive is not None:
                combos[(mbti_type, motive)] += 1
        for k in range(shortfall):
            skeleton = {
                "persona_id": "",
                "frame_id": frame_id,
                "segment_id": segment_id,
                "initial_lean": "",
                "identity": {"occupation": {}},
                "psyche": {"mbti": {}, "traits": {}},
                "situation": {},
                "priors": {},
            }
            results.append(_fill_skeleton(
                skeleton, segment, frame, behavior_map, seed, k, shortfall, enum_permutations,
                mbti_permutation, motive_permutation, birth_bins, combos, used_ids))
    return results
