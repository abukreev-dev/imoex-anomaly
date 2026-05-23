# Intraday volume monitor

Мониторинг **внутридневных** аномалий объёмов торгов на Мосбирже. Раз в минуту
снимает snapshot всей биржи, считает дельту накопленного оборота по каждому
тикеру и при резком всплеске шлёт алерт в Telegram-канал.

Это **второй независимый режим** проекта — в дополнение к дневному `detector.py`
(см. [README](README.md)). Они не share-ят кеш и работают изолированно.

## Чем отличается от dаily detector

|                     | `detector.py` (daily)             | `monitor.py` (intraday)               |
|---------------------|-----------------------------------|---------------------------------------|
| Источник            | `/iss/history/...` (закрытый день)| `/iss/.../securities.json` (snapshot) |
| Частота             | 1 раз в день по cron              | 1 раз в минуту                        |
| База сравнения      | 5 предыдущих торговых дней        | Скользящее окно 30 минут той же сессии|
| Сигналы             | Объём                             | Volume + block trade + price spike    |
| Разделение buy/sell | Нет                               | Да, по `/trades.json` для volume      |
| Деплой              | Docker / Coolify                  | systemd на linux-сервере              |
| Telegram            | `notify.py` (без прокси)          | `monitor.py` (с поддержкой прокси)    |

## Как работает алгоритм

Каждую минуту делается один лёгкий запрос на всю биржу:
`GET /iss/engines/stock/markets/shares/securities.json` с фильтром
`iss.only=securities,marketdata`. В ответе — `SECID`, `SHORTNAME`,
`VALTODAY`, `NUMTRADES`, `LAST`, `OPEN`, `LOW`, `HIGH`, `LASTTOPREVPRICE`
для всех ~500 инструментов.

Из этого считаются три независимых сигнала.

### 1. Volume anomaly — повышенный оборот

- Дельта = `VALTODAY_сейчас − VALTODAY_прошлая_минута`
- Скользящее окно последних 30 минут (per ticker), mean/stdev по окну
  **без текущей точки**
- Триггер (все три условия):
  - `z-score > ANOMALY_THRESHOLD_SIGMA` (по умолчанию 5.0)
  - `deviation > MIN_DEVIATION_PERCENT` (по умолчанию 500%)
  - `mean >= MIN_AVG_MINUTE_VALUE` (по умолчанию 200 тыс руб/мин)
- При срабатывании — +1 запрос `/trades.json` и +1 `/orderbook.json` для
  обогащения алерта buy/sell, топ-3 сделками и стаканом.

### 2. Block trade — одна крупная сделка

- Дельта `NUMTRADES` за минуту → средний размер сделки
  `delta_VALTODAY / delta_NUMTRADES`
- Триггер (оба условия):
  - `delta_VALTODAY ≥ BLOCK_MIN_MINUTE_VALUE` (по умолчанию 5 млн руб)
  - `средний размер сделки ≥ BLOCK_MIN_AVG_TRADE_SIZE` (по умолчанию 2 млн)
- Дополнительных запросов нет — данные уже в snapshot.

### 3. Price spike — резкое движение цены при обычном/малом объёме

- Дельта `LAST` за минуту в процентах
- Триггер (все три условия):
  - `|Δp| ≥ SPIKE_MIN_PRICE_PCT` (по умолчанию 1%)
  - `delta_VALTODAY ≥ SPIKE_MIN_DELTA_VAL` (по умолчанию 50 тыс — хоть что-то торговалось)
  - оборот **не** превышает `SPIKE_MAX_DELTA_VS_MEAN × mean` (иначе это уже volume anomaly)
- При срабатывании — +1 запрос `/orderbook.json`, чтобы увидеть тонкое место.

### Приоритет и кулдауны

Если по тикеру одновременно сработали volume + block + spike — отдаётся
приоритет volume, потом block, потом spike (без дубля). Кулдаун **30 минут
на (тип, тикер)**, т.е. volume и spike по одному тикеру не блокируют друг
друга, если разнесены по времени.

### IMOEX-контекст

При наличии любого алерта в тике подгружается `LASTCHANGEPRC` индекса
IMOEX (1 запрос на тик, не на тикер), чтобы видеть рынок-контекст.

## Окно сна 23:50–06:50 MSK

В это окно биржа закрыта (между вечеркой и утренней сессией). Сервис не
делает запросы и логирует `entering night sleep window`. При засыпании и
пробуждении сбрасываются окна и кеш `VALTODAY` — иначе утром первая дельта
будет огромным минусом из-за обнуления `VALTODAY` в новом торговом дне.

Выходные/праздники отдельно не учитываются: вне сессии MOEX отдаёт
неизменный `VALTODAY` → дельты ноль → окно заполняется нулями → аномалий
не находится.

## Настройки

Все параметры — в начале `monitor.py`:

```python
# volume anomaly
ANOMALY_THRESHOLD_SIGMA   = 5.0
MIN_DEVIATION_PERCENT     = 500
MIN_AVG_MINUTE_VALUE      = 200_000      # руб/мин
WINDOW_MINUTES            = 30
MIN_POINTS_FOR_STATS      = 10

# block trade
BLOCK_MIN_MINUTE_VALUE    = 5_000_000    # руб
BLOCK_MIN_AVG_TRADE_SIZE  = 2_000_000    # руб/сделка

# price spike
SPIKE_MIN_PRICE_PCT       = 1.0          # %
SPIKE_MIN_DELTA_VAL       = 50_000       # руб
SPIKE_MAX_DELTA_VS_MEAN   = 3.0          # × от mean окна

COOLDOWN_MINUTES          = 30           # на (kind, ticker)
SLEEP_START_MIN           = 23 * 60 + 50
SLEEP_END_MIN             = 6  * 60 + 50
```

После изменения — `systemctl restart imoex-monitor`.

## Переменные окружения

В `/etc/imoex-monitor.env` (см. `deploy/imoex-monitor.env.example`):

| Переменная           | Обязательная | Описание                                     |
|----------------------|--------------|----------------------------------------------|
| `TELEGRAM_BOT_TOKEN` | да           | Токен бота от @BotFather                     |
| `TELEGRAM_CHAT_ID`   | да           | `@channelname` или числовой `-100...`        |
| `TELEGRAM_PROXY`     | нет          | HTTP-прокси для TG (только TG, не MOEX)      |

`TELEGRAM_PROXY` нужен, если на сервере прямой доступ к `api.telegram.org`
закрыт — например, RKN-блокировка обходится через локальный squid.
Формат: `http://host:port`, при наличии auth — `http://user:pass@host:port`.

## Деплой

На целевом linux-сервере (Ubuntu/Debian, под root):

```bash
git clone https://github.com/abukreev-dev/imoex-anomaly.git /opt/imoex-anomaly
bash /opt/imoex-anomaly/deploy/install.sh
```

`install.sh` делает:
- `apt-get install python3 python3-requests git`
- кладёт unit-файл в `/etc/systemd/system/imoex-monitor.service`
- создаёт шаблон `/etc/imoex-monitor.env` (chmod 600)
- `systemctl enable imoex-monitor`

Дальше нужно заполнить env (`nano /etc/imoex-monitor.env`), добавить бота
админом канала с правом постить, и запустить:

```bash
systemctl start imoex-monitor
journalctl -u imoex-monitor -f
```

## Управление сервисом

```bash
systemctl {start|stop|restart|status} imoex-monitor
journalctl -u imoex-monitor -f                    # живой лог
journalctl -u imoex-monitor --since "10 min ago"  # за период
```

Обновление кода:

```bash
cd /opt/imoex-anomaly && git pull && systemctl restart imoex-monitor
```

## Прогрев

После каждого старта/пробуждения окна пустые. Аномалии физически не могут
детектироваться первые ~10 минут (`MIN_POINTS_FOR_STATS`). Это нормально.

## Что в логах

Норма:

```
[12:34:00] snapshot OK 487 tickers · no anomalies
[12:35:00] snapshot OK 487 tickers · volume: SBER(z=8.4) | block: GAZP(avg=4.1 млн) | spike: AFLT(+1.4%)
[12:35:01] alert sent: volume/SBER
[12:35:02] alert sent: block/GAZP
[12:35:02] alert sent: spike/AFLT
```

Проблемы:

- `snapshot error: ...` — MOEX недоступен/тротлит. Если разово — игнор;
  если постоянно — проверить сеть.
- `telegram error: ...` — TG/прокси недоступен. Алерт пропадёт, но детектор
  продолжит работать. Проверить `TELEGRAM_PROXY` и доступность канала.
- `entering night sleep window` — ожидаемое поведение в 23:50–06:50 MSK.

## Фильтры из коробки

Из анализа исключаются:
- Тикеры с префиксом `RU000` (облигации, ISIN коды)
- Тикеры с `ETF` в shortname

Конфигурируются константами `EXCLUDED_TICKER_PREFIXES` и
`EXCLUDED_SHORTNAME_KEYWORDS` в `monitor.py`.
