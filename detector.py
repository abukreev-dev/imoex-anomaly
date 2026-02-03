#!/usr/bin/env python3
"""
Детектор аномальных объемов торгов на Московской Бирже
Анализирует обороты (VALUE) по акциям и выявляет аномально высокие объемы
"""

import json
import os
import sys
import time
import argparse
import statistics
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode
from pathlib import Path

try:
    import requests
except ImportError:
    print("Ошибка: требуется библиотека requests")
    print("Установите: pip install requests")
    sys.exit(1)

# ============================================================================
# НАСТРОЙКИ
# ============================================================================

# Порог аномалии в стандартных отклонениях (σ)
ANOMALY_THRESHOLD_SIGMA = 3.0

# Минимальное отклонение для попадания в отчет (%)
MIN_DEVIATION_PERCENT = 300

# Минимальный средний дневной оборот для анализа (руб)
MIN_AVG_VALUE = 10_000_000  # 10 млн руб

# Префиксы тикеров для исключения (облигации, ISIN коды и т.д.)
EXCLUDED_TICKER_PREFIXES = ("RU000",)

# Ключевые слова в названии для исключения (ETF и т.д.)
EXCLUDED_SHORTNAME_KEYWORDS = ("ETF",)

# Параметры API
MOEX_API_BASE = "https://iss.moex.com/iss"
MAX_RETRIES = 5
RETRY_DELAY = 60  # секунд

# Директории
DATA_DIR = Path("data")
REPORTS_DIR = Path("reports")

# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

def ensure_directories():
    """Создать необходимые директории если их нет"""
    DATA_DIR.mkdir(exist_ok=True)
    REPORTS_DIR.mkdir(exist_ok=True)


def get_cache_filepath(date: str) -> Path:
    """Получить путь к файлу кеша для даты"""
    return DATA_DIR / f"volumes_{date}.json"


def get_trading_dates(end_date: datetime, days: int) -> List[str]:
    """
    Получить список дат для запроса (только будние дни)
    
    Args:
        end_date: Конечная дата
        days: Количество дней назад
    
    Returns:
        Список дат в формате YYYY-MM-DD (отсортированный от старых к новым)
    """
    dates = []
    current = end_date
    
    while len(dates) < days:
        # Пропускаем выходные (5=суббота, 6=воскресенье)
        if current.weekday() < 5:
            dates.append(current.strftime("%Y-%m-%d"))
        current -= timedelta(days=1)
    
    return list(reversed(dates))


def fetch_volumes_from_api(date: str) -> Dict:
    """
    Загрузить данные по объемам торгов с API Мосбиржи за конкретную дату
    
    Args:
        date: Дата в формате YYYY-MM-DD
    
    Returns:
        Словарь с данными по тикерам
    
    Raises:
        Exception: Если не удалось загрузить данные после всех попыток
    """
    url = f"{MOEX_API_BASE}/history/engines/stock/markets/shares/securities.json"
    params = {
        'date': date,
        'iss.meta': 'off',
        'iss.only': 'history',
        'history.columns': 'SECID,SHORTNAME,VOLUME,VALUE,NUMTRADES'
    }
    
    print(f"  Загрузка данных с API за {date}...", end=" ")
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # Пагинация - API отдает максимум 100 записей
            all_data = []
            start = 0
            
            while True:
                params['start'] = start
                response = requests.get(url, params=params, timeout=30)
                response.raise_for_status()
                
                data = response.json()
                rows = data.get('history', {}).get('data', [])
                
                if not rows:
                    break
                
                all_data.extend(rows)
                start += 100
                
                # Небольшая пауза между запросами
                if len(rows) == 100:
                    time.sleep(0.5)
            
            print(f"OK ({len(all_data)} записей)")
            return aggregate_ticker_data(all_data)
            
        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES:
                print(f"\n  Ошибка (попытка {attempt}/{MAX_RETRIES}): {e}")
                print(f"  Повтор через {RETRY_DELAY} секунд...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"\n  ОШИБКА: Не удалось загрузить данные после {MAX_RETRIES} попыток")
                raise Exception(f"Не удалось загрузить данные за {date}: {e}")


def aggregate_ticker_data(raw_data: List) -> Dict:
    """
    Агрегировать данные по тикерам (суммировать дубли из разных режимов торгов)

    Args:
        raw_data: Список записей [SECID, SHORTNAME, VOLUME, VALUE, NUMTRADES]

    Returns:
        Словарь вида {ticker: {shortname, volume, value, numtrades}}
    """
    aggregated = {}

    for row in raw_data:
        secid = row[0]

        # Пропускаем тикеры с исключенными префиксами (облигации, ISIN и т.д.)
        if secid.startswith(EXCLUDED_TICKER_PREFIXES):
            continue

        shortname = row[1]
        volume = row[2] or 0
        value = row[3] or 0
        numtrades = row[4] or 0

        if secid not in aggregated:
            aggregated[secid] = {
                'shortname': shortname,
                'volume': 0,
                'value': 0,
                'numtrades': 0
            }
        
        aggregated[secid]['volume'] += volume
        aggregated[secid]['value'] += value
        aggregated[secid]['numtrades'] += numtrades
    
    return aggregated


def load_or_fetch_data(date: str, force: bool = False) -> Dict:
    """
    Загрузить данные за дату (из кеша или API)
    
    Args:
        date: Дата в формате YYYY-MM-DD
        force: Принудительно загрузить с API (игнорировать кеш)
    
    Returns:
        Словарь с данными по тикерам
    """
    cache_file = get_cache_filepath(date)
    
    # Попытка загрузить из кеша
    if not force and cache_file.exists():
        print(f"  Загрузка из кеша: {date}")
        with open(cache_file, 'r', encoding='utf-8') as f:
            cached = json.load(f)
            return cached['tickers']
    
    # Загрузка с API
    data = fetch_volumes_from_api(date)
    
    # Сохранение в кеш
    cache_data = {
        'date': date,
        'tickers': data
    }
    with open(cache_file, 'w', encoding='utf-8') as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=2)
    
    return data


def calculate_statistics(base_data: List[Dict], target_data: Dict) -> Dict:
    """
    Рассчитать статистику и найти аномалии
    
    Args:
        base_data: Список словарей с данными за базовый период (5 дней)
        target_data: Словарь с данными за целевую дату
    
    Returns:
        Словарь со статистикой по каждому тикеру
    """
    stats = {}
    warnings = []

    for ticker, target_info in target_data.items():
        # Пропускаем тикеры с исключенными префиксами
        if ticker.startswith(EXCLUDED_TICKER_PREFIXES):
            continue

        # Пропускаем тикеры с исключенными ключевыми словами в названии (ETF и т.д.)
        shortname = target_info.get('shortname', '')
        if any(keyword in shortname.upper() for keyword in EXCLUDED_SHORTNAME_KEYWORDS):
            continue

        # Собрать значения VALUE за базовый период
        values = []
        for day_data in base_data:
            if ticker in day_data:
                values.append(day_data[ticker]['value'])
        
        if not values:
            # Нет данных за базовый период - пропускаем
            continue
        
        # Рассчитать среднее и стандартное отклонение
        mean_value = statistics.mean(values)
        
        if len(values) >= 2:
            std_value = statistics.stdev(values)
        else:
            # Если только 1 значение - используем простое процентное отклонение
            std_value = mean_value * 0.01 if mean_value > 0 else 1
            warnings.append(f"Тикер {ticker}: только {len(values)} день(ей) данных вместо 5")
        
        # Текущее значение
        current_value = target_info['value']
        
        # Z-score
        if std_value > 0:
            z_score = (current_value - mean_value) / std_value
        else:
            z_score = 0
        
        # Процентное отклонение
        if mean_value > 0:
            deviation_pct = ((current_value - mean_value) / mean_value) * 100
        else:
            deviation_pct = 0
        
        stats[ticker] = {
            'shortname': target_info['shortname'],
            'current_value': current_value,
            'mean_value': mean_value,
            'std_value': std_value,
            'z_score': z_score,
            'deviation_pct': deviation_pct,
            'base_days_count': len(values)
        }
    
    return stats, warnings


def find_anomalies(stats: Dict, threshold: float) -> List[Tuple[str, Dict]]:
    """
    Найти аномалии - тикеры с Z-score выше порога, отклонением выше минимума
    и средним оборотом выше минимального

    Args:
        stats: Статистика по тикерам
        threshold: Порог в сигмах

    Returns:
        Отсортированный список кортежей (ticker, stats_dict)
    """
    anomalies = [
        (ticker, info) for ticker, info in stats.items()
        if info['z_score'] > threshold
        and info['deviation_pct'] > MIN_DEVIATION_PERCENT
        and info['mean_value'] >= MIN_AVG_VALUE
    ]
    
    # Сортировка по Z-score (от большего к меньшему)
    anomalies.sort(key=lambda x: x[1]['z_score'], reverse=True)
    
    return anomalies


def format_number(num: float) -> str:
    """Форматировать число с разделителями тысяч"""
    if num >= 1_000_000_000:
        return f"{num / 1_000_000_000:,.1f} млрд"
    elif num >= 1_000_000:
        return f"{num / 1_000_000:,.1f} млн"
    else:
        return f"{num:,.0f}"


def generate_txt_report(anomalies: List, target_date: str, base_period: List[str], 
                       total_tickers: int, warnings: List[str]) -> str:
    """Сгенерировать текстовый отчет"""
    lines = []
    lines.append("=" * 70)
    lines.append("АНОМАЛИИ ОБЪЕМОВ ТОРГОВ")
    lines.append(f"Дата анализа: {target_date}")
    lines.append(f"Базовый период: {base_period[0]} - {base_period[-1]} ({len(base_period)} дней)")
    lines.append(f"Порог аномалии: {ANOMALY_THRESHOLD_SIGMA}σ")
    lines.append("=" * 70)
    lines.append("")
    
    if anomalies:
        lines.append(f"Обнаружено аномалий: {len(anomalies)}")
        lines.append("")
        
        for rank, (ticker, info) in enumerate(anomalies, 1):
            lines.append(f"[{rank}] {ticker} - {info['shortname']}")
            lines.append(f"    Оборот: {format_number(info['current_value'])} руб")
            lines.append(f"    Средний: {format_number(info['mean_value'])} руб")
            lines.append(f"    Z-score: {info['z_score']:+.2f}")
            lines.append(f"    Отклонение: {info['deviation_pct']:+.1f}%")
            if info['base_days_count'] < 5:
                lines.append(f"    ⚠️  Данных за базовый период: {info['base_days_count']} дней")
            lines.append("")
    else:
        lines.append("Аномалий не обнаружено")
        lines.append("")
    
    lines.append("=" * 70)
    lines.append("Статистика:")
    lines.append(f"- Всего тикеров: {total_tickers}")
    lines.append(f"- Аномалий найдено: {len(anomalies)} ({len(anomalies)/total_tickers*100:.1f}%)")
    lines.append(f"- Использовано дней для базы: {len(base_period)}")
    
    if warnings:
        lines.append("")
        lines.append("Предупреждения:")
        for warning in warnings[:10]:  # Показываем максимум 10 предупреждений
            lines.append(f"- {warning}")
        if len(warnings) > 10:
            lines.append(f"... и еще {len(warnings) - 10} предупреждений")
    
    lines.append("=" * 70)
    
    return "\n".join(lines)


def generate_json_report(anomalies: List, target_date: str, base_period: List[str],
                        total_tickers: int, warnings: List[str]) -> Dict:
    """Сгенерировать JSON отчет"""
    return {
        "metadata": {
            "analysis_date": target_date,
            "base_period_start": base_period[0],
            "base_period_end": base_period[-1],
            "base_period_days": len(base_period),
            "threshold_sigma": ANOMALY_THRESHOLD_SIGMA,
            "total_tickers": total_tickers,
            "anomalies_found": len(anomalies)
        },
        "anomalies": [
            {
                "rank": rank,
                "ticker": ticker,
                "shortname": info['shortname'],
                "current_value": info['current_value'],
                "avg_value": info['mean_value'],
                "std_value": info['std_value'],
                "z_score": round(info['z_score'], 2),
                "deviation_percent": round(info['deviation_pct'], 1),
                "base_days_count": info['base_days_count']
            }
            for rank, (ticker, info) in enumerate(anomalies, 1)
        ],
        "warnings": warnings
    }


def save_reports(anomalies: List, target_date: str, base_period: List[str],
                total_tickers: int, warnings: List[str]):
    """Сохранить отчеты в TXT и JSON форматах"""
    
    # TXT отчет
    txt_content = generate_txt_report(anomalies, target_date, base_period, 
                                      total_tickers, warnings)
    txt_path = REPORTS_DIR / f"anomalies_{target_date}.txt"
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write(txt_content)
    print(f"\n✓ Текстовый отчет: {txt_path}")
    
    # JSON отчет
    json_content = generate_json_report(anomalies, target_date, base_period,
                                       total_tickers, warnings)
    json_path = REPORTS_DIR / f"anomalies_{target_date}.json"
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(json_content, f, ensure_ascii=False, indent=2)
    print(f"✓ JSON отчет: {json_path}")
    
    # Вывод в консоль
    print("\n" + txt_content)


# ============================================================================
# ОСНОВНЫЕ ФУНКЦИИ
# ============================================================================

def init_historical_data(days: int):
    """
    Инициализация: загрузить исторические данные за N дней
    
    Args:
        days: Количество дней для загрузки
    """
    print(f"\n{'='*70}")
    print(f"ИНИЦИАЛИЗАЦИЯ: Загрузка данных за последние {days} торговых дней")
    print(f"{'='*70}\n")
    
    end_date = datetime.now()
    dates = get_trading_dates(end_date, days)
    
    print(f"Период: {dates[0]} - {dates[-1]}\n")
    
    for i, date in enumerate(dates, 1):
        print(f"[{i}/{len(dates)}] {date}")
        try:
            load_or_fetch_data(date, force=False)
        except Exception as e:
            print(f"  ⚠️  Ошибка: {e}")
            continue
    
    print(f"\n{'='*70}")
    print(f"Загрузка завершена!")
    print(f"{'='*70}\n")


def analyze_date(target_date: str, force: bool = False):
    """
    Анализировать аномалии объемов за указанную дату
    
    Args:
        target_date: Дата для анализа в формате YYYY-MM-DD
        force: Принудительно перезагрузить данные с API
    """
    print(f"\n{'='*70}")
    print(f"АНАЛИЗ АНОМАЛИЙ ОБЪЕМОВ")
    print(f"{'='*70}\n")
    
    # Преобразовать строку в дату
    try:
        target_dt = datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        print(f"Ошибка: неверный формат даты '{target_date}'. Используйте YYYY-MM-DD")
        sys.exit(1)
    
    # Получить базовый период (5 торговых дней до целевой даты)
    base_dates = get_trading_dates(target_dt - timedelta(days=1), 5)
    
    print(f"Целевая дата: {target_date}")
    print(f"Базовый период: {base_dates[0]} - {base_dates[-1]} ({len(base_dates)} дней)")
    print(f"Порог аномалии: {ANOMALY_THRESHOLD_SIGMA}σ")
    print()
    
    # Загрузить данные за базовый период
    print("Загрузка данных за базовый период:")
    base_data = []
    for date in base_dates:
        try:
            data = load_or_fetch_data(date, force=force)
            base_data.append(data)
        except Exception as e:
            print(f"  ⚠️  Ошибка загрузки {date}: {e}")
            print(f"  Продолжаем с имеющимися данными...")
    
    if not base_data:
        print("\n❌ ОШИБКА: Не удалось загрузить данные за базовый период")
        sys.exit(1)
    
    # Загрузить данные за целевую дату
    print("\nЗагрузка данных за целевую дату:")
    try:
        target_data = load_or_fetch_data(target_date, force=force)
    except Exception as e:
        print(f"\n❌ ОШИБКА: Не удалось загрузить данные за {target_date}")
        print(f"   {e}")
        sys.exit(1)
    
    if not target_data:
        print(f"\n⚠️  Нет данных за {target_date}. Возможно, это выходной день.")
        sys.exit(0)
    
    print(f"\n✓ Загружено: {len(target_data)} тикеров за {target_date}")
    
    # Рассчитать статистику
    print("\nРасчет статистики...")
    stats, warnings = calculate_statistics(base_data, target_data)
    
    # Найти аномалии
    anomalies = find_anomalies(stats, ANOMALY_THRESHOLD_SIGMA)
    
    print(f"✓ Проанализировано: {len(stats)} тикеров")
    print(f"✓ Найдено аномалий: {len(anomalies)}")
    
    # Сохранить отчеты
    save_reports(anomalies, target_date, base_dates, len(target_data), warnings)


# ============================================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Детектор аномальных объемов торгов на Московской Бирже',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:
  
  # Первый запуск - загрузить данные за 60 дней
  python detector.py --init --days 60
  
  # Анализ конкретной даты
  python detector.py --date 2026-01-31
  
  # Анализ вчерашнего дня (по умолчанию)
  python detector.py
  
  # Принудительно перезагрузить данные
  python detector.py --date 2026-01-31 --force
        """
    )
    
    parser.add_argument(
        '--date',
        type=str,
        help='Дата для анализа в формате YYYY-MM-DD (по умолчанию: вчера)'
    )
    
    parser.add_argument(
        '--init',
        action='store_true',
        help='Режим инициализации: загрузить исторические данные'
    )
    
    parser.add_argument(
        '--days',
        type=int,
        default=60,
        help='Количество дней для загрузки в режиме --init (по умолчанию: 60)'
    )
    
    parser.add_argument(
        '--force',
        action='store_true',
        help='Принудительно перезагрузить данные с API (игнорировать кеш)'
    )
    
    args = parser.parse_args()
    
    # Создать директории
    ensure_directories()
    
    # Режим инициализации
    if args.init:
        init_historical_data(args.days)
        return
    
    # Определить целевую дату
    if args.date:
        target_date = args.date
    else:
        # По умолчанию - вчерашний день
        yesterday = datetime.now() - timedelta(days=1)
        target_date = yesterday.strftime("%Y-%m-%d")
    
    # Анализ
    analyze_date(target_date, force=args.force)


if __name__ == "__main__":
    main()
