import importlib
import pkgutil
from dataclasses import dataclass
from importlib.metadata import entry_points

import pyoxigraph as ox
from pyramid.exceptions import ConfigurationError
from zope.interface import implementer

from gracia import init, uri
from gracia.graph import Graph
from gracia.interfaces import IGraciaType, known_types
from gracia.resources import Record, Type
from gracia.utils import assets
from gracia.views import record as record_views

SYSTEM_PACKAGES = (
    "gracia.types.repository",
    "gracia.types.type",
    "gracia.types.user",
    "gracia.types.group",
)
ENTRY_POINT_GROUP = "gracia.content_types"
_N = ox.NamedNode
_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"


@dataclass(frozen=True)
class ContentType:
    package: str
    name: str
    slug: str
    class_uri: str
    schema: str
    assets: str  # the asset spec of its directory: gracia.types:person/
    doc: str | None = (
        # the file in doc/ that makes the docs of its records (`gracia:docTemplate`)
        None
    )


SYSTEM_TYPES = (
    ContentType(
        "gracia.types.repository",
        "Repository",
        "repository",
        uri.REPOSITORY_CLASS,
        "Repository-full-01.ttl",
        "gracia.types:repository/",
    ),
    ContentType(
        "gracia.types.type",
        "Type",
        "type",
        uri.TYPE_CLASS,
        "Type-full-01.ttl",
        "gracia.types:type/",
    ),
    ContentType(
        "gracia.types.user",
        "User",
        "user",
        uri.USER_CLASS,
        "User-full-01.ttl",
        "gracia.types:user/",
    ),
    ContentType(
        "gracia.types.group",
        "UserGroup",
        "group",
        uri.GROUP_CLASS,
        "UserGroup-full-01.ttl",
        "gracia.types:group/",
    ),
)


def assets_spec(package: str) -> str:
    parent, _, name = package.rpartition(".")
    return f"{parent}:{name}/" if parent else f"{name}:"


def describe(package: str) -> ContentType:
    module = importlib.import_module(package)
    descriptor = getattr(module, "TYPE", None)
    if not isinstance(descriptor, dict):
        raise ValueError(f"{package} has no TYPE: it is not a content type")
    missing = {"name", "slug", "class", "schema"} - descriptor.keys()
    if missing:
        raise ValueError(f"the TYPE of {package} lacks {', '.join(sorted(missing))}")
    found = ContentType(
        package=package,
        name=descriptor["name"],
        slug=descriptor["slug"],
        class_uri=descriptor["class"],
        schema=descriptor["schema"],
        assets=assets_spec(package),
        doc=descriptor.get("doc"),
    )
    if found.slug in ("repository", "type", "user", "group") or found.name in (
        "Repository",
        "Type",
        "User",
        "UserGroup",
    ):
        raise ValueError(f"{package}: {found.name}/{found.slug} is a system type")
    init.type_turtle("rec:type:1", found)  # checks every field
    try:
        assets.resolve(assets.schema_spec(found.assets, found.schema, found.name))
    except (FileNotFoundError, ValueError) as err:
        raise ValueError(
            f"{package}: the schema {found.schema} is not a file of it ({err})"
        ) from None
    if found.doc:
        try:
            assets.resolve(assets.join(found.assets, assets.DOC_DIR + found.doc))
        except (FileNotFoundError, ValueError) as err:
            raise ValueError(
                f"{package}: the doc template {found.doc} is not a file of it ({err})"
            ) from None
    return found


def _packages(extra=()) -> list[str]:
    import gracia.types as here

    found = [
        f"gracia.types.{m.name}" for m in pkgutil.iter_modules(here.__path__) if m.ispkg
    ]
    found = [p for p in found if p not in SYSTEM_PACKAGES]
    found += [
        entry.value.split(":")[0] for entry in entry_points(group=ENTRY_POINT_GROUP)
    ]
    found += [p for p in extra if p not in SYSTEM_PACKAGES]
    return list(dict.fromkeys(found))


def available(extra=()) -> dict[str, ContentType]:
    found: dict[str, ContentType] = {}
    for package in _packages(extra):
        module = importlib.import_module(package)
        if not isinstance(getattr(module, "TYPE", None), dict):
            continue
        content_type = describe(package)
        if content_type.slug in found:
            raise ValueError(f"two content types with the slug {content_type.slug!r}")
        found[content_type.slug] = content_type
    return found


def listed(settings: dict, key: str) -> list[str]:
    return settings.get(key, "").split()


def content_types(registry) -> dict:
    return {k.slug: k for k in known_types(registry) if k.slug and not k.system}


def find_content_type(registry, name: str):
    known = content_types(registry)
    found = known.get(name) or next(
        (k for k in known.values() if k.package == name), None
    )
    if found is None:
        raise ValueError(
            f"no content type {name!r} (there are: "
            f"{', '.join(sorted(known)) or 'none'}; "
            "one that is not here has to be listed in gracia.types, or be an entry "
            "point of "
            f"the group {ENTRY_POINT_GROUP})"
        )
    return found


def enabled(registry, settings: dict) -> list:
    return [
        find_content_type(registry, name)
        for name in listed(settings, "gracia.content_types")
    ]


def packages(settings: dict) -> list[str]:
    extra = listed(settings, "gracia.types")
    found = [*SYSTEM_PACKAGES, *(c.package for c in available(extra).values()), *extra]
    return list(dict.fromkeys(found))


def add_content_type(store, repo_id: str, content_type: ContentType) -> str | None:
    repo_uri = uri.repository_uri()
    subject = _N(repo_uri)
    resource = store.get(repo_uri)
    if not len(resource):
        raise LookupError(f"repository {repo_id} has no repository record")
    content = resource.value(subject, _N(uri.RESOURCE))
    head = resource.value(content, _N(uri.TYPES)) if content is not None else None
    classes = [p.value for p in resource.walk_list(head)] if head is not None else []
    by_class = store.records_with_resource_uris(
        classes
    )  # the list has the classes: the resource uris of the types
    records = [by_class[c] for c in classes if c in by_class]
    existing = store.get_multi(records)
    for record in records:
        found = existing.value(_N(record), _N(uri.RESOURCE))
        slug = existing.value(found, _N(uri.SLUG)) if found is not None else None
        if slug is not None and slug.value == content_type.slug:
            return None
    record = uri.record_uri("type", store.mint_id("type"))
    first, rest, nil = (_N(_RDF + n) for n in ("first", "rest", "nil"))
    cells = {q.subject for q in resource if q.predicate in (first, rest)}
    graph = next(iter(resource.quads_for_subject(subject))).graph_name
    kept = [
        q
        for q in resource
        if q.subject not in cells
        and not (q.subject == content and q.predicate == _N(uri.TYPES))
    ]
    items = [*classes, content_type.class_uri]
    nodes = [ox.BlankNode() for _ in items]
    listing = [ox.Quad(content, _N(uri.TYPES), nodes[0], graph)]
    for position, (node, item) in enumerate(zip(nodes, items, strict=True)):
        following = nodes[position + 1] if position + 1 < len(nodes) else nil
        listing += [
            ox.Quad(node, first, _N(item), graph),
            ox.Quad(node, rest, following, graph),
        ]
    type_record = init.type_turtle(record, content_type)
    parsed = ox.parse(
        ("@prefix gracia: <https://gracia.pub/ns/> .\n" + type_record).encode(),
        format=ox.RdfFormat.TURTLE,
    )
    store.put_multi(
        Graph(
            ox.Dataset(
                [
                    *kept,
                    *listing,
                    *(ox.Quad(q.subject, q.predicate, q.object, graph) for q in parsed),
                ]
            )
        )
    )
    return record


# Registration. A record is a `resources.Record` unless a class is registered for the
# class of its
# type, by the `includeme` of the type's package:
#
#     config.add_record_class(uri.USER_CLASS, UserRecord)
#
# The registration is by the rdf class a type describes (`gracia:class`), so it does not
# depend on
# a repository or on names. An extension package registers its own the same way. A
# record class
# changes what a record does through the hooks of `Record` (`extra_fields`, `check`,
# `apply`), so
# the generic views serve it without views of its own; a view registered for the class
# (`@view_config(context=UserRecord, ...)`) overrides the generic one.


@implementer(IGraciaType)
class TypeInfo:
    def __init__(self, class_uri: str):
        self.class_uri = class_uri
        self.name = self.slug = self.schema = self.assets = self.package = None
        self.doc: str | None = None  # the doc template its type record gets
        self.system = False
        self.record_class = None

    def __repr__(self) -> str:
        return f"<TypeInfo {self.slug or self.class_uri}>"


def _info(registry, class_uri: str) -> TypeInfo:
    info = registry.queryUtility(IGraciaType, class_uri)
    if info is None:
        info = TypeInfo(class_uri)
        registry.registerUtility(info, IGraciaType, name=class_uri)
    return info


def add_gracia_type(config, content_type, system: bool = False) -> None:

    def register():
        registry = config.registry
        info = _info(registry, content_type.class_uri)
        if info.package not in (None, content_type.package):
            raise ConfigurationError(
                f"{content_type.class_uri} is the class of {info.package} and of "
                f"{content_type.package}"
            )
        taken = registry.queryUtility(IGraciaType, content_type.slug)
        if taken is not None and taken is not info:
            raise ConfigurationError(
                f"the slug {content_type.slug!r} is the slug of {taken.package} and of "
                f"{content_type.package}"
            )
        info.name, info.slug, info.schema = (
            content_type.name,
            content_type.slug,
            content_type.schema,
        )
        info.assets, info.package, info.system = (
            content_type.assets,
            content_type.package,
            system,
        )
        info.doc = getattr(content_type, "doc", None)
        registry.registerUtility(info, IGraciaType, name=content_type.slug)

    config.action(("gracia type", content_type.class_uri), register)
    config.action(
        ("gracia type slug", content_type.slug), None
    )  # two types, one slug: a conflict


class TypeClassPredicate:
    def __init__(self, value, config):
        self.value = value

    def text(self):
        return f"type_class = {self.value}"

    phash = text

    def __call__(self, context, request):
        return getattr(context, "class_uri", None) == self.value


def add_record_class(config, class_uri: str, cls: type[Record]) -> None:

    def register():
        _info(config.registry, class_uri).record_class = cls

    config.action(("record class", class_uri), register)
    if cls.edit_permission != Record.edit_permission:
        config.add_view(
            record_views.RecordView,
            attr="edit",
            context=cls,
            name="edit",
            permission=cls.edit_permission,
        )
    if cls.add_permission != Record.add_permission:
        config.add_view(
            record_views.RecordView,
            attr="add",
            context=Type,
            name="add",
            type_class=class_uri,
            permission=cls.add_permission,
        )
