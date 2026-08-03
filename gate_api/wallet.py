"""钱包（Wallet）只读接口封装。"""

from __future__ import annotations

from typing import Any, Optional

from .client import GateClient


class WalletAPI:
    def __init__(self, client: GateClient):
        self._c = client

    def get_total_balance(
        self,
        currency: Optional[str] = None,
    ) -> Any:
        """查询全账户总资产估值（默认折算 USDT）。"""
        return self._c.get("/wallet/total_balance", {"currency": currency})

    def get_deposit_address(self, currency: str) -> Any:
        """查询币种充值地址。"""
        return self._c.get("/wallet/deposit_address", {"currency": currency})

    def list_currency_chains(self, currency: str) -> Any:
        """查询币种支持的链列表。"""
        return self._c.get("/wallet/currency_chains", {"currency": currency})

    def list_deposits(self, limit: int = 100) -> Any:
        """查询充值记录。"""
        return self._c.get("/wallet/deposits", {"limit": limit})

    def list_withdrawals(self, limit: int = 100) -> Any:
        """查询提现记录。"""
        return self._c.get("/wallet/withdrawals", {"limit": limit})

    def get_withdraw_status(self, currency: Optional[str] = None) -> Any:
        """查询币种提现状态（是否可提现、手续费等）。"""
        return self._c.get("/wallet/withdraw_status", {"currency": currency})

    def list_sub_account_transfers(self, sub_uid: Optional[str] = None, limit: int = 100) -> Any:
        """查询母子账户划转记录（仅 2020-04-10 之后的记录）。"""
        return self._c.get(
            "/wallet/sub_account_transfers", {"sub_uid": sub_uid, "limit": limit}
        )

    def list_push(self, transaction_type: str = "withdraw", limit: int = 100) -> Any:
        """查询 UID 内转（站内转账）历史。transaction_type: withdraw/deposit。"""
        return self._c.get(
            "/wallet/push", {"transaction_type": transaction_type, "limit": limit}
        )

    def list_sub_account_balances(self, limit: int = 100, page: int = 1) -> Any:
        """查询子账户余额（母账户视角）。"""
        return self._c.get(
            "/wallet/sub_account_balances", {"limit": limit, "page": page}
        )

    def list_small_balance(self, currency: Optional[str] = None) -> Any:
        """查询可兑换的小额资产。"""
        return self._c.get("/wallet/small_balance", {"currency": currency})

    def list_small_balance_history(self, limit: int = 100) -> Any:
        """查询小额资产兑换历史。"""
        return self._c.get("/wallet/small_balance_history", {"limit": limit})

    def list_saved_address(
        self,
        currency: Optional[str] = None,
        limit: int = 100,
    ) -> Any:
        """查询提现地址簿（已保存地址）。"""
        return self._c.get(
            "/wallet/saved_address", {"currency": currency, "limit": limit}
        )

    def get_fee(self, currency: Optional[str] = None) -> Any:
        """查询个人现货交易费率。"""
        return self._c.get("/wallet/fee", {"currency": currency})
