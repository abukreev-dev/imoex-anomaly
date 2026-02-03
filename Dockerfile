FROM python:3.11-slim

WORKDIR /app

# Установка зависимостей системы
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    nginx \
    && rm -rf /var/lib/apt/lists/*

# Копирование и установка Python зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование приложения
COPY detector.py .
COPY web/ web/

# Создание директорий для данных и отчетов
RUN mkdir -p /app/data /app/reports

# Настройка nginx
COPY nginx.conf /etc/nginx/sites-available/default

# Копирование скриптов
COPY entrypoint.sh /entrypoint.sh
COPY cron/detector-cron /etc/cron.d/detector-cron
RUN chmod +x /entrypoint.sh && \
    chmod 0644 /etc/cron.d/detector-cron && \
    crontab /etc/cron.d/detector-cron

# Переменные окружения
ENV PYTHONUNBUFFERED=1
ENV TZ=Europe/Moscow

EXPOSE 80

VOLUME ["/app/data", "/app/reports"]

ENTRYPOINT ["/entrypoint.sh"]
