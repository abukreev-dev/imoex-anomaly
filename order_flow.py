#!/usr/bin/env python3
"""Кумулятивная дельта buy/sell за сессию — «кто-то набирает / сливает».

Монитор раскладывал ленту на buy/sell ровно один раз, в момент всплеска.
Так видно вспышку, но не видно бумагу, которую ровно и тихо набирают
третий час подряд — там нет ни одной аномальной минуты.

Здесь по ограниченному списку самых оборотистых бумаг раз в несколько
минут читается лента, из неё берётся доля покупок/продаж, и перекос
копится за сессию. Сигнал — устойчивый односторонний поток при цене,
которая почти не сдвинулась: набирают, не разгоняя.

Абсолютные объёмы берём не из ленты, а из ΔVALTODAY за тот же интервал:
лента отдаётся с limit=5000 и по ликвидной бумаге за 5 минут может
усечься. Доля buy/sell при усечении остаётся репрезентативной, а
абсолют восстанавливаем масштабированием на честный оборот интервала.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

# Сколько бумаг держим под наблюдением (топ по обороту дня).
FLOW_WATCH_TOP_N = 30
# Как часто опрашиваем ленту по каждой из них.
FLOW_SCAN_INTERVAL_MINUTES = 5

# Пороги сигнала.
FLOW_MIN_SESSION_VALUE = 100_000_000   # руб, накопленный оборот за сессию
FLOW_MIN_IMBALANCE = 0.30              # |buy-sell| / (buy+sell)
FLOW_MAX_PRICE_MOVE_PCT = 1.5          # цена почти не ушла — «тихий» набор
FLOW_MIN_SAMPLES = 4                   # не меньше 4 замеров (≈20 минут)

# {ticker: {"buy": руб, "sell": руб, "samples": int,
#           "first_price": float, "first_seen": datetime}}
_FLOW: Dict[str, dict] = {}
_LAST_SCAN: Optional[datetime] = None
# {ticker: TRADETIME последней учтённой сделки} — граница по шкале ленты.
# По стенным часам её отсчитывать нельзя: ISS отдаёт ленту с лагом ~15 минут.
_LAST_TRADETIME: Dict[str, str] = {}


def reset() -> None:
    global _LAST_SCAN
    _FLOW.clear()
    _LAST_TRADETIME.clear()
    _LAST_SCAN = None


def due_for_scan(now: datetime) -> bool:
    if _LAST_SCAN is None:
        return True
    return now - _LAST_SCAN >= timedelta(minutes=FLOW_SCAN_INTERVAL_MINUTES)


def mark_scanned(now: datetime) -> None:
    global _LAST_SCAN
    _LAST_SCAN = now


def pick_watchlist(valtoday: Dict[str, float], allowed: set) -> List[str]:
    """Топ-N бумаг по дневному обороту среди разрешённых инструментов."""
    ranked = sorted(
        ((t, v) for t, v in valtoday.items() if t in allowed),
        key=lambda x: x[1],
        reverse=True,
    )
    return [t for t, _ in ranked[:FLOW_WATCH_TOP_N]]


def update(
    ticker: str,
    trades: list,
    interval_value: float,
    price: Optional[float],
    now: datetime,
) -> None:
    """Досыпать в накопитель долю buy/sell за интервал, масштабированную на оборот.

    Берём сделки строго новее последней учтённой по этому тикеру — так между
    заходами ничего не теряется и ничего не считается дважды, независимо от
    того, на сколько лента отстаёт от стенных часов.
    """
    if interval_value <= 0 or not trades:
        return

    times = [t.get("TRADETIME") for t in trades if t.get("TRADETIME")]
    if not times:
        return
    newest = max(times)
    marker = _LAST_TRADETIME.get(ticker)
    _LAST_TRADETIME[ticker] = newest
    if marker is None:
        # Первый заход по бумаге: границы «прошлого раза» нет, копить начинаем
        # со следующего скана, иначе в накопитель попадёт случайный кусок ленты.
        return

    recent = [t for t in trades if marker < (t.get("TRADETIME") or "") <= newest]
    if not recent:
        return

    buy = sum(float(t["VALUE"]) for t in recent
              if t.get("BUYSELL") == "B" and t.get("VALUE") is not None)
    sell = sum(float(t["VALUE"]) for t in recent
               if t.get("BUYSELL") == "S" and t.get("VALUE") is not None)
    seen = buy + sell
    if seen <= 0:
        return

    # Масштабируем на фактический оборот интервала (лента могла усечься).
    scale = interval_value / seen
    entry = _FLOW.setdefault(ticker, {
        "buy": 0.0, "sell": 0.0, "samples": 0,
        "first_price": price, "first_seen": now,
    })
    entry["buy"] += buy * scale
    entry["sell"] += sell * scale
    entry["samples"] += 1
    if entry["first_price"] is None:
        entry["first_price"] = price


def detect(prices: Dict[str, float], shortnames: Dict[str, str]) -> List[Tuple[str, dict]]:
    """Найти бумаги с устойчивым односторонним потоком при неподвижной цене."""
    out = []
    for ticker, e in _FLOW.items():
        total = e["buy"] + e["sell"]
        if total < FLOW_MIN_SESSION_VALUE or e["samples"] < FLOW_MIN_SAMPLES:
            continue

        imbalance = (e["buy"] - e["sell"]) / total
        if abs(imbalance) < FLOW_MIN_IMBALANCE:
            continue

        first_price = e.get("first_price")
        now_price = prices.get(ticker)
        move_pct = None
        if first_price and now_price and first_price > 0:
            move_pct = (now_price - first_price) / first_price * 100
            # Если цену уже разогнали — это не тихий набор, а обычный тренд.
            if abs(move_pct) > FLOW_MAX_PRICE_MOVE_PCT:
                continue

        out.append((ticker, {
            "shortname": shortnames.get(ticker, ""),
            "buy_value": e["buy"],
            "sell_value": e["sell"],
            "total_value": total,
            "imbalance": imbalance,
            "buy_pct": e["buy"] / total * 100,
            "sell_pct": e["sell"] / total * 100,
            "samples": e["samples"],
            "since": e["first_seen"],
            "price_move_pct": move_pct,
        }))

    out.sort(key=lambda x: abs(x[1]["imbalance"]), reverse=True)
    return out


def snapshot_size() -> int:
    return len(_FLOW)
