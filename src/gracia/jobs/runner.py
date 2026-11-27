import contextlib
import json
import logging
import os
import signal
import socket
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path

import luigi
from luigi.execution_summary import LuigiStatusCode

import gracia
from gracia.jobs import procs
from gracia.jobs.db import NOW, JobsDb
from gracia.jobs import context
from gracia.jobs.progress import Snapshotter
from gracia.jobs.stage import (JobTask, Stage)
from gracia.jobs.builtin import Plan

EXIT_DONE, EXIT_FAILED, EXIT_NOTHING, EXIT_STOPPED = 0, 1, 2, 3
REPORT_EVERY = 1.0  # seconds between snapshots
REJECTED_FILE = "rejected.jsonl"

log = logging.getLogger("gracia.job")


class StopFlag(threading.Event):

    def install(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._handler)

    def _handler(self, signum, frame) -> None:
        self.set()
        luigi_handler = signal.getsignal(signal.SIGUSR1)
        if callable(luigi_handler):  # Luigi's worker is there: no more work, finish what runs
            signal.raise_signal(signal.SIGUSR1)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextlib.contextmanager
def _logging_to(job_dir: Path):
    handler = logging.FileHandler(job_dir / "luigi.log", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(process)d %(name)s %(levelname)s %(message)s")
    )
    root = logging.getLogger()
    before = root.level
    root.addHandler(handler)
    root.setLevel(min(before or logging.INFO, logging.INFO))
    try:
        yield
    finally:
        root.removeHandler(handler)
        root.setLevel(before)
        handler.close()


def _last_words(job_dir: Path, state: str, error: str | None = None, trace: str | None = None):
    record = {"state": state, "error": error, "traceback": (trace or "")[-4000:], "at": _now()}
    (job_dir / "exit.json").write_text(json.dumps(record))


class Reporter(threading.Thread):

    def __init__(self, application, job_id: str, snapshotter: Snapshotter, factory=None):
        super().__init__(daemon=True, name="gracia-job-reporter")
        self.application, self.job_id, self.snapshotter = application, job_id, snapshotter
        self.factory = factory
        self.finished = threading.Event()

    def run(self) -> None:
        with JobsDb(self.application) as db:
            while not self.finished.wait(REPORT_EVERY):
                try:
                    db.update_job(
                        self.job_id,
                        {
                            "progress": self.snapshotter.take(running=_running(self.factory)),
                            "heartbeat_at": NOW,
                        },
                        only_states=("running", "stopping"),
                    )
                except Exception:
                    log.warning("the progress could not be written: %s", traceback.format_exc())

    def stop(self) -> None:
        self.finished.set()
        self.join(timeout=10)


class _Scheduler:

    scheduler = None

    def __new__(cls):
        base = getattr(luigi.interface, "_WorkerSchedulerFactory", None)
        if base is None:
            return None

        class Factory(base):
            def create_local_scheduler(self):
                self.scheduler = luigi.scheduler.Scheduler(
                    prune_on_get_work=True, record_task_history=False
                )
                return self.scheduler

        return Factory()


def _running(factory) -> dict | None:
    scheduler = getattr(factory, "scheduler", None)
    if scheduler is None:
        return None
    try:
        counts: dict[str, int] = {}
        for task in scheduler.task_list("RUNNING", "").values():
            counts[task["name"]] = counts.get(task["name"], 0) + 1
        return counts
    except Exception:
        return None


def _build(roots: list, workers: int, settings: dict, factory=None):
    config = luigi.configuration.get_config()
    config.set("core", "no_configure_logging", "true")  # the logging is ours (luigi.log)
    timeout = settings.get("gracia.jobs_task_timeout")
    if timeout:
        config.set("worker", "timeout", str(int(float(timeout))))
    extra = {"worker_scheduler_factory": factory} if factory is not None else {}
    return luigi.build(
        roots,
        workers=workers,
        local_scheduler=True,
        detailed_summary=True,
        log_level="INFO",
        **extra,
    )


def _collect_rejected(job_dir: Path, stages: list[dict]) -> int:
    total = 0
    with open(job_dir / REJECTED_FILE, "w", encoding="utf-8") as out:
        for index, entry in enumerate(stages):
            directory = job_dir / f"{index:02d}-{entry['stage']}"
            for path in sorted(directory.glob("rejected-*.jsonl")):
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        record = json.loads(line)
                        record["stage"] = entry["stage"]
                        out.write(json.dumps(record, ensure_ascii=False) + "\n")
                        total += 1
    if not total:
        (job_dir / REJECTED_FILE).unlink()
    return total


def run_job(
    application, job_id: str, jobs_dir: Path, settings: dict, stop: StopFlag | None = None
) -> int:
    stop = stop or StopFlag()
    context.use(application, settings)
    Stage.load_modules(settings)
    with JobsDb(application) as db:
        job = db.get(job_id)
        if job is None:
            return EXIT_NOTHING
        job_dir = Path(jobs_dir) / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        with _logging_to(job_dir):
            claimed = db.update_job(
                job_id,
                {
                    "state": "running",
                    "pid": os.getpid(),
                    "pid_started": procs.start_time(os.getpid()),
                    "host": socket.gethostname(),
                    "heartbeat_at": NOW,
                    "code_version": gracia.__version__,
                    "started_at": job["started_at"] or NOW,
                },
                only_states=("queued", "running"),
            )
            if not claimed:
                log.info("the job is %s: nothing to do", job["state"])
                return EXIT_NOTHING
            log.info("job %s: started (attempt %d)", job_id, job["attempt"])
            return _run(db, application, job, job_dir, settings, stop)


def _run(db, application, job, job_dir: Path, settings: dict, stop: StopFlag) -> int:
    job_id, repo, stages = job["id"], job["repo"], job["stages"]
    snapshotter = Snapshotter(job_dir, stages)
    reporter = None
    try:
        plan = Plan(job_dir=str(job_dir), repo=repo, first=JobTask.dumps(stages[0]))
        if not stop.is_set():
            _build([plan], 1, settings)
        if stop.is_set():
            raise KeyboardInterrupt
        planned = snapshotter.plan()
        if planned is None:
            raise RuntimeError(_first_error(snapshotter) or "the plan could not be made")
        batches = planned["batches"]
        workers = context.worker_count(application, settings, batches)
        log.info(
            "job %s: %d batches of %s, %d workers", job_id, batches, planned.get("unit"), workers
        )
        factory = _Scheduler()
        reporter = Reporter(application, job_id, snapshotter, factory)
        reporter.start()
        result = None
        if batches:
            roots = [Stage.make(job_dir, repo, stages, n) for n in range(batches)]
            result = _build(roots, workers, settings, factory)
            (job_dir / "luigi-summary.txt").write_text(result.summary_text, encoding="utf-8")
        reporter.stop()
        if stop.is_set():
            raise KeyboardInterrupt
        ok = result is None or result.status in (
            LuigiStatusCode.SUCCESS,
            LuigiStatusCode.SUCCESS_WITH_RETRY,
        )
        progress = snapshotter.take(active=False)
        complete = all(s["state"] == "done" for s in progress["stages"])
        if not (ok and complete):
            raise RuntimeError(
                _first_error(snapshotter) or f"the build ended as {result.status.name}"
            )
        rejected = _collect_rejected(job_dir, stages)
        values = {"state": "done", "finished_at": NOW, "message": None, "progress": progress}
        if rejected:
            values.update(download_file=REJECTED_FILE, download_content_type="application/x-ndjson")
        else:  # a stage that makes something to take away (the export) offers it
            offered = getattr(Stage.named(stages[-1]["stage"]), "offer", None)
            found = offered(job_dir, stages, len(stages) - 1) if offered else None
            if found:
                values.update(download_file=found[0], download_content_type=found[1])
        (job_dir / "summary.json").write_text(json.dumps(progress, indent=1))
        if db.update_job(job_id, values, only_states=("running",)):
            log.info("job %s: done", job_id)
            _last_words(job_dir, "done")
            return EXIT_DONE
        raise KeyboardInterrupt  # a stop came in as the last batch ended
    except KeyboardInterrupt:
        if reporter is not None:
            reporter.stop()
        log.warning("job %s: stopped", job_id)
        _last_words(job_dir, "stopped")
        db.update_job(
            job_id,
            {
                "state": "stopped",
                "finished_at": NOW,
                "message": "stopped",
                "progress": snapshotter.take(active=False),
            },
            only_states=("running", "stopping"),
        )
        return EXIT_STOPPED
    except BaseException as err:
        if reporter is not None:
            reporter.stop()
        trace = traceback.format_exc()
        message = str(err) if isinstance(err, RuntimeError) else f"{type(err).__name__}: {err}"
        log.error("job %s: failed: %s", job_id, message)
        _last_words(job_dir, "failed", message, trace)
        try:
            db.update_job(
                job_id,
                {
                    "state": "failed",
                    "error_msg": message[:2000],
                    "finished_at": NOW,
                    "message": None,
                    "progress": snapshotter.take(active=False),
                },
                only_states=("running", "stopping", "queued"),
            )
        except Exception:
            log.error("and the job could not be marked failed: %s", traceback.format_exc())
        return EXIT_FAILED


def _first_error(snapshotter: Snapshotter) -> str | None:
    errors = snapshotter.errors()
    return errors[0]["error"] if errors else None
