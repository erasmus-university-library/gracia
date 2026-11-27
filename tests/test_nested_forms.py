"""A property of nodes (a place, a mark): the accordion of its items, the buttons that add
and remove them, what the server answers to them, and what the cardinality of the shape allows."""

import re
from types import SimpleNamespace

import colander
import deform
import pyoxigraph as ox
from widgets_support import ADMIN, EX, FormCase, sequence_params

from gracia.forms import Form, change_sequence
from gracia.graph import Graph

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
PLACE = [
    ("street", "Mekelweg 4"),
    ("code", "2628 CD"),
    ("city", "Delft"),
    ("country", "NL"),
]
OTHER = [
    ("street", "Julianalaan 134"),
    ("city", "Delft"),
    ("country", "NL"),
]
ADD, ADD_URL = "/test_repo/gadget/add", "/test_repo/gadget/sequence"
EDIT = "/test_repo/gadget/1/edit"
EDIT_URL = "http://localhost/test_repo/gadget/a-gadget/sequence"  # its slug


class NestedFormTest(FormCase):
    """The place of a gadget: a node that is there or not."""

    def ask(self, form_path, url, action, items=(), index=None):
        # what a button of the form asks (htmx): the form as it stands, and what to do
        params = sequence_params(
            self.posted(form_path, rows={"place": items}), "place", action, index
        )
        return self.web.post(url, params=params, headers=ADMIN, expect_errors=True)

    def places(self) -> list[dict[str, str]]:
        record = self.record()
        found = []
        for node in record.graph.objects(record.resource, EX + "place"):
            said = {
                q.predicate.value: q.object.value  # ty: ignore[missing-union-attribute]
                for q in record.graph.dataset.quads_for_subject(node)
            }
            found.append(
                {k.removeprefix(EX): v for k, v in said.items() if k != RDF_TYPE}
                | {"type": said[RDF_TYPE]}
            )
        return sorted(found, key=lambda a: a["street"])

    def test_there_is_no_place_and_a_button_to_add_one_with_the_icons_of_the_shapes(
        self,
    ):
        page = self.page(ADD)
        self.assertIn("Add place", page)
        self.assertNotIn('name="street"', page)  # none, until one is added
        self.assertEqual(len(re.findall(r">place</label>", page)), 1)  # one label
        # one widget for each property of nodes of the form: places and marks
        self.assertEqual(page.count('class="blank-nodes'), 2)
        self.assertEqual(len(re.findall(r"<details\s", page)), 0)
        self.assertIn('id="nodes-place"', page)
        self.assertRegex(
            page,
            r'(?s)data-bs-toggle="tab".*?'
            r'<i\s+class="bi bi-box me-1"\s+aria-hidden="true"></i>',
        )
        self.assertRegex(
            page,
            r'<i\s+class="bi bi-geo-alt me-1"\s+aria-hidden="true"></i>Add place',
        )
        self.assertRegex(
            page,
            r'<i\s+class="bi bi-fingerprint me-1"\s+aria-hidden="true"></i>Add mark',
        )
        self.assertIn("bootstrap-icons.min.css", page)
        found = re.findall(r"<button[^>]*>(?:<i[^>]*></i>)?Add place</button>", page)
        self.assertEqual(len(found), 1)  # the button asks the server for one more
        button = found[0]
        self.assertIn('hx-post="http://localhost/test_repo/gadget/sequence"', button)
        self.assertIn('hx-target="#nodes-place"', button)
        self.assertIn('hx-include="closest form"', button)
        self.assertIn(
            'hx-vals="{&#34;sequence&#34;: &#34;resource/place&#34;, '
            '&#34;action&#34;: &#34;add&#34;}"',
            button,
        )
        self.assertNotIn("disabled", button)  # a place can always be added

    def test_places_that_are_saved_are_shown_again_in_a_bar_each(
        self,
    ):
        self.saved(ADD, rows={"place": [PLACE, OTHER]})
        self.assertEqual(
            self.places(),  # with their class
            [
                dict(OTHER) | {"type": EX + "Place"},
                dict(PLACE) | {"type": EX + "Place"},
            ],
        )
        page = self.page(EDIT)
        self.assertEqual(len(re.findall(r">place</label>", page)), 1)  # one label
        items = re.findall(
            r'<details\s+name="nodes-place-[0-9a-f]+"( open)?>'
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
        # the bar says what the place has first, the close button is next to it, not in
        # the summary, and asks at the address of this record; both are shut when the
        # page is made (which of the two is first is not up to the form: the database
        # numbers blank nodes that have the same path in any order)
        self.assertEqual([opened for opened, _text, _url in items], ["", ""])
        self.assertEqual(
            sorted(text for _opened, text, _url in items),
            ["Julianalaan 134, Delft, NL", "Mekelweg 4, 2628 CD Delft, NL"],
        )
        self.assertEqual({url for *_rest, url in items}, {EDIT_URL})
        buttons = re.findall(r"<button[^>]*bar-remove[^>]*>", page)
        self.assertEqual(len(buttons), 2)
        self.assertFalse(any("tabindex" in b for b in buttons))  # the order of the page
        self.assertIn('value="Mekelweg 4"', page)
        self.assertNotIn("text-bg-danger", page)  # no hint where nothing is wrong
        self.assertNotIn("with errors", page)

    def test_a_place_that_is_not_complete_is_sent_back_with_what_is_missing(self):
        response = self.post(ADD, rows={"place": [[("street", "Mekelweg 4")]]})
        self.assertEqual(response.status_int, 200)
        self.assertIn("Required", response.text)
        store = self.application["test_repo"].store
        self.assertEqual(store.records_of_type("gadget"), [])  # nothing was saved

    def test_the_bar_of_an_item_with_errors_says_so_and_so_does_the_add_button(self):
        response = self.post(ADD, rows={"place": [PLACE, [("street", "Julianalaan")]]})
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
            r'(?s)>Add place</button>\s*<span class="text-danger small"[^>]*>'
            r"\s*<strong>1</strong> of 2 with errors",
        )

    def test_more_than_one_can_be_added_and_one_removed_and_the_last_too(self):
        self.saved(ADD, rows={"place": [PLACE, OTHER]})
        self.assertEqual(
            [a["street"] for a in self.places()], ["Julianalaan 134", "Mekelweg 4"]
        )
        self.saved(EDIT, rows={"place": [PLACE]})
        self.assertEqual([a["street"] for a in self.places()], ["Mekelweg 4"])
        self.saved(EDIT, rows={"place": []})
        self.assertEqual(self.places(), [])
        page = self.page(EDIT)
        self.assertNotIn('name="street"', page)  # and the button is there
        self.assertIn("Add place", page)

    def test_the_server_answers_an_add_with_the_sequence_and_one_more_open_item(self):
        answer = self.ask(ADD, ADD_URL, "add", items=[PLACE])
        self.assertEqual(answer.status_int, 200)
        self.assertEqual(len(re.findall(r"<details\s", answer.text)), 2)
        self.assertIn('value="Mekelweg 4"', answer.text)  # what was typed stays
        self.assertEqual(answer.text.count('open="open"'), 1)  # one open: the new
        self.assertRegex(
            answer.text,
            r'(?s)open="open"[^>]*>\s*<summary[^>]*>\s*'
            r'<i class="bi bi-geo-alt me-2" aria-hidden="true"></i>\s*'
            r'<span class="bar-text flex-grow-1"[^>]*>place</span>',
        )
        self.assertTrue(answer.text.lstrip().startswith("<div"))
        self.assertIn('id="nodes-place"', answer.text)  # what the swap replaces
        # the item that was added is marked to be focused
        self.assertEqual(answer.text.count('data-autofocus="'), 1)
        self.assertRegex(
            answer.text, r'(?s)open="open"\s+data-autofocus="data-autofocus"'
        )
        # what comes in the answer has ids that the page has not
        ids = set(re.findall(r' id="([^"]+)"', answer.text))
        page_ids = set(re.findall(r' id="([^"]+)"', self.page(ADD)))
        self.assertEqual(ids & page_ids, {"nodes-place"})

    def test_the_server_answers_a_remove_without_that_item(self):
        answer = self.ask(ADD, ADD_URL, "remove", items=[PLACE, OTHER], index=0)
        self.assertEqual(len(re.findall(r"<details\s", answer.text)), 1)
        self.assertIn('value="Julianalaan 134"', answer.text)
        self.assertNotIn('value="Mekelweg 4"', answer.text)
        self.assertNotIn("data-autofocus", answer.text)

    def test_the_form_of_a_record_that_is_there_has_its_own_address_to_ask_at(self):
        self.saved(ADD, rows={"place": [PLACE]})
        answer = self.ask(EDIT, EDIT_URL, "add", items=[PLACE])
        self.assertEqual(len(re.findall(r"<details\s", answer.text)), 2)  # one saved
        self.assertIn(f'hx-post="{EDIT_URL}"', answer.text)

    def test_no_answer_to_a_sequence_that_is_not_there_or_to_whoever_may_not_add(self):
        for sequence in ("nothing", "resource/nothing", ""):
            with self.subTest(sequence=sequence):
                params = [
                    *self.posted(ADD),
                    ("action", "add"),
                    ("sequence", sequence),
                ]
                bad = self.web.post(
                    ADD_URL, params=params, headers=ADMIN, expect_errors=True
                )
                self.assertEqual(bad.status_int, 400)
        asked = [("sequence", "resource/place"), ("action", "add")]
        anonymous = self.web.post(ADD_URL, params=asked, expect_errors=True)
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


class CardinalityTest(FormCase):
    """What `sh:minCount` and `sh:maxCount` of a property of nodes make of its field:
    the kind of field, the buttons, what the server does and what is valid."""

    def setUp(self):
        super().setUp()
        record = self.application["test_repo"].types["gadget"].blank_record()
        shapes = Graph(
            ox.Dataset(ox.parse(SHAPES.encode(), format=ox.RdfFormat.TURTLE))
        )
        request = SimpleNamespace(resource_url=lambda target, name: f"/{name}")
        nodes = record._nodes(shapes, ox.NamedNode(EX + "Thing"), request)
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

    def change(self, path, action, index=None, **labels) -> int:
        html = change_sequence(
            self.fresh(), self.controls(**labels), path, action, index
        )
        return self.items(html)

    def invalid(self, **labels) -> str:
        with self.assertRaises(deform.ValidationFailure) as caught:
            self.fresh().validate(self.controls(**labels))
        return caught.exception.render()

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

    def test_the_buttons_are_disabled_at_the_most_and_at_the_least_and_not_before(self):
        self.assertEqual(self.items(self.render()["any"]), 0)  # no limit
        widget = self.render(any=["a", "b", "c", "d"])["any"]
        self.assertEqual(self.items(widget), 4)
        self.assertTrue(self.adds(widget))
        self.assertEqual(self.removes(widget), [True] * 4)
        self.assertTrue(self.adds(self.render(upto2=["a"])["upto2"]))
        at_most = self.render(upto2=["a", "b"])["upto2"]
        self.assertFalse(self.adds(at_most))
        self.assertEqual(self.removes(at_most), [True, True])  # but they can go
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
        # none posted, one shown for the least of one: adding makes two, not one
        self.assertEqual(self.change("one_to_3", "add"), 2)

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
