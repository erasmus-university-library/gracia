import base64
import html
import json
import re
import unittest
from typing import ClassVar

import colander
import pyoxigraph as ox
from harness import ConfiguredCase, fake_type, open_store
from test_records import graph

from gracia import forms, templates, types
from gracia.predicates import HookContext
from gracia.utils import assets

SCHEMA = "https://schema.org/"
GIVEN, FAMILY, PREFIX = (SCHEMA + n for n in ("givenName", "familyName", "prefix"))
NAME = SCHEMA + "name"
AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:correct horse").decode()}


class ParseTest(unittest.TestCase):
    def test_a_placeholder_is_the_next_property(self):
        self.assertEqual(
            templates.parse("{} {}", 2),
            (("source", 0), ("text", " "), ("source", 1)),
        )

    def test_an_optional_part_has_its_own_placeholders(self):
        self.assertEqual(
            templates.parse("{} [{} ]{}", 3),
            (
                ("source", 0),
                ("text", " "),
                ("optional", (("source", 1), ("text", " "))),
                ("source", 2),
            ),
        )

    def test_doubled_braces_and_brackets_are_themselves(self):
        self.assertEqual(templates.parse("{{x}} [[y]]", 0), (("text", "{x} [y]"),))

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
                templates.parse(text, count)


def made(text: str, *sources: str) -> templates.PropertyTemplate:
    return templates.PropertyTemplate(
        NAME, sources, templates.parse(text, len(sources))
    )


class RenderTest(unittest.TestCase):
    full = made("{} [{} ]{}", GIVEN, PREFIX, FAMILY)

    def test_every_source_outside_the_brackets_is_needed(self):
        self.assertEqual(self.full.required, [GIVEN, FAMILY])
        self.assertEqual(
            self.full.render({GIVEN: "Ada", FAMILY: "Lovelace"}), "Ada Lovelace"
        )
        self.assertIsNone(self.full.render({GIVEN: "Ada"}))
        self.assertIsNone(self.full.render({GIVEN: "Ada", FAMILY: "  "}))

    def test_an_optional_part_is_left_out_without_its_source(self):
        values: dict[str, str | None] = {
            GIVEN: "Ludwig",
            FAMILY: "Beethoven",
            PREFIX: "van",
        }
        self.assertEqual(self.full.render(values), "Ludwig van Beethoven")
        self.assertEqual(self.full.render({**values, PREFIX: " "}), "Ludwig Beethoven")

    def test_the_result_and_the_values_are_trimmed(self):
        self.assertEqual(made(" {} ", GIVEN).render({GIVEN: "  Ada\t"}), "Ada")


class ShapeTest(unittest.TestCase):
    PREFIXES = (
        "@prefix sh: <http://www.w3.org/ns/shacl#> .\n"
        "@prefix gracia: <https://gracia.pub/ns/> .\n"
        "@prefix ex: <http://example.org/> .\n"
        "@prefix s: <https://schema.org/> .\n"
    )

    def shape(self, body: str):
        text = self.PREFIXES + "ex:S a sh:NodeShape ; sh:property " + body
        data = graph(text)
        return data, ox.NamedNode("http://example.org/S")

    def test_the_templates_of_a_shape_are_found(self):
        found = templates.of_shape(
            *self.shape(
                '[ sh:path s:name ; gracia:propertyTemplate ( "{} {}" s:givenName '
                "s:familyName ) ] , [ sh:path s:givenName ; sh:maxCount 1 ] , "
                "[ sh:path s:familyName ; sh:maxCount 1 ] ."
            )
        )
        self.assertEqual(
            [(t.target, t.sources) for t in found], [(NAME, (GIVEN, FAMILY))]
        )

    def test_a_source_must_be_a_single_plain_value_of_the_same_shape(self):
        bad = {
            "not in the shape": "[ sh:path s:name ; gracia:propertyTemplate "
            '( "{}" s:givenName ) ] .',
            "can have many": "[ sh:path s:name ; gracia:propertyTemplate "
            '( "{}" s:givenName ) ] , [ sh:path s:givenName ] .',
            "is the target": "[ sh:path s:name ; sh:maxCount 1 ; "
            'gracia:propertyTemplate ( "{}" s:name ) ] .',
            "is made by a template": "[ sh:path s:name ; gracia:propertyTemplate "
            '( "{}" s:givenName ) ] , [ sh:path s:givenName ; sh:maxCount 1 ; '
            'gracia:propertyTemplate ( "{}" s:familyName ) ] , '
            "[ sh:path s:familyName ; sh:maxCount 1 ] .",
            "is a node": "[ sh:path s:name ; gracia:propertyTemplate "
            '( "{}" s:address ) ] , [ sh:path s:address ; sh:maxCount 1 ; '
            "sh:node ex:S ] .",
        }
        for why, body in bad.items():
            with self.subTest(why), self.assertRaises(ValueError):
                templates.of_shape(*self.shape(body))

    def test_a_template_in_an_alternative_is_for_the_value_that_alternative_has(self):
        found = templates.of_shape(
            *self.shape(
                "[ sh:path s:url ; sh:nodeKind sh:IRI ; sh:maxCount 1 ] , "
                "[ sh:path s:id ; sh:maxCount 1 ] , "
                "[ sh:path s:kind ; sh:maxCount 1 ] ; "
                'sh:or ( [ sh:property [ sh:path s:kind ; sh:hasValue "A" ] ; '
                "sh:property [ sh:path s:url ; gracia:propertyTemplate "
                '( "https://a.example/{}" s:id ) ] ] '
                '[ sh:property [ sh:path s:kind ; sh:hasValue "B" ] ; '
                "sh:property [ sh:path s:url ; gracia:propertyTemplate "
                '( "https://b.example/{}" s:id ) ] ] ) .'
            )
        )
        self.assertEqual(
            sorted((t.when, t.iri, t.sources) for t in found),
            [
                ((SCHEMA + "kind", "A"), True, (SCHEMA + "id",)),
                ((SCHEMA + "kind", "B"), True, (SCHEMA + "id",)),
            ],
        )

    def test_an_alternative_with_a_template_names_its_kind_once(self):
        for guards in (
            "",
            'sh:property [ sh:path s:kind ; sh:hasValue "A" ] ; '
            'sh:property [ sh:path s:id ; sh:hasValue "B" ] ; ',
        ):
            with self.subTest(guards=guards), self.assertRaises(ValueError):
                templates.of_shape(
                    *self.shape(
                        "[ sh:path s:url ; sh:maxCount 1 ] , [ sh:path s:id ; "
                        "sh:maxCount 1 ] , [ sh:path s:kind ; sh:maxCount 1 ] ; "
                        f"sh:or ( [ {guards}sh:property [ sh:path s:url ; "
                        'gracia:propertyTemplate ( "x/{}" s:id ) ] ] ) .'
                    )
                )

    def test_the_shapes_of_the_person_profiles_have_theirs(self):
        foaf = "http://xmlns.com/foaf/0.1/"
        for spec, class_uri, name, given, family in (
            ("Person-schemaorg-01.ttl", SCHEMA + "Person", NAME, GIVEN, FAMILY),
            (
                "Person-full-01.ttl",
                foaf + "Person",
                foaf + "name",
                foaf + "givenName",
                foaf + "familyName",
            ),
            (
                "Person-minimal-01.ttl",
                foaf + "Person",
                foaf + "name",
                foaf + "givenName",
                foaf + "familyName",
            ),
        ):
            with self.subTest(spec=spec):
                path = assets.resolve(f"gracia.types:person/shacl/{spec}")
                shapes, shape = assets.load_shape(path, class_uri)
                self.assertEqual(
                    [(t.target, t.sources) for t in templates.of_shape(shapes, shape)],
                    [(name, (given, family))],
                )


class SqlTest(unittest.TestCase):
    TEMPLATE = made("{} [{} ]{}", GIVEN, PREFIX, FAMILY)

    def hooked(self, content: str, template=TEMPLATE, nodes: str = ""):
        context = HookContext(None, fake_type(slug="person"), template.target)
        store = open_store()
        self.addCleanup(store.close)
        store.configure(
            lambda: [fake_type(template.sql(context), slug="person", number=5)]
        )
        return store.apply_hooks(
            graph(
                "@prefix gracia: <https://gracia.pub/ns/> .\n"
                "@prefix schema: <https://schema.org/> .\n"
                f"<rec:person:1> a gracia:Record ; gracia:resource [ {content} ] ."
            )
        )

    def names(self, result) -> list[str]:
        return sorted(
            q.object.value for q in result.dataset if q.predicate.value == NAME
        )

    def test_the_name_is_made_of_the_parts(self):
        result = self.hooked(
            'a schema:Person ; schema:givenName "Ada" ; schema:familyName "Lovelace"'
        )
        self.assertEqual(self.names(result), ["Ada Lovelace"])

    def test_a_name_that_is_there_is_replaced_when_the_parts_are_all_there(self):
        result = self.hooked(
            'a schema:Person ; schema:name "A. Lovelace" ; '
            'schema:givenName "Ada" ; schema:familyName "Lovelace"'
        )
        self.assertEqual(self.names(result), ["Ada Lovelace"])

    def test_a_name_that_is_there_is_kept_when_a_part_is_missing_or_empty(self):
        for parts in (
            'schema:givenName "Ada"',
            'schema:givenName "Ada" ; schema:familyName "  "',
        ):
            with self.subTest(parts=parts):
                result = self.hooked(f'a schema:Person ; schema:name "A. L." ; {parts}')
                self.assertEqual(self.names(result), ["A. L."])

    def test_one_part_alone_makes_no_name(self):
        result = self.hooked('a schema:Person ; schema:givenName "Ada"')
        self.assertEqual(self.names(result), [])

    def test_the_optional_part_is_used_when_it_is_there(self):
        result = self.hooked(
            'a schema:Person ; schema:givenName "Ludwig" ; schema:prefix "van" ; '
            'schema:familyName "Beethoven"'
        )
        self.assertEqual(self.names(result), ["Ludwig van Beethoven"])
        result = self.hooked(
            'a schema:Person ; schema:givenName "Ludwig" ; '
            'schema:familyName "Beethoven"'
        )
        self.assertEqual(self.names(result), ["Ludwig Beethoven"])

    def test_the_sql_and_the_python_agree(self):
        for given, prefix, family in (
            ("Ada", "", "Lovelace"),
            ("Ludwig", "van", "Beethoven"),
            ("  Ada ", " de ", " Lovelace  "),
            ("Ada", "", ""),
        ):
            with self.subTest(given=given, prefix=prefix, family=family):
                parts = f'schema:givenName "{given}" ; schema:familyName "{family}"'
                if prefix:
                    parts += f' ; schema:prefix "{prefix}"'
                result = self.hooked(f"a schema:Person ; {parts}")
                expected = self.TEMPLATE.render(
                    {GIVEN: given, PREFIX: prefix, FAMILY: family}
                )
                self.assertEqual(self.names(result), [expected] if expected else [])

    def test_a_node_inside_the_record_has_its_own_name_from_its_own_parts(self):
        template = templates.PropertyTemplate(
            "http://example.org/label",
            ("http://example.org/a", "http://example.org/b"),
            templates.parse("{}, {}", 2),
        )
        result = self.hooked(
            'a schema:Thing ; schema:note [ <http://example.org/a> "x" ; '
            '<http://example.org/b> "y" ] , [ <http://example.org/a> "z" ]',
            template,
        )
        found = [
            q.object.value
            for q in result.dataset
            if q.predicate.value == "http://example.org/label"
        ]
        self.assertEqual(found, ["x, y"])


class PersonRecordTest(ConfiguredCase):
    def setUp(self):
        super().setUp()
        self.application.create_repository(
            "test_repo",
            admin=("admin", "correct horse"),
            content_types=[types.available()["person"]],
        )
        self.web = self.client()

    def post(self, path: str, **values):
        form = self.web.get(path, headers=AUTH).forms[0]
        out = []
        for name, value in form.submit_fields("save"):
            out.append((name, values.get(name, value)))
        return self.web.post(path, params=out, headers=AUTH, expect_errors=True)

    def person(self):
        record = self.application["test_repo"].types["person"]["1"]
        return record, record.graph.value(record.resource, NAME)

    def test_the_name_is_made_of_the_parts_and_is_the_label(self):
        response = self.post(
            "/test_repo/person/add", givenName="Ada", familyName="Lovelace"
        )
        self.assertEqual(response.status_int, 302, response.text[:500])
        record, name = self.person()
        self.assertEqual(name.value, "Ada Lovelace")
        self.assertEqual(record.label, "Ada Lovelace")

    def test_a_name_that_was_typed_gives_way_to_the_parts(self):
        self.post(
            "/test_repo/person/add",
            name="A. L.",
            givenName="Ada",
            familyName="Lovelace",
        )
        record, name = self.person()
        self.assertEqual((name.value, record.label), ("Ada Lovelace",) * 2)

    def test_a_name_alone_is_kept(self):
        self.post("/test_repo/person/add", name="Ada")
        record, name = self.person()
        self.assertEqual((name.value, record.label), ("Ada", "Ada"))

    def test_without_a_name_or_both_parts_the_form_says_what_to_give(self):
        response = self.post("/test_repo/person/add", givenName="Ada")
        self.assertEqual(response.status_int, 200)
        self.assertIn(
            "Give a name, or fill in given name and family name", response.text
        )

    def test_the_name_field_is_not_required_and_has_its_template_for_the_browser(self):
        page = self.web.get("/test_repo/person/add", headers=AUTH).text
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


SHAPES = "http://example.org/shapes#"


class HeaderTest(ConfiguredCase):
    def person_shapes(self):
        path = assets.resolve("gracia.types:person/shacl/Person-schemaorg-01.ttl")
        return assets.load_shape(path, SCHEMA + "Person")[0]

    def test_a_node_shape_has_a_header_and_perhaps_a_badge(self):
        shapes = self.person_shapes()
        header, addon = templates.header_of(
            shapes, ox.NamedNode(SHAPES + "PropertyValueShape")
        )
        assert header is not None and addon is not None
        self.assertEqual(header.sources, (SCHEMA + "value",))
        self.assertEqual(addon.sources, (SCHEMA + "propertyID",))
        address, none = templates.header_of(
            shapes, ox.NamedNode(SHAPES + "PostalAddressShape")
        )
        assert address is not None
        self.assertIsNone(none)
        self.assertEqual(
            address.render_names(
                {"streetAddress": "Mekelweg 4", "addressLocality": "Delft"}
            ),
            "Mekelweg 4, Delft",
        )
        self.assertEqual(
            address.render_names(
                {
                    "streetAddress": "Mekelweg 4",
                    "postalCode": "2628 CD",
                    "addressLocality": "Delft",
                    "addressCountry": "NL",
                }
            ),
            "Mekelweg 4, 2628 CD Delft, NL",
        )
        self.assertIsNone(address.render_names({}))  # nothing: the bar has its title

    def test_a_header_can_only_use_the_properties_of_its_shape(self):
        data = graph(
            "@prefix sh: <http://www.w3.org/ns/shacl#> .\n"
            "@prefix gracia: <https://gracia.pub/ns/> .\n"
            "@prefix s: <https://schema.org/> .\n"
            '<http://example.org/S> a sh:NodeShape ; gracia:headerTemplate ( "{}" '
            "s:streatAddress ) ; sh:property [ sh:path s:streetAddress ] ."
        )
        with self.assertRaises(ValueError):
            templates.header_of(data, ox.NamedNode("http://example.org/S"))

    def test_the_bar_of_a_node_has_the_icon_the_badge_and_the_header(self):
        record = self.person_record()
        form = forms.Form(record.edit_schema(), tabs="Record")
        page = form.render(
            {
                "resource": {
                    "identifier": [{"propertyID": "ORCID", "value": "0000-0001"}],
                    "address": [{"streetAddress": "Mekelweg 4"}],
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

    def test_without_a_value_for_the_badge_it_is_there_but_hidden(self):
        page = forms.Form(self.person_record().edit_schema(), tabs="Record").render(
            {"resource": {"identifier": [{"value": "x"}]}}
        )
        self.assertRegex(
            page, r'<span class="badge[^>]*data-header-template[^>]*hidden="hidden"'
        )

    def person_record(self):
        self.application.create_repository(
            "test_repo",
            admin=("admin", "correct horse"),
            content_types=[types.available()["person"]],
        )
        return self.application["test_repo"].types["person"].blank_record()


class IdentifierTest(ConfiguredCase):
    KINDS: ClassVar[dict[str, tuple[str, str | None]]] = {
        "ORCID": ("0000-0002-1825-0097", "https://orcid.org/0000-0002-1825-0097"),
        "ISNI": ("0000000121032683", "https://isni.org/isni/0000000121032683"),
        "OpenAlex": ("A5023888391", "https://openalex.org/A5023888391"),
        "Scopus": (
            "57189222659",
            "https://www.scopus.com/authid/detail.uri?authorId=57189222659",
        ),
        "ResearcherId": (
            "A-1234-2008",
            "https://www.webofscience.com/wos/author/rid/A-1234-2008",
        ),
        "Local": ("p-17", None),
        "External": ("whatever 1", None),
    }

    def setUp(self):
        super().setUp()
        self.application.create_repository(
            "test_repo",
            admin=("admin", "correct horse"),
            content_types=[types.available()["person"]],
        )
        self.web = self.client()

    def post(self, kind: str, value: str, url: str = "", name="Ada Lovelace"):
        form = self.web.get("/test_repo/person/add", headers=AUTH).forms[0]
        out = []
        for field, text in form.submit_fields("save"):
            out.append((field, name if field == "name" else text))
            if (field, text) == ("__start__", "identifier:sequence"):
                out += [
                    ("__start__", "item:mapping"),
                    ("propertyID", kind),
                    ("value", value),
                    ("url", url),
                    ("__end__", "item:mapping"),
                ]
        return self.web.post(
            "/test_repo/person/add", params=out, headers=AUTH, expect_errors=True
        )

    def identifier(self, number: int = 1):
        record = self.application["test_repo"].types["person"][str(number)]
        (node,) = record.graph.objects(record.resource, SCHEMA + "identifier")
        return {
            q.predicate.value.removeprefix(SCHEMA): q.object
            for q in record.graph.dataset.quads_for_subject(node)
        }

    def test_the_kind_is_a_choice_of_the_seven_and_the_url_is_a_text_input(self):
        self.post("ORCID", "0000-0002-1825-0097")
        page = self.web.get("/test_repo/person/ada-lovelace/edit", headers=AUTH).text
        select = re.search(r'(?s)<select[^>]*name="propertyID".*?</select>', page)
        assert select is not None
        self.assertEqual(
            re.findall(r'value="([^"]*)"', select.group(0)), ["", *self.KINDS]
        )
        self.assertRegex(page, r'<input[^>]*name="url"[^>]*>')

    def test_the_url_is_made_for_the_kinds_that_have_one_and_is_a_uri(self):
        for number, (kind, (value, url)) in enumerate(self.KINDS.items(), 1):
            with self.subTest(kind=kind):
                response = self.post(kind, value, name=f"Person {number}")
                self.assertEqual(response.status_int, 302, response.text[:400])
                found = self.identifier(number).get("url")
                self.assertEqual(found, ox.NamedNode(url) if url else None)

    def test_a_value_that_is_not_in_the_form_of_its_kind_is_refused(self):
        for kind, value, said in (
            ("ORCID", "not an orcid", "An ORCID iD is four groups of four digits"),
            ("ISNI", "12", "An ISNI is 16 digits"),
            ("OpenAlex", "5023", "An OpenAlex id is a letter and digits"),
            ("Scopus", "12", "A Scopus id is 6 to 12 digits"),
            ("ResearcherId", "1234", "A ResearcherId is like A-1234-2008"),
        ):
            with self.subTest(kind=kind):
                response = self.post(kind, value)
                self.assertEqual(response.status_int, 200)
                self.assertIn(said, response.text)  # at the value, as the shape says
                self.assertNotIn(
                    "An ORCID iD", response.text.replace(said, "")
                ) if kind != "ORCID" else None

    def test_a_kind_that_is_not_one_of_them_is_refused(self):
        response = self.post("Twitter", "ada")
        self.assertEqual(response.status_int, 200)
        self.assertIn("is not one of", response.text)

    def test_a_url_that_was_typed_is_a_uri(self):
        response = self.post("External", "x", "https://example.org/x")
        self.assertEqual(response.status_int, 302, response.text[:400])
        self.assertEqual(
            self.identifier()["url"], ox.NamedNode("https://example.org/x")
        )

    def test_a_url_of_a_kind_has_to_start_as_the_kind_says(self):
        response = self.post("ORCID", "0000-0002-1825-0097", "https://example.org/x")
        self.assertEqual(response.status_int, 200)
        self.assertIn("An ORCID URL starts with https://orcid.org/", response.text)
        ok = self.post(
            "ORCID", "0000-0002-1825-0097", "https://orcid.org/0000-0002-1825-0097"
        )
        self.assertEqual(ok.status_int, 302, ok.text[:300])

    def test_something_that_is_not_a_uri_is_refused(self):
        response = self.post("External", "x", "not a uri")
        self.assertEqual(response.status_int, 200)
        self.assertIn("Not a URI", response.text)

    def test_the_templates_for_the_kinds_are_in_the_page_for_the_browser(self):
        self.post("ORCID", "0000-0002-1825-0097")
        page = self.web.get("/test_repo/person/ada-lovelace/edit", headers=AUTH).text
        attribute = re.search(r'name="url"[^>]*data-template="([^"]*)"', page)
        assert attribute is not None
        variants = json.loads(html.unescape(attribute[1]))
        self.assertEqual(
            sorted(v["when"][1] for v in variants),
            sorted(["ORCID", "ISNI", "OpenAlex", "Scopus", "ResearcherId"]),
        )
        self.assertEqual(variants[0]["when"][0], "propertyID")
        self.assertIn("template.js", page)


class FewStatementsTest(unittest.TestCase):
    def test_the_variants_of_one_property_are_one_delete_and_one_insert(self):
        path = assets.resolve("gracia.types:person/shacl/Person-schemaorg-01.ttl")
        shapes, _shape = assets.load_shape(path, SCHEMA + "Person")
        found = templates.of_shape(shapes, ox.NamedNode(SHAPES + "PropertyValueShape"))
        self.assertEqual(len(found), 5)  # one for each kind that has an url
        context = HookContext(None, fake_type(slug="person"), SCHEMA + "url")
        statements = templates.sql_of(found, context)
        self.assertEqual(len(statements), 2)
        self.assertTrue(statements[0].lstrip().startswith("DELETE"))
        self.assertTrue(statements[1].lstrip().startswith("INSERT"))

    def test_only_the_hooks_of_the_types_in_the_batch_are_run(self):
        store = open_store()
        self.addCleanup(store.close)
        mark = lambda slug: f"CREATE TABLE ran_{slug} AS SELECT 1 AS one"  # noqa: E731
        store.configure(
            lambda: [
                fake_type([mark("person")], slug="person", number=5),
                fake_type([mark("book")], slug="book", number=6),
            ]
        )
        store.apply_hooks(
            graph(
                "@prefix gracia: <https://gracia.pub/ns/> .\n"
                "<rec:person:1> a gracia:Record ."
            )
        )
        tables = {r[0] for r in store.con.execute("SHOW TABLES").fetchall()}
        self.assertIn("ran_person", tables)
        self.assertNotIn("ran_book", tables)  # no book in the batch: no round trip
