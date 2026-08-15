"""Експорт бази у CSV для GitHub, Power BI і перевіряючого.

Два набори, бо в них різні задачі:

  export/star/     — зіркова схема окремими файлами. Це те, що вантажиться
                     в Power BI: виміри й факти зі зв'язками, щоб міри в DAX
                     писалися по-нормальному.

  export/f1_2026_race_analysis.csv — одна плоска таблиця, рядок = «пілот у
                     гонці». Для того, хто хоче просто відкрити файл і
                     побачити дані, не збираючи модель.

Обмеження GitHub: 100 МБ на файл жорстко, 50 МБ — попередження. Телеметрія
(74 млн рядків) у репозиторій не поміщається за жодних умов, тому сюди йдуть
лише похідні від неї показники. Усе, що перевищує поріг, скрипт стискає й
голосно про це повідомляє — мовчазне обрізання даних гірше за помилку.
"""

from __future__ import annotations

import csv
import gzip
import shutil
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
EXPORT = ROOT / "export"
STAR = EXPORT / "star"
DSN = "postgresql:///f1_2026"

# Понад це — стискаємо. З запасом до 100 МБ GitHub, бо CSV ще й роздувається
# при майбутніх перезаливках.
SIZE_WARN_MB = 45

# Зіркова схема під Power BI. Ключі лишаються числовими — саме по них
# будуються зв'язки в моделі.
STAR_TABLES = {
    "dim_circuit":   "SELECT * FROM f1.dim_circuit ORDER BY circuit_key",
    "dim_meeting":   "SELECT * FROM f1.dim_meeting ORDER BY year, date_start",
    "dim_session":   "SELECT * FROM f1.dim_session ORDER BY date_start",
    "dim_driver":    "SELECT * FROM f1.dim_driver ORDER BY driver_id",
    "dim_team":      "SELECT * FROM f1.dim_team ORDER BY team_id",
    "fact_entry":    "SELECT * FROM f1.fact_entry ORDER BY session_key, driver_number",
    "fact_result":   "SELECT * FROM f1.fact_result ORDER BY session_key, position",
    "fact_stint":    "SELECT * FROM f1.fact_stint ORDER BY session_key, driver_id, stint_number",
    "fact_pit":      "SELECT * FROM f1.fact_pit ORDER BY session_key, driver_id, lap_number",
    "fact_weather":  "SELECT * FROM f1.fact_weather ORDER BY session_key, ts",
    "fact_overtake": "SELECT * FROM f1.fact_overtake ORDER BY session_key, ts",
    "fact_race_control": "SELECT * FROM f1.fact_race_control ORDER BY session_key, ts",
    # Кола йдуть уже збагаченими: з командою, компаундом, віком гуми та
    # ознаками чистоти. Без цього кожен зріз у Power BI довелося б збирати
    # з чотирьох таблиць вручну.
    "fact_lap":      "SELECT * FROM f1.v_lap ORDER BY session_key, driver_id, lap_number",
    "fact_position": "SELECT * FROM f1.fact_position ORDER BY session_key, ts",
}

# Плоска таблиця: рядок = пілот у гонці. Темп рахується тільки по чистих
# колах — сирий час кола маскує різницю між пілотами майже повністю.
FLAT_QUERY = """
WITH clean_pace AS (
    SELECT session_key, driver_id,
           COUNT(*)                                                      AS clean_laps,
           ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY lap_duration)::numeric, 3) AS median_clean_lap_s,
           ROUND(MIN(lap_duration)::numeric, 3)                          AS best_lap_s
    FROM f1.v_lap
    WHERE is_clean_lap
    GROUP BY session_key, driver_id
),
all_laps AS (
    SELECT session_key, driver_id, COUNT(*) AS total_laps
    FROM f1.v_lap WHERE lap_duration IS NOT NULL
    GROUP BY session_key, driver_id
),
stops AS (
    SELECT session_key, driver_id, COUNT(*) AS pit_stops
    FROM f1.fact_pit GROUP BY session_key, driver_id
),
compounds AS (
    SELECT session_key, driver_id,
           string_agg(DISTINCT compound, '+' ORDER BY compound) AS compounds_used
    FROM f1.fact_stint WHERE compound IS NOT NULL
    GROUP BY session_key, driver_id
),
-- Стартову позицію беремо з кваліфікації того ж етапу: окремого ендпоінта
-- зі стартовою решіткою в API немає.
quali AS (
    SELECT s.meeting_key, r.driver_id, r.position AS quali_position
    FROM f1.fact_result r
    JOIN f1.dim_session s ON s.session_key = r.session_key
    WHERE s.session_name = 'Qualifying'
),
overtakes AS (
    SELECT session_key, overtaking_driver_id AS driver_id, COUNT(*) AS overtakes_made
    FROM f1.fact_overtake GROUP BY 1, 2
)
SELECT r.year,
       r.round,
       r.meeting_name,
       r.circuit_short_name        AS circuit,
       r.is_street,
       r.driver,
       r.driver_code,
       r.team,
       r.driver_number,
       q.quali_position,
       r.position                  AS finish_position,
       r.points,
       r.dnf,
       r.dsq,
       r.number_of_laps,
       a.total_laps,
       cp.clean_laps,
       cp.median_clean_lap_s,
       cp.best_lap_s,
       r.gap_to_leader_s,
       r.laps_down,
       st.pit_stops,
       c.compounds_used,
       o.overtakes_made,
       w.track_temp_avg,
       w.track_temp_max,
       w.air_temp_avg,
       w.humidity_avg,
       w.rainfall_max
FROM f1.v_result r
JOIN f1.dim_session s   ON s.session_key = r.session_key
LEFT JOIN clean_pace cp ON cp.session_key = r.session_key AND cp.driver_id = r.driver_id
LEFT JOIN all_laps a    ON a.session_key = r.session_key AND a.driver_id = r.driver_id
LEFT JOIN stops st      ON st.session_key = r.session_key AND st.driver_id = r.driver_id
LEFT JOIN compounds c   ON c.session_key = r.session_key AND c.driver_id = r.driver_id
LEFT JOIN overtakes o   ON o.session_key = r.session_key AND o.driver_id = r.driver_id
LEFT JOIN f1.v_session_weather w ON w.session_key = r.session_key
LEFT JOIN quali q       ON q.meeting_key = s.meeting_key AND q.driver_id = r.driver_id
WHERE r.session_name = 'Race' AND NOT r.is_testing
ORDER BY r.year, r.round, r.position NULLS LAST
"""

# Друга плоска таблиця — під перевірку гіпотези про погоду: одиниця
# спостереження «пілот × гонка», 2026 рік.
HYPOTHESIS_QUERY = """
SELECT round, meeting_name, circuit_short_name AS circuit, is_street,
       driver, driver_code, team,
       position, points, dnf, number_of_laps,
       track_temp_avg, track_temp_max, air_temp_avg, humidity_avg, rainfall_max
FROM f1.v_car_race
WHERE year = 2026
ORDER BY round, position NULLS LAST
"""


def dump(cur, name: str, query: str, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        with cur.copy(f"COPY ({query}) TO STDOUT WITH CSV HEADER") as copy:
            for block in copy:
                f.write(bytes(block).decode("utf-8"))
    return path


def report(path: Path, rows_hint: str = "") -> None:
    mb = path.stat().st_size / 1e6
    note = ""
    if mb > SIZE_WARN_MB:
        gz = path.with_suffix(".csv.gz")
        with open(path, "rb") as src, gzip.open(gz, "wb", compresslevel=9) as dst:
            shutil.copyfileobj(src, dst)
        path.unlink()
        note = (f"  → {mb:.1f} МБ завелико для зручного GitHub, стиснуто "
                f"до {gz.stat().st_size/1e6:.1f} МБ")
        path = gz
        mb = path.stat().st_size / 1e6
    print(f"  {path.name:<34} {mb:>7.2f} МБ {rows_hint}{note}")


def count_lines(path: Path) -> int:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        return sum(1 for _ in f) - 1


def main() -> None:
    if EXPORT.exists():
        shutil.rmtree(EXPORT)
    EXPORT.mkdir(parents=True)

    with psycopg.connect(DSN) as conn:
        cur = conn.cursor()

        print("зіркова схема (для Power BI):")
        for name, query in STAR_TABLES.items():
            p = dump(cur, name, query, STAR)
            report(p, f"{count_lines(p):>9,} рядків")

        print("\nплоскі таблиці (для перегляду й перевірки):")
        p = dump(cur, "f1_2026_race_analysis", FLAT_QUERY, EXPORT)
        report(p, f"{count_lines(p):>9,} рядків")
        p = dump(cur, "f1_2026_dnf_weather", HYPOTHESIS_QUERY, EXPORT)
        report(p, f"{count_lines(p):>9,} рядків")

    total = sum(f.stat().st_size for f in EXPORT.rglob("*") if f.is_file())
    print(f"\nвсього в export/: {total/1e6:.1f} МБ")
    print("телеметрія (74 млн рядків) свідомо не експортується — "
          "у репозиторій вона не поміститься;")
    print("похідні від неї показники підуть окремими вітринами.")


if __name__ == "__main__":
    main()
