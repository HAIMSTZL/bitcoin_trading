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

策略定义在 `trading/profiles.py`，可自行增删改。

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
