# ruff: noqa: E501  (the shapes are written out in the tests)
"""A property that has many values, as a bag of rows: a text, or a text and its language."""

import colander
import pyoxigraph as ox
from harness import ConfiguredCase
from test_records import graph
from widgets_support import (
    ADMIN,
    EX,
    FormCase,
    inputs,
    sequence_params,
)

from gracia import forms

SH = "http://www.w3.org/ns/shacl#"
ADD = "/test_repo/gadget/add"
EDIT = "/test_repo/gadget/1/edit"
SEQUENCE = "/test_repo/gadget/sequence"
PREFIXES = (
    "@prefix sh: <http://www.w3.org/ns/shacl#> .\n"
    "@prefix dash: <http://datashapes.org/dash#> .\n"
    "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n"
    "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
    "@prefix ex: <http://example.org/> .\n"
)


class AliasBagTest(FormCase):
    def post_aliases(self, names, path: str = ADD):
        rows = []
        for row in names:  # a text, or a text and its language
            text, language = (row, "") if isinstance(row, str) else row
            rows.append([("value", text), ("lang", language)])
        return self.post(path, rows={"alias": rows})

    def stored(self, with_language: bool = False) -> list:
        found = self.values("alias")
        if with_language:
            return sorted((o.value, o.language or "") for o in found)
        return sorted(o.value for o in found)

    def bag(self, page: str) -> str:
        bag = page[page.index('id="bag-alias"') :]
        return bag[: bag.index('name="__end__" value="alias:sequence"')]

    def test_the_form_has_a_bag_with_one_empty_row_that_has_a_plus(self):
        page = self.page(ADD)
        bag = self.bag(page)
        self.assertEqual(bag.count('class="bag-item '), 1)
        self.assertRegex(bag, r'(?s)<input[^>]*name="value"[^>]*value=""')
        self.assertRegex(
            bag,
            r'(?s)<button[^>]*bag-add[^>]*hx-vals="\{&#34;sequence&#34;: '
            r'&#34;resource/alias&#34;, &#34;action&#34;: &#34;add&#34;\}"',
        )
        self.assertNotIn("bag-remove", bag)  # the only row has no x
        self.assertNotIn("Add alias</button>", page)  # no button under it
        self.assertRegex(page, r'<button[^>]*dropdown-toggle-split[^>]*tabindex="-1"')

    def test_the_values_are_saved_as_they_are_and_shown_again_and_replaced_by_the_rows(
        self,
    ):
        response = self.post_aliases(["Ada", "A. Lovelace", "Countess"])
        self.assertEqual(response.status_int, 302, response.text[:400])
        self.assertEqual(self.stored(), ["A. Lovelace", "Ada", "Countess"])
        self.assertEqual(
            inputs(self.page(EDIT), "value"),
            ["A. Lovelace", "Ada", "Countess"],  # in an order of their own
        )
        self.post_aliases(["Countess", "Lady"], EDIT)
        self.assertEqual(self.stored(), ["Countess", "Lady"])
        self.post_aliases([], EDIT)
        self.assertEqual(self.stored(), [])

    def test_empty_rows_and_the_same_value_again_are_dropped(self):
        self.post_aliases(["Ada", "", "  ", "Ada", "Lady"])
        self.assertEqual(self.stored(), ["Ada", "Lady"])

    def test_each_value_can_have_a_language_and_it_is_a_code(self):
        response = self.post_aliases(
            [("Augusta Ada King", "en"), ("Gravin Lovelace", "nl"), "Ada"]
        )
        self.assertEqual(response.status_int, 302, response.text[:400])
        self.assertEqual(
            self.stored(True),
            [("Ada", ""), ("Augusta Ada King", "en"), ("Gravin Lovelace", "nl")],
        )
        page = self.page(EDIT)
        self.assertEqual(
            inputs(self.bag(page), "lang"), ["", "en", "nl"]
        )  # by language, then text
        self.assertIn('data-language="fr"', page)  # the menu is there
        # the same text in two languages is two values and the same one is not
        self.post_aliases([("Ada", "en"), ("Ada", "nl"), ("Ada", "en")], EDIT)
        self.assertEqual(self.stored(True), [("Ada", "en"), ("Ada", "nl")])
        response = self.post_aliases([("Ada", "not a code")], EDIT)
        self.assertEqual(response.status_int, 200)
        self.assertIn("Not a language code", response.text)

    def test_the_server_adds_and_removes_a_row_and_marks_the_one_that_was_added(self):
        for action, index, expected in (("add", None, 3), ("remove", 0, 1)):
            two = [[("value", "x"), ("lang", "")], [("value", "y"), ("lang", "")]]
            params = self.posted(ADD, rows={"alias": two})
            answer = self.web.post(
                SEQUENCE,
                params=sequence_params(params, "alias", action, index),
                headers=ADMIN,
            )
            self.assertEqual(answer.text.count('class="bag-item '), expected, action)
            self.assertIn('id="bag-alias"', answer.text)
            if action == "add":
                self.assertEqual(answer.text.count('data-autofocus="'), 1)
                last = answer.text[answer.text.rindex('class="bag-item ') :]
                self.assertIn("data-autofocus", last[:200])  # the third of the three
            else:
                self.assertNotIn("data-autofocus", answer.text)
        self.assertIn('value="y"', answer.text)
        self.assertNotIn('value="x"', answer.text)
        self.assertNotIn("data-autofocus", self.page(ADD))

    def test_the_save_button_has_an_icon_and_the_page_has_what_makes_it_busy(self):
        page = self.page(ADD)
        self.assertRegex(
            page,
            r'(?s)<button[^>]*name="save"[^>]*>\s*<i\s+class="bi bi-floppy me-1"\s+aria-hidden="true"></i>Save',
        )
        self.assertIn("form.js", page)
        # a type that has one class has no choice for the tab to follow
        self.assertNotIn("data-follow-tab", page)
        self.assertIn('class="tab-title">Gadget</span>', page)


class BagNodeTest(ConfiguredCase):
    def node(self, body: str, url: str | None = "http://x/sequence"):
        data = graph(PREFIXES + "ex:S a sh:NodeShape ; sh:property " + body)
        (prop,) = data.objects(ox.NamedNode(EX + "S"), SH + "property")
        return forms.bag_node(data, prop, EX + "alt", url), data, prop

    def render(self, node, values) -> str:
        form = forms.Form(colander.SchemaNode(colander.Mapping(), node))
        return form.render({"alt": values})

    def test_a_property_with_a_datatype_and_no_max_count_is_a_bag(self):
        data = graph(
            PREFIXES
            + "ex:S a sh:NodeShape ; sh:property [ sh:path ex:alt ; sh:datatype xsd:string ] , "
            "[ sh:path ex:one ; sh:datatype xsd:string ; sh:maxCount 1 ] , "
            "[ sh:path ex:node ; sh:node ex:S ] , [ sh:path ex:link ; sh:nodeKind sh:IRI ] , "
            "[ sh:path ex:uris ; sh:nodeKind sh:IRI ; dash:editor dash:URIEditor ] , "
            "[ sh:path ex:plain ] ."
        )
        found = {
            path: forms.is_bag(data, prop)
            for path, prop in forms.properties(data, ox.NamedNode(EX + "S"))
        }
        self.assertEqual(
            {p.removeprefix(EX): v for p, v in found.items()},
            {
                "alt": True,
                "one": False,
                "node": False,
                "link": False,
                "uris": True,
                "plain": False,
            },
        )

    def test_the_rows_are_shown_by_the_widget_of_the_item_and_the_buttons_follow_the_row(
        self,
    ):
        node, *_ = self.node("[ sh:path ex:alt ; sh:datatype xsd:string ] .")
        page = self.render(node, ["a", "b"])
        self.assertEqual(inputs(page, "item"), ["a", "b"])
        self.assertEqual(page.count('class="bag-item '), 2)
        one = self.render(node, [])  # none: one empty row to start with
        self.assertEqual(one.count('class="bag-item '), 1)
        self.assertEqual((one.count("bag-add"), one.count("bag-remove")), (1, 0))
        three = self.render(node, ["a", "b", "c"])
        self.assertEqual((three.count("bag-add"), three.count("bag-remove")), (1, 2))
        self.assertLess(three.index("bag-remove"), three.index("bag-add"))  # at the end

    def test_with_a_language_each_row_has_a_text_and_a_language(self):
        node, *_ = self.node(
            "[ sh:path ex:alt ; sh:or ( [ sh:datatype xsd:string ] [ sh:datatype rdf:langString ] ) ] ."
        )
        page = self.render(
            node, [{"value": "Hallo", "lang": "nl"}, {"value": "Hi", "lang": ""}]
        )
        self.assertEqual(inputs(page, "lang"), ["nl", ""])
        self.assertEqual(page.count('name="lang"'), 2)
        self.assertIn('data-language="en"', page)

    def test_the_buttons_are_disabled_where_the_cardinality_says_no(self):
        node, *_ = self.node(
            "[ sh:path ex:alt ; sh:datatype xsd:string ; sh:minCount 2 ; sh:maxCount 2 ] ."
        )
        full = self.render(node, ["a", "b"])  # no more, and none may go
        self.assertRegex(full, r"<button[^>]*bag-add[^>]*disabled")
        self.assertRegex(full, r"<button[^>]*bag-remove[^>]*disabled")
        node, *_ = self.node(
            "[ sh:path ex:alt ; sh:datatype xsd:string ; sh:maxCount 3 ] ."
        )
        some = self.render(node, ["a", "b"])  # one more, and one may go
        self.assertNotRegex(some, r"<button[^>]*bag-add[^>]*disabled")
        self.assertNotRegex(some, r"<button[^>]*bag-remove[^>]*disabled")

    def test_rows_with_nothing_do_not_count_for_the_cardinality(self):
        node, *_ = self.node(
            "[ sh:path ex:alt ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 2 ] ."
        )
        with self.assertRaises(colander.Invalid) as caught:
            node.deserialize([""])
        self.assertIn("At least one", str(caught.exception.asdict()))
        with self.assertRaises(colander.Invalid):
            node.deserialize(["a", "b", "c"])
        self.assertEqual(node.deserialize(["a", ""]), ["a", ""])
