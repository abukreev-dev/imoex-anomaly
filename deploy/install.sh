#!/bin/bash
# Запускать на сервере под root.
# Локально: scp -r deploy/ root@home:/tmp/imoex-deploy && ssh root@home bash /tmp/imoex-deploy/install.sh
# Либо: ssh root@home, склонировать репозиторий и запустить bash deploy/install.sh.

set -euo pipefail

REPO_URL="https://github.com/abukreev-dev/imoex-anomaly.git"
INSTALL_DIR="/opt/imoex-anomaly"
ENV_FILE="/etc/imoex-monitor.env"
SERVICE_NAME="imoex-monitor"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "==> Установка зависимостей"
apt-get update -qq
apt-get install -y -qq python3 python3-requests git

echo "==> Код в ${INSTALL_DIR}"
if [ ! -d "${INSTALL_DIR}/.git" ]; then
    git clone "${REPO_URL}" "${INSTALL_DIR}"
else
    git -C "${INSTALL_DIR}" pull --ff-only
fi

echo "==> Env-файл ${ENV_FILE}"
if [ ! -f "${ENV_FILE}" ]; then
    cp "${INSTALL_DIR}/deploy/imoex-monitor.env.example" "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
    echo "    создан шаблон — впиши TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID"
else
    echo "    уже существует, оставляю как есть"
fi

echo "==> systemd unit"
cp "${INSTALL_DIR}/deploy/${SERVICE_NAME}.service" "${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null

# Если токены не заполнены — не стартуем, чтобы не плодить ошибки в journal.
if grep -Eq '^TELEGRAM_BOT_TOKEN=$|^TELEGRAM_BOT_TOKEN=""' "${ENV_FILE}" \
   || grep -Eq '^TELEGRAM_CHAT_ID=$|^TELEGRAM_CHAT_ID=""' "${ENV_FILE}"; then
    echo ""
    echo "⚠️  Не запускаю сервис: токены в ${ENV_FILE} не заполнены."
    echo "    После заполнения: systemctl start ${SERVICE_NAME}"
    echo "    Логи: journalctl -u ${SERVICE_NAME} -f"
    exit 0
fi

echo "==> Запуск сервиса"
systemctl restart "${SERVICE_NAME}"
sleep 2
systemctl --no-pager status "${SERVICE_NAME}" || true

echo ""
echo "Готово. Логи: journalctl -u ${SERVICE_NAME} -f"
