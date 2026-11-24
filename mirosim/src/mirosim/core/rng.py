"""全系统统一使用的确定性随机数。"""
from __future__ import annotations

import hashlib


def u01(seed: int, *parts: object) -> float:
    """确定性均匀随机数 [0,1)。键相同则结果相同，与并发和调用顺序无关。

    曝光抽签的键不含 variant_id，因此同一人物在各平行世界里得到同一个随机数
    （公共随机数），方案之间的差异不会混入抽签噪声。
    """
    key = "|".join([str(seed), *map(str, parts)]).encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big") / 2**64
