"""The tabs of a form: the mapping of the resource, and the loose fields of the record."""

import re
import unittest

import colander
import deform

from gracia.forms import Form


class Person(colander.Schema):
    name = colander.SchemaNode(colander.String(), title="Name")


class Record(colander.Schema):
    subject = colander.SchemaNode(
        colander.String(),
        title="Subject",
        missing="",
        validator=colander.Regex(r"^$|^http"),
    )
    resource = Person(title="Person")


def pane(html: str, index: int) -> str:
    return re.findall(
        rf'<div class="tab-pane[^"]*" id="[^"]+-tab-{index}".*?(?=<div '
        r'class="tab-pane|<div '
        r'class="mb-3 deform-form-buttons)',
        html,
        re.S,
    )[0]


class TabsTest(unittest.TestCase):
    def form(self) -> Form:
        return Form(Record(), tabs="Record", buttons=("save",))

    def test_the_mapping_is_the_first_tab_and_the_loose_fields_are_the_record_tab(self):
        html = self.form().render({})
        self.assertEqual(
            re.findall(r'class="tab-title">([^<]+)', html), ["Person", "Record"]
        )
        self.assertIn('name="name"', pane(html, 0))
        self.assertNotIn('name="subject"', pane(html, 0))
        self.assertIn('name="subject"', pane(html, 1))
        self.assertIn("tab-pane active", pane(html, 0))
        data = self.form().validate(
            [
                ("__start__", "resource:mapping"),
                ("name", "Ada"),
                ("__end__", "resource:mapping"),
                ("subject", "http://x"),
            ]
        )  # a valid post comes back in the shape of the schema
        self.assertEqual(data, {"subject": "http://x", "resource": {"name": "Ada"}})

    def test_without_tabs_it_is_the_plain_form_that_lays_out_horizontally(self):
        html = Form(Record(), buttons=("save",)).render({})
        self.assertNotIn("nav-tabs", html)
        self.assertIn("form-horizontal", html)
        plain = Form(colander.SchemaNode(colander.Mapping())).render({})
        self.assertIn('autocomplete="off"', plain)  # no back-filling by the browser

    def test_a_tab_with_an_error_is_the_open_one_and_is_marked(self):
        form = self.form()
        with self.assertRaises(deform.ValidationFailure) as caught:
            form.validate(
                [
                    ("__start__", "resource:mapping"),
                    ("name", "x"),
                    ("__end__", "resource:mapping"),
                    ("subject", "bad"),
                ]
            )
        html = caught.exception.render()
        self.assertIn("tab-pane active", pane(html, 1))
        self.assertIn("This tab has an error", html)
        self.assertEqual(html.count("This tab has an error"), 1)
