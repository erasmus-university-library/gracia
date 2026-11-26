import pyoxigraph as ox
from pyramid.httpexceptions import HTTPNotFound
from pyramid.renderers import render_to_response
from pyramid.view import view_config

from gracia import forms, hints, uri
from gracia.resources import Record, RecordDeleted, Type
from gracia.store import PAGE_SIZE, page_window
from gracia.views import render
from gracia.views.index import index

# never shown, whoever asks and whatever the record says
HIDDEN = frozenset({uri.CREDENTIALS})


def _plain(term):
    return term.value


def _reachable(request, res_type, number) -> bool:
    try:
        record = res_type[str(number)]
    except (KeyError, RecordDeleted):
        return False
    return bool(request.has_permission("view", record))


SAFE_SCHEMES = frozenset(
    {"http", "https", "mailto", "tel"}
)  # a value is a link only to these


def wants_html(request) -> bool:
    accept = request.headers.get("Accept", "")
    return "text/html" in accept and "application/json" not in accept


def page_links(request, context, page: int, pages: int) -> list[dict]:

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
    """The records of a type, in the order of `etags`, a page of them at a time
    (`Cache.referencing_records`)."""
    context, store = request.context, request.store
    published = not request.has_permission("edit", context)
    total_published = store.count_referencing_records(record_type=context.number)
    total_all = (
        total_published
        if published
        else store.count_referencing_records(
            record_type=context.number, include_non_public=True
        )
    )
    asked = request.params.get("page")
    try:
        page, pages, offset, limit = page_window(
            total_published if published else total_all,
            int(asked) if asked is not None else None,
            PAGE_SIZE,
        )
    except ValueError:  # not a number, or a page that is not there
        raise HTTPNotFound from None
    ids = (
        store.referencing_records(
            record_type=context.number,
            offset=offset,
            limit=limit,
            include_non_public=not published,
        )
        if limit
        else []
    )
    # the labels are in the compound graphs of the records of the page: they are in the
    # graph cache, made in one go for those that are not (a placeholder, the page is
    # to be made by XSLT)
    names = store.get_multi(ids, compound_records=True).labels()
    items = []
    for record_id in ids:
        number = str(record_id % uri.TYPE_BASE)
        label, slug = names.get(uri.record_uri(context.slug, int(number)), ("", None))
        items.append(
            {
                "label": label or "(Unnamed)",
                "number": number,
                "url": request.resource_url(context, slug or number),
            }
        )
    if published:
        noun = "record" if total_published == 1 else "records"
        summary = f"{total_published} {noun}"
    else:
        unpublished = total_all - total_published
        summary = f"{total_published} published, {unpublished} not published"
    return render(
        request,
        "gracia:assets/templates/type_index.pt",
        heading=context.name,
        summary=summary,
        items=items,
        page=page,
        pages=pages,
        nav=page_links(request, context, page, pages),
        add_url=request.resource_url(context, "add")
        if request.has_permission("add", context)
        else None,
        manage_url=request.resource_url(context, "manage")
        if request.has_permission("manage", context)
        else None,
    )


def value_link(request, record, term) -> str | None:
    if not isinstance(term, ox.NamedNode):
        return None
    found = record.repository.store.record_with_resource_uri(term.value)
    parsed = uri.parse_record_uri(found) if found else None
    if parsed is not None:
        return request.resource_url(record.repository, parsed[0], str(parsed[1]))
    return term.value if term.value.split(":", 1)[0].lower() in SAFE_SCHEMES else None


def record_page(request):
    record = request.context
    res_type = record.type
    shapes, shape = res_type.shacl_shape()
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
            for path in res_type.property_role_paths("description")
            if (found := values(path))
        ),
        [],
    )
    rows = []
    for path in dict.fromkeys(res_type.property_role_paths("key_info")):
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
        for key in context.store.records_of_type(context.slug)
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
    if "application/json" in request.headers.get("Accept", ""):
        return render_to_response("json", record_values(request), request)
    if wants_html(request):
        return record_page(request)
    return index(request)


def record_values(request) -> dict:
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
