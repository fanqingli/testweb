import json
import random
import threading
import time

import pytest

from mirosim import ROOT
from mirosim.cli import main
from mirosim.core.gateway import BudgetExceeded, Gateway, MockBackend, Stopped
from mirosim.core.openai_backend import BackendError, OpenAICompatBackend
from mirosim.engines.panel import in_order, run_panel

EX = ROOT / "examples"
POLICY = EX / "experiment.policy.yaml"
PRODUCTS = ("responses.jsonl", "sample.jsonl", "summary.json")


class Slow(MockBackend):
    """每次调用随机停一小会儿，让完成顺序和发出顺序不一样；记下同时在途的最大个数。"""

    def __init__(self):
        self.lock, self.active, self.peak, self.calls = threading.Lock(), 0, 0, 0

    def complete(self, system, user, *, seed, spec=None):
        with self.lock:
            self.active += 1
            self.calls += 1
            self.peak = max(self.peak, self.active)
        time.sleep(random.random() * 0.01)
        with self.lock:
            self.active -= 1
        return super().complete(system, user, seed=seed, spec=spec)


def run(out, backend=None, concurrency=1, **kw):
    gateway = Gateway(backend or MockBackend(), out / "recording.jsonl", **kw)
    return run_panel(POLICY, EX / "personas", out, gateway, concurrency=concurrency), gateway


def keys(out):
    return sorted(json.loads(line)["call_key"] for line in (out / "recording.jsonl").read_text(encoding="utf-8").splitlines())


def test_concurrent_run_gives_the_same_products_as_a_serial_one(tmp_path):
    run(tmp_path / "serial")
    backend = Slow()
    _, gateway = run(tmp_path / "parallel", backend, concurrency=3)
    assert backend.peak > 1 and gateway.live_calls == 45
    for name in PRODUCTS:
        assert (tmp_path / "parallel" / name).read_bytes() == (tmp_path / "serial" / name).read_bytes()
    assert keys(tmp_path / "parallel") == keys(tmp_path / "serial")
    run(tmp_path / "parallel", concurrency=3, replay=True)  # 回放也可以并发
    assert (tmp_path / "parallel" / "responses.jsonl").read_bytes() == (tmp_path / "serial" / "responses.jsonl").read_bytes()


def test_concurrency_never_exceeds_the_limit():
    backend, gateway_calls = Slow(), []
    in_order(lambda i: gateway_calls.append(backend.complete("s", "u", seed=i, spec={"measures": {}, "behavior_options": ["none"]})),
             list(range(40)), 4)
    assert backend.calls == 40 and 1 < backend.peak <= 4


def test_call_budget_holds_under_concurrency(tmp_path):
    backend = Slow()
    with pytest.raises(BudgetExceeded):
        run(tmp_path, backend, concurrency=3, max_calls=10)
    assert backend.calls == 10 and len(keys(tmp_path)) == 10
    _, gateway = run(tmp_path, concurrency=3, max_calls=45)  # 续跑照旧
    assert gateway.live_calls == 35


def test_first_failure_stops_new_work_but_keeps_what_was_already_sent():
    started, lock = [], threading.Lock()

    def work(i):
        with lock:
            started.append(i)
        if i == 1:
            raise BackendError("服务拒绝")
        time.sleep(0.05)
        return i

    with pytest.raises(BackendError):
        in_order(work, list(range(200)), 2)
    assert len(started) < 10
    assert in_order(lambda i: i * 2, [3, 1, 2], 4) == [6, 2, 4] == in_order(lambda i: i * 2, [3, 1, 2], 1)


def test_interrupt_cancels_queued_work(monkeypatch):
    started, lock = [], threading.Lock()
    failed = threading.Event()

    def interrupted_wait(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("mirosim.engines.panel.wait", interrupted_wait)

    def work(i):
        with lock:
            started.append(i)
        time.sleep(0.05)

    with pytest.raises(KeyboardInterrupt):
        in_order(work, list(range(50)), 2, on_fail=failed.set)
    assert failed.is_set()
    assert len(started) < 5


def test_in_order_surfaces_real_failure_after_on_fail_stops_lower_index():
    stopped = threading.Event()
    lower_failed = threading.Event()

    def work(i):
        if i == 0:
            assert stopped.wait(timeout=1)
            lower_failed.set()
            raise Stopped("stopped")
        raise BackendError("服务拒绝")

    def on_fail():
        stopped.set()
        assert lower_failed.wait(timeout=1)

    with pytest.raises(BackendError, match="服务拒绝"):
        in_order(work, [0, 1], 2, on_fail=on_fail)


def test_rate_limit_spaces_live_calls_and_ignores_recorded_ones(tmp_path):
    now, slept = [100.0], []

    def sleep(seconds):
        slept.append(round(seconds, 6))
        now[0] += seconds

    spec = {"measures": {}, "behavior_options": ["none"]}
    gateway = Gateway(MockBackend(), tmp_path / "rec.jsonl", requests_per_minute=120, clock=lambda: now[0], sleep=sleep)
    for i in range(3):
        gateway.call(system="s", user=f"u{i}", purpose="p", run_id="r", spec=spec)
    assert slept == [0.5, 0.5]  # 第一次不等，之后每次隔 60/120 秒
    for i in range(3):
        assert gateway.call(system="s", user=f"u{i}", purpose="p", run_id="r", spec=spec).from_recording
    assert slept == [0.5, 0.5]
    now[0] += 10  # 闲了一阵之后不用补等
    gateway.call(system="s", user="u9", purpose="p", run_id="r", spec=spec)
    assert slept == [0.5, 0.5]


def test_token_budget_is_rechecked_after_rate_wait(tmp_path):
    now = [0.0]
    calls = []
    gateway = None

    class CountingBackend(MockBackend):
        def complete(self, system, user, *, seed, spec=None):
            calls.append(user)
            return super().complete(system, user, seed=seed, spec=spec)

    def sleep(seconds):
        now[0] += seconds
        gateway.usage["output_tokens"] = 5

    gateway = Gateway(CountingBackend(), tmp_path / "rec.jsonl", max_output_tokens=5,
                      requests_per_minute=60, clock=lambda: now[0], sleep=sleep)
    spec = {"measures": {}, "behavior_options": ["none"]}
    gateway.call(system="s", user="first", purpose="p", run_id="r", spec=spec)
    with pytest.raises(BudgetExceeded, match="output_tokens"):
        gateway.call(system="s", user="waiting", purpose="p", run_id="r", spec=spec)
    assert calls == ["first"]
    assert gateway._in_flight == 0
    gateway.max_tokens["output_tokens"] = 10
    gateway.call(system="s", user="waiting", purpose="p", run_id="r", spec=spec)
    assert calls == ["first", "waiting"]


def test_rate_limit_spaces_actual_sends_after_oversleep(tmp_path):
    now = [0.0]
    send_times = []

    class TimedBackend(MockBackend):
        def complete(self, system, user, *, seed, spec=None):
            send_times.append(now[0])
            return super().complete(system, user, seed=seed, spec=spec)

    def sleep(seconds):
        now[0] += seconds + 0.3

    gateway = Gateway(TimedBackend(), tmp_path / "rec.jsonl", requests_per_minute=60,
                      clock=lambda: now[0], sleep=sleep)
    spec = {"measures": {}, "behavior_options": ["none"]}
    for i in range(3):
        gateway.call(system="s", user=f"u{i}", purpose="p", run_id="r", spec=spec)
    assert len(send_times) == 3
    assert all(second - first >= 1.0 for first, second in zip(send_times, send_times[1:]))


def test_stop_rejects_live_calls_but_allows_recorded_calls(tmp_path):
    backend = Slow()
    gateway = Gateway(backend, tmp_path / "rec.jsonl")
    spec = {"measures": {}, "behavior_options": ["none"]}
    recorded = gateway.call(system="s", user="recorded", purpose="p", run_id="r", spec=spec)
    gateway.stop()
    assert gateway.call(system="s", user="recorded", purpose="p", run_id="r", spec=spec).from_recording
    with pytest.raises(Stopped):
        gateway.call(system="s", user="new", purpose="p", run_id="r", spec=spec)
    assert backend.calls == 1


def test_panel_failure_stops_further_sends(tmp_path):
    class FailingBackend(MockBackend):
        def __init__(self):
            self.lock = threading.Lock()
            self.calls = 0

        def complete(self, system, user, *, seed, spec=None):
            with self.lock:
                self.calls += 1
                call_number = self.calls
            if call_number == 3:
                raise BackendError("第三次调用失败")
            time.sleep(0.01)
            return super().complete(system, user, seed=seed, spec=spec)

    backend = FailingBackend()
    gateway = Gateway(backend, tmp_path / "recording.jsonl")
    with pytest.raises(BackendError, match="第三次调用失败"):
        run_panel(POLICY, EX / "personas", tmp_path / "panel", gateway, concurrency=4)
    assert backend.calls <= 3 + 4


def test_token_usage_is_attributed_to_the_right_call_under_concurrency(tmp_path):
    def transport(url, headers, body, timeout):
        n = int(json.loads(body)["messages"][1]["content"])
        time.sleep(random.random() * 0.01)
        reply = {"choices": [{"message": {"content": str(n)}}], "usage": {"prompt_tokens": n, "completion_tokens": 1000 + n}}
        return 200, {}, json.dumps(reply)

    backend = OpenAICompatBackend(model_id="m", base_url="https://api.example.com/v1", api_key="k", transport=transport)
    gateway = Gateway(backend, tmp_path / "rec.jsonl")
    in_order(lambda n: gateway.call(system="s", user=str(n), purpose="p", run_id="r"), list(range(1, 41)), 8)
    records = [json.loads(line) for line in (tmp_path / "rec.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(records) == 40
    for rec in records:
        n = int(rec["response"])
        assert rec["usage"] == {"input_tokens": n, "output_tokens": 1000 + n}
    assert gateway.usage == {"input_tokens": sum(range(1, 41)), "output_tokens": 40 * 1000 + sum(range(1, 41))}


def test_cli_flags(tmp_path, capsys):
    args = ["run-panel", "--experiment", str(POLICY), "--personas", str(EX / "personas"), "--out", str(tmp_path / "run")]
    assert main(args + ["--concurrency", "4", "--rpm", "60000"]) == 0
    assert json.loads(capsys.readouterr().out)["live_calls"] == 45
    for bad in (["--concurrency", "0"], ["--rpm", "0"]):
        with pytest.raises(SystemExit):
            main(args + bad)
