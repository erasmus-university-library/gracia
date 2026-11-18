import socket
import threading
import unittest

import duckdb
import pyoxigraph as ox
from base import REC, DbTestCase, rec
from test_serve_db import quack_available

from gracia import cli, store
from gracia.application import Application

EX = "http://example.org/"


def dataset(name: str, value: str = "x") -> ox.Dataset:
    quad = ox.Quad(ox.NamedNode(rec(name)), ox.NamedNode(EX + "p"), ox.Literal(value))
    return ox.Dataset([quad])


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(quack_available(), "the DuckDB quack extension is not available")
class RemoteApplicationTest(DbTestCase):
    def setUp(self):
        super().setUp()
        port, token = free_port(), "0123456789abcdef0123456789"
        uri = f"quack:localhost:{port}"
        owner = duckdb.connect(":memory:")  # an in-memory database can be served too
        owner.execute("INSTALL quack; LOAD quack")
        owner.execute(f"CALL quack_serve('{uri}', token = '{token}')")
        self.addCleanup(owner.close)
        self.addCleanup(lambda: owner.execute(f"CALL quack_stop('{uri}')"))
        self.app = Application(uri, token=token)
        self.addCleanup(self.app.close)

    def test_a_wrong_token_or_address_is_a_clear_error(self):
        with self.assertRaises(ValueError):
            Application("quack:localhost:1", token="not-the-token-0123456789")

    def test_create_list_and_the_initial_data(self):
        self.assertEqual(self.app.list_repositories(), [])
        self.assertTrue(self.app.create_repository("library"))
        self.assertFalse(self.app.create_repository("library"))
        self.assertEqual(self.app.list_repositories(), ["library"])
        with self.app.open_repository("library") as store:
            self.assertEqual(store.records_of_type("repository"), ["rec:repository:1"])

    def test_the_repository_lands_in_its_own_schema(self):
        """Over Quack an unqualified table silently lands in the owner's main schema."""
        self.app.create_repository("library")
        con = self.app._connect()
        tables = con.execute(
            "SELECT schema_name FROM duckdb_tables() WHERE table_name = 'triples'"
        ).fetchall()
        self.assertEqual(tables, [("library",)])

    def test_put_get_replace_and_delete(self):
        self.app.create_repository("library")
        with self.app.open_repository("library") as store:
            store.put(dataset("book", "one"))
            self.assertEqual(len(store.get(REC["book"])), 1)
            store.put(dataset("book", "two"))  # replaces the whole resource
            (quad,) = list(store.get(REC["book"]))
            self.assertEqual(quad.object.value, "two")
            store.delete([REC["book"]])
            self.assertEqual(len(store.get(REC["book"])), 0)

    def test_repositories_share_nothing(self):
        self.app.create_repository("a")
        self.app.create_repository("b")
        with self.app.open_repository("a") as a, self.app.open_repository("b") as b:
            b.set_type_ids([("book", 5)])  # b knows the type, and has no record of it
            a.put(dataset("book"))
            self.assertEqual(len(b.get(REC["book"])), 0)

    def test_bnodes_and_lists_survive(self):
        self.app.create_repository("library")
        text = (
            f"<rec:thing:9> <{EX}list> ( 1 2 3 ) ; "
            f"<{EX}part> [ <{EX}name> 'n' ; <{EX}sub> [ <{EX}k> 'v' ] ] ."
        )
        data = ox.Dataset(ox.parse(text.encode(), format=ox.RdfFormat.TURTLE))
        with self.app.open_repository("library") as store:
            store.put(data)
            back = store.get("rec:thing:9")
        self.assertEqual(len(back), len(data))

    def test_streamed_export_and_forks(self):
        self.app.create_repository("library")
        with self.app.open_repository("library") as store:
            store.put(dataset("book"))
            self.assertEqual(
                store.export_ntriples(self.tmp / "book.nt", [REC["book"]]), 1
            )
            with store.fork() as fork:
                self.assertEqual(len(fork.get(REC["book"])), 1)

    def test_concurrent_writers_do_not_clash(self):
        self.app.create_repository("library")
        with self.app.open_repository(
            "library"
        ) as store:  # the type is there before the writers start
            store.set_type_ids([("book", 20)])
        errors = []

        def write(n):
            try:
                with self.app.open_repository("library") as store:
                    store.put(dataset(f"book{n + 1}"))
            except Exception as err:
                errors.append(err)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        with self.app.open_repository("library") as store:
            self.assertEqual(len(store.get([rec(f"book{n + 1}") for n in range(4)])), 4)

    def test_minting_finding_and_logging_in_through_the_owner(self):
        import base64

        self.app.create_repository("library", admin=("admin", "correct horse"))
        with self.app.open_repository("library") as store:
            found = store.records_with("identifiers", "admin", rec_type="user")
            self.assertEqual(found, ["rec:user:1"])
            self.assertEqual(store.mint_id("user"), 2)
        from webtest import TestApp

        from gracia.main import make_config

        web = TestApp(make_config({"gracia.application": self.app}).make_wsgi_app())
        token = base64.b64encode(b"admin:correct horse").decode()
        ok = web.get("/library/user/1", headers={"Authorization": f"Basic {token}"})
        self.assertEqual(ok.status_int, 200)
        self.assertEqual(web.get("/library/user/1", expect_errors=True).status_int, 401)

    def test_adding_a_user_through_the_web_app_on_the_owner(self):
        import base64

        from webtest import TestApp

        from gracia.main import make_config

        self.app.create_repository("library", admin=("admin", "correct horse"))
        web = TestApp(make_config({"gracia.application": self.app}).make_wsgi_app())

        def auth(user, password):
            return {
                "Authorization": "Basic "
                + base64.b64encode(f"{user}:{password}".encode()).decode()
            }

        admin = auth("admin", "correct horse")
        form = web.get("/library/user/add", headers=admin).forms[0]
        form["userid"], form["password"] = "second", "another password"
        response = form.submit("save", headers=admin)
        self.assertEqual(response.status_int, 302)
        self.assertEqual(
            web.get("/library/user", headers=admin).json["records"], [1, 2]
        )
        again = web.get(
            "/library/user/2",
            headers=auth("second", "another password"),
            expect_errors=True,
        )
        self.assertEqual(again.status_int, 403)  # logged in, but users are for admins

    def test_the_web_app_runs_on_it(self):
        from webtest import TestApp

        from gracia.main import make_config

        self.app.create_repository("library")
        web = TestApp(make_config({"gracia.application": self.app}).make_wsgi_app())
        self.assertEqual(web.get("/").json, {"repositories": ["library"]})
        self.assertGreater(web.get("/library/").json["rows"], 0)


@unittest.skipUnless(quack_available(), "the DuckDB quack extension is not available")
class OwnerDiscoveryTest(DbTestCase):
    def owner(self, db, port, token="0123456789abcdef0123456789"):
        uri = f"quack:localhost:{port}"
        owner = duckdb.connect(":memory:")
        owner.execute("INSTALL quack; LOAD quack")
        owner.execute(f"CALL quack_serve('{uri}', token = '{token}')")
        self.addCleanup(owner.close)
        self.addCleanup(lambda: owner.execute(f"CALL quack_stop('{uri}')"))
        token_file = self.tmp / f"{db}.token"
        token_file.write_text(token + "\n")
        token_file.chmod(0o600)

    def test_a_running_owner_next_to_its_token_file_is_used(self):
        port, db = free_port(), str(self.tmp / "t.db")
        self.owner("t.db", port)
        found = cli._discover_owner({"gracia.db": db}, port=port)
        self.assertEqual(found["gracia.db"], f"quack:localhost:{port}")
        self.assertEqual(found["gracia.db.token_file"], f"{db}.token")

    def test_without_an_owner_or_token_file_the_file_is_used(self):
        db = str(self.tmp / "t.db")
        self.assertEqual(
            cli._discover_owner({"gracia.db": db})["gracia.db"], db
        )  # no token file
        (self.tmp / "t.db.token").write_text("0123456789abcdef0123456789\n")
        (self.tmp / "t.db.token").chmod(0o600)
        found = cli._discover_owner(
            {"gracia.db": db}, port=free_port()
        )  # nobody listens
        self.assertEqual(found["gracia.db"], db)

    def test_an_explicit_quack_target_is_left_alone(self):
        settings = {"gracia.db": "quack:localhost:1"}
        self.assertEqual(cli._discover_owner(settings), settings)

    def test_the_owner_file_gives_a_custom_port(self):
        port, db = free_port(), str(self.tmp / "t.db")
        self.owner("t.db", port)
        store.owner_file(db).write_text(f"quack:localhost:{port}\n{db}.token\n")
        found = cli._discover_owner({"gracia.db": db})  # not the default port
        self.assertEqual(found["gracia.db"], f"quack:localhost:{port}")

    def test_serve_db_writes_the_owner_file_and_removes_it_on_stop(self):
        db, port = self.tmp / "s.db", free_port()
        ready, stop = threading.Event(), threading.Event()
        thread = threading.Thread(
            target=store.serve_db,
            args=(db, "localhost", port),
            kwargs={"stop": stop, "ready": lambda *_: ready.set()},
        )
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(stop.set)
        self.assertTrue(ready.wait(30))
        uri, token_file = store.read_owner(db)  # ty: ignore[not-iterable]
        self.assertEqual(uri, f"quack:localhost:{port}")
        self.assertEqual(token_file, str((self.tmp / "s.db.token").resolve()))
        stop.set()
        thread.join()
        self.assertIsNone(store.read_owner(db))
