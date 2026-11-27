"""The fields of a work: the box of where it was published (`sh:group`), its volume and
issue, and the kinds of identifier."""

import html
import json
import re
import unittest
from types import SimpleNamespace

import pyoxigraph as ox
from schemaorg.cases import ADD, ADMIN, PEOPLE, POSTING, ListCase
from support import must_search

from gracia import forms
from gracia.forms import TabsWidget
from gracia.graph import Graph

SCHEMA = "https://schema.org/"
EX = "http://example.org/shapes#"
GROUP_BOX = r'(?s)<div class="mb-3 field-group"[^>]*>(.*?)<!-- /field-group -->'
PREFIXES = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix schema: <https://schema.org/> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix ex: <http://example.org/shapes#> .
"""


class GroupRunsTest(ListCase):
    @staticmethod
    def field(name, group=None):
        return SimpleNamespace(name=name, schema=SimpleNamespace(group=group))

    def names(self, runs):
        return [(r.group, [f.name for f in r.fields]) for r in runs]

    def test_fields_next_to_each_other_in_a_group_are_one_run_and_apart_are_two(self):
        found = TabsWidget.runs(
            [
                self.field("a"),
                self.field("b", "G"),
                self.field("c", "G"),
                self.field("d"),
            ]
        )
        self.assertEqual(
            self.names(found), [(None, ["a"]), ("G", ["b", "c"]), (None, ["d"])]
        )
        found = TabsWidget.runs(
            [
                self.field("a", "G"),
                self.field("b"),
                self.field("c", "G"),
                self.field("d", "H"),
            ]
        )
        self.assertEqual(
            self.names(found),
            [("G", ["a"]), (None, ["b"]), ("G", ["c"]), ("H", ["d"])],
        )
        # fields with no group are runs of their own with no name
        found = TabsWidget.runs([self.field("a"), self.field("b")])
        self.assertEqual(self.names(found), [(None, ["a"]), (None, ["b"])])

    def test_a_group_has_the_label_of_the_group_in_the_shape(self):
        graph, shape = self.application["work_repo"].types["work"].shacl_shape(False)
        said = {
            path: forms.property_group(graph, prop)
            for path, prop in forms.properties(graph, shape)
        }
        for path in ("isPartOf", "volumeNumber", "issueNumber", "pageStart", "pageEnd"):
            self.assertEqual(said[SCHEMA + path], "Host Reference", path)
        self.assertIsNone(said[SCHEMA + "name"])


class HostReferenceTest(ListCase):
    def page(self):
        return self.web.get(ADD, headers=ADMIN).text

    def test_the_host_reference_is_one_box_with_the_name_of_the_group_as_a_label(self):
        page = self.page()
        found = must_search(GROUP_BOX, page)
        group, box = found[0], found[1]
        self.assertEqual(
            re.findall(r"<label[^>]*>([^<]*)</label>", box),
            [
                "Host Reference",
                "is part of",
                "volume",
                "issue",
                "page start",
                "page end",
            ],
        )
        self.assertEqual(len(re.findall(r'<div class="mb-3 field-group"', page)), 1)
        outside = re.sub(GROUP_BOX, "", page)
        for name in ("volumeNumber", "issueNumber", "pageStart", "pageEnd"):
            self.assertNotIn(f'name="{name}"', outside)
        self.assertIn(">author<", outside)
        # there is no separate isbn: it is a kind of identifier
        self.assertNotIn('name="isbn"', page)
        label = must_search(
            r'<label class="form-label" id="([^"]*)">Host Reference</label>', group
        )
        self.assertRegex(group, rf'role="group"\s+aria-labelledby="{label[1]}"')
        self.assertLess(group.index("<label"), group.index("field-group-box"))
        self.assertRegex(group, r'class="field-group-box card-body border rounded')

    def test_the_volume_the_issue_and_the_pages_are_saved_and_come_back(self):
        form = self.web.get(ADD, headers=ADMIN).forms[0]
        form["type"] = SCHEMA + "ScholarlyArticle"
        form["name"] = "An article"
        form.fields["item"][0].value = PEOPLE[0]
        for name, value in (
            ("volumeNumber", "12"),
            ("issueNumber", "3"),
            ("pageStart", "101"),
            ("pageEnd", "119"),
        ):
            form[name] = value
        self.assertEqual(form.submit("save", headers=POSTING).status_int, 302)
        work = self.work()
        found = {
            name: [o.value for o in work.graph.objects(work.resource, SCHEMA + name)]
            for name in ("volumeNumber", "issueNumber", "pageStart", "pageEnd")
        }
        self.assertEqual(
            found,
            {
                "volumeNumber": ["12"],
                "issueNumber": ["3"],
                "pageStart": ["101"],
                "pageEnd": ["119"],
            },
        )
        self.assertEqual(work.validate(), [])
        page = self.web.get(f"/work_repo/work/{work.__name__}/edit", headers=ADMIN).text
        box = must_search(GROUP_BOX, page)[1]
        for value in ("12", "3", "101", "119"):
            self.assertIn(f'value="{value}"', box)


class IdentifierKindsTest(ListCase):
    made = 0

    def add(self, work, kind, value, url=None):
        node = ox.BlankNode()
        graph = work.graph
        graph.add_value(work.resource, SCHEMA + "identifier", node)
        graph.add_value(node, forms.RDF_TYPE, ox.NamedNode(SCHEMA + "PropertyValue"))
        graph.add_value(node, SCHEMA + "propertyID", ox.Literal(kind))
        graph.add_value(node, SCHEMA + "value", ox.Literal(value))
        if url:
            graph.add_value(node, SCHEMA + "url", ox.NamedNode(url))

    def work_with(self, kind, value, url=None):
        """A work of its own (a new one each time) that has the identifier."""
        self.made += 1
        self.post(authors=[PEOPLE[0]], name=f"Work {self.made}")
        work = self.work(self.made)
        self.assertEqual(work.validate(), [])  # it is right without the identifier
        self.add(work, kind, value, url)
        return work

    def test_the_kinds_are_doi_isbn_handle_openalex_scopus_local_and_external(self):
        graph, _shape = self.application["work_repo"].types["work"].shacl_shape(False)
        shape = ox.NamedNode("http://example.org/shapes#PropertyValueShape")
        (kind,) = [
            p
            for path, p in forms.properties(graph, shape)
            if path == SCHEMA + "propertyID"
        ]
        (head,) = graph.objects(kind, ox.NamedNode("http://www.w3.org/ns/shacl#in"))
        self.assertEqual(
            [i.value for i in forms._list_items(graph, head)],
            ["DOI", "ISBN", "Handle", "OpenAlex", "Scopus", "Local", "External"],
        )

    def test_a_value_in_the_form_of_its_kind_conforms(self):
        for kind, value, url in (
            ("DOI", "10.1000/xyz123", "https://doi.org/10.1000/xyz123"),
            ("ISBN", "978-3-16-148410-0", "urn:isbn:978-3-16-148410-0"),
            ("ISBN", "0-306-40615-2", "urn:isbn:0-306-40615-2"),
            ("Handle", "20.500.1234/5678", "https://hdl.handle.net/20.500.1234/5678"),
            ("OpenAlex", "W2741809807", "https://openalex.org/W2741809807"),
            (
                "Scopus",
                "2-s2.0-85012345678",
                "https://www.scopus.com/record/display.uri?eid=2-s2.0-85012345678",
            ),
            ("Local", "anything at all", None),
            ("External", "x-1", None),
        ):
            with self.subTest(kind=kind, value=value):
                self.assertEqual(self.work_with(kind, value, url).validate(), [])

    def test_a_value_or_a_url_that_is_not_in_the_form_of_its_kind_does_not(self):
        for kind, value in (
            ("DOI", "not a doi"),
            ("DOI", "11.1000/x"),
            ("ISBN", "12345"),
            ("Handle", "abc"),
            ("OpenAlex", "A5023888391"),  # an author, not a work
            ("Scopus", "85012345678"),
        ):
            with self.subTest(kind=kind, value=value):
                self.assertNotEqual(self.work_with(kind, value).validate(), [])
        # a url that is not of the kind does not conform either
        found = self.work_with("DOI", "10.1000/xyz123", "https://example.org/x")
        self.assertNotEqual(found.validate(), [])


class InSupportOfTest(ListCase):
    """The degree a thesis is in support of is a field of a thesis only: the shape
    says so (a rule: the work is a thesis, or it has no such property), and the form
    leaves it out of the other works and does not keep what it had."""

    def put_work(self, kind, supports="", path=ADD):
        params = []
        for field, value in self.fields(path, [PEOPLE[0]], "A work"):
            if field == "type":
                value = SCHEMA + kind
            if field == "inSupportOf":
                value = supports
            params.append((field, value))
        return self.web.post(path, params=params, headers=POSTING, expect_errors=True)

    def stored(self, number=1):
        work = self.work(number)
        return [
            o.value for o in work.graph.objects(work.resource, SCHEMA + "inSupportOf")
        ]

    def test_the_shape_and_the_page_say_which_fields_are_for_which_works(self):
        graph, shape = self.application["work_repo"].types["work"].shacl_shape(False)
        self.assertEqual(
            forms.fields_only_for(graph, shape),
            {SCHEMA + "inSupportOf": [SCHEMA + "Thesis"]},
        )
        page = self.web.get(ADD, headers=ADMIN).text
        found = must_search(
            r'<div class="tab-pane[^>]*data-only-for-fields="([^"]*)"', page
        )
        self.assertEqual(
            json.loads(html.unescape(found[1])),
            {"inSupportOf": [SCHEMA + "Thesis"]},
        )

    def test_only_a_thesis_keeps_what_it_is_in_support_of(self):
        self.assertEqual(self.put_work("Thesis", "Master of Science").status_int, 302)
        self.assertEqual(self.stored(), ["Master of Science"])
        self.assertEqual(self.work().validate(), [])
        # a thesis that becomes another work loses it
        edit = "/work_repo/work/1/edit"
        self.assertEqual(
            self.put_work("Report", "Master of Science", edit).status_int, 302
        )
        self.assertEqual(self.stored(), [])
        self.assertEqual(self.work().validate(), [])
        # another work does not keep it even when it is sent, but keeps the rest
        response = self.put_work("Book", "Master of Science")
        self.assertEqual(response.status_int, 302, response.text[:300])
        self.assertEqual(self.stored(2), [])
        work = self.work(2)
        self.assertEqual(
            [o.value for o in work.graph.objects(work.resource, SCHEMA + "name")],
            ["A work"],
        )

    def test_a_record_that_has_it_as_another_work_is_refused_by_the_shape_with_words(
        self,
    ):
        self.put_work("Thesis", "Master of Science")
        work = self.work()
        work.graph.replace(work.resource, forms.RDF_TYPE, ox.NamedNode(SCHEMA + "Book"))
        problems = " ".join(map(str, work.validate()))
        self.assertIn("In support of is only for a thesis.", problems)
        self.assertNotIn("are only for a thesis", problems)  # the other rule
        node = ox.BlankNode()
        work.graph.add_value(work.resource, SCHEMA + "contributor", node)
        work.graph.add_value(node, forms.RDF_TYPE, ox.NamedNode(SCHEMA + "Role"))
        work.graph.add_value(
            node,
            SCHEMA + "roleName",
            ox.NamedNode("http://example.org/vocab/role/CoSupervisor"),
        )
        work.graph.add_value(node, SCHEMA + "contributor", ox.NamedNode(PEOPLE[0]))
        problems = " ".join(map(str, work.validate()))
        self.assertIn("In support of is only for a thesis.", problems)
        self.assertIn("are only for a thesis", problems)


class FieldRuleTest(unittest.TestCase):
    """What the form reads of a rule: the alternatives of a shape for the same works."""

    @staticmethod
    def graph(text):
        return Graph.parse(PREFIXES + text, "turtle")

    def test_a_rule_is_a_shape_that_is_for_the_same_works(self):
        graph = self.graph(
            """
            ex:Work a sh:NodeShape ;
              sh:targetClass schema:CreativeWork , schema:Thesis .
            ex:Rule a sh:NodeShape ; sh:targetClass schema:Thesis , schema:Book .
            ex:Other a sh:NodeShape ; sh:targetClass schema:Person .
            ex:None a sh:NodeShape .
            """
        )
        found = forms._rule_shapes(graph, ox.NamedNode(EX + "Work"))
        self.assertEqual({s.value for s in found}, {EX + "Work", EX + "Rule"})

    def test_a_field_that_a_work_has_none_of_unless_it_is_of_the_class(self):
        graph = self.graph(
            """
            ex:Work a sh:NodeShape ; sh:targetClass schema:Thesis , schema:Book .
            ex:Rule a sh:NodeShape ; sh:targetClass schema:Thesis ;
              sh:or ( [ sh:property [ sh:path rdf:type ; sh:hasValue schema:Thesis ] ]
                      [ sh:property [ sh:path schema:inSupportOf ; sh:maxCount 0 ] ] ) .
            """
        )
        self.assertEqual(
            forms.fields_only_for(graph, ox.NamedNode(EX + "Work")),
            {SCHEMA + "inSupportOf": [SCHEMA + "Thesis"]},
        )
        # several classes and several rules
        graph = self.graph(
            """
            ex:Work a sh:NodeShape ;
              sh:targetClass schema:Thesis , schema:Report , schema:Book .
            ex:A a sh:NodeShape ; sh:targetClass schema:Thesis ;
              sh:or ( [ sh:property [ sh:path rdf:type ; sh:hasValue schema:Thesis ] ]
                      [ sh:property [ sh:path rdf:type ; sh:hasValue schema:Report ] ]
                      [ sh:property [ sh:path schema:inSupportOf ; sh:maxCount 0 ] ] ) .
            ex:B a sh:NodeShape ; sh:targetClass schema:Book ;
              sh:or ( [ sh:property [ sh:path rdf:type ; sh:hasValue schema:Book ] ]
                      [ sh:property [ sh:path schema:isbn ; sh:maxCount 0 ] ] ) .
            """
        )
        self.assertEqual(
            forms.fields_only_for(graph, ox.NamedNode(EX + "Work")),
            {
                SCHEMA + "inSupportOf": [SCHEMA + "Thesis", SCHEMA + "Report"],
                SCHEMA + "isbn": [SCHEMA + "Book"],
            },
        )
        # other alternatives are left alone
        graph = self.graph(
            """
            ex:Work a sh:NodeShape ; sh:targetClass schema:Thesis ;
              sh:or ( [ sh:property [ sh:path schema:name ; sh:minCount 1 ] ]
                      [ sh:property [ sh:path schema:url ; sh:minCount 1 ] ] ) .
            ex:Rule a sh:NodeShape ; sh:targetClass schema:Thesis ;
              sh:or ( [ sh:property [ sh:path rdf:type ; sh:hasValue schema:Thesis ] ]
                      [ sh:property [ sh:path schema:a ; sh:maxCount 0 ] ]
                      [ sh:property [ sh:path schema:b ; sh:maxCount 0 ] ] ) .
            """
        )
        # the first is no rule of this kind; the second says two fields: not understood
        self.assertEqual(forms.fields_only_for(graph, ox.NamedNode(EX + "Work")), {})
