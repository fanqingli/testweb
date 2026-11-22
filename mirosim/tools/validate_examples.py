#!/usr/bin/env python3
"""校验 examples/ 与 config/ 是否符合 schemas/，并做 schema 表达不了的交叉检查。

用法: python tools/validate_examples.py      （在 mirosim/ 目录下；依赖 jsonschema、pyyaml）
退出码非 0 表示有错误。实施时把它接进 CI。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from derive_behavior import hard_params, mbti_type, soft_text  # noqa: E402

errors: list[str] = []


def load(path: Path):
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)


def jsonable(obj):
    """YAML 会把日期解析成 date 对象；转回字符串再校验。"""
    return json.loads(json.dumps(obj, default=str, ensure_ascii=False))


def check(schema_name: str, path: Path, doc=None) -> dict:
    schema = load(ROOT / "schemas" / f"{schema_name}.schema.json")
    doc = jsonable(load(path) if doc is None else doc)
    for e in Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(doc):
        errors.append(f"{path.relative_to(ROOT)}: {'/'.join(map(str, e.absolute_path))}: {e.message}")
    return doc


def fail(path: Path, msg: str) -> None:
    errors.append(f"{path.relative_to(ROOT)}: {msg}")


def main() -> int:
    for name in ("persona", "memory_item", "stimulus", "experiment", "response", "run_manifest", "population_frame"):
        Draft202012Validator.check_schema(load(ROOT / "schemas" / f"{name}.schema.json"))

    channels = set(load(ROOT / "config" / "channels.yaml")["channels"])
    measures_cfg = load(ROOT / "config" / "measures.yaml")
    behavior_cfg = load(ROOT / "config" / "mbti_behavior_map.yaml")
    ex = ROOT / "examples"

    personas = {}
    for path in sorted((ex / "personas").glob("*.json")):
        p = check("persona", path)
        personas[p["persona_id"]] = p
        mbti = p["psyche"]["mbti"]
        if mbti_type(mbti["axes"]) != mbti["type"]:
            fail(path, f"mbti.type={mbti['type']} 与轴分值符号不符（应为 {mbti_type(mbti['axes'])}）")
        borderline = sorted(a for a, v in mbti["axes"].items() if abs(v) < 0.2)
        if borderline != sorted(mbti.get("borderline_axes", [])):
            fail(path, f"borderline_axes 应为 {borderline}")
        diet = p["situation"]["media_diet"]
        if sum(d["share"] for d in diet) > 1 + 1e-9:
            fail(path, "media_diet.share 合计超过 1")
        for d in diet:
            if d["channel"] not in channels:
                fail(path, f"未知渠道 {d['channel']}")
        if sum(1 for e in p["history"]["life_events"] if e.get("core")) > 3:
            fail(path, "core 生活事件超过 3 条")
        if path.stem != p["persona_id"]:
            fail(path, "文件名应等于 persona_id")
        hard_params(p, behavior_cfg)
        soft_text(p, behavior_cfg)
    for pid, p in personas.items():
        for r in p.get("relations", []):
            if r["to"] not in personas:
                fail(ex / "personas" / f"{pid}.json", f"relations.to 指向不存在的人物 {r['to']}")

    stimuli = {}
    for path in sorted(ex.glob("stimulus.*.yaml")):
        s = check("stimulus", path)
        stimuli[str(path.relative_to(ROOT))] = stimuli[s["stimulus_id"]] = s
        ids = [v["variant_id"] for v in s["variants"]]
        if len(ids) != len(set(ids)):
            fail(path, "variant_id 重复")
        if sum(v["role"] == "status_quo" for v in s["variants"]) > 1:
            fail(path, "status_quo 版本最多一个")
        by_id = {v["variant_id"]: v for v in s["variants"]}
        for v in s["variants"]:
            if v["role"] == "placebo":
                target = by_id.get(v.get("placebo_of"))
                if target is None:
                    fail(path, f"{v['variant_id']}: placebo_of 指向不存在的版本")
                elif target["content"] != v["content"] or target.get("renderings") != v.get("renderings"):
                    fail(path, f"{v['variant_id']}: 安慰剂的 content 与 renderings 必须与 {target['variant_id']} 完全相同")
        for v in s["variants"]:
            for ch in v.get("renderings", {}):
                if ch not in channels:
                    fail(path, f"{v['variant_id']}: renderings 含未知渠道 {ch}")
        for c in s["context"].get("channel_plan", []):
            if c["channel"] not in channels:
                fail(path, f"未知渠道 {c['channel']}")

    frames = {}
    for path in sorted(ex.glob("frame.*.yaml")):
        f = check("population_frame", path)
        frames[str(path.relative_to(ROOT))] = frames[f["frame_id"]] = f
        if abs(sum(s["share"] for s in f["segments"]) - 1) > 1e-6:
            fail(path, "segments.share 合计应为 1")

    for path in sorted(ex.glob("experiment.*.yaml")):
        x = check("experiment", path)
        s = stimuli.get(x["stimulus"])
        f = frames.get(x["population"]["frame"])
        if s is None:
            fail(path, f"找不到 stimulus {x['stimulus']}")
            continue
        if f is None:
            fail(path, f"找不到人群框架 {x['population']['frame']}")
            continue
        if s["pack"] != x["pack"]:
            fail(path, "实验与 stimulus 的 pack 不一致")
        known = set(measures_cfg[x["pack"]]["measures"])
        for m in x["measures"]:
            if m not in known:
                fail(path, f"量表项 {m} 未在 config/measures.yaml 的 {x['pack']} 包中定义")
        if sum(seg["n"] for seg in f["segments"]) != x["population"]["size"]:
            fail(path, "population.size 应等于各分群 n 之和")
        all_variants = [v["variant_id"] for v in s["variants"]]
        variants = x["design"].get("variants") or all_variants
        if len(variants) != len(set(variants)):
            fail(path, "design.variants 有重复")
        for vid in variants:
            if vid not in all_variants:
                fail(path, f"design.variants 中的 {vid} 不在刺激物的版本里")
        d = x["design"]
        calls = x["population"]["size"] * len(variants) * d["replicates"] * d.get("model_families", 1) * d.get("steps", 1)
        if d.get("baseline"):
            calls += x["population"]["size"] * d.get("model_families", 1)
        if calls > x["budget"]["max_calls"]:
            fail(path, f"人物调用上界 {calls} 超过 budget.max_calls")
        if d["mode"] == "diffusion" and x["memory"]["commit_policy"] != "sandbox":
            fail(path, "diffusion 模式需要 commit_policy=sandbox（步间记忆写入分支世界）")

    r = check("response", ex / "response.sample.json")
    if r["persona_id"] not in personas:
        fail(ex / "response.sample.json", "persona_id 不存在")
    pol = measures_cfg["policy"]
    for k in r["payload"]["answers"]:
        if k not in pol["measures"]:
            fail(ex / "response.sample.json", f"answers 含未定义量表项 {k}")
    if r["payload"]["behavior"] not in pol["behavior"]["options"]:
        fail(ex / "response.sample.json", "behavior 不在选项内")
    for pack in ("policy", "marketing", "brand"):
        if "none" not in measures_cfg[pack]["behavior"]["options"]:
            errors.append(f"config/measures.yaml: {pack}.behavior 必须含 none")

    for i, m in enumerate(load(ex / "memory.sample.json")):
        check("memory_item", ex / "memory.sample.json", m)

    if errors:
        print("\n".join(errors))
        print(f"\n{len(errors)} 处错误")
        return 1
    print(f"通过：{len(personas)} 张人物卡、{len(stimuli) // 2} 个刺激物、{len(frames) // 2} 个人群框架、3 个实验配置、样例反应与记忆")
    return 0


if __name__ == "__main__":
    sys.exit(main())
