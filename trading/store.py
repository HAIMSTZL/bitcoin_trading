"""SQLite 持久化：成交记录与权益曲线快照。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

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
                    profit  REAL NOT NULL DEFAULT 0
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
                """
            )

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

    def clear_bot_states(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM bot_state")

    def record_trade(
        self,
        mode: str,
        pair: str,
        side: str,
        price: float,
        amount: float,
        quote: float,
        profit: float = 0.0,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO trades(ts, mode, pair, side, price, amount, quote, profit)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (time.time(), mode, pair, side, price, amount, quote, profit),
            )

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
