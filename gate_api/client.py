"""Gate API v4 签名客户端。

- 密钥只从环境变量读取：MY_GATE_KEY / MY_GATE_SECRET，严禁硬编码。
- 签名规则（官方文档）：
    sign_payload = METHOD + "\\n" + url_path + "\\n" + query_string + "\\n" + sha512_hex(body) + "\\n" + timestamp
    SIGN = HMAC-SHA512(secret, sign_payload)
- 仅封装 GET 只读接口的调用在测试中使用；client 本身保留通用 request 能力。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Optional
from urllib.parse import urlencode

import requests

DEFAULT_BASE_URL = "https://api.gateio.ws"
API_PREFIX = "/api/v4"

ENV_KEY = "MY_GATE_KEY"
ENV_SECRET = "MY_GATE_SECRET"


class GateApiError(Exception):
    """Gate API 返回的业务错误。"""

    def __init__(self, status_code: int, label: str, message: str):
        self.status_code = status_code
        self.label = label
        super().__init__(f"HTTP {status_code} [{label}] {message}")


class GateClient:
    """Gate API v4 HTTP 客户端（自动签名）。"""

    def __init__(
        self,
        key: Optional[str] = None,
        secret: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
        use_proxy: Optional[bool] = None,
    ):
        self.key = key or os.environ.get(ENV_KEY)
        self.secret = secret or os.environ.get(ENV_SECRET)
        if not self.key or not self.secret:
            raise RuntimeError(
                f"未找到 API 密钥，请设置环境变量 {ENV_KEY} 和 {ENV_SECRET}"
            )
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        # 是否使用系统代理：默认直连（GATE_USE_PROXY=false/0 也视为直连）。
        # 直连不可达的网络环境可设 GATE_USE_PROXY=true 走系统代理。
        if use_proxy is None:
            use_proxy = os.environ.get("GATE_USE_PROXY", "").lower() in ("1", "true", "yes")
        self.session.trust_env = use_proxy

    # ------------------------------------------------------------------
    # 签名
    # ------------------------------------------------------------------
    def _sign(self, method: str, path: str, query_string: str, body: str) -> dict:
        timestamp = str(time.time())
        payload_hash = hashlib.sha512(body.encode("utf-8")).hexdigest()
        sign_payload = "\n".join(
            [method.upper(), path, query_string, payload_hash, timestamp]
        )
        sign = hmac.new(
            self.secret.encode("utf-8"),
            sign_payload.encode("utf-8"),
            hashlib.sha512,
        ).hexdigest()
        return {
            "KEY": self.key,
            "Timestamp": timestamp,
            "SIGN": sign,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # 请求
    # ------------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: Optional[Any] = None,
    ) -> Any:
        """发起签名请求。

        :param method: HTTP 方法，如 GET/POST
        :param path: 接口路径，如 /spot/accounts（不含 /api/v4 前缀）
        :param params: query 参数
        :param body: JSON 请求体（dict/list），GET 时为 None
        """
        # 过滤掉 None 参数，避免把 "None" 拼进签名串
        clean_params = {
            k: v for k, v in (params or {}).items() if v is not None
        }
        query_string = urlencode(clean_params, doseq=True)
        body_str = json.dumps(body) if body is not None else ""

        full_path = f"{API_PREFIX}{path}"
        headers = self._sign(method, full_path, query_string, body_str)

        url = f"{self.base_url}{full_path}"
        if query_string:
            url = f"{url}?{query_string}"

        resp = self.session.request(
            method.upper(),
            url,
            data=body_str if body_str else None,
            headers=headers,
            timeout=self.timeout,
        )
        return self._handle_response(resp)

    @staticmethod
    def _handle_response(resp: requests.Response) -> Any:
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            return resp.text

        if 200 <= resp.status_code < 300:
            return data

        label = data.get("label", "UNKNOWN") if isinstance(data, dict) else "UNKNOWN"
        message = (
            data.get("message", resp.text) if isinstance(data, dict) else resp.text
        )
        raise GateApiError(resp.status_code, label, message)

    # 便捷方法
    def get(self, path: str, params: Optional[dict] = None) -> Any:
        return self.request("GET", path, params=params)
