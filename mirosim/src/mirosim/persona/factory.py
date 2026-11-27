"""按人群框架缺口确定性生成骨架；未被过滤器指定的职业字段留给后续写作步骤。"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import random
import re
from collections import Counter
from copy import deepcopy
from datetime import date
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from mirosim import ROOT
from mirosim.core.rng import u01
from mirosim.engines.panel import _first_json_object, in_order
from mirosim.experiment.sampling import segment_of
from mirosim.persona.render import load_template

_PERSONA_SCHEMA = json.loads((ROOT / "schemas" / "persona.schema.json").read_text(encoding="utf-8"))
_IDENTITY_PROPERTIES = _PERSONA_SCHEMA["properties"]["identity"]["properties"]
_PSYCHE_PROPERTIES = _PERSONA_SCHEMA["properties"]["psyche"]["properties"]
_GENDERS = ["male", "female"]
_EDUCATION = _IDENTITY_PROPERTIES["education"]["enum"]
_REGIONS = _IDENTITY_PROPERTIES["region_tier"]["enum"]
_HOME_REGIONS = _IDENTITY_PROPERTIES["home_region"]["enum"]
_RESIDENCE_REGIONS = _IDENTITY_PROPERTIES["residence_region"]["enum"]
_INCOMES = _IDENTITY_PROPERTIES["income_band"]["enum"]
_MOTIVES = _PSYCHE_PROPERTIES["motive_type"]["enum"]
_LEANINGS = ["support", "oppose", "conditional", "indifferent"]
_VALUES = _PSYCHE_PROPERTIES["values_top3"]["items"]["enum"]
_AXES = {"ei": (0, "E"), "sn": (1, "N"), "tf": (2, "T"), "jp": (3, "J")}
_AXIS_NEGATIVE = {"ei": "I", "sn": "S", "tf": "F", "jp": "P"}
_TRAITS = ("emotional_stability", "risk_tolerance", "institutional_trust", "conformity", "price_sensitivity")
_MBTI_TYPES = ["".join(letters) for letters in itertools.product("EI", "SN", "TF", "JP")]
_REGION_SHIFT_FIELDS = {f"psyche.traits.{trait}" for trait in _TRAITS} | {
    f"psyche.mbti.axes.{axis}" for axis in _AXES
}
_MBTI_LEAK = re.compile(r"(?<![A-Za-z])[EI][SN][TF][JP](?![A-Za-z])", re.IGNORECASE)
_ENUM_FIELDS = {
    "identity.gender": _GENDERS,
    "identity.education": _EDUCATION,
    "identity.region_tier": _REGIONS,
    "identity.income_band": _INCOMES,
    "identity.home_region": _HOME_REGIONS,
    "identity.residence_region": _RESIDENCE_REGIONS,
}
_MARGINAL_FIELDS = {
    "identity.gender": _IDENTITY_PROPERTIES["gender"]["enum"],
    "identity.education": _EDUCATION,
    "identity.region_tier": _REGIONS,
    "identity.income_band": _INCOMES,
    "identity.home_region": _HOME_REGIONS,
    "identity.residence_region": _RESIDENCE_REGIONS,
    "psyche.motive_type": _MOTIVES,
}
_REROUTABLE_ENUM_FIELDS = set(_ENUM_FIELDS) | set(_MARGINAL_FIELDS)


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


def _permutation(values, seed: int, frame_id: str, segment_id: str, field: str, cycle: int = 0) -> list:
    return sorted(values, key=lambda value: (
        u01(seed, "skeleton", frame_id, segment_id, cycle, f"{field}:{value}"), str(value)))


def _rotated_value(values, seed: int, frame_id: str, segment_id: str, field: str, k: int):
    cycle = k // len(values)
    permutation = _permutation(values, seed, frame_id, segment_id, field, cycle)
    return permutation[k % len(values)]


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


def _sample_marginal(table: dict, values: list[str], seed: int, frame_id: str, segment_id: str,
                     k: int, shortfall: int, field: str) -> str:
    if not isinstance(table, dict) or set(table) - set(values):
        raise ValueError(f"{field} 的边际分布包含无效类别")
    weights = []
    for value in values:
        weight = float(table.get(value, 0))
        if not math.isfinite(weight) or weight < 0:
            raise ValueError(f"{field} 的边际权重必须是非负有限数")
        weights.append((value, weight))
    total = sum(weight for _, weight in weights)
    if total <= 0:
        raise ValueError(f"{field} 的边际权重总和必须大于 0")

    bins = _permutation(range(shortfall), seed, frame_id, segment_id, f"{field}.quota_bin")
    unit = (bins[k] + u01(seed, "skeleton", frame_id, segment_id, k, field)) / shortfall
    cumulative = 0.0
    last_positive = None
    for value, weight in weights:
        if weight > 0:
            last_positive = value
        cumulative += weight / total
        if unit < cumulative:
            return value
    return last_positive


def _new_persona_id(seed: int, frame_id: str, segment_id: str, k: int, used_ids: set[str]) -> str:
    digest = hashlib.sha256(f"{seed}|{frame_id}|{segment_id}|{k}".encode()).hexdigest()
    for length in range(8, len(digest) + 1):
        persona_id = "p_" + digest[:length]
        if persona_id not in used_ids:
            used_ids.add(persona_id)
            return persona_id
    raise ValueError("无法为人物骨架生成不重复的 persona_id")


def _validate_region_shifts(region_shifts: list | None) -> list[dict]:
    if region_shifts is None:
        return []
    if not isinstance(region_shifts, list):
        raise ValueError("地域性格修正必须是列表")
    valid_regions = set(_HOME_REGIONS)
    validated = []
    for index, shift in enumerate(region_shifts):
        if not isinstance(shift, dict):
            raise ValueError(f"地域性格修正第 {index + 1} 项必须是对象")
        region = shift.get("region")
        if not isinstance(region, str) or region not in valid_regions:
            raise ValueError(f"地域性格修正包含未知地域：{region!r}")
        field = shift.get("field")
        if not isinstance(field, str) or field not in _REGION_SHIFT_FIELDS:
            raise ValueError(f"地域性格修正包含未知字段：{field!r}")
        delta = shift.get("delta")
        if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not math.isfinite(delta) or not -0.3 <= delta <= 0.3:
            raise ValueError(f"地域性格修正 {field} 的 delta 必须在 [-0.3, 0.3] 内")
        source = shift.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"地域性格修正 {region}.{field} 缺少出处 source")
        validated.append({"region": region, "field": field, "delta": float(delta), "source": source})
    return validated


def _apply_region_shifts(skeleton: dict, region_shifts: list[dict], filter_paths: set[str]) -> None:
    home_region = skeleton["identity"]["home_region"]
    shifted_axis = False
    for shift in region_shifts:
        if shift["region"] != home_region:
            continue
        field = shift["field"]
        if any(field == path or field.startswith(path + ".") for path in filter_paths):
            continue
        value = _get_path(skeleton, field)
        is_axis = field.startswith("psyche.mbti.axes.")
        low, high = (-1.0, 1.0) if is_axis else (0.0, 1.0)
        shifted = min(high, max(low, value + shift["delta"]))
        if is_axis and shifted == 0:
            shifted = math.copysign(0.01, value if value != 0 else 1.0)
        _set_path(skeleton, field, shifted)
        shifted_axis = shifted_axis or is_axis

    if shifted_axis:
        axes = _get_path(skeleton, "psyche.mbti.axes")
        letters = [
            positive if axes[axis] >= 0 else _AXIS_NEGATIVE[axis]
            for axis, (_, positive) in _AXES.items()
        ]
        _set_path(skeleton, "psyche.mbti.type", "".join(letters))
        _set_path(skeleton, "psyche.mbti.borderline_axes",
                  [axis for axis in _AXES if abs(axes[axis]) < 0.2])


def _resample_residence_region(skeleton: dict, segment: dict, frame: dict, seed: int, k: int) -> None:
    path = "identity.residence_region"
    if path in segment.get("filter", {}) or path in segment.get("marginals", {}):
        return
    segment_index = next(i for i, item in enumerate(frame["segments"]) if item["segment_id"] == segment["segment_id"])
    if any(path in earlier.get("filter", {}) for earlier in frame["segments"][:segment_index]):
        return
    frame_id, segment_id = frame["frame_id"], segment["segment_id"]
    if u01(seed, "skeleton", frame_id, segment_id, k, "identity.residence_region.same_as_home") < 0.7:
        value = _get_path(skeleton, "identity.home_region")
    else:
        value = _rotated_value(_RESIDENCE_REGIONS, seed, frame_id, segment_id, path, k)
    _set_path(skeleton, path, value)
    skeleton["priors"][path] = "table"


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


def _product_at(index: int, domains: list[list]) -> list:
    values = []
    for domain in reversed(domains):
        index, value_index = divmod(index, len(domain))
        values.append(domain[value_index])
    return list(reversed(values))


def _reroute_skeleton(skeleton: dict, segment: dict, frame: dict, seed: int, k: int) -> dict:
    segment_index = next(i for i, item in enumerate(frame["segments"]) if item["segment_id"] == segment["segment_id"])
    target_filter = segment.get("filter", {})
    previous_paths = {
        path for earlier in frame["segments"][:segment_index] for path in earlier.get("filter", {})
    }
    paths = set(target_filter) | {
        path for path in previous_paths if path in _REROUTABLE_ENUM_FIELDS
    }
    marginals = segment.get("marginals", {})
    ordered_paths = []
    domains = []
    for path in sorted(paths):
        if path in target_filter:
            domain = target_filter[path]
        else:
            marginal = marginals.get(path)
            enum = _MARGINAL_FIELDS.get(path) if marginal is not None else _ENUM_FIELDS.get(path)
            enum = enum or _MARGINAL_FIELDS.get(path)
            if enum is None:
                continue
            domain = [value for value in enum if marginal.get(value, 0) > 0] if marginal is not None else enum
        unique = []
        for value in domain:
            if value not in unique:
                unique.append(value)
        if not unique:
            raise ValueError(f"{segment['segment_id']} 的过滤字段 {path} 没有可达取值")
        ordered_paths.append(path)
        domains.append(unique)

    total = math.prod(len(domain) for domain in domains)
    offset = int(u01(seed, "skeleton", frame["frame_id"], segment["segment_id"], k, "reroute.offset") * total)
    for step in range(min(total, 4096)):
        candidate = deepcopy(skeleton)
        for path, value in zip(ordered_paths, _product_at((offset + step) % total, domains)):
            if path not in target_filter and _get_path(candidate, path) != ([value] if _is_array_path(path) else value):
                candidate["priors"][path] = "rerouted"
            _set_path(candidate, path, [value] if _is_array_path(path) else value)
        if segment_of(candidate, frame) == segment["segment_id"]:
            return candidate
    raise ValueError(f"分群 {segment['segment_id']} 对所有可达取值均被前序分群遮蔽，无法按首匹配规则路由")


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
                   shortfall: int, birth_bins: list[int], combo_counts: Counter) -> dict:
    frame_id, segment_id = frame["frame_id"], segment["segment_id"]
    marginals = segment.get("marginals", {})
    filter_paths = _set_filter_fields(skeleton, segment, seed, frame_id, k, skeleton["priors"])

    for path, values in _ENUM_FIELDS.items():
        if path in filter_paths:
            continue
        if path in marginals:
            value = _sample_marginal(marginals[path], _MARGINAL_FIELDS[path], seed, frame_id, segment_id,
                                     k, shortfall, path)
            _set_path(skeleton, path, value)
            skeleton["priors"][path] = "table"
        elif path == "identity.residence_region":
            if u01(seed, "skeleton", frame_id, segment_id, k, "identity.residence_region.same_as_home") < 0.7:
                value = _get_path(skeleton, "identity.home_region")
            else:
                value = _rotated_value(values, seed, frame_id, segment_id, path, k)
            _set_path(skeleton, path, value)
            skeleton["priors"][path] = "table"
        else:
            _set_path(skeleton, path, _rotated_value(values, seed, frame_id, segment_id, path, k))
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
            mbti_type = _rotated_value(_MBTI_TYPES, seed, frame_id, segment_id, mbti_path, k)
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
        motive_cycle = k // len(_MOTIVES)
        motive_permutation = _permutation(
            _MOTIVES, seed, frame_id, segment_id, motive_path, motive_cycle)
        motive_start = k % len(motive_permutation)
        permuted_motives = [motive_permutation[(motive_start + offset) % len(motive_permutation)]
                            for offset in range(len(motive_permutation))]
        if motive_path in marginals:
            sampled_motive = _sample_marginal(
                marginals[motive_path], _MARGINAL_FIELDS[motive_path], seed, frame_id, segment_id,
                k, shortfall, motive_path)
            candidates = [sampled_motive] + [
                candidate for candidate in permuted_motives if candidate != sampled_motive
            ]
        else:
            candidates = permuted_motives
        motive = candidates[-1]
        for candidate in candidates:
            if combo_counts[(mbti_type, candidate)] < cap:
                motive = candidate
                break
        _set_path(skeleton, motive_path, motive)
        skeleton["priors"][motive_path] = "table" if motive_path in marginals else "stratified_uniform"

    if "psyche.values_top3" not in filter_paths:
        values_top3 = sorted(
            _VALUES,
            key=lambda value: u01(seed, "skeleton", frame_id, segment_id, k, f"psyche.values_top3:{value}"),
        )[:3]
        _set_path(skeleton, "psyche.values_top3", values_top3)
        skeleton["priors"]["psyche.values_top3"] = "uniform"

    if "initial_lean" not in filter_paths:
        _set_path(skeleton, "initial_lean",
                  _rotated_value(_LEANINGS, seed, frame_id, segment_id, "initial_lean", k))
        skeleton["priors"]["initial_lean"] = "stratified_uniform"

    return skeleton


def draw_skeletons(frame: dict, personas: list[dict], behavior_map: dict, seed: int,
                   region_shifts: list | None = None) -> list[dict]:
    """按分群目标数补出缺口；分群和过滤字段沿用 sampling.segment_of 的首匹配规则。"""
    region_shifts = _validate_region_shifts(region_shifts)
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
    for segment in segments:
        segment_id = segment["segment_id"]
        existing = existing_by_segment[segment_id]
        shortfall = max(0, int(segment["n"]) - len(existing))
        if not shortfall:
            continue
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
            _fill_skeleton(skeleton, segment, frame, behavior_map, seed, k, shortfall, birth_bins, combos)
            if segment_of(skeleton, frame) != segment_id:
                original_home_region = _get_path(skeleton, "identity.home_region")
                skeleton = _reroute_skeleton(skeleton, segment, frame, seed, k)
                if _get_path(skeleton, "identity.home_region") != original_home_region:
                    _resample_residence_region(skeleton, segment, frame, seed, k)
            if region_shifts:
                filter_paths = set(segment.get("filter", {}))
                _apply_region_shifts(skeleton, region_shifts, filter_paths)
                if segment_of(skeleton, frame) != segment_id:
                    skeleton = _reroute_skeleton(skeleton, segment, frame, seed, k)
                    if segment_of(skeleton, frame) != segment_id:
                        raise ValueError(f"地域修正后分群 {segment_id} 无法按首匹配规则路由")
            _set_path(skeleton, "persona_id", _new_persona_id(seed, frame_id, segment_id, k, used_ids))
            combo = (_get_path(skeleton, "psyche.mbti.type"), _get_path(skeleton, "psyche.motive_type"))
            combos[combo] += 1
            results.append(skeleton)
    return results


def _merge_fixed(base, generated):
    if isinstance(base, dict) and isinstance(generated, dict):
        merged = deepcopy(base)
        for key, value in generated.items():
            merged[key] = _merge_fixed(merged[key], value) if key in merged else deepcopy(value)
        return merged
    if isinstance(base, list) and isinstance(generated, list):
        merged = deepcopy(base)
        seen = set()
        for value in merged:
            key = ("channel", value["channel"]) if isinstance(value, dict) and "channel" in value else (
                "json", json.dumps(value, ensure_ascii=False, sort_keys=True))
            seen.add(key)
        for value in generated:
            key = ("channel", value["channel"]) if isinstance(value, dict) and "channel" in value else (
                "json", json.dumps(value, ensure_ascii=False, sort_keys=True))
            if key not in seen:
                merged.append(deepcopy(value))
                seen.add(key)
        return merged
    return deepcopy(base)


def _model_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _model_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _model_strings(item)


def _has_non_finite(value) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_has_non_finite(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_non_finite(item) for item in value)
    return False


def _assemble_persona(skeleton: dict, generated: dict, *, model_id: str, prompt_hash: str,
                      created_at: str, expression_intensity: float) -> dict:
    occupation_fields = ("title", "industry_code", "job_family", "employment_type", "years")
    occupation = {key: generated[key] for key in occupation_fields if key in generated}
    identity = _merge_fixed(skeleton["identity"], {
        "name": generated.get("name"),
        "occupation": occupation,
        "household": {
            key: generated[key] for key in ("marital", "children", "living_with") if key in generated
        },
    })
    expression = {"language_style": generated.get("language_style"),
                  "expression_intensity": expression_intensity}
    if generated.get("catchphrase") is not None:
        expression["catchphrase"] = generated["catchphrase"]
    psyche = _merge_fixed(skeleton["psyche"], {"expression": expression})
    situation = {
        key: generated[key] for key in
        ("stakes", "constraints", "media_diet", "consumption", "knowledge_bounds") if key in generated
    }
    history = _merge_fixed(skeleton.get("history", {}), {"life_events": generated.get("life_events")})
    return {
        "persona_id": skeleton["persona_id"],
        "version": 1,
        "synthetic": True,
        "disclaimer": "本人物为模拟构造，不代表任何真实个人。",
        "status": "active",
        "identity": identity,
        "psyche": psyche,
        "situation": _merge_fixed(skeleton["situation"], situation),
        "history": history,
        "provenance": {
            "created_by": "factory",
            "created_at": created_at,
            "parent_version": None,
            "archetype_id": None,
            "generator": f"{model_id}/persona_factory@sha256:{prompt_hash}",
            "change_note": "工厂生成，未做特质探针",
            "priors": deepcopy(skeleton.get("priors", {})),
        },
    }


def _persona_qa(card: dict, generated: dict, skeleton: dict, frame: dict, schema: dict,
                library_names: set[str], channel_ids: set[str]) -> str | None:
    for text in _model_strings(generated):
        if _MBTI_LEAK.search(text) or "内向" in text or "外向" in text or "mbti" in text.lower():
            return "模型输出包含 MBTI 类型或性格标签"
    missing_household = {"marital", "children", "living_with"} - set(generated)
    if missing_household:
        return f"缺少家庭字段：{sorted(missing_household)}"
    fixed_occupation = skeleton["identity"].get("occupation", {})
    missing_occupation = {
        field for field in ("industry_code", "job_family", "employment_type", "years")
        if field not in fixed_occupation and field not in generated
    }
    if missing_occupation:
        return f"缺少职业字段：{sorted(missing_occupation)}"
    error = next(iter(Draft202012Validator(schema).iter_errors(card)), None)
    if error is not None:
        path = ".".join(map(str, error.absolute_path)) or "$"
        return f"{path}: {error.message}"
    if segment_of(card, frame) != skeleton["segment_id"]:
        return f"分群路由不匹配：期望 {skeleton['segment_id']}"
    if card["identity"]["name"] in library_names:
        return "姓名已在人物库中"
    events = card["history"]["life_events"]
    if not 3 <= len(events) <= 5:
        return "生活事件必须为 3 到 5 条"
    if sum(event.get("core") is True for event in events) > 3:
        return "核心生活事件不能超过 3 条"
    media_diet = card["situation"]["media_diet"]
    if any(item["channel"] not in channel_ids for item in media_diet):
        return "媒体渠道包含 config/channels.yaml 之外的 ID"
    if sum(item["share"] for item in media_diet) > 1.0001:
        return "媒体渠道 share 合计超过 1"
    return None


def write_personas(skeletons: list[dict], gateway, *, library: list[dict], frame: dict,
                   root: Path = ROOT, concurrency: int = 1) -> tuple[list[dict], list[dict]]:
    if concurrency < 1:
        raise ValueError("concurrency 必须至少为 1")
    root = Path(root)
    schema = json.loads((root / "schemas" / "persona.schema.json").read_text(encoding="utf-8"))
    channel_ids = set(yaml.safe_load((root / "config" / "channels.yaml").read_text(encoding="utf-8"))["channels"])
    region_labels = yaml.safe_load((root / "config" / "persona_text.yaml").read_text(encoding="utf-8"))["region"]
    template_path = root / "prompts" / "persona_factory.md"
    template = load_template(root, "persona_factory")
    template_source = re.search(r"```text\n(.*?)\n```", template_path.read_text(encoding="utf-8"), re.S)
    template_hash = hashlib.sha256(template_source.group(1).encode()).hexdigest()
    model_id = gateway.backend.model_id
    created_at = date.today().isoformat()
    run_id = f"factory:{frame['frame_id']}"
    segments_by_id = {segment["segment_id"]: segment for segment in frame["segments"]}
    library_names = {persona["identity"]["name"] for persona in library}
    siblings = {}
    for persona in sorted(library, key=lambda item: item.get("persona_id", "")):
        segment_id = segment_of(persona, frame)
        core_events = [event for event in persona.get("history", {}).get("life_events", []) if event.get("core")]
        if segment_id is None or not core_events:
            continue
        siblings.setdefault(segment_id, []).append({
            "name": persona["identity"]["name"],
            "title": persona["identity"]["occupation"]["title"],
            "life_event": core_events[0]["event"],
        })
    sibling_prompts = {
        segment_id: json.dumps(items[:8], ensure_ascii=False, indent=2)
        for segment_id, items in siblings.items()
    }
    skeletons = list(skeletons)

    def write_one(skeleton: dict) -> dict:
        segment = segments_by_id.get(skeleton["segment_id"], {})
        prompt_skeleton = {
            key: deepcopy(value) for key, value in skeleton.items()
            if key not in {"persona_id", "frame_id", "priors"}
        }
        user = template.render(
            skeleton_json=prompt_skeleton,
            segment_label=segment.get("label") or skeleton["segment_id"],
            frame_universe=frame.get("universe"),
            sibling_summaries=sibling_prompts.get(skeleton["segment_id"], "[]"),
            channel_ids=sorted(channel_ids),
            region_labels=region_labels,
        )
        seed = int(u01(0, skeleton["persona_id"]) * 2**32)
        expression_seed = int(u01(seed, "expression", skeleton["persona_id"]) * 2**53)
        expression_intensity = round(random.Random(expression_seed).betavariate(2, 2), 2)
        reason = None
        for attempt in range(2):
            retry = f"\n\n你上一次的输出不合格：{reason}。请重新只输出一个 JSON 对象。" if attempt else ""
            result = gateway.call(
                system="你是写虚构人物档案的作者，只输出 JSON",
                user=user + retry,
                purpose="factory",
                run_id=run_id,
                ordinal=attempt,
                seed=seed,
                spec=None,
            )
            try:
                generated = _first_json_object(result.text)
            except json.JSONDecodeError as error:
                reason = f"不是合法的 JSON：{error.msg}"
                continue
            if _has_non_finite(generated):
                reason = "输出含非有限数值"
                continue
            card = _assemble_persona(
                skeleton,
                generated,
                model_id=model_id,
                prompt_hash=template_hash,
                created_at=created_at,
                expression_intensity=expression_intensity,
            )
            reason = _persona_qa(card, generated, skeleton, frame, schema, library_names, channel_ids)
            if reason is None:
                return {"card": card, "reason": None}
        return {"card": None, "reason": reason}

    results = in_order(write_one, skeletons, concurrency, on_fail=gateway.stop)
    cards, failures = [], []
    accepted_names = set(library_names)
    for skeleton, result in zip(skeletons, results):
        if result["card"] is None:
            failures.append({
                "persona_id": skeleton["persona_id"],
                "segment_id": skeleton["segment_id"],
                "reason": result["reason"],
            })
            continue
        name = result["card"]["identity"]["name"]
        if name in accepted_names:
            failures.append({
                "persona_id": skeleton["persona_id"],
                "segment_id": skeleton["segment_id"],
                "reason": "重名",
            })
            continue
        accepted_names.add(name)
        cards.append(result["card"])
    return cards, failures
