import random
from datetime import UTC, datetime, timedelta

import pyoxigraph as ox
from base import DbTestCase
from harness import open_store

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
        self.store = open_store(self.db)
        self.addCleanup(self.store.close)
        self.store.set_type_ids(TYPES)

    def put(self, *records: Graph) -> None:
        self.store.put_multi(Graph(ox.Dataset(q for graph in records for q in graph)))

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

    def test_a_record_has_a_row_with_the_ids_of_what_it_is_made_of_and_the_python_id_is_the_one_of_the_database(
        self,
    ):
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
                self.store.con.execute(
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

    def test_the_etag_changes_with_what_the_page_is_made_of_and_with_nothing_else(self):
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

        before = self.rows()
        self.put(record("rec:work:1", refs=["rec:person:1"]))
        self.assertEqual(self.rows(), before)

        first = self.etag("rec:work:1")
        self.put(record("rec:work:1", refs=["rec:person:2"]))
        self.assertNotEqual(self.etag("rec:work:1"), first)
        self.assertEqual(
            self.store.page_uris("rec:work:1"), ["rec:person:2", "rec:work:1"]
        )

    def test_a_reference_to_a_record_that_is_not_there_yet_or_by_resource_uri_is_found_when_it_is_made(
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

    def test_a_deleted_record_has_no_row_a_cycle_ends_and_the_page_has_the_time_of_the_last_record(
        self,
    ):
        first = self.etag("rec:work:1")
        self.store.delete(["rec:person:1"])
        self.assertIsNone(self.etag("rec:person:1"))
        self.assertNotEqual(self.etag("rec:work:1"), first)
        self.assertEqual(self.store.page_uris("rec:work:1"), ["rec:work:1"])
        self.assert_made_again_equals_a_rebuild()

        self.put(
            record("rec:person:1", refs=["rec:org:1", "rec:work:1"])
        )  # work 1 -> person 1 -> work 1
        self.assertEqual(
            self.store.page_uris("rec:work:1"),
            ["rec:org:1", "rec:person:1", "rec:work:1"],
        )
        self.assert_made_again_equals_a_rebuild()

        self.put(record("rec:org:1", stamp="2027-05-05T00:00:00.000000Z"))
        found = fetch_one(
            self.store.con.execute(
                "SELECT last_modified_at FROM etags WHERE record_uri = 'rec:work:1'"
            )
        )[0]
        self.assertEqual((found.year, found.month), (2027, 5))


class TimeOnlyGoesUpTest(EtagCase):
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

    def test_a_page_that_loses_the_latest_record_by_its_deletion_gets_the_time_of_the_write(
        self,
    ):
        before = self.last("rec:work:1")
        untouched = (self.etag("rec:work:2"), self.last("rec:work:2"))
        self.store.delete(["rec:org:1"])
        self.assertEqual((self.etag("rec:work:2"), self.last("rec:work:2")), untouched)
        self.assertEqual(
            self.store.page_uris("rec:work:1"), ["rec:person:1", "rec:work:1"]
        )
        self.assertGreater(self.last("rec:work:1"), before)
        self.assert_made_again_equals_a_rebuild()

        self.store.delete(["rec:org:1"])
        kept = self.last("rec:work:1")
        self.store.rebuild_etags()
        self.assertEqual(self.last("rec:work:1"), kept)

    def test_a_page_that_loses_the_latest_record_by_its_resource_uri_gets_the_time_of_the_write(
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

    def test_a_page_that_stops_pointing_at_the_latest_record_gets_the_time_of_the_write_and_one_with_the_same_etag_keeps_its_time(
        self,
    ):
        before = self.last("rec:work:1")
        self.put(record("rec:person:1"))  # no longer points at org 1
        self.assertGreater(self.last("rec:work:1"), before)
        self.assert_made_again_equals_a_rebuild()

        before = {u: self.last(u) for u in ("rec:work:1", "rec:work:2", "rec:person:1")}
        self.put(record("rec:work:2"))  # nothing changed
        self.assertEqual({u: self.last(u) for u in before}, before)


class WhenTheyAreMadeTest(EtagCase):
    def test_a_record_of_a_type_without_a_number_cannot_be_stored_until_the_type_has_one(
        self,
    ):
        fresh = open_store(self.tmp / "other.db")
        self.addCleanup(fresh.close)
        with self.assertRaisesRegex(Exception, "unknown record uri: rec:nobody:1"):
            fresh.put_multi(record("rec:nobody:1"))
        self.assertEqual(
            fetch_one(fresh.con.execute("SELECT count(*) FROM records"))[0], 0
        )
        fresh.set_type_ids([("nobody", 41)])
        fresh.put_multi(record("rec:nobody:1"))
        self.assertIsNotNone(fresh.etag_info("rec:nobody:1"))
        with self.assertRaisesRegex(
            Exception, "Conversion Error"
        ):  # nor a number that is too high
            fresh.put_multi(record("rec:nobody:10000000"))

    def test_a_rebuild_says_which_etags_were_wrong_an_outside_write_is_made_up_for_and_a_failed_write_changes_nothing(
        self,
    ):
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

        self.put(record("rec:org:1"))
        before = self.rows()
        with self.assertRaises(ValueError):
            self.store.put_multi(
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

    def test_a_type_keeps_its_number_a_new_type_changes_no_id_and_a_reopened_store_knows_them(
        self,
    ):
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

        self.store.set_type_ids([("magazine", 40)])
        again = open_store(self.db)
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

    def test_many_records_at_once_are_made_in_one_refresh_and_equal_a_rebuild(self):
        self.put(record("rec:org:1"))
        batch = Graph(
            ox.Dataset(
                q
                for n in range(1, 41)
                for q in record(f"rec:work:{n}", refs=["rec:org:1"])
            )
        )
        self.store.put_multi(batch)
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
        self.store.put_multi(
            again
        )  # all of them again, and now pointing at a person too
        self.assertEqual(
            self.store.count_referencing_records(
                self.store.record_id_of("rec:person:1")
            ),
            40,
        )
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
    return f"2026-03-01T{n // 60:02d}:{n % 60:02d}:00.000000Z"


def microseconds(text: str) -> int:
    when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return (when - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)


class ListingTest(EtagCase):
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

    def test_random_writes_edits_and_deletes_give_the_listing_a_model_gives(self):
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
                person = self.store.record_id_of("rec:person:1")
                self.assertEqual(
                    self.store.count_referencing_records(
                        person, include_non_public=not only_published
                    ),
                    len(newest_first),
                    f"step {step}",
                )
                pages = max(1, -(-len(newest_first) // 20))
                for page in range(1, pages + 1):
                    got = [
                        i % uri.TYPE_BASE
                        for i in self.store.referencing_records(
                            person,
                            offset=(page - 1) * 20,
                            limit=20,
                            include_non_public=not only_published,
                        )
                    ]
                    self.assertEqual(
                        got,
                        newest_first[(page - 1) * 20 : page * 20],
                        f"step {step}, page {page}, published={only_published}",
                    )


class OracleTest(EtagCase):
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
                self.store.put_multi(
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
        with open_store(self.db) as store:
            self.assertEqual(
                fetch_one(store.con.execute("SELECT count(*) FROM etags"))[0], made
            )
            self.assertIsNotNone(store.etag_info("rec:repository:1"))
            store.con.execute("DELETE FROM etags")
        code, out, _ = self.gracia("etags", "rebuild")
        self.assertEqual(int(out.split()[1]), made)  # the same again, from nothing
        self.assertIn(f"{made} of them different", out)  # every one was missing


class IndexTest(DbTestCase):
    def indexes(self, store) -> list[str]:
        rows = store.con.execute(
            "SELECT index_name FROM duckdb_indexes() WHERE schema_name = ? ORDER BY 1",
            [store.repo],
        ).fetchall()
        return [r[0] for r in rows]

    def test_a_new_repository_has_no_index_that_is_not_a_constraint(self):
        store = open_store(self.db)
        self.addCleanup(store.close)
        store.set_type_ids(TYPES)
        store.put_multi(record("rec:work:1"))
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
    def setUp(self):
        super().setUp()
        old = store_module.ETAGS_DELTA_ABOVE
        store_module.ETAGS_DELTA_ABOVE = 3  # ty: ignore[invalid-assignment]
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

    def test_a_rename_of_a_hub_does_not_walk_the_records_made_of_it_nor_an_earlier_time_set_them_back(
        self,
    ):
        before = self.rows()
        self.put(record("rec:org:1"))
        self.assertEqual(self.rows(), before)
        self.walked.clear()

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

    def test_a_hub_that_points_elsewhere_or_is_deleted_is_walked_again_with_the_records_made_of_it(
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

        self.walked.clear()
        self.store.delete(["rec:org:1"])
        self.assertEqual(self.walked, [1 + 1 + 7])
        self.assertEqual(
            self.store.page_uris("rec:work:1"), ["rec:person:1", "rec:work:1"]
        )
        self.assert_made_again_equals_a_rebuild()

    def test_two_records_changed_at_once_move_the_etags_by_both_changes_and_a_record_made_of_it_by_two_ways_changes_once(
        self,
    ):
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
        self.store.put_multi(both)
        self.assertEqual(self.walked, [2])  # the two seeds
        self.assertTrue(all(before[n] != self.etag(f"rec:work:{n}") for n in before))
        self.assert_made_again_equals_a_rebuild()

        before = self.etag("rec:work:1")
        self.put(record("rec:org:1", stamp="2027-01-01T00:00:00.000000Z"))
        self.assertNotEqual(self.etag("rec:work:1"), before)
        self.assert_made_again_equals_a_rebuild()

    def test_a_few_records_made_of_it_are_just_walked_again(self):
        store_module.ETAGS_DELTA_ABOVE = 200
        self.put(record("rec:org:1", stamp="2027-06-01T00:00:00.000000Z"))
        self.assertEqual(self.walked, [1 + 1 + 7])  # the delta is for many
        self.assert_made_again_equals_a_rebuild()


class DeltaOracleTest(OracleTest):
    def setUp(self):
        super().setUp()
        old = store_module.ETAGS_DELTA_ABOVE
        store_module.ETAGS_DELTA_ABOVE = 0  # ty: ignore[invalid-assignment]
        self.addCleanup(setattr, store_module, "ETAGS_DELTA_ABOVE", old)


class ResourceUriTest(EtagCase):
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
