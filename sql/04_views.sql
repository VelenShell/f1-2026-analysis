-- Вітрини під Power BI.
--
-- Свідомо ТОНКІ: тут тільки з'єднання ключів і розкриття довідників, жодних
-- агрегацій. Рейтинги, накопичені очки, частки, відставання від лідера і
-- прогнозні показники рахуються мірами в DAX — інакше в Power BI лишиться
-- самé перетягування готових чисел.
--
-- Єдиний виняток — ознака «чисте коло» (нижче): вона потребує зіставлення з
-- періодами жовтих прапорів, і робити це віконними функціями в SQL один раз
-- набагато надійніше, ніж повторювати логіку в DAX.

SET search_path TO f1, public;

-- CREATE OR REPLACE не вміє змінювати назви стовпців, тож перестворюємо з
-- нуля. CASCADE — бо вітрини нашаровані одна на одну.
DROP VIEW IF EXISTS v_car_race, v_lap, v_session_weather,
                    v_caution_period, v_result CASCADE;


-- Результати: усе, що потрібно для заліку, одним плоским набором.
CREATE OR REPLACE VIEW v_result AS
SELECT r.session_key,
       s.year,
       m.round,
       m.meeting_name,
       m.is_testing,
       c.circuit_short_name,
       c.country_name,
       c.is_street,
       s.session_name,
       s.session_type,
       s.is_points_session,
       s.date_start                    AS session_start,
       d.driver_id,
       d.full_name                     AS driver,
       d.name_acronym                  AS driver_code,
       t.team_id,
       t.team_name                     AS team,
       t.team_colour,
       r.driver_number,
       r.position,
       r.points,
       r.number_of_laps,
       r.dnf, r.dns, r.dsq,
       r.duration_s,
       r.gap_to_leader_s,
       r.laps_down,
       r.q1_s, r.q2_s, r.q3_s
FROM fact_result r
JOIN dim_session s ON s.session_key = r.session_key
JOIN dim_meeting m ON m.meeting_key = s.meeting_key
JOIN dim_circuit c ON c.circuit_key = m.circuit_key
JOIN dim_driver  d ON d.driver_id   = r.driver_id
JOIN fact_entry  e ON e.session_key = r.session_key AND e.driver_id = r.driver_id
JOIN dim_team    t ON t.team_id     = e.team_id
WHERE NOT s.is_cancelled;


-- Періоди обмежень на трасі. Потрібні, щоб відрізнити повільне коло «пілот
-- повільний» від повільного кола «на трасі жовтий прапор».
--
-- Тут легко помилитися, і помилка тиха. У даних:
--   • жовті прапори мають scope='Sector', а НЕ 'Track' — фільтр по 'Track'
--     не знаходить жодного;
--   • на рівні траси є тільки GREEN / CLEAR / CHEQUERED;
--   • сейфті-кар взагалі не прапор, а category='SafetyCar' з порожнім flag
--     і текстом у message ('VSC DEPLOYED' → 'VSC ENDING',
--     'SAFETY CAR DEPLOYED' → 'SAFETY CAR IN THIS LAP').
--
-- Два типи розділені навмисно: сейфті-кар гальмує всіх, а секторний жовтий —
-- лише тих, хто проїжджає той сектор. Модель темпу сама вирішує, що саме
-- відкидати.
CREATE OR REPLACE VIEW v_caution_period AS
-- 1. Сейфті-кар і віртуальний сейфті-кар: діють на всю трасу.
WITH sc AS (
    SELECT rc.session_key, rc.ts, rc.message,
           LEAD(rc.ts) OVER (PARTITION BY rc.session_key ORDER BY rc.ts) AS next_ts
    FROM fact_race_control rc
    WHERE rc.category = 'SafetyCar'
),
sc_periods AS (
    SELECT sc.session_key,
           sc.ts                                   AS caution_start,
           -- Незакритий період (сейфті-кар до кінця гонки) тягнемо до
           -- завершення сесії, інакше коло просто не позначиться.
           COALESCE(sc.next_ts, s.date_end)        AS caution_end,
           CASE WHEN sc.message LIKE 'VSC%' THEN 'VSC' ELSE 'SC' END AS kind
    FROM sc
    JOIN dim_session s ON s.session_key = sc.session_key
    WHERE sc.message IN ('VSC DEPLOYED', 'SAFETY CAR DEPLOYED')
),
-- 2. Секторні жовті: закриваються CLEAR у ТОМУ Ж секторі.
sector_events AS (
    SELECT rc.session_key, rc.sector, rc.ts, rc.flag,
           LEAD(rc.ts) OVER (PARTITION BY rc.session_key, rc.sector
                             ORDER BY rc.ts) AS next_ts
    FROM fact_race_control rc
    WHERE rc.scope = 'Sector'
      AND rc.flag IN ('YELLOW', 'DOUBLE YELLOW', 'CLEAR')
      AND rc.sector IS NOT NULL
),
yellow_periods AS (
    SELECT se.session_key,
           se.ts                            AS caution_start,
           COALESCE(se.next_ts, s.date_end) AS caution_end,
           'YELLOW'                         AS kind
    FROM sector_events se
    JOIN dim_session s ON s.session_key = se.session_key
    WHERE se.flag IN ('YELLOW', 'DOUBLE YELLOW')
)
SELECT * FROM sc_periods
UNION ALL
SELECT * FROM yellow_periods;


-- Кола з довідниками + ознака придатності для вимірювання темпу.
--
-- Коло вважається «чистим», якщо: час є, це не виїзд із боксів, це не коло
-- заїзду в бокси, і воно не перетинається з періодом жовтого/червоного.
-- Відсікання за 107% від найкращого кола сесії свідомо НЕ робиться тут —
-- це вже аналітичне рішення, його місце в моделі темпу.
CREATE OR REPLACE VIEW v_lap AS
SELECT l.session_key,
       s.year,
       m.round,
       m.meeting_name,
       c.circuit_short_name,
       c.is_street,
       s.session_name,
       s.session_type,
       d.driver_id,
       d.full_name    AS driver,
       d.name_acronym AS driver_code,
       t.team_name    AS team,
       t.team_colour,
       l.lap_number,
       l.date_start,
       l.lap_duration,
       l.duration_sector_1,
       l.duration_sector_2,
       l.duration_sector_3,
       l.i1_speed, l.i2_speed, l.st_speed,
       l.is_pit_out_lap,
       st.compound,
       st.tyre_age_at_start + (l.lap_number - st.lap_start) AS tyre_age,
       st.stint_number,
       (p.lap_number IS NOT NULL)                           AS is_pit_in_lap,
       -- Коло перетинається з періодом обмеження, якщо інтервали
       -- накладаються, а не лише якщо старт кола потрапив усередину:
       -- сейфті-кар, виїхавши на середині кола, псує це коло теж.
       EXISTS (SELECT 1 FROM v_caution_period cp
               WHERE cp.session_key = l.session_key
                 AND cp.kind IN ('SC', 'VSC')
                 AND l.date_start < cp.caution_end
                 AND l.date_start + make_interval(secs => l.lap_duration)
                       > cp.caution_start)                  AS under_sc,
       EXISTS (SELECT 1 FROM v_caution_period cp
               WHERE cp.session_key = l.session_key
                 AND cp.kind = 'YELLOW'
                 AND l.date_start < cp.caution_end
                 AND l.date_start + make_interval(secs => l.lap_duration)
                       > cp.caution_start)                  AS under_yellow,
       (l.lap_duration IS NOT NULL
        AND NOT l.is_pit_out_lap
        AND p.lap_number IS NULL
        AND NOT EXISTS (SELECT 1 FROM v_caution_period cp
                        WHERE cp.session_key = l.session_key
                          AND l.date_start < cp.caution_end
                          AND l.date_start + make_interval(secs => l.lap_duration)
                                > cp.caution_start))        AS is_clean_lap
FROM fact_lap l
JOIN dim_session s ON s.session_key = l.session_key
JOIN dim_meeting m ON m.meeting_key = s.meeting_key
JOIN dim_circuit c ON c.circuit_key = m.circuit_key
JOIN dim_driver  d ON d.driver_id   = l.driver_id
JOIN fact_entry  e ON e.session_key = l.session_key AND e.driver_id = l.driver_id
JOIN dim_team    t ON t.team_id     = e.team_id
LEFT JOIN fact_stint st ON st.session_key = l.session_key
                       AND st.driver_id   = l.driver_id
                       AND l.lap_number BETWEEN st.lap_start AND st.lap_end
LEFT JOIN fact_pit   p ON p.session_key = l.session_key
                      AND p.driver_id   = l.driver_id
                      AND p.lap_number  = l.lap_number
WHERE NOT s.is_cancelled;


-- Погода, зведена до однієї стрічки на сесію: для гіпотези про сходи
-- потрібен саме такий грануляр, поетапний ряд вимірів там зайвий.
CREATE OR REPLACE VIEW v_session_weather AS
SELECT w.session_key,
       ROUND(AVG(w.track_temperature), 1) AS track_temp_avg,
       MAX(w.track_temperature)           AS track_temp_max,
       ROUND(AVG(w.air_temperature), 1)   AS air_temp_avg,
       ROUND(AVG(w.humidity), 1)          AS humidity_avg,
       MAX(w.rainfall)                    AS rainfall_max,
       ROUND(AVG(w.wind_speed), 1)        AS wind_speed_avg,
       COUNT(*)                           AS measurements
FROM fact_weather w
GROUP BY w.session_key;


-- Одиниця спостереження для гіпотези «спека вбиває машини»: пілот × гонка.
-- На рівні гонок вибірка — 11 точок, на цьому рівні — 242.
CREATE OR REPLACE VIEW v_car_race AS
SELECT r.session_key,
       r.year, r.round, r.meeting_name, r.circuit_short_name, r.is_street,
       r.driver_id, r.driver, r.driver_code, r.team,
       r.position, r.points, r.dnf, r.number_of_laps,
       w.track_temp_avg, w.track_temp_max, w.air_temp_avg,
       w.humidity_avg, w.rainfall_max
FROM v_result r
LEFT JOIN v_session_weather w ON w.session_key = r.session_key
WHERE r.session_name = 'Race' AND NOT r.dns;
