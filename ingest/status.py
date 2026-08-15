"""What has the harvester actually got so far?

Reads the files on disk rather than the resume ledger — the ledger says what
was attempted, this says what landed.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
STATE = RAW / "_state.json"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def count_lines(path: Path) -> int:
    try:
        with gzip.open(path, "rt") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def main() -> None:
    if not RAW.exists():
        print("нічого ще не завантажено")
        return

    print(f"{'endpoint':<18} {'файлів':>8} {'розмір':>11}   {'рядків':>12}")
    print("-" * 56)
    total_size = total_files = 0
    for ep in sorted(p for p in RAW.iterdir() if p.is_dir()):
        files = list(ep.rglob("*.jsonl.gz"))
        if not files:
            continue
        size = sum(f.stat().st_size for f in files)
        total_size += size
        total_files += len(files)
        # Counting every telemetry line would take minutes; sample instead.
        if len(files) <= 40:
            rows = f"{sum(count_lines(f) for f in files):,}"
        else:
            sample = files[:12]
            avg = sum(count_lines(f) for f in sample) / len(sample)
            rows = f"~{int(avg * len(files)):,}"
        print(f"{ep.name:<18} {len(files):>8} {human(size):>11}   {rows:>12}")
    print("-" * 56)
    print(f"{'ВСЬОГО':<18} {total_files:>8} {human(total_size):>11}")

    if STATE.exists():
        st = json.loads(STATE.read_text())
        phases = [k for k in ("phase:1", "phase:2", "phase:3") if st.get(k)]
        print(f"\nзавершені фази: {', '.join(phases) if phases else 'жодної'}")
        for k in ("finished_at", "interrupted_at", "crashed_at"):
            if st.get(k):
                print(f"{k}: {st[k]}")

    log = ROOT / "data" / "logs" / "fetch.log"
    if log.exists():
        mtime = datetime.fromtimestamp(log.stat().st_mtime)
        age = (datetime.now() - mtime).total_seconds()
        state = "качає" if age < 120 else f"тиша вже {age/60:.0f} хв"
        print(f"остання активність: {mtime:%H:%M:%S}  ({state})")
        tail = log.read_text().splitlines()[-3:]
        for line in tail:
            print(f"  {line}")


if __name__ == "__main__":
    main()
