"""子账户/托管（SubAccount）只读接口封装。

Gate API key 的"托管"权限对应子账户托管场景（母账户管理子账户）。
"""

from __future__ import annotations

from typing import Any, Optional

from .client import GateClient


class SubAccountAPI:
    def __init__(self, client: GateClient):
        self._c = client

    def list_sub_accounts(self) -> Any:
        """查询子账户列表。"""
        return self._c.get("/sub_accounts")

    def get_sub_account(self, user_id: int) -> Any:
        """查询单个子账户信息。"""
        return self._c.get(f"/sub_accounts/{user_id}")

    def list_sub_account_keys(self, user_id: int) -> Any:
        """查询子账户 API Key 列表。"""
        return self._c.get(f"/sub_accounts/{user_id}/keys")

    def list_sub_account_balances(
        self,
        sub_user_id: Optional[int] = None,
        limit: int = 100,
        page: int = 1,
    ) -> Any:
        """查询子账户余额（母账户视角，经由钱包接口）。"""
        return self._c.get(
            "/wallet/sub_account_balances",
            {"sub_user_id": sub_user_id, "limit": limit, "page": page},
        )

    def list_sub_account_transfers(self, limit: int = 100) -> Any:
        """查询母子账户划转记录。"""
        return self._c.get("/wallet/sub_account_transfers", {"limit": limit})
