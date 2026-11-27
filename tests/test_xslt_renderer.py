import logging
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from pyramid import testing
from pyramid.renderers import render_to_response
from pyramid.security import NO_PERMISSION_REQUIRED
from test_manage import ADMIN, ManageCase

from gracia import templates
from gracia.main import make_config
from gracia.types.repository import Repository
from gracia.utils import assets, xslt

RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF"
RDFXML = '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>'
XSL = 'xmlns:xsl="http://www.w3.org/1999/XSL/Transform"'


def response_of(select: str, headers: str = "") -> str:
    """A stylesheet whose response has the value of an XPath for its body."""
    return f"""<xsl:stylesheet version="3.0" {XSL}>
  <xsl:output method="xml"/>
  <xsl:template match="/request">
    <response>
      <headers>{headers}</headers>
      <body><xsl:value-of select="{select}"/></body>
    </response>
  </xsl:template>
</xsl:stylesheet>"""


# says what the view gave its page
STYLESHEET = response_of("'Hi ' || string-join(view/param/(@name || '=' || .), ',')")
# says what is in the body and what the listing used
DATA = response_of(
    "string-join((name(body/*), records/@page, records/record/@uri), ' ')"
)
COMMON = f"""<xsl:stylesheet version="3.0" {XSL}>
  <xsl:template name="who">common</xsl:template>
</xsl:stylesheet>"""
MAIN = f"""<xsl:stylesheet version="3.0" {XSL}>
  <xsl:import href="common.xslt"/>
  <xsl:output method="xml"/>
  <xsl:template match="/">
    <response><body>Hello <xsl:call-template name="who"/></body></response>
  </xsl:template>
</xsl:stylesheet>"""
# says something while it works
MESSAGES = f"""<xsl:stylesheet version="3.0" {XSL}>
  <xsl:output method="xml"/>
  <xsl:template match="/request">
    <xsl:message>careful: <xsl:value-of select="view/@name"/>work</xsl:message>
    <xsl:message>second</xsl:message>
    <response><body>done</body></response>
  </xsl:template>
</xsl:stylesheet>"""
# a form that is HTML and not XML: what a browser takes, and a view may make
FORM = """<form action="/x?a=1&b=2"><input type="text" readonly name="q"><br>
<textarea name="t"> keep  spaces </textarea><uri-select data-lookup="/l?x=1&y=2"></uri-select>
<p>caf&eacute;&nbsp;<b>bold<i>both</b></i><script>if (a < b && c) { x = "$1 ]]>"; }</script></form>"""
# the fragment that was given, put in the page by the html5 writer
FRAGMENT = f"""<xsl:stylesheet version="3.0" {XSL}>
  <xsl:output method="html" html-version="5" indent="no" include-content-type="no"/>
  <xsl:template match="/request">
    <response><body><div><xsl:copy-of select="fragments/fragment[@name = 'form']/node()"/></div></body></response>
  </xsl:template>
</xsl:stylesheet>"""
# markup in the body, written by the html5 writer
MARKUP = f"""<xsl:stylesheet version="3.0" {XSL}>
  <xsl:output method="html" html-version="5" indent="no"/>
  <xsl:template match="/request">
    <response>
      <headers><Content-Type>text/html</Content-Type></headers>
      <body><p>a &amp; b<br/></p></body>
    </response>
  </xsl:template>
</xsl:stylesheet>"""
# the parameters of a stylesheet that is given the XML of something else
TYPED = f"""<xsl:stylesheet version="3.0" {XSL} xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xsl:output method="text"/>
  <xsl:param name="greeting" select="'Hi'"/>
  <xsl:param name="count" as="xs:integer" select="0"/>
  <xsl:param name="ratio" as="xs:double" select="1"/>
  <xsl:param name="flag" as="xs:boolean" select="false()"/>
  <xsl:param name="request" as="document-node()?" select="()"/>
  <xsl:template match="/">
    <xsl:value-of select="$greeting, $count + 1, $ratio * 2, $flag, $request/request/@method"/>
  </xsl:template>
</xsl:stylesheet>"""


def found(element, path: str):
    """The element at a path below another: it has to be there."""
    child = element.find(path)
    assert child is not None, path
    return child


class Node:
    """A node of a tree, for a request that has no application."""

    def __init__(self, name, parent=None, uri=None):
        self.__name__, self.__parent__ = name, parent
        if uri:
            self.uri = uri


class DebugViewTest(ManageCase):
    def test_the_debug_view_is_a_stylesheet_run_over_the_request(self):
        got = self.client().get("/test_repo/debug", headers=ADMIN)
        self.assertEqual((got.status_int, got.text), (200, "Hello"))
        self.assertEqual(got.content_type, "text/plain")  # the stylesheet says so

    def test_debug_input_shows_the_document_the_stylesheet_is_given(self):
        got = self.client().get("/test_repo/debug?debug=input&x=1", headers=ADMIN)
        self.assertEqual(got.status_int, 200)
        self.assertEqual(got.headers["Content-Type"], "application/xml; charset=UTF-8")
        root = ET.fromstring(got.text)
        self.assertEqual(root.tag, "request")
        self.assertEqual(
            (root.get("method"), root.get("path"), root.get("user")),
            ("GET", "/test_repo/debug", "admin"),
        )
        self.assertEqual(
            root.get("url"), "http://localhost/test_repo/debug?debug=input&x=1"
        )
        context = found(root, "context")
        self.assertEqual(
            (context.get("kind"), context.get("name"), context.get("record_uri")),
            ("Repository", "test_repo", "rec:repository:1"),
        )
        # every node up to the root, the root first, each with its address
        nodes = [
            (n.get("kind"), n.get("name"), n.get("url"))
            for n in context.findall("node")
        ]
        self.assertEqual(
            nodes,
            [
                ("Application", "", "http://localhost/"),
                ("Repository", "test_repo", "http://localhost/test_repo/"),
            ],
        )
        view = found(root, "view")  # and what the view gave its page
        self.assertEqual(view.get("name"), "debug")
        self.assertEqual(
            [(p.get("name"), p.text) for p in view.findall("param")],
            [("heading", "Debug")],  # `rdfxml` and `csrf` are not values of the page
        )
        self.assertIn(
            "manage", [p.text for p in root.findall("permissions/permission")]
        )
        self.assertEqual(root.findall("headers/header"), [])  # no Accept was sent
        self.assertEqual(
            [(p.get("name"), p.text) for p in root.findall("query/param")],
            [("debug", "input"), ("x", "1")],
        )
        self.assertTrue(root.findtext("csrf-token"))
        rdf = found(root, f"body/{RDF}")
        self.assertIn("rec:repository:1", ET.tostring(rdf, encoding="unicode"))

    def test_debug_output_shows_the_response_the_stylesheet_makes(self):
        got = self.client().get("/test_repo/debug?debug=output", headers=ADMIN)
        self.assertEqual(got.status_int, 200)
        self.assertEqual(got.headers["Content-Type"], "text/plain; charset=UTF-8")
        self.assertIn("<response>", got.text)  # the envelope, as written
        self.assertIn("<Content-Type>text/plain</Content-Type>", got.text)
        self.assertIn("<body>Hello</body>", got.text)

    def test_debug_xslt_shows_the_stylesheet(self):
        got = self.client().get("/test_repo/debug?debug=xslt", headers=ADMIN)
        self.assertEqual(got.status_int, 200)
        self.assertTrue(got.headers["Content-Type"].startswith("application/xslt+xml"))
        self.assertEqual(
            got.text, assets.read_text("gracia:assets/xslt/debug.xslt")
        )  # what the file says, not what it makes

    def test_debug_stylesheet_is_debug_xslt(self):
        client = self.client()
        same = client.get("/test_repo/debug?debug=xslt", headers=ADMIN)
        alias = client.get("/test_repo/debug?debug=stylesheet", headers=ADMIN)
        self.assertEqual(alias.text, same.text)
        self.assertEqual(alias.headers["Content-Type"], same.headers["Content-Type"])

    def test_debug_anything_is_a_text_page_that_says_what_the_switches_are(self):
        got = self.client().get("/test_repo/debug?debug=1", headers=ADMIN)
        self.assertEqual(got.status_int, 200)
        self.assertEqual(got.headers["Content-Type"], "text/plain; charset=UTF-8")
        self.assertEqual(got.headers["Cache-Control"], "no-store")
        self.assertTrue(got.text.startswith("Debug switches of this page"))
        for switch in ("input", "xslt", "stylesheet", "output", "<anything>"):
            self.assertIn(f"?debug={switch}", got.text)
        self.assertIn(
            "result: ok: text/plain; charset=UTF-8, a body of 5 char", got.text
        )
        self.assertIn("debug.xslt", got.text)  # which stylesheet it was
        for phase in ("the document of the request is made", "the stylesheet is run"):
            self.assertRegex(got.text, rf"\d+\.\d\d  {phase}")  # how long, in ms
        self.assertRegex(got.text, r"\d+\.\d\d  all of it")
        # the same for what is not a word: it is the page of the run
        empty = self.client().get("/test_repo/debug?debug=", headers=ADMIN)
        self.assertTrue(empty.text.startswith("Debug switches of this page"))
        self.assertIn("result: ok", empty.text)

    def test_only_who_manages_the_repository_may_see_it(self):
        for headers in ({}, {"X-User": "nobody"}):
            status = (
                self.client()
                .get("/test_repo/debug", headers=headers, expect_errors=True)
                .status_int
            )
            self.assertIn(status, (401, 403), headers)


class RendererCase(ManageCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "hi.xslt"
        self.path.write_text(STYLESHEET)
        self.addCleanup(self.forget)

    def forget(self):
        for path in list(templates._STYLESHEETS):
            if path.startswith(str(self.directory)):
                templates._STYLESHEETS.pop(path)[1].close()

    def render(self, values, path=None, reload=False, permissive=True, **params):
        """What a view gets from `render_to_response`: the response."""
        config = testing.setUp(settings={"pyramid.reload_templates": reload})
        self.addCleanup(testing.tearDown)
        config.testing_securitypolicy(userid="ada", permissive=permissive)
        config.add_renderer(".xslt", templates.XsltRenderer)
        request = testing.DummyRequest(params=params, path="/work/3")
        request.context = Node("work", Node(None), uri="rec:type:7")
        return render_to_response(str(path or self.path), values, request=request)


class DebugPermissionTest(RendererCase):
    def test_the_debug_modes_are_for_who_manages_whatever_the_permission_of_the_view(
        self,
    ):
        def plain(request):
            return {"greeting": "Hello"}

        config = make_config(self.settings)
        config.add_view(  # anyone may open it: only the renderer protects the modes
            plain,
            context=Repository,
            name="plain",
            request_method="GET",
            permission=NO_PERMISSION_REQUIRED,
            renderer=str(self.path),
        )
        client = self.client(config)
        normal = "Hi greeting=Hello"
        for headers in ({}, {"X-User": "nobody"}):  # not the admin
            self.assertEqual(
                client.get("/test_repo/plain", headers=headers).text, normal
            )
            for debug in ("input", "xslt", "stylesheet", "output", "anything"):
                got = client.get(f"/test_repo/plain?debug={debug}", headers=headers)
                self.assertEqual(got.text, normal, (headers, debug))
                self.assertNotIn("no-store", got.headers.get("Cache-Control", ""))
        for debug, content_type in (
            ("input", "application/xml"),
            ("xslt", "application/xslt+xml"),
            ("stylesheet", "application/xslt+xml"),
            ("output", "text/plain"),
            ("anything", "text/plain"),
        ):
            got = client.get(f"/test_repo/plain?debug={debug}", headers=ADMIN)
            self.assertNotEqual(got.text, normal, debug)
            self.assertTrue(got.headers["Content-Type"].startswith(content_type), debug)
            self.assertEqual(got.headers["Cache-Control"], "no-store")  # not kept


class XsltRendererTest(RendererCase):
    def test_the_values_of_the_view_are_in_the_document_as_they_are_given(self):
        response = self.render({"greeting": "Hello", "count": 9, "flag": True})
        self.assertEqual(response.text, "Hi greeting=Hello,count=9,flag=true")
        self.assertEqual(response.content_type, "text/html")  # the stylesheet says none
        self.assertEqual(self.render({}).text, "Hi")
        with self.assertRaisesRegex(TypeError, "the value count of the view is a list"):
            self.render({"count": [1]})

    def test_the_data_and_what_a_listing_used_are_in_the_document(self):
        path = self.directory / "data.xslt"
        path.write_text(DATA)
        values = {
            "rdfxml": RDFXML,
            "records": ["rec:work:41", "rec:work:42"],
            "paging": {"page": 2, "pages": 3},
        }
        response = self.render(values, path)
        self.assertEqual(response.text, "rdf:RDF 2 rec:work:41 rec:work:42")

    def test_a_stylesheet_finds_the_files_it_imports_beside_it(self):
        (self.directory / "common.xslt").write_text(COMMON)
        main = self.directory / "main.xslt"
        main.write_text(MAIN)
        self.assertEqual(self.render({}, main).text, "Hello common")

    def test_with_reload_an_edit_of_an_imported_file_is_seen_too(self):
        (self.directory / "common.xslt").write_text(COMMON)
        main = self.directory / "main.xslt"
        main.write_text(MAIN)
        self.assertEqual(self.render({}, main, reload=True).text, "Hello common")
        common = self.directory / "common.xslt"
        common.write_text(COMMON.replace(">common<", ">changed<"))
        later = common.stat().st_mtime_ns + 10**9
        os.utime(common, ns=(later, later))
        self.assertEqual(self.render({}, main, reload=True).text, "Hello changed")

    def test_a_fragment_of_html_is_put_in_the_page_as_the_browser_takes_it(self):
        path = self.directory / "fragment.xslt"
        path.write_text(FRAGMENT)
        text = self.render({"fragments": {"form": FORM}}, path).text
        self.assertIn('<form action="/x?a=1&amp;b=2">', text)
        self.assertIn('<input type="text" readonly name="q"><br>', text)
        self.assertIn('<textarea name="t"> keep  spaces </textarea>', text)  # as typed
        self.assertIn('<uri-select data-lookup="/l?x=1&amp;y=2"></uri-select>', text)
        self.assertIn("café&nbsp;<b>bold<i>both</i></b>", text)  # what was not nested
        self.assertIn('<script>if (a < b && c) { x = "$1 ]]>"; }</script>', text)

    def test_the_site_and_the_layout_are_what_the_pages_have_around_them(self):
        page = self.directory / "page.xslt"
        page.write_text(
            f"""<xsl:stylesheet version="3.0" {XSL}>
              <xsl:template match="/request" mode="content"><p>hello</p></xsl:template>
            </xsl:stylesheet>"""
        )
        site = {
            "home": "http://localhost/r/",
            "favicon": "http://localhost/icon.svg",
            "embed": False,
            "flashes": [("success", "Saved & done"), ("danger", "Not <saved>")],
        }
        values = {"layout": "gracia:assets/xslt/skin/layout.xslt", "site": site}
        text = self.render({**values, "heading": "A <page>"}, page).text
        self.assertIn("<title>A &lt;page&gt;</title>", text)  # the heading is the title
        self.assertIn(
            '<link rel="icon" type="image/svg+xml" href="http://localhost/icon.svg">',
            text,
        )
        self.assertIn('<a class="gracia-logo mb-3" href="http://localhost/r/"', text)
        self.assertIn('<h1 class="h3 mb-0">A &lt;page&gt;</h1>', text)  # the header
        self.assertIn(
            '<div class="alert alert-success alert-dismissible fade show"', text
        )
        self.assertIn("Saved &amp; done", text)
        self.assertIn("Not &lt;saved&gt;", text)
        self.assertIn('<main class="container pb-4">', text)
        self.assertIn("<p>hello</p>", text)  # what the page fills in
        self.assertIn(">gracia</footer>", text)
        framed = self.render({**values, "site": {**site, "embed": True}}, page).text
        self.assertNotIn("<header", framed)
        self.assertNotIn("<footer", framed)
        self.assertIn('<main class="container-fluid px-4 py-4">', framed)

    def test_render_to_response_gives_a_response_to_change_before_it_is_returned(self):
        response = self.render({"greeting": "Hello"})
        response.content_type = "text/plain"  # as the view may
        self.assertEqual(response.content_type, "text/plain")

    def test_the_headers_of_the_response_are_the_ones_the_stylesheet_makes(self):
        headers = (
            "<Content-Type>application/xml; charset=latin-1</Content-Type>"
            "<Cache-Control>private</Cache-Control>"
        )
        path = self.directory / "headers.xslt"
        path.write_text(
            response_of("'x'", headers).replace(
                "<body><xsl:value-of select=\"'x'\"/></body>", "<body><a/></body>"
            )
        )
        response = self.render({}, path)
        self.assertEqual(
            response.headers["Content-Type"], "application/xml; charset=UTF-8"
        )
        self.assertEqual(response.headers["Cache-Control"], "private")
        self.assertEqual(response.text, "<a/>")  # the body is what the writer wrote

    def test_a_response_the_stylesheet_may_not_make_is_an_error(self):
        wrong = {
            "<Set-Cookie>a=b</Set-Cookie>": "may not set the header Set-Cookie",
        }
        for header, message in wrong.items():
            path = self.directory / "wrong.xslt"
            path.write_text(response_of("'x'", header))
            with self.assertRaisesRegex(xslt.XsltError, message):
                self.render({}, path)
            self.forget()
        for text, message in (
            ("<xsl:value-of select=\"'Hello'\"/>", "does not make a <response>"),
            ("<page/>", "makes a <page>, not a <response>"),
            ("<response/>", "has no <body>"),
        ):
            sheet = f"""<xsl:stylesheet version="3.0" {XSL}><xsl:output method="xml"/>
              <xsl:template match="/">{text}</xsl:template></xsl:stylesheet>"""
            path = self.directory / "wrong.xslt"
            path.write_text(sheet)
            with self.assertRaisesRegex(xslt.XsltError, message):
                self.render({}, path)
            self.forget()

    def test_markup_in_the_body_is_written_by_the_writer_of_the_stylesheet(self):
        path = self.directory / "markup.xslt"
        path.write_text(MARKUP)
        response = self.render({}, path)
        self.assertEqual(response.text, "<p>a &amp; b<br></p>")
        self.assertEqual(response.content_type, "text/html")

    def test_a_stylesheet_is_compiled_once_and_again_when_it_was_edited_and_reload_is_on(
        self,
    ):
        for reload, expected in ((False, "Hi"), (True, "Bye")):
            self.assertEqual(self.render({}, reload=reload).text, "Hi")
            first = templates._STYLESHEETS[str(self.path)][1]
            self.render({}, reload=reload)
            self.assertIs(templates._STYLESHEETS[str(self.path)][1], first)  # once
            self.path.write_text(self.path.read_text().replace("Hi", "Bye"))
            later = self.path.stat().st_mtime_ns + 10**9  # a change that shows
            os.utime(self.path, ns=(later, later))
            self.assertEqual(self.render({}, reload=reload).text, expected, reload)
            self.path.write_text(self.path.read_text().replace("Bye", "Hi"))
            self.forget()

    def test_the_debug_page_has_the_messages_the_log_records_and_the_error(self):
        path = self.directory / "noisy.xslt"
        path.write_text(MESSAGES)
        noisy = patch.object(
            templates.XsltRenderer,
            "_stylesheet",
            autospec=True,
            side_effect=self.logging_stylesheet(templates.XsltRenderer._stylesheet),
        )
        with noisy:
            page = self.render({}, path, debug="x").text
        self.assertIn("result: ok", page)
        self.assertIn("  careful: work", page)  # the xsl:message output, of the run
        self.assertIn("  second", page)
        self.assertIn("  WARNING gracia.test: something odd", page)  # what was logged
        self.assertNotIn("error:", page)
        # an error is in the page, not raised, with where it was
        path.write_text(
            MESSAGES.replace("<response>", "<page>").replace("</response>", "</page>")
        )
        self.forget()
        page = self.render({}, path, debug="x").text
        self.assertIn("result: FAILED in the response of the stylesheet", page)
        self.assertIn("makes a <page>, not a <response>", page)
        self.assertIn("error:\nTraceback", page)
        self.assertIn("  careful: work", page)  # what was said before it
        # a stylesheet that does not compile
        path.write_text("<xsl:stylesheet")
        self.forget()
        page = self.render({}, path, debug="x").text
        self.assertIn("result: FAILED in the stylesheet", page)
        self.assertIn("XsltError", page)
        # an error in the document that is made of the request: in its step
        page = self.render({"count": [1]}, self.path, debug="x").text
        self.assertIn("result: FAILED in the document of the request", page)
        self.assertIn("the value count of the view is a list", page)

    def test_the_debug_page_times_the_steps_and_lists_the_files_it_is_made_of(self):
        (self.directory / "common.xslt").write_text(COMMON)
        main = self.directory / "main.xslt"
        main.write_text(
            MAIN.replace(
                "</xsl:template>",
                "<xsl:value-of select=\"doc('x.xml')\"/></xsl:template>",
                1,
            )
        )
        (self.directory / "x.xml").write_text("<x/>")
        page = self.render({}, main, debug="x").text
        self.assertRegex(
            page, r"\d+\.\d\d  the stylesheet is compiled"
        )  # the first time
        for step in ("parsed", "run", "read"):
            self.assertRegex(page, rf"\d+\.\d\d  the \w+ is {step}")
        files = page.split("files the stylesheet is made of")[1].split(
            "calls that may"
        )[0]
        self.assertIn(f"{main}  (", files)
        self.assertIn(f"{self.directory / 'common.xslt'}  (", files)  # what it imports
        self.assertIn("main.xslt:6: doc(", page)  # a call that may load a file
        again = self.render({}, main, debug="x").text
        self.assertIn("cached  the stylesheet was compiled before", again)
        (self.directory / "common.xslt").unlink()  # an import that is not there
        self.forget()
        page = self.render({}, main, debug="x").text
        self.assertIn("(there is no such file)", page)
        self.assertIn("result: FAILED in the stylesheet", page)

    def logging_stylesheet(self, real):
        def stylesheet(renderer, path, layout=None, timings=None):
            logging.getLogger("gracia.test").warning("something odd")
            return real(renderer, path, layout, timings)

        return stylesheet

    def test_debug_input_xslt_and_output_are_shown_only_to_who_manages(self):
        stylesheet = self.path.read_text()
        for debug in ("input", "xslt", "stylesheet", "output", "anything"):
            for allowed in (True, False):
                response = self.render(
                    {"greeting": "Hello"}, permissive=allowed, debug=debug
                )
                if not allowed:
                    expected = "Hi greeting=Hello"
                    self.assertEqual(response.text, expected, debug)
                elif debug in ("xslt", "stylesheet"):
                    self.assertEqual(response.text, stylesheet)
                elif debug == "anything":
                    self.assertIn("result: ok", response.text)
                elif debug == "output":
                    self.assertIn("<response>", response.text)
                else:
                    document = ET.fromstring(response.text)
                    self.assertEqual(found(document, "view/param").text, "Hello")


class RequestXmlTest(unittest.TestCase):
    def request(self, context, permissive=False, **params):
        config = testing.setUp()
        self.addCleanup(testing.tearDown)
        config.testing_securitypolicy(userid="ada", permissive=permissive)
        request = testing.DummyRequest(params=params, path="/work/3")
        request.context = context
        return request

    def test_the_records_and_the_paging_of_a_listing_are_in_the_document(self):
        root = Node(None)
        work = Node("work", root, uri="rec:type:7")
        request = self.request(work, page="2")
        uris = ["rec:work:41", "rec:work:42"]
        paging = {"page": 2, "pages": 3, "total": 45, "size": 20}
        text = templates.XsltRenderer.request_xml(
            request, "<?xml version='1.0'?><rdf:RDF xmlns:rdf='urn:x'/>", uris, paging
        )
        document = ET.fromstring(text)  # well-formed: the declaration is not repeated
        self.assertEqual(len(found(document, "body")), 1)  # the data is its one element
        records = found(document, "records")
        self.assertEqual(
            [(k, records.get(k)) for k in ("page", "pages", "total", "size")],
            [("page", "2"), ("pages", "3"), ("total", "45"), ("size", "20")],
        )
        self.assertEqual([r.get("uri") for r in records.findall("record")], uris)
        self.assertEqual(found(document, "context").get("record_uri"), "rec:type:7")
        self.assertEqual(document.get("user"), "ada")
        self.assertEqual(document.findall("permissions/permission"), [])
        self.assertIsNone(document.find("csrf-token"))  # only when it is asked for
        plain = ET.fromstring(templates.XsltRenderer.request_xml(request))
        self.assertIsNone(plain.find("records"))  # no list, no paging: no element

    def test_fragments_are_html_that_lxml_makes_into_markup(self):
        request = self.request(Node("work", Node(None)))
        document = ET.fromstring(
            templates.XsltRenderer.request_xml(
                request, fragments={"form": FORM, "text": "a & b <i>c</i>"}
            )
        )  # well-formed whatever the HTML was
        names = [f.get("name") for f in document.findall("fragments/fragment")]
        self.assertEqual(names, ["form", "text"])
        form = found(document, "fragments/fragment/form")
        self.assertEqual(found(form, "input").get("readonly"), "readonly")  # a bare one
        self.assertEqual(found(form, "textarea").text, " keep  spaces ")
        self.assertEqual(
            found(form, "p/script").text, 'if (a < b && c) { x = "$1 ]]>"; }'
        )
        self.assertEqual(found(form, "p/b/i").text, "both")  # b and i were mis-nested
        text = found(document, "fragments/fragment[@name='text']")
        self.assertEqual((text.text, found(text, "i").text), ("a & b ", "c"))

    def test_what_has_structure_is_data_with_an_element_for_each_key_and_item(self):
        data = {
            "revisions": [{"txn": 3, "current": True, "note": "a & <b>"}, {"txn": 2}],
            "none": None,
            "pair": ("x", 1.5),
            "empty": [],
        }
        request = self.request(Node("work", Node(None)))
        document = ET.fromstring(templates.XsltRenderer.request_xml(request, data=data))
        revisions = document.findall("data/revisions/item")
        self.assertEqual([found(r, "txn").text for r in revisions], ["3", "2"])
        self.assertEqual(found(revisions[0], "current").text, "true")
        self.assertEqual(found(revisions[0], "note").text, "a & <b>")
        self.assertIsNone(document.find("data/none"))  # None is not there
        self.assertEqual(
            [i.text for i in document.findall("data/pair/item")], ["x", "1.5"]
        )
        self.assertEqual(len(found(document, "data/empty")), 0)
        with self.assertRaisesRegex(ValueError, "cannot be the name of an element"):
            templates.XsltRenderer.request_xml(request, data={"200": "ok"})
        with self.assertRaisesRegex(TypeError, "the value x of the page is a set"):
            templates.XsltRenderer.request_xml(request, data={"x": {1}})

    def test_the_view_has_its_name_and_the_values_it_gives_its_page(self):
        request = self.request(Node("work", Node(None)))
        request.view_name = "edit"
        values = {"heading": 'A "title" & <more>', "count": 3, "draft": False}
        document = ET.fromstring(
            templates.XsltRenderer.request_xml(request, view_params=values)
        )
        view = found(document, "view")
        self.assertEqual(view.get("name"), "edit")
        self.assertEqual(
            [(p.get("name"), p.text) for p in view.findall("param")],
            [("heading", 'A "title" & <more>'), ("count", "3"), ("draft", "false")],
        )
        plain = ET.fromstring(
            templates.XsltRenderer.request_xml(self.request(Node("x")))
        )
        self.assertEqual(found(plain, "view").get("name"), "")  # always there

    def test_the_accept_header_is_in_the_document_when_there_is_one(self):
        request = self.request(Node("work", Node(None)))
        request.headers["Accept"] = 'text/html, application/xml;q=0.9, "x"&<y>'
        document = ET.fromstring(templates.XsltRenderer.request_xml(request))
        header = found(document, "headers/header")
        self.assertEqual(header.get("name"), "accept")
        self.assertEqual(header.text, request.headers["Accept"])

    def test_the_permissions_of_the_user_on_the_context_are_listed(self):
        request = self.request(Node("work", Node(None)), permissive=True)
        document = ET.fromstring(templates.XsltRenderer.request_xml(request))
        listed = [p.text for p in document.findall("permissions/permission")]
        self.assertEqual(listed, list(templates.PERMISSIONS))

    def test_what_is_in_a_name_cannot_break_the_document(self):
        odd = Node('a"b<c&d', Node(None))
        request = self.request(odd, q='<&">')
        document = ET.fromstring(templates.XsltRenderer.request_xml(request))
        self.assertEqual(found(document, "context").get("name"), 'a"b<c&d')
        self.assertEqual(found(document, "query/param").text, '<&">')


class ImportedFilesTest(unittest.TestCase):
    def test_a_stylesheet_and_what_it_imports_and_includes_by_relative_paths(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name).resolve()
            (directory / "sub").mkdir()
            sheet = f'<xsl:stylesheet version="3.0" {XSL}>{{}}</xsl:stylesheet>'
            (directory / "main.xslt").write_text(
                sheet.format(
                    '<xsl:import href="common.xslt"/><xsl:include href="sub/more.xslt"/>'
                    '<xsl:import href="type:person/view.xsl"/>'
                    '<xsl:import href="http://example.org/x.xsl"/>'
                    '<xsl:import href="missing.xslt"/>'
                )
            )
            (directory / "common.xslt").write_text(sheet.format(""))
            (directory / "sub" / "more.xslt").write_text(
                sheet.format('<xsl:import href="../common.xslt"/>')  # found already
            )
            (directory / "broken.xslt").write_text("<xsl:stylesheet")
            self.assertEqual(
                xslt.imported_files(directory / "main.xslt"),
                [
                    directory / "main.xslt",
                    directory / "common.xslt",  # the imports, then the includes
                    directory / "missing.xslt",  # the compile says it is not there
                    directory / "sub" / "more.xslt",
                ],
            )
            self.assertEqual(
                xslt.imported_files(directory / "broken.xslt"),
                [directory / "broken.xslt"],
            )


class CompiledParametersTest(unittest.TestCase):
    def test_strings_numbers_booleans_and_documents_are_what_the_stylesheet_says(self):
        compiled = xslt.Compiled(TYPED)
        self.assertEqual(compiled.transform("<a/>"), "Hi 1 2 false")
        got = compiled.transform(
            "<a/>",
            {
                "greeting": "Hello",
                "count": 41,
                "ratio": 0.25,
                "flag": True,
                "request": xslt.Xml('<request method="GET"/>'),  # a document
            },
        )
        self.assertEqual(got, "Hello 42 0.5 true GET")
        with self.assertRaisesRegex(xslt.XsltError, "the parameter count is a list"):
            compiled.transform("<a/>", {"count": [1]})

    def test_the_response_is_read_from_what_the_stylesheet_makes(self):
        def made(body: str, headers: str = "", root: str = "response"):
            sheet = f"""<xsl:stylesheet version="3.0" {XSL}><xsl:output method="xml"/>
              <xsl:template match="/"><{root}><headers>{headers}</headers>{body}</{root}>
              </xsl:template></xsl:stylesheet>"""
            messages: list[str] = []
            timings: dict[str, float] = {}
            got = xslt.Compiled(sheet).transform_response(
                "<a/>", None, messages, timings
            )
            self.assertEqual(sorted(timings), ["parse", "transform"])
            return got

        headers = "<Content-Type>text/plain</Content-Type><Vary> Accept </Vary>"
        got = made("<body>a &amp; &lt;b&gt;</body>", headers)
        self.assertEqual(got.root, "response")
        self.assertEqual(
            got.headers, [("Content-Type", "text/plain"), ("Vary", "Accept")]
        )
        self.assertEqual(got.body, "a &amp; &lt;b&gt;")  # as the writer wrote it
        self.assertEqual(made("<body><b>x</b></body>").body, "<b>x</b>")
        self.assertEqual(made("<body/>").body, "")
        self.assertIsNone(made("").body)
        self.assertEqual(made("", root="page").root, "page")
        text_sheet = f"""<xsl:stylesheet version="3.0" {XSL}><xsl:output method="text"/>
          <xsl:template match="/">Hello</xsl:template></xsl:stylesheet>"""
        self.assertEqual(xslt.Compiled(text_sheet).transform_response("<a/>").root, "")

    def test_the_messages_and_the_time_of_a_run_are_given_when_they_are_asked_for(self):
        sheet = f"""<xsl:stylesheet version="3.0" {XSL}><xsl:output method="text"/>
          <xsl:template match="/"><xsl:message>one</xsl:message>done</xsl:template>
        </xsl:stylesheet>"""
        compiled = xslt.Compiled(sheet)
        messages: list[str] = []
        timings: dict[str, float] = {}
        self.assertEqual(
            compiled.transform("<a/>", messages=messages, timings=timings), "done"
        )
        self.assertEqual(messages, ["one"])
        self.assertEqual(sorted(timings), ["parse", "transform"])
        self.assertTrue(all(seconds >= 0 for seconds in timings.values()))
        compiled.transform("<a/>")  # not asked for: nothing is kept
