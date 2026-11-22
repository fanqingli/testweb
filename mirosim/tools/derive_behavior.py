#!/usr/bin/env python3
"""计算一张人物卡的行为参数。实现在 src/mirosim/persona/behavior.py。

用法:
    python tools/derive_behavior.py examples/personas/p_0a31f7.json [--power-gap 0.6]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mirosim.core.rng import u01  # noqa: E402,F401
from mirosim.persona.behavior import hard_params, main, mbti_type, soft_text  # noqa: E402,F401

if __name__ == "__main__":
    main()
