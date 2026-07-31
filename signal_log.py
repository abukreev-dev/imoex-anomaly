#!/usr/bin/env python3
"""SQLite-лог отправленных алертов intraday-монитора + forward returns.

Каждый алерт (volume/block/spike) пишется в БД в момент отправки в Telegram.
На последующих тиках дозаполняются цены через +5 / +15 / +60 минут и цена
закрытия сессии (при уходе в ночной сон). Это позволяет потом честно
посчитать, отрабатывают ли сигналы — без ручного разбора чата.
"""

import os
import sqlite3
from datetime import datetime
from typing import Dict, Optional

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "signal_log.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    kind TEXT NOT NULL,
    ticker TEXT NOT NULL,
    shortname TEXT,
    price_at_alert REAL,
    metric_name TEXT,
    metric_value REAL,
    direction TEXT,
    market_change_pct REAL,
    price_5m REAL,
    price_15m REAL,
    price_60m REAL,
    price_eod REAL
);
CREATE INDEX IF NOT EXISTS idx_signal_log_open
    ON signal_log (trade_date, price_60m);
"""


def _get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    conn = _get_conn()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def log_signal(
    kind: str,
    ticker: str,
    shortname: str,
    price: Optional[float],
    metric_name: str,
    metric_value: float,
    direction: str,
    market_change_pct: Optional[float],
    now: datetime,
) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            """
            INSERT INTO signal_log
                (created_at, trade_date, kind, ticker, shortname, price_at_alert,
                 metric_name, metric_value, direction, market_change_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now.isoformat(),
                now.strftime("%Y-%m-%d"),
                kind,
                ticker,
                shortname,
                price,
                metric_name,
                metric_value,
                direction,
                market_change_pct,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_forward_returns(current_prices: Dict[str, float], now: datetime) -> None:
    """Дозаполнить price_5m/15m/60m для сегодняшних записей, у которых пора."""
    today = now.strftime("%Y-%m-%d")
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, ticker, created_at, price_5m, price_15m, price_60m
            FROM signal_log
            WHERE trade_date = ? AND (price_5m IS NULL OR price_15m IS NULL OR price_60m IS NULL)
            """,
            (today,),
        )
        rows = cur.fetchall()
        for row_id, ticker, created_at_str, p5, p15, p60 in rows:
            price = current_prices.get(ticker)
            if price is None:
                continue
            created_at = datetime.fromisoformat(created_at_str)
            elapsed_min = (now - created_at).total_seconds() / 60
            updates = {}
            if p5 is None and elapsed_min >= 5:
                updates["price_5m"] = price
            if p15 is None and elapsed_min >= 15:
                updates["price_15m"] = price
            if p60 is None and elapsed_min >= 60:
                updates["price_60m"] = price
            if updates:
                set_clause = ", ".join(f"{k} = ?" for k in updates)
                cur.execute(
                    f"UPDATE signal_log SET {set_clause} WHERE id = ?",
                    (*updates.values(), row_id),
                )
        conn.commit()
    finally:
        conn.close()


def finalize_eod(current_prices: Dict[str, float], now: datetime) -> None:
    """Проставить price_eod всем сегодняшним записям перед уходом в ночной сон."""
    today = now.strftime("%Y-%m-%d")
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, ticker FROM signal_log WHERE trade_date = ? AND price_eod IS NULL",
            (today,),
        )
        rows = cur.fetchall()
        for row_id, ticker in rows:
            price = current_prices.get(ticker)
            if price is None:
                continue
            cur.execute("UPDATE signal_log SET price_eod = ? WHERE id = ?", (price, row_id))
        conn.commit()
    finally:
        conn.close()
