import threading

import pyoxigraph as ox
from base import DbTestCase

from gracia.graph import Graph
from gracia.store import DuplicateValue, Store

G = "https://gracia.pub/ns/"


def graph(text: str) -> Graph:
    return Graph(ox.Dataset(ox.parse(text.encode(), format=ox.RdfFormat.TURTLE)))


def thing(number: int, value: str = "x") -> Graph:
    return graph(f'<rec:thing:{number}> <http://ex/p> "{value}" .')


class HistoryTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.db)
        self.addCleanup(self.store.close)

    def rows(self):  # ty: ignore[incompatible-method-override]
        return self.store.con.execute(
            "SELECT txn_id, record_uri(record_id), is_deleted, source FROM history "
            "ORDER BY "
            "txn_id, record_id"
        ).fetchall()

    def test_every_write_is_a_transaction_with_the_next_number_and_a_row_for_each_record(  # noqa: E501
        self,
    ):
        s = self.store
        s.put(
            graph('<rec:thing:1> <http://ex/p> "a" . <rec:thing:2> <http://ex/p> "b" .')
        )
        s.put(thing(1, "changed"))  # a replace: a delete and an insert, one row
        rows = self.rows()
        self.assertEqual(
            [(t, u) for t, u, *_ in rows],
            [(1, "rec:thing:1"), (1, "rec:thing:2"), (2, "rec:thing:1")],
        )
        self.assertEqual(
            rows[2][2:], (False, '<rec:thing:1> <http://ex/p> "changed" .\n')
        )
        self.assertEqual(s.last_transaction(), 2)

    def test_a_delete_is_a_transaction_and_leaves_a_tombstone(self):
        s = self.store
        s.put(thing(1))
        s.delete(["rec:thing:1"])
        self.assertEqual(self.rows()[-1], (2, "rec:thing:1", True, ""))

    def test_a_write_that_is_refused_leaves_no_history_and_gives_its_number_back(self):
        s = self.store
        s.set_property_roles([("thing", "unique", "http://ex/p", 0)])
        s.put(thing(1, "same"))
        with self.assertRaises(DuplicateValue):
            s.put(thing(2, "same"))
        self.assertEqual([r[1] for r in self.rows()], ["rec:thing:1"])
        s.put(thing(2, "other"))
        self.assertEqual(
            [r[:2] for r in self.rows()], [(1, "rec:thing:1"), (2, "rec:thing:2")]
        )  # no gap

    def test_what_is_written_outside_the_store_gets_the_next_number_from_the_triggers(
        self,
    ):
        s = self.store
        s.put(thing(1))
        s.con.execute(
            "UPDATE triples SET lit = 'raw' WHERE s = 'rec:thing:1'"
        )  # a rogue edit
        self.assertEqual(
            s.last_transaction(), 2
        )  # a reader that was at 1 knows it is behind
        self.assertEqual(s.changes_since(1), [(2, "rec:thing:1", False)])
        s.con.execute("DELETE FROM triples WHERE s = 'rec:thing:1'")
        self.assertEqual(s.changes_since(2), [(3, "rec:thing:1", True)])
        s.put(thing(2))  # the store goes on from there
        self.assertEqual(s.last_transaction(), 4)
        self.assertEqual([r[0] for r in self.rows()], [1, 2, 3, 4])
        # a raw write that is rolled back gives its number back, like the others
        s.con.begin()
        s.con.execute("DELETE FROM triples WHERE s = 'rec:thing:2'")
        s.con.rollback()
        self.assertEqual(s.last_transaction(), 4)

    def test_the_changes_after_a_number_in_the_order_of_the_transactions(self):
        s = self.store
        s.put(thing(1))
        s.put(thing(2))
        s.delete(["rec:thing:1"])
        self.assertEqual(
            s.changes_since(1), [(2, "rec:thing:2", False), (3, "rec:thing:1", True)]
        )
        self.assertEqual(s.changes_since(3), [])
        self.assertEqual(s.changes_since(0, limit=1), [(1, "rec:thing:1", False)])

    def test_a_rewrite_is_a_transaction_only_when_it_changes_something(self):
        s = self.store
        s.put(thing(1))
        s.configure(
            lambda: (["UPDATE @FLAT@ SET lit = upper(lit) WHERE p = 'http://ex/p'"], [])
        )
        self.assertEqual(s.rederive("thing"), ["rec:thing:1"])
        self.assertEqual(s.last_transaction(), 2)
        self.assertEqual(s.rederive("thing"), [])
        self.assertEqual(
            s.last_transaction(), 2
        )  # nothing changed: no transaction, no row

    def test_writers_at_once_get_numbers_in_the_order_of_their_commits_and_none_twice(
        self,
    ):
        s = self.store
        s.put(thing(100))
        errors = []

        def write(n):
            try:
                with s.fork() as mine:
                    for k in range(5):
                        mine.put(thing(n * 10 + k + 1, str(k)))
            except Exception as err:
                errors.append(err)

        threads = [threading.Thread(target=write, args=(n,)) for n in (1, 2, 3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        numbers = [r[0] for r in self.rows()]
        self.assertEqual(
            numbers, sorted(set(numbers))
        )  # one record to a write, none twice
        self.assertEqual(numbers[-1], 1 + 15)
        self.assertEqual(self.store.last_transaction(), 16)
