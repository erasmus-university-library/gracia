"""The repository: the node below the application that holds the types of a repository
(its records, jobs, ...), with the views of it: the page (search), the debug and api
views and the manage page."""

from __future__ import annotations

import datetime
import inspect
import json
import logging
import zoneinfo
from functools import cached_property
from pathlib import Path
from typing import ClassVar

import colander
import deform
import pyoxigraph as ox
from pyramid.authorization import DENY_ALL
from pyramid.events import ContextFound, subscriber
from pyramid.httpexceptions import HTTPBadRequest, HTTPFound, HTTPNotFound
from pyramid.response import Response
from pyramid.threadlocal import get_current_request
from pyramid.view import view_config
from zope.interface import implementer

from gracia import forms, interfaces, predicates, search, static, uri
from gracia.api import document as api_document
from gracia.extensions import ExtensionError, settings_form
from gracia.forms import Form, edit_address, fragment
from gracia.graph import Graph
from gracia.interfaces import IExtension, IRepository, Node, traversable
from gracia.jobs.job import all_jobs
from gracia.security import ADMINS
from gracia.store import new_store
from gracia.templates import XsltRenderer
from gracia.types.type import (
    MANAGE_TEMPLATE,
    ContentType,
    Manage,
    Record,
    RecordView,
    Type,
    record_class,
)
from gracia.utils import assets, dates, passwords, pretty_age, pretty_size, pretty_when
from gracia.utils.validate import validate_userid

_N = ox.NamedNode
_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
# what `gracia repo set` sets: the timezone and the locale of dates
REPOSITORY_SETTINGS = {
    "timezone": uri.TIMEZONE,
    "locale": uri.LOCALE,
    "layout_template": uri.LAYOUT_TEMPLATE,
    "type_template": uri.TYPE_TEMPLATE,
    "record_template": uri.RECORD_TEMPLATE,
}
# the stylesheets of the pages, when the repository has none of its own: the setting
# that
# can name another, and the one that comes with gracia (`templates.py`)
SKINS = {
    "layout": uri.LAYOUT_TEMPLATE,
    "type": uri.TYPE_TEMPLATE,
    "record": uri.RECORD_TEMPLATE,
}
SKIN_SPECS = {  # the stylesheet of gracia for each of them: the template of a view
    "layout": "gracia:assets/xslt/skin/layout.xslt",
    "type": "gracia:assets/xslt/skin/type.xslt",
    "record": "gracia:assets/xslt/view/record-minimal-01.xslt",
}
SKIN_KINDS = {spec: kind for kind, spec in SKIN_SPECS.items()}
SHOWN = 10  # what a typeahead shows


log = logging.getLogger(__name__)


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
    def repository(self) -> Repository:
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

    def __enter__(self) -> Repository:
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

    def record(self) -> Record:
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
        the locale, and the stylesheets of the pages); an empty value clears one (UTC,
        ISO
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

    def shape_types(self, shape_uris) -> list[Type]:
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

    def add_type(self, content_type: ContentType) -> str | None:
        """Adds a type to the repository (its type record, and its class to the list of
        types
        of the repository), unless the repository has a type with its slug: the number
        of its
        record, or None."""
        store, repo_id = self.store, self.__name__
        repo_uri = uri.repository_uri()
        subject = _N(repo_uri)
        resource = store.get(repo_uri)
        if not len(resource):
            raise LookupError(f"repository {repo_id} has no repository record")
        content = resource.value(subject, _N(uri.RESOURCE))
        head = resource.value(content, _N(uri.TYPES)) if content is not None else None
        classes = (
            [p.value for p in resource.walk_list(head)] if head is not None else []
        )
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
        type_record = content_type.turtle(record)
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
                        *(
                            ox.Quad(q.subject, q.predicate, q.object, graph)
                            for q in parsed
                        ),
                    ]
                )
            )
        )
        return record

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
        return own or SKIN_SPECS[kind]

    def template(self, spec: str, context=None) -> str:
        """The stylesheet that makes a view for this repository: `spec` is the template
        of the view (its renderer), and the repository can have another in its place.
        For
        a page of gracia (the `layout`, the `type` and the `record` skins) it is the one
        the settings of the repository name, else, for a record, the view stylesheet its
        type was given (a preset does that: `work-schemaorg-01.xslt` for the works),
        else `spec`; any other is `spec`."""
        kind = SKIN_KINDS.get(spec)
        if kind is None:
            return spec
        own = getattr(getattr(context, "type", None), "view_spec", None)
        return self.stylesheet(kind, own if kind == "record" else None)

    def locale(self) -> str:
        """The locale of dates (a key of `dates.FORMATS`); ISO 8601 if none."""
        name = self._setting(uri.LOCALE)
        return name if name in dates.FORMATS else ""

    @cached_property
    def types(self) -> dict[str, Type]:
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
        found = interfaces.find_child(self, name)
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


@traversable(prefix="rec:")
class Resolved(Node):
    def __init__(self, repository: Repository, key: str):
        super().__init__(repository)
        parsed = uri.parse_record_uri(key)
        if parsed is None or parsed[0] not in repository.types:
            raise KeyError(key)
        self.__name__ = key
        self.record = repository.types[parsed[0]].find_record(str(parsed[1]))


class ResolvedView:
    def __init__(self, request):
        self.request = request
        self.context = request.context

    @view_config(context=Resolved, request_method=("GET", "HEAD"))
    def resolve(self):
        record = self.context.record
        if not self.request.has_permission("view", record):
            raise HTTPNotFound()
        return HTTPFound(self.request.resource_url(record))


@subscriber(ContextFound)
def api_defaults_to_json(event) -> None:
    """The api of a repository is a document for a program: a request that does not
    say what it accepts (no Accept header, or just `*/*`, as a library sends) gets
    JSON. (The event comes after the traversal and before the views are looked up by
    what the request accepts.) The documentation made of it is html, for a browser,
    which asks for text/html."""
    request = event.request
    if (
        request.view_name == "api"
        and isinstance(request.context, Repository)
        and request.headers.get("Accept", "*/*").strip() == "*/*"
    ):
        request.accept = "application/json"


class RepositoryView:
    """The views of a repository: its page (a search for a browser, the counts for a
    program), the debug page and the API; the manage page is `RepositoryManage`."""

    def __init__(self, request):
        self.request = request
        self.context = request.context

    @view_config(context=Repository, request_method="GET", accept="application/json")
    def as_json(self):
        return Response(
            json.dumps({"id": self.context.__name__, **self.request.store.counts()}),
            content_type="application/json",
            charset="utf-8",
        )

    @view_config(context=Repository, request_method="GET", accept="text/html")
    def index(self):
        """The page of a repository: a search of its records."""
        request = self.request
        repository, query = self.context, request.params.get("q", "").strip()
        message, hits, total = "", [], 0
        indexes = request.root.indexes
        definitions = search.definitions_for(repository) if indexes is not None else []
        if indexes is None:
            message = "There is no search index: set gracia.index_dir."
        elif not definitions:
            message = "Search is turned off in this repository."
        elif query:
            try:
                total, found = indexes.find(
                    repository.__name__, definitions[0], query, limit=100
                )
                if indexes.status(repository.__name__, definitions[0]) == "stale":
                    message = (
                        "This index was made for other fields than the settings of the "
                        "repository "
                        "have, so it may"
                        " lack what was changed since: the administrator makes it "
                        "again "
                        "with Rebuild "
                        "search."
                    )
            except LookupError:
                message = (
                    "The search index is not there yet: "
                    "the administrator makes it with Rebuild search."
                )
                found = []
            for hit in self.visible(found):
                slug = hit.get("type", "")
                parsed = uri.parse_record_uri(hit["record"])
                # what `request.resource_url(record)` gives: below its type, by its slug
                # when it has one, else by its number (`Record.__name__`). The slug is a
                # field of the index, so no record has to be read to make the address.
                address = None
                if parsed:
                    address = request.resource_url(
                        repository[slug], hit.get("slug") or str(parsed[1])
                    )
                hits.append(
                    {
                        "label": hit.get("label") or hit["record"],
                        "type": slug,
                        "record": hit["record"],
                        "url": address,
                        "published": hit.get("published", True),
                    }
                )
            hits = hits[:20]
            if query and not hits:
                message = f"Nothing found. {message}".strip()
        response = XsltRenderer.render_page(
            request,
            "gracia:assets/xslt/skin/search.xslt",
            heading=repository.__name__,
            subtitle="Search the records",
            action=request.resource_url(repository),
            query=query,
            data={"hits": hits},
            message=message,
            shown=len(hits),
            total=total,
        )
        response.headers["Cache-Control"] = (
            "private"  # what is found depends on who asks
        )
        response.headers["Vary"] = "Authorization, Cookie, Accept"
        return response

    # ---- what the hits of the search may be, as the people who ask may see them

    def visible(self, hits: list[dict]) -> list[dict]:
        """The hits that the person may see: of a type they may view, and published, or
        of a type they may edit."""
        allowed: dict[str, tuple[bool, bool]] = {}  # per type: may view, may edit
        found = []
        for hit in hits:
            slug = hit.get("type", "")
            if slug not in allowed:
                try:
                    node = self.context[slug]
                    allowed[slug] = (
                        self.request.has_permission("view", node),
                        self.request.has_permission("edit", node),
                    )
                except KeyError:
                    allowed[slug] = (False, False)
            may_view, may_edit = allowed[slug]
            if may_view and (hit.get("published", True) or may_edit):
                found.append(hit)
        return found

    def type_icon(self, slug: str) -> str:
        res_type = self.context.types.get(slug)
        return (res_type.icon or "") if res_type is not None else ""

    def kind_of(self, hit: dict) -> str:
        """What the resource is, as it is shown: the name of its rdf type (the label the
        shape of the type gives it), or the name of the type of the record, if none."""
        res_type = self.context.types.get(hit.get("type", ""))
        given = hit.get("resource_type")
        given = given[0] if isinstance(given, list) else given
        shapes = res_type.shacl_shape(include_record_shape=False) if res_type else None
        if given and shapes:
            return forms.describe(shapes[0], [given])[given]["label"]
        return res_type.name if res_type is not None else ""

    @staticmethod
    def item(hit: dict, edit: str = "", icon: str = "", kind: str = "") -> dict:
        """What the widget of a reference gets of a hit: the uri to point at, the label
        to show for it, what it is and the record it is in, and where its form is (for
        the dialog), if the person may edit it."""
        record = hit["record"]
        return {
            "uri": hit.get("resource_uri") or uri.default_resource_uri(record),
            "label": hit.get("label") or record,
            "type": hit.get("type", ""),
            "kind": kind or hit.get("type", ""),
            "record": record,
            "published": hit.get("published", True),
            "edit": edit,
            "icon": icon,
        }

    def number(self, name: str, default: int, highest: int) -> int:
        try:
            return min(max(int(self.request.params.get(name, default)), 1), highest)
        except ValueError:
            return default

    # The typeahead of a reference (ReferenceWidget): the records whose label begins
    # with
    # the words typed (`q`; none: every record), of the resources of the rdf types in
    # `type`
    # (one parameter for each; none: any), as JSON, a page of `size` of them (`page`,
    # from
    # 1). Only what the person may see is told. Of a great many records only the first
    # LOOKUP_CAP, by label, can be paged through (`truncated`): more words narrow it
    # down.
    @view_config(context=Repository, name="lookup", request_method="GET")
    def lookup(self):
        request, repository = self.request, self.context
        indexes = request.root.indexes
        definitions = search.definitions_for(repository) if indexes is not None else []
        problem = ""
        items: list[dict] = []
        total, pages, page, truncated = 0, 1, 1, False
        size = self.number("size", SHOWN, 50)
        if indexes is None:
            problem = "There is no search index."
        elif not definitions:
            problem = "Search is turned off in this repository."
        else:
            try:
                count, hits = indexes.lookup(
                    repository.__name__,
                    definitions[0],
                    request.params.get("q", ""),
                    request.params.getall("type"),
                )
                seen = self.visible(hits)
                total = len(seen)
                truncated = count > len(hits)
                pages = max(1, -(-total // size))
                page = min(self.number("page", 1, pages), pages)
                items = [
                    self.item(
                        hit,
                        edit_address(
                            request, repository, hit["record"], hit.get("slug")
                        ),
                        self.type_icon(hit.get("type", "")),
                        self.kind_of(hit),
                    )
                    for hit in seen[(page - 1) * size : page * size]
                ]
            except LookupError as problem_:
                problem = str(problem_)
        response = Response(
            json.dumps(
                {
                    "items": items,
                    "problem": problem,
                    "page": page,
                    "pages": pages,
                    "size": size,
                    "total": total,
                    "truncated": truncated,
                }
            ),
            content_type="application/json",
            charset="utf-8",
            status=503 if problem else 200,
        )
        response.headers["Cache-Control"] = (
            "private"  # what is found depends on who asks
        )
        response.headers["Vary"] = "Authorization, Cookie"
        return response

    @view_config(
        context=Repository,
        name="debug",
        request_method="GET",
        permission="manage",
        renderer="gracia:assets/xslt/debug.xslt",
    )
    def debug(self):
        request = self.request
        """A view made by a stylesheet (`templates.py`), to try them: it is given the
        RDF/XML of the record of the repository."""
        rdfxml = request.store.get_multi(
            [request.context.uri], compound_records=True, format="rdfxml"
        )
        return {"rdfxml": rdfxml, "csrf": True, "heading": "Debug"}

    @view_config(
        context=Repository,
        name="api",
        renderer="json",
        request_method="GET",
        accept="application/json",
    )
    def api(self):
        request = self.request
        # what a caller may see depends on who they are: never share it through a public
        # cache
        request.response.headers["Cache-Control"] = "private"
        request.response.headers["Vary"] = "Authorization, Cookie, Accept"
        return api_document(request)

    @view_config(
        context=Repository, name="api", request_method="GET", accept="text/html"
    )
    def api_browser(self):
        request = self.request
        document = api_document(request)
        operations = []
        for path, methods in document["paths"].items():
            for method, found in methods.items():
                operations.append(
                    {
                        "path": path,
                        "method": method.upper(),
                        "summary": found.get("summary", ""),
                        "permission": found.get("x-gracia-permission", ""),
                    }
                )
        static.api_browser_js.need()
        response = XsltRenderer.render_page(
            request,
            "gracia:assets/xslt/skin/api.xslt",
            heading=document["info"]["title"],
            subtitle="What you may do here. The same document is JSON for a program.",
            data={"operations": operations},
            base=request.resource_url(request.context).rstrip(
                "/"
            ),  # the paths of operations follow it
            document_url=request.resource_url(request.context, "api"),
        )
        response.headers["Cache-Control"] = "private"
        response.headers["Vary"] = "Authorization, Cookie, Accept"
        return response


class RepositoryManage(Manage):
    scope = "repository"
    panes = (
        ("types", "Types"),
        ("settings", "Settings"),
        ("extensions", "Extensions"),
        ("search", "Search"),
        ("predicates", "Predicates"),
        ("stats", "Stats"),
        ("jobs", "Jobs"),
    )

    @view_config(
        context=Repository,
        name="manage",
        permission="manage",
        renderer=MANAGE_TEMPLATE,
    )
    def manage(self):
        return self.page()

    def heading(self) -> str:
        return f"Manage {self.context.__name__}"

    def subtitle(self) -> str:
        return self.context.uri

    # ---- Types
    # ------------------------------------------------------------------------------

    def _type_rows(self) -> tuple[list[dict], dict]:
        """A row for each type that is installed (and for one that has records and is
        not): what it is, how many records it has, their size and when one last changed;
        the totals of all."""
        request, repository = self.request, self.context
        installed = {res_type.slug: res_type for res_type in repository.types.values()}
        found = request.store.type_stats()
        empty = {"records": 0, "published": 0, "bytes": 0, "modified": None}
        rows = []
        for slug in [*installed, *(slug for slug in found if slug not in installed)]:
            stats, res_type = found.get(slug, empty), installed.get(slug)
            shape = (
                res_type.shacl_shape(include_record_shape=False) if res_type else None
            )
            rows.append(
                {
                    "name": res_type.name if res_type else slug,
                    "slug": slug,
                    # what the shape of the type says of itself (the schema file it is
                    # on)
                    "description": forms.shape_description(*shape) if shape else "",
                    "system": bool(
                        res_type and res_type.class_uri in uri.SYSTEM_CLASSES
                    ),
                    "url": (
                        request.resource_url(res_type, "manage")
                        if res_type and request.has_permission("manage", res_type)
                        else None
                    ),
                    "records": f"{stats['records']:,}",
                    "published": f"{stats['published']:,}",
                    "size": pretty_size(stats["bytes"]),
                    "average": pretty_size(stats["bytes"] / stats["records"])
                    if stats["records"]
                    else "",
                    "modified": pretty_when(stats["modified"]),
                }
            )
        totals = {
            "records": f"{sum(s['records'] for s in found.values()):,}",
            "size": pretty_size(sum(s["bytes"] for s in found.values())),
            "modified": pretty_when(
                max(
                    (s["modified"] for s in found.values() if s["modified"]),
                    default=None,
                )
            ),
        }
        return rows, totals

    def pane_types(self, state):
        types, totals = self._type_rows()
        return fragment(
            self.request,
            "gracia:assets/templates/repository_tab_types.pt",
            types=types,
            totals=totals,
        )

    # ---- Settings
    # ---------------------------------------------------------------------------

    def _settings_form(self) -> tuple[Form, colander.Schema]:
        schema = colander.Schema()
        shape = self.context.record().type.shacl_shape(include_record_shape=False)
        if shape is None:
            raise LookupError(f"repository {self.context.__name__} has no shape")
        for node in forms.schema_from_shape(*shape).children:
            code = predicates.find(
                self.context.registry, node.predicate
            )  # another widget, a rule: what a predicate adds
            changed = (
                code.schema_node(None, self.request, node) if code is not None else node
            )
            if changed is not None:
                changed.predicate = node.predicate
                schema.add(changed)
        form = Form(
            schema,
            formid=self.form_id("settings"),
            action=self.action("settings"),
            buttons=("save",),
        )
        return form, schema

    def pane_settings(self, state):
        form, schema = self._settings_form()
        static.need_form(form)
        if "html" in state:  # the form as it was posted, with what was wrong
            return state["html"]
        record = self.context.record()
        current = {
            n.name: next(
                (o.value for o in record.graph.objects(record.resource, n.predicate)),
                "",
            )
            for n in schema.children
        }
        return form.render(current)

    def post_repository_settings(self, _rest):
        repository = self.context
        form, schema = self._settings_form()
        static.need_form(form)
        try:
            data = form.validate(list(self.request.POST.items()))
        except deform.ValidationFailure as failure:
            return {"html": failure.render()}
        record = repository.record()
        subject, resource = record.resource, record.graph
        for node in schema.children:
            predicate = ox.NamedNode(node.predicate)
            for quad in [
                q
                for q in resource.quads_for_subject(subject)
                if q.predicate == predicate
            ]:
                resource.discard(quad)
            if data[node.name]:
                resource.add(
                    ox.Quad(
                        subject,
                        predicate,
                        ox.Literal(data[node.name]),
                        ox.DefaultGraph(),
                    )
                )
        record.save()  # the label of the repository follows its title
        self.request.session.flash("Saved.", queue="success")
        return HTTPFound(self.action("settings"))

    # ---- Extensions
    # -------------------------------------------------------------------------
    # A card for every extension there is: on or off, and a dialog with its settings.
    # The dialog
    # has two tabs, the Deform form of the extension and the same settings as JSON to
    # copy or
    # paste. Each of its forms names itself `form-extension-<name>`, and what a button
    # does is the
    # name of the button.

    def _extension_form(self, extension):
        return settings_form(
            extension,
            self.request,
            formid=f"form-extension-{extension.name}",
            action=self.action("extensions"),
            buttons=(deform.Button(name="update", title="Update settings"),),
        )

    def pane_extensions(self, state):
        cards = []
        for extension in self.context.extensions():
            name = extension.name
            settings = extension.settings_schema is not None and extension.enabled
            asked = state.get("open") == name
            form_html = ""
            if settings:
                form = self._extension_form(extension)
                static.need_form(form)
                form_html = (
                    state["html"]
                    if asked and "html" in state
                    else form.render(extension.settings)
                )
            cards.append(
                {
                    "name": name,
                    "form_id": f"form-extension-{name}",
                    "title": extension.title or name,
                    "description": extension.description,
                    "enabled": extension.enabled,
                    "can_disable": extension.can_disable,
                    "settings": settings,
                    "url": self.action("extensions"),
                    "form_html": form_html,
                    "json": (state.get("json") if asked and "json" in state else None)
                    or (extension.export() if settings else ""),
                    "open": asked,
                    "inner": state.get("inner", "settings") if asked else "settings",
                    "errors": state.get("errors", []) if asked else [],
                }
            )
        if any(card["settings"] for card in cards):
            static.editor_js.need()  # the settings as JSON are an editor
        return fragment(
            self.request,
            "gracia:assets/templates/repository_tab_extensions.pt",
            cards=cards,
        )

    def post_extension(self, name):
        request = self.request
        try:
            extension = self.context.extension(name)
        except KeyError:
            raise HTTPNotFound(f"there is no extension {name!r}") from None
        title = extension.title or name
        try:
            if "enable" in request.POST:
                extension.enable()
                request.session.flash(f"{title} is enabled.", queue="success")
            elif "disable" in request.POST:
                extension.disable()
                request.session.flash(
                    f"{title} is disabled. Its settings are kept.", queue="success"
                )
            elif "update" in request.POST:
                form = self._extension_form(extension)
                try:
                    data = form.validate(list(request.POST.items()))
                except deform.ValidationFailure as failure:
                    static.need_form(form)
                    return {"open": name, "inner": "settings", "html": failure.render()}
                extension.save_settings(data)
                request.session.flash(
                    f"The settings of {title} are updated.", queue="success"
                )
            elif "import_json" in request.POST:
                extension.import_json(request.POST.get("json", ""))
                request.session.flash(
                    f"The settings of {title} are updated from JSON.", queue="success"
                )
            else:
                raise HTTPBadRequest("nothing to do")
        except ExtensionError as err:
            return {
                "open": name,
                "inner": "json" if "import_json" in request.POST else "settings",
                "errors": err.errors,
                "json": request.POST.get("json"),
            }
        return HTTPFound(self.action("extensions"))

    # ---- Search
    # -----------------------------------------------------------------------------
    # The fields the docs can have (the schema mode of the doc stylesheets of the types)
    # against the
    # fields of
    # the indexes (the settings of the Search extension). A field the docs have and the
    # index lacks
    # is shown
    # greyed, with a button to add it to the settings; the button of the others takes it
    # out.
    # Changing the
    # settings is all it does: an index that is not what they say is stale, and the job
    # Rebuild
    # search makes it.

    def pane_search(self, state):
        request, repository = self.request, self.context
        application = request.root
        indexes = application.indexes
        notice = None
        try:
            extension = repository.extension("search")
        except KeyError:
            extension = None
        if indexes is None:
            notice = "No search index: set gracia.index_dir."
        elif extension is None or not extension.enabled:
            notice = "Search is turned off in this repository (the Search extension)."
        sections, problem = [], None
        if notice is None:
            try:
                offered = application.cache.docs.schema(
                    repository, list(repository.types)
                )
            except (
                Exception
            ) as err:  # a stylesheet that does not work is a message, not a failed page
                offered, problem = (
                    [],
                    f"The doc stylesheets could not make the schema: {err}",
                )
            records_total = sum(request.store.count_records_by_type().values())
            for definition in search.definitions_for(repository):
                rows = search.compare(offered, definition)
                counts = indexes.field_stats(repository.__name__, definition)
                for row in rows:
                    row["docs"] = (
                        f"{counts[row['name']]:,}"
                        if counts.get(row["name"]) is not None
                        else None
                    )
                    row["measured"] = row["name"] in counts
                    row["every_type"] = bool(row["types"]) and set(row["types"]) == set(
                        repository.types
                    )
                stats = indexes.stats(repository.__name__, [definition])
                documents = stats[0]["documents"]
                sections.append(
                    {
                        "name": definition.name,
                        "state": indexes.status(repository.__name__, definition),
                        "documents": f"{documents:,}",
                        # of the records there are: what the index lacks is what is not
                        # in it yet
                        "of": f"{records_total:,}",
                        "size": pretty_size(stats[0]["bytes"]),
                        "rows": rows,
                    }
                )
        jobs_node = repository["jobs"]
        job = next(
            (j for j in all_jobs(request.registry) if j.name == "rebuild_search"), None
        )
        rebuild = (
            {
                "title": job.title,
                "url": request.resource_url(
                    jobs_node, "start", query={"job": job.name}
                ),
            }
            if job and request.has_permission(job.start_permission, jobs_node)
            else None
        )
        return fragment(
            request,
            "gracia:assets/templates/repository_tab_search.pt",
            notice=notice,
            problem=problem,
            errors=state.get("errors", []),
            sections=sections,
            rebuild=rebuild,
            action=self.action("search"),
            list_url=request.resource_url(jobs_node, "list"),
            can_list=request.has_permission("list_jobs", jobs_node),
        )

    def post_repository_search(self, _rest):
        request, repository = self.request, self.context
        post = request.POST
        index = post.get("index", "")
        try:
            extension = repository.extension("search")
            if "add" in post:
                offered = request.root.cache.docs.schema(
                    repository, list(repository.types)
                )
                field = next((f for f in offered if f["name"] == post["add"]), None)
                if field is None:
                    raise ExtensionError([f"no doc has a field {post['add']!r}"])
                extension.add_field(index, field)
                request.session.flash(
                    f"{post['add']} is in the index {index}: "
                    "the index needs a rebuild.",
                    queue="success",
                )
            elif "remove" in post:
                extension.remove_field(index, post["remove"])
                request.session.flash(
                    f"{post['remove']} is out of the index {index}: "
                    "the index needs a rebuild.",
                    queue="success",
                )
            else:
                raise HTTPBadRequest("nothing to do")
        except KeyError:
            raise HTTPNotFound("this repository has no Search extension") from None
        except ExtensionError as err:
            return {"errors": err.errors}
        return HTTPFound(self.action("search"))

    # ---- Predicates
    # -------------------------------------------------------------------------

    def _titles(self) -> dict[str, tuple[str, str]]:
        found: dict[str, tuple[str, str]] = {}

        def walk(dataset, shape, above=()):
            for path, prop in forms.properties(dataset, shape):
                found.setdefault(
                    path,
                    (
                        forms.property_title(dataset, prop, ""),
                        forms.shape_description(dataset, prop),
                    ),
                )
                nested = forms.nested_shape(dataset, prop)
                if nested is not None and nested not in (*above, shape):
                    walk(dataset, nested, (*above, shape))

        for res_type in self.context.types.values():
            walk(*res_type.shacl_shape())
        return found

    def pane_predicates(self, state):
        used = (
            self.request.store.count_by_predicate()
        )  # the values there are of each property
        titles = self._titles()
        cards = []
        for code in predicates.all_predicates(self.context.registry):
            title, description = titles.get(code.uri, ("", ""))
            cards.append(
                {
                    "uri": code.uri,
                    "title": title or code.uri.rsplit("/", 1)[-1].rsplit("#", 1)[-1],  # ty: ignore[missing-union-attribute]
                    "description": description,
                    "name": f"{type(code).__module__}.{type(code).__qualname__}",
                    # the class's own docstring (getdoc would take the base class's),
                    # without markup
                    "doc": inspect.cleandoc(type(code).__doc__ or "").replace("`", ""),
                    "values": used.get(code.uri, 0),
                    "parts": [
                        part
                        for part in ("schema_node", "on_write_sql")
                        if getattr(type(code), part)
                        is not getattr(predicates.Predicate, part)
                    ],
                }
            )
        cards.sort(key=lambda card: card["uri"])  # ty: ignore[no-matching-overload]
        return fragment(
            self.request,
            "gracia:assets/templates/repository_tab_predicates.pt",
            cards=cards,
        )

    # ---- Stats
    # ------------------------------------------------------------------------------
    # What there is: the types, the caches, and what waits for the worker that follows
    # the writes.
    # The
    # buttons start the jobs of the Jobs tab, whose panel (it is on the page) follows
    # them.
    def pane_stats(self, state):
        request, repository = self.request, self.context
        store, application = request.store, request.root
        total = sum(s["records"] for s in store.type_stats().values())
        cache = application.cache
        pages = store.etag_stats()
        kept = cache.repo_stats(repository.__name__)
        cache_stats = {
            "pages": pages["pages"],
            "missing": max(0, total - pages["pages"]),  # records that have no etag yet
            "pages_size": pretty_size(pages["bytes"]),
            "modified": pretty_when(pages["modified"]),
            "written": pretty_when(kept["modified_at"]),
            "age": pretty_age(kept["modified_at"]),
            "kept": [
                {
                    "name": row["name"],
                    "kept": "entries" in row,  # no directory: the graphs and docs
                    "entries": f"{row.get('entries', 0):,}"
                    + ("+" if row.get("capped") else ""),
                    "stale": f"{row.get('stale', 0):,}" if row.get("stale") else "",
                    "bytes": pretty_size(row.get("bytes", 0)),
                }
                for row in kept["caches"]
            ],
        }
        pending = store.pending_summary()
        worker = cache.worker
        change_events = {
            "worker": self._worker_line(worker, repository.__name__),
            "rows": pending["rows"],
            "records": pending["records"],
            "size": pretty_size(pending["bytes"]),
            "by_kind": ", ".join(
                f"{n:,} {kind}" for kind, n in sorted(pending["change_events"].items())
            ),
            "since": pretty_when(pending["since"]),
            "oldest": pending["oldest"],
            "last": pending["last"],
        }
        jobs_node = repository["jobs"]
        buttons = {
            job.name: {
                "title": job.title,
                "url": request.resource_url(
                    jobs_node, "start", query={"job": job.name}
                ),
            }
            for job in all_jobs(request.registry)
            if job.name in ("update_cache", "process_change_events")
            and request.has_permission(job.start_permission, jobs_node)
        }
        return fragment(
            request,
            "gracia:assets/templates/repository_tab_stats.pt",
            cache=cache_stats,
            change_events=change_events,
            buttons=buttons,
            refresh_url=self.action("stats"),
            list_url=request.resource_url(jobs_node, "list"),
            can_list=request.has_permission("list_jobs", jobs_node),
        )

    @staticmethod
    def _worker_line(worker, repo: str) -> dict:
        if worker is None:
            return {
                "state": "off",
                "text": "Off: the setting gracia.worker is not on.",
                "ok": False,
            }
        status = worker.status(repo)
        if not status["running"]:
            text = (
                "Stopped."
                if status["stopped"]
                else "Not running. The next request starts it again."
            )
            return {"state": "stopped", "text": text, "ok": False}
        if status["failures"]:
            retry = (
                f", the next try in {status['retry_in']:.0f} s"
                if status["retry_in"] is not None
                else ""
            )
            return {
                "state": "failing",
                "text": (
                    f"Running, but this repository failed {status['failures']} "
                    f"time(s) in a row{retry}."
                ),
                "ok": False,
            }
        text = (
            "Running; this repository is waiting for its turn."
            if status["in_line"]
            else "Running; nothing to do here."
        )
        return {"state": "running", "text": text, "ok": True}

    # ---- Jobs
    # -------------------------------------------------------------------------------
    # The jobs about the repository as a whole; one is started with its button
    # (`jobs.js`).
    REPOSITORY_JOBS = (
        "rebuild_etags",
        "update_cache",
        "rebuild_search",
        "process_change_events",
    )

    def pane_jobs(self, state):
        request, repository = self.request, self.context
        jobs_node = repository["jobs"]
        items = [
            {
                "name": job.name,
                "title": job.title,
                "description": job.description,
                "url": request.resource_url(
                    jobs_node, "start", query={"job": job.name}
                ),
            }
            for job in all_jobs(request.registry)
            if job.name in self.REPOSITORY_JOBS
            and request.has_permission(job.start_permission, jobs_node)
        ]
        items.sort(key=lambda item: self.REPOSITORY_JOBS.index(item["name"]))
        return fragment(
            request,
            "gracia:assets/templates/repository_tab_jobs.pt",
            jobs=items,
            job_filter=",".join(
                self.REPOSITORY_JOBS
            ),  # the list shows the jobs of all of them
            list_url=request.resource_url(jobs_node, "list"),
            can_list=request.has_permission("list_jobs", jobs_node),
        )


class RepositoryRecord(Record):
    content_type = ContentType(
        name="Repository",
        slug="repository",
        class_uri=uri.REPOSITORY_CLASS,
        schema="repository-minimal-01.ttl",
        system=True,
    )
    acl: ClassVar[list] = [ADMINS, DENY_ALL]
    private = True
    edit_permission = add_permission = "manage"

    def cannot_be_added(self) -> list[str]:
        return ["A repository has one record, and it was made with the repository."]


class RepositoryRecordView(RecordView):
    """The views of the record of a repository: nothing of its own yet."""
