"""The reference widget: the lookup of the repository, and the form that shows the label
of what it points at and posts its uri."""

import html
import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pyoxigraph as ox
from harness import FOAF_PERSON
from pyramid.registry import Registry
from support import must_search
from test_list_widget import ListCase
from widgets_support import ADMIN, EX, ORG, POSTING, ROSTER

from gracia import forms
from gracia.change_events import RecordsChanged, process_pending
from gracia.search import Indexes, on_records_changed
from gracia.types.repository import RepositoryView

PERSON = FOAF_PERSON.class_uri
ORGANIZATION = EX + "Org"
ADA, DELFT = "ark:/99999/ir/person/1", "ark:/99999/ir/org/1"
ROSTER_ADD = "/test_repo/roster/add"


class ReferenceCase(ListCase):
    types = (ROSTER, FOAF_PERSON, ORG)

    def setUp(self):
        super().setUp()
        self._index = tempfile.TemporaryDirectory()
        self.addCleanup(self._index.cleanup)
        self.application.indexes = Indexes(Path(self._index.name))
        self.create("person", "Ada Lovelace")
        self.create("person", "Ada Byron")
        self.create("org", "Delft University")
        self.reindex()

    def create(self, kind: str, name: str) -> None:
        form = self.web.get(f"/test_repo/{kind}/add", headers=ADMIN).forms[0]
        form["name"] = name
        self.assertEqual(form.submit("save", headers=POSTING).status_int, 302)

    def reindex(self) -> None:
        registry = Registry()
        registry.registerHandler(on_records_changed, (RecordsChanged,))
        process_pending(self.application, registry)

    def lookup(self, text: str, *classes: str, who=ADMIN):
        params = [("q", text), *(("type", c) for c in classes)]
        return self.web.get(
            "/test_repo/lookup", params=params, headers=who, expect_errors=True
        )

    def found(self, text: str, *classes: str) -> list[str]:
        response = self.lookup(text, *classes)
        self.assertEqual(response.status_int, 200, response.text)
        return [i["label"] for i in response.json["items"]]

    def save_roster(self, members, host="", name="A roster") -> None:
        hosts = []
        if host:
            hosts = [host]
        self.saved(ROSTER_ADD, rows={"members": members, "host": hosts}, name=name)

    def roster_page(self, number=1) -> str:
        return self.page(f"/test_repo/roster/{number}/edit")


def reference_inputs(page: str) -> list[str]:
    """The tags of the text inputs of the references on a page."""
    return re.findall(r"<input[^>]*reference-input[^>]*>", page)


class LookupViewTest(ReferenceCase):
    def test_it_finds_the_records_of_the_classes_by_the_beginning_of_the_label_or_the_number(
        self,
    ):
        self.assertEqual(self.found("ada", PERSON), ["Ada Byron", "Ada Lovelace"])
        self.assertEqual(self.found("ada lov", PERSON), ["Ada Lovelace"])
        self.assertEqual(self.found("del", ORGANIZATION), ["Delft University"])
        # the class is the class of the resource, whatever the type of the record
        self.assertEqual(self.found("ada", ORGANIZATION), [])
        self.assertEqual(self.found("del", PERSON), [])
        self.assertEqual(self.found("del", PERSON, ORGANIZATION), ["Delft University"])
        # without a class it finds every record
        self.assertEqual(self.found("d"), ["Delft University"])
        self.assertEqual(len(self.found("ada")), 2)
        # digits find the record of that number
        found = self.lookup("2", PERSON).json["items"]
        self.assertEqual(
            [(i["label"], i["record"]) for i in found], [("Ada Byron", "rec:person:2")]
        )
        self.assertEqual(self.lookup("2", ORGANIZATION).json["items"], [])
        # nothing typed is every record of the classes (with no class every record is
        # one, those of the system too: the widget always says the classes)
        self.assertEqual(self.found("", PERSON), ["Ada Byron", "Ada Lovelace"])
        self.assertEqual(self.found("", ORGANIZATION), ["Delft University"])
        self.assertTrue(
            {"Ada Byron", "Delft University", "admin"} <= set(self.found(""))
        )

    def test_an_item_has_the_uri_to_point_at_the_label_to_show_and_what_the_resource_is(
        self,
    ):
        response = self.lookup("ada lov", PERSON)
        self.assertEqual(
            response.json["items"],
            [
                {
                    "uri": ADA,
                    "label": "Ada Lovelace",
                    "type": "person",
                    "kind": "Person",
                    "record": "rec:person:1",
                    "published": True,
                    "edit": "http://localhost/test_repo/person/ada-lovelace/edit?embed=1",
                    "icon": "person",
                }
            ],
        )
        (found,) = self.lookup("del", ORGANIZATION).json["items"]
        self.assertEqual(found["kind"], "Organization")  # the name its shape gives it
        self.assertEqual((found["type"], found["record"]), ("org", "rec:org:1"))
        self.assertEqual(response.headers["Cache-Control"], "private")

    def lookup_page(self, **params):
        query = [("q", ""), ("type", PERSON), *params.items()]
        response = self.web.get("/test_repo/lookup", params=query, headers=ADMIN)
        self.assertEqual(response.status_int, 200, response.text)
        return response.json

    def test_a_page_has_the_size_asked_for_and_says_where_it_is(self):
        first = self.lookup_page(size=1)
        self.assertEqual([i["label"] for i in first["items"]], ["Ada Byron"])
        self.assertEqual(
            (first["page"], first["pages"], first["size"], first["total"]), (1, 2, 1, 2)
        )
        second = self.lookup_page(size=1, page=2)
        self.assertEqual([i["label"] for i in second["items"]], ["Ada Lovelace"])
        self.assertEqual((second["page"], second["pages"]), (2, 2))
        self.assertFalse(second["truncated"])
        # a page that is not there is the last and a wrong number the first
        self.assertEqual(self.lookup_page(size=1, page=9)["page"], 2)
        self.assertEqual(self.lookup_page(size=1, page=0)["page"], 1)
        self.assertEqual(self.lookup_page(size=1, page="two")["page"], 1)
        self.assertEqual(self.lookup_page(size="many")["size"], 10)  # the usual
        self.assertEqual(
            self.lookup_page(size=500)["size"], 50
        )  # and no more than that
        everything = self.lookup_page()  # everything on one page is one page
        self.assertEqual((everything["page"], everything["pages"]), (1, 1))
        self.assertEqual(everything["total"], 2)
        # what may not be seen is not counted, so the pages are full
        self.assertEqual(
            self.lookup_page(size=2)["total"], len(self.lookup_page(size=2)["items"])
        )
        with patch("gracia.search.LOOKUP_CAP", 1):  # of a great many only the first
            found = self.lookup_page()
        self.assertTrue(found["truncated"])
        self.assertEqual(found["total"], 1)

    def test_a_repository_with_no_index_or_an_index_that_is_not_there_yet_says_so(self):
        self.application.indexes = None
        response = self.lookup("ada", PERSON)
        self.assertEqual(response.status_int, 503)
        self.assertEqual(response.json["items"], [])
        self.assertIn("no search index", response.json["problem"])
        self.application.indexes = Indexes(Path(self._index.name) / "elsewhere")
        response = self.lookup("ada", PERSON)
        self.assertEqual(response.status_int, 503)
        self.assertIn("missing", response.json["problem"])


class Allowed:
    """What `request.has_permission` and the repository say, for `visible`."""

    def __init__(self, view: set[str], edit: set[str]):
        self.view, self.edit = view, edit

    def has_permission(self, permission, node):
        return node in (self.view if permission == "view" else self.edit)

    def __getitem__(self, slug):
        if slug == "nowhere":
            raise KeyError(slug)
        return slug


class VisibleTest(unittest.TestCase):
    hits: ClassVar[list[dict]] = [
        {"type": "person", "record": "rec:person:1", "published": True},
        {"type": "person", "record": "rec:person:2", "published": False},
        {"type": "secret", "record": "rec:secret:1", "published": True},
        {"type": "nowhere", "record": "rec:nowhere:1", "published": True},
    ]

    def records(self, who):
        who.context = who  # the stand-in is the request and the repository
        return [h["record"] for h in RepositoryView(who).visible(self.hits)]

    def test_a_record_of_a_type_that_may_not_be_viewed_is_not_there_and_an_unpublished_one_is_for_who_may_edit(
        self,
    ):
        self.assertEqual(self.records(Allowed({"person"}, set())), ["rec:person:1"])
        both = Allowed({"person"}, {"person"})
        self.assertEqual(self.records(both), ["rec:person:1", "rec:person:2"])

    def test_the_uri_is_the_resource_uri_or_the_ark_of_the_record(self):
        self.assertEqual(
            RepositoryView.item(
                {"record": "rec:person:7", "resource_uri": "https://x/y"}
            )["uri"],
            "https://x/y",
        )
        self.assertEqual(
            RepositoryView.item({"record": "rec:person:7"})["uri"],
            "ark:/99999/ir/person/7",
        )
        self.assertEqual(
            RepositoryView.item({"record": "rec:person:7"})["label"], "rec:person:7"
        )


class ShapeClassesTest(ReferenceCase):
    def repository(self):
        return self.application["test_repo"]

    def test_the_classes_are_those_of_the_type_that_has_the_shape_and_none_if_there_is_no_type(
        self,
    ):
        shape = ox.NamedNode("https://gracia.pub/ns/PersonMinimalShape")
        self.assertEqual(self.repository().shape_classes([shape]), [PERSON])
        organization = ox.NamedNode(EX + "OrgShape")
        self.assertEqual(
            self.repository().shape_classes([organization]),
            [ORGANIZATION, EX + "University"],
        )
        nothing = ox.NamedNode(EX + "NoSuchShape")
        self.assertEqual(self.repository().shape_classes([nothing]), [])

    def test_a_reference_says_its_shapes_in_the_shape_of_the_roster(self):
        roster = self.repository().types["roster"]
        graph, shape = roster.shacl_shape(include_record_shape=False)
        said = {
            path: sorted(s.value for s in forms.reference_shapes(graph, prop))
            for path, prop in forms.properties(graph, shape)
        }
        self.assertEqual(
            said[EX + "members"], ["https://gracia.pub/ns/PersonMinimalShape"]
        )  # a list
        self.assertEqual(said[EX + "host"], [EX + "OrgShape"])
        self.assertEqual(said.get(EX + "parent", []), [])  # a uri is not a reference


class LabelsTest(ReferenceCase):
    def test_the_labels_of_the_references_are_asked_of_the_store_once(self):
        asked = []

        def ask(uris):
            asked.append(list(uris))
            return {ADA: ("Ada Lovelace", "rec:person:1", "ada-lovelace")}

        labels = forms.ReferenceLabels(ask)
        labels.prefetch([ADA, DELFT, ADA])
        self.assertEqual((labels.get(ADA), labels.get(DELFT)), ("Ada Lovelace", None))
        self.assertEqual(
            labels.record(ADA), ("Ada Lovelace", "rec:person:1", "ada-lovelace")
        )
        self.assertEqual(asked, [sorted([ADA, DELFT])])  # one question, for both
        labels.get("ark:/99999/ir/person/9")  # what the form did not have: asked for
        self.assertEqual(len(asked), 2)
        labels.get("ark:/99999/ir/person/9")  # and kept, though there is no label
        self.assertEqual(len(asked), 2)

    def test_the_store_gives_the_record_of_a_resource_uri(self):
        with self.application.new_store("test_repo") as store:
            found = store.resource_records([ADA, DELFT, "ark:/99999/ir/person/99"])
        self.assertEqual(
            found,
            {
                ADA: ("Ada Lovelace", "rec:person:1", "ada-lovelace"),
                DELFT: ("Delft University", "rec:org:1", "delft-university"),
            },
        )
        with self.application.new_store("test_repo") as store:
            self.assertEqual(store.resource_records([]), {})


class FormTest(ReferenceCase):
    def test_the_members_and_the_host_are_references_to_their_classes(self):
        page = self.page(ROSTER_ADD)
        found = [
            json.loads(html.unescape(c))
            for c in re.findall(r'data-classes="([^"]*)"', page)
        ]
        self.assertIn([PERSON], found)
        self.assertIn([ORGANIZATION, EX + "University"], found)
        self.assertIn('data-lookup="http://localhost/test_repo/lookup"', page)
        self.assertIn('role="combobox"', page)
        # a repository without the type has a text for the uri
        self.application.create_repository(
            "plain",
            admin=("admin", "correct horse battery"),
            content_types=[ROSTER],
        )
        plain = self.page("/plain/roster/add")
        self.assertNotIn("reference-input", plain)  # there is no type of persons there

    def test_the_uri_is_posted_and_stored_and_the_label_is_what_the_form_shows(self):
        self.save_roster([ADA], host=DELFT)
        roster = self.record("roster")
        head = roster.graph.objects(roster.resource, EX + "members")
        self.assertEqual([o.value for o in roster.graph.walk_list(head[0])], [ADA])
        self.assertEqual([o.value for o in self.values("host", "roster")], [DELFT])
        page = self.roster_page()
        self.assertRegex(page, r'name="item" value="' + re.escape(ADA) + '"')
        tags = reference_inputs(page)
        self.assertTrue(
            any(
                'value="Ada Lovelace"' in t and 'title="rec:person:1"' in t
                for t in tags
            )
        )
        self.assertTrue(any('value="Delft University"' in t for t in tags))
        self.assertIn("reference input-group flex-nowrap is-set", page)
        # the text tells the record and not the resource uri
        titles = sorted(must_search(r'title="([^"]*)"', t)[1] for t in tags)
        self.assertEqual(titles, ["rec:org:1", "rec:person:1"])
        self.assertFalse(any(ADA in t or DELFT in t for t in tags))
        self.assertEqual(roster.validate(), [])

    def test_a_uri_that_is_no_record_is_shown_as_it_is_and_a_form_shown_again_has_labels(
        self,
    ):
        self.save_roster(["ark:/99999/ir/person/99"])
        tags = reference_inputs(self.roster_page())
        self.assertTrue(any('value="ark:/99999/ir/person/99"' in t for t in tags))
        response = self.post(ROSTER_ADD, rows={"members": [ADA]}, name=" ")
        self.assertEqual(response.status_int, 200)  # no name: it is shown again
        self.assertTrue(
            any('value="Ada Lovelace"' in t for t in reference_inputs(response.text))
        )
        self.assertIn(f'value="{ADA}"', response.text)

    def test_every_label_of_a_form_is_asked_in_one_question(self):
        self.save_roster([ADA, "ark:/99999/ir/person/2"], host=DELFT)
        asked = []
        original = forms.ReferenceLabels.prefetch

        def counting(self, uris):
            asked.append(sorted(u for u in uris if u and u not in self.found))
            return original(self, uris)

        forms.ReferenceLabels.prefetch = counting
        self.addCleanup(setattr, forms.ReferenceLabels, "prefetch", original)
        page = self.roster_page()
        self.assertIn("Ada Byron", page)
        self.assertEqual(
            sum(1 for q in asked if len(q) > 1), 1
        )  # the members, together

    def test_a_value_that_is_a_node_stays_when_the_references_are_saved(self):
        self.save_roster([ADA], host=DELFT)
        roster = self.record("roster")
        university = ox.BlankNode()
        roster.graph.add_value(roster.resource, EX + "host", university)
        roster.graph.add_value(
            university, forms.RDF_TYPE, ox.NamedNode(EX + "University")
        )
        values = roster.form_values()
        values["resource"]["host"] = ["ark:/99999/ir/org/2"]
        roster.apply_form(values)
        found = roster.graph.objects(roster.resource, EX + "host")
        self.assertEqual(
            sorted(type(o).__name__ for o in found), ["BlankNode", "NamedNode"]
        )
        self.assertEqual(
            roster.graph.objects(university, forms.RDF_TYPE),
            [ox.NamedNode(EX + "University")],
        )


def group(page: str, uri: str) -> str:
    """The group of the reference that has the uri chosen."""
    found = re.search(
        r'(?s)<div class="reference input-group[^>]*>(?:(?!<div class="reference ).)*?'
        r'value="' + re.escape(uri) + '" class="reference-value".*?<ul class',
        page,
    )
    assert found, uri
    return found[0]


class ButtonTest(ReferenceCase):
    def test_a_chosen_resource_has_an_edit_button_for_its_form_and_its_types_icon(self):
        self.save_roster([ADA], host=DELFT)
        page = self.roster_page()
        ada = group(page, ADA)
        self.assertRegex(ada, r">Edit</span></button>")
        self.assertIn('class="bi bi-pencil me-1"', ada)
        self.assertIn(
            'data-edit-url="http://localhost/test_repo/person/ada-lovelace/edit?embed=1"',
            ada,
        )
        self.assertIn("bi bi-person", ada)
        delft = group(page, DELFT)
        self.assertIn("bi bi-building", delft)
        self.assertIn("/test_repo/org/delft-university/edit?embed=1", delft)

    def test_no_resource_has_a_new_button_for_the_form_of_a_new_one_and_the_icon_of_its_type(
        self,
    ):
        page = self.page(ROSTER_ADD)
        tags = re.findall(r'<div class="reference input-group[^>]*>', page)
        self.assertEqual(len(tags), 2)  # the members and the host
        news = [
            json.loads(html.unescape(must_search(r'data-new="([^"]*)"', t)[1]))
            for t in tags
        ]
        self.assertIn(
            [
                {
                    "icon": "person",
                    "label": "Person",
                    "url": "http://localhost/test_repo/person/add?embed=1",
                }
            ],
            news,
        )
        self.assertTrue(
            any(n and n[0]["url"].endswith("/org/add?embed=1") for n in news)
        )
        self.assertIn(">New</span></button>", page)
        self.assertIn('class="bi bi-stars me-1"', page)  # and not the + of a list
        self.assertNotIn(">Edit</span>", page)
        icons = re.findall(
            r'data-default-icon="([^"]*)"><i class="bi bi-([^"]*)"', page
        )
        self.assertEqual(
            sorted(icons), [("building", "building"), ("person", "person")]
        )

    def test_a_person_who_may_not_edit_that_type_has_no_edit_button(self):
        from gracia.forms import edit_address

        class Request:
            def has_permission(self, permission, node):
                return False

        repository = self.application["test_repo"]
        self.assertEqual(edit_address(Request(), repository, "rec:person:1", "ada"), "")
        self.assertEqual(edit_address(Request(), repository, "rec:nowhere:1", None), "")
        self.assertEqual(edit_address(Request(), repository, "not a record", None), "")


class EmbedTest(ReferenceCase):
    def test_a_page_in_a_dialog_is_the_form_without_the_page_around_it(self):
        embedded = self.page("/test_repo/person/add?embed=1")
        whole = self.page("/test_repo/person/add")
        self.assertIn("gracia-logo", whole)
        self.assertNotIn("gracia-logo", embedded)
        self.assertNotIn("<footer", embedded)
        self.assertRegex(embedded, r"<script[^>]*embed\.js")
        self.assertNotRegex(whole, r"<script[^>]*embed\.js")
        self.assertIn('name="name"', embedded)  # the form is there
        self.assertIn("container-fluid px-4 py-4", embedded)  # with room around it
        self.assertNotIn("container-fluid px-4 py-4", whole)
        form = self.web.get("/test_repo/person/add?embed=1", headers=ADMIN).forms[0]
        response = form.submit("save", headers=POSTING, expect_errors=True)  # no name
        self.assertEqual(response.status_int, 200)  # shown again in the dialog
        self.assertNotIn("gracia-logo", response.text)

    def test_saving_in_a_dialog_stays_in_it_and_says_what_it_is_pointed_at_by_and_how_it_is_shown(
        self,
    ):
        form = self.web.get("/test_repo/person/add?embed=1", headers=ADMIN).forms[0]
        form["name"] = "Grace Hopper"
        response = form.submit("save", headers=POSTING)
        self.assertEqual(response.status_int, 302)
        self.assertIn("embed=1", response.headers["Location"])
        self.assertIn("saved=1", response.headers["Location"])
        page = self.page(response.headers["Location"])
        said = re.search(r'<div id="gracia-saved"[^>]*>', page)
        assert said is not None
        self.assertIn('data-uri="ark:/99999/ir/person/3"', said[0])
        self.assertIn('data-label="Grace Hopper"', said[0])
        self.assertIn(
            'data-edit="http://localhost/test_repo/person/grace-hopper/edit?embed=1"',
            said[0],
        )
        self.assertIn('data-icon="person"', said[0])
        self.assertIn('data-record="rec:person:3"', said[0])

    def stored_label(self, resource_uri: str) -> str:
        with self.application.new_store("test_repo") as store:
            return store.resource_records([resource_uri])[resource_uri][0]

    def test_the_label_that_is_told_is_the_one_the_save_made_and_a_record_that_is_not_saved_says_nothing(
        self,
    ):
        form = self.web.get(
            "/test_repo/person/ada-lovelace/edit?embed=1", headers=ADMIN
        ).forms[0]
        self.assertEqual(self.stored_label(ADA), "Ada Lovelace")
        self.assertNotIn("gracia-saved", self.page(form.action))  # not saved yet
        form["name"] = "Ada King"  # the label follows the name when it is saved
        where = form.submit("save", headers=POSTING).headers["Location"]
        said = must_search(r'<div id="gracia-saved"[^>]*>', self.page(where))
        self.assertEqual(self.stored_label(ADA), "Ada King")
        self.assertIn('data-label="Ada King"', said[0])
        self.assertIn(f'data-uri="{ADA}"', said[0])
        page = self.page("/test_repo/person/ada-lovelace/edit?saved=1")
        self.assertNotIn("gracia-saved", page)  # not in a dialog

    def test_the_script_that_tells_waits_for_the_page_it_is_in_the_head_of(self):
        """The script is in the head, where the element that says what was saved is not
        yet: it has to look when the page has been read (it did not, and the dialog
        stayed open)."""
        script = (
            Path(forms.__file__).parent / "assets" / "static" / "scripts" / "embed.js"
        ).read_text()
        self.assertIn("DOMContentLoaded", script)
        self.assertIn("readyState", script)
