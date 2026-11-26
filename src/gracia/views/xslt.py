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
what a listing used, `csrf` asks for the csrf token, and every other item is a value of
the view (`<view><param name=>`: strings, numbers and booleans). The response is
what the stylesheet says it is, 200, and the view can change it before it is returned.
A stylesheet is compiled from its file, so the files it imports are found beside it.

What the stylesheet makes is the response:

    <response>
      <headers><Content-Type>application/xml</Content-Type></headers>
      <body>text</body>
    </response>

A header is an element named after it, with its value as the text, and only those of
`ALLOWED_HEADERS` can be set. The body is text: a stylesheet that makes markup writes
it with `serialize($nodes, map{'method': 'html', 'html-version': 5})` (or `xml`). With
no Content-Type the response is `text/html`.

It is registered for the extension `.xslt` (`gracia.main`). `pyramid.reload_templates`
(or PYRAMID_RELOAD_TEMPLATES=1) makes a stylesheet that was edited be compiled again
the next time it is used. `?debug=input` shows the document, `?debug=xslt` (or
`stylesheet`) the stylesheet, `?debug=output` what it makes, and any other value a text
page with what the run said (xsl:message), what was logged and the error, to who
manages the context: they are asked here, so every view with this renderer has them,
and a call of `render_to_response` too.
"""

import logging
import time
import traceback
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from pyramid.location import lineage

from gracia.utils import assets, xslt

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


def request_xml(
    request,
    rdfxml: str = "",
    records=(),
    paging=None,
    csrf: bool = False,
    view_params=None,
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
        rdfxml = values.pop("rdfxml", "")
        records = values.pop("records", ())
        paging = values.pop("paging", None)
        csrf = values.pop("csrf", False)
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
            return self._debug_page(
                request, path, (rdfxml, records, paging, csrf, values)
            )
        xml = request_xml(request, rdfxml, records, paging, csrf, values)
        if debug == "input":
            request.response.content_type = "application/xml"
            return xml
        stylesheet = self._stylesheet(path)
        if debug == "output":
            request.response.content_type = "application/xml"
            return stylesheet.transform(xml)
        return self._response(request, path, stylesheet.transform_response(xml))

    def _stylesheet(
        self, path: str, timings: dict[str, float] | None = None
    ) -> xslt.Compiled:
        """The stylesheet, compiled the first time (and again when it was edited, if
        `pyramid.reload_templates` is on)."""
        modified = 0
        if self.reload:  # the files that the stylesheet imports too
            for file in xslt.imported_files(path):
                if file.exists():
                    modified = max(modified, file.stat().st_mtime_ns)
        kept = _STYLESHEETS.get(path)
        if kept is None or kept[0] != modified:
            started = time.perf_counter()
            if kept is not None:  # an edited stylesheet
                kept[1].close()
            kept = (modified, xslt.Compiled(path))
            _STYLESHEETS[path] = kept
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
        if made.body_has_markup:
            raise xslt.XsltError(
                f"the <body> of {path} is text: write markup with serialize()"
            )
        return made.body

    def _debug_page(self, request, path: str, document_arguments: tuple) -> str:
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
            xml = request_xml(request, *document_arguments)
            timings["document"] = time.perf_counter() - started
            step = "the stylesheet"  # compiled, the document parsed, run
            stylesheet = self._stylesheet(path, timings)
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
        for file in xslt.imported_files(path):
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
