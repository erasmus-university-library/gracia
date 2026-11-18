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
    """`name` inside the directory spec `prefix` (`gracia.types:user/` + `view.xsl`).
    The name stays inside the directory: no `..`, no leading slash, no empty parts."""
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
    """The spec of the schema file `schema` of the type named `type`, in a directory
    spec. The name has to be `<type>-<profile>-<revision>.ttl` with the name of this
    type, so that a type cannot be pointed at the files of another."""
    found = SCHEMA_NAME.match(schema or "")
    if not found or found[1] != res_type:
        raise ValueError(
            f"not a schema file of {res_type}: {schema!r} (expected "
            f"{res_type}-<profile>-<revision>.ttl)"
        )
    return join(prefix, SCHEMA_DIR + schema)  # ty: ignore[unsupported-operator]


def doc_href(slug: str, name: str) -> str:
    """The `type:` address of the doc stylesheet `name` of the type `slug`; ValueError
    for a name that is not `doc-<profile>-<revision>.xsl`."""
    if not DOC_NAME.match(name or ""):
        raise ValueError(
            f"not a doc stylesheet: {name!r} (expected doc-<profile>-<revision>.xsl)"
        )
    return f"type:{slug}/{DOC_DIR}{name}"


def resolve(spec: str) -> Path:
    """The file an asset spec names, with the overrides of the current registry
    applied."""
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
    """The node shape in a Turtle file that targets `class_uri` (parsed once per file
    version)."""
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


def record_shape(
    content=None, class_uri: str | None = None
) -> tuple[Graph, ox.NamedNode]:
    """The shape of a record (`model/record.shacl.ttl`) with the shape of its content
    embedded on `gracia:resource` (`sh:node`): (a new graph, the node of the record
    shape). `content` is what `shape` gives, or None for a record whose type has no
    shape. The content has to be of the class of the type (`sh:class`): its shape only
    picks the nodes that are (`sh:targetClass`), so without this a node of another class
    would not be looked at."""
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
            data.add(
                ox.Quad(prop, sh("class"), ox.NamedNode(class_uri), ox.DefaultGraph())
            )
    return data, node


def shape(prefix: str | None, schema: str | None, class_uri: str, res_type: str):
    """The shape of `class_uri` in the schema file `schema` of the type named `type`, or
    None (logged) when the type has no assets or no schema, or they cannot be read: such
    a type still works, it just has no form."""
    if not prefix or not schema:
        return None
    try:
        return load_shape(resolve(schema_spec(prefix, schema, res_type)), class_uri)
    except (FileNotFoundError, ValueError, SyntaxError) as err:
        log.warning("no shape for %s from %s %s: %s", class_uri, prefix, schema, err)
        return None


@dataclass(frozen=True)
class Info:
    """What is known of a file: its spec, where it is, and its version."""

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
    """The file `spec` names, with its media type, size and checksum. The checksum is
    computed again only when the file (the one the overrides give) has changed."""
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
    """The files of a type that exist in the directory spec `prefix`: the schema file
    `schema` (of the type named `type`) and the stylesheet. A schema that is not a valid
    name or does not exist is left out."""
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
    """The names of the schema files in `shacl/` of the directory spec `prefix`, by name
    (with the overrides of the current registry applied)."""
    try:
        asset = AssetResolver().resolve(join(prefix, SCHEMA_DIR.rstrip("/")))
        if not asset.isdir():
            return []
        names = asset.listdir()
    except (ImportError, KeyError):
        return []
    return sorted(n for n in names if SCHEMA_NAME.match(n))


def docs_of(prefix: str) -> list[str]:
    """The names of the doc stylesheets in `doc/` of the directory spec `prefix`, by
    name."""
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
    """One digest over the files of a type (see `files_of`): it changes when one
    changes, is added or goes, and when the type switches to another schema file. Empty
    when there are no files. A record's ETag takes it in."""
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
    """Make a Pyramid registry current, so that its overrides apply to what is resolved
    inside. A request does this by itself; a command or a script has to."""
    manager.push({"registry": registry, "request": None})
    try:
        yield
    finally:
        manager.pop()
