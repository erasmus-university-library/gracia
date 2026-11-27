import pyoxigraph as ox
from base import DbTestCase
from harness import fake_type, open_store

from gracia import uri
from gracia.graph import Graph
from gracia.store import DuplicateValue, fetch_one

G = "https://gracia.pub/ns/"
PERSON = f"""@prefix g: <{G}> . @prefix d: <http://purl.org/dc/terms/> .
@prefix e: <http://ex/> .
<rec:person:1> a g:Record ; g:identifier "ID-1", <http://ex/id2> ;
    g:resource_uri <ark:/1/2> ; g:label "Ada <x>" ;
    g:resource [ e:name "Ada \\"L\\"\\nline"@en ; e:age 5 ; e:l ( "a"@en "b" ) ;
        e:link <http://ex/id2> ] .
<rec:person:2> g:label "Bob" .
"""


def graph(text: str) -> Graph:
    return Graph(ox.Dataset(ox.parse(text.encode(), format=ox.RdfFormat.TURTLE)))


class RecordsTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.store = open_store(self.db)
        self.addCleanup(self.store.close)
        self.store.set_property_roles(
            [
                ("person", "key_info", G + "identifier", 0),
                ("person", "description", "http://ex/name", 0),
                ("user", "unique", G + "userid", 0),
            ]
        )
        self.store.put_multi(graph(PERSON))

    def row(self, uri: str) -> dict:
        con = self.store.con
        found = con.execute(
            "SELECT * FROM records WHERE record_id = record_id(?)", [uri]
        ).fetchone()
        names = [d[0] for d in con.description]
        return dict(zip(names, found, strict=False)) if found else {}

    def test_a_record_row_has_its_columns_and_a_source_in_ntriples_that_is_the_same_every_time_and_a_subject_that_is_not_a_record_is_refused(
        self,
    ):
        with self.assertRaises(ValueError):
            self.store.put_multi(graph('<http://example.org/x> <http://ex/p> "x" .'))
        count = fetch_one(self.store.con.execute("SELECT count(*) FROM records"))[0]
        self.assertEqual(count, 2)

        r = self.row("rec:person:1")
        self.assertEqual(
            (r["rec_type"], r["record_id"], r["res_uri"]),
            ("person", uri.record_id(5, 1), "ark:/1/2"),
        )
        self.assertEqual(r["label"], "Ada <x>")
        self.assertEqual(sorted(r["identifiers"]), ["ID-1", "http://ex/id2"])
        self.assertEqual(
            r["references"], ["http://ex/id2"]
        )  # what the resource points at: the rest of the record is not a reference
        self.assertEqual(
            r["text"], 'Ada "L"\nline'
        )  # the literals of the properties with the description role

        source = self.row("rec:person:1")["source"]
        parsed = Graph(
            ox.Dataset(ox.parse(source.encode(), format=ox.RdfFormat.N_TRIPLES))
        )
        self.assertEqual(len(parsed), 14)
        labels = set()
        for quad in parsed:
            for term in (quad.subject, quad.object):
                if isinstance(term, ox.BlankNode):
                    # b<record id>_<n>: valid as an rdf:nodeID, and unique across
                    # records
                    self.assertRegex(
                        term.value, rf"^b{uri.record_id(5, 1)}_\d+(_c\d+)?$"
                    )
                    labels.add(term.value)
        self.assertGreater(len(labels), 1)
        self.assertIn('"Ada \\"L\\"\\nline"@en', source)
        self.assertIn("<http://www.w3.org/2001/XMLSchema#integer>", source)

        first = self.row("rec:person:1")["source"]
        self.store.con.execute(
            "UPDATE triples SET lit = lit WHERE s = 'rec:person:1'"
        )  # made again
        self.assertEqual(self.row("rec:person:1")["source"], first)

    def test_a_changed_record_is_rewritten_whole_and_a_deleted_one_is_in_the_history_and_can_come_back(
        self,
    ):
        before = self.row("rec:person:1")
        self.store.put_multi(graph('<rec:person:1> <http://ex/p> "changed"@nl .'))
        after = self.row("rec:person:1")
        self.assertEqual(
            after["source"], '<rec:person:1> <http://ex/p> "changed"@nl .\n'
        )
        self.assertEqual(
            (after["label"], after["identifiers"], after["text"]), (None, [], None)
        )
        self.assertGreater(after["modified_at"], before["modified_at"])

        self.store.delete(["rec:person:2"])
        self.assertEqual(self.row("rec:person:2"), {})
        self.assertEqual(self.store.count_records_by_type(), {"person": 1})
        last = self.store.con.execute(
            "SELECT is_deleted, source FROM history WHERE record_id = "
            "record_id('rec:person:2') "
            "ORDER BY txn_id DESC"
        ).fetchone()
        self.assertEqual(last, (True, ""))
        self.store.put_multi(
            graph('<rec:person:2> <http://ex/p> "back"@en .')
        )  # and comes back
        self.assertEqual(self.row("rec:person:2")["text"], None)
        self.assertIn('"back"@en', self.row("rec:person:2")["source"])

    def test_a_partial_change_of_the_rows_updates_the_row_and_sort_keeps_the_triggers_the_rows_and_list_tables(
        self,
    ):
        self.store.con.execute(
            "DELETE FROM triples WHERE s = 'rec:person:1' AND p = ?", [G + "label"]
        )
        r = self.row("rec:person:1")
        self.assertIsNone(r["label"])
        self.assertNotIn("label", r["source"])
        self.store.con.execute(
            "UPDATE triples SET lit = 'Ada B' WHERE s = 'rec:person:2'"
        )
        self.assertEqual(self.row("rec:person:2")["label"], "Ada B")
        self.assertIn('"Ada B"', self.row("rec:person:2")["source"])

        before = self.row("rec:person:1")["source"]
        self.store.sort()
        self.assertEqual(self.row("rec:person:1")["source"], before)
        triggers = fetch_one(
            self.store.con.execute("SELECT count(*) FROM duckdb_triggers()")
        )[0]
        self.assertEqual(triggers, 69)  # 23 steps for each of insert, update and delete

        self.store.sort()
        self.assertEqual(
            self.store.records_with("identifiers", "ID-1"), ["rec:person:1"]
        )
        self.store.put_multi(graph(f'<rec:person:3> <{G}identifier> "ID-3" .'))
        self.assertEqual(
            self.store.records_with("identifiers", "ID-3"), ["rec:person:3"]
        )

    def test_the_list_tables_follow_every_change(self):
        s = self.store
        self.assertEqual(s.records_with("identifiers", "ID-1"), ["rec:person:1"])
        self.assertEqual(
            s.records_with("references", "http://ex/id2"), ["rec:person:1"]
        )
        self.assertEqual(
            s.records_with("references", "ark:/1/2"), []
        )  # its own resource uri is not a reference
        s.put_multi(
            graph(
                f'<rec:person:2> <{G}identifier> "ID-1" ; <{G}resource> [ '
                "<http://ex/link> "
                "<http://ex/id2> ] ."
            )
        )  # a second record
        self.assertEqual(
            s.records_with("identifiers", "ID-1"), ["rec:person:1", "rec:person:2"]
        )
        s.put_multi(
            graph('<rec:person:1> <http://ex/p> "changed"@nl .')
        )  # replaced: old values gone
        self.assertEqual(s.records_with("identifiers", "ID-1"), ["rec:person:2"])
        self.assertEqual(
            s.records_with("references", "http://ex/id2"), ["rec:person:2"]
        )
        s.con.execute(
            "UPDATE triples SET lit = 'ID-2' WHERE s = 'rec:person:2' AND lit = 'ID-1'"
        )
        self.assertEqual(s.records_with("identifiers", "ID-1"), [])
        self.assertEqual(s.records_with("identifiers", "ID-2"), ["rec:person:2"])
        s.delete(["rec:person:2"])
        self.assertEqual(s.records_with("identifiers", "ID-2"), [])
        self.assertEqual(s.records_with("references", "http://ex/id2"), [])
        for table in ("records_identifiers", "records_references"):
            self.assertEqual(
                fetch_one(
                    s.con.execute(
                        f"SELECT count(*) FROM {table} WHERE record_id = "
                        "record_id('rec:person:2')"
                    )
                )[0],
                0,
            )

    def test_the_export_in_chunks_writes_whole_records_in_files_made_at_once(self):
        text = "".join(
            f'<rec:big:{i}> <http://ex/p> "value {i}" .\n' for i in range(1, 5001)
        )
        self.store.put_multi(graph(text))
        directory = self.tmp / "chunks"
        count, files = self.store.export_chunks(directory, "big", chunk_bytes=20_000)
        self.assertEqual(count, 5000)
        self.assertGreater(len(files), 1)  # DuckDB cuts after every 2048 rows
        self.assertEqual(
            sorted(p.name for p in directory.iterdir()), [p.name for p in files]
        )
        self.assertFalse((self.tmp / "chunks.writing").exists())
        subjects = []
        for path in files:
            body = path.read_text()
            self.assertTrue(body.endswith("\n\n"))  # a file ends with a whole record
            subjects += [line.split(" ", 1)[0] for line in body.splitlines() if line]
        self.assertEqual(
            sorted(subjects), sorted(f"<rec:big:{i}>" for i in range(1, 5001))
        )
        count, files = self.store.export_chunks(self.tmp / "one", "big", chunk_bytes=0)
        self.assertEqual((count, len(files)), (5000, 1))
        self.assertEqual(
            self.store.export_chunks(self.tmp / "none", "nobody", 20_000), (0, [])
        )

    def test_changing_the_roles_makes_the_records_again_and_a_unique_that_has_duplicates_changes_nothing(
        self,
    ):
        self.assertFalse(
            self.store.set_property_roles(self.roles())
        )  # the same: nothing to do
        self.assertTrue(
            self.store.set_property_roles([("person", "key_info", G + "label", 0)])
        )
        row = self.row("rec:person:1")
        self.assertEqual((row["identifiers"], row["text"]), (["Ada <x>"], None))
        self.assertEqual(self.store.records_with("identifiers", "ID-1"), [])
        self.assertEqual(
            self.store.records_with("identifiers", "Ada <x>"), ["rec:person:1"]
        )

        self.store.put_multi(
            graph(
                f'<rec:person:7> <{G}label> "same" . <rec:person:8> <{G}label> "same" .'
            )
        )
        before = self.roles()
        with self.assertRaises(DuplicateValue):
            self.store.set_property_roles(
                [*before, ("person", "unique", G + "label", 0)]
            )
        self.assertEqual(self.roles(), before)

    def roles(self):
        return [
            tuple(r)
            for r in self.store.con.execute("SELECT * FROM property_roles").fetchall()
        ]

    def user(self, number: int, userid: str) -> Graph:
        return graph(
            f'<rec:user:{number}> <{G}resource> [ <{G}userid> "{userid}" ] ; '
            f'<{G}label> "{userid}" '
            "."
        )

    def test_a_unique_value_can_only_be_used_by_one_record_also_in_one_save_but_not_across_types(
        self,
    ):
        s = self.store
        s.put_multi(self.user(1, "ada"))
        s.put_multi(self.user(1, "ada"))  # saving the same record again is not a clash
        s.put_multi(self.user(2, "bob"))
        with self.assertRaises(DuplicateValue) as caught:
            s.put_multi(self.user(3, "ada"))
        self.assertEqual(
            (
                caught.exception.value,
                caught.exception.holder,
                caught.exception.predicate,
            ),
            ("ada", "rec:user:1", G + "userid"),
        )
        self.assertEqual(
            fetch_one(
                s.con.execute("SELECT count(*) FROM triples WHERE s = 'rec:user:3'")
            )[0],
            0,
        )
        self.assertEqual(
            self.row("rec:user:3"), {}
        )  # nothing of it is stored, the record row too
        s.put_multi(self.user(2, "carol"))  # a changed value frees the old one
        s.put_multi(self.user(3, "bob"))
        s.delete(["rec:user:1"])  # and so does a delete
        s.put_multi(self.user(4, "ada"))

        with self.assertRaises(DuplicateValue):
            self.store.put_multi(
                graph(
                    f'<rec:user:5> <{G}resource> [ <{G}userid> "zed" ] . '
                    f'<rec:user:6> <{G}resource> [ <{G}userid> "zed" ] .'
                )
            )
        self.assertEqual(
            fetch_one(
                self.store.con.execute(
                    "SELECT count(*) FROM triples WHERE s IN ('rec:user:5', 'rec:user:6')"
                )
            )[0],
            0,
        )

        self.store.put_multi(
            graph(f'<rec:person:7> <{G}resource> [ <{G}userid> "ada" ] .')
        )

    def test_rederive_stores_only_what_the_hooks_change(self):
        upper = (
            f"DELETE FROM @FLAT@ WHERE len(loc) = 0 AND p = '{G}label' AND "
            "starts_with(s, "
            "'rec:thing:');"
            f"INSERT INTO @FLAT@ SELECT s, []::VARCHAR[], '{G}label', NULL, "
            "upper(lit), NULL, NULL "
            "FROM @FLAT@ WHERE p = 'http://ex/name' AND starts_with(s, 'rec:thing:')"
        ).split(";")
        s = self.store
        s.put_multi(
            graph(
                "".join(
                    f'<rec:thing:{n}> <http://ex/name> "name {n}" .\n'
                    for n in (1, 2, 3)
                )
            )
        )
        before = {n: self.row(f"rec:thing:{n}") for n in (1, 2, 3)}
        s.configure(lambda: [fake_type(upper, number=s.type_numbers()["thing"])])
        self.assertEqual(
            s.rederive("thing", (2, 2)), ["rec:thing:2"]
        )  # only the numbers asked for
        self.assertEqual(self.row("rec:thing:2")["label"], "NAME 2")
        self.assertIsNone(self.row("rec:thing:1")["label"])
        self.assertEqual(s.rederive("thing"), ["rec:thing:1", "rec:thing:3"])
        self.assertEqual(
            [self.row(f"rec:thing:{n}")["label"] for n in (1, 2, 3)],
            ["NAME 1", "NAME 2", "NAME 3"],
        )
        after = self.row("rec:thing:1")
        self.assertGreater(after["modified_at"], before[1]["modified_at"])
        self.assertIn('"NAME 1"', after["source"])
        # again: the hooks change nothing, so nothing is written, and the records are
        # not touched
        stamps = {n: self.row(f"rec:thing:{n}")["modified_at"] for n in (1, 2, 3)}
        self.assertEqual(s.rederive("thing"), [])
        self.assertEqual(
            {n: self.row(f"rec:thing:{n}")["modified_at"] for n in (1, 2, 3)}, stamps
        )
        count = fetch_one(
            s.con.execute("SELECT count(*) FROM triples WHERE s LIKE 'rec:thing:%'")
        )[0]
        self.assertEqual(count, 6)  # a name and a label each, no row twice

    def test_the_next_number_is_past_the_highest_record_whoever_wrote_it(self):
        s = self.store
        self.assertEqual(s.mint_id("gizmo"), 1)
        s.set_type_ids(
            [("gizmo", 30)]
        )  # a write outside the store does not give a type its number
        # written outside the store: the counters never hear of it, the records do
        s.con.execute(
            "INSERT INTO triples VALUES ('rec:gizmo:900', [], 'http://ex/p', NULL, "
            "'x', NULL, NULL)"
        )
        self.assertEqual(s.last_id("gizmo"), 900)
        self.assertEqual(s.mint_id("gizmo"), 901)
        s.put_multi(graph('<rec:gizmo:950> <http://ex/p> "y" .'))
        s.delete(["rec:gizmo:950"])  # a deleted record keeps its number: the row stays
        self.assertEqual(s.mint_id("gizmo"), 951)
        self.assertEqual(s.mint_id("other"), 1)  # each type for itself

    def test_the_records_of_a_type_and_their_count_leave_out_the_deleted(self):
        s = self.store
        s.put_multi(
            graph(
                '<rec:thing:2> <http://ex/p> "b" . <rec:thing:10> <http://ex/p> "c" .'
            )
        )
        s.put_multi(graph('<rec:thing:3> <http://ex/p> "d" .'))
        s.delete(["rec:thing:3"])
        self.assertEqual(
            s.records_of_type("thing"), ["rec:thing:2", "rec:thing:10"]
        )  # by number
        self.assertEqual(s.count_records_by_type(), {"person": 2, "thing": 2})
        self.assertEqual(s.record_with_resource_uri("ark:/1/2"), "rec:person:1")
        self.assertIsNone(s.record_with_resource_uri("ark:/none"))

    def test_a_reservation_keeps_the_numbers_up_to_it_and_never_lowers_what_is_in_use(
        self,
    ):
        s = self.store
        s.put_multi(graph('<rec:gizmo:7> <http://ex/p> "a" .'))
        self.assertEqual(s.numbering("gizmo")["next"], 8)
        with self.assertRaises(ValueError) as caught:
            s.reserve_ids("gizmo", 6)  # a record has 7
        self.assertIn("lower than 7", str(caught.exception))
        self.assertEqual(s.reserve_ids("gizmo", 1000), 1000)
        self.assertEqual(
            s.numbering("gizmo"),
            {
                "given": 7,
                "reserved": 1000,
                "highest": 7,
                "taken": 7,
                "last": 1000,
                "next": 1001,
            },
        )
        self.assertEqual(s.mint_id("gizmo"), 1001)  # past the reservation, once
        self.assertEqual(s.mint_id("gizmo"), 1002)
        with self.assertRaises(ValueError):
            s.reserve_ids("gizmo", 1001)  # 1002 was given out: that number is in use
        s.put_multi(
            graph('<rec:gizmo:5000> <http://ex/p> "imported" .')
        )  # a record may bring a number
        self.assertEqual(s.mint_id("gizmo"), 5001)
        self.assertEqual(
            s.reserve_ids("gizmo", 5001), 5001
        )  # a reservation is given up
        self.assertEqual(s.numbering("gizmo")["next"], 5002)
        self.assertEqual(s.reserve_ids("fresh", 40), 40)  # a type that has nothing yet
        self.assertEqual(s.mint_id("fresh"), 41)

    def test_a_number_that_a_deleted_record_had_is_not_given_out_again_also_when_it_was_raw(
        self,
    ):
        s = self.store
        s.set_type_ids(
            [("gizmo", 30)]
        )  # a write outside the store does not give a type its number
        s.con.execute(
            "INSERT INTO triples VALUES ('rec:gizmo:900', [], 'http://ex/p', NULL, "
            "'x', NULL, NULL)"
        )
        s.con.execute(
            "DELETE FROM triples WHERE s = 'rec:gizmo:900'"
        )  # no record, the counters never knew
        self.assertEqual(s.highest_number("gizmo"), 0)  # no record has a number now
        self.assertEqual(
            s.numbering("gizmo")["highest"], 900
        )  # but the history has had one
        self.assertEqual(s.mint_id("gizmo"), 901)


class EncodeNtriplesUriTest(DbTestCase):
    def test_the_macro_encodes_what_an_iri_may_not_hold_and_source_and_history_hold_the_same_text(
        self,
    ):
        s = open_store(self.db)
        self.addCleanup(s.close)
        got = fetch_one(
            s.con.execute(
                "SELECT encode_ntriples_uri(? || chr(1) || chr(127) || '`')",
                ['http://e/a b<c>"d{e}|f\\g^h%20i'],
            )
        )[0]
        self.assertEqual(got, "<http://e/a%20b%3Cc%3E%22d%7Be%7D%7Cf%5Cg%5Eh%20i>")

        s = open_store(self.db)
        self.addCleanup(s.close)
        s.put_multi(graph("<rec:person:1> <http://ex/p> <http://ex/a%20b?x=1&y=2> ."))
        (source,) = fetch_one(s.con.execute("SELECT source FROM records"))
        (hist,) = fetch_one(s.con.execute("SELECT source FROM history"))
        self.assertEqual(source, hist)
