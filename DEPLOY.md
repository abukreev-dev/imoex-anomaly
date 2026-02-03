# Деплой в Coolify

## Быстрый старт

### 1. Подготовка в Coolify

1. Откройте Coolify Dashboard
2. Перейдите в **Projects** → выберите проект (или создайте новый)
3. Нажмите **+ New** → **Docker Compose**

### 2. Настройка источника

**Вариант A: Из GitHub**
- Source: GitHub
- Repository: `abukreev-dev/imoex-anomaly`
- Branch: `main`

**Вариант B: Из локального Dockerfile**
- Source: Docker Compose
- Вставьте содержимое `docker-compose.yml`

### 3. Переменные окружения (Environment Variables)

Добавьте в Coolify:

```
TZ=Europe/Moscow

# Telegram уведомления (опционально)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Отправлять уведомления даже без аномалий
NOTIFY_ALWAYS=0
```

### 4. Настройка Telegram бота

1. Создайте бота через [@BotFather](https://t.me/BotFather):
   - Отправьте `/newbot`
   - Выберите имя и username
   - Скопируйте токен

2. Получите Chat ID:
   - Добавьте бота в группу или напишите ему
   - Откройте: `https://api.telegram.org/bot<TOKEN>/getUpdates`
   - Найдите `"chat":{"id":...}`

### 5. Порты и домен

В Coolify настройте:
- **Port**: 80
- **Domain**: например `anomaly.yourdomain.com`

### 6. Volumes (хранилище)

Coolify автоматически создаст volumes для:
- `/app/data` — кеш данных с MOEX
- `/app/reports` — сгенерированные отчеты

## Ручной запуск анализа

Через Coolify Execute Command или SSH:

```bash
# Анализ вчерашнего дня
docker exec moex-anomaly-detector python /app/detector.py

# Анализ конкретной даты
docker exec moex-anomaly-detector python /app/detector.py --date 2026-01-31

# Инициализация (загрузка 60 дней)
docker exec moex-anomaly-detector python /app/detector.py --init --days 60

# Принудительная отправка в Telegram
docker exec moex-anomaly-detector python /app/notify.py
```

## Расписание (Cron)

По умолчанию анализ запускается:
- **Время**: 10:00 MSK
- **Дни**: Понедельник — Пятница

Изменить расписание: отредактируйте `cron/detector-cron`

```
# Формат: минуты часы день месяц день_недели
0 10 * * 1-5    # 10:00 Пн-Пт
0 9 * * *       # 09:00 каждый день
30 18 * * 1-5   # 18:30 Пн-Пт
```

## Локальная разработка

```bash
# Сборка и запуск
docker-compose up --build

# Открыть в браузере
open http://localhost:8080
```

## Структура проекта

```
imoex-anomaly/
├── detector.py          # Основной скрипт анализа
├── notify.py            # Telegram уведомления
├── Dockerfile           # Сборка контейнера
├── docker-compose.yml   # Конфигурация сервисов
├── nginx.conf           # Веб-сервер для отчетов
├── entrypoint.sh        # Скрипт запуска
├── cron/
│   └── detector-cron    # Расписание cron
├── web/
│   └── generate_index.py # Генератор HTML
├── data/                # Кеш данных (volume)
└── reports/             # Отчеты (volume)
```

## Troubleshooting

### Логи
```bash
docker logs moex-anomaly-detector
docker logs moex-anomaly-detector -f  # follow
```

### Проверка cron
```bash
docker exec moex-anomaly-detector cat /var/log/detector.log
```

### Проверка данных
```bash
docker exec moex-anomaly-detector ls -la /app/data/
docker exec moex-anomaly-detector ls -la /app/reports/
```
