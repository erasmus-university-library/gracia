import re
from typing import ClassVar
from unittest import mock

import pyoxigraph as ox
from test_edit import FOAF, EditCase

from gracia.graph import Graph
from gracia.shacl import Shapes
from gracia.types.type import Type

HTML = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"}
PREFIX = (
    "@prefix gracia: <https://gracia.pub/ns/> . @prefix xsd: "
    "<http://www.w3.org/2001/XMLSchema#> . "
    f"@prefix foaf: <{FOAF}> .\n"
)


def person(
    number: int, name: str | None = None, published: bool = True, extra: str = ""
) -> str:
    name = name or f"Person {number}"
    when = f"2026-03-01T{number // 60:02d}:{number % 60:02d}:00.000000Z"
    return (
        f'<rec:person:{number}> a gracia:Record ; gracia:label "{name}" ; '
        f"gracia:resource_uri <ark:/99999/ir/person/{number}> ; gracia:created_at "
        f'"{when}"^^xsd:dateTime ; '
        f'gracia:modified_at "{when}"^^xsd:dateTime ; gracia:is_published '
        f'"{str(published).lower()}"^^xsd:boolean ; '
        f'gracia:resource [ a foaf:Person ; foaf:name "{name}"{extra} ] .\n'
    )


class PagesCase(EditCase):
    def add(self, text: str) -> None:
        with self.application.new_store("test_repo") as store:
            store.put_multi(
                Graph(
                    ox.Dataset(
                        ox.parse((PREFIX + text).encode(), format=ox.RdfFormat.TURTLE)
                    )
                )
            )

    def get(self, path: str, headers=None, **kw):
        return self.client().get(
            path, headers={**HTML, **(headers or {})}, expect_errors=True, **kw
        )


class TypeIndexTest(PagesCase):
    def setUp(self):
        super().setUp()
        self.add(
            "".join(person(n) for n in range(10, 56))
        )  # 46 records: 2 pages, 20 and 26 (+ Ada: 47)

    def labels(self, response) -> list[str]:
        return re.findall(
            r'<li class="list-group-item[^>]*>\s*<a href="[^"]+">([^<]*)</a>',
            response.text,
        )

    def test_it_is_page_one_with_the_newest_records_the_last_page_the_oldest_and_a_page_that_is_not_there_is_not_found(
        self,
    ):
        got = self.get("/test_repo/person/", self.admin)
        self.assertEqual((got.status_int, got.content_type), (200, "text/html"))
        labels = self.labels(got)
        self.assertEqual(len(labels), 20)  # 47 records: 3 pages of 20, 20 and 7
        # the newest created is first: Ada, the record the fixture made when the test
        # began, which
        # was
        # stored without the label hook, so it has no label; then the newest of the
        # others
        self.assertEqual(labels[:2], ["(Unnamed)", "Person 55"])
        self.assertIn("Page 1 of 3", got.text)
        self.assertIn("47 published, 0 not published", got.text)
        got = self.get("/test_repo/person/?page=3", self.admin)
        labels = self.labels(got)
        self.assertEqual(
            (len(labels), labels[0], labels[-1]), (7, "Person 16", "Person 10")
        )  # the oldest 7, newest first
        self.assertIn("Page 3 of 3", got.text)
        self.assertRegex(
            got.text,
            r'<a class="page-link" '
            r'href="http://localhost/test_repo/person/\?page=2">Newer</a>',
        )
        self.assertRegex(
            got.text, r'<span class="page-link">Older</span>'
        )  # nothing older: not a link
        for query in ("?page=0", "?page=4", "?page=-1", "?page=abc", "?page="):
            with self.subTest(query=query):
                self.assertEqual(
                    self.get("/test_repo/person/" + query, self.admin).status_int, 404
                )
        # the records of a type are json when asked and html by default
        got = self.client().get(
            "/test_repo/person/", headers={**self.admin, "Accept": "application/json"}
        )
        self.assertEqual(got.content_type, "application/json")
        self.assertEqual(len(got.json["records"]), 47)
        got = self.client().get("/test_repo/person/", headers=self.admin)
        self.assertEqual(got.content_type, "text/html")

    def test_the_links_are_the_page_numbers_near_the_page_with_gaps(self):
        self.add("".join(person(n) for n in range(60, 200)))  # 187 records: 10 pages
        got = self.get("/test_repo/person/?page=5", self.admin)
        numbers = re.findall(r'class="page-link"[^>]*>(\d+|…)</', got.text)
        self.assertEqual(numbers, ["1", "…", "3", "4", "5", "6", "7", "…", "10"])
        self.assertRegex(
            got.text,
            r'page-item active">\s*<a class="page-link" href="[^"]*\?page=5">5<',
        )

    def test_a_record_that_is_not_published_is_for_those_who_may_edit_and_labels_are_text_and_add_is_for_those_who_may_add(
        self,
    ):
        self.add(person(100, "Hidden Hannah", published=False))
        admin = self.get("/test_repo/person/", self.admin)
        self.assertIn("Hidden Hannah", admin.text)
        self.assertIn("1 not published", admin.text)
        public = self.get("/test_repo/person/")
        self.assertEqual(public.status_int, 200)
        self.assertNotIn("Hidden Hannah", public.text)
        self.assertIn("47 records", public.text)
        self.add(person(101, "<b>bold</b> & co"))
        self.add(person(102, "").replace('gracia:label "" ;', ""))
        text = self.get("/test_repo/person/", self.admin).text
        self.assertIn("&lt;b&gt;bold&lt;/b&gt; &amp; co", text)
        self.assertNotIn("<b>bold</b>", text)
        self.assertIn("(Unnamed)", text)
        self.assertIn("/test_repo/person/add", text)
        self.assertNotIn("/test_repo/person/add", public.text)

    def test_the_page_comes_from_etags_when_there_are_some_and_from_records_when_not_the_same_either_way(
        self,
    ):
        before = self.labels(
            self.get("/test_repo/person/?page=2", self.admin)
        )  # no etags in this repository yet
        with self.application.new_store("test_repo") as store:
            repository = self.application["test_repo"]
            self.addCleanup(repository.close)
            store.set_type_ids([(t.slug, t.number) for t in repository.types.values()])
            self.assertGreater(store.count_etags(), 40)
        after = self.labels(self.get("/test_repo/person/?page=2", self.admin))
        self.assertEqual(before, after)


class RecordPageTest(PagesCase):
    ROLES: ClassVar[list] = [
        ("label", FOAF + "name", 0),
        ("key_info", FOAF + "mbox", 1),
        ("key_info", FOAF + "phone", 2),
        ("key_info", FOAF + "knows", 3),
        ("description", FOAF + "title", 4),
    ]

    def setUp(self):
        super().setUp()
        self.add(person(1, "Ada"))  # with a resource uri: what Bob points at
        self.add(
            person(
                2,
                "Bob",
                extra=(
                    " ; foaf:mbox <mailto:bob@example.org>"
                    " ; foaf:phone <javascript:alert(1)>"
                    " ; foaf:knows <ark:/99999/ir/person/1>"
                    ' ; foaf:title "Dr Bob, <i>physician</i>"'
                ),
            )
        )

    def page(self, anonymous=False, roles=ROLES):
        with mock.patch.object(Type, "property_roles", roles):
            return self.get("/test_repo/person/2", None if anonymous else self.admin)

    def test_it_shows_the_label_and_the_type(self):
        got = self.page(roles=[("label", FOAF + "name", 0)])
        self.assertEqual((got.status_int, got.content_type), (200, "text/html"))
        self.assertRegex(got.text, r'<h1 class="h3 mb-0">Bob</h1>')
        self.assertRegex(
            got.text, r'<a href="http://localhost/test_repo/person/">Person</a>'
        )
        self.assertNotIn(
            "<table", got.text
        )  # a type with no key information has no table
        self.assertNotIn('class="lead"', got.text)

    def test_the_key_information_is_a_table_under_the_names_of_the_shape_with_links_a_text_description_and_no_row_for_what_is_missing(
        self,
    ):
        text = self.page().text
        rows = re.findall(r'<th scope="row" class="w-25">([^<]*)</th>', text)
        self.assertEqual(
            rows, ["Mail", "phone", "knows"]
        )  # "Mail" is the sh:name; the others have none: their uri's last part
        self.assertRegex(
            text, r'<a href="mailto:bob@example.org">mailto:bob@example.org</a>'
        )
        # a record is a link and an address that is not a web one is only text
        self.assertRegex(
            text,
            r'<a href="http://localhost/test_repo/person/1">ark:/99999/ir/person/1</a>',
        )
        self.assertIn("javascript:alert(1)", text)
        self.assertNotIn('href="javascript:', text)
        # the description is the property with that role, and is text
        self.assertRegex(
            text, r'<p class="lead">Dr Bob, &lt;i&gt;physician&lt;/i&gt;</p>'
        )
        self.assertNotIn("<i>physician</i>", text)
        roles = [*self.ROLES, ("key_info", FOAF + "homepage", 5)]
        self.assertEqual(
            len(re.findall(r'<th scope="row"', self.page(roles=roles).text)), 3
        )

    def test_edit_is_a_link_for_those_who_may_edit_and_the_credentials_of_a_user_are_not_a_row(
        self,
    ):
        self.assertIn("/test_repo/person/2/edit", self.page().text)
        self.assertNotIn("/test_repo/person/2/edit", self.page(anonymous=True).text)
        got = self.client().get("/test_repo/user/1", headers={**HTML, **self.admin})
        self.assertEqual(got.status_int, 200)
        self.assertIsNone(re.search(r"scrypt|\$argon|pbkdf", got.text))
        self.assertIn(
            "User id", got.text
        )  # its key information: the user id, under its name in the shape

    def test_no_accept_is_html_and_the_other_types_are_asked_for(self):
        for accept in (None, "*/*", "text/html"):
            headers = dict(self.admin)
            if accept:
                headers["Accept"] = accept
            got = self.client().get("/test_repo/person/1", headers=headers)
            self.assertEqual(got.content_type, "text/html", accept)
        for accept in ("application/json", "application/rdf+xml"):
            got = self.client().get(
                "/test_repo/person/1", headers={**self.admin, "Accept": accept}
            )
            self.assertEqual(got.content_type, accept)


class NamesTest(PagesCase):
    def test_what_a_shape_calls_a_property_and_the_last_part_of_the_uri_when_it_has_no_name(
        self,
    ):
        shapes = Shapes.parse(
            """@prefix sh: <http://www.w3.org/ns/shacl#> . @prefix ex: <http://ex/> .
               ex:Shape a sh:NodeShape ; sh:property [ sh:path ex:a ; sh:name "Alpha" ]
                 ,
                   [ sh:path ex:b ] , [ sh:path ex:c ; sh:name "Inner" ; sh:node
                     ex:Nested ] .
               ex:Nested a sh:NodeShape ; sh:property [ sh:path ex:d ; sh:name "Delta" ]
                 .""",
            "turtle",
        )
        self.assertEqual(
            shapes.property_names(ox.NamedNode("http://ex/Shape")),
            {
                "http://ex/a": "Alpha",
                "http://ex/b": "b",
                "http://ex/c": "Inner",
                "http://ex/d": "Delta",
            },
        )
