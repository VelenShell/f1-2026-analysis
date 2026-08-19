"""Телеметричні ознаки для КОЖНОГО пілота на КОЖНОМУ етапі 2026.

Навіщо окремим скриптом: обробка 11 кваліфікацій по 22 пілоти займає хвилини,
а результат потрібен у ноутбуці миттєво. Тут рахуємо один раз і кладемо в CSV —
заодно частина аналізу перестає вимагати доступу до PostgreSQL.

Ознаки беруться з КВАЛІФІКАЦІЇ, тобто відомі ДО гонки. Це принципово: у
прогнозній моделі вони не є витоком цільової змінної, на відміну від гоночного
темпу, який вимірюється під час самої гонки.

Обидві ознаки відносні (дефіцит до найшвидшого в цій же сесії), тому зіставні
між трасами різної довжини й характеру.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis"))
import telemetry as T  # noqa: E402

OUT = ROOT / "export" / "telemetry"


def main() -> None:
    conn = psycopg.connect("postgresql:///f1_2026")

    meetings = conn.execute("""
        SELECT m.meeting_name, m.round
        FROM f1.dim_meeting m
        WHERE m.year = 2026 AND NOT m.is_testing
          AND EXISTS (SELECT 1 FROM f1.dim_session s
                      WHERE s.meeting_key = m.meeting_key
                        AND s.session_name = 'Qualifying' AND NOT s.is_cancelled)
        ORDER BY m.round
    """).fetchall()

    corner_rows, straight_rows, catalogue_rows = [], [], []

    for name, rnd in meetings:
        laps = T.fastest_laps(conn, name, 2026, "Qualifying")
        if laps.empty:
            print(f"  етап {rnd} {name}: немає кіл")
            continue

        # Еталон — найшвидше коло з НЕзіпсованою телеметрією, а не просто
        # найшвидше: інакше зіпсований канал отруює весь каталог.
        ref, ref_lap = T.best_reference_lap(conn, laps)
        if ref_lap is None:
            print(f"  етап {rnd} {name}: немає жодного придатного кола")
            continue

        gref = T.resample(ref_lap)
        k = T.curvature(gref)
        corners = T.detect_corners(gref, k)
        straights = T.detect_straights(gref, k)
        longest = straights.head(1)

        cat = corners.copy()
        cat.insert(0, "round", rnd)
        cat.insert(1, "meeting_name", name)
        catalogue_rows.append(cat)

        n_ok, skipped = 0, 0
        for _, r in laps.iterrows():
            lap = T.load_lap(conn, r.session_key, r.driver_id,
                             r.date_start, float(r.lap_duration))
            if not T.lap_is_usable(lap):
                skipped += 1
                continue
            g = T.resample(lap)

            cm = T.driver_section_metrics(g, gref, corners, "corner")
            if not cm.empty:
                cm["round"], cm["meeting_name"] = rnd, name
                cm["driver"], cm["team"] = r.driver, r.team
                cm["lap_time"] = float(r.lap_duration)
                corner_rows.append(cm)

            if not longest.empty:
                sm = T.driver_section_metrics(g, gref, longest, "straight")
                if not sm.empty:
                    sm["round"], sm["meeting_name"] = rnd, name
                    sm["driver"], sm["team"] = r.driver, r.team
                    straight_rows.append(sm)
            n_ok += 1

        print(f"  етап {rnd:>2} {name[:26]:<26} поворотів {len(corners):>2}, "
              f"пілотів {n_ok:>2}, відкинуто {skipped:>2}, еталон {ref.driver}",
              flush=True)

    corners_all = pd.concat(corner_rows, ignore_index=True)
    straights_all = pd.concat(straight_rows, ignore_index=True)
    catalogue = pd.concat(catalogue_rows, ignore_index=True)

    # Дефіцит рахуємо В МЕЖАХ сесії — тоді траси зіставні між собою.
    corners_all["apex_deficit"] = (
        corners_all.groupby(["round", "corner"])["min_speed"].transform("max")
        - corners_all["min_speed"])
    straights_all["straight_deficit"] = (
        straights_all.groupby("round")["max_speed"].transform("max")
        - straights_all["max_speed"])

    features = (corners_all.groupby(["round", "meeting_name", "driver", "team"],
                                    as_index=False)
                .agg(corner_deficit=("apex_deficit", "mean"),
                     corner_deficit_max=("apex_deficit", "max"),
                     corners_measured=("corner", "count"))
                .merge(straights_all[["round", "driver", "straight_deficit",
                                      "max_speed"]]
                       .rename(columns={"max_speed": "straight_top_speed"}),
                       on=["round", "driver"], how="left"))
    features = features.round(2)

    OUT.mkdir(parents=True, exist_ok=True)
    catalogue.to_csv(OUT / "corner_catalogue.csv", index=False)
    corners_all.to_csv(OUT / "driver_corner_metrics.csv", index=False)
    straights_all.to_csv(OUT / "driver_straight_metrics.csv", index=False)
    features.to_csv(OUT / "telemetry_features.csv", index=False)

    print(f"\nетапів оброблено: {features['round'].nunique()}")
    print(f"рядків пілот×етап: {len(features)}")
    for f in sorted(OUT.glob("*.csv")):
        print(f"  {f.name:<32} {f.stat().st_size/1024:>8.1f} КБ")


if __name__ == "__main__":
    main()
