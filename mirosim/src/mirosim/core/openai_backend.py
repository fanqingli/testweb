"""OpenAI 兼容接口（/chat/completions）的后端。只用标准库，不依赖任何厂商的 SDK。

按老旧、低价模型来设计：默认不要求 JSON 模式，也不传 seed——格式靠提示词和 engines/panel.py 的
校验与重试来保证。两项功能只有在 config/model_tiers.yaml 里写明支持时才启用。
"""
from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}
LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class BackendError(RuntimeError):
    """模型服务拒绝了请求，或重试后仍然失败。"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向：标准库会把 Authorization 头原样带到重定向的目标，密钥可能被送到别的站点。"""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _http_post(url: str, headers: dict, body: bytes, timeout: float) -> tuple[int, dict, str]:
    """返回 (状态码, 响应头, 正文)；连不上或超时时状态码为 0。"""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read().decode("utf-8", "replace")
    except (OSError, http.client.HTTPException) as e:  # 连不上、超时、连接中途断开
        return 0, {}, str(e)


class OpenAICompatBackend:
    def __init__(self, *, model_id: str, base_url: str, api_key: str, params: dict | None = None,
                 supports_json_mode: bool = False, supports_seed: bool = False, timeout: float = 60,
                 max_retries: int = 3, allow_http: bool = False, provider: str = "", transport=_http_post, sleep=None):
        if not model_id or not base_url:
            raise ValueError("model_id 与 base_url 不能为空")
        address = urllib.parse.urlsplit(base_url)
        if address.username or address.password:  # 地址会进 call_key 的参数并写入录制
            raise ValueError("base_url 里不要带用户名或密码，密钥只放在环境变量里")
        if address.scheme != "https" and not (address.scheme == "http" and (allow_http or address.hostname in LOOPBACK)):
            raise ValueError(f"base_url 必须是 https：{base_url}。明文 http 会让密钥和提示词在网络上裸奔，"
                             "只对本机地址放行；内网自建的服务请在模型配置里写 allow_http: true")
        self.model_id, self.url, self._api_key = model_id, base_url.rstrip("/") + "/chat/completions", api_key
        self.params = dict(params or {})
        self.supports_json_mode, self.supports_seed = supports_json_mode, supports_seed
        self.timeout, self.max_retries, self._transport, self._sleep = timeout, max_retries, transport, sleep or time.sleep
        self.provider = provider or address.hostname or ""  # 写进运行清单
        self.last_usage: dict | None = None

    @property
    def key_params(self) -> dict:
        """进 call_key 的内容：凡是会改变请求的配置都在里面，改了其中任何一项都不沿用旧答案。"""
        return {**self.params, "url": self.url, "json_mode": self.supports_json_mode, "seed_sent": self.supports_seed}

    def complete(self, system: str, user: str, *, seed: int, spec: dict | None = None) -> str:
        body = {"model": self.model_id, **self.params,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if self.supports_json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.supports_seed:
            body["seed"] = seed
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.last_usage = None
        for attempt in range(self.max_retries + 1):
            status, response_headers, text = self._transport(self.url, headers, data, self.timeout)
            if status == 200:
                return self._content(text)
            if status not in RETRY_STATUS and status != 0:
                raise BackendError(f"{self.model_id} 返回 {status}：{text[:300]}")
            if attempt < self.max_retries:
                self._sleep(_retry_after(response_headers, attempt))
        raise BackendError(f"{self.model_id} 重试 {self.max_retries} 次后仍失败（{status or '连接失败'}）：{text[:300]}")

    def _content(self, text: str) -> str:
        try:
            reply = json.loads(text)
            content = reply["choices"][0]["message"].get("content")
            usage = dict(reply.get("usage") or {})
        except (ValueError, LookupError, TypeError, AttributeError) as e:
            raise BackendError(f"{self.model_id} 的响应不是 chat/completions 格式：{text[:300]}") from e
        self.last_usage = {"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")}
        return content or ""  # 空回答交给上层按不合格处理


def _retry_after(headers: dict, attempt: int) -> float:
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    try:
        return min(float(value), 60.0)
    except (TypeError, ValueError):
        return min(2.0 ** attempt, 30.0)


def from_tier(models_cfg: dict, tier: str, *, temperature: float | None = None, env=os.environ,
              require_key: bool = True, **kwargs) -> OpenAICompatBackend:
    """按 config/model_tiers.yaml 里某个档位的 primary 建后端。密钥只从环境变量读，不进任何文件。
    回放不发调用，可以不要密钥（require_key=False）。"""
    if tier not in models_cfg["tiers"]:
        raise ValueError(f"模型配置里没有档位 {tier}")
    entry = models_cfg["tiers"][tier]
    primary = entry["primary"]
    if not primary.get("model_id") or not primary.get("base_url"):
        raise ValueError(f"模型配置里 {tier} 的 primary.model_id 和 primary.base_url 还没有填")
    key_env = primary.get("api_key_env") or "MIROSIM_API_KEY"
    if require_key and not env.get(key_env):
        raise ValueError(f"环境变量 {key_env} 没有设置（{tier} 的模型密钥）")
    flags = {name: primary.get(name) or False for name in ("supports_json_mode", "supports_seed", "allow_http")}
    for name, value in flags.items():
        if not isinstance(value, bool):  # 加了引号的 "false" 是非空字符串，按真假值判断会被当成 true
            raise ValueError(f"模型配置里 {tier} 的 primary.{name} 只能写 true 或 false（不加引号），现在是 {value!r}")
    tier_params = entry.get("params", {})
    params = {"temperature": tier_params.get("temperature") if temperature is None else temperature,
              "top_p": tier_params.get("top_p"), "max_tokens": tier_params.get("max_output_tokens")}
    return OpenAICompatBackend(
        model_id=primary["model_id"], base_url=primary["base_url"], api_key=env.get(key_env, ""),
        params={k: v for k, v in params.items() if v is not None},
        provider=primary.get("provider") or "", **flags, **kwargs)
