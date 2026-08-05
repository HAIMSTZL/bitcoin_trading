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
from .indicators import IndicatorEngine, atr_percent
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
        self.client = GateClient(timeout=20.0)  # 引擎用更长超时，容忍网络抖动
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
        self._last_health = 0.0  # 首次 tick 即报一条健康状态，之后按间隔
        self.indicators = IndicatorEngine(
            self.spot, interval=config.INDICATOR_KLINE,
            ob_th=config.DEPTH_RATIO_THRESHOLD, tr_th=config.TRADE_RATIO_THRESHOLD,
        )
        self._last_indicator = 0.0
        self._last_signals: dict[str, int] = {}  # 各币对上一次【已确认】信号
        self._signal_buf: dict[str, list[int]] = {p: [] for p in config.PAIRS}  # 确认缓冲
        self._last_flip: dict[str, float] = {}  # 各币对上次信号翻转时间（冷却用）
        self._regimes: dict[str, str] = {}  # 各币对行情状态 ranging/trend_up/trend_down
        self._last_rebalance = time.time()  # 启动后过完整周期才首次再平衡
        self._init_atr: dict[str, float] = {}  # 建仓时的 ATR%（自适应区间兜底）
        # 熔断状态
        self._price_hist: dict[str, list[tuple[float, float]]] = {p: [] for p in config.PAIRS}
        self._cb_pairs: dict[str, float] = {}   # 被熔断的币对 -> 熔断时刻
        self._cb_low: dict[str, list[float]] = {}  # 币对 -> [最低价, 最近创新低时间]
        self._cb_global = False  # 大盘熔断（BTC 触发，全系统停止撮合）

        self._init_bots()
        if self.mode == "live":
            self.executor = LiveExecutor(self.spot)
        # 默认待命：服务启动后只初始化环境，等用户在 Web 面板点击"开始"才正式交易
        self._paused.set()

    # ------------------------------------------------------------------
    def _fetch_prices(self) -> dict[str, float]:
        """按币对分别查询 ticker（响应小、抗超时；全市场单次拉取响应数 MB 易超时）。"""
        prices: dict[str, float] = {}
        for pair in config.PAIRS:
            tickers = self.spot.list_tickers(pair)
            if tickers:
                prices[pair] = float(tickers[0]["last"])
        return prices

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
                sig_now = self._config_sig()
                if all(saved[p].get("config_sig") == sig_now[p] for p in config.PAIRS):
                    for pair in config.PAIRS:
                        bot = GridBot.from_dict(saved[pair], self.account)
                        bot.on_order = self._on_order_placed
                        bot.fee_rate = config.PAPER_FEE_RATE
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
                # 网格参数已变更：保留存档中的余额与利润，按新参数重建网格
                log.info("网格参数已变更，按新参数重建（保留余额与利润）")
                self._event("INFO", "lifecycle", "网格参数变更，按新参数重建网格（保留余额与利润）")
                for pair in config.PAIRS:
                    d = saved[pair]
                    self.account.init_pair(pair, d["quote"], d["base"])
                    bot = self._build_bot(pair, self.prices[pair],
                                          d["quote"], d["base"])
                    bot.realized_profit = d.get("realized_profit", 0.0)
                    bot.trade_count = d.get("trade_count", 0)
                    bot.blocked_count = d.get("blocked_count", 0)
                    bot.total_fees = d.get("total_fees", 0.0)
                    self.bots[pair] = bot
                return
            log.info("未找到模拟盘存档，按初始仓位新建网格")
            self._event("INFO", "lifecycle", "未找到模拟盘存档，按初始仓位新建网格")

        budgets = self._initial_budgets()
        for pair in config.PAIRS:
            price = self.prices.get(pair)
            if not price:
                raise RuntimeError(f"未获取到 {pair} 行情")
            quote_budget, base_budget = budgets[pair]
            self.account.init_pair(pair, quote_budget, base_budget)
            self.bots[pair] = self._build_bot(pair, price, quote_budget, base_budget)

    @staticmethod
    def _config_sig() -> dict[str, str]:
        """各币对网格参数签名，用于检测配置变更后重建。"""
        mode = "dyn" if config.DYNAMIC_ALLOCATION else "eq"
        return {
            p: (f'{config.GRID_CONFIG[p]["range_pct"]}:{config.GRID_CONFIG[p]["grids"]}'
                f':{config.TOTAL_QUOTE_BUDGET}:{mode}')
            for p in config.PAIRS
        }

    def _desired_range_pct(self, pair: str) -> float:
        """当前应有的网格区间幅度：ADAPTIVE_RANGE 开启时随 ATR% 自适应。"""
        cfg = config.GRID_CONFIG[pair]
        if not config.ADAPTIVE_RANGE:
            return cfg["range_pct"]
        atr = self.indicators.get(pair).get("atr_pct", 0.0) or self._init_atr.get(pair, 0.0)
        if atr <= 0:
            return cfg["range_pct"]
        return min(max(config.ADAPTIVE_RANGE_MULT * atr / 100,
                       config.RANGE_PCT_MIN), config.RANGE_PCT_MAX)

    def _build_bot(
        self, pair: str, price: float, quote_budget: float, base_budget: float
    ) -> GridBot:
        cfg = config.GRID_CONFIG[pair]
        range_pct = self._desired_range_pct(pair)
        lower = price * (1 - range_pct)
        upper = price * (1 + range_pct)
        bot = GridBot(pair, lower, upper, cfg["grids"], quote_budget, base_budget,
                      fee_rate=config.PAPER_FEE_RATE)
        bot.on_order = self._on_order_placed  # 先挂监听器，捕获初始挂单
        bot.start(price, self.account)
        log.info(
            "网格已启动 %s: 区间 %.6g ~ %.6g (±%.2f%%), %d 档, 挂单 %d 个",
            pair, lower, upper, range_pct * 100, cfg["grids"], len(bot.orders),
        )
        self._event(
            "INFO", "grid_init",
            f"新建网格: 区间 {lower:.6g} ~ {upper:.6g} (±{range_pct*100:.2f}%), {cfg['grids']} 档, "
            f"挂单 {len(bot.orders)} 个, USDT={quote_budget:.4f}, 基础币={base_budget:.4f}",
            pair=pair,
            detail={"lower": lower, "upper": upper, "grids": cfg["grids"],
                    "range_pct": range_pct,
                    "quote_budget": quote_budget, "base_budget": base_budget,
                    "start_price": price},
        )
        return bot

    def _recenter(self, pair: str, price: float) -> None:
        """价格跑出网格区间：以当前价为中心重建网格，保留余额与利润累计。"""
        old = self.bots[pair]
        bal = self.account.get(pair)
        bot = self._build_bot(pair, price, bal["quote"], bal["base"])
        bot.realized_profit = old.realized_profit
        bot.trade_count = old.trade_count
        bot.blocked_count = old.blocked_count
        bot.total_fees = old.total_fees
        self.bots[pair] = bot
        log.warning("%s 价格 %.6g 跑出区间 [%.6g, %.6g]，网格已重建", pair, price, old.lower, old.upper)
        self._event(
            "WARNING", "grid_recenter",
            f"价格 {price:.6g} 跑出区间 [{old.lower:.6g}, {old.upper:.6g}]，以现价为中心重建网格",
            pair=pair,
            detail={"old_lower": old.lower, "old_upper": old.upper, "price": price},
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
            p: (self._allocate_quotes().get(p, 0.0),
                float(config.GRID_CONFIG[p]["base_budget"]))
            for p in config.PAIRS
        }

    def _allocate_quotes(self) -> dict[str, float]:
        """把 TOTAL_QUOTE_BUDGET 按近期波动率（ATR%）动态分配到各币对。

        网格策略赚的是波动的钱：ATR% 越高权重越大；
        ALLOC_MIN_W/MAX_W 限制单币对占比。失败时回退均分。
        """
        n = len(config.PAIRS)
        total = config.TOTAL_QUOTE_BUDGET
        if not config.DYNAMIC_ALLOCATION:
            return {p: total / n for p in config.PAIRS}
        try:
            vols = {}
            for pair in config.PAIRS:
                candles = self.spot.list_candlesticks(pair, config.INDICATOR_KLINE, 40)
                vols[pair] = atr_percent(candles)
            if sum(vols.values()) <= 0:
                raise ValueError("ATR 全为 0")
            self._init_atr = vols  # 供自适应区间在建仓时使用
            weights = {p: v / sum(vols.values()) for p, v in vols.items()}
            # 夹紧到 [MIN, MAX] 后按剩余比例再分配（两轮收敛即可）
            for _ in range(2):
                fixed = {p: w for p, w in weights.items()
                         if w <= config.ALLOC_MIN_W or w >= config.ALLOC_MAX_W}
                free = [p for p in weights if p not in fixed]
                fixed_total = sum(
                    min(max(w, config.ALLOC_MIN_W), config.ALLOC_MAX_W)
                    for w in fixed.values()
                )
                free_total = sum(weights[p] for p in free)
                weights = {
                    p: (min(max(weights[p], config.ALLOC_MIN_W), config.ALLOC_MAX_W)
                        if p in fixed else weights[p] / free_total * (1 - fixed_total))
                    for p in weights
                }
            alloc = {p: total * weights[p] for p in config.PAIRS}
            log.info("动态预算分配: %s (ATR%%: %s)",
                     {p: round(a, 2) for p, a in alloc.items()},
                     {p: round(v, 3) for p, v in vols.items()})
            self._event(
                "INFO", "lifecycle",
                "动态预算分配: " + ", ".join(
                    f"{p} {a:.2f}U (ATR {vols[p]:.2f}%, 权重 {weights[p]*100:.0f}%)"
                    for p, a in alloc.items()
                ),
                detail={"alloc": alloc, "atr_pct": vols, "weights": weights},
            )
            return alloc
        except Exception as e:
            log.warning("动态分配失败(%s)，回退均分", e)
            return {p: total / n for p in config.PAIRS}

    def _save_bot_states(self) -> None:
        if self.mode != "paper":
            return
        sig = self._config_sig()
        for pair, bot in self.bots.items():
            data = bot.to_dict(self.account)
            data["config_sig"] = sig[pair]
            self.store.save_bot_state(pair, data)

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
        self._check_circuit_breaker()  # 熔断检测永远运行（含熔断期间，用于企稳恢复）
        self._update_indicators()
        if not self._cb_global:
            self._maybe_rebalance()
        for pair, bot in self.bots.items():
            if self._cb_global or pair in self._cb_pairs:
                continue  # 熔断中：不撮合、不补单
            price = self.prices.get(pair)
            if not price:
                continue
            if config.AUTO_RECENTER and (price > bot.upper or price < bot.lower):
                self._recenter(pair, price)
            self.bots[pair].step(price, self.account, record=self._record_fill)
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
        self._maybe_health_check()

    # ------------------------------------------------------------------
    # 熔断机制
    # ------------------------------------------------------------------
    @staticmethod
    def _window_drop(hist: list[tuple[float, float]], now: float, window_sec: float) -> float:
        """窗口内从最高点的回撤百分比。数据不足返回 0。"""
        recent = [p for t, p in hist if t >= now - window_sec]
        if len(recent) < 2:
            return 0.0
        high = max(recent)
        return (high - recent[-1]) / high * 100 if high > 0 else 0.0

    def _check_circuit_breaker(self) -> None:
        if not config.CB_ENABLED:
            return
        now = time.time()
        window = config.CB_WINDOW_MIN * 60
        stable = config.CB_RESUME_STABLE_MIN * 60
        for pair, price in self.prices.items():
            hist = self._price_hist[pair]
            hist.append((now, price))
            cutoff = now - max(window, stable) - 60  # 保留到恢复判定所需长度
            while hist and hist[0][0] < cutoff:
                hist.pop(0)

            if pair in self._cb_pairs:
                # 熔断中：跟踪是否企稳（不再创新低）
                low = self._cb_low[pair]
                if price < low[0]:
                    low[0], low[1] = price, now
                if config.CB_AUTO_RESUME and now - low[1] >= stable:
                    del self._cb_pairs[pair]
                    del self._cb_low[pair]
                    log.warning("%s 熔断恢复：已 %d 分钟未创新低，现价 %.6g",
                                pair, config.CB_RESUME_STABLE_MIN, price)
                    self._event("INFO", "circuit_resume",
                                f"熔断恢复：{config.CB_RESUME_STABLE_MIN:.0f} 分钟未创新低，恢复交易",
                                pair=pair, detail={"price": price})
                    if pair == "BTC_USDT" and self._cb_global:
                        self._cb_global = False
                        log.warning("大盘熔断恢复，全系统恢复撮合")
                        self._event("INFO", "circuit_resume",
                                    "大盘熔断恢复（BTC 企稳），全系统恢复交易")
                continue

            drop = self._window_drop(hist, now, window)
            is_btc = pair == "BTC_USDT"
            threshold = config.CB_GLOBAL_BTC_PCT if is_btc else config.CB_DROP_PCT
            if drop >= threshold:
                self._cb_pairs[pair] = now
                self._cb_low[pair] = [price, now]
                log.warning("%s 触发熔断: %d 分钟内回撤 %.2f%% (阈值 %.1f%%), 现价 %.6g",
                            pair, config.CB_WINDOW_MIN, drop, threshold, price)
                self._event("WARNING", "circuit_breaker",
                            f"触发熔断: {config.CB_WINDOW_MIN:.0f} 分钟回撤 {drop:.2f}%"
                            f"（阈值 {threshold:.1f}%），冻结该币对交易",
                            pair=pair,
                            detail={"drop_pct": drop, "threshold": threshold, "price": price})
                if is_btc:
                    self._cb_global = True
                    log.warning("大盘熔断: BTC 回撤 %.2f%%，全系统停止撮合", drop)
                    self._event("ERROR", "circuit_breaker",
                                f"大盘熔断: BTC {config.CB_WINDOW_MIN:.0f} 分钟回撤 {drop:.2f}%，"
                                f"全系统停止交易，等待企稳",
                                detail={"drop_pct": drop, "price": price})

    # ------------------------------------------------------------------
    def _update_indicators(self) -> None:
        """按周期刷新指标，确认信号后写入各网格，信号翻转/行情切换时记录事件。"""
        now = time.time()
        if now - self._last_indicator < config.INDICATOR_INTERVAL:
            return
        self._last_indicator = now
        self.indicators.update(config.PAIRS)
        for pair in config.PAIRS:
            ind = self.indicators.get(pair)
            raw_sig = ind["signal"] if config.USE_SIGNAL_FILTER else 0
            sig = self._confirm_signal(pair, raw_sig)  # 迟滞确认后的信号
            bot = self.bots.get(pair)
            if bot:
                bot.signal = sig
            self._update_regime(pair, sig, ind)
            # 每次指标刷新都检查挂单耗尽（信号解封/启动恢复后"熄火"的网格补挂）
            price = self.prices.get(pair)
            if bot and price:
                if sig != -1 and not any(
                        o["side"] == "buy" for o in bot.orders.values()):
                    bot.rebuild_buys(price, self.account)
                    n = sum(1 for o in bot.orders.values() if o["side"] == "buy")
                    if n:
                        self._event("INFO", "order_placed",
                                    f"买单侧耗尽，补挂 {n} 个", pair=pair)
                if sig != 1 and not any(
                        o["side"] == "sell" for o in bot.orders.values()):
                    bot.rebuild_sells(price, self.account)
                    n = sum(1 for o in bot.orders.values() if o["side"] == "sell")
                    if n:
                        self._event("INFO", "order_placed",
                                    f"卖单侧耗尽，补挂 {n} 个", pair=pair)
            if pair in self._last_signals and self._last_signals[pair] != sig:
                names = {1: "偏多(暂停卖单)", 0: "中性(双向挂单)", -1: "偏空(暂停买单)"}
                log.info("%s 趋势信号翻转: %s -> %s | %s",
                         pair, names.get(self._last_signals[pair]), names.get(sig),
                         ind["signal_text"])
                self._event(
                    "INFO", "signal_change",
                    f"趋势信号: {names.get(self._last_signals[pair])} → {names.get(sig)} · {ind['signal_text']}",
                    pair=pair,
                    detail={"from": self._last_signals[pair], "to": sig, **ind},
                )
            self._last_signals[pair] = sig

    def _confirm_signal(self, pair: str, raw: int) -> int:
        """信号迟滞确认：连续 SIGNAL_CONFIRM_COUNT 次同向才翻转，
        且两次翻转间隔不小于 SIGNAL_COOLDOWN 秒。抖动期间保持原信号。"""
        cur = self._last_signals.get(pair, 0)
        if raw == cur:
            self._signal_buf[pair] = []
            return cur
        buf = self._signal_buf[pair]
        buf.append(raw)
        del buf[:-config.SIGNAL_CONFIRM_COUNT]
        if len(buf) < config.SIGNAL_CONFIRM_COUNT or len(set(buf)) != 1:
            return cur  # 确认次数不足
        now = time.time()
        if now - self._last_flip.get(pair, 0) < config.SIGNAL_COOLDOWN:
            return cur  # 冷却期内
        self._last_flip[pair] = now
        self._signal_buf[pair] = []
        return raw

    def _update_regime(self, pair: str, sig: int, ind: dict) -> None:
        """趋势市识别：确认信号同向 + 3h 涨跌幅超阈值 → 趋势市。"""
        chg = ind.get("chg_3h", 0.0)
        if sig == -1 and chg <= -config.TREND_MOVE_PCT:
            regime = "trend_down"
        elif sig == 1 and chg >= config.TREND_MOVE_PCT:
            regime = "trend_up"
        else:
            regime = "ranging"
        bot = self.bots.get(pair)
        if bot:
            bot.regime = regime
        prev = self._regimes.get(pair)
        if prev is not None and prev != regime:
            names = {"ranging": "震荡市", "trend_up": "趋势上涨(冻结卖单)",
                     "trend_down": "趋势下跌(冻结买单)"}
            log.warning("%s 行情状态切换: %s -> %s (3h涨跌 %.2f%%)",
                        pair, names.get(prev), names.get(regime), chg)
            self._event(
                "WARNING" if regime != "ranging" else "INFO", "regime_change",
                f"行情切换: {names.get(prev)} → {names.get(regime)} · 3h涨跌 {chg:+.2f}% · {ind['signal_text']}",
                pair=pair,
                detail={"from": prev, "to": regime, "chg_3h": chg, **ind},
            )
        self._regimes[pair] = regime

    # ------------------------------------------------------------------
    def _maybe_rebalance(self) -> None:
        """定期再平衡（仅模拟盘）：按 ATR%×信号倾斜重算权重，
        重新分配各币对的全部 USDT 子弹并重建买单侧——
        只挪 USDT，绝不动持仓；卖单及其成本基准保持不变。

        偏离不足阈值（池子的 REBALANCE_MIN_DRIFT 或 1U）时不动作。
        """
        if self.mode != "paper":
            return
        now = time.time()
        if now - self._last_rebalance < config.REBALANCE_INTERVAL:
            return
        self._last_rebalance = now

        # 权重 = ATR% × (1 + 倾斜系数×信号)
        raw = {}
        for pair in config.PAIRS:
            ind = self.indicators.get(pair)
            atr = max(ind.get("atr_pct", 0.0), 1e-6)
            sig = ind.get("signal", 0) if config.USE_SIGNAL_FILTER else 0
            raw[pair] = atr * (1 + config.REBALANCE_SIGNAL_TILT * sig)
        total_raw = sum(raw.values())
        weights = {p: v / total_raw for p, v in raw.items()}

        # 子弹池 = 各币对 USDT 总额（买单是虚拟挂单，重切子弹零成本）
        pool = sum(self.account.get(p)["quote"] for p in config.PAIRS)
        if pool < 3:
            return  # 没有值得挪动的子弹

        deltas = {
            p: pool * weights[p] - self.account.get(p)["quote"]
            for p in config.PAIRS
        }
        if max(abs(d) for d in deltas.values()) < max(1.0, pool * config.REBALANCE_MIN_DRIFT):
            log.info("再平衡检查: 偏离不足阈值，不动作 (池子 %.2fU, 权重 %s)",
                     pool, {p: round(w, 2) for p, w in weights.items()})
            return

        # 执行：重设各币对 USDT = 池子×权重（总额守恒），并按新预算重建买单侧
        # 同步记账资金调拨（capital_adjust），避免盈亏基准被调拨污染
        for pair in config.PAIRS:
            old_q = self.account.get(pair)["quote"]
            new_q = pool * weights[pair]
            self.account.get(pair)["quote"] = new_q
            bot = self.bots.get(pair)
            if bot:
                bot.capital_adjust += new_q - old_q
        for pair, bot in self.bots.items():
            price = self.prices.get(pair)
            if not price:
                continue
            # 自适应区间漂移超过 50%：整个网格按新区间重建（保留利润累计）
            desired = self._desired_range_pct(pair)
            current_range = (bot.upper - bot.lower) / 2 / (bot.start_price or price)
            if abs(desired - current_range) / max(current_range, 1e-9) > 0.5:
                old = bot
                bal = self.account.get(pair)
                bot = self._build_bot(pair, price, bal["quote"], bal["base"])
                bot.realized_profit = old.realized_profit
                bot.trade_count = old.trade_count
                bot.blocked_count = old.blocked_count
                bot.total_fees = old.total_fees
                # 重建后新基准已含调拨资金，调整量清零
                bot.capital_adjust = 0.0
                self.bots[pair] = bot
                self._event(
                    "INFO", "grid_recenter",
                    f"波动率变化，区间自适应调整 ±{current_range*100:.2f}% → ±{desired*100:.2f}%",
                    pair=pair,
                    detail={"old_range": current_range, "new_range": desired},
                )
            else:
                bot.rebuild_buys(price, self.account)

        moves = {p: round(d, 2) for p, d in deltas.items() if abs(d) >= 0.01}
        log.info("再平衡执行: 池子 %.2fU, 权重 %s, 调动 %s",
                 pool, {p: round(w, 2) for p, w in weights.items()}, moves)
        self._event(
            "INFO", "rebalance",
            "子弹再平衡: " + ", ".join(
                f"{p.split('_')[0]} {'+' if d > 0 else ''}{d:.2f}U" for p, d in moves.items()
            ) + f" (池子 {pool:.2f}U, 权重 "
            + "/".join(f"{p.split('_')[0]}{w*100:.0f}%" for p, w in weights.items()) + ")",
            detail={"pool": pool, "weights": weights, "deltas": deltas},
        )

    # ------------------------------------------------------------------
    def _maybe_health_check(self) -> None:
        """每 HEALTH_INTERVAL 秒在运行日志里报一条健康状态。"""
        now = time.time()
        if now - self._last_health < config.HEALTH_INTERVAL:
            return
        self._last_health = now
        state = self.state()
        orders = sum(len(b.orders) for b in self.bots.values())
        trades = sum(b.trade_count for b in self.bots.values())
        tick_age = round(now - self.last_tick, 1) if self.last_tick else -1
        if self.last_error:
            msg = f"健康检查: 存在异常 {self.last_error}"
            level = "WARNING"
        else:
            msg = (
                f"运行正常: 总权益 {state['total_equity']:.2f} USDT, "
                f"已实现利润 {state['total_realized_profit']:.4f}, "
                f"挂单 {orders} 个, 成交 {trades} 笔, "
                f"行情 {tick_age}s 前更新"
            )
            level = "INFO"
        log.info("心跳: %s", msg)
        self._event(level, "health", msg, detail={
            "total_equity": state["total_equity"],
            "realized_profit": state["total_realized_profit"],
            "orders": orders, "trades": trades,
            "tick_age_sec": tick_age, "last_error": self.last_error,
        })

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
        pairs = {}
        for pair, bot in self.bots.items():
            s = bot.state(self.prices.get(pair, bot.start_price or 0), self.account)
            s["frozen"] = self._cb_global or pair in self._cb_pairs
            pairs[pair] = s
        total_equity = sum(s["equity"] for s in pairs.values())
        total_initial = sum(s["initial_equity"] for s in pairs.values())
        return {
            "mode": self.mode,
            "run_status": self.run_status,
            "circuit_breaker": {
                "global": self._cb_global,
                "pairs": sorted(self._cb_pairs.keys()),
            },
            "started_at": self.started_at,
            "last_tick": self.last_tick,
            "last_error": self.last_error,
            "total_equity": total_equity,
            "total_initial_equity": total_initial,
            "total_pnl": total_equity - total_initial,
            "total_realized_profit": sum(s["realized_profit"] for s in pairs.values()),
            "total_fees": sum(s["total_fees"] for s in pairs.values()),
            "pairs": pairs,
            "indicators": {p: self.indicators.get(p) for p in config.PAIRS},
            "signal_filter": config.USE_SIGNAL_FILTER,
            "recent_trades": self.store.recent_trades(50),
            "recent_events": self.store.recent_events(50),
            "equity_history": self.store.equity_history(300),
        }
