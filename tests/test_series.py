"""A series is of one of four classes, and has identifiers of five kinds."""

import re

import pyoxigraph as ox
from support import uri_select
from test_dates import ADMIN, POSTING, DatesCase

from gracia import forms

SCHEMA = "https://schema.org/"
CLASSES = ["Periodical", "Newspaper", "BookSeries", "CreativeWorkSeries"]
ADD = "/test_repo/series/add"


class SeriesCase(DatesCase):
    def put(self, kind=None, name="A series", path=ADD):
        form = self.web.get(path, headers=ADMIN).forms[0]
        form["name"] = name
        if kind is not None:
            form["type"] = SCHEMA + kind
        return form.submit("save", headers=POSTING, expect_errors=True)

    def series(self, number=1):
        return self.application["test_repo"].types["series"].find_record(str(number))

    def types_of(self, number=1):
        found = self.series(number)
        return sorted(
            o.value for o in found.graph.objects(found.resource, forms.RDF_TYPE)
        )


class SeriesTypeTest(SeriesCase):
    def test_the_form_has_a_choice_of_the_four_classes_named_by_their_labels(self):
        select = uri_select(self.web.get(ADD, headers=ADMIN).text)
        self.assertEqual(
            select.items,
            [
                ("", "- choose -"),
                (SCHEMA + "Periodical", "Periodical"),
                (SCHEMA + "Newspaper", "Newspaper"),
                (SCHEMA + "BookSeries", "Book series"),
                (SCHEMA + "CreativeWorkSeries", "Series"),
            ],
        )

    def test_the_options_have_the_icons_the_shape_gives(self):
        select = uri_select(self.web.get(ADD, headers=ADMIN).text)
        icons = dict(
            re.findall(r'data-value="([^"]+)"[^>]*data-icon="([^"]*)"', select.html)
        )
        self.assertEqual(
            {c.removeprefix(SCHEMA): i for c, i in icons.items()},
            {
                "Periodical": "journals",
                "Newspaper": "newspaper",
                "BookSeries": "bookshelf",
                "CreativeWorkSeries": "collection",
            },
        )

    def test_a_new_series_starts_as_the_class_of_its_type(self):
        select = uri_select(self.web.get(ADD, headers=ADMIN).text)
        self.assertEqual(select.chosen, SCHEMA + "CreativeWorkSeries")

    def test_a_series_is_stored_with_the_one_class_that_was_chosen(self):
        for number, kind in enumerate(CLASSES, 1):
            with self.subTest(kind=kind):
                response = self.put(kind, f"Series {number}")
                self.assertEqual(response.status_int, 302, response.text[:300])
                self.assertEqual(self.types_of(number), [SCHEMA + kind])
                self.assertEqual(self.series(number).validate(), [])

    def test_another_class_replaces_the_one_there_was(self):
        self.put("Periodical")
        self.assertEqual(
            self.put("BookSeries", path="/test_repo/series/1/edit").status_int, 302
        )
        self.assertEqual(self.types_of(), [SCHEMA + "BookSeries"])

    def test_a_class_that_is_not_one_of_them_is_refused(self):
        form = self.web.get(ADD, headers=ADMIN).forms[0]
        form["name"] = "A series"
        form["type"].force_value(SCHEMA + "Book")
        response = form.submit("save", headers=POSTING, expect_errors=True)
        self.assertEqual(response.status_int, 200)
        self.assertIn("is not one of", response.text)

    def test_two_classes_do_not_conform(self):
        self.put("Periodical")
        series = self.series()
        series.graph.add_value(
            series.resource, forms.RDF_TYPE, ox.NamedNode(SCHEMA + "Newspaper")
        )
        self.assertNotEqual(series.validate(), [])

    def test_a_work_can_be_part_of_a_series_of_any_of_the_four_classes(self):
        shape = ox.NamedNode("http://example.org/shapes#CreativeWorkSeriesShape")
        found = self.application["test_repo"].shape_classes([shape])
        self.assertEqual(found, sorted(SCHEMA + c for c in CLASSES))


class SeriesIdentifierTest(SeriesCase):
    made = 0

    def with_identifier(self, kind, value, url=None):
        """A series of its own that has the identifier."""
        self.made += 1
        self.assertEqual(self.put(None, f"Series {self.made}").status_int, 302)
        series = self.series(self.made)
        self.assertEqual(series.validate(), [])
        node = ox.BlankNode()
        graph = series.graph
        graph.add_value(series.resource, SCHEMA + "identifier", node)
        graph.add_value(node, forms.RDF_TYPE, ox.NamedNode(SCHEMA + "PropertyValue"))
        graph.add_value(node, SCHEMA + "propertyID", ox.Literal(kind))
        graph.add_value(node, SCHEMA + "value", ox.Literal(value))
        if url:
            graph.add_value(node, SCHEMA + "url", ox.NamedNode(url))
        return series

    def test_the_kinds_are_issn_isbn_openalex_local_and_external(self):
        graph, _shape = self.application["test_repo"].types["series"].shacl_shape(False)
        shape = ox.NamedNode("http://example.org/shapes#PropertyValueShape")
        (kind,) = [
            p
            for path, p in forms.properties(graph, shape)
            if path == SCHEMA + "propertyID"
        ]
        (head,) = graph.objects(kind, ox.NamedNode("http://www.w3.org/ns/shacl#in"))
        self.assertEqual(
            [i.value for i in forms._list_items(graph, head)],
            ["ISSN", "ISBN", "OpenAlex", "Local", "External"],
        )

    def test_a_value_in_the_form_of_its_kind_conforms(self):
        for kind, value, url in (
            ("ISSN", "0302-9743", "urn:issn:0302-9743"),
            ("ISSN", "1234-567X", "urn:issn:1234-567X"),
            ("ISBN", "978-3-16-148410-0", "urn:isbn:978-3-16-148410-0"),
            ("ISBN", "0-306-40615-2", "urn:isbn:0-306-40615-2"),
            ("OpenAlex", "S4210172589", "https://openalex.org/S4210172589"),
            ("Local", "anything at all", None),
            ("External", "x-1", None),
        ):
            with self.subTest(kind=kind, value=value):
                self.assertEqual(self.with_identifier(kind, value, url).validate(), [])

    def test_a_value_that_is_not_in_the_form_of_its_kind_does_not(self):
        for kind, value in (
            ("ISSN", "12345678"),
            ("ISSN", "0302-97"),
            ("ISBN", "12345"),
            ("OpenAlex", "W2741809807"),  # a work, not a source
            ("OpenAlex", "I114027177"),  # an institution
        ):
            with self.subTest(kind=kind, value=value):
                self.assertNotEqual(self.with_identifier(kind, value).validate(), [])

    def test_a_kind_that_a_series_does_not_have_is_refused(self):
        for kind in ("ORCID", "Scopus", "DOI", "Handle"):
            with self.subTest(kind=kind):
                self.assertNotEqual(self.with_identifier(kind, "x").validate(), [])

    def test_there_is_no_separate_issn_for_it_is_a_kind_of_identifier(self):
        self.assertNotIn('name="issn"', self.web.get(ADD, headers=ADMIN).text)
        graph, shape = self.application["test_repo"].types["series"].shacl_shape(False)
        self.assertNotIn(
            SCHEMA + "issn", [p for p, _ in forms.properties(graph, shape)]
        )
