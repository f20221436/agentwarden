"""Annotate eval executions that overlapped a Windows Modern Standby (laptop sleep) period.

Evidence source: Windows System event log, Microsoft-Windows-Kernel-Power events 506 (entering Modern
Standby) and 507 (exiting). Annotation only: pass/fail and metrics are never changed. The report lists the
affected executions so readers can discount them.

  python scripts/annotate_standby.py [--hours 48]        (Windows only)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime

from agentwarden.config import Settings
from agentwarden.store import connect

PS = (
    "$s=(Get-Date).AddHours(-{hours}); "
    "Get-WinEvent -FilterHashtable @{{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power'; "
    "Id=506,507; StartTime=$s}} -ErrorAction SilentlyContinue | "
    "Sort-Object TimeCreated | ForEach-Object {{ [pscustomobject]@{{id=$_.Id; "
    "t=$_.TimeCreated.ToUniversalTime().ToString('o')}} }} | ConvertTo-Json -Compress"
)


def standby_windows(hours: int) -> list[tuple[datetime, datetime]]:
    out = subprocess.run(["powershell", "-NoProfile", "-Command", PS.format(hours=hours)], capture_output=True,
                         text=True, check=True).stdout.strip()
    events = json.loads(out) if out else []
    events = events if isinstance(events, list) else [events]
    windows, start = [], None
    for e in events:
        t = datetime.fromisoformat(e["t"].replace("Z", "+00:00")).astimezone(UTC)
        if e["id"] == 506:
            start = t
        elif e["id"] == 507 and start is not None:
            windows.append((start, t))
            start = None
    return windows


def main() -> int:
    if sys.platform != "win32":
        print("Windows only")
        return 1
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=48)
    a = ap.parse_args()
    windows = standby_windows(a.hours)
    print(f"{len(windows)} Modern Standby periods in the last {a.hours} h")
    store = connect(Settings())
    db = store.db
    n = 0
    for ex in db.executions.find({"eval_run_id": {"$ne": None}, "ended_at": {"$exists": True}}):
        s, e = ex["started_at"], ex["ended_at"]
        overlaps = [(ws, we) for ws, we in windows if ws < e and we > s]
        if not overlaps:
            continue
        secs = sum((min(we, e) - max(ws, s)).total_seconds() for ws, we in overlaps)
        incident = {"type": "modern_standby", "overlap_s": round(secs, 1),
                    "windows": [[ws.isoformat(), we.isoformat()] for ws, we in overlaps],
                    "evidence": "Windows System log, Kernel-Power events 506/507"}
        db.executions.update_one({"_id": ex["_id"]}, {"$set": {"infra_incident": incident}})
        db.eval_results.update_one({"execution_id": ex["_id"]}, {"$set": {"infra_incident": incident}})
        n += 1
        print(f"  {ex['_id']} task={ex.get('task_id')} overlap={secs:.0f}s status={ex.get('status')}")
    print(f"annotated {n} executions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
