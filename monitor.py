#!/usr/bin/env python3
"""Мониторинг внутридневных аномалий объёмов торгов на Мосбирже (раз в минуту)."""

import html
import os
import statistics
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

# Все datetime.now() / time.localtime() в MSK независимо от TZ системы.
os.environ["TZ"] = "Europe/Moscow"
time.tzset()

try:
    import requests
except ImportError:
    print("Требуется requests: pip install requests", file=sys.stderr)
    sys.exit(1)

# ============================================================================
# НАСТРОЙКИ
# ============================================================================

ANOMALY_THRESHOLD_SIGMA = 8.0
MIN_DEVIATION_PERCENT = 500
MIN_AVG_MINUTE_VALUE = 500_000  # руб/мин
WINDOW_MINUTES = 30
MIN_POINTS_FOR_STATS = 10
COOLDOWN_MINUTES = 30

# После первого volume-алерта baseline (mean/std) замораживается на этот срок,
# чтобы продолжать видеть волну, пока окно не «привыкло» к новому уровню.
# Кулдаун в это время не блокирует — каждая аномальная минута идёт в чат.
VOLUME_FREEZE_MINUTES = 10

# Block trade: «одна большая сделка» детектится без trades.json через NUMTRADES.
# Сигнал: в минуту прошло мало сделок, но оборот значимый → средняя сделка огромная.
BLOCK_MIN_MINUTE_VALUE = 5_000_000      # руб, отсекаем мелкий шум
BLOCK_MIN_AVG_TRADE_SIZE = 2_000_000    # руб/сделка средняя за эту минуту

# Price spike: цена дёрнулась без сопоставимого объёма (тонкий стакан проткнули).
SPIKE_MIN_PRICE_PCT = 1.0               # |Δцены за минуту| ≥ 1%
SPIKE_MIN_DELTA_VAL = 50_000            # руб, минимум — хоть что-то торговалось
SPIKE_MAX_DELTA_VS_MEAN = 3.0           # выше — это уже volume anomaly, не spike

# Спим с 23:50 до 06:50 MSK (между вечеркой и утренней сессией).
SLEEP_START_MIN = 23 * 60 + 50
SLEEP_END_MIN = 6 * 60 + 50

EXCLUDED_TICKER_PREFIXES = ("RU000",)
EXCLUDED_SHORTNAME_KEYWORDS = ("ETF",)

MOEX_API_BASE = "https://iss.moex.com/iss"
SNAPSHOT_URL = f"{MOEX_API_BASE}/engines/stock/markets/shares/securities.json"
TRADES_URL_TPL = f"{MOEX_API_BASE}/engines/stock/markets/shares/securities/{{secid}}/trades.json"
ORDERBOOK_URL_TPL = f"{MOEX_API_BASE}/engines/stock/markets/shares/securities/{{secid}}/orderbook.json"
INDEX_URL = f"{MOEX_API_BASE}/engines/stock/markets/index/securities.json"
MOEX_PAGE_URL_TPL = "https://www.moex.com/ru/issue.aspx?code={ticker}"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
# Прокси применяется ТОЛЬКО к запросам в api.telegram.org. Запросы к MOEX идут напрямую.
TELEGRAM_PROXY = os.environ.get("TELEGRAM_PROXY", "").strip()

HTTP_TIMEOUT = 20

# ============================================================================
# СОСТОЯНИЕ
# ============================================================================

WINDOWS: Dict[str, deque] = {}                 # {ticker: deque[delta_per_minute]}
LAST_VALTODAY: Dict[str, float] = {}           # {ticker: VALTODAY на прошлом замере}
LAST_NUMTRADES: Dict[str, int] = {}            # {ticker: NUMTRADES на прошлом замере}
LAST_PRICES: Dict[str, float] = {}             # {ticker: LAST цена на прошлом замере}
SHORTNAMES: Dict[str, str] = {}                # {ticker: shortname}
# COOLDOWNS keyed by (kind, ticker) — у каждого типа алерта свой кулдаун.
COOLDOWNS: Dict[Tuple[str, str], datetime] = {}
# FROZEN_BASELINES[ticker] = (mean, std, expires_at) — заморозка для volume-волны.
FROZEN_BASELINES: Dict[str, Tuple[float, float, datetime]] = {}


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def format_price(p: float) -> str:
    """Адаптивная точность: копеечные бумаги показываем с 4 знаками."""
    ap = abs(p)
    if ap < 1:
        return f"{p:.4f}"
    if ap < 10:
        return f"{p:.3f}"
    return f"{p:.2f}"


def format_number(num: float) -> str:
    if num >= 1_000_000_000:
        return f"{num / 1_000_000_000:.1f} млрд"
    if num >= 1_000_000:
        return f"{num / 1_000_000:.1f} млн"
    if num >= 1000:
        return f"{num / 1000:.1f} тыс"
    return f"{num:,.0f}"


def is_excluded(secid: str, shortname: str) -> bool:
    if secid.startswith(EXCLUDED_TICKER_PREFIXES):
        return True
    if any(k in shortname.upper() for k in EXCLUDED_SHORTNAME_KEYWORDS):
        return True
    return False


def is_sleep_time(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    mins = now.hour * 60 + now.minute
    return mins >= SLEEP_START_MIN or mins < SLEEP_END_MIN


def reset_state() -> None:
    """Сбросить окна и накопленные кеши (после ночи или холодного старта)."""
    WINDOWS.clear()
    LAST_VALTODAY.clear()
    LAST_NUMTRADES.clear()
    LAST_PRICES.clear()
    FROZEN_BASELINES.clear()
    # SHORTNAMES не чистим (имена не меняются), COOLDOWNS истекают по времени.


# ============================================================================
# API
# ============================================================================

def fetch_snapshot() -> Optional[Tuple[
    Dict[str, str], Dict[str, float], Dict[str, int], Dict[str, dict]
]]:
    """Один запрос на всю биржу: имена, VALTODAY, NUMTRADES и дневная картина.

    Возвращает (shortnames, valtoday, numtrades, daily), где daily[ticker] = {
        last, open, low, high, last_to_prev (%), valtoday
    }. Поля могут быть None если данных нет.
    """
    params = {
        "iss.meta": "off",
        "iss.only": "securities,marketdata",
        "securities.columns": "SECID,SHORTNAME,BOARDID",
        "marketdata.columns":
            "SECID,BOARDID,VALTODAY,NUMTRADES,LAST,OPEN,LOW,HIGH,LASTTOPREVPRICE",
    }
    try:
        r = requests.get(SNAPSHOT_URL, params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log(f"snapshot error: {e}")
        return None

    shortnames: Dict[str, str] = {}
    for secid, shortname, _board in data.get("securities", {}).get("data", []):
        shortnames.setdefault(secid, shortname)

    # Берём только основной режим TQBR. Параллельные режимы (SMAL — лот=1 шт.,
    # SPEQ и т.п.) имеют свои LAST/HIGH/LOW и крошечный оборот: одна сделка по
    # «нерыночной» цене на SMAL ловилась как spike, хотя на TQBR (главный график)
    # цена туда не ходила. См. GAZP 2026-06-02: SMAL LAST=117.19/HIGH=118.82
    # против TQBR LAST=116.30/HIGH=116.38 при обороте SMAL 6.5 тыс ₽ за день.
    valtoday: Dict[str, float] = {}
    numtrades: Dict[str, int] = {}
    daily: Dict[str, dict] = {}
    for secid, board, val, ntr, last, open_, low, high, last_to_prev in \
            data.get("marketdata", {}).get("data", []):
        if board != "TQBR":
            continue
        if val is not None:
            valtoday[secid] = float(val)
        if ntr is not None:
            numtrades[secid] = int(ntr)
        if last is not None:
            daily[secid] = {
                "last": float(last),
                "open": float(open_) if open_ is not None else None,
                "low": float(low) if low is not None else None,
                "high": float(high) if high is not None else None,
                "last_to_prev": float(last_to_prev) if last_to_prev is not None else None,
            }

    for ticker, vt in valtoday.items():
        if ticker in daily:
            daily[ticker]["valtoday"] = vt

    return shortnames, valtoday, numtrades, daily


def fetch_index_context() -> Optional[float]:
    """Изменение индекса за день в %. None при любой ошибке.

    Берём IMOEX2: днём он совпадает с IMOEX, а в вечернюю сессию (после 19:00)
    продолжает обновляться, тогда как IMOEX замирает на закрытии основной сессии.
    """
    params = {
        "iss.meta": "off",
        "iss.only": "marketdata",
        "marketdata.columns": "SECID,LASTCHANGEPRC",
    }
    try:
        r = requests.get(INDEX_URL, params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        for row in data.get("marketdata", {}).get("data", []):
            if len(row) >= 2 and row[0] == "IMOEX2" and row[1] is not None:
                return float(row[1])
    except Exception as e:
        log(f"index error: {e}")
    return None


def fetch_orderbook(secid: str, top_n: int = 3) -> Optional[dict]:
    """Топ-N бид/аск из стакана. None если рынок закрыт или ошибка."""
    url = ORDERBOOK_URL_TPL.format(secid=secid)
    params = {
        "iss.meta": "off",
        "iss.only": "orderbook",
        "orderbook.columns": "BUYSELL,PRICE,QUANTITY",
    }
    try:
        r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        # На закрытом рынке MOEX отдаёт 200 OK с HTML-заглушкой → ValueError на json().
        data = r.json()
    except (requests.RequestException, ValueError):
        return None

    rows = data.get("orderbook", {}).get("data", [])
    if not rows:
        return None

    bids: List[Tuple[float, int]] = []  # (price, qty), от лучшей цены
    asks: List[Tuple[float, int]] = []
    for side, price, qty in rows:
        if price is None or qty is None:
            continue
        if side == "B":
            bids.append((float(price), int(qty)))
        elif side == "S":
            asks.append((float(price), int(qty)))

    bids.sort(key=lambda x: x[0], reverse=True)
    asks.sort(key=lambda x: x[0])

    return {"bids": bids[:top_n], "asks": asks[:top_n]}


def fetch_ticker_trades(secid: str) -> list:
    """Последние сделки по тикеру (до 5000, от свежих к старым)."""
    url = TRADES_URL_TPL.format(secid=secid)
    params = {
        "iss.meta": "off",
        "iss.only": "trades",
        "trades.columns": "TRADETIME,PRICE,QUANTITY,VALUE,BUYSELL,BOARDID",
        "reversed": 1,
        "limit": 5000,
    }
    try:
        r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log(f"trades error for {secid}: {e}")
        return []

    cols = data.get("trades", {}).get("columns", [])
    rows = data.get("trades", {}).get("data", [])
    return [dict(zip(cols, row)) for row in rows]


# ============================================================================
# ЛОГИКА
# ============================================================================

def update_windows(valtoday: Dict[str, float]) -> Dict[str, float]:
    """Записать минутные дельты в окна, вернуть {ticker: delta_за_минуту}."""
    deltas: Dict[str, float] = {}
    for ticker, val in valtoday.items():
        prev = LAST_VALTODAY.get(ticker)
        LAST_VALTODAY[ticker] = val
        if prev is None:
            continue
        delta = val - prev
        if delta < 0:
            # VALTODAY уменьшился — либо смена торгового дня, либо технический сбой.
            # Сбрасываем окно тикера, чтобы не словить мусорные значения.
            WINDOWS.pop(ticker, None)
            continue
        if ticker not in WINDOWS:
            WINDOWS[ticker] = deque(maxlen=WINDOW_MINUTES)
        WINDOWS[ticker].append(delta)
        deltas[ticker] = delta
    return deltas


def detect_anomalies(deltas: Dict[str, float]) -> list:
    """Найти volume-аномалии. Учитывает «замороженный» baseline на время волны."""
    now = datetime.now()
    anomalies = []
    for ticker, delta in deltas.items():
        shortname = SHORTNAMES.get(ticker, "")
        if is_excluded(ticker, shortname):
            continue

        window = WINDOWS.get(ticker)
        if not window or len(window) < MIN_POINTS_FOR_STATS:
            continue

        # Если есть актуальный freeze — берём mean/std оттуда, окно игнорируем.
        frozen = FROZEN_BASELINES.get(ticker)
        if frozen and frozen[2] > now:
            mean, std, _ = frozen
            window_size = len(window) - 1
        else:
            if frozen:
                del FROZEN_BASELINES[ticker]
            # База — окно БЕЗ текущей точки (она в конце deque).
            base = list(window)[:-1]
            if len(base) < 2:
                continue
            mean = statistics.mean(base)
            if mean < MIN_AVG_MINUTE_VALUE:
                continue
            std = statistics.stdev(base)
            if std <= 0:
                continue
            window_size = len(base)

        z = (delta - mean) / std
        deviation = (delta - mean) / mean * 100 if mean > 0 else 0

        if z > ANOMALY_THRESHOLD_SIGMA and deviation > MIN_DEVIATION_PERCENT:
            # Если freeze ещё не стоит — это первая минута волны, ставим его.
            if ticker not in FROZEN_BASELINES:
                FROZEN_BASELINES[ticker] = (
                    mean, std, now + timedelta(minutes=VOLUME_FREEZE_MINUTES)
                )
            anomalies.append((ticker, {
                "shortname": shortname,
                "delta": delta,
                "mean": mean,
                "std": std,
                "z": z,
                "deviation": deviation,
                "window_size": window_size,
            }))

    anomalies.sort(key=lambda x: x[1]["z"], reverse=True)
    return anomalies


def compute_numtrades_deltas(numtrades: Dict[str, int]) -> Dict[str, int]:
    """Дельта количества сделок за минуту. Обновляет кеш LAST_NUMTRADES."""
    deltas: Dict[str, int] = {}
    for ticker, val in numtrades.items():
        prev = LAST_NUMTRADES.get(ticker)
        LAST_NUMTRADES[ticker] = val
        if prev is None or val < prev:
            continue
        deltas[ticker] = val - prev
    return deltas


def compute_price_changes(daily: Dict[str, dict]) -> Dict[str, Tuple[float, float]]:
    """Минутное изменение цены: {ticker: (prev_last, new_last_change_pct)}.

    Обновляет кеш LAST_PRICES. Возвращает только тикеры, у которых есть и prev и new.
    """
    changes: Dict[str, Tuple[float, float]] = {}
    for ticker, info in daily.items():
        new_last = info.get("last")
        if new_last is None:
            continue
        prev = LAST_PRICES.get(ticker)
        LAST_PRICES[ticker] = float(new_last)
        if prev is None or prev <= 0:
            continue
        change_pct = (new_last - prev) / prev * 100
        changes[ticker] = (prev, change_pct)
    return changes


def detect_block_trades(
    deltas: Dict[str, float],
    trade_deltas: Dict[str, int],
    daily: Dict[str, dict],
) -> list:
    """Block trade: мало сделок в минуту, но крупный оборот → средняя сделка огромная."""
    out = []
    for ticker, dv in deltas.items():
        if dv < BLOCK_MIN_MINUTE_VALUE:
            continue
        shortname = SHORTNAMES.get(ticker, "")
        if is_excluded(ticker, shortname):
            continue
        dt = trade_deltas.get(ticker)
        if not dt or dt <= 0:
            continue
        avg_size = dv / dt
        if avg_size < BLOCK_MIN_AVG_TRADE_SIZE:
            continue
        out.append((ticker, {
            "shortname": shortname,
            "delta": dv,
            "trades_count": dt,
            "avg_trade_size": avg_size,
        }))
    out.sort(key=lambda x: x[1]["avg_trade_size"], reverse=True)
    return out


def detect_price_spikes(
    deltas: Dict[str, float],
    price_changes: Dict[str, Tuple[float, float]],
) -> list:
    """Price spike: цена двинулась на ≥ N% при обычном/малом объёме."""
    out = []
    for ticker, (prev_price, change_pct) in price_changes.items():
        if abs(change_pct) < SPIKE_MIN_PRICE_PCT:
            continue
        shortname = SHORTNAMES.get(ticker, "")
        if is_excluded(ticker, shortname):
            continue
        dv = deltas.get(ticker, 0.0)
        if dv < SPIKE_MIN_DELTA_VAL:
            continue
        # Если объём уже выше порога volume anomaly — не дублируем spike.
        window = WINDOWS.get(ticker)
        if window and len(window) >= MIN_POINTS_FOR_STATS:
            base = list(window)[:-1]
            if base:
                mean = statistics.mean(base)
                if mean > 0 and dv / mean > SPIKE_MAX_DELTA_VS_MEAN:
                    continue
        new_price = LAST_PRICES.get(ticker, prev_price)
        out.append((ticker, {
            "shortname": shortname,
            "prev_price": prev_price,
            "new_price": new_price,
            "change_pct": change_pct,
            "delta": dv,
        }))
    out.sort(key=lambda x: abs(x[1]["change_pct"]), reverse=True)
    return out


def analyze_ticker_trades(trades: list, since: datetime) -> Optional[dict]:
    """Разложить сделки тикера за последнюю минуту на buy/sell + топ-3 по обороту."""
    if not trades:
        return None

    since_str = since.strftime("%H:%M:%S")
    recent = [t for t in trades if t.get("TRADETIME", "") >= since_str]
    if not recent:
        return None

    buy_value = sum(float(t["VALUE"]) for t in recent if t.get("BUYSELL") == "B")
    sell_value = sum(float(t["VALUE"]) for t in recent if t.get("BUYSELL") == "S")
    total = buy_value + sell_value
    if total <= 0:
        return None

    # reversed=1 в API → recent[0] свежее всех, recent[-1] самая старая в окне.
    prices = [float(t["PRICE"]) for t in recent if t.get("PRICE") is not None]
    price_first = prices[-1] if prices else None
    price_last = prices[0] if prices else None
    price_change_pct = None
    if price_first and price_last and price_first > 0:
        price_change_pct = (price_last - price_first) / price_first * 100

    top3 = sorted(recent, key=lambda t: float(t.get("VALUE") or 0), reverse=True)[:3]

    return {
        "buy_value": buy_value,
        "sell_value": sell_value,
        "buy_pct": buy_value / total * 100,
        "sell_pct": sell_value / total * 100,
        "trades_count": len(recent),
        "price_last": price_last,
        "price_change_pct": price_change_pct,
        "top3": top3,
    }


# ============================================================================
# TELEGRAM
# ============================================================================

def _direction_emoji(daily: Optional[dict], fallback_pct: Optional[float]) -> str:
    direction = None
    if daily and daily.get("last_to_prev") is not None:
        direction = daily["last_to_prev"]
    elif fallback_pct is not None:
        direction = fallback_pct
    if direction is None:
        return "📊"
    return "🟩 📈" if direction > 0 else "🟥 📉"


def format_alert(
    ticker: str,
    info: dict,
    details: Optional[dict],
    daily: Optional[dict],
    market_change_pct: Optional[float],
    orderbook: Optional[dict],
    kind: str = "volume",
) -> str:
    shortname = html.escape(info["shortname"])

    if kind == "spike":
        # Для spike цвет шапки = минутное движение (само событие).
        head_color = _direction_emoji(None, info["change_pct"])
    elif kind == "volume":
        # Для volume — приоритет минутному движению аномалии, fallback на дневной импульс.
        minute_change = details.get("price_change_pct") if details else None
        if minute_change is not None:
            head_color = _direction_emoji(None, minute_change)
        else:
            head_color = _direction_emoji(daily, None)
    else:
        # block — дневной импульс (минутного движения у block нет).
        head_color = _direction_emoji(daily, None)

    if kind == "block":
        head = f"{head_color} 🧱 <b>{html.escape(ticker)}</b> — {shortname} · block trade"
        body = [
            f"Средняя сделка: <b>{format_number(info['avg_trade_size'])} руб</b>",
            f"Оборот за минуту: {format_number(info['delta'])} руб "
            f"({info['trades_count']} сделок)",
        ]
    elif kind == "spike":
        arrow = "⬆️" if info["change_pct"] > 0 else "⬇️"
        head = f"{head_color} ⚡ <b>{html.escape(ticker)}</b> — {shortname} · price spike"
        body = [
            f"Цена: {format_price(info['prev_price'])} → "
            f"<b>{format_price(info['new_price'])}</b> "
            f"{arrow} ({info['change_pct']:+.2f}% за мин)",
            f"Оборот за минуту: {format_number(info['delta'])} руб",
        ]
    else:
        multiplier = info["delta"] / info["mean"] if info["mean"] > 0 else 0
        head = f"{head_color} <b>{html.escape(ticker)}</b> — {shortname}"
        body = [
            f"Оборот за минуту: <b>{format_number(info['delta'])} руб</b> "
            f"(×{multiplier:.1f} от среднего)",
            f"Z-score: +{info['z']:.1f} | окно {info['window_size']} мин",
        ]

    lines = [head] + body

    # Дневная картина: LAST, изменение от пред. закрытия, диапазон, общий оборот.
    if daily:
        last = daily.get("last")
        ltp = daily.get("last_to_prev")
        if last is not None:
            day_line = f"Цена: <b>{format_price(last)}</b>"
            if ltp is not None:
                day_line += f" ({ltp:+.2f}% к закр.)"
            lines.append(day_line)
        low, high, open_ = daily.get("low"), daily.get("high"), daily.get("open")
        if low is not None and high is not None:
            range_line = f"День: мин {format_price(low)} / макс {format_price(high)}"
            if open_ is not None:
                range_line += f" · откр {format_price(open_)}"
            lines.append(range_line)
        vt = daily.get("valtoday")
        if vt:
            lines.append(f"Оборот за день: {format_number(vt)} руб")

    if market_change_pct is not None:
        lines.append(f"IMOEX: {market_change_pct:+.2f}%")

    if details:
        lines.append("")
        lines.append(f"Покупки: {details['buy_pct']:.0f}% "
                     f"({format_number(details['buy_value'])})")
        lines.append(f"Продажи: {details['sell_pct']:.0f}% "
                     f"({format_number(details['sell_value'])})")
        if details["price_last"] is not None and details["price_change_pct"] is not None:
            lines.append(f"Минута: {format_price(details['price_last'])} "
                         f"({details['price_change_pct']:+.2f}%)")
        lines.append(f"Сделок в минуту: {details['trades_count']}")

        top3 = details.get("top3") or []
        if top3:
            lines.append("")
            lines.append("Топ-сделки минуты:")
            for t in top3:
                side = "buy" if t.get("BUYSELL") == "B" else "sell"
                val = float(t.get("VALUE") or 0)
                price = t.get("PRICE")
                qty = t.get("QUANTITY")
                bits = [format_number(val) + " руб", side]
                if price is not None and qty is not None:
                    bits.append(f"{int(qty)}@{format_price(float(price))}")
                lines.append("• " + " · ".join(bits))

    if orderbook and (orderbook.get("bids") or orderbook.get("asks")):
        lines.append("")
        lines.append("Стакан (топ-3):")
        asks = orderbook.get("asks") or []
        bids = orderbook.get("bids") or []
        # Аски выводим сверху вниз (от худшей к лучшей), биды — от лучшей к худшей.
        for price, qty in reversed(asks):
            lines.append(f"   ask {format_price(price)} × {qty}")
        for price, qty in bids:
            lines.append(f"   bid {format_price(price)} × {qty}")

    lines.append("")
    lines.append(f'<a href="{MOEX_PAGE_URL_TPL.format(ticker=html.escape(ticker))}">'
                 f"страница на MOEX</a>")
    lines.append(datetime.now().strftime("%H:%M MSK · %Y-%m-%d"))
    return "\n".join(lines)


def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram не настроен (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID), пропуск")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    proxies = {"http": TELEGRAM_PROXY, "https": TELEGRAM_PROXY} if TELEGRAM_PROXY else None
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=HTTP_TIMEOUT, proxies=proxies)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        log(f"telegram error: {e}")
        return False


# ============================================================================
# ЦИКЛ
# ============================================================================

def tick() -> None:
    snapshot = fetch_snapshot()
    if snapshot is None:
        return
    shortnames, valtoday, numtrades, daily = snapshot

    SHORTNAMES.update(shortnames)
    deltas = update_windows(valtoday)
    trade_deltas = compute_numtrades_deltas(numtrades)
    price_changes = compute_price_changes(daily)

    anomalies = detect_anomalies(deltas)
    block_trades = detect_block_trades(deltas, trade_deltas, daily)
    spikes = detect_price_spikes(deltas, price_changes)

    # Один тикер может попасть и в volume, и в block, и в spike одновременно —
    # отдаём приоритет volume, чтобы не дублировать.
    blocked_tickers = {t for t, _ in anomalies}
    block_trades = [(t, i) for t, i in block_trades if t not in blocked_tickers]
    seen_block = blocked_tickers | {t for t, _ in block_trades}
    spikes = [(t, i) for t, i in spikes if t not in seen_block]

    parts = []
    if anomalies:
        parts.append("volume: " + ", ".join(
            f"{t}(z={i['z']:.1f})" for t, i in anomalies))
    if block_trades:
        parts.append("block: " + ", ".join(
            f"{t}(avg={format_number(i['avg_trade_size'])})"
            for t, i in block_trades))
    if spikes:
        parts.append("spike: " + ", ".join(
            f"{t}({i['change_pct']:+.2f}%)" for t, i in spikes))
    if parts:
        log(f"snapshot OK {len(valtoday)} tickers · " + " | ".join(parts))
    else:
        log(f"snapshot OK {len(valtoday)} tickers · no anomalies")

    if not (anomalies or block_trades or spikes):
        return

    market_change_pct = fetch_index_context()
    now = datetime.now()

    def maybe_send(kind: str, ticker: str, info: dict, *, fetch_extras: bool) -> None:
        # Пока volume-волна заморожена — кулдаун игнорируем, шлём каждую минуту.
        frozen = FROZEN_BASELINES.get(ticker) if kind == "volume" else None
        in_wave = bool(frozen and frozen[2] > now)
        if not in_wave and COOLDOWNS.get((kind, ticker), datetime.min) > now:
            return
        details = None
        orderbook = None
        if fetch_extras:
            trades = fetch_ticker_trades(ticker)
            details = analyze_ticker_trades(trades, since=now - timedelta(minutes=1))
            orderbook = fetch_orderbook(ticker)
        msg = format_alert(
            ticker, info, details,
            daily.get(ticker), market_change_pct, orderbook,
            kind=kind,
        )
        if send_telegram(msg):
            # После волны блокируем тикер ещё на COOLDOWN_MINUTES за пределами freeze.
            cd_until = (frozen[2] if in_wave else now) + timedelta(minutes=COOLDOWN_MINUTES)
            COOLDOWNS[(kind, ticker)] = cd_until
            log(f"alert sent: {kind}/{ticker}")

    for ticker, info in anomalies:
        maybe_send("volume", ticker, info, fetch_extras=True)
    for ticker, info in block_trades:
        # У block trade сделок единицы — лезть в /trades.json и /orderbook.json смысла мало.
        maybe_send("block", ticker, info, fetch_extras=False)
    for ticker, info in spikes:
        # Для spike стакан полезен (увидеть тонкое место), trades — нет.
        if COOLDOWNS.get(("spike", ticker), datetime.min) > now:
            continue
        orderbook = fetch_orderbook(ticker)
        msg = format_alert(
            ticker, info, None,
            daily.get(ticker), market_change_pct, orderbook,
            kind="spike",
        )
        if send_telegram(msg):
            COOLDOWNS[("spike", ticker)] = now + timedelta(minutes=COOLDOWN_MINUTES)
            log(f"alert sent: spike/{ticker}")


def main() -> None:
    log("MOEX intraday monitor started")
    log(f"volume: z>{ANOMALY_THRESHOLD_SIGMA}, dev>{MIN_DEVIATION_PERCENT}%, "
        f"mean>={format_number(MIN_AVG_MINUTE_VALUE)}/мин · freeze {VOLUME_FREEZE_MINUTES} min")
    log(f"block: val>={format_number(BLOCK_MIN_MINUTE_VALUE)}, "
        f"avg>={format_number(BLOCK_MIN_AVG_TRADE_SIZE)}/сделка")
    log(f"spike: |Δp|>={SPIKE_MIN_PRICE_PCT}% за мин, val>={format_number(SPIKE_MIN_DELTA_VAL)}")
    log(f"window: {WINDOW_MINUTES} min · cooldown: {COOLDOWN_MINUTES} min")
    log("sleep window: 23:50–06:50 MSK")

    reset_state()
    sleeping = False

    while True:
        try:
            if is_sleep_time():
                if not sleeping:
                    log("entering night sleep window, resetting state")
                    reset_state()
                    sleeping = True
                time.sleep(60)
                continue

            if sleeping:
                log("waking up, resetting state")
                reset_state()
                sleeping = False

            start = time.time()
            tick()
            elapsed = time.time() - start
            time.sleep(max(0.0, 60 - elapsed))

        except KeyboardInterrupt:
            log("interrupted, exiting")
            return
        except Exception as e:
            log(f"unexpected error: {type(e).__name__}: {e}")
            log("traceback:\n" + traceback.format_exc())
            time.sleep(10)


if __name__ == "__main__":
    main()
