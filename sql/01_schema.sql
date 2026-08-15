-- ═══════════════════════════════════════════════════════════════════════
--  F1 2026 — зіркова схема
-- ═══════════════════════════════════════════════════════════════════════
--
--  Рішення, які варто пояснити на захисті:
--
--  1. Ключ пілота — людина, а не номер боліда. У 2025 під №1 їздив
--     Ферстаппен, у 2026 під №1 їздить Норріс: номер переходить до чемпіона.
--     Номер зберігається як атрибут участі в сесії (fact_entry), а не як
--     ідентичність. Ключ по driver_number тихо злив би двох різних пілотів.
--
--  2. driver_id продубльовано в кожну факт-таблицю. Формально це
--     денормалізація, але Power BI будує зв'язки по одному стовпцю, і без
--     цього кожен зріз по пілоту йшов би через міст fact_entry.
--
--  3. Метрики свідомо НЕ пораховані. У таблицях лежать сирі величини —
--     час кола, очки, температура. Рейтинги, частки, накопичені підсумки
--     й прогнози рахуються мірами в DAX.
--
--  4. session_result з API приходить поліморфним: у гонці duration це число,
--     у квалі — список із трьох часів; gap_to_leader буває числом, списком
--     або рядком "+1 LAP". Тут воно розкладене на типізовані стовпці, а
--     оригінал збережено в gap_raw для звірки.

DROP SCHEMA IF EXISTS f1 CASCADE;
CREATE SCHEMA f1;
SET search_path TO f1, public;


-- ─────────────────────────── ВИМІРИ ────────────────────────────────────

CREATE TABLE dim_circuit (
    circuit_key        INTEGER PRIMARY KEY,
    circuit_short_name TEXT NOT NULL,
    location           TEXT,
    country_name       TEXT,
    country_code       TEXT,
    circuit_type       TEXT,               -- Permanent / Street
    is_street          BOOLEAN             -- контроль для гіпотези про погоду:
);                                         -- на вуличних сходять від стін

CREATE TABLE dim_meeting (
    meeting_key    INTEGER PRIMARY KEY,
    year           SMALLINT NOT NULL,
    meeting_name   TEXT NOT NULL,
    official_name  TEXT,
    circuit_key    INTEGER REFERENCES dim_circuit(circuit_key),
    date_start     TIMESTAMPTZ,
    gmt_offset     TEXT,
    is_testing     BOOLEAN NOT NULL DEFAULT FALSE,
    round          SMALLINT            -- порядковий номер етапу, тести = NULL
);

CREATE TABLE dim_session (
    session_key   INTEGER PRIMARY KEY,
    meeting_key   INTEGER NOT NULL REFERENCES dim_meeting(meeting_key),
    session_name  TEXT NOT NULL,        -- Race / Qualifying / Practice 1 / Day 1
    session_type  TEXT NOT NULL,        -- Race / Qualifying / Practice
    date_start    TIMESTAMPTZ,
    date_end      TIMESTAMPTZ,
    year          SMALLINT NOT NULL,
    is_cancelled  BOOLEAN NOT NULL DEFAULT FALSE,
    -- Чи нараховувались очки: гонки й спринти так, решта ні.
    is_points_session BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE dim_driver (
    driver_id     SERIAL PRIMARY KEY,
    full_name     TEXT NOT NULL UNIQUE,   -- справжня ідентичність
    name_acronym  TEXT,
    first_name    TEXT,
    last_name     TEXT,
    headshot_url  TEXT
);

CREATE TABLE dim_team (
    team_id     SERIAL PRIMARY KEY,
    team_name   TEXT NOT NULL UNIQUE,
    team_colour TEXT                       -- hex без решітки, для Power BI
);


-- ────────────────────── УЧАСТЬ (міст) ──────────────────────────────────
-- Хто, під яким номером і за яку команду виступав у конкретній сесії.
-- Саме тут живе driver_number — він змінний, тому не може бути ключем.

CREATE TABLE fact_entry (
    session_key   INTEGER NOT NULL REFERENCES dim_session(session_key),
    driver_number SMALLINT NOT NULL,
    driver_id     INTEGER NOT NULL REFERENCES dim_driver(driver_id),
    team_id       INTEGER NOT NULL REFERENCES dim_team(team_id),
    PRIMARY KEY (session_key, driver_number)
);


-- ──────────────────────── ФАКТИ ────────────────────────────────────────

CREATE TABLE fact_result (
    session_key     INTEGER NOT NULL REFERENCES dim_session(session_key),
    driver_id       INTEGER NOT NULL REFERENCES dim_driver(driver_id),
    driver_number   SMALLINT NOT NULL,
    position        SMALLINT,
    points          NUMERIC(5,2) NOT NULL DEFAULT 0,
    number_of_laps  SMALLINT,
    dnf             BOOLEAN NOT NULL DEFAULT FALSE,
    dns             BOOLEAN NOT NULL DEFAULT FALSE,
    dsq             BOOLEAN NOT NULL DEFAULT FALSE,
    -- гонка
    duration_s      NUMERIC(10,3),      -- NULL у тих, кого зі кола зняли
    gap_to_leader_s NUMERIC(10,3),      -- NULL, якщо відставання в колах
    laps_down       SMALLINT,           -- розібране з "+1 LAP"
    -- кваліфікація
    q1_s            NUMERIC(8,3),
    q2_s            NUMERIC(8,3),
    q3_s            NUMERIC(8,3),
    gap_raw         TEXT,               -- оригінал з API, для звірки
    PRIMARY KEY (session_key, driver_id)
);

CREATE TABLE fact_lap (
    session_key       INTEGER NOT NULL REFERENCES dim_session(session_key),
    driver_id         INTEGER NOT NULL REFERENCES dim_driver(driver_id),
    driver_number     SMALLINT NOT NULL,
    lap_number        SMALLINT NOT NULL,
    date_start        TIMESTAMPTZ,
    lap_duration      NUMERIC(9,3),     -- NULL на неповних колах
    duration_sector_1 NUMERIC(8,3),
    duration_sector_2 NUMERIC(8,3),
    duration_sector_3 NUMERIC(8,3),
    i1_speed          SMALLINT,
    i2_speed          SMALLINT,
    st_speed          SMALLINT,
    is_pit_out_lap    BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (session_key, driver_id, lap_number)
);

-- Міні-сектори: код стану кожного відрізка кола. Розкладено з масивів, бо
-- масив у стовпці Power BI не з'їсть.
CREATE TABLE fact_lap_segment (
    session_key   INTEGER NOT NULL,
    driver_id     INTEGER NOT NULL,
    lap_number    SMALLINT NOT NULL,
    sector        SMALLINT NOT NULL,      -- 1..3
    segment_index SMALLINT NOT NULL,
    status_code   SMALLINT,               -- 2048 не встановлено, 2049 жовтий,
                                          -- 2050 зелений, 2051 фіолетовий,
                                          -- 2064 піт-лейн
    PRIMARY KEY (session_key, driver_id, lap_number, sector, segment_index)
);

CREATE TABLE fact_stint (
    session_key       INTEGER NOT NULL REFERENCES dim_session(session_key),
    driver_id         INTEGER NOT NULL REFERENCES dim_driver(driver_id),
    stint_number      SMALLINT NOT NULL,
    lap_start         SMALLINT,
    lap_end           SMALLINT,
    compound          TEXT,               -- SOFT / MEDIUM / HARD / INTERMEDIATE
    tyre_age_at_start SMALLINT,
    PRIMARY KEY (session_key, driver_id, stint_number)
);

CREATE TABLE fact_pit (
    session_key   INTEGER NOT NULL REFERENCES dim_session(session_key),
    driver_id     INTEGER NOT NULL REFERENCES dim_driver(driver_id),
    lap_number    SMALLINT,
    pit_time      TIMESTAMPTZ,
    pit_duration  NUMERIC(9,3),   -- увага: сюди потрапляє і заїзд у бокси
    lane_duration NUMERIC(9,3),   -- на весь залишок сесії (сотні секунд)
    stop_duration NUMERIC(9,3)    -- власне стоянка; заповнене рідко
);

CREATE TABLE fact_position (
    session_key   INTEGER NOT NULL REFERENCES dim_session(session_key),
    driver_id     INTEGER NOT NULL REFERENCES dim_driver(driver_id),
    ts            TIMESTAMPTZ NOT NULL,
    position      SMALLINT
);

CREATE TABLE fact_interval (
    session_key   INTEGER NOT NULL REFERENCES dim_session(session_key),
    driver_id     INTEGER NOT NULL REFERENCES dim_driver(driver_id),
    ts            TIMESTAMPTZ NOT NULL,
    gap_to_leader NUMERIC(9,3),
    interval_s    NUMERIC(9,3)
);

CREATE TABLE fact_race_control (
    session_key   INTEGER NOT NULL REFERENCES dim_session(session_key),
    ts            TIMESTAMPTZ NOT NULL,
    category      TEXT,
    flag          TEXT,          -- GREEN / YELLOW / DOUBLE YELLOW / RED / CHEQUERED
    scope         TEXT,          -- Track / Sector / Driver
    sector        SMALLINT,
    lap_number    SMALLINT,
    driver_id     INTEGER REFERENCES dim_driver(driver_id),
    message       TEXT
);

CREATE TABLE fact_weather (
    session_key       INTEGER NOT NULL REFERENCES dim_session(session_key),
    ts                TIMESTAMPTZ NOT NULL,
    air_temperature   NUMERIC(5,2),
    track_temperature NUMERIC(5,2),
    humidity          NUMERIC(5,2),
    pressure          NUMERIC(7,2),
    rainfall          SMALLINT,
    wind_speed        NUMERIC(5,2),
    wind_direction    SMALLINT,
    PRIMARY KEY (session_key, ts)
);

CREATE TABLE fact_overtake (
    session_key       INTEGER NOT NULL REFERENCES dim_session(session_key),
    ts                TIMESTAMPTZ NOT NULL,
    overtaking_driver_id INTEGER NOT NULL REFERENCES dim_driver(driver_id),
    overtaken_driver_id  INTEGER NOT NULL REFERENCES dim_driver(driver_id),
    position          SMALLINT
);

CREATE TABLE fact_team_radio (
    session_key   INTEGER NOT NULL REFERENCES dim_session(session_key),
    driver_id     INTEGER NOT NULL REFERENCES dim_driver(driver_id),
    ts            TIMESTAMPTZ NOT NULL,
    recording_url TEXT
);


-- ───────────────────── ТЕЛЕМЕТРІЯ (великі таблиці) ─────────────────────
-- ~24 млн рядків кожна. Для SQL- і Python-аналізу; у модель Power BI
-- напряму не тягнути — туди підуть похідні вітрини.
--
-- Про drs: у 2026 стовпець порожній на 100%. Це не збій завантаження —
-- новий регламент скасував DRS, замінивши його на режим наддачі
-- потужності. Лишаю стовпець, щоб цей факт було видно з даних.

CREATE TABLE fact_car_data (
    session_key   INTEGER NOT NULL,
    driver_id     INTEGER NOT NULL,
    ts            TIMESTAMPTZ NOT NULL,
    speed         SMALLINT,
    rpm           INTEGER,
    n_gear        SMALLINT,
    throttle      SMALLINT,
    brake         SMALLINT,
    drs           SMALLINT
);

CREATE TABLE fact_location (
    session_key   INTEGER NOT NULL,
    driver_id     INTEGER NOT NULL,
    ts            TIMESTAMPTZ NOT NULL,
    x             INTEGER,
    y             INTEGER,
    z             INTEGER
);
