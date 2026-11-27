import threading
import time

import pyoxigraph as ox
from harness import etag_of, graph_of, open_application, open_store
from pyramid.registry import Registry
from test_etags import TYPES, EtagCase, record

from gracia import store as store_module
from gracia.change_events import (
    BATCH,
    ChangeEventsWorker,
    RecordsChanged,
    lock_dir_for,
    process_batch,
    process_pending,
    repository_lock,
)
from gracia.store import fetch_one

STAMP2 = "2026-06-01T00:00:00.000000Z"


class QueueCase(EtagCase):
    def queue(self, after: int = 0) -> list[tuple]:
        return self.store.con.execute(
            "SELECT transaction_id, record_uri(record_id), kind FROM change_events "
            f"WHERE transaction_id > {after} ORDER BY 1, 2"
        ).fetchall()


class WrittenTest(QueueCase):
    def test_a_new_record_is_an_insert_a_changed_one_an_update_and_the_rows_are_one_for_the_record_and_the_transaction(
        self,
    ):
        self.put(record("rec:org:1"), record("rec:org:2"))
        self.assertEqual(
            self.queue(), [(1, "rec:org:1", "insert"), (1, "rec:org:2", "insert")]
        )
        self.put(record("rec:org:1", refs=["rec:org:2"], stamp=STAMP2))
        self.assertIn((2, "rec:org:1", "update"), self.queue(1))
        # a delete is a delete and the record made again is an insert
        self.store.delete(["rec:org:1"])
        self.assertEqual(self.queue(2), [(3, "rec:org:1", "delete")])
        self.put(record("rec:org:1"))
        self.assertEqual(self.queue(3), [(4, "rec:org:1", "insert")])
        # a replace is one row for the record and the transaction
        self.put(record("rec:org:1", refs=["rec:org:9"], stamp=STAMP2))
        self.assertEqual(
            [r for r in self.queue(4) if r[1] == "rec:org:1"],
            [(5, "rec:org:1", "update")],
        )
        # the rows are in the transaction that wrote them and a rollback leaves none
        before = self.queue()
        self.store.con.begin()
        self.store.con.execute("DELETE FROM triples WHERE s = 'rec:org:1'")
        self.store.con.rollback()
        self.assertEqual(self.queue(), before)

    def test_the_pages_made_of_a_changed_record_are_ref_updates_and_nothing_else_is(
        self,
    ):
        self.put(
            record("rec:org:1"),
            record("rec:person:1", refs=["rec:org:1"]),
            record("rec:work:1", refs=["rec:person:1"]),
            record("rec:work:2"),
        )
        self.put(record("rec:org:1", stamp=STAMP2, refs=["rec:org:2"]))
        self.assertEqual(
            self.queue(1),
            [
                (2, "rec:org:1", "update"),
                (2, "rec:person:1", "ref_update"),
                (2, "rec:work:1", "ref_update"),
            ],
        )  # work 2 is not made of org 1
        # the same with the delta that does not walk the pages
        old = store_module.ETAGS_DELTA_ABOVE
        store_module.ETAGS_DELTA_ABOVE = 0  # ty: ignore[invalid-assignment]
        self.addCleanup(setattr, store_module, "ETAGS_DELTA_ABOVE", old)
        self.put(
            record("rec:org:11"),
            record("rec:person:11", refs=["rec:org:11"]),
            *[record(f"rec:work:{n}", refs=["rec:person:11"]) for n in range(11, 16)],
        )
        last = self.store.last_transaction()
        self.put(
            record("rec:org:11", stamp=STAMP2)
        )  # its references are the same: the pages are moved, not walked
        self.assertEqual(
            {r[1] for r in self.queue(last) if r[2] == "ref_update"},
            {"rec:person:11", *(f"rec:work:{n}" for n in range(11, 16))},
        )
        # a record that was written and made of something that changed is one row
        self.put(record("rec:org:21"), record("rec:person:21", refs=["rec:org:21"]))
        last = self.store.last_transaction()
        self.put(
            record("rec:org:21", stamp=STAMP2),
            record("rec:person:21", refs=["rec:org:21"], stamp=STAMP2),
        )
        self.assertEqual(
            self.queue(last),
            [(last + 1, "rec:org:21", "update"), (last + 1, "rec:person:21", "update")],
        )

    def test_a_write_outside_the_store_is_queued_by_the_triggers_and_a_rebuild_queues_the_pages_that_are_not_what_they_were(
        self,
    ):
        self.put(record("rec:org:1"), record("rec:work:1", refs=["rec:org:1"]))
        self.store.con.execute(
            "INSERT INTO triples VALUES ('rec:org:1', [], ?, NULL, "
            "'2027-01-01T00:00:00.000000Z', "
            "NULL, ?)",
            [
                "https://gracia.pub/ns/modified_at",
                "http://www.w3.org/2001/XMLSchema#dateTime",
            ],
        )
        self.assertEqual(
            next(r for r in self.queue(1) if r[1] == "rec:org:1")[2], "update"
        )
        self.assertFalse(
            [r for r in self.queue(1) if r[1] == "rec:work:1"]
        )  # no refresh yet
        self.assertTrue(self.store.catch_up_etags())
        self.assertIn(
            "ref_update", {r[2] for r in self.queue(1) if r[1] == "rec:work:1"}
        )
        self.assertFalse(self.store.catch_up_etags())  # and now there is nothing

        # a rebuild queues the pages that are not what they were
        self.store.con.execute(
            "UPDATE etags SET etag = etag + 1 WHERE record_uri = 'rec:work:1'"
        )
        last = self.store.last_transaction()
        self.assertEqual(self.store.rebuild_etags(), ["rec:work:1"])
        self.assertEqual(self.queue(last - 1)[-1], (last, "rec:work:1", "ref_update"))


class TakeTest(QueueCase):
    def setUp(self):
        super().setUp()
        self.put(
            *[record(f"rec:work:{n}") for n in range(1, 8)]
        )  # transaction 1: seven inserts

    def test_the_oldest_rows_in_the_order_they_were_queued_and_nothing_is_removed_until_they_are_done(
        self,
    ):
        taken, bound = self.store.take_change_events(3)
        self.assertEqual([t[1] for t in taken], [f"rec:work:{n}" for n in (1, 2, 3)])
        self.assertEqual((bound, self.store.pending_change_events()), (1, (7, 1)))
        # a record that changed again is one change with the newest action
        self.put(record("rec:work:2", refs=["rec:org:9"], stamp=STAMP2))
        self.store.delete(["rec:work:3"])
        taken, bound = self.store.take_change_events(100)
        by_record = {uri_: action for _, uri_, action in taken}
        self.assertEqual(by_record["rec:work:2"], "update")
        self.assertEqual(by_record["rec:work:3"], "delete")
        self.assertEqual(len(taken), 7)  # seven records, nine rows
        self.assertEqual(bound, 3)
        # what was queued after the read stays when the rows are taken off
        self.put(
            record("rec:work:1", refs=["rec:org:9"], stamp=STAMP2)
        )  # transaction 4, after the read
        self.store.change_events_done([t[0] for t in taken], bound)
        self.assertEqual([r[:2] for r in self.queue()], [(4, "rec:work:1")])
        self.assertEqual(self.store.pending_change_events(), (1, 4))
        # an empty queue has nothing
        self.store.change_events_done(
            [t[0] for t in self.store.take_change_events(100)[0]], 4
        )
        self.assertEqual(
            (self.store.take_change_events(10)[0], self.store.pending_change_events()),
            ([], (0, None)),
        )


class SizeTest(QueueCase):
    def setUp(self):
        super().setUp()
        self.put(
            record("rec:org:1"),
            record("rec:person:1", refs=["rec:org:1"]),
            record("rec:work:1", refs=["rec:person:1"]),
            record("rec:work:2"),
        )

    def sizes(self) -> dict[str, tuple[int, int]]:
        return {
            u: (b, c)
            for u, b, c in self.store.con.execute(
                "SELECT record_uri, bytes, closure_bytes FROM etags"
            ).fetchall()
        }

    def test_a_record_has_the_size_of_its_source_a_page_the_size_of_all_it_is_made_of_and_a_batch_stops_at_the_bytes(
        self,
    ):
        own = dict(
            self.store.con.execute(
                "SELECT record_uri(record_id), strlen(source) FROM records"
            ).fetchall()
        )
        self.assertTrue(all(b > 0 for b in own.values()))
        sizes = self.sizes()
        for uri_, (bytes_, _closure) in sizes.items():
            self.assertEqual(bytes_, own[uri_])
        self.assertEqual(sizes["rec:org:1"][1], own["rec:org:1"])
        self.assertEqual(
            sizes["rec:work:1"][1],
            own["rec:work:1"] + own["rec:person:1"] + own["rec:org:1"],
        )
        self.assertEqual(sizes["rec:work:2"][1], own["rec:work:2"])
        sizes = self.sizes()
        everything, _ = self.store.take_change_events(100)
        two, _ = self.store.take_change_events(
            100, max_bytes=sizes[everything[0][1]][1] + sizes[everything[1][1]][1]
        )
        self.assertEqual([t[1] for t in two], [t[1] for t in everything[:2]])
        tiny, _ = self.store.take_change_events(100, max_bytes=1)
        self.assertEqual(
            len(tiny), 1
        )  # the first is always taken: a batch is never empty
        self.assertEqual(
            len(self.store.take_change_events(100, max_bytes=10**9)[0]), len(everything)
        )
        # the work of the queue is how many records and how many bytes
        records, size = self.store.pending_size()
        self.assertEqual(records, 4)
        self.assertEqual(size, sum(c for _, c in self.sizes().values()))
        self.store.change_events_done(
            [t[0] for t in self.store.take_change_events(100)[0]],
            self.store.last_transaction(),
        )
        self.assertEqual(self.store.pending_size(), (0, 0))

    def test_the_size_of_a_page_follows_what_it_is_made_of_also_when_the_etags_are_moved_not_walked(
        self,
    ):
        old = store_module.ETAGS_DELTA_ABOVE
        store_module.ETAGS_DELTA_ABOVE = 0  # ty: ignore[invalid-assignment]
        self.addCleanup(setattr, store_module, "ETAGS_DELTA_ABOVE", old)
        before = self.sizes()
        self.put(
            record("rec:org:1", refs=["rec:org:9"], stamp=STAMP2)
        )  # a bigger record, the same references
        after = self.sizes()
        grown = after["rec:org:1"][0] - before["rec:org:1"][0]
        self.assertGreater(grown, 0)
        self.assertEqual(after["rec:work:1"][1], before["rec:work:1"][1] + grown)
        self.assertEqual(after["rec:person:1"][1], before["rec:person:1"][1] + grown)
        self.assertEqual(
            after["rec:work:2"][1], before["rec:work:2"][1]
        )  # not made of it
        self.assert_made_again_equals_a_rebuild()  # and a rebuild says the same


class BatchCase(QueueCase):
    def setUp(self):
        super().setUp()
        self.app = open_application(self, self.db, cache_dir=self.tmp / "cache")
        self.registry = Registry()
        self.events: list[RecordsChanged] = []
        self.registry.registerHandler(self.events.append, (RecordsChanged,))

    def drain(self, limit: int = BATCH) -> list[int]:
        done = []
        while changes := process_batch(self.app, self.registry, "main", limit):
            done.append(len(changes))
        return done

    def kept(self, rec_uri: str):
        record_id = self.store.record_id_of(rec_uri)
        etag = etag_of(self.app, "main", record_id)
        return self.app.cache.graph_store.get("main", record_id, etag)  # ty: ignore[missing-union-attribute]


class BatchTest(BatchCase):
    def test_a_batch_is_one_event_with_the_changes_and_a_dataset_of_every_record_that_is_there(
        self,
    ):
        self.put(record("rec:org:1"), record("rec:person:1", refs=["rec:org:1"]))
        self.assertEqual(self.drain(), [2])
        (event,) = self.events
        self.assertEqual((event.repo, event.application), ("main", self.app))
        self.assertEqual(
            {(c.record, c.kind) for c in event.changes},
            {("rec:org:1", "insert"), ("rec:person:1", "insert")},
        )

        def subjects(graph):
            return {
                q.subject.value for q in graph if isinstance(q.subject, ox.NamedNode)
            }

        self.assertEqual(
            subjects(event.graphs["rec:person:1"]), {"rec:person:1", "rec:org:1"}
        )  # and what it is made of
        self.assertEqual(subjects(event.graphs["rec:org:1"]), {"rec:org:1"})
        self.assertEqual(self.store.pending_change_events(), (0, None))
        # a record that is gone when the batch gets to it is a delete, and a record
        # changed three times is one change
        self.events.clear()
        self.put(record("rec:org:5"), record("rec:org:6"))
        self.store.delete(
            ["rec:org:6"]
        )  # still queued as an insert first, then a delete: the newest is a delete
        self.put(record("rec:org:7"))
        self.put(record("rec:org:7", refs=["rec:org:2"], stamp=STAMP2))
        self.put(
            record("rec:org:7", refs=["rec:org:3"], stamp="2026-07-01T00:00:00.000000Z")
        )
        self.drain()
        actions = {c.record: c.kind for e in self.events for c in e.changes}
        self.assertEqual(
            actions,
            {"rec:org:5": "insert", "rec:org:6": "delete", "rec:org:7": "update"},
        )
        self.assertEqual(
            sum(c.record == "rec:org:7" for e in self.events for c in e.changes), 1
        )

    def test_records_the_cache_lacked_are_made_when_the_worker_is_asked_to_make_them(
        self,
    ):
        self.put(record("rec:org:1"), record("rec:work:1", refs=["rec:org:1"]))
        worker = ChangeEventsWorker(
            self.app, self.registry
        )  # not running: the test turns it
        worker.stop()
        self.app.cache.worker = worker
        work = self.store.record_id_of("rec:work:1")
        graph_of(self.app, "main", work)  # the cache lacks it: the worker is asked
        self.assertIsNone(self.kept("rec:work:1"))  # and the read kept nothing
        self.assertEqual(self.events, [])
        self.assertTrue(worker.turn())
        self.assertIsNotNone(self.kept("rec:work:1"))  # the same as the queue makes
        (event,) = self.events
        self.assertEqual(
            [(c.record, c.kind) for c in event.changes], [("rec:work:1", "update")]
        )
        self.assertEqual(
            self.store.pending_change_events()[0], 2
        )  # the queue is not touched
        worker.rework("main", [work, 999])  # one that is not there is left out
        worker.turn()
        self.assertEqual(len(self.events), 2)

    def test_the_graph_cache_is_made_by_the_batch_and_the_graph_of_a_gone_record_forgotten(
        self,
    ):
        self.put(record("rec:org:1"), record("rec:work:1", refs=["rec:org:1"]))
        self.drain()
        first = self.kept("rec:work:1")
        self.assertIsNotNone(first)
        self.assertEqual(
            graph_of(self.app, "main", self.store.record_id_of("rec:work:1")).encode(),
            first,
        )  # a hit
        self.put(
            record("rec:org:1", refs=["rec:org:9"], stamp=STAMP2)
        )  # work 1 is made of it
        self.assertIsNone(self.kept("rec:work:1"))  # its entry is for the etag it had
        self.drain()
        self.assertIsNotNone(self.kept("rec:work:1"))  # the queue made the cache again
        record_id = self.store.record_id_of("rec:work:1")
        self.store.delete(["rec:work:1"])
        self.drain()
        self.assertEqual([c.kind for c in self.events[-1].changes], ["delete"])
        self.assertIsNone(
            self.app.cache.graph_store.get("main", record_id, "0" * 16)  # ty: ignore[missing-union-attribute]
        )
        with self.app.cache.graph_store._env.begin() as txn:  # ty: ignore[missing-union-attribute]
            self.assertIsNone(
                txn.get(self.app.cache.graph_store._key("main", record_id))  # ty: ignore[missing-union-attribute]
            )

    def test_a_write_outside_the_store_is_made_up_for_a_failing_handler_leaves_the_rows_and_without_a_graph_directory_the_events_come(
        self,
    ):
        self.put(record("rec:org:1"), record("rec:work:1", refs=["rec:org:1"]))
        self.drain()
        self.events.clear()
        self.store.con.execute(
            "INSERT INTO triples VALUES ('rec:org:1', [], ?, NULL, "
            "'2027-01-01T00:00:00.000000Z', "
            "NULL, ?)",
            [
                "https://gracia.pub/ns/modified_at",
                "http://www.w3.org/2001/XMLSchema#dateTime",
            ],
        )
        self.drain()
        self.assertEqual(
            {c.record for e in self.events for c in e.changes},
            {"rec:org:1", "rec:work:1"},
        )
        # a handler that fails leaves the rows and the batch is done again
        self.put(record("rec:org:31"))
        failing = {"on": True}

        def handler(event):
            if failing["on"]:
                raise RuntimeError("the index is down")

        self.registry.registerHandler(handler, (RecordsChanged,))
        with self.assertRaisesRegex(RuntimeError, "index is down"):
            process_batch(self.app, self.registry, "main")
        self.assertEqual(self.store.pending_change_events()[0], 1)
        failing["on"] = False
        self.assertEqual(len(process_batch(self.app, self.registry, "main")), 1)
        self.assertEqual(self.store.pending_change_events(), (0, None))
        # without a graph directory the events still come
        app = open_application(self, self.db)
        self.events.clear()
        self.put(record("rec:org:32"))
        self.assertEqual(len(process_batch(app, self.registry, "main")), 1)
        self.assertEqual(len(self.events), 1)


class ByteBatchTest(BatchCase):
    def test_an_event_is_as_many_records_as_fit_in_the_bytes_and_a_record_bigger_than_a_batch_is_a_batch_of_its_own(
        self,
    ):
        self.put(*[record(f"rec:work:{n}") for n in range(1, 11)])
        one = fetch_one(self.store.con.execute("SELECT max(closure_bytes) FROM etags"))[
            0
        ]
        self.assertEqual(
            process_pending(self.app, self.registry, max_bytes=one * 4), 10
        )
        self.assertEqual([len(e.changes) for e in self.events], [4, 4, 2])
        self.events.clear()
        self.put(record("rec:org:1"), record("rec:work:101", refs=["rec:org:1"]))
        self.assertEqual(process_pending(self.app, self.registry, max_bytes=1), 2)
        self.assertEqual([len(e.changes) for e in self.events], [1, 1])


class ManualTest(BatchCase):
    def test_it_handles_everything_that_is_queued_says_how_many_records_and_the_size_of_a_batch_is_the_size_of_an_event(
        self,
    ):
        self.put(record("rec:org:1"), record("rec:work:1", refs=["rec:org:1"]))
        self.assertEqual(process_pending(self.app, self.registry), 2)
        self.assertEqual(self.store.pending_change_events(), (0, None))
        self.assertEqual(sum(len(e.changes) for e in self.events), 2)
        self.assertEqual(
            process_pending(self.app, self.registry), 0
        )  # and nothing the second time
        # the size of a batch is the size of an event
        self.events.clear()
        self.put(*[record(f"rec:work:{n}") for n in range(11, 18)])
        self.assertEqual(process_pending(self.app, self.registry, batch=3), 7)
        self.assertEqual([len(e.changes) for e in self.events], [3, 3, 1])

    def test_a_repository_can_be_named_and_the_others_are_left(self):
        other = open_store(self.db, repo="other", create=True)
        self.addCleanup(other.close)
        other.set_type_ids(TYPES)
        other.put_multi(record("rec:org:1", res="ark:/1/other/1"))
        self.put(record("rec:org:1"))
        self.assertEqual(process_pending(self.app, self.registry, "main"), 1)
        self.assertEqual(
            (self.store.pending_change_events(), other.pending_change_events()),
            ((0, None), (1, 1)),
        )
        self.assertEqual(
            process_pending(self.app, self.registry), 1
        )  # all: the other one now
        self.assertEqual({e.repo for e in self.events}, {"main", "other"})

    def test_the_lock_is_waited_for_when_asked_to_and_the_locks_go_in_the_cache_directory_or_next_to_the_database(
        self,
    ):
        with repository_lock(None, "main") as held:
            self.assertTrue(held)
        holder = threading.Event()
        release = threading.Event()

        def hold():
            with repository_lock(self.tmp / "l", "main") as got:
                holder.set()
                release.wait(5)
                assert got

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(holder.wait(5))
        with repository_lock(self.tmp / "l", "main") as busy:
            self.assertFalse(busy)
        threading.Timer(0.2, release.set).start()
        with repository_lock(self.tmp / "l", "main", wait=True) as waited:
            self.assertTrue(waited)  # it came when the other let go
        thread.join()
        self.assertEqual(
            lock_dir_for({"gracia.cache_dir": "/c", "gracia.db": "/d/x.db"}).as_posix(),  # ty: ignore[missing-union-attribute]
            "/c",
        )
        self.assertEqual(lock_dir_for({"gracia.db": "/d/x.db"}).as_posix(), "/d")  # ty: ignore[missing-union-attribute]
        self.assertEqual(
            lock_dir_for(  # ty: ignore[missing-union-attribute]
                {"gracia.db": "quack:h:1", "gracia.db.file": "/e/y.db"}
            ).as_posix(),
            "/e",
        )
        self.assertIsNone(lock_dir_for({"gracia.db": "quack:h:1"}))
        self.assertIsNone(lock_dir_for({"gracia.db": ":memory:"}))


class FanOutTest(BatchCase):
    def test_a_change_with_many_dependents_is_a_few_events_of_a_hundred_and_one_statement(
        self,
    ):
        self.put(
            record("rec:org:1"),
            *[record(f"rec:work:{n}", refs=["rec:org:1"]) for n in range(1, 251)],
        )
        self.drain()
        self.events.clear()
        last = self.store.last_transaction()
        self.put(record("rec:org:1", stamp=STAMP2))
        self.assertEqual(
            self.store.pending_change_events()[0], 251
        )  # the org and the 250 pages made of it
        self.assertEqual(self.drain(100), [100, 100, 51])
        self.assertEqual([len(e.changes) for e in self.events], [100, 100, 51])
        self.assertEqual(
            sum(c.kind == "ref_update" for e in self.events for c in e.changes), 250
        )
        self.assertEqual(self.queue(last), [])
        self.assertEqual(sum(len(e.graphs) for e in self.events), 251)


class WorkerTest(BatchCase):
    def worker(self, **kw) -> ChangeEventsWorker:
        worker = ChangeEventsWorker(self.app, self.registry, poll=0.05, **kw)
        self.addCleanup(worker.stop)
        return worker

    def wait_for(self, condition, seconds: float = 10) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return True
            time.sleep(0.02)
        return False

    def test_it_handles_what_is_written_and_what_was_there_before_it_started(self):
        self.put(record("rec:org:1"))  # before
        worker = self.worker()
        worker.start()
        self.assertTrue(self.wait_for(lambda: self.events))
        self.put(record("rec:org:2"))  # after
        self.assertTrue(
            self.wait_for(
                lambda: (
                    {c.record for e in self.events for c in e.changes}
                    >= {"rec:org:1", "rec:org:2"}
                )
            )
        )
        self.assertTrue(
            self.wait_for(lambda: self.store.pending_change_events() == (0, None))
        )

    def test_a_wake_up_makes_it_look_at_once(self):
        worker = self.worker()
        worker.poll = 60
        worker.start()
        self.assertTrue(self.wait_for(lambda: not worker._first))
        self.put(record("rec:org:1"))
        worker.wake()
        self.assertTrue(self.wait_for(lambda: self.events))

    def test_without_a_file_to_watch_it_looks_every_time_and_one_process_at_a_time_handles_a_repository(
        self,
    ):
        app = open_application(self, ":memory:")
        self.assertIsNone(app.cache.file_signature())
        self.assertEqual(
            ChangeEventsWorker(app, self.registry).turn(), False
        )  # nothing to handle, nothing wrong
        # one process at a time handles a repository
        first, second = (
            self.worker(lock_dir=self.tmp / "locks"),
            self.worker(lock_dir=self.tmp / "locks"),
        )
        with first._lock("main") as held:
            self.assertTrue(held)
            with second._lock("main") as other:
                self.assertFalse(other)  # the lock of the file is the first one's
            with second._lock("other") as elsewhere:
                self.assertTrue(elsewhere)  # another repository is another lock
        with second._lock("main") as now:
            self.assertTrue(now)  # let go of when the first is done
        # a repository that another process has is looked at again
        self.put(record("rec:org:1"))
        first, second = (
            self.worker(lock_dir=self.tmp / "locks2"),
            self.worker(lock_dir=self.tmp / "locks2"),
        )
        with first._lock("main"):
            self.assertFalse(second.turn())  # the other one is on it
            self.assertIn(
                "main", second._line
            )  # and it stays in line: the other may leave rows behind
            self.assertEqual(self.store.pending_change_events()[0], 1)
        while second.turn():
            pass
        self.assertEqual(self.store.pending_change_events(), (0, None))

    def test_a_dead_thread_is_started_again_by_the_cache_and_a_stopped_worker_is_not(
        self,
    ):
        worker = self.worker()
        self.app.cache.worker = worker
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        worker._thread = dead
        self.assertFalse(worker.alive)
        self.app.cache.check()  # what every request does
        self.assertTrue(worker.alive)
        worker.stop()
        self.assertFalse(worker.alive)
        self.app.cache.check()
        self.assertFalse(worker.alive)

    def test_a_failing_handler_does_not_kill_the_thread_and_it_is_tried_again(self):
        calls = []

        def handler(event):
            calls.append(len(event.changes))
            if len(calls) < 3:
                raise RuntimeError("down")

        self.registry.registerHandler(handler, (RecordsChanged,))
        self.put(record("rec:org:1"))
        worker = self.worker()
        worker.start()
        self.assertTrue(
            self.wait_for(lambda: self.store.pending_change_events() == (0, None), 30)
        )
        self.assertGreaterEqual(len(calls), 3)
        self.assertTrue(worker.alive)


class LineTest(BatchCase):
    def setUp(self):
        super().setUp()
        self.other = open_store(self.db, repo="other", create=True)
        self.addCleanup(self.other.close)
        self.other.set_type_ids(TYPES)

    def worker(self, **kw) -> ChangeEventsWorker:
        return ChangeEventsWorker(self.app, self.registry, poll=0.05, **kw)

    def fill(self, n: int = 3) -> None:
        self.put(*[record(f"rec:work:{k}") for k in range(1, n + 1)])
        self.other.put_multi(
            self.graph_of(
                [
                    record(f"rec:work:{k}", res=f"ark:/1/other/{k}")
                    for k in range(1, n + 1)
                ]
            )
        )

    @staticmethod
    def graph_of(records):
        return ox.Dataset(q for g in records for q in g)

    def run_turns(self, worker: ChangeEventsWorker) -> int:
        turns = 0
        while worker.turn():
            turns += 1
        return turns

    def test_the_first_look_puts_every_repository_in_line_and_a_repository_that_has_nothing_leaves_it(
        self,
    ):
        worker = self.worker()
        worker._notice()
        self.assertEqual(sorted(worker._line), ["main", "other"])
        self.fill(2)
        self.run_turns(worker)
        self.assertEqual(list(worker._line), [])
        worker._notice()  # the rows the worker took off are not a write: nothing joins
        self.assertEqual(list(worker._line), [])
        self.other.put_multi(
            self.graph_of([record("rec:work:9", res="ark:/1/other/9")])
        )
        worker._notice()
        self.assertEqual(list(worker._line), ["other"])  # only the one that was written
        self.assertTrue(worker.turn())
        self.assertEqual(self.events[-1].repo, "other")

    def test_a_repository_with_a_backlog_does_not_keep_the_other_waiting(self):
        self.fill(3)
        worker = self.worker(batch=1)  # a batch is one record
        self.assertEqual(self.run_turns(worker), 6)
        self.assertEqual(
            [e.repo for e in self.events],
            ["main", "other", "main", "other", "main", "other"],
        )
        self.assertEqual(
            (self.store.pending_change_events(), self.other.pending_change_events()),
            ((0, None), (0, None)),
        )

    def test_a_repository_that_fails_waits_by_itself_and_the_pause_doubles_for_one_that_keeps_failing(
        self,
    ):
        self.fill(2)
        broken = {"main": True}

        def handler(event):
            if broken.get(event.repo):
                raise RuntimeError("the index of main is down")

        self.registry.registerHandler(handler, (RecordsChanged,))
        worker = self.worker()
        self.assertTrue(worker.turn())  # main fails, other is handled
        self.assertEqual(worker._retry["main"][1], 1)
        self.assertEqual(
            (self.store.pending_change_events()[0], self.other.pending_change_events()),
            (2, (0, None)),
        )
        self.assertFalse(
            worker.turn()
        )  # main is waiting out its pause: nothing to do now
        self.assertLess(worker._pause(), 3)  # and the wait is its, not a minute
        broken["main"] = False
        worker._retry["main"] = (0.0, 1)  # the pause is over
        self.assertTrue(worker.turn())
        self.assertEqual(self.store.pending_change_events(), (0, None))
        self.assertNotIn("main", worker._retry)
        # the pause doubles for a repository that keeps failing
        self.put(record("rec:work:50"))
        self.registry.registerHandler(lambda e: 1 / 0, (RecordsChanged,))
        worker = self.worker()
        worker.turn()
        first = worker._retry["main"]
        worker._retry["main"] = (0.0, first[1])
        worker.turn()
        self.assertEqual((first[1], worker._retry["main"][1]), (1, 2))


class WiringTest(BatchCase):
    def test_the_setting_starts_a_worker_with_the_application_and_the_subscribers_hear_it(
        self,
    ):
        from gracia.main import make_config

        seen = []
        config = make_config(
            {
                "gracia.application": self.app,
                "gracia.worker": "true",
                "gracia.db": str(self.db),
            }
        )
        config.add_subscriber(seen.append, RecordsChanged)
        app = config.make_wsgi_app()  # ApplicationCreated: the thread starts
        worker = self.app.cache.worker
        self.addCleanup(worker.stop)  # ty: ignore[missing-union-attribute]
        self.assertTrue(worker.alive)  # ty: ignore[missing-union-attribute]
        self.assertIsNotNone(app)
        self.put(record("rec:org:1"))
        worker.wake()  # ty: ignore[missing-union-attribute]
        deadline = time.monotonic() + 10
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual([c.record for c in seen[0].changes], ["rec:org:1"])

    def test_the_batch_and_the_poll_are_settings_and_without_the_setting_there_is_no_worker(
        self,
    ):
        from gracia.main import make_config

        make_config({"gracia.application": self.app}).make_wsgi_app()
        self.assertIsNone(self.app.cache.worker)
        make_config(
            {
                "gracia.application": self.app,
                "gracia.worker": "yes",
                "gracia.worker.batch": "7",
                "gracia.worker.batch_bytes": "123456",
                "gracia.worker.poll": "0.25",
                "gracia.db": str(self.db),
            }
        ).make_wsgi_app()
        worker = self.app.cache.worker
        self.addCleanup(worker.stop)  # ty: ignore[missing-union-attribute]
        self.assertEqual(
            (worker.batch, worker.batch_bytes, worker.poll),  # ty: ignore[missing-union-attribute]
            (7, 123456, 0.25),
        )
        self.assertEqual(worker.lock_dir, self.db.parent)  # ty: ignore[missing-union-attribute]
