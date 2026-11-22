#!/usr/bin/env python3
"""由人物卡与 config/mbti_behavior_map.yaml 计算行为参数的参考实现。

用法:
    python tools/derive_behavior.py examples/personas/p_0a31f7.json [--power-gap 0.6]

输出硬控制参数（代码强制执行）与软渲染文本（写入人物提示词）。
同一文件还提供 u01()：全系统统一使用的、与调用顺序无关的确定性随机数。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
AXES = ("ei", "sn", "tf", "jp")
LETTERS = {"ei": "EI", "sn": "NS", "tf": "TF", "jp": "JP"}  # 正向字母在前


def u01(seed: int, *parts: object) -> float:
    """确定性均匀随机数 [0,1)。键相同则结果相同，与并发和调用顺序无关。

    曝光抽签的键不含 variant_id，因此同一人物在各平行世界里得到同一个随机数
    （公共随机数），方案之间的差异不会混入抽签噪声。
    """
    key = "|".join([str(seed), *map(str, parts)]).encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big") / 2**64


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
