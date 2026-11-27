import functools
import hashlib
import importlib.resources
import logging
import re
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import pyoxigraph as ox
from pyramid.path import AssetResolver
from pyramid.threadlocal import manager

from gracia import uri
from gracia.graph import Graph

log = logging.getLogger(__name__)

SH_NODE_SHAPE = "http://www.w3.org/ns/shacl#NodeShape"
SH_TARGET_CLASS = "http://www.w3.org/ns/shacl#targetClass"

# The files of a type, by name (a directory is never listed: listing an overridden
# directory shows
# only the files of the override): its stylesheet, and the schema its data names
# (`gracia:schemaTemplate`)
# in the directory `shacl/`, as <Type>-<profile>-<revision>.ttl: Person-full-03.ttl,
# Person-minimal-01.ttl. A new revision is a new file, and switching to it is a change
# of data.
VIEW = "view.xsl"
SCHEMA_DIR = "shacl/"
SCHEMA_NAME = re.compile(r"^([A-Za-z][A-Za-z0-9]*)-([a-z][a-z0-9_]*)-([0-9]{2,})\.ttl$")
# the stylesheets that make the doc of a record (`gracia:docTemplate`) are in `doc/`, as
# doc-<profile>-<revision>.xsl; the one of the type `type`, `doc-base-01.xsl`, is what
# the others
# import
DOC_DIR = "doc/"
DOC_NAME = re.compile(r"^doc-([a-z][a-z0-9_]*)-([0-9]{2,})\.xsl$")
DOC_BASE = "type:type/" + DOC_DIR + "doc-base-01.xsl"
# the stylesheets that make the page of a record of a type (`gracia:viewTemplate`) are in
# `view/`, as <type>-<profile>-<revision>.xslt; the one with the profile `base` is what
# the others import, and is not one to choose
VIEW_DIR = "view/"
VIEW_NAME = re.compile(r"^([a-z][a-z0-9]*)-([a-z][a-z0-9_]*)-([0-9]{2,})\.xslt$")
MEDIA_TYPES = {
    ".ttl": "text/turtle",
    ".xsl": "application/xslt+xml",
    ".xslt": "application/xslt+xml",
    ".css": "text/css",
    ".js": "text/javascript",
    ".json": "application/json",
}

_SHAPES: dict[tuple[str, int, str], tuple[Graph, ox.NamedNode | ox.BlankNode]] = {}


def join(prefix: str, name: str) -> str:
    parts = name.split("/")
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or any(p in ("", ".", "..") for p in parts)
    ):
        raise ValueError(f"not a path inside a type: {name!r}")
    if not prefix.endswith(("/", ":")):
        prefix += "/"
    return prefix + name


def schema_spec(prefix: str, schema: str | None, res_type: str) -> str:
    found = SCHEMA_NAME.match(schema or "")
    if not found or found[1] != res_type:
        raise ValueError(
            f"not a schema file of {res_type}: {schema!r} (expected "
            f"{res_type}-<profile>-<revision>.ttl)"
        )
    return join(prefix, SCHEMA_DIR + schema)  # ty: ignore[unsupported-operator]


def doc_href(slug: str, name: str) -> str:
    if not DOC_NAME.match(name or ""):
        raise ValueError(
            f"not a doc stylesheet: {name!r} (expected doc-<profile>-<revision>.xsl)"
        )
    return f"type:{slug}/{DOC_DIR}{name}"


def resolve(spec: str) -> Path:
    try:
        asset = AssetResolver().resolve(spec)
        found = asset.exists() and not asset.isdir()
    except (ImportError, KeyError):  # the package is not installed
        found = False
    if not found:
        raise FileNotFoundError(f"no such asset: {spec}")
    return Path(asset.abspath())


def read_text(spec: str) -> str:
    return resolve(spec).read_text(encoding="utf-8")


def load_shape(path: Path, class_uri: str) -> tuple[Graph, ox.NamedNode | ox.BlankNode]:
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, class_uri)
    if key not in _SHAPES:
        data = Graph(
            ox.Dataset(ox.parse(path.read_bytes(), format=ox.RdfFormat.TURTLE))
        )
        shape_type, target = ox.NamedNode(SH_NODE_SHAPE), ox.NamedNode(SH_TARGET_CLASS)
        found = [
            q.subject
            for q in data.quads_for_object(shape_type)
            if q.predicate.value == uri.RDF_TYPE
            and ox.NamedNode(class_uri) in data.objects(q.subject, target)
        ]
        if not found:
            raise ValueError(
                f"{path}: no sh:NodeShape with sh:targetClass <{class_uri}>"
            )
        _SHAPES[key] = (data, found[0])
    return _SHAPES[key]


@functools.lru_cache(maxsize=1)
def _record_shapes() -> tuple[ox.Quad, ...]:
    text = (
        importlib.resources.files("gracia") / "assets" / "model" / "record.shacl.ttl"
    ).read_bytes()
    return tuple(ox.parse(text, format=ox.RdfFormat.TURTLE))


# an rdf list of [ sh:class <c> ] for each of the classes, added to the data
_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"


def _class_alternatives(data: Graph, sh, classes: list[str]) -> ox.BlankNode:
    first, rest, nil = (
        ox.NamedNode(_RDF + "first"),
        ox.NamedNode(_RDF + "rest"),
        ox.NamedNode(_RDF + "nil"),
    )
    home = ox.DefaultGraph()
    head = previous = None
    for class_uri in classes:
        item, cell = ox.BlankNode(), ox.BlankNode()
        data.add(ox.Quad(item, sh("class"), ox.NamedNode(class_uri), home))
        data.add(ox.Quad(cell, first, item, home))
        if previous is None:
            head = cell
        else:
            data.add(ox.Quad(previous, rest, cell, home))
        previous = cell
    assert previous is not None and head is not None
    data.add(ox.Quad(previous, rest, nil, home))
    return head


def record_shape(
    content=None, class_uri: str | None = None
) -> tuple[Graph, ox.NamedNode]:
    data = Graph(ox.Dataset(_record_shapes()))
    node = ox.NamedNode(uri.RECORD_SHAPE)
    sh = lambda name: ox.NamedNode("http://www.w3.org/ns/shacl#" + name)  # noqa: E731
    for prop in data.objects(node, sh("property")):
        if ox.NamedNode(uri.RESOURCE) not in data.objects(prop, sh("path")):
            continue
        if content is not None:
            content_data, content_node = content
            for quad in content_data:
                data.add(quad)
            data.add(ox.Quad(prop, sh("node"), content_node, ox.DefaultGraph()))
        if class_uri is not None:
            # the content has the class of the type or, when its shape names more
            # (sh:targetClass: the subclasses it can have, one to a resource), one of
            # those
            classes = [class_uri]
            if content is not None:
                content_data, content_node = content
                classes += [
                    o.value
                    for o in content_data.objects(content_node, sh("targetClass"))
                    if isinstance(o, ox.NamedNode) and o.value != class_uri
                ]
            if len(classes) == 1:
                data.add(
                    ox.Quad(
                        prop, sh("class"), ox.NamedNode(class_uri), ox.DefaultGraph()
                    )
                )
            else:
                data.add(
                    ox.Quad(
                        prop,
                        sh("or"),
                        _class_alternatives(data, sh, classes),
                        ox.DefaultGraph(),
                    )
                )
    return data, node


def shape(prefix: str | None, schema: str | None, class_uri: str, res_type: str):
    if not prefix or not schema:
        return None
    try:
        return load_shape(resolve(schema_spec(prefix, schema, res_type)), class_uri)
    except (FileNotFoundError, ValueError, SyntaxError) as err:
        log.warning("no shape for %s from %s %s: %s", class_uri, prefix, schema, err)
        return None


@dataclass(frozen=True)
class Info:
    spec: str
    path: Path
    media_type: str
    size: int
    checksum: str  # md5:<hex>: a version for caches, not a signature


_INFO: dict[tuple[str, int, int], tuple[str, str]] = {}


def _checksum(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return f"md5:{digest.hexdigest()}"


def info(spec: str) -> Info:
    path = resolve(spec)
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key not in _INFO:
        media = MEDIA_TYPES.get(path.suffix, "application/octet-stream")
        _INFO[key] = (media, _checksum(path))
    media, checksum = _INFO[key]
    return Info(spec, path, media, stat.st_size, checksum)


def files_of(
    prefix: str, schema: str | None = None, res_type: str | None = None
) -> list[Info]:
    specs = []
    if schema and res_type:
        with suppress(ValueError):
            specs.append(schema_spec(prefix, schema, res_type))
    specs.append(join(prefix, VIEW))
    found = []
    for spec in specs:
        try:
            found.append(info(spec))
        except FileNotFoundError:
            continue
    return found


def schemas_of(prefix: str) -> list[str]:
    try:
        asset = AssetResolver().resolve(join(prefix, SCHEMA_DIR.rstrip("/")))
        if not asset.isdir():
            return []
        names = asset.listdir()
    except (ImportError, KeyError):
        return []
    return sorted(n for n in names if SCHEMA_NAME.match(n))


def view_spec(prefix: str, name: str) -> str:
    """The asset spec of a view stylesheet of a type, whose files are at `prefix`."""
    found = VIEW_NAME.match(name or "")
    if not found or found[2] == "base":
        raise ValueError(
            f"not a view stylesheet: {name!r} (expected <type>-<profile>-<revision>.xslt)"
        )
    return join(prefix, VIEW_DIR + name)


def views_of(prefix: str) -> list[str]:
    try:
        asset = AssetResolver().resolve(join(prefix, VIEW_DIR.rstrip("/")))
        if not asset.isdir():
            return []
        names = asset.listdir()
    except (ImportError, KeyError):
        return []
    found = []
    for name in sorted(names):
        match = VIEW_NAME.match(name)
        if match and match[2] != "base":
            found.append(name)
    return found


def docs_of(prefix: str) -> list[str]:
    try:
        asset = AssetResolver().resolve(join(prefix, DOC_DIR.rstrip("/")))
        if not asset.isdir():
            return []
        names = asset.listdir()
    except (ImportError, KeyError):
        return []
    return sorted(n for n in names if DOC_NAME.match(n))


def version(
    prefix: str | None, schema: str | None = None, res_type: str | None = None
) -> str:
    if not prefix:
        return ""
    files = files_of(prefix, schema, res_type)
    if not files:
        return ""
    digest = hashlib.md5(usedforsecurity=False)
    for item in files:
        digest.update(f"{item.spec}\0{item.checksum}\n".encode())
    return digest.hexdigest()


@contextmanager
def using(registry):
    manager.push({"registry": registry, "request": None})
    try:
        yield
    finally:
        manager.pop()
