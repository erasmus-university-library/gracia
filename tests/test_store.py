import json
from typing import ClassVar
from unittest import mock

import duckdb
import pyoxigraph as ox
from base import DATA, REC, DbTestCase

from gracia import store as store_module
from gracia.graph import streaming_parse
from gracia.store import Store, fetch_one

EX = "http://example.org/"


class ModelTest(DbTestCase):
    def test_the_columns_are_strings_and_a_list(self):
        self.load(DATA / "05_shared.ttl")
        with Store(self.db) as store:
            types = dict(
                store.con.execute(
                    "SELECT column_name, column_type FROM (DESCRIBE triples)"
                ).fetchall()
            )
        self.assertEqual(list(types), ["s", "loc", "p", "o", "lit", "lang", "dt"])
        for column in ("s", "p", "o", "lit", "lang", "dt"):
            self.assertEqual(types[column], "VARCHAR", column)
        self.assertEqual(types["loc"], "VARCHAR[]")

    def test_bnodes_are_numbered_per_resource(self):
        self.load(DATA / "05_shared.ttl")
        with Store(self.db) as store:
            rows = store.con.execute(
                "SELECT s, list(DISTINCT x) FROM (SELECT s, unnest(loc) AS x FROM "
                "triples) "
                "WHERE starts_with(x, '_:') GROUP BY s"
            ).fetchall()
            others = fetch_one(
                store.con.execute(
                    "SELECT count(*) FROM (SELECT unnest(loc) AS x FROM triples) "
                    "WHERE NOT starts_with(x, '_:') AND NOT starts_with(x, 'http')"
                )
            )[0]
        self.assertTrue(rows)
        for _s, bnodes in rows:
            # numbered _:1, _:2, ... without gaps inside each resource
            self.assertEqual(
                sorted(int(b[2:]) for b in bnodes), list(range(1, len(bnodes) + 1))
            )
        self.assertEqual(others, 0)  # every other entry of loc is a predicate uri

    def test_the_top_level_rows_have_an_empty_loc_and_nested_ones_an_even_loc(self):
        self.load(
            self.write(
                "a.ttl",
                '<rec:thing:1> ex:p "1" ; ex:q [ ex:r "x" ; ex:t [ ex:u "y" ] ] .\n',
            )
        )
        with Store(self.db) as store:
            rows = store.con.execute(
                "SELECT p, loc FROM triples ORDER BY len(loc), p"
            ).fetchall()
        self.assertEqual(
            rows,
            [
                (EX + "p", []),
                (EX + "r", [EX + "q", "_:1"]),
                (EX + "u", [EX + "q", "_:1", EX + "t", "_:2"]),
            ],
        )

    def test_long_list_is_not_truncated(self):
        self.load(DATA / "06_long_list.ttl")
        items = [
            t
            for t in json.loads(self.export_json())["triples"]
            if t["predicate"]["value"].endswith("#first")
        ]
        self.assertEqual(len(items), 150)


class SortTest(DbTestCase):
    def test_sorting_puts_the_rows_of_a_type_together_and_keeps_every_row(self):
        for name in (
            "rec:person:2",
            "rec:work:1",
            "rec:person:10",
            "rec:person:1",
            "rec:work:2",
        ):
            self.load(
                self.write(
                    f"{name}.ttl", f'<{name}> <{EX}p> "x" ; <{EX}q> [ <{EX}r> "y" ] .\n'
                )
            )
        with Store(self.db) as store:
            before = store.con.execute("SELECT * FROM triples ORDER BY ALL").fetchall()
            self.assertNotEqual(
                [r[0] for r in store.con.execute("SELECT s FROM triples").fetchall()],
                sorted(r[0] for r in before),
            )
            self.assertEqual(store.sort(), len(before))
            self.assertEqual(
                store.con.execute("SELECT * FROM triples ORDER BY ALL").fetchall(),
                before,
            )
            subjects = [
                r[0] for r in store.con.execute("SELECT s FROM triples").fetchall()
            ]
            self.assertEqual(subjects, sorted(subjects))
            self.assertEqual(
                fetch_one(
                    store.con.execute(
                        "SELECT count(*) FROM duckdb_tables() WHERE table_name LIKE "
                        "'%sorting'"
                    )
                )[0],
                0,
            )

    def test_the_command(self):
        self.load(
            self.write("a.ttl", '<rec:thing:2> ex:p "1" .\n<rec:thing:1> ex:p "2" .\n')
        )
        code, out, err = self.gracia("sort")
        self.assertEqual((code, out.strip()), (0, "sorted 2 rows"), err)


class MergeDeleteTest(DbTestCase):
    def resource_rows(self, uri: str) -> int:
        with Store(self.db) as store:
            return fetch_one(
                store.con.execute(
                    "SELECT count(*) FROM triples WHERE s = ?",
                    [uri],
                )
            )[0]

    def test_merge_replaces_whole_resources_only(self):
        self.load(
            self.write(
                "a.ttl",
                '<rec:thing:1> ex:p "1", "2" ; ex:q [ ex:r "x" ] .\n<rec:thing:2> ex:p '
                '"keep" .\n',
            )
        )
        self.assertEqual(self.resource_rows(REC["a"]), 3)
        self.load(
            self.write(
                "b.ttl", '<rec:thing:1> ex:p "new" .\n<rec:thing:3> ex:p "added" .\n'
            )
        )
        self.assertEqual(self.resource_rows(REC["a"]), 1)  # replaced, bnode rows gone
        self.assertEqual(self.resource_rows(REC["b"]), 1)  # untouched
        self.assertEqual(self.resource_rows(REC["c"]), 1)  # new

    def test_merge_is_idempotent(self):
        path = self.write("a.ttl", '<rec:thing:1> ex:p "1" ; ex:q [ ex:r "x" ] .\n')
        self.load(path)
        before = self.export_json()
        self.load(path)
        self.assertEqual(before.count('"value"'), self.export_json().count('"value"'))

    def test_merge_never_deletes_other_resources(self):
        self.load(
            self.write("a.ttl", '<rec:thing:1> ex:p "1" .\n<rec:thing:2> ex:p "2" .\n')
        )
        self.load(self.write("b.ttl", '<rec:thing:1> ex:p "3" .\n'))
        self.assertEqual(self.resource_rows(REC["b"]), 1)

    def test_delete_resource(self):
        self.load(
            self.write(
                "a.ttl",
                '<rec:thing:1> ex:p "1" ; ex:q [ ex:r "x" ] .\n<rec:thing:2> ex:p "2" '
                ".\n",
            )
        )
        code, out, err = self.gracia("delete", REC["a"], EX + "unknown")
        self.assertEqual(code, 0, err)
        self.assertIn(f"deleted 2 rows of {REC['a']}", out)
        self.assertEqual(self.resource_rows(REC["a"]), 0)
        self.assertEqual(self.resource_rows(REC["b"]), 1)
        self.assertNotIn("unknown", out)

    def test_loading_a_resource_again_replaces_it_there_is_no_load_that_only_adds(self):
        """Every load replaces the resources it has: a second load of the same one
        leaves one copy of its bnode (the ids restart at -1 per load, which is why a
        load that appended would mix them)."""
        path = self.write("a.ttl", '<rec:thing:1> ex:q [ ex:r "x" ] .\n')
        self.load(path)
        self.load(path)
        self.assertEqual(self.resource_rows(REC["a"]), 1)
        with self.assertRaises(
            SystemExit
        ) as raised:  # the flag is gone: it is not a choice any more
            self.gracia("import", "--merge", str(path))
        self.assertEqual(raised.exception.code, 2)

    def test_failed_load_rolls_back(self):
        """Nothing is stored when the triples insert fails."""
        self.load(self.write("a.ttl", '<rec:thing:1> ex:p "1" .\n'))
        with Store(self.db) as store:
            before = fetch_one(store.con.execute("SELECT count(*) FROM triples"))[0]
            store.stage_rows(streaming_parse(DATA / "01_simple.ttl"))
            store.con.execute(
                f"ALTER TABLE {store.staging} DROP COLUMN o_dt"
            )  # makes the INSERT fail
            with self.assertRaises(Exception):  # noqa: B017 - a DuckDB binder error
                store.load()
            self.assertEqual(
                fetch_one(store.con.execute("SELECT count(*) FROM triples"))[0], before
            )

    def test_invalid_json_is_a_clean_error(self):
        bad = self.tmp / "bad.json"
        bad.write_text('{"triples": [{"subject": 1}]}', encoding="utf-8")
        code, _, err = self.gracia("import", str(bad))
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("gracia:"), err)


class CliTest(DbTestCase):
    def test_strict_refuses_warnings(self):
        code, _, err = self.gracia(
            "import", "--strict", str(DATA / "cycles" / "cycle.ttl")
        )
        self.assertEqual(code, 2)
        self.assertIn("self-loop", err)
        with Store(self.db) as store:
            self.assertEqual(
                fetch_one(store.con.execute("SELECT count(*) FROM triples"))[0], 0
            )

    def test_shared_bnode_is_only_a_warning(self):
        code, _, err = self.gracia("import", str(DATA / "05_shared.ttl"))
        self.assertEqual(code, 0)
        self.assertIn("parents", err)

    def test_export_formats(self):
        self.load(DATA / "01_simple.ttl")
        for fmt, needle in [
            ("turtle", "rec:book:1"),
            ("ntriples", "<rec:book:1>"),
            ("rdfxml", "rdf:RDF"),
            ("trig", "rec:book:1"),
            ("nquads", "<rec:book:1>"),
            ("json-triples", '"triples"'),
            ("rdfxml-collection", "rdf:RDF"),
        ]:
            with self.subTest(format=fmt):
                code, out, err = self.gracia("export", "-f", fmt)
                self.assertEqual(code, 0, err)
                self.assertIn(needle, out)

    def test_export_one_resource(self):
        self.load(DATA / "01_simple.ttl")
        code, out, _ = self.gracia("export", "-f", "ntriples", "-u", REC["alice"])
        self.assertEqual(code, 0)
        self.assertIn("Alice", out)
        self.assertNotIn("The Title", out)
        code, out, _ = self.gracia("export", "-f", "ntriples", "-u", EX + "nobody")
        self.assertEqual(out.strip(), "")

    def test_missing_database_is_not_created(self):
        for args in (["export"], ["delete", REC["a"]], ["prefixes"]):
            with self.subTest(command=args[0]):
                code, _, err = self.gracia(*args)
                self.assertEqual(code, 1)
                self.assertIn("no such database", err)
                self.assertFalse(self.db.exists())

    def test_unknown_format_is_an_error(self):
        self.load(DATA / "01_simple.ttl")
        code, _, err = self.gracia("export", "-f", "nonsense")
        self.assertEqual(code, 1)
        self.assertIn("unknown format", err)


class FailOnce:
    """A connection that fails the first time the triples are merged, as a writer that
    lost the race with another one does (in process: a transaction conflict; through
    Quack: a duplicate key), and passes everything else on."""

    def __init__(self, con, error):
        self._con, self._error, self.failures = con, error, 0
        self.always = False

    def __getattr__(self, name):
        return getattr(self._con, name)

    def execute(self, sql, *args, **kwargs):
        if "Replace-by-resource" in sql and (self.always or not self.failures):
            self.failures += 1
            raise self._error
        return self._con.execute(sql, *args, **kwargs)


class LostRaceTest(DbTestCase):
    SUBJECTS: ClassVar[dict] = {
        "book": "rec:book:3",
        "TransactionException": "rec:error:1",
        "ConstraintException": "rec:error:2",
    }

    def quads(self, name="book"):
        return ox.Dataset(
            [
                ox.Quad(
                    ox.NamedNode(self.SUBJECTS[name]),
                    ox.NamedNode(EX + "p"),
                    ox.Literal("x"),
                )
            ]
        )

    def test_a_writer_that_loses_the_race_loads_again(self):
        for error in (
            duckdb.TransactionException("Conflict on update"),
            duckdb.ConstraintException('Duplicate key "type: person"'),
        ):
            with self.subTest(error=type(error).__name__), Store(self.db) as store:
                store.con = FailOnce(store.con, error)
                with mock.patch("gracia.store.time.sleep"):
                    store.put(self.quads(type(error).__name__))
                self.assertEqual(store.con.failures, 1)
                self.assertEqual(len(store.get(self.SUBJECTS[type(error).__name__])), 1)

    def test_a_writer_that_keeps_losing_gives_up_with_the_error(self):
        with Store(self.db) as store:
            store.con = FailOnce(store.con, duckdb.ConstraintException("Duplicate key"))
            store.con.always = True
            with (
                mock.patch("gracia.store.time.sleep"),
                self.assertRaises(duckdb.ConstraintException),
            ):
                store.put(self.quads())
            self.assertEqual(store.con.failures, store_module.LOAD_ATTEMPTS)
