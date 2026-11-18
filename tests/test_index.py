import re
import unittest
from datetime import datetime
from pathlib import Path

import pyoxigraph as ox
from support import must_search
from test_edit import EditCase

from gracia.views.index import etag_of

PEOPLE = """
@prefix gracia: <https://gracia.pub/ns/> . @prefix foaf: <http://xmlns.com/foaf/0.1/> .
<rec:person:2> a gracia:Record ; gracia:resource_uri <ark:/99999/ir/person/2> ;
    gracia:resource [ a foaf:Person ; foaf:name "Bob" ; foaf:knows
      <ark:/99999/ir/person/3> ] .
<rec:person:3> a gracia:Record ; gracia:resource_uri <ark:/99999/ir/person/3> ;
    gracia:resource [ a foaf:Person ; foaf:name "Cy" ; foaf:knows
      <ark:/99999/ir/person/1> ] .
<rec:person:4> a gracia:Record ; gracia:resource_uri <ark:/99999/ir/person/4> ;
    gracia:resource [ a foaf:Person ; foaf:name "Dan" ] .
"""
JSON = {"Accept": "application/json"}


class EtagTest(unittest.TestCase):
    NOW = datetime(2026, 11, 20, 12, 0, 0)

    def test_it_follows_the_records_and_when_they_changed_and_nothing_else(self):
        versions = {"rec:a:1": self.NOW, "rec:a:2": datetime(2026, 1, 1)}
        same = {
            "rec:a:2": datetime(2026, 1, 1),
            "rec:a:1": self.NOW,
        }  # in another order
        self.assertEqual(etag_of(versions), etag_of(same))
        self.assertRegex(etag_of(versions), r"^[0-9a-f]{16}$")
        later = {**versions, "rec:a:2": datetime(2026, 1, 2)}
        self.assertNotEqual(etag_of(versions), etag_of(later))
        more = {**versions, "rec:a:3": self.NOW}
        self.assertNotEqual(etag_of(versions), etag_of(more))
        shaped = etag_of(versions, {"person": "v1", "type": "v7"})
        self.assertNotEqual(etag_of(versions), shaped)  # the shapes of the types count
        self.assertEqual(shaped, etag_of(versions, {"type": "v7", "person": "v1"}))
        self.assertNotEqual(shaped, etag_of(versions, {"person": "v2", "type": "v7"}))


class IndexCase(EditCase):
    def setUp(self):
        super().setUp()
        with self.application.open_repository("library") as store:
            store.put(ox.Dataset(ox.parse(PEOPLE.encode(), format=ox.RdfFormat.TURTLE)))

    def store(self):
        return self.application.open_repository("library")

    def rename(self, number: int, name: str) -> None:
        """Save one of the people again with another name (the others are not
        touched)."""
        header, *records = re.split(r"(?=<rec:person:\d+> a gracia:Record)", PEOPLE)
        text = header + next(
            r for r in records if r.startswith(f"<rec:person:{number}>")
        )
        text = re.sub(r'foaf:name "[^"]*"', f'foaf:name "{name}"', text)
        with self.store() as store:
            store.put(ox.Dataset(ox.parse(text.encode(), format=ox.RdfFormat.TURTLE)))


class DependenciesTest(IndexCase):
    def test_the_records_a_page_is_made_of_through_resource_uris_and_record_uris_and_a_cycle(  # noqa: E501
        self,
    ):
        with self.store() as store:
            found = store.dependencies("rec:person:1")
        # person 1 knows rec:person:2 (a record uri), which knows the ark of 3 (a
        # resource uri),
        # which knows rec:person:1 again; the type record is one of them too
        # (gracia:type)
        self.assertTrue(
            {"rec:person:1", "rec:person:2", "rec:person:3", "rec:type:5"} <= set(found)
        )
        self.assertNotIn("rec:person:4", found)
        self.assertTrue(all(isinstance(when, datetime) for when in found.values()))

    def test_it_stops_at_the_depth_it_is_given(self):
        with self.store() as store:
            self.assertEqual(
                {"rec:person:1", "rec:person:2"}
                - set(store.dependencies("rec:person:1", 1)),
                set(),
            )
            self.assertNotIn("rec:person:3", store.dependencies("rec:person:1", 1))
            self.assertIn("rec:person:3", store.dependencies("rec:person:1", 2))

    def test_a_deleted_record_is_not_one_and_a_record_that_is_not_there_has_none(self):
        with self.store() as store:
            store.delete(["rec:person:3"])
            self.assertNotIn("rec:person:3", store.dependencies("rec:person:1"))
            self.assertEqual(store.dependencies("rec:person:99"), {})


class IndexViewTest(IndexCase):
    def index(self, path="/library/person/1", **headers):
        return self.client().get(
            path, headers={**self.admin, **headers}, expect_errors=True
        )

    def test_it_is_rdfxml_of_the_record_and_the_records_it_is_made_of(self):
        got = self.index()
        self.assertEqual(got.status_int, 200)
        self.assertEqual(got.content_type, "application/rdf+xml")
        names = {
            q.object.value
            for q in ox.parse(got.body, format=ox.RdfFormat.RDF_XML)
            if q.predicate.value == "http://xmlns.com/foaf/0.1/name"
        }
        self.assertEqual(names, {"Ada", "Bob", "Cy"})  # not Dan: nothing leads to him

    def test_it_is_nested_and_the_record_the_page_is_about_comes_first(self):
        for number, name in ((2, "Bob"), (3, "Cy"), (1, "Ada")):
            got = self.index(f"/library/person/{number}")
            self.assertEqual(got.status_int, 200)
            first = must_search(r'<[\w:]+ rdf:about="([^"]+)"', got.text)[1]
            self.assertEqual(
                first, f"rec:person:{number}"
            )  # whichever of the three it is
            self.assertRegex(
                got.text, rf"\n +<foaf:name>{name}</foaf:name>"
            )  # indented, nested
        self.assertNotIn(
            "rdf:nodeID", self.index("/library/person/2").text
        )  # inline, not by id

    def test_the_shapes_of_the_types_on_the_page_are_in_it_and_no_others(self):
        text = self.index("/library/person/1").text
        names = {
            q.object.value
            for q in ox.parse(text.encode(), format=ox.RdfFormat.RDF_XML)
            if q.predicate.value == "http://www.w3.org/ns/shacl#name"
        }
        self.assertIn("Name", names)  # the shape of the person, from its schema file
        self.assertIn("Mail", names)
        self.assertIn("Resource URI", names)  # the shape every record has
        self.assertIn("urn:test:PersonShape", text)
        self.assertIn(
            "https://gracia.pub/ns/TypeShape", text
        )  # the type record of the person is on the page
        self.assertNotIn(
            "https://gracia.pub/ns/UserShape", text
        )  # nothing on the page is a user
        self.assertNotIn("https://gracia.pub/ns/UserGroupShape", text)
        self.assertIn('xmlns:sh="http://www.w3.org/ns/shacl#"', text)
        users = self.index("/library/user/1").text
        self.assertIn(
            "https://gracia.pub/ns/UserShape", users
        )  # and a user's page has its own

    def test_the_etag_follows_the_schema_files_of_the_types_on_the_page(self):
        first = self.index("/library/person/2").headers["ETag"]
        schema = Path(self.person_assets) / "shacl" / "Person-full-01.ttl"
        schema.write_text(schema.read_text() + "\n# a change\n")
        self.assertNotEqual(self.index("/library/person/2").headers["ETag"], first)
        self.assertEqual(self.index("/library/person/2").status_int, 200)

    def test_the_etag_changes_with_what_the_page_is_made_of_and_with_nothing_else(self):
        first = self.index().headers["ETag"]
        self.assertRegex(first, r'^"[0-9a-f]{16}"$')
        self.assertEqual(self.index().headers["ETag"], first)  # nothing changed
        self.rename(4, "Dan the Man")  # a record the page is not made of
        self.assertEqual(self.index().headers["ETag"], first)
        self.rename(3, "Cyrus")  # two steps away from it
        self.assertNotEqual(self.index().headers["ETag"], first)

    def test_what_the_browser_has_is_answered_with_not_modified_and_nothing_built(self):
        etag = self.index().headers["ETag"]
        got = self.index(**{"If-None-Match": etag})
        self.assertEqual(got.status_int, 304)
        self.assertEqual(got.body, b"")
        self.assertEqual(got.headers["ETag"], etag)
        self.rename(3, "Cyrus")
        self.assertEqual(
            self.index(**{"If-None-Match": etag}).status_int, 200
        )  # it changed

    def test_json_is_still_there_for_whoever_asks_for_it(self):
        got = self.index(**JSON)
        self.assertEqual(got.content_type, "application/json")
        self.assertIn("resource_uri", got.json)

    def test_the_credentials_of_a_user_are_not_in_it(self):
        got = self.index("/library/user/1")
        self.assertEqual(got.status_int, 200)
        self.assertNotIn(
            "<gracia:credentials", got.text
        )  # (the shape of a user names the property)
        self.assertIsNone(re.search(r"scrypt|\$argon|pbkdf", got.text))

    def test_a_record_that_is_not_there_is_not_found(self):
        self.assertEqual(self.index("/library/person/99").status_int, 404)
