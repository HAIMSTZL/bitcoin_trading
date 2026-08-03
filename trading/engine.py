"""交易引擎：行情轮询 -> 网格策略 -> 撮合/执行 -> 持久化。

- paper 模式：虚拟账户本地撮合，不发生任何真实交易；
- live  模式：策略信号转为真实市价单（需 TRADING_MODE=live 且
  LIVE_TRADING_CONFIRM=YES_I_ACCEPT_RISK 双重确认），并强制执行
  币对白名单与单笔金额上限。

注意：live 执行路径未经真实下单测试（遵守“禁止买卖测试”约束），
启用前请自行小额验证。
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Any, Optional

from gate_api import GateClient, SpotAPI

from . import config
from .grid import GridBot, PaperAccount
from .store import Store

log = logging.getLogger("trading.engine")


class LiveExecutor:
    """实盘执行器：把网格成交信号转换为真实市价单。

    安全约束：
    - 只允许 config.PAIRS 白名单内的现货交易对；
    - 单笔 quote 金额不超过 config.MAX_ORDER_QUOTE；
    - 下单金额按交易对规则精度取整，低于最小限额则跳过。
    """

    def __init__(self, spot: SpotAPI):
        self._spot = spot
        self._rules = {p: spot.get_currency_pair(p) for p in config.PAIRS}

    def execute(self, fill: dict) -> None:
        pair = fill["pair"]
        if pair not in config.PAIRS:
            raise RuntimeError(f"拒绝交易非白名单币对: {pair}")
        rules = self._rules[pair]
        if rules.get("trade_status") != "tradable":
            log.warning("%s 当前不可交易，跳过", pair)
            return

        if fill["side"] == "buy":
            quote = min(fill["quote"], config.MAX_ORDER_QUOTE)
            min_quote = float(rules.get("min_quote_amount") or 0)
            if quote < min_quote:
                log.warning("买单金额 %.4f 低于最小限额 %.4f，跳过", quote, min_quote)
                return
            # 市价买单 amount 以计价币（USDT）为单位
            body = {
                "currency_pair": pair, "side": "buy", "type": "market",
                "amount": f"{quote:.2f}", "time_in_force": "ioc",
            }
        else:
            amount = fill["amount"]
            min_base = float(rules.get("min_base_amount") or 0)
            if amount < min_base:
                log.warning("卖单数量 %.8f 低于最小限额 %.8f，跳过", amount, min_base)
                return
            prec = int(rules.get("amount_precision") or 8)
            body = {
                "currency_pair": pair, "side": "sell", "type": "market",
                "amount": f"{amount:.{prec}f}", "time_in_force": "ioc",
            }

        log.info("实盘下单: %s", body)
        self._spot._c.request("POST", "/spot/orders", body=body)  # noqa: SLF001


class Engine:
    def __init__(self) -> None:
        if config.TRADING_MODE == "live" and config.LIVE_CONFIRM != "YES_I_ACCEPT_RISK":
            raise RuntimeError(
                "实盘模式需要双重确认：TRADING_MODE=live 且 "
                "LIVE_TRADING_CONFIRM=YES_I_ACCEPT_RISK"
            )
        self.mode = config.TRADING_MODE
        self.client = GateClient()
        self.spot = SpotAPI(self.client)
        self.store = Store()
        self.account = PaperAccount()
        self.bots: dict[str, GridBot] = {}
        self.prices: dict[str, float] = {}
        self.executor: Optional[LiveExecutor] = None
        self.started_at = time.time()
        self.last_tick: Optional[float] = None
        self.last_error: Optional[str] = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._stopped = False
        self._thread: Optional[threading.Thread] = None
        self._snapshot_counter = 0

        self._init_bots()
        if self.mode == "live":
            self.executor = LiveExecutor(self.spot)
        # 默认待命：服务启动后只初始化环境，等用户在 Web 面板点击"开始"才正式交易
        self._paused.set()

    # ------------------------------------------------------------------
    def _fetch_prices(self) -> dict[str, float]:
        tickers = self.spot.list_tickers()
        return {
            t["currency_pair"]: float(t["last"])
            for t in tickers
            if t["currency_pair"] in config.PAIRS
        }

    def _real_spot_balances(self) -> dict[str, float]:
        return {a["currency"]: float(a["available"]) for a in self.spot.list_accounts()}

    def _init_bots(self) -> None:
        """初始化环境。

        - 模拟盘：优先恢复上次落盘的虚拟账户与网格状态，继续交易；
          无存档时按 PAPER_MIRROR_REAL 镜像真实现货账户（或虚拟预算）新建。
        - 实盘：每次启动重新读取真实账户状态建仓。
        """
        self.prices = self._fetch_prices()

        if self.mode == "paper":
            saved = self.store.load_bot_states()
            if all(p in saved for p in config.PAIRS):
                for pair in config.PAIRS:
                    bot = GridBot.from_dict(saved[pair], self.account)
                    bot.on_order = self._on_order_placed
                    self.bots[pair] = bot
                log.info(
                    "已恢复上次模拟盘状态: %s",
                    {p: {"quote": round(self.account.get(p)["quote"], 4),
                         "base": round(self.account.get(p)["base"], 4),
                         "orders": len(self.bots[p].orders)} for p in config.PAIRS},
                )
                self._event("INFO", "lifecycle", "恢复上次模拟盘存档继续交易",
                            detail={p: {"quote": self.account.get(p)["quote"],
                                        "base": self.account.get(p)["base"],
                                        "orders": len(self.bots[p].orders)}
                                    for p in config.PAIRS})
                return
            log.info("未找到模拟盘存档，按初始仓位新建网格")
            self._event("INFO", "lifecycle", "未找到模拟盘存档，按初始仓位新建网格")

        budgets = self._initial_budgets()
        for pair in config.PAIRS:
            cfg = config.GRID_CONFIG[pair]
            price = self.prices.get(pair)
            if not price:
                raise RuntimeError(f"未获取到 {pair} 行情")

            quote_budget, base_budget = budgets[pair]
            self.account.init_pair(pair, quote_budget, base_budget)
            lower = price * (1 - cfg["range_pct"])
            upper = price * (1 + cfg["range_pct"])
            bot = GridBot(pair, lower, upper, cfg["grids"], quote_budget, base_budget)
            bot.on_order = self._on_order_placed  # 先挂监听器，捕获初始挂单
            bot.start(price, self.account)
            self.bots[pair] = bot
            log.info(
                "网格已启动 %s: 区间 %.6g ~ %.6g, %d 档, 挂单 %d 个",
                pair, lower, upper, cfg["grids"], len(bot.orders),
            )
            self._event(
                "INFO", "grid_init",
                f"新建网格: 区间 {lower:.6g} ~ {upper:.6g}, {cfg['grids']} 档, "
                f"挂单 {len(bot.orders)} 个, USDT={quote_budget:.4f}, 基础币={base_budget:.4f}",
                pair=pair,
                detail={"lower": lower, "upper": upper, "grids": cfg["grids"],
                        "quote_budget": quote_budget, "base_budget": base_budget,
                        "start_price": price},
            )

    def _initial_budgets(self) -> dict[str, tuple[float, float]]:
        if config.PAPER_MIRROR_REAL:
            # 模拟盘仓位完全镜像真实现货账户：各基础币按实际可用余额，
            # USDT 按各币对持仓价值比例分配（无持仓则均分）。
            real = self._real_spot_balances()
            usdt = real.get("USDT", 0.0)
            bases = {p: real.get(p.split("_")[0], 0.0) for p in config.PAIRS}
            total_base_val = sum(
                bases[p] * self.prices.get(p, 0.0) for p in config.PAIRS
            )
            budgets = {}
            for pair in config.PAIRS:
                if total_base_val > 0:
                    weight = bases[pair] * self.prices[pair] / total_base_val
                else:
                    weight = 1.0 / len(config.PAIRS)
                budgets[pair] = (usdt * weight, bases[pair])
            log.info("镜像真实账户: USDT=%s, 持仓=%s", usdt, bases)
            return budgets
        return {
            p: (config.GRID_CONFIG[p]["quote_budget"],
                float(config.GRID_CONFIG[p]["base_budget"]))
            for p in config.PAIRS
        }

    def _save_bot_states(self) -> None:
        if self.mode != "paper":
            return
        for pair, bot in self.bots.items():
            self.store.save_bot_state(pair, bot.to_dict(self.account))

    # ------------------------------------------------------------------
    # 事件日志
    # ------------------------------------------------------------------
    def _event(
        self,
        level: str,
        type: str,
        message: str,
        pair: str | None = None,
        detail: dict | None = None,
    ) -> None:
        try:
            self.store.record_event(level, type, message, pair, detail)
        except Exception:
            log.exception("事件落库失败: %s %s", type, message)

    def _on_order_placed(self, order: dict) -> None:
        side = "买入" if order["side"] == "buy" else "卖出"
        self._event(
            "INFO", "order_placed",
            f"挂{side}单 @ {order['price']:.8g} 数量 {order['base_amount']:.8g}",
            pair=order["pair"], detail=order,
        )

    # ------------------------------------------------------------------
    def _record_fill(self, fill: dict) -> None:
        self.store.record_trade(
            self.mode, fill["pair"], fill["side"], fill["price"],
            fill["amount"], fill["quote"], fill["profit"],
        )
        side = "买入" if fill["side"] == "buy" else "卖出"
        self._event(
            "INFO", "order_filled",
            f"{side}成交 @ {fill['price']:.8g} 数量 {fill['amount']:.8g} "
            f"金额 {fill['quote']:.4f} USDT 利润 {fill['profit']:.4f}",
            pair=fill["pair"], detail=fill,
        )
        if self.mode == "live" and self.executor:
            try:
                self.executor.execute(fill)
            except Exception as e:  # 实盘下单失败不中断引擎
                log.error("实盘下单失败: %s", e)
                self._event("ERROR", "live_order_error", f"实盘下单失败: {e}",
                            pair=fill["pair"], detail={"fill": fill})

    def tick(self) -> None:
        self.prices = self._fetch_prices()
        for pair, bot in self.bots.items():
            price = self.prices.get(pair)
            if price:
                bot.step(price, self.account, record=self._record_fill)
        self.last_tick = time.time()
        self._save_bot_states()  # 模拟盘每个 tick 落盘，保证重启后可续跑

        # 每 10 个 tick 落一次权益快照，控制数据库体积
        self._snapshot_counter += 1
        if self._snapshot_counter % 10 == 0:
            state = self.state()
            self.store.record_equity(
                state["total_equity"], state["total_realized_profit"],
                {p: s["equity"] for p, s in state["pairs"].items()},
            )

    # ------------------------------------------------------------------
    def run(self) -> None:
        log.info("引擎启动，模式=%s，轮询间隔=%ss", self.mode, config.TICK_INTERVAL)
        self._event("INFO", "lifecycle", f"引擎线程启动, 模式={self.mode}")
        while not self._stop.is_set():
            if self._paused.is_set():
                self._stop.wait(config.TICK_INTERVAL)
                continue
            try:
                self.tick()
                self.last_error = None
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("tick 失败")
                self._event("ERROR", "tick_error", self.last_error,
                            detail={"traceback": traceback.format_exc()})
            self._stop.wait(config.TICK_INTERVAL)

    def start_background(self) -> None:
        self._thread = threading.Thread(target=self.run, daemon=True, name="trading-engine")
        self._thread.start()

    # ------------------------------------------------------------------
    # 运行控制（供 Web 面板调用）
    # ------------------------------------------------------------------
    def pause(self) -> str:
        if self._stopped:
            return "stopped"
        self._paused.set()
        log.info("引擎已暂停")
        self._event("INFO", "control", "用户暂停交易引擎")
        return "paused"

    def resume(self) -> str:
        if self._stopped:
            return "stopped"
        self._paused.clear()
        log.info("引擎已恢复运行")
        self._event("INFO", "control", "用户恢复交易引擎")
        return "running"

    def shutdown(self) -> str:
        """停止交易循环（不关闭存储，Web 服务退出时由 stop() 统一收尾）。"""
        self._save_bot_states()  # 停前落盘，保证模拟盘可无损续跑
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._stopped = True
        log.info("引擎已停止")
        self._event("INFO", "control", "用户停止交易引擎")
        return "stopped"

    def start(self) -> str:
        """开始/恢复：暂停时恢复；停止后重新启动交易循环（沿用现有网格状态）。"""
        if self._stopped:
            self._stop.clear()
            self._paused.clear()
            self._stopped = False
            self.start_background()
            log.info("引擎已重新启动")
            self._event("INFO", "control", "用户重新启动交易引擎")
            return "running"
        return self.resume()

    @property
    def run_status(self) -> str:
        if self._stopped:
            return "stopped"
        if self._paused.is_set():
            return "paused"
        return "running"

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self.store.close()

    # ------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        pairs = {
            pair: bot.state(self.prices.get(pair, bot.start_price or 0), self.account)
            for pair, bot in self.bots.items()
        }
        total_equity = sum(s["equity"] for s in pairs.values())
        total_initial = sum(s["initial_equity"] for s in pairs.values())
        return {
            "mode": self.mode,
            "run_status": self.run_status,
            "started_at": self.started_at,
            "last_tick": self.last_tick,
            "last_error": self.last_error,
            "total_equity": total_equity,
            "total_initial_equity": total_initial,
            "total_pnl": total_equity - total_initial,
            "total_realized_profit": sum(s["realized_profit"] for s in pairs.values()),
            "pairs": pairs,
            "recent_trades": self.store.recent_trades(50),
            "recent_events": self.store.recent_events(50),
            "equity_history": self.store.equity_history(300),
        }
