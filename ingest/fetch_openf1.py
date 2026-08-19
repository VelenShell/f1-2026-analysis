"""Overnight OpenF1 harvester for the F1-2026 mid-season analysis.

Design constraints, in priority order:

  1. It must survive an unattended overnight run. Nothing here imports pandas,
     duckdb or psycopg — only httpx + stdlib — so no dependency resolution can
     kill the job at 3am. Loading into Postgres happens later, offline.
  2. It must be resumable. Every unit of work writes a state entry only after
     its bytes are safely on disk; re-running skips completed work. Killing the
     process at any point and restarting loses at most one chunk.
  3. It must degrade, never crash. A single failing session logs and moves on.
  4. Most valuable data lands first: if it dies overnight, we still have the
     parts the analysis actually depends on.

Layout produced:

    data/raw/<endpoint>/<session_key>.jsonl.gz              (light endpoints)
    data/raw/<endpoint>/<session_key>/<driver>.jsonl.gz     (telemetry)
    data/raw/_state.json                                    (resume ledger)
    data/logs/fetch.log                                     (progress)

Telemetry is fetched in bounded time windows rather than one request per
driver-session: a full 8-hour test day in a single response is both a timeout
risk and un-resumable. Each window is appended as its own gzip member — gzip
streams concatenate legally, so the file stays readable at every point.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
LOGS = ROOT / "data" / "logs"
STATE_PATH = RAW / "_state.json"
ENV_PATH = Path("/Users/velen/Projects/F1/server/.env")

BASE = "https://api.openf1.org/v1"
TOKEN_URL = "https://api.openf1.org/token"

# Seasons to pull. 2026 is the subject; 2025 is pulled for the light endpoints
# only, as a pre-regulation-change baseline for "how different is this year".
SUBJECT_YEAR = 2026
BASELINE_YEARS = [2025]

# One request per session.
LIGHT_ENDPOINTS = [
    "drivers", "session_result", "laps", "stints", "pit", "position",
    "intervals", "race_control", "weather", "overtakes", "team_radio",
]
# One request per driver per time-window.
HEAVY_ENDPOINTS = ["car_data", "location"]

# Telemetry window size. 20 min at ~4 Hz ≈ 4800 rows ≈ 1 MB per driver —
# small enough to retry cheaply, large enough that overhead stays low.
WINDOW = timedelta(minutes=20)
# Guard against the known bad `date_end` on red-flagged sessions (it can sit
# hours past the real finish). Nothing legitimately runs longer than this.
MAX_SESSION_HOURS = 10
# Cars are on track before the green light and after the flag — and sessions
# routinely overrun their scheduled end: red flags, delayed starts. A 10-minute
# pad turned out to truncate 30 sessions of 2026, in one case by 38 minutes,
# taking the Q3 laps with it. 45 minutes covers every overrun observed.
PAD = timedelta(minutes=45)

# Rate limiting. The public tier hard-fails at 3 req/s; authenticated headroom
# is higher but undocumented, so we stay conservative and back off adaptively.
MIN_GAP = 0.28
_gap = MIN_GAP
_last_request = 0.0

_token: str | None = None
_token_expires = 0.0


# ─────────────────────────────── plumbing ────────────────────────────────

def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / "fetch.log", "a") as f:
        f.write(line + "\n")


def load_env() -> dict[str, str]:
    """Minimal .env reader — avoids a python-dotenv dependency."""
    out: dict[str, str] = {}
    if not ENV_PATH.exists():
        return out
    for raw_line in ENV_PATH.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


ENV = load_env()


def get_token(client: httpx.Client) -> str | None:
    """OAuth2 password grant, cached until 5 min before expiry.

    Auth is an optimisation, not a requirement: historical data is fully
    public. It buys us rate-limit headroom, so a failure here is logged and
    the harvest continues unauthenticated.
    """
    global _token, _token_expires
    user, pwd = ENV.get("OPENF1_USERNAME"), ENV.get("OPENF1_PASSWORD")
    if not user or not pwd:
        return None
    if _token and time.time() < _token_expires - 300:
        return _token
    try:
        r = client.post(TOKEN_URL, data={
            "username": user, "password": pwd, "grant_type": "password",
        }, timeout=20)
        r.raise_for_status()
        data = r.json()
        _token = data.get("access_token")
        _token_expires = time.time() + int(data.get("expires_in") or 3600)
        log(f"auth ok (expires in {int(data.get('expires_in') or 3600)}s)")
        return _token
    except Exception as e:
        log(f"auth failed, continuing public: {e!r}")
        return None


def fetch(client: httpx.Client, path: str, tries: int = 6) -> list | None:
    """GET one endpoint. Returns [] for an empty result, None for a failure.

    OpenF1 answers both 'no rows matched' and 'no such route' with a 404 and
    the same body, so 404 is treated as an empty result, never as an error.
    """
    global _gap, _last_request
    for attempt in range(tries):
        wait = _gap - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()

        headers = {}
        tok = get_token(client)
        if tok:
            headers["Authorization"] = f"Bearer {tok}"

        try:
            r = client.get(f"{BASE}/{path}", headers=headers, timeout=180)
        except Exception as e:
            log(f"  ! network {type(e).__name__} on {path[:70]} "
                f"(attempt {attempt + 1})")
            time.sleep(2 * (attempt + 1))
            continue

        if r.status_code == 429:
            _gap = min(_gap * 1.5, 2.0)          # slow down and stay slow
            log(f"  ! 429 → gap now {_gap:.2f}s")
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 404:
            return []
        if r.status_code == 401:
            globals()["_token"] = None            # force refresh, then retry
            continue
        if r.status_code != 200:
            log(f"  ! HTTP {r.status_code} on {path[:70]}")
            time.sleep(2 * (attempt + 1))
            continue

        # A clean run lets the gap drift back down toward the floor.
        _gap = max(MIN_GAP, _gap * 0.97)
        try:
            data = r.json()
        except Exception:
            log(f"  ! non-JSON body on {path[:70]}")
            return None
        return data if isinstance(data, list) else [data]
    return None


# ──────────────────────────── resume ledger ─────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            log("state file corrupt — starting a fresh ledger")
    return {}


_state = load_state()
_state_dirty = 0


def mark(key: str, value=True, flush: bool = False) -> None:
    """Record completed work. Flushed periodically rather than on every write:
    the ledger is rebuilt-safe, and re-doing one chunk is cheaper than an
    fsync per request."""
    global _state_dirty
    _state[key] = value
    _state_dirty += 1
    if flush or _state_dirty >= 20:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_state))
        tmp.replace(STATE_PATH)          # atomic: never a half-written ledger
        _state_dirty = 0


def write_rows(path: Path, rows: list, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "at" if append else "wt") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")


# ────────────────────────────── harvesting ──────────────────────────────

def session_window(session: dict) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(session["date_start"]) - PAD
    try:
        end = datetime.fromisoformat(session["date_end"]) + PAD
    except Exception:
        end = start + timedelta(hours=2)
    if end <= start or (end - start) > timedelta(hours=MAX_SESSION_HOURS):
        end = start + timedelta(hours=MAX_SESSION_HOURS)
    return start, end


def fetch_light(client: httpx.Client, session: dict, label: str) -> None:
    sk = session["session_key"]
    for ep in LIGHT_ENDPOINTS:
        key = f"light:{ep}:{sk}"
        if _state.get(key):
            continue
        rows = fetch(client, f"{ep}?session_key={sk}")
        if rows is None:
            log(f"  FAILED {ep} {label}")
            continue
        write_rows(RAW / ep / f"{sk}.jsonl.gz", rows)
        # Sentinel must stay truthy: a legitimately empty result (intervals on
        # a practice session, say) is *done*, not pending. Marking it 0 would
        # make every rerun re-request every empty endpoint forever.
        mark(key, len(rows) or "empty")
        if rows:
            log(f"  {ep:15} {len(rows):>7} rows  {label}")


def fetch_heavy(client: httpx.Client, session: dict, drivers: list[int],
                label: str) -> None:
    sk = session["session_key"]
    start, end = session_window(session)
    n_windows = max(1, int((end - start) / WINDOW) + 1)

    for ep in HEAVY_ENDPOINTS:
        for drv in drivers:
            key = f"heavy:{ep}:{sk}:{drv}"
            done_upto = _state.get(key, 0)
            if done_upto == "done":
                continue
            path = RAW / ep / str(sk) / f"{drv}.jsonl.gz"
            total = 0
            for i in range(int(done_upto), n_windows):
                w0 = start + WINDOW * i
                w1 = min(w0 + WINDOW, end)
                if w0 >= end:
                    break
                q = (f"{ep}?session_key={sk}&driver_number={drv}"
                     f"&date>{w0.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%S}"
                     f"&date<{w1.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%S}")
                rows = fetch(client, q)
                if rows is None:
                    log(f"  FAILED {ep} sk={sk} drv={drv} window {i}")
                    break                     # resume from this window later
                if rows:
                    write_rows(path, rows, append=(i > 0 or path.exists()))
                    total += len(rows)
                mark(key, i + 1)
            else:
                mark(key, "done")
            if total:
                log(f"  {ep:10} drv {drv:>2} {total:>7} rows  {label}")


def order_sessions(sessions: list[dict]) -> list[dict]:
    """Most analytically valuable first, so an interrupted run still leaves a
    workable dataset: races, then qualifying, then practice, then test days."""
    rank = {"Race": 0, "Sprint": 1, "Qualifying": 2, "Sprint Qualifying": 3}
    return sorted(sessions, key=lambda s: (
        rank.get(s["session_name"], 9 if s["session_name"].startswith("Day") else 4),
        s["date_start"],
    ))


def main() -> None:
    started = time.time()
    log("=" * 70)
    log("OpenF1 harvest starting")

    with httpx.Client(follow_redirects=True) as client:
        # ---- schedule metadata -------------------------------------------
        all_sessions: dict[int, list[dict]] = {}
        for year in [SUBJECT_YEAR] + BASELINE_YEARS:
            meetings = fetch(client, f"meetings?year={year}") or []
            sessions = fetch(client, f"sessions?year={year}") or []
            write_rows(RAW / "meetings" / f"{year}.jsonl.gz", meetings)
            write_rows(RAW / "sessions" / f"{year}.jsonl.gz", sessions)
            live = [s for s in sessions if not s.get("is_cancelled")]
            all_sessions[year] = live
            log(f"{year}: {len(meetings)} meetings, {len(sessions)} sessions "
                f"({len(sessions) - len(live)} cancelled → skipped)")

        # ---- phase 1: light endpoints, subject year ----------------------
        # These are what the pace model, teammate duels and Monte Carlo all
        # run on. ~15 minutes, and the project is viable the moment it ends.
        log("\n--- phase 1: 2026 light endpoints ---")
        subject = order_sessions(all_sessions[SUBJECT_YEAR])
        now = datetime.now(timezone.utc).isoformat()
        subject = [s for s in subject if s["date_start"] < now]
        log(f"{len(subject)} completed 2026 sessions")
        for s in subject:
            fetch_light(client, s, f"{s['circuit_short_name']} {s['session_name']}")
        mark("phase:1", True, flush=True)

        # ---- phase 2: light endpoints, baseline years --------------------
        log("\n--- phase 2: 2025 baseline light endpoints ---")
        for year in BASELINE_YEARS:
            for s in order_sessions(all_sessions[year]):
                fetch_light(client, s, f"{year} {s['circuit_short_name']} {s['session_name']}")
        mark("phase:2", True, flush=True)

        # ---- phase 3: telemetry, subject year ----------------------------
        # Tens of GB. Ordered so races finish first; test days come last and
        # are the acceptable casualty if the night runs out.
        log("\n--- phase 3: 2026 telemetry (car_data + location) ---")
        for s in subject:
            sk = s["session_key"]
            drivers_path = RAW / "drivers" / f"{sk}.jsonl.gz"
            if not drivers_path.exists():
                log(f"  no driver list for sk={sk}, skipping telemetry")
                continue
            with gzip.open(drivers_path, "rt") as f:
                drivers = sorted({json.loads(line)["driver_number"] for line in f})
            label = f"{s['circuit_short_name']} {s['session_name']}"
            log(f"\n  → {label} (sk={sk}, {len(drivers)} drivers)")
            fetch_heavy(client, s, drivers, label)
        mark("phase:3", True, flush=True)

    mark("finished_at", datetime.now().isoformat(), flush=True)
    mins = (time.time() - started) / 60
    log(f"\nHARVEST COMPLETE in {mins:.0f} min")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        mark("interrupted_at", datetime.now().isoformat(), flush=True)
        log("interrupted — rerun to resume from the ledger")
        sys.exit(130)
    except Exception:
        mark("crashed_at", datetime.now().isoformat(), flush=True)
        log("CRASH:\n" + traceback.format_exc())
        sys.exit(1)
