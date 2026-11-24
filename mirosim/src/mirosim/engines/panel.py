"""群体反应模式（骨架）。

已有：按人群框架取人与权重、公共随机数曝光、录制与回放、反应校验与一次重试、调用预算。
提示词由 prompts/persona_system.md 与 prompts/panel_reaction.md 渲染；引语字数上限由代码执行。
未做：记忆读取、原有态度、同伴信号、多模型族。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from mirosim import NOTICE, ROOT
from mirosim.core.gateway import Gateway
from mirosim.core.rng import u01
from mirosim.experiment.sampling import draw_sample
from mirosim.persona.behavior import hard_params
from mirosim.persona.render import load_template, system_prompt

K_EXPOSURE = 2.0   # docs/03 §3 的归一化系数
MAX_ATTEMPTS = 2   # 首次调用 + 至多一次重试


def _yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def measure_spec(pack_cfg: dict, scales: dict, measure_ids: list[str]) -> dict:
    """写进提示词、也用于校验的题目定义。"""
    measures = {}
    for mid in measure_ids:
        m = pack_cfg["measures"][mid]
        item = {"q": m["q"], "allow_dk": bool(m.get("allow_dk"))}
        if m["type"] in scales:
            item.update(scales[m["type"]])
        elif m["type"] == "enum":
            item["options"] = m["options"]
        else:
            item["max_chars"] = m.get("max_chars", 60)
            if "count" in m:
                item["count"] = m["count"]
        if "ask_if" in m:
            item["ask_if"] = m["ask_if"]
        measures[mid] = item
    options = pack_cfg["behavior"]["options"]
    return {"measures": measures, "behavior_options": list(options), "behavior_labels": dict(options)}


def format_hint(m: dict) -> str:
    if "min" in m:
        labels = "，".join(f"{k}={v}" for k, v in m.get("labels", {}).items())
        hint = f"（填 {m['min']} 到 {m['max']} 的整数" + (f"：{labels}）" if labels else "）")
    elif "options" in m:
        hint = f"（从 {'、'.join(m['options'])} 里选一个）"
    elif "count" in m:
        hint = f"（{m['count']} 个词，用顿号隔开）"
    else:
        hint = f"（一句话，不超过 {m['max_chars']} 个字）"
    on_answer = _ON_ANSWER.match(m.get("ask_if", ""))
    if on_answer:
        hint += f"（只在 {on_answer[1]} 选了 {' 或 '.join(v.strip() for v in on_answer[2].split(','))} 时回答）"
    return hint


_ON_ANSWER = re.compile(r"^(\w+) in \[(.+)\]$")          # 取决于另一道题的答案：照问，由人物自己判断，不必答
_ON_ATTRIBUTE = re.compile(r"^variant\.attributes\.(\w+) exists$")
_ON_KIND = re.compile(r"^stimulus\.kind == (\w+)$")


def applicable_spec(spec: dict, stimulus: dict, variant: dict) -> dict:
    """按 ask_if 去掉对这个刺激物、这个版本不适用的题。条件由代码判断，条件表达式本身不进提示词。"""
    measures = {}
    for mid, m in spec["measures"].items():
        cond = m.get("ask_if")
        if cond is None or _ON_ANSWER.match(cond):
            keep = True
        elif _ON_ATTRIBUTE.match(cond):
            keep = _ON_ATTRIBUTE.match(cond)[1] in (variant.get("attributes") or {})
        elif _ON_KIND.match(cond):
            keep = stimulus.get("kind") == _ON_KIND.match(cond)[1]
        else:
            raise ValueError(f"{mid} 的 ask_if 写法不认识：{cond}")
        if keep:
            measures[mid] = m
    return {**spec, "measures": measures}


def exposure(seed: int, persona: dict, replicate: int, mode: str, plan: list[dict]) -> dict:
    """抽签的键里没有 variant_id：同一人物在同一次重复的各个版本里，看到与否、从哪看到都相同。"""
    if mode == "forced":
        return {"exposed": True, "channel": None, "draw": 0.0}
    share = {d["channel"]: d["share"] for d in persona["situation"]["media_diet"]}
    contrib = [(c["channel"], min(1.0, c["reach"] * share.get(c["channel"], 0.0) * K_EXPOSURE)) for c in plan]
    miss = 1.0
    for _, p in contrib:
        miss *= 1 - p
    pid = persona["persona_id"]
    draw = u01(seed, "exposure", pid, replicate)
    if draw >= 1 - miss:
        return {"exposed": False, "channel": None, "draw": draw}
    target, acc = u01(seed, "channel", pid, replicate) * sum(p for _, p in contrib), 0.0
    channel = contrib[-1][0]
    for ch, p in contrib:
        acc += p
        if target < acc:
            channel = ch
            break
    return {"exposed": True, "channel": channel, "draw": draw}


def user_prompt(template, stimulus: dict, variant: dict, channel: str | None, channel_cfg: dict, spec: dict,
                max_chars: int) -> str:
    """版本的编号、标签、角色不进提示词：安慰剂的提示词必须与其对应版本逐字相同。"""
    content = (variant.get("renderings") or {}).get(channel, variant["content"]) if channel else variant["content"]
    text = template.render(
        as_of_date=str(stimulus["context"]["as_of_date"]),
        memories=[], prior_attitude=None, peer_signal=None,  # 记忆、原有态度、同伴信号尚未接入
        background=stimulus["context"]["background"].strip(),
        channel_phrase=f"在「{channel_cfg.get(channel, channel)}」" if channel else "",
        variant_content=content.strip(),
        measures=[{"id": mid, "q": m["q"], "format_hint": format_hint(m)} for mid, m in spec["measures"].items()],
        behavior_options=spec["behavior_labels"],
        max_chars=max_chars,
    )
    return re.sub(r"\n{3,}", "\n\n", text)  # 没接入的段落留下的空行


def validate_payload(text: str, spec: dict, payload_schema: dict, max_chars: int | None = None) -> tuple[dict | None, str | None, bool]:
    """合格则返回 (payload, None, 引语是否被截断)，否则 (None, 原因, False)。

    模型常在 JSON 外面包一层代码块或多写一句话，所以只取第一个 { 到最后一个 } 之间的部分。
    引语超过 max_chars 时截断，不算不合格。
    没在意（noticed=false）是有效的反应：答案清空、行为记为 none，不重试。
    """
    try:
        payload = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except json.JSONDecodeError as e:
        return None, f"不是合法的 JSON：{e.msg}", False
    if not isinstance(payload, dict):
        return None, "不是 JSON 对象", False
    truncated = max_chars is not None and isinstance(payload.get("quote"), str) and len(payload["quote"]) > max_chars
    if truncated:
        payload["quote"] = payload["quote"][:max_chars]
    for e in Draft202012Validator(payload_schema).iter_errors(payload):
        return None, f"{'/'.join(map(str, e.absolute_path))}: {e.message}", False
    if not payload["noticed"]:
        payload["answers"], payload["behavior"] = {}, "none"
        return payload, None, truncated
    if payload["behavior"] not in spec["behavior_options"]:
        return None, f"behavior={payload['behavior']} 不在选项内", False
    for mid, value in payload["answers"].items():
        m = spec["measures"].get(mid)
        if m is None:
            return None, f"answers 含未问的题目 {mid}", False
        if value == "dk" and m["allow_dk"]:
            continue
        if "min" in m:
            ok = isinstance(value, int) and not isinstance(value, bool) and m["min"] <= value <= m["max"]
        elif "options" in m:
            ok = value in m["options"]
        else:
            ok = isinstance(value, str) and len(value) <= m["max_chars"]
        if not ok:
            return None, f"{mid}={value!r} 不合题目定义", False
    missing = [mid for mid, m in spec["measures"].items() if "max_chars" not in m and "ask_if" not in m and mid not in payload["answers"]]
    if missing:
        return None, f"缺少题目 {missing}", False
    return payload, None, truncated


def load_personas(directory: Path) -> list[dict]:
    personas = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(directory).glob("*.json"))]
    if not personas:
        raise FileNotFoundError(f"{directory} 下没有人物卡")
    return sorted(personas, key=lambda p: p["persona_id"])


def run_panel(experiment_path: Path, personas_dir: Path, out_dir: Path, gateway: Gateway, root: Path = ROOT) -> dict:
    experiment_path, out_dir = Path(experiment_path), Path(out_dir)
    x = _yaml(experiment_path)
    if x["design"]["mode"] != "panel":
        raise ValueError(f"只实现了 panel 模式，实验是 {x['design']['mode']}")
    stimulus_path = root / x["stimulus"]
    if not stimulus_path.is_file():
        raise FileNotFoundError(f"骨架只支持用文件路径引用刺激物：{x['stimulus']}")
    s = _yaml(stimulus_path)
    measures_cfg = _yaml(root / "config" / "measures.yaml")
    behavior_cfg = _yaml(root / "config" / "mbti_behavior_map.yaml")
    channel_cfg = _yaml(root / "config" / "channels.yaml")["channels"]
    text_cfg = _yaml(root / "config" / "persona_text.yaml")
    response_schema = json.loads((root / "schemas" / "response.schema.json").read_text(encoding="utf-8"))
    templates = {name: load_template(root, name) for name in ("persona_system", "panel_reaction")}
    spec = measure_spec(measures_cfg[x["pack"]], measures_cfg["scales"], x["measures"])
    frame_path = root / x["population"]["frame"]
    if not frame_path.is_file():
        raise FileNotFoundError(f"骨架只支持用文件路径引用人群框架：{x['population']['frame']}")
    library = load_personas(personas_dir)
    sample = draw_sample(_yaml(frame_path), library, x["design"]["seed"])
    if not sample["members"]:
        raise ValueError(f"{personas_dir} 下没有人物符合人群框架 {sample['frame_id']} 的任何分群")
    in_sample = {m["persona_id"] for m in sample["members"]}
    personas = [p for p in library if p["persona_id"] in in_sample]
    by_id = {v["variant_id"]: v for v in s["variants"]}
    # 安慰剂按其对应版本的属性决定问哪些题，否则两者的提示词会不同，噪声底就不再是噪声
    specs = {vid: applicable_spec(spec, s, by_id.get(v.get("placebo_of"), v) if v["role"] == "placebo" else v)
             for vid, v in by_id.items()}
    variant_ids = x["design"].get("variants") or list(by_id)
    seed, d = x["design"]["seed"], x["design"]

    responses = []
    for replicate in range(1, d["replicates"] + 1):
        for vid in variant_ids:
            run_id = f"run_{x['experiment_id']}_{vid}_r{replicate}_fam1"
            for p in personas:
                pid = p["persona_id"]
                exp = exposure(seed, p, replicate, d.get("exposure", "forced"), s["context"].get("channel_plan", []))
                rec = {
                    "response_id": f"r_{x['experiment_id']}_{vid}_{replicate}_{pid}", "run_id": run_id,
                    "persona_id": pid, "persona_version": p["version"], "variant_id": vid, "replicate": replicate,
                    "exposure": exp, "call_id": None, "valid": True, "invalid_reason": None,
                }
                if exp["exposed"]:
                    max_chars = hard_params(p, behavior_cfg)["max_chars"]
                    system = system_prompt(templates["persona_system"], p, behavior_cfg, text_cfg, s["context"]["as_of_date"])
                    user = user_prompt(templates["panel_reaction"], s, by_id[vid], exp["channel"], channel_cfg, specs[vid], max_chars)
                    reason = None
                    for attempt in range(MAX_ATTEMPTS):
                        retry = f"\n\n你上一次的输出不合格：{reason}。请重新只输出一个 JSON 对象。" if attempt else ""
                        result = gateway.call(
                            system=system, user=user + retry, purpose="persona", run_id=run_id, ordinal=attempt,
                            seed=int(u01(seed, "call", run_id, pid, attempt) * 2**32), spec=specs[vid])
                        payload, reason, truncated = validate_payload(result.text, specs[vid], response_schema["properties"]["payload"], max_chars)
                        rec["call_id"] = "c_" + result.call_key[:16]
                        if payload is not None:
                            break
                    rec["valid"], rec["invalid_reason"] = payload is not None, reason
                    if payload is not None:
                        rec["payload"] = payload
                        if truncated:
                            rec["truncated"] = True
                responses.append(rec)

    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "responses.jsonl").open("w", encoding="utf-8") as f:
        for rec in responses:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    with (out_dir / "sample.jsonl").open("w", encoding="utf-8") as f:
        for m in sample["members"]:
            f.write(json.dumps(m, ensure_ascii=False, sort_keys=True) + "\n")
    summary = {
        "notice": NOTICE,
        "experiment_id": x["experiment_id"],
        "model_id": gateway.backend.model_id,
        "seed": seed,
        "inputs": {"experiment": _sha(experiment_path), "stimulus": _sha(stimulus_path), "frame": _sha(frame_path),
                   "prompts": {name: _sha(root / "prompts" / f"{name}.md") for name in sorted(templates)}},
        "personas": {p["persona_id"]: p["version"] for p in personas},
        "variants": variant_ids,
        "replicates": d["replicates"],
        "model_families_run": 1,
        "sample": {k: v for k, v in sample.items() if k != "members"},
        "responses": len(responses),
        "exposed": sum(r["exposure"]["exposed"] for r in responses),
        "invalid": sum(not r["valid"] for r in responses),
        "truncated": sum(r.get("truncated", False) for r in responses),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return summary
