#!/usr/bin/env python3
"""
Telegram уведомления об аномалиях
"""

import os
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta

try:
    import requests
except ImportError:
    print("Ошибка: требуется библиотека requests")
    sys.exit(1)

# Настройки из переменных окружения
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

REPORTS_DIR = Path("/app/reports")


def get_latest_report():
    """Получить последний JSON отчет"""
    # По умолчанию - вчерашний день
    yesterday = datetime.now() - timedelta(days=1)
    date_str = yesterday.strftime("%Y-%m-%d")

    report_path = REPORTS_DIR / f"anomalies_{date_str}.json"

    if not report_path.exists():
        # Попробуем найти любой последний отчет
        reports = sorted(REPORTS_DIR.glob("anomalies_*.json"), reverse=True)
        if reports:
            report_path = reports[0]
        else:
            return None

    with open(report_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def format_number(num):
    """Форматировать число"""
    if num >= 1_000_000_000:
        return f"{num / 1_000_000_000:.1f} млрд"
    elif num >= 1_000_000:
        return f"{num / 1_000_000:.1f} млн"
    else:
        return f"{num:,.0f}"


def format_telegram_message(report):
    """Сформировать сообщение для Telegram"""
    meta = report["metadata"]
    anomalies = report["anomalies"]

    lines = []
    lines.append(f"📊 *Аномалии объемов торгов*")
    lines.append(f"📅 Дата: {meta['analysis_date']}")
    lines.append(f"📉 Базовый период: {meta['base_period_start']} — {meta['base_period_end']}")
    lines.append(f"🎯 Порог: {meta['threshold_sigma']}σ")
    lines.append("")

    if anomalies:
        lines.append(f"🔥 *Найдено аномалий: {len(anomalies)}*")
        lines.append("")

        # Показываем топ-10 аномалий
        for item in anomalies[:10]:
            emoji = "🚀" if item["z_score"] >= 3 else "📈"
            lines.append(
                f"{emoji} *{item['ticker']}* — {item['shortname']}\n"
                f"   💰 {format_number(item['current_value'])} руб\n"
                f"   📊 Z-score: {item['z_score']:+.2f} | {item['deviation_percent']:+.1f}%"
            )
            lines.append("")

        if len(anomalies) > 10:
            lines.append(f"_...и еще {len(anomalies) - 10} аномалий_")
    else:
        lines.append("✅ Аномалий не обнаружено")

    lines.append("")
    lines.append(f"📋 Всего тикеров: {meta['total_tickers']}")

    return "\n".join(lines)


def send_telegram_message(text):
    """Отправить сообщение в Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  Telegram не настроен: установите TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        print("✓ Уведомление отправлено в Telegram")
        return True
    except requests.exceptions.RequestException as e:
        print(f"❌ Ошибка отправки в Telegram: {e}")
        return False


def main():
    print("=== Telegram Notification ===")

    report = get_latest_report()
    if not report:
        print("Отчет не найден")
        return

    message = format_telegram_message(report)

    # Отправляем только если есть аномалии или это принудительная отправка
    if report["anomalies"] or os.environ.get("NOTIFY_ALWAYS"):
        send_telegram_message(message)
    else:
        print("Аномалий нет, уведомление не отправлено")
        print("(установите NOTIFY_ALWAYS=1 для отправки всех отчетов)")


if __name__ == "__main__":
    main()
