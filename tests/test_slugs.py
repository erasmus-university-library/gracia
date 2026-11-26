import re
import unittest

from harness import FOAF_PERSON, open_store
from support import must_search
from test_web import HeaderPolicy, WebCase

from gracia import uri
from gracia.main import make_config
from gracia.predicates import HookContext
from gracia.predicates.gracia_slug import SlugPredicate
from gracia.store import fetch_one

ADMIN = {"X-User": "admin"}
POSTING = {**ADMIN, "Origin": "http://localhost:80"}
SLUG, LABEL = uri.SLUG, uri.LABEL


class SlugSqlTest(unittest.TestCase):
    class Type:
        slug = "person"

    def setUp(self):
        self.store = open_store(":memory:")
        self.addCleanup(self.store.close)
        self.con = self.store.con
        self.con.execute(
            "CREATE TABLE flat (s VARCHAR, loc VARCHAR[], p VARCHAR, o VARCHAR, lit "
            "VARCHAR, "
            "lang VARCHAR, dt VARCHAR)"
        )

    def taken(self, *slugs: tuple[str, str]) -> None:
        for slug, record in slugs:
            self.con.execute(
                "INSERT INTO slugs (rec_type, slug, record_uri, is_deleted) VALUES ('person', ?, ?, false)",
                [slug, record],
            )

    def slugged(
        self, records: dict[str, tuple[str | None, str | None]]
    ) -> dict[str, str | None]:
        for record, (label, stated) in records.items():
            for p, value in ((LABEL, label), (SLUG, stated)):
                if value is not None:
                    self.con.execute(
                        "INSERT INTO flat VALUES (?, [], ?, NULL, ?, NULL, NULL)",
                        [record, p, value],
                    )
        context = HookContext(None, self.Type(), SLUG, flat="flat")
        for statement in SlugPredicate().on_write_sql(context):
            self.con.execute(statement)
        found = dict(
            self.con.execute(
                "SELECT s, lit FROM flat WHERE p = ? AND len(loc) = 0", [SLUG]
            ).fetchall()
        )
        return {record: found.get(record) for record in records}

    def test_a_slug_is_made_of_the_label(self):
        made = self.slugged(
            {
                "rec:person:1": ("John Doe", None),
                "rec:person:2": ("Café Müller", None),
                "rec:person:3": ("Ünïcödé 日本語", None),
                "rec:person:4": ("  --Hello,   World!!  ", None),
                "rec:person:5": ("Dr. Jane O'Neill-Smith (2nd)", None),
            }
        )
        self.assertEqual(
            made,
            {
                "rec:person:1": "john-doe",
                "rec:person:2": "cafe-muller",
                "rec:person:3": "unicode",
                "rec:person:4": "hello-world",
                "rec:person:5": "dr-jane-o-neill-smith-2nd",
            },
        )

    def test_what_makes_no_slug_leaves_the_record_at_its_number(self):
        made = self.slugged(
            {
                "rec:person:1": ("日本語", None),  # nothing of it is left
                "rec:person:2": (
                    "2024",
                    None,
                ),  # numbers only: that would be an address of a number
                "rec:person:3": (None, None),  # no label
                "rec:person:4": ("!!!", None),
            }
        )
        self.assertEqual(made, dict.fromkeys(made))

    def test_a_long_label_is_cut_and_does_not_end_in_a_dash(self):
        made = self.slugged({"rec:person:1": ("word " * 40, None)})["rec:person:1"]
        self.assertLessEqual(len(made), 80)  # ty: ignore[invalid-argument-type]
        self.assertRegex(made, r"^word(-word)*$")  # ty: ignore[invalid-argument-type]

    def test_a_slug_that_is_given_is_cleaned_and_used_instead_of_the_label(self):
        made = self.slugged(
            {
                "rec:person:1": ("John Doe", "Johnny B. Goode"),
                "rec:person:2": ("Jane Roe", "   "),  # empty: the label
            }
        )
        self.assertEqual(
            made, {"rec:person:1": "johnny-b-goode", "rec:person:2": "jane-roe"}
        )

    def test_a_slug_another_record_has_gets_the_next_number_that_is_free(self):
        self.taken(("john-doe", "rec:person:9"))
        self.assertEqual(
            self.slugged({"rec:person:1": ("John Doe", None)}),
            {"rec:person:1": "john-doe-2"},
        )

    def test_it_goes_on_past_the_numbers_that_are_taken_and_fills_a_gap(self):
        self.taken(("john-doe", "rec:person:9"), ("john-doe-2", "rec:person:8"))
        self.assertEqual(
            self.slugged({"rec:person:1": ("John Doe", None)}),
            {"rec:person:1": "john-doe-3"},
        )
        self.con.execute("DELETE FROM flat")
        self.con.execute("DELETE FROM slugs WHERE slug = 'john-doe-2'")
        self.taken(
            ("john-doe-3", "rec:person:7")
        )  # john-doe and -3 are taken, -2 is free
        self.assertEqual(
            self.slugged({"rec:person:1": ("John Doe", None)}),
            {"rec:person:1": "john-doe-2"},
        )

    def test_the_slug_of_a_deleted_record_is_taken_too(self):
        self.con.execute(
            "INSERT INTO slugs (rec_type, slug, record_uri, is_deleted) VALUES ('person', 'john-doe', 'rec:person:9', true)"
        )
        self.assertEqual(
            self.slugged({"rec:person:1": ("John Doe", None)}),
            {"rec:person:1": "john-doe-2"},
        )

    def test_a_record_keeps_what_it_has_and_can_take_an_old_one_again(self):
        self.taken(
            ("john-doe", "rec:person:1"),
            ("johnny", "rec:person:1"),
            ("j-doe", "rec:person:2"),
        )
        made = self.slugged(
            {
                "rec:person:1": ("John Doe", "john-doe"),  # its own: not john-doe-2
                "rec:person:3": ("Someone", None),
            }
        )
        self.assertEqual(made["rec:person:1"], "john-doe")
        self.con.execute("DELETE FROM flat")
        self.assertEqual(
            self.slugged({"rec:person:1": ("John Doe", "johnny")}),
            {"rec:person:1": "johnny"},
        )
        self.con.execute("DELETE FROM flat")
        # an old slug of another record is not its to take back: it gets the next one
        self.assertEqual(
            self.slugged({"rec:person:1": ("x", "j-doe")}), {"rec:person:1": "j-doe-2"}
        )

    def test_records_of_one_batch_with_one_label_get_a_number_each_in_the_order_of_their_numbers(  # noqa: E501
        self,
    ):
        made = self.slugged({f"rec:person:{n}": ("John Doe", None) for n in (10, 2, 3)})
        self.assertEqual(
            made,
            {
                "rec:person:2": "john-doe",
                "rec:person:3": "john-doe-2",
                "rec:person:10": "john-doe-3",
            },
        )
        self.con.execute("DELETE FROM flat")
        self.taken(("john-doe", "rec:person:99"))
        made = self.slugged({f"rec:person:{n}": ("John Doe", None) for n in (1, 2)})
        self.assertEqual(
            made, {"rec:person:1": "john-doe-2", "rec:person:2": "john-doe-3"}
        )

    def test_the_names_of_the_views_of_a_type_are_not_slugs(self):
        made = self.slugged(
            {"rec:person:1": ("Add", None), "rec:person:2": ("Manage", None)}
        )
        self.assertEqual(made, {"rec:person:1": "add-2", "rec:person:2": "manage-2"})

    def test_records_of_other_types_are_left_alone(self):
        self.con.execute(
            "INSERT INTO flat VALUES ('rec:book:1', [], ?, NULL, 'A Book', NULL, NULL)",
            [LABEL],
        )
        self.slugged({"rec:person:1": ("John Doe", None)})
        books = self.con.execute(
            "SELECT count(*) FROM flat WHERE s = 'rec:book:1' AND p = ?", [SLUG]
        )
        self.assertEqual(fetch_one(books)[0], 0)

    def test_it_does_not_leave_a_table_behind(self):
        self.slugged({"rec:person:1": ("John Doe", None)})
        tables = self.con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE table_name LIKE 'flat%'"
        )
        self.assertEqual([t[0] for t in tables.fetchall()], ["flat"])


class SlugCase(WebCase):
    def setUp(self):
        super().setUp()
        self.settings["gracia.security_policy"] = HeaderPolicy()  # ty: ignore[invalid-assignment]
        self.config = make_config(self.settings)
        self.config.commit()
        self.config.begin()
        self.addCleanup(self.config.end)
        self.application.create_repository(
            "test_repo",
            admin=("admin", "correct horse battery"),
            content_types=[FOAF_PERSON],
        )
        self.web = self.client(self.config)  # goes on to where a record has moved
        self.raw = self.client(self.config)  # shows the redirect
        self.raw.follow = False  # ty: ignore[missing-attribute]

    def add(self, name: str, **fields) -> int:
        form = self.web.get("/test_repo/person/add", headers=ADMIN).forms[0]
        form["name"] = name
        for key, value in fields.items():
            form[key] = value
        response = form.submit("save", headers=POSTING)
        self.assertEqual(response.status_int, 302, response.text[:300])
        name = must_search(r"/person/([^/]+)/", response.headers["Location"])[1]
        if name.isdigit():
            return int(name)
        with self.application.new_store("test_repo") as store:
            return store.slug_lookup("person", name)[0] % 10_000_000  # ty: ignore[not-subscriptable]

    def slugs(self, record_uri: str):
        with self.application.new_store("test_repo") as store:
            return store.current_slug(record_uri), store.slugs_of(record_uri)

    def get(self, path, **kw):
        return self.raw.get(path, headers=ADMIN, expect_errors=True, **kw)


class SlugRecordTest(SlugCase):
    def test_a_record_gets_a_slug_of_its_label_and_the_next_one_gets_a_number(self):
        one, two, three = (self.add("John Doe") for _ in range(3))
        self.assertEqual(
            [self.slugs(f"rec:person:{n}")[0] for n in (one, two, three)],
            ["john-doe", "john-doe-2", "john-doe-3"],
        )

    def test_the_slug_stays_when_the_label_changes_and_the_address_by_slug_is_the_record(  # noqa: E501
        self,
    ):
        number = self.add("John Doe")
        form = self.web.get(f"/test_repo/person/{number}/edit", headers=ADMIN).forms[0]
        form["name"] = "Johnathan Doe"
        form.submit("save", headers=POSTING)
        self.assertEqual(self.slugs(f"rec:person:{number}"), ("john-doe", ["john-doe"]))
        self.assertEqual(self.get("/test_repo/person/john-doe").status_int, 200)
        moved = self.get(
            f"/test_repo/person/{number}"
        )  # the number goes on to the slug
        self.assertEqual(moved.status_int, 301)
        self.assertEqual(
            moved.headers["Location"], "http://localhost/test_repo/person/john-doe"
        )

    def test_a_new_slug_is_a_new_address_and_the_old_one_goes_on_to_it(self):
        number = self.add("John Doe")
        form = self.web.get(f"/test_repo/person/{number}/edit", headers=ADMIN).forms[0]
        form["slug"] = "johnny"
        form.submit("save", headers=POSTING)
        self.assertEqual(
            self.slugs(f"rec:person:{number}"), ("johnny", ["john-doe", "johnny"])
        )
        old = self.get("/test_repo/person/john-doe")
        self.assertEqual(old.status_int, 301)
        self.assertEqual(
            old.headers["Location"], "http://localhost/test_repo/person/johnny"
        )
        self.assertEqual(self.get("/test_repo/person/johnny").status_int, 200)
        # the rest of the address and the query go with it
        deep = self.get("/test_repo/person/john-doe/edit?source=turtle")
        self.assertEqual(
            deep.headers["Location"],
            "http://localhost/test_repo/person/johnny/edit?source=turtle",
        )
        posted = self.raw.post(
            "/test_repo/person/john-doe/edit", {}, headers=POSTING, expect_errors=True
        )
        self.assertEqual(
            posted.status_int, 308
        )  # what changes something keeps its method

    def test_an_old_slug_can_be_chosen_again(self):
        number = self.add("John Doe")
        for slug in ("johnny", "john-doe"):
            form = self.web.get(
                f"/test_repo/person/{number}/edit", headers=ADMIN
            ).forms[0]
            form["slug"] = slug
            form.submit("save", headers=POSTING)
        self.assertEqual(
            self.slugs(f"rec:person:{number}"), ("john-doe", ["john-doe", "johnny"])
        )
        self.assertEqual(self.get("/test_repo/person/johnny").status_int, 301)
        self.assertEqual(self.get("/test_repo/person/john-doe").status_int, 200)

    def test_the_old_slugs_of_a_record_are_the_suggestions_of_the_slug_field(self):
        number = self.add("John Doe")
        form = self.web.get(f"/test_repo/person/{number}/edit", headers=ADMIN).forms[0]
        form["slug"] = "johnny"
        form.submit("save", headers=POSTING)
        page = self.web.get(f"/test_repo/person/{number}/edit", headers=ADMIN).text
        self.assertRegex(page, r'<input[^>]*name="slug"')
        select = must_search(r'<select[^>]*id="slug-options".*?</select>', page, re.S)[
            0
        ]
        options = re.findall(r'<option value="([^"]*)"', select)
        self.assertEqual(
            options, ["", "john-doe", "johnny"]
        )  # (the first is the prompt)
        self.assertRegex(select, r'<option value="johnny" selected>johnny \(now\)')
        self.assertIn(
            "document.getElementById(", select
        )  # choosing one fills the field
        add = self.web.get("/test_repo/person/add", headers=ADMIN).text
        self.assertNotIn("slug-options", add)  # a new record has no old ones yet

    def test_a_slug_that_is_typed_is_cleaned_and_made_free(self):
        self.add("Jane Roe")
        second = self.add("Someone", slug="Jane  ROE!")
        self.assertEqual(self.slugs(f"rec:person:{second}")[0], "jane-roe-2")


class SlugAddressTest(SlugCase):
    def test_the_address_of_a_deleted_record_is_gone_by_its_slug_and_by_its_number(
        self,
    ):
        number = self.add("John Doe")
        self.assertEqual(self.get("/test_repo/person/john-doe").status_int, 200)
        with self.application.new_store("test_repo") as store:
            store.delete([f"rec:person:{number}"])
        self.assertEqual(self.get("/test_repo/person/john-doe").status_int, 410)
        self.assertEqual(self.get(f"/test_repo/person/{number}").status_int, 410)
        self.assertEqual(
            self.get(f"/test_repo/person/{number + 1}").status_int, 404
        )  # never used
        self.assertEqual(self.get("/test_repo/person/nobody").status_int, 404)

    def test_the_slugs_of_a_record_that_comes_back_work_again_and_a_slug_is_never_given_twice(  # noqa: E501
        self,
    ):
        number = self.add("John Doe")
        with self.application.new_store("test_repo") as store:
            store.delete([f"rec:person:{number}"])
        again = self.add("John Doe")  # the slug of the deleted one is not free
        self.assertEqual(self.slugs(f"rec:person:{again}")[0], "john-doe-2")
        self.assertEqual(self.get("/test_repo/person/john-doe").status_int, 410)
        self.assertEqual(self.get("/test_repo/person/john-doe-2").status_int, 200)

    def test_a_record_without_a_slug_is_at_its_number_and_the_names_of_the_views_are_not_records(  # noqa: E501
        self,
    ):
        self.assertEqual(self.get("/test_repo/person/add").status_int, 200)
        self.assertEqual(self.get("/test_repo/person/manage").status_int, 200)
        self.assertEqual(self.get("/test_repo/person/Not-A-Slug").status_int, 404)
        self.assertEqual(self.get("/test_repo/person/-").status_int, 404)
