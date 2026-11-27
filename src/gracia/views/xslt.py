"""The XSLT renderer of views, next to the Chameleon templates and `json`. As with a
template, the view returns a dict, or gives one to `render_to_response`:

    response = render_to_response(
        "gracia:assets/xslt/debug.xslt", {"heading": "Debug", "rdfxml": rdfxml},
        request=request,
    )
    response.content_type = "text/plain"  # as with a template, before it is returned
    return response

The stylesheet is run over the `<request>` document that `request_xml` makes of the
request and of the dict: `rdfxml` is the data (in `<body>`), `records` and `paging` say
what a listing used, `csrf` asks for the csrf token, `fragments` are pieces of HTML that
the view made (a form), `site` and `properties` are what the pages have around them and
what the shape lets them show, `layout` is a stylesheet that the one of the page is put in
(they are imported by one that is made, the page after the layout, so that what the page
defines counts), and every other item is a value of the view (`<view><param name=>`:
strings, numbers and booleans). `page` is the one that makes a page of the site, with the
stylesheets of the repository (`skin/layout.xslt`, `skin/type.xslt`, `skin/record.xslt`). The response is
what the stylesheet says it is, 200, and the view can change it before it is returned.
A stylesheet is compiled from its file, so the files it imports are found beside it.

What the stylesheet makes is the response, written by Saxon as its `xsl:output` says (the
skins say `method="html" html-version="5"`):

    <response>
      <headers><Content-Type>text/html</Content-Type></headers>
      <body><html>..</html></body>
    </response>

A header is an element named after it, with its value as the text, and only those of
`ALLOWED_HEADERS` can be set. The renderer does not parse it: it cuts it (`parse_response`),
the body is what is between the first `<body>` and the last `</body>`. A body of
`text/html` or of an XML type is markup and is as the writer wrote it (with the doctype
added to a page, the writer has none, the root is the response); of another type it is text,
and is unescaped. With no Content-Type the response is `text/html`.

It is registered for the extension `.xslt` (`gracia.main`). `pyramid.reload_templates`
(or PYRAMID_RELOAD_TEMPLATES=1) makes a stylesheet that was edited be compiled again
the next time it is used. `?debug=input` shows the document, `?debug=xslt` (or
`stylesheet`) the stylesheet, `?debug=output` what it makes, and any other value a text
page with what the run said (xsl:message), what was logged and the error, to who
manages the context: they are asked here, so every view with this renderer has them,
and a call of `render_to_response` too.
"""

import logging
import re
import time
import traceback
from html import unescape
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import lxml.html
from lxml import etree  # ty: ignore[missing-module-member]
from pyramid.location import lineage
from pyramid.renderers import render_to_response
from pyramid.response import Response

from gracia import static
from gracia.utils import assets, xslt
from gracia.views import home_url, pop_flashes

# the permissions of the context that the request document says the user has: the
# stylesheet cannot ask Python (SaxonC-HE does not call extension functions in Python)
PERMISSIONS = ("view", "edit", "add", "manage")

# the headers that a stylesheet may set in its response (a stylesheet that is made of
# data is not trusted); the status is the view's
ALLOWED_HEADERS = (
    "content-type",
    "content-disposition",
    "cache-control",
    "expires",
    "location",
    "vary",
    "link",
)

# the keys and the functions to jump between the resources of the RDF/XML: before the
# layout and the page, whatever they are
HELPERS = assets.resolve("gracia:assets/xslt/skin/rdf.xslt")

DEFAULT_LAYOUT = "gracia:assets/xslt/skin/layout.xslt"

# what a view can give besides the values of its page, and what the document has of it
DOCUMENT_ITEMS = (
    "rdfxml",
    "records",
    "paging",
    "csrf",
    "fragments",
    "site",
    "properties",
    "data",
)

# the stylesheets that were compiled, by file, with the time the file had: compiling
# takes a while, running does not
_STYLESHEETS: dict[str, tuple[int, xslt.Compiled]] = {}


def record_uri_of(node) -> str:
    """The uri of the record that a node is or has (a record, a type, a repository)."""
    for attribute in ("uri", "record"):
        value = getattr(node, attribute, None)
        if isinstance(value, str) and value.startswith("rec:"):
            return value
    return ""


def html_to_xml(html: str) -> str:
    """HTML as markup that is XML: lxml reads it the way a browser would, as far as it
    does, and writes it again."""
    xml = ""
    for part in lxml.html.fragments_fromstring(html):
        if isinstance(part, str):  # text before the first element
            xml += escape(part)
        else:
            xml += etree.tostring(part, method="xml", encoding="unicode")
    return xml


def data_xml(name: str, value) -> str:
    """The XML of what a view gives its page: a dict is an element with an element for
    each key, a list or tuple an element with an `<item>` for each of its values, a
    string, number or boolean the text of an element, and None nothing at all. A key must
    be a name that an element can have."""
    if not re.fullmatch(r"[A-Za-z_][\w.-]*", name):
        raise ValueError(f"{name!r} cannot be the name of an element")
    if value is None:
        return ""
    inner = ""
    if isinstance(value, dict):
        for key, part in value.items():
            inner += data_xml(key, part)
    elif isinstance(value, list | tuple):
        for part in value:
            inner += data_xml("item", part)
    elif isinstance(value, bool):
        inner = "true" if value else "false"
    elif isinstance(value, str | int | float):
        inner = escape(str(value))
    else:
        raise TypeError(f"the value {name} of the page is a {type(value).__name__}")
    return f"<{name}>{inner}</{name}>"


def request_xml(
    request,
    rdfxml: str = "",
    records=(),
    paging=None,
    csrf: bool = False,
    view_params=None,
    fragments=None,
    site=None,
    properties=(),
    data=None,
) -> str:
    """The XML text that a stylesheet makes the response from: the request, where it is
    in the tree (every node up to the root, with its address, so the stylesheet can
    make absolute addresses), what the user may do there, the records that the page is
    of with the paging, and the RDF/XML of the data.

        <request method= url= path= application-url= user=>
          <context kind= name= url= record_uri=>
            <node kind= name= url= record_uri=/>  the root first, the context last
          </context>
          <view name="debug"><param name="heading">Debug</param></view>
          <permissions><permission>manage</permission></permissions>
          <headers><header name="accept">text/html</header></headers>
          <query><param name=>2</param></query>
          <records page= pages= total= size=><record uri="rec:work:1"/></records>
          <csrf-token>..</csrf-token>
          <body><rdf:RDF>..</rdf:RDF></body>
        </request>

    `records` are the uris of the records that were got, as of `referencing_records`,
    and `paging` is what a listing says of its page (`page`, `pages`, `total`, `size`).
    `fragments` are pieces of HTML that the view made (a Deform form): each is read by
    lxml, which repairs what is not XML (a bare `readonly`, a `<br>`, `&nbsp;`), and put
    in as markup, `<fragments><fragment name=>..</fragment></fragments>`, for the
    stylesheet to copy where it wants it. `site` is what the pages of the site have
    around them (`home`, `favicon`, `embed` and the `flashes`, each a level and a text)
    and `properties` the properties of the shape of the type that a page may show, each
    a path, a name and a role (`key_info`, `description`). `data` is what a page has of
    structure, a dict as of `data_xml`: `<data><revisions><item><txn>3</txn>..`.
    The view has its name (`request.view_name`) and `view_params`, the values it gives
    its page besides the data (a heading, a subtitle: strings, numbers, booleans). The
    headers are only `Accept` for now. The csrf token is made when it is asked for: it makes a session, and so a cookie."""
    context = request.context
    attributes = (
        f"method={quoteattr(request.method)} url={quoteattr(request.url)} "
        f"path={quoteattr(request.path)} "
        f"application-url={quoteattr(request.application_url)}"
    )
    user = request.authenticated_userid
    if user:
        attributes += f" user={quoteattr(str(user))}"
    xml = [f"<request {attributes}>"]

    context_name = getattr(context, "__name__", None) or ""
    xml.append(
        f"<context kind={quoteattr(type(context).__name__)} "
        f"name={quoteattr(context_name)} "
        f"url={quoteattr(request.resource_url(context))} "
        f"record_uri={quoteattr(record_uri_of(context))}>"
    )
    for node in reversed(list(lineage(context))):
        node_name = getattr(node, "__name__", None) or ""
        xml.append(
            f"<node kind={quoteattr(type(node).__name__)} name={quoteattr(node_name)} "
            f"url={quoteattr(request.resource_url(node))} "
            f"record_uri={quoteattr(record_uri_of(node))}/>"
        )
    xml.append("</context>")

    xml.append(f"<view name={quoteattr(getattr(request, 'view_name', '') or '')}>")
    for name, value in (view_params or {}).items():
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, str | int | float):
            text = str(value)
        else:
            raise TypeError(
                f"the value {name} of the view is a {type(value).__name__}: "
                "a string, a number or a boolean is expected"
            )
        xml.append(f"<param name={quoteattr(name)}>{escape(text)}</param>")
    xml.append("</view>")

    if data:
        xml.append(data_xml("data", data))

    if site:
        embed = "true" if site["embed"] else "false"
        xml.append(
            f"<site home={quoteattr(site['home'])} favicon={quoteattr(site['favicon'])} "
            f"embed={quoteattr(embed)}>"
        )
        for level, text in site["flashes"]:
            xml.append(f"<flash level={quoteattr(level)}>{escape(text)}</flash>")
        xml.append("</site>")

    if properties:
        xml.append("<properties>")
        for path, name, role in properties:
            xml.append(
                f"<property path={quoteattr(path)} name={quoteattr(name)} "
                f"role={quoteattr(role)}/>"
            )
        xml.append("</properties>")

    xml.append("<permissions>")
    for permission in PERMISSIONS:
        if request.has_permission(permission, context):
            xml.append(f"<permission>{permission}</permission>")
    xml.append("</permissions>")

    xml.append("<headers>")
    accept = request.headers.get("Accept")
    if accept:
        xml.append(f'<header name="accept">{escape(accept)}</header>')
    xml.append("</headers>")

    xml.append("<query>")
    for name, value in request.GET.items():
        xml.append(f"<param name={quoteattr(name)}>{escape(value)}</param>")
    xml.append("</query>")

    if records or paging:
        attributes = ""
        for name, value in (paging or {}).items():
            attributes += f" {name}={quoteattr(str(value))}"
        xml.append(f"<records{attributes}>")
        for record_uri in records:
            xml.append(f"<record uri={quoteattr(record_uri)}/>")
        xml.append("</records>")

    if fragments:
        xml.append("<fragments>")
        for name, html in fragments.items():
            xml.append(
                f"<fragment name={quoteattr(name)}>{html_to_xml(html)}</fragment>"
            )
        xml.append("</fragments>")

    if csrf:
        xml.append(
            f"<csrf-token>{escape(request.session.get_csrf_token())}</csrf-token>"
        )
    if rdfxml.startswith("<?xml"):  # one document has one declaration
        rdfxml = rdfxml.split("?>", 1)[1]
    xml.append("<body>")
    xml.append(rdfxml)
    xml.append("</body>")
    xml.append("</request>")
    return "\n".join(xml)


def render_page(
    request,
    spec: str,
    status: int = 200,
    fragments=None,
    data=None,
    bare: bool = False,
    **values,
) -> Response:
    """A page of the site made by stylesheets: the stylesheet `spec` (an asset spec or a
    path) put in the layout of the repository, or alone when it is `bare` (a page that
    is no part of the site). The values are those of `XsltRenderer`: strings, numbers and
    booleans for the page, `data` for what has structure, and `fragments` for the HTML
    that the view made (the pieces are copied into the page by the stylesheet). The site
    is added: where home is, the icon, whether it is shown in a frame, and the flashes."""
    repository = getattr(request.context, "repository", None)
    embed = request.params.get("embed") == "1"
    static.page.need()  # the styles and scripts of every page
    if embed:
        static.embed_js.need()  # tells the page around it that the record is saved
    flashes = []
    if "debug" not in request.params:  # a look at the page does not use them up
        flashes = pop_flashes(request)
    values["site"] = {
        "home": home_url(request),
        "favicon": static.url("img/gracia.svg"),
        "embed": embed,
        "flashes": flashes,
    }
    if fragments:
        values["fragments"] = fragments
    if data:
        values["data"] = data
    if not bare:
        values["layout"] = DEFAULT_LAYOUT
        if repository is not None:
            values["layout"] = repository.stylesheet("layout")
    response = render_to_response(spec, values, request=request)
    response.status_int = status
    return response


def page(request, kind: str, own: str | None = None, **values) -> Response:
    """The page of a kind (`type` or `record`) of the repository, as `render_page`: the
    stylesheet that the repository names, else `own` (that of the type), else gracia's."""
    repository = request.context.repository
    return render_page(request, repository.stylesheet(kind, own), **values)


class _Records(logging.Handler):
    """The log records of the process while a stylesheet is run, for `?debug=`."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(f"{record.levelname} {record.name}: {record.getMessage()}")


DEBUG_HELP = """Debug switches of this page, for who manages it (?debug=...):

  ?debug=input       the document the stylesheet is given (application/xml)
  ?debug=xslt        the stylesheet, not the files it imports (also ?debug=stylesheet)
  ?debug=output      what the stylesheet makes, the <response> (application/xml)
  ?debug=<anything>  this page: it runs the stylesheet and says how long each step took,
                     what it said, what was logged, which files it is made of, and the
                     error, in which step
"""


class XsltRenderer:
    def __init__(self, info):
        self.spec = info.name  # an asset spec, as in gracia:assets/xslt/debug.xslt
        self.reload = bool(info.settings.get("pyramid.reload_templates"))

    def __call__(self, value, system):
        request = system["request"]
        values = dict(value)
        document: dict[str, Any] = {}  # what is not a value of the view: the data ...
        for name in DOCUMENT_ITEMS:
            if name in values:
                document[name] = values.pop(name)
        document["view_params"] = values
        layout = values.pop("layout", None)  # the stylesheet that the page is put in
        if layout is not None:
            layout = str(assets.resolve(layout))
        path = str(assets.resolve(self.spec))
        # `?debug=` is for who manages the context: whatever it says, for the others
        # the page is the page
        debug = request.params.get("debug")
        if debug is not None and not request.has_permission(
            "manage", system["context"]
        ):
            debug = None
        if debug is not None:  # the document has the csrf token and the user in it
            request.response.headers["Cache-Control"] = "no-store"
        if debug in ("xslt", "stylesheet"):
            request.response.content_type = "application/xslt+xml"
            return Path(path).read_text(encoding="utf-8")
        if debug not in (None, "input", "output"):
            return self._debug_page(request, path, layout, document)
        xml = request_xml(request, **document)
        if debug == "input":
            request.response.content_type = "application/xml"
            return xml
        stylesheet = self._stylesheet(path, layout)
        if debug == "output":  # as the writer wrote it: html5 is not XML
            request.response.content_type = "text/plain"
            return stylesheet.transform(xml)
        return self._response(request, path, stylesheet.transform_response(xml))

    def _stylesheet(
        self,
        path: str,
        layout: str | None = None,
        timings: dict[str, float] | None = None,
    ) -> xslt.Compiled:
        """The stylesheet, compiled the first time (and again when it was edited, if
        `pyramid.reload_templates` is on). With a layout it is one that imports the
        helpers (`skin/rdf.xslt`), the layout and then the stylesheet of the page, so what
        the page defines is what counts, and a repository can have another of each."""
        files = xslt.imported_files(path)
        key = path
        if layout:
            files += xslt.imported_files(layout) + xslt.imported_files(HELPERS)
            key = f"{layout} {path}"
        modified = 0
        if self.reload:  # the files that the stylesheets import too
            for file in files:
                if file.exists():
                    modified = max(modified, file.stat().st_mtime_ns)
        kept = _STYLESHEETS.get(key)
        if kept is None or kept[0] != modified:
            started = time.perf_counter()
            if kept is not None:  # an edited stylesheet
                kept[1].close()
            source = path
            if layout:  # by their addresses: a stylesheet that is text has no place
                source = (
                    f'<xsl:stylesheet version="3.0" xmlns:xsl="{xslt.XSL}">'
                    f'<xsl:import href="{HELPERS.as_uri()}"/>'
                    f'<xsl:import href="{Path(layout).as_uri()}"/>'
                    f'<xsl:import href="{Path(path).as_uri()}"/></xsl:stylesheet>'
                )
            kept = (modified, xslt.Compiled(source))
            _STYLESHEETS[key] = kept
            if timings is not None:
                timings["compile"] = time.perf_counter() - started
        return kept[1]

    def _response(self, request, path: str, made: xslt.Response) -> str:
        """Sets the headers of the response that the stylesheet made, and gives its body."""
        if made.root == "":
            raise xslt.XsltError(f"{path} does not make a <response>")
        if made.root != "response":
            raise xslt.XsltError(f"{path} makes a <{made.root}>, not a <response>")
        for name, header_value in made.headers:
            if name.lower() not in ALLOWED_HEADERS:
                raise xslt.XsltError(f"{path} may not set the header {name}")
            if name.lower() == "content-type":
                # the charset is the one the text is written in: UTF-8
                request.response.content_type = header_value.split(";")[0].strip()
            else:
                request.response.headers[name] = header_value
        if made.body is None:
            raise xslt.XsltError(f"the <response> of {path} has no <body>")
        content_type = request.response.content_type
        if content_type != "text/html" and not content_type.endswith("xml"):
            return unescape(made.body)  # text: the writer escaped its `&` and `<`
        body = made.body.strip()  # markup is as the writer wrote it
        if content_type == "text/html" and body.lower().startswith("<html"):
            body = "<!DOCTYPE html>\n" + body  # the root of the writer was the response
        return body

    def _debug_page(
        self, request, path: str, layout: str | None, document: dict
    ) -> str:
        """The page of `?debug=<anything>`: the whole run, the document made too, with
        how long each step took, what the stylesheet said, what was logged and what went
        wrong and where, as text."""
        messages: list[str] = []
        timings: dict[str, float] = {}
        records = _Records()
        logging.getLogger().addHandler(records)
        step = "the document of the request"
        failure = ""
        result = ""
        started = time.perf_counter()
        try:
            xml = request_xml(request, **document)
            timings["document"] = time.perf_counter() - started
            step = "the stylesheet"  # compiled, the document parsed, run
            stylesheet = self._stylesheet(path, layout, timings)
            made = stylesheet.transform_response(xml, None, messages, timings)
            step = "the response of the stylesheet"
            response_started = time.perf_counter()
            body = self._response(request, path, made)
            timings["response"] = time.perf_counter() - response_started
            content_type = request.response.headers["Content-Type"]
            result = f"ok: {content_type}, a body of {len(body)} characters"
        except Exception:
            failure = traceback.format_exc()
        finally:
            logging.getLogger().removeHandler(records)
        total = time.perf_counter() - started
        lines = [DEBUG_HELP, f"stylesheet: {path}", ""]
        lines.append("result: " + (result or f"FAILED in {step}"))
        lines += ["", "time (milliseconds):"]
        for name, label in (
            ("document", "the document of the request is made"),
            ("compile", "the stylesheet is compiled"),
            ("parse", "the document is parsed"),
            ("transform", "the stylesheet is run"),
            ("response", "the response is read"),
        ):
            if name in timings:
                lines.append(f"  {timings[name] * 1000:9.2f}  {label}")
            elif name == "compile":
                lines.append("     cached  the stylesheet was compiled before")
        lines.append(f"  {total * 1000:9.2f}  all of it")
        lines += ["", "xsl:message output:"]
        lines += [f"  {message}" for message in messages] or ["  (none)"]
        lines += ["", "log records during the run (of the process):"]
        lines += [f"  {line}" for line in records.lines] or ["  (none)"]
        lines += ["", "files the stylesheet is made of (read from the stylesheet):"]
        calls = []
        files = xslt.imported_files(path)
        if layout:
            files += xslt.imported_files(layout) + xslt.imported_files(HELPERS)
        for file in files:
            if not file.exists():
                lines.append(f"  {file}  (there is no such file)")
                continue
            lines.append(f"  {file}  ({file.stat().st_size} bytes)")
            text = file.read_text(encoding="utf-8", errors="replace")
            for found in xslt.LOADING_FUNCTIONS.finditer(text):
                number = text.count("\n", 0, found.start()) + 1
                calls.append(f"  {file.name}:{number}: {found.group(0)}")
        lines += ["", "calls that may load files (read from the stylesheet, Saxon does"]
        lines += ["not say what it loads: the addresses may be made when it runs):"]
        lines += calls or ["  (none)"]
        if failure:
            lines += ["", "error:", failure]
        request.response.content_type = "text/plain"
        request.response.headers["Cache-Control"] = "no-store"
        return "\n".join(lines) + "\n"
