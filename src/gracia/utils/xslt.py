import contextlib
import faulthandler
import hashlib
import queue
import re
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from concurrent.futures import Future
from dataclasses import dataclass, field
from functools import wraps
from html import unescape
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import quoteattr

from saxonche import PySaxonApiError, PySaxonProcessor

NESTED = "rdfxml-to-nested.xsl"
XSL = "http://www.w3.org/1999/XSL/Transform"
# xsl:import href="type:person/view.xsl": a file of a type of the repository
TYPE_SCHEME = "type:"

# export format -> stylesheets run over the nested RDF/XML (none: the nested RDF/XML
# itself)
FORMATS: dict[str, list[str]] = {
    "rdfxml-collection": [],
    "html-table": ["rdfxml-to-html.xsl"],
}


_armed = False


def arm_crash_report() -> None:
    global _armed
    if _armed:
        return
    _armed = True
    with contextlib.suppress(
        AttributeError, ValueError, OSError
    ):  # no stderr to write to: no report
        faulthandler.enable(file=sys.__stderr__, all_threads=True)  # ty: ignore[invalid-argument-type]


class XsltError(RuntimeError):
    pass


@dataclass(frozen=True)
class Response:
    """What a stylesheet made, as the response of a view: the name of the element it
    made (`response`, or another, or none), the header elements in it (name, text) and
    the body as the output method wrote it (None: it has none)."""

    root: str
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: str | None = None


def parse_response(text: str) -> Response:
    """The headers and the body of what a stylesheet made, which Saxon wrote in whatever
    way its `xsl:output` says (html5, xml): the text is cut, and not parsed. The body is
    what is between the first `<body>` and the last `</body>`: the ones of the response
    are the outermost, so a page in it, with its own body, is in between whole. The
    headers are the elements of `<headers>` (a value is escaped as the writer does)."""
    found = re.match(r"\s*(?:<\?xml[^>]*\?>\s*)?<([A-Za-z][\w:.-]*)", text)
    root = found.group(1) if found else ""
    if root != "response":
        return Response(root)
    head, body = text, None
    start, end = text.find("<body>"), text.rfind("</body>")
    if start != -1 and end > start:
        head, body = text[:start], text[start + len("<body>") : end]
    elif "<body/>" in text:  # the xml writer writes an empty element like that
        head, body = text[: text.find("<body/>")], ""
    headers = []
    block = re.search(r"<headers>(.*?)</headers>", head, re.S)
    for name, value in re.findall(
        r"<([A-Za-z][\w-]*)>(.*?)</\1>", block[1] if block else "", re.S
    ):
        headers.append((name, unescape(value).strip()))
    return Response("response", headers, body)


class Xml(str):
    """XML text that is given to a stylesheet as a parameter that is a document (for
    `xsl:param as="document-node()"`), and not as a string."""


def _guarded(function):

    @wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except PySaxonApiError as err:
            raise XsltError(" ".join(str(err).split())) from err

    return wrapper


def bundled_names() -> list[str]:
    return sorted(
        p.name
        for p in (files("gracia") / "assets" / "xslt").iterdir()
        if p.name.endswith(".xsl")
    )


def bundled_text(name: str) -> str:
    return (files("gracia") / "assets" / "xslt" / name).read_text(encoding="utf-8")


def _compile(compiler, stylesheet: str | Path):
    text = str(stylesheet)
    if "<xsl:" in text:
        executable = compiler.compile_stylesheet(stylesheet_text=text)
    elif not Path(text).exists() and text in bundled_names():
        executable = compiler.compile_stylesheet(stylesheet_text=bundled_text(text))
    else:
        executable = compiler.compile_stylesheet(stylesheet_file=text)
    if executable is None:
        raise XsltError(f"cannot compile stylesheet: {compiler.error_message}")
    return executable


def _text(stylesheet: str | Path) -> str:
    text = str(stylesheet)
    if "<xsl:" in text:
        return text
    if not Path(text).exists() and text in bundled_names():
        return bundled_text(text)
    return Path(text).read_text(encoding="utf-8")


# the functions of XPath that read a file (or a page) by an address
LOADING_FUNCTIONS = re.compile(
    r"\b(doc|document|unparsed-text|unparsed-text-lines|json-doc|collection)\s*\("
)


def imported_files(path: str | Path) -> list[Path]:
    """A stylesheet and the files it imports and includes, by the hrefs that are
    relative paths (those of `type:` and of the web are not files here), each as the
    file that has the href says: the files a stylesheet is made of. Saxon does not say
    what it loads, so this is read from the stylesheets. A file that is not there or
    not XML is in the list: compiling says what is wrong with it."""
    found: list[Path] = []
    pending = [Path(path).resolve()]
    while pending:
        current = pending.pop(0)
        if current in found:
            continue
        found.append(current)
        try:
            root = ET.parse(current).getroot()
        except (OSError, ET.ParseError):
            continue
        for tag in ("import", "include"):
            for element in root.iter(f"{{{XSL}}}{tag}"):
                href = element.get("href") or ""
                if href and not urlsplit(href).scheme:
                    pending.append((current.parent / href).resolve())
    return found


def type_references(text: str) -> list[str]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []  # not well-formed: Saxon reports that, with a line number
    found = []
    for tag in ("import", "include"):
        for element in root.iter(f"{{{XSL}}}{tag}"):
            href = element.get("href") or ""
            if href.startswith(TYPE_SCHEME):
                found.append(href)
    return found


def type_files(
    stylesheets: Iterable[str], resolve: Callable[[str], Path]
) -> dict[str, Path]:
    entries: dict[str, Path] = {}
    pending = list(stylesheets)
    while pending:
        for href in type_references(pending.pop()):
            if href in entries:
                continue
            try:
                path = resolve(href)
                text = path.read_text(encoding="utf-8")
            except (LookupError, ValueError, OSError) as err:
                raise XsltError(f"cannot import {href}: {err}") from err
            entries[href] = path
            pending.append(text)
    return entries


def _catalog(entries: dict[str, Path]) -> str:
    lines = [
        f"  <uri name={quoteattr(h)} uri={quoteattr(p.resolve().as_uri())}/>"
        for h, p in entries.items()
    ]
    return (
        '<?xml version="1.0"?>\n<catalog '
        'xmlns="urn:oasis:names:tc:entity:xmlns:xml:catalog">\n'
        + "\n".join(lines)
        + "\n</catalog>\n"
    )


def type_catalog(
    stylesheets: Iterable[str], resolve: Callable[[str], Path]
) -> str | None:
    entries = type_files(stylesheets, resolve)
    return _catalog(entries) if entries else None


class _Owner:
    STACK = 64 * 1024 * 1024

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        old = threading.stack_size(self.STACK)
        try:
            self.thread = threading.Thread(target=self._loop, name="saxon", daemon=True)
            self.thread.start()
        finally:
            threading.stack_size(old)

    def _loop(self) -> None:
        while True:
            future, function, args = self._queue.get()
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(function(*args))
                except BaseException as err:
                    future.set_exception(err)

    def call(self, function, *args):
        if threading.current_thread() is self.thread:
            return function(*args)
        future: Future = Future()
        self._queue.put((future, function, args))
        return future.result()


_owner: _Owner | None = None
_owner_lock = threading.Lock()


def owner() -> _Owner:
    global _owner
    with _owner_lock:
        if _owner is None or not _owner.thread.is_alive():
            _owner = _Owner()
        return _owner


class Compiled:
    def __init__(self, stylesheet: str, res_types: Callable[[str], Path] | None = None):
        text = _text(stylesheet)
        entries = type_files([text], res_types) if res_types else {}
        digest = hashlib.sha256(text.encode())
        for href, path in sorted(entries.items()):
            digest.update(href.encode() + b"\0" + path.read_bytes() + b"\0")
        self.fingerprint = digest.hexdigest()[:16]
        self.threads: set[int] = set()
        self._owner = owner()
        self._executable = None
        self._processor = None
        # a file is compiled as a file, so that what it imports is found beside it;
        # text (the stylesheets that are made of data) has no address to import from
        self._owner.call(self._build, stylesheet, entries)

    def _build(self, stylesheet: str | Path, entries: dict[str, Path]) -> None:
        self.threads.add(threading.get_ident())
        self._processor = PySaxonProcessor(license=False)
        arm_crash_report()
        try:
            with tempfile.TemporaryDirectory() as scratch:
                if entries:
                    catalog = Path(scratch) / "catalog.xml"
                    catalog.write_text(_catalog(entries), encoding="utf-8")
                    self._processor.set_catalog_files([str(catalog)])
                self._executable = _compile(
                    self._processor.new_xslt30_processor(), stylesheet
                )
        except PySaxonApiError as err:
            self._close()
            raise XsltError(" ".join(str(err).split())) from err
        except BaseException:
            self._close()
            raise

    @_guarded
    def transform(
        self,
        xml_text: str,
        params: dict[str, str | int | float | bool] | None = None,
        messages: list[str] | None = None,
        timings: dict[str, float] | None = None,
    ) -> str:
        """The stylesheet run over the XML text. A parameter is a string, an integer, a
        float (an xs:double), a boolean or `Xml` (a document): what `xsl:param` gets,
        and `as=` can say. The `xsl:message` output of the run is added to `messages`,
        if there is a list (it is not kept, nor written anywhere, otherwise). `timings`
        gets the seconds it took to parse the XML (`parse`) and to run (`transform`)."""
        return self._owner.call(self._run, xml_text, params, messages, timings)

    def transform_response(
        self,
        xml_text: str,
        params: dict[str, str | int | float | bool] | None = None,
        messages: list[str] | None = None,
        timings: dict[str, float] | None = None,
    ) -> Response:
        """What the stylesheet makes, read as the response of a view (`parse_response`).
        (The nodes of the document of Saxon, read with `transform_to_value`, crashed the
        process now and then when the tests ran in parallel, with no report, and a
        forced
        garbage collection did not help: the text is what is read.)"""
        return parse_response(self.transform(xml_text, params, messages, timings))

    def _run(
        self,
        xml_text: str,
        params: dict[str, str | int | float | bool] | None,
        messages: list[str] | None,
        timings: dict[str, float] | None,
    ) -> str:
        """The run, on the thread of Saxon: the text it makes."""
        self.threads.add(threading.get_ident())
        processor = self._processor
        if self._executable is None or processor is None:
            raise XsltError("the stylesheet was closed")
        # the executable keeps them: a run must not see the last one's
        self._executable.clear_parameters()
        for name, value in (params or {}).items():
            if isinstance(value, Xml):  # before str: it is one
                xdm_value = processor.parse_xml(xml_text=str(value))
            elif isinstance(value, bool):  # before int: a bool is an int
                xdm_value = processor.make_boolean_value(value)
            elif isinstance(value, int):
                xdm_value = processor.make_integer_value(value)
            elif isinstance(value, float):
                xdm_value = processor.make_double_value(value)
            elif isinstance(value, str):
                xdm_value = processor.make_string_value(value)
            else:
                raise XsltError(
                    f"the parameter {name} is a {type(value).__name__}: "
                    "a string, a number or a boolean is expected"
                )
            self._executable.set_parameter(name, xdm_value)
        started = time.perf_counter()
        node = processor.parse_xml(xml_text=xml_text)
        parsed = time.perf_counter()
        if messages is not None:
            self._executable.set_save_xsl_message(True)
        try:
            result = self._executable.transform_to_string(xdm_node=node)
        finally:
            if timings is not None:
                timings["parse"] = parsed - started
                timings["transform"] = time.perf_counter() - parsed
            if messages is not None:  # also what was said before it failed
                saved = self._executable.get_xsl_messages()
                if saved is not None:
                    for item in saved:
                        messages.append(item.string_value)
                self._executable.clear_xsl_messages()
                self._executable.set_save_xsl_message(False)
        if result is None:
            raise XsltError(f"transformation failed: {self._executable.error_message}")
        return result

    @property
    def closed(self) -> bool:
        return self._executable is None

    def close(self) -> None:
        if self._processor is not None:
            self._owner.call(self._close)

    def _close(self) -> None:
        self.threads.add(threading.get_ident())
        self._executable = None
        if self._processor is not None:
            self._processor.__exit__(
                None, None, None
            )  # what `with PySaxonProcessor()` ends with
            self._processor = None


@_guarded
def transform(
    xml_text: str,
    *stylesheets: str | Path,
    params: dict[str, str] | None = None,
    res_types: Callable[[str], Path] | None = None,
) -> str:
    catalog = (
        type_catalog([_text(s) for s in stylesheets], res_types) if res_types else None
    )
    with (
        tempfile.TemporaryDirectory() as scratch,
        PySaxonProcessor(license=False) as proc,
    ):
        arm_crash_report()
        if catalog:
            (Path(scratch) / "catalog.xml").write_text(catalog, encoding="utf-8")
            proc.set_catalog_files([str(Path(scratch) / "catalog.xml")])
        compiler = proc.new_xslt30_processor()
        executables = [_compile(compiler, s) for s in stylesheets]
        for executable in executables:
            for name, value in (params or {}).items():
                executable.set_parameter(name, proc.make_string_value(value))
        node = proc.parse_xml(xml_text=xml_text)
        for executable in executables[:-1]:
            value = executable.transform_to_value(xdm_node=node)
            if value is None or value.head is None:
                raise XsltError(f"transformation failed: {executable.error_message}")
            node = value.head
        result = executables[-1].transform_to_string(xdm_node=node)
        if result is None:
            raise XsltError(f"transformation failed: {executables[-1].error_message}")
        return result


def render(name: str, flat_rdfxml: str, params: dict[str, str] | None = None) -> str:
    return transform(flat_rdfxml, NESTED, *FORMATS[name], params=params)


def nest(flat_rdfxml: str, params: dict[str, str] | None = None) -> str:
    return transform(flat_rdfxml, NESTED, params=params)
