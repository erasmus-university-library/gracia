import json
import random
import shutil
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

import gracia
from gracia.jobs import ACTIVE, JOB_STATES, TERMINAL

NOW = object()  # as a value: the current time (UTC)
_NOW_SQL = "(now() AT TIME ZONE 'UTC')"
# The earlier design had a task table and per-task columns on the jobs table. Jobs live
# for hours,
# so a database that still has that shape just loses its (finished) jobs.
_OLD_SHAPE = (
    "SELECT 1 FROM duckdb_columns() WHERE schema_name = '_gracia' AND table_name = "
    "'jobs' "
    "AND column_name = 'task_count'"
)
JOB_COLUMNS = frozenset(
    {
        "state",
        "message",
        "error_msg",
        "progress",
        "download_file",
        "download_content_type",
        "host",
        "pid",
        "pid_started",
        "attempt",
        "code_version",
        "started_at",
        "heartbeat_at",
        "finished_at",
    }
)
JSON_COLUMNS = frozenset({"progress"})
WRITE_ATTEMPTS = 8


def _states(values: Iterable[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


SCHEMA = f"""
CREATE SCHEMA IF NOT EXISTS _gracia;
CREATE TABLE IF NOT EXISTS _gracia.jobs (
    id             VARCHAR PRIMARY KEY,        -- a uuid4
    repo           VARCHAR NOT NULL,
    created_by     VARCHAR,                    -- user:<userid>, NULL from the CLI
    job            VARCHAR,                    -- the name of the job, if it has one
    subject        VARCHAR,                    -- what it is about (a type slug), if any
    stages         JSON NOT NULL,              -- [{{"stage": ..., "params": ...}}]
    progress       JSON,                       -- the snapshot of every stage
    state          VARCHAR NOT NULL DEFAULT 'queued',
    message        VARCHAR,
    error_msg      VARCHAR,
    download_file  VARCHAR,
    download_content_type VARCHAR,
    host           VARCHAR,
    pid            BIGINT,
    pid_started    BIGINT,                     -- start time of that pid (/proc ticks)
    attempt        INTEGER NOT NULL DEFAULT 0,
    code_version   VARCHAR,
    created_at     TIMESTAMP NOT NULL DEFAULT {_NOW_SQL},
    started_at     TIMESTAMP,
    heartbeat_at   TIMESTAMP,
    finished_at    TIMESTAMP,
    CHECK (state IN ({_states(JOB_STATES)}))
);
"""


def _assign(values: dict, allowed: frozenset) -> tuple[str, list]:
    parts, params = [], []
    for column, value in values.items():
        if column not in allowed:
            raise ValueError(f"not a column that changes: {column}")
        if value is NOW:
            parts.append(f"{column} = {_NOW_SQL}")
        else:
            parts.append(f"{column} = ?")
            params.append(
                json.dumps(value)
                if column in JSON_COLUMNS and value is not None
                else value
            )
    return ", ".join(parts), params


def _decoded(row: dict) -> dict:
    for column in ("stages", "progress"):
        if isinstance(row.get(column), str):
            row[column] = json.loads(row[column])
    return row


class JobsDb:
    def __init__(self, application):
        self.application = application
        self._conn = None

    @property
    def conn(self):
        if self._conn is None:
            conn = self.application.database.cursor()
            if conn.execute(_OLD_SHAPE).fetchall():
                conn.execute("DROP TABLE IF EXISTS _gracia.job_tasks")
                conn.execute("DROP TABLE IF EXISTS _gracia.jobs")
            conn.execute(SCHEMA)
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "JobsDb":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _write(self, sql: str, params=None) -> list[tuple]:
        for attempt in range(WRITE_ATTEMPTS):
            try:
                return self.conn.execute(sql, params or []).fetchall()
            except duckdb.TransactionException:
                if attempt == WRITE_ATTEMPTS - 1:
                    raise
                time.sleep(random.uniform(0.005, 0.05) * (attempt + 1))
        raise AssertionError("unreachable")

    def _rows(self, sql: str, params=None) -> list[dict]:
        cursor = self.conn.execute(sql, params or [])
        names = [d[0] for d in cursor.description]
        return [
            _decoded(dict(zip(names, row, strict=True))) for row in cursor.fetchall()
        ]

    # ---- making a job ----------------------------------------------------------------

    def create(self, spec: dict, created_by: str | None = None) -> str:
        job_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO _gracia.jobs (id, repo, created_by, job, subject, stages, "
            "download_file, download_content_type, code_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                job_id,
                spec["repo"],
                created_by,
                spec.get("job"),
                spec.get("subject"),
                json.dumps(spec["stages"]),
                spec.get("download_file"),
                spec.get("download_content_type"),
                gracia.__version__,
            ],
        )
        return job_id

    # ---- reading
    # ------------------------------------------------------------------------

    def get(self, job_id: str) -> dict | None:
        rows = self._rows("SELECT * FROM _gracia.jobs WHERE id = ?", [job_id])
        return rows[0] if rows else None

    def list_jobs(
        self,
        state: str | None = None,
        repo: str | None = None,
        job: str | None = None,
        subject: str | None = None,
    ) -> list[dict]:
        where, params = [], []
        for column, value in (
            ("state", state),
            ("repo", repo),
            ("job", job),
            ("subject", subject),
        ):
            if value:
                names = value.split(",") if column == "job" else [value]
                where.append(f"{column} IN ({', '.join('?' * len(names))})")
                params.extend(names)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        return self._rows(
            f"SELECT * FROM _gracia.jobs{clause} ORDER BY created_at DESC, id", params
        )

    # ---- state
    # ----------------------------------------------------------------------------

    def update_job(
        self, job_id: str, values: dict, only_states: Iterable[str] | None = None
    ) -> bool:
        assignment, params = _assign(values, JOB_COLUMNS)
        sql = f"UPDATE _gracia.jobs SET {assignment} WHERE id = ?"
        params.append(job_id)
        if only_states is not None:
            sql += f" AND state IN ({_states(only_states)})"
        return bool(self._write(sql + " RETURNING id", params))

    def heartbeat(self, job_id: str) -> None:
        self.update_job(job_id, {"heartbeat_at": NOW})

    # ---- removing
    # ---------------------------------------------------------------------------

    def delete(self, job_id: str, only_states: Iterable[str] = TERMINAL) -> bool:
        gone = self._write(
            "DELETE FROM _gracia.jobs WHERE id = ? "
            f"AND state IN ({_states(only_states)}) RETURNING id",
            [job_id],
        )
        return bool(gone)

    def finished_before(self, seconds: float) -> list[dict]:
        return self._rows(
            f"SELECT * FROM _gracia.jobs WHERE state IN ({_states(TERMINAL)}) "
            f"AND finished_at < {_NOW_SQL} - to_seconds(?) ORDER BY finished_at",
            [seconds],
        )

    def active(self) -> list[dict]:
        return self._rows(
            f"SELECT * FROM _gracia.jobs WHERE state IN ({_states(ACTIVE)})"
        )


@dataclass
class CleanReport:
    jobs: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    bytes: int = 0
    dry_run: bool = False


def safe_job_dir(jobs_dir: Path, name: str) -> Path | None:
    try:
        if str(uuid.UUID(name)) != name:
            return None
    except ValueError:
        return None
    path = jobs_dir / name
    if path.is_symlink() or not path.is_dir():
        return None
    if path.resolve().parent != jobs_dir.resolve():
        return None
    return path


def _size(path: Path) -> int:
    return sum(
        f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink()
    )


def clean(
    db: JobsDb, jobs_dir: Path, older_than: float, dry_run: bool = False
) -> CleanReport:
    report = CleanReport(dry_run=dry_run)
    for job in db.finished_before(older_than):
        directory = safe_job_dir(jobs_dir, job["id"])
        size = _size(directory) if directory else 0
        if dry_run:
            report.jobs.append(job["id"])
            report.bytes += size
            continue
        if db.delete(job["id"]):
            report.jobs.append(job["id"])
            report.bytes += size
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)
    if jobs_dir.is_dir():
        known = {job["id"] for job in db._rows("SELECT id FROM _gracia.jobs")}
        cutoff = time.time() - older_than
        for entry in jobs_dir.iterdir():
            directory = safe_job_dir(jobs_dir, entry.name)
            if (
                directory is None
                or entry.name in known
                or directory.stat().st_mtime > cutoff
            ):
                continue
            report.orphans.append(entry.name)
            report.bytes += _size(directory)
            if not dry_run:
                shutil.rmtree(directory, ignore_errors=True)
    return report
