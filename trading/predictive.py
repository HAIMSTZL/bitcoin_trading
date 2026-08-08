"""多币种、滚动训练的 long/flat 现货预测策略回测。

这个模块刻意与实时下单引擎解耦：它从已经收盘的 OHLCV K 线构造特征，用过去
训练集拟合岭回归模型，预测未来 ``horizon_bars`` 的收益率；信号在下一根 K 线
开盘才成交。预测不足以覆盖成本时持有 USDT，因此适合“只有 USDT 起步”的约束。

没有把未来 K 线、当前盘口或事后选币混入训练：币池是调用时固定的静态列表，
每次重训时只使用标签已经兑现的样本。当前环境不依赖 sklearn/XGBoost；这里的
NumPy 岭回归是轻量且可复现的基线，特征和 walk-forward 框架可直接接入更复杂模型。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Sequence

import numpy as np

from gate_api import GatePublicClient

from . import config
from .backtest import Candle, fetch_gate_candles, interval_seconds


# 不按回测期最终表现挑选；这些是长期主流、高流动性现货的预先定义候选池。
DEFAULT_UNIVERSE = config.PREDICTIVE_PAIRS
_WARMUP_BARS = 72
ModelKind = Literal["ridge", "xgboost"]


@dataclass(frozen=True)
class PredictiveSettings:
    """预测策略参数。所有时间参数以 K 线根数为单位。"""

    pairs: tuple[str, ...] = DEFAULT_UNIVERSE
    # ridge 是轻量线性基线；xgboost 使用相同特征、标签与成交时序进行非线性比较。
    model: ModelKind = "ridge"
    total_quote_budget: float = config.TOTAL_QUOTE_BUDGET
    fee_rate: float = config.PAPER_FEE_RATE
    # 每一侧相对 K 线开盘价的不利滑点；3 bps 比只计手续费更保守。
    slippage_bps: float = 3.0
    # 标签为 close[t+horizon] / close[t] - 1。
    horizon_bars: int = 12
    # 每次仅使用最近这些 bar 的、已成熟标签训练；避免旧制度支配模型。
    train_bars: int = 24 * 120
    retrain_interval_bars: int = 24 * 7
    rebalance_interval_bars: int = 6
    ridge_alpha: float = 20.0
    # XGBoost 的默认值刻意偏保守：浅树、较大叶子样本、行列抽样与 L2 正则，
    # 以降低一年级别样本上记忆噪声的风险。
    xgb_estimators: int = 120
    xgb_max_depth: int = 2
    xgb_learning_rate: float = 0.03
    xgb_min_child_weight: float = 50.0
    xgb_subsample: float = 0.8
    xgb_colsample_bytree: float = 0.8
    # 模型预期收益必须超过此阈值才建仓；阈值应至少覆盖一次买入和潜在卖出成本。
    expected_return_threshold: float = 0.002
    max_positions: int = 1
    # 非零时，BTC 低于该 EMA 时组合强制空仓，作为系统性下跌保护。
    market_ema_period: int = 0
    report_period_bars: int = 24 * 30


@dataclass(frozen=True)
class OosPeriod:
    start_ts: int
    end_ts: int
    return_pct: float
    max_drawdown_pct: float

    def to_dict(self) -> dict:
        data = asdict(self)
        data["start"] = _format_ts(self.start_ts)
        data["end"] = _format_ts(self.end_ts)
        return data


@dataclass
class PredictiveResult:
    start_ts: int
    end_ts: int
    interval: str
    settings: PredictiveSettings
    initial_equity: float
    final_equity: float
    return_pct: float
    buy_hold_equity: float
    buy_hold_return_pct: float
    max_drawdown_pct: float
    total_fees: float
    total_turnover: float
    trade_count: int
    rebalance_count: int
    model_refit_count: int
    exposure_pct: float
    periods: list[OosPeriod]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["settings"]["pairs"] = list(self.settings.pairs)
        data["start"] = _format_ts(self.start_ts)
        data["end"] = _format_ts(self.end_ts)
        data["periods"] = [period.to_dict() for period in self.periods]
        return data


class RidgeReturnModel:
    """带标准化的岭回归，只预测条件期望收益，不拟合事后价格路径。"""

    def __init__(self, alpha: float) -> None:
        self.alpha = alpha
        self.mean: np.ndarray | None = None
        self.scale: np.ndarray | None = None
        self.coef: np.ndarray | None = None
        self.intercept = 0.0

    def fit(self, features: np.ndarray, labels: np.ndarray) -> "RidgeReturnModel":
        if features.ndim != 2 or labels.ndim != 1 or len(features) != len(labels):
            raise ValueError("训练特征与标签维度不匹配")
        if len(features) < features.shape[1] * 5:
            raise ValueError("训练样本不足以拟合预测模型")
        self.mean = features.mean(axis=0)
        self.scale = features.std(axis=0)
        self.scale = np.where(self.scale < 1e-12, 1.0, self.scale)
        normalized = (features - self.mean) / self.scale
        centered_labels = labels - labels.mean()
        penalty = np.eye(normalized.shape[1]) * self.alpha
        self.coef = np.linalg.solve(
            normalized.T @ normalized + penalty,
            normalized.T @ centered_labels,
        )
        self.intercept = float(labels.mean())
        return self

    def predict(self, features: np.ndarray) -> np.ndarray:
        if self.mean is None or self.scale is None or self.coef is None:
            raise ValueError("模型尚未训练")
        return (features - self.mean) / self.scale @ self.coef + self.intercept


class XGBoostReturnModel:
    """受限深度的 XGBoost 回归器。

    采用延迟导入：未选择该模型时仍可只使用 NumPy 运行 ridge 基线；选择时若依赖
    未正确安装，则给出可操作的错误，而不是悄悄退化为另一种模型。
    """

    def __init__(self, settings: PredictiveSettings) -> None:
        self.settings = settings
        self.model: object | None = None

    def fit(self, features: np.ndarray, labels: np.ndarray) -> "XGBoostReturnModel":
        if features.ndim != 2 or labels.ndim != 1 or len(features) != len(labels):
            raise ValueError("训练特征与标签维度不匹配")
        if len(features) < features.shape[1] * 10:
            raise ValueError("训练样本不足以拟合 XGBoost 模型")
        try:
            from xgboost import XGBRegressor
        except Exception as error:
            raise RuntimeError(
                "XGBoost 不可用；请安装 requirements.txt 中的 xgboost、scikit-learn，"
                "并在 macOS 上安装 libomp"
            ) from error
        self.model = XGBRegressor(
            objective="reg:squarederror",
            n_estimators=self.settings.xgb_estimators,
            max_depth=self.settings.xgb_max_depth,
            learning_rate=self.settings.xgb_learning_rate,
            min_child_weight=self.settings.xgb_min_child_weight,
            subsample=self.settings.xgb_subsample,
            colsample_bytree=self.settings.xgb_colsample_bytree,
            reg_alpha=0.1,
            reg_lambda=10.0,
            tree_method="hist",
            n_jobs=1,
            random_state=42,
            verbosity=0,
        )
        self.model.fit(features, labels)
        return self

    def predict(self, features: np.ndarray) -> np.ndarray:
        if self.model is None:
            raise ValueError("模型尚未训练")
        return np.asarray(self.model.predict(features), dtype=float)


def _new_model(settings: PredictiveSettings) -> RidgeReturnModel | XGBoostReturnModel:
    if settings.model == "ridge":
        return RidgeReturnModel(settings.ridge_alpha)
    if settings.model == "xgboost":
        return XGBoostReturnModel(settings)
    raise ValueError(f"未知预测模型 {settings.model!r}")


def _format_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def save_market_snapshot(path: str | Path, candles_by_pair: dict[str, Sequence[Candle]]) -> None:
    """原子保存公共 K 线快照，避免进程中断留下半写入缓存。"""
    payload = {
        pair: [asdict(candle) for candle in candles]
        for pair, candles in candles_by_pair.items()
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        # os.replace 成功后临时文件不存在；失败时也不残留损坏的候选缓存。
        temporary.unlink(missing_ok=True)


def load_market_snapshot(path: str | Path, pairs: Sequence[str]) -> dict[str, list[Candle]]:
    """读取 ``save_market_snapshot`` 生成的快照，拒绝缺失请求币对的缓存。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("K 线快照必须是按交易对组织的对象")
    output = {}
    for pair in pairs:
        rows = raw.get(pair)
        if not isinstance(rows, list):
            raise ValueError(f"快照缺少交易对 {pair}")
        try:
            output[pair] = [Candle(**row) for row in rows]
        except (TypeError, ValueError) as error:
            raise ValueError(f"快照中的 {pair} K 线格式无效") from error
    return output


def _max_drawdown_pct(curve: Sequence[float]) -> float:
    peak = -math.inf
    maximum = 0.0
    for equity in curve:
        peak = max(peak, equity)
        if peak > 0:
            maximum = max(maximum, (peak - equity) / peak * 100)
    return maximum


def _ema(values: np.ndarray, period: int) -> np.ndarray:
    output = np.empty_like(values)
    output[0] = values[0]
    alpha = 2.0 / (period + 1)
    for index in range(1, len(values)):
        output[index] = alpha * values[index] + (1 - alpha) * output[index - 1]
    return output


def _feature_matrix(candles: Sequence[Candle]) -> np.ndarray:
    """仅由当根及以前 OHLCV 构造 14 个特征，暖机前保持 NaN。"""
    closes = np.asarray([c.close for c in candles], dtype=float)
    highs = np.asarray([c.high for c in candles], dtype=float)
    lows = np.asarray([c.low for c in candles], dtype=float)
    opens = np.asarray([c.open for c in candles], dtype=float)
    volumes = np.asarray([c.volume for c in candles], dtype=float)
    if np.any(closes <= 0) or np.any(opens <= 0):
        raise ValueError("K 线价格必须为正")

    log_close = np.log(closes)
    returns = np.diff(log_close, prepend=np.nan)
    ema_fast = _ema(closes, 12)
    ema_slow = _ema(closes, 48)
    features = np.full((len(candles), 14), np.nan, dtype=float)
    for index in range(_WARMUP_BARS, len(candles)):
        gains = np.maximum(np.diff(closes[index - 14:index + 1]), 0.0)
        losses = np.maximum(-np.diff(closes[index - 14:index + 1]), 0.0)
        avg_loss = float(losses.mean())
        rsi = 100.0 if avg_loss == 0 else 100 - 100 / (1 + gains.mean() / avg_loss)
        # 成交量记录可能不可用（旧的 Candle / 少数交易对）；此时把量能特征中性化。
        volume_feature = 0.0
        recent_volume = volumes[index - 24:index + 1]
        if np.all(recent_volume > 0):
            log_volume = np.log(recent_volume)
            volume_std = log_volume[:-1].std()
            if volume_std > 1e-12:
                volume_feature = float((log_volume[-1] - log_volume[:-1].mean()) / volume_std)
        features[index] = (
            *(float(log_close[index] - log_close[index - lookback])
              for lookback in (1, 3, 6, 12, 24, 72)),
            float(returns[index - 5:index + 1].std()),
            float(returns[index - 23:index + 1].std()),
            float(closes[index] / ema_fast[index] - 1),
            float(closes[index] / ema_slow[index] - 1),
            (rsi - 50.0) / 50.0,
            float((highs[index] - lows[index]) / closes[index]),
            float((closes[index] - opens[index]) / opens[index]),
            volume_feature,
        )
    return features


def _align(candles_by_pair: dict[str, Sequence[Candle]], pairs: Sequence[str]) -> dict[str, list[Candle]]:
    series = {pair: list(candles_by_pair.get(pair, ())) for pair in pairs}
    missing = [pair for pair, candles in series.items() if not candles]
    if missing:
        raise ValueError(f"缺少 K 线: {', '.join(missing)}")
    common = {candle.ts for candle in series[pairs[0]]}
    for pair in pairs[1:]:
        common.intersection_update(candle.ts for candle in series[pair])
    if not common:
        raise ValueError("币对之间没有共同的 K 线时间点")
    timestamps = sorted(common)
    return {
        pair: [candle for candle in series[pair] if candle.ts in common]
        for pair in pairs
    }


def _validate(settings: PredictiveSettings) -> None:
    if len(settings.pairs) < 2:
        raise ValueError("预测组合至少需要两个预先确定的交易对")
    if settings.total_quote_budget <= 0 or settings.fee_rate < 0 or settings.slippage_bps < 0:
        raise ValueError("资金必须为正，手续费和滑点不能为负")
    if settings.horizon_bars < 1 or settings.train_bars < _WARMUP_BARS:
        raise ValueError("标签周期至少为 1，训练窗口不能短于特征暖机期")
    if settings.retrain_interval_bars < 1 or settings.rebalance_interval_bars < 1:
        raise ValueError("重训和调仓间隔至少为 1 根 K 线")
    if settings.model not in ("ridge", "xgboost"):
        raise ValueError("model 必须是 ridge 或 xgboost")
    if settings.ridge_alpha < 0 or settings.expected_return_threshold < 0:
        raise ValueError("正则系数和开仓阈值不能为负")
    if (settings.xgb_estimators < 1 or settings.xgb_max_depth < 1
            or settings.xgb_learning_rate <= 0 or settings.xgb_min_child_weight <= 0
            or not 0 < settings.xgb_subsample <= 1
            or not 0 < settings.xgb_colsample_bytree <= 1):
        raise ValueError("XGBoost 参数非法")
    if not 1 <= settings.max_positions <= len(settings.pairs):
        raise ValueError("max_positions 应在 1 到交易对数量之间")
    if settings.market_ema_period < 0 or settings.report_period_bars < 1:
        raise ValueError("EMA 周期不能为负，报告周期至少为 1")


def _training_set(
    features: dict[str, np.ndarray],
    labels: dict[str, np.ndarray],
    pairs: Sequence[str],
    now_index: int,
    settings: PredictiveSettings,
) -> tuple[np.ndarray, np.ndarray]:
    """取训练窗；最后一个标签在 ``now_index`` 时已经完全实现。"""
    last_label_index = now_index - settings.horizon_bars
    first_index = max(_WARMUP_BARS, last_label_index - settings.train_bars + 1)
    if last_label_index < first_index:
        return np.empty((0, 14)), np.empty(0)
    all_features = []
    all_labels = []
    for pair in pairs:
        x = features[pair][first_index:last_label_index + 1]
        y = labels[pair][first_index:last_label_index + 1]
        mask = np.isfinite(x).all(axis=1) & np.isfinite(y)
        all_features.append(x[mask])
        all_labels.append(y[mask])
    return np.vstack(all_features), np.concatenate(all_labels)


def _rebalance(
    quote: float,
    base: dict[str, float],
    target: tuple[str, ...],
    open_prices: dict[str, float],
    fee_rate: float,
    slippage_rate: float,
) -> tuple[float, dict[str, float], float, float, int]:
    """在下一根开盘按目标等权调仓，先卖后买并按每一侧计费/滑点。"""
    mid_equity = quote + sum(base[pair] * open_prices[pair] for pair in base)
    desired_value = mid_equity / len(target) if target else 0.0
    fees = 0.0
    turnover = 0.0
    trades = 0

    # 先将非目标仓位清掉；目标仓位若超过等权也降到目标权重。
    for pair, amount in list(base.items()):
        mid_price = open_prices[pair]
        wanted_base = desired_value / mid_price if pair in target else 0.0
        sell_amount = max(0.0, amount - wanted_base)
        if sell_amount <= 1e-14:
            continue
        gross = sell_amount * mid_price * (1 - slippage_rate)
        fee = gross * fee_rate
        quote += gross - fee
        base[pair] -= sell_amount
        fees += fee
        turnover += gross
        trades += 1

    # 根据买入后的有效价格计算每个目标缺口；若现金被费用耗尽则同比缩放。
    costs: dict[str, float] = {}
    for pair in target:
        buy_price = open_prices[pair] * (1 + slippage_rate)
        wanted_base = desired_value / open_prices[pair]
        needed = max(0.0, wanted_base - base[pair])
        costs[pair] = needed * buy_price / (1 - fee_rate) if fee_rate < 1 else math.inf
    total_cost = sum(costs.values())
    scale = min(1.0, quote / total_cost) if total_cost > 0 else 0.0
    for pair, cost in costs.items():
        spend = cost * scale
        if spend <= 1e-14:
            continue
        buy_price = open_prices[pair] * (1 + slippage_rate)
        fee = spend * fee_rate
        acquired = (spend - fee) / buy_price
        quote -= spend
        base[pair] += acquired
        fees += fee
        turnover += spend
        trades += 1
    return quote, base, fees, turnover, trades


def _periods(points: Sequence[tuple[int, float]], bars: int) -> list[OosPeriod]:
    periods = []
    for start in range(0, len(points) - 1, bars):
        segment = points[start:min(start + bars, len(points))]
        if len(segment) < 2:
            continue
        start_equity = segment[0][1]
        end_equity = segment[-1][1]
        periods.append(OosPeriod(
            start_ts=segment[0][0], end_ts=segment[-1][0],
            return_pct=(end_equity / start_equity - 1) * 100,
            max_drawdown_pct=_max_drawdown_pct([value for _, value in segment]),
        ))
    return periods


def run_predictive_backtest(
    candles_by_pair: dict[str, Sequence[Candle]],
    interval: str,
    settings: PredictiveSettings = PredictiveSettings(),
) -> PredictiveResult:
    """执行无前视、滚动重训的多币种 long/flat 回测。"""
    _validate(settings)
    aligned = _align(candles_by_pair, settings.pairs)
    count = len(next(iter(aligned.values())))
    required = _WARMUP_BARS + settings.train_bars + settings.horizon_bars + 2
    if count < required:
        raise ValueError(f"K 线不足：至少需要 {required} 根，实际只有 {count} 根")
    features = {pair: _feature_matrix(aligned[pair]) for pair in settings.pairs}
    labels = {
        pair: np.asarray([
            (aligned[pair][index + settings.horizon_bars].close / aligned[pair][index].close - 1)
            if index + settings.horizon_bars < count else np.nan
            for index in range(count)
        ], dtype=float)
        for pair in settings.pairs
    }
    market_ema = None
    if settings.market_ema_period:
        if "BTC_USDT" not in aligned:
            raise ValueError("启用市场 EMA 保护时，交易对必须包含 BTC_USDT")
        market_ema = _ema(
            np.asarray([candle.close for candle in aligned["BTC_USDT"]], dtype=float),
            settings.market_ema_period,
        )

    test_start = _WARMUP_BARS + settings.train_bars + settings.horizon_bars - 1
    quote = settings.total_quote_budget
    base = {pair: 0.0 for pair in settings.pairs}
    model: RidgeReturnModel | XGBoostReturnModel | None = None
    target: tuple[str, ...] = ()
    total_fees = 0.0
    turnover = 0.0
    trades = 0
    rebalances = 0
    refits = 0
    invested_bars = 0
    points: list[tuple[int, float]] = [(aligned[settings.pairs[0]][test_start].ts, quote)]
    slippage_rate = settings.slippage_bps / 10_000

    for index in range(test_start, count - 1):
        # 收盘后才训练/预测，因而执行一定发生在后一根 K 线开盘。
        if model is None or (index - test_start) % settings.retrain_interval_bars == 0:
            train_x, train_y = _training_set(features, labels, settings.pairs, index, settings)
            model = _new_model(settings).fit(train_x, train_y)
            refits += 1
        if (index - test_start) % settings.rebalance_interval_bars == 0:
            permit_risk = (
                market_ema is None
                or aligned["BTC_USDT"][index].close > market_ema[index]
            )
            if permit_risk:
                scores = [
                    (pair, float(model.predict(features[pair][index:index + 1])[0]))
                    for pair in settings.pairs
                    if np.isfinite(features[pair][index]).all()
                ]
                scores.sort(key=lambda item: item[1], reverse=True)
                target = tuple(
                    pair for pair, score in scores
                    if score >= settings.expected_return_threshold
                )[:settings.max_positions]
            else:
                target = ()
            open_prices = {pair: aligned[pair][index + 1].open for pair in settings.pairs}
            quote, base, fees, gross, count_trades = _rebalance(
                quote, base, target, open_prices, settings.fee_rate, slippage_rate,
            )
            total_fees += fees
            turnover += gross
            trades += count_trades
            rebalances += 1

        closes = {pair: aligned[pair][index + 1].close for pair in settings.pairs}
        equity = quote + sum(base[pair] * closes[pair] for pair in settings.pairs)
        points.append((aligned[settings.pairs[0]][index + 1].ts, equity))
        if any(amount > 1e-14 for amount in base.values()):
            invested_bars += 1

    initial_prices = {pair: aligned[pair][test_start + 1].open for pair in settings.pairs}
    final_prices = {pair: aligned[pair][-1].close for pair in settings.pairs}
    buy_hold_equity = sum(
        settings.total_quote_budget / len(settings.pairs) * (1 - settings.fee_rate)
        / (initial_prices[pair] * (1 + slippage_rate)) * final_prices[pair]
        for pair in settings.pairs
    )
    final_equity = points[-1][1]
    return PredictiveResult(
        start_ts=points[0][0], end_ts=points[-1][0], interval=interval,
        settings=settings, initial_equity=settings.total_quote_budget,
        final_equity=final_equity,
        return_pct=(final_equity / settings.total_quote_budget - 1) * 100,
        buy_hold_equity=buy_hold_equity,
        buy_hold_return_pct=(buy_hold_equity / settings.total_quote_budget - 1) * 100,
        max_drawdown_pct=_max_drawdown_pct([value for _, value in points]),
        total_fees=total_fees, total_turnover=turnover, trade_count=trades,
        rebalance_count=rebalances, model_refit_count=refits,
        exposure_pct=invested_bars / max(1, len(points) - 1) * 100,
        periods=_periods(points, settings.report_period_bars),
    )


def _bars_from_hours(hours: float, seconds: int, label: str) -> int:
    bars = hours * 3600 / seconds
    if hours <= 0 or abs(round(bars) - bars) > 1e-9:
        raise ValueError(f"{label} 必须为当前 K 线周期的整数倍")
    return int(round(bars))


def _print_result(result: PredictiveResult) -> None:
    print(
        f"滚动预测 long/flat 回测 | {_format_ts(result.start_ts)} 至 "
        f"{_format_ts(result.end_ts)} | {result.interval}"
    )
    settings = result.settings
    print(
        f"币池: {', '.join(settings.pairs)} | 预测 {settings.horizon_bars} bar | "
        f"训练 {settings.train_bars} bar | 重训 {settings.retrain_interval_bars} bar | "
        f"调仓 {settings.rebalance_interval_bars} bar"
    )
    model_label = (
        f"ridge(alpha={settings.ridge_alpha:g})" if settings.model == "ridge" else
        "xgboost("
        f"trees={settings.xgb_estimators}, depth={settings.xgb_max_depth}, "
        f"lr={settings.xgb_learning_rate:g})"
    )
    print(
        f"阈值 {settings.expected_return_threshold:.2%} | 持仓数 {settings.max_positions} | "
        f"模型 {model_label} | BTC EMA 保护 "
        f"{settings.market_ema_period or '关'} | 滑点 {settings.slippage_bps:g} bps/侧"
    )
    print(
        f"策略: {result.final_equity:.2f} USDT ({result.return_pct:+.2f}%) | "
        f"等权持有币池: {result.buy_hold_equity:.2f} USDT ({result.buy_hold_return_pct:+.2f}%)"
    )
    print(
        f"最大回撤: {result.max_drawdown_pct:.2f}% | 敞口: {result.exposure_pct:.1f}% | "
        f"成交: {result.trade_count} | 调仓: {result.rebalance_count} | 重训: {result.model_refit_count} | "
        f"手续费: {result.total_fees:.4f} U | 换手: {result.total_turnover:.2f} U"
    )
    for period in result.periods:
        print(
            f"  {_format_ts(period.start_ts)[:10]} 至 {_format_ts(period.end_ts)[:10]}: "
            f"{period.return_pct:+.2f}% | 回撤 {period.max_drawdown_pct:.2f}%"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多币种滚动预测 long/flat K 线回测")
    parser.add_argument("--days", type=int, default=365, help="历史读取天数（1h 最多约 400 天）")
    parser.add_argument("--interval", default="1h", help="Gate K 线周期，例如 1h 或 4h")
    parser.add_argument("--pairs", default=",".join(DEFAULT_UNIVERSE), help="预先确定的、逗号分隔的币池")
    parser.add_argument("--model", choices=("ridge", "xgboost"), default="ridge")
    parser.add_argument("--budget", type=float, default=config.TOTAL_QUOTE_BUDGET)
    parser.add_argument("--train-days", type=float, default=120)
    parser.add_argument("--horizon-hours", type=float, default=12)
    parser.add_argument("--retrain-hours", type=float, default=168)
    parser.add_argument("--rebalance-hours", type=float, default=6)
    parser.add_argument("--threshold", type=float, default=0.002, help="预测收益开仓阈值，例如 .002")
    parser.add_argument("--max-positions", type=int, default=1)
    parser.add_argument("--ridge-alpha", type=float, default=20)
    parser.add_argument("--xgb-estimators", type=int, default=120)
    parser.add_argument("--xgb-max-depth", type=int, default=2)
    parser.add_argument("--xgb-learning-rate", type=float, default=0.03)
    parser.add_argument("--market-ema", type=int, default=0, help="BTC EMA 风控；0 关闭")
    parser.add_argument("--slippage-bps", type=float, default=3)
    parser.add_argument("--cache", help="K 线快照文件；不存在时下载并保存，存在时直接复用")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        seconds = interval_seconds(args.interval)
        pairs = tuple(pair.strip().upper() for pair in args.pairs.split(",") if pair.strip())
        settings = PredictiveSettings(
            pairs=pairs, model=args.model, total_quote_budget=args.budget, slippage_bps=args.slippage_bps,
            train_bars=_bars_from_hours(args.train_days * 24, seconds, "训练天数"),
            horizon_bars=_bars_from_hours(args.horizon_hours, seconds, "预测周期"),
            retrain_interval_bars=_bars_from_hours(args.retrain_hours, seconds, "重训周期"),
            rebalance_interval_bars=_bars_from_hours(args.rebalance_hours, seconds, "调仓周期"),
            expected_return_threshold=args.threshold, max_positions=args.max_positions,
            ridge_alpha=args.ridge_alpha, xgb_estimators=args.xgb_estimators,
            xgb_max_depth=args.xgb_max_depth, xgb_learning_rate=args.xgb_learning_rate,
            market_ema_period=args.market_ema,
        )
        _validate(settings)
    except ValueError as error:
        parser.error(str(error))
    end_ts = int(time.time())
    start_ts = end_ts - args.days * 24 * 60 * 60
    cache_path = Path(args.cache) if args.cache else None
    if cache_path and cache_path.exists():
        candles = load_market_snapshot(cache_path, settings.pairs)
    else:
        client = GatePublicClient(timeout=20.0)
        try:
            candles = {
                pair: fetch_gate_candles(pair, args.interval, start_ts, end_ts, client=client)
                for pair in settings.pairs
            }
        finally:
            client.close()
        if cache_path:
            save_market_snapshot(cache_path, candles)
    result = run_predictive_backtest(candles, args.interval, settings)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
