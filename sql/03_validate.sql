-- Перевірка завантаження. Не «чи є рядки», а «чи вони означають те, що треба».
SET search_path TO f1, public;

\echo ═══ 1. Пастка з номером №1: Ферстаппен (2025) vs Норріс (2026) ═══
SELECT s.year,
       e.driver_number,
       d.full_name,
       t.team_name,
       COUNT(*) AS сесій
FROM fact_entry e
JOIN dim_session s ON s.session_key = e.session_key
JOIN dim_driver  d ON d.driver_id   = e.driver_id
JOIN dim_team    t ON t.team_id     = e.team_id
WHERE e.driver_number = 1
GROUP BY 1, 2, 3, 4
ORDER BY 1;

\echo
\echo ═══ 2. Особистий залік 2026 (має збігтися з Jolpica: 219/169/160) ═══
SELECT ROW_NUMBER() OVER (ORDER BY SUM(r.points) DESC) AS поз,
       d.full_name,
       t.team_name,
       SUM(r.points)                        AS очки,
       COUNT(*) FILTER (WHERE r.position = 1) AS перемоги,
       COUNT(*) FILTER (WHERE r.dnf)          AS сходи
FROM fact_result r
JOIN dim_session s ON s.session_key = r.session_key
JOIN dim_driver  d ON d.driver_id   = r.driver_id
JOIN fact_entry  e ON e.session_key = r.session_key AND e.driver_id = r.driver_id
JOIN dim_team    t ON t.team_id     = e.team_id
WHERE s.year = 2026 AND s.is_points_session AND NOT s.is_cancelled
GROUP BY d.full_name, t.team_name
HAVING SUM(r.points) > 0
ORDER BY очки DESC
LIMIT 8;

\echo
\echo ═══ 3. Скасовані етапи не потрапили в залік ═══
SELECT m.meeting_name, m.round, s.session_name, s.is_cancelled,
       COUNT(r.driver_id) AS результатів
FROM dim_session s
JOIN dim_meeting m ON m.meeting_key = s.meeting_key
LEFT JOIN fact_result r ON r.session_key = s.session_key
WHERE s.year = 2026 AND s.session_name = 'Race'
GROUP BY 1, 2, 3, 4
ORDER BY m.round NULLS FIRST
LIMIT 30;

\echo
\echo ═══ 4. Поліморфні поля розібрані правильно ═══
SELECT s.session_name,
       COUNT(*)                                   AS рядків,
       COUNT(r.duration_s)                        AS має_тривалість,
       COUNT(r.q1_s)                              AS має_q1,
       COUNT(r.gap_to_leader_s)                   AS гап_секунди,
       COUNT(r.laps_down)                         AS гап_кола
FROM fact_result r
JOIN dim_session s ON s.session_key = r.session_key
WHERE s.year = 2026
GROUP BY 1 ORDER BY 1;

\echo
\echo ═══ 5. Дуель напарників у Mercedes: медіанний темп у гонках 2026 ═══
-- Кола без піт-аутів, без кіл під жовтими, у межах 107% від найкращого:
-- груба, але чесна фільтрація сміття.
WITH clean AS (
    SELECT l.driver_id, l.session_key, l.lap_duration,
           MIN(l.lap_duration) OVER (PARTITION BY l.session_key) AS best
    FROM fact_lap l
    JOIN dim_session s ON s.session_key = l.session_key
    WHERE s.year = 2026 AND s.session_name = 'Race'
      AND l.lap_duration IS NOT NULL AND NOT l.is_pit_out_lap
)
SELECT d.full_name,
       COUNT(*)                                        AS кіл,
       ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY lap_duration)::numeric, 3) AS медіана_с
FROM clean c
JOIN dim_driver d ON d.driver_id = c.driver_id
JOIN fact_entry e ON e.session_key = c.session_key AND e.driver_id = c.driver_id
JOIN dim_team   t ON t.team_id = e.team_id
WHERE c.lap_duration < c.best * 1.07 AND t.team_name = 'Mercedes'
GROUP BY d.full_name
ORDER BY медіана_с;

\echo
-- Апостроф у \echo psql приймає за початок рядкового літерала, тому текст
-- без нього.
\echo ═══ 6. Телеметрія: чи прикріплена до правильних пілотів ═══
SELECT d.full_name,
       COUNT(*)        AS точок,
       MAX(c.speed)    AS макс_швидкість,
       MAX(c.rpm)      AS макс_обертів,
       COUNT(c.drs)    AS drs_не_null
FROM fact_car_data c
JOIN dim_driver d  ON d.driver_id = c.driver_id
JOIN dim_session s ON s.session_key = c.session_key
JOIN dim_meeting m ON m.meeting_key = s.meeting_key
WHERE m.meeting_name = 'Hungarian Grand Prix' AND s.session_name = 'Race'
GROUP BY d.full_name ORDER BY макс_швидкість DESC LIMIT 5;

\echo
\echo ═══ 7. Розмір бази ═══
SELECT relname AS таблиця,
       to_char(n_live_tup, '999G999G999') AS рядків,
       pg_size_pretty(pg_total_relation_size(relid)) AS розмір
FROM pg_stat_user_tables
WHERE schemaname = 'f1'
ORDER BY pg_total_relation_size(relid) DESC LIMIT 8;

SELECT pg_size_pretty(pg_database_size('f1_2026')) AS база_всього;
