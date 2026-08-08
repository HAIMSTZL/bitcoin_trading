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
import queue
import threading
import time
import traceback
from typing import Any, Optional

from gate_api import GateClient, GatePublicClient, SpotAPI

from . import config
from . import screener
from .grid import GridBot, PaperAccount
from .indicators import IndicatorEngine, atr_percent
from .store import Store

log = logging.getLogger("trading.engine")

# 多策略引擎共享的按币对行情缓存。Gate 的全市场 ticker 响应较大，不能每个
# tick 下载全部交易对；这里仅请求策略实际需要的币池，并防止多策略重复拉取同一币。
_TICKER_CACHE: dict = {"data": {}, "updated": {}, "failures": {}, "inflight": {}}
# 刷新周期至少覆盖一个 tick：请求完成后，下一个 tick 通常直接复用缓存，避免每轮
# 都请求全币池；异步刷新期间仍可用上一帧报价撮合。
_TICKER_TTL = max(3.0, config.TICK_INTERVAL)
_TICKER_LOCK = threading.Lock()
# ticker 是可很快再次获取的非关键读；不能让单次网络抖动占满并发槽 80 秒。
# 与会下单/读账户的客户端分开，使用短超时、一次重试的刷新预算。
_TICKER_WORKER_COUNT = 4
_TICKER_REQUEST_TIMEOUT_SEC = 5.0
_TICKER_REQUEST_RETRIES = 1
_TICKER_INITIAL_WAIT_SEC = 1.0
_TICKER_BOOTSTRAP_WAIT_SEC = 8.0
_TICKER_MAX_STALE_SEC = 30.0
_TICKER_FAILURE_LIMIT = 3
_TICKER_SLOW_REFRESH_SEC = 5.0
_TICKER_TASKS: queue.Queue = queue.Queue()
_TICKER_WORKERS: list[threading.Thread] = []
_TICKER_WORKERS_LOCK = threading.Lock()


def _new_ticker_spot() -> SpotAPI:
    """为一个长寿命行情 worker 创建独立、可复用连接的公开客户端。"""
    return SpotAPI(GatePublicClient(
        timeout=_TICKER_REQUEST_TIMEOUT_SEC,
        retries=_TICKER_REQUEST_RETRIES,
    ))


def _parse_ticker_price(pair: str, rows) -> float:
    if not rows:
        raise RuntimeError(f"ticker 响应为空: {pair}")
    try:
        row = rows[0]
        if row.get("currency_pair") not in (None, pair):
            raise RuntimeError(f"ticker 响应币对不匹配: 期望 {pair}")
        return float(row["last"])
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f"ticker 响应无有效价格: {pair}") from error


def _refresh_ticker_pair(pair: str, spot: SpotAPI, done: threading.Event) -> None:
    """后台刷新一个币对；网络 I/O 永远不占用 ``_TICKER_LOCK``。"""
    started = time.monotonic()
    try:
        rows = spot.list_tickers(pair)
        price = _parse_ticker_price(pair, rows)
    except Exception as error:
        with _TICKER_LOCK:
            cache = _TICKER_CACHE
            failures = cache.setdefault("failures", {})
            failures[pair] = int(failures.get(pair, 0)) + 1
            cache.setdefault("inflight", {}).pop(pair, None)
            attempts = failures[pair]
        elapsed = time.monotonic() - started
        log.warning("ticker 刷新失败: %s（连续 %d 次，耗时 %.2fs）：%s",
                    pair, attempts, elapsed, error)
    else:
        with _TICKER_LOCK:
            cache = _TICKER_CACHE
            cache.setdefault("data", {})[pair] = price
            cache.setdefault("updated", {})[pair] = time.time()
            cache.setdefault("failures", {}).pop(pair, None)
            cache.setdefault("inflight", {}).pop(pair, None)
        elapsed = time.monotonic() - started
        if elapsed >= _TICKER_SLOW_REFRESH_SEC:
            log.warning("ticker 刷新偏慢: %s 耗时 %.2fs", pair, elapsed)
    finally:
        done.set()


def _ticker_worker(worker_number: int) -> None:
    """长寿命 worker：串行消费任务，并复用本线程的 HTTP Session/TLS 连接。"""
    worker_spot = _new_ticker_spot()
    while True:
        pair, requested_spot, done = _TICKER_TASKS.get()
        try:
            # 显式 spot 仅用于测试；生产任务统一使用该 worker 的连接池。
            _refresh_ticker_pair(pair, requested_spot or worker_spot, done)
        except Exception:
            # _refresh_ticker_pair 已自行记录普通网络错误。此处仅防御不可预期的
            # worker 异常，保证任务事件不会永久卡住。
            log.exception("ticker worker %d 执行任务时发生未处理异常: %s", worker_number, pair)
            with _TICKER_LOCK:
                _TICKER_CACHE.setdefault("inflight", {}).pop(pair, None)
            done.set()
        finally:
            _TICKER_TASKS.task_done()


def _ensure_ticker_workers() -> None:
    """按需启动固定数量的长寿命 worker；不在 ticker 缓存锁内执行。"""
    with _TICKER_WORKERS_LOCK:
        _TICKER_WORKERS[:] = [worker for worker in _TICKER_WORKERS if worker.is_alive()]
        while len(_TICKER_WORKERS) < _TICKER_WORKER_COUNT:
            worker_number = len(_TICKER_WORKERS) + 1
            worker = threading.Thread(
                target=_ticker_worker, args=(worker_number,), daemon=True,
                name=f"ticker-worker-{worker_number}",
            )
            worker.start()
            _TICKER_WORKERS.append(worker)


def _enqueue_ticker_refresh(pair: str, spot: SpotAPI | None, done: threading.Event) -> None:
    """投递刷新任务；worker 池本身限制所有生产网络并发为四。"""
    _ensure_ticker_workers()
    _TICKER_TASKS.put_nowait((pair, spot, done))


def _clear_inflight(pair: str, done: threading.Event) -> None:
    """投递失败时回滚登记，避免一个永不触发的 Event 永久阻塞该币对。"""
    with _TICKER_LOCK:
        inflight = _TICKER_CACHE.setdefault("inflight", {})
        if inflight.get(pair) is done:
            inflight.pop(pair, None)
    done.set()


def _schedule_ticker_refresh(pair: str, spot: SpotAPI | None) -> tuple[float | None, threading.Event | None]:
    """锁内只检查/登记缓存；真正的请求由后台线程在锁外完成。"""
    now = time.time()
    with _TICKER_LOCK:
        cache = _TICKER_CACHE
        data = cache.setdefault("data", {})
        updated = cache.setdefault("updated", {})
        current = data.get(pair)
        if current is not None and now - float(updated.get(pair, 0.0)) < _TICKER_TTL:
            return current, None
        inflight = cache.setdefault("inflight", {})
        done = inflight.get(pair)
        if done is not None:
            return current, done
        done = threading.Event()
        inflight[pair] = done
    try:
        _enqueue_ticker_refresh(pair, spot, done)
    except Exception:
        _clear_inflight(pair, done)
        raise
    return current, done


def _cached_ticker_or_error(pair: str) -> float | None:
    """允许有限时效的上一帧价格；连续失败或明显过期后升级为 tick 错误。"""
    with _TICKER_LOCK:
        cache = _TICKER_CACHE
        value = cache.setdefault("data", {}).get(pair)
        if value is None:
            return None
        age = time.time() - float(cache.setdefault("updated", {}).get(pair, 0.0))
        failures = int(cache.setdefault("failures", {}).get(pair, 0))
    if age > _TICKER_MAX_STALE_SEC or failures >= _TICKER_FAILURE_LIMIT:
        return None
    return value


def _fetch_tickers_cached(
    spot: SpotAPI | None, pairs, *, initial_wait_sec: float = _TICKER_INITIAL_WAIT_SEC,
) -> dict[str, float]:
    """只读缓存并安排后台 ticker 刷新，不让行情网络阻塞策略 tick。

    Gate 的 ``/spot/tickers`` 仅支持单个 ``currency_pair`` 或全市场响应，并不支持
    多币对参数。全市场响应在本地实测约 0.5MB，若按 3 秒轮询会产生不必要的大流量
    和 ReadTimeout 风险。因此每个失效币对只发一个小请求。缓存锁只保护数据结构；
    网络请求由最多四个后台线程执行；策略 tick 首次无缓存默认只等待一秒。后台
    初始化调用者可显式提供较长窗口，但等待不占用缓存锁，也不阻塞 Web 服务。
    ``spot`` 仅供测试/显式注入，生产传 ``None``。
    """
    requested = tuple(sorted(set(pairs)))
    pending: dict[str, threading.Event] = {}
    values: dict[str, float] = {}
    for pair in requested:
        value, done = _schedule_ticker_refresh(pair, spot)
        if value is not None:
            values[pair] = value
        elif done is not None:
            pending[pair] = done

    # 首次启动没有上一帧价格时，只在一个共享截止时间内等后台请求，绝不逐币串行等待。
    deadline = time.monotonic() + max(0.0, initial_wait_sec)
    for done in pending.values():
        done.wait(max(0.0, deadline - time.monotonic()))

    missing = []
    for pair in requested:
        value = _cached_ticker_or_error(pair)
        if value is None:
            missing.append(pair)
        else:
            values[pair] = value
    if missing:
        raise RuntimeError(
            f"ticker 无可用价格（预热中、连续失败或缓存过期）: {', '.join(missing)}"
        )
    return {pair: values[pair] for pair in requested}


class LiveExecutor:
    """实盘执行器：把网格成交信号转换为真实市价单。

    安全约束：
    - 只允许白名单内的现货交易对（引擎运行时币对，补位替换后自动更新）；
    - 单笔 quote 金额不超过 config.MAX_ORDER_QUOTE；
    - 下单金额按交易对规则精度取整，低于最小限额则跳过。
    """

    def __init__(self, spot: SpotAPI, pairs: list[str]):
        self._spot = spot
        self._rules = {p: spot.get_currency_pair(p) for p in pairs}

    def allow_pair(self, pair: str) -> None:
        """补位新币对加入白名单。"""
        self._rules[pair] = self._spot.get_currency_pair(pair)

    def execute(self, fill: dict) -> None:
        pair = fill["pair"]
        if pair not in self._rules:
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
            side, amount = "buy", f"{quote:.2f}"
        else:
            amount_f = fill["amount"]
            min_base = float(rules.get("min_base_amount") or 0)
            if amount_f < min_base:
                log.warning("卖单数量 %.8f 低于最小限额 %.8f，跳过", amount_f, min_base)
                return
            prec = int(rules.get("amount_precision") or 8)
            side, amount = "sell", f"{amount_f:.{prec}f}"

        log.info("实盘下单: %s %s %s", pair, side, amount)
        self._spot.create_order(pair, side, amount, order_type="market",
                                time_in_force="ioc")


class Engine:
    def __init__(self, profile=None) -> None:
        if config.TRADING_MODE == "live" and config.LIVE_CONFIRM != "YES_I_ACCEPT_RISK":
            raise RuntimeError(
                "实盘模式需要双重确认：TRADING_MODE=live 且 "
                "LIVE_TRADING_CONFIRM=YES_I_ACCEPT_RISK"
            )
        # 策略档案：不传则用 config 的默认完整配置（筛选轮换版）
        if profile is None:
            from .profiles import PROFILES
            profile = PROFILES["rotation"]
        self.profile = profile
        self.mode = config.TRADING_MODE
        self.client = GateClient(timeout=20.0)  # 引擎用更长超时，容忍网络抖动
        self.spot = SpotAPI(self.client)
        # 模拟/实盘数据库严格隔离，报表不混模式
        db_path = profile.db_path
        if config.TRADING_MODE == "live":
            import os as _os
            db_path = _os.path.join(config.DATA_DIR, f"live_{profile.name}.db")
        self.store = Store(db_path)
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
        self._last_snapshot = 0.0  # 权益快照按时间驱动（每30秒），而非 tick 计数
        self._last_health = 0.0  # 首次 tick 即报一条健康状态，之后按间隔
        self.indicators = IndicatorEngine(
            self.spot, interval=config.INDICATOR_KLINE,
            ob_th=config.DEPTH_RATIO_THRESHOLD, tr_th=config.TRADE_RATIO_THRESHOLD,
        )
        self._last_indicator = 0.0
        self._last_signals: dict[str, int] = {}  # 各币对上一次【已确认】信号
        self._signal_buf: dict[str, list[int]] = {}  # 确认缓冲（按需初始化）
        self._last_flip: dict[str, float] = {}  # 各币对上次信号翻转时间（冷却用）
        self._regimes: dict[str, str] = {}  # 各币对行情状态 ranging/trend_up/trend_down
        self._last_rebalance = time.time()  # 启动后过完整周期才首次再平衡
        self._init_atr: dict[str, float] = {}  # 建仓时的 ATR%（自适应区间兜底）
        # 运行时交易序列（槽位制）：初始为 PAIRS，补位替换时更新
        self.pairs: list[str] = list(profile.pairs)
        self._last_screen = 0.0  # 上次空仓筛选时刻
        # 熔断状态
        self._price_hist: dict[str, list[tuple[float, float]]] = {}
        self._cb_pairs: dict[str, float] = {}   # 被熔断的币对 -> 熔断时刻
        self._cb_low: dict[str, list[float]] = {}  # 币对 -> [最低价, 最近创新低时间]
        self._cb_global = False  # 大盘熔断（BTC 触发，全系统停止撮合）
        # API 中断告警状态
        self._last_success: Optional[float] = None  # 最近一次成功 tick 时间
        self._api_outage = False  # 是否处于持续中断告警状态
        # 行情预热不能阻塞 Web 服务，更不能因短暂网络故障让 run.py 退出。
        # 由引擎线程在后台完成，并按 ENGINE_INIT_RETRY_SEC 自动重试。
        self._ready = threading.Event()
        self._initializing = True
        self._init_error: Optional[str] = None
        self._next_init_attempt = 0.0
        # 组合总盈亏基准：首次成功建仓时固定并持久化；预热阶段尚无仓位时用 0 展示。
        ie = self.store.get_meta("initial_equity")
        self._initial_total = float(ie) if ie is not None else 0.0
        self._initial_equity_persisted = ie is not None
        # 默认待命：服务启动后只初始化环境，等用户在 Web 面板点击"开始"才正式交易
        self._paused.set()

    # ------------------------------------------------------------------
    def _fetch_prices(
        self, *, initial_wait_sec: float = _TICKER_INITIAL_WAIT_SEC,
    ) -> dict[str, float]:
        """按币对分别查询 ticker（响应小、抗超时；全市场单次拉取响应数 MB 易超时）。

        BTC 作为大盘熔断基准，无论是否在交易序列中都必须拉取（P1 修复：
        补位换币可能把 BTC 换出序列，不能让大盘熔断失明）。
        多引擎共享 _TICKER_CACHE，同一批行情供所有策略。
        """
        return _fetch_tickers_cached(
            None, set(self.pairs) | {"BTC_USDT"}, initial_wait_sec=initial_wait_sec,
        )

    def _initialize(self) -> None:
        """在引擎线程中完成首次建仓，失败可安全重试。

        这里刻意不在构造函数中访问行情：冷启动时 ticker 尚未到达只是“预热中”，
        不是 Web 服务应该退出的致命错误。每次重试从空的内存仓位开始，避免半初始化
        状态被下一次尝试复用。
        """
        self.account = PaperAccount()
        self.bots = {}
        self.prices = {}
        self.executor = None
        self.pairs = list(self.profile.pairs)
        self._init_atr = {}
        self._init_bots()
        if self.mode == "live":
            self.executor = LiveExecutor(self.spot, self.pairs)
        if not self._initial_equity_persisted:
            self._initial_total = sum(
                self.account.get(p)["quote"]
                + self.account.get(p)["base"] * self.prices.get(p, 0.0)
                for p in self.pairs
            )
            self.store.set_meta("initial_equity", str(self._initial_total))
            self._initial_equity_persisted = True
        self._init_error = None
        self.last_error = None
        self._initializing = False
        self._ready.set()
        log.info("引擎行情预热完成，等待交易控制指令")
        self._event("INFO", "engine_ready", "行情预热完成，网格已就绪")

    def _real_spot_balances(self) -> dict[str, float]:
        return {a["currency"]: float(a["available"]) for a in self.spot.list_accounts()}

    def _init_bots(self) -> None:
        """初始化环境。

        - 模拟盘：优先恢复上次落盘的虚拟账户与网格状态（含补位后的币对序列），
          继续交易；无存档时按 PAPER_MIRROR_REAL 镜像真实现货账户（或虚拟预算）新建。
        - 实盘：每次启动重新读取真实账户状态建仓。
        """
        if self.mode == "paper":
            saved = self.store.load_bot_states()
            if saved:
                self.pairs = list(saved.keys())  # 运行时币对 = 存档币对
                self.prices = self._fetch_prices(initial_wait_sec=_TICKER_BOOTSTRAP_WAIT_SEC)
                sig_now = self._config_sig()
                for pair in self.pairs:
                    d = saved[pair]
                    if d.get("config_sig") == sig_now.get(pair):
                        bot = GridBot.from_dict(d, self.account)
                        bot.on_order = self._on_order_placed
                        bot.fee_rate = config.PAPER_FEE_RATE
                        self.bots[pair] = bot
                    else:
                        # 参数已变更：保留余额与利润，按新参数重建
                        self.account.init_pair(pair, d["quote"], d["base"])
                        bot = self._build_bot(pair, self.prices[pair],
                                              d["quote"], d["base"])
                        bot.realized_profit = d.get("realized_profit", 0.0)
                        bot.trade_count = d.get("trade_count", 0)
                        bot.blocked_count = d.get("blocked_count", 0)
                        bot.total_fees = d.get("total_fees", 0.0)
                        bot.ever_held = d.get("ever_held", False)
                        bot.avg_cost = d.get("avg_cost")
                        self._drop_below_cost_sells(bot)
                        self.bots[pair] = bot
                log.info(
                    "已恢复上次模拟盘状态: %s",
                    {p: {"quote": round(self.account.get(p)["quote"], 4),
                         "base": round(self.account.get(p)["base"], 4),
                         "orders": len(self.bots[p].orders)} for p in self.pairs},
                )
                self._event("INFO", "lifecycle", "恢复上次模拟盘存档继续交易",
                            detail={p: {"quote": self.account.get(p)["quote"],
                                        "base": self.account.get(p)["base"],
                                        "orders": len(self.bots[p].orders)}
                                    for p in self.pairs})
                return
            log.info("未找到模拟盘存档，按初始仓位新建网格")
            self._event("INFO", "lifecycle", "未找到模拟盘存档，按初始仓位新建网格")

        # 猎手模式：启动即全市场筛选 Top-N 建仓（忽略默认币对）
        if self.mode == "paper" and self.profile.auto_screen:
            try:
                cands = screener.screen_top(self.spot, exclude=set(), n=len(self.pairs))
            except Exception as e:
                log.exception("启动筛选失败，使用默认币对")
                self._event("ERROR", "screen_error",
                            f"启动筛选失败: {type(e).__name__}: {e}，使用默认币对")
                cands = []
            if cands:
                self.pairs = [c["pair"] for c in cands]
                log.warning("猎手建仓: 筛选结果 %s",
                            [(c["pair"], c["score"]) for c in cands])
                self._event(
                    "WARNING", "slot_replace",
                    "启动筛选建仓: " + ", ".join(
                        f"{c['pair']}({c['score']}分)" for c in cands),
                    detail={"candidates": cands},
                )
            else:
                log.warning("启动筛选无合格候选，使用默认币对 %s", self.pairs)
                self._event("INFO", "screen_none",
                            "启动筛选无合格候选，使用默认币对建仓")

        self.prices = self._fetch_prices(initial_wait_sec=_TICKER_BOOTSTRAP_WAIT_SEC)
        budgets = self._initial_budgets()
        for pair in self.pairs:
            price = self.prices.get(pair)
            if not price:
                raise RuntimeError(f"未获取到 {pair} 行情")
            quote_budget, base_budget = budgets[pair]
            self.account.init_pair(pair, quote_budget, base_budget)
            self.bots[pair] = self._build_bot(pair, price, quote_budget, base_budget)

    def _config_sig(self) -> dict[str, str]:
        """各币对网格参数签名，用于检测配置变更后重建。"""
        mode = "dyn" if self.profile.dynamic_allocation else "eq"
        geo = "g" if config.GRID_GEOMETRIC else "a"
        sig = {}
        for p in self.pairs:
            cfg = config.GRID_CONFIG.get(p, config.GRID_DEFAULT)
            sig[p] = (f'{cfg["range_pct"]}:{cfg["grids"]}'
                      f':{config.TOTAL_QUOTE_BUDGET}:{mode}:{geo}')
        return sig

    def _desired_range_pct(self, pair: str) -> float:
        """当前应有的网格区间幅度：ADAPTIVE_RANGE 开启时随 ATR% 自适应。"""
        cfg = config.GRID_CONFIG.get(pair, config.GRID_DEFAULT)
        if not self.profile.adaptive_range:
            return cfg["range_pct"]
        atr = self.indicators.get(pair).get("atr_pct", 0.0) or self._init_atr.get(pair, 0.0)
        if atr <= 0:
            return cfg["range_pct"]
        return min(max(config.ADAPTIVE_RANGE_MULT * atr / 100,
                       config.RANGE_PCT_MIN), config.RANGE_PCT_MAX)

    def _build_bot(
        self, pair: str, price: float, quote_budget: float, base_budget: float
    ) -> GridBot:
        cfg = config.GRID_CONFIG.get(pair, config.GRID_DEFAULT)
        range_pct = self._desired_range_pct(pair)
        grids = cfg["grids"]
        # 费用守卫：单格间距必须显著大于双边手续费，否则加宽区间
        min_range = 2.2 * config.PAPER_FEE_RATE * (grids - 1) / 2
        if range_pct < min_range:
            log.warning("%s 区间 ±%.2f%% 过窄（单格利润率低于费用门槛），加宽至 ±%.2f%%",
                        pair, range_pct * 100, min_range * 100)
            range_pct = min_range
        lower = price * (1 - range_pct)
        upper = price * (1 + range_pct)
        bot = GridBot(pair, lower, upper, grids, quote_budget, base_budget,
                      fee_rate=config.PAPER_FEE_RATE,
                      geometric=config.GRID_GEOMETRIC)
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

    @staticmethod
    def _drop_below_cost_sells(bot) -> None:
        """重建后摘除低于持仓成本的卖单（不亏卖，等价格回到成本上方）。"""
        if bot.avg_cost is not None:
            bot.orders = {
                i: o for i, o in bot.orders.items()
                if not (o["side"] == "sell" and o["price"] <= bot.avg_cost)
            }

    def _recenter(self, pair: str, price: float) -> None:
        """价格跑出网格区间：以当前价为中心重建网格，保留余额与利润累计。"""
        old = self.bots[pair]
        bal = self.account.get(pair)
        bot = self._build_bot(pair, price, bal["quote"], bal["base"])
        bot.realized_profit = old.realized_profit
        bot.trade_count = old.trade_count
        bot.blocked_count = old.blocked_count
        bot.total_fees = old.total_fees
        bot.avg_cost = old.avg_cost
        self._drop_below_cost_sells(bot)
        self.bots[pair] = bot
        log.warning("%s 价格 %.6g 跑出区间 [%.6g, %.6g]，网格已重建", pair, price, old.lower, old.upper)
        self._event(
            "WARNING", "grid_recenter",
            f"价格 {price:.6g} 跑出区间 [{old.lower:.6g}, {old.upper:.6g}]，以现价为中心重建网格",
            pair=pair,
            detail={"old_lower": old.lower, "old_upper": old.upper, "price": price},
        )

    def _initial_budgets(self) -> dict[str, tuple[float, float]]:
        # 实盘必须读真实账户（P0 修复）：PAPER_MIRROR_REAL 只约束模拟盘。
        if self.mode == "live" or config.PAPER_MIRROR_REAL:
            # 模拟盘仓位完全镜像真实现货账户：各基础币按实际可用余额，
            # USDT 按各币对持仓价值比例分配（无持仓则均分）。
            real = self._real_spot_balances()
            usdt = real.get("USDT", 0.0)
            bases = {p: real.get(p.split("_")[0], 0.0) for p in self.pairs}
            total_base_val = sum(
                bases[p] * self.prices.get(p, 0.0) for p in self.pairs
            )
            budgets = {}
            for pair in self.pairs:
                if total_base_val > 0:
                    weight = bases[pair] * self.prices[pair] / total_base_val
                else:
                    weight = 1.0 / len(self.pairs)
                budgets[pair] = (usdt * weight, bases[pair])
            log.info("镜像真实账户: USDT=%s, 持仓=%s", usdt, bases)
            return budgets
        return {
            p: (self._allocate_quotes().get(p, 0.0),
                float(config.GRID_CONFIG.get(p, config.GRID_DEFAULT)["base_budget"]))
            for p in self.pairs
        }

    def _allocate_quotes(self) -> dict[str, float]:
        """把 TOTAL_QUOTE_BUDGET 按近期波动率（ATR%）动态分配到各币对。

        网格策略赚的是波动的钱：ATR% 越高权重越大；
        ALLOC_MIN_W/MAX_W 限制单币对占比。失败时回退均分。
        """
        n = len(self.pairs)
        total = config.TOTAL_QUOTE_BUDGET
        if not self.profile.dynamic_allocation:
            return {p: total / n for p in self.pairs}
        try:
            vols = {}
            for pair in self.pairs:
                candles = self.spot.list_candlesticks(pair, config.INDICATOR_KLINE, 40)
                vols[pair] = atr_percent(candles)
            if sum(vols.values()) <= 0:
                raise ValueError("ATR 全为 0")
            self._init_atr = vols  # 供自适应区间在建仓时使用
            weights = self._apply_weight_caps(
                {p: v / sum(vols.values()) for p, v in vols.items()})
            alloc = {p: total * weights[p] for p in self.pairs}
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
            return {p: total / n for p in self.pairs}

    @staticmethod
    def _apply_weight_caps(weights: dict[str, float]) -> dict[str, float]:
        """权重夹紧到 [ALLOC_MIN_W, ALLOC_MAX_W] 后按剩余比例再分配。"""
        for _ in range(2):
            fixed = {p: w for p, w in weights.items()
                     if w <= config.ALLOC_MIN_W or w >= config.ALLOC_MAX_W}
            free = [p for p in weights if p not in fixed]
            if not free:
                break
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
        return weights

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
        sweep = "（清仓扫尾）" if fill.get("sweep") else ""
        if fill.get("stoploss"):
            sweep = "（水下超时止损）"
        self._event(
            "INFO", "order_filled",
            f"{side}成交{sweep} @ {fill['price']:.8g} 数量 {fill['amount']:.8g} "
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
        self._maybe_fill_slot()  # 空仓槽位补位筛选
        self._save_bot_states()  # 模拟盘每个 tick 落盘，保证重启后可续跑

        # 权益快照按时间驱动（30 秒一条），tick 卡顿也不丢曲线
        if self.last_tick - self._last_snapshot >= 30:
            self._last_snapshot = self.last_tick
            state = self.state()
            self.store.record_equity(
                state["total_equity"], state["total_realized_profit"],
                {p: s["equity"] for p, s in state["pairs"].items()},
            )
        self._maybe_health_check()

    # ------------------------------------------------------------------
    # 槽位补位：空仓币对触发全市场筛选，最优合格候选替换
    # ------------------------------------------------------------------
    def _flat_pairs(self) -> list[str]:
        """绝对空仓的槽位：曾持仓、当前 base=0 且无卖单。"""
        flat = []
        for pair, bot in self.bots.items():
            bal = self.account.get(pair)
            has_sell = any(o["side"] == "sell" for o in bot.orders.values())
            if bot.ever_held and bal["base"] <= 0 and not has_sell:
                flat.append(pair)
        return flat

    def _maybe_fill_slot(self) -> None:
        if self.mode != "paper":
            return  # 实盘换币需人工确认，暂不自动补位
        if not self.profile.slot_rotation:
            return  # 该策略未启用换币（对照组）
        flat = self._flat_pairs()
        if not flat:
            return
        now = time.time()
        if now - self._last_screen < config.SCREEN_INTERVAL:
            return
        self._last_screen = now
        log.info("槽位空仓: %s，启动全市场筛选", flat)
        self._event("INFO", "screen_start", f"槽位空仓: {','.join(flat)}，开始全市场筛选")
        try:
            best = screener.screen(self.spot, exclude=set(self.pairs))
        except Exception as e:
            log.exception("筛选失败")
            self._event("ERROR", "screen_error", f"筛选失败: {type(e).__name__}: {e}")
            # 异常失败（网络等）10 分钟后重试，不等完整周期
            self._last_screen = now - config.SCREEN_INTERVAL + 600
            return
        if not best:
            log.info("未筛到合格候选，维持现状（%s 继续）", flat[0])
            self._event("INFO", "screen_none",
                        f"未筛到合格候选（及格线 {config.SCREEN_MIN_SCORE} 分），维持 {flat[0]} 继续交易")
            return
        self._replace_pair(flat[0], best)

    def _replace_pair(self, old: str, cand: dict) -> None:
        """用候选币对替换空仓槽位：转移 USDT 子弹，建仓新网格。"""
        new = cand["pair"]
        tickers = self.spot.list_tickers(new)
        if not tickers:
            log.error("补位失败：无法获取 %s 行情", new)
            return
        price = float(tickers[0]["last"])

        bal = self.account.get(old)
        freed_quote = bal["quote"]
        log.warning("槽位替换: %s -> %s (得分 %.1f, 转移 %.2fU)",
                    old, new, cand["score"], freed_quote)

        # 旧槽位退役：撤掉虚拟挂单，余额清零
        del self.bots[old]
        self.account.balances.pop(old, None)
        self.store.delete_bot_state(old)
        self.pairs[self.pairs.index(old)] = new

        # 新币对建仓
        self.account.init_pair(new, freed_quote, 0.0)
        self.prices[new] = price
        self.bots[new] = self._build_bot(new, price, freed_quote, 0.0)
        if self.executor:
            self.executor.allow_pair(new)

        self._event(
            "WARNING", "slot_replace",
            f"槽位替换: {old} → {new} · 得分 {cand['score']} · "
            f"ATR {cand['atr_pct']}% · 点差 {cand['spread_pct']}% · "
            f"深度 {cand['depth_usdt']:.0f}U · 转移子弹 {freed_quote:.2f}U",
            pair=new, detail=cand,
        )
        self._save_bot_states()

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
            hist = self._price_hist.setdefault(pair, [])
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
        self.indicators.update(self.pairs)
        for pair in self.pairs:
            ind = self.indicators.get(pair)
            raw_sig = ind["signal"] if self.profile.use_signal_filter else 0
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
        buf = self._signal_buf.setdefault(pair, [])
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

        # 权重 = ATR% × (1 + 倾斜系数×【已确认】信号)，再经过占比夹紧
        raw = {}
        for pair in self.pairs:
            ind = self.indicators.get(pair)
            atr = max(ind.get("atr_pct", 0.0), 1e-6)
            sig = self._last_signals.get(pair, 0) if self.profile.use_signal_filter else 0
            raw[pair] = atr * (1 + config.REBALANCE_SIGNAL_TILT * sig)
        total_raw = sum(raw.values())
        weights = self._apply_weight_caps(
            {p: v / total_raw for p, v in raw.items()})

        # 子弹池 = 各币对 USDT 总额（买单是虚拟挂单，重切子弹零成本）
        pool = sum(self.account.get(p)["quote"] for p in self.pairs)
        if pool < 3:
            return  # 没有值得挪动的子弹

        deltas = {
            p: pool * weights[p] - self.account.get(p)["quote"]
            for p in self.pairs
        }
        if max(abs(d) for d in deltas.values()) < max(1.0, pool * config.REBALANCE_MIN_DRIFT):
            log.info("再平衡检查: 偏离不足阈值，不动作 (池子 %.2fU, 权重 %s)",
                     pool, {p: round(w, 2) for p, w in weights.items()})
            return

        # 执行：重设各币对 USDT = 池子×权重（总额守恒），并按新预算重建买单侧
        # 同步记账资金调拨（capital_adjust），避免盈亏基准被调拨污染
        for pair in self.pairs:
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
                bot.avg_cost = old.avg_cost
                self._drop_below_cost_sells(bot)
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
            if not self._ready.is_set():
                now = time.monotonic()
                if now >= self._next_init_attempt:
                    try:
                        self._initialize()
                    except Exception as e:
                        self._init_error = f"{type(e).__name__}: {e}"
                        self.last_error = self._init_error
                        self._next_init_attempt = now + config.ENGINE_INIT_RETRY_SEC
                        log.exception("引擎行情预热失败，将在 %.0f 秒后重试",
                                      config.ENGINE_INIT_RETRY_SEC)
                        self._event(
                            "ERROR", "engine_init_error",
                            f"行情预热失败，将自动重试: {self._init_error}",
                            detail={"traceback": traceback.format_exc()},
                        )
                # 不使用一次长 sleep，保证 shutdown/control 的响应速度。
                wait_for = max(0.0, self._next_init_attempt - time.monotonic())
                self._stop.wait(min(config.TICK_INTERVAL, wait_for or config.TICK_INTERVAL))
                continue
            if self._paused.is_set():
                self._stop.wait(config.TICK_INTERVAL)
                continue
            try:
                self.tick()
                self.last_error = None
                self._last_success = time.time()
                if self._api_outage:
                    self._api_outage = False
                    log.warning("API 连接已恢复")
                    self._event("INFO", "api_recovered", "API 连接已恢复，交易继续")
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("tick 失败")
                self._event("ERROR", "tick_error", self.last_error,
                            detail={"traceback": traceback.format_exc()})
                # 持续中断超过阈值 → 升级告警（只报一次，恢复时再报）
                if (self._last_success
                        and time.time() - self._last_success > config.API_OUTAGE_ALERT_SEC
                        and not self._api_outage):
                    self._api_outage = True
                    log.error("API 持续中断超过 %ds", config.API_OUTAGE_ALERT_SEC)
                    self._event(
                        "ERROR", "api_outage",
                        f"API 持续中断超过 {config.API_OUTAGE_ALERT_SEC:.0f} 秒！"
                        f"最近错误: {self.last_error}。系统将持续重试，恢复后自动继续。",
                        detail={"last_error": self.last_error},
                    )
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
        return self.run_status

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
            return self.run_status
        return self.resume()

    @property
    def run_status(self) -> str:
        if self._stopped:
            return "stopped"
        if not self._ready.is_set():
            return "initializing"
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
        for pair, bot in list(self.bots.items()):  # 快照遍历，防引擎线程并发修改
            s = bot.state(self.prices.get(pair, bot.start_price or 0), self.account)
            s["frozen"] = self._cb_global or pair in self._cb_pairs
            pairs[pair] = s
        total_equity = sum(s["equity"] for s in pairs.values())
        total_initial = self._initial_total  # 固定基准（建仓时落库），不受重建/调拨影响
        return {
            "mode": self.mode,
            "strategy": self.profile.name,
            "strategy_label": self.profile.label,
            "run_status": self.run_status,
            "initializing": not self._ready.is_set(),
            "initialization_error": self._init_error,
            "circuit_breaker": {
                "global": self._cb_global,
                "pairs": sorted(self._cb_pairs.keys()),
            },
            "api_outage": self._api_outage,
            "last_success": self._last_success,
            "started_at": self.started_at,
            "last_tick": self.last_tick,
            "last_error": self.last_error,
            "total_equity": total_equity,
            "total_initial_equity": total_initial,
            "total_pnl": total_equity - total_initial,
            "total_realized_profit": sum(s["realized_profit"] for s in pairs.values()),
            "total_fees": sum(s["total_fees"] for s in pairs.values()),
            "pairs": pairs,
            "indicators": {p: self.indicators.get(p) for p in self.pairs},
            "signal_filter": self.profile.use_signal_filter,
            "recent_trades": self.store.recent_trades(50),
            "recent_events": self.store.recent_events(50),
            "equity_history": self.store.equity_history(300),
        }
