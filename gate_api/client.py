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
import random
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


class GatePublicClient:
    """Gate 公共行情客户端。

    公共行情不应依赖 API Key，也不能在策略模块中散落 ``requests.get``。
    该客户端统一使用项目的代理开关、连接复用、有限重试和指数退避；只用于
    幂等的读取请求，绝不用于下单等写操作。
    """

    _RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 20.0,
        use_proxy: Optional[bool] = None,
        retries: int = 3,
        retry_backoff: float = 0.4,
    ):
        if retries < 0:
            raise ValueError("retries 不能为负数")
        if timeout <= 0 or retry_backoff < 0:
            raise ValueError("timeout 必须为正且 retry_backoff 不能为负")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.retry_backoff = retry_backoff
        if use_proxy is None:
            use_proxy = os.environ.get("GATE_USE_PROXY", "").lower() in ("1", "true", "yes")
        self._use_proxy = use_proxy
        self.session = self._new_session()

    def _new_session(self) -> requests.Session:
        session = requests.Session()
        # 与 GateClient 保持一致：默认直连，显式 GATE_USE_PROXY=true 才读取系统代理。
        session.trust_env = self._use_proxy
        return session

    def _reset_session(self) -> None:
        self.session.close()
        self.session = self._new_session()

    def _delay(self, attempt: int) -> float:
        # 给同一时刻失败的多个引擎加入少量抖动，避免同时重试形成尖峰。
        return min(self.retry_backoff * (2 ** attempt), 4.0) + random.uniform(0, 0.15)

    def request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
    ) -> Any:
        """请求公开 API，网络错误、429 与暂态 5xx 会有限重试。"""
        method = method.upper()
        if method != "GET":
            raise ValueError("GatePublicClient 只允许 GET 请求")
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{self.base_url}{API_PREFIX}{path}"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self.session.request(
                    method, url, params=clean_params,
                    headers={"Accept": "application/json"}, timeout=self.timeout,
                )
                if response.status_code not in self._RETRYABLE_STATUS:
                    return GateClient._handle_response(response)
                last_error = GateApiError(
                    response.status_code, "RETRYABLE", response.text[:300],
                )
                response.close()
            except requests.exceptions.RequestException as error:
                last_error = error
                self._reset_session()
            if attempt < self.retries:
                time.sleep(self._delay(attempt))
        assert last_error is not None
        raise last_error

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        return self.request("GET", path, params=params)

    def close(self) -> None:
        self.session.close()


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
        self._use_proxy = use_proxy
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

        try:
            resp = self.session.request(
                method.upper(),
                url,
                data=body_str if body_str else None,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException:
            # 网络层失败（断连/超时等）：GET 立即换连接重试一次。
            # POST/PUT/DELETE 不重试——订单类请求重试可能造成重复提交。
            if method.upper() != "GET":
                raise
            self.session.close()
            self.session = requests.Session()
            self.session.trust_env = self._use_proxy
            resp = self.session.request(
                "GET", url, headers=headers, timeout=self.timeout,
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
