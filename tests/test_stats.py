import datetime
import threading
import time

from pyramid.registry import Registry
from test_actions import STAMP2, BatchCase
from test_etags import record

from gracia.actions import Worker
from gracia.cache import GraphStore
from gracia.store import fetch_one


class StoreStatsTest(BatchCase):
    def setUp(self):
        super().setUp()
        self.put(
            record("rec:org:1"),
            record("rec:person:1", refs=["rec:org:1"]),
            record("rec:person:2", published=False),
            record("rec:work:1", refs=["rec:person:1"]),
        )

    def test_the_types_have_their_records_the_published_ones_the_bytes_and_the_latest_change(  # noqa: E501
        self,
    ):
        stats = self.store.type_stats()
        self.assertEqual(
            {k: v["records"] for k, v in stats.items()},
            {"org": 1, "person": 2, "work": 1},
        )
        self.assertEqual(
            (stats["person"]["published"], stats["org"]["published"]), (1, 1)
        )
        own = dict(
            self.store.con.execute(
                "SELECT rec_type, sum(strlen(source)) FROM records GROUP BY 1"
            ).fetchall()
        )
        self.assertEqual(
            {k: v["bytes"] for k, v in stats.items()},
            {k: int(v) for k, v in own.items()},
        )
        self.assertIsInstance(stats["person"]["modified"], datetime.datetime)

    def test_the_pages_have_their_number_and_the_size_of_what_they_are_made_of(self):
        stats = self.store.etag_stats()
        self.assertEqual(stats["pages"], 4)
        self.assertEqual(
            stats["bytes"],
            fetch_one(self.store.con.execute("SELECT sum(closure_bytes) FROM etags"))[
                0
            ],
        )
        self.assertGreater(
            stats["bytes"], sum(v["bytes"] for v in self.store.type_stats().values())
        )  # shared ones count again

    def test_an_empty_repository_has_nothing_to_count(self):
        self.assertEqual(self.store.pending_summary()["rows"], 4)  # what was put above
        self.store.con.execute("DELETE FROM actions_queue")
        summary = self.store.pending_summary()
        self.assertEqual(
            (
                summary["rows"],
                summary["records"],
                summary["bytes"],
                summary["oldest"],
                summary["since"],
            ),
            (0, 0, 0, None, None),
        )
        self.assertEqual(summary["actions"], {})

    def test_the_queue_is_summed_by_action_with_the_oldest_transaction_and_when_it_was(
        self,
    ):
        self.put(record("rec:org:1", refs=["rec:org:9"], stamp=STAMP2))
        summary = self.store.pending_summary()
        self.assertEqual(
            summary["actions"], {"insert": 4, "update": 1, "ref_update": 2}
        )
        self.assertEqual(
            (summary["oldest"], summary["last"], summary["records"]), (1, 2, 4)
        )
        self.assertEqual(summary["rows"], 7)
        self.assertEqual(summary["bytes"], self.store.pending_size()[1])
        self.assertIsInstance(summary["since"], datetime.datetime)


class GraphCacheStatsTest(BatchCase):
    def setUp(self):
        super().setUp()
        self.put(record("rec:org:1"), record("rec:work:1", refs=["rec:org:1"]))

    def test_the_entries_of_a_repository_and_of_all_of_them_and_the_space(self):
        graphs = GraphStore(self.tmp / "stats")
        self.addCleanup(graphs.close)
        etag = "0123456789abcdef"
        graphs.put_many(
            [("main", n, etag, b"<x/>" * 100) for n in range(1, 6)]
            + [("other", 1, etag, b"<y/>")]
        )
        self.assertEqual(
            (graphs.count("main"), graphs.count("other"), graphs.count("nobody")),
            (5, 1, 0),
        )
        self.assertEqual(
            graphs.count("main", limit=3), 3
        )  # counted no further than that
        stats = graphs.stats()
        self.assertEqual(stats["entries"], 6)
        self.assertGreater(stats["bytes"], 0)
        self.assertGreater(stats["file"], 0)
        self.assertGreater(stats["limit"], stats["file"])

    def test_the_cache_says_what_it_holds_and_none_without_a_directory(self):
        record_id = self.store.record_id_of("rec:work:1")
        self.assertEqual(self.app.cache.etags_held("main"), 0)
        self.app.cache.etags("main", [record_id])
        self.assertEqual(self.app.cache.etags_held("main"), 1)
        self.assertEqual(self.app.cache.etags_held("other"), 0)
        self.app.cache.graph("main", record_id)  # ty: ignore[invalid-argument-type]
        found = self.app.cache.graph_stats("main")
        self.assertEqual((found["repo_entries"], found["entries"]), (1, 1))  # ty: ignore[not-subscriptable]
        self.assertTrue(found["directory"].endswith("cache"))  # ty: ignore[not-subscriptable]
        self.app.cache._graph_dir = None
        self.app.cache._graphs = None
        self.assertIsNone(self.app.cache.graph_stats("main"))


class WorkerStatusTest(BatchCase):
    def test_a_worker_that_was_never_started_is_not_running_and_not_in_line(self):
        worker = Worker(self.app, Registry())
        self.assertEqual(
            worker.status("main"),
            {
                "running": False,
                "stopped": False,
                "in_line": False,
                "line": 0,
                "failures": 0,
                "retry_in": None,
            },
        )

    def test_a_running_worker_is_running_and_a_stopped_one_says_so(self):
        worker = Worker(self.app, Registry(), poll=0.05)
        self.addCleanup(worker.stop)
        worker.start()
        self.assertTrue(worker.status("main")["running"])
        worker.stop()
        status = worker.status("main")
        self.assertEqual((status["running"], status["stopped"]), (False, True))

    def test_the_line_and_the_failures_of_a_repository_are_in_it(self):
        worker = Worker(self.app, Registry())
        worker._line.extend(["main", "other"])
        worker._retry["main"] = (time.monotonic() + 20, 2)
        status = worker.status("main")
        self.assertEqual(
            (status["in_line"], status["line"], status["failures"]), (True, 2, 2)
        )
        self.assertTrue(18 < status["retry_in"] <= 20)
        self.assertEqual(worker.status("other")["failures"], 0)
        worker._retry["main"] = (time.monotonic() - 5, 2)
        self.assertEqual(
            worker.status("main")["retry_in"], 0.0
        )  # the pause is over: not negative

    def test_the_thread_of_the_worker_is_a_thread(self):
        worker = Worker(self.app, Registry(), poll=0.05)
        self.addCleanup(worker.stop)
        worker.start()
        self.assertIn("gracia-worker", [t.name for t in threading.enumerate()])
