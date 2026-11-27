import html
import re
from pathlib import Path

import pyoxigraph as ox
from support import must_search
from test_edit import EditCase

from gracia.graph import Graph

FOAF = "http://xmlns.com/foaf/0.1/"


class SourceCase(EditCase):
    def get(self, path, headers=None, **kw):
        return self.client().get(path, headers=headers or self.admin, **kw)

    def text(self, response) -> str:
        return html.unescape(
            must_search(r"<textarea[^>]*>(.*?)</textarea>", response.text, re.S)[1]
        )

    def post(
        self, path, text, fmt="turtle", button="save", was=None, headers=None, **kw
    ):
        data = {
            "source_text": text,
            "source_format": fmt,
            "source_format_was": was or fmt,
            button: button,
        }
        return self.client().post(path, data, headers=headers or self.admin, **kw)

    def edited(self, **replace):
        text = self.text(self.get("/test_repo/person/1/edit?source=turtle"))
        for old, new in replace.items():
            self.assertIn(old, text)
            text = text.replace(old, new)
        return text


class LinkTest(SourceCase):
    def test_the_form_has_a_source_link_above_it_at_the_right(self):
        page = self.get("/test_repo/person/1/edit").text
        self.assertRegex(
            page,
            r'<a class="btn btn-outline-secondary btn-sm" '
            r'href="http://localhost/test_repo/person/1/edit\?source=turtle">Source '
            r"Edit</a>",
        )
        self.assertLess(page.index(">Source Edit</a>"), page.index("<form"))
        self.assertLess(
            page.index("Last modified"), page.index(">Source Edit</a>")
        )  # above them

    def test_the_add_form_has_one_too(self):
        page = self.get("/test_repo/person/add").text
        self.assertIn(
            'href="http://localhost/test_repo/person/add?source=turtle">'
            "Source Edit</a>",
            page,
        )

    def test_the_source_page_is_the_text_instead_of_the_form_and_a_link_back(self):
        page = self.get("/test_repo/person/1/edit?source=turtle").text
        self.assertIn(
            '<textarea class="form-control font-monospace" name="source_text"', page
        )
        self.assertNotIn('name="resource_uri"', page)  # not the form
        self.assertIn(
            'href="http://localhost/test_repo/person/1/edit">Form Edit</a>', page
        )
        self.assertIn("source.js", page.split("</head>")[0])
        self.assertNotIn(
            "source.js", self.get("/test_repo/person/1/edit").text
        )  # only here

    def test_a_user_has_no_source(self):
        page = self.get("/test_repo/user/1/edit").text
        self.assertNotIn(">Source Edit</a>", page)
        shown = self.get("/test_repo/user/1/edit?source=turtle").text
        self.assertNotIn("source_text", shown)  # the form, with the password


class FormatTest(SourceCase):
    def test_turtle_is_the_default_and_the_list_has_the_other_serializations(self):
        page = self.get("/test_repo/person/1/edit?source=turtle").text
        select = must_search(r"<select.*?</select>", page, re.S)[0]
        self.assertEqual(
            re.findall(r'<option[^>]*value="([^"]+)"', select),
            ["turtle", "ntriples", "rdfxml", "jsonld"],
        )
        self.assertRegex(select, r'<option[^>]*value="turtle"[^>]*selected')
        text = self.text(self.get("/test_repo/person/1/edit?source=turtle"))
        self.assertIn(
            "@prefix foaf: <http://xmlns.com/foaf/0.1/>", text
        )  # the prefixes of the repository
        self.assertIn('foaf:name "Ada"', text)

    def test_each_format_shows_the_same_data(self):
        marks = {
            "ntriples": f'<{FOAF}name> "Ada"',
            "rdfxml": "<rdf:RDF",
            "jsonld": '"@id"',
        }
        for fmt, mark in marks.items():
            with self.subTest(fmt=fmt):
                text = self.text(self.get(f"/test_repo/person/1/edit?source={fmt}"))
                self.assertIn(mark, text)
                self.assertIn("Ada", text)

    def test_choosing_another_format_converts_the_text_and_keeps_what_was_typed(self):
        text = self.edited(**{'foaf:name "Ada"': 'foaf:name "Ada of Lovelace"'})
        shown = self.post(
            "/test_repo/person/1/edit",
            text,
            fmt="ntriples",
            was="turtle",
            button="convert",
        )
        self.assertEqual(shown.status_int, 200)
        converted = self.text(shown)
        self.assertIn(
            f'<{FOAF}name> "Ada of Lovelace"', converted
        )  # what was typed, converted
        self.assertRegex(shown.text, r'<option[^>]*value="ntriples"[^>]*selected')
        self.assertIn('name="source_format_was" value="ntriples"', shown.text)
        self.assertNotIn(
            (f"{FOAF}name", "Ada of Lovelace"), self.values("rec:person:1")
        )

    def test_text_that_is_not_in_the_format_it_says_is_an_error_and_is_kept(self):
        shown = self.post(
            "/test_repo/person/1/edit", "this is not turtle {", button="convert"
        )
        self.assertEqual(shown.status_int, 200)
        self.assertIn("This is not valid turtle", shown.text)
        self.assertIn("this is not turtle {", self.text(shown))


CLOSED = """@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix foaf: <http://xmlns.com/foaf/0.1/> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
<urn:test:Closed> a sh:NodeShape ; sh:targetClass foaf:Person ;
  sh:closed true ; sh:ignoredProperties ( rdf:type ) ;
  sh:property [ sh:path foaf:name ; sh:datatype xsd:string ; sh:minCount 1 ;
                sh:maxCount 1 ] ,
  [ sh:path foaf:knows ; sh:nodeKind sh:IRI ] ."""


class ClosedShapeTest(SourceCase):
    def errors(self, response) -> list[str]:
        return [
            html.unescape(m)
            for m in re.findall(r"alert-danger[^>]*>\s*([^<]*?)\s*<", response.text)
        ]

    def use_the_closed_profile(self):
        (Path(self.person_assets) / "shacl" / "person-closed-01.ttl").write_text(CLOSED)
        with self.application.new_store("test_repo") as store:
            data = store.get("rec:type:5")
            old = next(q for q in data if q.predicate.value.endswith("/schemaTemplate"))
            data.discard(old)
            data.add(
                ox.Quad(
                    old.subject,
                    old.predicate,
                    ox.Literal("person-closed-01.ttl"),
                    old.graph_name,
                )
            )
            store.put_multi(data)

    def test_a_property_of_the_record_that_the_record_shape_does_not_have_is_an_error(
        self,
    ):
        text = self.edited() + '\n<rec:person:1> <https://gracia.pub/ns/lable> "x" .'
        got = self.post("/test_repo/person/1/edit", text)
        self.assertEqual(got.status_int, 200)
        self.assertEqual(
            self.errors(got),
            ['lable: this property is not described by the shape (its value is "x")'],
        )
        self.assertNotIn("lable", "".join(p for p, _ in self.values("rec:person:1")))

    def test_the_record_shape_is_closed_but_not_for_the_type_of_the_node(self):
        got = self.post("/test_repo/person/1/edit", self.edited())
        self.assertEqual(
            got.status_int, 302
        )  # rdf:type, gracia:type and the rest are described

    def test_a_property_of_the_content_that_a_closed_shape_does_not_have_is_an_error(
        self,
    ):
        self.use_the_closed_profile()
        text = self.edited(
            **{'foaf:name "Ada"': 'foaf:name "Ada" ; foaf:givnName "Ada"'}
        )
        got = self.post("/test_repo/person/1/edit", text)
        self.assertEqual(got.status_int, 200)
        self.assertEqual(
            self.errors(got),
            [
                "givnName: this property is not described by the shape (its value is "
                '"Ada")'
            ],
        )  # said once, and not the note that the content does not conform
        self.assertNotIn(f"{FOAF}givnName", [p for p, _ in self.values("rec:person:1")])

    def test_what_the_closed_shape_describes_is_saved(self):
        self.use_the_closed_profile()
        text = self.edited(**{'foaf:name "Ada"': 'foaf:name "Ada Lovelace"'})
        self.assertEqual(self.post("/test_repo/person/1/edit", text).status_int, 302)

    def test_a_shape_that_is_not_closed_lets_other_properties_be(self):
        text = self.edited(
            **{'foaf:name "Ada"': 'foaf:name "Ada" ; foaf:givnName "Ada"'}
        )
        self.assertEqual(
            self.post("/test_repo/person/1/edit", text).status_int, 302
        )  # open


class PrefixesTest(SourceCase):
    def test_turtle_declares_only_the_prefixes_it_uses(self):
        text = self.text(self.get("/test_repo/person/1/edit?source=turtle"))
        declared = re.findall(r"^@prefix (\w+):", text, re.M)
        self.assertEqual(
            declared, ["foaf", "gracia"]
        )  # not rdf, rdfs, skos, ...: `a` is not rdf:
        self.assertNotIn("@prefix skos", text)

    def test_a_prefix_is_kept_when_it_is_used_and_not_when_it_is_only_in_a_string(self):
        data = Graph.parse(
            '<rec:person:1> <http://purl.org/dc/terms/title> "see skos:Concept" .',
            "turtle",
        )
        text = data.serialize(
            "turtle",
            {
                "dcterms": "http://purl.org/dc/terms/",
                "skos": "http://www.w3.org/2004/02/skos/core#",
            },
        )
        self.assertIn("@prefix dcterms:", text)
        self.assertNotIn("@prefix skos:", text)  # skos: is only in a literal

    def test_rdfxml_declares_only_the_namespaces_it_uses_and_always_rdf(self):
        text = self.text(self.get("/test_repo/person/1/edit?source=rdfxml"))
        declared = re.findall(r"xmlns:(\w+)=", text)
        self.assertEqual(declared, ["foaf", "gracia", "rdf"])


class RdfXmlTest(SourceCase):
    def test_rdfxml_is_nested_by_the_stylesheet(self):
        text = self.text(self.get("/test_repo/person/1/edit?source=rdfxml"))
        self.assertNotIn(
            "rdf:nodeID", text
        )  # the content is inside the property that points to it
        self.assertRegex(text, r"<gracia:resource>\s*<foaf:Person>")
        self.assertEqual(re.findall(r"xmlns:(\w+)=", text), ["foaf", "gracia", "rdf"])

    def test_nested_rdfxml_is_the_same_data_and_can_be_saved(self):
        text = self.text(self.get("/test_repo/person/1/edit?source=rdfxml"))
        self.assertEqual(
            len(Graph.parse(text, "rdfxml")),
            len(
                Graph.parse(
                    self.text(self.get("/test_repo/person/1/edit?source=ntriples")),
                    "ntriples",
                )
            ),
        )
        got = self.post(
            "/test_repo/person/1/edit", text.replace("Ada", "Ada N"), fmt="rdfxml"
        )
        self.assertEqual(got.status_int, 302, got.text[:300])
        self.assertIn((f"{FOAF}name", "Ada N"), self.values("rec:person:1"))


class OrderTest(SourceCase):
    def test_the_same_data_is_the_same_text_with_the_type_first(self):
        first = self.text(self.get("/test_repo/person/1/edit?source=turtle"))
        self.assertEqual(
            first, self.text(self.get("/test_repo/person/1/edit?source=turtle"))
        )
        self.assertRegex(
            first, r"<rec:person:1> a gracia:Record ;"
        )  # the type, then the rest
        self.assertRegex(first, r"_:[\w-]+ a foaf:Person ;")


class JsonLdTest(SourceCase):
    def test_jsonld_is_indented_and_is_still_json_ld(self):
        text = self.text(self.get("/test_repo/person/1/edit?source=jsonld"))
        self.assertGreater(len(text.splitlines()), 10)  # not one line
        self.assertIn('\n  {\n    "@id": ', text)  # two spaces to a level
        before = Graph.parse(text, "jsonld")
        self.assertEqual(
            len(before), len(self.record().graph.without(self.record().hidden))
        )  # the same data

    def record(self):
        from gracia.main import make_config

        config = make_config(self.settings)
        config.commit()
        config.begin()
        self.addCleanup(config.end)
        node = self.application["test_repo"]
        self.addCleanup(node.close)
        return node["person"].find_record("1")


class HighlightTest(SourceCase):
    def test_the_text_is_an_editor_with_the_mode_of_its_format(self):
        for fmt, mode in (
            ("turtle", "turtle"),
            ("ntriples", "turtle"),
            ("rdfxml", "xml"),
            ("jsonld", "json"),
        ):
            with self.subTest(fmt=fmt):
                page = self.get(f"/test_repo/person/1/edit?source={fmt}").text
                self.assertIn(f'data-editor="{mode}"', page)
                self.assertIn(
                    "codemirror", page.split("</head>")[0]
                )  # shipped with the page
                self.assertIn("editor.js", page.split("</head>")[0])


class SaveTest(SourceCase):
    def test_saving_the_text_stores_the_record_like_the_form_does(self):
        text = self.edited(**{'foaf:name "Ada"': 'foaf:name "Ada Lovelace"'})
        got = self.post("/test_repo/person/1/edit", text)
        self.assertEqual(got.status_int, 302)
        self.assertEqual(
            got.headers["Location"],
            "http://localhost/test_repo/person/ada-lovelace/edit?saved=1",
        )
        stored = self.values("rec:person:1")
        self.assertIn((f"{FOAF}name", "Ada Lovelace"), stored)
        self.assertIn(
            ("https://gracia.pub/ns/label", "Ada Lovelace"), stored
        )  # the hooks ran
        self.assertIn(
            ("https://gracia.pub/ns/resource_uri", "ark:/99999/ir/person/1"), stored
        )

    def test_the_text_is_read_in_the_format_it_was_shown_in(self):
        text = self.edited(**{'foaf:name "Ada"': 'foaf:name "Ada Lovelace"'})
        got = self.post("/test_repo/person/1/edit", text, fmt="ntriples", was="turtle")
        self.assertEqual(
            got.status_int, 302
        )  # the list says ntriples, the text is turtle

    def test_any_format_can_be_saved(self):
        current = "Ada"
        for fmt in ("ntriples", "rdfxml", "jsonld"):
            with self.subTest(fmt=fmt):
                shown = self.get(f"/test_repo/person/1/edit?source={fmt}")
                text = self.text(shown).replace(current, f"Ada in {fmt}")
                got = self.post("/test_repo/person/1/edit", text, fmt=fmt)
                self.assertEqual(got.status_int, 302, got.text[:300])
                self.assertIn(
                    (f"{FOAF}name", f"Ada in {fmt}"), self.values("rec:person:1")
                )
                current = f"Ada in {fmt}"

    def test_text_that_cannot_be_parsed_stores_nothing(self):
        got = self.post("/test_repo/person/1/edit", "<rec:person:1> a gracia:Record .")
        self.assertEqual(got.status_int, 200)
        self.assertIn("This is not valid turtle", got.text)
        self.assertIn((f"{FOAF}name", "Ada"), self.values("rec:person:1"))

    def test_what_the_shape_requires_is_checked_and_nothing_is_stored_when_it_is_missing(  # noqa: E501
        self,
    ):
        text = self.edited(
            **{'foaf:name "Ada"': 'foaf:nick "Ada"'}
        )  # no name: the shape needs one
        got = self.post("/test_repo/person/1/edit", text)
        self.assertEqual(got.status_int, 200)
        self.assertRegex(got.text, r"alert-danger[^>]*>\s*name:")
        self.assertIn("foaf:nick", self.text(got))  # what was typed stays
        self.assertIn((f"{FOAF}name", "Ada"), self.values("rec:person:1"))

    def test_the_record_keeps_its_address_and_nothing_else_is_written(
        self,
    ):  # its type is in its address
        base = self.edited()
        for bad, message in (
            (
                base.replace("rec:person:1", "rec:person:7"),
                "Only the record rec:person:1",
            ),
            (
                base + '\n<http://example.org/other> <http://example.org/p> "x" .',
                "Only the record",
            ),
        ):
            with self.subTest(message=message):
                got = self.post("/test_repo/person/1/edit", bad)
                self.assertEqual(got.status_int, 200)
                self.assertIn(message, html.unescape(got.text))
        self.assertEqual(self.values("rec:person:7"), [])

    def test_the_content_has_to_stay_of_the_class_of_the_type(self):
        text = self.edited(**{"a foaf:Person": "a foaf:Prson"})
        got = self.post("/test_repo/person/1/edit", text)
        self.assertEqual(got.status_int, 200)
        self.assertRegex(
            got.text, r"alert-danger[^>]*>\s*resource:"
        )  # the property of the content
        self.assertIn("foaf:Prson", self.text(got))  # what was typed stays
        self.assertNotIn((f"{FOAF}Prson", ""), self.values("rec:person:1"))
        stored_types = [
            v
            for p, v in self.values("rec:person:1")
            if p.endswith("rdf-syntax-ns#type")
        ]
        self.assertNotIn(f"{FOAF}Prson", stored_types)  # nothing was stored

    def test_a_record_without_a_resource_uri_gets_the_ark_before_it_is_checked(self):
        text = self.edited()
        text = re.sub(r"\s*gracia:resource_uri [^;]*;", "", text)
        self.assertNotIn("resource_uri", text)
        got = self.post("/test_repo/person/1/edit", text)
        self.assertEqual(got.status_int, 302, got.text[:400])
        stored = dict(self.values("rec:person:1"))
        self.assertEqual(
            stored["https://gracia.pub/ns/resource_uri"], "ark:/99999/ir/person/1"
        )

    def test_who_may_not_edit_may_not_use_the_source_either(self):
        path = "/test_repo/person/1/edit?source=turtle"
        self.assertEqual(self.status(path, None), 401)
        self.assertEqual(self.status(path, self.editor), 200)  # an editor may


class AddTest(SourceCase):
    def text_of_new(self):
        return self.text(self.get("/test_repo/person/add?source=turtle"))

    def test_a_new_record_starts_from_what_every_record_has_under_a_placeholder(self):
        text = self.text_of_new()
        self.assertIn("rec:person:0", text)
        self.assertIn("gracia:Record", text)
        self.assertIn("foaf:Person", text)
        self.assertIn(
            "number made when it is saved",
            self.get("/test_repo/person/add?source=turtle").text,
        )

    def test_saving_makes_the_number_and_the_hooks_do_their_work(self):
        text = self.text_of_new().replace(
            "a foaf:Person", 'a foaf:Person ; foaf:name "Grace"'
        )
        got = self.post("/test_repo/person/add", text)
        self.assertEqual(got.status_int, 302, got.text[:400])
        location = got.headers["Location"]
        slug = must_search(r"/person/([a-z0-9-]+)/edit\?saved=1$", location)[1]
        self.assertEqual(slug, "grace")
        number = self.number_of(slug)
        self.assertNotEqual(number, "0")
        stored = dict(self.values(f"rec:person:{number}"))
        self.assertEqual(stored[f"{FOAF}name"], "Grace")
        self.assertEqual(stored["https://gracia.pub/ns/label"], "Grace")
        self.assertEqual(
            stored["https://gracia.pub/ns/resource_uri"],
            f"ark:/99999/ir/person/{number}",
        )

    def test_a_new_record_whose_content_is_of_another_class_is_refused(self):
        text = self.text_of_new().replace(
            "a foaf:Person", 'a foaf:Prson ; foaf:name "Grace"'
        )
        got = self.post("/test_repo/person/add", text)
        self.assertEqual(got.status_int, 200)
        self.assertRegex(got.text, r"alert-danger[^>]*>\s*resource:")

    def test_a_refused_text_does_not_use_up_a_number(self):
        got = self.post(
            "/test_repo/person/add", self.text_of_new()
        )  # no name: the shape needs one
        self.assertEqual(got.status_int, 200)
        self.assertRegex(got.text, r"alert-danger[^>]*>\s*name:")
        text = self.text_of_new().replace(
            "a foaf:Person", 'a foaf:Person ; foaf:name "Grace"'
        )
        first = self.number_of(
            must_search(
                r"/person/([a-z0-9-]+)/edit",
                self.post("/test_repo/person/add", text).headers["Location"],
            )[1]
        )
        again = self.text_of_new().replace(
            "a foaf:Person", 'a foaf:Person ; foaf:name "Alan"'
        )
        second = self.number_of(
            must_search(
                r"/person/([a-z0-9-]+)/edit",
                self.post("/test_repo/person/add", again).headers["Location"],
            )[1]
        )
        self.assertEqual(int(second), int(first) + 1)  # nothing was used between them
