"""Завантаження сирих jsonl.gz у зіркову схему Postgres.

Через COPY, а не INSERT: 48 млн рядків телеметрії поштучними вставками
вантажились би годинами.

Що тут насправді відбувається, окрім перекладання рядків:

  • driver_number → driver_id. Номер боліда не є ідентичністю пілота
    (у 2025 під №1 їздив Ферстаппен, у 2026 — Норріс), тож кожен факт
    прив'язується до людини через склад учасників конкретної сесії.

  • Розбір поліморфних полів session_result. API віддає duration числом у
    гонці й списком із трьох часів у квалі, а gap_to_leader — числом,
    списком або рядком "+1 LAP".

  • Проставляння ознак, яких в API немає: чи етап тестовий, чи нараховувались
    очки, чи траса вулична, порядковий номер етапу.

Скрипт ідемпотентний: схема перестворюється з нуля при кожному запуску.
"""

from __future__ import annotations

import gzip
import io
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
SQL = ROOT / "sql"
DSN = "postgresql:///f1_2026"

YEARS = [2026, 2025]

# У API немає ознаки вуличної траси, а для гіпотези про погоду вона потрібна:
# на вуличних сходять переважно від контакту зі стіною, а не від перегріву.
STREET_CIRCUITS = {
    "Monaco", "Montreal", "Melbourne", "Miami", "Jeddah", "Baku",
    "Singapore", "Las Vegas", "Imola",
}


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with gzip.open(path, "rt") as f:
        return [json.loads(line) for line in f]


def log(msg: str) -> None:
    print(msg, flush=True)


# ───────────────────── розбір поліморфних полів ────────────────────────

def parse_gap(value):
    """gap_to_leader приходить як число, список (квала), рядок ('+1 LAP')
    або None. Повертає (секунди, кола_відставання, оригінал)."""
    if value is None:
        return None, None, None
    if isinstance(value, list):
        return None, None, json.dumps(value)
    if isinstance(value, (int, float)):
        return float(value), None, str(value)
    text = str(value).strip()
    m = re.match(r"^\+?(\d+)\s*LAPS?$", text, re.IGNORECASE)
    if m:
        return None, int(m.group(1)), text
    try:
        return float(text), None, text
    except ValueError:
        return None, None, text


def parse_durations(value):
    """duration — число (гонка) або список із трьох часів Q1/Q2/Q3 (квала).
    Повертає (тривалість_гонки, q1, q2, q3)."""
    if value is None:
        return None, None, None, None
    if isinstance(value, list):
        q = (list(value) + [None, None, None])[:3]
        return None, q[0], q[1], q[2]
    return float(value), None, None, None


def copy_rows(cur, table: str, columns: list[str], rows: list[tuple]) -> int:
    """Заливає рядки через COPY у текстовому форматі."""
    if not rows:
        return 0
    cols = ", ".join(columns)
    with cur.copy(f"COPY f1.{table} ({cols}) FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)
    return len(rows)


# ──────────────────────────── завантаження ──────────────────────────────

def main() -> None:
    started = time.time()

    log("створюю базу f1_2026…")
    subprocess.run(["dropdb", "--if-exists", "f1_2026"], check=True)
    subprocess.run(["createdb", "f1_2026"], check=True)

    with psycopg.connect(DSN, autocommit=False) as conn:
        cur = conn.cursor()
        log("накатую схему…")
        cur.execute((SQL / "01_schema.sql").read_text())
        conn.commit()

        # ---------- зчитуємо все, що стосується довідників ----------------
        meetings, sessions = [], []
        for year in YEARS:
            meetings += read(RAW / "meetings" / f"{year}.jsonl.gz")
            sessions += read(RAW / "sessions" / f"{year}.jsonl.gz")

        # ---------- виміри: траси ----------------------------------------
        circuits = {}
        for m in meetings:
            circuits[m["circuit_key"]] = (
                m["circuit_key"], m["circuit_short_name"], m.get("location"),
                m.get("country_name"), m.get("country_code"),
                m.get("circuit_type"),
                m["circuit_short_name"] in STREET_CIRCUITS,
            )
        n = copy_rows(cur, "dim_circuit",
                      ["circuit_key", "circuit_short_name", "location",
                       "country_name", "country_code", "circuit_type",
                       "is_street"], list(circuits.values()))
        log(f"  dim_circuit        {n}")

        # ---------- виміри: етапи ----------------------------------------
        # Порядковий номер етапу рахуємо самі: в API його немає, а тести
        # мають випадати з нумерації.
        rounds: dict[int, int] = {}
        for year in YEARS:
            year_meetings = sorted(
                [m for m in meetings if m["year"] == year],
                key=lambda m: m["date_start"])
            counter = 0
            for m in year_meetings:
                if "Testing" in m["meeting_name"]:
                    continue
                counter += 1
                rounds[m["meeting_key"]] = counter

        meeting_rows = []
        for m in meetings:
            is_testing = "Testing" in m["meeting_name"]
            meeting_rows.append((
                m["meeting_key"], m["year"], m["meeting_name"],
                m.get("meeting_official_name"), m["circuit_key"],
                m["date_start"], m.get("gmt_offset"), is_testing,
                rounds.get(m["meeting_key"]),
            ))
        n = copy_rows(cur, "dim_meeting",
                      ["meeting_key", "year", "meeting_name", "official_name",
                       "circuit_key", "date_start", "gmt_offset", "is_testing",
                       "round"], meeting_rows)
        log(f"  dim_meeting        {n}")

        # ---------- виміри: сесії ----------------------------------------
        session_rows = [(
            s["session_key"], s["meeting_key"], s["session_name"],
            s["session_type"], s["date_start"], s["date_end"], s["year"],
            bool(s.get("is_cancelled")),
            s["session_name"] in ("Race", "Sprint"),
        ) for s in sessions]
        n = copy_rows(cur, "dim_session",
                      ["session_key", "meeting_key", "session_name",
                       "session_type", "date_start", "date_end", "year",
                       "is_cancelled", "is_points_session"], session_rows)
        log(f"  dim_session        {n}")
        conn.commit()

        # ---------- виміри: пілоти й команди ------------------------------
        # Ключ — людина. Номер боліда живе у fact_entry.
        all_drivers = []
        for path in sorted(RAW.glob("drivers/*.jsonl.gz")):
            all_drivers += read(path)

        people, teams = {}, {}
        for d in all_drivers:
            people.setdefault(d["full_name"], (
                d["full_name"], d.get("name_acronym"), d.get("first_name"),
                d.get("last_name"), d.get("headshot_url")))
            teams.setdefault(d["team_name"], (d["team_name"], d.get("team_colour")))

        n = copy_rows(cur, "dim_driver",
                      ["full_name", "name_acronym", "first_name", "last_name",
                       "headshot_url"], list(people.values()))
        log(f"  dim_driver         {n}")
        n = copy_rows(cur, "dim_team", ["team_name", "team_colour"],
                      list(teams.values()))
        log(f"  dim_team           {n}")
        conn.commit()

        cur.execute("SELECT full_name, driver_id FROM f1.dim_driver")
        driver_id_by_name = dict(cur.fetchall())
        cur.execute("SELECT team_name, team_id FROM f1.dim_team")
        team_id_by_name = dict(cur.fetchall())

        # ---------- міст: склад учасників ---------------------------------
        # Це і є перекладач driver_number → driver_id, окремий для кожної
        # сесії. Далі всі факти проходять через нього.
        entries = {}
        lookup: dict[int, dict[int, int]] = defaultdict(dict)
        for d in all_drivers:
            key = (d["session_key"], d["driver_number"])
            did = driver_id_by_name[d["full_name"]]
            entries[key] = (d["session_key"], d["driver_number"], did,
                            team_id_by_name[d["team_name"]])
            lookup[d["session_key"]][d["driver_number"]] = did
        n = copy_rows(cur, "fact_entry",
                      ["session_key", "driver_number", "driver_id", "team_id"],
                      list(entries.values()))
        log(f"  fact_entry         {n}")
        conn.commit()

        known_sessions = {s["session_key"] for s in sessions}

        def did_of(session_key: int, driver_number: int):
            """Пілот за номером у межах конкретної сесії, або None, якщо в
            складі учасників його немає (буває у гоночній дирекції)."""
            return lookup.get(session_key, {}).get(driver_number)

        # ---------- результати --------------------------------------------
        rows = []
        for path in sorted(RAW.glob("session_result/*.jsonl.gz")):
            for r in read(path):
                sk = r["session_key"]
                did = did_of(sk, r["driver_number"])
                if did is None or sk not in known_sessions:
                    continue
                dur, q1, q2, q3 = parse_durations(r.get("duration"))
                gap_s, laps_down, gap_raw = parse_gap(r.get("gap_to_leader"))
                rows.append((
                    sk, did, r["driver_number"], r.get("position"),
                    r.get("points") or 0, r.get("number_of_laps"),
                    bool(r.get("dnf")), bool(r.get("dns")), bool(r.get("dsq")),
                    dur, gap_s, laps_down, q1, q2, q3, gap_raw))
        # Один і той самий пілот не може мати два результати в сесії, але
        # API зрідка дублює рядок — беремо перший.
        seen, deduped = set(), []
        for r in rows:
            if (r[0], r[1]) in seen:
                continue
            seen.add((r[0], r[1]))
            deduped.append(r)
        n = copy_rows(cur, "fact_result",
                      ["session_key", "driver_id", "driver_number", "position",
                       "points", "number_of_laps", "dnf", "dns", "dsq",
                       "duration_s", "gap_to_leader_s", "laps_down",
                       "q1_s", "q2_s", "q3_s", "gap_raw"], deduped)
        log(f"  fact_result        {n}  (відкинуто дублів: {len(rows) - n})")
        conn.commit()

        # ---------- кола та міні-сектори ----------------------------------
        lap_rows, seg_rows, seen_laps = [], [], set()
        for path in sorted(RAW.glob("laps/*.jsonl.gz")):
            for r in read(path):
                sk = r["session_key"]
                did = did_of(sk, r["driver_number"])
                if did is None or sk not in known_sessions:
                    continue
                key = (sk, did, r["lap_number"])
                if key in seen_laps:
                    continue
                seen_laps.add(key)
                lap_rows.append((
                    sk, did, r["driver_number"], r["lap_number"],
                    r.get("date_start"), r.get("lap_duration"),
                    r.get("duration_sector_1"), r.get("duration_sector_2"),
                    r.get("duration_sector_3"), r.get("i1_speed"),
                    r.get("i2_speed"), r.get("st_speed"),
                    bool(r.get("is_pit_out_lap"))))
                for sector in (1, 2, 3):
                    segs = r.get(f"segments_sector_{sector}") or []
                    for idx, code in enumerate(segs):
                        seg_rows.append((sk, did, r["lap_number"], sector, idx, code))
        n = copy_rows(cur, "fact_lap",
                      ["session_key", "driver_id", "driver_number", "lap_number",
                       "date_start", "lap_duration", "duration_sector_1",
                       "duration_sector_2", "duration_sector_3", "i1_speed",
                       "i2_speed", "st_speed", "is_pit_out_lap"], lap_rows)
        log(f"  fact_lap           {n}")
        n = copy_rows(cur, "fact_lap_segment",
                      ["session_key", "driver_id", "lap_number", "sector",
                       "segment_index", "status_code"], seg_rows)
        log(f"  fact_lap_segment   {n}")
        conn.commit()

        # ---------- прості факти ------------------------------------------
        def simple(folder: str, table: str, columns: list[str], build):
            rows, seen_keys = [], set()
            for path in sorted(RAW.glob(f"{folder}/*.jsonl.gz")):
                for r in read(path):
                    sk = r["session_key"]
                    if sk not in known_sessions:
                        continue
                    did = did_of(sk, r["driver_number"]) if "driver_number" in r else None
                    out = build(r, sk, did)
                    if out is None:
                        continue
                    rows.append(out)
            n = copy_rows(cur, table, columns, rows)
            log(f"  {table:<18} {n}")
            conn.commit()

        simple("stints", "fact_stint",
               ["session_key", "driver_id", "stint_number", "lap_start",
                "lap_end", "compound", "tyre_age_at_start"],
               lambda r, sk, did: None if did is None else
               (sk, did, r["stint_number"], r.get("lap_start"), r.get("lap_end"),
                r.get("compound"), r.get("tyre_age_at_start")))

        simple("pit", "fact_pit",
               ["session_key", "driver_id", "lap_number", "pit_time",
                "pit_duration", "lane_duration", "stop_duration"],
               lambda r, sk, did: None if did is None else
               (sk, did, r.get("lap_number"), r.get("date"),
                r.get("pit_duration"), r.get("lane_duration"),
                r.get("stop_duration")))

        simple("position", "fact_position",
               ["session_key", "driver_id", "ts", "position"],
               lambda r, sk, did: None if did is None else
               (sk, did, r["date"], r.get("position")))

        simple("intervals", "fact_interval",
               ["session_key", "driver_id", "ts", "gap_to_leader", "interval_s"],
               lambda r, sk, did: None if did is None else
               (sk, did, r["date"],
                r["gap_to_leader"] if isinstance(r.get("gap_to_leader"), (int, float)) else None,
                r["interval"] if isinstance(r.get("interval"), (int, float)) else None))

        simple("race_control", "fact_race_control",
               ["session_key", "ts", "category", "flag", "scope", "sector",
                "lap_number", "driver_id", "message"],
               lambda r, sk, did: (
                   sk, r["date"], r.get("category"), r.get("flag"),
                   r.get("scope"), r.get("sector"), r.get("lap_number"),
                   did, r.get("message")))

        simple("team_radio", "fact_team_radio",
               ["session_key", "driver_id", "ts", "recording_url"],
               lambda r, sk, did: None if did is None else
               (sk, did, r["date"], r.get("recording_url")))

        # погода: єдиний ключ (сесія, час), тож дублі відкидаємо
        rows, seen_w = [], set()
        for path in sorted(RAW.glob("weather/*.jsonl.gz")):
            for r in read(path):
                sk = r["session_key"]
                if sk not in known_sessions or (sk, r["date"]) in seen_w:
                    continue
                seen_w.add((sk, r["date"]))
                rows.append((sk, r["date"], r.get("air_temperature"),
                             r.get("track_temperature"), r.get("humidity"),
                             r.get("pressure"), r.get("rainfall"),
                             r.get("wind_speed"), r.get("wind_direction")))
        n = copy_rows(cur, "fact_weather",
                      ["session_key", "ts", "air_temperature", "track_temperature",
                       "humidity", "pressure", "rainfall", "wind_speed",
                       "wind_direction"], rows)
        log(f"  fact_weather       {n}")
        conn.commit()

        rows = []
        for path in sorted(RAW.glob("overtakes/*.jsonl.gz")):
            for r in read(path):
                sk = r["session_key"]
                a = did_of(sk, r["overtaking_driver_number"])
                b = did_of(sk, r["overtaken_driver_number"])
                if a is None or b is None or sk not in known_sessions:
                    continue
                rows.append((sk, r["date"], a, b, r.get("position")))
        n = copy_rows(cur, "fact_overtake",
                      ["session_key", "ts", "overtaking_driver_id",
                       "overtaken_driver_id", "position"], rows)
        log(f"  fact_overtake      {n}")
        conn.commit()

        # ---------- телеметрія --------------------------------------------
        # Партіями по сесіях: тримати 24 млн рядків у пам'яті немає потреби.
        for folder, table, cols, build in [
            ("car_data", "fact_car_data",
             ["session_key", "driver_id", "ts", "speed", "rpm", "n_gear",
              "throttle", "brake", "drs"],
             lambda r, sk, did: (sk, did, r["date"], r.get("speed"), r.get("rpm"),
                                 r.get("n_gear"), r.get("throttle"),
                                 r.get("brake"), r.get("drs"))),
            ("location", "fact_location",
             ["session_key", "driver_id", "ts", "x", "y", "z"],
             lambda r, sk, did: (sk, did, r["date"], r.get("x"), r.get("y"),
                                 r.get("z"))),
        ]:
            total = 0
            t0 = time.time()
            session_dirs = sorted((RAW / folder).iterdir()) if (RAW / folder).exists() else []
            for i, sdir in enumerate(session_dirs, 1):
                if not sdir.is_dir():
                    continue
                sk = int(sdir.name)
                if sk not in known_sessions:
                    continue
                batch = []
                for f in sorted(sdir.glob("*.jsonl.gz")):
                    # .stem знімає лише один суфікс: у "1.jsonl.gz" це дає
                    # "1.jsonl". Ім'я файла — номер боліда до першої крапки.
                    drv = int(f.name.split(".", 1)[0])
                    did = did_of(sk, drv)
                    if did is None:
                        continue
                    for r in read(f):
                        batch.append(build(r, sk, did))
                total += copy_rows(cur, table, cols, batch)
                conn.commit()
                if i % 10 == 0 or i == len(session_dirs):
                    log(f"    {table}: {i}/{len(session_dirs)} сесій, "
                        f"{total:,} рядків, {time.time()-t0:.0f} c")
            log(f"  {table:<18} {total:,}")

        # ---------- індекси ------------------------------------------------
        log("\nбудую індекси…")
        t0 = time.time()
        cur.execute((SQL / "02_indexes.sql").read_text())
        conn.commit()
        log(f"  готово за {time.time()-t0:.0f} c")

    log(f"\nЗАВАНТАЖЕННЯ ЗАВЕРШЕНО за {(time.time()-started)/60:.1f} хв")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
