"""XSLT 3.0 through SaxonC.

RDF reaches a stylesheet as RDF/XML: the Dataset is serialized by pyoxigraph (flat, with the
prefixes of the ns_prefix extension), and the bundled `rdfxml-to-nested.xsl` turns that into nested
RDF/XML with rdf:parseType="Collection". Every other stylesheet runs over that nested result.
Stages are chained in memory: a result tree is handed to the next stylesheet as it is, it is
not serialized and parsed again.
"""

import faulthandler
import hashlib
import queue
import sys
import tempfile
import threading
from concurrent.futures import Future
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from functools import wraps
from importlib.resources import files
from pathlib import Path
from xml.sax.saxutils import quoteattr

from saxonche import PySaxonApiError, PySaxonProcessor

NESTED = "rdfxml-to-nested.xsl"
XSL = "http://www.w3.org/1999/XSL/Transform"
TYPE_SCHEME = "type:"  # xsl:import href="type:person/view.xsl": a file of a type of the repository

# export format -> stylesheets run over the nested RDF/XML (none: the nested RDF/XML itself)
FORMATS: dict[str, list[str]] = {
    "rdfxml-collection": [],
    "html-table": ["rdfxml-to-html.xsl"],
}


_armed = False


def arm_crash_report() -> None:
    """Have a fatal signal print the Python stack of every thread, once the process has SaxonC in it.

    SaxonC (a GraalVM native image) installs a handler for SIGSEGV, and any segmentation fault in the
    process, whichever native library it came from (Tantivy, DuckDB, LMDB), is then reported as a
    `StackOverflowError` of the "wrong IsolateThread", which says nothing about the cause. Python's
    `faulthandler` passes the signal on to the handler that was there before it, so enabled after SaxonC's
    it first says what every thread was doing. Called when a processor is made; it does nothing again."""
    global _armed
    if _armed:
        return
    _armed = True
    try:
        faulthandler.enable(file=sys.__stderr__, all_threads=True)
    except (AttributeError, ValueError, OSError):  # no stderr to write to: no report
        pass


class XsltError(RuntimeError):
    """A stylesheet could not be compiled or run, or its input is not well-formed."""


def _guarded(function):
    """Saxon raises its own exception type: report it as an XsltError with its message."""

    @wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except PySaxonApiError as err:
            raise XsltError(" ".join(str(err).split())) from err

    return wrapper


def bundled_names() -> list[str]:
    """File names of every stylesheet shipped with gracia."""
    return sorted(
        p.name for p in (files("gracia") / "assets" / "xslt").iterdir() if p.name.endswith(".xsl")
    )


def bundled_text(name: str) -> str:
    return (files("gracia") / "assets" / "xslt" / name).read_text(encoding="utf-8")


def _compile(compiler, stylesheet: str | Path):
    """A stylesheet given as its text, as a path, or as the name of a bundled one."""
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
    """The text of a stylesheet given as text, as a path or as the name of a bundled one."""
    text = str(stylesheet)
    if "<xsl:" in text:
        return text
    if not Path(text).exists() and text in bundled_names():
        return bundled_text(text)
    return Path(text).read_text(encoding="utf-8")


def type_references(text: str) -> list[str]:
    """The `type:` hrefs of the xsl:import and xsl:include elements of a stylesheet."""
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


def type_files(stylesheets: Iterable[str], resolve: Callable[[str], Path]) -> dict[str, Path]:
    """The file `resolve` gives for every `type:` address the stylesheets import, and that the
    files they import import in turn (empty: there are none). Every file is resolved by itself,
    which is what keeps a file-by-file override of a type and the type assets of the repository
    right."""
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
        '<?xml version="1.0"?>\n<catalog xmlns="urn:oasis:names:tc:entity:xmlns:xml:catalog">\n'
        + "\n".join(lines)
        + "\n</catalog>\n"
    )


def type_catalog(stylesheets: Iterable[str], resolve: Callable[[str], Path]) -> str | None:
    """An OASIS catalog that maps every `type:` address the stylesheets import, and that the
    files they import import in turn, to the file `resolve` gives for it (None: there are none).

    Saxon (as SaxonC offers it) takes no resolver written in Python, only catalogs, so the
    addresses are found beforehand."""
    entries = type_files(stylesheets, resolve)
    return _catalog(entries) if entries else None


class _Owner:
    """The one thread that owns every long-lived SaxonC object (`Compiled`): they are made, run and let go of
    here and nowhere else, whoever asks.

    SaxonC is a GraalVM native image, and its objects belong to the OS thread that made them. Used from
    another thread they may seem to work, until a garbage collection finds a thread running with another's
    handle and the process dies with "wrong IsolateThread" (a fatal `StackOverflowError` that no Python
    `except` can catch). A cache is used by the worker thread and by requests, so a compiled stylesheet
    cannot be left to the thread that happened to compile it: callers hand the work over (`call`) and wait.
    The thread has a big stack, since a stylesheet may recurse (`threading.stack_size` is for the threads
    made after it is set, so it is set only while this one starts)."""

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
                except BaseException as err:  # noqa: BLE001  whatever the caller would have got
                    future.set_exception(err)

    def call(self, function, *args):
        """Run `function(*args)` on the thread and give back what it returns (or raise what it raised). On
        the thread itself it just runs."""
        if threading.current_thread() is self.thread:
            return function(*args)
        future: Future = Future()
        self._queue.put((future, function, args))
        return future.result()


_owner: _Owner | None = None
_owner_lock = threading.Lock()


def owner() -> _Owner:
    """The thread that owns the long-lived SaxonC objects, started by the first one that needs it."""
    global _owner
    with _owner_lock:
        if _owner is None or not _owner.thread.is_alive():
            _owner = _Owner()
        return _owner


class Compiled:
    """A stylesheet compiled once and run many times (`transform`): what a cache keeps.

    SaxonC resolves `xsl:import` and `xsl:include` when it compiles, so the executable has the
    text of every file as it was then: an edit of an imported file is not seen by it. Whoever keeps
    one throws it away when the files may have changed. `fingerprint` is a hash of the stylesheet
    and of the files it imports, which is what something made with it is stamped with.

    The processor lives as long as the executable, and both live on the thread of `owner()`: every method
    here is run there (see `_Owner` for why), so a run, which sets parameters on the executable, cannot meet
    another, and the worker thread and a request may both ask. `threads` is the set of the threads that ever
    touched the objects (one, the owner: a test looks)."""

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
        self._owner.call(self._build, text, entries)

    def _build(self, text: str, entries: dict[str, Path]) -> None:
        self.threads.add(threading.get_ident())
        self._processor = PySaxonProcessor(license=False)
        arm_crash_report()
        try:
            with tempfile.TemporaryDirectory() as scratch:
                if entries:
                    catalog = Path(scratch) / "catalog.xml"
                    catalog.write_text(_catalog(entries), encoding="utf-8")
                    self._processor.set_catalog_files([str(catalog)])
                self._executable = _compile(self._processor.new_xslt30_processor(), text)
        except PySaxonApiError as err:
            self._close()
            raise XsltError(" ".join(str(err).split())) from err
        except BaseException:
            self._close()
            raise

    @_guarded
    def transform(self, xml_text: str, params: dict[str, str] | None = None) -> str:
        """The result of the stylesheet over an XML document (the parameters are strings)."""
        return self._owner.call(self._transform, xml_text, params)

    def _transform(self, xml_text: str, params: dict[str, str] | None) -> str:
        self.threads.add(threading.get_ident())
        if self._executable is None:
            raise XsltError("the stylesheet was closed")
        self._executable.clear_parameters()  # the executable keeps them: a run must not see the last one's
        for name, value in (params or {}).items():
            self._executable.set_parameter(name, self._processor.make_string_value(value))
        node = self._processor.parse_xml(xml_text=xml_text)
        result = self._executable.transform_to_string(xdm_node=node)
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
            self._processor.__exit__(None, None, None)  # what `with PySaxonProcessor()` ends with
            self._processor = None


@_guarded
def transform(
    xml_text: str,
    *stylesheets: str | Path,
    params: dict[str, str] | None = None,
    res_types: Callable[[str], Path] | None = None,
) -> str:
    """Run stylesheets one after the other over an XML document, each over the previous result
    (kept in memory). Returns the serialized result of the last one.

    With `types`, a stylesheet may `xsl:import` or `xsl:include` the file of a type with
    `href="type:<slug>/<file>"`; `types` turns such an address into a path (see
    `resources.Repository.resolve_type`)."""
    catalog = type_catalog([_text(s) for s in stylesheets], res_types) if res_types else None
    with tempfile.TemporaryDirectory() as scratch, PySaxonProcessor(license=False) as proc:
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
    """Flat RDF/XML -> the output of a bundled export format (see FORMATS)."""
    return transform(flat_rdfxml, NESTED, *FORMATS[name], params=params)


def nest(flat_rdfxml: str, params: dict[str, str] | None = None) -> str:
    """Flat RDF/XML -> nested RDF/XML with collections. `params` are the parameters of the
    stylesheet (`record_uri`: the resource the document is about comes first)."""
    return transform(flat_rdfxml, NESTED, params=params)
