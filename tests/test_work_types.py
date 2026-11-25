"""A work is of one of the classes of its shape: Thesis, ScholarlyArticle, Book..."""

import re

import pyoxigraph as ox
from support import uri_select
from test_list_widget import ADD, EDIT, PEOPLE, ListCase

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
        from test_dates import POSTING

        return POSTING

    def types_of(self, number=1):
        work = self.work(number)
        return sorted(o.value for o in work.graph.objects(work.resource, RDF_TYPE))


class WorkTypeTest(WorkTypeCase):
    def test_the_form_has_a_choice_of_the_eight_classes_named_by_their_labels(self):
        page = self.web.get(ADD, headers={"X-User": "admin"}).text
        select = uri_select(page)
        self.assertEqual(
            [v for v, _ in select.items], ["", *[SCHEMA + c for c in CLASSES]]
        )
        self.assertIn("Scholarly article", select.html)
        self.assertIn("Software source code", select.html)

    def test_a_work_is_stored_with_the_one_class_that_was_chosen(self):
        for number, kind in enumerate(CLASSES, 1):
            with self.subTest(kind=kind):
                response = self.kind(kind, name=f"Work {number}")
                self.assertEqual(response.status_int, 302, response.text[:500])
                self.assertEqual(self.types_of(number), [SCHEMA + kind])
                self.assertEqual(self.work(number).validate(), [])

    def test_another_class_replaces_the_one_there_was(self):
        self.kind("Thesis")
        response = self.kind("Book", EDIT.format(1))
        self.assertEqual(response.status_int, 302, response.text[:500])
        self.assertEqual(self.types_of(), [SCHEMA + "Book"])

    def test_a_class_that_is_not_one_of_them_is_refused(self):
        for kind in ("Person", "CreativeWork"):
            with self.subTest(kind=kind):
                response = self.kind(kind)
                self.assertEqual(response.status_int, 200)
                self.assertIn("is not one of", response.text)

    def test_no_class_is_refused_for_the_form_asks_what_the_work_is(self):
        params = [
            (f, "" if f == "type" else v) for f, v in self.fields(authors=[PEOPLE[0]])
        ]
        response = self.web.post(
            ADD, params=params, headers=self.posting(), expect_errors=True
        )
        self.assertEqual(response.status_int, 200)
        self.assertIn("Required", response.text)

    def test_two_classes_do_not_conform(self):
        self.kind("Thesis")
        work = self.work()
        work.graph.add_value(work.resource, RDF_TYPE, ox.NamedNode(SCHEMA + "Book"))
        self.assertNotEqual(work.validate(), [])

    def test_the_tab_can_be_named_and_iconed_by_the_class(self):
        page = self.web.get(ADD, headers={"X-User": "admin"}).text
        data = re.search(r'data-follow-tab="([^"]*)"', page)
        assert data is not None
        self.assertIn("mortarboard", data[0])
        self.assertIn("journal-text", data[0])
        self.assertEqual(
            forms.uri_label(SCHEMA + "SoftwareSourceCode"), "Software source code"
        )


class WorkFieldsTest(WorkTypeCase):
    def test_the_abstract_is_a_text_area_and_there_is_no_description(self):
        page = self.web.get(ADD, headers={"X-User": "admin"}).text
        self.assertRegex(
            page, r'(?s)id="bag-abstract".*?<textarea[^>]*name="value"[^>]*>'
        )
        self.assertNotRegex(page, r'name="description"')
        self.assertNotIn(">description<", page)

    def test_a_work_without_an_author_is_asked_for_one_in_plain_words(self):
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

    def test_it_is_a_dropdown_with_a_hidden_input_that_is_posted(self):
        page = self.web.get(ADD, headers={"X-User": "admin"}).text
        self.assertNotRegex(page, r'<select[^>]*name="type"')
        self.assertRegex(
            page, r'<input type="hidden" name="type"[^>]*class="uri-select-value"'
        )
        self.assertRegex(page, r'<button[^>]*class="form-select uri-select-toggle')
        self.assertRegex(page, r'data-bs-toggle="dropdown"')
        self.assertRegex(page, r"<script[^>]*uri_select\.js")

    def test_a_class_that_is_not_a_choice_is_shown_as_nothing_chosen(self):
        select = uri_select(self.web.get(ADD, headers={"X-User": "admin"}).text)
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
