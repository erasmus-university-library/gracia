import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from gracia.jobs import TERMINAL
from gracia.jobs.db import NOW, JobsDb

DEFAULT_GRACE = 15.0  # seconds between SIGTERM and SIGKILL
SPAWN_SETTLE = 5.0  # a job this young with no live process is not judged yet


def _stat_fields(pid: int) -> list[str] | None:
    """The fields of /proc/<pid>/stat after the name (which may hold spaces)."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return text.rsplit(")", 1)[1].split()


def start_time(pid: int) -> int | None:
    fields = _stat_fields(pid)
    return int(fields[19]) if fields else None


def is_alive(pid: int | None, started: int | None = None) -> bool:
    if not pid:
        return False
    if Path("/proc/self/stat").exists():
        fields = _stat_fields(pid)
        if fields is None or fields[0] in ("Z", "X"):
            return False
        return started is None or start_time(pid) == started
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def spawn(job_id: str, log_path: Path, config: str | None = None, db: str | None = None) -> int:
    command = [sys.executable, "-m", "gracia"]
    if config:
        command += ["-c", str(config)]
    if db:
        command += ["-d", str(db)]
    command += ["run", job_id]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    _children.append(process)
    return process.pid


def spawn_stop(
    job_id: str, config: str | None = None, db: str | None = None, by: str | None = None
) -> int:
    command = [sys.executable, "-m", "gracia"]
    if config:
        command += ["-c", str(config)]
    if db:
        command += ["-d", str(db)]
    command += ["jobs", "stop", job_id]
    if by:
        command += ["--by", by]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    _children.append(process)
    return process.pid


_children: list[subprocess.Popen] = []


def reap_children() -> None:
    for process in list(_children):
        if process.poll() is not None:
            _children.remove(process)


class JobStateError(RuntimeError):
    pass


def start_job(
    db: JobsDb,
    job_id: str,
    jobs_dir: Path,
    config: str | None = None,
    database: str | None = None,
    host: str | None = None,
) -> int:
    host = host or socket.gethostname()
    job = db.get(job_id)
    if job is None:
        raise KeyError(job_id)
    if job["state"] in ("stopped", "failed"):
        db.update_job(
            job_id,
            {
                "state": "queued",
                "attempt": job["attempt"] + 1,
                "error_msg": None,
                "finished_at": None,
                "message": None,
            },
            only_states=("stopped", "failed"),
        )
    elif job["state"] != "queued":
        raise JobStateError(f"the job is {job['state']}")
    pid = spawn(job_id, jobs_dir / job_id / "job.log", config, database)
    db.update_job(
        job_id,
        {
            "state": "running",
            "pid": pid,
            "pid_started": start_time(pid),
            "host": host,
            "heartbeat_at": NOW,
            **({"started_at": NOW} if job["started_at"] is None else {}),
        },
        only_states=("queued",),
    )
    return pid


def exit_record(job_dir: Path) -> dict | None:
    try:
        return json.loads((job_dir / "exit.json").read_text())
    except (OSError, ValueError):
        return None


def reconcile(db: JobsDb, jobs_dir: Path, host: str | None = None) -> list[str]:
    host = host or socket.gethostname()
    reap_children()
    judged = []
    for job in db.active():
        if job["state"] not in ("running", "stopping") or job["host"] != host or not job["pid"]:
            continue
        if is_alive(job["pid"], job["pid_started"]):
            continue
        moment = job["started_at"]
        seconds = (
            float("inf")
            if moment is None
            else (datetime.now(UTC).replace(tzinfo=None) - moment).total_seconds()
        )
        if seconds < SPAWN_SETTLE:
            continue
        last = exit_record(jobs_dir / job["id"])
        message = (last or {}).get("error") or "the process disappeared"
        if db.update_job(
            job["id"],
            {"state": "failed", "error_msg": message, "finished_at": NOW},
            only_states=("running", "stopping"),
        ):
            judged.append(job["id"])
    return judged


def stop_job(
    db: JobsDb,
    job_id: str,
    grace: float = DEFAULT_GRACE,
    now: bool = False,
    host: str | None = None,
    who: str | None = None,
) -> str:
    host = host or socket.gethostname()
    stopped_by = f"stopped by {who}" if who else "stopped"
    job = db.get(job_id)
    if job is None:
        raise KeyError(job_id)
    if job["state"] in TERMINAL:
        return "already"
    if job["state"] == "queued":
        done = db.update_job(
            job_id,
            {"state": "stopped", "finished_at": NOW, "message": stopped_by},
            only_states=("queued",),
        )
        return "stopped" if done else "stopping"
    if not db.update_job(job_id, {"state": "stopping"}, only_states=("running",)):
        return "stopping"  # another stop is in progress
    pid, started = job["pid"], job["pid_started"]
    if job["host"] == host and is_alive(pid, started) and _leads_its_group(pid):
        _signal_group(pid, signal.SIGKILL if now else signal.SIGTERM)
        deadline = time.monotonic() + (0 if now else grace)
        while is_alive(pid, started) and time.monotonic() < deadline:
            time.sleep(0.1)
            reap_children()
        if is_alive(pid, started):
            _signal_group(pid, signal.SIGKILL)
            for _ in range(50):  # it takes a moment to go
                if not is_alive(pid, started):
                    break
                time.sleep(0.1)
                reap_children()
    elif job["host"] != host:
        raise RuntimeError(f"the job runs on {job['host']}, not on {host}: stop it there")
    message = stopped_by if job["pid"] else "stopped (the process was gone)"
    db.update_job(
        job_id,
        {"state": "stopped", "finished_at": NOW, "message": message},
        only_states=("stopping", "running"),
    )
    return "stopped"


def _leads_its_group(pid: int) -> bool:
    try:
        return os.getpgid(pid) == pid
    except OSError:
        return False


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass
