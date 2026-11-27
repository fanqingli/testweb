"""模型网关（骨架）：录制与回放、调用数与 token 预算、限速；可以被多个线程同时调用。尚无降级。"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mirosim.core.rng import u01


class ReplayMiss(KeyError):
    """回放模式下录制里没有这次调用。"""


class BudgetExceeded(RuntimeError):
    """调用数或 token 用量已到预算。"""


class Stopped(RuntimeError):
    """运行已因别处失败而停止，不再发新调用"""


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
             ordinal: int, seed: int, member: str | None = None) -> str:
    """run_id 在键里：安慰剂与其对应版本、同一版本的各次重复，提示词逐字相同，
    但必须是各自独立的调用，否则噪声底恒为零。"""
    parts = [run_id, purpose, model_id, params, seed, system, user, ordinal]
    if member is not None:
        parts.append(member)
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


class Gateway:
    def __init__(self, backend: Backend | None = None, recording: Path | None = None, *, replay: bool = False,
                 params: dict | None = None, max_calls: int | None = None,
                 max_input_tokens: int | None = None, max_output_tokens: int | None = None,
                 requests_per_minute: float | None = None, clock=time.monotonic, sleep=time.sleep,
                 backends: dict[str, Backend] | None = None, params_by: dict[str, dict] | None = None,
                 tiers_by: dict[str, str] | None = None):
        if recording is None:
            raise ValueError("recording 路径必填")
        if (backend is None) == (backends is None):
            raise ValueError("必须且只能设置 backend 或 backends")
        if backends is not None and not backends:
            raise ValueError("backends 不能为空")
        if backends is None and params_by is not None:
            raise ValueError("单后端模式不能设置 params_by")
        if backends is not None:
            params_by = params_by or {
                member: getattr(item, "key_params", {}) for member, item in backends.items()
            }
            if set(params_by) != set(backends):
                raise ValueError("params_by 必须与 backends 使用相同的 member")
            self.params_by = {member: params_by[member] or {} for member in backends}
            tiers_by = tiers_by or {member: member for member in backends}
            if set(tiers_by) != set(backends):
                raise ValueError("tiers_by 必须与 backends 使用相同的 member")
            self.tiers_by = dict(tiers_by)
        else:
            self.params_by = None
            if tiers_by is not None:
                raise ValueError("单后端模式不能设置 tiers_by")
            self.tiers_by = None
        self.backend, self.recording, self.replay = backend, Path(recording), replay
        self.params, self.max_calls = params or {}, max_calls
        self.backends = backends
        self.max_tokens = {"input_tokens": max_input_tokens, "output_tokens": max_output_tokens}
        self.usage = {"input_tokens": 0, "output_tokens": 0}  # 录制里全部调用的合计；后端不报用量时不计
        self.live_calls = 0
        self._lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._in_flight = 0  # 已发出、还没回来的调用；算进调用数预算，并发时才不会超
        self._interval = 60.0 / requests_per_minute if requests_per_minute else 0.0
        self._next_send, self._clock, self._sleep = 0.0, clock, sleep
        self._stopped = False
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

    def _check_token_budget(self) -> None:
        for k, limit in self.max_tokens.items():  # 事后计量：到线后不再发新调用，在途的那几次可能使总量略超
            if limit is not None and self.usage[k] >= limit:
                raise BudgetExceeded(f"{k} 已用 {self.usage[k]}，预算 {limit}")

    def _check_send(self) -> None:
        with self._lock:
            if self._stopped:
                raise Stopped("运行已因别处失败而停止，不再发新调用")
            self._check_token_budget()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True

    def call(self, *, system: str, user: str, purpose: str, run_id: str, ordinal: int = 0, seed: int = 0,
             spec: dict | None = None, member: str | None = None) -> CallResult:
        if self.backends is None:
            if member is not None:
                raise ValueError("单后端模式不能指定 member")
            backend, params, selected_member = self.backend, self.params, None
        else:
            if member not in self.backends:
                raise ValueError(f"未知或缺失的 model member：{member!r}")
            backend, params, selected_member = self.backends[member], self.params_by[member], member
        key = call_key(run_id=run_id, purpose=purpose, model_id=backend.model_id, params=params,
                       system=system, user=user, ordinal=ordinal, seed=seed, member=selected_member)
        with self._lock:
            if key in self._recorded:
                return CallResult(key, self._recorded[key], True)
            if self._stopped:
                raise Stopped("运行已因别处失败而停止，不再发新调用")
            if self.replay:
                raise ReplayMiss(f"{run_id} #{ordinal}")
            used = len(self._recorded) + self._in_flight  # 录制里已有的调用也算
            if self.max_calls is not None and used >= self.max_calls:
                raise BudgetExceeded(f"录制里已有 {len(self._recorded)} 次调用，另有 {self._in_flight} 次在途，预算 {self.max_calls}")
            self._check_token_budget()
            self._in_flight += 1
        try:
            if self._interval:
                with self._rate_lock:  # 发送按顺序排队：相邻两次真实发送至少隔 60/rpm 秒
                    wait = self._next_send - self._clock()
                    if wait > 0:
                        self._sleep(wait)
                    self._check_send()
                    self._next_send = self._clock() + self._interval
            else:
                self._check_send()
            text = backend.complete(system, user, seed=seed, spec=spec)
            usage = getattr(backend, "last_usage", None)  # 后端按线程各存一份，见 OpenAICompatBackend
            rec = {"call_key": key, "run_id": run_id, "purpose": purpose, "model_id": backend.model_id,
                   "params": params, "seed": seed, "ordinal": ordinal, "request": {"system": system, "user": user}, "response": text}
            if selected_member is not None:
                rec["model_member"] = selected_member
            if usage:
                rec["usage"] = usage
            with self._lock:
                self.recording.parent.mkdir(parents=True, exist_ok=True)
                with self.recording.open("a", encoding="utf-8") as f:  # 写入成功后才把结果交给调用方
                    f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                self._recorded[key] = text
                self._count(usage)
                self.live_calls += 1
        finally:
            with self._lock:
                self._in_flight -= 1
        return CallResult(key, text, False)


class MockBackend:
    """不是模型。按种子给出格式合法的随机答案，只用来打通流程和做测试；
    它的输出不含任何关于人物或方案的信息，不能当作模拟结果。"""

    model_id = "mock-v1"
    provider = "mock"

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
