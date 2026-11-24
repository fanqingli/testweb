import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml

from mirosim import ROOT
from mirosim.cli import main
from mirosim.core.gateway import BudgetExceeded, Gateway
from mirosim.core.openai_backend import BackendError, OpenAICompatBackend, from_tier
from mirosim.engines.panel import run_panel

EX = ROOT / "examples"
KEY = "sk-test-do-not-leak"


def reply(content='{"ok": 1}', usage=(120, 30)):
    return 200, {}, json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}],
                                "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}})


class Script:
    """按顺序给出预设的 (状态码, 响应头, 正文)，记下收到的请求。"""

    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, url, headers, body, timeout):
        self.requests.append({"url": url, "headers": headers, "body": json.loads(body)})
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def backend(transport, **kw):
    waits = []
    b = OpenAICompatBackend(model_id="old-cheap-model", base_url="https://api.example.com/v1/", api_key=KEY,
                            params={"temperature": 0.9, "max_tokens": 600}, transport=transport, sleep=waits.append, **kw)
    return b, waits


def test_request_is_plain_chat_completions_by_default():
    t = Script(reply())
    b, _ = backend(t)
    assert b.complete("系统", "用户", seed=7) == '{"ok": 1}'
    req = t.requests[0]
    assert req["url"] == "https://api.example.com/v1/chat/completions"
    assert req["headers"]["Authorization"] == f"Bearer {KEY}"
    assert req["body"] == {"model": "old-cheap-model", "temperature": 0.9, "max_tokens": 600,
                           "messages": [{"role": "system", "content": "系统"}, {"role": "user", "content": "用户"}]}
    assert b.last_usage == {"input_tokens": 120, "output_tokens": 30}


def test_json_mode_and_seed_are_sent_only_when_declared():
    t = Script(reply())
    b, _ = backend(t, supports_json_mode=True, supports_seed=True)
    b.complete("s", "u", seed=7)
    assert t.requests[0]["body"]["response_format"] == {"type": "json_object"} and t.requests[0]["body"]["seed"] == 7


def test_transient_failures_are_retried_with_backoff():
    t = Script((429, {"Retry-After": "3"}, "slow down"), (503, {}, "busy"), (0, {}, "timed out"), reply("好"))
    b, waits = backend(t)
    assert b.complete("s", "u", seed=1) == "好" and len(t.requests) == 4 and waits == [3.0, 2.0, 4.0]


def test_gives_up_after_max_retries_and_does_not_retry_client_errors():
    b, waits = backend(Script((500, {}, "boom")), max_retries=2)
    with pytest.raises(BackendError, match="重试 2 次"):
        b.complete("s", "u", seed=1)
    assert len(waits) == 2
    t = Script((401, {}, "invalid api key"))
    b, waits = backend(t)
    with pytest.raises(BackendError, match="401") as e:
        b.complete("s", "u", seed=1)
    assert len(t.requests) == 1 and waits == [] and KEY not in str(e.value)


def test_odd_responses():
    b, _ = backend(Script((200, {}, json.dumps({"choices": [{"message": {"content": None}}]}))))
    assert b.complete("s", "u", seed=1) == "" and b.last_usage == {"input_tokens": None, "output_tokens": None}
    for body in ("<html>gateway</html>", json.dumps({"error": {"message": "x"}}), json.dumps({"choices": []})):
        b, _ = backend(Script((200, {}, body)))
        with pytest.raises(BackendError, match="不是 chat/completions 格式"):
            b.complete("s", "u", seed=1)


def test_from_tier_reads_config_and_takes_key_from_env_only():
    cfg = yaml.safe_load((ROOT / "config" / "model_tiers.yaml").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="还没有填"):
        from_tier(cfg, "tier-economy", env={"MIROSIM_API_KEY": KEY})
    cfg["tiers"]["tier-economy"]["primary"].update(model_id="m", base_url="https://x/v1", api_key_env="MY_KEY")
    with pytest.raises(ValueError, match="MY_KEY"):
        from_tier(cfg, "tier-economy", env={"MIROSIM_API_KEY": KEY})
    with pytest.raises(ValueError, match="没有档位"):
        from_tier(cfg, "tier-nope", env={})
    b = from_tier(cfg, "tier-economy", temperature=0.3, env={"MY_KEY": KEY})
    assert b.params == {"temperature": 0.3, "top_p": 0.95, "max_tokens": 600}
    assert not b.supports_json_mode and not b.supports_seed
    assert from_tier(cfg, "tier-economy", env={}, require_key=False).params["temperature"] == 0.9


def test_gateway_records_usage_and_enforces_token_budget(tmp_path):
    b, _ = backend(Script(reply(usage=(100, 40))))
    g = Gateway(b, tmp_path / "rec.jsonl", params=b.params, max_output_tokens=80)
    g.call(system="s", user="u1", purpose="p", run_id="r")
    g.call(system="s", user="u2", purpose="p", run_id="r")
    assert g.usage == {"input_tokens": 200, "output_tokens": 80}
    with pytest.raises(BudgetExceeded, match="output_tokens"):
        g.call(system="s", user="u3", purpose="p", run_id="r")
    text = (tmp_path / "rec.jsonl").read_text(encoding="utf-8")
    assert json.loads(text.splitlines()[0])["usage"] == {"input_tokens": 100, "output_tokens": 40} and KEY not in text
    resumed = Gateway(b, tmp_path / "rec.jsonl", params=b.params, max_output_tokens=80)  # 续跑不会重新得到一份预算
    assert resumed.usage == g.usage
    assert resumed.call(system="s", user="u1", purpose="p", run_id="r").from_recording
    with pytest.raises(BudgetExceeded):
        resumed.call(system="s", user="u3", purpose="p", run_id="r")


def test_sampling_params_are_part_of_the_call_key(tmp_path):
    b, _ = backend(Script(reply()))
    Gateway(b, tmp_path / "rec.jsonl", params={"temperature": 0.9}).call(system="s", user="u", purpose="p", run_id="r")
    hotter = Gateway(b, tmp_path / "rec.jsonl", params={"temperature": 1.2})
    assert not hotter.call(system="s", user="u", purpose="p", run_id="r").from_recording


ANSWER = {"noticed": True, "takeaway": "要设过渡期", "inner_reaction": "又要涨价？",
          "answers": {"pol_impact": 1, "pol_fair": 4, "pol_stance": "conditional", "pol_trust_exec": 2},
          "behavior": "wait", "reasons": ["先看看"], "share": {"would_share": False, "to_whom": None, "what": None},
          "quote": "先看看再说吧"}


class Handler(BaseHTTPRequestHandler):
    """本机上的假服务：像老模型那样，在 JSON 前面带一段推理、外面再包一层代码块。"""
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.seen.append((self.path, self.headers["Authorization"], body))
        content = "<think>他会在意 {价格}</think>\n好的，以下是回答：\n```json\n" + json.dumps(ANSWER, ensure_ascii=False) + "\n```"
        data = json.dumps({"choices": [{"message": {"content": content}}],
                           "usage": {"prompt_tokens": 900, "completion_tokens": 150}}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    Handler.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1"
    httpd.shutdown()


def test_panel_runs_over_real_http_then_replays_without_network(tmp_path, server):
    b = OpenAICompatBackend(model_id="old-cheap-model", base_url=server, api_key=KEY, params={"temperature": 0.9})
    g = Gateway(b, tmp_path / "recording.jsonl", params=b.params)
    summary = run_panel(EX / "experiment.policy.yaml", EX / "personas", tmp_path, g)
    assert summary["model_id"] == "old-cheap-model" and summary["invalid"] == 0
    assert g.live_calls == summary["exposed"] == len(Handler.seen) > 0  # 一次就合格，没有重试
    assert g.usage == {"input_tokens": 900 * g.live_calls, "output_tokens": 150 * g.live_calls}
    path, auth, body = Handler.seen[0]
    assert path == "/v1/chat/completions" and auth == f"Bearer {KEY}" and "你现在是" in body["messages"][0]["content"]
    assert KEY not in (tmp_path / "recording.jsonl").read_text(encoding="utf-8")
    before = (tmp_path / "responses.jsonl").read_bytes()

    dead = OpenAICompatBackend(model_id="old-cheap-model", base_url="http://127.0.0.1:9/v1", api_key="", params={"temperature": 0.9})
    replay = Gateway(dead, tmp_path / "recording.jsonl", params=dead.params, replay=True)
    run_panel(EX / "experiment.policy.yaml", EX / "personas", tmp_path, replay)
    assert replay.live_calls == 0 and (tmp_path / "responses.jsonl").read_bytes() == before


def test_unreachable_server_is_a_backend_error():
    b = OpenAICompatBackend(model_id="m", base_url="http://127.0.0.1:9/v1", api_key=KEY, max_retries=1, sleep=lambda s: None, timeout=2)
    with pytest.raises(BackendError, match="连接失败"):
        b.complete("s", "u", seed=1)


def test_cli_refuses_to_start_without_model_config(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MIROSIM_API_KEY", KEY)
    code = main(["run-panel", "--experiment", str(EX / "experiment.policy.yaml"), "--personas", str(EX / "personas"),
                 "--out", str(tmp_path), "--backend", "openai"])
    assert code == 2 and "还没有填" in capsys.readouterr().err and not (tmp_path / "recording.jsonl").exists()


def test_cli_end_to_end_with_own_model_config(tmp_path, capsys, monkeypatch, server):
    cfg = yaml.safe_load((ROOT / "config" / "model_tiers.yaml").read_text(encoding="utf-8"))
    cfg["tiers"]["tier-economy"]["primary"].update(model_id="old-cheap-model", base_url=server)
    models = tmp_path / "models.yaml"
    models.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    run = ["run-panel", "--experiment", str(EX / "experiment.policy.yaml"), "--personas", str(EX / "personas"),
           "--out", str(tmp_path / "run"), "--backend", "openai", "--models", str(models)]
    monkeypatch.delenv("MIROSIM_API_KEY", raising=False)
    assert main(run) == 2 and "MIROSIM_API_KEY" in capsys.readouterr().err
    monkeypatch.setenv("MIROSIM_API_KEY", KEY)
    assert main(run) == 0
    out = capsys.readouterr()
    result = json.loads(out.out)
    assert result["model_id"] == "old-cheap-model" and result["live_calls"] > 0 and "mock" not in out.err
    assert result["usage"]["output_tokens"] == 150 * result["live_calls"]
    assert Handler.seen[0][2]["temperature"] == 0.9 and "response_format" not in Handler.seen[0][2]
    monkeypatch.delenv("MIROSIM_API_KEY")  # 回放不需要密钥，也不发调用
    n = len(Handler.seen)
    assert main(run + ["--replay"]) == 0 and len(Handler.seen) == n
    assert json.loads(capsys.readouterr().out)["live_calls"] == 0
    assert main(["contrast", "--run", str(tmp_path / "run"), "--experiment", str(EX / "experiment.policy.yaml"),
                 "--measure", "pol_fair"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["model_id"] == "old-cheap-model" and "mock" not in out.err


def test_cli_reports_where_it_stopped_when_the_service_fails(tmp_path, capsys, monkeypatch):
    cfg = yaml.safe_load((ROOT / "config" / "model_tiers.yaml").read_text(encoding="utf-8"))
    cfg["tiers"]["tier-economy"]["primary"].update(model_id="m", base_url="http://127.0.0.1:9/v1")
    models = tmp_path / "models.yaml"
    models.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("MIROSIM_API_KEY", KEY)
    monkeypatch.setattr("mirosim.core.openai_backend.time.sleep", lambda s: None)
    code = main(["run-panel", "--experiment", str(EX / "experiment.policy.yaml"), "--personas", str(EX / "personas"),
                 "--out", str(tmp_path / "run"), "--backend", "openai", "--models", str(models)])
    err = capsys.readouterr().err
    assert code == 1 and "中断" in err and "已完成的 0 次调用" in err and KEY not in err


def test_redirects_are_refused_so_the_key_never_reaches_another_origin(server):
    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(302)
            self.send_header("Location", server + "/chat/completions")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        b = OpenAICompatBackend(model_id="m", base_url=f"http://127.0.0.1:{httpd.server_port}/v1", api_key=KEY)
        with pytest.raises(BackendError, match="302"):
            b.complete("s", "u", seed=1)
    finally:
        httpd.shutdown()
    assert Handler.seen == []


def test_plain_http_is_only_allowed_for_loopback_or_by_explicit_opt_in():
    for url in ("http://api.example.com/v1", "http://192.168.1.20:8000/v1", "ftp://x/v1", "api.example.com/v1"):
        with pytest.raises(ValueError, match="https"):
            OpenAICompatBackend(model_id="m", base_url=url, api_key=KEY)
    for url in ("https://api.example.com/v1", "http://localhost:11434/v1", "http://127.0.0.1:8000/v1", "http://[::1]:8000/v1"):
        OpenAICompatBackend(model_id="m", base_url=url, api_key=KEY)
    OpenAICompatBackend(model_id="m", base_url="http://192.168.1.20:8000/v1", api_key=KEY, allow_http=True)
    cfg = yaml.safe_load((ROOT / "config" / "model_tiers.yaml").read_text(encoding="utf-8"))
    cfg["tiers"]["tier-economy"]["primary"].update(model_id="m", base_url="http://192.168.1.20:8000/v1")
    with pytest.raises(ValueError, match="allow_http"):
        from_tier(cfg, "tier-economy", env={"MIROSIM_API_KEY": KEY})
    cfg["tiers"]["tier-economy"]["primary"]["allow_http"] = True
    assert from_tier(cfg, "tier-economy", env={"MIROSIM_API_KEY": KEY}).url == "http://192.168.1.20:8000/v1/chat/completions"
