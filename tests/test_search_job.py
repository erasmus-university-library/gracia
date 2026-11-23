import json
from unittest import mock

import tantivy
from jobs_support import person as jobs_person
from test_graph_jobs import LINKED, GraphJobsCase

from gracia.actions import RecordsChanged, process_pending
from gracia.jobs import context, runner
from gracia.jobs.repository_jobs import RebuildSearchJob
from gracia.search import RECORDS, Indexes, on_records_changed
from gracia.store import fetch_one

PEOPLE = (
    "@prefix gracia: <https://gracia.pub/ns/> . "
    "@prefix foaf: <http://xmlns.com/foaf/0.1/> .\n"
)


class SearchJobCase(GraphJobsCase):
    def setUp(self):
        super().setUp()
        import tempfile

        self.index_dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.index_dir, True)
        self.application.indexes = Indexes(self.index_dir)
        self.indexes = self.application.indexes
        context.use(self.application, self.settings)
        context.registry().registerHandler(on_records_changed, (RecordsChanged,))

    def rebuild(self) -> str:
        job_id = self.make(*RebuildSearchJob().spec("test_repo", {})["stages"])
        self.assertEqual(
            self.run_job(job_id), runner.EXIT_DONE, self.job(job_id)["error_msg"]
        )
        return job_id

    def docs(self) -> int:
        return self.indexes.stats("test_repo")[0]["documents"]

    def hits(self, query: str, fields=("label", "text")) -> list[int]:
        index = tantivy.Index.open(str(self.indexes.path("test_repo", RECORDS)))
        index.reload()
        searcher = index.searcher()
        return sorted(
            searcher.doc(a).to_dict()["record_id"][0]
            for _, a in searcher.search(
                index.parse_query(query, list(fields)), 100
            ).hits
        )

    def records(self) -> int:
        return fetch_one(self.store.con.execute("SELECT count(*) FROM etags"))[0]


class RebuildTest(SearchJobCase):
    def test_an_index_that_is_not_there_is_made_with_every_record(self):
        self.assertEqual(self.indexes.status("test_repo", RECORDS), "missing")
        self.rebuild()
        self.assertEqual(
            (self.indexes.status("test_repo", RECORDS), self.docs()),
            ("ok", self.records()),
        )
        self.assertEqual(
            self.hits("Thirteen"), [self.store.record_id_of("rec:person:13")]
        )

    def test_the_documents_are_the_ones_the_worker_makes(self):
        self.rebuild()
        before = self.hits("Fourteen")
        process_pending(
            self.application, context.registry()
        )  # the worker's way, for the same records
        self.assertEqual(self.hits("Fourteen"), before)
        self.assertEqual(self.docs(), self.records())  # and no record twice

    def test_a_stale_index_is_replaced_and_answers_until_then(self):
        self.rebuild()
        path = self.indexes.path("test_repo", RECORDS)
        meta = self.indexes._meta("test_repo", RECORDS)
        meta.write_text(
            json.dumps({"hash": "of-another-definition"})
        )  # it was made for another one
        self.assertEqual(self.indexes.status("test_repo", RECORDS), "stale")
        seen = {}
        real = Indexes.swap

        def swap(this, repo, definition, token):
            seen["during"] = (
                this.status(repo, definition),
                tantivy.Index.open(str(path)).searcher().num_docs,
            )
            return real(this, repo, definition, token)

        with mock.patch.object(Indexes, "swap", swap):
            self.rebuild()
        self.assertEqual(
            seen["during"], ("stale", self.records())
        )  # the old one was there to the end
        self.assertEqual(
            (self.indexes.status("test_repo", RECORDS), self.docs()),
            ("ok", self.records()),
        )

    def test_nothing_of_the_making_is_left(self):
        self.rebuild()
        left = sorted(p.name for p in (self.indexes.directory / "test_repo").iterdir())
        self.assertEqual(left, ["records", "records.json"])

    def test_an_index_that_missed_records_has_them_after(self):
        self.rebuild()
        self.indexes.reset("test_repo", RECORDS)
        self.indexes.open(
            "test_repo", RECORDS
        )  # there, and empty: it has not seen a record
        self.assertEqual(self.docs(), 0)
        self.rebuild()
        self.assertEqual(self.docs(), self.records())

    def test_without_a_directory_for_the_indexes_the_job_fails_and_says_why(self):
        self.application.indexes = None
        job_id = self.make(*RebuildSearchJob().spec("test_repo", {})["stages"])
        self.assertEqual(self.run_job(job_id), runner.EXIT_FAILED)
        self.assertIn("gracia.index_dir", self.job(job_id)["error_msg"])

    def test_it_is_a_job_of_the_repository_for_the_administrators(self):
        spec = RebuildSearchJob().spec("test_repo", {})
        self.assertEqual(
            [s["stage"] for s in spec["stages"]], ["Export", "IndexRecords"]
        )
        self.assertTrue(spec["stages"][0]["params"]["graphs"])
        self.assertEqual(RebuildSearchJob.groups, ())


class MeanwhileTest(SearchJobCase):
    def test_what_is_written_while_it_is_made_is_in_the_new_index_after(self):
        process_pending(
            self.application, context.registry()
        )  # the queue is empty: what follows is new
        real = Indexes.swap
        written = {}

        def swap(this, repo, definition, token):
            self.put(
                PEOPLE + jobs_person(300).replace("Person 300", "Newcomer")
            )  # after the export, before the swap
            self.put(LINKED.replace("Thirteen", "Renamed"))
            self.store.delete(["rec:person:14"])
            written["queued"] = self.store.pending_actions()[0]
            return real(this, repo, definition, token)

        with mock.patch.object(Indexes, "swap", swap):
            self.rebuild()
        self.assertGreater(
            self.store.pending_actions()[0], written["queued"]
        )  # and more, queued again after the swap
        process_pending(
            self.application, context.registry()
        )  # the worker brings the new index up to date
        self.assertEqual(
            self.hits("Newcomer"), [self.store.record_id_of("rec:person:300")]
        )
        self.assertEqual(len(self.hits("Renamed")), 1)
        self.assertEqual(self.hits("Fourteen"), [])  # deleted
        self.assertEqual(self.docs(), self.records())

    def test_queueing_again_is_what_changed_after_a_transaction(self):
        self.put(
            PEOPLE + jobs_person(400)
        )  # the fixture's etag rebuild moved some pages: start from a write
        bound = self.store.last_transaction()
        self.assertEqual(self.store.requeue_changes_since(bound), 0)
        self.put(
            LINKED.split("<rec:person:14>")[0].replace("Thirteen", "Renamed")
        )  # only 13: 14 is made of it
        self.store.delete(["rec:person:2"])
        self.store.con.execute("DELETE FROM actions_queue")
        count = self.store.requeue_changes_since(bound)
        rows = dict(
            (
                (r, a)
                for r, a in self.store.con.execute(
                    "SELECT record_uri(record_id), action FROM actions_queue"
                ).fetchall()
            )
        )
        self.assertEqual(count, len(rows))
        self.assertEqual(rows["rec:person:13"], "update")
        self.assertEqual(rows["rec:person:2"], "delete")
        self.assertEqual(
            rows["rec:person:14"], "ref_update"
        )  # not written, but its page changed
        self.assertNotIn("rec:person:1", rows)  # nothing to do with it


class FromTheSettingsTest(GraphJobsCase):
    """A job process knows the directory of the indexes from the settings it was started
    with, and from nothing else: the application it makes has none until the
    configuration is made."""

    def test_the_job_finds_the_directory_of_the_indexes_in_the_settings(self):
        import shutil
        import tempfile

        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.settings = {"gracia.index_dir": directory}  # ty: ignore[invalid-attribute-access]
        self.assertIsNone(self.application.indexes)  # nothing has set it
        job_id = self.make(*RebuildSearchJob().spec("test_repo", {})["stages"])
        self.assertEqual(
            self.run_job(job_id), runner.EXIT_DONE, self.job(job_id)["error_msg"]
        )
        indexes = self.application.indexes
        self.assertEqual(indexes.directory.as_posix(), directory)
        self.assertEqual(indexes.stats("test_repo")[0]["state"], "ok")
        self.assertEqual(
            indexes.stats("test_repo")[0]["documents"],
            fetch_one(self.store.con.execute("SELECT count(*) FROM etags"))[0],
        )

    def test_without_the_setting_the_job_says_what_to_set(self):
        self.settings = {}  # ty: ignore[invalid-attribute-access]
        job_id = self.make(*RebuildSearchJob().spec("test_repo", {})["stages"])
        self.assertEqual(self.run_job(job_id), runner.EXIT_FAILED)
        self.assertIn("gracia.index_dir", self.job(job_id)["error_msg"])

    def test_a_worker_process_that_has_not_made_the_configuration_yet_finds_the_indexes(
        self,
    ):
        """The batches of a job run in worker processes, each of which starts without
        the configuration (the plan, which makes it, ran in another): the stage has to
        make it before it asks for the indexes."""
        import shutil
        import tempfile
        from pathlib import Path

        from gracia.jobs.builtin import IndexRecords
        from gracia.jobs.stage import dumps, read_graphs

        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        context.use(self.application, {"gracia.index_dir": directory})
        self.assertIsNone(context._current.config)  # a process that has not made it
        self.assertIsNone(self.application.indexes)
        _, files = self.store.export_chunks(
            Path(directory) / "export", None, 32_000_000, graphs=True
        )
        items = read_graphs(files[0].read_text())
        job_dir = Path(directory) / "job"
        job_dir.mkdir()
        (job_dir / "plan.json").write_text(
            json.dumps({"batches": 1, "bound": self.store.last_transaction()})
        )
        stage = IndexRecords(
            job_dir=str(job_dir),  # ty: ignore[unknown-argument]
            repo="test_repo",  # ty: ignore[unknown-argument]
            n=0,  # ty: ignore[unknown-argument]
            chain=dumps([{"stage": "IndexRecords"}]),  # ty: ignore[unknown-argument]
        )
        passed, rejected = stage.process(items)
        self.assertEqual((len(passed), rejected), (len(items), []))
        self.assertEqual(
            self.application.indexes.stats("test_repo")[0]["documents"],
            len(items),
        )
