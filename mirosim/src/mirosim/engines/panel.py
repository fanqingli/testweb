"""群体反应模式（骨架）。

已有：公共随机数曝光、录制与回放、反应校验与一次重试、调用预算。
未做：按人群框架抽样与加权（人物由目录直接给出）、记忆读取、同伴信号、
多模型族、prompts/ 下模板的接入（这里的提示词是临时的最小版本）、字数等硬控制。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from mirosim import NOTICE, ROOT
from mirosim.core.gateway import Gateway
from mirosim.core.rng import u01
from mirosim.persona.behavior import soft_text

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
            item.update(min=scales[m["type"]]["min"], max=scales[m["type"]]["max"])
        elif m["type"] == "enum":
            item["options"] = m["options"]
        else:
            item["max_chars"] = m.get("max_chars", 60)
        measures[mid] = item
    return {"measures": measures, "behavior_options": list(pack_cfg["behavior"]["options"])}


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


def system_prompt(persona: dict, behavior_cfg: dict) -> str:
    """临时的最小版本。四字母类型不进提示词，只给行为描述。"""
    card = {k: persona[k] for k in ("identity", "situation", "history")}
    return (
        "你在扮演下面这个虚构的合成人物。只按他知道的事、他的处境和他的说话方式作答，不要解释自己是模型。\n\n"
        "人物：\n" + json.dumps(card, ensure_ascii=False, sort_keys=True, indent=1) + "\n\n"
        "行为倾向：\n" + "\n".join(f"- {line}" for line in soft_text(persona, behavior_cfg))
    )


def user_prompt(stimulus: dict, variant: dict, channel: str | None, channel_cfg: dict, spec: dict) -> str:
    """版本的编号、标签、角色不进提示词：安慰剂的提示词必须与其对应版本逐字相同。"""
    text = (variant.get("renderings") or {}).get(channel, variant["content"]) if channel else variant["content"]
    seen = f"你在「{channel_cfg.get(channel, channel)}」看到了下面这件事。\n" if channel else ""
    return (
        f"背景：{stimulus['context']['background'].strip()}\n\n{seen}内容：{text.strip()}\n\n"
        "只输出一个 JSON 对象，字段为 noticed、takeaway、inner_reaction、answers、behavior、reasons、share、quote。"
        "answers 的键是下面 measures 里的题目编号；behavior 从 behavior_options 里选一个。"
        "如果你不会停下来看（noticed=false），answers 留空，behavior 填 none。\n\n"
        "```json\n" + json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=1) + "\n```"
    )


def validate_payload(text: str, spec: dict, payload_schema: dict) -> tuple[dict | None, str | None]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"不是合法的 JSON：{e.msg}"
    for e in Draft202012Validator(payload_schema).iter_errors(payload):
        return None, f"{'/'.join(map(str, e.absolute_path))}: {e.message}"
    if payload["behavior"] not in spec["behavior_options"]:
        return None, f"behavior={payload['behavior']} 不在选项内"
    if not payload["noticed"]:
        if payload["behavior"] != "none" or payload["answers"]:
            return None, "noticed=false 时 answers 应为空、behavior 应为 none"
        return payload, None
    for mid, value in payload["answers"].items():
        m = spec["measures"].get(mid)
        if m is None:
            return None, f"answers 含未问的题目 {mid}"
        if value == "dk" and m["allow_dk"]:
            continue
        if "min" in m:
            ok = isinstance(value, int) and not isinstance(value, bool) and m["min"] <= value <= m["max"]
        elif "options" in m:
            ok = value in m["options"]
        else:
            ok = isinstance(value, str) and len(value) <= m["max_chars"]
        if not ok:
            return None, f"{mid}={value!r} 不合题目定义"
    missing = [mid for mid, m in spec["measures"].items() if "max_chars" not in m and mid not in payload["answers"]]
    if missing:
        return None, f"缺少题目 {missing}"
    return payload, None


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
    response_schema = json.loads((root / "schemas" / "response.schema.json").read_text(encoding="utf-8"))
    spec = measure_spec(measures_cfg[x["pack"]], measures_cfg["scales"], x["measures"])
    personas = load_personas(personas_dir)
    by_id = {v["variant_id"]: v for v in s["variants"]}
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
                    system = system_prompt(p, behavior_cfg)
                    user = user_prompt(s, by_id[vid], exp["channel"], channel_cfg, spec)
                    for attempt in range(MAX_ATTEMPTS):
                        result = gateway.call(
                            system=system, user=user, purpose="persona", run_id=run_id, ordinal=attempt,
                            seed=int(u01(seed, "call", run_id, pid, attempt) * 2**32))
                        payload, reason = validate_payload(result.text, spec, response_schema["properties"]["payload"])
                        rec["call_id"] = "c_" + result.call_key[:16]
                        if payload is not None:
                            break
                    rec["valid"], rec["invalid_reason"] = payload is not None, reason
                    if payload is not None:
                        rec["payload"] = payload
                responses.append(rec)

    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "responses.jsonl").open("w", encoding="utf-8") as f:
        for rec in responses:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    summary = {
        "notice": NOTICE,
        "experiment_id": x["experiment_id"],
        "model_id": gateway.backend.model_id,
        "seed": seed,
        "inputs": {"experiment": _sha(experiment_path), "stimulus": _sha(stimulus_path)},
        "personas": {p["persona_id"]: p["version"] for p in personas},
        "variants": variant_ids,
        "replicates": d["replicates"],
        "model_families_run": 1,
        "weighted": False,
        "responses": len(responses),
        "exposed": sum(r["exposure"]["exposed"] for r in responses),
        "invalid": sum(not r["valid"] for r in responses),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return summary
