"""Аналіз телеметрії: каталог поворотів і поведінка пілотів на трасі.

Каталог поворотів будується автоматично з кривизни траєкторії, а не заноситься
руками. Логіка така:

  1. Береться найшвидше коло сесії (кваліфікація — там немає ні трафіку,
     ні палива, ні стратегії, тому порівняння пілотів чисте).
  2. Координати `location` переводяться в метри й перекладаються на рівномірну
     сітку по дистанції: при змінному кроці похідні нестабільні.
  3. Кривизна k = (x'y" - y'x") / (x'^2+y'^2)^1.5. Знак дає напрямок повороту,
     інтеграл по ділянці — кут, обернена величина в апексі — радіус.
  4. Швидкість інтерполюється на моменти позицій, тому геометрія і швидкість
     опиняються на спільній осі дистанції.

Обмеження, про яке треба пам'ятати: `location` віддається з частотою ~4 Гц.
На 250 км/год це точка кожні ~17 м, тож швидкість в апексі й на виході
вимірюється надійно, а точка гальмування — ні.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.signal import savgol_filter
from scipy.spatial import cKDTree

# 1 одиниця координат OpenF1 = 1/10 метра. Перевірено калібруванням:
# геометрична довжина кола Хунгаторингу виходить 4313 м проти офіційних 4381
# (ламана зрізає повороти, тому невелике заниження очікуване).
UNITS_PER_METRE = 10.0

GRID_STEP_M = 5.0          # крок рівномірної сітки
CORNER_MIN_RADIUS_M = 220  # крутіше за це вважаємо поворотом
CORNER_MIN_LENGTH_M = 25   # коротші ділянки — шум
CORNER_MIN_ANGLE_DEG = 25  # менші зміни напрямку — не поворот
STRAIGHT_MIN_LENGTH_M = 300

# Канал телеметрії подекуди завмирає: замість реальних значень приходить
# заповнювач throttle=104 / brake=104 при допустимому діапазоні 0-100, а
# швидкість застигає на останньому значенні. У сезоні 2026 такі рядки
# становлять 25% усього car_data.
#
# Небезпека в тому, що location при цьому пише далі, тож коло виглядає цілим:
# машина «їде», але з постійною швидкістю. Саме так найшвидше коло Антонеллі
# в Японії показало 189 км/год на прямій, де решта їхала за 320.
SENTINEL_LIMIT = 100          # усе, що більше, — не дані
MIN_VALID_FRACTION = 0.80     # коло з меншою часткою придатних точок не беремо


def fastest_laps(conn, meeting_name: str, year: int, session_name: str,
                 limit: int = 30) -> pd.DataFrame:
    """Найшвидше коло КОЖНОГО пілота в сесії."""
    rows = conn.execute("""
        SELECT DISTINCT ON (l.driver_id)
               l.session_key, l.driver_id, d.name_acronym, t.team_name,
               l.lap_number, l.date_start, l.lap_duration
        FROM f1.fact_lap l
        JOIN f1.dim_session s ON s.session_key = l.session_key
        JOIN f1.dim_meeting m ON m.meeting_key = s.meeting_key
        JOIN f1.dim_driver  d ON d.driver_id   = l.driver_id
        JOIN f1.fact_entry  e ON e.session_key = l.session_key
                             AND e.driver_id   = l.driver_id
        JOIN f1.dim_team    t ON t.team_id     = e.team_id
        WHERE m.meeting_name = %s AND s.year = %s AND s.session_name = %s
          AND l.lap_duration IS NOT NULL AND NOT l.is_pit_out_lap
        ORDER BY l.driver_id, l.lap_duration
        LIMIT %s
    """, (meeting_name, year, session_name, limit)).fetchall()
    return pd.DataFrame(rows, columns=["session_key", "driver_id", "driver",
                                       "team", "lap_number", "date_start",
                                       "lap_duration"]).sort_values("lap_duration")


def load_lap(conn, session_key: int, driver_id: int, date_start, duration: float
             ) -> pd.DataFrame | None:
    """Телеметрія одного кола на спільній осі дистанції."""
    loc = conn.execute("""
        SELECT ts, x, y FROM f1.fact_location
        WHERE session_key=%s AND driver_id=%s
          AND ts >= %s AND ts <= %s + %s * interval '1 second'
        ORDER BY ts""", (session_key, driver_id, date_start, date_start, duration)).fetchall()
    car = conn.execute("""
        SELECT ts, speed, throttle, brake, n_gear, rpm FROM f1.fact_car_data
        WHERE session_key=%s AND driver_id=%s
          AND ts >= %s AND ts <= %s + %s * interval '1 second'
        ORDER BY ts""", (session_key, driver_id, date_start, date_start, duration)).fetchall()
    if len(loc) < 50 or len(car) < 50:
        return None

    loc = pd.DataFrame(loc, columns=["ts", "x", "y"])
    car = pd.DataFrame(car, columns=["ts", "speed", "throttle", "brake", "n_gear", "rpm"])

    x = loc["x"].astype(float).to_numpy() / UNITS_PER_METRE
    y = loc["y"].astype(float).to_numpy() / UNITS_PER_METRE
    t = loc["ts"].map(lambda v: v.timestamp()).to_numpy()
    ct = car["ts"].map(lambda v: v.timestamp()).to_numpy()

    out = pd.DataFrame({"t": t, "x": x, "y": y})
    for col in ("speed", "throttle", "brake", "n_gear", "rpm"):
        out[col] = np.interp(t, ct, car[col].astype(float).to_numpy())

    # Позначаємо ділянки, де канал віддавав заповнювач. Прапорець переносимо
    # на моменти позицій найближчим сусідом: інтерполювати ознаку «дані
    # зіпсовані» не можна, її можна лише перенести.
    bad = ((car["throttle"].astype(float) > SENTINEL_LIMIT)
           | (car["brake"].astype(float) > SENTINEL_LIMIT)).to_numpy()
    nearest = np.searchsorted(ct, t).clip(0, len(ct) - 1)
    out["valid"] = ~bad[nearest]

    out["distance"] = np.concatenate([[0], np.cumsum(np.hypot(np.diff(x), np.diff(y)))])
    # Координати іноді «застигають» на кілька семплів; нульові кроки ламають
    # інтерполяцію на сітку.
    out = out[out["distance"].diff().fillna(1) > 0].reset_index(drop=True)
    if len(out) <= 50:
        return None
    out.attrs["valid_fraction"] = float(out["valid"].mean())
    return out


def lap_is_usable(lap: pd.DataFrame | None) -> bool:
    """Чи достатньо на колі придатних точок, щоб на ньому щось міряти."""
    return lap is not None and lap.attrs.get("valid_fraction", 0) >= MIN_VALID_FRACTION


def best_reference_lap(conn, laps: pd.DataFrame):
    """Найшвидше коло, телеметрія якого не зіпсована.

    Просто «найшвидше» брати не можна: якщо саме на ньому завмер канал, увесь
    каталог поворотів дістане неправильні швидкості, а разом із ним і всі
    похідні порівняння.
    """
    for _, r in laps.iterrows():
        lap = load_lap(conn, r.session_key, r.driver_id, r.date_start,
                       float(r.lap_duration))
        if lap_is_usable(lap):
            return r, lap
    return None, None


def resample(lap: pd.DataFrame, step: float = GRID_STEP_M) -> pd.DataFrame:
    """Рівномірна сітка по дистанції + згладжування траєкторії."""
    s = lap["distance"].to_numpy()
    grid = np.arange(0, s[-1], step)
    out = pd.DataFrame({"distance": grid})
    for col in ("x", "y", "speed", "throttle", "brake", "n_gear", "rpm"):
        out[col] = np.interp(grid, s, lap[col].to_numpy())
    # Прапорець придатності переносимо порогом: точка сітки вважається
    # придатною, лише якщо обидві сусідні сирі точки придатні.
    out["valid"] = np.interp(grid, s, lap["valid"].astype(float).to_numpy()) > 0.999
    win = min(15, len(grid) // 2 * 2 - 1)
    if win >= 5:
        out["x_s"] = savgol_filter(out["x"], win, 3)
        out["y_s"] = savgol_filter(out["y"], win, 3)
    else:
        out["x_s"], out["y_s"] = out["x"], out["y"]
    return out


def curvature(grid: pd.DataFrame, step: float = GRID_STEP_M) -> np.ndarray:
    """Кривизна зі знаком: додатна — поворот ліворуч, від'ємна — праворуч."""
    dx, dy = np.gradient(grid["x_s"], step), np.gradient(grid["y_s"], step)
    ddx, ddy = np.gradient(dx, step), np.gradient(dy, step)
    k = (dx * ddy - dy * ddx) / np.power(dx * dx + dy * dy, 1.5)
    win = min(15, len(k) // 2 * 2 - 1)
    return savgol_filter(k, win, 3) if win >= 5 else k


def _segments(mask: np.ndarray) -> list[tuple[int, int]]:
    edges = np.diff(mask.astype(int))
    starts = list(np.where(edges == 1)[0] + 1)
    ends = list(np.where(edges == -1)[0] + 1)
    if mask[0]:
        starts = [0] + starts
    if mask[-1]:
        ends = ends + [len(mask) - 1]
    return list(zip(starts, ends))


def detect_corners(grid: pd.DataFrame, k: np.ndarray) -> pd.DataFrame:
    """Каталог поворотів: межі, напрямок, кут, радіус, швидкість в апексі."""
    total = grid["distance"].iloc[-1]
    rows = []
    for a, b in _segments(np.abs(k) > 1 / CORNER_MIN_RADIUS_M):
        if grid["distance"][b] - grid["distance"][a] < CORNER_MIN_LENGTH_M:
            continue
        seg = slice(a, b + 1)
        turn_deg = np.degrees(np.trapezoid(k[seg], grid["distance"][seg]))
        if abs(turn_deg) < CORNER_MIN_ANGLE_DEG:
            continue
        apex = a + int(np.argmax(np.abs(k[seg])))
        rows.append({
            "corner": len(rows) + 1,
            "start_m": float(grid["distance"][a]),
            "end_m": float(grid["distance"][b]),
            "apex_m": float(grid["distance"][apex]),
            "start_frac": float(grid["distance"][a] / total),
            "end_frac": float(grid["distance"][b] / total),
            "direction": "лівий" if turn_deg > 0 else "правий",
            "angle_deg": round(abs(turn_deg)),
            "radius_m": round(1 / abs(k[apex])),
            "apex_speed": round(float(grid["speed"][seg].min())),
        })
    return pd.DataFrame(rows, columns=["corner", "start_m", "end_m", "apex_m",
                                       "start_frac", "end_frac", "direction",
                                       "angle_deg", "radius_m", "apex_speed"])


def detect_straights(grid: pd.DataFrame, k: np.ndarray) -> pd.DataFrame:
    """Прямі: довгі ділянки з малою кривизною."""
    total = grid["distance"].iloc[-1]
    rows = []
    for a, b in _segments(np.abs(k) <= 1 / CORNER_MIN_RADIUS_M):
        length = grid["distance"][b] - grid["distance"][a]
        if length < STRAIGHT_MIN_LENGTH_M:
            continue
        seg = slice(a, b + 1)
        rows.append({
            "straight": len(rows) + 1,
            "start_m": float(grid["distance"][a]),
            "end_m": float(grid["distance"][b]),
            "start_frac": float(grid["distance"][a] / total),
            "end_frac": float(grid["distance"][b] / total),
            "length_m": round(length),
            "entry_speed": round(float(grid["speed"][a])),
            "exit_speed": round(float(grid["speed"][b])),
            "max_speed": round(float(grid["speed"][seg].max())),
        })
    cols = ["straight", "start_m", "end_m", "start_frac", "end_frac",
            "length_m", "entry_speed", "exit_speed", "max_speed"]
    if not rows:
        # Монако — реальний випадок: жодної прямої, довшої за поріг.
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows).sort_values("length_m", ascending=False)


def project_onto_reference(grid: pd.DataFrame, ref_grid: pd.DataFrame) -> np.ndarray:
    """Дистанція КОЖНОЇ точки пілота вздовж ЕТАЛОННОЇ траси.

    Раніше тут була частка власного кола пілота — і це виявилось хибним.
    Якщо у зрізі бракує точок на початку або в кінці (а таке трапляється),
    уся вісь дистанції зсувається, і вікно повороту накладається не на той
    відрізок траси. У результаті виходили нефізичні значення на кшталт
    316 км/год у повороті радіусом 59 м.

    Правильний спосіб — геометрична прив'язка: для кожної точки шукаємо
    найближчу точку еталонної траєкторії. Зсуви й різні траєкторії тоді
    не мають значення.

    Пошук обмежений вікном попереду попередньої знайденої точки: на трасах
    із паралельними ділянками (стартова пряма поряд із фінішною) найближча
    точка інакше «перестрибує» на сусідню пряму.
    """
    ref_xy = np.column_stack([ref_grid["x_s"], ref_grid["y_s"]])
    tree = cKDTree(ref_xy)
    pts = np.column_stack([grid["x_s"], grid["y_s"]])
    n_ref = len(ref_xy)

    # Стартову точку беремо як безумовно найближчу.
    _, idx0 = tree.query(pts[0])
    out = np.empty(len(pts), dtype=int)
    out[0] = idx0
    WINDOW = max(20, n_ref // 12)
    for i in range(1, len(pts)):
        lo = out[i - 1]
        window = np.arange(lo, lo + WINDOW) % n_ref
        d = np.hypot(ref_xy[window, 0] - pts[i, 0], ref_xy[window, 1] - pts[i, 1])
        out[i] = window[int(np.argmin(d))]
    return ref_grid["distance"].to_numpy()[out]


def driver_section_metrics(grid: pd.DataFrame, ref_grid: pd.DataFrame,
                           sections: pd.DataFrame, kind: str = "corner"
                           ) -> pd.DataFrame:
    """Показники пілота на ділянках еталонної траси.

    Пілот прив'язується до еталона геометрично (див. project_onto_reference),
    тому різні траєкторії та неповні зрізи не ламають відповідність.
    """
    ref_dist = project_onto_reference(grid, ref_grid)
    rows = []
    for _, sec in sections.iterrows():
        m = (ref_dist >= sec["start_m"]) & (ref_dist <= sec["end_m"])
        if m.sum() < 3:
            continue
        seg = grid[m]
        # Ділянку з зіпсованим каналом краще не міряти взагалі, ніж міряти
        # застиглою швидкістю.
        if "valid" in seg and seg["valid"].mean() < 0.9:
            continue
        rec = {kind: int(sec[kind]),
               "min_speed": round(float(seg["speed"].min())),
               "max_speed": round(float(seg["speed"].max())),
               "mean_speed": round(float(seg["speed"].mean()), 1),
               "mean_throttle": round(float(seg["throttle"].mean()), 1),
               "n_points": int(m.sum())}
        if kind == "corner":
            rec["exit_speed"] = round(float(seg["speed"].iloc[-1]))
        else:
            rec["entry_speed"] = round(float(seg["speed"].iloc[0]))
            rec["end_speed"] = round(float(seg["speed"].iloc[-1]))
        rows.append(rec)
    return pd.DataFrame(rows)
