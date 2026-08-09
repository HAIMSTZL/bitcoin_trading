"""SQLite 持久化：成交记录与权益曲线快照。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Optional

from . import config


class Store:
    def __init__(self, db_path: str = config.DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts      REAL NOT NULL,
                    mode    TEXT NOT NULL,
                    pair    TEXT NOT NULL,
                    side    TEXT NOT NULL,
                    price   REAL NOT NULL,
                    amount  REAL NOT NULL,
                    quote   REAL NOT NULL,
                    profit  REAL NOT NULL DEFAULT 0,
                    fee     REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts       REAL NOT NULL,
                    equity   REAL NOT NULL,
                    realized REAL NOT NULL,
                    detail   TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS bot_state (
                    pair    TEXT PRIMARY KEY,
                    updated REAL NOT NULL,
                    data    TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts      REAL NOT NULL,
                    level   TEXT NOT NULL,
                    type    TEXT NOT NULL,
                    pair    TEXT,
                    message TEXT NOT NULL,
                    detail  TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS meta (
                    k TEXT PRIMARY KEY,
                    v TEXT NOT NULL
                );
                """
            )
            # 旧库没有逐笔手续费字段。网格成交的 quote 是买入总支出或卖出净收入，
            # 因此 |price * amount - quote| 可无损还原该笔手续费；新成交直接落原始 fee。
            columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(trades)")
            }
            if "fee" not in columns:
                self._conn.execute(
                    "ALTER TABLE trades ADD COLUMN fee REAL NOT NULL DEFAULT 0"
                )
                self._conn.execute(
                    "UPDATE trades SET fee=ABS(price * amount - quote) WHERE fee=0"
                )
                # 历史权益曲线同样曾写入“当前在场币对”的局部利润；按成交时间重算，
                # 让曲线与新的全周期口径连续可审计。
                self._conn.execute(
                    "UPDATE equity_snapshots AS snapshots SET realized=COALESCE("
                    "(SELECT SUM(trades.profit) FROM trades WHERE trades.ts <= snapshots.ts), 0)"
                )

    def get_meta(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO meta(k, v) VALUES (?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))

    # ------------------------------------------------------------------
    # 事件日志（挂单/成交/控制/异常，供复盘）
    # ------------------------------------------------------------------
    def record_event(
        self,
        level: str,
        type: str,
        message: str,
        pair: str | None = None,
        detail: dict | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO events(ts, level, type, pair, message, detail)"
                " VALUES (?,?,?,?,?,?)",
                (time.time(), level, type, pair, message, json.dumps(detail or {})),
            )

    def recent_events(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 网格机器人状态（模拟盘重启后恢复用）
    # ------------------------------------------------------------------
    def save_bot_state(self, pair: str, data: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO bot_state(pair, updated, data) VALUES (?,?,?) "
                "ON CONFLICT(pair) DO UPDATE SET updated=excluded.updated, data=excluded.data",
                (pair, time.time(), json.dumps(data)),
            )

    def load_bot_states(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT pair, data FROM bot_state").fetchall()
        return {r["pair"]: json.loads(r["data"]) for r in rows}

    def delete_bot_state(self, pair: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM bot_state WHERE pair=?", (pair,))

    def clear_bot_states(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM bot_state")

    def clear_trades(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM trades")

    def clear_equity_snapshots(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM equity_snapshots")

    def delete_meta(self, key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM meta WHERE k=?", (key,))

    def record_trade(
        self,
        mode: str,
        pair: str,
        side: str,
        price: float,
        amount: float,
        quote: float,
        profit: float = 0.0,
        fee: float = 0.0,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO trades(ts, mode, pair, side, price, amount, quote, profit, fee)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (time.time(), mode, pair, side, price, amount, quote, profit, fee),
            )

    def trade_summary(self) -> dict[str, float | int]:
        """全周期成交账本汇总；不依赖当前仍在场的策略槽位。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS trade_count, COALESCE(SUM(profit), 0) AS realized_profit, "
                "COALESCE(SUM(fee), 0) AS total_fees FROM trades"
            ).fetchone()
        return {
            "trade_count": int(row["trade_count"]),
            "realized_profit": float(row["realized_profit"]),
            "total_fees": float(row["total_fees"]),
        }

    def record_equity(self, equity: float, realized: float, detail: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO equity_snapshots(ts, equity, realized, detail) VALUES (?,?,?,?)",
                (time.time(), equity, realized, json.dumps(detail)),
            )

    def recent_trades(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def equity_history(self, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, equity, realized FROM equity_snapshots ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
