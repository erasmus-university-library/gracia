import contextlib
import io
import os
import signal
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest
from jobs_support import JobsCase, echo, quack_owner

from gracia import cli
from gracia.jobs import cleanup, parse_duration, procs, retention, runner
from gracia.jobs.db import NOW, JobsDb

ECHO = echo()

TESTS = str(Path(__file__).resolve().parent)


def naive_utc(delta: timedelta = timedelta()) -> datetime:
    return (datetime.now(UTC) + delta).replace(tzinfo=None)


pytestmark = pytest.mark.processes


class ProcessCase(JobsCase):
    def make_application(self):
        application, self.uri, self.token = quack_owner(self)
        return application

    def setUp(self):
        super().setUp()
        self.ini = self.tmp / "config.ini"
        self.ini.write_text(
            "[app:main]\n"
            f"gracia.db = {self.uri}\n"
            f"gracia.db.token = {self.token}\n"
            f"gracia.jobs_dir = {self.jobs_dir}\n"
            "gracia.stages = jobs_support\n"
            "gracia.jobs_workers = 3\n"
        )
        # a run in this process forks workers too: they reach the database through the
        # owner
        self.settings = {  # ty: ignore[invalid-attribute-access]
            "gracia.db": self.uri,
            "gracia.db.token": self.token,
            "gracia.stages": "jobs_support",
            "gracia.jobs_workers": "3",
        }
        self.hold = (
            self.tmp / "hold"
        )  # a batch that is given this file waits until it is gone
        self.marker = self.tmp / "started"
        patcher = mock.patch.dict(os.environ, {"PYTHONPATH": TESTS})
        patcher.start()
        self.addCleanup(patcher.stop)
        procs.SPAWN_SETTLE, settle = 0.0, procs.SPAWN_SETTLE
        self.addCleanup(setattr, procs, "SPAWN_SETTLE", settle)
        self.addCleanup(self.kill_everything)

    def kill_everything(self):
        with JobsDb(self.application) as db:
            for job in db.list_jobs():
                # the group, whether or not its leader is still there (never the one
                # pytest is in:
                # a test may record pytest's own pid, which leads a group when run from
                # a shell)
                if job["pid"] and job["pid"] != os.getpid():
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(job["pid"], signal.SIGKILL)
        procs.reap_children()

    def held(self, **params) -> dict:
        self.hold.write_text("")
        return echo(hold=str(self.hold), marker=str(self.marker), **params)

    def started(self) -> list[str]:
        return self.marker.read_text().split() if self.marker.exists() else []

    def wait_started(self, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.started():
                return
            time.sleep(0.05)
        raise AssertionError("no batch has started")

    def wait_for(self, job_id: str, states, timeout: float = 40.0, until=None) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.job(job_id)
            if job["state"] in states and (until is None or until(job)):
                return job
            time.sleep(0.1)
        raise AssertionError(f"the job is {self.job(job_id)['state']}, not {states}")

    def wait_gone(self, pid: int, started, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            procs.reap_children()
            if not procs.is_alive(pid, started):
                return
            time.sleep(0.05)
        raise AssertionError(f"process {pid} is still there")

    def start(self, job_id: str) -> int:
        with JobsDb(self.application) as db:
            return procs.start_job(db, job_id, self.jobs_dir, config=str(self.ini))

    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["-c", str(self.ini), *args])
        return code, out.getvalue(), err.getvalue()


class SpawnTest(ProcessCase):
    def test_a_job_runs_to_its_end_in_a_process_of_its_own(self):
        job_id = self.make(echo(n_items=6, size=2), {"stage": "Mark"})
        pid = self.start(job_id)
        self.assertNotEqual(pid, os.getpid())
        job = self.wait_for(job_id, ("done", "failed"))
        self.assertEqual(job["state"], "done", job["error_msg"])
        self.assertEqual(job["pid"], pid)
        self.assertEqual(len(self.lines(job_id, "01-Mark/batch-00001.jsonl")), 2)
        self.assertEqual(self.stage(job_id, "Mark")["processed"], 6)
        self.wait_gone(
            pid, job["pid_started"]
        )  # it ends just after it has said it is done

    def test_the_batches_run_in_worker_processes_of_the_job(self):
        job_id = self.make(echo(n_items=8, size=1, marker=str(self.marker), sleep=0.2))
        self.start(job_id)
        self.wait_for(job_id, ("done", "failed"))
        logged = (self.jobs_dir / job_id / "luigi.log").read_text()
        pids = {line.split()[2] for line in logged.splitlines() if line[:2] == "20"}
        self.assertGreater(
            len(pids), 1
        )  # the job process and its workers write one log
        self.assertEqual(sorted(self.started()), [str(n) for n in range(8)])

    def test_a_started_job_leads_its_own_session_reports_progress_and_cannot_be_started_twice(  # noqa: E501
        self,
    ):
        job_id = self.make(self.held(n_items=4, size=1))
        pid = self.start(job_id)
        self.wait_started()
        self.assertEqual(os.getpgid(pid), pid)  # it leads its own group
        self.assertNotEqual(os.getsid(pid), os.getsid(0))
        with self.assertRaises(procs.JobStateError):
            self.start(job_id)  # it is running
        job = self.wait_for(
            job_id, ("running",), until=lambda j: j["progress"] is not None
        )
        stage = job["progress"]["stages"][0]
        self.assertEqual(
            (stage["name"], stage["state"], stage["batches"]["total"]),
            ("Echo", "running", 4),
        )
        self.assertGreater(
            stage["batches"]["running"], 0
        )  # the scheduler says what runs now
        self.assertIsNotNone(job["heartbeat_at"])
        self.hold.unlink()
        self.assertEqual(
            self.wait_for(job_id, ("done",))["progress"]["stages"][0]["state"], "done"
        )
        with self.assertRaises(procs.JobStateError):
            self.start(job_id)  # done

    def test_a_job_that_fails_in_its_process_says_why(self):
        job_id = self.make(echo(fail_batch=0))
        self.start(job_id)
        job = self.wait_for(job_id, ("failed",))
        self.assertEqual(job["error_msg"], "batch 0 was asked to fail")

    def test_a_failed_job_can_be_started_again_and_does_what_is_left(self):
        fail = self.tmp / "fail"
        fail.write_text("")
        job_id = self.make(echo(n_items=4, size=2, fail_batch=1, fail_file=str(fail)))
        self.assertEqual(self.run_job(job_id), runner.EXIT_FAILED)
        self.assertEqual(self.job(job_id)["attempt"], 0)
        fail.unlink()  # the cause is mended
        self.start(job_id)
        job = self.wait_for(job_id, ("done", "failed"))
        self.assertEqual((job["state"], job["attempt"]), ("done", 1))


class StopTest(ProcessCase):
    def running(self, **params) -> str:
        job_id = self.make(self.held(**params))
        self.start(job_id)
        self.wait_started()
        return job_id

    def stop(self, job_id, **kw) -> str:
        with JobsDb(self.application) as db:
            return procs.stop_job(db, job_id, **kw)

    def test_a_job_that_listens_stops_taking_work_and_ends_when_what_runs_is_done(self):
        job_id = self.make(echo(n_items=40, size=1, sleep=0.3, marker=str(self.marker)))
        self.start(job_id)
        self.wait_started()
        started = time.monotonic()
        self.assertEqual(self.stop(job_id, grace=20), "stopped")
        self.assertLess(
            time.monotonic() - started, 10
        )  # the grace period was not used up
        job = self.job(job_id)
        self.assertEqual(job["state"], "stopped")
        self.assertEqual(
            json_state(self.jobs_dir / job_id), "stopped"
        )  # it said so itself
        procs.reap_children()
        self.assertFalse(procs.is_alive(job["pid"], job["pid_started"]))
        self.assertLess(len(self.started()), 40)  # it did not go on to the end

    def test_a_job_that_does_not_stop_in_time_is_killed_with_the_workers_it_forked(
        self,
    ):
        job_id = self.running(n_items=3, size=1)  # batches that never end
        pid = self.job(job_id)["pid"]
        self.assertEqual(os.getpgid(pid), pid)
        started = time.monotonic()
        self.assertEqual(self.stop(job_id, grace=1), "stopped")
        self.assertLess(time.monotonic() - started, 15)
        job = self.job(job_id)
        self.assertEqual(job["state"], "stopped")
        deadline = time.monotonic() + 10
        while (
            time.monotonic() < deadline
        ):  # a killed process is a zombie until it is reaped
            procs.reap_children()
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                self.assertFalse(procs.is_alive(pid, job["pid_started"]))
                return  # no process is left in the group
            time.sleep(0.05)
        self.fail("a process of the job's group is still there")

    def test_stopping_what_is_not_running(self):
        queued = self.make(ECHO)
        self.assertEqual(
            self.stop(queued), "stopped"
        )  # a queued job is just set to stopped
        self.assertEqual(self.job(queued)["state"], "stopped")
        self.assertEqual(self.stop(queued), "already")
        done = self.make(ECHO)
        self.run_job(done)
        self.assertEqual(self.stop(done), "already")
        self.assertEqual(self.job(done)["state"], "done")
        with self.assertRaises(KeyError):
            self.stop("00000000-0000-0000-0000-000000000000")

    def test_a_stopped_job_can_be_started_again(self):
        job_id = self.running(n_items=8, size=1)
        started = time.monotonic()
        self.assertEqual(self.stop(job_id, grace=60, now=True), "stopped")
        self.assertLess(time.monotonic() - started, 10)  # now: no grace period
        self.hold.unlink()
        self.start(job_id)
        job = self.wait_for(job_id, ("done", "failed"))
        self.assertEqual((job["state"], job["attempt"]), ("done", 1))
        self.assertEqual(self.stage(job_id, "Echo")["processed"], 8)

    def test_a_pid_that_is_not_the_job_is_never_signalled(self):
        job_id = self.make(ECHO)
        with JobsDb(self.application) as db:
            db.update_job(
                job_id,
                {
                    "state": "running",
                    "pid": os.getpid(),
                    "pid_started": 1,
                    "host": os.uname().nodename,
                },
            )
            self.assertEqual(procs.stop_job(db, job_id, grace=0.2), "stopped")
        self.assertTrue(procs.is_alive(os.getpid()))  # this very process is still here

    def test_a_job_of_another_host_is_not_stopped_from_here(self):
        job_id = self.make(ECHO)
        with JobsDb(self.application) as db:
            db.update_job(
                job_id,
                {"state": "running", "pid": 123, "pid_started": 1, "host": "elsewhere"},
            )
            with self.assertRaises(RuntimeError):
                procs.stop_job(db, job_id)
            self.assertEqual(
                db.get(job_id)["state"],  # ty: ignore[not-subscriptable]
                "stopping",
            )  # honest: it was being stopped


def json_state(job_dir: Path) -> str | None:
    record = procs.exit_record(job_dir)
    return record["state"] if record else None


class ReconcileTest(ProcessCase):
    def test_a_process_killed_from_outside_is_found_out_on_the_next_look(self):
        job_id = self.make(self.held(n_items=2, size=1))
        pid = self.start(job_id)
        self.wait_started()
        os.kill(pid, signal.SIGKILL)  # as the out-of-memory killer would
        time.sleep(0.3)
        with JobsDb(self.application) as db:
            self.assertEqual(procs.reconcile(db, self.jobs_dir), [job_id])
            job = db.get(job_id)
        self.assertEqual(job["state"], "failed")  # ty: ignore[not-subscriptable]
        self.assertEqual(job["error_msg"], "the process disappeared")  # ty: ignore[not-subscriptable]
        self.assertIsNotNone(job["finished_at"])  # ty: ignore[not-subscriptable]
        # its workers are still there, holding: end them, and it can be started again
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
        self.hold.unlink()
        self.start(job_id)
        self.assertEqual(self.wait_for(job_id, ("done", "failed"))["state"], "done")

    def test_the_last_words_of_a_process_are_used_when_there_are_some(self):
        job_id = self.make(ECHO)
        (self.jobs_dir / job_id).mkdir(parents=True)
        (self.jobs_dir / job_id / "exit.json").write_text(
            '{"state": "failed", "error": "it ran out of room"}'
        )
        with JobsDb(self.application) as db:
            db.update_job(
                job_id,
                {
                    "state": "running",
                    "pid": 2**22 - 3,
                    "pid_started": 5,
                    "host": os.uname().nodename,
                    "started_at": NOW,
                },
            )
            procs.reconcile(db, self.jobs_dir)
            self.assertEqual(db.get(job_id)["error_msg"], "it ran out of room")  # ty: ignore[not-subscriptable]

    def test_a_live_job_a_young_job_and_a_job_of_another_host_are_left_alone(self):
        live = self.make(self.held(n_items=1, size=1))
        self.start(live)
        self.wait_started()
        young, elsewhere = self.make(ECHO), self.make(ECHO)
        with JobsDb(self.application) as db:
            db.update_job(
                young,
                {
                    "state": "running",
                    "pid": 2**22 - 3,
                    "pid_started": 5,
                    "host": os.uname().nodename,
                    "started_at": NOW,
                },
            )
            db.update_job(
                elsewhere,
                {
                    "state": "running",
                    "pid": 2**22 - 3,
                    "pid_started": 5,
                    "host": "elsewhere",
                    "started_at": naive_utc(timedelta(hours=-1)),
                },
            )
            with mock.patch.object(procs, "SPAWN_SETTLE", 60.0):
                self.assertEqual(
                    procs.reconcile(db, self.jobs_dir), []
                )  # young: not judged yet
            self.assertEqual(
                procs.reconcile(db, self.jobs_dir), [young]
            )  # now it is old enough
            self.assertEqual(db.get(live)["state"], "running")  # ty: ignore[not-subscriptable]
            self.assertEqual(
                db.get(elsewhere)["state"],  # ty: ignore[not-subscriptable]
                "running",
            )  # a pid of another host means nothing

    def test_a_process_that_has_ended_but_is_not_yet_collected_counts_as_gone(self):
        job_id = self.make(self.held(n_items=1, size=1))
        pid = self.start(job_id)
        self.wait_started()
        started = procs.start_time(pid)
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.3)
        self.assertFalse(
            procs.is_alive(pid, started)
        )  # a zombie, until it is collected
        procs.reap_children()


class CleanTest(ProcessCase):
    def finished(self, hours_ago: float, state: str = "done") -> str:
        job_id = self.make(ECHO)
        (self.jobs_dir / job_id).mkdir(parents=True)
        (self.jobs_dir / job_id / "job.log").write_text("x" * 2048)
        with JobsDb(self.application) as db:
            db.update_job(
                job_id,
                {"state": state, "finished_at": naive_utc(timedelta(hours=-hours_ago))},
            )
        return job_id

    def clean(self, **kw):
        with JobsDb(self.application) as db:
            return cleanup.clean(
                db, self.jobs_dir, parse_duration(kw.pop("older_than", "8h")), **kw
            )

    def test_old_finished_jobs_go_with_their_directories_and_the_rest_stays(self):
        old, older, recent = (
            self.finished(9),
            self.finished(30, "failed"),
            self.finished(2),
        )
        running = self.make(ECHO)
        (self.jobs_dir / running).mkdir(parents=True)
        report = self.clean()
        self.assertEqual(sorted(report.jobs), sorted([old, older]))
        self.assertGreaterEqual(report.bytes, 4096)
        for gone in (old, older):
            self.assertIsNone(self.job(gone))
            self.assertFalse((self.jobs_dir / gone).exists())
        for kept in (recent, running):
            self.assertIsNotNone(self.job(kept))
            self.assertTrue((self.jobs_dir / kept).exists())

    def test_a_running_job_is_never_cleaned_however_old(self):
        job_id = self.make(ECHO)
        with JobsDb(self.application) as db:
            db.update_job(
                job_id,
                {"state": "running", "finished_at": naive_utc(timedelta(days=9))},
            )
        self.assertEqual(self.clean().jobs, [])

    def test_a_dry_run_only_reports(self):
        old = self.finished(9)
        report = self.clean(dry_run=True)
        self.assertEqual((report.jobs, report.dry_run), ([old], True))
        self.assertIsNotNone(self.job(old))
        self.assertTrue((self.jobs_dir / old).exists())

    def test_running_it_twice_is_harmless(self):
        self.finished(9)
        self.assertEqual(len(self.clean().jobs), 1)
        self.assertEqual(self.clean().jobs, [])

    def test_a_directory_left_without_a_row_goes_after_the_same_period(self):
        orphan, young = str(uuid.uuid4()), str(uuid.uuid4())
        for name in (orphan, young):
            (self.jobs_dir / name).mkdir(parents=True)
            (self.jobs_dir / name / "x").write_text("x")
        old = time.time() - 20 * 3600
        os.utime(self.jobs_dir / orphan, (old, old))
        report = self.clean()
        self.assertEqual(report.orphans, [orphan])
        self.assertFalse((self.jobs_dir / orphan).exists())
        self.assertTrue((self.jobs_dir / young).exists())

    def test_only_a_uuid_directory_inside_the_jobs_directory_is_ever_removed(self):
        outside = self.tmp / "precious"
        outside.mkdir()
        (outside / "file").write_text("keep me")
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        link = self.jobs_dir / str(uuid.uuid4())
        link.symlink_to(outside, target_is_directory=True)
        (self.jobs_dir / "not-a-uuid").mkdir()
        self.assertIsNone(cleanup.safe_job_dir(self.jobs_dir, link.name))  # a symlink
        self.assertIsNone(cleanup.safe_job_dir(self.jobs_dir, "not-a-uuid"))
        self.assertIsNone(cleanup.safe_job_dir(self.jobs_dir, "../precious"))
        self.assertIsNone(
            cleanup.safe_job_dir(self.jobs_dir, str(uuid.uuid4()).upper())
        )  # not canonical
        old = time.time() - 20 * 3600
        os.utime(self.jobs_dir / "not-a-uuid", (old, old))
        self.clean()
        self.assertTrue((outside / "file").exists())
        self.assertTrue((self.jobs_dir / "not-a-uuid").exists())

    def test_the_retention_comes_from_the_setting_and_the_flag(self):
        self.assertEqual(retention({}), 8 * 3600)
        self.assertEqual(retention({"gracia.jobs_retention": "2d"}), 2 * 86400)
        self.assertEqual(retention({"gracia.jobs_retention": "2d"}, "30m"), 1800)
        for bad in ("soon", "8 hours", "-1h", ""):
            with self.assertRaises(ValueError):
                parse_duration(bad)


class CommandsTest(ProcessCase):
    def test_validate_makes_a_job_and_runs_it(self):
        code, out, err = self.cli(
            "jobs", "validate", "--repo", "test_repo", "--type", "person",
            "--schema", "person-strict-01.ttl", "--batch-size", "5",
        )  # fmt: skip
        self.assertEqual(code, 0, err)
        job_id = out.strip().splitlines()[0]
        self.assertEqual(self.wait_for(job_id, ("done", "failed"))["state"], "done")
        found = self.lines(job_id, "rejected.jsonl")
        self.assertEqual(len(found), 7)  # six violations and the record without content
        code, out, _ = self.cli("jobs", "show", job_id)
        self.assertEqual(code, 0)
        self.assertIn("Export", out)
        self.assertIn("Validate", out)
        self.assertIn("6 violation", out)
        self.assertIn("rejected.jsonl", out)
        code, out, _ = self.cli("jobs", "log", job_id)
        self.assertEqual(code, 0)
        self.assertIn("luigi", out)  # the logging of the process and its workers
        self.assertEqual(self.cli("jobs", "log", job_id, "--raw")[0], 0)

    def test_no_start_only_makes_the_job_and_start_runs_it(self):
        code, out, _ = self.cli(
            "jobs", "validate", "--repo", "test_repo", "--type", "person", "--no-start"
        )
        job_id = out.strip().splitlines()[0]
        self.assertEqual(self.job(job_id)["state"], "queued")
        code, out, err = self.cli("jobs", "start", job_id)
        self.assertEqual(code, 0, err)
        self.assertIn("started (pid", out)
        self.assertEqual(self.wait_for(job_id, ("done", "failed"))["state"], "done")
        code, _, err = self.cli("jobs", "start", job_id)  # done: not startable
        self.assertEqual(code, 1)
        self.assertIn("cannot start", err)

    def test_a_job_that_is_not_valid_is_refused_with_every_reason(self):
        spec = self.tmp / "bad.json"
        spec.write_text(
            '{"repo": "test_repo", "stages": [{"stage": "Nope"},'
            ' {"stage": "Keep", "params": {"x": 1}}]}'
        )
        code, out, err = self.cli("jobs", "create", str(spec))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("stages.0.stage", err)
        self.assertIn("stages.1.params", err)
        self.assertEqual(
            self.cli("jobs", "list", "--json")[1].strip(), "[]"
        )  # nothing was made

    def test_a_repository_that_does_not_exist_is_refused(self):
        code, _, err = self.cli(
            "jobs", "validate", "--repo", "nowhere", "--type", "person"
        )
        self.assertEqual(code, 1)
        self.assertIn("no such repository: nowhere", err)

    def test_create_takes_a_job_from_stdin(self):
        with mock.patch(
            "sys.stdin",
            io.StringIO('{"repo": "test_repo", "stages": [{"stage": "Echo"}]}'),
        ):
            code, out, err = self.cli("jobs", "create", "-", "--no-start")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.job(out.strip())["state"], "queued")
        with mock.patch("sys.stdin", io.StringIO("not json")):
            self.assertEqual(self.cli("jobs", "create", "-")[0], 1)

    def test_list_shows_the_jobs_with_their_progress_and_when_a_download_expires(self):
        job_id = self.make(echo(n_items=4, size=2))
        self.run_job(job_id)
        other = self.make(ECHO)
        code, out, _ = self.cli("jobs", "list")
        self.assertEqual(code, 0)
        self.assertIn(job_id, out)
        self.assertIn("done", out)
        self.assertIn("2/2", out)  # batches
        self.assertIn(other, out)
        code, out, _ = self.cli("jobs", "list", "--state", "queued")
        self.assertNotIn(job_id, out)
        self.assertIn(other, out)
        import json

        rows = json.loads(self.cli("jobs", "list", "--json", "--repo", "test_repo")[1])
        done = next(r for r in rows if r["id"] == job_id)
        self.assertEqual(done["progress"]["stages"][0]["processed"], 4)
        self.assertIsNotNone(done["expires_at"])  # finished plus the retention
        self.assertIsNone(next(r for r in rows if r["id"] == other)["expires_at"])

    def test_stop_and_clean_from_the_command_line(self):
        job_id = self.make(self.held(n_items=3, size=1))
        self.start(job_id)
        self.wait_started()
        code, out, err = self.cli("jobs", "stop", job_id, "--grace", "1")
        self.assertEqual((code, out.strip()), (0, "stopped"), err)
        self.assertEqual(self.job(job_id)["state"], "stopped")
        self.assertIn("already finished", self.cli("jobs", "stop", job_id)[1])
        self.assertEqual(
            self.cli("jobs", "clean")[1].split()[1:3], ["0", "jobs"]
        )  # just finished: kept
        with JobsDb(self.application) as db:
            db.update_job(job_id, {"finished_at": naive_utc(timedelta(hours=-9))})
        code, out, _ = self.cli("jobs", "clean", "--dry-run")
        self.assertIn("would remove 1 jobs", out)
        code, out, _ = self.cli("jobs", "clean")
        self.assertIn("removed 1 jobs", out)
        self.assertIsNone(self.job(job_id))
        self.assertEqual(self.cli("jobs", "stop", "nope")[0], 1)
        self.assertEqual(self.cli("jobs", "show", "nope")[0], 1)
