import time
from typing import cast

import pyoxigraph as ox
from harness import open_application, open_store, stopped_worker
from test_etags import TYPES, EtagCase, record

from gracia import cache as cache_module
from gracia.cache import Cache, EtagStore, GraphStore
from gracia.graph import Graph, rdfxml
from gracia.store import new_store


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
        self.worker = stopped_worker(self.app)  # asked by a read of what is not kept
        self.cached = self.app.new_store("main")  # a store that has the cache
        self.etag_cache = cast(EtagStore, self.cache.stamped("etags"))
        self.addCleanup(self.cached.close)

    def rid(self, rec_uri: str) -> int:
        return self.store.record_id_of(rec_uri)

    def queries(self, cache: Cache | None = None) -> list[str]:
        """What is asked of the database from now on: a `cursor` of the database, and
        each `sql` statement of the store that has the cache."""
        asked = []
        database = (cache or self.cache)._database
        cursor, con = database.cursor, self.cached.con
        database.cursor = lambda: (asked.append("cursor"), cursor())[1]

        class Counting:
            def execute(_, sql, *args, **kwargs):  # noqa: N805
                asked.append("sql")
                return con.execute(sql, *args, **kwargs)

            def __getattr__(_, name):  # noqa: N805
                return getattr(con, name)

        self.cached.con = Counting()
        return asked


class EtagsTest(CacheCase):
    def test_many_record_ids_in_one_go_with_the_etag_and_the_types(self):
        ids = [self.rid(u) for u in ("rec:work:1", "rec:person:2")]
        got = self.cached.etags([*ids, 999])
        self.assertEqual(got[ids[0]], self.etag("rec:work:1"))
        self.assertEqual(got[ids[1]], self.etag("rec:person:2"))
        self.assertIsNone(got[999])
        self.assertEqual(list(got), [*ids, 999])

    def test_it_is_asked_once_and_only_for_what_is_not_there_yet(self):
        a, b = self.rid("rec:work:1"), self.rid("rec:work:2")
        self.cached.etags([a])
        asked = self.queries()
        self.cached.etags([a])
        self.assertEqual(asked.count("sql"), 0)
        self.cached.etags([a, b])
        self.assertEqual(
            asked.count("sql"), 1
        )  # one query for the one that was missing
        self.cached.etags([a, b, 999])
        self.cached.etags([999])  # a record that is not there is remembered too
        self.assertEqual(asked.count("sql"), 2)

    def test_the_files_are_not_read_while_they_do_not_change(self):
        self.cached.etags([self.rid("rec:work:1")])
        asked = self.queries()
        for _ in range(3):
            self.cached.etags([self.rid("rec:work:1")])
        self.assertEqual(asked, [])  # not even the table of the times

    def test_a_write_gives_a_new_last_modified_at_and_the_new_etag(self):
        work = self.rid("rec:work:1")
        before = self.cached.etags([work])[work]
        stamp = self.cache.repo_last_modified_at("main")
        self.put(
            record("rec:org:1", refs=["rec:org:2"], stamp="2026-06-01T00:00:00.000000Z")
        )  # something work 1 is made of
        after = self.cached.etags([work])[work]
        self.assertNotEqual(after, before)
        self.assertEqual(after, self.etag("rec:work:1"))
        self.assertGreater(self.cache.repo_last_modified_at("main"), stamp)
        # the etags of the old time are deleted, not just not read
        self.assertEqual(self.etag_cache.get_many("main", stamp, [work]), {})

    def test_a_deleted_record_is_none_after_the_write(self):
        person = self.rid("rec:person:2")
        self.assertIsNotNone(self.cached.etags([person])[person])
        self.store.delete(["rec:person:2"])
        self.assertIsNone(self.cached.etags([person])[person])

    def test_a_write_that_bypasses_the_store_is_seen(self):
        work = self.rid("rec:work:1")
        self.cached.etags([work])
        stamp = self.cache.repo_last_modified_at("main")
        self.store.con.execute(
            "UPDATE triples SET lit = 'raw' WHERE s = 'rec:person:2' AND lit = 'x'"
        )
        self.store.con.execute(
            "INSERT INTO triples VALUES ('rec:person:2', [], 'http://ex/raw', NULL, "
            "'raw', NULL, "
            "NULL)"
        )
        self.cache.check()
        self.assertGreater(self.cache.repo_last_modified_at("main"), stamp)

    def test_a_request_that_was_running_cannot_fill_the_new_last_modified_at(self):
        work = self.rid("rec:work:1")
        store = self.etag_cache
        old = self.cache.repo_last_modified_at("main")
        self.put(record("rec:person:2"), record("rec:org:1", refs=["rec:org:2"]))
        self.cache.check()
        new = self.cache.repo_last_modified_at("main")
        self.assertGreater(new, old)
        # what the late request writes: under the key it took
        store.put_many("main", old, {work: "stale"})
        self.assertEqual(store.get_many("main", new, [work]), {})
        self.assertNotEqual(self.cached.etags([work])[work], "stale")

    def test_the_etags_of_old_times_are_deleted_in_a_range(self):
        store = self.etag_cache
        for repo in ("main", "other"):
            store.put_many(repo, 100, {1: "a", 2: None})
        self.assertEqual(store.repo_stats("main", 100)["entries"], 2)
        store.sweep("main", 101)
        self.assertEqual(store.repo_stats("main", 100)["entries"], 0)
        self.assertEqual(
            store.repo_stats("other", 100)["entries"], 2
        )  # another repository is left alone
        self.assertEqual(store.get_many("other", 100, [1, 2]), {1: "a", 2: None})

    def test_a_range_delete_goes_on_over_the_batches(self):
        env = self.etag_cache._env
        with env.begin(write=True) as txn:
            for i in range(25):
                txn.put(b"k%03d" % i, b"v")
        self.assertEqual(
            cache_module.delete_range_batched(
                env, b"k005", b"k019", batch_size=4, inclusive_end=False
            ),
            14,
        )
        with env.begin() as txn:
            self.assertEqual(len(list(txn.cursor().iternext(values=False))), 11 + 0)

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
        self.cached.etags([work])
        stamp = self.cache.repo_last_modified_at("main")
        time.sleep(0.01)
        self.store.rebuild_etags()
        self.cache.check()
        self.assertGreater(self.cache.repo_last_modified_at("main"), stamp)

    def test_an_application_watches_its_file_and_a_memory_database_nothing(self):
        self.assertEqual(
            [p.name for p in self.cache._files],
            [self.db.name, self.db.name + ".wal"],
        )
        memory = open_application(self, ":memory:")
        self.assertEqual(memory.cache._files, [])


class SlugsTest(CacheCase):
    def test_a_slug_is_kept_until_the_repository_is_written_and_none_is_not_kept(self):
        for rec_type, record_uri in (("person", "rec:person:1"), ("org", "rec:org:1")):
            self.store.con.execute(
                "INSERT INTO slugs (rec_type, slug, record_uri, is_deleted) "
                f"VALUES ('{rec_type}', 'ada', '{record_uri}', false)"
            )
        person, org = self.rid("rec:person:1"), self.rid("rec:org:1")
        asked = self.queries()
        for _ in range(2):
            self.assertEqual(self.cached.slug_lookup("person", "ada"), (person, False))
        self.assertEqual(asked.count("sql"), 1)
        # the same slug of another type is an entry of its own
        self.assertEqual(self.cached.slug_lookup("org", "ada"), (org, False))
        self.assertEqual(asked.count("sql"), 2)
        for _ in range(2):  # a slug that is not there may be made next: not kept
            self.assertIsNone(self.cached.slug_lookup("person", "no"))
        self.assertEqual(asked.count("sql"), 4)
        self.put(record("rec:person:2", stamp="2026-06-01T00:00:00.000000Z"))
        self.cached.slug_lookup("person", "ada")
        self.assertEqual(asked.count("sql"), 5)  # asked again after a write


class ReferencingTest(CacheCase):
    def ids(self, *uris):
        return [self.rid(u) for u in uris]

    def test_the_records_that_point_at_a_record_newest_created_first(self):
        org = self.rid("rec:org:1")
        got = self.store.referencing_records(org, limit=10)
        self.assertEqual(
            sorted(got), sorted(self.ids("rec:person:1", "rec:work:2"))
        )  # work 1 points at person 1 only
        self.assertEqual(self.store.count_referencing_records(org), 2)

    def test_a_type_number_filters_and_offset_and_limit_page(self):
        org = self.rid("rec:org:1")
        work = dict(TYPES)["work"]
        self.assertEqual(
            self.store.referencing_records(org, record_type=work),
            self.ids("rec:work:2"),
        )
        self.assertEqual(self.store.count_referencing_records(org, record_type=work), 1)
        both = self.store.referencing_records(org)
        self.assertEqual(
            self.store.referencing_records(org, offset=1, limit=1), both[1:]
        )

    def test_what_is_not_published_is_left_out_unless_asked(self):
        self.put(record("rec:work:3", refs=["rec:org:1"], published=False))
        org = self.rid("rec:org:1")
        self.assertEqual(self.store.count_referencing_records(org), 2)
        self.assertEqual(
            self.store.count_referencing_records(org, include_non_public=True), 3
        )

    def number(self, slug: str) -> int:
        return dict(TYPES)[slug]

    def test_one_of_a_record_and_a_type_has_to_be_given(self):
        with self.assertRaises(ValueError):
            self.store.referencing_records()
        with self.assertRaises(ValueError):
            self.store.count_referencing_records(None, 0)

    def test_the_records_of_a_type_are_read_by_type_and_not_by_references(self):
        person = self.number("person")
        got = self.store.referencing_records(record_type=person)
        self.assertEqual(sorted(got), sorted(self.ids("rec:person:1", "rec:person:2")))
        self.assertEqual(self.store.count_referencing_records(record_type=person), 2)
        plan = "\n".join(
            r[1]
            for r in self.store.con.execute(
                "EXPLAIN SELECT record_id FROM etags WHERE "
                + self.store._referencing_where(None, person, False, None)
            ).fetchall()
        )
        self.assertNotIn("list_contains", plan)
        self.assertIn("record_type", plan)

    def test_deleted_records_of_a_type_are_listed_by_the_time_they_went(self):
        work = self.number("work")
        # the records that are there, with nothing deleted
        self.assertEqual(
            sorted(
                self.store.referencing_records(record_type=work, include_deleted=True)
            ),
            sorted(self.ids("rec:work:1", "rec:work:2")),
        )
        self.store.delete(["rec:work:1"])
        time.sleep(0.01)
        self.store.delete(["rec:work:2"])
        gone = self.ids("rec:work:2", "rec:work:1")  # the last one deleted first
        self.assertEqual(
            self.store.referencing_records(record_type=work, include_deleted=True), gone
        )
        self.assertEqual(
            self.store.count_referencing_records(
                record_type=work, include_deleted=True
            ),
            2,
        )
        self.assertEqual(
            self.store.referencing_records(
                record_type=work, offset=1, limit=1, include_deleted=True
            ),
            gone[1:],
        )
        self.assertEqual(
            self.store.referencing_records(record_type=work), []
        )  # not without
        # a deleted record of another type is not of this one
        self.store.delete(["rec:person:2"])
        self.assertEqual(
            self.store.referencing_records(record_type=work, include_deleted=True), gone
        )
        person = self.store.referencing_records(
            record_type=self.number("person"), include_deleted=True
        )
        self.assertEqual(person, self.ids("rec:person:2", "rec:person:1"))

    def test_only_a_type_can_be_asked_for_with_the_deleted(self):
        work, org = self.number("work"), self.rid("rec:org:1")
        for call in (
            self.store.referencing_records,
            self.store.count_referencing_records,
        ):
            with self.assertRaises(ValueError):  # no record that is pointed at
                call(org, work, include_deleted=True)
            with self.assertRaises(ValueError):
                call(org, include_deleted=True)
            with self.assertRaises(ValueError):  # a record that is gone has no state
                call(record_type=work, include_deleted=True, state="draft")

    def test_a_deleted_record_that_comes_back_is_not_listed_twice(self):
        work = self.number("work")

        def counts():
            return (
                self.store.count_referencing_records(record_type=work),
                self.store.count_referencing_records(
                    record_type=work, include_deleted=True
                ),
            )

        self.assertEqual(counts(), (2, 2))
        self.store.delete(["rec:work:1"])
        self.assertEqual(counts(), (1, 2))  # one is only a deleted one
        self.put(record("rec:work:1", refs=["rec:person:1"]))
        self.assertEqual(counts(), (2, 2))

    def test_deleted_records_are_mixed_in_with_the_records_of_the_type(self):
        cls = "http://ex/class/Person"
        self.put(
            record("rec:person:3", refs=[cls], stamp="2026-03-01T00:00:00.000000Z"),
            record("rec:person:4", refs=[cls], stamp="2026-05-01T00:00:00.000000Z"),
        )
        person = self.number("person")
        self.store.delete(["rec:person:2"])
        # by the time modified (the one that went last, now, first)
        every = self.ids("rec:person:2", "rec:person:4", "rec:person:3", "rec:person:1")
        self.assertEqual(
            self.store.referencing_records(record_type=person, include_deleted=True),
            every,
        )
        self.assertEqual(
            self.store.referencing_records(
                record_type=person, offset=2, limit=5, include_deleted=True
            ),
            every[2:],
        )

    def test_the_ids_of_deleted_records_have_no_etag(self):
        work = self.number("work")
        self.store.delete(["rec:work:1"])
        gone = self.rid("rec:work:1")
        self.assertIn(
            gone, self.store.referencing_records(record_type=work, include_deleted=True)
        )
        self.assertEqual(self.cached.etags([gone]), {gone: None})
        live = self.rid("rec:work:2")
        self.assertEqual(
            self.cached.etags([live, gone]),
            {live: self.etag("rec:work:2"), gone: None},
        )

    def test_the_state_of_a_record_is_in_records_and_etags_and_filters(self):
        self.put(
            record("rec:work:3", refs=["rec:org:1"]),
            record("rec:work:4", refs=["rec:org:1"]),
        )
        for number, state in ((3, "draft"), (4, "final")):
            self.store.con.execute(
                "INSERT INTO triples VALUES (?, [], 'https://gracia.pub/ns/state', "
                "NULL, ?, NULL, NULL)",
                [f"rec:work:{number}", state],
            )
        self.store.rebuild_etags()  # a write outside the store: the etags are made again
        for table in ("records", "etags"):
            got = dict(
                self.store.con.execute(
                    f"SELECT record_id, state FROM {table} WHERE state IS NOT NULL"
                ).fetchall()
            )
            self.assertEqual(
                got,
                {self.rid("rec:work:3"): "draft", self.rid("rec:work:4"): "final"},
                table,
            )
        org = self.rid("rec:org:1")
        self.assertEqual(
            self.store.referencing_records(org, state="draft"),
            [self.rid("rec:work:3")],
        )
        self.assertEqual(self.store.count_referencing_records(org, state="final"), 1)
        self.assertEqual(self.store.count_referencing_records(org, state="nope"), 0)
        self.assertEqual(self.store.count_referencing_records(org), 4)  # none: all

    def test_a_page_and_the_total_are_kept_until_the_repository_is_written(self):
        org = self.rid("rec:org:1")
        asked = self.queries()
        first = self.cached.referencing_records(org)
        self.assertEqual(self.cached.referencing_records(org), first)
        self.assertEqual(asked.count("sql"), 1)
        self.cached.referencing_records(org, offset=20)  # another page, another key
        self.assertEqual(asked.count("sql"), 2)
        # the total is a key of its own, the same for every page
        self.assertEqual(self.cached.count_referencing_records(org), 2)
        self.assertEqual(self.cached.count_referencing_records(org), 2)
        self.assertEqual(asked.count("sql"), 3)
        self.cached.count_referencing_records(org, record_type=7)  # another filter
        self.assertEqual(asked.count("sql"), 4)
        self.cached.count_referencing_records(org, ignore_cache=True)  # not kept
        self.cached.referencing_records(org, ignore_cache=True)
        self.assertEqual(asked.count("sql"), 6)
        self.put(
            record(
                "rec:work:3", refs=["rec:org:1"], stamp="2026-06-01T00:00:00.000000Z"
            )
        )
        self.assertEqual(self.cached.count_referencing_records(org), 3)  # after a write
        self.assertEqual(len(self.cached.referencing_records(org)), 3)


class GraphTest(CacheCase):
    @staticmethod
    def subjects(xml: str) -> set[str]:
        return {
            q.subject.value
            for q in ox.parse(xml, format=ox.RdfFormat.RDF_XML)
            if isinstance(q.subject, ox.NamedNode)
        }

    def graph(self, record_id: int, store=None) -> str:
        """The compound graph of a record as RDF/XML ("" if there is none)."""
        return (store or self.cached).get_multi(
            [record_id], compound_records=True, format="rdfxml"
        )

    def test_the_graph_is_rdfxml_of_the_record_and_everything_it_is_made_of(self):
        self.assertEqual(
            self.subjects(self.graph(self.rid("rec:work:1"))),
            {"rec:work:1", "rec:person:1", "rec:org:1"},
        )
        self.assertEqual(
            self.subjects(self.graph(self.rid("rec:person:2"))), {"rec:person:2"}
        )
        self.assertEqual(self.graph(999), "")  # a record that is not there has none

    @staticmethod
    def triples(xml: str) -> set:
        return set(ox.parse(xml, format=ox.RdfFormat.RDF_XML))

    def kept(self) -> int:
        return self.cache.graph_store.repo_stats("main")["entries"]  # ty: ignore[missing-union-attribute]

    def test_a_graph_the_cache_lacks_is_made_by_the_worker_once_per_etag(self):
        work = self.rid("rec:work:1")
        first = self.graph(work)  # from the database, and the worker is asked
        self.assertEqual(self.kept(), 0)  # the read did not keep it
        self.worker.turn()
        self.assertEqual(self.kept(), 1)  # and only what was asked for
        asked = self.queries()
        second = self.graph(work)
        self.assertEqual(asked.count("sql"), 0)  # neither the etag nor the graph
        self.assertEqual(self.triples(second), self.triples(first))
        # a write to what it is made of: from the database again, and made again
        self.put(
            record(
                "rec:org:1", refs=["rec:person:2"], stamp="2026-06-01T00:00:00.000000Z"
            )
        )
        made_of = {"rec:work:1", "rec:person:1", "rec:org:1", "rec:person:2"}
        self.assertEqual(self.subjects(self.graph(work)), made_of)
        self.worker.turn()
        self.assertEqual(self.subjects(self.graph(work)), made_of)
        # a write elsewhere keeps it: the etag is asked for again, the graph is not
        work2 = self.rid("rec:work:2")
        self.graph(work2)
        self.worker.turn()
        kept = self.graph(work2)
        self.put(
            record("rec:org:3", stamp="2026-07-01T00:00:00.000000Z")
        )  # work 2 is not made of it
        asked = self.queries()
        self.assertEqual(self.graph(work2), kept)
        self.assertEqual(asked.count("sql"), 1)

    def test_a_deleted_record_has_no_graph_any_more(self):
        person = self.rid("rec:person:2")
        self.assertTrue(self.graph(person))
        self.store.delete(["rec:person:2"])
        self.assertEqual(self.graph(person), "")

    def test_without_a_directory_the_graph_is_made_every_time(self):
        cache = Cache(self.app.database, self.app.registry)
        store = new_store(self.app.database, "main", cache=cache)
        self.addCleanup(store.close)
        work = self.rid("rec:work:1")
        self.assertEqual(
            self.subjects(self.graph(work, store)), self.subjects(self.graph(work))
        )
        self.assertIsNone(cache.graph_store)

    def test_the_graphs_of_repositories_are_apart(self):
        other = open_store(self.db, repo="other", create=True)
        self.addCleanup(other.close)
        other.set_type_ids(TYPES)
        other.put_multi(record("rec:org:1", refs=["rec:org:2"], res="ark:/1/other/1"))
        org = self.rid("rec:org:1")
        cached = self.app.new_store("other")
        self.addCleanup(cached.close)
        self.assertNotEqual(self.graph(org), self.graph(org, cached))

    def test_the_compound_graphs_from_the_database_are_what_the_cache_keeps(self):
        work = self.rid("rec:work:1")
        etag, text = self.store.fetch_compound_graphs([work])[work]
        self.assertEqual(etag, self.etag("rec:work:1"))
        sources = dict(
            self.store.con.execute(
                "SELECT record_uri(record_id), source FROM records"
            ).fetchall()
        )
        self.assertEqual(  # the record first, then what it is made of
            text,
            "\n".join(sources[u] for u in ("rec:work:1", "rec:person:1", "rec:org:1")),
        )
        self.assertEqual(self.store.fetch_compound_graphs([999]), {})
        self.assertEqual(  # the same triples: the labels of the blank nodes are the same
            set(ox.parse(self.graph(work), format=ox.RdfFormat.RDF_XML)),
            set(ox.parse(rdfxml(text), format=ox.RdfFormat.RDF_XML)),
        )

    def test_the_labels_of_the_sources_are_valid_nodeids_and_the_graphs_merge(self):
        xml = self.graph(
            self.rid("rec:work:1")
        )  # not canonicalized: as the database made
        labels = {
            t.value
            for q in ox.parse(xml, format=ox.RdfFormat.RDF_XML)  # which RDF/XML accepts
            for t in (q.subject, q.object)
            if isinstance(t, ox.BlankNode)
        }
        self.assertTrue(labels)
        self.assertTrue(all(label.startswith("b") for label in labels))
        self.assertIn('nodeID="b', xml)
        # so the graphs of two records merge without duplicates: person 1's graph adds
        # nothing to work 1's
        both = self.cached.get_multi(
            [self.rid("rec:work:1"), self.rid("rec:person:1")], compound_records=True
        )
        self.assertEqual(
            len(both), len(self.cached.get_multi([self.rid("rec:work:1")], True))
        )


class GetMultiTest(CacheCase):
    def test_a_record_by_uri_or_by_id_is_its_own_triples_only(self):
        work = self.rid("rec:work:1")
        graph = self.cached.get_multi(["rec:work:1", work, ox.NamedNode("rec:org:1")])
        self.assertEqual(
            {q.subject.value for q in graph if isinstance(q.subject, ox.NamedNode)},
            {
                "rec:work:1",
                "rec:org:1",
            },  # nothing it is made of; what is not there: nothing
        )
        self.assertEqual(
            set(self.cached.get("rec:work:1")), set(self.cached.get_multi([work]))
        )
        self.assertEqual(len(self.cached.get_multi([999, "rec:nothing:1"])), 0)
        self.assertEqual(len(self.cached.get_multi([])), 0)

    def test_the_compound_records_of_many_are_one_graph_whatever_the_source(self):
        ids = [self.rid(u) for u in ("rec:work:1", "rec:work:2")]
        want = self.store.get_multi(
            ids, compound_records=True
        )  # no cache: the database
        got = self.cached.get_multi(ids, compound_records=True)  # not kept yet
        self.assertEqual(set(got), set(want))
        self.worker.turn()
        self.assertEqual(self.cache.graph_store.repo_stats("main")["entries"], 2)  # ty: ignore[missing-union-attribute]
        got = self.cached.get_multi(ids, compound_records=True)  # kept
        self.assertEqual(set(got), set(want))
        asked = self.queries()
        got = self.cached.get_multi(ids, compound_records=True, ignore_cache=True)
        self.assertEqual(set(got), set(want))
        self.assertGreater(asked.count("sql"), 0)  # not from the cache

    def test_the_formats(self):
        work = self.rid("rec:work:1")
        graph = self.cached.get_multi([work], compound_records=True)
        for fmt in ("rdfxml", "ntriples", "turtle"):
            text = self.cached.get_multi([work], compound_records=True, format=fmt)
            self.assertIsInstance(text, str, fmt)
            self.assertEqual(len(Graph.parse(text, fmt)), len(graph), fmt)
        self.worker.turn()  # now it is kept
        kept = self.cache.graph_store.get("main", work, self.etag("rec:work:1"))  # ty: ignore[missing-union-attribute]
        self.assertEqual(  # one that is kept passes through as it is
            self.cached.get_multi([work], compound_records=True, format="rdfxml"),
            kept.decode(),  # ty: ignore[missing-union-attribute]
        )
        self.assertEqual(self.cached.get_multi([999], format="rdfxml"), "")

    def test_the_labels_and_slugs_of_the_records_in_a_graph(self):
        graph = self.cached.get_multi(["rec:work:1", "rec:org:1"])
        self.assertEqual(
            graph.labels(), {}
        )  # the records of these tests have neither a label nor a slug
        text = (
            '<rec:person:9> <https://gracia.pub/ns/label> "Ada" ; '
            '<https://gracia.pub/ns/slug> "ada" .\n'
            '<rec:person:8> <https://gracia.pub/ns/label> "Bob" .\n'
            '<http://ex/x> <https://gracia.pub/ns/label> "Not a record" .'
        )
        self.assertEqual(
            Graph.parse(text, "turtle").labels(),
            {"rec:person:9": ("Ada", "ada"), "rec:person:8": ("Bob", None)},
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


class SizesTest(CacheCase):
    def test_the_sizes_are_the_defaults_and_what_the_config_sets(self):
        from gracia import config

        self.assertEqual(config.parse_size("256M"), 256 * 1024**2)
        self.assertEqual(config.parse_size("1g"), 1024**3)
        self.assertEqual(config.parse_size("512MB"), 512 * 1024**2)
        self.assertEqual(config.parse_size("65536"), 65536)
        for wrong in ("", "big", "0", "-1M", "1.5.2G"):
            with self.assertRaises(ValueError, msg=wrong):
                config.parse_size(wrong)
        with self.assertRaisesRegex(ValueError, "gracia.cache.etags_size"):
            config.cache_sizes({"gracia.cache.etags_size": "lots"})
        sizes = config.cache_sizes({"gracia.cache.etags_size": "8M"})
        self.assertEqual(sizes, {"etags": 8 * 1024**2})
        cache = Cache(self.app.database, self.app.registry, sizes=sizes)
        self.addCleanup(cache.close)
        self.assertEqual(cache._sizes["etags"], 8 * 1024**2)
        self.assertEqual(cache._sizes["slugs"], cache_module.SLUG_CACHE_BYTES)
        self.assertEqual(cache.stamped("etags")._env.info()["map_size"], 8 * 1024**2)
