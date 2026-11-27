"""A text that has a language: the text, the language and the menu of languages."""

import re

import colander
import pyoxigraph as ox
from widgets_support import ADMIN, FormCase

from gracia import forms

ADD = "/test_repo/gadget/add"
EDIT = "/test_repo/gadget/1/edit"


class LanguageWidgetTest(FormCase):
    def post_note(self, text: str, language: str, path: str = ADD):
        return self.post(path, mappings={"note": [("value", text), ("lang", language)]})

    def note(self):
        record = self.record()
        return record.graph.value(record.resource, "http://example.org/note")

    def test_the_note_has_a_text_area_of_four_lines_a_language_and_a_menu_of_languages(
        self,
    ):
        page = self.page(ADD)
        group = re.search(
            r'(?s)<div class="input-group language-text[^"]*">.*?</ul>', page
        )
        assert group is not None
        text = group.group(0)
        self.assertRegex(
            text, r'(?s)<input[^>]*name="lang"[^>]*class="[^"]*language-code'
        )
        self.assertIn('placeholder="lang"', text)
        self.assertRegex(page, r"<label[^>]*>note</label>")  # and a label
        self.assertIn("dropdown-toggle-split", text)  # the caret
        for code in ("en", "fr", "de", "nl"):
            self.assertIn(f'data-language="{code}"', text)
        self.assertIn('data-language=""', text)  # no language
        self.assertIn("language.js", page)
        self.assertRegex(page, r'<textarea[^>]*name="value"[^>]*rows="4"')
        self.assertRegex(
            page,
            r'(?s)</textarea>.*?<div class="d-flex justify-content-end mt-1">\s*'
            r'<div class="input-group language-text w-auto">',
        )

    def test_the_text_is_stored_with_its_language_as_a_tagged_literal_and_shown_again(
        self,
    ):
        self.assertEqual(self.post_note("Hallo wereld", "nl").status_int, 302)
        self.assertEqual(self.note(), ox.Literal("Hallo wereld", language="nl"))
        page = self.page(EDIT)
        self.assertRegex(page, r'(?s)<input[^>]*name="lang"[^>]*value="nl"')
        self.assertRegex(
            page, r'(?s)<textarea[^>]*name="value"[^>]*>Hallo wereld</textarea>'
        )
        self.post_note("Hallo wereld", "de", EDIT)  # a change of language replaces it
        self.assertEqual(self.note(), ox.Literal("Hallo wereld", language="de"))
        self.post_note("Hello", "", EDIT)  # with no language it is a plain literal
        self.assertEqual(self.note(), ox.Literal("Hello"))
        self.post_note("", "nl", EDIT)  # no text is no note whatever the language
        self.assertIsNone(self.note())

    def test_something_that_is_not_a_language_code_is_refused_and_nothing_is_saved(
        self,
    ):
        response = self.post_note("Hello", "not a code!")
        self.assertEqual(response.status_int, 200)
        self.assertIn("Not a language code", response.text)
        self.web.get(EDIT, headers=ADMIN, status=404)


class NoBrowserValidationTest(FormCase):
    def test_the_forms_are_left_to_the_server_to_validate(self):
        self.assertRegex(self.page(ADD), r"<form[^>]*novalidate")
        plain = forms.Form(colander.Schema()).render({})
        self.assertRegex(plain, r"<form[^>]*novalidate")
