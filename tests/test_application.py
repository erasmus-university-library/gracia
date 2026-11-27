import tempfile
import threading
import unittest
from pathlib import Path

import pyoxigraph as ox
from base import DATA, REC, DbTestCase, rec
from harness import open_application, open_store, opened

from gracia.store import NoSuchRepository, fetch_one

EX = "http://example.org/"


def dataset(name: str, value: str = "x") -> ox.Dataset:
    quad = ox.Quad(ox.NamedNode(rec(name)), ox.NamedNode(EX + "p"), ox.Literal(value))
    return ox.Dataset([quad])


class ApplicationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.app = open_application(self, ":memory:")

    def test_repositories_are_created_once_listed_and_main_is_an_ordinary_one_and_unknown_is_not_created(
        self,
    ):
        # starts without repositories
        self.assertEqual(self.app.list_repositories(), [])

        # create and list
        self.assertTrue(self.app.create_repository("test_repo"))
        self.assertFalse(self.app.create_repository("test_repo"))  # idempotent
        self.assertTrue(self.app.create_repository("archive"))
        self.assertEqual(self.app.list_repositories(), ["archive", "test_repo"])
        self.assertTrue(self.app.has_repository("test_repo"))
        self.assertFalse(self.app.has_repository("nope"))

        # main is an ordinary repository
        self.assertTrue(self.app.create_repository("main"))
        self.assertIn("main", self.app.list_repositories())

        # opening an unknown repository says how to create it
        with self.assertRaisesRegex(NoSuchRepository, "gracia repo create nope"):
            self.app.new_store("nope")
        self.assertNotIn("nope", self.app.list_repositories())  # not created by it

    def test_ids_must_be_safe_schema_names_and_reserved_ones_are_refused(self):
        # ids must be safe schema names
        for bad in (
            "",
            "Library",
            "1abc",
            "a-b",
            "a b",
            "x" * 64,
            'a"b',
            "a;drop table x",
            "é",
            'x"; DROP SCHEMA main; --',
            "x' OR '1'='1",
            "a.b",
            "../etc",
        ):
            with self.subTest(id=bad):
                with self.assertRaises(ValueError):
                    self.app.create_repository(bad)
                with self.assertRaises(NoSuchRepository):
                    self.app.new_store(bad)
        self.assertEqual(self.app.list_repositories(), [])

        # reserved ids are refused
        for reserved in (
            "docs",
            "api",
            "information_schema",
            "pg_catalog",
            "temp",
            "repositories",
        ):
            with (
                self.subTest(id=reserved),
                self.assertRaisesRegex(ValueError, "reserved"),
            ):
                self.app.create_repository(reserved)

    def test_a_repository_is_a_duckdb_schema_with_its_own_tables_and_macros_and_finds_types_by_class(
        self,
    ):
        # a repository is a duckdb schema with its own tables and macros
        self.app.create_repository("test_repo")
        cursor = self.app.database._con.cursor()  # ty: ignore[missing-union-attribute]
        tables = {
            r[0]
            for r in cursor.execute(
                "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'test_repo'"
            ).fetchall()
        }
        self.assertEqual(
            tables,
            {
                "triples",
                "ids",
                "records",
                "records_identifiers",
                "records_references",
                "records_changes",
                "property_roles",
                "unique_values",
                "history",
                "change_events",  # what the followers of the data still have to do
                "slugs",
                "etags",  # the etags of the pages and what a refresh works on
                "etags_seed",
                "etags_todo",
                "etags_refs",
                "etags_new",
                "etags_before",
                "etags_delta",
                "etags_holders",
                "etags_backdelta",
                "etags_backnew",
            },
        )
        macros = fetch_one(
            cursor.execute(
                "SELECT count(*) FROM duckdb_functions() "
                "WHERE schema_name = 'test_repo' AND function_name = 'triples_rows'"
            )
        )[0]
        self.assertEqual(macros, 1)
        ids = cursor.execute(
            "SELECT function_name FROM duckdb_functions() WHERE schema_name = "
            "'test_repo' "
            "AND function_name IN ('type_slug', 'record_id', 'record_uri', "
            "'resource_uri') "
            "ORDER BY 1"
        ).fetchall()
        self.assertEqual(
            [r[0] for r in ids],
            ["record_id", "record_uri", "resource_uri", "type_slug"],
        )

        # the types are found by their class and the groups by their ark
        self.app.create_repository("test_repo")
        with self.app.new_store("test_repo") as store:
            found = store.records_with_resource_uris(
                [
                    "https://gracia.pub/ns/UserGroup",
                    # the type records are published as their classes
                    "https://gracia.pub/ns/Type",
                    "ark:/99999/ir/group/2",
                    "ark:/99999/ir/nothing/1",
                    # the records of the others by their arks; no record uri
                    "rec:type:2",
                ]
            )
            self.assertEqual(
                found,
                {
                    "https://gracia.pub/ns/UserGroup": "rec:type:4",
                    "https://gracia.pub/ns/Type": "rec:type:2",
                    "ark:/99999/ir/group/2": "rec:group:2",
                },
            )
            self.assertEqual(store.records_with_resource_uris([]), {})

    def test_repositories_share_nothing_and_cursors_forks_and_concurrent_readers_use_their_own_schema(
        self,
    ):
        # repositories share nothing
        self.app.create_repository("a")
        self.app.create_repository("b")
        with self.app.new_store("a") as a, self.app.new_store("b") as b:
            b.set_type_ids([("book", 5)])  # b knows the type, and has no record of it
            a.put_multi(dataset("book"))
            self.assertEqual(len(a.get(REC["book"])), 1)
            self.assertEqual(len(b.get(REC["book"])), 0)
            self.assertGreater(
                a.counts()["rows"], b.counts()["rows"]
            )  # b has only its own data
            self.assertEqual(len(b.get(REC["book"])), 0)

        # cursors and forks use the repository schema
        self.app.create_repository("test_repo")
        with self.app.new_store("test_repo") as store:
            store.put_multi(dataset("book"))
            self.assertEqual(
                store.export_ntriples(self.tmp / "book.nt", [REC["book"]]), 1
            )
            with store.fork() as fork:
                self.assertEqual(len(fork.get(REC["book"])), 1)

        # concurrent readers each get their own store
        self.app.create_repository("test_repo")
        with self.app.new_store("test_repo") as store:
            store.put_multi(dataset("book"))
        failures, results = [], []

        def read():
            try:
                with self.app.new_store("test_repo") as s:
                    results.append(len(s.get(REC["book"])))
            except Exception as err:
                failures.append(err)

        threads = [threading.Thread(target=read) for _ in range(16)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(failures, [])
        self.assertEqual(results, [1] * 16)


class ConcurrentWritersTest(unittest.TestCase):
    def test_writers_adding_the_same_new_term_all_succeed(self):
        with opened(":memory:") as app:
            app.create_repository("test_repo")
            with app.new_store(
                "test_repo"
            ) as store:  # the types are there before the writers start
                store.set_type_ids([("book", 20)])
            errors = []

            def write(n):
                try:
                    with app.new_store("test_repo") as store:
                        store.put_multi(dataset(f"book{n + 1}"))
                except Exception as err:
                    errors.append(err)

            threads = [threading.Thread(target=write, args=(n,)) for n in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            with app.new_store("test_repo") as store:
                self.assertEqual(
                    len(store.get_multi([rec(f"book{n + 1}") for n in range(6)])), 6
                )


class ApplicationOnDiskTest(DbTestCase):
    def test_repositories_survive_a_restart_and_one_may_be_named_like_the_database_file(
        self,
    ):
        # repositories survive a restart
        with opened(self.db) as app:
            app.create_repository("test_repo")
            with app.new_store("test_repo") as store:
                store.put_multi(dataset("book"))
        with opened(self.db) as app:
            self.assertEqual(app.list_repositories(), ["test_repo"])
            with app.new_store("test_repo") as store:
                self.assertEqual(len(store.get(REC["book"])), 1)

        # a repository may be named like the database file
        path = self.tmp / "gracia.db"
        with opened(path) as app:
            app.create_repository("gracia")
            with app.new_store("gracia") as store:
                store.put_multi(dataset("book"))
                self.assertEqual(len(store.get(REC["book"])), 1)
                self.assertEqual(
                    store.export_ntriples(self.tmp / "book.nt", [REC["book"]]), 1
                )

    def test_store_by_path_needs_an_existing_repository_except_main(self):
        with self.assertRaises(NoSuchRepository):
            open_store(self.db, repo="test_repo")
        with open_store(self.db, repo="test_repo", create=True) as store:
            self.assertEqual(store.repo, "test_repo")
        with open_store(self.db, repo="test_repo") as store:  # now it exists
            self.assertEqual(
                store.counts()["rows"], 0
            )  # not through the application: no data
        with open_store(
            self.db
        ) as store:  # the default repository is created on first use
            self.assertEqual(store.repo, "main")


class RepositoryCommandsTest(DbTestCase):
    def test_create_list_twice_and_bad_ids(self):
        # create and list
        code, out, err = self.create_repositories("test_repo", "archive")
        self.assertEqual(code, 0, err)
        self.assertIn("created repository test_repo", out)
        self.assertIn("created repository archive", out)
        self.assertTrue(self.db.exists())  # the database file was started
        code, out, _ = self.gracia("repo", "list")
        self.assertEqual(out.split(), ["archive", "test_repo"])

        # create twice is fine
        self.create_repositories("test_repo")
        code, out, _ = self.create_repositories("test_repo")
        self.assertEqual(code, 0)
        self.assertIn("repository test_repo already exists", out)

        # bad ids are refused
        for bad in ("Bad-ID", "docs", "1abc"):
            with self.subTest(id=bad):
                code, _, err = self.create_repositories(bad)
                self.assertEqual(code, 1)
                self.assertTrue(err.startswith("gracia:"), err)

    def test_list_does_not_create_a_database_and_an_unknown_repository_is_an_error_and_not_created(
        self,
    ):
        # list does not create a database
        code, _, err = self.gracia("repo", "list")
        self.assertEqual(code, 1)
        self.assertIn("no such database", err)
        self.assertFalse(self.db.exists())

        # unknown repository is an error and is not created
        self.create_repositories("a")
        for args in (
            ["import", str(DATA / "01_simple.ttl")],
            ["export"],
            ["delete", EX + "x"],
        ):
            with self.subTest(command=args[0]):
                code, _, err = self.gracia("-r", "nope", *args)
                self.assertEqual(code, 1)
                self.assertIn("no such repository: nope", err)
        _, out, _ = self.gracia("repo", "list")
        self.assertEqual(out.split(), ["a"])

    def test_commands_work_inside_the_chosen_repository(self):
        self.create_repositories("a", "b")
        code, _, err = self.gracia("-r", "a", "import", str(DATA / "01_simple.ttl"))
        self.assertEqual(code, 0, err)
        code, _, err = self.gracia("-r", "b", "import", str(DATA / "02_bnodes.ttl"))
        self.assertEqual(code, 0, err)
        _, out_a, _ = self.gracia("-r", "a", "export", "-f", "ntriples")
        _, out_b, _ = self.gracia("-r", "b", "export", "-f", "ntriples")
        self.assertIn("The Title", out_a)
        self.assertNotIn("Ghent", out_a)
        self.assertIn("Ghent", out_b)
        self.assertNotIn("The Title", out_b)
        _, out, _ = self.gracia("repo", "list")
        self.assertEqual(out.split(), ["a", "b"])  # nothing was created on the side
