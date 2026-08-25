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

import order_flow
import signal_log
import volume_profile

# ============================================================================
# НАСТРОЙКИ
# ============================================================================

ANOMALY_THRESHOLD_SIGMA = 8.0
MIN_DEVIATION_PERCENT = 500
MIN_AVG_MINUTE_VALUE = 1_000_000  # руб/мин
WINDOW_MINUTES = 30
MIN_POINTS_FOR_STATS = 10
COOLDOWN_MINUTES = 30

# После первого volume-алерта baseline (mean/std) замораживается на этот срок,
# чтобы продолжать видеть волну, пока окно не «привыкло» к новому уровню.
# Кулдаун в это время не блокирует — каждая аномальная минута идёт в чат.
VOLUME_FREEZE_MINUTES = 5

# Потолок volume-алертов по одной бумаге за торговый день. Замер по 6389
# сигналам (31.07–24.08.2026) показал у volume нулевую прогностику на всех
# горизонтах и на всех z — при 76% доли трафика канала. Резать по z бесполезно
# (z=25 предсказывает ровно столько же, сколько z=8), поэтому режем поток:
# волна SGZH 04.08.2026 дала 27 алертов за день, теперь даст не больше 3.
MAX_VOLUME_ALERTS_PER_DAY = 3

# Поток накапливается часами и, сработав, остаётся сработавшим — обычных
# 30 минут мало, иначе одна и та же бумага уедет в канал двадцать раз за день.
FLOW_COOLDOWN_MINUTES = 120

# Big trade: настоящий фильтр по размеру ОДНОЙ сделки из ленты (не средней).
# Кандидаты отбираются дёшево — принта на 30 млн не может быть в минуте,
# где всего прошло меньше 30 млн, — и только по ним читается /trades.json.
BIG_TRADE_MIN_VALUE = 30_000_000        # руб, размер одной сделки
BIG_TRADE_MAX_CANDIDATES = 25           # потолок доп. запросов в минуту

# Price spike: цена дёрнулась без сопоставимого объёма (тонкий стакан проткнули).
# По статистике это разворотный сигнал: 77% спайков вверх откатывают, медиана
# возврата к концу дня растёт монотонно с силой прокола (−0.57% на 1–1.5%,
# −3.51% на 5%+). Сильным считаем от SPIKE_STRONG_PRICE_PCT.
SPIKE_MIN_PRICE_PCT = 1.0               # |Δцены за минуту| ≥ 1%
SPIKE_STRONG_PRICE_PCT = 2.0            # выше — разворот заметно надёжнее
SPIKE_MIN_DELTA_VAL = 50_000            # руб, минимум — хоть что-то торговалось
SPIKE_MAX_DELTA_VS_MEAN = 3.0           # выше — это уже volume anomaly, не spike

# Спим с 23:50 до 06:50 MSK (между вечеркой и утренней сессией).
SLEEP_START_MIN = 23 * 60 + 50
SLEEP_END_MIN = 6 * 60 + 50

EXCLUDED_TICKER_PREFIXES = ("RU000",)
EXCLUDED_SHORTNAME_KEYWORDS = ("ETF",)

# Фильтр по типу инструмента. На TQBR помимо акций торгуются паи ЗПИФ/БПИФ
# (SECTYPE A/B/9) и ETF (J) — у них маркетмейкерская цена, которая за день не
# двигается, а оборот идёт редкими крупными блоками. Именно они давали 27 из 35
# block-сигналов (XSECUR, XKLUCH, XSBORA, XFMAIN) с forward return ровно 0.00%.
# Фильтрация по префиксу RU000 и слову ETF их не ловила: тикеры вида XSECUR
# выглядят как обычные акции.
#   1 — акция обыкновенная, 2 — привилегированная, D — депозитарная расписка.
ALLOWED_SECTYPES = ("1", "2", "D")

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
LAST_PRICES: Dict[str, float] = {}             # {ticker: LAST цена на прошлом замере}
SHORTNAMES: Dict[str, str] = {}                # {ticker: shortname}
EQUITIES: set = set()                          # тикеры с разрешённым SECTYPE
# COOLDOWNS keyed by (kind, ticker) — у каждого типа алерта свой кулдаун.
COOLDOWNS: Dict[Tuple[str, str], datetime] = {}
# FROZEN_BASELINES[ticker] = (mean, std, expires_at) — заморозка для volume-волны.
FROZEN_BASELINES: Dict[str, Tuple[float, float, datetime]] = {}
# Счётчик volume-алертов за торговый день: {ticker: count}, сбрасывается на ночь.
VOLUME_ALERTS_TODAY: Dict[str, int] = {}
# Оборот тикера на момент прошлого захода order_flow — для дельты за интервал.
FLOW_LAST_VALTODAY: Dict[str, float] = {}


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
    # Основной фильтр — SECTYPE из snapshot. Пока он не заполнен (самый первый
    # тик), не отсекаем ничего этим правилом, чтобы не ослепнуть на старте.
    if EQUITIES and secid not in EQUITIES:
        return True
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
    LAST_PRICES.clear()
    FROZEN_BASELINES.clear()
    VOLUME_ALERTS_TODAY.clear()
    FLOW_LAST_VALTODAY.clear()
    order_flow.reset()
    # SHORTNAMES и EQUITIES не чистим (меняются раз в вечность),
    # COOLDOWNS истекают по времени.


# ============================================================================
# API
# ============================================================================

def fetch_snapshot() -> Optional[Tuple[
    Dict[str, str], Dict[str, float], Dict[str, dict]
]]:
    """Один запрос на всю биржу: имена, VALTODAY и дневная картина.

    Возвращает (shortnames, valtoday, daily), где daily[ticker] = {
        last, open, low, high, last_to_prev (%), valtoday
    }. Поля могут быть None если данных нет.
    """
    params = {
        "iss.meta": "off",
        "iss.only": "securities,marketdata",
        "securities.columns": "SECID,SHORTNAME,BOARDID,SECTYPE",
        "marketdata.columns":
            "SECID,BOARDID,VALTODAY,LAST,OPEN,LOW,HIGH,LASTTOPREVPRICE",
    }
    try:
        r = requests.get(SNAPSHOT_URL, params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log(f"snapshot error: {e}")
        return None

    shortnames: Dict[str, str] = {}
    for secid, shortname, board, sectype in data.get("securities", {}).get("data", []):
        shortnames.setdefault(secid, shortname)
        # SECTYPE смотрим только на основном режиме: на SMAL/SPEQ у той же
        # бумаги встречаются свои строки, а нам нужен статус на TQBR.
        if board == "TQBR" and sectype in ALLOWED_SECTYPES:
            EQUITIES.add(secid)

    # Берём только основной режим TQBR. Параллельные режимы (SMAL — лот=1 шт.,
    # SPEQ и т.п.) имеют свои LAST/HIGH/LOW и крошечный оборот: одна сделка по
    # «нерыночной» цене на SMAL ловилась как spike, хотя на TQBR (главный график)
    # цена туда не ходила. См. GAZP 2026-06-02: SMAL LAST=117.19/HIGH=118.82
    # против TQBR LAST=116.30/HIGH=116.38 при обороте SMAL 6.5 тыс ₽ за день.
    valtoday: Dict[str, float] = {}
    daily: Dict[str, dict] = {}
    for secid, board, val, last, open_, low, high, last_to_prev in \
            data.get("marketdata", {}).get("data", []):
        if board != "TQBR":
            continue
        if val is not None:
            valtoday[secid] = float(val)
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

    return shortnames, valtoday, daily


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

def lenta_window_start(trades: list, minutes: float) -> Optional[str]:
    """Начало окна в ШКАЛЕ ЛЕНТЫ: (самая свежая сделка − minutes), как "HH:MM:SS".

    Публичный ISS отдаёт данные с лагом ~15 минут — это политика биржи, а не
    наша задержка. Лаг одинаков и у snapshot, и у ленты: замер 24.08.2026
    показал, что сумма VALUE сделок между двумя отметками ленты совпадает с
    ΔVALTODAY за тот же интервал с точностью 1.5%. То есть источники синхронны
    друг с другом и оба сдвинуты относительно стенных часов.

    Поэтому окно сделок нельзя отсчитывать от datetime.now(): «последняя
    минута» по стенным часам в ленте ещё не существует, и фильтр отдаёт пустоту.
    Отсчитываем от самой свежей сделки в самой ленте.
    """
    times = [t.get("TRADETIME") for t in trades if t.get("TRADETIME")]
    if not times:
        return None
    newest = max(times)
    try:
        dt = datetime.strptime(newest, "%H:%M:%S")
    except ValueError:
        return None
    return (dt - timedelta(minutes=minutes)).strftime("%H:%M:%S")


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
                # Вторая база сравнения: не «против соседних минут», а
                # «против этого же времени дня в прошлые сессии».
                "rel_volume": volume_profile.rel_volume(ticker, delta, now),
            }))

    anomalies.sort(key=lambda x: x[1]["z"], reverse=True)
    return anomalies


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


def pick_big_trade_candidates(deltas: Dict[str, float]) -> List[str]:
    """Бумаги, где принт на BIG_TRADE_MIN_VALUE физически мог пройти в эту минуту.

    Дешёвый предфильтр: если за минуту суммарно наторговали меньше порога, то
    одной сделки такого размера там точно не было — лента не нужна.
    """
    candidates = [
        (t, dv) for t, dv in deltas.items()
        if dv >= BIG_TRADE_MIN_VALUE and not is_excluded(t, SHORTNAMES.get(t, ""))
    ]
    candidates.sort(key=lambda x: x[1], reverse=True)
    return [t for t, _ in candidates[:BIG_TRADE_MAX_CANDIDATES]]


def detect_big_trades(ticker: str, trades: list, delta: float,
                      minutes: float = 1.0) -> Optional[dict]:
    """Найти в ленте сделки от BIG_TRADE_MIN_VALUE за последнюю минуту.

    В отличие от прежнего детектора по средней сделке (delta_VAL / delta_NUMTRADES),
    здесь смотрим размер каждой сделки: 40 принтов по 1 млн больше не выглядят
    как один принт на 40 млн.
    """
    since_str = lenta_window_start(trades, minutes)
    if since_str is None:
        return None
    big = [
        t for t in trades
        if (t.get("TRADETIME") or "") >= since_str
        and float(t.get("VALUE") or 0) >= BIG_TRADE_MIN_VALUE
    ]
    if not big:
        return None

    big.sort(key=lambda t: float(t.get("VALUE") or 0), reverse=True)
    largest = big[0]
    buy_value = sum(float(t["VALUE"]) for t in big if t.get("BUYSELL") == "B")
    sell_value = sum(float(t["VALUE"]) for t in big if t.get("BUYSELL") == "S")

    return {
        "shortname": SHORTNAMES.get(ticker, ""),
        "delta": delta,
        "count": len(big),
        "largest_value": float(largest.get("VALUE") or 0),
        "largest_price": float(largest["PRICE"]) if largest.get("PRICE") is not None else None,
        "largest_qty": int(largest["QUANTITY"]) if largest.get("QUANTITY") is not None else None,
        "largest_side": largest.get("BUYSELL"),
        "big_value": buy_value + sell_value,
        "buy_value": buy_value,
        "sell_value": sell_value,
        "trades": big[:3],
    }


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
            "strong": abs(change_pct) >= SPIKE_STRONG_PRICE_PCT,
        }))
    out.sort(key=lambda x: abs(x[1]["change_pct"]), reverse=True)
    return out


def analyze_ticker_trades(trades: list, minutes: float = 1.0) -> Optional[dict]:
    """Разложить сделки тикера за последнюю минуту на buy/sell + топ-3 по обороту.

    Окно берётся по шкале ленты (см. lenta_window_start): при отсчёте от
    datetime.now() сюда не попадала ни одна сделка, и блок с buy/sell в алерт
    не выводился вовсе.
    """
    since_str = lenta_window_start(trades, minutes)
    if since_str is None:
        return None

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


def _signal_direction(kind: str, info: dict, details: Optional[dict], daily: Optional[dict]) -> str:
    """Та же логика знака, что и у _direction_emoji, но как строка для лога."""
    if kind == "spike":
        pct = info["change_pct"]
    elif kind == "volume":
        pct = details.get("price_change_pct") if details else None
        if pct is None:
            pct = daily.get("last_to_prev") if daily else None
    elif kind == "flow":
        # Для потока «направление» — это сторона перекоса, а не движение цены.
        return "up" if info["imbalance"] > 0 else "down"
    elif kind == "bigtrade":
        # Сторона принта: по ней потом и считать, отрабатывают ли покупки крупным.
        side = info.get("largest_side")
        return "up" if side == "B" else "down" if side == "S" else "unknown"
    else:
        pct = daily.get("last_to_prev") if daily else None
    if pct is None:
        return "unknown"
    return "up" if pct > 0 else "down" if pct < 0 else "flat"


# Медиана изменения цены ОТ момента алерта ДО закрытия сессии. Пересчитано
# 25.08.2026 на 1556 спайках за 31.07–25.08, только акции (паи ЗПИФ/БПИФ с
# приколоченной ценой из выборки убраны). Ключ — нижняя граница |Δp| за минуту.
#
# Проколы ВВЕРХ откатывают тем сильнее, чем резче были: от 62% на слабых до
# 91% на 5%+, медиана монотонно растёт. Проколы ВНИЗ отскакивают заметно
# слабее и немонотонно (2–3% даёт столько же, сколько 3–5%), поэтому бакеты
# от 2% слиты в один. Ниже 1.5% отскока нет вовсе — см. _spike_expectation.
_SPIKE_STATS_UP = [(5.0, -3.7, 22), (3.0, -2.2, 112), (2.0, -1.6, 188),
                   (1.5, -1.2, 160), (1.0, -0.6, 366)]
_SPIKE_STATS_DOWN = [(2.0, +0.6, 181), (1.5, +0.2, 149)]


def _spike_expectation(info: dict) -> str:
    """Что исторически делала цена после такого прокола."""
    pct = abs(info["change_pct"])
    table = _SPIKE_STATS_UP if info["change_pct"] > 0 else _SPIKE_STATS_DOWN
    for lower, median, n in table:
        if pct >= lower:
            return (f"↩️ Исторически: медиана к закрытию <b>{median:+.1f}%</b> "
                    f"(n={n}) — движение чаще откатывает")
    # Сюда попадают только падения 1.0–1.5%: медиана к закрытию ровно 0.00%
    # на n=378, разворот в 56% случаев — то есть монетка. Обещать здесь откат
    # нельзя, это была бы неправда.
    return "↩️ Исторически: к закрытию <b>±0.0%</b> (n=378) — отскока нет, это шум"


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
    elif kind == "flow":
        head_color = _direction_emoji(None, info["imbalance"])
    elif kind == "bigtrade":
        # Цвет по стороне самого принта: событие — это покупка или продажа,
        # а не то, куда бумага шла с утра.
        side = info.get("largest_side")
        head_color = _direction_emoji(None, 1.0 if side == "B" else -1.0 if side == "S" else None)
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

    if kind == "bigtrade":
        side = info.get("largest_side")
        side_ru = {"B": "покупка", "S": "продажа"}.get(side, "")
        head = f"{head_color} 🧱 <b>{html.escape(ticker)}</b> — {shortname} · крупная сделка"
        largest = f"Сделка: <b>{format_number(info['largest_value'])} руб</b>"
        if info.get("largest_qty") is not None and info.get("largest_price") is not None:
            largest += (f" · {info['largest_qty']}@"
                        f"{format_price(info['largest_price'])}")
        if side_ru:
            largest += f" · {side_ru}"
        body = [largest]
        if info["count"] > 1:
            body.append(f"Всего таких сделок за минуту: {info['count']} "
                        f"(на {format_number(info['big_value'])} руб)")
        body.append(f"Оборот за минуту: {format_number(info['delta'])} руб")
    elif kind == "flow":
        side_ru = "набирают" if info["imbalance"] > 0 else "сливают"
        head = (f"{head_color} 🎯 <b>{html.escape(ticker)}</b> — {shortname} · "
                f"поток: {side_ru}")
        since_str = info["since"].strftime("%H:%M")
        body = [
            f"Покупки <b>{info['buy_pct']:.0f}%</b> / продажи "
            f"<b>{info['sell_pct']:.0f}%</b> с {since_str}",
            f"Перекос: {info['imbalance']*100:+.0f}% "
            f"на обороте {format_number(info['total_value'])} руб",
        ]
        if info.get("price_move_pct") is not None:
            body.append(f"Цена за это время: {info['price_move_pct']:+.2f}% "
                        f"— поток односторонний, а цена стоит")
    elif kind == "spike":
        arrow = "⬆️" if info["change_pct"] > 0 else "⬇️"
        strength = "сильный" if info.get("strong") else "слабый"
        head = (f"{head_color} ⚡ <b>{html.escape(ticker)}</b> — {shortname} · "
                f"прокол ({strength})")
        body = [
            f"Цена: {format_price(info['prev_price'])} → "
            f"<b>{format_price(info['new_price'])}</b> "
            f"{arrow} ({info['change_pct']:+.2f}% за мин)",
            f"Оборот за минуту: {format_number(info['delta'])} руб",
            _spike_expectation(info),
        ]
    else:
        multiplier = info["delta"] / info["mean"] if info["mean"] > 0 else 0
        head = f"{head_color} <b>{html.escape(ticker)}</b> — {shortname}"
        body = [
            f"Оборот за минуту: <b>{format_number(info['delta'])} руб</b> "
            f"(×{multiplier:.1f} от среднего)",
            f"Z-score: +{info['z']:.1f} | окно {info['window_size']} мин",
        ]
        rel = info.get("rel_volume")
        if rel is not None:
            body.append(f"К типичному для этого времени дня: <b>×{rel:.1f}</b>")

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

def run_order_flow(valtoday: Dict[str, float], now: datetime) -> list:
    """Раз в FLOW_SCAN_INTERVAL_MINUTES читать ленту по топ-бумагам и копить перекос."""
    if not order_flow.due_for_scan(now):
        return []

    watchlist = order_flow.pick_watchlist(valtoday, EQUITIES)
    for ticker in watchlist:
        current = valtoday.get(ticker)
        if current is None:
            continue
        prev = FLOW_LAST_VALTODAY.get(ticker)
        FLOW_LAST_VALTODAY[ticker] = current
        if prev is None or current <= prev:
            continue
        trades = fetch_ticker_trades(ticker)
        order_flow.update(
            ticker, trades,
            interval_value=current - prev,
            price=LAST_PRICES.get(ticker),
            now=now,
        )
    order_flow.mark_scanned(now)
    return order_flow.detect(LAST_PRICES, SHORTNAMES)


def tick() -> None:
    snapshot = fetch_snapshot()
    if snapshot is None:
        return
    shortnames, valtoday, daily = snapshot

    now = datetime.now()
    SHORTNAMES.update(shortnames)
    deltas = update_windows(valtoday)
    price_changes = compute_price_changes(daily)

    signal_log.update_forward_returns(LAST_PRICES, now)

    # Профиль объёма по времени дня копится всегда, независимо от алертов.
    volume_profile.accumulate(deltas, now)
    volume_profile.flush(now)
    if volume_profile.medians_loaded_for() != now.strftime("%Y-%m-%d"):
        volume_profile.prune(now)
        loaded = volume_profile.load_medians(now)
        log(f"volume profile reloaded: {loaded} (ticker, bucket) медиан")

    anomalies = detect_anomalies(deltas)
    spikes = detect_price_spikes(deltas, price_changes)

    # Крупные сделки: дешёвый предфильтр по обороту минуты, лента — только по
    # выжившим кандидатам.
    big_trades = []
    trades_cache: Dict[str, list] = {}
    for ticker in pick_big_trade_candidates(deltas):
        trades = fetch_ticker_trades(ticker)
        trades_cache[ticker] = trades
        info = detect_big_trades(ticker, trades, delta=deltas[ticker])
        if info:
            big_trades.append((ticker, info))
    big_trades.sort(key=lambda x: x[1]["largest_value"], reverse=True)

    flows = run_order_flow(valtoday, now)

    # Один тикер может попасть и в volume, и в bigtrade, и в spike одновременно —
    # отдаём приоритет volume, чтобы не дублировать. Поток (flow) живёт своей
    # логикой: он про часы накопления, а не про эту минуту, и не дублирует их.
    volume_tickers = {t for t, _ in anomalies}
    big_trades = [(t, i) for t, i in big_trades if t not in volume_tickers]
    seen = volume_tickers | {t for t, _ in big_trades}
    spikes = [(t, i) for t, i in spikes if t not in seen]

    parts = []
    if anomalies:
        parts.append("volume: " + ", ".join(
            f"{t}(z={i['z']:.1f})" for t, i in anomalies))
    if big_trades:
        parts.append("bigtrade: " + ", ".join(
            f"{t}({format_number(i['largest_value'])})" for t, i in big_trades))
    if spikes:
        parts.append("spike: " + ", ".join(
            f"{t}({i['change_pct']:+.2f}%)" for t, i in spikes))
    if flows:
        parts.append("flow: " + ", ".join(
            f"{t}({i['imbalance']*100:+.0f}%)" for t, i in flows))
    if parts:
        log(f"snapshot OK {len(valtoday)} tickers · " + " | ".join(parts))
    else:
        log(f"snapshot OK {len(valtoday)} tickers · no anomalies")

    if not (anomalies or big_trades or spikes or flows):
        return

    market_change_pct = fetch_index_context()

    def maybe_send(kind: str, ticker: str, info: dict, *, fetch_extras: bool) -> None:
        # Пока volume-волна заморожена — кулдаун игнорируем, шлём каждую минуту.
        frozen = FROZEN_BASELINES.get(ticker) if kind == "volume" else None
        in_wave = bool(frozen and frozen[2] > now)
        if not in_wave and COOLDOWNS.get((kind, ticker), datetime.min) > now:
            return
        # Дневной потолок для volume действует и внутри волны — иначе одна
        # длинная волна по-прежнему выгружала бы в канал десятки сообщений.
        if kind == "volume" and VOLUME_ALERTS_TODAY.get(ticker, 0) >= MAX_VOLUME_ALERTS_PER_DAY:
            return
        details = None
        orderbook = None
        if fetch_extras:
            trades = trades_cache.get(ticker)
            if trades is None:
                trades = fetch_ticker_trades(ticker)
                trades_cache[ticker] = trades
            details = analyze_ticker_trades(trades)
            orderbook = fetch_orderbook(ticker)
        ticker_daily = daily.get(ticker)
        msg = format_alert(
            ticker, info, details,
            ticker_daily, market_change_pct, orderbook,
            kind=kind,
        )
        if send_telegram(msg):
            # После волны блокируем тикер ещё на COOLDOWN_MINUTES за пределами freeze.
            cd_minutes = FLOW_COOLDOWN_MINUTES if kind == "flow" else COOLDOWN_MINUTES
            cd_until = (frozen[2] if in_wave else now) + timedelta(minutes=cd_minutes)
            COOLDOWNS[(kind, ticker)] = cd_until
            if kind == "volume":
                VOLUME_ALERTS_TODAY[ticker] = VOLUME_ALERTS_TODAY.get(ticker, 0) + 1
            log(f"alert sent: {kind}/{ticker}")
            if kind == "volume":
                metric_name, metric_value = "z_score", info["z"]
            elif kind == "bigtrade":
                metric_name, metric_value = "largest_trade_value", info["largest_value"]
            else:  # flow
                metric_name, metric_value = "imbalance", info["imbalance"]
            signal_log.log_signal(
                kind=kind,
                ticker=ticker,
                shortname=info["shortname"],
                price=ticker_daily.get("last") if ticker_daily else None,
                metric_name=metric_name,
                metric_value=metric_value,
                direction=_signal_direction(kind, info, details, ticker_daily),
                market_change_pct=market_change_pct,
                now=now,
            )

    for ticker, info in anomalies:
        maybe_send("volume", ticker, info, fetch_extras=True)
    for ticker, info in big_trades:
        # Лента по кандидату уже прочитана в trades_cache — стакан тоже полезен,
        # видно, съели ли принтом всю плотность.
        maybe_send("bigtrade", ticker, info, fetch_extras=True)
    for ticker, info in flows:
        # Поток — это итог накопления за часы, разовые детали минуты не нужны.
        maybe_send("flow", ticker, info, fetch_extras=False)
    for ticker, info in spikes:
        # Для spike стакан полезен (увидеть тонкое место), trades — нет.
        if COOLDOWNS.get(("spike", ticker), datetime.min) > now:
            continue
        orderbook = fetch_orderbook(ticker)
        ticker_daily = daily.get(ticker)
        msg = format_alert(
            ticker, info, None,
            ticker_daily, market_change_pct, orderbook,
            kind="spike",
        )
        if send_telegram(msg):
            COOLDOWNS[("spike", ticker)] = now + timedelta(minutes=COOLDOWN_MINUTES)
            log(f"alert sent: spike/{ticker}")
            signal_log.log_signal(
                kind="spike",
                ticker=ticker,
                shortname=info["shortname"],
                price=info["new_price"],
                metric_name="change_pct",
                metric_value=info["change_pct"],
                direction=_signal_direction("spike", info, None, ticker_daily),
                market_change_pct=market_change_pct,
                now=now,
            )


def main() -> None:
    log("MOEX intraday monitor started")
    log(f"volume: z>{ANOMALY_THRESHOLD_SIGMA}, dev>{MIN_DEVIATION_PERCENT}%, "
        f"mean>={format_number(MIN_AVG_MINUTE_VALUE)}/мин · freeze {VOLUME_FREEZE_MINUTES} min "
        f"· max {MAX_VOLUME_ALERTS_PER_DAY}/день на тикер")
    log(f"bigtrade: одна сделка >={format_number(BIG_TRADE_MIN_VALUE)} руб "
        f"· до {BIG_TRADE_MAX_CANDIDATES} кандидатов/мин")
    log(f"spike: |Δp|>={SPIKE_MIN_PRICE_PCT}% за мин (сильный от "
        f"{SPIKE_STRONG_PRICE_PCT}%), val>={format_number(SPIKE_MIN_DELTA_VAL)}")
    log(f"flow: топ-{order_flow.FLOW_WATCH_TOP_N} каждые "
        f"{order_flow.FLOW_SCAN_INTERVAL_MINUTES} мин · перекос "
        f">={order_flow.FLOW_MIN_IMBALANCE*100:.0f}% на "
        f"{format_number(order_flow.FLOW_MIN_SESSION_VALUE)}")
    log(f"window: {WINDOW_MINUTES} min · cooldown: {COOLDOWN_MINUTES} min "
        f"(flow {FLOW_COOLDOWN_MINUTES} min)")
    log(f"instruments: только SECTYPE {'/'.join(ALLOWED_SECTYPES)} на TQBR")
    log("sleep window: 23:50–06:50 MSK")

    signal_log.init_db()
    volume_profile.init_db()
    reset_state()
    sleeping = False

    while True:
        try:
            if is_sleep_time():
                if not sleeping:
                    log("entering night sleep window, resetting state")
                    now = datetime.now()
                    signal_log.finalize_eod(LAST_PRICES, now)
                    # Досыпать хвост профиля до сброса состояния, иначе
                    # последний бакет вечерней сессии теряется каждый день.
                    written = volume_profile.flush(now, force=True)
                    log(f"volume profile flushed: {written} строк")
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
