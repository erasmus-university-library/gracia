import random
from datetime import UTC, datetime, timedelta

import pyoxigraph as ox
from base import DbTestCase

from gracia import store as store_module
from gracia import uri
from gracia.graph import Graph
from gracia.store import Store, fetch_one

G = "https://gracia.pub/ns/"
TYPES = [
    ("person", 5),
    ("work", 6),
    ("org", 7),
]  # the numbers 1 to 4 are the types a repository starts with
STAMP = "2026-01-01T00:00:00.000000Z"


def record(
    rec_uri: str, refs=(), res=None, stamp=STAMP, created=STAMP, published=True
) -> Graph:
    """One record: its resource uri (its ARK when none is given), what it points at
    (uris), the time it was modified (what `records.modified_at` takes)."""
    number = rec_uri.rsplit(":", 1)[1]
    kind = rec_uri.split(":")[1]
    res = res or f"ark:/1/{kind}/{number}"
    # what a record points at is in its resource, by the resource uri of the other
    # record (the ARK
    # that `record` gives
    # a record that says nothing else: a resource points at resources, never at records)
    pointed = [
        f"ark:/1/{r.split(':')[1]}/{r.rsplit(':', 1)[1]}" if r.startswith("rec:") else r
        for r in refs
    ]
    pointers = (
        " ; g:resource [ "
        + " ; ".join(f"<http://ex/p> <{ref}>" for ref in pointed)
        + " ]"
        if pointed
        else ""
    )
    text = (
        f"@prefix g: <{G}> . @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n"
        f"<{rec_uri}> a g:Record ; g:resource_uri <{res}> ; g:modified_at "
        f'"{stamp}"^^xsd:dateTime '
        "; "
        f'g:created_at "{created}"^^xsd:dateTime ; g:is_published '
        f'"{str(published).lower()}"^^xsd:boolean{pointers} .'
    )
    return Graph(ox.Dataset(ox.parse(text.encode(), format=ox.RdfFormat.TURTLE)))


class EtagCase(DbTestCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.store.set_type_ids(TYPES)

    def put(self, *records: Graph) -> None:
        """The records in one write (a write has a fixed cost of 0.1 s, so the setUp of
        a test that makes 45 records does not make 45 of them)."""
        self.store.put(Graph(ox.Dataset(q for graph in records for q in graph)))

    def etag(self, rec_uri: str):
        found = self.store.etag_info(rec_uri)
        return found[0] if found else None

    def rows(self) -> list[tuple]:  # ty: ignore[incompatible-method-override]
        return [
            tuple(r)
            for r in self.store.con.execute(
                "SELECT * FROM etags ORDER BY record_id"
            ).fetchall()
        ]

    def assert_totals_are_counts(self, message: str = ""):
        """`backref_totals` of every record is what counting the references of all the
        rows gives."""
        found: dict[int, dict[str, list[int]]] = {}
        for target, source_type, published in self.store.con.execute(
            'SELECT unnest("references"), record_type, is_published FROM etags'
        ).fetchall():
            entry = found.setdefault(target, {}).setdefault(source_type, [0, 0])
            entry[0] += 1
            entry[1] += bool(published)
        for record_id, totals in self.store.con.execute(
            "SELECT record_id, backref_totals FROM etags"
        ).fetchall():
            expected = {
                k: {"total_all": a, "total_published": p}
                for k, (a, p) in found.get(record_id, {}).items()
            }
            self.assertEqual(
                dict(totals), expected, f"the totals of {record_id} {message}"
            )

    def assert_made_again_equals_a_rebuild(self, message: str = ""):
        self.assert_totals_are_counts(message)
        stored = [
            r[0]
            for r in self.store.con.execute("SELECT record_id FROM etags").fetchall()
        ]
        ordered = [
            r[0]
            for r in self.store.con.execute(
                "SELECT record_id FROM etags ORDER BY record_type, sort_key DESC, "
                "record_id"
            ).fetchall()
        ]
        self.assertEqual(
            stored,
            ordered,
            f"the table is not in the order a listing reads it {message}",
        )
        before = self.rows()
        self.store.rebuild_etags()
        self.assertEqual(before, self.rows(), message)


class EtagTest(EtagCase):
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

    def test_a_record_has_a_row_with_the_ids_of_what_it_is_made_of(self):
        row = dict(
            zip(
                (
                    "id",
                    "uri",
                    "type",
                    "res",
                    "sort",
                    "published",
                    "etag",
                    "local",
                    "modified",
                    "last",
                    "refs",
                    "deep",
                    "types",
                    "backrefs",
                ),
                self.store.con.execute(  # ty: ignore[invalid-argument-type]
                    "SELECT * FROM etags WHERE record_uri = 'rec:work:1'"
                ).fetchone(),
                strict=False,
            )
        )
        self.assertEqual(row["id"], uri.record_id(6, 1))
        self.assertEqual(row["refs"], [uri.record_id(5, 1)])
        self.assertEqual(
            sorted(row["deep"]), sorted([uri.record_id(5, 1), uri.record_id(7, 1)])
        )
        self.assertEqual(row["types"], ["org", "person", "work"])
        self.assertEqual(
            row["sort"], 1767225600000000
        )  # when it was created: 2026-01-01 as microseconds
        self.assertIsInstance(
            row["etag"], int
        )  # the XOR of the local hashes of the record and what it is made of
        self.assertRegex(
            self.etag("rec:work:1"), r"^[0-9a-f]{16}$"
        )  # shown as 16 hex digits

        def local(uri_):
            return fetch_one(
                self.store.con.execute(
                    "SELECT local_hash FROM etags WHERE record_uri = ?", [uri_]
                )
            )[0]

        self.assertEqual(
            row["etag"],
            local("rec:work:1") ^ local("rec:person:1") ^ local("rec:org:1"),
        )
        self.assertEqual(
            self.store.page_uris("rec:work:1"),
            ["rec:org:1", "rec:person:1", "rec:work:1"],
        )

    def test_the_python_id_is_the_one_of_the_database(self):
        for slug, number in TYPES:
            found = fetch_one(
                self.store.con.execute("SELECT record_id(?)", [f"rec:{slug}:{7}"])
            )[0]
            self.assertEqual(found, uri.record_id(number, 7))
        self.assertIsNone(
            fetch_one(self.store.con.execute("SELECT record_id(NULL)"))[0]
        )
        for unknown in (
            "rec:nothing:7",
            "ark:/1/person/7",
            "rec:person",
            "person:7",
        ):  # not the uri of a record that has an id
            with (
                self.subTest(uri=unknown),
                self.assertRaisesRegex(Exception, "unknown record uri"),
            ):
                self.store.con.execute("SELECT record_id(?)", [unknown])
        for bad in (
            "rec:person:10000000",
            "rec:person:abc",
            "rec:person:-3",
            "rec:person:",
        ):  # a record uri with no number
            with (
                self.subTest(uri=bad),
                self.assertRaisesRegex(Exception, "Conversion Error"),
            ):
                self.store.con.execute("SELECT record_id(?)", [bad])
        with self.assertRaises(ValueError):
            uri.record_id(1, 10**7)
        with self.assertRaises(ValueError):
            uri.record_id(100, 1)
        self.assertEqual(
            uri.record_id(99, 9_999_999), 999_999_999
        )  # the highest id fits an INTEGER
        self.assertEqual(
            fetch_one(
                self.store.con.execute("SELECT typeof(record_id) FROM etags LIMIT 1")
            )[0],
            "INTEGER",
        )

    def test_it_changes_with_what_the_page_is_made_of_and_with_nothing_else(self):
        first = self.etag("rec:work:1")
        self.put(
            record("rec:person:2", stamp="2026-02-01T00:00:00.000000Z")
        )  # not part of it
        self.assertEqual(self.etag("rec:work:1"), first)
        self.put(
            record("rec:org:1", stamp="2026-02-01T00:00:00.000000Z")
        )  # two steps away
        self.assertNotEqual(self.etag("rec:work:1"), first)
        self.assertNotEqual(self.etag("rec:person:1"), first)

    def test_a_save_that_changes_nothing_leaves_the_etag(self):
        before = self.rows()
        self.put(record("rec:work:1", refs=["rec:person:1"]))
        self.assertEqual(self.rows(), before)

    def test_a_record_that_points_somewhere_else_has_another_page(self):
        first = self.etag("rec:work:1")
        self.put(record("rec:work:1", refs=["rec:person:2"]))
        self.assertNotEqual(self.etag("rec:work:1"), first)
        self.assertEqual(
            self.store.page_uris("rec:work:1"), ["rec:person:2", "rec:work:1"]
        )

    def test_a_reference_to_a_record_that_is_not_there_yet_is_found_when_it_is_made(
        self,
    ):
        self.put(record("rec:work:3", refs=["rec:person:9"]))
        waiting = self.etag("rec:work:3")
        self.assertEqual(self.store.page_uris("rec:work:3"), ["rec:work:3"])
        self.put(record("rec:person:9"))
        self.assertNotEqual(self.etag("rec:work:3"), waiting)
        self.assertEqual(
            self.store.page_uris("rec:work:3"), ["rec:person:9", "rec:work:3"]
        )
        self.assert_made_again_equals_a_rebuild()

    def test_a_reference_by_resource_uri_follows_the_resource_uri_of_the_record(self):
        self.put(record("rec:work:3", refs=["urn:orcid:42"]))
        self.assertEqual(self.store.page_uris("rec:work:3"), ["rec:work:3"])
        self.put(
            record("rec:person:2", res="urn:orcid:42")
        )  # now there is a record with that uri
        self.assertEqual(
            self.store.page_uris("rec:work:3"), ["rec:person:2", "rec:work:3"]
        )
        self.put(record("rec:person:2", res="urn:orcid:43"))  # and now there is not
        self.assertEqual(self.store.page_uris("rec:work:3"), ["rec:work:3"])
        self.assert_made_again_equals_a_rebuild()

    def test_a_deleted_record_has_no_row_and_the_pages_made_of_it_change(self):
        first = self.etag("rec:work:1")
        self.store.delete(["rec:person:1"])
        self.assertIsNone(self.etag("rec:person:1"))
        self.assertNotEqual(self.etag("rec:work:1"), first)
        self.assertEqual(self.store.page_uris("rec:work:1"), ["rec:work:1"])
        self.assert_made_again_equals_a_rebuild()

    def test_a_cycle_ends(self):
        self.put(
            record("rec:person:1", refs=["rec:org:1", "rec:work:1"])
        )  # work 1 -> person 1 -> work 1
        self.assertEqual(
            self.store.page_uris("rec:work:1"),
            ["rec:org:1", "rec:person:1", "rec:work:1"],
        )
        self.assert_made_again_equals_a_rebuild()

    def test_the_time_of_the_record_that_the_page_is_made_of_is_the_last_one(self):
        self.put(record("rec:org:1", stamp="2027-05-05T00:00:00.000000Z"))
        found = fetch_one(
            self.store.con.execute(
                "SELECT last_modified_at FROM etags WHERE record_uri = 'rec:work:1'"
            )
        )[0]
        self.assertEqual((found.year, found.month), (2027, 5))


class TimeOnlyGoesUpTest(EtagCase):
    """`last_modified_at` of a page never goes down, and is the time of the write
    whenever its etag changes, also when it loses the record that was the latest (the
    stamps are in the past: this store has no hooks)."""

    def setUp(self):
        super().setUp()
        self.put(
            record("rec:org:1"),
            record("rec:person:1", refs=["rec:org:1"]),
            record("rec:work:1", refs=["rec:person:1"]),
            record("rec:work:2"),
        )
        self.put(
            record("rec:org:1", stamp="2026-05-05T00:00:00.000000Z", res="ark:/1/org/1")
        )

    def last(self, rec_uri: str):
        return fetch_one(
            self.store.con.execute(
                "SELECT last_modified_at FROM etags WHERE record_uri = ?", [rec_uri]
            )
        )[0]

    def test_a_page_that_loses_the_latest_record_by_its_deletion_gets_the_time_of_the_write(  # noqa: E501
        self,
    ):
        before = self.last("rec:work:1")
        self.store.delete(["rec:org:1"])
        self.assertEqual(
            self.store.page_uris("rec:work:1"), ["rec:person:1", "rec:work:1"]
        )
        self.assertGreater(self.last("rec:work:1"), before)
        self.assert_made_again_equals_a_rebuild()

    def test_a_page_that_loses_the_latest_record_by_its_resource_uri_gets_the_time_of_the_write(  # noqa: E501
        self,
    ):
        before = self.last("rec:work:1")
        self.put(
            record(
                "rec:org:1", res="ark:/1/moved/1", stamp="2026-05-05T00:00:00.000000Z"
            )
        )  # nothing points at it now
        self.assertEqual(
            self.store.page_uris("rec:work:1"), ["rec:person:1", "rec:work:1"]
        )
        self.assertGreater(self.last("rec:work:1"), before)
        self.assert_made_again_equals_a_rebuild()

    def test_a_page_that_stops_pointing_at_the_latest_record_gets_the_time_of_the_write(
        self,
    ):
        before = self.last("rec:work:1")
        self.put(record("rec:person:1"))  # no longer points at org 1
        self.assertGreater(self.last("rec:work:1"), before)
        self.assert_made_again_equals_a_rebuild()

    def test_a_rebuild_does_not_set_a_page_back_or_forward(self):
        self.store.delete(["rec:org:1"])
        kept = self.last("rec:work:1")
        self.store.rebuild_etags()
        self.assertEqual(self.last("rec:work:1"), kept)

    def test_a_page_whose_etag_is_the_same_keeps_its_time(self):
        before = {u: self.last(u) for u in ("rec:work:1", "rec:work:2", "rec:person:1")}
        self.put(record("rec:work:2"))  # nothing changed
        self.assertEqual({u: self.last(u) for u in before}, before)

    def test_every_page_that_changed_has_the_time_of_the_write_and_no_other_has_(self):
        before = {
            u: (self.etag(u), self.last(u))
            for u in ("rec:work:1", "rec:work:2", "rec:person:1", "rec:org:1")
        }
        self.store.delete(["rec:org:1"])
        for rec_uri in ("rec:work:1", "rec:person:1"):  # made of org 1
            self.assertNotEqual(self.etag(rec_uri), before[rec_uri][0])
            self.assertGreater(self.last(rec_uri), before[rec_uri][1])
        self.assertEqual(
            (self.etag("rec:work:2"), self.last("rec:work:2")), before["rec:work:2"]
        )  # not made of it


class WhenTheyAreMadeTest(EtagCase):
    def test_a_record_of_a_type_without_a_number_cannot_be_stored_until_the_type_has_one(  # noqa: E501
        self,
    ):
        fresh = Store(self.tmp / "other.db")
        self.addCleanup(fresh.close)
        with self.assertRaisesRegex(Exception, "unknown record uri: rec:nobody:1"):
            fresh.put(record("rec:nobody:1"))
        self.assertEqual(
            fetch_one(fresh.con.execute("SELECT count(*) FROM records"))[0], 0
        )
        fresh.set_type_ids([("nobody", 41)])
        fresh.put(record("rec:nobody:1"))
        self.assertIsNotNone(fresh.etag_info("rec:nobody:1"))
        with self.assertRaisesRegex(
            Exception, "Conversion Error"
        ):  # nor a number that is too high
            fresh.put(record("rec:nobody:10000000"))

    def test_a_rebuild_says_which_etags_were_wrong(self):
        self.put(
            record("rec:org:1"),
            record("rec:work:1", refs=["rec:org:1"]),
            record("rec:person:1"),
        )
        self.assertEqual(
            self.store.rebuild_etags(), []
        )  # kept up to date: nothing was wrong
        self.assertEqual(self.store.count_etags(), 3)
        self.store.con.execute(
            "UPDATE etags SET etag = 12345 WHERE record_uri = 'rec:work:1'"
        )
        self.store.con.execute("DELETE FROM etags WHERE record_uri = 'rec:person:1'")
        self.store.con.execute(
            "INSERT INTO etags SELECT * REPLACE ('rec:ghost:1' AS record_uri) FROM "
            "etags WHERE "
            "record_uri = 'rec:org:1'"
        )
        self.assertEqual(
            self.store.rebuild_etags(), ["rec:ghost:1", "rec:person:1", "rec:work:1"]
        )
        self.assertEqual(self.store.rebuild_etags(), [])

    def test_a_type_keeps_its_number_and_a_new_type_changes_no_id(self):
        self.put(record("rec:work:1"))
        before = self.rows()
        self.assertFalse(self.store.set_type_ids(TYPES))  # the same: nothing
        self.assertTrue(
            self.store.set_type_ids([("magazine", 40)])
        )  # a new type: the ids of the others stay
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.store.type_numbers()["magazine"], 40)
        with self.assertRaisesRegex(ValueError, "keeps its number"):
            self.store.set_type_ids([("work", 9)])
        with self.assertRaisesRegex(ValueError, "one number"):
            self.store.set_type_ids([("gadget", 40)])
        self.assertEqual(self.rows(), before)
        self.put(record("rec:magazine:1"))  # the new type has records now
        self.assertIsNotNone(self.etag("rec:magazine:1"))

    def test_a_reopened_store_knows_the_numbers_of_its_types(self):
        self.store.set_type_ids([("magazine", 40)])
        again = Store(self.db)
        self.addCleanup(again.close)
        expected = {**uri.BASE_TYPE_NUMBERS, **dict(TYPES), "magazine": 40}
        self.assertEqual(
            {k: v for k, v in again.type_numbers().items() if k in expected}, expected
        )
        self.assertEqual(
            fetch_one(
                again.con.execute("SELECT resource_uri(record_id('rec:magazine:12'))")
            )[0],
            "rec:magazine:12",
        )
        with self.assertRaisesRegex(Exception, "unknown record uri"):
            again.con.execute("SELECT resource_uri(record_id('rec:nobody:12'))")

    def test_a_write_made_outside_the_store_is_made_up_for_by_the_next(self):
        self.put(record("rec:org:1"), record("rec:work:1", refs=["rec:org:1"]))
        first = self.etag("rec:work:1")
        # the database shell: no store, no refresh (the triggers still keep `records`
        # and the
        # history)
        self.store.con.execute(
            "INSERT INTO triples VALUES ('rec:org:1', [], ?, NULL, "
            "'2027-01-01T00:00:00.000000Z', "
            "NULL, ?)",
            [G + "modified_at", "http://www.w3.org/2001/XMLSchema#dateTime"],
        )
        self.assertEqual(self.etag("rec:work:1"), first)  # not yet
        self.put(record("rec:person:2"))  # any write of the store
        self.assertNotEqual(self.etag("rec:work:1"), first)
        self.assert_made_again_equals_a_rebuild()

    def test_a_write_that_fails_leaves_the_etags_as_they_were(self):
        self.put(record("rec:org:1"))
        before = self.rows()
        with self.assertRaises(ValueError):
            self.store.put(
                Graph(
                    ox.Dataset(
                        ox.parse(
                            b'<http://example.org/x> <http://ex/p> "x" .',
                            format=ox.RdfFormat.TURTLE,
                        )
                    )
                )
            )
        self.assertEqual(self.rows(), before)

    def test_many_records_at_once_are_made_in_one_refresh_and_equal_a_rebuild(self):
        """There is no limit on how many records a write changes: the refresh does them
        all."""
        self.put(record("rec:org:1"))
        batch = Graph(
            ox.Dataset(
                q
                for n in range(1, 41)
                for q in record(f"rec:work:{n}", refs=["rec:org:1"])
            )
        )
        self.store.put(batch)
        self.assertEqual(len(self.rows()), 41)
        self.assert_made_again_equals_a_rebuild()
        again = Graph(
            ox.Dataset(
                q
                for n in range(1, 41)
                for q in record(
                    f"rec:work:{n}",
                    refs=["rec:org:1", "rec:person:1"],
                    stamp="2027-02-01T00:00:00.000000Z",
                )
            )
        )
        self.put(record("rec:person:1"))
        self.store.put(again)  # all of them again, and now pointing at a person too
        self.assertEqual(self.store.referrers("rec:person:1").total, 40)
        self.assert_made_again_equals_a_rebuild()

    def test_the_records_made_of_many_changed_ones_are_found_by_unnesting(self):
        self.put(
            record("rec:org:1"),
            *[record(f"rec:work:{n}", refs=["rec:org:1"]) for n in range(1, 6)],
        )
        old = store_module.ETAGS_CONTAINS_UP_TO
        store_module.ETAGS_CONTAINS_UP_TO = 0  # ty: ignore[invalid-assignment]
        self.addCleanup(setattr, store_module, "ETAGS_CONTAINS_UP_TO", old)
        before = {r[1]: r[6] for r in self.rows()}
        self.put(record("rec:org:1", stamp="2027-01-01T00:00:00.000000Z"))
        after = {r[1]: r[6] for r in self.rows()}
        self.assertTrue(
            all(before[u] != after[u] for u in before)
        )  # all six pages are made of it
        self.assert_made_again_equals_a_rebuild()


def created(n: int) -> str:
    """When work n was created: one minute after the one before, so that a bigger number
    is newer."""
    return f"2026-03-01T{n // 60:02d}:{n % 60:02d}:00.000000Z"


def microseconds(text: str) -> int:
    when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return (when - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)


class PagingTest(EtagCase):
    """`Store.referrers`: the records that point at one, a page of them, numbered from
    the oldest."""

    def setUp(self):
        super().setUp()
        self.put(record("rec:person:1"))
        self.count = 0

    def add(self, n: int = 1, **kw):
        first = self.count + 1
        self.count += n
        if n:
            self.put(
                *[
                    record(
                        f"rec:work:{k}", refs=["rec:person:1"], created=created(k), **kw
                    )
                    for k in range(first, first + n)
                ]
            )

    def numbers(self, page) -> list[int]:
        return [int(u.rsplit(":", 1)[1]) for u, _ in page.items]

    def physical(self) -> list[int]:
        return [
            r[0]
            for r in self.store.con.execute("SELECT record_id FROM etags").fetchall()
        ]

    def test_the_table_is_kept_newest_created_first_whatever_was_written(self):
        self.add(12)
        self.put(
            record(
                "rec:work:3",
                refs=["rec:person:1"],
                created=created(3),
                stamp="2027-01-01T00:00:00.000000Z",
            )
        )
        self.store.delete(["rec:work:5"])
        self.put(record("rec:person:1", refs=["rec:work:2"]))
        sorted_ = [
            r[0]
            for r in self.store.con.execute(
                "SELECT record_id FROM etags ORDER BY record_type, sort_key DESC, "
                "record_id"
            ).fetchall()
        ]
        self.assertEqual(self.physical(), sorted_)  # as stored, with no ORDER BY

    def test_the_pages_have_twenty_records_and_the_last_has_the_rest(self):
        sizes = {
            0: [0],
            1: [1],
            19: [19],
            20: [20],
            21: [20, 1],
            39: [20, 19],
            40: [20, 20],
            41: [20, 20, 1],
        }
        for total in sorted(sizes):
            self.add(total - self.count)
            found = [
                len(self.store.referrers("rec:person:1", page=n).items)
                for n in range(1, len(sizes[total]) + 1)
            ]
            self.assertEqual(found, sizes[total], f"{total} records")
            first = self.store.referrers("rec:person:1")  # no page asked for: page 1
            self.assertEqual(
                (first.page, first.pages, first.total), (1, len(sizes[total]), total)
            )

    def test_page_one_is_the_newest_and_the_pages_go_back_in_time_with_the_newest_first_on_a_page(  # noqa: E501
        self,
    ):
        self.add(45)
        pages = [self.store.referrers("rec:person:1", page=n) for n in (1, 2, 3)]
        self.assertEqual(
            self.numbers(pages[0]), list(range(45, 25, -1))
        )  # the newest 20
        self.assertEqual(self.numbers(pages[1]), list(range(25, 5, -1)))
        self.assertEqual(
            self.numbers(pages[2]), list(range(5, 0, -1))
        )  # what is left, the oldest
        self.assertEqual(
            self.numbers(self.store.referrers("rec:person:1")), self.numbers(pages[0])
        )  # the default

    def test_a_new_record_is_first_on_page_one_and_moves_the_others_down_a_place(self):
        """Page numbers are the ordinary kind and are not stable: every page changes
        when one is added."""
        self.add(45)
        before = [
            self.numbers(self.store.referrers("rec:person:1", page=n))
            for n in (1, 2, 3)
        ]
        self.add(1)
        after = [
            self.numbers(self.store.referrers("rec:person:1", page=n))
            for n in (1, 2, 3)
        ]
        self.assertEqual(
            after[0], [46, *before[0][:-1]]
        )  # the new one first, the last of the page went down
        self.assertEqual(after[1], [before[0][-1], *before[1][:-1]])
        self.assertEqual(after[2], [before[1][-1], *before[2]])

    def test_editing_a_record_does_not_move_it_but_changes_its_etag(self):
        self.add(45)
        before = self.store.referrers(
            "rec:person:1", page=2
        )  # work 7 is on page 2 (25 down to 6)
        self.put(
            record(
                "rec:work:7",
                refs=["rec:person:1"],
                created=created(7),
                stamp="2027-01-01T00:00:00.000000Z",
            )
        )
        after = self.store.referrers("rec:person:1", page=2)
        self.assertEqual(self.numbers(after), self.numbers(before))
        changed = [
            u
            for (u, a), (_, b) in zip(after.items, before.items, strict=False)
            if a != b
        ]
        self.assertEqual(changed, ["rec:work:7"])

    def test_both_totals_are_known_and_the_pages_count_the_ones_asked_for(self):
        self.add(30)
        self.add(
            25, published=False
        )  # 55 in all, 30 of them published (the unpublished are the newest)
        everyone = self.store.referrers("rec:person:1")
        self.assertEqual(
            (everyone.total, everyone.total_all, everyone.total_published), (30, 55, 30)
        )
        self.assertEqual((everyone.pages, len(everyone.items)), (2, 20))
        self.assertEqual(
            self.numbers(everyone)[:2], [30, 29]
        )  # the unpublished are not in it
        every = self.store.referrers("rec:person:1", published=False)
        self.assertEqual(
            (every.total, every.total_all, every.total_published, every.pages),
            (55, 55, 30, 3),
        )
        self.assertEqual(
            self.numbers(self.store.referrers("rec:person:1", page=2, published=False))[
                :3
            ],
            [35, 34, 33],
        )

    def test_a_listing_can_be_of_one_type(self):
        self.add(25)
        self.put(record("rec:person:2", refs=["rec:person:1"], created=created(99)))
        both = self.store.referrers("rec:person:1")
        works = self.store.referrers("rec:person:1", source_type="work")
        people = self.store.referrers("rec:person:1", source_type="person")
        self.assertEqual((both.total, works.total, people.total), (26, 25, 1))
        self.assertEqual(people.items[0][0], "rec:person:2")

    def test_a_page_that_is_not_there_is_an_error_and_a_record_nothing_points_at_has_an_empty_listing(  # noqa: E501
        self,
    ):
        self.add(45)
        for page in (0, 4, -1):
            with self.assertRaises(ValueError):
                self.store.referrers("rec:person:1", page=page)
        nobody = self.store.referrers("rec:work:1")
        self.assertEqual((nobody.items, nobody.total, nobody.pages), ([], 0, 1))
        unknown = self.store.referrers("rec:nothing:1")  # a type without a number
        self.assertEqual((unknown.items, unknown.total), ([], 0))

    def test_random_writes_edits_and_deletes_give_the_pages_a_model_gives(self):
        rng = random.Random(7)
        model: dict[int, tuple[int, bool]] = {}  # work -> (created, published)
        for step in range(90):
            n = rng.randint(1, 60)
            if n in model and rng.random() < 0.25:
                self.store.delete([f"rec:work:{n}"])
                del model[n]
            else:
                when = (
                    model[n][0]
                    if n in model
                    else microseconds(created(rng.randint(0, 600)))
                )
                published = rng.random() < 0.8
                stamp = f"2027-01-{1 + step % 28:02d}T00:00:{step % 60:02d}.000000Z"
                text = (
                    datetime.fromtimestamp(when / 1e6, UTC).strftime(
                        "%Y-%m-%dT%H:%M:%S.%f"
                    )
                    + "Z"
                )
                self.put(
                    record(
                        f"rec:work:{n}",
                        refs=["rec:person:1"],
                        created=text,
                        stamp=stamp,
                        published=published,
                    )
                )
                model[n] = (when, published)
            for only_published in (True, False):
                chosen = [
                    (w, c) for w, (c, p) in model.items() if p or not only_published
                ]
                chosen.sort(
                    key=lambda item: (-item[1], uri.record_id(6, item[0]))
                )  # newest created first
                newest_first = [w for w, _ in chosen]
                pages = max(1, -(-len(newest_first) // 20))
                listing = self.store.referrers("rec:person:1", published=only_published)
                self.assertEqual(
                    (listing.pages, listing.total),
                    (pages, len(newest_first)),
                    f"step {step}",
                )
                self.assertEqual(
                    (listing.total_all, listing.total_published),
                    (len(model), sum(1 for _, (_, p) in model.items() if p)),
                    f"step {step}",
                )
                for page in range(1, pages + 1):
                    got = self.numbers(
                        self.store.referrers(
                            "rec:person:1", page=page, published=only_published
                        )
                    )
                    self.assertEqual(
                        got,
                        newest_first[(page - 1) * 20 : page * 20],
                        f"step {step}, page {page}, published={only_published}",
                    )


class RecordsPageTest(EtagCase):
    """`Store.records_page`: a page of the records of a type, numbered like
    `referrers`."""

    def setUp(self):
        super().setUp()
        self.put(
            *[
                record(f"rec:work:{n}", created=created(n), published=n % 5 != 0)
                for n in range(1, 46)
            ],
            record("rec:person:1"),
        )  # another type: not in the listing

    def numbers(self, page) -> list[int]:
        return [int(u.rsplit(":", 1)[1]) for u, _ in page.items]

    def test_the_published_records_are_the_listing_and_both_totals_are_known(self):
        listing = self.store.records_page("work")
        self.assertEqual(
            (listing.total, listing.total_all, listing.total_published, listing.pages),
            (36, 45, 36, 2),
        )
        self.assertEqual(
            self.numbers(listing)[:3], [44, 43, 42]
        )  # 45 is not published; newest first
        every = self.store.records_page("work", published=False)
        self.assertEqual((every.total, every.pages), (45, 3))
        self.assertEqual(
            self.numbers(self.store.records_page("work", page=1, published=False)),
            list(range(45, 25, -1)),
        )
        self.assertEqual(
            self.numbers(self.store.records_page("work", page=3, published=False)),
            list(range(5, 0, -1)),
        )

    def test_a_page_that_is_not_there_is_an_error_and_a_type_with_no_records_has_an_empty_page(  # noqa: E501
        self,
    ):
        for page in (0, 4):
            with self.assertRaises(ValueError):
                self.store.records_page("work", page=page, published=False)
        empty = self.store.records_page("org")
        self.assertEqual((empty.items, empty.total, empty.pages), ([], 0, 1))

    def test_a_repository_that_has_no_etags_yet_gives_the_same_pages_from_the_records(
        self,
    ):
        bare = Store(self.tmp / "bare.db")  # its etags are gone
        self.addCleanup(bare.close)
        bare.set_type_ids(TYPES)
        bare.put(
            Graph(
                ox.Dataset(
                    q
                    for n in range(1, 46)
                    for q in record(
                        f"rec:work:{n}", created=created(n), published=n % 5 != 0
                    )
                )
            )
        )
        bare.con.execute("DELETE FROM etags")
        self.assertEqual(bare.count_etags(), 0)
        for published in (True, False):
            for page in range(
                1, self.store.records_page("work", published=published).pages + 1
            ):
                kept = self.store.records_page("work", page=page, published=published)
                fallback = bare.records_page("work", page=page, published=published)
                self.assertEqual(
                    self.numbers(fallback),
                    self.numbers(kept),
                    f"page {page}, published={published}",
                )
                self.assertEqual(
                    (fallback.total_all, fallback.total_published),
                    (kept.total_all, kept.total_published),
                )
        self.assertTrue(
            all(etag == "" for _, etag in bare.records_page("work").items)
        )  # no etag to give

    def test_the_labels_and_slugs_of_records(self):
        found = self.store.labels(["rec:work:1", "rec:work:2", "rec:work:999"])
        self.assertEqual(
            sorted(found), ["rec:work:1", "rec:work:2"]
        )  # one that is not there is not in it
        self.assertEqual(
            found["rec:work:1"], ("", None)
        )  # made without a label or a slug
        self.assertEqual(self.store.labels([]), {})


class OracleTest(EtagCase):
    """Random writes: after every one, the etags that were made are those a rebuild
    makes."""

    def test_random_writes_deletes_and_references_always_equal_a_rebuild(self):
        rng = random.Random(20261112)
        uris = [
            f"rec:{slug}:{n}" for slug in ("person", "work", "org") for n in range(1, 9)
        ]
        resource_uris = {
            u: f"ark:/1/{u.split(':')[1]}/{u.rsplit(':', 1)[1]}" for u in uris
        }
        alive: set[str] = set()
        clock = 0
        for step in range(70):
            target = rng.choice(uris)
            action = rng.random()
            times = dict(
                self.store.con.execute(
                    "SELECT record_uri, last_modified_at FROM etags"
                ).fetchall()
            )
            if action < 0.15 and alive:
                gone = rng.choice(sorted(alive))
                self.store.delete([gone])
                alive.discard(gone)
            else:
                clock += 1
                pool = (
                    uris
                    + list(resource_uris.values())
                    + ["rec:person:99", "urn:nothing"]
                )
                refs = rng.sample(pool, rng.randint(0, 4))
                res = (
                    resource_uris[target]
                    if rng.random() < 0.8
                    else f"ark:/1/moved/{step}"
                )
                resource_uris[target] = res
                self.store.put(
                    record(
                        target,
                        refs=refs,
                        res=res,
                        stamp=(
                            f"2026-03-{1 + clock % 28:02d}"
                            f"T00:00:{clock % 60:02d}.000000Z"
                        ),
                    )
                )
                alive.add(target)
            self.assert_made_again_equals_a_rebuild(f"after step {step}")
            self.assertEqual({r[1] for r in self.rows()}, alive)
            now = dict(
                self.store.con.execute(
                    "SELECT record_uri, last_modified_at FROM etags"
                ).fetchall()
            )
            for (
                rec_uri,
                before,
            ) in (
                times.items()
            ):  # the stamps of this test go back and forth: the times do not
                if rec_uri in now:
                    self.assertGreaterEqual(
                        now[rec_uri],
                        before,
                        f"{rec_uri} went back in time after step {step}",
                    )


class CommandTest(DbTestCase):
    with_record = True  # the repository `main`, with its types

    def test_the_command_makes_all_of_them_again(self):
        code, out, err = self.gracia("etags", "rebuild")
        self.assertEqual(code, 0, err)
        made = int(out.split()[1])
        self.assertRegex(out, r"^made \d+ etags, \d+ of them different")
        self.assertGreater(made, 4)  # the repository record, its types, the groups
        with Store(self.db) as store:
            self.assertEqual(
                fetch_one(store.con.execute("SELECT count(*) FROM etags"))[0], made
            )
            self.assertIsNotNone(store.etag_info("rec:repository:1"))
            store.con.execute("DELETE FROM etags")
        code, out, _ = self.gracia("etags", "rebuild")
        self.assertEqual(int(out.split()[1]), made)  # the same again, from nothing
        self.assertIn(f"{made} of them different", out)  # every one was missing


class IndexTest(DbTestCase):
    """No index but the ones that enforce a unique constraint: they make the commit of a
    large write slow, and the commit holds up every other repository (records.sql,
    etags.sql)."""

    def indexes(self, store) -> list[str]:
        rows = store.con.execute(
            "SELECT index_name FROM duckdb_indexes() WHERE schema_name = ? ORDER BY 1",
            [store.repo],
        ).fetchall()
        return [r[0] for r in rows]

    def test_a_new_repository_has_no_index_that_is_not_a_constraint(self):
        store = Store(self.db)
        self.addCleanup(store.close)
        store.set_type_ids(TYPES)
        store.put(record("rec:work:1"))
        self.assertEqual(self.indexes(store), [])
        constraints = {
            (r[0], r[1])
            for r in store.con.execute(
                "SELECT table_name, constraint_type FROM duckdb_constraints() "
                "WHERE schema_name = ? AND constraint_type IN ('PRIMARY KEY', "
                "'UNIQUE')",
                [store.repo],
            ).fetchall()
        }
        self.assertTrue(
            {
                ("records", "PRIMARY KEY"),
                ("slugs", "PRIMARY KEY"),
                ("unique_values", "UNIQUE"),
            }
            <= constraints
        )


class DeltaTest(EtagCase):
    """Many records are made of the one that changed: they are not walked again when
    what it points at is the same, their etag is moved by the change of its local hash.
    Every case ends in the same etags as a rebuild makes."""

    def setUp(self):
        super().setUp()
        old = store_module.ETAGS_DELTA_ABOVE
        store_module.ETAGS_DELTA_ABOVE = 3  # "many" is more than three  # ty: ignore[invalid-assignment]
        self.addCleanup(setattr, store_module, "ETAGS_DELTA_ABOVE", old)
        # org 1 is the hub; person 1 is in it; work n is by person 1 and in org 1 too (a
        # shared
        # dependency: org 1 is reached by two ways, which a sum or a composition would
        # count twice)
        self.put(
            record("rec:org:1"),
            record("rec:org:2"),
            record("rec:person:1", refs=["rec:org:1"]),
        )
        self.put(
            *[
                record(f"rec:work:{n}", refs=["rec:person:1", "rec:org:1"])
                for n in range(1, 8)
            ]
        )
        self.walked: list[int] = []
        real = Store._closure_etags

        def spy(store):
            self.walked.append(
                store.con.execute("SELECT count(*) FROM etags_todo").fetchone()[0]
            )
            real(store)

        Store._closure_etags = spy  # ty: ignore[invalid-assignment]
        self.addCleanup(setattr, Store, "_closure_etags", real)

    def etags_of_works(self):
        return {n: self.etag(f"rec:work:{n}") for n in range(1, 8)}

    def test_a_rename_of_a_hub_does_not_walk_the_records_made_of_it(self):
        before, last = (
            self.etags_of_works(),
            fetch_one(
                self.store.con.execute(
                    "SELECT last_modified_at FROM etags WHERE record_uri = 'rec:work:1'"
                )
            )[0],
        )
        self.put(record("rec:org:1", stamp="2027-06-01T00:00:00.000000Z"))
        self.assertEqual(
            self.walked, [1]
        )  # the hub only: the person and the seven works were not walked
        after = self.etags_of_works()
        self.assertTrue(all(before[n] != after[n] for n in before))
        moved = fetch_one(
            self.store.con.execute(
                "SELECT last_modified_at FROM etags WHERE record_uri = 'rec:work:1'"
            )
        )[0]
        self.assertEqual((last.year, moved.year, moved.month), (2026, 2027, 6))
        self.assert_made_again_equals_a_rebuild()

    def test_a_hub_that_points_somewhere_else_is_walked_again_with_the_records_made_of_it(  # noqa: E501
        self,
    ):
        self.put(
            record("rec:org:1", refs=["rec:org:2"], stamp="2027-06-01T00:00:00.000000Z")
        )
        self.assertEqual(
            self.walked, [1 + 1 + 7]
        )  # the hub, the person and the seven works
        self.assertEqual(
            self.store.page_uris("rec:work:1"),
            ["rec:org:1", "rec:org:2", "rec:person:1", "rec:work:1"],
        )
        self.assert_made_again_equals_a_rebuild()

    def test_a_hub_that_is_written_with_an_earlier_time_does_not_set_the_pages_back(
        self,
    ):
        """The store makes the times (`gracia_modified_at.py`), so a record never goes
        back in time; a write that does anyway (this store has no hooks) is not walked
        for it, and the latest time of the pages made of it stays where it was."""
        self.put(record("rec:org:1", stamp="2027-06-01T00:00:00.000000Z"))
        self.walked.clear()
        self.put(record("rec:org:1", stamp="2026-03-01T00:00:00.000000Z"))
        self.assertEqual(self.walked, [1])  # the hub only
        last = fetch_one(
            self.store.con.execute(
                "SELECT last_modified_at FROM etags WHERE record_uri = 'rec:work:1'"
            )
        )[0]
        self.assertEqual((last.year, last.month), (2027, 6))
        self.assert_made_again_equals_a_rebuild()

    def test_a_hub_that_is_deleted_is_walked_again_with_the_records_made_of_it(self):
        self.store.delete(["rec:org:1"])
        self.assertEqual(self.walked, [1 + 1 + 7])
        self.assertEqual(
            self.store.page_uris("rec:work:1"), ["rec:person:1", "rec:work:1"]
        )
        self.assert_made_again_equals_a_rebuild()

    def test_two_records_changed_at_once_move_the_etags_by_both_changes(self):
        both = Graph(
            ox.Dataset(
                list(record("rec:org:1", stamp="2027-06-01T00:00:00.000000Z"))
                + list(
                    record(
                        "rec:person:1",
                        refs=["rec:org:1"],
                        stamp="2027-07-01T00:00:00.000000Z",
                    )
                )
            )
        )
        before = self.etags_of_works()
        self.store.put(both)
        self.assertEqual(self.walked, [2])  # the two seeds
        self.assertTrue(all(before[n] != self.etag(f"rec:work:{n}") for n in before))
        self.assert_made_again_equals_a_rebuild()

    def test_a_record_made_of_a_changed_one_by_two_ways_changes_once_not_twice(self):
        """Work 1 reaches org 1 directly and through person 1: XORing the change in
        twice would cancel it, and the etag would not change."""
        before = self.etag("rec:work:1")
        self.put(record("rec:org:1", stamp="2027-01-01T00:00:00.000000Z"))
        self.assertNotEqual(self.etag("rec:work:1"), before)
        self.assert_made_again_equals_a_rebuild()

    def test_a_few_records_made_of_it_are_just_walked_again(self):
        store_module.ETAGS_DELTA_ABOVE = 200
        self.put(record("rec:org:1", stamp="2027-06-01T00:00:00.000000Z"))
        self.assertEqual(self.walked, [1 + 1 + 7])  # the delta is for many
        self.assert_made_again_equals_a_rebuild()

    def test_a_save_that_changes_nothing_moves_nothing(self):
        before = self.rows()
        self.put(record("rec:org:1"))
        self.assertEqual(self.rows(), before)

    def test_the_hub_itself_gets_its_own_columns(self):
        self.put(
            record(
                "rec:org:1", res="ark:/1/moved/1", stamp="2027-06-01T00:00:00.000000Z"
            )
        )
        row = self.store.con.execute(
            "SELECT res_uri, sort_key FROM etags WHERE record_uri = 'rec:org:1'"
        ).fetchone()
        self.assertEqual(
            row, ("ark:/1/moved/1", 1767225600000000)
        )  # created: an edit does not move it


class DeltaOracleTest(OracleTest):
    """The random writes again, with the delta used whenever a record is made of
    another."""

    def setUp(self):
        super().setUp()
        old = store_module.ETAGS_DELTA_ABOVE
        store_module.ETAGS_DELTA_ABOVE = 0  # ty: ignore[invalid-assignment]
        self.addCleanup(setattr, store_module, "ETAGS_DELTA_ABOVE", old)


class ResourceUriTest(EtagCase):
    """A resource uri names one record: the links between resources go through it."""

    def test_a_second_record_with_the_same_resource_uri_is_refused(self):
        self.put(record("rec:person:1", res="ark:/1/shared"))
        with self.assertRaisesRegex(
            Exception, "the resource uri ark:/1/shared is of two records"
        ):
            self.put(record("rec:work:1", res="ark:/1/shared"))
        self.assertEqual(
            self.store.count_etags(), 1
        )  # nothing of the refused write is there
        self.put(
            record("rec:person:1", res="ark:/1/shared")
        )  # the same record again is the same record
