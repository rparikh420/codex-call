"""SQLite persistence with atomic idempotency and lifecycle claims."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from codex_call.models import (
    TERMINAL_STATES,
    CallRequest,
    CallUpdate,
    TaskRequest,
    TaskResult,
    TranscriptItem,
)


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL, room_name TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, error TEXT
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    call_id TEXT NOT NULL REFERENCES calls(id), request_id TEXT NOT NULL,
                    prompt TEXT NOT NULL, timeout_s INTEGER NOT NULL, status TEXT NOT NULL,
                    result TEXT, expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
                    event_id TEXT NOT NULL UNIQUE, PRIMARY KEY(call_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, url TEXT NOT NULL,
                    name TEXT NOT NULL, arguments TEXT NOT NULL, secret TEXT NOT NULL,
                    old_secret TEXT, rotate_until TEXT, expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                    event_id TEXT NOT NULL, call_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    body BLOB NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL,
                    PRIMARY KEY(subscription_id, event_id)
                );
                CREATE TRIGGER IF NOT EXISTS cancel_call_tasks AFTER UPDATE OF status ON calls
                WHEN NEW.status IN ('completed','failed','cancelled') BEGIN
                    UPDATE tasks SET status='cancelled'
                        WHERE call_id=NEW.id AND status='pending';
                    DELETE FROM outbox WHERE call_id=NEW.id AND status='pending';
                END;
                CREATE TRIGGER IF NOT EXISTS stop_task_delivery AFTER UPDATE OF status ON tasks
                WHEN NEW.status != 'pending' BEGIN
                    DELETE FROM outbox WHERE call_id=NEW.call_id AND request_id=NEW.request_id
                        AND status='pending';
                END;
                CREATE TABLE IF NOT EXISTS transcripts (
                    call_id TEXT NOT NULL REFERENCES calls(id), item_id TEXT NOT NULL,
                    role TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(call_id, item_id)
                );
            """)

        with self.connect() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(calls)")}
            for name, definition in [
                ("mode", "TEXT NOT NULL DEFAULT 'simulate'"),
                ("deadline_at", "TEXT"),
                ("cleanup_pending", "INTEGER NOT NULL DEFAULT 0"),
            ]:
                if name not in columns:
                    db.execute(f"ALTER TABLE calls ADD COLUMN {name} {definition}")

    @contextmanager
    def connect(self):
        """Yield a fresh transaction; extensions can safely create their own tables."""
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _record(row) -> dict:
        return {
            "id": row["id"],
            "room_name": row["room_name"],
            **json.loads(row["payload"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "error": row["error"],
            "mode": row["mode"],
        }

    @staticmethod
    def _row(db, call_id):
        row = db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        if row is None:
            raise NotFound("call_not_found")
        return row

    def create_call(
        self, request: CallRequest | dict, key: str, mode: str = "simulate"
    ) -> tuple[dict, bool]:
        if mode not in {"simulate", "live"}:
            raise ValueError("invalid_mode")
        request = CallRequest.model_validate(request)
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise ValueError("invalid_idempotency_key")
        payload = json.dumps(request.model_dump(), sort_keys=True)
        with self.connect() as db:
            row = db.execute("SELECT * FROM calls WHERE idempotency_key=?", (key,)).fetchone()
            if row is not None:
                if row["payload"] != payload:
                    raise Conflict("idempotency_conflict")
                return self._record(row), False
            call_id = str(uuid4())
            timestamp = now()
            db.execute(
                "INSERT INTO calls "
                "(id,idempotency_key,payload,room_name,status,created_at,updated_at,error,mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    call_id,
                    key,
                    payload,
                    f"codex-call-{call_id}",
                    "queued",
                    timestamp,
                    timestamp,
                    None,
                    mode,
                ),
            )
            return self._record(self._row(db, call_id)), True

    def get_call(self, call_id: str) -> dict:
        with self.connect() as db:
            return self._record(self._row(db, call_id))

    def list_calls(self, limit: int = 100, offset: int = 0) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM calls ORDER BY created_at, id LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
            return [self._record(row) for row in rows]

    def active_calls(self) -> list[dict]:
        with self.connect() as db:
            return [
                self._record(row)
                for row in db.execute(
                    "SELECT * FROM calls WHERE status IN ('dispatching','dialing','answered')"
                ).fetchall()
            ]

    def transition(self, call_id: str, status: str, error: str | None = None) -> dict:
        update = CallUpdate(status=status, error=error)
        allowed = {
            "queued": {"dispatching", "cancelled", "failed"},
            "dispatching": {"dialing", "answered", "completed", "cancelled", "failed"},
            "dialing": {"answered", "completed", "cancelled", "failed"},
            "answered": {"completed", "cancelled", "failed"},
        }
        with self.connect() as db:
            row = self._row(db, call_id)
            previous = row["status"]
            if previous == status:
                return self._record(row)
            if previous in TERMINAL_STATES or status not in allowed.get(previous, set()):
                raise Conflict("invalid_transition")
            db.execute(
                "UPDATE calls SET status=?,error=?,updated_at=? WHERE id=?",
                (update.status, update.error, now(), call_id),
            )
            if status == "failed" and error in {
                "cleanup_failed",
                "provider_failed",
                "duration_exceeded",
            }:
                db.execute("UPDATE calls SET cleanup_pending=1 WHERE id=?", (call_id,))
            if status == "dispatching" and row["deadline_at"] is None:
                self._set_deadline(db, row)
            return self._record(self._row(db, call_id))

    @staticmethod
    def _set_deadline(db, row):
        duration = json.loads(row["payload"])["max_duration_s"]
        deadline = (datetime.now(UTC) + timedelta(seconds=duration)).isoformat()
        db.execute(
            "UPDATE calls SET deadline_at=? WHERE id=? AND deadline_at IS NULL",
            (deadline, row["id"]),
        )

    def worker_call(self, call_id):
        with self.connect() as db:
            row = self._row(db, call_id)
            return self._record(row) | {"deadline_at": row["deadline_at"]}

    def claim_worker(self, call_id):
        with self.connect() as db:
            row = self._row(db, call_id)
            if row["status"] != "dispatching":
                raise Conflict("worker_claim_conflict")
            if row["mode"] != "live":
                raise Conflict("worker_requires_live_call")
            db.execute(
                "UPDATE calls SET status='dialing',updated_at=? WHERE id=?", (now(), call_id)
            )
            return self._record(self._row(db, call_id)) | {"deadline_at": row["deadline_at"]}

    def expire_calls(self, timestamp: datetime):
        with self.connect() as db:
            db.execute(
                """UPDATE calls SET status='failed',error='duration_exceeded',
                          cleanup_pending=1,updated_at=? WHERE deadline_at<=?
                          AND status IN ('dispatching','dialing','answered')""",
                (timestamp.isoformat(), timestamp.isoformat()),
            )
            return [
                self._record(row)
                for row in db.execute("SELECT * FROM calls WHERE cleanup_pending=1").fetchall()
            ]

    def mark_cleanup(self, call_id):
        """Persist room deletion work independently of terminal lifecycle ownership."""
        with self.connect() as db:
            self._row(db, call_id)
            db.execute("UPDATE calls SET cleanup_pending=1 WHERE id=?", (call_id,))
            return self._record(self._row(db, call_id))

    def clear_cleanup(self, call_id):
        with self.connect() as db:
            db.execute("UPDATE calls SET cleanup_pending=0 WHERE id=?", (call_id,))

    def claim_queued(self) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM calls WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE calls SET status='dispatching',updated_at=? WHERE id=?", (now(), row["id"])
            )
            self._set_deadline(db, row)
            return self._record(self._row(db, row["id"]))

    def append_transcript(self, call_id: str, item: TranscriptItem | dict) -> tuple[dict, bool]:
        item = TranscriptItem.model_validate(item)
        with self.connect() as db:
            self._row(db, call_id)
            previous = db.execute(
                "SELECT * FROM transcripts WHERE call_id=? AND item_id=?", (call_id, item.item_id)
            ).fetchone()
            if previous is not None:
                if previous["role"] != item.role or previous["text"] != item.text:
                    raise Conflict("transcript_conflict")
                return dict(previous), False
            timestamp = now()
            db.execute(
                "INSERT INTO transcripts VALUES (?, ?, ?, ?, ?)",
                (call_id, item.item_id, item.role, item.text, timestamp),
            )
            return {"call_id": call_id, **item.model_dump(), "created_at": timestamp}, True

    def transcript(self, call_id: str) -> list[dict]:
        with self.connect() as db:
            self._row(db, call_id)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM transcripts WHERE call_id=? ORDER BY rowid", (call_id,)
                ).fetchall()
            ]

    @staticmethod
    def _task_record(row):
        return {
            key: row[key]
            for key in (
                "call_id",
                "request_id",
                "prompt",
                "status",
                "result",
                "expires_at",
                "created_at",
            )
        }

    @staticmethod
    def _expire_tasks(db):
        db.execute(
            "UPDATE tasks SET status='expired' WHERE status='pending' AND expires_at<=?", (now(),)
        )

    def create_task(self, call_id, request):
        request = TaskRequest.model_validate(request)
        request_id = str(request.request_id)
        with self.connect() as db:
            call = self._row(db, call_id)
            if call["status"] in TERMINAL_STATES:
                raise Conflict("call_ended")
            self._expire_tasks(db)
            previous = db.execute(
                "SELECT * FROM tasks WHERE call_id=? AND request_id=?", (call_id, request_id)
            ).fetchone()
            if previous:
                if (
                    previous["prompt"] != request.prompt
                    or previous["timeout_s"] != request.timeout_s
                ):
                    raise Conflict("task_conflict")
                return self._task_record(previous)
            timestamp = now()
            expiration = (datetime.now(UTC) + timedelta(seconds=request.timeout_s)).isoformat()
            event_id = str(uuid4())
            db.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    call_id,
                    request_id,
                    request.prompt,
                    request.timeout_s,
                    "pending",
                    None,
                    expiration,
                    timestamp,
                    event_id,
                ),
            )
            data = {
                "call_id": call_id,
                "request_id": request_id,
                "prompt": request.prompt,
                "expires_at": expiration,
            }
            body = json.dumps(
                {
                    "eventId": event_id,
                    "name": "call.task_requested",
                    "timestamp": timestamp,
                    "data": data,
                    "cursor": None,
                },
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
            for sub in db.execute("SELECT * FROM subscriptions WHERE expires_at>?", (timestamp,)):
                arguments = json.loads(sub["arguments"])
                if arguments.get("call_id", call_id) == call_id:
                    db.execute(
                        "INSERT INTO outbox (subscription_id,event_id,call_id,request_id,"
                        "body,next_attempt_at) VALUES (?,?,?,?,?,?)",
                        (sub["id"], event_id, call_id, request_id, body, timestamp),
                    )
            return self._task_record(
                db.execute(
                    "SELECT * FROM tasks WHERE call_id=? AND request_id=?", (call_id, request_id)
                ).fetchone()
            )

    def get_task(self, call_id, request_id):
        with self.connect() as db:
            self._row(db, call_id)
            self._expire_tasks(db)
            row = db.execute(
                "SELECT * FROM tasks WHERE call_id=? AND request_id=?", (call_id, request_id)
            ).fetchone()
            if row is None:
                raise NotFound("task_not_found")
            return self._task_record(row)

    def list_tasks(self, call_id):
        with self.connect() as db:
            self._row(db, call_id)
            self._expire_tasks(db)
            return [
                self._task_record(row)
                for row in db.execute(
                    "SELECT * FROM tasks WHERE call_id=? ORDER BY created_at, request_id",
                    (call_id,),
                )
            ]

    def submit_result(self, call_id, request_id, result):
        result = TaskResult(result=result).result
        with self.connect() as db:
            self._row(db, call_id)
            self._expire_tasks(db)
            row = db.execute(
                "SELECT * FROM tasks WHERE call_id=? AND request_id=?", (call_id, request_id)
            ).fetchone()
            if row is None:
                raise NotFound("task_not_found")
            if row["status"] == "completed" and row["result"] == result:
                return self._task_record(row)
            if row["status"] == "pending" and row["expires_at"] <= now():
                db.execute(
                    "UPDATE tasks SET status='expired' WHERE call_id=? AND request_id=?",
                    (call_id, request_id),
                )
                conflict = True
            else:
                conflict = row["status"] != "pending"
            if not conflict:
                db.execute(
                    "UPDATE tasks SET status='completed',result=? WHERE call_id=? AND request_id=?",
                    (result, call_id, request_id),
                )
                record = self._task_record(
                    db.execute(
                        "SELECT * FROM tasks WHERE call_id=? AND request_id=?",
                        (call_id, request_id),
                    ).fetchone()
                )
        # Raise only after committing expiry and the corresponding outbox deletion.
        if conflict:
            raise Conflict("task_result_conflict")
        return record
