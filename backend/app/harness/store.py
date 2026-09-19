"""Durable checkpoints and append-only run events in a separate SQLite database."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


DEFAULT_DB = Path(__file__).resolve().parents[3] / "data" / "harness_runs.db"


class RunBusy(Exception):
    """Another worker currently owns this run."""


class LeaseLost(Exception):
    """A worker attempted to write after its lease was taken over."""


class RunStore:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, state TEXT NOT NULL, "
                       "lease_owner TEXT, lease_until REAL)")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
            if "lease_owner" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN lease_owner TEXT")
            if "lease_until" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN lease_until REAL")
            db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "run_id TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, "
                       "created_at TEXT DEFAULT CURRENT_TIMESTAMP)")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Commit or roll back, then close the file handle on every platform."""
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            with db:
                yield db
        finally:
            db.close()

    def create(self, question: str, mode: str) -> dict:
        run = {"run_id": uuid.uuid4().hex, "question": question, "mode": mode,
               "status": "ready", "transcript": [{"role": "user", "content": question}],
               "rounds": 0, "tool_calls": 0, "answer": None, "error": None}
        with self.connect() as db:
            db.execute("INSERT INTO runs(id,state) VALUES(?,?)",
                       (run["run_id"], json.dumps(run, ensure_ascii=False)))
            db.execute("INSERT INTO events(run_id,kind,detail) VALUES(?,?,?)",
                       (run["run_id"], "created", json.dumps({"mode": mode})))
        return run

    def acquire(self, run_id: str, lease_seconds: float = 30.0) -> tuple[dict, str]:
        """Claim one run atomically across processes; expired claims can be recovered."""
        token = uuid.uuid4().hex
        now = time.time()
        with self.connect() as db:
            updated = db.execute(
                "UPDATE runs SET lease_owner=?,lease_until=? WHERE id=? "
                "AND (lease_owner IS NULL OR lease_until<=?)",
                (token, now + lease_seconds, run_id, now),
            ).rowcount
            if not updated:
                if db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
                    raise KeyError(run_id)
                raise RunBusy(run_id)
            row = db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row["state"]), token

    def renew(self, run_id: str, token: str, lease_seconds: float = 30.0) -> None:
        now = time.time()
        with self.connect() as db:
            updated = db.execute(
                "UPDATE runs SET lease_until=? WHERE id=? AND lease_owner=? AND lease_until>?",
                (now + lease_seconds, run_id, token, now),
            ).rowcount
            if not updated:
                raise LeaseLost(run_id)

    def release(self, run_id: str, token: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE runs SET lease_owner=NULL,lease_until=NULL "
                       "WHERE id=? AND lease_owner=?", (run_id, token))

    def get(self, run_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
        return json.loads(row["state"]) if row else None

    def save(self, run: dict, kind: str, detail: dict, token: str | None = None) -> None:
        """Checkpoint and event commit in one transaction."""
        with self.connect() as db:
            if token is None:
                updated = db.execute("UPDATE runs SET state=? WHERE id=? AND lease_owner IS NULL",
                                     (json.dumps(run, ensure_ascii=False), run["run_id"])).rowcount
            else:
                updated = db.execute("UPDATE runs SET state=? WHERE id=? "
                                     "AND lease_owner=? AND lease_until>?",
                                     (json.dumps(run, ensure_ascii=False), run["run_id"],
                                      token, time.time())).rowcount
            if not updated:
                raise LeaseLost(run["run_id"])
            db.execute("INSERT INTO events(run_id,kind,detail) VALUES(?,?,?)",
                       (run["run_id"], kind, json.dumps(detail, ensure_ascii=False)))

    def events(self, run_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id,kind,detail,created_at FROM events WHERE run_id=? ORDER BY id",
                              (run_id,)).fetchall()
        return [{"id": r["id"], "kind": r["kind"], "detail": json.loads(r["detail"]),
                 "created_at": r["created_at"]} for r in rows]
