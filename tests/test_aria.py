import base64
import re

from bs4 import BeautifulSoup, Tag
from harness import ConfiguredCase

from gracia import types

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:correct horse").decode()}
ADD = "/test_repo/person/add"


def one(page: BeautifulSoup | Tag, selector: str) -> Tag:
    found = page.select_one(selector)
    assert found is not None, selector
    return found


def attr(tag: Tag, name: str) -> str:
    return str(tag.get(name))


class AriaTest(ConfiguredCase):
    def setUp(self):
        super().setUp()
        self.application.create_repository(
            "test_repo",
            admin=("admin", "correct horse"),
            content_types=[types.available()["person"]],
        )
        self.web = self.client()

    def soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    def with_errors(self) -> str:
        # an address that is not complete, and an alternate name with a language that
        # is not one
        form = self.web.get(ADD, headers=AUTH).forms[0]
        out = []
        for name, value in form.submit_fields("save"):
            out.append((name, "Ada" if name == "name" else value))
            if (name, value) == ("__start__", "address:sequence"):
                out += [
                    ("__start__", "item:mapping"),
                    ("streetAddress", "x"),
                    ("postalCode", ""),
                    ("addressLocality", ""),
                    ("addressCountry", ""),
                    ("__end__", "item:mapping"),
                ]
            if (name, value) == ("__start__", "alternateName:sequence"):
                out += [
                    ("__start__", "item:mapping"),
                    ("value", "Ada"),
                    ("lang", "not a code"),
                    ("__end__", "item:mapping"),
                ]
        return self.web.post(ADD, params=out, headers=AUTH, expect_errors=True).text

    def test_every_control_has_a_name_a_screen_reader_can_find(self):
        page = self.soup(self.web.get(ADD, headers=AUTH).text)
        unnamed = []
        for e in page.find_all(["input", "select", "textarea"]):
            if e.get("type") in ("hidden", "submit", "button"):
                continue
            named = (
                e.get("aria-label")
                or e.get("aria-labelledby")
                or page.select_one(f'label[for="{e.get("id")}"]')
            )
            if not named:
                unnamed.append(e.get("name"))
        self.assertEqual(unnamed, [])

    def test_the_rows_of_a_list_are_named_by_the_list_and_their_place(self):
        page = self.soup(self.web.get(ADD, headers=AUTH).text)
        bag = one(page, "#bag-alternateName")
        self.assertEqual(attr(bag, "role"), "group")
        self.assertEqual(attr(bag, "aria-labelledby"), "req-bag-alternateName")
        self.assertIsNotNone(
            page.find(id="req-bag-alternateName")
        )  # the label is there
        row = one(bag, ".bag-item")
        self.assertEqual(
            attr(one(row, "input[name=value]"), "aria-label"), "alternate name 1"
        )
        self.assertEqual(
            attr(one(row, "input[name=lang]"), "aria-label"),
            "alternate name 1, language",
        )

    def test_the_buttons_say_what_they_add(self):
        page = self.web.get(ADD, headers=AUTH).text
        self.assertIn('aria-label="Add alternate name"', page)

    def test_the_lists_of_nodes_are_groups_with_their_label(self):
        group = one(self.soup(self.web.get(ADD, headers=AUTH).text), "#nodes-address")
        self.assertEqual(attr(group, "role"), "group")
        self.assertEqual(attr(group, "aria-labelledby"), "req-nodes-address")

    def test_a_field_that_is_wrong_is_invalid_and_points_at_its_message(self):
        page = self.soup(self.with_errors())
        invalid = page.select('[aria-invalid="true"]')
        self.assertGreaterEqual(len(invalid), 2)
        for e in invalid:
            message = page.select_one("#" + attr(e, "aria-describedby"))
            assert message is not None, e.get("name")  # the message is there
            self.assertTrue(message.get_text(strip=True))

    def test_the_language_that_is_wrong_points_at_the_message_of_its_widget(self):
        page = self.soup(self.with_errors())
        lang = one(page, "#bag-alternateName input[name=lang]")
        self.assertEqual(attr(lang, "aria-invalid"), "true")
        message = one(page, "#" + attr(lang, "aria-describedby"))
        self.assertIn("Not a language code", message.get_text())

    def test_a_field_that_is_right_is_not_marked_invalid(self):
        page = self.soup(self.web.get(ADD, headers=AUTH).text)
        self.assertEqual(page.select("[aria-invalid]"), [])

    def test_the_tabs_and_their_panels_name_each_other(self):
        page = self.soup(self.web.get(ADD, headers=AUTH).text)
        tabs = page.select("[data-bs-toggle=tab]")
        self.assertEqual(len(tabs), 2)
        for tab in tabs:
            panel = one(page, "#" + attr(tab, "aria-controls"))
            self.assertEqual(panel.get("role"), "tabpanel")
            self.assertEqual(panel.get("aria-labelledby"), tab.get("id"))

    def test_the_icons_are_hidden_from_a_screen_reader(self):
        html = self.with_errors()
        icons = re.findall(r"<i\s[^>]*class=\"bi [^>]*>", html)
        self.assertGreater(len(icons), 5)
        self.assertTrue(all('aria-hidden="true"' in i for i in icons), icons)

    def test_the_x_of_a_node_is_outside_the_summary_and_says_which_node(self):
        node = one(self.soup(self.with_errors()), ".blank-node")
        self.assertIsNone(one(node, "summary").find("button"))  # not in the summary
        remove = one(node, "button.bar-remove")
        self.assertEqual(attr(remove, "aria-label"), "Remove address 1")
        self.assertIsNone(remove.find_parent("details"))
