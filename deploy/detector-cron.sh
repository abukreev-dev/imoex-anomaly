#!/bin/bash
# Ежедневный прогон daily-детектора. Вызывается из /etc/cron.d/imoex-detector,
# руками запускается так же: /opt/imoex-anomaly/deploy/detector-cron.sh
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

echo "=== $(date '+%Y-%m-%d %H:%M:%S') запуск детектора ==="

# Детектор сам решает, за какую дату считать (по умолчанию — вчера), и
# пишет отчёты в reports/. Если он упал, слать в Telegram нечего.
if ! python3 detector.py; then
    echo "!!! detector.py завершился с ошибкой, уведомление не отправляю"
    exit 1
fi

python3 web/generate_index.py
python3 notify.py

echo "=== $(date '+%Y-%m-%d %H:%M:%S') готово ==="
