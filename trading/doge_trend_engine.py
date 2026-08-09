"""DOGE 趋势恢复策略的独立前向模拟引擎。

该引擎只做 ``DOGE_USDT`` 的 paper long/flat 模拟，和网格及实盘执行器完全隔离。
回测用“下一根开盘”作为可复现的代理；前向模拟则在发现新的已收盘 1h K 线后，
以当时读取到的 ticker 加保守滑点成交，并把两种时间点完整写入审计事件。
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from gate_api import GatePublicClient

from . import config
from .backtest import Candle, fetch_gate_candles
from .doge_trend import DogeTrendSettings, _ema, _simple_rsi
from .engine import _TICKER_BOOTSTRAP_WAIT_SEC, _fetch_tickers_cached
from .predictive import load_market_snapshot, save_market_snapshot
from .store import Store


log = logging.getLogger("trading.doge_trend_engine")
_STATE_KEY = "__doge_trend_portfolio__"
_CANDLE_SECONDS = 60 * 60


class DogeTrendPaperEngine:
    """DOGE 的低频 staged long/flat 前向模拟。

    只有超卖时才试探半仓；试探仓确认恢复后才提高到满仓。任何现货成交均是
    虚拟成交，且构造函数不会请求网络，避免首次下载历史数据阻塞 Web 服务。
    """

    def __init__(self, profile) -> None:
        if config.TRADING_MODE != "paper":
            raise RuntimeError("DOGE 趋势恢复策略仅支持模拟盘，禁止接入实盘模式")
        if profile.kind != "doge_trend":
            raise ValueError("DogeTrendPaperEngine 只能使用 doge_trend Profile")
        if tuple(profile.pairs) != ("DOGE_USDT",):
            raise ValueError("DOGE 趋势恢复策略仅允许 DOGE_USDT")

        self.profile = profile
        self.mode = "paper"
        self.pair = "DOGE_USDT"
        self.pairs = [self.pair]
        self.settings = DogeTrendSettings(
            pair=self.pair,
            total_quote_budget=config.TOTAL_QUOTE_BUDGET,
            fee_rate=config.PAPER_FEE_RATE,
            slippage_bps=config.DOGE_TREND_SLIPPAGE_BPS,
        )
        # 公开行情专用客户端：不需要 API key，也绝不含交易写接口。
        self.client = GatePublicClient(timeout=20.0, retries=3)
        self.store = Store(profile.db_path)
        self.prices: dict[str, float] = {self.pair: 0.0}
        self.candles: list[Candle] = []

        # 账户与仓位状态。position_cost 是累计实际买入支出，含买入费率与滑点。
        self.quote = 0.0
        self.base = 0.0
        self.average_cost = 0.0
        self.position_cost = 0.0
        self.held_bars = 0
        self.waiting_for_recovery = False
        self.pending_target: float | None = None
        self.rsi_rearmed = True
        self.last_stop_candle_ts = 0

        self.realized_profit = 0.0
        self.total_fees = 0.0
        self.total_slippage = 0.0
        self.total_turnover = 0.0
        self.trade_count = 0
        self.entry_count = 0
        self.add_count = 0
        self.exit_count = 0
        self.take_profit_count = 0
        self.stop_count = 0
        self.time_exit_count = 0
        self.closed_trade_count = 0
        self.winning_trade_count = 0
        self.last_processed_candle_ts = 0
        self.last_signal_candle_ts = 0
        self.latest_rsi: float | None = None
        self.latest_ema: float | None = None

        self._initial_total = config.TOTAL_QUOTE_BUDGET
        self.started_at = time.time()
        self.last_tick: float | None = None
        self.last_error: str | None = None
        self._last_success: float | None = None
        self._api_outage = False
        self._last_history_refresh = 0.0
        self._price_observed_at: float | None = None
        self._last_snapshot = 0.0
        self._last_health = 0.0
        self._cache_path = Path(config.DOGE_TREND_CACHE_PATH)
        self._ready = threading.Event()
        self._initializing = True
        self._init_error: str | None = None
        self._decision_pause_reason: str | None = None
        self._candle_lag_seconds: float | None = None
        self._next_init_attempt = 0.0
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._stopped = False
        self._thread: threading.Thread | None = None
        self._tick_lock = threading.Lock()
        self._warm_next_tick = False

        self._restore_or_seed()
        self._load_cached_history()
        # 与其他策略保持一致：后台只预热，用户在 Web Tab 点击开始后才会交易。
        self._paused.set()
        self._event("INFO", "doge_trend_init",
                    "DOGE 趋势行情将在后台预热；Web 服务无需等待历史下载")

    # ------------------------------------------------------------------
    # 持久化与历史行情
    # ------------------------------------------------------------------
    def _restore_or_seed(self) -> None:
        saved = self.store.load_bot_states().get(_STATE_KEY)
        if not saved:
            self.quote = config.TOTAL_QUOTE_BUDGET
            self._initial_total = self.quote
            self._event("INFO", "lifecycle",
                        f"DOGE 趋势模拟盘新建：纯 USDT {self.quote:.2f}，等待开始")
            self._save_state()
            return
        try:
            self.quote = float(saved["quote"])
            self.base = float(saved.get("base", 0.0))
            self.average_cost = float(saved.get("average_cost", 0.0))
            self.position_cost = float(saved.get("position_cost", self.base * self.average_cost))
            self.held_bars = max(0, int(saved.get("held_bars", 0)))
            self.waiting_for_recovery = bool(saved.get("waiting_for_recovery", False))
            raw_target = saved.get("pending_target")
            self.pending_target = None if raw_target is None else float(raw_target)
            self.rsi_rearmed = bool(saved.get("rsi_rearmed", True))
            self.last_stop_candle_ts = int(saved.get("last_stop_candle_ts", 0))
            self.realized_profit = float(saved.get("realized_profit", 0.0))
            self.total_fees = float(saved.get("total_fees", 0.0))
            self.total_slippage = float(saved.get("total_slippage", 0.0))
            self.total_turnover = float(saved.get("total_turnover", 0.0))
            self.trade_count = int(saved.get("trade_count", 0))
            self.entry_count = int(saved.get("entry_count", 0))
            self.add_count = int(saved.get("add_count", 0))
            self.exit_count = int(saved.get("exit_count", 0))
            self.take_profit_count = int(saved.get("take_profit_count", 0))
            self.stop_count = int(saved.get("stop_count", 0))
            self.time_exit_count = int(saved.get("time_exit_count", 0))
            self.closed_trade_count = int(saved.get("closed_trade_count", 0))
            self.winning_trade_count = int(saved.get("winning_trade_count", 0))
            self.last_processed_candle_ts = int(saved.get("last_processed_candle_ts", 0))
            self.last_signal_candle_ts = int(saved.get("last_signal_candle_ts", 0))
            self._initial_total = float(saved.get("initial_total", config.TOTAL_QUOTE_BUDGET))
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError(f"DOGE 趋势模拟盘存档无效: {error}") from error
        if self.base <= 1e-12:
            self.base = self.average_cost = self.position_cost = 0.0
            self.held_bars = 0
            self.waiting_for_recovery = False
        self._event("INFO", "lifecycle",
                    f"恢复 DOGE 趋势模拟盘：现金 {self.quote:.2f}，持仓 {self.base:.8g} DOGE")

    def _save_state(self) -> None:
        self.store.save_bot_state(_STATE_KEY, {
            "version": 1,
            "quote": self.quote, "base": self.base,
            "average_cost": self.average_cost, "position_cost": self.position_cost,
            "held_bars": self.held_bars, "waiting_for_recovery": self.waiting_for_recovery,
            "pending_target": self.pending_target, "rsi_rearmed": self.rsi_rearmed,
            "last_stop_candle_ts": self.last_stop_candle_ts,
            "realized_profit": self.realized_profit, "total_fees": self.total_fees,
            "total_slippage": self.total_slippage, "total_turnover": self.total_turnover,
            "trade_count": self.trade_count, "entry_count": self.entry_count,
            "add_count": self.add_count, "exit_count": self.exit_count,
            "take_profit_count": self.take_profit_count, "stop_count": self.stop_count,
            "time_exit_count": self.time_exit_count,
            "closed_trade_count": self.closed_trade_count,
            "winning_trade_count": self.winning_trade_count,
            "last_processed_candle_ts": self.last_processed_candle_ts,
            "last_signal_candle_ts": self.last_signal_candle_ts,
            "initial_total": self._initial_total,
        })

    def _normalise_candles(self, candles: list[Candle], now: float) -> list[Candle]:
        """去重、排序且只保留已收盘 K 线，绝不让正在形成的 K 线触发信号。"""
        by_ts = {candle.ts: candle for candle in candles}
        limit = config.DOGE_TREND_HISTORY_DAYS * 24 + 8
        closed_before = int(now) - 1
        return [
            by_ts[ts] for ts in sorted(by_ts)
            if ts + _CANDLE_SECONDS <= closed_before
        ][-limit:]

    def _assert_history_ready(self) -> None:
        required = max(self.settings.rsi_period, self.settings.confirmation_ema_period) + 2
        if len(self.candles) < required:
            raise RuntimeError(f"DOGE 趋势策略历史 K 线不足：需要 {required} 根，实际 {len(self.candles)} 根")

    def _update_indicators(self) -> None:
        if not self.candles:
            self.latest_rsi = self.latest_ema = None
            return
        closes = [candle.close for candle in self.candles]
        self.latest_rsi = _simple_rsi(closes, self.settings.rsi_period)[-1]
        self.latest_ema = _ema(closes, self.settings.confirmation_ema_period)[-1]

    def _update_decision_freshness(self) -> None:
        if not self.candles:
            return
        latest = self.candles[-1]
        lag = max(0.0, time.time() - (latest.ts + _CANDLE_SECONDS))
        self._candle_lag_seconds = lag
        if lag > config.DOGE_TREND_MAX_CANDLE_LAG_SEC:
            if self._decision_pause_reason is None:
                self._decision_pause_reason = (
                    f"最新已收盘 K 线滞后 {lag / 3600:.2f}h，超过 "
                    f"{config.DOGE_TREND_MAX_CANDLE_LAG_SEC / 3600:.2f}h 阈值"
                )
                self._event("ERROR", "doge_trend_decision_paused",
                            f"DOGE 新建仓/加仓已暂停：{self._decision_pause_reason}",
                            detail={"latest_candle_ts": latest.ts, "lag_seconds": lag,
                                    "max_lag_seconds": config.DOGE_TREND_MAX_CANDLE_LAG_SEC})
            return
        if self._decision_pause_reason is not None:
            previous = self._decision_pause_reason
            self._decision_pause_reason = None
            self._event("INFO", "doge_trend_decision_resumed",
                        "DOGE K 线已恢复新鲜，重新允许新建仓和加仓",
                        detail={"previous_reason": previous, "latest_candle_ts": latest.ts,
                                "lag_seconds": lag})

    def _load_cached_history(self) -> None:
        """缓存只加快启动；无效缓存自动降级为后台下载。"""
        if not self._cache_path.exists():
            return
        try:
            cached = load_market_snapshot(self._cache_path, (self.pair,))
            self.candles = self._normalise_candles(cached[self.pair], time.time())
            self._assert_history_ready()
            self._update_indicators()
            self._update_decision_freshness()
        except Exception as error:
            self.candles = []
            self._event("WARNING", "doge_trend_cache_invalid",
                        f"DOGE K 线缓存不可用，将后台重新下载：{type(error).__name__}: {error}")
            return
        self._event("INFO", "doge_trend_cache",
                    f"已从本地缓存恢复 {len(self.candles)} 根 DOGE 1h 已收盘 K 线",
                    detail={"path": str(self._cache_path), "candles": len(self.candles),
                            "latest_ts": self.candles[-1].ts})

    def _save_history_cache(self) -> None:
        try:
            save_market_snapshot(self._cache_path, {self.pair: self.candles})
        except Exception as error:
            self._event("WARNING", "doge_trend_cache_error",
                        f"DOGE K 线缓存保存失败：{type(error).__name__}: {error}")

    def _load_initial_history(self) -> None:
        now = time.time()
        start_ts = int(now) - config.DOGE_TREND_HISTORY_DAYS * 24 * _CANDLE_SECONDS
        raw = fetch_gate_candles(self.pair, "1h", start_ts, int(now), client=self.client)
        self.candles = self._normalise_candles(raw, now)
        self._assert_history_ready()
        self._update_indicators()
        self._last_history_refresh = now
        self._update_decision_freshness()
        self._save_history_cache()
        self._event("INFO", "doge_trend_history",
                    f"已读取 {len(self.candles)} 根 DOGE 1h 已收盘 K 线",
                    detail={"candles": len(self.candles), "latest_ts": self.candles[-1].ts,
                            "source": "full_download"})

    def _refresh_history(self, *, force: bool = False) -> bool:
        """合并末尾 K 线；失败时保留旧序列，避免一次网络抖动停掉止盈止损。"""
        if not self.candles:
            raise RuntimeError("DOGE 趋势 K 线尚未初始化")
        now = time.time()
        if not force and now - self._last_history_refresh < config.DOGE_TREND_KLINE_REFRESH_SEC:
            self._update_decision_freshness()
            return False
        old_latest = self.candles[-1].ts
        try:
            update = fetch_gate_candles(
                self.pair, "1h", old_latest - 2 * _CANDLE_SECONDS, int(now), client=self.client,
            )
        except Exception as error:
            self._last_history_refresh = now
            self._update_decision_freshness()
            self._event("WARNING", "doge_trend_history_partial",
                        f"DOGE K 线刷新失败；保留旧数据并等待下次刷新：{type(error).__name__}: {error}",
                        pair=self.pair, detail={"latest_ts": old_latest})
            return False
        self.candles = self._normalise_candles(self.candles + update, now)
        self._assert_history_ready()
        self._update_indicators()
        self._last_history_refresh = now
        self._update_decision_freshness()
        changed = self.candles[-1].ts > old_latest
        if changed:
            self._save_history_cache()
            self._event("INFO", "doge_trend_history",
                        "DOGE 已合并新的已收盘 1h K 线",
                        pair=self.pair, detail={"latest_ts": self.candles[-1].ts})
        return changed

    def _bootstrap_market_data(self) -> None:
        """仅在后台线程预热，构造阶段不进行任何网络 I/O。"""
        if self.candles:
            self._refresh_history(force=True)
        else:
            self._load_initial_history()
        self.prices = _fetch_tickers_cached(
            None, self.pairs, initial_wait_sec=_TICKER_BOOTSTRAP_WAIT_SEC,
        )
        price = self.prices.get(self.pair, 0.0)
        if price <= 0:
            raise RuntimeError("未获取到 DOGE_USDT 行情")
        self._price_observed_at = time.time()

        # 停机期间不伪造“早该成交”的历史交易。保留已持有仓位，但把 K 线观察点
        # 对齐到当前，并取消尚未成交的历史信号；之后只记录真正的前向触发。
        latest_ts = self.candles[-1].ts
        if self.last_processed_candle_ts and latest_ts > self.last_processed_candle_ts:
            missed = int((latest_ts - self.last_processed_candle_ts) / _CANDLE_SECONDS)
            if self.base > 0:
                self.held_bars += max(0, missed)
            self._event("WARNING", "doge_trend_resume_gap",
                        f"DOGE 策略离线期间跨过 {missed} 根 K 线；不补造历史信号",
                        detail={"previous_ts": self.last_processed_candle_ts,
                                "latest_ts": latest_ts, "missed_bars": missed})
        if self.pending_target is not None:
            self.pending_target = None
            self._event("WARNING", "doge_trend_signal_cancelled",
                        "重启后取消未成交的历史信号，避免以事后 ticker 补造成交")
        self.last_processed_candle_ts = latest_ts
        self._save_state()
        self._ready.set()
        self._initializing = False
        self._init_error = None
        self._event("INFO", "doge_trend_ready", "DOGE 趋势行情预热完成，可开始前向模拟",
                    detail={"cache_path": str(self._cache_path), "latest_candle_ts": latest_ts,
                            "ticker_observed_at": self._price_observed_at})

    # ------------------------------------------------------------------
    # 信号、成交与风险退出
    # ------------------------------------------------------------------
    def _equity(self) -> float:
        return self.quote + self.base * self.prices.get(self.pair, 0.0)

    def _record_fill(self, fill: dict) -> None:
        self.store.record_trade(
            self.mode, self.pair, fill["side"], fill["price"], fill["amount"],
            fill["quote"], fill["profit"], fill.get("fee", 0.0),
        )
        side = "买入" if fill["side"] == "buy" else "卖出"
        reason_labels = {
            "oversold_entry": "RSI 超卖试探", "trend_confirmation": "EMA 趋势确认加仓",
            "take_profit": "止盈", "stop": "止损", "time": "持仓到期",
        }
        self._event(
            "INFO", "doge_trend_fill",
            f"DOGE 趋势模拟{side}（{reason_labels.get(fill.get('reason'), fill.get('reason', ''))}）: "
            f"@ {fill['price']:.8g} 数量 {fill['amount']:.8g} 金额 {fill['quote']:.4f}U，"
            f"利润 {fill['profit']:.4f}U",
            pair=self.pair, detail=fill,
        )

    def _buy_to_target(self, target: float, mid: float, audit: dict) -> bool:
        """把 DOGE 提高到目标权益权重；目标仅允许 50% 或 100%。"""
        if target <= 0 or target > 1 or mid <= 0:
            return False
        equity = self.quote + self.base * mid
        desired_notional = equity * target
        current_notional = self.base * mid
        if desired_notional <= current_notional + 1e-12:
            return False
        spend = min(self.quote, (desired_notional - current_notional) / (1 + self.settings.fee_rate))
        if spend <= 1e-12:
            return False
        fee = spend * self.settings.fee_rate
        price = mid * (1 + self.settings.slippage_bps / 10_000)
        amount = (spend - fee) / price
        if amount <= 1e-12:
            return False
        previous_base = self.base
        self.quote -= spend
        self.base += amount
        self.position_cost += spend
        self.average_cost = self.position_cost / self.base
        self.total_fees += fee
        self.total_slippage += amount * (price - mid)
        self.total_turnover += amount * mid
        self.trade_count += 1
        if previous_base <= 1e-12:
            self.entry_count += 1
            self.held_bars = 0
            self.waiting_for_recovery = target < 1.0
        else:
            self.add_count += 1
            self.waiting_for_recovery = False
        self._record_fill({
            "pair": self.pair, "side": "buy", "price": price, "amount": amount,
            "quote": spend, "profit": 0.0, "fee": fee, "market_mid": mid,
            "target_weight": target, **audit,
        })
        return True

    def _sell_all(self, mid: float, reason: str, audit: dict | None = None) -> bool:
        if self.base <= 1e-12 or mid <= 0:
            return False
        amount = self.base
        price = mid * (1 - self.settings.slippage_bps / 10_000)
        gross = amount * price
        fee = gross * self.settings.fee_rate
        net = gross - fee
        profit = net - self.position_cost
        self.quote += net
        self.realized_profit += profit
        self.total_fees += fee
        self.total_slippage += amount * (mid - price)
        self.total_turnover += amount * mid
        self.trade_count += 1
        self.exit_count += 1
        self.closed_trade_count += 1
        self.winning_trade_count += int(profit > 0)
        if reason == "take_profit":
            self.take_profit_count += 1
        elif reason == "stop":
            self.stop_count += 1
            if self.settings.require_rsi_rearm_after_stop:
                self.rsi_rearmed = False
                self.last_stop_candle_ts = self.last_processed_candle_ts
        elif reason == "time":
            self.time_exit_count += 1
        self.base = self.average_cost = self.position_cost = 0.0
        self.held_bars = 0
        self.waiting_for_recovery = False
        self.pending_target = None
        fill_audit = {
            "pair": self.pair, "side": "sell", "price": price, "amount": amount,
            "quote": gross, "profit": profit, "fee": fee, "market_mid": mid,
            "reason": reason, "price_source": "live_ticker",
            "ticker_observed_at": self._price_observed_at,
        }
        if audit:
            fill_audit.update(audit)
        self._record_fill(fill_audit)
        return True

    def _observe_new_candle(self) -> bool:
        """推进到最新已收盘 K 线；缺口不逐根补造历史决策。"""
        if not self.candles:
            return False
        latest = self.candles[-1]
        if latest.ts <= self.last_processed_candle_ts:
            return False
        if self.last_processed_candle_ts:
            elapsed = int((latest.ts - self.last_processed_candle_ts) / _CANDLE_SECONDS)
            if self.base > 0:
                self.held_bars += max(1, elapsed)
        self.last_processed_candle_ts = latest.ts
        return True

    def _maybe_exit(self, mid: float) -> bool:
        """止盈、止损以每个 ticker 检查；K 线过旧也不取消已有仓位风控。"""
        if self.base <= 1e-12:
            return False
        if mid <= self.average_cost * (1 - self.settings.stop_loss_pct):
            return self._sell_all(mid, "stop")
        if mid >= self.average_cost * (1 + self.settings.take_profit_pct):
            return self._sell_all(mid, "take_profit")
        if self.held_bars >= self.settings.max_hold_bars:
            return self._sell_all(mid, "time")
        return False

    def _schedule_signal(self) -> None:
        """只用刚刚确认已收盘的 K 线生成下一笔前向模拟信号。"""
        if self._decision_pause_reason is not None or not self.candles:
            return
        latest = self.candles[-1]
        if latest.ts == self.last_signal_candle_ts:
            return
        closes = [candle.close for candle in self.candles]
        rsi = _simple_rsi(closes, self.settings.rsi_period)
        ema = _ema(closes, self.settings.confirmation_ema_period)
        self.latest_rsi = rsi[-1]
        self.latest_ema = ema[-1]
        target: float | None = None
        reason = ""
        if self.settings.require_rsi_rearm_after_stop and not self.rsi_rearmed:
            # 停损当根不能立即“自我原谅”。只有一个后续完整小时的 RSI 回到阈值
            # 上方，才重新允许将来的超卖信号入场。
            if (latest.ts > self.last_stop_candle_ts and rsi[-1] is not None
                    and rsi[-1] >= self.settings.rsi_threshold):
                self.rsi_rearmed = True
                self._event("INFO", "doge_trend_rearmed",
                            "DOGE RSI 已回到阈值上方，策略重新允许未来超卖试探",
                            pair=self.pair,
                            detail={"candle_ts": latest.ts, "rsi_20": rsi[-1],
                                    "threshold": self.settings.rsi_threshold})
            else:
                self.last_signal_candle_ts = latest.ts
                return
        if self.base <= 1e-12 and rsi[-1] is not None and rsi[-1] < self.settings.rsi_threshold:
            target = self.settings.initial_fraction
            reason = "oversold_entry"
        elif self.base > 1e-12 and self.waiting_for_recovery and len(closes) >= 2:
            recovered = (
                closes[-2] <= ema[-2]
                and latest.close > ema[-1]
                and latest.close > self.average_cost
            )
            if recovered:
                target = 1.0
                reason = "trend_confirmation"
        self.last_signal_candle_ts = latest.ts
        if target is None:
            return
        self.pending_target = target
        self._event(
            "INFO", "doge_trend_signal",
            ("DOGE RSI 超卖，准备试探 50% 仓位" if reason == "oversold_entry"
             else "DOGE EMA 上穿确认且高于成本，准备加仓至 100%"),
            pair=self.pair,
            detail={"signal_candle_ts": latest.ts, "signal_close": latest.close,
                    "rsi_20": rsi[-1], "ema_6": ema[-1], "average_cost": self.average_cost,
                    "target_weight": target, "reason": reason},
        )

    def _execute_pending(self, mid: float) -> bool:
        if self.pending_target is None or self._decision_pause_reason is not None:
            return False
        target = self.pending_target
        latest = self.candles[-1] if self.candles else None
        reason = "oversold_entry" if target < 1 else "trend_confirmation"
        audit = {
            # 回测的代理为下一根开盘；这里记录纸盘真实的收到信号后的 ticker 成交。
            "reason": reason, "signal_candle_ts": latest.ts if latest else None,
            "signal_close": latest.close if latest else None,
            "decision_ts": time.time(), "ticker_observed_at": self._price_observed_at,
            "price_source": "live_ticker_after_closed_candle",
            "slippage_bps": self.settings.slippage_bps,
        }
        self.pending_target = None
        return self._buy_to_target(target, mid, audit)

    # ------------------------------------------------------------------
    # 引擎循环、控制与 Web 状态
    # ------------------------------------------------------------------
    def tick(self) -> None:
        self._refresh_history()
        if self._warm_next_tick:
            self._warm_next_tick = False
            self.prices = _fetch_tickers_cached(
                None, self.pairs, initial_wait_sec=_TICKER_BOOTSTRAP_WAIT_SEC,
            )
        else:
            self.prices = _fetch_tickers_cached(None, self.pairs)
        self._price_observed_at = time.time()
        mid = self.prices.get(self.pair, 0.0)
        if mid <= 0:
            raise RuntimeError("DOGE_USDT 无可用 ticker 价格")
        new_candle = self._observe_new_candle()
        self._maybe_exit(mid)
        if new_candle:
            self._schedule_signal()
        self._execute_pending(mid)
        self.last_tick = time.time()
        self._save_state()
        if self.last_tick - self._last_snapshot >= 30:
            self._last_snapshot = self.last_tick
            self.store.record_equity(self._equity(), self.realized_profit,
                                     {self.pair: self.base * mid, "USDT": self.quote})
        self._maybe_health_check()

    def _maybe_health_check(self) -> None:
        now = time.time()
        if now - self._last_health < config.HEALTH_INTERVAL:
            return
        self._last_health = now
        message = (f"DOGE 趋势策略正常：权益 {self._equity():.2f}U，"
                   f"仓位 {self.base * self.prices.get(self.pair, 0.0):.2f}U，"
                   f"成交 {self.trade_count} 笔")
        log.info(message)
        self._event("INFO", "health", message)

    def run(self) -> None:
        log.info("DOGE 趋势模拟引擎启动：轮询 %ss，K 线刷新 %ss",
                 config.TICK_INTERVAL, config.DOGE_TREND_KLINE_REFRESH_SEC)
        self._event("INFO", "lifecycle", "DOGE 趋势模拟引擎线程启动")
        while not self._stop.is_set():
            if not self._ready.is_set():
                if time.time() < self._next_init_attempt:
                    self._stop.wait(min(config.DOGE_TREND_INIT_RETRY_SEC, 1.0))
                    continue
                try:
                    self._bootstrap_market_data()
                    self._last_success = time.time()
                except Exception as error:
                    self._init_error = f"{type(error).__name__}: {error}"
                    self.last_error = self._init_error
                    self._next_init_attempt = time.time() + config.DOGE_TREND_INIT_RETRY_SEC
                    log.exception("DOGE 趋势策略行情预热失败，将在 %.0f 秒后重试",
                                  config.DOGE_TREND_INIT_RETRY_SEC)
                    self._event("ERROR", "doge_trend_init_error",
                                f"DOGE 趋势行情预热失败，将自动重试：{self._init_error}",
                                detail={"traceback": traceback.format_exc(),
                                        "retry_after_sec": config.DOGE_TREND_INIT_RETRY_SEC})
                continue
            if self._paused.is_set():
                self._stop.wait(config.TICK_INTERVAL)
                continue
            try:
                with self._tick_lock:
                    self.tick()
                self.last_error = None
                self._last_success = time.time()
                if self._api_outage:
                    self._api_outage = False
                    self._event("INFO", "api_recovered", "DOGE 趋势策略 API 已恢复")
            except Exception as error:
                self.last_error = f"{type(error).__name__}: {error}"
                log.exception("DOGE 趋势策略 tick 失败")
                self._event("ERROR", "tick_error", self.last_error,
                            detail={"traceback": traceback.format_exc()})
                if (self._last_success and time.time() - self._last_success
                        > config.API_OUTAGE_ALERT_SEC and not self._api_outage):
                    self._api_outage = True
                    self._event("ERROR", "api_outage",
                                f"DOGE 趋势策略 API 持续中断：{self.last_error}")
            self._stop.wait(config.TICK_INTERVAL)

    def start_background(self) -> None:
        self._thread = threading.Thread(target=self.run, daemon=True, name="doge-trend-paper-engine")
        self._thread.start()

    def pause(self) -> str:
        if self._stopped:
            return "stopped"
        self._paused.set()
        self._event("INFO", "control", "用户暂停 DOGE 趋势模拟策略")
        return "paused"

    def reset_paper(self, budget: float) -> str:
        if not isinstance(budget, (int, float)) or isinstance(budget, bool):
            raise ValueError("重置金额必须是数字")
        if not (0 < float(budget) <= 1_000_000):
            raise ValueError("重置金额必须在 (0, 1000000] USDT 之间")
        budget = float(budget)
        self._paused.set()
        with self._tick_lock:
            self.quote = budget
            self.base = self.average_cost = self.position_cost = 0.0
            self.held_bars = 0
            self.waiting_for_recovery = False
            self.pending_target = None
            self.rsi_rearmed = True
            self.last_stop_candle_ts = 0
            self.realized_profit = self.total_fees = self.total_slippage = self.total_turnover = 0.0
            self.trade_count = self.entry_count = self.add_count = self.exit_count = 0
            self.take_profit_count = self.stop_count = self.time_exit_count = 0
            self.closed_trade_count = self.winning_trade_count = 0
            # 与冷启动一致：若行情已预热，不以可能接近一小时前的历史 K 线补做信号。
            self.last_processed_candle_ts = self.candles[-1].ts if self.candles else 0
            self.last_signal_candle_ts = self.last_processed_candle_ts
            self._initial_total = budget
            self.store.clear_bot_states()
            self.store.clear_trades()
            self.store.clear_equity_snapshots()
            self._save_state()
        log.warning("DOGE 趋势模拟盘仓位已重置: %.2f USDT", budget)
        self._event("WARNING", "paper_reset",
                    f"DOGE 趋势模拟盘已重置为 {budget:.2f} USDT，等待开始指令",
                    detail={"budget": budget})
        return self.run_status

    def resume(self) -> str:
        if self._stopped:
            return "stopped"
        self._paused.clear()
        self._warm_next_tick = True
        self._event("INFO", "control", "用户开始/恢复 DOGE 趋势模拟策略")
        return self.run_status

    def start(self) -> str:
        if self._stopped:
            self._stop.clear()
            self._paused.clear()
            self._stopped = False
            self._warm_next_tick = True
            self.start_background()
            self._event("INFO", "control", "用户重新启动 DOGE 趋势模拟策略")
            return self.run_status
        return self.resume()

    def shutdown(self) -> str:
        with self._tick_lock:
            self._save_state()
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._stopped = True
        self._event("INFO", "control", "用户停止 DOGE 趋势模拟策略")
        return "stopped"

    def stop(self) -> None:
        self._save_state()
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self.store.close()
        self.client.close()

    @property
    def run_status(self) -> str:
        if self._stopped:
            return "stopped"
        if not self._ready.is_set():
            return "initializing"
        if self._paused.is_set():
            return "paused"
        return "running"

    def _event(self, level: str, type: str, message: str, pair: str | None = None,
               detail: dict | None = None) -> None:
        try:
            self.store.record_event(level, type, message, pair, detail)
        except Exception:
            log.exception("DOGE 趋势策略事件落库失败: %s", type)

    def state(self) -> dict[str, Any]:
        price = self.prices.get(self.pair, 0.0)
        unrealized = self.base * (price - self.average_cost) if self.base > 0 and price else 0.0
        equity = self._equity()
        take_price = self.average_cost * (1 + self.settings.take_profit_pct) if self.base else None
        stop_price = self.average_cost * (1 - self.settings.stop_loss_pct) if self.base else None
        position_weight = (self.base * price / equity) if equity > 0 and price > 0 else 0.0
        stage = ("止损后等待 RSI 重新武装" if self.base <= 1e-12 and not self.rsi_rearmed else
                 "等待超卖" if self.base <= 1e-12 else
                 "半仓等待 EMA 确认" if self.waiting_for_recovery else "满仓趋势确认")
        rows = {
            self.pair: {
                "pair": self.pair, "price": price, "quote": 0.0, "base": self.base,
                "equity": self.base * price, "pnl": self.realized_profit + unrealized,
                "realized_profit": self.realized_profit, "total_fees": 0.0,
                "trade_count": self.trade_count, "blocked_count": 0, "orders": [],
                "lower": stop_price or (price * 0.95), "upper": take_price or (price * 1.05),
                "signal": 0, "regime": "doge_trend", "frozen": False,
                "average_cost": self.average_cost or None, "take_price": take_price,
                "stop_price": stop_price, "position_weight": position_weight,
            },
            "USDT": {
                "pair": "USDT", "price": 1.0, "quote": self.quote, "base": 0.0,
                "equity": self.quote, "pnl": 0.0, "realized_profit": 0.0,
                "total_fees": self.total_fees, "trade_count": 0, "blocked_count": 0,
                "orders": [], "lower": 0.99, "upper": 1.01, "signal": 0,
                "regime": "cash", "frozen": False,
            },
        }
        return {
            "mode": self.mode, "strategy": self.profile.name,
            "strategy_label": self.profile.label, "strategy_kind": "doge_trend",
            "run_status": self.run_status,
            "circuit_breaker": {"global": False, "pairs": []},
            "api_outage": self._api_outage, "last_success": self._last_success,
            "started_at": self.started_at, "last_tick": self.last_tick,
            "last_error": self.last_error, "total_equity": equity,
            "total_initial_equity": self._initial_total, "total_pnl": equity - self._initial_total,
            "total_realized_profit": self.realized_profit, "total_fees": self.total_fees,
            "pairs": rows, "indicators": {}, "signal_filter": False,
            "doge_trend": {
                "ready": self._ready.is_set(), "initializing": self._initializing,
                "init_error": self._init_error, "cache_path": str(self._cache_path),
                "rsi_20": self.latest_rsi, "ema_6": self.latest_ema,
                "average_cost": self.average_cost or None, "take_price": take_price,
                "stop_price": stop_price, "held_bars": self.held_bars,
                "max_hold_bars": self.settings.max_hold_bars, "position_weight": position_weight,
                "stage": stage, "pending_target": self.pending_target,
                "rsi_rearmed": self.rsi_rearmed, "last_stop_candle_ts": self.last_stop_candle_ts,
                "last_processed_candle_ts": self.last_processed_candle_ts,
                "last_signal_candle_ts": self.last_signal_candle_ts,
                "slippage_bps": self.settings.slippage_bps,
                "ticker_observed_at": self._price_observed_at,
                "decision_paused": self._decision_pause_reason is not None,
                "decision_pause_reason": self._decision_pause_reason,
                "candle_lag_seconds": self._candle_lag_seconds,
                "entry_count": self.entry_count, "add_count": self.add_count,
                "exit_count": self.exit_count, "take_profit_count": self.take_profit_count,
                "stop_count": self.stop_count, "time_exit_count": self.time_exit_count,
            },
            "recent_trades": self.store.recent_trades(50),
            "recent_events": self.store.recent_events(50),
            "equity_history": self.store.equity_history(300),
        }
