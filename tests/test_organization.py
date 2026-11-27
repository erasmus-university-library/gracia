import base64
import html
import json
import re
from typing import ClassVar

import pyoxigraph as ox
from harness import BUNDLED, ConfiguredCase
from support import uri_select

from gracia import forms
from gracia.utils import assets

SCHEMA = "https://schema.org/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:correct horse").decode()}
ADD = "/test_repo/organization/add"
CLASSES = [
    "Organization",
    "EducationalOrganization",
    "CollegeOrUniversity",
    "ResearchOrganization",
    "Corporation",
    "FundingScheme",
]


class URILabelTest(ConfiguredCase):
    def test_the_label_of_a_uri_is_its_name_without_the_namespace_in_words(self):
        for class_uri, label in (
            (SCHEMA + "Organization", "Organization"),
            (SCHEMA + "EducationalOrganization", "Educational organization"),
            (SCHEMA + "CollegeOrUniversity", "College or university"),
            (SCHEMA + "FundingScheme", "Funding scheme"),
            ("http://example.org/ns#ISSNRegistry", "ISSN registry"),
            ("http://example.org/a/b", "B"),
        ):
            with self.subTest(class_uri=class_uri):
                self.assertEqual(forms.uri_label(class_uri), label)


class OrganizationCase(ConfiguredCase):
    def setUp(self):
        super().setUp()
        self.application.create_repository(
            "test_repo",
            admin=("admin", "correct horse"),
            content_types=[BUNDLED["organization"]],
        )
        self.web = self.client()

    def post(self, kind: str | None, name="University of Delft", path=ADD, items=()):
        form = self.web.get(path, headers=AUTH).forms[0]
        out = []
        for field, value in form.submit_fields("save"):
            if field == "type" and kind is not None:
                value = kind
            if field == "name" and not value:
                value = name
            out.append((field, value))
            if (field, value) == ("__start__", "identifier:sequence"):
                for item in items:
                    out += [("__start__", "item:mapping"), *item]
                    out += [("__end__", "item:mapping")]
        return self.web.post(path, params=out, headers=AUTH, expect_errors=True)

    def record(self, number: int = 1):
        return (
            self.application["test_repo"].types["organization"].find_record(str(number))
        )

    def types_of(self, number: int = 1) -> list[str]:
        record = self.record(number)
        return sorted(o.value for o in record.graph.objects(record.resource, RDF_TYPE))


class SubclassTest(OrganizationCase):
    def test_the_form_has_a_choice_of_the_classes_named_by_their_names(self):
        select = uri_select(self.web.get(ADD, headers=AUTH).text)
        self.assertEqual(
            select.items,
            [("", "- choose -")] + [(SCHEMA + c, forms.uri_label(c)) for c in CLASSES],
        )
        self.assertIn("College or university", select.html)

    def test_the_class_of_the_type_is_the_one_a_new_resource_starts_with(self):
        select = uri_select(self.web.get(ADD, headers=AUTH).text)
        self.assertEqual(select.chosen, SCHEMA + "Organization")

    def test_a_subclass_is_the_only_rdf_type_the_resource_has(self):
        for number, kind in enumerate(CLASSES, 1):
            with self.subTest(kind=kind):
                response = self.post(SCHEMA + kind, name=f"Org {number}")
                self.assertEqual(response.status_int, 302, response.text[:400])
                self.assertEqual(self.types_of(number), [SCHEMA + kind])

    def test_what_is_stored_is_selected_in_the_form_again(self):
        self.post(SCHEMA + "CollegeOrUniversity")
        page = self.web.get(
            "/test_repo/organization/university-of-delft/edit", headers=AUTH
        ).text
        select = uri_select(page)
        self.assertEqual(select.chosen, SCHEMA + "CollegeOrUniversity")
        self.assertEqual(
            select.selected, 1
        )  # one item says it is selected, and only one

    def test_another_class_replaces_the_one_there_was(self):
        self.post(SCHEMA + "CollegeOrUniversity")
        edit = "/test_repo/organization/university-of-delft/edit"
        response = self.post(SCHEMA + "Corporation", path=edit)
        self.assertEqual(response.status_int, 302, response.text[:400])
        self.assertEqual(self.types_of(), [SCHEMA + "Corporation"])  # and not both

    def test_a_class_that_is_not_one_of_them_is_refused(self):
        response = self.post(SCHEMA + "Person")
        self.assertEqual(response.status_int, 200)
        self.assertIn("is not one of", response.text)

    def test_no_class_is_refused(self):
        response = self.post("")
        self.assertEqual(response.status_int, 200)
        self.assertIn("Required", response.text)

    def test_a_resource_of_a_subclass_conforms_to_the_shape_of_the_type(self):
        for number, kind in enumerate(CLASSES, 1):
            with self.subTest(kind=kind):
                self.post(SCHEMA + kind, name=f"Org {number}")
                self.assertEqual(self.record(number).validate(), [])

    def test_two_classes_do_not_conform(self):
        self.post(SCHEMA + "Corporation")
        record = self.record()
        record.graph.add_value(
            record.resource, RDF_TYPE, ox.NamedNode(SCHEMA + "FundingScheme")
        )
        problems = record.validate()
        self.assertTrue(problems)
        self.assertTrue(any("rdf:type" in p or "type" in p for p in problems), problems)

    def test_a_class_that_is_not_allowed_does_not_conform(self):
        self.post(SCHEMA + "Corporation")
        record = self.record()
        record.graph.replace(record.resource, RDF_TYPE, ox.NamedNode(SCHEMA + "Person"))
        self.assertTrue(record.validate())

    def test_the_shape_is_found_by_any_of_its_classes(self):
        content = BUNDLED["organization"]
        path = assets.resolve(
            assets.schema_spec(content.assets, content.schema, content.slug)
        )
        for kind in CLASSES:
            with self.subTest(kind=kind):
                assets.load_shape(
                    path, SCHEMA + kind
                )  # a ValueError when there is none

    def test_the_record_shape_asks_for_one_of_the_classes_and_not_the_class_of_the_type(
        self,
    ):
        content = BUNDLED["organization"]
        shapes, shape = assets.shape(
            content.assets, content.schema, content.class_uri, content.slug
        )
        data, _node = assets.record_shape((shapes, shape), content.class_uri)
        resource = next(
            prop
            for prop in data.objects(
                ox.NamedNode("https://gracia.pub/ns/RecordShape"),
                "http://www.w3.org/ns/shacl#property",
            )
            if ox.NamedNode("https://gracia.pub/ns/resource")
            in data.objects(prop, "http://www.w3.org/ns/shacl#path")
        )
        self.assertEqual(data.objects(resource, "http://www.w3.org/ns/shacl#class"), [])
        (head,) = data.objects(resource, "http://www.w3.org/ns/shacl#or")
        classes = set()
        while isinstance(head, ox.BlankNode):
            (item,) = data.objects(
                head, "http://www.w3.org/1999/02/22-rdf-syntax-ns#first"
            )
            classes |= {
                c.value for c in data.objects(item, "http://www.w3.org/ns/shacl#class")
            }
            rest = data.objects(head, "http://www.w3.org/1999/02/22-rdf-syntax-ns#rest")
            head = rest[0] if rest else None
        self.assertEqual(classes, {SCHEMA + c for c in CLASSES})

    def test_a_type_with_one_class_still_asks_for_that_class(self):
        content = BUNDLED["person"]
        shapes, shape = assets.shape(
            content.assets, content.schema, content.class_uri, content.slug
        )
        data, _node = assets.record_shape((shapes, shape), content.class_uri)
        found = {
            o.value
            for q in data.dataset
            if q.predicate.value == "http://www.w3.org/ns/shacl#class"
            for o in [q.object]
            if o.value == SCHEMA + "Person"
        }
        self.assertEqual(found, {SCHEMA + "Person"})


class TabFollowsTheClassTest(OrganizationCase):
    LOOKS: ClassVar[dict[str, tuple[str, str]]] = {
        "Organization": ("Organization", "building"),
        "EducationalOrganization": ("Educational organization", "easel"),
        "CollegeOrUniversity": ("College or university", "mortarboard"),
        "ResearchOrganization": ("Research organization", "lightbulb"),
        "Corporation": ("Corporation", "briefcase"),
        "FundingScheme": ("Funding scheme", "cash-coin"),
    }

    def first_tab(self, page: str) -> tuple[str, str | None]:
        tab = re.search(
            r'(?s)data-bs-toggle="tab".*?'
            r'(?:<i\s+class="bi bi-([a-z-]+) me-1"[^>]*></i>)?'
            r'<span class="tab-title">([^<]*)</span>',
            page,
        )
        assert tab is not None
        return tab[2], tab[1]

    def test_the_shape_says_what_each_class_is_called_and_has_as_an_icon(self):
        content = BUNDLED["organization"]
        shapes, _shape = assets.shape(
            content.assets, content.schema, content.class_uri, content.slug
        )
        said = forms.describe(shapes, [SCHEMA + c for c in self.LOOKS])
        self.assertEqual(
            {
                c: (said[SCHEMA + c]["label"], said[SCHEMA + c]["icon"])
                for c in self.LOOKS
            },
            self.LOOKS,
        )

    def test_a_class_the_shape_does_not_describe_is_named_by_its_uri(self):
        content = BUNDLED["organization"]
        shapes, _shape = assets.shape(
            content.assets, content.schema, content.class_uri, content.slug
        )
        said = forms.describe(shapes, ["http://example.org/ns#SomethingElse"])
        self.assertEqual(
            said["http://example.org/ns#SomethingElse"],
            {"label": "Something else", "icon": None},
        )

    def test_the_options_of_the_select_have_the_names_the_shape_gives(self):
        select = uri_select(self.web.get(ADD, headers=AUTH).text)
        self.assertEqual(
            [label for _value, label in select.items],
            ["- choose -", *(label for label, _icon in self.LOOKS.values())],
        )

    def test_the_options_have_the_icons_the_shape_gives(self):
        select = uri_select(self.web.get(ADD, headers=AUTH).text)
        icons = dict(
            re.findall(r'data-value="([^"]+)"[^>]*data-icon="([^"]*)"', select.html)
        )
        self.assertEqual(
            {c.removeprefix(SCHEMA): i for c, i in icons.items()},
            {c: icon for c, (_label, icon) in self.LOOKS.items()},
        )

    def test_the_select_tells_the_browser_what_the_tab_is_called_and_has(self):
        page = self.web.get(ADD, headers=AUTH).text
        attribute = re.search(r'name="type"[^>]*data-follow-tab="([^"]*)"', page)
        assert attribute is not None
        said = json.loads(html.unescape(attribute[1]))
        self.assertEqual(
            {c.removeprefix(SCHEMA): (d["label"], d["icon"]) for c, d in said.items()},
            self.LOOKS,
        )

    def test_a_new_resource_has_the_tab_of_the_class_of_its_type(self):
        self.assertEqual(
            self.first_tab(self.web.get(ADD, headers=AUTH).text),
            ("Organization", "building"),
        )

    def test_the_tab_of_a_resource_that_is_there_is_named_by_its_class(self):
        for number, (kind, look) in enumerate(self.LOOKS.items(), 1):
            with self.subTest(kind=kind):
                self.post(SCHEMA + kind, name=f"Org {number}")
                page = self.web.get(
                    f"/test_repo/organization/org-{number}/edit", headers=AUTH
                ).text
                self.assertEqual(self.first_tab(page), look)

    def test_the_tab_keeps_the_class_that_was_chosen_when_the_form_is_wrong(self):
        response = self.post(SCHEMA + "Corporation", name="Ok")
        self.assertEqual(response.status_int, 302)
        form = self.web.get(ADD, headers=AUTH).forms[0]
        out = [
            (k, SCHEMA + "CollegeOrUniversity" if k == "type" else v)
            for k, v in form.submit_fields("save")
        ]  # no name: the form is wrong, and says it
        again = self.web.post(ADD, params=out, headers=AUTH)
        self.assertEqual(again.status_int, 200)
        self.assertEqual(self.first_tab(again.text), self.LOOKS["CollegeOrUniversity"])


class OrganizationIdentifierTest(OrganizationCase):
    KINDS: ClassVar[dict[str, tuple[str, str | None]]] = {
        "ROR": ("05gq02987", "https://ror.org/05gq02987"),
        "Local": ("org-17", None),
        "External": ("whatever 1", None),
        "OpenAlex": ("I114027177", "https://openalex.org/I114027177"),
        "Scopus": (
            "60030347",
            "https://www.scopus.com/affil/profile.uri?afid=60030347",
        ),
        "Wikidata": ("Q1065", "https://www.wikidata.org/wiki/Q1065"),
        "GRID": ("grid.4989.c", "https://grid.ac/institutes/grid.4989.c"),
    }

    def identifier(self, number: int = 1):
        record = self.record(number)
        (node,) = record.graph.objects(record.resource, SCHEMA + "identifier")
        return {
            q.predicate.value.removeprefix(SCHEMA): q.object
            for q in record.graph.dataset.quads_for_subject(node)
        }

    def with_identifier(self, kind: str, value: str, url: str = "", name="Org"):
        return self.post(
            None,
            name=name,
            items=[[("propertyID", kind), ("value", value), ("url", url)]],
        )

    def test_the_kinds_of_identifier_of_an_organization(self):
        self.post(
            None, items=[[("propertyID", "ROR"), ("value", "05gq02987"), ("url", "")]]
        )
        page = self.web.get(
            "/test_repo/organization/university-of-delft/edit", headers=AUTH
        ).text
        select = re.search(r'(?s)<select[^>]*name="propertyID".*?</select>', page)
        assert select is not None
        self.assertEqual(re.findall(r'value="([^"]*)"', select[0]), ["", *self.KINDS])

    def test_the_url_is_made_for_the_kinds_that_have_one(self):
        for number, (kind, (value, url)) in enumerate(self.KINDS.items(), 1):
            with self.subTest(kind=kind):
                response = self.with_identifier(kind, value, name=f"Org {number}")
                self.assertEqual(response.status_int, 302, response.text[:400])
                found = self.identifier(number).get("url")
                self.assertEqual(found, ox.NamedNode(url) if url else None)

    def test_a_value_that_is_not_in_the_form_of_its_kind_is_refused_with_its_message(
        self,
    ):
        for kind, value, said in (
            ("ROR", "not a ror", "A ROR id is nine characters"),
            ("OpenAlex", "5023", "An OpenAlex id is a letter and digits"),
            ("Scopus", "12", "A Scopus affiliation id is 6 to 12 digits"),
            ("Wikidata", "1065", "A Wikidata id is a Q and digits"),
            ("GRID", "4989", "A GRID id is like grid.4989.c"),
        ):
            with self.subTest(kind=kind):
                response = self.with_identifier(kind, value)
                self.assertEqual(response.status_int, 200)
                self.assertIn(said, response.text)

    def test_a_url_of_a_kind_has_to_start_as_the_kind_says(self):
        response = self.with_identifier("ROR", "05gq02987", "https://example.org/x")
        self.assertEqual(response.status_int, 200)
        self.assertIn("A ROR URL starts with https://ror.org/", response.text)

    def test_the_kinds_of_a_person_are_not_the_kinds_of_an_organization(self):
        response = self.with_identifier("ORCID", "0000-0002-1825-0097")
        self.assertEqual(response.status_int, 200)
        self.assertIn("is not one of", response.text)
