import re
import tempfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

from test_html_pages import PagesCase, person

from gracia.graph import Graph
from gracia.templates import HELPERS
from gracia.types.repository import SKIN_SPECS, Repository
from gracia.types.type import HIDDEN
from gracia.utils import xslt

XSL = 'xmlns:xsl="http://www.w3.org/1999/XSL/Transform"'
# a layout of a repository that is not the one of gracia: it only says the title
LAYOUT = f"""<xsl:stylesheet version="3.0" {XSL}>
  <xsl:output method="xml"/>
  <xsl:template match="/request">
    <response><body>LAYOUT <xsl:apply-templates select="." mode="title"/></body></response>
  </xsl:template>
</xsl:stylesheet>"""
# a page that is not the one of gracia: it only says its title, the layout has the rest
PAGE = f"""<xsl:stylesheet version="3.0" {XSL}>
  <xsl:template match="/request" mode="title">MY PAGE</xsl:template>
</xsl:stylesheet>"""
# the helpers of rdf.xslt over the RDF/XML of two records, one that knows the other
GRAPH = """@prefix gracia: <https://gracia.pub/ns/> . @prefix foaf: <http://xmlns.com/foaf/0.1/> .
<rec:person:2> a gracia:Record ; gracia:label "Bob" ; gracia:slug "bob" ;
  gracia:resource_uri <ark:/1/person/2> ;
  gracia:resource [ a foaf:Person ; foaf:name "Bob" ; foaf:mbox <mailto:bob@x.org> ;
    foaf:knows <ark:/1/person/1> , <javascript:alert(1)> , <http://x.org/> ] .
<rec:person:1> a gracia:Record ; gracia:label "Ada" ; gracia:resource_uri <ark:/1/person/1> ;
  gracia:resource [ a foaf:Person ; foaf:name "Ada" ] ."""
FOAF = "http://xmlns.com/foaf/0.1/"
HELPERS_TEST = f"""<xsl:stylesheet version="3.0" {XSL} xmlns:f="https://gracia.pub/ns/functions">
  <xsl:output method="text"/>
  <xsl:template match="/request">
    <xsl:variable name="bob" select="f:record('rec:person:2', .)"/>
    <xsl:variable name="resource" select="f:resource($bob)"/>
    <xsl:variable name="knows" select="f:objects($resource, '{FOAF}knows')"/>
    <xsl:value-of select="string-join((
        f:label($bob), f:slug($bob), f:label(f:record('rec:person:1', .)),
        '[' || f:slug(f:record('rec:person:1', .)) || ']', f:number('rec:person:2'),
        f:text(f:objects($resource, '{FOAF}name')),
        string-join(f:objects($resource, '{FOAF}mbox') ! f:text(.), ','),
        string(count($knows)),
        string-join(sort(for $k in $knows return (f:link($k, 'http://r/'), '-')[1]), ' '),
        string(empty(f:record('rec:person:9', .))),
        string(empty(f:objects($resource, '{FOAF}none')))), '|')"/>
  </xsl:template>
</xsl:stylesheet>"""
# the elements of HTML5 that have no end tag
VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "track",
    "wbr",
}


class Structure(HTMLParser):
    """What can be checked of a page without a validator: that the elements are closed
    in the order they were opened (but the void ones), that the ids are the ones of one
    element, and what the head has."""

    def __init__(self):
        super().__init__()
        self.open: list[str] = []
        self.problems: list[str] = []
        self.ids: list[str] = []
        self.doctype = ""
        self.charsets = 0
        self.titles = 0

    def handle_decl(self, decl):
        self.doctype = decl

    def handle_starttag(self, tag, attrs):
        found = dict(attrs)
        self.ids += [found["id"]] if "id" in found else []
        self.charsets += 1 if tag == "meta" and "charset" in found else 0
        self.titles += 1 if tag == "title" else 0
        if tag not in VOID:
            self.open.append(tag)

    def handle_endtag(self, tag):
        if (
            tag in VOID
        ):  # a `<link ... />` of fanstatic: the slash does nothing in HTML5
            return
        if tag in self.open[-1:]:
            self.open.pop()
        else:
            self.problems.append(f"</{tag}> where {self.open[-1:]} is open")


class SkinTest(PagesCase):
    def setUp(self):
        super().setUp()
        self.add(person(7, "Bob"))
        self.tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def repository(self) -> Repository:
        repository = self.application["test_repo"]
        self.addCleanup(repository.close)
        return repository

    def stylesheet_uri(self, name: str, text: str) -> str:
        return Path(self.stylesheet(name, text)).as_uri()

    def stylesheet(self, name: str, text: str) -> str:
        path = self.tmp_path / name
        path.write_text(text)
        return str(path)

    def test_the_type_and_the_record_are_in_the_layout_of_the_site_and_a_page_in_a_frame_has_no_header_and_no_footer(
        self,
    ):
        for path, title in (
            ("/test_repo/person/", "Person"),
            ("/test_repo/person/7", "Bob"),
        ):
            text = self.get(path, self.admin).text
            self.assertIn(f"<title>{title}</title>", text, path)
            self.assertIn('<header class="container py-3 border-bottom mb-4">', text)
            self.assertIn(
                '<a class="gracia-logo mb-3" href="http://localhost/test_repo/"', text
            )
            self.assertIn('<main class="container pb-4">', text)
            self.assertIn(
                '<footer class="container py-3 border-top text-muted small">gracia</footer>',
                text,
            )
            self.assertRegex(
                text, r"(?s)<head>.*\.css.*</head>"
            )  # the styles are in it
        text = self.get("/test_repo/person/7?embed=1", self.admin).text
        self.assertNotIn("<header", text)  # a page in a frame has no header or footer
        self.assertNotIn("<footer", text)
        self.assertIn('<main class="container-fluid px-4 py-4">', text)

    def test_the_pages_are_html5_and_are_indented(self):
        self.add("".join(person(n) for n in range(10, 40)))
        for path in (
            "/test_repo/person/?page=2",
            "/test_repo/person/7",
            "/test_repo/user/1",
            "/test_repo/manage",
            "/test_repo/person/manage",
            "/test_repo/person/7/edit",
            "/test_repo/person/7/history",
            "/test_repo/jobs/",
            "/test_repo/api",
            "/test_repo/?q=bob",
        ):
            text = self.get(path, self.admin).text
            page = Structure()
            page.feed(text)
            self.assertEqual(page.doctype.lower(), "doctype html", path)
            self.assertEqual((page.open, page.problems), ([], []), path)
            self.assertEqual((page.charsets, page.titles), (1, 1), path)
            # many forms on a page: Deform numbers the fields of each from 1
            forms = "manage" in path or "jobs" in path
            if not forms:
                self.assertEqual(len(page.ids), len(set(page.ids)), path)
            self.assertRegex(text, r'<html lang="en">')
            self.assertGreater(len(text.splitlines()), 15, path)  # not one line

    def test_the_manage_page_has_its_panes_as_fragments_and_the_debug_views_show_the_input_and_the_output(
        self,
    ):
        text = self.get("/test_repo/manage?debug=input", self.admin).text
        document = ET.fromstring(text)
        names = [f.get("name") for f in document.findall("fragments/fragment")]
        self.assertIn("pane-types", names)
        self.assertEqual(len(document.findall("data/panes/item")), len(names))
        page = self.get("/test_repo/manage", self.admin).text
        self.assertIn('id="tab-types"', page)
        self.assertIn('id="types-table"', page)
        got = self.get("/test_repo/person/7?debug=output", self.admin)
        self.assertEqual(got.headers["Content-Type"], "text/plain; charset=UTF-8")
        self.assertIn("<response>", got.text)  # as the writer wrote it
        self.assertIn('<html lang="en">', got.text)
        self.assertNotIn("&lt;", got.text)

    def test_what_a_repository_has_of_its_own_is_what_its_pages_are_made_of(self):
        repository = self.repository()
        for kind in ("layout", "type", "record"):  # without any, the ones of gracia
            self.assertEqual(repository.stylesheet(kind), SKIN_SPECS[kind])
        layout = self.stylesheet("layout.xslt", LAYOUT)
        repository.set_settings({"layout_template": layout})
        self.assertEqual(repository.stylesheet("layout"), layout)
        # its layout, with the page of gracia in it: the hooks of the page are used
        self.assertEqual(
            self.get("/test_repo/person/", self.admin).text, "LAYOUT Person"
        )
        self.assertEqual(self.get("/test_repo/person/7", self.admin).text, "LAYOUT Bob")
        repository.set_settings({"layout_template": ""})  # and given up again
        self.assertIn("<header", self.get("/test_repo/person/", self.admin).text)
        # a page of its own, in the layout of gracia
        repository.set_settings(
            {
                "type_template": self.stylesheet("type.xslt", PAGE),
                "record_template": self.stylesheet("record.xslt", PAGE),
            }
        )
        for path in ("/test_repo/person/", "/test_repo/person/7"):
            self.assertIn("<title>MY PAGE</title>", self.get(path, self.admin).text)

    def test_a_stylesheet_that_is_not_there_is_not_a_setting_and_is_not_missed(self):
        repository = self.repository()
        with self.assertRaisesRegex(ValueError, "not a stylesheet that is there"):
            repository.set_settings(
                {"layout_template": str(self.tmp_path / "none.xslt")}
            )
        layout = self.stylesheet("layout.xslt", LAYOUT)
        repository.set_settings({"layout_template": layout})
        Path(layout).unlink()  # it was there when it was set, or was set by the form
        with self.assertLogs("gracia.types.repository", "WARNING") as logged:
            text = self.get("/test_repo/person/", self.admin).text
        self.assertIn("<title>Person</title>", text)  # gracia's is used, the site is up
        self.assertIn("layout stylesheet", logged.output[0])

    def test_the_helpers_jump_from_a_record_to_its_resource_and_its_properties(self):
        graph = Graph.parse(GRAPH, "turtle")
        document = f"<request><body>{graph.rdfxml(without=HIDDEN).split('?>', 1)[1]}</body></request>"
        sheet = (
            f'<xsl:stylesheet version="3.0" {XSL}>'
            f'<xsl:import href="{HELPERS.as_uri()}"/>'
            f'<xsl:import href="{self.stylesheet_uri("helpers.xslt", HELPERS_TEST)}"/>'
            "</xsl:stylesheet>"
        )
        made = xslt.Compiled(sheet).transform(document)
        self.assertEqual(
            made,
            "Bob|bob|Ada|[]|2|Bob|mailto:bob@x.org|3|"
            "- http://r/person/1 http://x.org/|true|true",  # - : javascript: is no link
        )

    def test_the_pages_say_what_they_are_made_of_to_who_manages_them_and_the_record_is_given_no_credentials(
        self,
    ):
        text = self.get("/test_repo/person/?debug=1", self.admin).text
        self.assertIn("skin/type.xslt", text)  # the page
        self.assertIn("skin/layout.xslt", text)  # and what it is put in
        self.assertIn("result: ok", text)
        public = self.get("/test_repo/person/?debug=1")
        self.assertIn("<title>Person</title>", public.text)  # for the others: the page
        document = ET.fromstring(
            self.get("/test_repo/person/?debug=input", self.admin).text
        )
        records = document.find("records")
        assert records is not None
        self.assertEqual(
            [records.get(k) for k in ("page", "pages", "total", "size")],
            ["1", "1", "2", "20"],  # Ada, and Bob
        )
        self.assertEqual(len(records.findall("record")), 2)
        params = {p.get("name"): p.text for p in document.findall("view/param")}
        self.assertEqual(
            params, {"heading": "Person", "total_published": "2", "total_all": "2"}
        )
        site = document.find("site")
        assert site is not None
        self.assertEqual(site.get("home"), "http://localhost/test_repo/")
        # the record is given the properties of its shape and never the credentials
        document = ET.fromstring(
            self.get("/test_repo/user/1?debug=input", self.admin).text
        )
        properties = document.findall("properties/property")
        self.assertTrue(properties)
        for found in properties:
            self.assertIn(found.get("role"), ("key_info", "description"))
        self.assertNotIn("credentials", ET.tostring(document, encoding="unicode"))
        page = self.get("/test_repo/user/1", self.admin).text
        self.assertIsNone(re.search(r"scrypt|\$argon|pbkdf", page))
