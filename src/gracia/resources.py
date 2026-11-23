import datetime
import re
import zoneinfo
from functools import cached_property
from pathlib import Path

import colander
import pyoxigraph as ox
from pyramid.authorization import ALL_PERMISSIONS, DENY_ALL, Allow, Everyone
from pyramid.threadlocal import get_current_request
from zope.interface import implementer

from gracia import forms, hints, predicates, traversal, uri
from gracia.graph import Graph
from gracia.interfaces import IDeformView, IExtension, IGraciaType, IRepository
from gracia.store import new_store
from gracia.traversal import traversable
from gracia.utils import assets, passwords
from gracia.utils.validate import validate_userid

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_NUMBER = re.compile(r"^[1-9][0-9]{0,17}$")
_N = ox.NamedNode

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
        store = self.store = new_store(self.application.database, self.__name__, db)
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

    def timezone(self) -> datetime.tzinfo:
        try:
            record = self.record()
        except LookupError:
            return datetime.UTC
        if record.resource is None:
            return datetime.UTC
        found = record.graph.value(record.resource, _N(uri.TIMEZONE))
        try:
            return (
                zoneinfo.ZoneInfo(found.value)
                if found is not None and found.value
                else datetime.UTC
            )
        except (
            zoneinfo.ZoneInfoNotFoundError,
            ValueError,
            OSError,
        ):  # a name this system does not know
            return datetime.UTC

    @cached_property
    def types(self) -> dict[str, "Type"]:
        records, found = self.application.cache.types(self.__name__, self._type_records)
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
        return records, self.store.get(records) if records else Graph()

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
        found = store.get(groups) if groups else Graph()
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
    def number(self) -> int:
        return int(self.record.rsplit(":", 1)[1])

    @cached_property
    def property_roles(self) -> list[tuple[str, str, int]]:
        return hints.property_roles(*self.shacl_shape(), self.class_uri)

    def property_role_paths(self, role: str) -> list[str]:
        return [path for r, path, _place in self.property_roles if r == role]

    @cached_property
    def sql_hooks(self) -> list[str]:
        return self._sql_plan(*self.shacl_shape())

    def _sql_plan(self, shapes, shape) -> list[str]:
        properties = [
            (path, forms.nested_shape(shapes, prop))
            for path, prop in forms.properties(shapes, shape)
        ]
        statements = []
        for _path, nested in properties:  # what is embedded comes first
            if nested is not None:
                statements += self._sql_plan(shapes, nested)
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
            found = self._slug(key)
            if found is None:
                raise KeyError(key)
            if found[1]:
                raise RecordDeleted(key)
            number = found[0].rsplit(":", 1)[1]
        else:
            raise KeyError(key)
        record = self._record(number)
        if record is None:
            if number == key:  # a number that was deleted is a slug of its own
                found = self._slug(key)
                if found is not None and found[1]:
                    raise RecordDeleted(key)
            raise KeyError(key)
        return record

    def _slug(self, key: str) -> tuple[str, bool] | None:
        return self.application.cache.slug(
            self.repository.__name__,
            self.slug,
            key,
            lambda: self.store.slug_lookup(self.slug, key),
        )

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
        self, shapes, shape, request, depth: int = 0
    ) -> list[colander.SchemaNode]:
        nodes = []
        for path, prop in forms.properties(shapes, shape):
            if path in self.hidden:
                continue
            nested = forms.nested_shape(shapes, prop)
            if nested is not None:
                node = colander.SchemaNode(
                    colander.Mapping(),
                    name=forms.local_name(path),
                    title=forms.property_title(shapes, prop, path),
                )
                for child in self._nodes(shapes, nested, request, depth + 1):
                    node.add(child)
                node.validator = self._sibling_validator(node.children)
            else:
                node = forms.leaf_node(shapes, prop, path)
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

    def _sibling_validator(self, nodes):
        found = []
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

    def form_values(self) -> dict:
        def read(nodes, subject) -> dict:
            values = {}
            for node in nodes:
                path = getattr(node, "predicate", None)
                if isinstance(node.typ, colander.Mapping):
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

        return read(self.edit_schema().children, _N(self.uri) if self.uri else None)

    def apply_form(self, data: dict) -> None:
        def write(nodes, data: dict, subject) -> None:
            for node in nodes:
                path = getattr(node, "predicate", None)
                if path is None or node.name not in data:
                    continue  # a field that is not stored as it is, or nothing was said
                if isinstance(node.typ, colander.Mapping):
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
        store.put(self.graph)
        self.__name__ = store.current_slug(self.uri) or self.uri.rsplit(":", 1)[1]
        if self.type.slug == "type":  # a type was made or changed: its roles are new
            self.repository.sync_roles()
