"""预测轮动策略的严格模拟盘引擎。

它与 ``Engine`` 的网格下单、实盘执行器完全隔离：只允许 paper 模式，使用 1h
已收盘 K 线按周滚动训练 Ridge 模型，信号在新 K 线后的现价虚拟成交。这样 Web
面板能观察真正的前向模拟，而不会把回测收益或预测信号转化为真实订单。
"""

from __future__ import annotations

import logging
import math
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import numpy as np

from gate_api import GatePublicClient

from . import config
from .backtest import Candle, fetch_gate_candles
from .engine import _TICKER_BOOTSTRAP_WAIT_SEC, _fetch_tickers_cached
from .predictive import (
    PredictiveSettings,
    _WARMUP_BARS,
    _align,
    _ema,
    _feature_matrix,
    load_market_snapshot,
    _new_model,
    save_market_snapshot,
    _training_set,
)
from .store import Store


log = logging.getLogger("trading.predictive_engine")
_STATE_KEY = "__predictive_portfolio__"


class PredictivePaperEngine:
    """以真实行情进行虚拟成交的低频 long/flat 预测策略。

    模型、币池与参数固定为当前研究候选。每次成交都按手续费和不利滑点扣减；
    没有任何 ``LiveExecutor`` 或 Gate 写接口，因此无法在实盘模式下运行。
    """

    def __init__(self, profile) -> None:
        if config.TRADING_MODE != "paper":
            raise RuntimeError("预测轮动策略仅支持模拟盘，禁止接入实盘模式")
        if profile.kind != "predictive":
            raise ValueError("PredictivePaperEngine 只能使用 predictive Profile")
        self.profile = profile
        self.mode = "paper"
        self.settings = PredictiveSettings(
            pairs=profile.pairs,
            total_quote_budget=config.TOTAL_QUOTE_BUDGET,
            fee_rate=config.PAPER_FEE_RATE,
            slippage_bps=config.PREDICTIVE_SLIPPAGE_BPS,
            horizon_bars=config.PREDICTIVE_HORIZON_HOURS,
            train_bars=config.PREDICTIVE_TRAIN_DAYS * 24,
            retrain_interval_bars=config.PREDICTIVE_RETRAIN_HOURS,
            rebalance_interval_bars=config.PREDICTIVE_REBALANCE_HOURS,
            expected_return_threshold=config.PREDICTIVE_THRESHOLD,
            max_positions=config.PREDICTIVE_MAX_POSITIONS,
            market_ema_period=config.PREDICTIVE_MARKET_EMA,
            # 研究结论表明 XGBoost 当前过拟合；纸盘固定使用 ridge 候选。
            model="ridge",
        )
        # 预测策略只读公开行情，不依赖交易 API Key；K 线和 ticker 都经由统一客户端。
        self.client = GatePublicClient(timeout=20.0, retries=3)
        self.store = Store(profile.db_path)
        self.pairs = list(profile.pairs)
        self.prices: dict[str, float] = {pair: 0.0 for pair in self.pairs}
        self.candles: dict[str, list[Candle]] = {}
        self.quote = 0.0
        self.base = {pair: 0.0 for pair in self.pairs}
        self.avg_cost: dict[str, float | None] = {pair: None for pair in self.pairs}
        self.realized_profit = {pair: 0.0 for pair in self.pairs}
        self.trade_count = {pair: 0 for pair in self.pairs}
        self.total_fees = 0.0
        self.target: tuple[str, ...] = ()
        self.predictions: dict[str, float] = {}
        self.last_decision_candle_ts = 0
        self.last_refit_candle_ts = 0
        self._model: Any = None
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
        self._cache_path = Path(config.PREDICTIVE_CACHE_PATH)
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
        # tick 与仓位重置互斥：防止重置与进行中的调仓决策并发修改账户
        self._tick_lock = threading.Lock()
        # 恢复/重启交易后的首个 tick：暂停期间缓存必然过期，用预热预算等行情
        self._warm_next_tick = False

        self._restore_or_seed()
        self._load_cached_history()
        # 和其他策略一致：面板显式点击“开始”才产生新的预测决策与虚拟成交。
        self._paused.set()
        self._event("INFO", "predictive_init", "预测行情将在后台预热；Web 服务无需等待历史下载")

    # ------------------------------------------------------------------
    # 初始化、历史 K 线与持久化
    # ------------------------------------------------------------------
    def _restore_or_seed(self) -> None:
        saved = self.store.load_bot_states().get(_STATE_KEY)
        if not saved:
            self.quote = config.TOTAL_QUOTE_BUDGET
            self._initial_total = self.quote
            self._event(
                "INFO", "lifecycle",
                f"预测模拟盘新建：纯 USDT {self.quote:.2f}，等待开始后首次模型决策",
            )
            self._save_state()
            return
        try:
            self.quote = float(saved["quote"])
            self.base = {pair: float(saved.get("base", {}).get(pair, 0.0)) for pair in self.pairs}
            self.avg_cost = {
                pair: (None if saved.get("avg_cost", {}).get(pair) is None
                       else float(saved["avg_cost"][pair]))
                for pair in self.pairs
            }
            self.realized_profit = {
                pair: float(saved.get("realized_profit", {}).get(pair, 0.0))
                for pair in self.pairs
            }
            self.trade_count = {
                pair: int(saved.get("trade_count", {}).get(pair, 0))
                for pair in self.pairs
            }
            self.total_fees = float(saved.get("total_fees", 0.0))
            self.target = tuple(pair for pair in saved.get("target", []) if pair in self.pairs)
            self.predictions = {
                pair: float(value) for pair, value in saved.get("predictions", {}).items()
                if pair in self.pairs
            }
            self.last_decision_candle_ts = int(saved.get("last_decision_candle_ts", 0))
            self.last_refit_candle_ts = int(saved.get("last_refit_candle_ts", 0))
            self._initial_total = float(saved.get("initial_total", config.TOTAL_QUOTE_BUDGET))
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError(f"预测模拟盘存档无效: {error}") from error
        self._event(
            "INFO", "lifecycle",
            f"恢复预测模拟盘：现金 {self.quote:.2f}，目标 {','.join(self.target) or '空仓'}",
        )

    def _save_state(self) -> None:
        self.store.save_bot_state(_STATE_KEY, {
            "version": 1,
            "quote": self.quote,
            "base": self.base,
            "avg_cost": self.avg_cost,
            "realized_profit": self.realized_profit,
            "trade_count": self.trade_count,
            "total_fees": self.total_fees,
            "target": list(self.target),
            "predictions": self.predictions,
            "last_decision_candle_ts": self.last_decision_candle_ts,
            "last_refit_candle_ts": self.last_refit_candle_ts,
            "initial_total": self._initial_total,
        })

    def _load_initial_history(self) -> None:
        end_ts = int(time.time())
        start_ts = end_ts - config.PREDICTIVE_HISTORY_DAYS * 24 * 60 * 60
        log.info("预测策略读取 %d 天 1h 历史 K 线（%d 个币对）",
                 config.PREDICTIVE_HISTORY_DAYS, len(self.pairs))
        raw = {
            pair: fetch_gate_candles(pair, "1h", start_ts, end_ts, client=self.client)
            for pair in self.pairs
        }
        self.candles = _align(raw, self.pairs)
        self._assert_history_ready()
        self._last_history_refresh = time.time()
        self._update_decision_freshness()
        self._save_history_cache()
        latest = self.candles[self.pairs[0]][-1]
        self._event(
            "INFO", "predictive_history",
            f"已读取 {len(self.candles[self.pairs[0]])} 根共同 1h K 线，最新 {latest.ts}",
            detail={"candles": len(self.candles[self.pairs[0]]), "latest_ts": latest.ts,
                    "source": "full_download"},
        )

    def _load_cached_history(self) -> None:
        """快速恢复本地 K 线缓存；失败只降级为后台完整下载，不阻断服务。"""
        if not self._cache_path.exists():
            return
        try:
            cached = load_market_snapshot(self._cache_path, self.pairs)
            self.candles = _align(cached, self.pairs)
            self._assert_history_ready()
        except Exception as error:
            self.candles = {}
            self._event(
                "WARNING", "predictive_cache_invalid",
                f"预测 K 线缓存不可用，将后台重新下载：{type(error).__name__}: {error}",
            )
            return
        latest = self.candles[self.pairs[0]][-1]
        self._event(
            "INFO", "predictive_cache",
            f"已从本地缓存恢复 {len(self.candles[self.pairs[0]])} 根共同 1h K 线",
            detail={"path": str(self._cache_path), "candles": len(self.candles[self.pairs[0]]),
                    "latest_ts": latest.ts},
        )

    def _save_history_cache(self) -> None:
        try:
            save_market_snapshot(self._cache_path, self.candles)
        except Exception as error:
            # 缓存失败不能中断已就绪的模拟盘，只记录供运维排查。
            self._event(
                "WARNING", "predictive_cache_error",
                f"预测 K 线缓存保存失败：{type(error).__name__}: {error}",
            )

    def _assert_history_ready(self) -> None:
        required = _WARMUP_BARS + self.settings.train_bars + self.settings.horizon_bars + 2
        count = len(self.candles[self.pairs[0]])
        if count < required:
            raise RuntimeError(f"预测策略历史 K 线不足：需要 {required} 根，实际 {count} 根")

    def _update_decision_freshness(self) -> None:
        """共同 K 线过旧时暂停新决策，避免静默地以陈旧特征调仓。"""
        latest = self.candles[self.pairs[0]][-1]
        # Candle.ts 是该 1h K 线的开始时间；应从其收盘时刻计算可接受的延迟。
        lag = max(0.0, time.time() - (latest.ts + 3600))
        self._candle_lag_seconds = lag
        if lag > config.PREDICTIVE_MAX_CANDLE_LAG_SEC:
            if self._decision_pause_reason is None:
                self._decision_pause_reason = (
                    f"共同已收盘 K 线滞后 {lag / 3600:.2f}h，超过 "
                    f"{config.PREDICTIVE_MAX_CANDLE_LAG_SEC / 3600:.2f}h 阈值"
                )
                self._event(
                    "ERROR", "predictive_decision_paused",
                    f"预测调仓已暂停：{self._decision_pause_reason}",
                    detail={"latest_candle_ts": latest.ts, "lag_seconds": lag,
                            "max_lag_seconds": config.PREDICTIVE_MAX_CANDLE_LAG_SEC},
                )
            return
        if self._decision_pause_reason is not None:
            previous = self._decision_pause_reason
            self._decision_pause_reason = None
            self._event(
                "INFO", "predictive_decision_resumed",
                "预测 K 线已恢复新鲜，重新允许调仓",
                detail={"previous_reason": previous, "latest_candle_ts": latest.ts,
                        "lag_seconds": lag},
            )

    def _refresh_history(self, *, force: bool = False) -> bool:
        """低频合并最新已收盘 K 线；返回是否观察到新的共同 K 线。"""
        if not self.candles:
            raise RuntimeError("预测 K 线尚未初始化")
        now = time.time()
        if not force and now - self._last_history_refresh < config.PREDICTIVE_KLINE_REFRESH_SEC:
            self._update_decision_freshness()
            return False
        old_latest = self.candles[self.pairs[0]][-1].ts
        updates: dict[str, list[Candle]] = {}
        failures: dict[str, str] = {}
        for pair in self.pairs:
            # 留两个小时重叠，防止接口边界、延迟或去重造成新收盘 K 线缺失。
            start_ts = self.candles[pair][-1].ts - 2 * 3600
            try:
                updates[pair] = fetch_gate_candles(
                    pair, "1h", start_ts, int(now), client=self.client,
                )
            except Exception as error:
                failures[pair] = f"{type(error).__name__}: {error}"
        limit = config.PREDICTIVE_HISTORY_DAYS * 24 + 8
        merged = {}
        for pair in self.pairs:
            by_ts = {candle.ts: candle for candle in self.candles[pair]}
            by_ts.update({candle.ts: candle for candle in updates.get(pair, ())})
            merged[pair] = [by_ts[ts] for ts in sorted(by_ts)[-limit:]]
        self.candles = _align(merged, self.pairs)
        self._assert_history_ready()
        self._last_history_refresh = now
        self._update_decision_freshness()
        changed = self.candles[self.pairs[0]][-1].ts > old_latest
        if changed:
            self._save_history_cache()
        if failures:
            self._event(
                "WARNING", "predictive_history_partial",
                f"预测 K 线刷新部分失败：{', '.join(sorted(failures))}；保留旧数据并等待下次刷新",
                detail={"failures": failures, "latest_ts": self.candles[self.pairs[0]][-1].ts},
            )
        return changed

    def _bootstrap_market_data(self) -> None:
        """在引擎线程中完成缓存补齐和行情读取，避免构造函数阻塞 Web。"""
        if self.candles:
            self._refresh_history(force=True)
        else:
            self._load_initial_history()
        # 这是后台预热而不是 tick：给四并发行情请求足够时间完成两三批，避免
        # 网络正常但 RTT 略高于运行时 1 秒预算时反复预热失败。不会占用 ticker 锁。
        self.prices = _fetch_tickers_cached(
            None, self.pairs, initial_wait_sec=_TICKER_BOOTSTRAP_WAIT_SEC,
        )
        missing = [pair for pair in self.pairs if self.prices.get(pair, 0.0) <= 0]
        if missing:
            raise RuntimeError(f"未获取到预测币池行情: {', '.join(missing)}")
        self._price_observed_at = time.time()
        self._ready.set()
        self._initializing = False
        self._init_error = None
        self._event(
            "INFO", "predictive_ready",
            "预测行情预热完成，可开始前向模拟",
            detail={"cache_path": str(self._cache_path),
                    "latest_candle_ts": self.candles[self.pairs[0]][-1].ts},
        )

    # ------------------------------------------------------------------
    # 模型决策和虚拟成交
    # ------------------------------------------------------------------
    def _fit_and_score(
        self, *, emit_signal: bool = True, decision_audit: dict | None = None,
    ) -> tuple[tuple[str, ...], dict[str, float]]:
        count = len(self.candles[self.pairs[0]])
        index = count - 1
        current_candle = self.candles[self.pairs[0]][index]
        features = {pair: _feature_matrix(self.candles[pair]) for pair in self.pairs}
        labels = {
            pair: np.asarray([
                (self.candles[pair][i + self.settings.horizon_bars].close
                 / self.candles[pair][i].close - 1)
                if i + self.settings.horizon_bars < count else np.nan
                for i in range(count)
            ], dtype=float)
            for pair in self.pairs
        }
        if (self._model is None or current_candle.ts - self.last_refit_candle_ts
                >= self.settings.retrain_interval_bars * 3600):
            train_x, train_y = _training_set(features, labels, self.pairs, index, self.settings)
            self._model = _new_model(self.settings).fit(train_x, train_y)
            self.last_refit_candle_ts = current_candle.ts
            self._event(
                "INFO", "predictive_train",
                f"Ridge 滚动训练完成：{len(train_y)} 样本，训练窗 {self.settings.train_bars}h",
                detail={"samples": len(train_y), "candle_ts": current_candle.ts},
            )

        scores = {
            pair: float(self._model.predict(features[pair][index:index + 1])[0])
            for pair in self.pairs
            if np.isfinite(features[pair][index]).all()
        }
        risk_on = True
        if self.settings.market_ema_period:
            btc_closes = np.asarray([candle.close for candle in self.candles["BTC_USDT"]])
            risk_on = bool(
                btc_closes[-1] > _ema(btc_closes, self.settings.market_ema_period)[-1]
            )
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        target = (() if not risk_on else tuple(
            pair for pair, score in ranked if score >= self.settings.expected_return_threshold
        )[:self.settings.max_positions])
        if emit_signal:
            self._event(
                "INFO", "predictive_signal",
                "预测决策: " + (
                    f"目标 {','.join(target)}" if target else "空仓（预测不足或 BTC 风控）"
                ),
                detail={"scores": scores, "target": target, "risk_on": risk_on,
                        "threshold": self.settings.expected_return_threshold,
                        "candle_ts": current_candle.ts, "execution": decision_audit or {}},
            )
        return target, scores

    def _record_fill(self, fill: dict) -> None:
        self.store.record_trade(
            self.mode, fill["pair"], fill["side"], fill["price"], fill["amount"],
            fill["quote"], fill["profit"], fill.get("fee", 0.0),
        )
        side = "买入" if fill["side"] == "buy" else "卖出"
        self._event(
            "INFO", "predictive_fill",
            f"预测模拟{side}: {fill['pair']} @ {fill['price']:.8g} 数量 {fill['amount']:.8g} "
            f"金额 {fill['quote']:.4f}U，利润 {fill['profit']:.4f}U",
            pair=fill["pair"], detail=fill,
        )

    def _equity(self) -> float:
        return self.quote + sum(self.base[pair] * self.prices.get(pair, 0.0) for pair in self.pairs)

    def _rebalance(self, target: tuple[str, ...], decision_audit: dict | None = None) -> None:
        """按当前 ticker 虚拟成交。先卖后买，逐侧计手续费和保守滑点。"""
        decision_audit = decision_audit or {}
        equity = self._equity()
        target_value = equity / len(target) if target else 0.0
        slippage = self.settings.slippage_bps / 10_000
        fills = []
        # 先卖非目标仓位；目标仓超过等权权重时也降仓。
        for pair in self.pairs:
            mid = self.prices[pair]
            wanted_base = target_value / mid if pair in target else 0.0
            amount = max(0.0, self.base[pair] - wanted_base)
            if amount <= 1e-12:
                continue
            price = mid * (1 - slippage)
            gross = amount * price
            fee = gross * self.settings.fee_rate
            net = gross - fee
            cost = self.avg_cost[pair] or 0.0
            profit = net - amount * cost
            self.quote += net
            self.base[pair] -= amount
            if self.base[pair] <= 1e-12:
                self.base[pair] = 0.0
                self.avg_cost[pair] = None
            self.realized_profit[pair] += profit
            self.trade_count[pair] += 1
            self.total_fees += fee
            fills.append({"pair": pair, "side": "sell", "price": price, "amount": amount,
                          "quote": gross, "profit": profit, "fee": fee, "market_mid": mid,
                          **decision_audit})

        # 以买入有效价计算目标缺口；现金不足时按同一比例缩放，绝不借贷。
        costs: dict[str, float] = {}
        for pair in target:
            mid = self.prices[pair]
            wanted_base = target_value / mid
            needed = max(0.0, wanted_base - self.base[pair])
            costs[pair] = needed * mid * (1 + slippage) / (1 - self.settings.fee_rate)
        total_cost = sum(costs.values())
        scale = min(1.0, self.quote / total_cost) if total_cost > 0 else 0.0
        for pair, full_cost in costs.items():
            spend = full_cost * scale
            if spend <= 1e-12:
                continue
            price = self.prices[pair] * (1 + slippage)
            fee = spend * self.settings.fee_rate
            amount = (spend - fee) / price
            old_amount = self.base[pair]
            old_cost = self.avg_cost[pair] or 0.0
            self.quote -= spend
            self.base[pair] += amount
            self.avg_cost[pair] = (old_amount * old_cost + spend) / self.base[pair]
            self.trade_count[pair] += 1
            self.total_fees += fee
            fills.append({"pair": pair, "side": "buy", "price": price, "amount": amount,
                          "quote": spend, "profit": 0.0, "fee": fee, "market_mid": self.prices[pair],
                          **decision_audit})
        for fill in fills:
            self._record_fill(fill)
        self.target = target
        self._event(
            "INFO", "predictive_rebalance",
            f"预测调仓：{'、'.join(target) if target else '全部回到 USDT'}，"
            f"成交 {len(fills)} 笔，权益 {self._equity():.2f}U",
            detail={"target": target, "fills": fills, "equity": self._equity(),
                    "execution": decision_audit},
        )

    def _maybe_decide(self) -> None:
        if self._decision_pause_reason is not None:
            return
        latest_ts = self.candles[self.pairs[0]][-1].ts
        due = not self.last_decision_candle_ts or (
            latest_ts - self.last_decision_candle_ts
            >= self.settings.rebalance_interval_bars * 3600
        )
        # 模型对象不会持久化。重启后的首次 tick 只恢复训练和评分，不在既定
        # 决策日之前重复调仓，避免因为进程恢复产生额外的虚拟成交。
        if not due and self._model is None:
            _, scores = self._fit_and_score(emit_signal=False)
            self.predictions = scores
            self._event(
                "INFO", "predictive_score",
                "恢复后已重新训练并更新预测分数；下一次调仓仍按原计划执行",
                detail={"scores": scores, "candle_ts": latest_ts},
            )
            self._save_state()
            return
        if not due:
            return
        signal_candle = self.candles[self.pairs[0]][-1]
        decision_audit = {
            # 回测以次根开盘近似；前向纸盘以信号收盘后实际取得的 ticker 成交，
            # 两个时间点完整落库，后续可直接统计真实延迟和价格偏离。
            "signal_candle_ts": signal_candle.ts,
            "signal_close": signal_candle.close,
            "decision_ts": time.time(),
            "ticker_observed_at": self._price_observed_at,
            "price_source": "live_ticker_after_closed_candle",
            "slippage_bps": self.settings.slippage_bps,
        }
        target, scores = self._fit_and_score(decision_audit=decision_audit)
        self.predictions = scores
        self._rebalance(target, decision_audit)
        self.last_decision_candle_ts = latest_ts
        self._save_state()

    # ------------------------------------------------------------------
    # 引擎循环、控制与 Web 状态
    # ------------------------------------------------------------------
    def tick(self) -> None:
        # 先确认已收盘 K 线，再读取成交用 ticker，避免使用刷新历史之前的旧报价。
        self._refresh_history()
        if self._warm_next_tick:
            self._warm_next_tick = False
            self.prices = _fetch_tickers_cached(
                None, self.pairs, initial_wait_sec=_TICKER_BOOTSTRAP_WAIT_SEC)
        else:
            self.prices = _fetch_tickers_cached(None, self.pairs)
        self._price_observed_at = time.time()
        missing = [pair for pair in self.pairs if self.prices.get(pair, 0.0) <= 0]
        if missing:
            raise RuntimeError(f"未获取到预测币池行情: {', '.join(missing)}")
        self._maybe_decide()
        self.last_tick = time.time()
        self._save_state()
        if self.last_tick - self._last_snapshot >= 30:
            self._last_snapshot = self.last_tick
            self.store.record_equity(
                self._equity(), sum(self.realized_profit.values()),
                {pair: self.base[pair] * self.prices[pair] for pair in self.pairs},
            )
        self._maybe_health_check()

    def _maybe_health_check(self) -> None:
        now = time.time()
        if now - self._last_health < config.HEALTH_INTERVAL:
            return
        self._last_health = now
        message = (
            f"预测策略正常：权益 {self._equity():.2f}U，目标 {','.join(self.target) or '空仓'}，"
            f"成交 {sum(self.trade_count.values())} 笔"
        )
        log.info(message)
        self._event("INFO", "health", message)

    def run(self) -> None:
        log.info("预测模拟引擎启动：轮询 %ss，K 线刷新 %ss", config.TICK_INTERVAL,
                 config.PREDICTIVE_KLINE_REFRESH_SEC)
        self._event("INFO", "lifecycle", "预测模拟引擎线程启动")
        while not self._stop.is_set():
            if not self._ready.is_set():
                if time.time() < self._next_init_attempt:
                    self._stop.wait(min(config.PREDICTIVE_INIT_RETRY_SEC, 1.0))
                    continue
                try:
                    self._bootstrap_market_data()
                    self._last_success = time.time()
                except Exception as error:
                    self._init_error = f"{type(error).__name__}: {error}"
                    self.last_error = self._init_error
                    self._next_init_attempt = time.time() + config.PREDICTIVE_INIT_RETRY_SEC
                    log.exception("预测策略行情预热失败，将在 %.0f 秒后重试",
                                  config.PREDICTIVE_INIT_RETRY_SEC)
                    self._event(
                        "ERROR", "predictive_init_error",
                        f"预测行情预热失败，将自动重试：{self._init_error}",
                        detail={"traceback": traceback.format_exc(),
                                "retry_after_sec": config.PREDICTIVE_INIT_RETRY_SEC},
                    )
                continue
            if self._paused.is_set():
                self._stop.wait(config.TICK_INTERVAL)
                continue
            try:
                with self._tick_lock:  # 与 reset_paper 互斥
                    self.tick()
                self.last_error = None
                self._last_success = time.time()
                if self._api_outage:
                    self._api_outage = False
                    self._event("INFO", "api_recovered", "预测策略 API 已恢复")
            except Exception as error:
                self.last_error = f"{type(error).__name__}: {error}"
                log.exception("预测策略 tick 失败")
                self._event("ERROR", "tick_error", self.last_error,
                            detail={"traceback": traceback.format_exc()})
                if (self._last_success and time.time() - self._last_success
                        > config.API_OUTAGE_ALERT_SEC and not self._api_outage):
                    self._api_outage = True
                    self._event("ERROR", "api_outage", f"预测策略 API 持续中断：{self.last_error}")
            self._stop.wait(config.TICK_INTERVAL)

    def start_background(self) -> None:
        self._thread = threading.Thread(target=self.run, daemon=True, name="predictive-paper-engine")
        self._thread.start()

    def pause(self) -> str:
        if self._stopped:
            return "stopped"
        self._paused.set()
        self._event("INFO", "control", "用户暂停预测模拟策略")
        return "paused"

    def reset_paper(self, budget: float) -> str:
        """模拟盘仓位重置：清空持仓/预测/成交与权益历史，按给定 USDT 重新起步。

        事件日志保留（审计复盘用）；K 线与行情预热成果不受影响，重置后处于
        待命状态，需在面板点击"开始"才会产生新的调仓决策。
        """
        if not isinstance(budget, (int, float)) or isinstance(budget, bool):
            raise ValueError("重置金额必须是数字")
        if not (0 < float(budget) <= 1_000_000):
            raise ValueError("重置金额必须在 (0, 1000000] USDT 之间")
        budget = float(budget)
        self._paused.set()  # 先阻断新的决策
        with self._tick_lock:  # 等待进行中的 tick 完成
            self.quote = budget
            self.base = {pair: 0.0 for pair in self.pairs}
            self.avg_cost = {pair: None for pair in self.pairs}
            self.realized_profit = {pair: 0.0 for pair in self.pairs}
            self.trade_count = {pair: 0 for pair in self.pairs}
            self.total_fees = 0.0
            self.target = ()
            self.predictions = {}
            self.last_decision_candle_ts = 0
            self.last_refit_candle_ts = 0
            self._initial_total = budget
            self.store.clear_bot_states()
            self.store.clear_trades()
            self.store.clear_equity_snapshots()
            self._save_state()
        log.warning("预测模拟盘仓位已重置: %.2f USDT", budget)
        self._event("WARNING", "paper_reset",
                    f"模拟盘仓位已重置为 {budget:.2f} USDT，持仓/成交记录已清空，"
                    f"等待开始指令",
                    detail={"budget": budget})
        return self.run_status

    def resume(self) -> str:
        if self._stopped:
            return "stopped"
        self._paused.clear()
        self._warm_next_tick = True  # 暂停期间缓存已过期，首个 tick 走预热等待
        self._event("INFO", "control", "用户开始/恢复预测模拟策略")
        return self.run_status

    def start(self) -> str:
        if self._stopped:
            self._stop.clear()
            self._paused.clear()
            self._stopped = False
            self._warm_next_tick = True  # 停止期间缓存已过期，首个 tick 走预热等待
            self.start_background()
            self._event("INFO", "control", "用户重新启动预测模拟策略")
            return self.run_status
        return self.resume()

    def shutdown(self) -> str:
        with self._tick_lock:  # 与 reset_paper 互斥，避免重置后又把旧状态落盘
            self._save_state()
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._stopped = True
        self._event("INFO", "control", "用户停止预测模拟策略")
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
            log.exception("预测策略事件落库失败: %s", type)

    def state(self) -> dict[str, Any]:
        rows = {}
        for pair in self.pairs:
            price = self.prices.get(pair, 0.0)
            base = self.base[pair]
            cost = self.avg_cost[pair]
            unrealized = base * (price - cost) if price and cost is not None else 0.0
            rows[pair] = {
                "pair": pair, "price": price, "quote": 0.0, "base": base,
                "equity": base * price, "pnl": self.realized_profit[pair] + unrealized,
                "realized_profit": self.realized_profit[pair], "total_fees": 0.0,
                "trade_count": self.trade_count[pair], "blocked_count": 0,
                "orders": [], "lower": price * 0.9, "upper": price * 1.1,
                "signal": 0, "regime": "predictive", "frozen": False,
                "predicted_return": self.predictions.get(pair), "selected": pair in self.target,
            }
        # 将中心化 USDT 余额显式显示，不伪造为分散到十个币对的现金。
        rows["USDT"] = {
            "pair": "USDT", "price": 1.0, "quote": self.quote, "base": 0.0,
            "equity": self.quote, "pnl": 0.0, "realized_profit": 0.0,
            "total_fees": self.total_fees, "trade_count": 0, "blocked_count": 0,
            "orders": [], "lower": 0.99, "upper": 1.01, "signal": 0,
            "regime": "cash", "frozen": False, "predicted_return": None,
            "selected": not self.target,
        }
        equity = self._equity()
        return {
            "mode": self.mode, "strategy": self.profile.name,
            "strategy_label": self.profile.label, "strategy_kind": "predictive",
            "run_status": self.run_status,
            "circuit_breaker": {"global": False, "pairs": []},
            "api_outage": self._api_outage, "last_success": self._last_success,
            "started_at": self.started_at, "last_tick": self.last_tick,
            "last_error": self.last_error, "total_equity": equity,
            "total_initial_equity": self._initial_total,
            "total_pnl": equity - self._initial_total,
            "total_realized_profit": sum(self.realized_profit.values()),
            "total_fees": self.total_fees, "pairs": rows,
            "indicators": {}, "signal_filter": False,
            "predictive": {
                "model": "ridge", "target": list(self.target), "predictions": self.predictions,
                "horizon_hours": self.settings.horizon_bars,
                "rebalance_hours": self.settings.rebalance_interval_bars,
                "threshold": self.settings.expected_return_threshold,
                "slippage_bps": self.settings.slippage_bps,
                "last_decision_candle_ts": self.last_decision_candle_ts,
                "last_refit_candle_ts": self.last_refit_candle_ts,
                "ready": self._ready.is_set(), "initializing": self._initializing,
                "init_error": self._init_error, "cache_path": str(self._cache_path),
                "ticker_observed_at": self._price_observed_at,
                "decision_paused": self._decision_pause_reason is not None,
                "decision_pause_reason": self._decision_pause_reason,
                "candle_lag_seconds": self._candle_lag_seconds,
            },
            "recent_trades": self.store.recent_trades(50),
            "recent_events": self.store.recent_events(50),
            "equity_history": self.store.equity_history(300),
        }
