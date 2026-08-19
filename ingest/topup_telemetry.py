"""Дозавантаження телеметрії там, де сесія затягнулася за розклад.

Первинний харвестер брав вікно від номінального часу сесії (date_start …
date_end) з запасом 10 хвилин. Виявилося, що цього замало: кваліфікації й
практики регулярно тривають довше — червоні прапори, затримки старту. У 30
сесіях 2026 року хвіст телеметрії виявився обрізаним, подекуди на 38 хвилин,
і найшвидші кола Q3 туди просто не потрапили.

Тут вікно рахується не з розкладу, а з ФАКТИЧНИХ кіл: від початку першого
кола до кінця останнього. Це джерело правди, бо коло без телеметрії нам і
не потрібне.

Скрипт доливає лише відсутні проміжки: наявні дані не чіпає й не дублює.
"""

from __future__ import annotations

import gzip
import json
import sys
import time
from datetime import timedelta, timezone
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
ENV_PATH = Path("/Users/velen/Projects/F1/server/.env")
BASE = "https://api.openf1.org/v1"
TOKEN_URL = "https://api.openf1.org/token"
DSN = "postgresql:///f1_2026"

PAD = timedelta(minutes=5)
# Вікно менше, ніж у первинного харвестера: тут один запит тягне ВСІХ пілотів
# одразу, тож відповідь у 22 рази більша.
WINDOW = timedelta(minutes=10)
MIN_GAP = 0.30

_gap, _last = MIN_GAP, 0.0
_token, _token_exp = None, 0.0


def env() -> dict:
    out = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


ENV = env()


def token(client):
    global _token, _token_exp
    u, p = ENV.get("OPENF1_USERNAME"), ENV.get("OPENF1_PASSWORD")
    if not u or not p:
        return None
    if _token and time.time() < _token_exp - 300:
        return _token
    try:
        r = client.post(TOKEN_URL, data={"username": u, "password": p,
                                         "grant_type": "password"}, timeout=20)
        r.raise_for_status()
        d = r.json()
        _token = d["access_token"]
        _token_exp = time.time() + int(d.get("expires_in") or 3600)
        return _token
    except Exception as e:
        print(f"  авторизація не вдалась, працюю публічно: {e!r}")
        return None


def fetch(client, path, tries=6):
    global _gap, _last
    for attempt in range(tries):
        wait = _gap - (time.monotonic() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.monotonic()
        h = {}
        t = token(client)
        if t:
            h["Authorization"] = f"Bearer {t}"
        try:
            r = client.get(f"{BASE}/{path}", headers=h, timeout=180)
        except Exception:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429:
            _gap = min(_gap * 1.5, 2.0)
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 404:
            return []
        if r.status_code != 200:
            time.sleep(2 * (attempt + 1))
            continue
        _gap = max(MIN_GAP, _gap * 0.97)
        try:
            return r.json()
        except Exception:
            return None
    return None


def main() -> None:
    conn = psycopg.connect(DSN, autocommit=False)

    gaps = conn.execute("""
        WITH cov AS (
          SELECT s.session_key, m.meeting_name, s.session_name,
                 (SELECT max(l.date_start + make_interval(secs => l.lap_duration))
                    FROM f1.fact_lap l
                   WHERE l.session_key = s.session_key AND l.lap_duration IS NOT NULL
                 ) AS last_lap_end,
                 (SELECT max(ts) FROM f1.fact_car_data c
                   WHERE c.session_key = s.session_key) AS tel_end
          FROM f1.dim_session s
          JOIN f1.dim_meeting m ON m.meeting_key = s.meeting_key
          WHERE s.year = 2026 AND NOT s.is_cancelled
        )
        SELECT session_key, meeting_name, session_name, tel_end, last_lap_end
        FROM cov
        WHERE tel_end IS NOT NULL AND last_lap_end > tel_end + interval '1 minute'
        ORDER BY last_lap_end - tel_end DESC
    """).fetchall()

    print(f"сесій із обрізаним хвостом: {len(gaps)}", flush=True)
    total_rows = 0

    with httpx.Client() as client:
        for sk, meeting, sname, tel_end, last_lap in gaps:
            drivers = conn.execute("""
                SELECT DISTINCT e.driver_number, e.driver_id
                FROM f1.fact_entry e WHERE e.session_key = %s
                ORDER BY e.driver_number""", (sk,)).fetchall()
            start = tel_end
            end = last_lap + PAD
            minutes = (end - start).total_seconds() / 60
            print(f"\n{meeting[:26]:<26} {sname:<18} +{minutes:.0f} хв, "
                  f"{len(drivers)} пілотів", flush=True)

            added = 0
            by_number = {num: did for num, did in drivers}

            for endpoint, table, cols, build in (
                ("car_data", "fact_car_data",
                 ("session_key", "driver_id", "ts", "speed", "rpm", "n_gear",
                  "throttle", "brake", "drs"),
                 lambda r, did: (r["session_key"], did, r["date"], r.get("speed"),
                                 r.get("rpm"), r.get("n_gear"), r.get("throttle"),
                                 r.get("brake"), r.get("drs"))),
                ("location", "fact_location",
                 ("session_key", "driver_id", "ts", "x", "y", "z"),
                 lambda r, did: (r["session_key"], did, r["date"], r.get("x"),
                                 r.get("y"), r.get("z"))),
            ):
                batch, t = [], start
                while t < end:
                    t2 = min(t + WINDOW, end)
                    # Час ОБОВ'ЯЗКОВО в UTC. Postgres віддає позначки в
                    # локальній зоні сесії; якщо надіслати їх як є, API
                    # прочитає локальний час як UTC, вікно поїде на кілька
                    # годин і відповідь буде порожня — тихо, без помилки.
                    q = (f"{endpoint}?session_key={sk}"
                         f"&date>{t.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%S}"
                         f"&date<{t2.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%S}")
                    rows = fetch(client, q)
                    if rows:
                        batch.extend(rows)
                    t = t2

                if batch:
                    per_driver = {}
                    for r in batch:
                        per_driver.setdefault(r["driver_number"], []).append(r)
                    for num, rs in per_driver.items():
                        path = RAW / endpoint / str(sk) / f"{num}.jsonl.gz"
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with gzip.open(path, "at") as f:
                            for r in rs:
                                f.write(json.dumps(r, separators=(",", ":")) + "\n")
                    with conn.cursor() as cur:
                        with cur.copy(f"COPY f1.{table} ({', '.join(cols)}) "
                                      f"FROM STDIN") as copy:
                            for r in batch:
                                did = by_number.get(r["driver_number"])
                                if did is not None:
                                    copy.write_row(build(r, did))
                    added += len(batch)
                conn.commit()
            print(f"  долито рядків: {added:,}", flush=True)
            total_rows += added

    print(f"\nусього долито: {total_rows:,} рядків")
    conn.close()


if __name__ == "__main__":
    main()
