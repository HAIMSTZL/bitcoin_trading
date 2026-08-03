"""永续合约（Perpetual Futures）只读接口封装。"""

from __future__ import annotations

from typing import Any, Optional

from .client import GateClient


class FuturesAPI:
    """settle: 结算币种，如 usdt / btc。"""

    def __init__(self, client: GateClient, settle: str = "usdt"):
        self._c = client
        self.settle = settle.lower()

    def _p(self, suffix: str) -> str:
        return f"/futures/{self.settle}{suffix}"

    # ---- 公共 ----
    def list_contracts(self) -> Any:
        """查询全部合约信息。"""
        return self._c.get(self._p("/contracts"))

    def get_contract(self, contract: str) -> Any:
        """查询单个合约信息，如 BTC_USDT。"""
        return self._c.get(self._p(f"/contracts/{contract}"))

    def list_tickers(self, contract: Optional[str] = None) -> Any:
        """查询合约行情 ticker。"""
        return self._c.get(self._p("/tickers"), {"contract": contract})

    # ---- 私有（只读）----
    def get_account(self) -> Any:
        """查询合约账户资产。"""
        return self._c.get(self._p("/accounts"))

    def list_account_book(self, limit: int = 100) -> Any:
        """查询合约账户账单（流水）。"""
        return self._c.get(self._p("/account_book"), {"limit": limit})

    def list_positions(self, limit: int = 100) -> Any:
        """查询当前持仓列表。"""
        return self._c.get(self._p("/positions"), {"limit": limit})

    def get_position(self, contract: str) -> Any:
        """查询单个合约持仓。"""
        return self._c.get(self._p(f"/positions/{contract}"))

    def list_orders(
        self,
        status: str = "open",
        contract: Optional[str] = None,
        limit: int = 100,
    ) -> Any:
        """查询合约订单列表。status: open/finished。"""
        return self._c.get(
            self._p("/orders"),
            {"status": status, "contract": contract, "limit": limit},
        )

    def get_order(self, order_id: str) -> Any:
        """查询单个合约订单。"""
        return self._c.get(self._p(f"/orders/{order_id}"))

    def list_my_trades(
        self,
        contract: Optional[str] = None,
        limit: int = 100,
    ) -> Any:
        """查询合约个人成交历史。"""
        return self._c.get(
            self._p("/my_trades"), {"contract": contract, "limit": limit}
        )

    def list_position_close(
        self,
        contract: Optional[str] = None,
        limit: int = 100,
    ) -> Any:
        """查询平仓历史。"""
        return self._c.get(
            self._p("/position_close"), {"contract": contract, "limit": limit}
        )

    def list_liquidates(
        self,
        contract: Optional[str] = None,
        limit: int = 100,
    ) -> Any:
        """查询强平历史。"""
        return self._c.get(
            self._p("/liquidates"), {"contract": contract, "limit": limit}
        )

    def list_price_orders(
        self,
        status: str = "open",
        contract: Optional[str] = None,
        limit: int = 100,
    ) -> Any:
        """查询合约计划委托（止盈止损等）。status: open/finished。"""
        return self._c.get(
            self._p("/price_orders"),
            {"status": status, "contract": contract, "limit": limit},
        )
