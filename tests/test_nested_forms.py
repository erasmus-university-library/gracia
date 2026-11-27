import base64
import re
from types import SimpleNamespace

import colander
import deform
import pyoxigraph as ox
from harness import ConfiguredCase

from gracia import types
from gracia.forms import Form, change_sequence
from gracia.graph import Graph

SCHEMA = "https://schema.org/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:correct horse").decode()}
ADDRESS = [
    ("streetAddress", "Mekelweg 4"),
    ("postalCode", "2628 CD"),
    ("addressLocality", "Delft"),
    ("addressCountry", "NL"),
]
OTHER = [
    ("streetAddress", "Julianalaan 134"),
    ("addressLocality", "Delft"),
    ("addressCountry", "NL"),
]
EDIT = "/test_repo/person/ada-lovelace/edit"  # the address of the person that is made
SEQUENCE = "http://localhost/test_repo/person/ada-lovelace/sequence"


class NestedFormTest(ConfiguredCase):
    """The address of a schema.org person: a node that is there or not."""

    def setUp(self):
        super().setUp()
        self.application.create_repository(
            "test_repo",
            admin=("admin", "correct horse"),
            content_types=[types.available()["person"]],
        )
        self.web = self.client()

    def controls(self, path: str, items=(), drop=None):
        # What the form of `path` posts, with `items` (each a list of (name, value))
        # added to the sequence of addresses, as the buttons of the page do, and the
        # items that have the value `drop` taken out, as the button of an item does.
        form = self.web.get(path, headers=AUTH).forms[0]
        out, item = [], None
        for name, value in form.submit_fields("save"):
            if (name, value) == ("__start__", "item:mapping"):
                item = []
            if item is not None:
                item.append((name, value))
                if (name, value) == ("__end__", "item:mapping"):
                    if not any(v == drop for _n, v in item):
                        out += item
                    item = None
                continue
            if name == "name" and not value:
                value = "Ada Lovelace"  # a person has a name
            out.append((name, value))
            if (name, value) == ("__start__", "address:sequence"):
                for added in items:
                    out += [("__start__", "item:mapping"), *added]
                    out += [("__end__", "item:mapping")]
        return out

    def post(self, path: str, items=(), drop=None):
        return self.web.post(
            path,
            params=self.controls(path, items, drop),
            headers=AUTH,
            expect_errors=True,
        )

    def ask(self, form_path, url, action, items=(), index=None, **extra):
        # what a button of the form asks (htmx): the form as it stands, and what to do
        params = self.controls(form_path, items)
        params += [("sequence", "resource/address"), ("action", action)]
        if index is not None:
            params.append(("index", str(index)))
        params += list(extra.items())
        return self.web.post(url, params=params, headers=AUTH, expect_errors=True)

    def saved(self, **kwargs):
        response = self.post("/test_repo/person/add", **kwargs)
        self.assertEqual(response.status_int, 302, response.text[:400])

    def addresses(self) -> list[dict[str, str]]:
        record = self.application["test_repo"].types["person"]["1"]
        found = []
        for node in record.graph.objects(record.resource, SCHEMA + "address"):
            said = {
                q.predicate.value: q.object.value  # ty: ignore[missing-union-attribute]
                for q in record.graph.dataset.quads_for_subject(node)
            }
            found.append(
                {k.removeprefix(SCHEMA): v for k, v in said.items() if k != RDF_TYPE}
                | {"type": said[RDF_TYPE]}
            )
        return sorted(found, key=lambda a: a["streetAddress"])

    def test_there_is_no_address_and_a_button_to_add_one(self):
        page = self.web.get("/test_repo/person/add", headers=AUTH).text
        self.assertIn("Add address", page)
        self.assertNotIn('name="streetAddress"', page)  # none, until one is added

    def test_one_label_and_an_accordion_with_no_item_to_start_with(self):
        page = self.web.get("/test_repo/person/add", headers=AUTH).text
        self.assertEqual(len(re.findall(r">address</label>", page)), 1)
        # one widget for each property of nodes of the form: addresses and identifiers
        self.assertEqual(page.count('class="blank-nodes'), 2)
        self.assertEqual(len(re.findall(r"<details\s", page)), 0)
        self.assertIn('id="nodes-address"', page)

    def test_the_icons_of_the_shapes_are_before_the_tab_and_the_add_buttons(self):
        page = self.web.get("/test_repo/person/add", headers=AUTH).text
        self.assertRegex(
            page,
            r'(?s)data-bs-toggle="tab".*?'
            r'<i\s+class="bi bi-person me-1"\s+aria-hidden="true"></i>',
        )
        self.assertRegex(
            page,
            r'<i\s+class="bi bi-geo-alt me-1"\s+aria-hidden="true"></i>'
            r"Add address",
        )
        self.assertRegex(
            page,
            r'<i\s+class="bi bi-fingerprint me-1"\s+aria-hidden="true"></i>'
            r"Add identifier",
        )
        self.assertIn("bootstrap-icons.min.css", page)

    def test_the_add_button_asks_the_server_for_the_sequence_with_one_more(self):
        page = self.web.get("/test_repo/person/add", headers=AUTH).text
        found = re.findall(r"<button[^>]*>(?:<i[^>]*></i>)?Add address</button>", page)
        self.assertEqual(len(found), 1)
        button = found[0]
        self.assertIn('hx-post="http://localhost/test_repo/person/sequence"', button)
        self.assertIn('hx-target="#nodes-address"', button)
        self.assertIn('hx-include="closest form"', button)
        self.assertIn(
            'hx-vals="{&#34;sequence&#34;: &#34;resource/address&#34;, '
            '&#34;action&#34;: &#34;add&#34;}"',
            button,
        )
        self.assertNotIn("disabled", button)  # an address can always be added

    def test_a_native_accordion_item_for_each_address_with_a_close_button_in_its_bar(
        self,
    ):
        self.saved(items=[ADDRESS, OTHER])
        page = self.web.get(EDIT, headers=AUTH).text
        self.assertEqual(len(re.findall(r">address</label>", page)), 1)  # one label
        items = re.findall(
            r'<details\s+name="nodes-address-[0-9a-f]+"( open)?>'
            r'\s*<summary class="accordion-button collapsed">\s*'
            r'<i class="bi bi-geo-alt me-2" aria-hidden="true"></i>\s*'
            r'<span class="bar-text flex-grow-1"[^>]*>([^<]*)</span>\s*'
            r"</summary>.*?</details>\s*"
            r'<button type="button" class="bar-addon bar-remove"'
            r'[^>]*hx-post="([^"]*)"[^>]*>'
            r'<i class="bi bi-x-lg" aria-hidden="true"></i></button>\s*</div>',
            page,
            re.S,
        )
        # the bar says what the address has first, the close button is next to it, not
        # in the summary, and asks at the address of this record; both are shut when
        # the page is made
        # (which of the two is first is not up to the form: the database numbers
        # blank nodes that have the same path in any order)
        self.assertEqual([opened for opened, _text, _url in items], ["", ""])
        self.assertEqual(
            sorted(text for _opened, text, _url in items),
            ["Julianalaan 134, Delft, NL", "Mekelweg 4, 2628 CD Delft, NL"],
        )
        self.assertEqual({url for *_rest, url in items}, {SEQUENCE})

    def test_a_person_is_saved_without_an_address(self):
        self.saved()
        self.assertEqual(self.addresses(), [])

    def test_an_address_that_was_added_is_saved_with_its_class_and_shown_again(self):
        self.saved(items=[ADDRESS])
        self.assertEqual(
            self.addresses(), [dict(ADDRESS) | {"type": SCHEMA + "PostalAddress"}]
        )
        page = self.web.get(EDIT, headers=AUTH).text
        self.assertIn('value="Mekelweg 4"', page)
        self.assertIn('value="Delft"', page)

    def test_an_address_that_is_not_complete_is_sent_back_with_what_is_missing(self):
        response = self.post(
            "/test_repo/person/add", items=[[("streetAddress", "Mekelweg 4")]]
        )
        self.assertEqual(response.status_int, 200)
        self.assertIn("Required", response.text)
        store = self.application["test_repo"].store
        self.assertEqual(store.records_of_type("person"), [])  # nothing was saved

    def test_the_bar_of_an_item_with_errors_says_so_and_so_does_the_add_button(self):
        response = self.post(
            "/test_repo/person/add", items=[ADDRESS, [("streetAddress", "Julianalaan")]]
        )
        self.assertEqual(response.status_int, 200)
        page = response.text
        details = re.findall(r"<details\s+name=.*?</details>", page, re.S)
        self.assertEqual(len(details), 2)
        # the one that is wrong has the badge, and it is the one that is open
        wrong = [d for d in details if "text-bg-danger" in d]
        self.assertEqual(len(wrong), 1)
        self.assertIn("Julianalaan", wrong[0])
        self.assertRegex(wrong[0], r"<details[^>]* open[ >]")
        other = next(d for d in details if d is not wrong[0])
        self.assertNotRegex(other, r"<details[^>]* open[ >]")
        # and the add button says how many are wrong
        self.assertRegex(
            page,
            r'(?s)>Add address</button>\s*<span class="text-danger small"[^>]*>'
            r"\s*<strong>1</strong> of 2 with errors",
        )

    def test_there_is_no_hint_where_nothing_is_wrong(self):
        self.saved(items=[ADDRESS])
        page = self.web.get(EDIT, headers=AUTH).text
        self.assertNotIn("text-bg-danger", page)
        self.assertNotIn("with errors", page)

    def test_more_than_one_can_be_added_and_one_removed_and_the_last_too(self):
        self.saved(items=[ADDRESS, OTHER])
        self.assertEqual(
            [a["streetAddress"] for a in self.addresses()],
            ["Julianalaan 134", "Mekelweg 4"],
        )
        self.assertEqual(self.post(EDIT, drop="Julianalaan 134").status_int, 302)
        self.assertEqual([a["streetAddress"] for a in self.addresses()], ["Mekelweg 4"])
        self.assertEqual(self.post(EDIT, drop="Mekelweg 4").status_int, 302)
        self.assertEqual(self.addresses(), [])
        page = self.web.get(EDIT, headers=AUTH).text
        self.assertNotIn('name="streetAddress"', page)  # and the button is there
        self.assertIn("Add address", page)

    ADD, ADD_URL = "/test_repo/person/add", "/test_repo/person/sequence"
    EDIT_URL = "/test_repo/person/ada-lovelace/sequence"

    def test_the_server_answers_an_add_with_the_sequence_and_one_more_open_item(self):
        answer = self.ask(self.ADD, self.ADD_URL, "add", items=[ADDRESS])
        self.assertEqual(answer.status_int, 200)
        self.assertEqual(len(re.findall(r"<details\s", answer.text)), 2)
        self.assertIn('value="Mekelweg 4"', answer.text)  # what was typed stays
        self.assertEqual(answer.text.count('open="open"'), 1)  # one open: the new
        self.assertRegex(
            answer.text,
            r'(?s)open="open"[^>]*>\s*<summary[^>]*>\s*'
            r'<i class="bi bi-geo-alt me-2" aria-hidden="true"></i>\s*'
            r'<span class="bar-text flex-grow-1"[^>]*>address</span>',
        )
        self.assertTrue(answer.text.lstrip().startswith("<div"))
        self.assertIn('id="nodes-address"', answer.text)  # what the swap replaces

    def test_the_x_of_a_bar_is_the_next_stop_of_the_tab_key_after_the_bar(self):
        self.saved(items=[ADDRESS, OTHER])
        page = self.web.get(EDIT, headers=AUTH).text
        buttons = re.findall(r"<button[^>]*bar-remove[^>]*>", page)
        self.assertEqual(len(buttons), 2)
        self.assertFalse(
            any("tabindex" in b for b in buttons)
        )  # in the order of the page

    def test_the_item_that_was_added_is_marked_to_be_focused(self):
        answer = self.ask(self.ADD, self.ADD_URL, "add", items=[ADDRESS])
        self.assertEqual(answer.text.count('data-autofocus="'), 1)
        self.assertRegex(
            answer.text, r'(?s)open="open"\s+data-autofocus="data-autofocus"'
        )
        removed = self.ask(
            self.ADD, self.ADD_URL, "remove", items=[ADDRESS, OTHER], index=0
        )
        self.assertNotIn("data-autofocus", removed.text)

    def test_the_server_answers_a_remove_without_that_item(self):
        answer = self.ask(
            self.ADD, self.ADD_URL, "remove", items=[ADDRESS, OTHER], index=0
        )
        self.assertEqual(len(re.findall(r"<details\s", answer.text)), 1)
        self.assertIn('value="Julianalaan 134"', answer.text)
        self.assertNotIn('value="Mekelweg 4"', answer.text)

    def test_the_form_of_a_record_that_is_there_has_its_own_address_to_ask_at(self):
        self.saved(items=[ADDRESS])
        answer = self.ask(EDIT, self.EDIT_URL, "add")
        self.assertEqual(len(re.findall(r"<details\s", answer.text)), 2)  # one saved
        self.assertIn(f'hx-post="{SEQUENCE}"', answer.text)

    def test_what_comes_in_the_answer_has_ids_that_the_page_has_not(self):
        page = self.web.get(self.ADD, headers=AUTH).text
        answer = self.ask(self.ADD, self.ADD_URL, "add", items=[ADDRESS])

        def ids(html):
            return set(re.findall(r' id="([^"]+)"', html))

        self.assertEqual(ids(answer.text) & ids(page), {"nodes-address"})

    def test_no_answer_to_a_sequence_that_is_not_there_or_to_whoever_may_not_add(self):
        for sequence in ("nothing", "resource/nothing", ""):
            with self.subTest(sequence=sequence):
                params = [
                    *self.controls(self.ADD),
                    ("action", "add"),
                    ("sequence", sequence),
                ]
                bad = self.web.post(
                    self.ADD_URL, params=params, headers=AUTH, expect_errors=True
                )
                self.assertEqual(bad.status_int, 400)
        asked = [("sequence", "resource/address"), ("action", "add")]
        anonymous = self.web.post(self.ADD_URL, params=asked, expect_errors=True)
        self.assertIn(anonymous.status_int, (401, 403))


SHAPES = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix ex: <http://example.org/> .
ex:Thing a sh:NodeShape ; sh:targetClass ex:Thing ;
  sh:property [ sh:path ex:one ; sh:node ex:Part ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path ex:any ; sh:node ex:Part ] ;
  sh:property [ sh:path ex:upto2 ; sh:node ex:Part ; sh:maxCount 2 ] ;
  sh:property [ sh:path ex:one_to_3 ; sh:node ex:Part ;
                sh:minCount 1 ; sh:maxCount 3 ] .
ex:Part a sh:NodeShape ; sh:class ex:PartClass ;
  sh:property [ sh:path ex:label ; sh:maxCount 1 ; sh:datatype xsd:string ] .
"""


class CardinalityTest(ConfiguredCase):
    """What `sh:minCount` and `sh:maxCount` of a property of nodes make of its field:
    the kind of field, the buttons, what the server does and what is valid."""

    def setUp(self):
        super().setUp()
        self.application.create_repository(
            "test_repo", content_types=[types.available()["person"]]
        )
        self.config.commit()
        record = self.application["test_repo"].types["person"].blank_record()
        shapes = Graph(
            ox.Dataset(ox.parse(SHAPES.encode(), format=ox.RdfFormat.TURTLE))
        )
        request = SimpleNamespace(resource_url=lambda target, name: f"/{name}")
        nodes = record._nodes(shapes, ox.NamedNode("http://example.org/Thing"), request)
        self.nodes = {node.name: node for node in nodes}

    def fresh(self) -> Form:
        # a form for each thing that is asked: a request makes its own, and what was
        # validated or changed before is not on it
        return Form(colander.SchemaNode(colander.Mapping(), *self.nodes.values()))

    def items(self, html: str) -> int:
        return len(re.findall(r"<details\s", html))

    def controls(self, **labels):
        # what the form posts: for each property, the labels of its nodes
        out = [("__start__", "one:mapping"), ("label", "x"), ("__end__", "one:mapping")]
        for name in ("any", "upto2", "one_to_3"):
            out.append(("__start__", f"{name}:sequence"))
            for label in labels.get(name, ()):
                out += [("__start__", "item:mapping"), ("label", label)]
                out.append(("__end__", "item:mapping"))
            out.append(("__end__", f"{name}:sequence"))
        return out

    def render(self, **labels) -> dict[str, str]:
        # each of the sequences as the server draws it, with nothing to change
        return {
            name: change_sequence(self.fresh(), self.controls(**labels), name, "none")
            for name in ("any", "upto2", "one_to_3")
        }

    def adds(self, widget: str) -> bool:  # whether the Add button is there to press
        button = re.findall(r"<button[^>]*hx-vals='[^']*\"add\"[^>]*>", widget)
        self.assertEqual(len(button), 1)
        return "disabled" not in button[0]

    def removes(self, widget: str) -> list[bool]:  # for each item, its close button
        return [
            "disabled" not in button
            for button in re.findall(r"<button[^>]*bar-remove[^>]*>", widget)
        ]

    def test_exactly_one_is_a_mapping_and_the_rest_are_sequences_of_a_length(self):
        self.assertIsInstance(self.nodes["one"].typ, colander.Mapping)
        for name, lowest, highest in (
            ("any", 0, None),
            ("upto2", 0, 2),
            ("one_to_3", 1, 3),
        ):
            with self.subTest(name=name):
                node = self.nodes[name]
                self.assertIsInstance(node.typ, colander.Sequence)
                self.assertEqual(
                    (node.widget.min_len, node.widget.max_len), (lowest, highest)
                )
                self.assertEqual(
                    node.children[0].node_class, "http://example.org/PartClass"
                )

    def test_there_is_no_limit_to_add_or_remove_where_there_is_none(self):
        self.assertEqual(self.items(self.render()["any"]), 0)
        widget = self.render(any=["a", "b", "c", "d"])["any"]
        self.assertEqual(self.items(widget), 4)
        self.assertTrue(self.adds(widget))
        self.assertEqual(self.removes(widget), [True] * 4)

    def test_add_is_disabled_at_the_most_and_not_before(self):
        self.assertTrue(self.adds(self.render(upto2=["a"])["upto2"]))
        at_most = self.render(upto2=["a", "b"])["upto2"]
        self.assertFalse(self.adds(at_most))
        self.assertEqual(self.removes(at_most), [True, True])  # but they can go

    def test_close_is_disabled_at_the_least_and_not_before(self):
        # at least one: one is shown when none was posted, and it cannot go
        shown = self.render()["one_to_3"]
        self.assertEqual(self.items(shown), 1)
        self.assertEqual(self.removes(shown), [False])
        self.assertTrue(self.adds(shown))
        two = self.render(one_to_3=["a", "b"])["one_to_3"]
        self.assertEqual(self.removes(two), [True, True])
        three = self.render(one_to_3=["a", "b", "c"])["one_to_3"]
        self.assertFalse(self.adds(three))
        self.assertEqual(self.removes(three), [True] * 3)

    def change(self, path, action, index=None, **labels) -> int:
        html = change_sequence(
            self.fresh(), self.controls(**labels), path, action, index
        )
        return self.items(html)

    def test_the_server_does_not_add_beyond_the_most_or_remove_below_the_least(self):
        self.assertEqual(self.change("upto2", "add", upto2=["a"]), 2)
        self.assertEqual(self.change("upto2", "add", upto2=["a", "b"]), 2)  # no more
        self.assertEqual(
            self.change("one_to_3", "remove", 0, one_to_3=["a"]), 1
        )  # no less
        self.assertEqual(self.change("one_to_3", "remove", 0, one_to_3=["a", "b"]), 1)
        self.assertEqual(self.change("one_to_3", "add", one_to_3=["a", "b", "c"]), 3)
        self.assertEqual(self.change("any", "add", any=list("abcde")), 6)  # no limit
        self.assertEqual(self.change("any", "remove", 9, any=["a"]), 1)  # not an item

    def test_what_is_shown_to_fill_the_least_counts_when_one_is_added(self):
        # none posted, one shown for the least of one: adding makes two, not one
        self.assertEqual(self.change("one_to_3", "add"), 2)

    def invalid(self, **labels) -> str:
        with self.assertRaises(deform.ValidationFailure) as caught:
            self.fresh().validate(self.controls(**labels))
        return caught.exception.render()

    def test_what_is_posted_beyond_the_most_or_below_the_least_is_not_valid(self):
        self.assertIn(
            "Longer than maximum length 2", self.invalid(upto2=["a", "b", "c"])
        )
        self.assertIn("Shorter than minimum length 1", self.invalid())  # none of 1..3
        self.assertIn(
            "Longer than maximum length 3", self.invalid(one_to_3=list("abcd"))
        )
        valid = self.fresh().validate(self.controls(upto2=["a", "b"], one_to_3=["a"]))
        self.assertEqual(len(valid["upto2"]), 2)
        self.assertEqual(valid["any"], [])
