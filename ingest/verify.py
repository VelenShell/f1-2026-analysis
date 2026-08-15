"""Перевірка цілісності завантаженого — до того, як на цьому щось будувати.

«Харвестер написав 'COMPLETE'» і «дані придатні» — різні твердження. Тут
перевіряється друге: чи читаються файли, чи покриті всі гонки, чи не порожні
ключові ендпоінти, чи не побився багаточленний gzip телеметрії.
"""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
LOG = ROOT / "data" / "logs" / "fetch.log"

LIGHT_CRITICAL = ["drivers", "session_result", "laps", "stints", "position",
                  "race_control", "weather"]


def read(path: Path):
    """Читає jsonl.gz. Повертає None, якщо файл побитий — саме це нам і треба
    відрізнити від 'файл є, але порожній'."""
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt") as f:
            return [json.loads(line) for line in f]
    except Exception as e:
        print(f"  ПОБИТИЙ {path.relative_to(RAW)}: {e}")
        return None


def main() -> None:
    print("=" * 68)
    print("1. ПОМИЛКИ В ЛОЗІ")
    print("=" * 68)
    if LOG.exists():
        lines = LOG.read_text().splitlines()
        failed = [l for l in lines if "FAILED" in l]
        r429 = [l for l in lines if " ! 429" in l]
        net = [l for l in lines if " ! network" in l]
        http = [l for l in lines if " ! HTTP" in l]
        print(f"  FAILED (втрачено остаточно): {len(failed)}")
        print(f"  мережевих помилок (з ретраєм): {len(net)}")
        print(f"  HTTP-помилок (з ретраєм):      {len(http)}")
        print(f"  спрацювань 429 (сповільнення): {len(r429)}")
        for l in failed[:10]:
            print(f"    {l}")

    sessions = read(RAW / "sessions" / "2026.jsonl.gz") or []
    meetings = {m["meeting_key"]: m["meeting_name"]
                for m in (read(RAW / "meetings" / "2026.jsonl.gz") or [])}
    done = [s for s in sessions
            if not s.get("is_cancelled") and s["date_start"][:10] < "2026-08-14"]

    print("\n" + "=" * 68)
    print("2. ПОКРИТТЯ ЛЕГКИХ ЕНДПОІНТІВ (2026, проведені сесії)")
    print("=" * 68)
    missing = defaultdict(list)
    for s in done:
        sk = s["session_key"]
        label = f"{meetings.get(s['meeting_key'], '?')[:20]} {s['session_name']}"
        for ep in LIGHT_CRITICAL:
            rows = read(RAW / ep / f"{sk}.jsonl.gz")
            if rows is None:
                missing[ep].append(f"{label} (файлу немає)")
            elif not rows:
                missing[ep].append(f"{label} (порожньо)")
    print(f"  перевірено сесій: {len(done)}")
    if not missing:
        print("  усі критичні ендпоінти на місці")
    for ep, items in missing.items():
        print(f"\n  {ep}: немає у {len(items)} сесіях")
        for it in items[:6]:
            print(f"     – {it}")

    print("\n" + "=" * 68)
    print("3. ГОНКИ: повнота даних, на яких будується модель")
    print("=" * 68)
    races = sorted([s for s in done if s["session_name"] == "Race"],
                   key=lambda s: s["date_start"])
    print(f"  {'гонка':<22} {'кола':>6} {'стінти':>7} {'піти':>6} "
          f"{'рез':>4} {'car_data':>9} {'location':>9}")
    for s in races:
        sk = s["session_key"]
        laps = read(RAW / "laps" / f"{sk}.jsonl.gz") or []
        stints = read(RAW / "stints" / f"{sk}.jsonl.gz") or []
        pit = read(RAW / "pit" / f"{sk}.jsonl.gz") or []
        res = read(RAW / "session_result" / f"{sk}.jsonl.gz") or []
        cd = len(list((RAW / "car_data" / str(sk)).glob("*.jsonl.gz"))) \
            if (RAW / "car_data" / str(sk)).exists() else 0
        loc = len(list((RAW / "location" / str(sk)).glob("*.jsonl.gz"))) \
            if (RAW / "location" / str(sk)).exists() else 0
        name = meetings.get(s["meeting_key"], "?").replace(" Grand Prix", "")
        flag = "" if (laps and res and cd >= 15) else "   ← ПРОБЛЕМА"
        print(f"  {name[:21]:<22} {len(laps):>6} {len(stints):>7} {len(pit):>6} "
              f"{len(res):>4} {cd:>7} пт {loc:>7} пт{flag}")

    print("\n" + "=" * 68)
    print("4. ТЕЛЕМЕТРІЯ: чи читається багаточленний gzip")
    print("=" * 68)
    # Найбільший файл — найкращий тест: у ньому найбільше склеєних членів.
    tele = sorted(RAW.glob("car_data/*/*.jsonl.gz"),
                  key=lambda p: p.stat().st_size, reverse=True)[:3]
    for p in tele:
        rows = read(p)
        if rows is None:
            continue
        dates = [r["date"] for r in rows]
        speeds = [r["speed"] for r in rows if r.get("speed") is not None]
        sk_set = {r["session_key"] for r in rows}
        print(f"  {p.relative_to(RAW)}")
        print(f"     {len(rows):>8,} рядків, {p.stat().st_size/1e6:.1f} MB")
        print(f"     час: {min(dates)[:19]} → {max(dates)[:19]}")
        print(f"     швидкість: {min(speeds)}–{max(speeds)} км/год, "
              f"session_key унікальних: {len(sk_set)}")
        # Дублікати між вікнами — головний ризик схеми з нахлистом.
        stamps = [(r["date"], r["driver_number"]) for r in rows]
        dupes = len(stamps) - len(set(stamps))
        print(f"     дублікатів (date, driver): {dupes}"
              f"{'  ← треба дедуплікувати при завантаженні' if dupes else '  чисто'}")

    print("\n" + "=" * 68)
    print("5. ПІДСУМОК ПО СЕЗОНАХ")
    print("=" * 68)
    for year in (2026, 2025):
        ss = read(RAW / "sessions" / f"{year}.jsonl.gz") or []
        live = [s for s in ss if not s.get("is_cancelled")]
        have = sum(1 for s in live
                   if (RAW / "laps" / f"{s['session_key']}.jsonl.gz").exists())
        print(f"  {year}: {have}/{len(live)} сесій із колами")


if __name__ == "__main__":
    main()
