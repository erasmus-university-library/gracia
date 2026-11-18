import datetime
import re
import zoneinfo
from functools import cached_property
from pathlib import Path

import colander
import pyoxigraph as ox
from pyramid.authorization import ALL_PERMISSIONS, DENY_ALL, Allow, Everyone
from pyramid.httpexceptions import HTTPGone, HTTPMovedPermanently, HTTPPermanentRedirect
from pyramid.threadlocal import get_current_registry, get_current_request
from zope.interface import implementer

from gracia import forms, hints, predicates, uri
from gracia.graph import Graph
from gracia.interfaces import IDeformView, IExtension, IGraciaType, IRepository
from gracia.utils import assets, passwords
from gracia.utils.validate import validate_userid

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_NUMBER = re.compile(r"^[1-9][0-9]{0,17}$")
_N = ox.NamedNode

# Who may do what with the records of a type, by the rdf class the type describes. These
# are ACLs
# of the type's node; its records inherit them. A type that is not listed has no entries
# of its
# own, so the repository's applies: admins everything, editors `edit` and `add`,
# everyone `view`.
# The special permissions are named apart from `edit` so that they can be granted on
# their own
# (an entry for another group here): `super_edit` for users, `manage` for groups.
_ADMINS = (Allow, "group:admin", ALL_PERMISSIONS)
TYPE_ACLS = {
    # the record of the repository: settings and the list of the extensions, for
    # administrators only
    uri.REPOSITORY_CLASS: [_ADMINS, DENY_ALL],
    # credentials and membership: never public, not even to editors. Managers add and
    # change
    # users (`super_edit`) but cannot touch groups: nobody gives themselves more rights
    # that way
    uri.USER_CLASS: [
        _ADMINS,
        (Allow, "group:manager", ["view", "super_edit"]),
        DENY_ALL,
    ],
    uri.GROUP_CLASS: [_ADMINS, DENY_ALL],
    # the types define the repository: everybody may read them, only admins change them
    uri.TYPE_CLASS: [_ADMINS, (Allow, Everyone, "view"), DENY_ALL],
}
# the types that are never open to the public (and the type records that describe them)
PRIVATE_CLASSES = frozenset({uri.USER_CLASS, uri.GROUP_CLASS, uri.REPOSITORY_CLASS})


class Node:
    """A node of the tree below a repository: it knows the repository it is in. It does
    not need a request for that, only its parents, so code that runs without one (a
    command, a job, an extension) can ask too. Found once and kept: a node lives for one
    request."""

    __parent__ = None

    @cached_property
    def repository(self) -> "Repository":
        node = self.__parent__
        while node is not None and not isinstance(node, Repository):
            node = getattr(node, "__parent__", None)
        if node is None:
            raise AttributeError("this node is not in a repository")
        return node


class Resolved(Node):
    """What `/<repo>/rec:<slug>:<n>` is: the key of a record, which answers by sending
    the caller to the address of the record (see `gracia.views.repository.resolve`). The
    record was found by traversal, so it is one the caller may see."""

    def __init__(self, repository: "Repository", key: str, record: "Record"):
        self.__name__, self.__parent__, self.record = key, repository, record


def resolve_record_key(repository: "Repository", key: str) -> Resolved:
    """`/<repo>/rec:person:34`: the node that sends the caller to the record (`KeyError`
    when there is none, or when the caller may not see it)."""
    parsed = uri.parse_record_uri(key)
    if parsed is None or parsed[0] not in repository.res_types:
        raise KeyError(key)
    return Resolved(
        repository, key, repository.res_types[parsed[0]].by_number(str(parsed[1]))
    )


@implementer(IRepository)
class Repository:
    """One repository (a schema of the database)."""

    def __init__(self, repo_id: str, parent):
        self.__name__ = repo_id
        self.__parent__ = parent
        self._store = None

    @property
    def repository(self) -> "Repository":
        return self

    @property
    def uri(self) -> str:
        return uri.repository_uri()

    def store(self):
        """The repository's store: opened once, closed when the request ends."""
        if self._store is None:
            self._store = self.__parent__.open_repository(self.__name__)
            self.configure(self._store)
            request = get_current_request()
            if request is not None:
                request.add_finished_callback(lambda request: self.close())
        return self._store

    def sync_roles(self, store):
        """The types of the repository changed (one was added, or its schema was
        switched): read them again, and tell the database what their shapes say of their
        properties now. The records are made again from the roles when those changed
        (`Store.set_property_roles`)."""
        for key in ("res_types", "_sql_hooks"):
            self.__dict__.pop(key, None)
        self.configure(store).sync()
        return store

    def configure(self, store):
        """Tell a store of this repository what its predicates and types need done when
        it stores something (`Store.configure`); the store is returned."""
        store.configure(
            lambda: (self.sql_hooks(), self.property_roles(), self.type_numbers())
        )
        return store

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def resource(self, store=None) -> tuple[str, Graph]:
        """The repository's own record (the one record of the type `repository`) and its
        data."""
        graph = (store or self.store()).get(self.uri)
        if not len(graph):
            raise LookupError(f"repository {self.__name__} has no repository record")
        return self.uri, graph

    def content(self, store=None) -> tuple[ox.BlankNode, Graph]:
        """The content of the repository record (its title, types, extensions, schema),
        as the node that holds it and the graph of the record."""
        record_uri, graph = self.resource(store)
        node = graph.value(record_uri, uri.RESOURCE)
        if node is None:
            raise LookupError(
                f"the record of repository {self.__name__} has no content"
            )
        return node, graph

    def timezone(self, store=None) -> datetime.tzinfo:
        """The time zone times are shown in (`gracia:timezone` of the repository): UTC
        when none is chosen. The database holds UTC only."""
        try:
            node, graph = self.content(store)
        except LookupError:
            return datetime.UTC
        found = graph.value(node, _N(uri.TIMEZONE))
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

    def record(self, graph: Graph) -> "Record":
        """The repository record as a `Record` over this graph (no permission asked: the
        caller is changing the repository), so that the predicates of a record do their
        work for it too."""
        res_type = self.res_types["repository"]
        return record_class(res_type.class_uri)(res_type, "1", self.uri, graph)

    def with_hooks(self, graph: "Graph | ox.Dataset", store=None) -> Graph:
        """A copy of a graph with what saving it would add to it: every record has been
        through the SQL of the predicates of its type (`Store.apply_hooks`), the way a
        save does it. What is not a record of a type of this repository is left as it
        is."""
        return (store or self.store()).apply_hooks(graph)

    def sql_hooks(self) -> list[str]:
        """The SQL of all the predicates of all the types, in the order it runs (see
        `predicates`). Made once for this node."""
        if "_sql_hooks" not in self.__dict__:
            self.__dict__["_sql_hooks"] = [
                statement
                for res_type in self.res_types.values()
                for statement in res_type.sql_hooks()
            ]
        return self.__dict__["_sql_hooks"]

    def property_roles(self) -> list[tuple[str, str, str, int]]:
        """What the shapes of the types say about their properties, for the database
        (`Store.set_property_roles`): (type slug, role, property uri, place)."""
        return [
            (res_type.slug, role, path, place)
            for res_type in self.res_types.values()
            for role, path, place in res_type.roles()
        ]

    def type_numbers(self) -> list[tuple[str, int]]:
        """The number of every type, by its slug: the number of its type record
        (`rec:type:<n>`), what the ids of the etags are made of
        (`Store.set_type_ids`)."""
        return [
            (t.slug, int(t.record.rsplit(":", 1)[1])) for t in self.res_types.values()
        ]

    def shape(self, store=None):
        """The shape of the repository's content, from the files named by its
        `gracia:assets`: (the shape's graph, its node). Raises LookupError when there is
        none."""
        node, graph = self.content(store)
        prefix = graph.value(node, _N(uri.ASSETS))
        schema = graph.value(node, _N(uri.SCHEMA_TEMPLATE))
        found = assets.shape(
            prefix.value if prefix else None,
            schema.value if schema else None,
            uri.REPOSITORY_CLASS,
            "Repository",
        )
        if found is None:
            raise LookupError(
                f"repository {self.__name__} has no shape for its resource"
            )
        return found

    @cached_property
    def res_types(self) -> dict[str, "Type"]:
        """The types the repository lists in `gracia:types`, by slug; none for a
        repository that has no repository resource."""
        try:
            node, graph = self.content()
        except LookupError:
            return {}
        head = graph.value(node, _N(uri.TYPES))
        classes = [p.value for p in graph.walk_list(head)] if head is not None else []
        by_class = self.store().records_with_resource_uris(
            classes
        )  # the list has the classes: the resource uris
        records = [by_class[c] for c in classes if c in by_class]
        found = self.store().get(records) if records else Graph()
        res_types = {}
        for record in records:
            res_type = Type.read(self, record, found)
            if res_type is not None:
                res_types[res_type.slug] = res_type
        return res_types

    def resolve_type(self, href: str) -> Path:
        """The file `type:<slug>/<file>` names: `<file>` in the assets of the type of
        this repository with that slug (so a repository whose person type has other
        assets gets those). Raises LookupError for an unknown type, ValueError for a
        path that leaves the type, FileNotFoundError for a missing file."""
        slug, _, name = href.removeprefix("type:").partition("/")
        res_type = self.res_types.get(slug)
        if res_type is None or not res_type.assets:
            raise LookupError(
                f"no type {slug!r} with files in repository {self.__name__}"
            )
        return assets.resolve(assets.join(res_type.assets, name))

    def prefix_map(self) -> dict[str, str]:
        """The prefixes of this repository (the `ns_prefix` extension) as {prefix:
        namespace}."""
        items = self.extension("ns_prefix").settings["prefixes"]
        return {item["prefix"]: item["namespace"] for item in items}

    def extension(self, name: str):
        """The extension `name` as it is in this repository (see `gracia.extensions`):
        it says whether it is on (`.enabled`) and what its settings are. `KeyError` when
        there is none. One object per name for as long as this node lives (a
        request)."""
        cache = self.__dict__.setdefault("_extension_objects", {})
        if name not in cache:
            found = get_current_registry().queryAdapter(self, IExtension, name)
            if found is None:
                raise KeyError(name)
            cache[name] = found
        return cache[name]

    def extensions(self) -> list:
        """All the extensions there are, as they are in this repository, by title."""
        found = get_current_registry().getAdapters((self,), IExtension)
        return sorted(
            (e for _name, e in found), key=lambda e: (e.title or e.name, e.name)
        )

    def __getitem__(self, name: str):
        """What is below a repository: what the registry has for the name (`jobs`,
        `deform`, `rec:...`, and whatever an extension registers, see
        `gracia.extensions`), else a type, by its slug. The repository does not know
        which of them there are."""
        from gracia.extensions import repository_child

        child = repository_child(get_current_registry(), name)
        if child is not None:
            return child.factory(self, name)
        try:
            return self.res_types[name]
        except KeyError:
            raise KeyError(name) from None

    def login(self, userid: str, password: str) -> set[str] | None:
        """The names of the groups of the user `userid` when `password` is right, else
        None. The user is found by its `gracia:userid`, not by its record number."""
        try:
            validate_userid(userid)
        except ValueError:
            return passwords.verify_nothing(password) or None  # ty: ignore[invalid-explicit-return]
        store = self.store()
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


class Jobs(Node):
    """`/<repo>/jobs`: the jobs of the repository, and the jobs that can be started in
    it.

    Who may do what: the administrators everything; the groups a job names (`groups`)
    may start it (`start_<name>_job`) and list the jobs (`list_jobs`); nobody else, the
    public included. A job below this node (`JobRun`) is seen and stopped by whoever
    started it."""

    __name__ = "jobs"

    def __init__(self, parent: Repository):
        self.__parent__ = parent

    @property
    def __acl__(self):
        from gracia.jobs.job import all_jobs

        grants: dict[str, list[str]] = {}
        for job in all_jobs(get_current_registry()):
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

        with JobsDb(self.repository.__parent__) as db:
            row = db.get(job_id)
        if row is None or row["repo"] != self.repository.__name__:
            raise KeyError(job_id)
        return JobRun(self, row)


class DeformViews(Node):
    """`/<repo>/deform`: the deform views (see `gracia.views.deform`) by name. The node
    of a view has no ACL of its own: the view asks for the permission it was declared
    with, of the node it was declared for (the repository, or the node below it that
    `at` names)."""

    __name__ = "deform"

    def __init__(self, parent: Repository):
        self.__parent__ = parent

    def __getitem__(self, name: str) -> "DeformView":
        function = get_current_registry().queryUtility(IDeformView, name)
        if function is None:
            raise KeyError(name)
        return DeformView(self, name, function)


class DeformView(Node):
    """`/<repo>/deform/<name>`: one deform view."""

    def __init__(self, parent: DeformViews, name: str, function):
        self.__name__ = name
        self.__parent__ = parent
        self.function = function

    @property
    def target(self):
        """The node the function is declared for, and the one its permission is asked
        of."""
        repository = self.repository
        at = self.function.deform_at
        return repository[at] if at else repository


class JobRun(Node):
    """`/<repo>/jobs/<uuid>`: one job (the row as it was when the node was made). Its
    creator may see and stop it, administrators may do anything, nobody else."""

    def __init__(self, parent: Jobs, row: dict):
        self.__name__ = row["id"]
        self.__parent__ = parent
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
    """A type of record: its records are `rec:<slug>:<n>`, its address is the slug."""

    def __init__(
        self, parent: Repository, record: str, name: str, slug: str, class_uri: str
    ):
        self.__name__ = slug
        self.__parent__ = parent
        self.record, self.name, self.slug, self.class_uri = (
            record,
            name,
            slug,
            class_uri,
        )
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

    def shape(self):
        """The shape of its records' content from the schema file the type is on
        (`<assets>shacl/<schema>`): (graph, node), or None."""
        return assets.shape(self.assets, self.schema, self.class_uri, self.name)

    def roles(self) -> list[tuple[str, str, int]]:
        """What the shape of its records says about the properties: (role, property,
        place), see `hints.roles`. Made once for this node."""
        if "_roles" not in self.__dict__:
            self.__dict__["_roles"] = hints.roles(*self.record_shape(), self.class_uri)
        return self.__dict__["_roles"]

    def role_paths(self, role: str) -> list[str]:
        """The properties that have a role, in order (`label`, `key_info` or
        `description`)."""
        return [path for r, path, _place in self.roles() if r == role]

    def sql_hooks(self) -> list[str]:
        """The SQL that the predicates of the properties of its records run when they
        are saved, in the order it runs: what is embedded first (`predicates`). Made
        once for this node."""
        if "_sql_hooks" not in self.__dict__:
            self.__dict__["_sql_hooks"] = self._sql_plan(*self.record_shape())
        return self.__dict__["_sql_hooks"]

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
            code = predicates.find(path)
            if code is not None:
                context = predicates.HookContext(self.repository, self, path)
                # a property that is at the record and at its content (gracia:slug) is
                # one hook
                statements += [
                    s for s in (code.on_write_sql(context) or []) if s not in statements
                ]
        return statements

    def record_shape(self):
        """The shape of its records: the record shape with the shape of the content
        embedded (`assets.record_shape`), and the content has to be of the class of the
        type: (graph, node). Made once for this node."""
        if "_record_shape" not in self.__dict__:
            self.__dict__["_record_shape"] = assets.record_shape(
                self.shape(), self.class_uri
            )
        return self.__dict__["_record_shape"]

    @property
    def assets_version(self) -> str:
        """The version of the files of this type (the schema it is on, its stylesheet):
        part of the ETag of its records, so that they change when a file does or the
        type switches to another schema file. See `assets.version`."""
        return assets.version(self.assets, self.schema, self.name)

    @property
    def __acl__(self):
        return TYPE_ACLS.get(self.class_uri, [])

    def records(self) -> list[str]:
        """The record URIs of this type."""
        return self.repository.store().records_of_type(self.slug)

    @property
    def placeholder(self) -> str:
        """What a record of this type that has no number yet is called in its text: its
        number is made when it is saved."""
        return uri.record_uri(self.slug, 0)

    def skeleton(self) -> Graph:
        """The text of a new record: what every record has, under the placeholder."""
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
        """A record of this type that is not stored yet: no number, no data. It has the
        hooks of the class registered for the type, so the form to add one is the form
        to edit one."""
        return record_class(self.class_uri)(self, None, None, Graph())  # ty: ignore[invalid-argument-type]

    def __getitem__(self, key: str) -> "Record":  # ty: ignore[invalid-implicit-return]
        """A record by its number, or by a slug it has or had. A number is a record, or
        (the record was deleted: its number is a slug of its own then) HTTP 410, and the
        number of a record that has a slug goes on to the slug (301, as below). A slug
        that is the current one is the record; an old one goes on to the current slug,
        or to the number when there is none (301, the rest of the address and the query
        as they were); one of a deleted record is 410."""
        if _NUMBER.match(key):
            record = self.by_number(key)
            if record.__name__ != key:  # it has a slug: that is its address
                self._go_to(key, record.__name__)
            return record
        if not uri.SLUG_NAME.match(key):
            raise KeyError(key)
        found = self.repository.store().slug_lookup(self.slug, key)
        if found is None:
            raise KeyError(key)
        record_uri, deleted = found
        number = record_uri.rsplit(":", 1)[1]
        record = None if deleted else self._record(number)
        if record is None:
            raise HTTPGone()
        current = self.repository.store().current_slug(record_uri)
        if current == key:
            return record
        self._go_to(key, current or number)

    def by_number(self, number: str) -> "Record":
        """The record with this number as it is (no redirect to its slug), 410 for one
        that was deleted, `KeyError` when there is none."""
        return self._record(number) or self._gone_or_missing(number)

    def _record(self, number: str) -> "Record | None":
        """The record `rec:<type>:<number>` (the caller may see), None when there is
        none."""
        record_uri = uri.record_uri(self.slug, int(number))
        graph = self.repository.store().get(record_uri)
        if not len(graph):
            # no such record (the uri has the type: a record of another type has another
            # uri)
            return None
        record = record_class(self.class_uri)(self, number, record_uri, graph)
        request = get_current_request()
        if request is not None and not request.has_permission("view", record):
            return None  # a record the caller may not see is not there for them
        return record

    def _gone_or_missing(self, number: str):
        """No record has this number: HTTP 410 when one had, else not found."""
        found = self.repository.store().slug_lookup(self.slug, number)
        if found is not None and found[1]:
            raise HTTPGone()
        raise KeyError(number)

    def _go_to(self, old: str, new: str):
        """Answer with a redirect from the address with the slug `old` to the one with
        `new`: the rest of the path and the query stay. Get and head are 301, what
        changes something 308 (the method is kept)."""
        request = get_current_request()
        if request is None:
            raise KeyError(old)
        parts = request.path_info.split("/")
        rest = ""
        for index in range(1, len(parts)):
            if parts[index] == old and parts[index - 1] == self.slug:
                rest = "/".join(parts[index + 1 :])
                break
        target = request.resource_url(self) + new + (f"/{rest}" if rest else "")
        if request.query_string:
            target += f"?{request.query_string}"
        redirect = (
            HTTPMovedPermanently
            if request.method in ("GET", "HEAD")
            else HTTPPermanentRedirect
        )
        raise redirect(location=target)


def _slug_of(graph: Graph, record_uri: str) -> str | None:
    """The `gracia:slug` a record's graph says it has, or None."""
    found = graph.value(_N(record_uri), _N(uri.SLUG))
    return found.value if found is not None else None


def record_class(class_uri: str) -> type["Record"]:
    """The class registered for the type of `class_uri` (see gracia.types), else
    `Record`."""
    info = get_current_registry().queryUtility(IGraciaType, class_uri)
    return (info.record_class if info is not None else None) or Record


class Record(Node):
    """One record and its description (the record and the blank node that holds its
    content).

    The hooks below are what a class for a type overrides (see gracia.types): the
    generic edit view builds its form and saves through them."""

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

    def __init__(self, parent: Type, number: str, uri: str, graph: Graph):
        self.__parent__ = parent
        self.uri, self.graph = uri, graph
        # the address of a record is its slug when it has one, else its number
        self.__name__ = (_slug_of(graph, uri) if uri else None) or number

    @property
    def resource(self):
        """The blank node with the content, or None (also for a record that is not
        stored yet)."""
        if self.uri is None:
            return None
        return self.graph.value(_N(self.uri), _N(uri.RESOURCE))

    @property
    def __acl__(self):
        """A record of a private type, or the type record that describes one, has the
        ACL of that type and nothing else: whatever a record may come to say about
        itself (a public flag, ...) cannot open it, because the lookup ends here, at the
        closing entry of the type's ACL."""
        res_type = self.__parent__
        if res_type.class_uri in PRIVATE_CLASSES:  # ty: ignore[missing-union-attribute]
            return TYPE_ACLS[res_type.class_uri]  # ty: ignore[missing-union-attribute]
        if res_type.class_uri == uri.TYPE_CLASS and self.resource is not None:  # ty: ignore[missing-union-attribute]
            described = self.graph.value(self.resource, _N(uri.CLASS))
            if described is not None and described.value in PRIVATE_CLASSES:
                return TYPE_ACLS[described.value]
        return []

    @property
    def type(self) -> "Type":
        return self.__parent__  # ty: ignore[invalid-explicit-return]

    @property
    def label(self) -> str:
        """The label of the record (`gracia:label`, made when it is saved), or an empty
        text."""
        found = self.graph.value(_N(self.uri), _N(uri.LABEL)) if self.uri else None
        return found.value if found is not None else ""

    @property
    def title(self) -> str:
        """What a form calls the record: its label, else "(Unnamed)"."""
        return self.label or "(Unnamed)"

    def content_value(self, predicate: str) -> str | None:
        """What the content says now for a property, as text, or None."""
        found = (
            self.graph.value(self.resource, _N(predicate)) if self.resource else None
        )
        return found.value if found is not None else None

    # ---- the edit form: one Colander schema, made of the fields of the predicates
    # ----------

    def _nodes(
        self, shapes, shape, request, depth: int = 0
    ) -> list[colander.SchemaNode]:
        """The nodes of the form for a node shape, in `sh:order`: what the shape makes
        of every property (a text field, or a mapping for an embedded shape), changed by
        the predicate registered for the property when there is one."""
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
            code = predicates.find(path)
            if code is not None:
                node = code.schema_node(self, request, node)
            if node is not None:
                node.predicate = path
                node.as_uri = forms.is_iri(shapes, prop)
                nodes.append(node)
        return nodes

    @staticmethod
    def _sibling_validator(nodes):
        """The validators the predicates of these nodes want for the mapping they are
        in, as one (or None)."""
        found = []
        for node in nodes:
            code = predicates.find(getattr(node, "predicate", None) or "")
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
        """The form fields of the content: the fields of the node `gracia:resource` that
        the shape made (the ones a class adds, `extra_fields`, are not stored as they
        are)."""
        content = next(
            (n for n in self.edit_schema().children if n.predicate == uri.RESOURCE),
            None,
        )
        return (
            [c for c in content.children if hasattr(c, "predicate")] if content else []
        )

    def extra_fields(self) -> list[colander.SchemaNode]:
        """More fields that are not stored as they are (a new password, ...)."""
        return []

    def edit_schema(self, request=None) -> colander.Schema:
        """The form: one Colander node for every property of the shape of the record,
        with the shape of the content embedded on `gracia:resource` (see `_nodes`)."""
        request = request or get_current_request()
        schema = colander.Schema()
        for node in self._nodes(*self.__parent__.record_shape(), request):  # ty: ignore[missing-union-attribute]
            schema.add(node)
        schema.validator = self._sibling_validator(schema.children)
        return schema

    def _read(self, nodes, subject) -> dict:
        values = {}
        for node in nodes:
            path = getattr(node, "predicate", None)
            if isinstance(node.typ, colander.Mapping):
                inner = (
                    self.graph.value(subject, _N(path))
                    if path and subject is not None
                    else None
                )
                values[node.name] = self._read(node.children, inner)
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

    def edit_values(self) -> dict:
        """The form values of what is stored now."""
        return self._read(
            self.edit_schema().children, _N(self.uri) if self.uri else None
        )

    @property
    def is_new(self) -> bool:
        return self.uri is None

    def cannot_be_added(self) -> list[str]:
        """Why a record of this type cannot be made with the form: what the shape
        requires but neither a field nor the class supplies. Empty when it can."""
        res_type = self.__parent__
        found = res_type.shape()  # ty: ignore[missing-union-attribute]
        if found is None:
            return [f"The type {res_type.name} has no shape"]  # ty: ignore[missing-union-attribute]
        have = {n.predicate for n in self.fields} | set(self.supplies)
        return [
            f"The form cannot fill in {path}, which {res_type.name} requires"  # ty: ignore[missing-union-attribute]
            for path in sorted(forms.required_paths(*found) - have)
        ]

    def begin(self, number: int) -> None:
        """Give a new record its number and the skeleton every record has: the blank
        node that will hold the content, typed with the class of the type (the type of
        the record is in its uri)."""
        res_type = self.__parent__
        self.__name__, self.uri = str(number), uri.record_uri(res_type.slug, number)  # ty: ignore[missing-union-attribute]
        record, content, rdf_type = _N(self.uri), ox.BlankNode(), _N(uri.RDF_TYPE)
        for triple in (
            (record, rdf_type, _N(uri.RECORD_CLASS)),
            (record, _N(uri.RESOURCE), content),
            (content, rdf_type, _N(res_type.class_uri)),  # ty: ignore[missing-union-attribute]
        ):
            self.graph.add(ox.Quad(*triple, ox.DefaultGraph()))

    def shown_graph(self) -> Graph:
        """The data of the record without what is never shown (`hidden`: the
        credentials)."""
        return self.graph.without(self.hidden)

    def with_hidden_kept(self, graph: Graph) -> Graph:
        """What was parsed from a text, with what this record has that is never shown
        put back (and nothing that the text says of it taken): a password hash does not
        come from a text."""
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

    def structure_problems(self, graph: Graph, subject: str | None = None) -> list[str]:
        """What makes a text something other than this record: other resources in it.
        (The type of a record is in its uri, and that its content is of the class of the
        type is in the shape of the record.) `subject` is what it is called in the text
        (a placeholder for a record that has no number yet)."""
        subject, problems = subject or self.uri, []
        others = [s.value for s in graph.subjects() if s.value != subject]
        if others:
            problems.append(
                f"Only the record {subject} can be written here, not "
                f"{', '.join(others)}"
            )
        if _N(uri.RECORD_CLASS) not in graph.objects(subject, uri.RDF_TYPE):
            problems.append(f"{subject} has to be a gracia:Record")
        return problems

    def shape_problems(self) -> list[str]:
        """The violations of the shape of the record (the record shape with the shape of
        the content in it) by its graph, once, as `property: message`."""
        shapes, _node = self.type.record_shape()
        errors = self.graph.validate(shapes).errors()
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

    def check(self, data: dict, store) -> list[str]:
        """Problems with valid form data that only the database can see (a name that is
        taken). An empty list lets the save go on."""
        return []

    def _home(self):
        """The graph name of the record: what is added to it is added there."""
        return self.graph.graph_name_of(_N(self.uri)) or ox.DefaultGraph()

    def add(self, subject, predicate: str, term) -> None:
        """Add one more value of `subject predicate`, in the graph of this record."""
        self.graph.add_value(subject, predicate, term, graph=self._home())

    def replace(self, subject, predicate: str, new) -> None:
        """Set `subject predicate` to `new` (a term, or None for no value) in this
        record."""
        self.graph.replace(subject, predicate, new, graph=self._home())

    def _write(self, nodes, data: dict, subject) -> None:
        for node in nodes:
            path = getattr(node, "predicate", None)
            if path is None or node.name not in data:
                continue  # a field that is not stored as it is, or nothing was said
            if isinstance(node.typ, colander.Mapping):
                self._write(
                    node.children, data[node.name], self.graph.value(subject, _N(path))
                )
            else:
                text = data[node.name]
                predicates.write_text(
                    self, subject, path, text, getattr(node, "as_uri", False)
                )

    def apply(self, data: dict) -> None:
        """Put valid form data into the record's graph, as plain text for every field
        (the caller saves it with `save`, which is what runs the hooks)."""
        self._write(self.edit_schema().children, data, _N(self.uri))

    def save(self, store) -> None:
        """Store the record: the one way a record gets in the database, so that whoever
        saves it (a form, an import) gets what the predicates make of it: the store runs
        their SQL."""
        self.repository.configure(store)
        store.put(self.graph)
        self.__name__ = store.current_slug(self.uri) or self.uri.rsplit(":", 1)[1]
        if (
            self.__parent__.slug == "type"  # ty: ignore[missing-union-attribute]
        ):  # a type was made or changed: its roles are new
            self.repository.sync_roles(store)
