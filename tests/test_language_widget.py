import base64
import re

import colander
import pyoxigraph as ox
from harness import ConfiguredCase

from gracia import forms, types

SCHEMA = "https://schema.org/"
AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:correct horse").decode()}
ADD = "/test_repo/person/add"


class LanguageWidgetTest(ConfiguredCase):
    def setUp(self):
        super().setUp()
        self.application.create_repository(
            "test_repo",
            admin=("admin", "correct horse"),
            content_types=[types.available()["person"]],
        )
        self.web = self.client()

    def post(self, text: str, language: str, path: str = ADD):
        # what the form posts, with the description and its language filled in
        form = self.web.get(path, headers=AUTH).forms[0]
        out, inside = [], False
        for name, value in form.submit_fields("save"):
            if (name, value) == ("__start__", "description:mapping"):
                inside = True
            elif (name, value) == ("__end__", "description:mapping"):
                inside = False
            elif inside and name == "value":
                value = text
            elif inside and name == "lang":
                value = language
            elif name == "name" and not value:
                value = "Ada Lovelace"
            out.append((name, value))
        return self.web.post(path, params=out, headers=AUTH, expect_errors=True)

    def description(self):
        record = self.application["test_repo"].types["person"]["1"]
        return record.graph.value(record.resource, SCHEMA + "description")

    def test_the_description_has_a_text_a_language_and_a_menu_of_languages(self):
        page = self.web.get(ADD, headers=AUTH).text
        group = re.search(
            r'(?s)<div class="input-group language-text[^"]*">.*?</ul>', page
        )
        assert group is not None
        text = group.group(0)
        self.assertRegex(page, r'name="value"')
        self.assertRegex(
            text, r'(?s)<input[^>]*name="lang"[^>]*class="[^"]*language-code'
        )
        self.assertIn('placeholder="lang"', text)
        self.assertRegex(page, r"<label[^>]*>description</label>")  # and a label
        self.assertIn("dropdown-toggle-split", text)  # the caret
        for code in ("en", "fr", "de", "nl"):
            self.assertIn(f'data-language="{code}"', text)
        self.assertIn('data-language=""', text)  # no language
        self.assertIn("language.js", page)

    def test_the_description_is_a_text_area_of_four_lines_with_the_language_below(self):
        page = self.web.get(ADD, headers=AUTH).text
        self.assertRegex(page, r'<textarea[^>]*name="value"[^>]*rows="4"')
        self.assertRegex(
            page,
            r'(?s)</textarea>.*?<div class="d-flex justify-content-end mt-1">\s*'
            r'<div class="input-group language-text w-auto">',
        )

    def test_a_language_is_stored_with_the_text_as_a_tagged_literal(self):
        self.assertEqual(self.post("Hallo wereld", "nl").status_int, 302)
        self.assertEqual(self.description(), ox.Literal("Hallo wereld", language="nl"))

    def test_with_no_language_the_text_is_a_plain_literal(self):
        self.assertEqual(self.post("Hello", "").status_int, 302)
        self.assertEqual(self.description(), ox.Literal("Hello"))

    def test_no_text_is_no_description_whatever_the_language(self):
        self.assertEqual(self.post("", "nl").status_int, 302)
        self.assertIsNone(self.description())

    def test_what_is_stored_is_in_the_form_again(self):
        self.post("Bonjour", "fr")
        page = self.web.get("/test_repo/person/ada-lovelace/edit", headers=AUTH).text
        self.assertRegex(page, r'(?s)<input[^>]*name="lang"[^>]*value="fr"')
        self.assertRegex(
            page, r'(?s)<textarea[^>]*name="value"[^>]*>Bonjour</textarea>'
        )

    def test_a_change_of_language_replaces_the_literal(self):
        self.post("Hello", "en")
        self.post("Hello", "de", "/test_repo/person/ada-lovelace/edit")
        self.assertEqual(self.description(), ox.Literal("Hello", language="de"))

    def test_something_that_is_not_a_language_code_is_refused(self):
        response = self.post("Hello", "not a code!")
        self.assertEqual(response.status_int, 200)
        self.assertIn("Not a language code", response.text)
        self.web.get("/test_repo/person/ada-lovelace/edit", headers=AUTH, status=404)


class NoBrowserValidationTest(ConfiguredCase):
    def test_the_forms_are_left_to_the_server_to_validate(self):
        self.application.create_repository(
            "test_repo",
            admin=("admin", "correct horse"),
            content_types=[types.available()["person"]],
        )
        page = self.client().get(ADD, headers=AUTH).text
        self.assertRegex(page, r"<form[^>]*novalidate")
        plain = forms.Form(colander.Schema()).render({})
        self.assertRegex(plain, r"<form[^>]*novalidate")
