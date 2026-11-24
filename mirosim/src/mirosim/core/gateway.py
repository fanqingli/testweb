"""模型网关（骨架）：录制与回放、调用数与 token 预算。尚无并发、限流、降级。"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mirosim.core.rng import u01


class ReplayMiss(KeyError):
    """回放模式下录制里没有这次调用。"""


class BudgetExceeded(RuntimeError):
    """调用数或 token 用量已到预算。"""


class Backend(Protocol):
    model_id: str

    def complete(self, system: str, user: str, *, seed: int, spec: dict | None = None) -> str:
        """spec 是这次调用期望的题目定义，供支持结构化输出的后端使用；它已体现在提示词里，不进 call_key。"""
        ...


@dataclass(frozen=True)
class CallResult:
    call_key: str
    text: str
    from_recording: bool


def call_key(*, run_id: str, purpose: str, model_id: str, params: dict, system: str, user: str,
             ordinal: int, seed: int) -> str:
    """run_id 在键里：安慰剂与其对应版本、同一版本的各次重复，提示词逐字相同，
    但必须是各自独立的调用，否则噪声底恒为零。"""
    blob = json.dumps([run_id, purpose, model_id, params, seed, system, user, ordinal], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


class Gateway:
    def __init__(self, backend: Backend, recording: Path, *, replay: bool = False,
                 params: dict | None = None, max_calls: int | None = None,
                 max_input_tokens: int | None = None, max_output_tokens: int | None = None):
        self.backend, self.recording, self.replay = backend, Path(recording), replay
        self.params, self.max_calls = params or {}, max_calls
        self.max_tokens = {"input_tokens": max_input_tokens, "output_tokens": max_output_tokens}
        self.usage = {"input_tokens": 0, "output_tokens": 0}  # 录制里全部调用的合计；后端不报用量时不计
        self.live_calls = 0
        self._recorded: dict[str, str] = {}
        if self.recording.exists():
            for line in self.recording.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                self._recorded[rec["call_key"]] = rec["response"]
                self._count(rec.get("usage"))
        elif replay:
            raise FileNotFoundError(self.recording)

    @property
    def recorded_calls(self) -> int:
        return len(self._recorded)

    def _count(self, usage: dict | None) -> None:
        for k in self.usage:
            self.usage[k] += (usage or {}).get(k) or 0

    def call(self, *, system: str, user: str, purpose: str, run_id: str, ordinal: int = 0, seed: int = 0,
             spec: dict | None = None) -> CallResult:
        key = call_key(run_id=run_id, purpose=purpose, model_id=self.backend.model_id, params=self.params,
                       system=system, user=user, ordinal=ordinal, seed=seed)
        if key in self._recorded:
            return CallResult(key, self._recorded[key], True)
        if self.replay:
            raise ReplayMiss(f"{run_id} #{ordinal}")
        if self.max_calls is not None and len(self._recorded) >= self.max_calls:  # 录制里已有的调用也算
            raise BudgetExceeded(f"录制里已有 {len(self._recorded)} 次调用，预算 {self.max_calls}")
        for k, limit in self.max_tokens.items():  # 事后计量：到线后不再发新调用，最后一次调用可能略超
            if limit is not None and self.usage[k] >= limit:
                raise BudgetExceeded(f"{k} 已用 {self.usage[k]}，预算 {limit}")
        text = self.backend.complete(system, user, seed=seed, spec=spec)
        self.live_calls += 1
        usage = getattr(self.backend, "last_usage", None)
        rec = {"call_key": key, "run_id": run_id, "purpose": purpose, "model_id": self.backend.model_id,
               "params": self.params, "seed": seed, "ordinal": ordinal, "request": {"system": system, "user": user}, "response": text}
        if usage:
            rec["usage"] = usage
        self.recording.parent.mkdir(parents=True, exist_ok=True)
        with self.recording.open("a", encoding="utf-8") as f:  # 写入成功后才把结果交给调用方
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._recorded[key] = text
        self._count(usage)
        return CallResult(key, text, False)


class MockBackend:
    """不是模型。按种子给出格式合法的随机答案，只用来打通流程和做测试；
    它的输出不含任何关于人物或方案的信息，不能当作模拟结果。"""

    model_id = "mock-v1"

    def complete(self, system: str, user: str, *, seed: int, spec: dict | None = None) -> str:
        if spec is None:
            raise ValueError("MockBackend 需要题目定义 spec 才能给出格式合法的答案")
        prompt = hashlib.sha256((system + user).encode()).hexdigest()

        def pick(key: str, n: int) -> int:
            return int(u01(seed, prompt, key) * n)

        noticed = u01(seed, prompt, "noticed") < 0.85
        answers: dict = {}
        if noticed:
            for mid, m in spec["measures"].items():
                if "min" in m:
                    answers[mid] = m["min"] + pick(mid, m["max"] - m["min"] + 1)
                elif "options" in m:
                    answers[mid] = m["options"][pick(mid, len(m["options"]))]
                else:
                    answers[mid] = "[mock]"
        options = spec["behavior_options"]
        return json.dumps({
            "noticed": noticed,
            "takeaway": "[mock]" if noticed else "",
            "inner_reaction": "[mock]",
            "answers": answers,
            "behavior": options[pick("behavior", len(options))] if noticed else "none",
            "reasons": ["[mock]"] if noticed else [],
            "share": {"would_share": noticed and u01(seed, prompt, "share") < 0.2, "to_whom": None, "what": None},
            "quote": "[mock]",
        }, ensure_ascii=False)
