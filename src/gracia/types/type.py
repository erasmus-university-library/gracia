"""The type `type`, and the base classes of the records of every type. `Type` is the
node
of a type of a repository, below which its records are (`Record`, with the views of
them,
`RecordView`); `TypeRecord` is the record that describes a type. The node and the views
of every other type start from these, in a module of their own (`types/person.py`). The
default add and edit views are methods of
`RecordView`, as are the pages of a record: `index` is the one of a browser, which gives
the values of the stylesheet that makes the page (the stylesheet is the template of the
view, and the repository may name another for the records of a type:
`Repository.template`),
`as_json` and `as_rdfxml`."""

from __future__ import annotations

import difflib
import itertools
import json
import logging
import re
import secrets
from dataclasses import dataclass
from functools import cached_property
from html import escape
from typing import TYPE_CHECKING, ClassVar

import colander
import deform
import deform.widget
import pyoxigraph as ox
from pyramid.authorization import DENY_ALL, Allow, Everyone
from pyramid.exceptions import ConfigurationError
from pyramid.httpexceptions import (
    HTTPBadRequest,
    HTTPFound,
    HTTPNotFound,
    HTTPNotModified,
)
from pyramid.response import Response
from pyramid.threadlocal import get_current_request
from pyramid.view import view_config
from zope.interface import implementer

from gracia import (
    forms,
    init,
    predicates,
    shacl,
    static,
    uri,
)
from gracia.forms import Form, change_sequence, edit_address, fragment
from gracia.graph import Graph, ParseError
from gracia.interfaces import IRecord, IRecordType, IType, Node
from gracia.jobs.job import all_jobs
from gracia.security import ADMINS, same_origin
from gracia.store import PAGE_SIZE, DuplicateValue, page_window
from gracia.templates import XsltRenderer
from gracia.utils import assets, dates, etags, pretty_modified

log = logging.getLogger(__name__)


class RecordDeleted(LookupError):
    pass


class RecordMoved(LookupError):
    """A record was asked for by an address that is not its own now (its number, when it
    has a slug; a slug it had before): it is at `record.__name__` below its type."""

    def __init__(self, key: str, record: Record):
        super().__init__(key)
        self.key, self.record = key, record


_N = ox.NamedNode
_NUMBER = re.compile(r"^[1-9][0-9]{0,17}$")

if TYPE_CHECKING:
    from gracia.types.repository import Repository
HIDDEN = frozenset({uri.CREDENTIALS})  # never shown, whoever asks and whatever it says
RECORD_TEMPLATE = "gracia:assets/xslt/view/record-minimal-01.xslt"
MANAGE_TEMPLATE = "gracia:assets/xslt/skin/manage.xslt"
TYPE_TEMPLATE = "gracia:assets/xslt/skin/type.xslt"
FORM_TEMPLATE = "gracia:assets/xslt/skin/form.xslt"
HISTORY_TEMPLATE = "gracia:assets/xslt/skin/history.xslt"
MAX_DEPTH = 10  # how far out from the record the records of a page are looked for
SHAPES = {"sh": "http://www.w3.org/ns/shacl#", "dash": "http://datashapes.org/dash#"}
SHOWN = "%Y-%m-%d %H:%M:%S UTC"  # a time of the history
FORMATS = (
    "turtle",
    "ntriples",
    "rdfxml",
    "jsonld",
)  # what the text of a record can be written in
DEFAULT_FORMAT = "turtle"
EDITORS = {
    "turtle": "turtle",
    "ntriples": "turtle",
    "rdfxml": "xml",
    "jsonld": "json",
}  # editor.js


# where the files of the types that come with gracia are (`shacl/`, `xslt/doc/`,
# `xslt/view/`); a package of another place says where its own are
BUNDLED_ASSETS = "gracia:assets/"


@dataclass(frozen=True)
class ContentType:
    """What a class of records declares of its type: what a repository is given when the
    type is added to it (its type record)."""

    name: str
    slug: str
    class_uri: str
    schema: str  # the file in shacl/ its records follow (`gracia:schemaTemplate`)
    doc: str | None = None  # the file in xslt/doc/ that makes their docs
    view: str | None = None  # the file in xslt/view/ that makes their pages
    assets: str = BUNDLED_ASSETS  # the asset spec of the directory with those
    system: bool = False  # a type a repository cannot work without


@implementer(IType)
class Type(Node):
    """A type of a repository: the node below which its records are. A type that wants
    another node subclasses this and names its slug (`slug = "person"` in `PersonType`);
    the scan registers it, and the repository makes that class for the type (`read`)."""

    slug: str = ""  # the slug of the type that a subclass is the node of (none: any)

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
    def read(cls, repository: Repository, record: str, graph: Graph) -> Type | None:
        resource = graph.value(_N(record), _N(uri.RESOURCE))
        if resource is None:
            return None
        fields = [graph.value(resource, _N(p)) for p in (uri.NAME, uri.SLUG, uri.CLASS)]
        if any(f is None for f in fields) or fields[1].value in uri.RESERVED_SLUGS:
            return None
        node = type_class(repository.registry, fields[1].value) or cls
        res_type = node(
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
                self.assets, self.schema, self.class_uri, self.slug
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
        return forms.property_roles(*self.shacl_shape(), self.class_uri)

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
        for template in shacl.of_shape(shapes, shape):
            made.setdefault(template.target, []).append(template)
        for target, variants in made.items():  # before any label is made
            context = predicates.HookContext(self.repository, self, target)
            statements += [
                s for s in shacl.sql_of(variants, context) if s not in statements
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
        return assets.version(self.assets, self.schema, self.slug)

    @property
    def __acl__(self):
        return record_class(self.registry, self.class_uri).acl

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

    def blank_record(self) -> Record:
        cls = record_class(self.registry, self.class_uri)
        record = cls(self.repository, None, None, Graph())  # ty: ignore[invalid-argument-type]
        record.__parent__ = self
        return record

    def __getitem__(self, key: str) -> Record:
        """The traversal of an address: the record at the segment, if it is the current
        name of the record (its slug, or its number while it has none). The number of a
        record that has a slug now, and an old slug, are `RecordMoved`; to find a record
        by either, `find_record` does."""
        record = self.find_record(key)
        if key != record.__name__:
            raise RecordMoved(key, record)
        return record

    def find_record(self, key: str) -> Record:
        """The record with the number or the slug (one it has now or had before); a
        KeyError when there is none, `RecordDeleted` when it was deleted."""
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

    def _record(self, number: str) -> Record | None:
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


def type_class(registry, slug: str) -> type[Type] | None:
    """The node of the type with the slug, if a subclass of `Type` was registered for
    it."""
    return registry.queryUtility(IType, slug)


def record_class(registry, class_uri: str) -> type[Record]:
    """The node of the records of a class: the one its type registered, else
    `Record`."""
    return registry.queryUtility(IRecordType, class_uri) or Record


def _natural(node) -> list:
    """The key to sort blank nodes on their identifiers, the numbers as numbers: `b7_2`
    comes before `b7_10`."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", node.value)]


def _slug_of(graph: Graph, record_uri: str) -> str | None:
    found = graph.value(_N(record_uri), _N(uri.SLUG))
    return found.value if found is not None else None


@implementer(IRecord)
class Record(Node):
    content_type: ContentType | None = None  # what a class that declares a type says
    # who may do what with the type (`Type.__acl__`), and whether the records of the
    # type are as private as that: a user is, the record of a type is not
    acl: ClassVar[list] = []
    private = False
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
    source_editable = True  # whether its data can be edited as text (`RecordView`)

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
        own = record_class(self.registry, res_type.class_uri)
        if own.private:
            return own.acl
        if res_type.class_uri == uri.TYPE_CLASS and self.resource is not None:
            described = self.graph.value(self.resource, _N(uri.CLASS))
            if described is not None:  # the record of a type is as private as the type
                other = record_class(self.registry, described.value)
                if other.private:
                    return other.acl
        return []

    @property
    def type(self) -> Type:
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
        for template in shacl.of_shape(shapes, shape):
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
                node.header, node.header_addon = shacl.header_of(shapes, nested)
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
                        shacl.variants_json(made[path]),
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


class SourceError(ValueError):
    def __init__(self, errors):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors))


class RecordView:
    def __init__(self, request):
        self.request = request
        self.context = request.context
        self.new = isinstance(
            self.context, Type
        )  # an add page: the context is the type
        self.res_type = self.context if self.new else self.context.type
        self.record = self.res_type.blank_record() if self.new else self.context
        self.view = "add" if self.new else "edit"
        self.target = (
            self.res_type if self.new else self.record
        )  # what the page is the page of
        self.address = request.resource_url(self.target, self.view)
        if self.new:
            self.heading, self.subtitle = (
                f"Add {self.res_type.name}",
                self.res_type.record,
            )
        else:
            self.heading, self.subtitle = self.record.title, self.record.uri

    # ---- the pages of a record: for a browser, a program and a client of RDF
    # ----------------

    @view_config(
        context=IRecord,
        request_method="GET",
        accept="text/html",
        renderer=RECORD_TEMPLATE,
    )
    def index(self) -> dict:
        """The values of the page of a record (the stylesheet is the template of this
        view): the properties of the shape that may be shown, each under the name the
        shape gives it, and the RDF/XML of the record and what it is made of."""
        record = self.record
        res_type = self.res_type
        shapes, shape = res_type.shacl_shape()
        names = forms.property_names(shapes, shape)
        properties = []
        for role in ("key_info", "description"):
            for path in dict.fromkeys(res_type.property_role_paths(role)):
                if path not in HIDDEN:
                    properties.append(
                        (path, names.get(path, forms.local_name(path)), role)
                    )
        graph = self.request.store.get_multi([record.uri], compound_records=True)
        return XsltRenderer.page_values(
            self.request,
            heading=record.title,
            type_name=res_type.name,
            properties=properties,
            rdfxml=graph.rdfxml(without=HIDDEN),
        )

    @view_config(
        context=IRecord,
        request_method="GET",
        accept="application/json",
        renderer="json",
    )
    def as_json(self) -> dict:
        context = self.record
        subject, resource = _N(context.uri), context.resource

        def values(node):
            found: dict[str, list[str]] = {}
            for quad in context.graph.quads_for_subject(node):
                if quad.predicate.value in HIDDEN or isinstance(
                    quad.object, ox.BlankNode
                ):
                    continue
                found.setdefault(quad.predicate.value, []).append(quad.object.value)
            return {p: sorted(items) for p, items in sorted(found.items())}

        shown = values(subject)
        shown.pop(uri.RESOURCE, None)
        return {
            "uri": context.uri,
            "type": context.__parent__.name,
            "record": shown,
            "resource": values(resource) if resource is not None else {},
            "resource_uri": [
                o.value for o in context.graph.objects(subject, uri.RESOURCE_URI)
            ],
        }

    # ---- what the RDF/XML of a record says of the shapes of the types on its page

    def shape_versions(self, slugs) -> dict[str, str]:
        types = self.context.repository.types
        return {
            slug: types[slug].assets_version for slug in sorted(set(slugs) & set(types))
        }

    def shapes_of(self, slugs) -> list:
        types = self.context.repository.types
        quads = list(assets.record_shape(None, None)[0])
        for slug in sorted(set(slugs) & set(types)):
            content = types[slug].shacl_shape(include_record_shape=False)
            if content is not None:
                quads += list(content[0])
        return quads

    @staticmethod
    def slugs_of(uris) -> set[str]:
        found = set()
        for each in uris:
            parsed = uri.parse_record_uri(each)
            if parsed:
                found.add(parsed[0])
        return found

    @view_config(context=IRecord, request_method="GET", accept="application/rdf+xml")
    def as_rdfxml(self):
        """The record and what its page is made of, as RDF/XML; the etag says when it
        changed, and a browser that has it is answered with `304`."""
        request, record, store = self.request, self.record, self.request.store
        record_id = store.record_id_of(record.uri)
        cached = store.etags([record_id])[record_id] if record_id is not None else None
        if cached is not None:  # one row: no walk through the references
            # the version of the schema of every type: a change of one is a change of
            # all
            # the pages (the types of the page are known when it is made)
            etag = etags.page_etag(
                cached, self.shape_versions(record.repository.types.keys())
            )
            slugs, uris = None, None
        else:
            versions = store.dependencies(record.uri, MAX_DEPTH)
            slugs, uris = self.slugs_of(versions), sorted(versions)
            etag = etags.etag_of(versions, self.shape_versions(slugs))
        response = Response(content_type="application/rdf+xml", charset="utf-8")
        response.etag = etag
        response.cache_control = (
            "private, no-cache"  # always ask, the etag answers (see the module)
        )
        if etag in request.if_none_match:
            raise HTTPNotModified(headers={"ETag": response.headers["ETag"]})
        if uris is None:
            uris = store.page_uris(record.uri)
        if slugs is None:
            slugs = self.slugs_of(uris)
        found = store.get_multi(uris)
        shown = Graph(
            ox.Dataset(
                q
                for q in [*found, *self.shapes_of(slugs)]
                if q.predicate.value not in HIDDEN
            )
        )
        response.text = shown.serialize(
            "rdfxml",
            {**SHAPES, **record.repository.prefix_map()},
            xslt_params={"record_uri": record.uri},
        )
        return response

    @view_config(context=IRecord, name="doc", permission="manage", request_method="GET")
    def doc(self):
        doc = self.request.store.get_doc(self.record.uri)
        if doc is None:
            raise HTTPNotFound("this record has no doc yet (it has no etag)")
        return Response(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
            content_type="application/json",
            charset="utf-8",
            status=500 if "error" in doc else 200,
        )

    # ---- the history: the versions of a record, and going back to one
    # -----------------------

    def require_source(self) -> None:
        """The history is of a record whose data can be edited as text: not a user's."""
        if not self.record.source_editable:
            raise HTTPNotFound()

    def _turtle(self, source: str) -> str:
        if not source:
            return ""
        graph = Graph(
            ox.Dataset(ox.parse(source.encode("utf-8"), format=ox.RdfFormat.N_TRIPLES))
        )
        return graph.serialize("turtle", self.context.repository.prefix_map())

    def _txn(self) -> int:
        try:
            return int(self.request.params["txn"])
        except (KeyError, ValueError):
            raise HTTPBadRequest("a version is a number") from None

    @view_config(
        context=IRecord, name="history", permission="edit", request_method="GET"
    )
    def history(self):
        self.require_source()
        if "txn" in self.request.params:
            return self.version()
        request, record = self.request, self.record
        revisions = self.request.store.revisions(record.uri)
        shown = [
            {
                "txn": txn,
                "when": when.strftime(SHOWN),
                "ago": pretty_modified(when)
                .removeprefix("Last modified ")
                .removeprefix("on "),
                "deleted": deleted,
                "current": index == 0 and not deleted,
                "can_restore": index > 0 and not deleted,
                "view": request.resource_url(record, "history", query={"txn": txn}),
                "restore": request.resource_url(record, "restore"),
            }
            for index, (txn, when, deleted) in enumerate(revisions)
        ]
        static.history_js.need()
        static.manage_js.need()  # the question before a restore (`data-confirm`)
        return XsltRenderer.render_page(
            request,
            HISTORY_TEMPLATE,
            heading=record.title,
            subtitle=record.uri,
            data={"revisions": shown},
            back=request.resource_url(record, "edit"),
        )

    def version(self) -> Response:
        txn = self._txn()
        found = self.request.store.revision(self.record.uri, txn)
        if found is None:
            raise HTTPNotFound()
        when, deleted, source = found
        text = self._turtle(source)
        before = self.request.store.revision_before(self.record.uri, txn)
        diff = None
        if before is not None:
            earlier = self.request.store.revision(self.record.uri, before)
            previous = self._turtle(earlier[2]) if earlier else ""
            diff = "\n".join(
                difflib.unified_diff(
                    previous.splitlines(),
                    text.splitlines(),
                    f"version {before}",
                    f"version {txn}",
                    lineterm="",
                    n=3,
                )
            )
        body = {
            "txn": txn,
            "when": when.strftime(SHOWN),
            "deleted": deleted,
            "source": text,
            "diff": diff,
        }
        response = Response(
            json.dumps(body), content_type="application/json", charset="utf-8"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @view_config(
        context=IRecord, name="restore", permission="edit", request_method="POST"
    )
    def restore(self):
        self.require_source()
        same_origin(self.request)
        record, store = self.record, self.request.store
        found = store.revision(record.uri, self._txn())
        if found is None or found[1]:
            # there is no such version, or it is a record that was deleted
            raise HTTPNotFound()
        when, _deleted, source = found
        record.graph = Graph(
            ox.Dataset(ox.parse(source.encode("utf-8"), format=ox.RdfFormat.N_TRIPLES))
        )
        try:
            record.save()  # a new version, made by the same path as every save
        except DuplicateValue as err:  # a value that was free then and is taken now
            self.request.session.flash(f"It cannot be restored: {err}", queue="danger")
            return HTTPFound(self.request.resource_url(record, "history"))
        self.request.session.flash(
            f"Restored the version of {when.strftime(SHOWN)}.", queue="success"
        )
        return HTTPFound(
            self.request.resource_url(record, "edit", query={"saved": "1"})
        )

    # ---- the two views
    # -------------------------------------------------------------------

    @view_config(context=IRecord, name="edit", permission="edit")
    def edit(self):
        record = self.record
        if isinstance(record.__parent__, Type) and record.__parent__.slug == "type":
            # a type is configured on its manage page, in the Settings tab
            for res_type in record.repository.types.values():
                if res_type.record == record.uri:
                    return HTTPFound(
                        self.request.resource_url(
                            res_type, "manage", query={"tab": "settings"}
                        )
                    )
        return self.respond()

    @view_config(context=IType, name="add", permission="add")
    def add(self):
        problems = self.record.validate_form()
        if problems:  # a record made with this form would be incomplete
            return self.page("", errors=problems, status=501)
        return self.respond()

    # What the buttons of a sequence of blank nodes of the form ask (htmx): the
    # form as it stands, and `sequence` (the path of the field in it), `action`
    # (add or remove) and the `index` of the one to remove. The answer is that
    # sequence.
    @view_config(
        context=IRecord, name="sequence", request_method="POST", permission="edit"
    )
    @view_config(
        context=IType, name="sequence", request_method="POST", permission="add"
    )
    def sequence(self):
        post = self.request.POST
        form = Form(
            self.record.edit_schema(self.request),
            tabs="Record",
            counter=itertools.count(secrets.randbelow(2**31)),  # ids no page has
        )
        try:
            html = change_sequence(
                form,
                post.items(),
                post["sequence"],
                post["action"],
                int(post["index"]) if post.get("index") else None,
            )
        except (KeyError, ValueError):
            raise HTTPBadRequest("not a sequence of this form") from None
        return Response(html, content_type="text/html", charset="utf-8")

    # ---- the page
    # ------------------------------------------------------------------------

    def respond(self):
        if (
            "source_text" in self.request.POST or "source" in self.request.GET
        ) and self.record.source_editable:
            return self.source_page()
        return self.form_page()

    def saved(self) -> HTTPFound:
        query = {"saved": "1"}
        if self.request.params.get("embed") == "1":
            query["embed"] = "1"  # the form is in a dialog: and stays there
        return HTTPFound(self.request.resource_url(self.record, "edit", query=query))

    def page(self, body: str, errors=(), **options):
        if (
            not self.new
        ):  # when it was last changed, and the way to its history (not of a user:
            when = self.request.store.record_modified(
                self.record.uri
            )  # the history has the credentials)
            options.setdefault("modified", pretty_modified(when) if when else None)
            if self.record.source_editable:
                options.setdefault(
                    "history", self.request.resource_url(self.record, "history")
                )
        request = self.request
        if (
            not self.new
            and request.params.get("embed") == "1"
            and "saved" in request.GET
            and request.method == "GET"
        ):
            # what the page around the dialog needs of the record that was saved: the
            # uri it is pointed at by, how it is shown, and where its form is
            record = self.record
            found = record.graph.value(
                ox.NamedNode(record.uri), ox.NamedNode(uri.RESOURCE_URI)
            )
            options.setdefault(
                "announce",
                {
                    "uri": found.value
                    if found is not None
                    else uri.default_resource_uri(record.uri),
                    "label": record.title,
                    "edit": request.resource_url(record, "edit", query={"embed": "1"}),
                    "icon": self.res_type.icon or "",
                    "record": record.uri,
                },
            )
        widgets = options.get("widgets")
        if widgets is not None:
            static.need_form(widgets)  # the scripts and styles that its widgets need
        data: dict = {"errors": list(errors)}
        if options.get("toggle"):
            label, url = options["toggle"]
            data["toggle"] = {"label": label, "url": url}
        if options.get("announce"):
            data["announce"] = options["announce"]
        return XsltRenderer.render_page(
            request,
            FORM_TEMPLATE,
            options.get("status", 200),
            fragments={"form": body},
            data=data,
            heading=self.heading,
            subtitle=self.subtitle,
            saved=options.get("saved", False),
            modified=options.get("modified") or "",
            history=options.get("history") or "",
        )

    # ---- the form
    # ------------------------------------------------------------------------

    def form_page(self):
        request, record = self.request, self.record
        form = Form(record.edit_schema(request), tabs="Record", buttons=("save",))
        errors: list[str] = []
        if "save" in request.POST:
            try:
                data = form.validate(list(request.POST.items()))
            except deform.ValidationFailure as failure:
                rendered = failure.render()
            else:
                errors = record.check(data)
                if not errors:
                    try:
                        self.commit_form(data)
                    except (
                        DuplicateValue
                    ) as err:  # the database refused a value that is taken
                        errors = [str(err)]
                    else:
                        return self.saved()
                rendered = form.render(data)
        else:
            rendered = form.render(record.form_values())
        toggle = None
        if record.source_editable:
            query = {"source": DEFAULT_FORMAT}
            toggle = (
                "Source Edit",
                request.resource_url(self.target, self.view, query=query),
            )
        return self.page(
            rendered,
            errors=errors,
            saved="saved" in request.GET,
            widgets=form,
            toggle=toggle,
        )

    def commit_form(self, data: dict) -> None:
        if self.new:
            self.record.begin(self.request.store.mint_id(self.res_type.slug))
        self.record.apply_form(data)
        self.record.save()

    # ---- the source
    # ----------------------------------------------------------------------

    def source_page(self):
        request = self.request
        prefix_map = self.context.repository.prefix_map()
        errors: list[str] = []
        if "source_text" in request.POST:
            text = request.POST["source_text"]
            was = self.known_format(request.POST.get("source_format_was"))
            chosen = self.known_format(request.POST.get("source_format"))
            try:
                graph = Graph.parse(text, was)
                if "convert" in request.POST:
                    text, was = graph.serialize(chosen, prefix_map), chosen
                else:
                    self.check_source(graph)
                    self.commit_source(graph)
                    return self.saved()
            except (ParseError, SourceError) as err:
                errors = getattr(err, "errors", [str(err)])
            except DuplicateValue as err:
                errors = [str(err)]
            fmt = chosen if "convert" in request.POST and not errors else was
        else:
            fmt = was = self.known_format(request.GET.get("source"))
            start = (
                self.res_type.skeleton()
                if self.new
                else self.record.graph.without(self.record.hidden)
            )
            text = start.serialize(fmt, prefix_map)
        static.source_js.need()
        static.editor_js.need()  # the text is highlighted
        body = fragment(
            request,
            "gracia:assets/templates/source_form.pt",
            action=self.address,
            formats=FORMATS,
            fmt=fmt,
            editor=EDITORS[fmt],
            was=was,
            text=text,
            new=self.new,
            placeholder=self.res_type.placeholder,
        )
        return self.page(body, errors=errors, toggle=("Form Edit", self.address))

    @staticmethod
    def known_format(name: str | None) -> str:
        return name if name in FORMATS else DEFAULT_FORMAT

    def subject(self) -> str:
        return self.res_type.placeholder if self.new else self.record.uri

    def check_source(self, graph) -> None:
        if not self.new:
            graph = self.record.with_hidden_kept(graph)  # what is never shown is kept
        problems = self.record.validate(graph)
        if problems:
            raise SourceError(problems)

    def commit_source(self, graph) -> None:
        record, slug = self.record, self.res_type.slug
        if self.new:
            number = self.request.store.mint_id(slug)  # only now that the text is right
            record.__name__, record.uri = str(number), uri.record_uri(slug, number)
            record.graph = graph.remap(self.subject(), record.uri)
        else:
            record.graph = self.record.with_hidden_kept(graph).copy()
        record.save()


class Manage:
    """A page of tabs: each tab is a pane, the HTML of `pane_<name>(state)`, and a form
    in a pane is handled by `post_<form id>`. The page is made by `manage.xslt`; a view
    for it returns `self.page()` and names that stylesheet as its renderer."""

    scope = ""  # "repository" or "type": the form of a pane is `form-<scope>-<name>`
    panes: tuple[tuple[str, str], ...] = ()  # (name, title) in the order of the strip

    def __init__(self, request):
        self.request = request
        self.context = request.context

    def heading(self) -> str:
        raise NotImplementedError

    def subtitle(self) -> str:
        raise NotImplementedError

    def form_id(self, name: str) -> str:
        return f"form-{self.scope}-{name}"

    def action(self, pane: str) -> str:
        return self.request.resource_url(self.context, "manage", query={"tab": pane})

    def page(self):
        request = self.request
        names = [name for name, _ in self.panes]
        active = request.GET.get("tab")
        states: dict[str, dict] = {}
        if request.method == "POST":
            same_origin(
                request
            )  # it changes data, and the browser sends the credentials itself
            result = self._post(request.POST.get("__formid__", ""))
            if isinstance(result, Response):
                return result
            active = active if active in names else names[0]
            states[active] = result
        if active not in names:
            active = names[0]
        panes = []
        fragments = {}
        for name, title in self.panes:
            panes.append({"name": name, "title": title, "active": name == active})
            fragments[f"pane-{name}"] = getattr(self, f"pane_{name}")(
                states.get(name, {})
            )
        static.jobs_js.need()  # the job panel that a pane may include
        static.manage_js.need()
        return XsltRenderer.page_values(
            request,
            heading=self.heading(),
            subtitle=self.subtitle(),
            data={"panes": panes},
            fragments=fragments,
        )

    def _post(self, form_id: str):
        parts = form_id.split("-")
        for end in range(len(parts), 1, -1):  # the longest name that is a handler
            handler = getattr(self, "post_" + "_".join(parts[1:end]), None)
            if handler is not None:
                return handler("-".join(parts[end:]))
        raise HTTPBadRequest("no such form")


class TypeView(Manage):
    """The views of a type: the page of its records (`index`, and `as_json` for a
    program) and its manage page. A type that wants other pages has a class of its own
    (`PersonType`) and a view class that subclasses this one for it."""

    scope = "type"
    panes = (
        ("settings", "Settings"),
        ("identification", "Identification"),
        ("jobs", "Jobs"),
    )

    @view_config(
        context=IType, name="manage", permission="manage", renderer=MANAGE_TEMPLATE
    )
    def manage(self):
        return self.page()

    def heading(self) -> str:
        return f"Manage {self.context.name}"

    def subtitle(self) -> str:
        return self.context.record

    def _record(self) -> Record:
        res_type = self.context
        return res_type.repository["type"].find_record(
            res_type.record.rsplit(":", 1)[1]
        )

    def _form(self, record: Record) -> Form:
        return Form(
            record.edit_schema(self.request),
            formid=self.form_id("settings"),
            action=self.action("settings"),
            buttons=("save",),
        )

    def pane_settings(self, state):
        record = self._record()
        form = self._form(record)
        static.need_form(form)
        html = state["html"] if "html" in state else form.render(record.form_values())
        errors = "".join(
            f'<div class="alert alert-danger">{escape(e)}</div>'
            for e in state.get("errors", [])
        )
        return errors + html

    # ---- Identification: the numbers of the records of the type
    # ---------------------------------
    # The numbers are given out one after the other, past the highest record there is. A
    # person can
    # reserve a swath of them (before an import of records that come with their own
    # numbers): the
    # next number is then past the number that was set. It cannot be set lower than a
    # number that
    # is in use, only higher; lowering a reservation to what is in use gives it up.

    class IdentificationForm(colander.MappingSchema):
        last = colander.SchemaNode(
            colander.Integer(),
            title="Numbers up to this are taken",
            description=(
                "The next record gets the number after this one. Set it higher to keep "
                "the numbers up to it for records that are going to be imported with "
                "numbers of their own; it cannot be lower than a number that is in use."
            ),
            validator=colander.Range(min=0, max=999_999_999_999_999),
        )

    def _identification_form(self) -> Form:
        return Form(
            self.IdentificationForm(),
            formid=self.form_id("identification"),
            action=self.action("identification"),
            buttons=("save",),
        )

    def pane_identification(self, state):
        numbering = self.request.store.numbering(self.context.slug)
        form = self._identification_form()
        static.need_form(form)
        html = (
            state["html"]
            if "html" in state
            else form.render({"last": numbering["last"]})
        )
        errors = "".join(
            f'<div class="alert alert-danger">{escape(e)}</div>'
            for e in state.get("errors", [])
        )
        facts = (
            f'<dl class="row small text-muted">'
            f'<dt class="col-sm-3">Highest used</dt>'
            f'<dd class="col-sm-9">{numbering["highest"]}</dd>'
            f'<dt class="col-sm-3">Last given out</dt>'
            f'<dd class="col-sm-9">{numbering["given"]}</dd>'
            f'<dt class="col-sm-3">Reserved up to</dt>'
            f'<dd class="col-sm-9">{numbering["reserved"]}</dd>'
            f'<dt class="col-sm-3">Next number</dt>'
            f'<dd class="col-sm-9">{numbering["next"]}</dd>'
            "</dl>"
        )
        return errors + html + facts

    def post_type_identification(self, _rest):
        form = self._identification_form()
        static.need_form(form)
        try:
            data = form.validate(list(self.request.POST.items()))
        except deform.ValidationFailure as failure:
            return {"html": failure.render()}
        try:
            self.request.store.reserve_ids(self.context.slug, data["last"])
        except ValueError as err:
            return {"html": form.render(data), "errors": [str(err)]}
        self.request.session.flash("Saved.", queue="success")
        return HTTPFound(self.action("identification"))

    # The jobs about the records of the type. A button starts one with the fields of its
    # own form
    # (`jobs.js`); the validate job also asks for the schema file to check them against.
    TYPE_JOBS = ("validate", "export", "rewrite", "purge_slugs")

    def pane_jobs(self, state):
        request, res_type = self.request, self.context
        jobs_node = res_type.repository["jobs"]
        items = []
        for job in all_jobs(request.registry):
            if job.name not in self.TYPE_JOBS or not request.has_permission(
                job.start_permission, jobs_node
            ):
                continue
            files = (
                [(f, f) for f in assets.schemas_of(res_type.assets, res_type.slug)]
                if (job.name == "validate" and res_type.assets)
                else []
            )
            if files:
                files.insert(0, ("", "the schema the type is on"))
            items.append(
                {
                    "name": job.name,
                    "title": job.title,
                    "description": job.description,
                    "url": request.resource_url(
                        jobs_node, "start", query={"job": job.name}
                    ),
                    "schemas": files,
                    "fields": "res_type,schema" if files else "res_type",
                }
            )
        items.sort(key=lambda item: self.TYPE_JOBS.index(item["name"]))
        return fragment(
            request,
            "gracia:assets/templates/type_tab_jobs.pt",
            jobs=items,
            res_type=res_type.slug,
            list_url=request.resource_url(jobs_node, "list"),
            can_list=request.has_permission("list_jobs", jobs_node),
        )

    def post_type_settings(self, _rest):
        record = self._record()
        form = self._form(record)
        static.need_form(form)
        try:
            data = form.validate(list(self.request.POST.items()))
        except deform.ValidationFailure as failure:
            return {"html": failure.render()}
        problems = record.check(data)
        if problems:
            return {"html": form.render(data), "errors": problems}
        record.apply_form(data)
        record.save()
        self.request.session.flash("Saved.", queue="success")
        return HTTPFound(self.action("settings"))

    # ---- the records of the type, a page of them at a time

    @view_config(
        context=IType, request_method="GET", accept="text/html", renderer=TYPE_TEMPLATE
    )
    def index(self) -> dict:
        """The values of the page of the records of the type, in the order of `etags`:
        the numbers, the records of the page and their own triples, which have their
        labels and slugs (the stylesheets are `skin/type.xslt`, or the repository's)."""
        request, context, store = self.request, self.context, self.request.store
        published = not request.has_permission("edit", context)
        total_published = store.count_referencing_records(record_type=context.number)
        total_all = total_published
        if not published:
            total_all = store.count_referencing_records(
                record_type=context.number, include_non_public=True
            )
        asked = request.params.get("page")
        try:
            page_number, pages, offset, limit = page_window(
                total_published if published else total_all,
                int(asked) if asked is not None else None,
                PAGE_SIZE,
            )
        except ValueError:  # not a number, or a page that is not there
            raise HTTPNotFound from None
        ids = []
        if limit:
            ids = store.referencing_records(
                record_type=context.number,
                offset=offset,
                limit=limit,
                include_non_public=not published,
            )
        record_uris = []
        for record_id in ids:
            record_uris.append(uri.record_uri(context.slug, record_id % uri.TYPE_BASE))
        return XsltRenderer.page_values(
            request,
            heading=context.name,
            total_published=total_published,
            total_all=total_all,
            records=record_uris,
            paging={
                "page": page_number,
                "pages": pages,
                "total": total_published if published else total_all,
                "size": PAGE_SIZE,
            },
            rdfxml=store.get_multi(ids).rdfxml(without=HIDDEN),
        )

    @view_config(
        context=IType, request_method="GET", accept="application/json", renderer="json"
    )
    def as_json(self) -> dict:
        context = self.context
        prefix = f"rec:{context.slug}:"
        numbers = []
        for key in context.store.records_of_type(context.slug):
            if key.startswith(prefix):
                numbers.append(int(key.removeprefix(prefix)))
        numbers.sort()
        if context.class_uri == uri.TYPE_CLASS:
            # the type records are public, except those of the private types: do not
            # even list them
            numbers = [n for n in numbers if self.reachable(n)]
        return {
            "type": context.name,
            "slug": context.slug,
            "class": context.class_uri,
            "records": numbers,
        }

    def reachable(self, number: int) -> bool:
        try:
            record = self.context.find_record(str(number))
        except (KeyError, RecordDeleted):
            return False
        return bool(self.request.has_permission("view", record))


class TypeRecord(Record):
    """The node of a type record (what describes a type of the repository)."""

    content_type = ContentType(
        name="Type",
        slug="type",
        class_uri=uri.TYPE_CLASS,
        schema="type-minimal-01.ttl",
        system=True,
    )
    # the types define the repository: everybody may read them, only admins change them
    acl: ClassVar[list] = [ADMINS, (Allow, Everyone, "view"), DENY_ALL]


class TypeRecordView(RecordView):
    """The views of a type record: nothing of its own yet (a type is managed on the
    manage page of the type: `TypeView`)."""


# ---- registration: a class of records that declares a type is found by the scan
# (`gracia.scan`), as it implements `IRecord`, and registered by the class uri and by
# the slug of its type. A class with no type of its own can be registered for a class
# with `config.add_record_class(class_uri, cls)`. A record is a `Record` unless a class
# is registered for the class of its type, so the generic views serve every type; a view
# for the class (`@view_config(context=UserRecord, ...)`) overrides the generic one.


def check_declaration(cls: type[Record], declared: ContentType) -> None:
    """A ValueError that says what is wrong with what a class declares of its type."""
    where = cls.__module__
    taken = set()
    for name, slug, _class_uri in init.TYPES_AT_START:
        taken.add(name)
        taken.add(slug)
    if not declared.system and (declared.name in taken or declared.slug in taken):
        raise ValueError(f"{where}: {declared.name}/{declared.slug} is a system type")
    init.type_turtle("rec:type:1", declared)  # checks every field
    try:
        assets.resolve(
            assets.schema_spec(declared.assets, declared.schema, declared.slug)
        )
    except (FileNotFoundError, ValueError) as err:
        raise ValueError(
            f"{where}: the schema {declared.schema} is not a file of it ({err})"
        ) from None
    if declared.doc:
        try:
            name = assets.doc_name(declared.slug, declared.doc)
            assets.resolve(assets.join(declared.assets, assets.DOC_DIR + name))
        except (FileNotFoundError, ValueError) as err:
            raise ValueError(
                f"{where}: the doc template {declared.doc} is not a file of it ({err})"
            ) from None
    if declared.view:
        try:
            assets.resolve(assets.view_spec(declared.assets, declared.view))
        except (FileNotFoundError, ValueError) as err:
            raise ValueError(
                f"{where}: the view {declared.view} is not a file of it ({err})"
            ) from None


def add_gracia_type(config, cls: type[Record]) -> None:
    """Registers a class of records that declares a type (`content_type` in its own
    body: one that only inherits it is another class of the same type, which is not
    registered here); the directive of the scan."""
    declared = vars(cls).get("content_type")
    if declared is not None:
        check_declaration(cls, declared)
        add_record_class(config, declared.class_uri, cls)


def add_gracia_type_node(config, cls: type[Type]) -> None:
    """Registers a subclass of `Type` that names its slug; the directive of the scan."""
    slug = vars(cls).get("slug")
    if not slug:
        return

    def register():
        taken = config.registry.queryUtility(IType, slug)
        if taken is not None and taken is not cls:
            raise ConfigurationError(
                f"the type {slug!r} has the node {taken.__module__}.{taken.__name__} "
                f"and {cls.__module__}.{cls.__name__}"
            )
        config.registry.registerUtility(cls, IType, name=slug)

    config.action(("gracia type node", slug), register)


def add_record_class(config, class_uri: str, cls: type[Record]) -> None:
    declared = vars(cls).get("content_type")

    def register():
        registry = config.registry
        taken = registry.queryUtility(IRecordType, class_uri)
        if taken is not None and taken is not cls:
            raise ConfigurationError(
                f"{class_uri} is the class of {taken.__module__} and of "
                f"{cls.__module__}"
            )
        registry.registerUtility(cls, IRecordType, name=class_uri)
        if declared is not None:
            by_slug = registry.queryUtility(IRecordType, declared.slug)
            if by_slug is not None and by_slug is not cls:
                raise ConfigurationError(
                    f"the slug {declared.slug!r} is the slug of {by_slug.__module__} "
                    f"and of {cls.__module__}"
                )
            registry.registerUtility(cls, IRecordType, name=declared.slug)

    config.action(("record class", class_uri), register)
    if declared is not None:  # two types, one slug: a conflict
        config.action(("gracia type slug", declared.slug), None)
    if cls.edit_permission != Record.edit_permission:
        config.add_view(
            RecordView,
            attr="edit",
            context=cls,
            name="edit",
            permission=cls.edit_permission,
        )
    if cls.add_permission != Record.add_permission:
        config.add_view(
            RecordView,
            attr="add",
            context=IType,
            name="add",
            type_class=class_uri,
            permission=cls.add_permission,
        )


class TypeClassPredicate:
    def __init__(self, value, config):
        self.value = value

    def text(self):
        return f"type_class = {self.value}"

    phash = text

    def __call__(self, context, request):
        return getattr(context, "class_uri", None) == self.value
