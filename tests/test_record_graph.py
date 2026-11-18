"""`record_graph()`: the rows of `etags` with the record and everything its page is made of as one TriG graph."""

import pyoxigraph as ox
from test_etags import EtagCase, record

from gracia.store import Store

RG = "SELECT record_id, record_uri, record_type, etag, last_modified_at, graph FROM record_graph()"


class RecordGraphTest(EtagCase):
    def setUp(self):
        super().setUp()
        # work 1 -> person 1 -> org 1; work 2 -> org 1; person 2 stands alone
        self.put(
            record("rec:org:1"),
            record("rec:person:1", refs=["rec:org:1"]),
            record("rec:person:2"),
            record("rec:work:1", refs=["rec:person:1"]),
            record("rec:work:2", refs=["rec:org:1"]),
        )

    def graphs(self, where: str = "true") -> dict[str, str]:
        return dict(self.store.con.execute(f"SELECT record_uri, graph FROM record_graph() WHERE {where}").fetchall())

    @staticmethod
    def subjects(trig: str) -> set[str]:
        return {q.subject.value for q in ox.parse(trig.encode(), format=ox.RdfFormat.TRIG) if isinstance(q.subject, ox.NamedNode)}

    def test_a_row_for_every_etag_with_the_columns_of_the_etag(self):
        rows = self.store.con.execute(RG + " ORDER BY record_id").fetchall()
        etags = self.store.con.execute(
            "SELECT record_id, record_uri, record_type, etag, last_modified_at FROM etags ORDER BY record_id"
        ).fetchall()
        self.assertEqual([r[:5] for r in rows], etags)
        self.assertEqual(len(rows), 5)

    def test_the_graph_is_trig_named_by_the_record_and_holds_the_record_and_all_it_is_made_of(self):
        graphs = self.graphs()
        for rec_uri, trig in graphs.items():
            self.assertTrue(trig.startswith(f"<{rec_uri}> {{\n"), trig[:40])
            self.assertTrue(trig.endswith("\n}"))
            self.assertEqual({str(q.graph_name) for q in ox.parse(trig.encode(), format=ox.RdfFormat.TRIG)}, {f"<{rec_uri}>"})
        self.assertEqual(self.subjects(graphs["rec:work:1"]), {"rec:work:1", "rec:person:1", "rec:org:1"})  # deep
        self.assertEqual(self.subjects(graphs["rec:person:1"]), {"rec:person:1", "rec:org:1"})
        self.assertEqual(self.subjects(graphs["rec:work:2"]), {"rec:work:2", "rec:org:1"})
        self.assertEqual(self.subjects(graphs["rec:org:1"]), {"rec:org:1"})  # nothing to point at
        self.assertEqual(self.subjects(graphs["rec:person:2"]), {"rec:person:2"})

    def test_the_graph_is_the_sources_of_the_records_joined_by_a_newline_the_record_first(self):
        sources = dict(self.store.con.execute("SELECT record_uri(record_id), source FROM records").fetchall())
        got = self.graphs("record_uri = 'rec:work:1'")["rec:work:1"]
        # the blank nodes are the stored ones (labelled per record), so the text is those sources
        self.assertEqual(got, "<rec:work:1> {\n" + "\n".join(sources[u] for u in ("rec:work:1", "rec:person:1", "rec:org:1")) + "\n}")

    def test_the_graph_has_the_triples_of_the_sources_and_no_others(self):
        quads = list(ox.parse(self.graphs()["rec:person:1"].encode(), format=ox.RdfFormat.TRIG))
        lines = self.store.con.execute(
            "SELECT sum(len(string_split(trim(source, chr(10)), chr(10)))) FROM records "
            "WHERE record_uri(record_id) IN ('rec:person:1', 'rec:org:1')").fetchone()[0]
        self.assertEqual(len(quads), lines)  # a line a triple, none twice

    def test_it_is_filtered_like_a_view(self):
        self.assertEqual(set(self.graphs("record_type = 'work'")), {"rec:work:1", "rec:work:2"})
        rid = self.store.record_id_of("rec:work:2")
        self.assertEqual(set(self.graphs(f"record_id = {rid}")), {"rec:work:2"})
        self.assertEqual(self.graphs("record_id = -1"), {})
        newest = self.store.con.execute("SELECT max(last_modified_at) FROM etags").fetchone()[0]
        self.assertEqual(set(self.graphs(f"last_modified_at > TIMESTAMP '{newest}'")), set())
        self.assertEqual(len(self.graphs(f"last_modified_at <= TIMESTAMP '{newest}'")), 5)

    def test_it_follows_the_writes(self):
        before = self.graphs()["rec:work:1"]
        self.put(record("rec:person:1", refs=["rec:org:1", "rec:person:2"]))  # the table etags is made again
        self.assertEqual(self.subjects(self.graphs()["rec:work:1"]), {"rec:work:1", "rec:person:1", "rec:org:1", "rec:person:2"})
        self.store.delete(["rec:org:1"])
        self.assertEqual(self.subjects(self.graphs()["rec:work:1"]), {"rec:work:1", "rec:person:1", "rec:person:2"})
        self.assertNotEqual(self.graphs()["rec:work:1"], before)
        self.assertNotIn("rec:org:1", self.graphs())

    def test_a_cycle_has_each_record_once(self):
        self.put(record("rec:person:1", refs=["rec:org:1", "rec:work:1"]))  # work 1 -> person 1 -> work 1
        trig = self.graphs()["rec:work:1"]
        self.assertEqual(self.subjects(trig), {"rec:work:1", "rec:person:1", "rec:org:1"})
        self.assertEqual(trig.count("<rec:person:1> <https://gracia.pub/ns/resource_uri>"), 1)

    def test_every_repository_reads_its_own_records(self):
        other = Store(self.db, repo="other", create=True)
        self.addCleanup(other.close)
        other.set_type_ids([("work", 6)])
        other.put(record("rec:work:1"))
        self.assertEqual(self.subjects(other.con.execute("SELECT graph FROM record_graph()").fetchone()[0]), {"rec:work:1"})
        self.assertEqual(len(self.graphs()), 5)

    def test_an_iri_in_the_name_is_encoded_like_the_sources(self):
        self.assertEqual(
            self.store.con.execute("SELECT encode_ntriples_uri(record_uri) FROM etags WHERE record_uri = 'rec:org:1'").fetchone()[0],
            "<rec:org:1>")
