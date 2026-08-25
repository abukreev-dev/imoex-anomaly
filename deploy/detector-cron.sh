#!/bin/bash
# Ежедневный прогон daily-детектора. Вызывается из /etc/cron.d/imoex-detector,
# руками запускается так же: /opt/imoex-anomaly/deploy/detector-cron.sh
#
# Скрипт идемпотентный: отчёт за целевой день делается и отправляется РОВНО
# один раз. Поэтому cron может дёргать его несколько раз за утро — первая
# удачная попытка сделает работу, остальные тихо выйдут. Это нужно потому,
# что точное время публикации истории на ISS не гарантировано: в ранний слот
# данных может ещё не быть.
#
# Env берём из того же файла, что и intraday-монитор: бот, канал и прокси
# у них общие. Прокси нужен только для Telegram — MOEX ходит напрямую.

set -uo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="/etc/imoex-monitor.env"

cd "${INSTALL_DIR}" || exit 1

if [ -f "${ENV_FILE}" ]; then
    set -a
    # shellcheck disable=SC1090
    . "${ENV_FILE}"
    set +a
fi

TARGET_DATE="$(python3 detector.py --print-target-date)"
if [ -z "${TARGET_DATE}" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') не смог определить целевую дату"
    exit 1
fi

# Отчёт за этот день уже готов — значит его уже и отправили.
if [ -f "reports/anomalies_${TARGET_DATE}.json" ]; then
    exit 0
fi

echo "=== $(date '+%Y-%m-%d %H:%M:%S') запуск детектора за ${TARGET_DATE} ==="

python3 detector.py
rc=$?

if [ ${rc} -eq 3 ]; then
    # Истории за целевой день ещё нет (или это выходной). Не ошибка:
    # молча уступаем следующему слоту.
    echo "Данных за ${TARGET_DATE} пока нет, попробуем в следующий раз"
    exit 0
fi

if [ ${rc} -ne 0 ]; then
    echo "!!! detector.py завершился с кодом ${rc}, уведомление не отправляю"
    exit 1
fi

python3 web/generate_index.py
python3 notify.py "${TARGET_DATE}"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') готово ==="
