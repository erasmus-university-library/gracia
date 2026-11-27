import datetime
import threading
import time
from typing import cast

from harness import etag_of, graph_of, stopped_worker
from pyramid.registry import Registry
from test_change_events import STAMP2, BatchCase
from test_etags import record

from gracia.cache import EtagStore, GraphStore
from gracia.change_events import ChangeEventsWorker
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
        self.store.con.execute("DELETE FROM change_events")
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
        self.assertEqual(summary["change_events"], {})

    def test_the_queue_is_summed_by_action_with_the_oldest_transaction_and_when_it_was(
        self,
    ):
        self.put(record("rec:org:1", refs=["rec:org:9"], stamp=STAMP2))
        summary = self.store.pending_summary()
        self.assertEqual(
            summary["change_events"], {"insert": 4, "update": 1, "ref_update": 2}
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

    def test_the_graphs_of_a_repository_and_the_space_of_them(self):
        graphs = GraphStore(self.tmp / "stats")
        self.addCleanup(graphs.close)
        etag = "0123456789abcdef"
        graphs.put_many(
            [("main", n, etag, b"<x/>" * 100) for n in range(1, 6)]
            + [("other", 1, etag, b"<y/>")]
        )
        main, other = graphs.repo_stats("main"), graphs.repo_stats("other")
        self.assertEqual((main["entries"], other["entries"]), (5, 1))
        self.assertEqual(graphs.repo_stats("nobody")["entries"], 0)
        self.assertGreater(main["bytes"], 5 * 400)  # the keys and the values
        self.assertGreater(main["bytes"], other["bytes"])
        capped = graphs.repo_stats("main", limit=3)
        self.assertEqual((capped["entries"], capped["capped"]), (3, True))  # no further
        self.assertFalse(main["capped"])

    def test_every_cache_says_what_it_holds_of_a_repository(self):
        cache = self.app.cache
        record_id = self.store.record_id_of("rec:work:1")

        def held(repo="main"):
            stats = cache.repo_stats(repo)
            return stats["modified_at"], {c["name"]: c for c in stats["caches"]}

        _, before = held()
        self.assertEqual(
            [c["name"] for c in before.values()],
            ["etags", "slugs", "referencing", "graphs", "docs"],
        )
        self.assertEqual(before["etags"]["entries"], 0)
        worker = stopped_worker(self.app)
        graph_of(self.app, "main", record_id)  # asks for the etag too, and the worker
        worker.turn()  # to make the graph for the cache
        self.store.con.execute(
            "INSERT INTO slugs (rec_type, slug, record_uri, is_deleted) "
            "VALUES ('work', 'w', 'rec:work:1', false)"
        )
        with self.app.new_store("main") as cached:
            cached.slug_lookup("work", "w")
            cached.referencing_records(record_type=6)  # a page and its total
            cached.count_referencing_records(record_type=6)
        modified, now = held()
        self.assertIsInstance(modified, datetime.datetime)
        self.assertEqual(
            {name: c["entries"] for name, c in now.items() if name != "docs"},
            {"etags": 1, "slugs": 1, "referencing": 2, "graphs": 1},
        )
        self.assertTrue(all(c["bytes"] > 0 for n, c in now.items() if c["entries"]))
        self.assertEqual(held("other")[1]["etags"]["entries"], 0)  # another repository
        # a write: what the open caches kept is deleted when the cache sees it, not when
        # it is asked for again
        self.put(record("rec:org:2", stamp="2026-06-01T00:00:00.000000Z"))
        later, after = held()
        self.assertGreater(later, modified)
        for name in ("etags", "slugs", "referencing"):
            self.assertEqual(
                (after[name]["entries"], after[name]["stale"]), (0, 0), name
            )
        etag_of(self.app, "main", record_id)  # and kept again as it is asked for
        self.assertEqual(
            (held()[1]["etags"]["entries"], held()[1]["etags"]["stale"]), (1, 0)
        )
        # a request that was running wrote under the old time: that is what is old
        cast(EtagStore, cache.stamped("etags")).put_many(
            "main", cache.repo_last_modified_at("main") - 1, {1: "x"}
        )
        self.assertEqual(held()[1]["etags"]["stale"], 1)
        self.put(record("rec:org:3", stamp="2026-07-01T00:00:00.000000Z"))
        self.assertEqual(held()[1]["etags"]["stale"], 0)  # and goes with the next write
        # without a directory the graphs and docs are not kept
        cache._graph_dir = None
        cache._graphs = None
        none = held()[1]
        self.assertNotIn("entries", none["graphs"])
        self.assertNotIn("entries", none["docs"])


class WorkerStatusTest(BatchCase):
    def test_a_worker_that_was_never_started_is_not_running_and_not_in_line(self):
        worker = ChangeEventsWorker(self.app, Registry())
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
        worker = ChangeEventsWorker(self.app, Registry(), poll=0.05)
        self.addCleanup(worker.stop)
        worker.start()
        self.assertTrue(worker.status("main")["running"])
        worker.stop()
        status = worker.status("main")
        self.assertEqual((status["running"], status["stopped"]), (False, True))

    def test_the_line_and_the_failures_of_a_repository_are_in_it(self):
        worker = ChangeEventsWorker(self.app, Registry())
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
        worker = ChangeEventsWorker(self.app, Registry(), poll=0.05)
        self.addCleanup(worker.stop)
        worker.start()
        self.assertIn("gracia-worker", [t.name for t in threading.enumerate()])
