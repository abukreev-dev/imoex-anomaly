#!/usr/bin/env python3
"""Профиль объёма по времени дня: сколько бумага ОБЫЧНО торгует в эту минуту.

Скользящее окно 30 минут (см. monitor.py) сравнивает минуту с соседними
минутами той же сессии. Это делает утренний и предзакрывочный всплески
системно аномальными — они аномальны относительно затишья до них, но
совершенно нормальны для своего времени суток.

Здесь копится вторая база сравнения: медиана оборота тикера в конкретном
5-минутном бакете дня за последние N торговых дней. Отношение текущей
минуты к этой медиане (`rel_volume`) отвечает на вопрос «много ли это
для 11:47», а не «много ли это по сравнению с 11:17».

Профиль накапливается сам, из тех же минутных дельт, что уже считает
монитор. Пока данных мало (< MIN_DAYS_FOR_PROFILE дней), rel_volume
возвращает None и никакая логика на него не опирается.
"""

import os
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "volume_profile.db")

BUCKET_MINUTES = 5          # гранулярность профиля
PROFILE_DAYS = 30           # глубина истории для медианы
MIN_DAYS_FOR_PROFILE = 5    # меньше — считаем, что профиля нет
PRUNE_DAYS = 45             # старше — удаляем
FLUSH_EVERY_MINUTES = 5     # как часто сбрасывать накопленное в БД

_SCHEMA = """
CREATE TABLE IF NOT EXISTS volume_profile (
    trade_date TEXT NOT NULL,
    ticker     TEXT NOT NULL,
    bucket     INTEGER NOT NULL,
    value      REAL NOT NULL,
    PRIMARY KEY (trade_date, ticker, bucket)
);
CREATE INDEX IF NOT EXISTS idx_vp_ticker_bucket ON volume_profile (ticker, bucket);
"""

# Накопленное с прошлого флаша: {(trade_date, ticker, bucket): суммарный оборот}
_PENDING: Dict[Tuple[str, str, int], float] = defaultdict(float)
_LAST_FLUSH: Optional[datetime] = None

# Медианы, загруженные в память: {(ticker, bucket): медиана оборота ЗА БАКЕТ}
_MEDIANS: Dict[Tuple[str, int], float] = {}
_MEDIANS_LOADED_FOR: Optional[str] = None


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


def bucket_of(now: datetime) -> int:
    return (now.hour * 60 + now.minute) // BUCKET_MINUTES


def accumulate(deltas: Dict[str, float], now: datetime) -> None:
    """Досыпать минутные дельты в текущий бакет (в памяти)."""
    date = now.strftime("%Y-%m-%d")
    b = bucket_of(now)
    for ticker, value in deltas.items():
        if value > 0:
            _PENDING[(date, ticker, b)] += value


def flush(now: datetime, force: bool = False) -> int:
    """Сбросить накопленное в БД. Возвращает число записанных строк."""
    global _LAST_FLUSH
    if not _PENDING:
        _LAST_FLUSH = now
        return 0
    if not force and _LAST_FLUSH is not None:
        if now - _LAST_FLUSH < timedelta(minutes=FLUSH_EVERY_MINUTES):
            return 0

    rows = [(d, t, b, v) for (d, t, b), v in _PENDING.items()]
    conn = _get_conn()
    try:
        # Бакет может дозаписываться несколько раз (флаш в середине бакета),
        # поэтому именно += , а не замена.
        conn.executemany(
            "INSERT INTO volume_profile (trade_date, ticker, bucket, value) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (trade_date, ticker, bucket) "
            "DO UPDATE SET value = value + excluded.value",
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    _PENDING.clear()
    _LAST_FLUSH = now
    return len(rows)


def prune(now: datetime) -> int:
    cutoff = (now - timedelta(days=PRUNE_DAYS)).strftime("%Y-%m-%d")
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM volume_profile WHERE trade_date < ?", (cutoff,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def load_medians(now: datetime) -> int:
    """Пересчитать медианы в память. Вызывать раз в сутки (дешевле, чем каждый тик)."""
    global _MEDIANS, _MEDIANS_LOADED_FOR
    today = now.strftime("%Y-%m-%d")
    cutoff = (now - timedelta(days=PROFILE_DAYS)).strftime("%Y-%m-%d")

    per_key: Dict[Tuple[str, int], list] = defaultdict(list)
    conn = _get_conn()
    try:
        # Текущий день исключаем: он ещё не полон и сам себя объяснять не должен.
        for ticker, bucket, value in conn.execute(
            "SELECT ticker, bucket, value FROM volume_profile "
            "WHERE trade_date >= ? AND trade_date < ?",
            (cutoff, today),
        ):
            per_key[(ticker, bucket)].append(float(value))
    finally:
        conn.close()

    _MEDIANS = {
        key: statistics.median(vals)
        for key, vals in per_key.items()
        if len(vals) >= MIN_DAYS_FOR_PROFILE
    }
    _MEDIANS_LOADED_FOR = today
    return len(_MEDIANS)


def medians_loaded_for() -> Optional[str]:
    return _MEDIANS_LOADED_FOR


def profile_size() -> int:
    return len(_MEDIANS)


def rel_volume(ticker: str, delta: float, now: datetime) -> Optional[float]:
    """Во сколько раз минутный оборот выше типичного для ЭТОГО времени дня.

    None — если профиля по (тикер, бакет) ещё нет.
    """
    median_bucket = _MEDIANS.get((ticker, bucket_of(now)))
    if not median_bucket or median_bucket <= 0:
        return None
    median_minute = median_bucket / BUCKET_MINUTES
    if median_minute <= 0:
        return None
    return delta / median_minute
