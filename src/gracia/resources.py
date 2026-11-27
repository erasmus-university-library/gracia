import datetime
import logging
import re
import zoneinfo
from functools import cached_property
from pathlib import Path

import colander
import deform.widget
import pyoxigraph as ox
from pyramid.authorization import ALL_PERMISSIONS, DENY_ALL, Allow, Everyone
from pyramid.threadlocal import get_current_request
from zope.interface import implementer

from gracia import dates, forms, hints, predicates, templates, traversal, uri
from gracia.graph import Graph
from gracia.interfaces import IDeformView, IExtension, IGraciaType, IRepository
from gracia.store import new_store
from gracia.traversal import traversable
from gracia.utils import assets, passwords
from gracia.utils.validate import validate_userid

log = logging.getLogger(__name__)
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_NUMBER = re.compile(r"^[1-9][0-9]{0,17}$")
_N = ox.NamedNode

# what `gracia repo set` sets: the timezone and the locale of dates
REPOSITORY_SETTINGS = {
    "timezone": uri.TIMEZONE,
    "locale": uri.LOCALE,
    "layout_template": uri.LAYOUT_TEMPLATE,
    "type_template": uri.TYPE_TEMPLATE,
    "record_template": uri.RECORD_TEMPLATE,
}

# the stylesheets of the pages, when the repository has none of its own: the setting that
# can name another, and the one that comes with gracia (`views/xslt.py`)
SKINS = {
    "layout": uri.LAYOUT_TEMPLATE,
    "type": uri.TYPE_TEMPLATE,
    "record": uri.RECORD_TEMPLATE,
}

_ADMINS = (Allow, "group:admin", ALL_PERMISSIONS)
TYPE_ACLS = {
    uri.REPOSITORY_CLASS: [_ADMINS, DENY_ALL],
    uri.USER_CLASS: [
        _ADMINS,
        (Allow, "group:manager", ["view", "super_edit"]),
        DENY_ALL,
    ],
    uri.GROUP_CLASS: [_ADMINS, DENY_ALL],
    # the types define the repository: everybody may read them, only admins change them
    uri.TYPE_CLASS: [_ADMINS, (Allow, Everyone, "view"), DENY_ALL],
}
PRIVATE_CLASSES = frozenset({uri.USER_CLASS, uri.GROUP_CLASS, uri.REPOSITORY_CLASS})


class RecordDeleted(LookupError):
    pass


class Node:
    def __init__(self, repository, parent=None):
        self.repository: Repository = repository
        self.__parent__ = repository if parent is None else parent

    @property
    def store(self):
        return self.repository.store

    @property
    def registry(self):
        return self.repository.registry

    @property
    def application(self):
        return self.repository.application


@traversable(prefix="rec:")
class Resolved(Node):
    def __init__(self, repository: "Repository", key: str):
        super().__init__(repository)
        parsed = uri.parse_record_uri(key)
        if parsed is None or parsed[0] not in repository.types:
            raise KeyError(key)
        self.__name__ = key
        self.record = repository.types[parsed[0]][str(parsed[1])]


def edit_address(request, repository, record: str, slug: str | None) -> str:
    """Where the form of a record is, for a dialog (`embed`): none, when the person may
    not edit records of its type."""
    parsed = uri.parse_record_uri(record)
    if parsed is None:
        return ""
    try:
        node = repository[parsed[0]]
    except KeyError:
        return ""
    if not request.has_permission("edit", node):
        return ""
    return request.resource_url(
        node, slug or str(parsed[1]), "edit", query={"embed": "1"}
    )


@implementer(IRepository)
class Repository:
    def __init__(self, registry, repo_id: str, parent):
        self.registry = registry
        self.__name__ = repo_id
        self.__parent__ = parent
        self._owns_store = True
        self._extensions: dict[str, object] = {}  # by name, for this node
        self.extension_states: dict | None = None  # read by `gracia.extensions`
        self.extension_settings: dict = {}  # likewise

    def use_store(self, store) -> None:
        self.close()
        self.store = store
        self._owns_store = False
        self.configure()

    @property
    def repository(self) -> "Repository":
        return self

    @property
    def application(self):
        return self.__parent__

    @property
    def uri(self) -> str:
        return uri.repository_uri()

    @cached_property
    def store(self):
        db = self.application.db
        store = self.store = new_store(
            self.application.database, self.__name__, db, self.application.cache
        )
        store.repository = self
        self._owns_store = db is None
        self.configure()
        request = get_current_request()
        if request is not None and db is None:
            request.add_finished_callback(lambda request: self.close())
        return store

    def sync_roles(self) -> None:
        self.__dict__.pop("types", None)
        self.application.cache.invalidate(self.__name__)
        self.configure()
        self.store.sync()

    def configure(self) -> None:
        self.store.configure(lambda: self.types.values())

    def __enter__(self) -> "Repository":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        store = self.__dict__.pop("store", None)
        if store is not None and self._owns_store:
            store.close()
        self._owns_store = True

    def _record_graph(self) -> Graph:
        graph = self.store.get(self.uri)
        if not len(graph):
            raise LookupError(f"repository {self.__name__} has no repository record")
        return graph

    def record(self) -> "Record":
        graph = self._record_graph()
        return record_class(self.registry, uri.REPOSITORY_CLASS)(
            self, "1", self.uri, graph
        )

    def _setting(self, predicate: str) -> str:
        try:
            record = self.record()
        except LookupError:
            return ""
        if record.resource is None:
            return ""
        found = record.graph.value(record.resource, _N(predicate))
        return found.value if found is not None and found.value else ""

    def set_settings(self, values: dict[str, str]) -> None:
        """Sets the settings of the repository (`REPOSITORY_SETTINGS`: the timezone and
        the locale, and the stylesheets of the pages); an empty value clears one (UTC, ISO
        8601, gracia's own). A value that is not valid is a ValueError."""
        for name, value in values.items():
            if name not in REPOSITORY_SETTINGS:
                raise ValueError(
                    f"no setting {name!r} (there are: {', '.join(REPOSITORY_SETTINGS)})"
                )
            if name == "locale" and value not in dates.FORMATS:
                raise ValueError(f"{value!r} is not a locale of dates")
            if name.endswith("_template") and value:
                try:
                    assets.resolve(value)
                except FileNotFoundError:
                    raise ValueError(
                        f"{value!r} is not a stylesheet that is there"
                    ) from None
            if name == "timezone" and value:
                try:
                    zoneinfo.ZoneInfo(value)
                except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
                    raise ValueError(f"{value!r} is not a time zone") from None
        record = self.record()
        subject, graph = record.resource, record.graph
        if subject is None:
            raise LookupError(
                f"the record of repository {self.__name__} has no content"
            )
        for name, value in values.items():
            graph.discard_plain(subject, REPOSITORY_SETTINGS[name])
            if value:
                graph.add_value(subject, REPOSITORY_SETTINGS[name], ox.Literal(value))
        self.store.put_multi(graph)

    def timezone(self) -> datetime.tzinfo:
        try:
            return (
                zoneinfo.ZoneInfo(name)
                if (name := self._setting(uri.TIMEZONE))
                else datetime.UTC
            )
        except (
            zoneinfo.ZoneInfoNotFoundError,
            ValueError,
            OSError,
        ):  # a name this system does not know
            return datetime.UTC

    def shape_types(self, shape_uris) -> list["Type"]:
        """The types whose shape is one of the shapes (`ex:PersonShape`): what the
        resources a reference points at are records of."""
        wanted = {s.value if hasattr(s, "value") else str(s) for s in shape_uris}
        found = []
        for res_type in self.types.values():
            shapes = res_type.shacl_shape(include_record_shape=False)
            if shapes is not None and getattr(shapes[1], "value", None) in wanted:
                found.append(res_type)
        return found

    def shape_classes(self, shape_uris) -> list[str]:
        """The classes of the resources a reference points at: the target classes of the
        shapes of those types."""
        found: set[str] = set()
        for res_type in self.shape_types(shape_uris):
            graph, primary = res_type.shacl_shape(include_record_shape=False)
            found.update(
                o.value for o in graph.objects(primary, _N(uri.SH_TARGET_CLASS))
            )
        return sorted(found)

    def stylesheet(self, kind: str, own: str | None = None) -> str:
        """The stylesheet of a kind of page (`layout`, `type` or `record`): the one the
        repository names, as an asset spec or a path, else `own` (the one of the type of
        the record), else the one of gracia."""
        named = self._setting(SKINS[kind])
        if named:
            try:
                assets.resolve(named)
                return named
            except FileNotFoundError:  # a bad setting must not stop every page
                log.warning(
                    "%s: the %s stylesheet %s is not there, gracia's is used",
                    self.__name__,
                    kind,
                    named,
                )
        return own or f"gracia:assets/xslt/skin/{kind}.xslt"

    def locale(self) -> str:
        """The locale of dates (a key of `dates.FORMATS`); ISO 8601 if none."""
        name = self._setting(uri.LOCALE)
        return name if name in dates.FORMATS else ""

    @cached_property
    def types(self) -> dict[str, "Type"]:
        cache = self.application.cache
        cache.refresh()  # a repository that was written has dropped what was kept
        kept = cache.registry.setdefault("types", {})
        if self.__name__ not in kept:
            kept[self.__name__] = self._type_records()
        records, found = kept[self.__name__]
        found_types = {}
        for record in records:
            res_type = Type.read(self, record, found)
            if res_type is not None:
                found_types[res_type.slug] = res_type
        return found_types

    def _type_records(self) -> tuple[list[str], Graph]:
        try:
            graph = self._record_graph()
        except LookupError:
            return [], Graph()
        node = graph.value(self.uri, uri.RESOURCE)
        if node is None:
            return [], Graph()
        head = graph.value(node, _N(uri.TYPES))
        classes = [p.value for p in graph.walk_list(head)] if head is not None else []
        by_class = self.store.records_with_resource_uris(
            classes
        )  # the list has the classes: the resource uris
        records = [by_class[c] for c in classes if c in by_class]
        return records, self.store.get_multi(records)

    def resolve_type(self, href: str) -> Path:
        slug, _, name = href.removeprefix("type:").partition("/")
        res_type = self.types.get(slug)
        if res_type is None or not res_type.assets:
            raise LookupError(
                f"no type {slug!r} with files in repository {self.__name__}"
            )
        return assets.resolve(assets.join(res_type.assets, name))

    def prefix_map(self) -> dict[str, str]:
        items = self.extension("ns_prefix").settings["prefixes"]
        return {item["prefix"]: item["namespace"] for item in items}

    def extension(self, name: str):
        if name not in self._extensions:
            found = self.registry.queryAdapter(self, IExtension, name)
            if found is None:
                raise KeyError(name)
            self._extensions[name] = found
        return self._extensions[name]

    def extensions(self) -> list:
        found = self.registry.getAdapters((self,), IExtension)
        return sorted(
            (e for _name, e in found), key=lambda e: (e.title or e.name, e.name)
        )

    def __getitem__(self, name: str):
        found = traversal.child(self, name)
        if found is not None:
            return found
        try:
            return self.types[name]
        except KeyError:
            raise KeyError(name) from None

    def login(self, userid: str, password: str) -> set[str] | None:
        try:
            validate_userid(userid)
        except ValueError:
            return passwords.verify_nothing(password) or None  # ty: ignore[invalid-explicit-return]
        store = self.store
        records = store.records_with(
            "identifiers", userid, rec_type="user"
        )  # its key info
        graph = store.get(records[0]) if records else Graph()
        resource = graph.value(_N(records[0]), _N(uri.RESOURCE)) if records else None
        stored = graph.value(resource, _N(uri.CREDENTIALS)) if resource else None
        if stored is None:
            return passwords.verify_nothing(password) or None  # ty: ignore[invalid-explicit-return]
        if not passwords.verify_password(password, stored.value):
            return None
        by_resource = store.records_with_resource_uris(
            [g.value for g in graph.objects(resource, _N(uri.MEMBER_OF))]
        )
        groups = list(
            by_resource.values()
        )  # `gracia:memberOf` has the resource uris of the groups
        found = store.get_multi(groups)
        names = set()
        for group in groups:
            inner = found.value(_N(group), _N(uri.RESOURCE))
            name = found.value(inner, _N(uri.NAME)) if inner else None
            if name is not None:
                names.add(name.value)
        return names


@traversable("jobs")
class Jobs(Node):
    __name__ = "jobs"

    def __init__(self, repository: Repository):
        super().__init__(repository)

    @property
    def __acl__(self):
        from gracia.jobs.job import all_jobs

        grants: dict[str, list[str]] = {}
        for job in all_jobs(self.registry):
            for group in job.groups:
                grants.setdefault(f"group:{group}", ["list_jobs"]).append(
                    job.start_permission
                )
        return [
            _ADMINS,
            *((Allow, group, perms) for group, perms in sorted(grants.items())),
            DENY_ALL,
        ]

    def __getitem__(self, job_id: str) -> "JobRun":
        if not _UUID.match(job_id):
            raise KeyError(job_id)
        from gracia.jobs.db import JobsDb

        with JobsDb(self.application) as db:
            row = db.get(job_id)
        if row is None or row["repo"] != self.repository.__name__:
            raise KeyError(job_id)
        return self.run(row)

    def run(self, row: dict) -> "JobRun":
        run = JobRun(self.repository, row)
        run.__parent__ = self
        return run


@traversable("deform")
class DeformViews(Node):
    __name__ = "deform"

    def __init__(self, repository: Repository):
        super().__init__(repository)

    def __getitem__(self, name: str) -> "DeformView":
        function = self.registry.queryUtility(IDeformView, name)
        if function is None:
            raise KeyError(name)
        view = DeformView(self.repository, name, function)
        view.__parent__ = self
        return view


class DeformView(Node):
    def __init__(self, repository: Repository, name: str, function):
        self.__name__ = name
        super().__init__(repository)
        self.function = function

    @property
    def target(self):
        repository = self.repository
        at = self.function.deform_at
        return repository[at] if at else repository


class JobRun(Node):
    def __init__(self, repository: Repository, row: dict):
        self.__name__ = row["id"]
        super().__init__(repository)
        self.row = row

    @property
    def __acl__(self):
        creator = self.row[
            "created_by"
        ]  # `user:<userid>`; None for a job from the command line
        return [
            *([(Allow, creator, ["view_job", "stop_job"])] if creator else []),
            _ADMINS,
            DENY_ALL,
        ]


class Type(Node):
    def __init__(
        self, parent: Repository, record: str, name: str, slug: str, class_uri: str
    ):
        self.__name__ = slug
        super().__init__(parent)
        self.record, self.name, self.slug, self.class_uri = (
            record,
            name,
            slug,
            class_uri,
        )
        self._shapes: dict[bool, tuple | None] = {}  # by include_record_shape
        self.graph = Graph()  # the type record
        self.assets: str | None = None  # asset spec of the directory with its files
        self.schema: str | None = (
            None  # the file in shacl/ its records follow (`gracia:schemaTemplate`)
        )
        self.doc_template: str | None = (
            None  # the file in doc/ that makes their docs (`gracia:docTemplate`)
        )
        self.view_template: str | None = (
            None  # the file in view/ that makes their pages (`gracia:viewTemplate`)
        )

    @classmethod
    def read(cls, repository: Repository, record: str, graph: Graph) -> "Type | None":
        resource = graph.value(_N(record), _N(uri.RESOURCE))
        if resource is None:
            return None
        fields = [graph.value(resource, _N(p)) for p in (uri.NAME, uri.SLUG, uri.CLASS)]
        if any(f is None for f in fields) or fields[1].value in uri.RESERVED_SLUGS:
            return None
        res_type = cls(
            repository, record, fields[0].value, fields[1].value, fields[2].value
        )
        res_type.graph = graph
        prefix = graph.value(resource, _N(uri.ASSETS))
        res_type.assets = prefix.value if prefix is not None else None
        schema = graph.value(resource, _N(uri.SCHEMA_TEMPLATE))
        res_type.schema = schema.value if schema is not None else None
        doc_template = graph.value(resource, _N(uri.DOC_TEMPLATE))
        res_type.doc_template = doc_template.value if doc_template is not None else None
        view_template = graph.value(resource, _N(uri.VIEW_TEMPLATE))
        res_type.view_template = (
            view_template.value if view_template is not None else None
        )
        return res_type

    def shacl_shape(self, include_record_shape: bool = True):
        if False not in self._shapes:
            self._shapes[False] = assets.shape(
                self.assets, self.schema, self.class_uri, self.name
            )
        if include_record_shape and True not in self._shapes:
            self._shapes[True] = assets.record_shape(
                self._shapes[False], self.class_uri
            )
        return self._shapes[include_record_shape]

    @property
    def view_spec(self) -> str | None:
        """The asset spec of the stylesheet that makes the page of a record of the type,
        if the type has one and it is there."""
        if not self.view_template or not self.assets:
            return None
        try:
            spec = assets.view_spec(self.assets, self.view_template)
            assets.resolve(spec)
        except (FileNotFoundError, ValueError):  # a bad name must not stop every page
            log.warning(
                "%s: the view %s is not a file of the type, the default is used",
                self.slug,
                self.view_template,
            )
            return None
        return spec

    @property
    def number(self) -> int:
        return int(self.record.rsplit(":", 1)[1])

    @cached_property
    def property_roles(self) -> list[tuple[str, str, int]]:
        return hints.property_roles(*self.shacl_shape(), self.class_uri)

    @cached_property
    def icon(self) -> str | None:
        """The Bootstrap icon of the type (the icon role of its shape), if any."""
        shapes = self.shacl_shape(include_record_shape=False)
        return forms.icon(*shapes) if shapes else None

    def property_role_paths(self, role: str) -> list[str]:
        return [path for r, path, _place in self.property_roles if r == role]

    @cached_property
    def sql_hooks(self) -> list[str]:
        return self._sql_plan(*self.shacl_shape())

    def _sql_plan(self, shapes, shape, above: tuple = ()) -> list[str]:
        properties = [
            (path, forms.nested_shape(shapes, prop))
            for path, prop in forms.properties(shapes, shape)
        ]
        statements = []
        for _path, nested in properties:  # what is embedded comes first
            if nested is not None and nested not in (*above, shape):  # a shape that
                statements += self._sql_plan(
                    shapes, nested, (*above, shape)
                )  # is its own
        made: dict[str, list] = {}
        for template in templates.of_shape(shapes, shape):
            made.setdefault(template.target, []).append(template)
        for target, variants in made.items():  # before any label is made
            context = predicates.HookContext(self.repository, self, target)
            statements += [
                s for s in templates.sql_of(variants, context) if s not in statements
            ]
        for path, _nested in properties:
            code = predicates.find(self.registry, path)
            if code is not None:
                context = predicates.HookContext(self.repository, self, path)
                statements += [
                    s for s in (code.on_write_sql(context) or []) if s not in statements
                ]
        return statements

    @property
    def assets_version(self) -> str:
        return assets.version(self.assets, self.schema, self.name)

    @property
    def __acl__(self):
        return TYPE_ACLS.get(self.class_uri, [])

    @property
    def placeholder(self) -> str:
        return uri.record_uri(self.slug, 0)

    def skeleton(self) -> Graph:
        record, content, rdf_type = (
            _N(self.placeholder),
            ox.BlankNode(),
            _N(uri.RDF_TYPE),
        )
        home = ox.DefaultGraph()
        return Graph(
            ox.Dataset(
                [
                    ox.Quad(record, rdf_type, _N(uri.RECORD_CLASS), home),
                    ox.Quad(record, _N(uri.RESOURCE), content, home),
                    ox.Quad(content, rdf_type, _N(self.class_uri), home),
                ]
            )
        )

    def blank_record(self) -> "Record":
        cls = record_class(self.registry, self.class_uri)
        record = cls(self.repository, None, None, Graph())  # ty: ignore[invalid-argument-type]
        record.__parent__ = self
        return record

    def __getitem__(self, key: str) -> "Record":
        if _NUMBER.match(key):
            number = key
        elif uri.SLUG_NAME.match(key):
            found = self.store.slug_lookup(self.slug, key)
            if found is None:
                raise KeyError(key)
            if found[1]:
                raise RecordDeleted(key)
            number = str(found[0] % uri.TYPE_BASE)
        else:
            raise KeyError(key)
        record = self._record(number)
        if record is None:
            if number == key:  # a number that was deleted is a slug of its own
                found = self.store.slug_lookup(self.slug, key)
                if found is not None and found[1]:
                    raise RecordDeleted(key)
            raise KeyError(key)
        return record

    def _record(self, number: str) -> "Record | None":
        record_uri = uri.record_uri(self.slug, int(number))
        graph = self.store.get(record_uri)
        if not len(graph):
            # no such record (the uri has the type: a record of another type has another
            # uri)
            return None
        cls = record_class(self.registry, self.class_uri)
        record = cls(self.repository, number, record_uri, graph)
        record.__parent__ = self
        return record


def _natural(node) -> list:
    """The key to sort blank nodes on their identifiers, the numbers as numbers: `b7_2`
    comes before `b7_10`."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", node.value)]


def _slug_of(graph: Graph, record_uri: str) -> str | None:
    found = graph.value(_N(record_uri), _N(uri.SLUG))
    return found.value if found is not None else None


def record_class(registry, class_uri: str) -> type["Record"]:
    info = registry.queryUtility(IGraciaType, class_uri)
    return (info.record_class if info is not None else None) or Record


class Record(Node):
    hidden = frozenset(
        {uri.CREDENTIALS}
    )  # predicates of the content no form shows or sets
    edit_permission = (
        "edit"  # what the edit view of this class asks for (see gracia.types)
    )
    add_permission = "add"  # what adding a record of its type asks for
    supplies = (
        frozenset()
    )  # required predicates that `apply` sets without a field (a hash)
    source_editable = True  # whether its data can be edited as text (`views/record.py`)

    def __init__(self, repository: Repository, number: str, uri: str, graph: Graph):
        super().__init__(repository)
        self.uri, self.graph = uri, graph
        # the address of a record is its slug when it has one, else its number
        self.__name__ = (_slug_of(graph, uri) if uri else None) or number

    @property
    def resource(self):
        if self.uri is None:
            return None
        return self.graph.value(_N(self.uri), _N(uri.RESOURCE))

    @property
    def __acl__(self):
        res_type = self.type
        if res_type.class_uri in PRIVATE_CLASSES:
            return TYPE_ACLS[res_type.class_uri]
        if res_type.class_uri == uri.TYPE_CLASS and self.resource is not None:
            described = self.graph.value(self.resource, _N(uri.CLASS))
            if described is not None and described.value in PRIVATE_CLASSES:
                return TYPE_ACLS[described.value]
        return []

    @property
    def type(self) -> "Type":
        parent = self.__parent__
        if isinstance(parent, Type):  # where traversal put it, or a blank record
            return parent
        parsed = uri.parse_record_uri(self.uri or "")
        if parsed is None:
            raise AttributeError("this record is not in a type")
        return self.repository.types[parsed[0]]

    @property
    def label(self) -> str:
        found = self.graph.value(_N(self.uri), _N(uri.LABEL)) if self.uri else None
        return found.value if found is not None else ""

    @property
    def title(self) -> str:
        return self.label or "(Unnamed)"

    def content_value(self, predicate: str) -> str | None:
        found = (
            self.graph.value(self.resource, _N(predicate)) if self.resource else None
        )
        return found.value if found is not None else None

    # ---- the edit form: one Colander schema, made of the fields of the predicates
    # ----------

    def _nodes(
        self, shapes, shape, request, depth: int = 0, above: tuple = ()
    ) -> list[colander.SchemaNode]:
        nodes = []
        made: dict[str, list] = {}
        for template in templates.of_shape(shapes, shape):
            made.setdefault(template.target, []).append(template)
        only_for = forms.fields_only_for(shapes, shape)  # fields of some works only
        for path, prop in forms.properties(shapes, shape):
            if path in self.hidden:
                continue
            nested = forms.nested_shape(shapes, prop)
            if nested is not None and nested in (*above, shape):
                node = None  # a shape that refers to itself: no form is that deep
            elif nested is not None:
                title = forms.property_title(shapes, prop, path)
                node = colander.SchemaNode(
                    colander.Mapping(), name=forms.local_name(path), title=title
                )
                for child in self._nodes(
                    shapes, nested, request, depth + 1, (*above, shape)
                ):
                    node.add(child)
                forms.apply_only_for(shapes, shape, path, node.children)
                node.only_for_fields = {  # of some works only: the page hides them
                    child.name: child.only_for_classes
                    for child in node.children
                    if getattr(child, "only_for_classes", None)
                }
                node.validator = self._sibling_validator(
                    node.children,
                    forms.alternatives_validator(shapes, nested, node.children),
                )
                node.icon = forms.icon(shapes, prop, nested)
                node.header, node.header_addon = templates.header_of(shapes, nested)
                lowest, highest = forms.cardinality(shapes, prop)
                if (lowest, highest) != (1, 1):  # not exactly one: none, one, or more,
                    node.name = "item"  # each of them added (a button) or removed
                    node.node_class = forms.nested_class(shapes, nested)
                    node.widget = deform.widget.MappingWidget(template="mapping_plain")
                    target = self.type if self.is_new else self
                    node = colander.SchemaNode(
                        colander.Sequence(),
                        node,
                        name=forms.local_name(path),
                        title=title,
                        missing=[],
                        validator=colander.Length(min=lowest or None, max=highest),
                        widget=forms.BlankNodeWidget(
                            url=(
                                request.resource_url(target, "sequence")
                                if request is not None
                                else None
                            ),
                            min_len=lowest,
                            max_len=highest,
                        ),
                    )
                    node.oid = (
                        f"nodes-{forms.local_name(path)}"  # the same on every page
                    )
                    node.icon = node.children[0].icon
            else:
                node = forms.leaf_node(shapes, prop, path)
                if node is None and forms.is_list(shapes, prop):
                    target = self.type if self.is_new else self
                    node = forms.list_node(
                        shapes,
                        prop,
                        path,
                        request.resource_url(target, "sequence") if request else None,
                    )
                if node is None and forms.is_bag(shapes, prop):
                    target = self.type if self.is_new else self
                    node = forms.bag_node(
                        shapes,
                        prop,
                        path,
                        request.resource_url(target, "sequence") if request else None,
                    )
                if node is not None:
                    forms.localize_dates(node, self.repository)
                if (
                    node is not None
                    and request is not None
                    and forms.by_uri(shapes, prop)
                ):
                    named = forms.reference_shapes(shapes, prop, shape)
                    classes, new, icons = self.reference_kinds(request, named)
                    if classes:  # a reference to records: looked for by their labels
                        forms.wire_references(
                            node,
                            classes,
                            request.resource_url(self.repository, "lookup"),
                            self.reference_labels,
                            lambda record, slug: edit_address(
                                request, self.repository, record, slug
                            ),
                            new,
                            icons,
                            self._follow(request, shapes, shape, prop),
                        )
                if node is not None and path == uri.RDF_TYPE:
                    node.initial = (
                        self.type.class_uri
                    )  # the class of the type, to begin
                if node is not None and path in made and not getattr(node, "bag", 0):
                    forms.made_of(
                        node,
                        made[path],
                        templates.variants_json(made[path]),
                        forms.needed(shapes, shape, path),
                    )
            if node is not None:
                node.group = forms.property_group(shapes, prop)
            if node is not None and path in only_for:
                node.only_for_classes = only_for[path]
            if node is not None:
                node.depth = (
                    depth  # 0: a property of the record, 1: of its content (a predicate
                )
                # for a property that is at both, like gracia:slug, tells them apart by
                # it)
            code = predicates.find(self.registry, path)
            if code is not None:
                node = code.schema_node(self, request, node)
            if node is not None:
                node.predicate = path
                node.as_uri = forms.is_iri(shapes, prop)
                nodes.append(node)
        return nodes

    def reference_kinds(self, request, shape_uris) -> tuple[list, list, dict]:
        """What a reference to the records of these shapes needs: the classes of their
        resources (the lookup), the types of the records that a person may add (the New
        button, with the address of each form) and the icon of each type."""
        found = self.repository.shape_types(shape_uris)
        return (
            self.repository.shape_classes(shape_uris),
            [
                {
                    "icon": res_type.icon or "",
                    "label": res_type.name,
                    "url": request.resource_url(
                        self.repository[res_type.slug], "add", query={"embed": "1"}
                    ),
                }
                for res_type in found
                if request.has_permission("add", self.repository[res_type.slug])
            ],
            {res_type.slug: res_type.icon or "" for res_type in found},
        )

    def _follow(self, request, shapes, shape, prop) -> dict | None:
        """For a reference that follows the field next to it (the role of a
        contributor): what it points at for each value of that field."""
        followed = forms.class_follow(shapes, shape, prop)
        if followed is None:
            return None
        path, by_value = followed
        kinds = {}
        for value, named in by_value.items():
            classes, new, icons = self.reference_kinds(request, named)
            if classes:
                known = {i for i in icons.values() if i}
                kinds[value] = {
                    "classes": classes,
                    "new": new,
                    "icon": known.pop() if len(known) == 1 else forms.GENERIC_ICON,
                }
        return {"name": forms.local_name(path), "map": kinds} if kinds else None

    def _sibling_validator(self, nodes, extra=None):
        found = [extra] if extra is not None else []
        if (validator := forms.template_validator(nodes)) is not None:
            found.append(validator)
        if (validator := forms.only_for_validator(nodes)) is not None:
            found.append(validator)
        for node in nodes:
            code = predicates.find(
                self.registry, getattr(node, "predicate", None) or ""
            )
            validator = (
                code.sibling_validator(list(nodes)) if code is not None else None
            )
            if validator is not None and validator not in found:
                found.append(validator)
        if not found:
            return None

        def validate(mapping, value):
            for validator in found:
                validator(mapping, value)

        return validate

    @cached_property
    def fields(self) -> list[colander.SchemaNode]:
        content = next(
            (n for n in self.edit_schema().children if n.predicate == uri.RESOURCE),
            None,
        )
        return (
            [c for c in content.children if hasattr(c, "predicate")] if content else []
        )

    def extra_fields(self) -> list[colander.SchemaNode]:
        return []

    @cached_property
    def reference_labels(self) -> forms.ReferenceLabels:
        return forms.ReferenceLabels(self.repository.store.resource_records)

    def edit_schema(self, request=None) -> colander.Schema:
        request = request or get_current_request()
        schema = colander.Schema()
        for node in self._nodes(*self.type.shacl_shape(), request):
            schema.add(node)
        schema.validator = self._sibling_validator(schema.children)
        return schema

    @property
    def is_new(self) -> bool:
        return self.uri is None

    def begin(self, number: int) -> None:
        res_type = self.type
        self.__name__, self.uri = str(number), uri.record_uri(res_type.slug, number)
        record, content, rdf_type = _N(self.uri), ox.BlankNode(), _N(uri.RDF_TYPE)
        for triple in (
            (record, rdf_type, _N(uri.RECORD_CLASS)),
            (record, _N(uri.RESOURCE), content),
            (content, rdf_type, _N(res_type.class_uri)),
        ):
            self.graph.add(ox.Quad(*triple, ox.DefaultGraph()))

    def with_hidden_kept(self, graph: Graph) -> Graph:
        hidden = {_N(p) for p in self.hidden}
        kept = graph.without(hidden)
        old, new = self.resource, kept.value(self.uri, uri.RESOURCE)
        for quad in self.graph:
            if quad.predicate in hidden:
                subject = (
                    new if quad.subject == old and new is not None else quad.subject
                )
                kept.add(
                    ox.Quad(subject, quad.predicate, quad.object, ox.DefaultGraph())
                )
        return kept

    def _nested_nodes(self, subject, path: str | None) -> list:
        found = (
            self.graph.objects(subject, _N(path))
            if path and subject is not None
            else []
        )
        return sorted((o for o in found if isinstance(o, ox.BlankNode)), key=_natural)

    # the values a property has, for the rows of a bag, in an order of their own (a
    # graph has none)
    def _bag_values(self, node, subject, path) -> list:
        found = [
            o
            for o in (self.graph.objects(subject, path) if subject is not None else [])
            if not isinstance(o, ox.BlankNode)
        ]
        if getattr(node.children[0], "language_tagged", False):
            return [
                {"value": o.value, "lang": getattr(o, "language", None) or ""}
                for o in sorted(
                    found, key=lambda o: (getattr(o, "language", None) or "", o.value)
                )
            ]
        return sorted(o.value for o in found)

    # the rows of a list: the items of the rdf:list that is the value, in its order
    def _list_values(self, subject, path) -> list[str]:
        heads = self.graph.objects(subject, path) if subject is not None else []
        return [o.value for head in heads[:1] for o in self.graph.walk_list(head)]

    def form_values(self) -> dict:
        nested_nodes = self._nested_nodes

        def read(nodes, subject) -> dict:
            values = {}
            for node in nodes:
                path = getattr(node, "predicate", None)
                if getattr(node, "list", False):
                    values[node.name] = self._list_values(subject, path)
                elif getattr(node, "bag", False):
                    values[node.name] = self._bag_values(node, subject, path)
                elif getattr(node, "language_tagged", False):
                    text, language = predicates.read_language_text(
                        self, subject, str(path)
                    )
                    values[node.name] = {"value": text, "lang": language}
                elif isinstance(node.typ, colander.Sequence):
                    values[node.name] = [
                        read(node.children[0].children, inner)
                        for inner in nested_nodes(subject, path)
                    ]
                elif isinstance(node.typ, colander.Mapping):
                    inner = (
                        self.graph.value(subject, _N(path))
                        if path and subject is not None
                        else None
                    )
                    values[node.name] = read(node.children, inner)
                elif path is None:
                    values[node.name] = (
                        ""  # a field that is not stored as it is (a password)
                    )
                else:
                    found = predicates.read_text(self, subject, path)
                    values[node.name] = (
                        getattr(node, "initial", "") if found is None else found
                    )
            return values

        schema = self.edit_schema()
        values = read(schema.children, _N(self.uri) if self.uri else None)
        self._prefetch_labels(schema.children, values)
        return values

    def _prefetch_labels(self, nodes, values: dict) -> None:
        """The labels of the references the form shows, in one go."""
        found: list[str] = []

        def walk(nodes, values) -> None:
            for node in nodes:
                value = values.get(node.name)
                if getattr(node, "reference", False):
                    found.extend(value if isinstance(value, list) else [value])
                elif isinstance(node.typ, colander.Sequence) and isinstance(
                    value, list
                ):
                    for each in value:
                        walk(node.children[0].children, each)
                elif isinstance(node.typ, colander.Mapping) and isinstance(value, dict):
                    walk(node.children, value)

        walk(nodes, values)
        self.reference_labels.prefetch([u for u in found if isinstance(u, str)])

    def apply_form(self, data: dict) -> None:
        def write(nodes, data: dict, subject) -> None:
            typed = next(
                (n for n in nodes if getattr(n, "predicate", None) == uri.RDF_TYPE),
                None,
            )
            kind = (data.get(typed.name) or "").strip() if typed is not None else None
            for node in nodes:
                path = getattr(node, "predicate", None)
                classes = getattr(node, "only_for_classes", None)
                if path and classes and kind is not None and kind not in classes:
                    self.graph.replace(subject, _N(path), None)  # not for this work
                    continue
                if path is None or node.name not in data:
                    continue  # a field that is not stored as it is, or nothing was said
                if getattr(node, "list", False):
                    rows = [(t or "").strip() for t in data[node.name] or []]
                    self.graph.replace_list(
                        subject, path, [_N(t) for t in dict.fromkeys(rows) if t]
                    )
                elif getattr(node, "bag", False):
                    wanted: list[tuple[str, str]] = []
                    for said in data[node.name] or []:
                        text = said.get("value") if isinstance(said, dict) else said
                        language = said.get("lang") if isinstance(said, dict) else None
                        row = ((text or "").strip(), language or "")
                        if row[0] and row not in wanted:  # no value, or the same again
                            wanted.append(row)
                    self.graph.discard_plain(subject, path)  # the blank nodes stay
                    kinds = getattr(node.children[0], "date_kinds", None)
                    for text, language in wanted:
                        datatype = dates.datatype_of(text) if kinds else None
                        term = (
                            _N(text)
                            if getattr(node, "as_uri", False)
                            else ox.Literal(
                                text,
                                language=language or None,
                                datatype=_N(datatype) if datatype else None,
                            )
                        )
                        self.graph.add_value(subject, path, term)
                elif getattr(node, "language_tagged", False):
                    said = data[node.name] or {}
                    predicates.write_text(
                        self,
                        subject,
                        path,
                        said.get("value"),
                        language=said.get("lang"),
                    )
                elif isinstance(node.typ, colander.Sequence):
                    item, items = node.children[0], data[node.name] or []
                    existing = self._nested_nodes(subject, path)
                    for index, values in enumerate(items):
                        if index < len(existing):
                            inner = existing[index]
                        else:  # one more, of the class its shape names
                            inner = ox.BlankNode()
                            self.graph.add_value(subject, path, inner)
                            if item.node_class:
                                self.graph.add_value(
                                    inner, uri.RDF_TYPE, _N(item.node_class)
                                )
                        write(item.children, values, inner)
                    for inner in existing[len(items) :]:  # one less
                        self.graph.discard_node(subject, path, inner)
                elif isinstance(node.typ, colander.Mapping):
                    write(
                        node.children,
                        data[node.name],
                        self.graph.value(subject, _N(path)),
                    )
                else:
                    predicates.write_text(
                        self,
                        subject,
                        path,
                        data[node.name],
                        getattr(node, "as_uri", False),
                        datatype=(
                            dates.datatype_of(data[node.name] or "")
                            if getattr(node, "date_kinds", None)
                            else None
                        ),
                    )

        write(self.edit_schema().children, data, _N(self.uri))

    def validate_form(self) -> list[str]:
        found = self.type.shacl_shape(include_record_shape=False)
        if found is None:
            return [f"The type {self.type.name} has no shape"]
        have = {n.predicate for n in self.fields} | set(self.supplies)
        return [
            f"The form cannot fill in {path}, which {self.type.name} requires"
            for path in sorted(forms.required_paths(*found) - have)
        ]

    def validate(self, graph: Graph | None = None) -> list[str]:
        record = self
        if graph is not None:  # text a person edited: it is about this record, with
            subject = self.uri or self.type.placeholder  # the hooks applied to it
            problems = []
            others = [s.value for s in graph.subjects() if s.value != subject]
            if others:
                problems.append(
                    f"Only the record {subject} can be written here, not "
                    f"{', '.join(others)}"
                )
            if _N(uri.RECORD_CLASS) not in graph.objects(subject, uri.RDF_TYPE):
                problems.append(f"{subject} has to be a gracia:Record")
            if problems:
                return problems
            record = type(self)(
                self.repository,
                self.__name__ or "0",
                subject,
                self.store.apply_hooks(graph),
            )  # a trial record: nothing of this one changes, no number is used up
            record.__parent__ = self.__parent__
        shapes, _node = record.type.shacl_shape()
        errors = record.graph.validate(shapes).errors()
        nested = "http://www.w3.org/ns/shacl#NodeConstraintComponent"
        if any(v.constraint_component != nested for v in errors):
            # "the content does not conform to its shape" only says that what is listed
            # does not
            errors = tuple(v for v in errors if v.constraint_component != nested)
        seen, found = set(), []
        for violation in errors:
            path = (violation.path or "").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            message = violation.message
            if (
                violation.constraint_component
                == "http://www.w3.org/ns/shacl#ClosedConstraintComponent"
            ):
                message = (
                    "this property is not described by the shape "
                    f"(its value is {violation.value})"
                )
            # a node-level rule: rudof does not use `sh:message`, the shape's is better
            if not path:
                try:
                    custom = shapes.value(
                        violation.source_shape, "http://www.w3.org/ns/shacl#message"
                    )
                except (
                    ValueError
                ):  # the shape is a blank node: it has no name to look up
                    custom = None
                if custom is not None:
                    message = custom.value
            text = f"{path}: {message}" if path else message
            if text not in seen:  # the shape of the content is reached by two ways
                seen.add(text)
                found.append(text)
        return found

    def check(self, data: dict) -> list[str]:
        return []

    def save(self) -> None:
        store = self.store
        store.put_multi(self.graph)
        self.__name__ = store.current_slug(self.uri) or self.uri.rsplit(":", 1)[1]
        if self.type.slug == "type":  # a type was made or changed: its roles are new
            self.repository.sync_roles()
