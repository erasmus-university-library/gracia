import io
import sys
from html.parser import HTMLParser

from base import DATA, DbTestCase
from support import flat_rdfxml_from_file

from gracia.utils import xslt


class Page(HTMLParser):
    """Collects what the tests care about from the generated page."""

    def __init__(self, html: str):
        super().__init__()
        self.tags: list[str] = []
        self.links: list[str] = []
        self.ids: list[str] = []
        self.text: list[str] = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        attrs = dict(attrs)
        if tag == "a" and "href" in attrs:
            self.links.append(attrs["href"])
        if "id" in attrs:
            self.ids.append(attrs["id"])

    def handle_data(self, data):
        if data.strip():
            self.text.append(" ".join(data.split()))


def html_for(path, **params: str) -> str:
    """The graph of an RDF file as an HTML page (shared bnodes stay shared)."""
    return xslt.render("html-table", flat_rdfxml_from_file(path), params or None)


class BundledFilesTest(DbTestCase):
    def test_every_stylesheet_the_code_names_exists(self):
        names = set(xslt.bundled_names())
        for filename in (
            xslt.NESTED,
            *(s for sheets in xslt.FORMATS.values() for s in sheets),
        ):
            self.assertIn(filename, names)


class HtmlTableTest(DbTestCase):
    def test_page_for_a_database(self):
        self.load(DATA / "01_simple.ttl")
        code, out, err = self.gracia(
            "export", "-f", "html-table", "-p", "ex=http://example.org/"
        )
        self.assertEqual(code, 0, err)
        page = Page(out)
        self.assertTrue(out.lower().startswith("<!doctype html>"))
        self.assertEqual(page.tags.count("section"), 2)  # alice and book1
        self.assertIn("rec:book:1", page.links)
        self.assertIn(
            "ex:Book", page.text
        )  # type, as prefixed name from the --prefix option
        self.assertIn("dcterms:title", page.text)
        self.assertIn("@fr", page.text)  # language note
        self.assertIn("^^integer", page.text)  # datatype note

    def test_nested_blank_nodes_and_collections(self):
        self.load(DATA / "04_complex.ttl")
        _, out, _ = self.gracia("export", "-f", "html-table", "-u", "rec:doc:1")
        page = Page(out)
        self.assertEqual(page.tags.count("section"), 1)
        self.assertGreater(
            page.tags.count("table"), 3
        )  # doc1, the part bnode, order items
        self.assertEqual(page.tags.count("ol"), 1)  # ex:order ( [..] [..] )
        self.assertEqual(page.tags.count("li"), 2)

    def test_shared_blank_node_links_to_its_section(self):
        """Raw json-triples with a bnode used twice: written once, linked from both
        uses."""
        html = html_for(DATA / "04_complex.ttl")
        page = Page(html)
        targets = [link for link in page.links if link.startswith("#b-")]
        self.assertEqual(len(targets), 2)  # ex:doc2 and ex:doc3 both point at _:shared
        self.assertEqual(len(set(targets)), 1)
        self.assertIn(targets[0][1:], page.ids)

    def test_title_parameter_and_escaping(self):
        path = self.write("e.ttl", '<rec:thing:1> ex:p "<b>bold</b> & more" .\n')
        self.load(path)
        _, out, _ = self.gracia("export", "-f", "html-table")
        self.assertIn("&lt;b&gt;bold&lt;/b&gt; &amp; more", out)  # never raw markup
        self.assertNotIn("<b>", out)
        page = html_for(path, title="My <data>")
        self.assertIn("<title>My &lt;data&gt;</title>", page)

    def test_empty_database(self):
        self.gracia(
            "import", str(self.write("empty.ttl", ""))
        )  # creates an empty database
        code, out, err = self.gracia("export", "-f", "html-table")
        self.assertEqual(code, 0, err)
        self.assertIn("0 resource(s)", out)


class TransformCommandTest(DbTestCase):
    def test_transform_file_with_bundled_stylesheet(self):
        self.load(DATA / "01_simple.ttl")
        _, rdfxml, _ = self.gracia("export", "-f", "rdfxml-collection")
        source = self.tmp / "in.rdf"
        source.write_text(rdfxml, encoding="utf-8")
        out_file = self.tmp / "out.html"
        code, _, err = self.gracia(
            "transform", "rdfxml-to-html.xsl", str(source), "-o", str(out_file)
        )
        self.assertEqual(code, 0, err)
        self.assertIn("<table", out_file.read_text(encoding="utf-8"))

    def test_transform_stdin_and_param(self):
        self.load(DATA / "01_simple.ttl")
        _, rdfxml, _ = self.gracia("export", "-f", "rdfxml-collection")
        stdin, sys.stdin = sys.stdin, io.StringIO(rdfxml)
        try:
            code, out, err = self.gracia(
                "transform", "rdfxml-to-html.xsl", "--param", "title=Piped"
            )
        finally:
            sys.stdin = stdin
        self.assertEqual(code, 0, err)
        self.assertIn("<h1>Piped</h1>", out)

    def test_errors_are_clean(self):
        source = self.tmp / "in.xml"
        source.write_text("<a/>", encoding="utf-8")
        for args, needle in [
            (["transform", str(self.tmp / "nope.xsl"), str(source)], "nope.xsl"),
            (
                ["transform", "rdfxml-to-html.xsl", str(self.tmp / "missing.xml")],
                "missing.xml",
            ),
            (["transform", "rdfxml-to-html.xsl", "--param", "oops"], "NAME=VALUE"),
        ]:
            with self.subTest(args=args[1:]):
                code, _, err = self.gracia(*args)
                self.assertEqual(code, 1)
                self.assertTrue(err.startswith("gracia:"), err)
                self.assertIn(needle, err)

    def test_no_input_on_a_terminal_does_not_wait(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        stdin, sys.stdin = sys.stdin, Terminal()
        try:
            code, _, err = self.gracia("transform", "rdfxml-to-html.xsl")
        finally:
            sys.stdin = stdin
        self.assertEqual(code, 1)
        self.assertIn("no input", err)

    def test_bad_xml_input(self):
        bad = self.tmp / "bad.xml"
        bad.write_text("<not xml", encoding="utf-8")
        code, _, err = self.gracia("transform", "rdfxml-to-html.xsl", str(bad))
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("gracia:"), err)

    def test_bad_stylesheet_in_export(self):
        self.load(DATA / "01_simple.ttl")
        sheet = self.tmp / "bad.xsl"
        sheet.write_text("<xsl:stylesheet", encoding="utf-8")
        code, _, err = self.gracia("export", "-x", str(sheet))
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("gracia:"), err)
