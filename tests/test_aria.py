"""What a screen reader gets of the forms: names, groups, errors, tabs and icons."""

import re

from bs4 import BeautifulSoup, Tag
from widgets_support import FormCase

ADD = "/test_repo/gadget/add"


def one(page: BeautifulSoup | Tag, selector: str) -> Tag:
    found = page.select_one(selector)
    assert found is not None, selector
    return found


def attr(tag: Tag, name: str) -> str:
    return str(tag.get(name))


class AriaTest(FormCase):
    def soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    def with_errors(self) -> str:
        # a place that is not complete, and an alias with a language that is not one
        response = self.post(
            ADD,
            rows={
                "place": [
                    [("street", "x"), ("code", ""), ("city", ""), ("country", "")]
                ],
                "alias": [[("value", "Ada"), ("lang", "not a code")]],
            },
        )
        return response.text

    def test_every_control_has_a_name_and_the_tabs_and_their_panels_name_each_other(
        self,
    ):
        page = self.soup(self.page(ADD))
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
        self.assertEqual(page.select("[aria-invalid]"), [])  # none is wrong yet
        tabs = page.select("[data-bs-toggle=tab]")
        self.assertEqual(len(tabs), 2)
        for tab in tabs:
            panel = one(page, "#" + attr(tab, "aria-controls"))
            self.assertEqual(panel.get("role"), "tabpanel")
            self.assertEqual(panel.get("aria-labelledby"), tab.get("id"))

    def test_the_rows_and_the_nodes_of_a_list_are_groups_named_by_the_list_and_their_place(
        self,
    ):
        html = self.page(ADD)
        page = self.soup(html)
        bag = one(page, "#bag-alias")
        self.assertEqual(attr(bag, "role"), "group")
        self.assertEqual(attr(bag, "aria-labelledby"), "req-bag-alias")
        self.assertIsNotNone(page.find(id="req-bag-alias"))  # the label is there
        row = one(bag, ".bag-item")
        self.assertEqual(attr(one(row, "input[name=value]"), "aria-label"), "alias 1")
        self.assertEqual(
            attr(one(row, "input[name=lang]"), "aria-label"), "alias 1, language"
        )
        self.assertIn('aria-label="Add alias"', html)  # the buttons say what they add
        group = one(page, "#nodes-place")
        self.assertEqual(attr(group, "role"), "group")
        self.assertEqual(attr(group, "aria-labelledby"), "req-nodes-place")

    def test_a_field_that_is_wrong_is_invalid_and_points_at_its_message(self):
        page = self.soup(self.with_errors())
        invalid = page.select('[aria-invalid="true"]')
        self.assertGreaterEqual(len(invalid), 2)
        for e in invalid:
            message = page.select_one("#" + attr(e, "aria-describedby"))
            assert message is not None, e.get("name")  # the message is there
            self.assertTrue(message.get_text(strip=True))
        lang = one(
            page, "#bag-alias input[name=lang]"
        )  # the one of the language widget
        self.assertEqual(attr(lang, "aria-invalid"), "true")
        message = one(page, "#" + attr(lang, "aria-describedby"))
        self.assertIn("Not a language code", message.get_text())

    def test_the_icons_are_hidden_from_a_screen_reader(self):
        html = self.with_errors()
        icons = re.findall(r"<i\s[^>]*class=\"bi [^>]*>", html)
        self.assertGreater(len(icons), 5)
        self.assertTrue(all('aria-hidden="true"' in i for i in icons), icons)

    def test_the_x_of_a_node_is_outside_the_summary_and_says_which_node(self):
        node = one(self.soup(self.with_errors()), ".blank-node")
        self.assertIsNone(one(node, "summary").find("button"))  # not in the summary
        remove = one(node, "button.bar-remove")
        self.assertEqual(attr(remove, "aria-label"), "Remove place 1")
        self.assertIsNone(remove.find_parent("details"))
