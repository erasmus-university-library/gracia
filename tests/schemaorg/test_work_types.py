"""A work is of one of the classes of its shape: Thesis, ScholarlyArticle, Book..."""

import re

import pyoxigraph as ox
from schemaorg.cases import ADD, EDIT, PEOPLE, ListCase
from support import uri_select

from gracia import forms

SCHEMA = "https://schema.org/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
CLASSES = [
    "Thesis",
    "ScholarlyArticle",
    "Article",
    "Book",
    "Report",
    "Dataset",
    "SoftwareSourceCode",
    "Chapter",
]


class WorkTypeCase(ListCase):
    def kind(self, kind, path=ADD, name="A work"):
        params = [
            (f, SCHEMA + kind if f == "type" else v)
            for f, v in self.fields(path, [PEOPLE[0]], name)
        ]
        return self.web.post(
            path, params=params, headers=self.posting(), expect_errors=True
        )

    def posting(self):
        from schemaorg.cases import POSTING

        return POSTING

    def types_of(self, number=1):
        work = self.work(number)
        return sorted(o.value for o in work.graph.objects(work.resource, RDF_TYPE))


class WorkTypeTest(WorkTypeCase):
    def test_the_form_has_a_choice_of_the_eight_classes_and_the_tab_follows_the_class(
        self,
    ):
        page = self.web.get(ADD, headers={"X-User": "admin"}).text
        select = uri_select(page)
        self.assertEqual(
            [v for v, _ in select.items], ["", *[SCHEMA + c for c in CLASSES]]
        )
        self.assertIn("Scholarly article", select.html)
        self.assertIn("Software source code", select.html)
        data = re.search(r'data-follow-tab="([^"]*)"', page)
        assert data is not None
        self.assertIn("mortarboard", data[0])
        self.assertIn("journal-text", data[0])
        self.assertEqual(
            forms.uri_label(SCHEMA + "SoftwareSourceCode"), "Software source code"
        )

    def test_a_work_is_stored_with_the_one_class_chosen_and_another_replaces_it(self):
        for number, kind in enumerate(CLASSES, 1):
            with self.subTest(kind=kind):
                response = self.kind(kind, name=f"Work {number}")
                self.assertEqual(response.status_int, 302, response.text[:500])
                self.assertEqual(self.types_of(number), [SCHEMA + kind])
                self.assertEqual(self.work(number).validate(), [])
        response = self.kind("Book", EDIT.format(1))
        self.assertEqual(response.status_int, 302, response.text[:500])
        self.assertEqual(self.types_of(), [SCHEMA + "Book"])

    def test_a_class_that_is_not_one_of_them_and_no_class_are_refused_two_do_not_conform(
        self,
    ):
        for kind in ("Person", "CreativeWork"):
            with self.subTest(kind=kind):
                response = self.kind(kind)
                self.assertEqual(response.status_int, 200)
                self.assertIn("is not one of", response.text)
        params = [
            (f, "" if f == "type" else v) for f, v in self.fields(authors=[PEOPLE[0]])
        ]
        response = self.web.post(
            ADD, params=params, headers=self.posting(), expect_errors=True
        )
        self.assertEqual(response.status_int, 200)
        self.assertIn("Required", response.text)  # the form asks what the work is
        self.kind("Thesis")
        work = self.work()
        work.graph.add_value(work.resource, RDF_TYPE, ox.NamedNode(SCHEMA + "Book"))
        self.assertNotEqual(work.validate(), [])


class WorkFieldsTest(WorkTypeCase):
    def test_the_abstract_is_a_text_area_and_a_missing_author_is_asked_for_in_plain_words(
        self,
    ):
        page = self.web.get(ADD, headers={"X-User": "admin"}).text
        self.assertRegex(
            page, r'(?s)id="bag-abstract".*?<textarea[^>]*name="value"[^>]*>'
        )
        self.assertNotRegex(page, r'name="description"')
        self.assertNotIn(">description<", page)
        params = [
            (f, SCHEMA + "Thesis" if f == "type" else v)
            for f, v in self.fields(authors=[])
        ]
        response = self.web.post(
            ADD, params=params, headers=self.posting(), expect_errors=True
        )
        self.assertEqual(response.status_int, 200)
        self.assertIn("At least one author is needed", response.text)
        self.assertNotIn("Shorter than minimum length", response.text)


class SelectTest(WorkTypeCase):
    """The choice of the class is a Bootstrap dropdown, not a select of the browser."""

    def test_it_is_a_dropdown_with_a_hidden_input_and_a_class_not_a_choice_is_nothing(
        self,
    ):
        page = self.web.get(ADD, headers={"X-User": "admin"}).text
        self.assertNotRegex(page, r'<select[^>]*name="type"')
        self.assertRegex(
            page, r'<input type="hidden" name="type"[^>]*class="uri-select-value"'
        )
        self.assertRegex(page, r'<button[^>]*class="form-select uri-select-toggle')
        self.assertRegex(page, r'data-bs-toggle="dropdown"')
        self.assertRegex(page, r"<script[^>]*uri_select\.js")
        select = uri_select(page)
        # a new work is of schema:CreativeWork, which is not one of the eight
        self.assertEqual(select.chosen, "")
        self.assertEqual(select.selected, 1)  # the first item, "- choose -"
        self.assertIn('<span class="uri-select-label">- choose -</span>', select.html)

    def test_the_class_that_is_stored_is_chosen_with_its_icon_on_the_button(self):
        self.kind("Book")
        page = self.web.get("/work_repo/work/1/edit", headers={"X-User": "admin"}).text
        select = uri_select(page)
        self.assertEqual(select.chosen, SCHEMA + "Book")
        self.assertEqual(select.selected, 1)
        self.assertRegex(
            select.html,
            r'<i class="bi bi-book me-2"[^>]*></i><span class="uri-select-label">Book',
        )
        # one item has the check mark that is not hidden: the one that is chosen
        shown = re.findall(r'uri-select-check" aria-hidden="true"\s*>', select.html)
        self.assertEqual(len(shown), 1)
