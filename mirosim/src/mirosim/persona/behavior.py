"""由人物卡与 config/mbti_behavior_map.yaml 计算行为参数。

输出硬控制参数（代码强制执行）与软渲染文本（写入人物提示词）。
命令行入口见 tools/derive_behavior.py。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from mirosim import ROOT

AXES = ("ei", "sn", "tf", "jp")
LETTERS = {"ei": "EI", "sn": "NS", "tf": "TF", "jp": "JP"}  # 正向字母在前


def mbti_type(axes: dict) -> str:
    return "".join(LETTERS[a][0] if axes[a] >= 0 else LETTERS[a][1] for a in AXES)


def band(value: float, bands: dict) -> str:
    for name, (lo, hi) in bands.items():
        if lo <= value < hi or (hi == 1.0 and value == 1.0):
            return name
    raise ValueError(value)


def normalized_inputs(persona: dict) -> dict:
    psyche = persona["psyche"]
    inputs = dict(psyche["mbti"]["axes"])
    inputs.update({k: 2 * v - 1 for k, v in psyche["traits"].items()})
    return inputs


def hard_params(persona: dict, cfg: dict, context: dict | None = None) -> dict:
    inputs = normalized_inputs(persona)
    context = context or {}
    out = {}
    for name, spec in cfg["hard"].items():
        value = spec["base"]
        value += sum(w * inputs[k] for k, w in spec.get("terms", {}).items())
        value += sum(w * context.get(k, 0.0) for k, w in spec.get("context_terms", {}).items())
        lo, hi = spec["clamp"]
        value = max(lo, min(hi, value))
        if "round_to" in spec:
            value = int(round(value / spec["round_to"]) * spec["round_to"])
        else:
            value = round(value, 3)
        out[name] = value
    return out


def soft_text(persona: dict, cfg: dict) -> list[str]:
    psyche = persona["psyche"]
    lines = [cfg["soft"][a][band(psyche["mbti"]["axes"][a], cfg["bands"])] for a in AXES]
    for trait, value in psyche["traits"].items():
        level = "low" if value < 0.35 else "high" if value > 0.65 else None
        if level:
            lines.append(cfg["soft"]["traits"][trait][level])
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("persona")
    ap.add_argument("--power-gap", type=float, default=0.0)
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config" / "mbti_behavior_map.yaml").read_text(encoding="utf-8"))
    persona = json.loads(Path(args.persona).read_text(encoding="utf-8"))
    result = {
        "persona_id": persona["persona_id"],
        "mbti_from_axes": mbti_type(persona["psyche"]["mbti"]["axes"]),
        "hard": hard_params(persona, cfg, {"power_gap": args.power_gap}),
        "soft": soft_text(persona, cfg),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
