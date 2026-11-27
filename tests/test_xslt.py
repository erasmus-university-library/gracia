import re
import unittest
import xml.etree.ElementTree as ET

from base import DATA, RDF_FILES, DbTestCase
from harness import open_store
from support import (
    diff,
    flat_rdfxml_from_file,
    same,
    triples_from_rdfxml,
    triples_from_rows,
)

from gracia.extensions.ns_prefix import DEFAULT_PREFIXES
from gracia.graph import streaming_serialize
from gracia.utils import xslt

RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
COLLECTION = f"{{{RDF_NS}}}parseType"


def collections(rdfxml: str) -> list[ET.Element]:
    return [
        e for e in ET.fromstring(rdfxml).iter() if e.get(COLLECTION) == "Collection"
    ]


class NestedRdfXmlTest(DbTestCase):
    def nested(self, uri: str | None = None) -> str:
        with open_store(self.db) as store:
            flat = streaming_serialize(
                self.rows(store, uri),
                "rdfxml",
                prefixes=store.used_prefixes(
                    {
                        **{p["prefix"]: p["namespace"] for p in DEFAULT_PREFIXES},
                        "ex": "http://example.org/",
                    }
                ),
            )
        return xslt.nest(flat)  # ty: ignore[invalid-argument-type]

    def test_same_graph_for_every_data_file(self):
        for path in RDF_FILES:
            with self.subTest(file=path.name):
                self.db.unlink(missing_ok=True)
                self.load(path)
                with open_store(self.db) as store:
                    expected = triples_from_rows(self.rows(store))
                rdfxml = self.nested()
                ET.fromstring(rdfxml)  # well-formed
                actual = triples_from_rdfxml(rdfxml)
                self.assertTrue(
                    same(expected, actual, False), diff(expected, actual, False)
                )

    def test_lists_of_resources_become_collections_and_a_custom_stylesheet_gets_the_nested_rdfxml(
        self,
    ):
        self.load(DATA / "04_complex.ttl")
        found = collections(self.nested())
        self.assertTrue(found, "expected a parseType=Collection")
        # ex:order ( [..] [..] ) -> two node elements, and nothing but node elements
        # inside
        order = [e for e in found if e.tag == "{http://example.org/}order"]
        self.assertEqual(len(order), 1)
        self.assertEqual(len(order[0]), 2)

        sheet = self.tmp / "count.xsl"
        sheet.write_text(
            """<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
                xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
              <xsl:template match="/rdf:RDF">
                <n resources="{count(*)}"
                   collections="{count(//*[@rdf:parseType = 'Collection'])}"/>
              </xsl:template>
            </xsl:stylesheet>""",
            encoding="utf-8",
        )
        self.load(DATA / "04_complex.ttl")
        code, out, err = self.gracia("export", "-x", str(sheet))
        self.assertEqual(code, 0, err)
        result = ET.fromstring(out)
        self.assertEqual(
            result.get("resources"), "4"
        )  # doc1..doc4 (doc2/doc3 hold their bnode)
        self.assertEqual(result.get("collections"), "2")

    def test_lists_with_literals_stay_explicit_typed_nodes_shared_bnodes_and_one_resource_only(
        self,
    ):
        self.load(DATA / "03_lists.ttl")
        root = ET.fromstring(self.nested())
        tags = next(e for e in root.iter("{http://example.org/}tags"))
        self.assertIsNone(tags.get(COLLECTION))
        self.assertTrue(list(tags.iter(f"{{{RDF_NS}}}first")))

        self.load(DATA / "01_simple.ttl")
        rdfxml = self.nested()
        self.assertIn("<ex:Book", rdfxml)  # rdf:type -> element name
        self.assertIn('xml:lang="fr"', rdfxml)
        self.assertIn("XMLSchema#integer", rdfxml)

        flat = flat_rdfxml_from_file(DATA / "04_complex.ttl")  # _:shared is used twice
        refs = [
            e
            for e in ET.fromstring(xslt.nest(flat)).iter()
            if e.get(f"{{{RDF_NS}}}nodeID")
        ]
        self.assertGreaterEqual(len(refs), 3)  # one definition + two references

        rdfxml = self.nested("rec:person:1")
        self.assertIn("Alice", rdfxml)
        self.assertNotIn("The Title", rdfxml)


class PrefixesInXsltTest(DbTestCase):
    with_record = True

    def test_the_prefixes_of_the_repository_are_used_an_option_overrides_them_and_an_unknown_namespace_is_well_formed(
        self,
    ):
        self.load(DATA / "01_simple.ttl")
        code, out, err = self.gracia("export", "-f", "rdfxml-collection")
        self.assertEqual(code, 0, err)
        self.assertIn("<ex:Book", out)
        self.assertIn("<dcterms:title", out)

        self.load(DATA / "01_simple.ttl")
        _, out, _ = self.gracia(
            "export", "-f", "rdfxml-collection", "-p", "ex=http://nowhere.org/"
        )
        self.assertIn('xmlns:ex="http://nowhere.org/"', out)

        path = self.tmp / "u.nt"
        path.write_text(
            '<rec:thing:1> <http://unknown.org/ns#p> "v" .\n', encoding="utf-8"
        )
        self.load(path)
        _, out, _ = self.gracia("export", "-f", "rdfxml-collection")
        root = ET.fromstring(out)
        self.assertTrue(any(e.tag == "{http://unknown.org/ns#}p" for e in root.iter()))


class RecordUriParamTest(unittest.TestCase):
    FLAT = (
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns:ex="http://ex/">'
        '<rdf:Description rdf:about="rec:a:1"><ex:p>one</ex:p></rdf:Description>'
        '<rdf:Description rdf:about="rec:a:2"><ex:p>two</ex:p></rdf:Description>'
        "<rdf:Description "
        'rdf:about="rec:a:3"><ex:p>three</ex:p></rdf:Description></rdf:RDF>'
    )

    def order(self, **params) -> list[str]:
        text = xslt.nest(self.FLAT, params or None)
        return re.findall(r'rdf:about="([^"]+)"', text)

    def test_the_record_comes_first_and_the_others_keep_their_order(self):
        self.assertEqual(
            self.order(), ["rec:a:1", "rec:a:2", "rec:a:3"]
        )  # no parameter
        self.assertEqual(
            self.order(record_uri="rec:a:3"), ["rec:a:3", "rec:a:1", "rec:a:2"]
        )
        self.assertEqual(
            self.order(record_uri="rec:a:2"), ["rec:a:2", "rec:a:1", "rec:a:3"]
        )
        self.assertEqual(
            self.order(record_uri="rec:none:1"), ["rec:a:1", "rec:a:2", "rec:a:3"]
        )


class TypeFilesTest(unittest.TestCase):
    def test_an_address_that_two_stylesheets_name_is_resolved_once(self):
        import tempfile
        from pathlib import Path

        asked = []
        with tempfile.TemporaryDirectory() as folder:
            shared = Path(folder) / "shared.xsl"
            shared.write_text(f'<xsl:stylesheet version="3.0" xmlns:xsl="{xslt.XSL}"/>')

            def resolve(href):
                asked.append(href)
                return shared

            one = (
                f'<xsl:stylesheet xmlns:xsl="{xslt.XSL}">'
                '<xsl:import href="type:a/x.xsl"/></xsl:stylesheet>'
            )
            two = one  # a second stylesheet that names the same address
            found = xslt.type_files([one, two], resolve)
        self.assertEqual(list(found), ["type:a/x.xsl"])
        self.assertEqual(asked, ["type:a/x.xsl"])
