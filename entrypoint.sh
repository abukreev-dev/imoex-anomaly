#!/bin/bash
set -e

echo "=== MOEX Anomaly Detector ==="
echo "Starting services..."

# Создание директорий если не существуют
mkdir -p /app/data /app/reports

# Генерация index.html для веб-интерфейса
python /app/web/generate_index.py

# Запуск cron
echo "Starting cron..."
cron

# Запуск nginx
echo "Starting nginx on port 80..."
nginx -g 'daemon off;'
