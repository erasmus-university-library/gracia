import shutil
import tempfile

import pyoxigraph as ox
from jobs_support import JobsCase, person

from gracia.actions import RecordsChanged, process_pending
from gracia.application import Application
from gracia.jobs import context, runner
from gracia.jobs.repository_jobs import ProcessActionsJob, UpdateCacheJob
from gracia.jobs.stage import read_graphs
from gracia.jobs.type_jobs import ExportJob
from gracia.store import fetch_one

LINKED = """
@prefix gracia: <https://gracia.pub/ns/> . @prefix foaf: <http://xmlns.com/foaf/0.1/> .
<rec:person:13> a gracia:Record ; gracia:resource_uri <urn:p:13> ; gracia:resource [ a
  foaf:Person ; foaf:name "Thirteen" ] .
<rec:person:14> a gracia:Record ; gracia:resource [ a foaf:Person ; foaf:name "Fourteen"
  ; foaf:knows <urn:p:13> ] .
"""


class GraphJobsCase(JobsCase):
    """The people of `JobsCase`, and two that know each other: person 14 is made of
    person 13."""

    def make_application(self) -> Application:
        self.cache_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cache_dir, True)
        return Application(":memory:", cache_dir=self.cache_dir)

    def setUp(self):
        super().setUp()
        self.put(LINKED)
        with self.application.open_repository("library") as store:
            store.rebuild_etags()
        self.store = self.application.open_repository("library")
        self.addCleanup(self.store.close)

    def graphs(self, export: dict | None = None, update: bool = True) -> str:
        stages = [{"stage": "Export", "params": {"graphs": True, **(export or {})}}]
        if update:
            stages.append({"stage": "UpdateCache"})
        job_id = self.make(*stages)
        self.assertEqual(
            self.run_job(job_id), runner.EXIT_DONE, self.job(job_id)["error_msg"]
        )
        return job_id

    def kept(self, rec_uri: str) -> bytes | None:
        """What the graph cache has for a record, at the etag it has now."""
        record_id = self.store.record_id_of(rec_uri)
        etag = self.application.cache.etags("library", [record_id])[record_id][0]
        return self.application.cache._graph_store().get("library", record_id, etag)


class ExportGraphsTest(GraphJobsCase):
    def test_every_record_with_an_etag_is_a_graph_after_a_comment_with_its_etag(self):
        count, files = self.store.export_chunks(self.tmp / "out", None, graphs=True)
        self.assertEqual([f.name for f in files], ["chunk-0.trig"])
        items = read_graphs(files[0].read_text())
        self.assertEqual(count, len(items))
        self.assertEqual(
            count, fetch_one(self.store.con.execute("SELECT count(*) FROM etags"))[0]
        )
        for item in items:
            record_id = self.store.record_id_of(item["id"])
            self.assertEqual(
                item["etag"],
                self.application.cache.etags("library", [record_id])[record_id][0],
            )
            quads = list(
                ox.parse(item["source"].encode(), format=ox.RdfFormat.N_TRIPLES)
            )
            self.assertIn(
                item["id"],
                {q.subject.value for q in quads if isinstance(q.subject, ox.NamedNode)},
            )
        whole = ox.parse(
            files[0].read_bytes(), format=ox.RdfFormat.TRIG
        )  # and the file is TriG as a whole
        self.assertEqual(
            {str(q.graph_name) for q in whole}, {f"<{i['id']}>" for i in items}
        )

    def test_a_graph_has_the_record_and_what_it_is_made_of(self):
        _, files = self.store.export_chunks(self.tmp / "out", "person", graphs=True)
        by_record = {i["id"]: i for i in read_graphs(files[0].read_text())}
        subjects = lambda item: {  # noqa: E731
            q.subject.value
            for q in ox.parse(item["source"].encode(), format=ox.RdfFormat.N_TRIPLES)
            if isinstance(q.subject, ox.NamedNode)
        }
        self.assertLessEqual(
            {"rec:person:14", "rec:person:13", "rec:type:5"},
            subjects(by_record["rec:person:14"]),
        )
        self.assertIn("rec:person:13", subjects(by_record["rec:person:13"]))
        self.assertNotIn(
            "rec:person:14", subjects(by_record["rec:person:13"])
        )  # 13 is not made of 14
        self.assertNotIn("rec:person:1", subjects(by_record["rec:person:14"]))

    def test_a_type_is_chosen_and_none_is_every_type(self):
        every, _ = self.store.export_chunks(self.tmp / "all", None, graphs=True)
        people, _ = self.store.export_chunks(self.tmp / "people", "person", graphs=True)
        self.assertGreater(every, people)
        self.assertEqual(people, 14)
        nobody, files = self.store.export_chunks(
            self.tmp / "nobody", "nobody", graphs=True
        )
        self.assertEqual((nobody, files), (0, []))

    def test_in_chunks_the_files_are_trig_and_all_of_the_graphs_are_there(self):
        # (DuckDB starts a new file after 2048 rows at the earliest: a small repository
        # is one file)
        count, files = self.store.export_chunks(
            self.tmp / "chunks", None, chunk_bytes=1, graphs=True
        )
        self.assertEqual([f.name for f in files], ["chunk-0.trig"])
        ids = [i["id"] for f in files for i in read_graphs(f.read_text())]
        self.assertEqual(len(ids), count)
        self.assertEqual(len(set(ids)), count)

    def test_the_export_without_graphs_is_as_it_was(self):
        count, files = self.store.export_chunks(self.tmp / "nt", "person")
        self.assertEqual([f.name for f in files], ["chunk-0.nt"])
        self.assertEqual(count, 14)

    def test_the_export_job_has_an_option_for_it_and_offers_the_trig_to_download(self):
        spec = ExportJob().spec("library", {"res_type": "person", "graphs": True})
        self.assertEqual(
            spec["stages"],
            [{"stage": "Export", "params": {"res_type": "person", "graphs": True}}],
        )
        self.assertEqual(
            ExportJob().spec("library", {"res_type": "person"})["stages"][0]["params"],
            {"res_type": "person"},
        )
        job_id = self.graphs({"res_type": "person"}, update=False)
        batch = self.jobs_dir / job_id / "00-Export" / "batch-00000.trig"
        self.assertTrue(batch.is_file())
        self.assertEqual(
            self.stage(job_id, "Export")["processed"],
            len(read_graphs(batch.read_text())),
        )
        from gracia.jobs.builtin import Export

        name, content_type = Export.offer(self.jobs_dir / job_id, [], 0)
        self.assertEqual((name, content_type), ("export.trig", "application/trig"))


class ExportQueueTest(GraphJobsCase):
    def items(self):
        count, files, bound = self.store.export_queue(self.tmp / "queue", 32_000_000)
        return (
            count,
            bound,
            {i["id"]: i for f in files for i in read_graphs(f.read_text())},
        )

    def test_every_waiting_record_is_one_graph_with_its_etag_and_the_newest_action(
        self,
    ):
        count, bound, items = self.items()
        waiting = fetch_one(
            self.store.con.execute(
                "SELECT count(DISTINCT record_id) FROM actions_queue"
            )
        )[0]
        self.assertEqual(
            (count, len(items), bound),
            (waiting, waiting, self.store.last_transaction()),
        )
        record_id = self.store.record_id_of("rec:person:14")
        etag = self.application.cache.etags("library", [record_id])[record_id][0]
        self.assertEqual(
            (items["rec:person:14"]["etag"], items["rec:person:14"]["action"]),
            (etag, "insert"),
        )
        self.assertIn(
            "rec:person:13", items["rec:person:14"]["source"]
        )  # and what it is made of

    def test_a_record_that_is_gone_is_an_empty_graph_marked_delete(self):
        self.store.delete(["rec:person:14"])
        _, _, items = self.items()
        gone = items["rec:person:14"]
        self.assertEqual(
            (gone["etag"], gone["action"], gone["source"].strip()),
            ("delete", "delete", ""),
        )

    def test_nothing_is_exported_when_nothing_waits_and_the_rows_are_not_touched(self):
        _, _, items = self.items()
        pending = self.store.pending_actions()
        self.assertEqual(
            self.store.pending_actions(), pending
        )  # exporting takes nothing off
        self.store.actions_done(
            [self.store.record_id_of(u) for u in items], self.store.last_transaction()
        )
        count, files, _ = self.store.export_queue(self.tmp / "empty", 32_000_000)
        self.assertEqual((count, files), (0, []))


class UpdateCacheTest(GraphJobsCase):
    def test_the_graph_of_every_record_is_kept_as_rdfxml_under_its_etag(self):
        job_id = self.graphs()
        records = fetch_one(self.store.con.execute("SELECT count(*) FROM etags"))[0]
        self.assertEqual(self.stage(job_id, "UpdateCache")["passed"], records)
        for record_uri in ("rec:person:1", "rec:person:13", "rec:person:14"):
            xml = self.kept(record_uri)
            self.assertIsNotNone(xml, record_uri)
        subjects = {
            q.subject.value
            for q in ox.parse(self.kept("rec:person:14"), format=ox.RdfFormat.RDF_XML)
            if isinstance(q.subject, ox.NamedNode)
        }
        self.assertLessEqual({"rec:person:14", "rec:person:13"}, subjects)
        self.assertNotIn("rec:person:1", subjects)

    def test_what_it_keeps_is_what_the_cache_makes_by_itself(self):
        self.graphs()
        kept = self.kept("rec:person:14")
        record_id = self.store.record_id_of("rec:person:14")
        fresh = Application(
            ":memory:"
        )  # no cache directory: made from the database every time
        self.addCleanup(fresh.close)

        def triples(xml):
            dataset = ox.Dataset(ox.parse(xml, format=ox.RdfFormat.RDF_XML))
            dataset.canonicalize(ox.CanonicalizationAlgorithm.RDFC_1_0)
            return {str(q) for q in dataset}

        made = self.application.cache.graph("library", record_id)  # a hit: the kept one
        self.assertEqual(made, kept)
        from gracia.cache import rdfxml

        _etag, text = self.store.closure_source(record_id)
        self.assertEqual(triples(rdfxml(text)), triples(kept))

    def test_a_graph_is_made_again_only_when_the_record_changed(self):
        self.graphs()
        before = {u: self.kept(u) for u in ("rec:person:1", "rec:person:14")}
        self.put(
            '<rec:person:13> <http://ex/p> "changed" .'
        )  # person 14 is made of it; person 1 is not
        self.assertIsNone(
            self.kept("rec:person:14")
        )  # the entry is for the etag it had
        self.assertEqual(self.kept("rec:person:1"), before["rec:person:1"])
        self.graphs()
        self.assertIsNotNone(self.kept("rec:person:14"))
        self.assertNotEqual(self.kept("rec:person:14"), before["rec:person:14"])

    def test_with_a_chunk_size_every_record_is_kept_too(self):
        job_id = self.graphs({"chunk_bytes": 1})
        records = fetch_one(self.store.con.execute("SELECT count(*) FROM etags"))[0]
        self.assertEqual(self.stage(job_id, "UpdateCache")["passed"], records)
        self.assertIsNotNone(self.kept("rec:person:14"))

    def test_without_a_cache_directory_the_job_fails_and_says_why(self):
        self.application.cache._graph_dir = None
        self.application.cache._graphs = None
        job_id = self.make(
            {"stage": "Export", "params": {"graphs": True}}, {"stage": "UpdateCache"}
        )
        self.assertEqual(self.run_job(job_id), runner.EXIT_FAILED)
        self.assertIn("gracia.cache_dir", self.job(job_id)["error_msg"])

    def test_the_job_of_the_repository_is_the_two_stages_and_the_administrators_only(
        self,
    ):
        spec = UpdateCacheJob().spec("library", {})
        self.assertEqual(
            [s["stage"] for s in spec["stages"]], ["Export", "UpdateCache"]
        )
        self.assertTrue(spec["stages"][0]["params"]["graphs"])
        self.assertEqual(UpdateCacheJob.groups, ())
        job_id = self.make(*spec["stages"])
        self.assertEqual(
            self.run_job(job_id), runner.EXIT_DONE, self.job(job_id)["error_msg"]
        )
        self.assertIsNotNone(self.kept("rec:person:14"))


class ProcessActionsJobTest(GraphJobsCase):
    def subscribe(self) -> list:
        """What the job's process would have subscribed: handlers in the registry it
        works with."""
        events = []
        context.use(self.application, self.settings)
        context.registry().registerHandler(events.append, (RecordsChanged,))
        return events

    def test_the_job_hands_the_waiting_changes_to_the_subscribers_and_makes_the_cache(
        self,
    ):
        events = self.subscribe()
        self.put(
            "@prefix gracia: <https://gracia.pub/ns/> . @prefix foaf: "
            "<http://xmlns.com/foaf/0.1/> "
            ".\n" + "\n".join(person(n) for n in range(100, 250))
        )
        rows = self.store.pending_actions()[0]
        self.assertGreater(rows, 100)  # more than one batch
        job_id = self.make(*ProcessActionsJob().spec("library", {})["stages"])
        self.assertEqual(
            self.run_job(job_id), runner.EXIT_DONE, self.job(job_id)["error_msg"]
        )
        stage = self.stage(job_id, "ProcessActions")
        self.assertEqual(
            stage["batches"]["total"], 1
        )  # a file of 32 MB of graphs, however many records
        self.assertEqual(self.store.pending_actions(), (0, None))
        records = fetch_one(self.store.con.execute("SELECT count(*) FROM records"))[0]
        self.assertEqual(
            {c.record for e in events for c in e.changes}
            >= {"rec:person:14", "rec:person:1"},
            True,
        )
        self.assertEqual(
            sum(len(e.changes) for e in events), records
        )  # each record once
        self.assertEqual(stage["passed"], records)
        self.assertIsNotNone(
            self.kept("rec:person:14")
        )  # the cache was made by the batches

    def test_with_nothing_waiting_the_job_is_done_at_once(self):
        self.subscribe()
        process_pending(self.application, context.registry())
        job_id = self.make(*ProcessActionsJob().spec("library", {})["stages"])
        self.assertEqual(
            self.run_job(job_id), runner.EXIT_DONE, self.job(job_id)["error_msg"]
        )
        self.assertEqual(self.stage(job_id, "Export")["batches"]["total"], 0)

    def test_it_is_a_job_of_the_repository_for_the_administrators(self):
        spec = ProcessActionsJob().spec("library", {})
        self.assertEqual(
            [st["stage"] for st in spec["stages"]], ["Export", "ProcessActions"]
        )
        self.assertTrue(spec["stages"][0]["params"]["queued"])
        self.assertEqual(ProcessActionsJob.groups, ())

    def test_what_is_queued_after_the_export_stays_in_the_queue(self):
        events = self.subscribe()
        job_id = self.make(*ProcessActionsJob().spec("library", {})["stages"])
        self.assertEqual(
            self.run_job(job_id), runner.EXIT_DONE, self.job(job_id)["error_msg"]
        )
        self.put(
            "@prefix gracia: <https://gracia.pub/ns/> . @prefix foaf: "
            "<http://xmlns.com/foaf/0.1/> "
            ".\n" + person(500)
        )
        self.assertEqual(
            self.store.pending_actions()[0], 1
        )  # written after: the next run's
        self.assertFalse(
            any(c.record == "rec:person:500" for e in events for c in e.changes)
        )

    def test_a_delete_and_a_record_that_changed_again_are_what_the_events_say(self):
        events = self.subscribe()
        self.store.delete(["rec:person:14"])
        self.put(LINKED.replace("Thirteen", "Thirteen again"))
        job_id = self.make(*ProcessActionsJob().spec("library", {})["stages"])
        self.assertEqual(
            self.run_job(job_id), runner.EXIT_DONE, self.job(job_id)["error_msg"]
        )
        actions = {c.record: c.action for e in events for c in e.changes}
        self.assertEqual(actions["rec:person:13"], "update")
        self.assertIn(
            actions["rec:person:14"], ("insert", "update")
        )  # made again after it was deleted
