import json
import os
from unittest import mock

from jobs_support import JobsCase, echo, quack_owner  # noqa: F401 - shared set-up
from pyramid.exceptions import ConfigurationConflictError

from gracia.jobs import job as jobs_job
from gracia.jobs import runner
from gracia.jobs import stage as stage_module
from gracia.jobs.db import NOW, JobsDb
from gracia.jobs.progress import Snapshotter
from gracia.jobs.spec import JobSpecError, validate_spec
from gracia.jobs.stage import read_items, stage_class, stage_names
from gracia.main import make_config
from gracia.store import fetch_one

CHUNK = 0  # one file: DuckDB splits only after 2048 records, and the tests have twelve
EXPORT = {"stage": "Export", "params": {"res_type": "person", "chunk_bytes": CHUNK}}
VALIDATE = {"stage": "Validate", "params": {"res_type": "person"}}
ECHO = {"stage": "Echo", "params": {"n_items": 6, "size": 2}}


class SpecTest(JobsCase):
    def valid(self, **extra):
        return {"repo": "test_repo", "stages": [ECHO], **extra}

    def errors(self, data) -> dict:
        with self.assertRaises(JobSpecError) as caught:
            validate_spec(data)
        return caught.exception.errors

    def test_a_valid_job_is_normalised(self):
        spec = validate_spec(
            {
                "repo": "test_repo",
                "job": "validate",
                "subject": "person",
                "stages": [EXPORT, VALIDATE],
            }
        )
        self.assertEqual(spec["repo"], "test_repo")
        self.assertEqual((spec["job"], spec["subject"]), ("validate", "person"))
        self.assertEqual([s["stage"] for s in spec["stages"]], ["Export", "Validate"])
        self.assertEqual(
            validate_spec(self.valid())["stages"][0]["params"]["n_items"], 6
        )
        self.assertEqual(
            (
                validate_spec(self.valid())["job"],
                validate_spec(self.valid())["subject"],
            ),
            (None, None),
        )

    def test_the_stages_are_the_ones_luigi_knows_and_the_bases_are_not_among_them(self):
        names = stage_names()
        for name in ("Echo", "Export", "Validate", "Reject", "Keep", "Mark"):
            self.assertIn(name, names)
        for base in ("Stage", "Source", "Plan", "PinSchema"):
            self.assertNotIn(base, names)
        self.assertIs(stage_class("Export").__name__, "Export")
        with self.assertRaises(KeyError):
            stage_class("Source")

    def test_an_unknown_stage_says_what_there_is(self):
        errors = self.errors(self.valid(stages=[{"stage": "Nope"}]))
        self.assertIn("No such stage: 'Nope'", errors["stages.0.stage"])
        self.assertIn("Export", errors["stages.0.stage"])

    def test_a_job_starts_with_a_source_and_has_no_other(self):
        self.assertIn(
            "cannot be the first",
            self.errors(self.valid(stages=[{"stage": "Keep"}]))["stages.0.stage"],
        )
        errors = self.errors(self.valid(stages=[ECHO, ECHO]))
        self.assertIn("must come first", errors["stages.1.stage"])

    def test_the_parameters_are_checked_by_luigi_and_reported_where_they_are(self):
        errors = self.errors(
            self.valid(stages=[ECHO, {"stage": "Reject", "params": {"idz": "x"}}])
        )
        self.assertIn("unknown parameter idz", errors["stages.1.params"])
        errors = self.errors(
            self.valid(stages=[{"stage": "Echo", "params": {"n_items": "many"}}])
        )
        self.assertIn("not a valid value: 'many'", errors["stages.0.params.n_items"])
        errors = self.errors(self.valid(stages=[{"stage": "Echo", "params": {"n": 3}}]))
        self.assertIn(
            "unknown parameter n", errors["stages.0.params"]
        )  # Luigi's own are not ours
        errors = self.errors(self.valid(stages=[ECHO, {"stage": "Validate"}]))
        self.assertIn(
            "res_type", errors["stages.1.params"]
        )  # a parameter that has no default

    def test_the_shape_is_checked_with_colander(self):
        for data in (
            {},
            {"repo": "Bad Repo", "stages": [ECHO]},
            {"repo": "test_repo", "stages": []},
            {"repo": "test_repo"},
        ):
            with self.subTest(data=data):
                self.assertTrue(self.errors(data))

    def test_every_error_is_reported_at_once(self):
        errors = self.errors(
            self.valid(
                stages=[
                    {"stage": "Nope"},
                    {"stage": "Keep"},
                    {"stage": "Reject", "params": {"x": 1}},
                ]
            )
        )
        self.assertEqual(sorted(errors), ["stages.0.stage", "stages.2.params"])


class DbTest(JobsCase):
    def test_a_job_row_has_its_definition_and_no_progress_until_the_process_writes_some(
        self,
    ):
        job_id = self.make(ECHO, job="echo", subject="things")
        row = self.job(job_id)
        self.assertEqual(
            (row["state"], row["job"], row["subject"]), ("queued", "echo", "things")
        )
        self.assertEqual(row["stages"][0]["stage"], "Echo")  # JSON comes back as data
        self.assertIsNone(row["progress"])

    def test_the_progress_is_written_as_json_and_read_back(self):
        job_id = self.make(ECHO)
        with JobsDb(self.application) as db:
            self.assertTrue(
                db.update_job(job_id, {"progress": {"stages": [{"name": "Echo"}]}})
            )
        self.assertEqual(self.job(job_id)["progress"], {"stages": [{"name": "Echo"}]})

    def test_a_change_that_names_the_states_it_may_follow_takes_only_then(self):
        job_id = self.make(ECHO)
        with JobsDb(self.application) as db:
            self.assertTrue(
                db.update_job(job_id, {"state": "running"}, only_states=("queued",))
            )
            self.assertFalse(
                db.update_job(job_id, {"state": "stopped"}, only_states=("queued",))
            )
            self.assertEqual(db.get(job_id)["state"], "running")  # ty: ignore[not-subscriptable]
            with self.assertRaises(ValueError):
                db.update_job(job_id, {"stages": "[]"})  # the definition never changes

    def test_the_list_filters_and_comes_newest_first(self):
        first = self.make(ECHO, job="a", subject="x")
        second = self.make(ECHO, job="b", subject="x")
        third = self.make(ECHO, job="a", subject="y")
        with JobsDb(self.application) as db:
            self.assertEqual([j["id"] for j in db.list_jobs()], [third, second, first])
            self.assertEqual([j["id"] for j in db.list_jobs(job="a")], [third, first])
            self.assertEqual(
                [j["id"] for j in db.list_jobs(job="a,b")], [third, second, first]
            )  # several
            self.assertEqual([j["id"] for j in db.list_jobs(job="b,nothing")], [second])
            self.assertEqual(
                [j["id"] for j in db.list_jobs(subject="x")], [second, first]
            )
            self.assertEqual(
                [j["id"] for j in db.list_jobs(job="a", subject="y")], [third]
            )
            self.assertEqual(db.list_jobs(state="done"), [])
            self.assertEqual(len(db.list_jobs(repo="test_repo")), 3)

    def test_a_job_is_deleted_only_in_a_state_that_allows_it(self):
        job_id = self.make(ECHO)
        with JobsDb(self.application) as db:
            self.assertFalse(db.delete(job_id))  # queued
            db.update_job(job_id, {"state": "done", "finished_at": NOW})
            self.assertTrue(db.delete(job_id))
            self.assertIsNone(db.get(job_id))

    def test_a_table_of_the_earlier_design_is_replaced(self):
        conn = self.application.database.cursor()
        conn.execute("CREATE SCHEMA IF NOT EXISTS _gracia")
        conn.execute("CREATE TABLE _gracia.jobs (id VARCHAR, task_count INTEGER)")
        conn.execute("CREATE TABLE _gracia.job_tasks (job_id VARCHAR)")
        conn.close()
        job_id = self.make(ECHO)  # the first use finds the old shape and starts again
        self.assertEqual(self.job(job_id)["state"], "queued")
        with JobsDb(self.application) as db:
            names = [
                r[0]
                for r in db.conn.execute(
                    "SELECT table_name FROM duckdb_tables() WHERE schema_name = "
                    "'_gracia'"
                ).fetchall()
            ]
        self.assertEqual(names, ["jobs"])


class RegistryTest(JobsCase):
    def test_what_is_scanned_for_jobs(self):
        built_in = [
            "gracia.jobs.validate",
            "gracia.jobs.type_jobs",
            "gracia.jobs.repository_jobs",
        ]
        self.assertEqual(jobs_job.scan_targets({}), built_in)
        self.assertEqual(
            jobs_job.scan_targets({"gracia.jobs": "mylib.jobs other.module"}),
            [*built_in, "mylib.jobs", "other.module"],
        )
        # one inside another is not scanned twice: that would be a conflict
        self.assertEqual(
            jobs_job.scan_targets({"gracia.jobs": "gracia.jobs.validate"}), built_in
        )
        from types import SimpleNamespace

        point = SimpleNamespace(value="installed.jobs:Thing")
        with mock.patch.object(jobs_job, "entry_points", lambda group: [point]):
            self.assertEqual(jobs_job.scan_targets({}), [*built_in, "installed.jobs"])

    def test_the_modules_with_stages_are_the_built_in_ones_the_entry_points_and_the_setting(  # noqa: E501
        self,
    ):
        from types import SimpleNamespace

        self.assertEqual(stage_module.load_modules({}), [stage_module.BUILTIN])
        self.assertEqual(
            stage_module.load_modules({"gracia.stages": "jobs_support jobs_support"}),
            [stage_module.BUILTIN, "jobs_support"],  # once each
        )
        point = SimpleNamespace(value="jobs_support:Echo")
        with mock.patch.object(stage_module, "entry_points", lambda group: [point]):
            self.assertEqual(
                stage_module.load_modules({}), [stage_module.BUILTIN, "jobs_support"]
            )

    def test_two_jobs_with_one_name_are_a_conflict_at_commit(self):
        from gracia.jobs.job import Job

        class One(Job):
            title = "One"

        config = make_config({"gracia.application": self.application})
        config.add_gracia_job("same", One)
        config.add_gracia_job("same", One)
        with self.assertRaises(ConfigurationConflictError):
            config.commit()


class StageTest(JobsCase):
    def test_a_job_runs_its_stages_over_every_batch_and_ends_done(self):
        job_id = self.make(echo(), {"stage": "Mark"})
        self.assertEqual(
            self.run_job(job_id), runner.EXIT_DONE, self.job(job_id)["error_msg"]
        )
        job = self.job(job_id)
        self.assertEqual((job["state"], job["error_msg"]), ("done", None))
        self.assertIsNotNone(job["finished_at"])
        echo_stage, mark = self.stage(job_id, "Echo"), self.stage(job_id, "Mark")
        for stage in (echo_stage, mark):
            self.assertEqual(stage["state"], "done")
            self.assertEqual(stage["batches"], {"done": 3, "running": 0, "total": 3})
            self.assertEqual((stage["processed"], stage["passed"]), (6, 6))
        self.assertEqual((echo_stage["total"], echo_stage["estimated"]), (6, True))
        self.assertEqual(
            (mark["total"], mark["estimated"]), (6, False)
        )  # what Echo passed on
        self.assertEqual(
            [i["id"] for i in self.lines(job_id, "01-Mark/batch-00001.jsonl")],
            ["item-3", "item-4"],
        )

    def test_every_batch_leaves_its_data_its_rejects_and_its_stats(self):
        job_id = self.make(
            echo(), {"stage": "Reject", "params": {"ids": "item-2,item-5"}}
        )
        self.run_job(job_id)
        directory = self.jobs_dir / job_id / "01-Reject"
        self.assertEqual(
            sorted(p.name for p in directory.glob("*-00001*")),
            ["batch-00001.jsonl", "rejected-00001.jsonl", "stats-00001.json"],
        )
        self.assertEqual(
            [i["id"] for i in self.lines(job_id, "01-Reject/batch-00000.jsonl")],
            ["item-1"],
        )
        self.assertEqual(
            self.lines(job_id, "01-Reject/rejected-00000.jsonl"),
            [{"id": "item-2", "reason": "unwanted", "message": "it is not wanted"}],
        )
        stats = json.loads((directory / "stats-00000.json").read_text())
        self.assertEqual(
            (stats["processed"], stats["passed"], stats["rejected"]),
            (2, 1, {"unwanted": 1}),
        )
        self.assertEqual(stats["samples"][0]["id"], "item-2")

    def test_what_is_rejected_stops_there_and_is_counted_by_reason(self):
        job_id = self.make(
            echo(),
            {"stage": "Reject", "params": {"ids": "item-2,item-5"}},
            {"stage": "Mark"},
        )
        self.run_job(job_id)
        reject, mark = self.stage(job_id, "Reject"), self.stage(job_id, "Mark")
        self.assertEqual(
            (reject["processed"], reject["passed"], reject["rejected"]),
            (6, 4, {"unwanted": 2}),
        )
        self.assertEqual((mark["processed"], mark["total"]), (4, 4))  # the funnel
        self.assertEqual([s["id"] for s in reject["samples"]], ["item-2", "item-5"])
        self.assertEqual(
            self.lines(job_id, "02-Mark/batch-00000.jsonl")[0]["id"], "item-1"
        )

    def test_the_rejected_items_of_all_stages_are_one_file_to_download(self):
        job_id = self.make(
            echo(), {"stage": "Reject", "params": {"ids": "item-2,item-5"}}
        )
        self.run_job(job_id)
        job = self.job(job_id)
        self.assertEqual(
            (job["download_file"], job["download_content_type"]),
            ("rejected.jsonl", "application/x-ndjson"),
        )
        found = self.lines(job_id, "rejected.jsonl")
        self.assertEqual(sorted(f["id"] for f in found), ["item-2", "item-5"])
        self.assertEqual({f["stage"] for f in found}, {"Reject"})

    def test_a_job_that_rejects_nothing_has_nothing_to_download(self):
        job_id = self.make(echo(), {"stage": "Keep"})
        self.run_job(job_id)
        job = self.job(job_id)
        self.assertIsNone(job["download_file"])
        self.assertFalse((self.jobs_dir / job_id / "rejected.jsonl").exists())

    def test_a_stage_that_changes_nothing_links_the_file_it_got_instead_of_copying_it(
        self,
    ):
        job_id = self.make(echo(), {"stage": "Keep"}, {"stage": "Mark"})
        self.run_job(job_id)
        d = self.jobs_dir / job_id
        for name in ("batch-00000.jsonl", "batch-00001.jsonl", "batch-00002.jsonl"):
            kept, source = d / "01-Keep" / name, d / "00-Echo" / name
            self.assertTrue(
                os.path.samefile(kept, source), name
            )  # the same file, two names
            self.assertEqual(kept.stat().st_nlink, 2)
        # a stage that changes the data writes a file of its own: the earlier ones stay
        # as they were
        changed = d / "02-Mark" / "batch-00000.jsonl"
        self.assertFalse(os.path.samefile(changed, d / "01-Keep" / "batch-00000.jsonl"))
        self.assertNotIn("marked", (d / "01-Keep" / "batch-00000.jsonl").read_text())
        self.assertIn("marked", changed.read_text())

    def test_a_stage_that_rejects_something_writes_the_items_that_are_left(self):
        job_id = self.make(echo(), {"stage": "Reject", "params": {"ids": "item-1"}})
        self.run_job(job_id)
        d = self.jobs_dir / job_id
        self.assertFalse(
            os.path.samefile(
                d / "01-Reject/batch-00000.jsonl", d / "00-Echo/batch-00000.jsonl"
            )
        )
        # a batch where nothing was rejected is still the link
        self.assertTrue(
            os.path.samefile(
                d / "01-Reject/batch-00001.jsonl", d / "00-Echo/batch-00001.jsonl"
            )
        )

    def test_the_batches_are_the_plan_and_an_empty_plan_is_a_job_that_is_done(self):
        job_id = self.make(echo(n_items=0), {"stage": "Keep"})
        self.assertEqual(self.run_job(job_id), runner.EXIT_DONE)
        self.assertEqual(
            json.loads((self.jobs_dir / job_id / "plan.json").read_text())["batches"], 0
        )
        self.assertEqual(self.stage(job_id, "Keep")["state"], "done")
        self.assertEqual(self.job(job_id)["state"], "done")

    def test_a_job_that_is_done_is_not_run_again(self):
        job_id = self.make(echo())
        self.assertEqual(self.run_job(job_id), runner.EXIT_DONE)
        self.assertEqual(self.run_job(job_id), runner.EXIT_NOTHING)
        self.assertEqual(
            self.run_job("00000000-0000-0000-0000-000000000000"), runner.EXIT_NOTHING
        )


class FailureTest(JobsCase):
    def fail(self, **extra):  # ty: ignore[incompatible-method-override]
        self.marker = self.tmp / "ran.txt"
        self.fail_file = self.tmp / "fail"
        self.fail_file.write_text("")
        return self.make(
            echo(
                fail_batch=1,
                fail_file=str(self.fail_file),
                marker=str(self.marker),
                **extra,
            ),
            {"stage": "Mark"},
        )

    def ran(self):
        return self.marker.read_text().split() if self.marker.exists() else []

    def test_a_stage_that_raises_fails_the_job_with_its_message(self):
        job_id = self.fail()
        self.assertEqual(self.run_job(job_id), runner.EXIT_FAILED)
        job = self.job(job_id)
        self.assertEqual(
            (job["state"], job["error_msg"]), ("failed", "batch 1 was asked to fail")
        )
        self.assertIsNotNone(job["finished_at"])
        self.assertEqual(self.stage(job_id, "Echo")["state"], "failed")
        self.assertEqual(
            self.stage(job_id, "Mark")["state"], "pending"
        )  # it never got its batch
        last = json.loads((self.jobs_dir / job_id / "exit.json").read_text())
        self.assertEqual(
            (last["state"], last["error"]), ("failed", "batch 1 was asked to fail")
        )

    def test_the_other_batches_were_done_and_are_not_done_again(self):
        job_id = self.fail()
        self.run_job(job_id)
        self.assertEqual(sorted(self.ran()), ["0", "1", "2"])
        self.fail_file.unlink()  # the cause is mended
        with JobsDb(self.application) as db:  # `gracia jobs start`
            db.update_job(job_id, {"state": "queued", "error_msg": None})
        self.marker.write_text("")
        self.assertEqual(
            self.run_job(job_id), runner.EXIT_DONE, self.job(job_id)["error_msg"]
        )
        self.assertEqual(self.ran(), ["1"])  # only the batch that failed
        mark = self.stage(job_id, "Mark")
        self.assertEqual((mark["state"], mark["processed"]), ("done", 6))

    def test_an_exception_that_is_not_ours_says_what_it_was(self):
        counter = self.tmp / "tool"
        job_id = self.make(
            echo(),
            {"stage": "Tool", "params": {"failures": 1, "counter": str(counter)}},
        )
        self.assertEqual(self.run_job(job_id), runner.EXIT_FAILED)
        self.assertEqual(self.job(job_id)["error_msg"], "RuntimeError: the tool failed")
        errors = [
            json.loads(line)
            for line in (self.jobs_dir / job_id / "errors.jsonl")
            .read_text()
            .splitlines()
        ]
        self.assertEqual({e["stage"] for e in errors}, {"Tool"})
        self.assertIn("Traceback", errors[0]["traceback"])

    def test_a_plan_that_cannot_be_made_fails_the_job_in_words(self):
        job_id = self.make({"stage": "Export", "params": {"res_type": "nobody"}})
        self.assertEqual(self.run_job(job_id), runner.EXIT_FAILED)
        self.assertIn(
            "no type 'nobody' in repository test_repo", self.job(job_id)["error_msg"]
        )


class StopTest(JobsCase):
    def test_a_stop_before_the_start_ends_the_job_stopped_with_nothing_done(self):
        stop = runner.StopFlag()
        stop.set()
        job_id = self.make(echo())
        self.assertEqual(self.run_job(job_id, stop), runner.EXIT_STOPPED)
        job = self.job(job_id)
        self.assertEqual((job["state"], job["message"]), ("stopped", "stopped"))

    def test_a_stop_while_it_runs_ends_it_stopped_and_it_can_be_run_again(self):
        import jobs_support

        stop = runner.StopFlag()
        jobs_support.STOP = stop  # ty: ignore[invalid-assignment]
        self.addCleanup(setattr, jobs_support, "STOP", None)
        job_id = self.make(echo(stop_at=1), {"stage": "Keep"})
        self.assertEqual(self.run_job(job_id, stop), runner.EXIT_STOPPED)
        self.assertEqual(self.job(job_id)["state"], "stopped")
        jobs_support.STOP = None
        with JobsDb(self.application) as db:
            db.update_job(job_id, {"state": "queued"})
        self.assertEqual(self.run_job(job_id), runner.EXIT_DONE)

    def test_a_sigterm_sets_the_flag_and_asks_luigi_to_stop_taking_work(self):
        import signal

        stop = runner.StopFlag()
        asked = []
        before = signal.signal(
            signal.SIGUSR1, lambda *_: asked.append(True)
        )  # as luigi's worker does
        self.addCleanup(signal.signal, signal.SIGUSR1, before)
        previous = signal.signal(signal.SIGTERM, stop._handler)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        os.kill(os.getpid(), signal.SIGTERM)
        self.assertTrue(stop.is_set())
        self.assertEqual(asked, [True])

    def test_a_sigterm_before_luigi_is_there_only_sets_the_flag(self):
        import signal

        stop = runner.StopFlag()
        before = signal.signal(
            signal.SIGUSR1, signal.SIG_DFL
        )  # a SIGUSR1 now would end the process
        self.addCleanup(signal.signal, signal.SIGUSR1, before)
        previous = signal.signal(signal.SIGTERM, stop._handler)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        os.kill(os.getpid(), signal.SIGTERM)
        self.assertTrue(stop.is_set())


class ProgressTest(JobsCase):
    def files(self, directory, stage, batches, **stats):
        d = directory / stage
        d.mkdir(parents=True, exist_ok=True)
        for n in range(batches):
            (d / f"stats-{n:05d}.json").write_text(
                json.dumps({"processed": 10, "passed": 9, **stats})
            )

    def setUp(self):
        super().setUp()
        self.d = self.tmp / "job"
        self.d.mkdir()
        self.stages = [
            {"stage": "Export", "params": {}},
            {"stage": "Validate", "params": {}},
        ]

    def take(self, **kw):
        return Snapshotter(self.d, self.stages).take(**kw)

    def test_before_the_plan_everything_is_pending(self):
        snapshot = self.take()
        self.assertEqual(
            [s["state"] for s in snapshot["stages"]], ["pending", "pending"]
        )
        self.assertEqual(snapshot["stages"][0]["batches"]["total"], None)

    def test_with_a_plan_the_first_stage_runs_and_the_next_waits_for_its_first_batch(
        self,
    ):
        (self.d / "plan.json").write_text(
            json.dumps({"batches": 4, "estimate": 40, "unit": "records"})
        )
        first, second = self.take()["stages"]
        self.assertEqual((first["state"], second["state"]), ("running", "pending"))
        self.assertEqual(
            (first["total"], first["estimated"], second["total"]), (40, True, None)
        )
        self.files(self.d, "00-Export", 1)
        first, second = self.take()["stages"]
        self.assertEqual(second["state"], "running")  # its first batch can start
        self.assertEqual(first["batches"], {"done": 1, "running": 0, "total": 4})

    def test_a_stage_that_has_all_its_batches_is_done_and_the_next_one_knows_its_total(
        self,
    ):
        (self.d / "plan.json").write_text(json.dumps({"batches": 2, "estimate": 20}))
        self.files(self.d, "00-Export", 2)
        first, second = self.take()["stages"]
        self.assertEqual(
            (first["state"], first["processed"], first["passed"]), ("done", 20, 18)
        )
        self.assertEqual((second["total"], second["estimated"]), (18, False))

    def test_the_counts_add_up_and_the_samples_are_a_few(self):
        (self.d / "plan.json").write_text(json.dumps({"batches": 3, "estimate": 30}))
        self.files(
            self.d,
            "01-Validate",
            3,
            rejected={"violation": 2, "no_content": 1},
            samples=[{"id": "a"}, {"id": "b"}, {"id": "c"}],
        )
        second = self.take()["stages"][1]
        self.assertEqual(second["rejected"], {"violation": 6, "no_content": 3})
        self.assertEqual(len(second["samples"]), 5)

    def test_the_scheduler_says_how_many_run_and_the_stage_that_failed_is_marked(self):
        (self.d / "plan.json").write_text(json.dumps({"batches": 4, "estimate": 40}))
        self.files(self.d, "00-Export", 4)
        second = self.take(running={"Validate": 3})["stages"][1]
        self.assertEqual(
            (second["state"], second["batches"]["running"]), ("running", 3)
        )
        (self.d / "errors.jsonl").write_text(
            json.dumps({"stage": "Validate", "error": "x"}) + "\n"
        )
        self.assertEqual(self.take(active=False)["stages"][1]["state"], "failed")
        self.assertEqual(self.take(active=False)["stages"][0]["state"], "done")

    def test_a_job_that_is_over_has_nothing_running(self):
        (self.d / "plan.json").write_text(json.dumps({"batches": 4, "estimate": 40}))
        self.files(self.d, "00-Export", 2)
        self.assertEqual(self.take(active=False)["stages"][0]["state"], "pending")

    def test_a_stats_file_is_read_once(self):
        (self.d / "plan.json").write_text(json.dumps({"batches": 1, "estimate": 10}))
        self.files(self.d, "00-Export", 1)
        shot = Snapshotter(self.d, self.stages)
        shot.take()
        (self.d / "00-Export" / "stats-00000.json").write_text(
            json.dumps({"processed": 99})
        )
        self.assertEqual(
            shot.take()["stages"][0]["processed"], 10
        )  # a stats file never changes


class ExportValidateTest(JobsCase):
    def validate(self, schema=None, chunk_bytes=CHUNK, **more):
        params = {
            "res_type": "person",
            **({"schema": schema} if schema else {}),
            **more,
        }
        job_id = self.make(
            {
                "stage": "Export",
                "params": {"res_type": "person", "chunk_bytes": chunk_bytes},
            },
            {"stage": "Validate", "params": params},
        )
        return job_id, self.run_job(job_id)

    def test_the_plan_is_the_export_one_batch_for_every_file_it_made(self):
        job_id, code = self.validate(chunk_bytes=20_000_000)
        self.assertEqual(code, runner.EXIT_DONE, self.job(job_id)["error_msg"])
        plan = json.loads((self.jobs_dir / job_id / "plan.json").read_text())
        files = sorted((self.jobs_dir / job_id / "00-Export").glob("batch-*.nt"))
        self.assertEqual(
            (plan["batches"], plan["estimate"], plan["unit"]), (1, 12, "records")
        )
        self.assertEqual(len(files), 1)
        self.assertEqual(plan["chunk_bytes"], 20_000_000)

    def test_every_record_of_the_type_is_checked_against_the_schema_it_is_on(self):
        job_id, code = self.validate()
        self.assertEqual(code, runner.EXIT_DONE, self.job(job_id)["error_msg"])
        exported, validated = (
            self.stage(job_id, "Export"),
            self.stage(job_id, "Validate"),
        )
        self.assertEqual((exported["batches"]["total"], exported["processed"]), (1, 12))
        # 12 records in one file; the record without content and the two without a name
        # stop here
        self.assertEqual((validated["processed"], validated["passed"]), (12, 9))
        self.assertEqual(validated["rejected"], {"violation": 2, "no_content": 1})
        self.assertEqual(exported["passed"], 12)  # the export passes everything on

    def test_violations_are_reported_on_the_record_that_has_them(self):
        job_id, _ = self.validate()
        found = {r["id"]: r for r in self.lines(job_id, "rejected.jsonl")}
        self.assertEqual(
            sorted(found), ["rec:person:12", "rec:person:4", "rec:person:8"]
        )
        self.assertEqual(found["rec:person:12"]["reason"], "no_content")
        violation = found["rec:person:4"]["violations"][0]
        self.assertEqual(found["rec:person:4"]["reason"], "violation")
        self.assertEqual(violation["path"], "http://xmlns.com/foaf/0.1/name")
        self.assertEqual(violation["severity"], "Violation")
        self.assertIn("MinCount", violation["constraint"])
        job = self.job(job_id)
        self.assertEqual(
            (job["download_file"], job["download_content_type"]),
            ("rejected.jsonl", "application/x-ndjson"),
        )
        samples = self.stage(job_id, "Validate")["samples"]
        self.assertIn("MinCount", samples[0]["message"])

    def test_a_candidate_schema_shows_what_it_would_do_before_the_type_switches(self):
        job_id, code = self.validate("Person-strict-01.ttl")
        self.assertEqual(code, runner.EXIT_DONE, self.job(job_id)["error_msg"])
        bad = {
            r["id"].rsplit(":", 1)[1]
            for r in self.lines(job_id, "rejected.jsonl")
            if r["reason"] == "violation"
        }
        # no name: 4 and 8; no mail: 3, 6 and 9; a mail that is none: 11
        self.assertEqual(bad, {"3", "4", "6", "8", "9", "11"})
        self.assertEqual(self.stage(job_id, "Validate")["rejected"]["violation"], 6)
        node = self.application[
            "test_repo"
        ]  # the type itself is still on its own schema
        self.addCleanup(node.close)
        self.assertEqual(node["person"].schema, "Person-full-01.ttl")

    def test_the_job_checks_against_a_copy_of_the_schema_made_when_it_started(self):
        job_id, _ = self.validate("Person-strict-01.ttl")
        d = self.jobs_dir / job_id / "schema" / "person" / "Person-strict-01.ttl"
        from jobs_support import SCHEMAS

        self.assertEqual(
            (d / "schema.ttl").read_text(), SCHEMAS["Person-strict-01.ttl"]
        )
        info = json.loads((d / "info.json").read_text())
        self.assertEqual(
            (info["type"], info["schema"]), ("Person", "Person-strict-01.ttl")
        )
        self.assertTrue(info["checksum"].startswith("md5:"))

    def test_without_a_schema_the_one_the_type_is_on_is_used(self):
        job_id, _ = self.validate()
        info = json.loads(
            (
                self.jobs_dir / job_id / "schema" / "person" / "default" / "info.json"
            ).read_text()
        )
        self.assertEqual(info["schema"], "Person-full-01.ttl")

    def test_a_number_without_a_record_is_just_not_there(self):
        with self.application.new_store("test_repo") as store:
            store.delete([f"rec:person:{n}" for n in range(5, 9)])
        job_id, code = self.validate()
        self.assertEqual(code, runner.EXIT_DONE)
        self.assertEqual(self.stage(job_id, "Export")["processed"], 8)

    def test_a_type_without_records_is_an_empty_job_not_an_error(self):
        with self.application.new_store("test_repo") as store:
            store.delete([f"rec:person:{n}" for n in range(1, 13)])
        job_id, code = self.validate()
        self.assertEqual(code, runner.EXIT_DONE)
        self.assertEqual(self.stage(job_id, "Validate")["processed"], 0)
        self.assertIsNone(self.job(job_id)["download_file"])

    def test_what_cannot_be_validated_fails_the_job_in_words(self):
        (self.tmp / "person" / "shacl" / "Person-odd-01.ttl").write_text(
            "@prefix sh: <http://www.w3.org/ns/shacl#> .\n"
            "<urn:t:Odd> a sh:NodeShape ; sh:targetClass <urn:t:SomethingElse> ."
        )
        for params, message in (
            ({"res_type": "nobody"}, "no type 'nobody'"),
            (
                {"res_type": "person", "schema": "Person-full-09.ttl"},
                "no schema file Person-full-09.ttl",
            ),
            (
                {"res_type": "person", "schema": "Work-full-01.ttl"},
                "not a schema file of Person",
            ),
            (
                {"res_type": "person", "schema": "Person-odd-01.ttl"},
                "cannot use Person-odd-01.ttl",
            ),
        ):
            with self.subTest(params=params):
                job_id = self.make(
                    {"stage": "Export", "params": {"res_type": "person"}},
                    {"stage": "Validate", "params": params},
                )
                self.assertEqual(self.run_job(job_id), runner.EXIT_FAILED)
                self.assertIn(message, self.job(job_id)["error_msg"])
                self.assertEqual(self.job(job_id)["state"], "failed")

    def test_the_export_alone_writes_the_records_with_their_rows(self):
        job_id = self.make(
            {"stage": "Export", "params": {"res_type": "person", "chunk_bytes": CHUNK}}
        )
        self.assertEqual(self.run_job(job_id), runner.EXIT_DONE)
        files = sorted((self.jobs_dir / job_id / "00-Export").glob("batch-*.nt"))
        records = [item for path in files for item in read_items(path)]
        self.assertEqual(len(records), 12)
        self.assertTrue(all(p["source"].startswith("<rec:person:") for p in records))
        self.assertEqual(records[0]["id"], "rec:person:1")


class RewriteExportTest(JobsCase):
    def test_the_rewrite_job_stores_what_the_hooks_change_and_the_next_one_finds_nothing(  # noqa: E501
        self,
    ):
        job_id = self.make(
            {"stage": "Rewrite", "params": {"res_type": "person", "size": 5}}
        )
        self.assertEqual(
            self.run_job(job_id), runner.EXIT_DONE, self.job(job_id)["error_msg"]
        )
        first = self.stage(job_id, "Rewrite")
        self.assertEqual(first["batches"]["total"], 3)  # 12 numbers in batches of 5
        changed = first[
            "passed"
        ]  # the ARK is made for the records that have none, and the label
        self.assertGreater(changed, 0)
        self.assertEqual(
            len(self.lines(job_id, "00-Rewrite/batch-00000.jsonl")) > 0, True
        )
        again = self.make(
            {"stage": "Rewrite", "params": {"res_type": "person", "size": 5}}
        )
        self.assertEqual(self.run_job(again), runner.EXIT_DONE)
        self.assertEqual(
            self.stage(again, "Rewrite")["passed"], 0
        )  # nothing left to change

    def test_the_purge_slugs_job_forgets_old_slugs_of_live_records_and_keeps_the_rest(
        self,
    ):
        with self.application.new_store("test_repo") as store:
            store.con.execute(
                "INSERT INTO slugs VALUES ('person', 'old-name', 'rec:person:1', false)"
            )
            store.con.execute(
                "INSERT INTO slugs VALUES ('person', 'gone', 'rec:person:99', true)"
            )
            store.con.execute(
                "INSERT INTO slugs VALUES ('person', '99', 'rec:person:99', true)"
            )
            store.con.execute(
                "INSERT INTO slugs VALUES ('book', 'other-type', 'rec:book:1', false)"
            )
            before = fetch_one(store.con.execute("SELECT count(*) FROM slugs"))[0]
            current = store.current_slug("rec:person:1")
        job_id = self.make({"stage": "PurgeSlugs", "params": {"res_type": "person"}})
        self.assertEqual(
            self.run_job(job_id), runner.EXIT_DONE, self.job(job_id)["error_msg"]
        )
        self.assertEqual(self.stage(job_id, "PurgeSlugs")["passed"], 1)
        with self.application.new_store("test_repo") as store:
            slugs = {
                r[0] for r in store.con.execute("SELECT slug FROM slugs").fetchall()
            }
            self.assertNotIn("old-name", slugs)
            self.assertLessEqual(
                {"gone", "99", "other-type"}, slugs
            )  # deleted ones and other types
            self.assertEqual(store.current_slug("rec:person:1"), current)
            self.assertEqual(
                fetch_one(store.con.execute("SELECT count(*) FROM slugs"))[0],
                before - 1,
            )
            self.assertEqual(store.purge_slugs("person"), [])  # nothing left to forget

    def test_the_rebuild_etags_job_makes_them_again_and_lists_the_records_whose_etag_was_wrong(  # noqa: E501
        self,
    ):
        first = self.make({"stage": "RebuildEtags"})
        self.assertEqual(
            self.run_job(first), runner.EXIT_DONE, self.job(first)["error_msg"]
        )
        self.assertEqual(self.stage(first, "RebuildEtags")["batches"]["total"], 1)
        again = self.make({"stage": "RebuildEtags"})
        self.assertEqual(self.run_job(again), runner.EXIT_DONE)
        self.assertEqual(
            self.stage(again, "RebuildEtags")["passed"], 0
        )  # kept up to date: all right
        with self.application.new_store("test_repo") as store:
            self.assertGreater(
                store.count_etags(), 12
            )  # the people, and the records of the types
            store.con.execute(
                "UPDATE etags SET etag = 12345 WHERE record_uri = 'rec:person:1'"
            )
            store.con.execute("DELETE FROM etags WHERE record_uri = 'rec:person:2'")
        fixed = self.make({"stage": "RebuildEtags"})
        self.assertEqual(self.run_job(fixed), runner.EXIT_DONE)
        self.assertEqual(self.stage(fixed, "RebuildEtags")["passed"], 2)
        self.assertEqual(
            [i["id"] for i in self.lines(fixed, "00-RebuildEtags/batch-00000.jsonl")],
            ["rec:person:1", "rec:person:2"],
        )
        with self.application.new_store("test_repo") as store:
            self.assertRegex(store.etag_info("rec:person:1")[0], r"^[0-9a-f]{16}$")  # ty: ignore[not-subscriptable]

    def test_the_export_job_offers_the_records_as_one_file_to_download(self):
        job_id = self.make({"stage": "Export", "params": {"res_type": "person"}})
        self.assertEqual(
            self.run_job(job_id), runner.EXIT_DONE, self.job(job_id)["error_msg"]
        )
        job = self.job(job_id)
        self.assertEqual(
            (job["download_file"], job["download_content_type"]),
            ("export.nt", "application/n-triples"),
        )
        text = (self.jobs_dir / job_id / "export.nt").read_text()
        subjects = {
            line.split(" ", 1)[0]
            for line in text.splitlines()
            if line.startswith("<rec:")
        }
        self.assertEqual(subjects, {f"<rec:person:{n}>" for n in range(1, 13)})


class WriteConflictTest(JobsCase):
    def failing(self, db, times):
        import duckdb

        real = db.conn
        calls = {"failed": 0}

        class Conflicting:
            def __getattr__(self, name):
                return getattr(real, name)

            def execute(self, sql, params=None):
                if (
                    sql.lstrip().startswith(("UPDATE", "DELETE"))
                    and calls["failed"] < times
                ):
                    calls["failed"] += 1
                    raise duckdb.TransactionException("Conflict on tuple deletion!")
                return real.execute(sql, params)

        db._conn = Conflicting()
        return calls

    def test_a_change_that_meets_a_conflict_is_run_again(self):
        from gracia.jobs import db as jobs_db

        job_id = self.make(ECHO)
        with JobsDb(self.application) as db:
            calls = self.failing(db, 3)
            with mock.patch.object(jobs_db.time, "sleep"):
                changed = db.update_job(
                    job_id, {"state": "stopped"}, only_states=("queued",)
                )
                db.update_job(job_id, {"message": "by hand"})
            self.assertTrue(changed)
            self.assertEqual(calls["failed"], 3)
        self.assertEqual(self.job(job_id)["state"], "stopped")
        self.assertEqual(self.job(job_id)["message"], "by hand")

    def test_the_condition_is_judged_again_on_the_row_as_it_is_then(self):
        from gracia.jobs import db as jobs_db

        job_id = self.make(ECHO)
        with JobsDb(self.application) as db:
            self.failing(db, 1)
            original = db._conn.execute  # ty: ignore[missing-union-attribute]

            def finishes_first(sql, params=None):
                try:
                    return original(sql, params)
                finally:
                    with JobsDb(self.application) as other:
                        other.update_job(job_id, {"state": "done"})

            db._conn.execute = finishes_first  # ty: ignore[invalid-assignment]
            with mock.patch.object(jobs_db.time, "sleep"):
                self.assertFalse(
                    db.update_job(job_id, {"state": "stopped"}, only_states=("queued",))
                )
        self.assertEqual(self.job(job_id)["state"], "done")

    def test_a_conflict_that_lasts_is_raised(self):
        import duckdb

        from gracia.jobs import db as jobs_db

        job_id = self.make(ECHO)
        with JobsDb(self.application) as db:
            calls = self.failing(db, 10**6)
            with (
                mock.patch.object(jobs_db.time, "sleep"),
                self.assertRaises(duckdb.TransactionException),
            ):
                db.update_job(job_id, {"state": "stopped"})
            self.assertEqual(calls["failed"], jobs_db.WRITE_ATTEMPTS)
