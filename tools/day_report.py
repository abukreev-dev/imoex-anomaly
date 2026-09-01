#!/usr/bin/env python3
"""Разбор одного торгового дня по журналу imoex-monitor.

Отвечает на вопросы, которые мы задаём каждый раз вручную: сколько volume-
аномалий обнаружено против отправленных, что срезал дневной потолок, дожил ли
резервный слот до предзакрытия.

Использование:

    ssh root@home "journalctl -u imoex-monitor --since '2026-08-28 00:00:00' \\
        --until '2026-08-29 00:00:00' --no-pager -o short-iso" > day.log
    python3 tools/day_report.py day.log

Журнал — единственный источник, где видна КАЖДАЯ обнаруженная аномалия:
`signal_log.db` знает только про отправленные.
"""
import collections
import datetime as dt
import re
import sys

# Держать в согласии с monitor.py.
MAX_VOLUME_ALERTS_PER_DAY = 3
VOLUME_RESERVED_SLOTS = 1
VOLUME_RESERVED_SLOT_FROM = dt.time(18, 30)
VOLUME_RESERVED_SLOT_UNTIL = dt.time(19, 30)
VOLUME_FREEZE_MINUTES = 5
COOLDOWN_MINUTES = 30

# Аукцион закрытия 18:40–18:50 плюс лаг ISS ~15 мин.
PRECLOSE_FROM, PRECLOSE_TO = dt.time(18, 50), dt.time(19, 15)
EVENING_FROM = dt.time(19, 15)

LINE_RE = re.compile(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] (.*)$")
VOLUME_RE = re.compile(r"([A-Z0-9]+)\(z=([\d.]+)\)")
# Алерт логируется через секунду-две после снапшота: сопоставляем с допуском.
SEND_MATCH_WINDOW_SEC = 180


def budget(when: dt.datetime) -> int:
    if when.weekday() >= 5:
        return MAX_VOLUME_ALERTS_PER_DAY
    if VOLUME_RESERVED_SLOT_FROM <= when.time() < VOLUME_RESERVED_SLOT_UNTIL:
        return MAX_VOLUME_ALERTS_PER_DAY
    return MAX_VOLUME_ALERTS_PER_DAY - VOLUME_RESERVED_SLOTS


def parse(path):
    detections, sends, snapshots, problems = [], [], 0, []
    for line in open(path, encoding="utf-8"):
        m = LINE_RE.search(line.rstrip())
        if not m:
            continue
        when = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        body = m.group(2)
        if body.startswith("snapshot OK"):
            snapshots += 1
            volume_part = re.search(r"volume: ([^|]*)", body)
            if volume_part:
                for ticker, z in VOLUME_RE.findall(volume_part.group(1)):
                    detections.append((when, ticker, float(z)))
        elif body.startswith("alert sent:"):
            kind, ticker = body.split(": ", 1)[1].split("/")
            sends.append((when, kind, ticker))
        elif "error" in body or "resetting state" in body:
            problems.append((when, body))
    return detections, sends, snapshots, problems


def attribute(detections, volume_sends):
    """Для каждого обнаружения — ушло, срезано потолком или кулдауном."""
    sent_at = collections.defaultdict(list)
    for when, ticker in volume_sends:
        sent_at[ticker].append(when)
    used = collections.Counter()
    spent = collections.Counter()
    cooldown, freeze = {}, {}
    out = {"sent": [], "cap": [], "cooldown": []}
    for when, ticker, z in detections:
        active_freeze = freeze.get(ticker)
        if not active_freeze or active_freeze <= when:
            freeze[ticker] = when + dt.timedelta(minutes=VOLUME_FREEZE_MINUTES)
            active_freeze = None
        times = sent_at.get(ticker, [])
        while used[ticker] < len(times) and (times[used[ticker]] - when).total_seconds() < -1:
            used[ticker] += 1
        matched = (used[ticker] < len(times)
                   and -1 <= (times[used[ticker]] - when).total_seconds() <= SEND_MATCH_WINDOW_SEC)
        if matched:
            used[ticker] += 1
            spent[ticker] += 1
            base = freeze[ticker] if active_freeze else when
            cooldown[ticker] = base + dt.timedelta(minutes=COOLDOWN_MINUTES)
            out["sent"].append((when, ticker, z))
        elif not active_freeze and cooldown.get(ticker, dt.datetime.min) > when:
            out["cooldown"].append((when, ticker, z))
        elif spent[ticker] >= budget(when):
            out["cap"].append((when, ticker, z))
        else:
            out["cooldown"].append((when, ticker, z))
    return out, sent_at


def window(when):
    if when.time() < VOLUME_RESERVED_SLOT_FROM:
        return "до 18:30"
    if when.time() < EVENING_FROM:
        return "закрытие 18:30-19:15"
    return "вечерка 19:15-23:50"


def main(path):
    detections, sends, snapshots, problems = parse(path)
    volume_sends = [(w, t) for w, k, t in sends if k == "volume"]
    by_kind = collections.Counter(k for _, k, _ in sends)
    verdict, sent_at = attribute(detections, volume_sends)

    print(f"снапшотов: {snapshots}")
    print(f"сообщений: {sum(by_kind.values())} — " +
          ", ".join(f"{k} {n}" for k, n in by_kind.most_common()))
    print(f"volume: обнаружено {len(detections)}, отправлено {len(verdict['sent'])}, "
          f"срезано потолком {len(verdict['cap'])}, кулдауном {len(verdict['cooldown'])}")
    print("  по окнам (отправлено): " + str(dict(
        collections.Counter(window(w) for w, _ in volume_sends))))
    print("  по окнам (срезано потолком): " + str(dict(
        collections.Counter(window(w) for w, _, _ in verdict["cap"]))))

    full = {t: v for t, v in sent_at.items() if len(v) >= MAX_VOLUME_ALERTS_PER_DAY}
    late = [t for t, v in full.items()
            if v[MAX_VOLUME_ALERTS_PER_DAY - 1].time() >= VOLUME_RESERVED_SLOT_FROM]
    print(f"\nрезерв: {len(full)} бумаг потратили все слоты, "
          f"у {len(late)} последний после {VOLUME_RESERVED_SLOT_FROM:%H:%M}")
    for ticker, times in sorted(full.items(), key=lambda kv: kv[1][-1]):
        print("  " + ticker.ljust(8) + " ".join(f"{t:%H:%M}" for t in times))

    pre = [(w, t, z) for w, t, z in detections if PRECLOSE_FROM <= w.time() < PRECLOSE_TO]
    pre_sent = {(w.replace(second=0), t) for w, t, _ in verdict["sent"]}
    print(f"\nпредзакрытие {PRECLOSE_FROM:%H:%M}-{PRECLOSE_TO:%H:%M}: обнаружений {len(pre)}")
    for when, ticker, z in pre:
        mark = "ушло" if (when.replace(second=0), ticker) in pre_sent else "СРЕЗАНО"
        print(f"  {when:%H:%M} {ticker:8s} z={z:5.1f}  {mark}")

    if problems:
        print("\nсобытия журнала:")
        for when, body in problems:
            print(f"  {when:%H:%M} {body}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
