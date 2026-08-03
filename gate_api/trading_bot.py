"""交易机器人（Bot / AIHub）只读接口封装。

Gate API v4 的 Bot 标签提供策略推荐、运行中机器人查询与详情查询。
策略类型 strategy_type: spot_grid / margin_grid / infinite_grid /
futures_grid / spot_martingale / contract_martingale
"""

from __future__ import annotations

from typing import Any, Optional

from .client import GateClient


class TradingBotAPI:
    def __init__(self, client: GateClient):
        self._c = client

    def list_strategy_recommend(
        self,
        market: Optional[str] = None,
        strategy_type: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        """查询 AI 推荐的机器人策略。"""
        return self._c.get(
            "/bot/strategy/recommend",
            {"market": market, "strategy_type": strategy_type, "limit": limit},
        )

    def list_running_bots(
        self,
        strategy_type: Optional[str] = None,
        market: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Any:
        """查询正在运行的机器人（组合）列表。"""
        return self._c.get(
            "/bot/portfolio/running",
            {
                "strategy_type": strategy_type,
                "market": market,
                "page": page,
                "page_size": page_size,
            },
        )

    def get_bot_detail(self, strategy_id: str, strategy_type: str) -> Any:
        """查询单个机器人详情。"""
        return self._c.get(
            "/bot/portfolio/detail",
            {"strategy_id": strategy_id, "strategy_type": strategy_type},
        )
