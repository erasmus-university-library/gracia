# ruff: noqa: E501  (the shapes are written out in the tests)
import base64
import re

import colander
import pyoxigraph as ox
from harness import BUNDLED, ConfiguredCase
from test_records import graph

from gracia import forms

SCHEMA = "https://schema.org/"
AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:correct horse").decode()}
ADD = "/test_repo/person/add"
SEQUENCE = "http://localhost/test_repo/person/sequence"
PREFIXES = (
    "@prefix sh: <http://www.w3.org/ns/shacl#> .\n"
    "@prefix dash: <http://datashapes.org/dash#> .\n"
    "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n"
    "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
    "@prefix s: <https://schema.org/> .\n"
    "@prefix ex: <http://example.org/> .\n"
)


class PersonBagTest(ConfiguredCase):
    def setUp(self):
        super().setUp()
        self.application.create_repository(
            "test_repo",
            admin=("admin", "correct horse"),
            content_types=[BUNDLED["person"]],
        )
        self.web = self.client()

    def controls(self, path: str, names=()):
        # what the form of `path` posts with `names` as the rows of alternateName
        form = self.web.get(path, headers=AUTH).forms[0]
        out, skipping = [], False
        for name, value in form.submit_fields("save"):
            if (name, value) == ("__start__", "alternateName:sequence"):
                skipping = True
                out.append((name, value))
                for row in names:  # a text, or a text and its language
                    text, language = (row, "") if isinstance(row, str) else row
                    out += [
                        ("__start__", "item:mapping"),
                        ("value", text),
                        ("lang", language),
                        ("__end__", "item:mapping"),
                    ]
                continue
            if skipping:
                if (name, value) == ("__end__", "alternateName:sequence"):
                    skipping = False
                    out.append((name, value))
                continue
            out.append(
                (name, "Ada Lovelace" if name == "name" and not value else value)
            )
        return out

    def post(self, names, path: str = ADD):
        return self.web.post(
            path, params=self.controls(path, names), headers=AUTH, expect_errors=True
        )

    def stored(self, with_language: bool = False) -> list:
        record = self.application["test_repo"].types["person"].find_record("1")
        found = record.graph.objects(record.resource, SCHEMA + "alternateName")
        if with_language:
            return sorted((o.value, o.language or "") for o in found)
        return sorted(o.value for o in found)

    def test_the_form_has_a_bag_with_one_empty_row_that_has_a_plus(self):
        page = self.web.get(ADD, headers=AUTH).text
        bag = page[page.index('id="bag-alternateName"') :]
        bag = bag[: bag.index('name="__end__" value="alternateName:sequence"')]
        self.assertEqual(bag.count('class="bag-item '), 1)
        self.assertRegex(bag, r'(?s)<input[^>]*name="value"[^>]*value=""')
        self.assertRegex(
            bag,
            r'(?s)<button[^>]*bag-add[^>]*hx-vals="\{&#34;sequence&#34;: '
            r'&#34;resource/alternateName&#34;, &#34;action&#34;: &#34;add&#34;\}"',
        )
        self.assertNotIn("bag-remove", bag)  # the only row has no x
        self.assertNotIn("Add alternate name</button>", page)  # no button under it

    def test_the_values_are_saved_as_they_are_and_shown_again(self):
        response = self.post(["Ada", "A. Lovelace", "Countess"])
        self.assertEqual(response.status_int, 302, response.text[:400])
        self.assertEqual(self.stored(), ["A. Lovelace", "Ada", "Countess"])
        page = self.web.get("/test_repo/person/ada-lovelace/edit", headers=AUTH).text
        self.assertEqual(
            re.findall(r'<input[^>]*name="value"[^>]*value="([^"]*)"', page),
            ["A. Lovelace", "Ada", "Countess"],  # in an order of their own
        )

    def test_each_name_can_have_a_language(self):
        response = self.post(
            [("Augusta Ada King", "en"), ("Gravin Lovelace", "nl"), "Ada"]
        )
        self.assertEqual(response.status_int, 302, response.text[:400])
        self.assertEqual(
            self.stored(True),
            [("Ada", ""), ("Augusta Ada King", "en"), ("Gravin Lovelace", "nl")],
        )
        page = self.web.get("/test_repo/person/ada-lovelace/edit", headers=AUTH).text
        bag = page[page.index('id="bag-alternateName"') :]
        bag = bag[: bag.index('name="__end__" value="alternateName:sequence"')]
        self.assertEqual(
            re.findall(r'<input[^>]*name="lang"[^>]*value="([^"]*)"', bag),
            ["", "en", "nl"],  # by language, then text
        )
        self.assertIn('data-language="fr"', page)  # the menu is there

    def test_the_same_name_in_two_languages_is_two_values_and_the_same_one_is_not(self):
        self.post([("Ada", "en"), ("Ada", "nl"), ("Ada", "en")])
        self.assertEqual(self.stored(True), [("Ada", "en"), ("Ada", "nl")])

    def test_a_language_that_is_not_a_code_is_refused(self):
        response = self.post([("Ada", "not a code")])
        self.assertEqual(response.status_int, 200)
        self.assertIn("Not a language code", response.text)

    def test_empty_rows_and_the_same_value_again_are_dropped(self):
        self.post(["Ada", "", "  ", "Ada", "Lady"])
        self.assertEqual(self.stored(), ["Ada", "Lady"])

    def test_the_values_that_are_there_are_replaced_by_the_rows(self):
        self.post(["Ada", "Lady"])
        self.post(["Lady", "Countess"], "/test_repo/person/ada-lovelace/edit")
        self.assertEqual(self.stored(), ["Countess", "Lady"])
        self.post([], "/test_repo/person/ada-lovelace/edit")
        self.assertEqual(self.stored(), [])

    def test_the_server_adds_and_removes_a_row(self):
        for action, index, expected in (("add", None, 3), ("remove", 0, 1)):
            params = self.controls(ADD, ["x", "y"])
            params += [("sequence", "resource/alternateName"), ("action", action)]
            if index is not None:
                params.append(("index", str(index)))
            answer = self.web.post(SEQUENCE, params=params, headers=AUTH)
            self.assertEqual(answer.text.count('class="bag-item '), expected, action)
            self.assertIn('id="bag-alternateName"', answer.text)
        self.assertIn('value="y"', answer.text)
        self.assertNotIn('value="x"', answer.text)

    def test_the_row_that_was_added_is_marked_to_be_focused_and_no_other_is(self):
        def answer(action, index=None):
            params = self.controls(ADD, ["x", "y"])
            params += [("sequence", "resource/alternateName"), ("action", action)]
            if index is not None:
                params.append(("index", str(index)))
            return self.web.post(SEQUENCE, params=params, headers=AUTH).text

        added = answer("add")
        self.assertEqual(added.count('data-autofocus="'), 1)
        last = added[added.rindex('class="bag-item ') :]
        self.assertIn("data-autofocus", last[:200])  # the third of the three rows
        self.assertEqual(answer("remove", 0).count('data-autofocus="'), 0)
        self.assertNotIn("data-autofocus", self.web.get(ADD, headers=AUTH).text)

    def test_the_save_button_has_an_icon_and_the_page_has_what_makes_it_busy(self):
        page = self.web.get(ADD, headers=AUTH).text
        self.assertRegex(
            page,
            r'(?s)<button[^>]*name="save"[^>]*>\s*<i\s+class="bi bi-floppy me-1"\s+aria-hidden="true"></i>Save',
        )
        self.assertIn("form.js", page)

    def test_a_type_that_has_one_class_has_no_choice_for_the_tab_to_follow(self):
        page = self.web.get(ADD, headers=AUTH).text
        self.assertNotIn("data-follow-tab", page)
        self.assertIn('class="tab-title">Person</span>', page)

    def test_the_caret_of_the_language_menu_is_not_a_stop_of_the_tab_key(self):
        page = self.web.get(ADD, headers=AUTH).text
        self.assertRegex(page, r'<button[^>]*dropdown-toggle-split[^>]*tabindex="-1"')


class BagNodeTest(ConfiguredCase):
    def node(self, body: str, url: str | None = "http://x/sequence"):
        data = graph(PREFIXES + "ex:S a sh:NodeShape ; sh:property " + body)
        (prop,) = data.objects(
            ox.NamedNode("http://example.org/S"), SCHEMA_SH + "property"
        )
        return forms.bag_node(data, prop, SCHEMA + "alt", url), data, prop

    def render(self, node, values) -> str:
        form = forms.Form(colander.SchemaNode(colander.Mapping(), node))
        return form.render({"alt": values})

    def test_a_property_with_a_datatype_and_no_max_count_is_a_bag(self):
        data = graph(
            PREFIXES
            + "ex:S a sh:NodeShape ; sh:property [ sh:path s:alt ; sh:datatype xsd:string ] , "
            "[ sh:path s:one ; sh:datatype xsd:string ; sh:maxCount 1 ] , "
            "[ sh:path s:node ; sh:node ex:S ] , [ sh:path s:link ; sh:nodeKind sh:IRI ] , "
            "[ sh:path s:uris ; sh:nodeKind sh:IRI ; dash:editor dash:URIEditor ] , "
            "[ sh:path s:plain ] ."
        )
        found = {
            path: forms.is_bag(data, prop)
            for path, prop in forms.properties(
                data, ox.NamedNode("http://example.org/S")
            )
        }
        self.assertEqual(
            {p.removeprefix(SCHEMA): v for p, v in found.items()},
            {
                "alt": True,
                "one": False,
                "node": False,
                "link": False,
                "uris": True,
                "plain": False,
            },
        )

    def test_the_rows_are_shown_by_the_widget_of_the_item(self):
        node, *_ = self.node("[ sh:path s:alt ; sh:datatype xsd:string ] .")
        page = self.render(node, ["a", "b"])
        self.assertEqual(
            re.findall(r'<input[^>]*name="item"[^>]*value="([^"]*)"', page), ["a", "b"]
        )
        self.assertEqual(page.count('class="bag-item '), 2)

    def test_with_a_language_each_row_has_a_text_and_a_language(self):
        node, *_ = self.node(
            "[ sh:path s:alt ; sh:or ( [ sh:datatype xsd:string ] [ sh:datatype rdf:langString ] ) ] ."
        )
        page = self.render(
            node, [{"value": "Hallo", "lang": "nl"}, {"value": "Hi", "lang": ""}]
        )
        self.assertEqual(
            re.findall(r'name="lang"[^>]*value="([^"]*)"', page), ["nl", ""]
        )
        self.assertEqual(page.count('name="lang"'), 2)
        self.assertIn('data-language="en"', page)

    def test_the_last_row_has_the_plus_and_the_others_the_x(self):
        node, *_ = self.node("[ sh:path s:alt ; sh:datatype xsd:string ] .")
        one = self.render(node, [])  # none: one empty row to start with
        self.assertEqual(one.count('class="bag-item '), 1)
        self.assertEqual((one.count("bag-add"), one.count("bag-remove")), (1, 0))
        three = self.render(node, ["a", "b", "c"])
        self.assertEqual((three.count("bag-add"), three.count("bag-remove")), (1, 2))
        self.assertLess(three.index("bag-remove"), three.index("bag-add"))  # at the end

    def test_the_buttons_are_disabled_where_the_cardinality_says_no(self):
        node, *_ = self.node(
            "[ sh:path s:alt ; sh:datatype xsd:string ; sh:minCount 2 ; sh:maxCount 2 ] ."
        )
        full = self.render(node, ["a", "b"])  # no more, and none may go
        self.assertRegex(full, r"<button[^>]*bag-add[^>]*disabled")
        self.assertRegex(full, r"<button[^>]*bag-remove[^>]*disabled")
        node, *_ = self.node(
            "[ sh:path s:alt ; sh:datatype xsd:string ; sh:maxCount 3 ] ."
        )
        some = self.render(node, ["a", "b"])  # one more, and one may go
        self.assertNotRegex(some, r"<button[^>]*bag-add[^>]*disabled")
        self.assertNotRegex(some, r"<button[^>]*bag-remove[^>]*disabled")

    def test_rows_with_nothing_do_not_count_for_the_cardinality(self):
        node, *_ = self.node(
            "[ sh:path s:alt ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 2 ] ."
        )
        with self.assertRaises(colander.Invalid) as caught:
            node.deserialize([""])
        self.assertIn("At least one", str(caught.exception.asdict()))
        with self.assertRaises(colander.Invalid):
            node.deserialize(["a", "b", "c"])
        self.assertEqual(node.deserialize(["a", ""]), ["a", ""])


SCHEMA_SH = "http://www.w3.org/ns/shacl#"
