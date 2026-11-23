import contextlib
import faulthandler
import hashlib
import queue
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from concurrent.futures import Future
from functools import wraps
from importlib.resources import files
from pathlib import Path
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
                self._executable = _compile(
                    self._processor.new_xslt30_processor(), text
                )
        except PySaxonApiError as err:
            self._close()
            raise XsltError(" ".join(str(err).split())) from err
        except BaseException:
            self._close()
            raise

    @_guarded
    def transform(self, xml_text: str, params: dict[str, str] | None = None) -> str:
        return self._owner.call(self._transform, xml_text, params)

    def _transform(self, xml_text: str, params: dict[str, str] | None) -> str:
        self.threads.add(threading.get_ident())
        if self._executable is None:
            raise XsltError("the stylesheet was closed")
        # the executable keeps them: a run must not see the last one's
        self._executable.clear_parameters()
        for name, value in (params or {}).items():
            self._executable.set_parameter(
                name,
                self._processor.make_string_value(value),  # ty: ignore[missing-union-attribute]
            )
        node = self._processor.parse_xml(xml_text=xml_text)  # ty: ignore[missing-union-attribute]
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
