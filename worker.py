from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from datetime import datetime, timedelta
from uuid import uuid4

from app import (
    app,
    get_db,
    get_sla_calendar,
    calendar_now,
    now_iso,
    run_sla_escalation_check,
)


def run_cycle(owner_token: str) -> tuple[bool, dict]:
    """Claim the worker lease, heartbeat and run a due SLA cycle when enabled."""
    with app.app_context():
        db = get_db()
        current = datetime.now().replace(microsecond=0)
        try:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute("SELECT * FROM worker_state WHERE id=1").fetchone()
            lease_until = None
            if state and state["lease_until"]:
                try:
                    lease_until = datetime.fromisoformat(state["lease_until"])
                except ValueError:
                    lease_until = None
            if (
                state and state["owner_token"] and state["owner_token"] != owner_token
                and lease_until and lease_until > current
            ):
                db.rollback()
                return False, {"status": "busy", "owner": state["worker_name"]}

            started_at = state["started_at"] if state and state["owner_token"] == owner_token else now_iso()
            db.execute(
                """UPDATE worker_state
                SET owner_token=?, worker_name='PulseDesk SLA Worker', status='starting',
                    started_at=?, last_heartbeat=?, lease_until=?, last_error=''
                WHERE id=1""",
                (owner_token, started_at, now_iso(),
                 (current + timedelta(seconds=35)).isoformat(timespec="seconds")),
            )
            db.commit()

            calendar = get_sla_calendar(db)
            if not calendar["scheduler_enabled"]:
                db.execute(
                    """UPDATE worker_state
                    SET status='paused', last_heartbeat=?, lease_until=?, last_error=''
                    WHERE id=1 AND owner_token=?""",
                    (now_iso(), (datetime.now() + timedelta(seconds=35)).isoformat(timespec="seconds"), owner_token),
                )
                db.commit()
                return True, {"status": "paused", "message": "Worker online; scheduled checks are disabled."}

            state = db.execute("SELECT last_run FROM worker_state WHERE id=1").fetchone()
            last_run = None
            if state and state["last_run"]:
                try:
                    last_run = datetime.fromisoformat(state["last_run"])
                except ValueError:
                    last_run = None
            interval = int(calendar["interval_seconds"])
            is_due = last_run is None or (calendar_now(calendar) - last_run).total_seconds() >= interval

            result = None
            if is_due:
                result = run_sla_escalation_check(db, None, "PulseDesk SLA Worker")
                db.execute(
                    """UPDATE worker_state
                    SET status='running', last_heartbeat=?, last_run=?, last_result=?,
                        last_error='', lease_until=?
                    WHERE id=1 AND owner_token=?""",
                    (now_iso(), result["checked_at"], json.dumps(result, ensure_ascii=False),
                     (datetime.now() + timedelta(seconds=35)).isoformat(timespec="seconds"), owner_token),
                )
            else:
                db.execute(
                    """UPDATE worker_state
                    SET status='running', last_heartbeat=?, last_error='', lease_until=?
                    WHERE id=1 AND owner_token=?""",
                    (now_iso(), (datetime.now() + timedelta(seconds=35)).isoformat(timespec="seconds"), owner_token),
                )
            db.commit()
            return True, {
                "status": "running",
                "checked": bool(is_due),
                "result": result,
                "interval_seconds": interval,
            }
        except Exception as exc:
            db.rollback()
            db.execute(
                """UPDATE worker_state
                SET status='error', last_heartbeat=?, last_error=?, lease_until=?
                WHERE id=1 AND owner_token=?""",
                (now_iso(), str(exc)[:1000],
                 (datetime.now() + timedelta(seconds=35)).isoformat(timespec="seconds"), owner_token),
            )
            db.commit()
            return True, {"status": "error", "error": str(exc)}


def release_worker(owner_token: str, status: str = "offline") -> None:
    with app.app_context():
        db = get_db()
        db.execute(
            """UPDATE worker_state
            SET status=?, owner_token=NULL, lease_until=NULL, last_heartbeat=?
            WHERE id=1 AND owner_token=?""",
            (status, now_iso(), owner_token),
        )
        db.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description="PulseDesk scheduled SLA escalation worker")
    parser.add_argument("--once", action="store_true", help="perform one configured cycle and exit")
    args = parser.parse_args()
    owner_token = str(uuid4())
    stop_requested = False

    def request_stop(_signal, _frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    claimed, result = run_cycle(owner_token)
    if not claimed:
        print("Another PulseDesk worker currently holds the processing lease.", flush=True)
        return 2
    print(f"PulseDesk SLA worker: {result['status']}", flush=True)
    if args.once:
        release_worker(owner_token, "offline")
        return 0 if result["status"] != "error" else 1

    try:
        while not stop_requested:
            with app.app_context():
                interval = int(get_sla_calendar(get_db())["interval_seconds"])
                enabled = bool(get_sla_calendar(get_db())["scheduler_enabled"])
            time.sleep(10 if not enabled else min(10, max(2, interval / 4)))
            if stop_requested:
                break
            claimed, result = run_cycle(owner_token)
            if not claimed:
                print("Worker lease lost to another process; stopping.", flush=True)
                return 2
            if result["status"] == "error":
                print(f"Worker error: {result['error']}", file=sys.stderr, flush=True)
            elif result.get("checked") and result.get("result"):
                print(
                    f"Checked {result['result']['checked']} incident(s); "
                    f"new escalations: {result['result']['escalated_count']}.",
                    flush=True,
                )
    finally:
        release_worker(owner_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
