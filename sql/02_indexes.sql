-- Індекси накатуються ПІСЛЯ завантаження: будувати їх на порожніх таблицях
-- і доливати 48 млн рядків через них — значно повільніше, ніж навпаки.

SET search_path TO f1, public;

-- Легкі факти: зрізи майже завжди «сесія + пілот».
CREATE INDEX idx_lap_session      ON fact_lap (session_key);
CREATE INDEX idx_lap_driver       ON fact_lap (driver_id);
CREATE INDEX idx_lap_duration     ON fact_lap (lap_duration) WHERE lap_duration IS NOT NULL;

CREATE INDEX idx_result_driver    ON fact_result (driver_id);
CREATE INDEX idx_stint_session    ON fact_stint (session_key);
CREATE INDEX idx_pit_session      ON fact_pit (session_key, driver_id);
CREATE INDEX idx_position_sd      ON fact_position (session_key, driver_id);
CREATE INDEX idx_interval_sd      ON fact_interval (session_key, driver_id);
CREATE INDEX idx_rc_session       ON fact_race_control (session_key, ts);
CREATE INDEX idx_rc_flag          ON fact_race_control (flag) WHERE flag IS NOT NULL;
CREATE INDEX idx_overtake_session ON fact_overtake (session_key);
CREATE INDEX idx_segment_lap      ON fact_lap_segment (session_key, driver_id, lap_number);
CREATE INDEX idx_entry_driver     ON fact_entry (driver_id);

-- Телеметрія. Btree по (сесія, пілот) — це те, як її завжди читають.
-- Для часу BRIN: рядки лягали хронологічно, тож діапазонний індекс дає
-- майже ту саму користь за часток відсотка розміру btree.
CREATE INDEX idx_car_sd  ON fact_car_data (session_key, driver_id);
CREATE INDEX idx_car_ts  ON fact_car_data USING BRIN (ts);
CREATE INDEX idx_loc_sd  ON fact_location (session_key, driver_id);
CREATE INDEX idx_loc_ts  ON fact_location USING BRIN (ts);

ANALYZE;
