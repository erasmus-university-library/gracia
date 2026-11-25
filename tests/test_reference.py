"""The reference widget: the lookup of the repository, and the form that shows the label
of what it points at and posts its uri."""

import html
import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

import pyoxigraph as ox
from pyramid.registry import Registry
from support import must_search
from test_dates import ADMIN, POSTING
from test_list_widget import ListCase

from gracia import forms, types
from gracia.actions import RecordsChanged, process_pending
from gracia.search import Indexes, on_records_changed
from gracia.views.lookup import item, visible

PERSON = "https://schema.org/Person"
UNIVERSITY = "https://schema.org/CollegeOrUniversity"
ADA, DELFT = "ark:/99999/ir/person/1", "ark:/99999/ir/organization/1"
WORK = "/refs/work/add"


class ReferenceCase(ListCase):
    def setUp(self):
        super().setUp()
        self.application.create_repository(
            "refs",
            admin=("admin", "correct horse battery"),
            content_types=[
                types.available()[k] for k in ("work", "person", "organization")
            ],
        )
        self._index = tempfile.TemporaryDirectory()
        self.addCleanup(self._index.cleanup)
        self.application.indexes = Indexes(Path(self._index.name))
        self.person("Ada Lovelace")
        self.person("Ada Byron")
        self.organization("Delft University")
        self.reindex()

    def create(self, kind: str, **fields) -> None:
        form = self.web.get(f"/refs/{kind}/add", headers=ADMIN).forms[0]
        for name, value in fields.items():
            form[name] = value
        self.assertEqual(form.submit("save", headers=POSTING).status_int, 302)

    def person(self, name: str) -> None:
        self.create("person", name=name)

    def organization(self, name: str) -> None:
        self.create("organization", name=name, type=UNIVERSITY)

    def reindex(self) -> None:
        registry = Registry()
        registry.registerHandler(on_records_changed, (RecordsChanged,))
        process_pending(self.application, registry)

    def lookup(self, text: str, *classes: str, who=ADMIN):
        params = [("q", text), *(("type", c) for c in classes)]
        return self.web.get(
            "/refs/lookup", params=params, headers=who, expect_errors=True
        )

    def found(self, text: str, *classes: str) -> list[str]:
        response = self.lookup(text, *classes)
        self.assertEqual(response.status_int, 200, response.text)
        return [i["label"] for i in response.json["items"]]

    def save_work(self, authors, publisher="", name="A work") -> None:
        params = self.fields(WORK, authors, name)
        if publisher:  # the first row of the bag of the publisher
            params = self.with_publisher(params, publisher)
        response = self.web.post(
            WORK, params=params, headers=POSTING, expect_errors=True
        )
        self.assertEqual(response.status_int, 302, response.text[:500])

    def with_publisher(self, params, publisher):
        seen, out = False, []
        for field, value in params:
            if (field, value) == ("__start__", "publisher:sequence"):
                seen = True
                out += [(field, value), ("item", publisher)]
            elif seen and (field, value) == ("item", ""):
                continue
            else:
                out.append((field, value))
        return out

    def work_page(self, number=1) -> str:
        return self.web.get(f"/refs/work/{number}/edit", headers=ADMIN).text


def reference_inputs(page: str) -> list[str]:
    """The tags of the text inputs of the references on a page."""
    return re.findall(r"<input[^>]*reference-input[^>]*>", page)


class LookupViewTest(ReferenceCase):
    def test_it_finds_the_records_of_the_classes_by_the_beginning_of_the_label(self):
        self.assertEqual(self.found("ada", PERSON), ["Ada Byron", "Ada Lovelace"])
        self.assertEqual(self.found("ada lov", PERSON), ["Ada Lovelace"])
        self.assertEqual(self.found("del", UNIVERSITY), ["Delft University"])

    def test_the_class_is_the_class_of_the_resource_whatever_the_type_of_the_record(
        self,
    ):
        self.assertEqual(self.found("ada", UNIVERSITY), [])
        self.assertEqual(self.found("del", PERSON), [])
        self.assertEqual(self.found("del", PERSON, UNIVERSITY), ["Delft University"])

    def test_without_a_class_it_finds_every_record(self):
        self.assertEqual(self.found("d"), ["Delft University"])
        self.assertEqual(len(self.found("ada")), 2)

    def test_an_item_has_the_uri_to_point_at_and_the_label_to_show(self):
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
                    "edit": "http://localhost/refs/person/ada-lovelace/edit?embed=1",
                    "icon": "person",
                }
            ],
        )

    def test_an_item_says_what_the_resource_is_by_the_name_its_shape_gives_the_class(
        self,
    ):
        (found,) = self.lookup("del", UNIVERSITY).json["items"]
        self.assertEqual(found["kind"], "College or university")  # not "organization"
        self.assertEqual(
            (found["type"], found["record"]), ("organization", "rec:organization:1")
        )

    def test_digits_find_the_record_of_that_number(self):
        found = self.lookup("2", PERSON).json["items"]
        self.assertEqual(
            [(i["label"], i["record"]) for i in found], [("Ada Byron", "rec:person:2")]
        )
        self.assertEqual(
            self.lookup("2", UNIVERSITY).json["items"], []
        )  # not an organization

    def test_nothing_typed_is_every_record_of_the_classes(self):
        self.assertEqual(self.found("", PERSON), ["Ada Byron", "Ada Lovelace"])
        self.assertEqual(self.found("", UNIVERSITY), ["Delft University"])
        # with no class every record is one, those of the system too (the widget always
        # says the classes)
        self.assertTrue(
            {"Ada Byron", "Delft University", "admin"} <= set(self.found(""))
        )

    def page(self, **params):
        query = [("q", ""), ("type", PERSON), *params.items()]
        response = self.web.get("/refs/lookup", params=query, headers=ADMIN)
        self.assertEqual(response.status_int, 200, response.text)
        return response.json

    def test_a_page_has_the_size_asked_for_and_says_where_it_is(self):
        first = self.page(size=1)
        self.assertEqual([i["label"] for i in first["items"]], ["Ada Byron"])
        self.assertEqual(
            (first["page"], first["pages"], first["size"], first["total"]), (1, 2, 1, 2)
        )
        second = self.page(size=1, page=2)
        self.assertEqual([i["label"] for i in second["items"]], ["Ada Lovelace"])
        self.assertEqual((second["page"], second["pages"]), (2, 2))
        self.assertFalse(second["truncated"])

    def test_a_page_that_is_not_there_is_the_last_and_a_wrong_number_the_first(self):
        self.assertEqual(self.page(size=1, page=9)["page"], 2)
        self.assertEqual(self.page(size=1, page=0)["page"], 1)
        self.assertEqual(self.page(size=1, page="two")["page"], 1)
        self.assertEqual(self.page(size="many")["size"], 10)  # the usual
        self.assertEqual(self.page(size=500)["size"], 50)  # and no more than that

    def test_everything_on_one_page_is_one_page(self):
        everything = self.page()
        self.assertEqual((everything["page"], everything["pages"]), (1, 1))
        self.assertEqual(everything["total"], 2)

    def test_what_may_not_be_seen_is_not_counted(self):
        # (what `visible` leaves out is left out of the total, so the pages are full)
        self.assertEqual(self.page(size=2)["total"], len(self.page(size=2)["items"]))

    def test_of_a_great_many_it_says_that_only_the_first_are_paged(self):
        from unittest.mock import patch

        with patch("gracia.search.LOOKUP_CAP", 1):
            found = self.page()
        self.assertTrue(found["truncated"])
        self.assertEqual(found["total"], 1)

    def test_it_is_private_to_who_asks(self):
        response = self.lookup("ada", PERSON)
        self.assertEqual(response.headers["Cache-Control"], "private")

    def test_a_repository_with_no_index_says_so(self):
        self.application.indexes = None
        response = self.lookup("ada", PERSON)
        self.assertEqual(response.status_int, 503)
        self.assertEqual(response.json["items"], [])
        self.assertIn("no search index", response.json["problem"])

    def test_an_index_that_is_not_there_yet_says_so(self):
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
        return [h["record"] for h in visible(who, who, self.hits)]

    def test_a_record_of_a_type_that_may_not_be_viewed_is_not_there(self):
        self.assertEqual(self.records(Allowed({"person"}, set())), ["rec:person:1"])

    def test_an_unpublished_record_is_for_who_may_edit_its_type(self):
        both = Allowed({"person"}, {"person"})
        self.assertEqual(self.records(both), ["rec:person:1", "rec:person:2"])

    def test_the_uri_is_the_resource_uri_or_the_ark_of_the_record(self):
        self.assertEqual(
            item({"record": "rec:person:7", "resource_uri": "https://x/y"})["uri"],
            "https://x/y",
        )
        self.assertEqual(
            item({"record": "rec:person:7"})["uri"], "ark:/99999/ir/person/7"
        )
        self.assertEqual(item({"record": "rec:person:7"})["label"], "rec:person:7")


class ShapeClassesTest(ReferenceCase):
    def repository(self):
        return self.application["refs"]

    def test_the_classes_are_those_of_the_type_that_has_the_shape(self):
        shape = ox.NamedNode("http://example.org/shapes#PersonShape")
        self.assertEqual(self.repository().shape_classes([shape]), [PERSON])
        organization = ox.NamedNode("http://example.org/shapes#OrganizationShape")
        self.assertEqual(len(self.repository().shape_classes([organization])), 6)

    def test_a_shape_no_type_has_gives_no_class(self):
        shape = ox.NamedNode("http://example.org/shapes#NoSuchShape")
        self.assertEqual(self.repository().shape_classes([shape]), [])

    def test_a_reference_says_its_shapes_in_the_shape_of_the_work(self):
        work = self.repository().types["work"]
        graph, shape = work.shacl_shape(include_record_shape=False)
        said = {
            path: sorted(s.value for s in forms.reference_shapes(graph, prop))
            for path, prop in forms.properties(graph, shape)
        }
        base = "http://example.org/shapes#"
        self.assertEqual(
            said["https://schema.org/author"], [base + "PersonShape"]
        )  # a list
        self.assertEqual(
            said["https://schema.org/publisher"], [base + "OrganizationShape"]
        )
        self.assertIn(base + "CollectionShape", said["https://schema.org/isPartOf"])


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
        with self.application.new_store("refs") as store:
            found = store.resource_records([ADA, DELFT, "ark:/99999/ir/person/99"])
        self.assertEqual(
            found,
            {
                ADA: ("Ada Lovelace", "rec:person:1", "ada-lovelace"),
                DELFT: ("Delft University", "rec:organization:1", "delft-university"),
            },
        )
        with self.application.new_store("refs") as store:
            self.assertEqual(store.resource_records([]), {})


class FormTest(ReferenceCase):
    def test_the_author_and_the_publisher_are_references_to_their_classes(self):
        page = self.web.get(WORK, headers=ADMIN).text
        found = [
            json.loads(__import__("html").unescape(c))
            for c in re.findall(r'data-classes="([^"]*)"', page)
        ]
        self.assertIn([PERSON], found)
        self.assertTrue(
            any(UNIVERSITY in classes and len(classes) == 6 for classes in found)
        )
        self.assertIn('data-lookup="http://localhost/refs/lookup"', page)
        self.assertIn('role="combobox"', page)

    def test_a_repository_without_the_type_has_a_text_for_the_uri(self):
        page = self.web.get("/work_repo/work/add", headers=ADMIN).text
        self.assertNotIn("reference-input", page)  # there is no type of persons there

    def test_the_uri_is_posted_and_stored_and_the_label_is_what_the_form_shows(self):
        self.save_work([ADA], publisher=DELFT)
        work = self.application["refs"].types["work"]["1"]
        head = work.graph.objects(work.resource, "https://schema.org/author")
        self.assertEqual([o.value for o in work.graph.walk_list(head[0])], [ADA])
        publisher = work.graph.objects(work.resource, "https://schema.org/publisher")
        self.assertEqual([o.value for o in publisher], [DELFT])
        page = self.work_page()
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

    def test_every_label_of_a_form_is_asked_in_one_question(self):
        self.save_work([ADA, "ark:/99999/ir/person/2"], publisher=DELFT)
        asked = []
        original = forms.ReferenceLabels.prefetch

        def counting(self, uris):
            asked.append(sorted(u for u in uris if u and u not in self.found))
            return original(self, uris)

        forms.ReferenceLabels.prefetch = counting
        self.addCleanup(setattr, forms.ReferenceLabels, "prefetch", original)
        page = self.work_page()
        self.assertIn("Ada Byron", page)
        self.assertEqual(
            sum(1 for q in asked if len(q) > 1), 1
        )  # the authors, together

    def test_the_text_tells_the_record_and_not_the_resource_uri(self):
        self.save_work([ADA], publisher=DELFT)
        tags = reference_inputs(self.work_page())
        titles = sorted(must_search(r'title="([^"]*)"', t)[1] for t in tags)
        self.assertEqual(titles, ["rec:organization:1", "rec:person:1"])
        self.assertFalse(any(ADA in t or DELFT in t for t in tags))

    def test_a_uri_that_is_no_record_is_shown_as_it_is(self):
        self.save_work(["ark:/99999/ir/person/99"])
        tags = reference_inputs(self.work_page())
        self.assertTrue(any('value="ark:/99999/ir/person/99"' in t for t in tags))

    def test_a_form_that_is_shown_again_has_the_labels_of_the_choices(self):
        params = self.fields(WORK, [ADA], name="")  # no name: it is shown again
        response = self.web.post(
            WORK, params=params, headers=POSTING, expect_errors=True
        )
        self.assertEqual(response.status_int, 200)
        self.assertTrue(
            any('value="Ada Lovelace"' in t for t in reference_inputs(response.text))
        )
        self.assertIn(f'value="{ADA}"', response.text)

    def test_a_work_with_an_author_that_was_looked_for_conforms(self):
        self.save_work([ADA, "ark:/99999/ir/person/2"])
        self.assertEqual(self.application["refs"].types["work"]["1"].validate(), [])


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
        self.save_work([ADA], publisher=DELFT)
        page = self.work_page()
        ada = group(page, ADA)
        self.assertRegex(ada, r">Edit</span></button>")
        self.assertIn('class="bi bi-pencil me-1"', ada)
        self.assertIn(
            'data-edit-url="http://localhost/refs/person/ada-lovelace/edit?embed=1"',
            ada,
        )
        self.assertIn("bi bi-person", ada)
        delft = group(page, DELFT)
        self.assertIn("bi bi-building", delft)
        self.assertIn("/refs/organization/delft-university/edit?embed=1", delft)

    def test_no_resource_has_a_new_button_for_the_form_of_a_new_one(self):
        page = self.web.get(WORK, headers=ADMIN).text
        tags = re.findall(r'<div class="reference input-group[^>]*>', page)
        self.assertEqual(len(tags), 2)  # the author and the publisher
        news = [
            json.loads(html.unescape(must_search(r'data-new="([^"]*)"', t)[1]))
            for t in tags
        ]
        self.assertIn(
            [
                {
                    "icon": "person",
                    "label": "Person",
                    "url": "http://localhost/refs/person/add?embed=1",
                }
            ],
            news,
        )
        self.assertTrue(
            any(n and n[0]["url"].endswith("/organization/add?embed=1") for n in news)
        )
        self.assertIn(">New</span></button>", page)
        self.assertIn('class="bi bi-stars me-1"', page)  # and not the + of a list
        self.assertNotIn(">Edit</span>", page)

    def test_the_icon_of_a_reference_with_nothing_chosen_is_the_one_of_its_type(self):
        page = self.web.get(WORK, headers=ADMIN).text
        icons = re.findall(
            r'data-default-icon="([^"]*)"><i class="bi bi-([^"]*)"', page
        )
        self.assertEqual(
            sorted(icons), [("building", "building"), ("person", "person")]
        )

    def test_a_person_who_may_not_edit_that_type_has_no_edit_button(self):
        from gracia.resources import edit_address

        class Request:
            def has_permission(self, permission, node):
                return False

        self.assertEqual(
            edit_address(Request(), self.application["refs"], "rec:person:1", "ada"), ""
        )
        self.assertEqual(
            edit_address(Request(), self.application["refs"], "rec:nowhere:1", None), ""
        )
        self.assertEqual(
            edit_address(Request(), self.application["refs"], "not a record", None), ""
        )


class EmbedTest(ReferenceCase):
    def test_a_page_in_a_dialog_is_the_form_without_the_page_around_it(self):
        embedded = self.web.get("/refs/person/add?embed=1", headers=ADMIN).text
        whole = self.web.get("/refs/person/add", headers=ADMIN).text
        self.assertIn("gracia-logo", whole)
        self.assertNotIn("gracia-logo", embedded)
        self.assertNotIn("<footer", embedded)
        self.assertRegex(embedded, r"<script[^>]*embed\.js")
        self.assertNotRegex(whole, r"<script[^>]*embed\.js")
        self.assertIn('name="name"', embedded)  # the form is there
        self.assertIn("container-fluid px-4 py-4", embedded)  # with room around it
        self.assertNotIn("container-fluid px-4 py-4", whole)

    def test_saving_a_new_record_in_a_dialog_stays_in_the_dialog(self):
        form = self.web.get("/refs/person/add?embed=1", headers=ADMIN).forms[0]
        form["name"] = "Grace Hopper"
        response = form.submit("save", headers=POSTING)
        self.assertEqual(response.status_int, 302)
        self.assertIn("embed=1", response.headers["Location"])
        self.assertIn("saved=1", response.headers["Location"])

    def test_the_saved_record_says_what_it_is_pointed_at_by_and_how_it_is_shown(self):
        form = self.web.get("/refs/person/add?embed=1", headers=ADMIN).forms[0]
        form["name"] = "Grace Hopper"
        page = self.web.get(
            form.submit("save", headers=POSTING).headers["Location"], headers=ADMIN
        ).text
        said = re.search(r'<div id="gracia-saved"[^>]*>', page)
        assert said is not None
        self.assertIn('data-uri="ark:/99999/ir/person/3"', said[0])
        self.assertIn('data-label="Grace Hopper"', said[0])
        self.assertIn(
            'data-edit="http://localhost/refs/person/grace-hopper/edit?embed=1"',
            said[0],
        )
        self.assertIn('data-icon="person"', said[0])
        self.assertIn('data-record="rec:person:3"', said[0])

    def test_the_label_that_is_told_is_the_one_the_save_made(self):
        form = self.web.get(
            "/refs/person/ada-lovelace/edit?embed=1", headers=ADMIN
        ).forms[0]
        self.assertEqual(self.stored_label(ADA), "Ada Lovelace")
        form["name"] = "Ada King"  # the label follows the name when it is saved
        where = form.submit("save", headers=POSTING).headers["Location"]
        said = must_search(
            r'<div id="gracia-saved"[^>]*>', self.web.get(where, headers=ADMIN).text
        )
        self.assertEqual(self.stored_label(ADA), "Ada King")
        self.assertIn('data-label="Ada King"', said[0])
        self.assertIn(f'data-uri="{ADA}"', said[0])

    def stored_label(self, resource_uri: str) -> str:
        with self.application.new_store("refs") as store:
            return store.resource_records([resource_uri])[resource_uri][0]

    def test_the_script_that_tells_waits_for_the_page_it_is_in_the_head_of(self):
        """The script is in the head, where the element that says what was saved is not
        yet: it has to look when the page has been read (it did not, and the dialog
        stayed open)."""
        script = (
            Path(forms.__file__).parent / "assets" / "static" / "scripts" / "embed.js"
        ).read_text()
        self.assertIn("DOMContentLoaded", script)
        self.assertIn("readyState", script)

    def test_a_record_that_is_not_saved_in_a_dialog_says_nothing(self):
        page = self.web.get(
            "/refs/person/ada-lovelace/edit?saved=1", headers=ADMIN
        ).text
        self.assertNotIn("gracia-saved", page)
        embedded = self.web.get(
            "/refs/person/ada-lovelace/edit?embed=1", headers=ADMIN
        ).text
        self.assertNotIn("gracia-saved", embedded)  # not saved yet

    def test_a_form_in_a_dialog_that_is_wrong_is_shown_again_in_the_dialog(self):
        form = self.web.get("/refs/person/add?embed=1", headers=ADMIN).forms[0]
        response = form.submit("save", headers=POSTING, expect_errors=True)  # no name
        self.assertEqual(response.status_int, 200)
        self.assertNotIn("gracia-logo", response.text)
