#!/usr/bin/env python3
"""Мониторинг внутридневных аномалий объёмов торгов на Мосбирже (раз в минуту)."""

import html
import os
import statistics
import sys
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

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

ANOMALY_THRESHOLD_SIGMA = 5.0
MIN_DEVIATION_PERCENT = 500
MIN_AVG_MINUTE_VALUE = 1_000_000  # руб/мин
WINDOW_MINUTES = 30
MIN_POINTS_FOR_STATS = 10
COOLDOWN_MINUTES = 30

# Спим с 23:50 до 06:50 MSK (между вечеркой и утренней сессией).
SLEEP_START_MIN = 23 * 60 + 50
SLEEP_END_MIN = 6 * 60 + 50

EXCLUDED_TICKER_PREFIXES = ("RU000",)
EXCLUDED_SHORTNAME_KEYWORDS = ("ETF",)

MOEX_API_BASE = "https://iss.moex.com/iss"
SNAPSHOT_URL = f"{MOEX_API_BASE}/engines/stock/markets/shares/securities.json"
TRADES_URL_TPL = f"{MOEX_API_BASE}/engines/stock/markets/shares/securities/{{secid}}/trades.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
# Прокси применяется ТОЛЬКО к запросам в api.telegram.org. Запросы к MOEX идут напрямую.
TELEGRAM_PROXY = os.environ.get("TELEGRAM_PROXY", "").strip()

HTTP_TIMEOUT = 20

# ============================================================================
# СОСТОЯНИЕ
# ============================================================================

WINDOWS: Dict[str, deque] = {}           # {ticker: deque[delta_per_minute]}
LAST_VALTODAY: Dict[str, float] = {}     # {ticker: VALTODAY на прошлом замере}
SHORTNAMES: Dict[str, str] = {}          # {ticker: shortname}
COOLDOWNS: Dict[str, datetime] = {}      # {ticker: время окончания кулдауна}


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


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
    """Сбросить окна и кеш VALTODAY (после ночи или холодного старта)."""
    WINDOWS.clear()
    LAST_VALTODAY.clear()
    # SHORTNAMES не чистим (имена не меняются), COOLDOWNS истекают по времени.


# ============================================================================
# API
# ============================================================================

def fetch_snapshot() -> Optional[Tuple[Dict[str, str], Dict[str, float]]]:
    """Один запрос на всю биржу: имена + накопленный с открытия VALTODAY."""
    params = {
        "iss.meta": "off",
        "iss.only": "securities,marketdata",
        "securities.columns": "SECID,SHORTNAME,BOARDID",
        "marketdata.columns": "SECID,BOARDID,VALTODAY",
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

    # VALTODAY суммируем по всем режимам торгов одного тикера.
    valtoday: Dict[str, float] = {}
    for secid, _board, val in data.get("marketdata", {}).get("data", []):
        if val is None:
            continue
        valtoday[secid] = valtoday.get(secid, 0.0) + float(val)

    return shortnames, valtoday


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
    """Найти аномалии. Возвращает [(ticker, info), ...] отсортированный по z."""
    anomalies = []
    for ticker, delta in deltas.items():
        shortname = SHORTNAMES.get(ticker, "")
        if is_excluded(ticker, shortname):
            continue

        window = WINDOWS.get(ticker)
        if not window or len(window) < MIN_POINTS_FOR_STATS:
            continue

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

        z = (delta - mean) / std
        deviation = (delta - mean) / mean * 100

        if z > ANOMALY_THRESHOLD_SIGMA and deviation > MIN_DEVIATION_PERCENT:
            anomalies.append((ticker, {
                "shortname": shortname,
                "delta": delta,
                "mean": mean,
                "std": std,
                "z": z,
                "deviation": deviation,
                "window_size": len(base),
            }))

    anomalies.sort(key=lambda x: x[1]["z"], reverse=True)
    return anomalies


def analyze_ticker_trades(trades: list, since: datetime) -> Optional[dict]:
    """Разложить сделки тикера за последнюю минуту на buy/sell и т.п."""
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

    top_trade = max(recent, key=lambda t: float(t.get("VALUE") or 0))

    return {
        "buy_value": buy_value,
        "sell_value": sell_value,
        "buy_pct": buy_value / total * 100,
        "sell_pct": sell_value / total * 100,
        "trades_count": len(recent),
        "price_last": price_last,
        "price_change_pct": price_change_pct,
        "top_value": float(top_trade.get("VALUE") or 0),
        "top_side": top_trade.get("BUYSELL"),
    }


# ============================================================================
# TELEGRAM
# ============================================================================

def format_alert(ticker: str, info: dict, details: Optional[dict]) -> str:
    multiplier = info["delta"] / info["mean"] if info["mean"] > 0 else 0
    shortname = html.escape(info["shortname"])

    # TG HTML не поддерживает цвет текста — направление передаём эмодзи в шапке.
    price_change = details.get("price_change_pct") if details else None
    if price_change is None:
        head_emoji = "📊"
    elif price_change > 0:
        head_emoji = "🟩 📈"
    else:
        head_emoji = "🟥 📉"

    lines = [
        f"{head_emoji} <b>{html.escape(ticker)}</b> — {shortname}",
        f"Оборот за минуту: <b>{format_number(info['delta'])} руб</b> "
        f"(×{multiplier:.1f} от среднего)",
        f"Z-score: +{info['z']:.1f} | окно {info['window_size']} мин",
    ]

    if details:
        lines.append("")
        lines.append(f"Покупки: {details['buy_pct']:.0f}% "
                     f"({format_number(details['buy_value'])})")
        lines.append(f"Продажи: {details['sell_pct']:.0f}% "
                     f"({format_number(details['sell_value'])})")
        if details["price_last"] is not None:
            line = f"Цена: {details['price_last']:.2f}"
            if details["price_change_pct"] is not None:
                line += f" ({details['price_change_pct']:+.2f}% мин)"
            lines.append(line)
        side = "buy" if details["top_side"] == "B" else "sell"
        lines.append(f"Топ-сделка: {format_number(details['top_value'])} руб ({side})")
        lines.append(f"Сделок в минуту: {details['trades_count']}")

    lines.append("")
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
    shortnames, valtoday = snapshot

    SHORTNAMES.update(shortnames)
    deltas = update_windows(valtoday)
    anomalies = detect_anomalies(deltas)

    if anomalies:
        summary = ", ".join(
            f"{t}(z={i['z']:.1f},+{i['deviation']:.0f}%)" for t, i in anomalies
        )
        log(f"snapshot OK {len(valtoday)} tickers · anomalies: {summary}")
    else:
        log(f"snapshot OK {len(valtoday)} tickers · no anomalies")

    now = datetime.now()
    for ticker, info in anomalies:
        if COOLDOWNS.get(ticker, datetime.min) > now:
            continue
        trades = fetch_ticker_trades(ticker)
        details = analyze_ticker_trades(trades, since=now - timedelta(minutes=1))
        msg = format_alert(ticker, info, details)
        if send_telegram(msg):
            COOLDOWNS[ticker] = now + timedelta(minutes=COOLDOWN_MINUTES)
            log(f"alert sent: {ticker}")


def main() -> None:
    log("MOEX intraday monitor started")
    log(f"thresholds: z>{ANOMALY_THRESHOLD_SIGMA}, dev>{MIN_DEVIATION_PERCENT}%, "
        f"mean>={format_number(MIN_AVG_MINUTE_VALUE)}/мин")
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
            time.sleep(10)


if __name__ == "__main__":
    main()
