"""The property template language of the shapes (`gracia:propertyTemplate`, the headers of
nodes): what a template means, how a shape says it, and what the form and the database make of it."""

import html
import json
import re
import unittest
from typing import ClassVar

import colander
import pyoxigraph as ox
from harness import FIXTURES, FOAF_PERSON, fake_type, open_store
from widgets_support import EX, FormCase

from gracia import forms, shacl
from gracia.predicates import HookContext
from gracia.shacl import Shapes
from gracia.store import Store
from gracia.utils import assets

FOAF = "http://xmlns.com/foaf/0.1/"
GIVEN, FAMILY, PREFIX, NAME = (
    EX + n for n in ("givenName", "familyName", "prefix", "name")
)


def shapes_of(text: str) -> Shapes:
    return Shapes.parse(text, "turtle")


class ParseTest(unittest.TestCase):
    def test_a_placeholder_is_the_next_property_and_a_part_in_brackets_is_optional(
        self,
    ):
        self.assertEqual(
            shacl.PropertyTemplate.pieces_of("{} {}", 2),
            (("source", 0), ("text", " "), ("source", 1)),
        )
        self.assertEqual(
            shacl.PropertyTemplate.pieces_of("{} [{} ]{}", 3),
            (
                ("source", 0),
                ("text", " "),
                ("optional", (("source", 1), ("text", " "))),
                ("source", 2),
            ),
        )
        # doubled braces and brackets are themselves
        self.assertEqual(
            shacl.PropertyTemplate.pieces_of("{{x}} [[y]]", 0), (("text", "{x} [y]"),)
        )

    def test_a_template_that_makes_no_sense_is_refused(self):
        for text, count in (
            ("{} {}", 1),  # a placeholder too many
            ("{}", 2),  # a property too many
            ("{ }", 1),  # a brace alone
            ("}", 0),
            ("[{}", 1),  # not closed
            ("{}]", 1),  # closes nothing
            ("[x]{}", 1),  # nothing to leave out in it
            ("[[{}]{}]", 2),  # nested
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                shacl.PropertyTemplate.pieces_of(text, count)


def made(text: str, *sources: str) -> shacl.PropertyTemplate:
    return shacl.PropertyTemplate(
        NAME, sources, shacl.PropertyTemplate.pieces_of(text, len(sources))
    )


class RenderTest(unittest.TestCase):
    full = made("{} [{} ]{}", GIVEN, PREFIX, FAMILY)

    def test_the_sources_outside_the_brackets_are_needed_and_the_values_are_trimmed(
        self,
    ):
        self.assertEqual(self.full.required, [GIVEN, FAMILY])
        self.assertEqual(
            self.full.render({GIVEN: "Ada", FAMILY: "Lovelace"}), "Ada Lovelace"
        )
        self.assertIsNone(self.full.render({GIVEN: "Ada"}))
        self.assertIsNone(self.full.render({GIVEN: "Ada", FAMILY: "  "}))
        values: dict[str, str | None] = {
            GIVEN: "Ludwig",
            FAMILY: "Beethoven",
            PREFIX: "van",
        }
        self.assertEqual(self.full.render(values), "Ludwig van Beethoven")
        self.assertEqual(self.full.render({**values, PREFIX: " "}), "Ludwig Beethoven")
        self.assertEqual(made(" {} ", GIVEN).render({GIVEN: "  Ada\t"}), "Ada")


class ShapeTest(unittest.TestCase):
    PREFIXES = (
        "@prefix sh: <http://www.w3.org/ns/shacl#> .\n"
        "@prefix gracia: <https://gracia.pub/ns/> .\n"
        "@prefix ex: <http://example.org/> .\n"
    )

    def templates(self, body: str):
        data, shape = self.shape(body)
        return data.templates(shape)

    def shape(self, body: str):
        text = self.PREFIXES + "ex:S a sh:NodeShape ; sh:property " + body
        data = shapes_of(text)
        return data, ox.NamedNode(EX + "S")

    def test_the_templates_of_a_shape_are_found_also_in_the_person_profiles(self):
        found = self.templates(
            '[ sh:path ex:name ; gracia:propertyTemplate ( "{} {}" ex:givenName '
            "ex:familyName ) ] , [ sh:path ex:givenName ; sh:maxCount 1 ] , "
            "[ sh:path ex:familyName ; sh:maxCount 1 ] ."
        )
        self.assertEqual(
            [(t.target, t.sources) for t in found], [(NAME, (GIVEN, FAMILY))]
        )
        for spec in ("person-unittest-01.ttl", "person-minimal-01.ttl"):
            with self.subTest(spec=spec):
                path = assets.resolve(f"{FIXTURES}/foaf/shacl/{spec}")
                shapes, shape = assets.load_shape(path, FOAF + "Person")
                self.assertEqual(
                    [(t.target, t.sources) for t in shapes.templates(shape)],
                    [(FOAF + "name", (FOAF + "givenName", FOAF + "familyName"))],
                )

    def test_a_source_must_be_a_single_plain_value_of_the_same_shape(self):
        bad = {
            "not in the shape": "[ sh:path ex:name ; gracia:propertyTemplate "
            '( "{}" ex:givenName ) ] .',
            "can have many": "[ sh:path ex:name ; gracia:propertyTemplate "
            '( "{}" ex:givenName ) ] , [ sh:path ex:givenName ] .',
            "is the target": "[ sh:path ex:name ; sh:maxCount 1 ; "
            'gracia:propertyTemplate ( "{}" ex:name ) ] .',
            "is made by a template": "[ sh:path ex:name ; gracia:propertyTemplate "
            '( "{}" ex:givenName ) ] , [ sh:path ex:givenName ; sh:maxCount 1 ; '
            'gracia:propertyTemplate ( "{}" ex:familyName ) ] , '
            "[ sh:path ex:familyName ; sh:maxCount 1 ] .",
            "is a node": "[ sh:path ex:name ; gracia:propertyTemplate "
            '( "{}" ex:address ) ] , [ sh:path ex:address ; sh:maxCount 1 ; '
            "sh:node ex:S ] .",
        }
        for why, body in bad.items():
            with self.subTest(why), self.assertRaises(ValueError):
                self.templates(body)

    def test_a_template_in_an_alternative_is_for_the_value_that_alternative_has_and_names_its_kind_once(
        self,
    ):
        found = self.templates(
            "[ sh:path ex:url ; sh:nodeKind sh:IRI ; sh:maxCount 1 ] , "
            "[ sh:path ex:id ; sh:maxCount 1 ] , "
            "[ sh:path ex:kind ; sh:maxCount 1 ] ; "
            'sh:or ( [ sh:property [ sh:path ex:kind ; sh:hasValue "A" ] ; '
            "sh:property [ sh:path ex:url ; gracia:propertyTemplate "
            '( "https://a.example/{}" ex:id ) ] ] '
            '[ sh:property [ sh:path ex:kind ; sh:hasValue "B" ] ; '
            "sh:property [ sh:path ex:url ; gracia:propertyTemplate "
            '( "https://b.example/{}" ex:id ) ] ] ) .'
        )
        self.assertEqual(
            sorted((t.when, t.iri, t.sources) for t in found),
            [
                ((EX + "kind", "A"), True, (EX + "id",)),
                ((EX + "kind", "B"), True, (EX + "id",)),
            ],
        )
        for guards in (
            "",
            'sh:property [ sh:path ex:kind ; sh:hasValue "A" ] ; '
            'sh:property [ sh:path ex:id ; sh:hasValue "B" ] ; ',
        ):
            with self.subTest(guards=guards), self.assertRaises(ValueError):
                self.templates(
                    "[ sh:path ex:url ; sh:maxCount 1 ] , [ sh:path ex:id ; "
                    "sh:maxCount 1 ] , [ sh:path ex:kind ; sh:maxCount 1 ] ; "
                    f"sh:or ( [ {guards}sh:property [ sh:path ex:url ; "
                    'gracia:propertyTemplate ( "x/{}" ex:id ) ] ] ) .'
                )


class SqlTest(unittest.TestCase):
    TEMPLATE = made("{} [{} ]{}", GIVEN, PREFIX, FAMILY)

    def hooked(self, content: str, template=TEMPLATE):
        context = HookContext(None, fake_type(slug="person"), template.target)
        store = open_store()
        self.addCleanup(store.close)
        store.configure(
            lambda: [
                fake_type(
                    Store.template_statements([template], context),
                    slug="person",
                    number=5,
                )
            ]
        )
        return store.apply_hooks(
            shapes_of(
                "@prefix gracia: <https://gracia.pub/ns/> .\n"
                "@prefix ex: <http://example.org/> .\n"
                f"<rec:person:1> a gracia:Record ; gracia:resource [ {content} ] ."
            )
        )

    def names(self, result) -> list[str]:
        return sorted(
            q.object.value for q in result.dataset if q.predicate.value == NAME
        )

    def test_the_name_is_made_of_the_parts_and_gives_way_to_them_unless_one_is_missing(
        self,
    ):
        result = self.hooked(
            'a ex:Person ; ex:givenName "Ada" ; ex:familyName "Lovelace"'
        )
        self.assertEqual(self.names(result), ["Ada Lovelace"])
        result = self.hooked(
            'a ex:Person ; ex:name "A. Lovelace" ; '
            'ex:givenName "Ada" ; ex:familyName "Lovelace"'
        )  # a name that is there is replaced when the parts are all there
        self.assertEqual(self.names(result), ["Ada Lovelace"])
        for parts in (  # and kept when a part is missing or empty
            'ex:givenName "Ada"',
            'ex:givenName "Ada" ; ex:familyName "  "',
        ):
            with self.subTest(parts=parts):
                result = self.hooked(f'a ex:Person ; ex:name "A. L." ; {parts}')
                self.assertEqual(self.names(result), ["A. L."])
        result = self.hooked('a ex:Person ; ex:givenName "Ada"')
        self.assertEqual(self.names(result), [])  # one part alone makes no name

    def test_the_sql_and_the_python_agree_also_on_the_optional_part(self):
        for given, prefix, family in (
            ("Ada", "", "Lovelace"),
            ("Ludwig", "van", "Beethoven"),
            ("  Ada ", " de ", " Lovelace  "),
            ("Ada", "", ""),
        ):
            with self.subTest(given=given, prefix=prefix, family=family):
                parts = f'ex:givenName "{given}" ; ex:familyName "{family}"'
                if prefix:
                    parts += f' ; ex:prefix "{prefix}"'
                result = self.hooked(f"a ex:Person ; {parts}")
                expected = self.TEMPLATE.render(
                    {GIVEN: given, PREFIX: prefix, FAMILY: family}
                )
                self.assertEqual(self.names(result), [expected] if expected else [])
        result = self.hooked(
            'a ex:Person ; ex:givenName "Ludwig" ; ex:prefix "van" ; '
            'ex:familyName "Beethoven"'
        )
        self.assertEqual(self.names(result), ["Ludwig van Beethoven"])

    def test_a_node_inside_the_record_has_its_own_name_from_its_own_parts(self):
        template = shacl.PropertyTemplate(
            EX + "label",
            (EX + "a", EX + "b"),
            shacl.PropertyTemplate.pieces_of("{}, {}", 2),
        )
        result = self.hooked(
            'a ex:Thing ; ex:note [ ex:a "x" ; ex:b "y" ] , [ ex:a "z" ]', template
        )
        found = [
            q.object.value for q in result.dataset if q.predicate.value == EX + "label"
        ]
        self.assertEqual(found, ["x, y"])


class FoafPersonTest(FormCase):
    types = (FOAF_PERSON,)

    def test_the_name_field_is_not_required_and_has_its_template_for_the_browser(self):
        page = self.page("/test_repo/person/add")
        tag = re.search(r'<input[^>]*name="name"[^>]*>', page)
        assert tag is not None
        found = tag.group(0)
        self.assertNotIn("required", found)
        attribute = re.search(r'data-template="([^"]*)"', found)
        assert attribute is not None
        pieces = json.loads(html.unescape(attribute[1]))
        self.assertEqual(
            pieces,
            [
                {
                    "when": None,
                    "pieces": [
                        ["source", "givenName"],
                        ["text", " "],
                        ["source", "familyName"],
                    ],
                }
            ],
        )
        self.assertIn("template.js", page)

    def test_the_form_validator_wants_the_target_or_its_required_sources(self):
        def node(name, path):
            return colander.SchemaNode(
                colander.String(), name=name, title=name, predicate=path
            )

        name, given, family = (
            node("name", NAME),
            node("given", GIVEN),
            node("fam", FAMILY),
        )
        forms.made_of(name, [made("{} {}", GIVEN, FAMILY)], "[]", True)
        self.assertEqual(name.missing, "")  # not required by itself
        validate = forms.template_validator([name, given, family])
        mapping = colander.SchemaNode(colander.Mapping(), name, given, family)
        validate(mapping, {"name": "Ada", "given": "", "fam": ""})
        validate(mapping, {"name": "", "given": "Ada", "fam": "Lovelace"})
        with self.assertRaises(colander.Invalid) as caught:
            validate(mapping, {"name": "", "given": "Ada", "fam": " "})
        self.assertEqual(
            caught.exception.asdict(), {"name": "Give a name, or fill in given and fam"}
        )


class HeaderTest(FormCase):
    def shapes(self):
        return (
            self.application["test_repo"]
            .types["gadget"]
            .shacl_shape(include_record_shape=False)[0]
        )

    def test_a_node_shape_has_a_header_and_perhaps_a_badge(self):
        shapes = self.shapes()
        header, addon = shapes.header_templates(ox.NamedNode(EX + "MarkShape"))
        assert header is not None and addon is not None
        self.assertEqual(header.sources, (EX + "value",))
        self.assertEqual(addon.sources, (EX + "kind",))
        place, none = shapes.header_templates(ox.NamedNode(EX + "PlaceShape"))
        assert place is not None
        self.assertIsNone(none)
        self.assertEqual(
            place.render_names({"street": "Mekelweg 4", "city": "Delft"}),
            "Mekelweg 4, Delft",
        )
        self.assertEqual(
            place.render_names(
                {
                    "street": "Mekelweg 4",
                    "code": "2628 CD",
                    "city": "Delft",
                    "country": "NL",
                }
            ),
            "Mekelweg 4, 2628 CD Delft, NL",
        )
        self.assertIsNone(place.render_names({}))  # nothing: the bar has its title

    def test_a_header_can_only_use_the_properties_of_its_shape(self):
        data = shapes_of(
            "@prefix sh: <http://www.w3.org/ns/shacl#> .\n"
            "@prefix gracia: <https://gracia.pub/ns/> .\n"
            "@prefix ex: <http://example.org/> .\n"
            '<http://example.org/S> a sh:NodeShape ; gracia:headerTemplate ( "{}" '
            "ex:streatAddress ) ; sh:property [ sh:path ex:streetAddress ] ."
        )
        with self.assertRaises(ValueError):
            data.header_templates(ox.NamedNode(EX + "S"))

    def test_the_bar_of_a_node_has_the_icon_the_badge_and_the_header(self):
        record = self.application["test_repo"].types["gadget"].blank_record()
        form = forms.Form(record.edit_schema(), tabs="Record")
        page = form.render(
            {
                "resource": {
                    "mark": [{"kind": "ORCID", "value": "0000-0001"}],
                    "place": [{"street": "Mekelweg 4"}],
                }
            }
        )
        self.assertRegex(
            page,
            r"(?s)<summary[^>]*>\s*"
            r'<i class="bi bi-fingerprint me-2" aria-hidden="true"></i>\s*'
            r'<span class="badge[^>]*>ORCID</span>\s*'
            r'<span class="bar-text flex-grow-1"[^>]*>0000-0001</span>',
        )
        self.assertRegex(
            page,
            r'(?s)<i class="bi bi-geo-alt me-2" aria-hidden="true"></i>\s*'
            r'<span class="bar-text flex-grow-1"[^>]*>Mekelweg 4</span>',
        )
        page = forms.Form(record.edit_schema(), tabs="Record").render(
            {"resource": {"mark": [{"value": "x"}]}}
        )  # without a value for the badge it is there but hidden
        self.assertRegex(
            page, r'<span class="badge[^>]*data-header-template[^>]*hidden="hidden"'
        )


class MarkTest(FormCase):
    KINDS: ClassVar[dict[str, tuple[str, str | None]]] = {
        "ORCID": ("0000-0002-1825-0097", "https://orcid.org/0000-0002-1825-0097"),
        "ISNI": ("0000000121032683", "https://isni.org/isni/0000000121032683"),
        "Local": ("p-17", None),
        "External": ("whatever 1", None),
    }

    def post_mark(self, kind: str, value: str, url: str = "", name="A gadget"):
        row = [("kind", kind), ("value", value), ("url", url)]
        return self.post("/test_repo/gadget/add", rows={"mark": [row]}, name=name)

    def mark(self, number: int = 1):
        record = self.record("gadget", number)
        (node,) = record.graph.objects(record.resource, EX + "mark")
        return {
            q.predicate.value.removeprefix(EX): q.object
            for q in record.graph.dataset.quads_for_subject(node)
        }

    def test_the_kind_is_a_choice_and_the_url_is_a_text_input_with_the_templates_of_the_kinds(
        self,
    ):
        self.saved(
            "/test_repo/gadget/add",
            rows={
                "mark": [
                    [("kind", "ORCID"), ("value", "0000-0002-1825-0097"), ("url", "")]
                ]
            },
        )
        page = self.page("/test_repo/gadget/1/edit")
        select = re.search(r'(?s)<select[^>]*name="kind".*?</select>', page)
        assert select is not None
        self.assertEqual(
            re.findall(r'value="([^"]*)"', select.group(0)),
            ["", "ORCID", "ISNI", "Local", "External"],
        )
        self.assertRegex(page, r'<input[^>]*name="url"[^>]*>')
        attribute = re.search(r'name="url"[^>]*data-template="([^"]*)"', page)
        assert attribute is not None
        variants = json.loads(html.unescape(attribute[1]))
        self.assertEqual(sorted(v["when"][1] for v in variants), ["ISNI", "ORCID"])
        self.assertEqual(variants[0]["when"][0], "kind")
        self.assertIn("template.js", page)

    def test_the_url_is_made_for_the_kinds_that_have_one_and_a_typed_one_is_a_uri(self):
        for number, (kind, (value, url)) in enumerate(self.KINDS.items(), 1):
            with self.subTest(kind=kind):
                response = self.post_mark(kind, value, name=f"Gadget {number}")
                self.assertEqual(response.status_int, 302, response.text[:400])
                found = self.mark(number).get("url")
                self.assertEqual(found, ox.NamedNode(url) if url else None)
        response = self.post_mark("External", "x", "https://example.org/x", "Typed")
        self.assertEqual(response.status_int, 302, response.text[:400])
        self.assertEqual(self.mark(5)["url"], ox.NamedNode("https://example.org/x"))

    def test_what_is_not_in_the_form_of_its_kind_is_refused_as_the_shape_says(self):
        for kind, value, url, said in (
            ("ORCID", "not an orcid", "", "An ORCID iD is four groups of four digits"),
            ("ISNI", "12", "", "An ISNI is 16 digits"),
            (
                "ORCID",
                "0000-0002-1825-0097",
                "https://example.org/x",
                "An ORCID URL starts with https://orcid.org/",
            ),
            ("Twitter", "ada", "", "is not one of"),
            ("External", "x", "not a uri", "Not a URI"),
        ):
            with self.subTest(kind=kind, url=url):
                response = self.post_mark(kind, value, url)
                self.assertEqual(response.status_int, 200)
                self.assertIn(said, response.text)  # at the value, as the shape says
        ok = self.post_mark("ORCID", "0000-0002-1825-0097", "https://orcid.org/x")
        self.assertEqual(ok.status_int, 302, ok.text[:300])


class FewStatementsTest(FormCase):
    def test_the_variants_of_one_property_are_one_delete_and_one_insert(self):
        shapes = (
            self.application["test_repo"]
            .types["gadget"]
            .shacl_shape(include_record_shape=False)[0]
        )
        found = shapes.templates(ox.NamedNode(EX + "MarkShape"))
        self.assertEqual(len(found), 2)  # one for each kind that has an url
        context = HookContext(None, fake_type(slug="gadget"), EX + "url")
        statements = Store.template_statements(found, context)
        self.assertEqual(len(statements), 2)
        self.assertTrue(statements[0].lstrip().startswith("DELETE"))
        self.assertTrue(statements[1].lstrip().startswith("INSERT"))

    def test_only_the_hooks_of_the_types_in_the_batch_are_run(self):
        store = open_store()
        self.addCleanup(store.close)

        def mark(slug):
            return f"CREATE TABLE ran_{slug} AS SELECT 1 AS one"

        store.configure(
            lambda: [
                fake_type([mark("person")], slug="person", number=5),
                fake_type([mark("book")], slug="book", number=6),
            ]
        )
        store.apply_hooks(
            shapes_of(
                "@prefix gracia: <https://gracia.pub/ns/> .\n"
                "<rec:person:1> a gracia:Record ."
            )
        )
        tables = {r[0] for r in store.con.execute("SHOW TABLES").fetchall()}
        self.assertIn("ran_person", tables)
        self.assertNotIn("ran_book", tables)  # no book in the batch: no round trip
