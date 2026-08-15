"""Розвідка гіпотези «спека вбиває машини» на вже завантажених даних.

Це чернетка, не фінальний аналіз: працює на тому, що харвестер устиг покласти
на диск. Мета — заздалегідь знати, куди хилить результат, щоб не будувати
презентацію навколо висновку, якого в даних немає.

Ключове методологічне рішення: одиниця спостереження — НЕ гонка, а «пілот ×
гонка». На рівні гонок у нас 11 точок, на них не доведеш нічого. На рівні
екіпажів — 242.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with gzip.open(path, "rt") as f:
        return [json.loads(line) for line in f]


def main() -> None:
    sessions = read(RAW / "sessions" / "2026.jsonl.gz")
    meetings = {m["meeting_key"]: m["meeting_name"]
                for m in read(RAW / "meetings" / "2026.jsonl.gz")}

    races = [s for s in sessions
             if s["session_name"] == "Race"
             and not s.get("is_cancelled")
             and s["date_start"][:10] < "2026-08-14"]
    races.sort(key=lambda s: s["date_start"])

    rows = []
    for s in races:
        sk = s["session_key"]
        results = read(RAW / "session_result" / f"{sk}.jsonl.gz")
        weather = read(RAW / "weather" / f"{sk}.jsonl.gz")
        if not results or not weather:
            print(f"  пропускаю {meetings.get(s['meeting_key'])} — даних ще немає")
            continue
        temps = [w["track_temperature"] for w in weather
                 if w.get("track_temperature") is not None]
        air = [w["air_temperature"] for w in weather
               if w.get("air_temperature") is not None]
        if not temps:
            continue
        name = meetings.get(s["meeting_key"], "?")
        for r in results:
            if r.get("dns"):
                continue                     # не стартував — не наш випадок
            rows.append({
                "race": name.replace(" Grand Prix", ""),
                "date": s["date_start"][:10],
                "circuit": s["circuit_short_name"],
                "driver": r["driver_number"],
                "dnf": bool(r.get("dnf")),
                "t_track": float(np.mean(temps)),
                "t_track_max": float(np.max(temps)),
                "t_air": float(np.mean(air)) if air else np.nan,
            })

    df = pd.DataFrame(rows)
    if df.empty:
        print("даних ще недостатньо")
        return

    # Вуличні траси: сходи там переважно від аварій, а не від температури.
    STREET = {"Monaco", "Montreal", "Melbourne", "Miami", "Jeddah", "Baku",
              "Singapore", "Las Vegas"}
    df["street"] = df["circuit"].isin(STREET)

    print(f"\nспостережень (пілот × гонка): {len(df)}")
    print(f"з них сходів: {df['dnf'].sum()}  "
          f"({df['dnf'].mean()*100:.1f}%)")
    print(f"гонок у вибірці: {df['race'].nunique()}")

    print("\n--- сходи по гонках ---")
    per_race = (df.groupby(["race", "t_track", "street"])
                  .agg(starters=("dnf", "size"), dnf=("dnf", "sum"))
                  .reset_index().sort_values("t_track"))
    for _, r in per_race.iterrows():
        bar = "█" * r["dnf"]
        tag = " (вулична)" if r["street"] else ""
        print(f"  {r['race'][:22]:<22} {r['t_track']:>5.1f}°  "
              f"{r['dnf']:>2}/{r['starters']:<3} {bar}{tag}")

    hot = df[df["t_track"] >= df["t_track"].median()]
    cold = df[df["t_track"] < df["t_track"].median()]
    print(f"\nмедіана температури траси: {df['t_track'].median():.1f}°")
    print(f"  тепліше медіани: {hot['dnf'].mean()*100:5.1f}% сходів  (n={len(hot)})")
    print(f"  холодніше:       {cold['dnf'].mean()*100:5.1f}% сходів  (n={len(cold)})")

    print("\n" + "=" * 62)
    print("H0: частка сходів не залежить від температури траси")
    print("H1: чим гарячіше, тим більше сходів")
    print("=" * 62)

    # Тест 1 — таблиця спряженості. Правильний інструмент для бінарного
    # наслідку, на відміну від t-тесту з попереднього проєкту.
    table = [[int(hot["dnf"].sum()), int((~hot["dnf"]).sum())],
             [int(cold["dnf"].sum()), int((~cold["dnf"]).sum())]]
    chi2, p_chi, _, _ = stats.chi2_contingency(table)
    print(f"\nТест 1 (хі-квадрат, гаряче vs холодне):  p = {p_chi:.3f}")

    # Тест 2 — точний тест Фішера, бо частина комірок мала.
    odds, p_fisher = stats.fisher_exact(table, alternative="greater")
    print(f"Тест 2 (точний тест Фішера):             p = {p_fisher:.3f}   "
          f"odds ratio = {odds:.2f}")

    # Тест 3 — а чи відрізняються самі температури в тих, хто зійшов?
    u, p_mw = stats.mannwhitneyu(df[df["dnf"]]["t_track"],
                                 df[~df["dnf"]]["t_track"],
                                 alternative="greater")
    print(f"Тест 3 (Манна-Уітні по температурах):    p = {p_mw:.3f}")

    # Тест 4 — перестановочний: рушимо мітки сходів випадково 10 000 разів.
    rng = np.random.default_rng(42)
    observed = hot["dnf"].mean() - cold["dnf"].mean()
    labels = df["dnf"].values.copy()
    is_hot = (df["t_track"] >= df["t_track"].median()).values
    null = np.empty(10_000)
    for i in range(10_000):
        shuffled = rng.permutation(labels)
        null[i] = shuffled[is_hot].mean() - shuffled[~is_hot].mean()
    p_perm = (null >= observed).mean()
    lo, hi = np.percentile(null, [2.5, 97.5])
    print(f"Тест 4 (перестановочний, 10k):           p = {p_perm:.3f}")
    print(f"   спостережувана різниця часток: {observed*100:+.1f} в.п.")
    print(f"   95% нульового розподілу:       [{lo*100:+.1f}, {hi*100:+.1f}] в.п.")

    # Тест 5 — а якщо прибрати вуличні траси, де сходи від аварій?
    perm = df[~df["street"]]
    if len(perm) > 40:
        med = perm["t_track"].median()
        h, c = perm[perm["t_track"] >= med], perm[perm["t_track"] < med]
        tab2 = [[int(h["dnf"].sum()), int((~h["dnf"]).sum())],
                [int(c["dnf"].sum()), int((~c["dnf"]).sum())]]
        _, p_f2 = stats.fisher_exact(tab2, alternative="greater")
        print(f"\nТест 5 (тільки автодроми, без вуличних):  p = {p_f2:.3f}")
        print(f"   гаряче {h['dnf'].mean()*100:.1f}% vs холодно {c['dnf'].mean()*100:.1f}%  "
              f"(n={len(perm)})")

    print("\n" + "-" * 62)
    alpha = 0.05
    if min(p_fisher, p_perm) < alpha:
        print("→ H0 відхиляється: зв'язок температури зі сходами є")
    else:
        print("→ H0 НЕ відхиляється: на наявних даних зв'язок температури")
        print("  зі сходами статистично не підтверджується")
    print("-" * 62)


if __name__ == "__main__":
    main()
