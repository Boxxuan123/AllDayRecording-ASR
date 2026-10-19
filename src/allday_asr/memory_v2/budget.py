"""Durable reservation ledger, separate from every historical budget."""

import json
import sqlite3
import time
from pathlib import Path


class BudgetExceeded(RuntimeError):
    pass


class Ledger:
    def __init__(self, root, max_calls=48, max_tokens=800_000):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "budget.sqlite3"
        self.max_calls = min(max_calls, 48)
        self.max_tokens = min(max_tokens, 800_000)
        with self.connect() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS attempts(
                id INTEGER PRIMARY KEY, phase TEXT, started REAL, completed REAL,
                status TEXT, reserved INTEGER, used INTEGER, input_tokens INTEGER,
                output_tokens INTEGER, error TEXT)""")

            c.execute(
                "CREATE TABLE IF NOT EXISTS reconciliations(attempt_id INTEGER PRIMARY KEY, tokens INTEGER, evidence TEXT)"
            )

    def reconcile_not_dispatched(self, number, evidence):
        # Append proof without overwriting the original failed/unknown record.
        with self.connect() as c:
            row = c.execute(
                "SELECT status,error FROM attempts WHERE id=?", (number,)
            ).fetchone()
            if row != ("failed", "model batch budget exhausted"):
                raise ValueError("NOT_A_PROVEN_LOCAL_PREDISPATCH_FAILURE")
            c.execute(
                "INSERT INTO reconciliations VALUES(?,0,?)",
                (number, json.dumps(evidence)),
            )

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def reserve(self, phase, reserve):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            count, committed = c.execute(
                "SELECT count(*),coalesce(sum(coalesce(a.used,r.tokens,a.reserved)),0) FROM attempts a LEFT JOIN reconciliations r ON r.attempt_id=a.id"
            ).fetchone()
            if count >= self.max_calls or committed + reserve > self.max_tokens:
                raise BudgetExceeded("STAGE_BUDGET_EXHAUSTED")
            cur = c.execute(
                "INSERT INTO attempts(phase,started,status,reserved) VALUES(?,?,'started',?)",
                (phase, time.time(), reserve),
            )
            return cur.lastrowid

    def finish(self, number, usage=None, error=None):
        # Unknown usage always retains the full reservation, including timeouts.
        u = usage or {}
        u = u.get("total", u)
        inbound = u.get("input_tokens", u.get("inputTokens"))
        outbound = u.get("output_tokens", u.get("outputTokens"))
        known = isinstance(inbound, int) and isinstance(outbound, int)
        with self.connect() as c:
            c.execute(
                "UPDATE attempts SET completed=?,status=?,used=?,input_tokens=?,output_tokens=?,error=? WHERE id=?",
                (
                    time.time(),
                    "failed" if error else "completed",
                    inbound + outbound if known else None,
                    inbound,
                    outbound,
                    str(error)[:1000] if error else None,
                    number,
                ),
            )
        self.export()

    def export(self):
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            rows = [dict(x) for x in c.execute("SELECT * FROM attempts ORDER BY id")]
            resolutions = {
                r["attempt_id"]: dict(r)
                for r in c.execute("SELECT * FROM reconciliations")
            }
        for row in rows:
            if row["id"] in resolutions:
                row["reconciliation"] = resolutions[row["id"]]

        def actual(row):
            return (
                row["used"]
                if row["used"] is not None
                else resolutions.get(row["id"], {}).get("tokens")
            )

        report = {
            "max_calls": self.max_calls,
            "max_tokens": self.max_tokens,
            "attempts": rows,
            "known_tokens": sum(actual(r) or 0 for r in rows),
            "accounted_tokens": sum(
                actual(r) if actual(r) is not None else r["reserved"] for r in rows
            ),
            "unknown_usage_count": sum(actual(r) is None for r in rows),
            "verified_currency_cost": None,
        }
        (self.root / "ledger.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        return report
