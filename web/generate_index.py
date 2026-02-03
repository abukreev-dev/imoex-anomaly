#!/usr/bin/env python3
"""
Генератор HTML страницы со списком отчетов
"""

import os
from pathlib import Path
from datetime import datetime

REPORTS_DIR = Path("/app/reports")
INDEX_FILE = REPORTS_DIR / "index.html"

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MOEX Anomaly Detector - Отчеты</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            color: #e0e0e0;
            padding: 20px;
        }}
        .container {{
            max-width: 900px;
            margin: 0 auto;
        }}
        h1 {{
            text-align: center;
            margin-bottom: 10px;
            color: #00d4ff;
            font-size: 2em;
        }}
        .subtitle {{
            text-align: center;
            color: #888;
            margin-bottom: 30px;
        }}
        .stats {{
            display: flex;
            justify-content: center;
            gap: 30px;
            margin-bottom: 30px;
        }}
        .stat-box {{
            background: rgba(255,255,255,0.05);
            border-radius: 10px;
            padding: 15px 25px;
            text-align: center;
        }}
        .stat-number {{
            font-size: 2em;
            font-weight: bold;
            color: #00d4ff;
        }}
        .stat-label {{
            color: #888;
            font-size: 0.9em;
        }}
        .reports-list {{
            background: rgba(255,255,255,0.03);
            border-radius: 15px;
            padding: 20px;
        }}
        .report-item {{
            display: flex;
            align-items: center;
            padding: 15px;
            border-bottom: 1px solid rgba(255,255,255,0.1);
            transition: background 0.2s;
        }}
        .report-item:hover {{
            background: rgba(255,255,255,0.05);
        }}
        .report-item:last-child {{
            border-bottom: none;
        }}
        .report-date {{
            font-size: 1.2em;
            font-weight: 600;
            color: #fff;
            width: 150px;
        }}
        .report-links {{
            display: flex;
            gap: 10px;
            margin-left: auto;
        }}
        .report-link {{
            padding: 8px 16px;
            border-radius: 6px;
            text-decoration: none;
            font-size: 0.9em;
            transition: all 0.2s;
        }}
        .link-txt {{
            background: #2d5016;
            color: #90EE90;
        }}
        .link-txt:hover {{
            background: #3d6820;
        }}
        .link-json {{
            background: #1e3a5f;
            color: #87CEEB;
        }}
        .link-json:hover {{
            background: #2a4a70;
        }}
        .empty {{
            text-align: center;
            color: #666;
            padding: 50px;
        }}
        .refresh-info {{
            text-align: center;
            color: #555;
            margin-top: 20px;
            font-size: 0.85em;
        }}
        .manual-run {{
            text-align: center;
            margin-top: 20px;
        }}
        .manual-run code {{
            background: rgba(255,255,255,0.1);
            padding: 10px 20px;
            border-radius: 6px;
            font-family: monospace;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 MOEX Anomaly Detector</h1>
        <p class="subtitle">Детектор аномальных объемов торгов на Московской Бирже</p>

        <div class="stats">
            <div class="stat-box">
                <div class="stat-number">{total_reports}</div>
                <div class="stat-label">Отчетов</div>
            </div>
            <div class="stat-box">
                <div class="stat-number">{last_date}</div>
                <div class="stat-label">Последний</div>
            </div>
        </div>

        <div class="reports-list">
            {reports_html}
        </div>

        <p class="refresh-info">
            Обновлено: {updated_at}<br>
            Автоматический анализ: ежедневно в 10:00 MSK (Пн-Пт)
        </p>
    </div>
</body>
</html>
"""

REPORT_ITEM_TEMPLATE = """
<div class="report-item">
    <span class="report-date">{date}</span>
    <div class="report-links">
        <a href="anomalies_{date}.txt" class="report-link link-txt">📄 TXT</a>
        <a href="anomalies_{date}.json" class="report-link link-json">📋 JSON</a>
    </div>
</div>
"""


def get_report_dates():
    """Получить список дат отчетов"""
    dates = set()

    if not REPORTS_DIR.exists():
        return []

    for file in REPORTS_DIR.glob("anomalies_*.txt"):
        # Извлечь дату из имени файла
        date_str = file.stem.replace("anomalies_", "")
        dates.add(date_str)

    return sorted(dates, reverse=True)


def generate_index():
    """Сгенерировать index.html"""
    dates = get_report_dates()

    if dates:
        reports_html = "\n".join(
            REPORT_ITEM_TEMPLATE.format(date=date) for date in dates
        )
        last_date = dates[0]
    else:
        reports_html = '<p class="empty">Отчетов пока нет. Запустите анализ командой:<br><code>python detector.py --date YYYY-MM-DD</code></p>'
        last_date = "—"

    html = HTML_TEMPLATE.format(
        total_reports=len(dates),
        last_date=last_date,
        reports_html=reports_html,
        updated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

    REPORTS_DIR.mkdir(exist_ok=True)
    with open(INDEX_FILE, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"Generated: {INDEX_FILE}")


if __name__ == "__main__":
    generate_index()
