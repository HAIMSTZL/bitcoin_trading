# Gate API v4 Python 封装

对 [Gate API v4](https://www.gate.com/docs/developers/apiv4/en/) 的轻量 Python 封装，
覆盖现货、永续合约、交割合约、钱包、子账户/托管、交易机器人模块。

- 以只读查询接口为主；唯一的写接口是 `SpotAPI.create_order()`（供交易系统实盘
  模式使用，受币对白名单与单笔限额约束）；
- 密钥只从环境变量 `MY_GATE_KEY` / `MY_GATE_SECRET` 读取，代码中无明文密钥；
- 仅依赖 `requests`。

## 目录结构

```
gate_api/                # 封装包
  client.py              # 签名 HTTP 客户端
  spot.py                # 现货
  futures.py             # 永续合约
  delivery.py            # 交割合约
  wallet.py              # 钱包
  subaccount.py          # 子账户/托管
  trading_bot.py         # 交易机器人
trading/                 # 自动化交易系统（现货网格）
  config.py              # 币对白名单 / 模式 / 网格参数 / 风控
  grid.py                # 网格策略 + 模拟账户
  engine.py              # 行情轮询引擎（paper 撮合 / live 执行器）
  store.py               # SQLite 成交与权益持久化
  web/                   # FastAPI + WebSocket 实时面板
run.py                   # 系统入口
scripts/
  check_availability.py  # 只读接口可用性测试
docs/
  API.md                 # 接口说明文档
requirements.txt
```

## 自动化交易系统

仅允许 `BTC_USDT`、`DOGE_USDT`、`ETH_USDT` 三个现货交易对（代码级硬白名单），
网格策略低买高卖，盈亏以 USDT 为口径统计。

```bash
# 模拟盘（默认，真实行情 + 虚拟资金撮合，不发生真实交易）
.venv/bin/python run.py
# 打开 http://127.0.0.1:8000 查看实时面板

# 只跑部分策略（默认全部启用）
STRATEGIES=classic,rotation .venv/bin/python run.py

# 实盘（双重确认，真实下单；live 执行路径未经真实下单测试，启用前请小额验证）
TRADING_MODE=live LIVE_TRADING_CONFIRM=YES_I_ACCEPT_RISK .venv/bin/python run.py
```

**多策略 A/B 对照**（`feature/coin-rotation` 分支起）：多个策略实例同时运行，
面板顶部选项卡切换查看；各策略独立虚拟账户、独立数据库
（`trading/data/trading_<name>.db`，rotation 沿用主库 `trading.db`）：

| 策略 | 说明 |
|------|------|
| `classic` 经典网格 | 对照组：固定三币对、固定区间、均分、不换币 |
| `rotation` 筛选轮换 | 完整版：信号过滤 + 自适应区间 + 动态分配 + 空仓换币 |
| `aggressive` 激进轮动 | 无信号过滤双向硬跑 + 自适应区间 + 换币 |
| `hunter` 猎手精选 | 启动即全市场筛选 Top3 建仓，无信号过滤激进风格 |
| `predictive` 预测轮动（研究） | 独立 paper-only：1h K 线滚动 Ridge、纯 USDT 起步、long/flat、每日决策 |

策略定义在 `trading/profiles.py`，可自行增删改。

`predictive` 使用十个预先固定的高流动性币对，120 天训练窗、48 小时收益预测、每周重训、
BTC 168h EMA 风控、最多三币等权。模拟盘默认采用每侧 **10 bps** 滑点（比研究默认更保守），
它只会写入独立的 `trading/data/trading_predictive.db`，并且代码级禁止在 `live` 模式运行。首次
启动需要下载约 150 天 1h K 线；完成后它会显示在 Web 顶部的“预测轮动（研究）”Tab，点击该
Tab 的开始按钮才产生前向模拟交易。

- 网格参数（区间幅度、层数）在 `trading/config.py` 中调整；
- **启动流程**：服务启动后只做环境初始化，处于待命状态，需在面板点击 **开始** 才正式交易：
  - 模拟盘：自动恢复上次落盘的虚拟账户与网格挂单状态（每个 tick 及停止时保存到
    `trading/data/trading.db` 的 `bot_state` 表），无缝续跑；首次运行（无存档）时按
    `PAPER_MIRROR_REAL=true` 镜像真实现货账户建仓；
  - 实盘：每次启动重新读取真实账户状态；
  - 想清空模拟盘重新来过：删掉 `trading/data/trading.db` 即可；
- 面板右上角有 **开始 / 暂停 / 停止** 按钮（对应 `POST /api/control`）：
  暂停只挂起交易循环；停止会终止引擎但保留 Web 服务，之后可再点开始恢复；
  彻底退出进程请在终端 Ctrl+C；
- 成交记录与权益曲线持久化在 `trading/data/trading.db`。
- **日志（供复盘）**：
  - 结构化事件入库 `trading.db` 的 `events` 表——挂单、成交、网格初始化、启动/暂停/停止
    控制、异常（含堆栈）全覆盖，面板底部"运行日志"区块实时展示；
  - 文本日志同时写控制台和 `log/` 目录（Logger + 每日 0 点滚动，历史文件按日期命名
    如 `trading.log.2026-08-04`，保留 30 天；每行含 时间|级别|文件:代码行|模块|内容）；
  - 所有异常（tick 循环、实盘下单、控制接口、引擎初始化）均已捕获并记录，不会静默失败。
- 实盘风控：币对白名单强制校验 + 单笔 USDT 上限（`MAX_ORDER_QUOTE`，默认 50）。

### K 线级回测

`trading.backtest` 复用生产 `GridBot` 的网格、手续费、成本、止损和自动重心逻辑，
从 Gate 公共接口读取已收盘 K 线，因此不需要 API Key。它评估的是不依赖盘口/逐笔成交
信号的经典 long-only 网格基线；`rotation` / `hunter` 的实时筛选与盘口信号不能由 OHLC
数据忠实回放。

```bash
# 最近 90 天，15 分钟 K 线；输出策略、买入持有、回撤、手续费及逐币对结果
.venv/bin/python -m trading.backtest --days 90 --interval 15m

# 以仅收盘价成交作为更保守的敏感性检查
.venv/bin/python -m trading.backtest --days 90 --interval 15m --path close

# 消融：关闭限时止损、关闭自动重心、加宽全部币对网格 50%
.venv/bin/python -m trading.backtest --days 90 --stoploss-hours 0
.venv/bin/python -m trading.backtest --days 90 --no-recenter
.venv/bin/python -m trading.backtest --days 90 --range-scale 1.5

# 研究双向库存网格：50% 初始基础币、9 层宽网格；跌破下界时撤买单
.venv/bin/python -m trading.backtest --days 90 --initial-base-fraction 0.5 \
  --range-scale 2.5 --grids 9 --no-recenter --downside-freeze
```

默认 `directional` 路径使用阳线 `O→L→H→C`、阴线 `O→H→L→C` 近似 K 线内触价顺序；
它不是逐笔成交回放。应将其和 `--path close` 结果一起看，不能把单一路径结果视为实盘收益预测。
关闭自动重心意味着价格跑出网格后不再按新价格继续布网，可能因显著降低风险暴露而得到更好
的回测数值；它是不同的风控策略，不应直接与持续运行网格等同。
`--downside-freeze` 会在价格跌破网格下界时撤掉全部买单，并在价格重新回到区间后才补挂；
它适合检验“避免下跌追价”这一风控假设。初始基础币比例用于模拟常规双向网格，默认仍为
当前策略的纯 USDT 起步。所有新参数只用于回测研究，尚未改变实盘引擎的默认行为。

### 多币种滚动预测回测（研究候选）

`trading.predictive` 是与实盘引擎隔离的 second research track：它预先固定一组高流动性
现货币对，使用已经收盘的 OHLCV 构造收益率、波动、EMA 偏离、RSI、K 线形态与相对成交量
特征，以过去 120 天、标签已经兑现的数据滚动训练正则化回归模型，预测未来 12 小时收益。
信号只在**下一根 K 线开盘**成交，初始状态完全是 USDT；预测没有覆盖成本就保持空仓。每侧
默认还扣除 0.1% 手续费和 3 bps 滑点。

```bash
# 首次下载固定的 10 币种、一年 1h K 线并保存快照；之后同一命令复用快照，
# 方便让参数比较严格发生在相同历史样本上。
.venv/bin/python -m trading.predictive --days 365 --interval 1h \
  --cache /tmp/bitcoin_trading_1h_snapshot.json

# 更低频、提高成本门槛的候选；--market-ema 168 表示 BTC 低于 7 天 EMA 时空仓。
.venv/bin/python -m trading.predictive --cache /tmp/bitcoin_trading_1h_snapshot.json \
  --rebalance-hours 24 --threshold 0.008 --market-ema 168

# 与 Ridge 完全同口径的 XGBoost 对照（研究用途；当前验证结果未通过）
.venv/bin/python -m trading.predictive --model xgboost \
  --cache /tmp/bitcoin_trading_1h_snapshot.json
```

这不是 XGBoost 的替代承诺：当前只引入 NumPy，先得到可审计且无前视的基线；若它通过跨阶段
样本外验证，才值得用完全相同的特征/标签/执行时序增加 XGBoost 或时序模型作增量比较。不得
根据单一总收益选择参数：至少检查每 30 天分段收益、最大回撤、换手和滑点后的结果。该模块尚未
连接 `Engine`，更不应据此开启实盘。

已完成的探索结果、被否决的配置及实盘前门槛见 [docs/STRATEGY_RESEARCH.md](docs/STRATEGY_RESEARCH.md)。

## 使用

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt

export MY_GATE_KEY="..."     # 切勿写入代码或提交
export MY_GATE_SECRET="..."

# 可选: 网络必须走系统代理才能访问 Gate 时设置（默认直连，更快更稳）
export GATE_USE_PROXY=true

.venv/bin/python scripts/check_availability.py
```

```python
from gate_api import GateClient, SpotAPI, FuturesAPI

client = GateClient()
print(SpotAPI(client).list_accounts())
print(FuturesAPI(client, settle="usdt").list_positions())
```

详细接口清单见 [docs/API.md](docs/API.md)，网格交易系统技术方案见 [docs/GRID_TRADING.md](docs/GRID_TRADING.md)。
