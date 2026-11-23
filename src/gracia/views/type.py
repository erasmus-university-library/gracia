import pyoxigraph as ox
from pyramid.httpexceptions import HTTPNotFound
from pyramid.renderers import render_to_response
from pyramid.view import view_config

from gracia import forms, hints, uri
from gracia.resources import Record, RecordDeleted, Type
from gracia.views import render
from gracia.views.index import index

# never shown, whoever asks and whatever the record says
HIDDEN = frozenset({uri.CREDENTIALS})


def _plain(term):
    return term.value


def _reachable(request, res_type, number) -> bool:
    """Whether the caller may see the record at all."""
    try:
        record = res_type[str(number)]
    except (KeyError, RecordDeleted):
        return False
    return bool(request.has_permission("view", record))


SAFE_SCHEMES = frozenset(
    {"http", "https", "mailto", "tel"}
)  # a value is a link only to these


def wants_html(request) -> bool:
    """A browser: `text/html` in the `Accept` header, and not JSON. A client that names
    nothing (or `*/*`) gets what it always got."""
    accept = request.headers.get("Accept", "")
    return "text/html" in accept and "application/json" not in accept


def page_links(request, context, page: int, pages: int) -> list[dict]:
    """The links under a listing, as dicts of `label`, `url` (None: not a link), `kind`
    (`step`, `page`, `gap`) and `current`: Newer, the first page, the pages near this
    one, the last, Older. Page 1 is the newest records and the pages go back in time."""

    def address(n: int) -> str:
        return request.resource_url(context, query={"page": n})

    near = sorted({1, pages, *range(max(1, page - 2), min(pages, page + 2) + 1)})
    links = [
        {
            "label": "Newer",
            "url": address(page - 1) if page > 1 else None,
            "kind": "step",
            "current": False,
        }
    ]
    before = 0
    for n in near:
        if n - before > 1:
            links.append({"label": "…", "url": None, "kind": "gap", "current": False})
        links.append(
            {"label": str(n), "url": address(n), "kind": "page", "current": n == page}
        )
        before = n
    links.append(
        {
            "label": "Older",
            "url": address(page + 1) if page < pages else None,
            "kind": "step",
            "current": False,
        }
    )
    return links


def type_page(request):
    """The records of the type, a page of them: page 1, the one shown when none is asked
    for, is the newest created, and the pages go back in time. Those who may edit the
    records see the ones that are not published too."""
    context, store = request.context, request.store
    published = not request.has_permission("edit", context)
    asked = request.params.get("page")
    try:
        page = store.records_page(
            context.slug,
            page=int(asked) if asked is not None else None,
            published=published,
        )
    except ValueError:  # not a number, or a page that is not there
        raise HTTPNotFound from None
    found = store.labels([u for u, _ in page.items])
    items = []
    for record_uri, _etag in page.items:
        label, slug = found.get(record_uri, ("", None))
        number = str(record_uri.rsplit(":", 1)[1])
        items.append(
            {
                "label": label or "(Unnamed)",
                "number": number,
                "url": request.resource_url(context, slug or number),
            }
        )
    if published:
        noun = "record" if page.total_published == 1 else "records"
        summary = f"{page.total_published} {noun}"
    else:
        unpublished = page.total_all - page.total_published
        summary = f"{page.total_published} published, {unpublished} not published"
    return render(
        request,
        "gracia:assets/templates/type_index.pt",
        heading=context.name,
        summary=summary,
        items=items,
        page=page.page,
        pages=page.pages,
        nav=page_links(request, context, page.page, page.pages),
        add_url=request.resource_url(context, "add")
        if request.has_permission("add", context)
        else None,
        manage_url=request.resource_url(context, "manage")
        if request.has_permission("manage", context)
        else None,
    )


def value_link(request, record, term) -> str | None:
    """Where a value leads, if it is a place: the record whose resource has this uri (a
    resource points at the resource uri of another record, never at the record), or a
    web, mail or phone address. Nothing else is made a link (a `javascript:` value is
    text)."""
    if not isinstance(term, ox.NamedNode):
        return None
    found = record.repository.store.record_with_resource_uri(term.value)
    parsed = uri.parse_record_uri(found) if found else None
    if parsed is not None:
        return request.resource_url(record.repository, parsed[0], str(parsed[1]))
    return term.value if term.value.split(":", 1)[0].lower() in SAFE_SCHEMES else None


def record_page(request):
    """A record for a person: its label, its type, the description if the shape of the
    type has a property with the role `dash:DescriptionRole` that the record has, and a
    table of the properties with `dash:KeyInfoRole`, each under the name its shape gives
    it (`sh:name`)."""
    record = request.context
    res_type = record.type
    shapes, shape = res_type.record_shape()
    names = hints.names(shapes, shape)

    def values(path: str) -> list[dict]:
        found = [
            q.object
            for q in record.graph
            if q.predicate.value == path and path not in HIDDEN
        ]
        return [
            {"text": term.value, "url": value_link(request, record, term)}
            for term in sorted(found, key=lambda t: t.value)
        ]

    description = next(
        (
            found
            for path in res_type.role_paths("description")
            if (found := values(path))
        ),
        [],
    )
    rows = []
    for path in dict.fromkeys(res_type.role_paths("key_info")):
        if found := values(path):
            rows.append(
                {"name": names.get(path, forms.local_name(path)), "values": found}
            )
    return render(
        request,
        "gracia:assets/templates/record.pt",
        heading=record.title,
        type_name=res_type.name,
        type_url=request.resource_url(res_type),
        uri=record.uri,
        description=[item["text"] for item in description],
        rows=rows,
        edit_url=request.resource_url(record, "edit")
        if request.has_permission("edit", record)
        else None,
    )


@view_config(context=Type, request_method="GET")
def res_type(request):
    if wants_html(request):
        return type_page(request)
    context = request.context
    prefix = f"rec:{context.slug}:"
    numbers = sorted(
        int(key.removeprefix(prefix))
        for key in context.records()
        if key.startswith(prefix)
    )
    if context.class_uri == uri.TYPE_CLASS:
        # the type records are public, except those of the private types: do not even
        # list them
        numbers = [n for n in numbers if _reachable(request, context, n)]
    return render_to_response(
        "json",
        {
            "type": context.name,
            "slug": context.slug,
            "class": context.class_uri,
            "records": numbers,
        },
        request,
    )


@view_config(context=Record, request_method="GET")
def record(request):
    """The index view of a record: RDF/XML (`views.index`); the plain values as JSON for
    whoever names `application/json` in its `Accept`, and a page for a browser."""
    if "application/json" in request.headers.get("Accept", ""):
        return render_to_response("json", record_values(request), request)
    if wants_html(request):
        return record_page(request)
    return index(request)


def record_values(request) -> dict:
    """The record and its content: the plain values of the record and of the blank node
    it holds (nested blank nodes, like a type's shape, are left out)."""
    context = request.context
    subject, resource = ox.NamedNode(context.uri), context.resource

    def values(node):
        found: dict[str, list[str]] = {}
        for quad in context.graph.quads_for_subject(node):
            if quad.predicate.value in HIDDEN or isinstance(quad.object, ox.BlankNode):
                continue
            found.setdefault(quad.predicate.value, []).append(_plain(quad.object))
        return {predicate: sorted(items) for predicate, items in sorted(found.items())}

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
