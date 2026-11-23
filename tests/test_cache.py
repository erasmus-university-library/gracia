import time
from unittest import mock

import pyoxigraph as ox
from harness import open_application, open_store
from test_etags import TYPES, EtagCase, record

from gracia import cache as cache_module
from gracia.cache import Cache, GraphStore, rdfxml


class CacheCase(EtagCase):
    def setUp(self):
        super().setUp()
        self.put(
            record("rec:org:1"),
            record("rec:person:1", refs=["rec:org:1"]),
            record("rec:person:2"),
            record("rec:work:1", refs=["rec:person:1"]),
            record("rec:work:2", refs=["rec:org:1"]),
        )
        self.app = open_application(self, self.db, cache_dir=self.tmp / "cache")
        self.cache = self.app.cache

    def rid(self, rec_uri: str) -> int:
        return self.store.record_id_of(rec_uri)

    def queries(self, cache: Cache | None = None) -> list[str]:
        asked = []

        def counting(name, real):
            return lambda *a, **k: (asked.append(name), real(*a, **k))[1]

        database = (cache or self.cache)._database
        database.cursor = counting("cursor", database.cursor)
        patcher = mock.patch.object(
            cache_module, "new_store", counting("new_store", cache_module.new_store)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return asked


class EtagsTest(CacheCase):
    def test_many_record_ids_in_one_go_with_the_etag_and_the_types(self):
        ids = [self.rid(u) for u in ("rec:work:1", "rec:person:2")]
        got = self.cache.etags("main", [*ids, 999])
        self.assertEqual(
            got[ids[0]], (self.etag("rec:work:1"), ("org", "person", "work"))
        )
        self.assertEqual(got[ids[1]], (self.etag("rec:person:2"), ("person",)))
        self.assertIsNone(got[999])
        self.assertEqual(list(got), [*ids, 999])

    def test_it_is_asked_once_and_only_for_what_is_not_there_yet(self):
        a, b = self.rid("rec:work:1"), self.rid("rec:work:2")
        self.cache.etags("main", [a])
        asked = self.queries()
        self.cache.etags("main", [a])
        self.assertEqual(asked.count("new_store"), 0)
        self.cache.etags("main", [a, b])
        self.assertEqual(
            asked.count("new_store"), 1
        )  # one query for the one that was missing
        self.cache.etags("main", [a, b, 999])
        self.cache.etags("main", [999])  # a record that is not there is remembered too
        self.assertEqual(asked.count("new_store"), 2)

    def test_the_files_are_not_read_while_they_do_not_change(self):
        self.cache.etags("main", [self.rid("rec:work:1")])
        asked = self.queries()
        for _ in range(3):
            self.cache.etags("main", [self.rid("rec:work:1")])
        self.assertEqual(asked, [])  # not even the table of the times

    def test_a_write_gives_a_new_generation_and_the_new_etag(self):
        work = self.rid("rec:work:1")
        before = self.cache.etags("main", [work])[work]
        generation = self.cache._table("main", "etags")
        self.put(
            record("rec:org:1", refs=["rec:org:2"], stamp="2026-06-01T00:00:00.000000Z")
        )  # something work 1 is made of
        after = self.cache.etags("main", [work])[work]
        self.assertNotEqual(after[0], before[0])  # ty: ignore[not-subscriptable]
        self.assertEqual(after, (self.etag("rec:work:1"), ("org", "person", "work")))
        self.assertIsNot(self.cache._table("main", "etags"), generation)
        self.assertEqual(generation[work], before)  # the old generation is as it was

    def test_a_deleted_record_is_none_after_the_write(self):
        person = self.rid("rec:person:2")
        self.assertIsNotNone(self.cache.etags("main", [person])[person])
        self.store.delete(["rec:person:2"])
        self.assertIsNone(self.cache.etags("main", [person])[person])

    def test_a_write_that_bypasses_the_store_is_seen(self):
        work = self.rid("rec:work:1")
        self.cache.etags("main", [work])
        generation = self.cache._table("main", "etags")
        self.store.con.execute(
            "UPDATE triples SET lit = 'raw' WHERE s = 'rec:person:2' AND lit = 'x'"
        )
        self.store.con.execute(
            "INSERT INTO triples VALUES ('rec:person:2', [], 'http://ex/raw', NULL, "
            "'raw', NULL, "
            "NULL)"
        )
        self.cache.check()
        self.assertIsNot(self.cache._table("main", "etags"), generation)

    def test_a_request_that_was_running_cannot_fill_the_new_generation(self):
        work = self.rid("rec:work:1")
        old = self.cache._table("main", "etags")
        self.put(record("rec:person:2"), record("rec:org:1", refs=["rec:org:2"]))
        self.cache.check()
        new = self.cache._table("main", "etags")
        old[work] = (
            "stale",
            (),
        )  # what the late request writes: into the generation it took
        self.assertNotIn(work, new)
        self.assertNotEqual(self.cache.etags("main", [work])[work][0], "stale")  # ty: ignore[not-subscriptable]

    def test_without_a_file_the_table_is_read_every_time(self):
        app = open_application(self, ":memory:")
        cache = app.cache
        self.assertEqual(cache._files, [])
        asked = self.queries(cache)
        cache.check()
        cache.check()
        self.assertEqual(asked.count("cursor"), 2)

    def test_a_rebuild_of_the_etags_is_a_write_for_the_cache(self):
        work = self.rid("rec:work:1")
        self.cache.etags("main", [work])
        generation = self.cache._table("main", "etags")
        time.sleep(0.01)
        self.store.rebuild_etags()
        self.cache.check()
        self.assertIsNot(self.cache._table("main", "etags"), generation)

    def test_an_application_watches_its_file_and_a_memory_database_nothing(self):
        self.assertEqual(
            [p.name for p in self.cache._files],
            [self.db.name, self.db.name + ".wal"],
        )
        memory = open_application(self, ":memory:")
        self.assertEqual(memory.cache._files, [])


class GraphTest(CacheCase):
    @staticmethod
    def subjects(xml: bytes) -> set[str]:
        return {
            q.subject.value
            for q in ox.parse(xml, format=ox.RdfFormat.RDF_XML)
            if isinstance(q.subject, ox.NamedNode)
        }

    def test_the_graph_is_rdfxml_of_the_record_and_everything_it_is_made_of(self):
        xml = self.cache.graph("main", self.rid("rec:work:1"))
        self.assertEqual(
            self.subjects(xml),  # ty: ignore[invalid-argument-type]
            {"rec:work:1", "rec:person:1", "rec:org:1"},
        )
        self.assertEqual(
            self.subjects(self.cache.graph("main", self.rid("rec:person:2"))),  # ty: ignore[invalid-argument-type]
            {"rec:person:2"},
        )

    def test_a_record_that_is_not_there_has_no_graph(self):
        self.assertIsNone(self.cache.graph("main", 999))

    def test_it_is_made_once_per_etag(self):
        work = self.rid("rec:work:1")
        first = self.cache.graph("main", work)
        asked = self.queries()
        self.assertEqual(self.cache.graph("main", work), first)
        self.assertEqual(
            asked.count("new_store"), 0
        )  # neither the etag nor the graph was asked for

    def test_a_write_to_what_the_record_is_made_of_makes_the_graph_again(self):
        work = self.rid("rec:work:1")
        first = self.cache.graph("main", work)
        self.put(
            record(
                "rec:org:1", refs=["rec:person:2"], stamp="2026-06-01T00:00:00.000000Z"
            )
        )
        second = self.cache.graph("main", work)
        self.assertNotEqual(second, first)
        self.assertEqual(
            self.subjects(second),  # ty: ignore[invalid-argument-type]
            {"rec:work:1", "rec:person:1", "rec:org:1", "rec:person:2"},
        )

    def test_a_write_elsewhere_keeps_the_graph(self):
        work = self.rid("rec:work:2")
        first = self.cache.graph("main", work)
        self.put(
            record(
                "rec:person:2", refs=["rec:org:2"], stamp="2026-06-01T00:00:00.000000Z"
            )
        )  # work 2 is not made of it
        asked = self.queries()
        self.assertEqual(self.cache.graph("main", work), first)
        self.assertEqual(
            asked.count("new_store"), 1
        )  # the etag was asked for again, the graph was not

    def test_a_deleted_record_has_no_graph_any_more(self):
        person = self.rid("rec:person:2")
        self.assertIsNotNone(self.cache.graph("main", person))
        self.store.delete(["rec:person:2"])
        self.assertIsNone(self.cache.graph("main", person))

    def test_without_a_directory_the_graph_is_made_every_time(self):
        cache = Cache(self.app.database, self.app.registry)
        work = self.rid("rec:work:1")
        self.assertEqual(
            self.subjects(cache.graph("main", work)),  # ty: ignore[invalid-argument-type]
            self.subjects(cache.graph("main", work)),  # ty: ignore[invalid-argument-type]
        )
        self.assertIsNone(cache._graph_store())

    def test_the_graphs_of_repositories_are_apart(self):
        other = open_store(self.db, repo="other", create=True)
        self.addCleanup(other.close)
        other.set_type_ids(TYPES)
        other.put(record("rec:org:1", refs=["rec:org:2"], res="ark:/1/other/1"))
        org = self.rid("rec:org:1")
        self.assertNotEqual(
            self.cache.graph("main", org), self.cache.graph("other", org)
        )


class GraphStoreTest(CacheCase):
    def test_an_entry_is_used_for_its_etag_only(self):
        graphs = GraphStore(self.tmp / "g")
        self.addCleanup(graphs.close)
        graphs.put("main", 7, "0123456789abcdef", b"<xml/>")
        self.assertEqual(graphs.get("main", 7, "0123456789abcdef"), b"<xml/>")
        self.assertIsNone(graphs.get("main", 7, "fedcba9876543210"))
        self.assertIsNone(graphs.get("other", 7, "0123456789abcdef"))
        graphs.put("main", 7, "fedcba9876543210", b"<new/>")  # replaces
        self.assertIsNone(graphs.get("main", 7, "0123456789abcdef"))
        self.assertEqual(graphs.get("main", 7, "fedcba9876543210"), b"<new/>")

    def test_many_entries_are_one_transaction_and_a_batch_that_does_not_fit_is_an_error(
        self,
    ):
        graphs = GraphStore(self.tmp / "many")
        self.addCleanup(graphs.close)
        etag = "0123456789abcdef"
        graphs.put_many(("main", n, etag, f"<x{n}/>".encode()) for n in range(1, 5001))
        self.assertEqual(graphs.get("main", 1, etag), b"<x1/>")
        self.assertEqual(graphs.get("main", 5000, etag), b"<x5000/>")
        tiny = GraphStore(self.tmp / "tiny", size=256 * 1024)
        self.addCleanup(tiny.close)
        with self.assertRaisesRegex(ValueError, "do not fit"):
            tiny.put_many(
                ("main", n, etag, b"x" * 20_000) for n in range(1, 40)
            )  # 780 KB in one batch

    def test_a_full_map_starts_over_and_keeps_the_new_entry(self):
        graphs = GraphStore(self.tmp / "small", size=256 * 1024)
        self.addCleanup(graphs.close)
        etag = "0123456789abcdef"
        for n in range(1, 40):
            graphs.put(
                "main", n, etag, b"x" * 20_000
            )  # about 780 KB in a map of 256 KB
        self.assertEqual(graphs.get("main", 39, etag), b"x" * 20_000)
        self.assertIsNone(graphs.get("main", 1, etag))

    def test_the_labels_of_the_sources_are_valid_nodeids_and_are_kept(self):
        _, text = self.store.closure_source(self.rid("rec:work:1"))
        xml = rdfxml(
            text
        )  # not canonicalized: the labels are the ones the database made
        quads = list(
            ox.parse(xml, format=ox.RdfFormat.RDF_XML)
        )  # and RDF/XML accepts them
        labels = {
            t.value
            for q in quads
            for t in (q.subject, q.object)
            if isinstance(t, ox.BlankNode)
        }
        self.assertTrue(labels)
        self.assertTrue(all(label.startswith("b") for label in labels))
        self.assertIn(b'nodeID="b', xml)

    def test_the_graphs_of_two_records_merge_without_duplicates(self):
        graphs = [
            ox.Dataset(
                ox.parse(
                    self.cache.graph("main", self.rid(u)), format=ox.RdfFormat.RDF_XML
                )
            )
            for u in ("rec:work:1", "rec:person:1")
        ]
        merged = ox.Dataset()
        for graph in graphs:
            for quad in graph:
                merged.add(quad)
        self.assertEqual(
            len(merged), len(graphs[0])
        )  # person 1's graph adds nothing to work 1's
        self.assertLess(len(merged), len(graphs[0]) + len(graphs[1]))


class ClosureSourceTest(CacheCase):
    def test_the_etag_and_the_sources_of_the_record_first_and_then_what_it_is_made_of(
        self,
    ):
        etag, text = self.store.closure_source(self.rid("rec:work:1"))
        self.assertEqual(etag, self.etag("rec:work:1"))
        sources = dict(
            self.store.con.execute(
                "SELECT record_uri(record_id), source FROM records"
            ).fetchall()
        )
        self.assertEqual(
            text,
            "\n".join(sources[u] for u in ("rec:work:1", "rec:person:1", "rec:org:1")),
        )
        self.assertIsNone(self.store.closure_source(999))
