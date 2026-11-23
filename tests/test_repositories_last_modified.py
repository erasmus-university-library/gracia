import threading
import time

import pyoxigraph as ox
from base import DbTestCase
from harness import open_store

from gracia.graph import Graph
from gracia.store import fetch_one


def thing(number: int, value: str = "x") -> Graph:
    text = f'<rec:thing:{number}> <http://ex/p> "{value}" .'
    return Graph(ox.Dataset(ox.parse(text.encode(), format=ox.RdfFormat.TURTLE)))


class RepositoriesLastModifiedTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.store = open_store(self.db)
        self.addCleanup(self.store.close)

    def rows(self):  # ty: ignore[incompatible-method-override]
        return dict(
            self.store.con.execute(
                "SELECT repo_id, last_modified_at FROM "
                '"main".repositories_last_modified_at'
            ).fetchall()
        )

    def test_the_table_is_there_and_empty_after_init(self):
        self.assertEqual(self.rows(), {})

    def test_a_write_upserts_one_row_with_the_time_of_the_record(self):
        self.store.put(thing(1))
        first = self.rows()
        self.assertEqual(list(first), ["main"])
        modified = fetch_one(
            self.store.con.execute("SELECT max(modified_at) FROM records")
        )[0]
        self.assertEqual(first["main"], modified)
        time.sleep(0.01)
        self.store.put(thing(1, "changed"))
        second = self.rows()
        self.assertEqual(list(second), ["main"])
        self.assertGreater(second["main"], first["main"])

    def test_each_repository_has_its_own_row(self):
        other = open_store(self.db, repo="other", create=True)
        self.addCleanup(other.close)
        self.store.put(thing(1))
        other.put(thing(1))
        self.assertEqual(sorted(self.rows()), ["main", "other"])

    def test_a_rolled_back_write_does_not_change_the_row(self):
        s = self.store
        s.put(thing(9))  # the type is known now
        before = self.rows()
        s.con.begin()
        s.con.execute(
            "INSERT INTO triples (s, p, lit) VALUES ('rec:thing:1', 'http://ex/p', 'x')"
        )
        s.con.rollback()
        self.assertEqual(self.rows(), before)

    def test_repositories_do_not_block_each_other(self):
        # a writer of repository a holds its transaction open while repository b writes
        # and commits
        a = open_store(self.db, repo="a", create=True)
        b = open_store(self.db, repo="b", create=True)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.put(thing(9))  # the type is known in a
        b.put(thing(9))
        before = self.rows()
        a.con.begin()
        a.con.execute(
            "INSERT INTO triples (s, p, lit) VALUES ('rec:thing:1', 'http://ex/p', 'x')"
        )
        done = threading.Event()
        errors = []

        def write_b():
            try:
                b.put(thing(1))
            except Exception as err:
                errors.append(err)
            done.set()

        thread = threading.Thread(target=write_b)
        thread.start()
        self.assertTrue(
            done.wait(10), "the write of b waited for the open transaction of a"
        )
        thread.join()
        a.con.commit()
        self.assertEqual(errors, [])
        after = self.rows()
        self.assertGreater(after["b"], before["b"])
        self.assertEqual(sorted(after), ["a", "b"])

    def test_concurrent_first_writes_of_many_repositories(self):
        stores = [open_store(self.db, repo=f"r{n}", create=True) for n in range(6)]
        for s in stores:
            self.addCleanup(s.close)
        errors = []

        def write(s):
            try:
                for i in range(1, 6):
                    s.put(thing(i, str(i)))
            except Exception as err:
                errors.append(err)

        threads = [threading.Thread(target=write, args=(s,)) for s in stores]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(self.rows()), [f"r{n}" for n in range(6)])
